import json
import os
import subprocess
import sys
import webbrowser

import pytest

from hihive import cli as cli_module
from hihive import paths, service
from hihive.cli import HiHiveCLI, Exit


def run_with_closed_stdout(code):
    process = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process.stdout.close()  # Close the read end before the child can flush its buffered output.
    stderr = process.stderr.read()
    return process.wait(timeout=10), stderr.decode()


@pytest.mark.parametrize('expression', ["'short output'", "'x' * 200000"])
def test_cli_closed_pipe_is_quiet_for_buffered_and_large_output(expression):
    code = ('from hihive.cli import main, HiHiveCLI\n'
            f'HiHiveCLI.run = lambda self, argv: print({expression})\n'
            'main([])\n')
    status, stderr = run_with_closed_stdout(code)
    assert status == 0
    assert stderr == ''


def test_show_followup_closed_pipe_is_quiet():
    code = ('from hihive.cli import main, HiHiveCLI\n'
            'HiHiveCLI.resolve_task_id = lambda self, value: value\n'
            'HiHiveCLI.get = lambda self, path: {"messages": ['
            '{"role": "user", "kind": "follow_up", "content": "hello"}, '
            '{"role": "assistant", "kind": "follow_up", "content": "answer"}]}\n'
            'main(["show", "task", "--followup", "--no-header"])\n')
    status, stderr = run_with_closed_stdout(code)
    assert status == 0
    assert stderr == ''


def test_cli_closed_pipe_does_not_mask_an_existing_command_failure():
    code = ('from hihive.cli import main, HiHiveCLI, Exit\n'
            'def run(self, argv):\n'
            '    print("buffered output")\n'
            '    raise Exit(3)\n'
            'HiHiveCLI.run = run\n'
            'main([])\n')
    status, stderr = run_with_closed_stdout(code)
    assert status == 3
    assert stderr == ''


def test_cli_preserves_non_pipe_flush_errors():
    code = ('import sys\n'
            'from hihive.cli import main, HiHiveCLI\n'
            'class BadOutput:\n'
            '    def __init__(self): self.failed = False\n'
            '    def flush(self):\n'
            '        if not self.failed:\n'
            '            self.failed = True\n'
            '            raise OSError(5, "output failure")\n'
            'HiHiveCLI.run = lambda self, argv: None\n'
            'sys.stdout = BadOutput()\n'
            'main([])\n')
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert 'output failure' in result.stderr


