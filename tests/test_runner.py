import json
import os
import signal
import subprocess
import sys
import time
import uuid

import pytest

from hihive import HiveError
from hihive.diagnostics import redact
from hihive.protocol import ClaudeStreamProtocol, LiveProtocol, PiRpcProtocol, protocol_for
from hihive.runner import GUARDIAN, LiveRunner
from conftest import FIXTURE, TESTS, alive, touch, wait_until


class Harness:
    """Drives a LiveRunner against the fake harness and records its events."""

    def __init__(self, directory):
        self.dir = directory
        self.events = []
        self.runner = None

    def wait(self, condition, timeout=8):
        def check():
            if self.runner:
                self.events.extend(self.runner.drain())
            if condition():
                if self.runner:
                    self.events.extend(self.runner.drain())
                return True
            return False

        return wait_until(check, timeout, describe=lambda: repr(self.events[-20:]))

    def launch(self, kind, scenario=None, idle_timeout=60, receipt_timeout=30, queued_inputs=(),
               input_interval=0.02, idle_probe_timeout=0.1):
        self.session = str(uuid.uuid4())
        args = [FIXTURE, "--fake-kind", kind, "--fake-root", self.dir]
        if scenario:
            args += ["--fake-case", scenario]
        protocol = protocol_for(kind)
        if kind == "pi":
            protocol.input_interval = input_interval
        else:
            protocol.idle_probe_timeout = idle_probe_timeout
        self.runner = LiveRunner(protocol=protocol, command=sys.executable, args=args,
                                 session_id=self.session, chdir=self.dir, idle_timeout=idle_timeout,
                                 receipt_timeout=receipt_timeout, task_id="test-task", harness=kind,
                                 diagnostics_path=self.path("diagnostics.jsonl"))
        self.base = self.runner.submit("BASE")
        self.queued = [self.runner.submit(text) for text in queued_inputs]
        self.runner.start()
        self.wait(lambda: os.path.exists(self.path("started")))

    def path(self, name):
        return os.path.join(self.dir, name)

    def receipt(self, message_id):
        return next((data for kind, data in reversed(self.events)
                     if kind == "delivery" and data["id"] == message_id), None)

    def accepted(self, message_id):
        receipt = self.receipt(message_id)
        return bool(receipt and receipt.get("harness_accepted"))

    def settled(self, message_id):
        receipt = self.receipt(message_id)
        return bool(receipt and receipt.get("settled"))

    def of(self, kind):
        return [data for event_kind, data in self.events if event_kind == kind]

    def release(self):
        touch(self.dir, "release")

    def inputs(self):
        with open(self.path("inputs")) as file:
            return [json.loads(line) for line in file]

    def diagnostics(self):
        with open(self.path("diagnostics.jsonl")) as file:
            return [json.loads(line) for line in file]

    def protocol_event(self, event):
        with self.runner._lock:
            self.runner.protocol.event(self.runner, event)


@pytest.fixture
def h(test_dir):
    harness = Harness(test_dir)
    yield harness
    if harness.runner:
        # Let the fake harness finish blocked work before its stdin is closed.
        # Tests that exercise cancellation perform and assert it themselves.
        harness.release()
        harness.runner.close("test cleanup")
        harness.runner.join()


@pytest.mark.parametrize("kind", ["pi", "claude-code"])
def test_two_steers_and_idle_follow_up(h, kind):
    h.launch(kind, scenario="stderr")
    first = h.runner.submit("MARKER_ONE")
    second = h.runner.submit("MARKER_TWO")
    h.wait(lambda: h.accepted(first["id"]) and h.accepted(second["id"]))
    assert h.of("output") == []
    pid = h.runner.pid
    h.release()
    h.wait(lambda: h.runner.state == "idle")
    output = h.of("output")
    assert len(output) == 1
    assert "MARKER_ONE" in output[0]["content"]
    assert "MARKER_TWO" in output[0]["content"]
    if kind == "pi":
        assert output[0]["input_ids"] is None
        assert h.receipt(first["id"])["status"] == "accepted"
    else:
        assert first["id"] in output[0]["input_ids"]
        assert h.receipt(first["id"])["status"] == "included"
    follow_up = h.runner.submit("FOLLOW_UP")
    h.wait(lambda: h.settled(follow_up["id"]) and h.runner.state == "idle")
    assert h.runner.pid == pid
    assert len(h.of("output")) == 2
    assert all(session == h.session for session in h.of("session"))
    assert h.runner.submit("FOLLOW_UP", id=follow_up["id"])["id"] == follow_up["id"]
    assert sum(1 for event in h.inputs() if (event.get("id") or event.get("uuid")) == follow_up["id"]) == 1


