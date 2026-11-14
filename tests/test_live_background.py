"""Explicit opt-in only. Claude background Bash and Monitor work must survive idle shutdown."""

import os
import time
import uuid

import pytest

from hihive.protocol import ClaudeStreamProtocol
from hihive.runner import LiveRunner
from live_support import EventWaiter, claude_credentials, live, remove, scratch_dir, write_secret


class ObservedClaude(ClaudeStreamProtocol):
    def __init__(self):
        super().__init__()
        self.records = []

    def event(self, runner, event):
        self.records.append({key: event[key] for key in ("type", "subtype", "tasks", "origin") if key in event})
        if event.get("type") == "assistant":
            content = (event.get("message") or {}).get("content")
            blocks = content if isinstance(content, list) else []
            self.records.append({"tools": [b.get("name") for b in blocks if b.get("type") == "tool_use"]})
        super().event(runner, event)


@live
@pytest.mark.parametrize("kind", ["bash", "monitor", "monitor_stop"])
def test_live_claude_background_work_survives_idle_timeout(kind):
    directory = scratch_dir(f"{kind}-")
    runner = None
    try:
        config = os.path.join(directory, "config")
        os.makedirs(config)
        write_secret(os.path.join(config, ".credentials.json"), claude_credentials())
        env = {"CLAUDE_CONFIG_DIR": config, "CLAUDECODE": None, "TMPDIR": directory,
               "XDG_CACHE_HOME": os.path.join(directory, "cache")}
        seconds = 90 if kind == "monitor_stop" else 12
        tool = "Bash" if kind == "bash" else "Monitor"
        args = ["--model", os.environ.get("HIVE_SMOKE_CLAUDE_MODEL", "sonnet"), "--effort", "low", "--safe-mode",
                "--setting-sources", "", "--strict-mcp-config", "--no-chrome", "--disable-slash-commands",
                "--tools", f"{tool},TaskStop", "--allowedTools", f"Bash(sleep {seconds})", "Monitor", "TaskStop",
                "--permission-mode", "dontAsk", "--max-budget-usd", "1",
                "--system-prompt", "You are testing background lifecycle. Use only the requested tools and exact "
                                   "commands. Do not inspect or modify files. Keep replies short."]
        protocol = ObservedClaude()
        runner = LiveRunner(protocol=protocol, command=os.environ.get("HIVE_SMOKE_CLAUDE_COMMAND", "claude"), args=args,
                            session_id=str(uuid.uuid4()), chdir=directory, env=env, idle_timeout=1)
        if kind == "bash":
            prompt = f"Use Bash to run sleep {seconds} with run_in_background=true."
        else:
            prompt = f"Use Monitor with command sleep {seconds} and timeout_ms 120000."
        runner.submit(f"{prompt} Say STARTED when launched. When completion arrives, report FINISHED. "
                      "Do not wait synchronously or run any other command.")
        runner.start()
        waiter = EventWaiter(runner, 120, kind, fail_on=("error", "warning"))
        waiter.wait(lambda: waiter.of("output"))
        assert any(tool in record.get("tools", []) for record in protocol.records), f"Expected actual {tool} invocation"
        assert any(r.get("subtype") == "background_tasks_changed" and r.get("tasks") for r in protocol.records)
        assert runner.background_activity()["task_count"] > 0
        pid = runner.pid
        time.sleep(2)
        assert runner.state != "exited", "Background work must survive the one-second idle timeout"
        if kind == "monitor_stop":
            runner.submit("Stop the monitor now using TaskStop, then say STOPPED. Do not start any new work.")
        waiter.wait(lambda: runner.state == "exited")
        assert runner.pid == pid
        assert runner.background_activity() == {"task_count": 0, "preparing_reply": False}
        assert any(data["preparing_reply"] for data in waiter.of("background_activity")), \
            "Completion generation must be visible"
        outputs = waiter.of("output")
        assert len(outputs) >= 2
        assert any(r.get("subtype") == "background_tasks_changed" and r.get("tasks") == [] for r in protocol.records)
        if kind == "monitor_stop":
            assert any("TaskStop" in record.get("tools", []) for record in protocol.records)
        else:
            assert any(o["input_ids"] is None and "FINISHED" in o["content"] for o in outputs)
        print(f"Live Claude {kind}: survived idle timeout, reported completion, and shut down normally")
    finally:
        if runner:
            runner.close("Background smoke complete")
            runner.join()
        remove(directory)