def test_pager_early_exit_stays_quiet_and_restores_stdout():
    code = ('import sys\n'
            'from hihive.cli import with_pager\n'
            'original = sys.stdout\n'
            'with_pager(lambda: print("x" * 200000))\n'
            'assert sys.stdout is original\n'
            'print("after pager")\n')
    result = subprocess.run([sys.executable, '-c', code], env={**os.environ, 'PAGER': 'exit 0'},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert result.stdout == 'after pager\n'
    assert result.stderr == ''


class FakeCLI(HiHiveCLI):
    def __init__(self, snapshots):
        super().__init__()
        self.snapshots = list(snapshots)
        self.posts = []

    def resolve_task_id(self, prefix):
        return "task-id"

    def get(self, path):
        if not self.snapshots:
            raise AssertionError("No more task snapshots")
        return self.snapshots.pop(0)

    def post(self, path, body=None):
        self.posts.append((path, body))
        return {"message": {"id": "input-id"}}

    def sleep(self, seconds):
        pass


@pytest.mark.parametrize("kind", ["initial", "follow_up"])
@pytest.mark.parametrize("delivery", ["pending", "accepted", "delivered", "included"])
def test_send_during_initial_or_follow_up_work_including_legacy_pending_messages(capsys, kind, delivery):
    cli = FakeCLI([{"status": "running", "messages": [
        {"role": "user", "kind": kind, "delivery": delivery, "settled": False}]}])
    cli.cmd_followup(["task-id", "more context"])
    assert cli.posts == [("/api/tasks/task-id/chat", {"message": "more context"})]
    assert "Message accepted" in capsys.readouterr().out


def test_cancel_requests_stop_of_the_whole_run_without_sending_a_message(capsys):
    cli = FakeCLI([])
    cli.cmd_followup(["task-id", "--cancel"])
    assert cli.posts == [("/api/tasks/task-id/chat/cancel", {"msg_id": None})]
    assert "Requested stop of the active conversation run" in capsys.readouterr().out


def test_label_resolves_the_task_and_prints_the_cleaned_label(capsys):
    class LabelCLI(FakeCLI):
        def post(self, path, body=None):
            self.posts.append((path, body))
            return {"ok": True, "label": "New label"}

    cli = LabelCLI([])
    cli.run(["label", "task", "New", "label"])

    assert cli.posts == [("/api/tasks/task-id/label", {"label": "New label"})]
    assert capsys.readouterr().out == "Changed label for task-id: New label\n"


def test_label_requires_an_id_and_nonempty_label(capsys):
    with pytest.raises(Exit):
        FakeCLI([]).run(["label", "task-id"])
    assert 'Usage: hihive label <id> "new label"' in capsys.readouterr().err


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_wait_stops_on_failure_or_cancellation_even_before_delivery_settles(capsys, status):
    cli = FakeCLI([{"messages": []}, {"status": status, "messages": [
        {"role": "user", "id": "input-id", "delivery": "accepted", "settled": False}]}])
    with pytest.raises(Exit) as failure:
        cli.cmd_followup(["task-id", "hello", "--wait"])
    assert failure.value.code == 1
    assert f"Task {status}" in capsys.readouterr().err


def test_wait_keeps_waiting_when_its_input_settles_but_other_work_continues(capsys):
    cli = FakeCLI([
        {"messages": []},
        {"status": "running", "messages": [
            {"role": "user", "id": "input-id", "delivery": "included", "settled": True},
            {"role": "assistant", "id": "early", "content": "early answer"}]},
        {"status": "completed", "messages": [
            {"role": "user", "id": "input-id", "delivery": "included", "settled": True},
            {"role": "assistant", "id": "final", "content": "final shared answer"}]},
    ])
    cli.cmd_followup(["task-id", "hello", "--wait"])
    assert capsys.readouterr().out == "final shared answer\n"


def test_wait_ignores_other_inputs_and_prints_shared_final_answer(capsys):
    cli = FakeCLI([
        {"messages": []},
        {"status": "running", "messages": [
            {"role": "user", "id": "input-id", "delivery": "accepted", "settled": False},
            {"role": "user", "id": "another-input", "content": "not an answer"}]},
        {"status": "completed", "messages": [
            {"role": "user", "id": "input-id", "delivery": "included", "settled": True},
            {"role": "assistant", "id": "answer", "content": "shared final answer"}]},
    ])
    cli.cmd_followup(["task-id", "hello", "--wait"])
    assert capsys.readouterr().out == "shared final answer\n"


def test_wait_reports_unknown_delivery_instead_of_hanging(capsys):
    cli = FakeCLI([{"messages": []}, {"status": "failed", "messages": [
        {"role": "user", "id": "input-id", "delivery": "unknown", "error": "Transport lost"}]}])
    with pytest.raises(Exit):
        cli.cmd_followup(["task-id", "hello", "--wait"])
    assert "Message unknown: Transport lost" in capsys.readouterr().err


MESSAGES = [
    {"role": "user", "kind": "initial", "content": "PROMPT"},
    {"role": "user", "kind": "steering", "content": "STEER"},
    {"role": "assistant", "kind": "initial", "content": "FIRST_ANSWER"},
    {"role": "user", "kind": "review", "content": "CHECK", "review_id": "r1"},
    {"role": "assistant", "kind": "review", "content": "REVIEW_ANSWER", "review_id": "r1"},
    {"role": "user", "kind": "follow_up", "content": "FOLLOW_UP"},
    {"role": "assistant", "kind": "follow_up", "content": "FOLLOW_ANSWER"},
]


def show(capsys, *flags, messages=MESSAGES):
    FakeCLI([{"status": "completed", "messages": messages}]).cmd_show(["task-id", *flags, "--no-header"])
    return capsys.readouterr().out


def test_show_modes_select_explicit_kinds_and_full_prints_each_message_once(capsys):
    assert show(capsys) == "FIRST_ANSWER\n"
    review = show(capsys, "--review")
    assert "[review question] CHECK" in review
    assert "[review answer] REVIEW_ANSWER" in review
    assert "FIRST_ANSWER" not in review
    follow = show(capsys, "--followup")
    assert follow.index("STEER") < follow.index("FIRST_ANSWER")
    full = show(capsys, "--full")
    for message in MESSAGES:
        assert full.count(message["content"]) == 1
    assert "[review question] CHECK" in full
    assert "[review answer] REVIEW_ANSWER" in full


def test_open_prints_ssh_tunnel_and_login_url_in_headless_linux(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HIVE_HOME", str(tmp_path))
    monkeypatch.setenv("SSH_CONNECTION", "client server")
    monkeypatch.delenv("HIVE_TOKEN", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(cli_module.sys, "platform", "linux")
    monkeypatch.setattr(webbrowser, "open", lambda _url: pytest.fail("headless Linux must not try a browser"))
    paths.ensure_token()
    paths.server_lock_file().write_text("4789\n")
    monkeypatch.setattr(HiHiveCLI, "probe_health", lambda self: True)

    HiHiveCLI().cmd_open()

    output = capsys.readouterr().out
    assert "ssh -N -L 4789:127.0.0.1:4789 <user>@<hi-hive-host>" in output
    assert "http://localhost:4789/login#token=" in output
    assert "grants full hi-hive access" in output
    assert "do not paste it into logs or issues" in output


def test_service_installed_machine_query_uses_exit_status_only(monkeypatch, capsys):
    monkeypatch.setattr(service, "installed", lambda: True)
    cli_module.main(["service", "installed"])
    assert capsys.readouterr().out == ""

    monkeypatch.setattr(service, "installed", lambda: False)
    with pytest.raises(SystemExit) as missing:
        cli_module.main(["service", "installed"])
    assert missing.value.code == 3
    assert capsys.readouterr().out == ""


def test_open_not_running_prints_start_choices(monkeypatch, capsys):
    monkeypatch.delenv("HIVE_TOKEN", raising=False)
    monkeypatch.setattr(HiHiveCLI, "probe_health", lambda self: False)
    client = HiHiveCLI()
    client.token = "token"

    with pytest.raises(Exit):
        client.cmd_open()

    error = capsys.readouterr().err
    assert "hi-hive is not running" in error
    assert "hihive serve" in error
    assert "hihive service install" in error


def test_open_desktop_reports_success_without_printing_token(monkeypatch, capsys):
    monkeypatch.delenv("HIVE_TOKEN", raising=False)
    client = HiHiveCLI()
    client.token = "SECRET_TOKEN"
    monkeypatch.setattr(client, "probe_health", lambda: True)
    monkeypatch.setattr(webbrowser, "open", lambda _url: True)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")

    client.cmd_open()

    output = capsys.readouterr().out
    assert "Opened http://localhost:" in output
    assert "SECRET_TOKEN" not in output


def test_start_with_no_coding_agent_gives_onboarding_guidance(capsys):
    client = FakeCLI([])
    client._project = {"id": "project", "name": "My project", "harnesses": [], "templates": []}

    with pytest.raises(Exit):
        client.cmd_start(["-p", "hello"])

    error = capsys.readouterr().err
    assert "No harness is set up yet" in error
    assert "Claude Code 2.1.283+" in error
    assert "Pi 0.87.1+" in error


def test_first_serve_detects_agents_before_start_and_prints_terminal_guidance(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HIVE_HOME", str(tmp_path))
    monkeypatch.delenv("HIVE_TOKEN", raising=False)
    calls = []

    def setup(args):
        calls.append(args)
        paths.ensure_config(paths.config_file())

    monkeypatch.setattr(cli_module, "setup_coding_agents", setup)
    monkeypatch.setattr("uvicorn.run", lambda *_args, **_kwargs: None)

    cli_module.serve(["--port", "4789"])

    assert calls == [["--fresh", "--plain"]]
    output = capsys.readouterr().out
    assert "hi-hive is running on http://localhost:4789" in output
    assert "Closing it or pressing Ctrl-C stops hi-hive" in output
    assert "hihive open" in output
    assert "hihive service install" in output


def test_version_includes_recorded_main_commit(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HIVE_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    paths.installer_state_dir().mkdir(parents=True)
    paths.installer_record_file().write_text(json.dumps({"commit": "a" * 40}))

    HiHiveCLI().run(["--version"])

    output = capsys.readouterr().out
    assert "hi-hive 0.2.0" in output
    assert f"main@{'a' * 40}" in output


def test_version_omits_untrusted_record(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HIVE_HOME", raising=False)
    canonical = tmp_path / ".hihive"
    canonical.mkdir(mode=0o700)
    record = canonical / "install.json"
    record.write_text(json.dumps({"commit": "a" * 40}))
    record.chmod(0o666)
    HiHiveCLI().run(["--version"])
    assert "main@" not in capsys.readouterr().out


def test_show_prints_only_roles_and_content_not_delivery_or_kind(capsys):
    messages = [
        {"role": "user", "kind": "initial", "content": "Hello", "delivery": "unknown", "error": "Internal delivery detail"},
        {"role": "assistant", "kind": "initial", "content": "Hi"},
        {"role": "user", "kind": "follow_up", "content": "Thanks", "delivery": "accepted"},
    ]
    assert show(capsys, "--full", messages=messages) == "[user] Hello\n\n[assistant] Hi\n\n[user] Thanks\n\n"