def test_pi_spaces_startup_and_idle_bursts_in_order(h):
    h.launch("pi", queued_inputs=["STARTUP_ONE", "STARTUP_TWO"], input_interval=0.15)
    h.release()
    # Completion probes must still work while later inputs wait in Hive.
    h.wait(lambda: h.settled(h.base["id"]))
    assert not h.receipt(h.queued[-1]["id"]).get("written")
    assert h.runner.state == "active"
    h.wait(lambda: h.runner.state == "idle")
    later = [h.runner.submit(text) for text in ("IDLE_ONE", "IDLE_TWO")]
    h.wait(lambda: h.runner.state == "idle" and h.settled(later[-1]["id"]))
    ids = [m["id"] for m in [h.base, *h.queued, *later]]
    with open(h.path("input_times")) as file:
        times = [entry for entry in map(json.loads, file) if entry["id"] in ids]
    assert [entry["id"] for entry in times] == ids
    # Allow small pipe-reader scheduling jitter with the shortened test interval.
    for a, b in zip(times, times[1:]):
        assert b["at"] - a["at"] >= 0.12
    assert all(h.receipt(i)["harness_accepted"] and h.receipt(i)["settled"] for i in ids)


def test_pi_default_spacing_is_observed_by_real_writes(h):
    h.launch("pi", queued_inputs=["NEXT"], input_interval=PiRpcProtocol.input_interval)
    h.wait(lambda: h.accepted(h.queued[0]["id"]))
    ids = {h.base["id"], h.queued[0]["id"]}
    with open(h.path("input_times")) as file:
        times = [entry["at"] for entry in map(json.loads, file) if entry["id"] in ids]
    assert len(times) == 2
    assert times[1] - times[0] >= 0.95


def test_pi_cancellation_does_not_flush_messages_waiting_for_spacing(h):
    h.launch("pi", queued_inputs=["MUST_STAY_QUEUED"], input_interval=1.0)
    h.runner.close("Cancelled while queued")
    h.wait(lambda: h.runner.state == "exited")
    assert h.receipt(h.queued[0]["id"])["status"] == "failed"
    with open(h.path("inputs")) as file:
        assert "MUST_STAY_QUEUED" not in file.read()


def test_input_spacing_is_pi_only():
    assert PiRpcProtocol().input_interval == 1.0
    assert ClaudeStreamProtocol().input_interval == 0
    assert ClaudeStreamProtocol().idle_probe_timeout == 5.0


def test_claude_does_not_settle_at_result_with_outstanding_input_ids(h):
    h.launch("claude-code", scenario="split-result")
    a = h.runner.submit("ONE")
    b = h.runner.submit("TWO")
    h.wait(lambda: h.accepted(b["id"]))
    h.release()
    h.wait(lambda: h.runner.state == "idle")
    assert len(h.of("output")) == 2
    assert h.receipt(a["id"])["settled"]
    assert h.receipt(b["id"])["settled"]
    assert len(h.of("settled")) == 1


def test_result_is_displayed_once(h):
    h.launch("claude-code", scenario="duplicate-result")
    h.release()
    h.wait(lambda: h.runner.state == "idle")
    assert len(h.of("output")) == 1


def test_claude_unknown_events_are_diagnostics_only(h):
    h.launch("claude-code", scenario="unknown-event")
    h.release()
    h.wait(lambda: h.runner.state == "idle")
    assert not h.of("warning") and not h.of("error")
    records = h.diagnostics()
    assert any(record["kind"] == "unknown_event" for record in records)
    assert "PRIVATE_PROMPT" not in json.dumps(records)


