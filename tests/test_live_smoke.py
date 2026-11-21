"""Explicit opt-in only. Runs one small, gated real-model conversation per harness."""

import json
import os
import shlex
import uuid

import pytest

from hihive.protocol import protocol_for
from hihive.runner import LiveRunner
from live_support import EventWaiter, ROOT, claude_credentials, live, pi_auth, remove, scratch_dir, write_secret

GATE = str(ROOT / "tests" / "fixtures" / "live_gate.py")
SYSTEM_PROMPT = ("You are a protocol smoke test participant. Follow the user exactly. Run only the requested gate "
                 "command, once, in the foreground. Do not delegate, inspect files, or modify files. Keep answers "
                 "short. Remember all markers across messages.")


def harness_setup(kind, directory, env):
    if kind == "pi":
        agent_dir = os.path.join(directory, "agent")
        os.makedirs(agent_dir)
        provider = os.environ.get("HIVE_SMOKE_PI_PROVIDER", "openai-codex")
        write_secret(os.path.join(agent_dir, "auth.json"), json.dumps(pi_auth(provider)))
        env.update(PI_CODING_AGENT_DIR=agent_dir, PI_OFFLINE="1", PI_TELEMETRY="0")
        args = ["--provider", provider, "--model", os.environ.get("HIVE_SMOKE_PI_MODEL", "gpt-6-astra"),
                "--thinking", "low", "--session-dir", os.path.join(directory, "sessions"), "--no-extensions",
                "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files", "--no-approve",
                "--offline", "--tools", "bash", "--system-prompt", SYSTEM_PROMPT]
        return os.environ.get("HIVE_SMOKE_PI_COMMAND", "pi"), args
    config_dir = os.path.join(directory, "claude-config")
    os.makedirs(config_dir)
    write_secret(os.path.join(config_dir, ".credentials.json"), claude_credentials())
    env["CLAUDE_CONFIG_DIR"] = config_dir
    args = ["--model", os.environ.get("HIVE_SMOKE_CLAUDE_MODEL", "sonnet"), "--effort", "low", "--safe-mode",
            "--setting-sources", "", "--strict-mcp-config", "--no-chrome", "--disable-slash-commands",
            "--tools", "Bash", "--allowedTools", f"Bash(python3 {GATE} *)", "--permission-mode", "dontAsk",
            "--max-budget-usd", "1", "--system-prompt", SYSTEM_PROMPT]
    return os.environ.get("HIVE_SMOKE_CLAUDE_COMMAND", "claude"), args


@live
@pytest.mark.parametrize("kind", ["pi", "claude-code"])
def test_live_stop_and_resume(kind):
    directory = scratch_dir(f"{kind}-stop-")
    runner = resumed = None
    try:
        env = {key: None for key in ("PI_SESSION_ID", "PI_SESSION_FILE", "PI_PROVIDER", "PI_MODEL",
                                     "PI_REASONING_LEVEL", "CLAUDECODE")}
        env["TMPDIR"] = os.path.join(directory, "tmp")
        env["XDG_CACHE_HOME"] = os.path.join(directory, "cache")
        os.makedirs(env["TMPDIR"])
        os.makedirs(env["XDG_CACHE_HOME"])
        command, args = harness_setup(kind, directory, env)
        session = str(uuid.uuid4())
        gate = f"python3 {shlex.quote(GATE)} {shlex.quote(directory)}"
        runner = LiveRunner(protocol=protocol_for(kind), command=command, args=args, session_id=session,
                            chdir=directory, env=env, idle_timeout=5)
        runner.submit(f"Remember marker HIVE_STOP_RECALL. Run this exact command in the foreground and wait: {gate}\n"
                      "Use a tool timeout of at least 60 seconds. No other tools.")
        runner.start()
        waiter = EventWaiter(runner, 120, f"{kind} stop")
        waiter.wait(lambda: os.path.exists(os.path.join(directory, "started")))
        runner.close("Conversation run cancelled")
        assert runner.join(), "Stopped harness did not exit"
        assert runner.state == "exited"
        resumed = LiveRunner(protocol=protocol_for(kind), command=command, args=args, session_id=session,
                             chdir=directory, env=env, idle_timeout=5, resume=True)
        resumed.submit("Without running tools, reply with the marker I asked you to remember.")
        resumed.start()
        resumed_waiter = EventWaiter(resumed, 120, f"{kind} resume")
        resumed_waiter.wait(lambda: resumed.state == "idle")
        assert any("HIVE_STOP_RECALL" in answer["content"] for answer in resumed_waiter.of("output"))
        print(f"Live {kind}: stopped process and resumed session with recall")
    finally:
        for process in (runner, resumed):
            if process:
                process.close("Smoke complete")
                process.join()
        remove(directory)


