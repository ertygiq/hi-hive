"""Shared setup for the opt-in live tests that spend model tokens (see LIVE_STEERING.md)."""

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

live = pytest.mark.skipif(os.environ.get("HIVE_LIVE_SMOKE") != "1",
                          reason="Set HIVE_LIVE_SMOKE=1 to spend model tokens on the installed harnesses")


def scratch_dir(prefix):
    base = ROOT / ".live-smoke"
    base.mkdir(exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=base)


def remove(directory):
    shutil.rmtree(directory, ignore_errors=True)


def write_secret(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(text)


def claude_credentials():
    """Existing Claude Code credentials, from the config folder or the macOS keychain."""
    source = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / ".credentials.json"
    if source.is_file():
        return source.read_text()
    result = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("Claude credentials unavailable")
    return result.stdout


def pi_auth(provider):
    agent_dir = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent")
    auth = json.loads((agent_dir / "auth.json").read_text())
    if provider not in auth:
        raise RuntimeError(f"Missing Pi credential for {provider}")
    return {provider: auth[provider]}


class EventWaiter:
    """Collects runner events and fails fast on visible errors."""

    def __init__(self, runner, deadline_seconds, label, fail_on=("error",)):
        self.runner = runner
        self.events = []
        self.deadline = time.monotonic() + deadline_seconds
        self.label = label
        self.fail_on = fail_on

    def wait(self, condition):
        while True:
            self.events.extend(self.runner.drain())
            failures = [event for event in self.events if event[0] in self.fail_on]
            if failures:
                raise AssertionError(f"Live harness errors: {failures}")
            if condition():
                self.events.extend(self.runner.drain())
                return
            if time.monotonic() >= self.deadline:
                raise AssertionError(f"Live {self.label} exceeded its deadline")
            time.sleep(0.05)

    def of(self, kind):
        return [data for event_kind, data in self.events if event_kind == kind]