def test_claude_background_result_publishes_once_without_settling_inputs(h):
    h.launch("claude-code")
    h.wait(lambda: h.accepted(h.base["id"]))
    protocol = ClaudeStreamProtocol()
    event = {"type": "result", "uuid": "background-result", "origin": {"kind": "task-notification"},
             "result": "Background finished"}
    protocol.event(h.runner, event)
    protocol.event(h.runner, event)
    h.events.extend(h.runner.drain())
    outputs = h.of("output")
    assert [output["content"] for output in outputs] == ["Background finished"]
    assert outputs[0]["input_ids"] is None
    assert not h.receipt(h.base["id"])["settled"]
    assert h.runner.state == "active"
    h.release()
    h.wait(lambda: h.runner.state == "idle")
    assert len(h.of("output")) == 2

    protocol.event(h.runner, {**event, "uuid": "empty", "result": ""})
    protocol.event(h.runner, {**event, "uuid": "failed", "result": "", "is_error": True, "errors": ["background failure"]})
    h.events.extend(h.runner.drain())
    assert len(h.of("output")) == 2
    assert any("Background task failed" in warning for warning in h.of("warning"))
    assert h.runner.state == "idle"


def test_claude_normal_results_still_require_associations():
    with pytest.raises(HiveError, match="plural input association"):
        ClaudeStreamProtocol().event(object(), {"type": "result", "uuid": "result", "result": "text"})


def test_assistant_text_contract_rejects_malformed_blocks_but_allows_extra_fields():
    protocol = LiveProtocol()
    assert protocol.text([{"type": "text", "text": "hello", "future": True}]) == "hello"
    assert protocol.text([{"type": "future_block"}]) == ""
    with pytest.raises(HiveError):
        protocol.text([{"type": "text", "text": 42}])
    with pytest.raises(HiveError):
        protocol.text(42)


def test_missing_capability_fails_closed_and_is_visible(h):
    h.launch("claude-code", scenario="no-capability")
    h.wait(lambda: h.runner.state == "exited")
    assert any("msg_lifecycle_v1" in message for message in h.of("error"))
    assert h.receipt(h.base["id"])["status"] == "unknown"


def _reaped(pid):
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def test_crash_marks_inputs_unknown_and_reaps(h):
    h.launch("pi")
    h.runner.submit("CRASH")
    pid = h.runner.pid
    h.wait(lambda: h.runner.state == "exited")
    assert h.receipt(h.base["id"])["status"] == "unknown"
    assert h.of("error")
    _reaped(pid)


def test_cancellation_and_idle_shutdown(h):
    h.launch("pi")
    pid = h.runner.pid
    with open(h.path("pid")) as file:
        child = int(file.read())
    h.runner.close("Cancelled")
    h.wait(lambda: h.runner.state == "exited")
    _reaped(pid)
    assert not alive(child)
    assert h.receipt(h.base["id"])["status"] == "unknown"


def test_background_activity_events_track_work_reply_and_exit(h):
    h.launch("claude-code", scenario="background-activity")
    h.release()
    h.wait(lambda: h.runner.state == "idle" and h.runner.background_activity()["task_count"] == 2)
    assert h.runner.background_activity() == {"task_count": 2, "preparing_reply": False}
    touch(h.dir, "background-finished")
    h.wait(lambda: h.runner.background_activity()["preparing_reply"])
    assert h.runner.background_activity() == {"task_count": 0, "preparing_reply": True}
    touch(h.dir, "background-reply")
    h.wait(lambda: any(output["content"] == "Background finished" for output in h.of("output")))
    assert h.runner.background_activity() == {"task_count": 0, "preparing_reply": False}
    activities = h.of("background_activity")
    assert {"task_count": 2, "preparing_reply": False} in activities
    assert {"task_count": 0, "preparing_reply": True} in activities
    assert activities[-1] == {"task_count": 0, "preparing_reply": False}


def test_stopped_runner_does_not_report_stale_background_work(h):
    h.launch("claude-code", scenario="background-activity")
    h.release()
    h.wait(lambda: h.runner.background_activity()["task_count"] == 2)
    h.runner.close("Explicit stop")
    h.wait(lambda: h.runner.state == "exited")
    assert h.runner.background_activity() == {"task_count": 0, "preparing_reply": False}


