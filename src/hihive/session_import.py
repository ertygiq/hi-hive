"""Read native harness sessions conservatively, without changing their files."""

import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path

from . import HiveError

MAX_BYTES = 32 * 1024 * 1024
MAX_LINE = 2 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_DISCOVERY_ENTRIES = 10_000
SESSION_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
CLAUDE_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def native_executable(harness):
    """Return the identity of the exact verified local executable, or None."""
    kind = harness.get("type")
    command = str(harness.get("command") or "")
    resolved = shutil.which(command)
    if not resolved:
        return None
    path = os.path.realpath(resolved)
    try:
        directory = _directory(Path(path).parent)
        try:
            fd = os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
                    return None
                magic = os.read(fd, 4)
                after = os.fstat(fd)
                current = os.stat(Path(path).name, dir_fd=directory, follow_symlinks=False)
                identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or identity != (
                        current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns):
                    return None
            finally:
                os.close(fd)
        finally:
            os.close(directory)
    except (HiveError, OSError):
        return None
    valid = False
    if kind == "pi" and "/node_modules/" in path:
        for directory in Path(path).parents:
            if directory.name == "pi-coding-agent":
                try:
                    package = json.loads(_safe_bytes(directory / "package.json", 1024 * 1024, allow_root=True)[0])
                    executable = package.get("bin", {}).get("pi")
                    valid = (package.get("name") in ("@earendil-works/pi-coding-agent", "@mariozechner/pi-coding-agent")
                             and isinstance(executable, str) and os.path.realpath(directory / executable) == path)
                except (HiveError, OSError, ValueError, AttributeError):
                    pass
                break
    elif kind == "claude-code":
        valid = os.path.basename(command) == "claude" and magic in (
            b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe")
    if not valid or shutil.which(command) != resolved or os.path.realpath(resolved) != path:
        return None
    return {"executable": path, "executable_device": info.st_dev, "executable_inode": info.st_ino,
            "executable_size": info.st_size, "executable_mtime_ns": info.st_mtime_ns,
            "executable_ctime_ns": info.st_ctime_ns}


def native_harness(harness):
    return native_executable(harness) is not None


def _directory(path):
    """Open a directory by walking trusted ancestors without following links."""
    if not all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")):
        raise HiveError("Safe session file access is unavailable")
    path = os.path.abspath(os.fspath(path))
    # macOS exposes these root-owned aliases to /private. Do not resolve other links.
    if os.uname().sysname == "Darwin":
        for alias in ("/tmp", "/var"):
            target = "/private" + alias
            if path == alias or path.startswith(alias + "/"):
                link = os.lstat(alias)
                if link.st_uid != 0 or not stat.S_ISLNK(link.st_mode) or os.readlink(alias) != target.lstrip("/"):
                    raise HiveError("Untrusted system directory alias")
                path = target + path[len(alias):]
                break
    parts = [part for part in path.split(os.sep) if part]
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        root_info = os.fstat(fd)
        if root_info.st_uid != 0 or root_info.st_mode & 0o022:
            raise HiveError("Session directory permissions are unsafe")
        previous_sticky = False
        for i, part in enumerate(parts):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            sticky_ancestor = (i < len(parts) - 1 and info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX))
            if info.st_uid not in (0, os.getuid()) or (info.st_mode & 0o022 and (not sticky_ancestor or previous_sticky)):
                raise HiveError("Session directory permissions are unsafe")
            previous_sticky = sticky_ancestor
        return fd
    except BaseException:
        os.close(fd)
        raise


def _entries(directory, limit=10_000, budget=None):
    try:
        fd = _directory(directory)
        try:
            names = []
            with os.scandir(fd) as iterator:
                for entry in iterator:
                    if len(names) >= limit or (budget is not None and budget[0] <= 0):
                        raise HiveError("Too many session entries to inspect safely")
                    if budget is not None:
                        budget[0] -= 1
                    names.append(entry.name)
            return names
        finally:
            os.close(fd)
    except OSError:
        raise HiveError("Session directory is unavailable or unsafe") from None


