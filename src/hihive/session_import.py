"""Read native harness sessions conservatively, without changing their files."""

import json
import os
import re
import shutil
from pathlib import Path

from . import HiveError

MAX_BYTES = 32 * 1024 * 1024
MAX_LINE = 2 * 1024 * 1024
MAX_ENTRIES = 100_000
SESSION_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
CLAUDE_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def native_harness(harness):
    """Accept only recognizable local Pi or Claude executables, not wrappers."""
    kind = harness.get("type")
    command = str(harness.get("command") or "")
    resolved = shutil.which(command)
    if not resolved:
        return False
    path = os.path.realpath(resolved)
    if kind == "pi":
        if "/node_modules/" not in path:
            return False
        for directory in Path(path).parents:
            if directory.name != "pi-coding-agent":
                continue
            try:
                package = json.loads((directory / "package.json").read_text())
                executable = package.get("bin", {}).get("pi")
                return (package.get("name") in ("@earendil-works/pi-coding-agent", "@mariozechner/pi-coding-agent")
                        and isinstance(executable, str) and os.path.realpath(directory / executable) == path)
            except (OSError, ValueError, AttributeError):
                return False
        return False
    if kind == "claude-code":
        try:
            with open(path, "rb") as f:
                magic = f.read(4)
            return os.path.basename(command) == "claude" and magic in (
                b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe")
        except OSError:
            return False
    return False


def _read(path):
    try:
        with open(path, "rb") as f:
            before = os.fstat(f.fileno())
            if not 0 < before.st_size <= MAX_BYTES:
                raise HiveError("Session file is empty or too large")
            data = f.read(MAX_BYTES + 1)
            after = os.fstat(f.fileno())
        current = os.stat(path)
    except OSError:
        raise HiveError("Session file is unavailable") from None
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or identity != (
            current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
        raise HiveError("Session changed while reading; try again")
    if len(data) > MAX_BYTES or not data.endswith(b"\n"):
        raise HiveError("Session file is incomplete or too large")
    lines = data.splitlines()
    if len(lines) > MAX_ENTRIES or any(len(line) > MAX_LINE for line in lines):
        raise HiveError("Session file exceeds import limits")
    try:
        entries = [json.loads(line) for line in lines if line.strip()]
    except (ValueError, UnicodeError):
        raise HiveError("Session file has invalid JSON") from None
    if any(not isinstance(entry, dict) for entry in entries):
        raise HiveError("Session file has invalid entries")
    return entries, {"path": str(path), "device": before.st_dev, "inode": before.st_ino}


def _text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(block["text"] for block in content if isinstance(block, dict)
                     and block.get("type") == "text" and isinstance(block.get("text"), str))


def _branch(entries, id_key, parent_key, leaf):
    index = {}
    for entry in entries:
        key = entry.get(id_key)
        if not isinstance(key, str) or not key or key in index:
            raise HiveError("Session has invalid or duplicate entry IDs")
        index[key] = entry
    result, seen = [], set()
    while leaf is not None:
        if leaf in seen or leaf not in index:
            raise HiveError("Session branch is incomplete or cyclic")
        seen.add(leaf)
        entry = index[leaf]
        result.append(entry)
        leaf = entry.get(parent_key)
        if leaf is not None and not isinstance(leaf, str):
            raise HiveError("Session has an invalid parent")
    return list(reversed(result))


def _pi_dir(cwd, args):
    # Pi resolves session-dir against the process working directory.
    if "--session-dir" in args:
        i = args.index("--session-dir")
        if i + 1 >= len(args):
            raise HiveError("Pi session directory is missing")
        value = args[i + 1]
    else:
        value = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
        agent = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent")
        setting_value = None
        for settings in (agent / "settings.json", Path(cwd) / ".pi" / "settings.json"):
            if settings.is_file():
                try:
                    config = json.loads(settings.read_text())
                    if not isinstance(config, dict):
                        raise ValueError()
                    if "sessionDir" in config:
                        setting_value = config["sessionDir"]
                except (ValueError, OSError):
                    raise HiveError("Cannot read Pi settings") from None
        value = value or setting_value
        if not value:
            encoded = re.sub(r"[/\\:]", "-", str(cwd).lstrip("/\\"))
            return agent / "sessions" / f"--{encoded}--"
    if not isinstance(value, str) or not value:
        raise HiveError("Invalid Pi session directory")
    return Path(os.path.expanduser(value)) if os.path.isabs(os.path.expanduser(value)) else Path(cwd) / value


def read_pi(cwd, args, session_id):
    if args.count("--session-dir") > 1:
        raise HiveError("Pi harness has ambiguous session directories")
    if any(arg in ("--no-session", "--continue", "--resume", "--session", "--fork", "--session-id")
           or arg.startswith(("--session=", "--session-id=", "--fork=", "--session-dir=")) for arg in args):
        raise HiveError("Pi harness arguments do not support session import")
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        raise HiveError("Invalid Pi session ID")
    directory = _pi_dir(cwd, args)
    try:
        candidates = list(directory.glob("*.jsonl"))
    except OSError:
        raise HiveError("Cannot inspect Pi sessions") from None
    if len(candidates) > 10_000:
        raise HiveError("Too many Pi sessions to inspect safely")
    matches = []
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("rb") as file:
                first = file.readline(65537)
            if len(first) > 65536:
                continue
            header = json.loads(first)
        except (OSError, ValueError):
            continue
        if not isinstance(header, dict) or header.get("type") != "session" or header.get("id") != session_id:
            continue
        entries, identity = _read(path)
        if not entries or entries[0] != header:
            raise HiveError("Session changed while reading; try again")
        if os.path.realpath(str(header.get("cwd") or "")) != os.path.realpath(cwd):
            continue
        matches.append((entries, identity))
    if not matches:
        raise HiveError("Session not found for this project")
    if len(matches) > 1:
        raise HiveError("Session is ambiguous")
    entries, identity = matches[0]
    version = entries[0].get("version", 1)
    if version not in (1, 2, 3):
        raise HiveError("Unsupported Pi session version")
    body = entries[1:]
    if version == 1:
        branch = body
    else:
        branch = _branch(body, "id", "parentId", body[-1].get("id")) if body else []
    messages = [entry.get("message") for entry in branch if entry.get("type") == "message"
                and isinstance(entry.get("message"), dict)]
    first_user = next((m for m in messages if m.get("role") == "user"), None)
    prompt = _text(first_user.get("content")) if first_user else ""
    answer = ""
    for m in messages:
        if m.get("role") == "user":
            answer = ""
        elif (m.get("role") == "assistant" and m.get("stopReason") == "stop"
              and isinstance(m.get("content"), list)
              and all(isinstance(block, dict) and block.get("type") in ("text", "thinking") for block in m["content"])):
            answer = _text(m.get("content"))
        elif m.get("role") == "assistant":
            answer = ""
    return {"session_id": session_id, "prompt": prompt, "answer": answer,
            "message_count": None, "last_activity": None, "source": identity}


def read_claude(cwd, session_id):
    if not isinstance(session_id, str) or not CLAUDE_ID.fullmatch(session_id):
        raise HiveError("Invalid Claude session ID")
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    canonical = os.path.realpath(cwd)
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", canonical)
    if len(encoded) > 200:
        raise HiveError("Claude project path is too long for safe discovery")
    directory = root / encoded
    if directory.is_symlink():
        raise HiveError("Claude session directory is a symbolic link")
    path = directory / f"{session_id}.jsonl"
    try:
        matches = [candidate for candidate in root.glob(f"*/{session_id}.jsonl") if candidate.is_file()]
    except OSError:
        raise HiveError("Cannot inspect Claude sessions") from None
    if len(matches) > 1:
        raise HiveError("Session is ambiguous")
    if path.is_symlink() or not path.is_file():
        raise HiveError("Session not found for this project")
    entries, identity = _read(path)
    cwds = [e.get("cwd") for e in entries if isinstance(e.get("cwd"), str)]
    if not cwds or any(os.path.realpath(value) != canonical for value in cwds):
        raise HiveError("Session working directory does not match this project")
    if any(e.get("sessionId") not in (None, session_id) for e in entries):
        raise HiveError("Session ID does not match transcript")
    relevant = [e for e in entries if e.get("type") in ("user", "assistant", "progress", "system", "attachment")
                and isinstance(e.get("uuid"), str) and not e.get("isSidechain") and not e.get("teamName")]
    prompt = answer = ""
    first_user_seen = False
    try:
        parent_ids = {e.get("parentUuid") for e in relevant if isinstance(e.get("parentUuid"), str)}
        leaves = [e for e in relevant if e["uuid"] not in parent_ids]
        if len(leaves) != 1:
            raise HiveError("Session branch is ambiguous")
        branch = _branch(relevant, "uuid", "parentUuid", leaves[0]["uuid"])
        compacted = any(e.get("isCompactSummary") or e.get("subtype") == "compact_boundary" for e in branch)
        for entry in branch:
            if entry.get("isMeta") or entry.get("isCompactSummary"):
                continue
            message = entry.get("message")
            if not isinstance(message, dict) or message.get("role") != entry.get("type"):
                continue
            content = message.get("content")
            text = _text(content)
            if entry.get("type") == "user":
                if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    continue
                if text.lstrip().startswith(("<", "[Request interrupted")):
                    continue
                if not first_user_seen:
                    first_user_seen = True
                    if not compacted and (isinstance(content, str) or (isinstance(content, list) and all(
                            isinstance(b, dict) and b.get("type") in ("text", "image") for b in content))):
                        prompt = text
                answer = ""
            elif entry.get("type") == "assistant":
                answer = ""
                if (message.get("stop_reason") == "end_turn" and not entry.get("isApiErrorMessage")
                        and isinstance(content, list) and all(isinstance(b, dict) and b.get("type") == "text" for b in content)):
                    answer = text
    except HiveError:
        # Do not invent conversation text from an uncertain branch.
        pass
    return {"session_id": session_id, "prompt": prompt, "answer": answer,
            "message_count": None, "last_activity": None, "source": identity}