def test_claude_idle_shutdown_queries_state(h):
    h.launch("claude-code", idle_timeout=0.1)
    h.release()
    h.wait(lambda: h.runner.state == "exited")
    assert any((event.get("request") or {}).get("subtype") == "initialize" for event in h.inputs())
    assert not h.of("error")


def test_background_work_and_completion_generation_prevent_idle_shutdown(h):
    h.launch("claude-code", idle_timeout=0.1)
    h.protocol_event({"type": "system", "subtype": "background_tasks_changed",
                      "tasks": [{"task_id": "monitor", "task_type": "local_bash"}]})
    h.release()
    h.wait(lambda: h.runner.state == "idle")
    time.sleep(0.2)
    assert h.runner.state == "idle", "Background work must outlive the idle timeout"
    # Last task ends, but Claude has not started its completion turn yet.
    h.protocol_event({"type": "system", "subtype": "background_tasks_changed", "tasks": []})
    time.sleep(0.02)
    assert h.runner.state == "idle", "Task completion must start a fresh grace period"
    h.protocol_event({"type": "system", "subtype": "init", "capabilities": ["msg_lifecycle_v1"]})
    time.sleep(0.2)
    assert h.runner.state == "idle", "An active completion turn must not be interrupted"
    h.protocol_event({"type": "result", "uuid": "completion", "origin": {"kind": "task-notification"}, "result": "Done"})
    time.sleep(0.02)
    assert h.runner.state == "idle", "Completion reply must start a fresh grace period"
    h.wait(lambda: h.runner.state == "exited")
    assert any(output["content"] == "Done" for output in h.of("output"))
    assert not h.of("error")


def test_idle_probe_failure_keeps_process_alive_and_warns_once(h):
    h.launch("claude-code", scenario="lost-idle-probe", idle_timeout=0.05)
    h.release()
    h.wait(lambda: any("Automatic idle shutdown is disabled" in warning for warning in h.of("warning")))
    assert h.runner.state == "idle"
    time.sleep(0.1)
    h.events.extend(h.runner.drain())
    assert len(h.of("warning")) == 1
    h.runner.close("Explicit stop still works")
    h.wait(lambda: h.runner.state == "exited")


def test_idle_runner_shutdown(h):
    h.launch("pi", idle_timeout=0.05)
    h.release()
    h.wait(lambda: h.runner.state == "exited")
    assert not h.of("error")


def test_rejected_message_and_settlement_race(h):
    h.launch("pi")
    reject = h.runner.submit("REJECT")
    h.wait(lambda: h.settled(reject["id"]))
    assert h.receipt(reject["id"])["status"] == "rejected"
    h.release()
    h.wait(lambda: h.runner.state == "idle")
    # Same API is race-safe if the process has already become idle.
    late = h.runner.submit("LATE_STEER")
    h.wait(lambda: h.settled(late["id"]))
    sent = next(event for event in h.inputs() if event.get("id") == late["id"])
    assert sent["type"] == "prompt"
    assert sent["streamingBehavior"] == "steer"


def test_claude_expected_system_events_on_initial_turn_and_follow_up(h):
    h.launch("claude-code", scenario="claude-notices")
    h.wait(lambda: len(h.of("warning")) == 2)
    h.release()
    h.wait(lambda: h.runner.state == "idle")
    follow_up = h.runner.submit("FOLLOW_UP")
    h.wait(lambda: h.settled(follow_up["id"]))
    assert h.of("warning") == ["Harness information", "Harness switched model"] * 2
    assert not h.of("error")
    records = h.diagnostics()
    assert not any(record["kind"] == "unknown_event" for record in records)
    assert sum(1 for record in records if record["kind"] == "harness_notice") == 4