@live
@pytest.mark.parametrize("kind", ["pi", "claude-code"])
def test_live_steering_and_recall(kind):
    directory = scratch_dir(f"{kind}-")
    runner = None
    try:
        env = {key: None for key in ("PI_SESSION_ID", "PI_SESSION_FILE", "PI_PROVIDER", "PI_MODEL",
                                     "PI_REASONING_LEVEL", "CLAUDECODE")}
        env["TMPDIR"] = os.path.join(directory, "tmp")
        env["XDG_CACHE_HOME"] = os.path.join(directory, "cache")
        os.makedirs(env["TMPDIR"])
        os.makedirs(env["XDG_CACHE_HOME"])
        command, args = harness_setup(kind, directory, env)
        gate_command = f"python3 {shlex.quote(GATE)} {shlex.quote(directory)}"
        session = str(uuid.uuid4())
        runner = LiveRunner(protocol=protocol_for(kind), command=command, args=args, session_id=session,
                            chdir=directory, env=env, idle_timeout=5)
        runner.submit(f"Remember nonce=HIVE_NONCE. Run this exact command in the foreground and wait: {gate_command}\n"
                      "Use a tool timeout of at least 60 seconds. After the gate ends, obey newer messages and reply "
                      "with all remembered markers and the nonce. No more tools.")
        # Queue a burst before process startup, not only while a tool is running.
        runner.submit("Remember marker HIVE_STARTUP_ONE. Continue the original gate task. "
                      "Include this marker in the final reply.")
        runner.submit("Remember marker HIVE_STARTUP_TWO. Continue the original gate task. "
                      "Include both startup markers in the final reply.")
        runner.start()
        waiter = EventWaiter(runner, 120, f"{kind} smoke")
        waiter.wait(lambda: os.path.exists(os.path.join(directory, "started")))
        a = runner.submit("Remember explicit marker HIVE_STEER_ONE. Include it and all later markers in your final reply.")
        b = runner.submit("Remember explicit marker HIVE_STEER_TWO. In the final reply include BOTH HIVE_STEER_ONE and "
                          "HIVE_STEER_TWO and the remembered nonce. No tools.")
        waiter.wait(lambda: all(any(d["id"] == m["id"] and d.get("harness_accepted") for d in waiter.of("delivery"))
                                for m in (a, b)))
        assert waiter.of("output") == [], "Acceptance must not fabricate an answer"
        pid = runner.pid
        with open(os.path.join(directory, "release"), "w") as file:
            file.write("go")
        waiter.wait(lambda: runner.state == "idle")
        final = waiter.of("output")[-1]["content"]
        for marker in ("HIVE_STEER_ONE", "HIVE_STEER_TWO", "HIVE_NONCE", "HIVE_STARTUP_ONE", "HIVE_STARTUP_TWO"):
            assert marker in final
        outputs = len(waiter.of("output"))
        # Another burst after the conversation has settled. Claude is unpaced.
        runner.submit("Remember marker HIVE_IDLE_ONE. No tools. Reply briefly.")
        runner.submit("Ordinary follow-up: reply HIVE_FOLLOW_UP, the new idle marker, both steering markers, both "
                      "startup markers, and the remembered nonce. No tools.")
        waiter.wait(lambda: runner.state == "idle" and len(waiter.of("output")) > outputs)
        follow_up = waiter.of("output")[-1]["content"]
        for marker in ("HIVE_FOLLOW_UP", "HIVE_IDLE_ONE", "HIVE_STARTUP_ONE", "HIVE_STARTUP_TWO", "HIVE_STEER_ONE",
                       "HIVE_STEER_TWO", "HIVE_NONCE"):
            assert marker in follow_up
        assert runner.pid == pid
        assert all(found == session for found in waiter.of("session"))
        print(f"Live {kind}: startup burst, active steering, idle burst, and recall passed in session {session}")
    finally:
        if runner:
            runner.close("Smoke complete")
            runner.join()
        remove(directory)
