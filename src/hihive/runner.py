"""One persistent harness process per task conversation.

One owner thread does all pipe I/O. Public methods only enqueue under the lock.
Events are consumed by the pool, never callbacks into its lock.
"""

import json
import os
import queue
import select
import signal
import subprocess
import sys
import threading
import time
import uuid

from . import HiveError
from .diagnostics import RunnerDiagnostics, redact, utc_timestamp
from .protocol import IDLE_BACKGROUND, find_version

GUARDIAN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guardian.py")
MAX_MESSAGE = 262_144
MAX_PENDING = 100
PI_COMPACTION_ALLOWANCE = 300


def clock():
    return time.monotonic()


def valid_message(content):
    return isinstance(content, str) and content.strip() != "" and len(content.encode()) <= MAX_MESSAGE


def child_env(overrides):
    """The process environment with overrides applied; None removes a variable."""
    env = dict(os.environ)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


class _Exited(Exception):
    """Internal: the child exited and both channels are closed."""


class LiveRunner:
    def __init__(self, protocol, command, args, session_id, chdir, env=None, resume=False, parent=None,
                 idle_timeout=60, receipt_timeout=30, task_id=None, harness=None, diagnostics_path=None):
        self.protocol, self.command, self.args = protocol, command, list(args)
        self.session_id, self.chdir, self.env = session_id, chdir, env or {}
        self.resume, self.parent, self.idle_timeout = resume, parent, idle_timeout
        self.receipt_timeout = receipt_timeout
        self.pid = None
        self.activity_version = 0
        self._stop_id = str(uuid.uuid4())
        self._receipt_deadlines = {}
        self._compaction_caps = {}
        self._compaction_input = None
        self._compacting = False
        self._lock = threading.Lock()
        self._events = queue.SimpleQueue()
        self._messages = {}
        self._controls = []
        self._state = "starting"
        self._diagnostics = RunnerDiagnostics(task_id=task_id, session_id=session_id,
                                              harness=harness or type(protocol).__name__, path=diagnostics_path)
        self._phase = "starting"
        self._ready = False
        self._initial_only = False
        self._seen_results = set()
        self._probes = {}
        self._answer = ""
        self._stderr = b""
        self._version_failure_output = b""
        self._run_error = None
        self._close_reason = None
        self._unexpected_eof = False
        self._stdout_closed_at = None
        self._last_event = None
        self._thread = None
        self._process = None
        self.touch_activity()

    # --- Public API (any thread) ---

    def start(self):
        self._thread = threading.Thread(target=self._run, name=f"runner-{self.session_id}", daemon=True)
        self._thread.start()
        return self

    def submit(self, content, id=None):
        message_id = id or str(uuid.uuid4())
        with self._lock:
            if message_id in self._messages:
                if self._messages[message_id]["content"] != content:
                    raise HiveError("Message ID already used for different text")
                return dict(self._messages[message_id])
            if self._state in ("closing", "exited"):
                raise HiveError("Runner is closing; retry after it exits")
            if not valid_message(content):
                raise HiveError("Message must contain text and be at most 256 KiB")
            if len(self._outstanding()) >= MAX_PENDING:
                raise HiveError("Too many outstanding inputs")
            message = {"id": message_id, "sequence": len(self._messages) + 1, "content": content,
                       "status": "accepted", "harness_accepted": False, "settled": False}
            self._messages[message_id] = message
            if self._state != "starting":
                self._state = "active"
            self.touch_activity()
            self._emit("delivery", dict(message))
            self._diagnose("input_accepted", input_id=message_id)
            return dict(message)

    @property
    def state(self):
        with self._lock:
            return self._state

    def background_activity(self):
        with self._lock:
            if self._state in ("closing", "exited"):
                return dict(IDLE_BACKGROUND)
            return self.protocol.background_activity()

    def stop_snapshot(self):
        with self._lock:
            return {"state": self._state,
                    "compacting": self._compacting and self._state not in ("closing", "exited"),
                    "background_activity": self.protocol.background_activity() if self._state not in ("closing", "exited") else dict(IDLE_BACKGROUND),
                    "stop_token": f"{self._stop_id}:{self.activity_version}"}

    def pi_compaction(self, started):
        # Pi does not identify the prompt in compaction events. Only the oldest
        # unacknowledged written input can have started preflight compaction.
        if self._compacting == started:
            return
        self._compacting = started
        if started:
            candidate = next((m["id"] for m in self._messages.values()
                              if m.get("written") and m["id"] in self._receipt_deadlines), None)
            if candidate is not None:
                deadline = self._receipt_deadlines[candidate]
                cap = self._compaction_caps.setdefault(candidate, deadline + PI_COMPACTION_ALLOWANCE)
                self._receipt_deadlines[candidate] = cap
                self._compaction_input = candidate
        elif not started:
            candidate = self._compaction_input
            if candidate in self._receipt_deadlines:
                self._receipt_deadlines[candidate] = min(self._compaction_caps[candidate],
                                                         time.monotonic() + self.receipt_timeout)
            self._compaction_input = None
        self._emit("compaction", {"compacting": started})

    def stop_if_current(self, token, kind, message_id=None):
        with self._lock:
            if token != f"{self._stop_id}:{self.activity_version}":
                return False
            if kind == "reply":
                message = self._messages.get(message_id)
                if self._state not in ("starting", "active") or not message or message.get("settled") is not False:
                    return False
            elif kind == "background":
                if self._state != "idle" or not self.protocol.background_activity().get("task_count"):
                    return False
            else:
                return False
            self._close_reason = "Conversation run stopped"
            self._diagnose("close_requested", error=self._safe_error(self._close_reason))
            self._state = "closing"
            return True

    def drain(self):
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def close(self, reason="Runner stopped"):
        with self._lock:
            if self._state in ("exited", "closing"):
                return
            self._close_reason = reason
            self._diagnose("close_requested", error=self._safe_error(reason))
            self._state = "closing"

    def join(self, timeout=5):
        if not self._thread:
            return True
        self._thread.join(timeout)
        if self._thread.is_alive():
            with self._lock:
                self._diagnose("cleanup_timeout", error="Runner thread did not stop within the cleanup deadline")
                self._emit("error", "Runner cleanup timed out; the process lease will close when Hive exits")
            return False
        return True

    # --- Called by the controller or protocol while the lock is held ---

    def touch_activity(self):
        self._last_activity = clock()
        self.activity_version += 1

    def idle_shutdown_uncertain(self):
        self._diagnose("idle_shutdown_uncertain", error="Could not confirm Claude is idle; keeping its process alive")
        self._emit("warning", "Could not confirm Claude is idle. Automatic idle shutdown is disabled for this process; "
                              "you can stop it explicitly.")

    def unknown_event(self, event, visible=True):
        self._diagnose("unknown_event", event=event, context=json.dumps(event),
                       error="Unrecognized harness event; delivery fidelity may be reduced" if visible else None)
        if visible:
            self._emit("warning", "Unrecognized harness event. Delivery fidelity may be reduced; see runner diagnostics.")

    def harness_notice(self, event):
        text = event.get("content")
        if not isinstance(text, str) or not text.strip():
            raise HiveError("Harness notice must contain non-empty string content")
        message = self._safe_error(text)
        self._diagnose("harness_notice", event=event, context=text)
        self._emit("warning", message)

    def control(self, record):
        self._controls.append(record)

    def ready(self, initial_only=False):
        self._ready = True
        self._initial_only = initial_only
        if self._state == "starting" and self._outstanding():
            self._state = "active"

    def session(self, session_id):
        if session_id != self.session_id:
            raise HiveError(f"Harness changed session from {self.session_id} to {session_id}")
        self._emit("session", session_id)

    def delivery(self, input_id, status, **facts):
        message = self._messages.get(input_id)
        if not message:
            self._diagnose("unknown_input_receipt", event={"type": "receipt"},
                           error="Receipt did not match a Hive input ID")
            self._emit("warning", "Harness receipt did not match a Hive input ID; see runner diagnostics.")
            return
        if not status or message["settled"]:
            return
        # Receipts can arrive after stronger evidence. Never downgrade delivery.
        if status == "accepted" and message["status"] == "delivered":
            status = message["status"]
        self._receipt_deadlines.pop(input_id, None)
        if facts.get("error"):
            facts["error"] = self._safe_error(facts["error"])
        message.update(facts)
        message["status"] = status
        self._diagnose("input_delivery", input_id=input_id, event={"type": status}, error=facts.get("error"))
        failed = status in ("rejected", "failed")
        if failed:
            message["settled"] = True
            message["error"] = message.get("error") or "Harness rejected or failed this input"
            if not self._outstanding():
                self._run_error = message["error"]
        self._emit("delivery", dict(message))
        if failed:
            self._settle_if_done()

    def answer(self, text):
        if text:
            self._answer = text

    def run_error(self, error):
        self._run_error = self._safe_error(error) if error else None
        if self._run_error:
            self._diagnose("model_error", error=self._run_error)

    def probe_pi_settlement(self):
        probe_id = f"hive-settled-{uuid.uuid4()}"
        self._probes[probe_id] = max((m["sequence"] for m in self._messages.values() if m.get("written")), default=0)
        self.control({"type": "get_state", "id": probe_id})

    def pi_settled(self, probe_id, data):
        ceiling = self._probes.pop(probe_id, None)
        if ceiling is None or data.get("isStreaming") is not False or data.get("pendingMessageCount") != 0:
            return
        candidates = [m for m in self._outstanding() if m["sequence"] <= ceiling]
        if any(not m["harness_accepted"] for m in candidates):
            return
        for message in candidates:
            # Pi has no native input-ID delivery/result attribution. Session
            # settlement is recorded without claiming a per-message reply.
            message["settled"] = True
            self._diagnose("input_run_settled", input_id=message["id"], event={"type": "session_settled"})
            self._emit("delivery", dict(message))
        self._publish_answer(None)
        self._settle_if_done()

    def claude_background_result(self, result_id, text, error):
        if result_id in self._seen_results:
            return
        self._seen_results.add(result_id)
        self._diagnose("background_result", result_id=result_id,
                       event={"type": "result", "origin": {"kind": "task-notification"}},
                       error=self._safe_error(error) if error else None)
        # Use this result's text, never a stale answer from another turn.
        self._answer = text or ""
        self._publish_answer(None)
        if error:
            self._emit("warning", f"Background task failed: {self._safe_error(error)}")
        # Background completion neither answers pending user inputs nor settles them.

    def claude_result(self, result_id, ids, text, error):
        key = result_id or ":".join(ids)
        if key in self._seen_results:
            return
        self._seen_results.add(key)
        if not all(input_id in self._messages for input_id in ids):
            raise HiveError("Claude result contains an input ID not sent by Hive")
        for input_id in ids:
            message = self._messages[input_id]
            self._receipt_deadlines.pop(input_id, None)
            message.update(status="failed" if error else "included", settled=True, result_id=key,
                           harness_accepted=True, error=self._safe_error(error) if error else None)
            self._diagnose("input_result", input_id=input_id, event={"type": message["status"]}, result_id=key)
            self._emit("delivery", dict(message))
        self.answer(text)
        self._run_error = self._safe_error(error) if error else None
        self._diagnose("result", event={"type": "result"}, error=self._run_error)
        self._publish_answer(ids)
        self._settle_if_done()

    # --- Internals ---

    def _emit(self, kind, data):
        self._events.put((kind, data))

    def _diagnose(self, kind, **facts):
        record = self._diagnostics.record(kind, state=self._state, **facts)
        self._emit("diagnostic", record)
        if record.get("persistence_error"):
            self._emit("error", record["persistence_error"])
            if self._state != "exited":
                self._state = "closing"

    def _safe_error(self, error):
        return redact(str(error), prompts=[m["content"] for m in self._messages.values()])

    def _outstanding(self):
        return [m for m in self._messages.values() if not m["settled"]]

    def _publish_answer(self, ids):
        if self._answer:
            self._diagnose("assistant_output", context=self._answer, input_ids=ids)
            self._emit("output", {"id": str(uuid.uuid4()), "content": self._answer, "input_ids": ids})
        self._answer = ""

    def _settle_if_done(self):
        if self._outstanding() or self._state in ("closing", "idle", "exited"):
            return
        self._state = "idle"
        self.touch_activity()
        self._diagnose("settled", error=self._run_error)
        if self._state == "closing":
            return
        self._emit("settled", {"error": self._run_error})
        self._run_error = None

    def _spawn_guardian(self, argv):
        process = subprocess.Popen(
            [sys.executable, "-I", GUARDIAN, json.dumps(argv)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=self.chdir, env=child_env(self.env), close_fds=True, bufsize=0)
        for stream in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
        return process

    def _verify_version(self):
        process = self._spawn_guardian([self.command, *self.args, "--version"])
        output = bytearray()
        streams = [process.stdout, process.stderr]
        exited = False
        try:
            deadline = clock() + 10
            while True:
                with self._lock:
                    if self._state == "closing":
                        raise HiveError("Harness version check cancelled")
                if clock() >= deadline:
                    raise HiveError("Harness version check timed out")
                ready = select.select(streams, [], [], 0.05)[0] if streams else []
                for stream in ready:
                    chunk = _read(stream)
                    if chunk is None:
                        continue
                    if chunk == b"":
                        streams.remove(stream)
                    else:
                        output += chunk
                        if len(output) > 65_536:
                            raise HiveError("Harness version output too large")
                status = process.poll()
                if status is not None:
                    exited = True
                    if status != 0:
                        raise HiveError(f"Harness version check failed (exit {status})")
                    break
            text = output.decode("utf-8", "replace")
            self._diagnostics.binary_version = find_version(text)
            self._diagnose("version_check", context=bytes(output))
            self.protocol.check_version(text)
        except Exception:
            self._version_failure_output = bytes(output)
            raise
        finally:
            self._diagnose("version_probe_output", context=bytes(output))
            if not exited:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                process.wait()
            _close(process.stdin)
            _close(process.stdout)
            _close(process.stderr)

    def _run(self):
        status = None
        process = None
        stdin_closed = False
        try:
            self._phase = "version_check"
            self.protocol.validate_args(self.args)
            self._verify_version()
            argv = self.protocol.argv(self.command, self.args, self.session_id, resume=self.resume, parent=self.parent)
            process = self._process = self._spawn_guardian(argv)
            self.pid = process.pid
            self._diagnose("process_started")
            with self._lock:
                self.protocol.boot(self)
            stdin = process.stdin
            streams = [process.stdout, process.stderr]
            buffer = b""
            writing = b""
            write_started = None
            writing_input = False
            next_input_at = 0
            started = clock()
            closing_at = None
            while True:
                with self._lock:
                    idle_expired = (self._state == "idle" and clock() - self._last_activity >= self.idle_timeout
                                    and self.protocol.idle_shutdown_ready(self))
                    if self._state == "closing" or idle_expired:
                        self._state = "closing"
                        # EOF is an observation, not proof of process exit. Give a
                        # live child a short grace period, then revoke its lease
                        # and let the guardian reap.
                        if status is not None or self._stdout_closed_at is None or clock() - self._stdout_closed_at >= 0.25:
                            if closing_at is None:
                                closing_at = clock()
                            if not stdin_closed:
                                _close(stdin)
                                stdin_closed = True
                    elif not writing:
                        writing_input = False
                        record = self._controls.pop(0) if self._controls else None
                        if record is None and self._ready and clock() >= next_input_at:
                            message = next((m for m in self._messages.values()
                                            if not m.get("written") and not m["settled"]), None)
                            if message:
                                message["written"] = True
                                self._receipt_deadlines[message["id"]] = clock() + self.receipt_timeout
                                message["status"] = "written"
                                self._diagnose("stdin_queued", input_id=message["id"])
                                self._emit("delivery", dict(message))
                                record = self.protocol.input(message["id"], message["content"])
                                writing_input = True
                                if self._initial_only:
                                    self._ready = False
                        if record is not None:
                            if record.get("id"):
                                self._receipt_deadlines.setdefault(record["id"], clock() + self.receipt_timeout)
                            writing = (json.dumps(record, ensure_ascii=False) + "\n").encode()
                            write_started = clock()
                    closing = self._state == "closing"
                self._phase = "transport"
                if closing_at is None and any(clock() >= deadline for deadline in self._receipt_deadlines.values()):
                    raise HiveError("Harness did not acknowledge input before the receipt deadline")
                if not self._ready and clock() - started > 20:
                    raise HiveError("Harness startup/capability check timed out")
                if writing and clock() - write_started > 15 and closing_at is None:
                    raise HiveError("Harness stdin stalled")
                writers = [] if (not writing or stdin_closed or closing) else [stdin]
                if streams or writers:
                    readable, writable, _ = select.select(streams, writers, [], 0.05)
                else:
                    time.sleep(0.05)
                    readable, writable = [], []
                for stream in readable:
                    chunk = _read(stream)
                    if chunk is None:
                        continue
                    if chunk == b"":
                        streams.remove(stream)
                        channel = "stdout" if stream is process.stdout else "stderr"
                        self._emit("channel_closed", {"channel": channel, "timestamp": utc_timestamp()})
                        self._diagnose(f"{channel}_eof")
                        if channel == "stdout" and closing_at is None:
                            self._unexpected_eof = True
                            self._stdout_closed_at = clock()
                            with self._lock:
                                if self._outstanding():
                                    self._emit("warning", "Harness closed stdout before the run settled; see runner "
                                                          "diagnostics. Uncertain inputs will not be resent.")
                                self._state = "closing"
                    elif stream is process.stderr:
                        self._stderr = (self._stderr + chunk)[-65_536:]
                    else:
                        buffer += chunk
                        if len(buffer) > 8_388_608:
                            raise HiveError("Protocol record exceeds 8 MiB")
                        while (index := buffer.find(b"\n")) >= 0:
                            line, buffer = buffer[:index + 1], buffer[index + 1:]
                            if line.strip():
                                self._handle_line(line)
                if stdin in writable:
                    self._phase = "stdin_write"
                    try:
                        count = os.write(stdin.fileno(), writing)
                        writing = writing[count:]
                    except BlockingIOError:
                        pass
                    # Pace completed user-message writes, never control commands or reads.
                    if not writing and writing_input:
                        next_input_at = clock() + self.protocol.input_interval
                if status is None:
                    status = process.poll()
                    if status is not None:
                        self._diagnose("child_exit", error=_exit_text(status), context=self._stderr)
                if status is not None and not streams:
                    break
                if closing_at is not None and clock() - closing_at > 5:
                    raise HiveError("Harness shutdown timed out")
            if buffer:
                self._diagnose("truncated_jsonl", context=buffer)
                raise HiveError("Harness ended with a truncated protocol record")
            with self._lock:
                orderly = not self._outstanding() and (status == 0 or (closing_at is not None and not self._unexpected_eof))
            if not orderly:
                suffix = "" if status == 0 else f" ({status})"
                raise HiveError(f"Harness exited{suffix} before orderly shutdown; see runner diagnostics")
        except Exception as error:
            with self._lock:
                message = self._safe_error(str(error))
                self._state = "closing"
                self._diagnose(self._phase or "runner_error", event=self._last_event, error=message,
                               context=self._stderr)
                if not self._close_reason:
                    output = self._version_failure_output or self._stderr
                    # Redaction is a precaution: we have not established that harness
                    # errors contain credentials. It is best-effort, not a guarantee.
                    excerpt = self._safe_error("\n".join(output.decode("utf-8", "replace").splitlines()[-8:])).strip()
                    # Keep subprocess text out of the diagnostics journal above.
                    self._emit("error", f"{message}\n{excerpt}" if excerpt else message)
        finally:
            status = self._cleanup(process, status)
            with self._lock:
                for message in self._outstanding():
                    message.update(status="unknown" if message.get("written") else "failed", settled=True,
                                   error=self._close_reason or "Transport lost; not automatically resent")
                    self._diagnose("uncertain_delivery" if message.get("written") else "unsent_failed",
                                   input_id=message["id"], error=message["error"])
                    self._emit("delivery", dict(message))
                self._state = "exited"
                self._diagnose("runner_exited")
                self._emit("exited", {"reason": self._close_reason})

    def _handle_line(self, line):
        self._phase = "decode_jsonl"
        try:
            event = json.loads(line.decode("utf-8"))
        except ValueError:
            self._diagnose("malformed_jsonl", context=line, error="Malformed JSONL from harness")
            raise HiveError("Malformed JSONL from harness; raw record omitted for privacy") from None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise HiveError("Protocol record must be a JSON object with a string type")
        if event["type"] == "response":
            self._receipt_deadlines.pop(event.get("id"), None)
            self._compaction_caps.pop(event.get("id"), None)
            if self._compaction_input == event.get("id"):
                self._compaction_input = None
        self._phase = "protocol_event"
        self._last_event = {key: event[key] for key in ("type", "subtype") if key in event}
        with self._lock:
            previous = self.protocol.background_activity()
            self.protocol.event(self, event)
            activity = self.protocol.background_activity()
            if activity != previous:
                self._emit("background_activity", activity)

    def _cleanup(self, process, status):
        if process is None:
            return status
        _close(process.stdin)
        if status is None:
            try:
                try:
                    process.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass  # Already exited; wait still reaps its status.
                # Keep consuming both channels even after a protocol error.
                # Otherwise a verbose TERM handler can block the guardian's
                # relays during reaping.
                streams = [s for s in (process.stdout, process.stderr) if not s.closed]
                while status is None:
                    if streams:
                        for stream in select.select(streams, [], [], 0.05)[0]:
                            chunk = _read(stream)
                            if chunk == b"":
                                streams.remove(stream)
                            elif chunk and stream is process.stderr:
                                self._stderr = (self._stderr + chunk)[-65_536:]
                    else:
                        time.sleep(0.05)
                    status = process.poll()
                self._diagnose("child_reaped", error=_exit_text(status), context=self._stderr)
            except Exception as error:
                self._diagnose("cleanup_failure", error=f"Child cleanup failed ({type(error).__name__})")
                self._emit("error", f"Child cleanup failed ({type(error).__name__}); see runner diagnostics")
        _close(process.stdout)
        _close(process.stderr)
        return status


def _read(stream):
    """Bytes read, b'' at EOF, or None when nothing is available yet."""
    try:
        return os.read(stream.fileno(), 65_536)
    except BlockingIOError:
        return None


def _close(stream):
    try:
        if stream and not stream.closed:
            stream.close()
    except OSError:
        pass


def _exit_text(status):
    exit_status = status if status >= 0 else None
    signal_number = -status if status < 0 else None
    return f"exit_status={exit_status} signal={signal_number}"