def test_unknown_event_is_visible_and_diagnostics_do_not_log_secrets(h):
    h.launch("pi", scenario="unknown-event")
    h.wait(lambda: h.of("warning"))
    h.release()
    h.wait(lambda: h.runner.state == "idle")
    records = h.diagnostics()
    unknown = next(record for record in records if record["kind"] == "unknown_event")
    assert unknown["task_id"] == "test-task"
    assert unknown["session_id"] == h.session
    assert unknown["binary_version"] == "0.87.1"
    assert unknown["event_type"] == "future_protocol_event"
    assert unknown["context"]["sha256"]
    assert "sk-secret-test" not in json.dumps(records)
    assert "PRIVATE_PROMPT" not in json.dumps(records)
    assert any(record.get("hive_message_id") == h.base["id"] for record in h.of("diagnostic"))


def test_malformed_jsonl_is_reported_without_raw_sensitive_data(h):
    h.launch("pi", scenario="malformed")
    h.wait(lambda: h.runner.state == "exited")
    assert any("Malformed JSONL" in message for message in h.of("error"))
    with open(h.path("diagnostics.jsonl")) as file:
        diagnostics = file.read()
    assert "malformed_jsonl" in diagnostics
    assert "sk-secret-test" not in diagnostics
    assert "PRIVATE_PROMPT" not in diagnostics


def test_lost_acknowledgement_times_out_and_is_unknown_not_resent(h):
    h.launch("pi", receipt_timeout=0.3)
    sent = h.runner.submit("NO_ACK")
    h.wait(lambda: h.runner.state == "exited")
    assert h.receipt(sent["id"])["status"] == "unknown"
    assert any("receipt deadline" in message for message in h.of("error"))
    assert sum(1 for event in h.inputs() if event.get("id") == sent["id"]) == 1


def test_closed_stdin_and_stdout_are_reported_and_reaped(h):
    # Use the normal receipt deadline so Pi input pacing cannot trigger a
    # receipt timeout before the next write exposes the closed stdin.
    h.launch("pi")
    sent = h.runner.submit("CLOSE_STDIN")
    h.runner.submit("AFTER_STDIN_CLOSED")
    h.wait(lambda: h.runner.state == "exited")
    assert h.receipt(sent["id"])["status"] == "unknown"
    assert h.of("error")
    assert not any("receipt deadline" in message for message in h.of("error"))
    assert any(event.get("message") == "CLOSE_STDIN" for event in h.inputs())
    assert not any(event.get("message") == "AFTER_STDIN_CLOSED" for event in h.inputs())
    assert any(record["kind"] in ("stdout_eof", "child_exit") for record in h.diagnostics())


def test_stdout_eof_does_not_leave_a_hanging_child(h):
    h.launch("pi", scenario="stdout-eof")
    h.wait(lambda: h.runner.state == "exited")
    assert any(record["kind"] == "stdout_eof" for record in h.of("diagnostic"))
    assert h.of("error")


def test_normal_stdout_eof_and_clean_exit_after_result(h):
    h.launch("claude-code", scenario="clean-exit")
    h.release()
    h.wait(lambda: h.runner.state == "exited")
    assert h.receipt(h.base["id"])["settled"]
    assert len(h.of("output")) == 1
    assert any(data["channel"] == "stdout" for data in h.of("channel_closed"))
    assert not h.of("error")