def _header(path):
    try:
        directory = _directory(Path(path).parent)
        try:
            fd = os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                    raise HiveError("Session file ownership or permissions are unsafe")
                with os.fdopen(fd, "rb", closefd=False) as file:
                    return json.loads(file.readline(65537))
            finally:
                os.close(fd)
        finally:
            os.close(directory)
    except (OSError, ValueError):
        raise HiveError("Session header is unavailable or unsafe") from None


def executable_identity_matches(source):
    path = source["executable"]
    try:
        directory = _directory(Path(path).parent)
        try:
            fd = os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(fd)
                return (stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.getuid()) and not info.st_mode & 0o022
                        and (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) ==
                        (source["executable_device"], source["executable_inode"], source["executable_size"],
                         source["executable_mtime_ns"], source["executable_ctime_ns"]))
            finally:
                os.close(fd)
        finally:
            os.close(directory)
    except (OSError, HiveError, KeyError):
        return False


def source_identity_matches(source):
    path = source["path"]
    try:
        directory = _directory(Path(path).parent)
        try:
            fd = os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(fd)
                return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022
                        and (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) ==
                        (source["device"], source["inode"], source["size"], source["mtime_ns"], source["ctime_ns"]))
            finally:
                os.close(fd)
        finally:
            os.close(directory)
    except (OSError, HiveError, KeyError):
        return False


def _safe_bytes(path, limit=MAX_BYTES, allow_root=False):
    try:
        directory = _directory(Path(path).parent)
        try:
            fd = os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                before = os.fstat(fd)
                owners = (os.getuid(), 0) if allow_root else (os.getuid(),)
                if not stat.S_ISREG(before.st_mode) or before.st_uid not in owners or before.st_mode & 0o022:
                    raise HiveError("Session file ownership or permissions are unsafe")
                if not 0 < before.st_size <= limit:
                    raise HiveError("Session file is empty or too large")
                with os.fdopen(fd, "rb", closefd=False) as file:
                    data = file.read(limit + 1)
                after = os.fstat(fd)
                current = os.stat(Path(path).name, dir_fd=directory, follow_symlinks=False)
            finally:
                os.close(fd)
        finally:
            os.close(directory)
    except OSError:
        raise HiveError("Session file is unavailable or unsafe") from None
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or identity != (
            current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns):
        raise HiveError("Session changed while reading; try again")
    if len(data) > limit:
        raise HiveError("Session file is too large")
    return data, before


def _read(path):
    data, before = _safe_bytes(path)
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
    return entries, {"path": str(path), "device": before.st_dev, "inode": before.st_ino,
                     "size": before.st_size, "mtime_ns": before.st_mtime_ns, "ctime_ns": before.st_ctime_ns,
                     "digest": hashlib.sha256(data).hexdigest()}


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
            if os.path.lexists(settings):
                try:
                    config = json.loads(_safe_bytes(settings, 1024 * 1024)[0])
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
        raise HiveError("This is not a valid Pi session ID.")
    directory = _pi_dir(cwd, args)
    candidates = (directory / name for name in _entries(directory) if name.endswith(".jsonl"))
    matches = []
    for path in candidates:
        try:
            header = _header(path)
        except HiveError:
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
        raise HiveError("No session with this ID was found in this project.")
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
    matches = []
    budget = [MAX_DISCOVERY_ENTRIES]
    for name in _entries(root, budget=budget):
        folder = root / name
        try:
            if f"{session_id}.jsonl" in _entries(folder, budget=budget):
                matches.append(folder)
        except HiveError as error:
            if "Too many session entries" in str(error):
                raise
            # A non-directory or inaccessible unrelated entry is not a session root.
            continue
    if len(matches) > 1:
        raise HiveError("Session is ambiguous")
    if not matches or matches[0] != directory:
        raise HiveError("No session with this ID was found in this project.")
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
                if text.lstrip().startswith(("<local-command-stdout>", "<session-start-hook>", "<tick>",
                                             "<goal>", "<command-name>", "<ide_opened_file>",
                                             "<ide_selection>", "[Request interrupted")):
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
