"""Output actions turn tags in agent output, such as <run-id>ab12</run-id>, into buttons.

A click runs the configured command with the tag's value. Agent output is
untrusted, so the tag supplies only a value. The command comes from config,
the value must fully match the action's pattern, and the command runs without
a shell.
"""

import re
import subprocess
import threading

from .projects import normalize_only

TAG_FORMAT = re.compile(r"[a-z][a-z0-9-]*")
VALUE_PLACEHOLDER = "{value}"
RUN_TIMEOUT = 60  # seconds; a command still running after this is left running
OUTPUT_LIMIT = 2000  # characters of output kept for the failure message


class OutputActionError(ValueError):
    """Invalid output action configuration."""


def normalize_all(entries):
    actions = [normalize(entry) for entry in (entries or [])]
    seen = set()
    for action in actions:
        if action["tag"] in seen:
            raise OutputActionError(f"Output action tag '{action['tag']}' is defined more than once")
        seen.add(action["tag"])
    return actions


def normalize(entry):
    if not isinstance(entry, dict):
        raise OutputActionError("Each output action must be an object")
    tag = _text(entry.get("tag"))
    if not TAG_FORMAT.fullmatch(tag):
        raise OutputActionError(f"Output action tag '{tag}' must be lowercase letters, digits and dashes")
    command = _text(entry.get("command")).strip()
    if not command:
        raise OutputActionError(f"Output action '{tag}' needs a command")
    pattern = _text(entry.get("pattern"))
    if not pattern:
        raise OutputActionError(f"Output action '{tag}' needs a pattern for its value")
    try:
        re.compile(pattern)
    except re.error as error:
        raise OutputActionError(f"Output action '{tag}' has an invalid pattern: {error}") from None
    args = entry.get("args") or []
    if not isinstance(args, list):
        raise OutputActionError(f"Output action '{tag}' args must be a list")
    return {"tag": tag, "label": _text(entry.get("label")), "pattern": pattern, "command": command,
            "args": [_text(arg) for arg in args], "only": normalize_only(entry.get("only"))}


def _text(value):
    return "" if value is None else str(value)


def run(action, value, env):
    """Run the action. Returns None on success or an error message."""
    value = _text(value)
    if not re.fullmatch(action["pattern"], value):
        return f"'{value}' does not match the pattern of output action '{action['tag']}'"
    args = [arg.replace(VALUE_PLACEHOLDER, value) for arg in action["args"]]
    try:
        process = subprocess.Popen([action["command"], *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, env=env)
    except OSError as error:
        return f"Could not start '{action['command']}': {error.strerror or error}"

    output = []

    def read():
        kept = ""
        while chunk := process.stdout.read1(8192):
            kept = (kept + chunk.decode("utf-8", "replace"))[-OUTPUT_LIMIT:]
            output[:] = [kept]
        process.stdout.close()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        status = process.wait(RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Left running; a background wait reaps it when it exits.
        threading.Thread(target=process.wait, daemon=True).start()
        return None
    if status == 0:
        return None
    reader.join(1)
    reason = f"exited with status {status}" if status > 0 else f"was stopped by signal {-status}"
    message = f"'{action['command']}' {reason}."
    tail = "".join(output).strip()
    return f"{message}\n\n{tail}" if tail else message