def test_guardian_relay_failure_terminates_and_reaps_child(test_dir):
    argv = [sys.executable, FIXTURE, "--fake-kind", "pi", "--fake-root", test_dir, "--session-id", str(uuid.uuid4())]
    guardian = subprocess.Popen([sys.executable, "-I", GUARDIAN, json.dumps(argv)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        guardian.stdout.close()  # The guardian's output consumer disappears while the child lives.
        guardian.stdin.write((json.dumps({"type": "prompt", "id": str(uuid.uuid4()), "message": "BASE"}) + "\n").encode())
        guardian.stdin.flush()
        assert guardian.wait(5) == 74, "Guardian must not hang after a relay failure"
        assert b"HIVE_GUARDIAN_RELAY_FAILURE" in guardian.stderr.read()
        with open(os.path.join(test_dir, "pid")) as file:
            assert not alive(int(file.read()))
    finally:
        guardian.kill()
        guardian.wait()


def test_server_sigkill_closes_guardian_lease_and_reaps_harness(test_dir):
    env = {**os.environ, "PYTHONPATH": str(TESTS.parent / "src")}
    owner = subprocess.Popen([sys.executable, str(TESTS / "fixtures" / "runner_owner.py"), test_dir], env=env)
    try:
        wait_until(lambda: os.path.exists(os.path.join(test_dir, "started")))
        with open(os.path.join(test_dir, "pid")) as file:
            child = int(file.read())
        owner.send_signal(signal.SIGKILL)
        owner.wait()
        wait_until(lambda: not alive(child))
    finally:
        owner.kill()
        owner.wait()


def test_diagnostic_write_failure_stops_delivery_but_does_not_hide_exit(h):
    h.launch("pi")
    path = h.path("diagnostics.jsonl")
    os.remove(path)
    os.mkdir(path)
    sent = h.runner.submit("MUST_NOT_BE_SENT")
    h.wait(lambda: h.runner.state == "exited")
    assert h.receipt(sent["id"])["status"] == "failed"
    assert any("Cannot persist runner diagnostics" in message for message in h.of("error"))
    with open(h.path("inputs")) as file:
        assert "MUST_NOT_BE_SENT" not in file.read()


def test_unsupported_binary_version_is_a_visible_startup_error(h):
    h.runner = LiveRunner(protocol=PiRpcProtocol(), command=sys.executable,
                          args=[FIXTURE, "--fake-root", h.dir, "--fake-case", "old-version"],
                          session_id=str(uuid.uuid4()), chdir=h.dir)
    base = h.runner.submit("DO_NOT_RUN")
    h.runner.start()
    h.wait(lambda: h.runner.state == "exited")
    assert any("requires 0.87.1" in message for message in h.of("error"))
    assert h.receipt(base["id"])["status"] == "failed"
    assert not os.path.exists(h.path("pid"))


def test_errors_redact_credentials_and_full_input_text():
    prompt = "PRIVATE_PROMPT\nSECOND_LINE"
    redacted = redact(f"Bearer secret sk-secret api_key=SECRET {prompt}", prompts=[prompt])
    for secret in ("PRIVATE_PROMPT", "SECOND_LINE", "sk-secret", "=SECRET"):
        assert secret not in redacted


@pytest.mark.parametrize("phase", ["version", "session"])
def test_subprocess_failure_excerpt_is_visible_but_not_journaled(h, phase):
    script = h.path("failing.py")
    with open(script, "w") as file:
        file.write(
            "import sys\n"
            f"if '--version' in sys.argv and {phase != 'version'!r}:\n"
            "    print('0.87.1')\n"
            "    sys.exit(0)\n"
            "print('old noise\\n' * 20, file=sys.stderr)\n"
            "print('Bridge failed: api_key=secret-value BASE', file=sys.stderr)\n"
            "sys.exit(1)\n"
        )
    h.runner = LiveRunner(protocol=PiRpcProtocol(), command=sys.executable,
                          args=[script], session_id=str(uuid.uuid4()), chdir=h.dir,
                          diagnostics_path=h.path("diagnostics.jsonl"))
    h.runner.submit("BASE")
    h.runner.start()
    h.wait(lambda: h.runner.state == "exited")
    error = "\n".join(h.of("error"))
    assert "Bridge failed: api_key=[credential omitted] [input omitted]" in error
    assert "secret-value" not in error
    assert error.count("old noise") < 20
    assert len(error) < 1000
    journal = json.dumps(h.diagnostics())
    assert "Bridge failed" not in journal
    assert "secret-value" not in journal
    assert "sha256" in journal


def test_version_and_conflicting_flags():
    with pytest.raises(HiveError):
        PiRpcProtocol().check_version("0.86.0")
    with pytest.raises(HiveError):
        ClaudeStreamProtocol().check_version("2.1.2")
    # Newer versions, including new major versions, are accepted.
    PiRpcProtocol().check_version("0.87.1")
    PiRpcProtocol().check_version("1.5.2")
    ClaudeStreamProtocol().check_version("2.1.325 (Claude Code)")
    ClaudeStreamProtocol().check_version("3.0.0")
    with pytest.raises(HiveError):
        PiRpcProtocol().validate_args(["--mode=json"])
    with pytest.raises(HiveError):
        ClaudeStreamProtocol().validate_args(["--resume", "other"])
