import json
import os

import pytest

from hihive import HiveError
from hihive import session_import


def save(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))


def test_imported_pi_resume_uses_existing_file_not_creation_by_id():
    from hihive.protocol import PiRpcProtocol
    protocol = PiRpcProtocol()
    protocol.resume_path = "/tmp/original-session.jsonl"
    argv = protocol.argv("pi", [], "session-id", resume=True)
    assert argv[-2:] == ["--session", "/tmp/original-session.jsonl"]
    assert "--session-id" not in argv


def test_pi_active_branch_and_read_only(tmp_path, monkeypatch):
    cwd = tmp_path / "project"
    cwd.mkdir()
    root = tmp_path / "agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(root))
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    folder = session_import._pi_dir(str(cwd), [])
    file = folder / "2026_id.jsonl"
    entries = [
        {"type": "session", "version": 3, "id": "id", "cwd": str(cwd)},
        {"type": "message", "id": "a", "parentId": None, "message": {"role": "user", "content": "question"}},
        {"type": "message", "id": "b", "parentId": "a", "message": {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "abandoned"}]}},
        {"type": "compaction", "id": "c", "parentId": "a", "summary": "not an answer"},
        {"type": "message", "id": "d", "parentId": "c", "message": {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "answer"}]}},
    ]
    save(file, entries)
    before = file.read_bytes()
    result = session_import.read_pi(str(cwd), [], "id")
    assert (result["prompt"], result["answer"]) == ("question", "answer")
    assert file.read_bytes() == before
    entries[-1]["message"]["stopReason"] = "aborted"
    save(file, entries)
    assert session_import.read_pi(str(cwd), [], "id")["answer"] == ""


@pytest.mark.parametrize("reason", ["toolUse", "error", "aborted", "length", "pending", "deferred", None])
def test_pi_never_imports_partial_answer(tmp_path, monkeypatch, reason):
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    file = session_import._pi_dir(str(cwd), []) / "x_id.jsonl"
    save(file, [
        {"type": "session", "version": 3, "id": "id", "cwd": str(cwd)},
        {"type": "message", "id": "a", "parentId": None, "message": {"role": "user", "content": "question"}},
        {"type": "message", "id": "b", "parentId": "a", "message": {"role": "assistant", "stopReason": reason,
            "content": [{"type": "text", "text": "partial"}]}},
    ])
    assert session_import.read_pi(str(cwd), [], "id")["answer"] == ""


def test_pi_rejects_bad_parent_and_mismatched_cwd(tmp_path, monkeypatch):
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    file = session_import._pi_dir(str(cwd), []) / "x_id.jsonl"
    header = {"type": "session", "version": 3, "id": "id", "cwd": str(cwd)}
    save(file, [header, {"type": "message", "id": "a", "parentId": "a", "message": {}}])
    with pytest.raises(HiveError, match="cyclic"):
        session_import.read_pi(str(cwd), [], "id")
    header["cwd"] = str(tmp_path)
    save(file, [header])
    with pytest.raises(HiveError, match="No session with this ID was found in this project\\."):
        session_import.read_pi(str(cwd), [], "id")


def test_native_executable_rejects_symlink_swap_during_validation(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"\\x7fELFfirst")
    second.write_bytes(b"\\x7fELFsecond")
    first.chmod(0o755)
    second.chmod(0o755)
    command = tmp_path / "claude"
    command.symlink_to(first)
    monkeypatch.setattr(session_import.shutil, "which", lambda name: str(command))
    real_read = os.read
    def swap(fd, count):
        data = real_read(fd, count)
        if data == b"\\x7fELF":
            command.unlink()
            command.symlink_to(second)
        return data
    monkeypatch.setattr(os, "read", swap)
    assert session_import.native_executable({"type": "claude-code", "command": str(command)}) is None


def test_trusted_root_package_metadata_does_not_weaken_transcript_ownership(tmp_path, monkeypatch):
    from types import SimpleNamespace
    path = tmp_path / "package.json"
    path.write_text('{}\n')
    monkeypatch.setattr(session_import, "_directory", lambda directory: os.open(directory, os.O_RDONLY | os.O_DIRECTORY))
    original_fstat = os.fstat
    def root_owned(fd):
        info = original_fstat(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_dev=info.st_dev, st_ino=info.st_ino,
                               st_size=info.st_size, st_mtime_ns=info.st_mtime_ns, st_ctime_ns=info.st_ctime_ns)
    monkeypatch.setattr(os, "fstat", root_owned)
    if os.getuid() == 0:
        pytest.skip("Requires a non-root test user")
    assert session_import._safe_bytes(path, allow_root=True)[0] == b'{}\n'
    with pytest.raises(HiveError, match="ownership"):
        session_import._safe_bytes(path)


def test_reader_refuses_fifo_symlink_and_unsafe_ancestor(tmp_path):
    safe = tmp_path / "safe"
    safe.mkdir()
    fifo = safe / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(HiveError):
        session_import._read(fifo)
    target = safe / "target"
    target.write_text('{}\n')
    link = tmp_path / "link"
    link.symlink_to(safe, target_is_directory=True)
    with pytest.raises(HiveError):
        session_import._read(link / "target")
    safe.chmod(0o775)
    with pytest.raises(HiveError, match="permissions"):
        session_import._read(target)


def test_reader_rejects_file_swapped_to_symlink_at_open(tmp_path, monkeypatch):
    parent = tmp_path / "safe"
    parent.mkdir()
    source = parent / "session.jsonl"
    source.write_text('{}\n')
    target = parent / "other"
    target.write_text('{}\n')
    original_open = os.open
    def swap(path, flags, *args, **kwargs):
        if path == source.name and kwargs.get("dir_fd") is not None:
            source.unlink()
            source.symlink_to(target)
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", swap)
    with pytest.raises(HiveError):
        session_import._read(source)


def test_bounded_reader_rejects_truncation_and_large_line(tmp_path, monkeypatch):
    path = tmp_path / "session"
    path.write_bytes(b'{"type":"session"}')
    with pytest.raises(HiveError, match="incomplete"):
        session_import._read(path)
    monkeypatch.setattr(session_import, "MAX_LINE", 4)
    path.write_text('{"test":1}\n')
    with pytest.raises(HiveError, match="limits"):
        session_import._read(path)


@pytest.mark.parametrize("reason", ["tool_use", "refusal", "stop_sequence", None])
def test_claude_does_not_treat_incomplete_output_as_answer(tmp_path, monkeypatch, reason):
    cwd = tmp_path / "p"
    cwd.mkdir()
    root = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    sid = "12345678-1234-1234-1234-123456789abc"
    folder = root / "projects" / __import__("re").sub(r"[^a-zA-Z0-9]", "-", str(cwd))
    file = folder / f"{sid}.jsonl"
    save(file, [
        {"type": "user", "uuid": "a", "parentUuid": None, "cwd": str(cwd), "sessionId": sid,
         "message": {"role": "user", "content": "question"}},
        {"type": "assistant", "uuid": "b", "parentUuid": "a", "cwd": str(cwd), "sessionId": sid,
         "message": {"role": "assistant", "stop_reason": reason, "content": [{"type": "text", "text": "partial"}]}},
    ])
    result = session_import.read_claude(str(cwd), sid)
    assert result["prompt"] == "question"
    assert result["answer"] == ""


def test_claude_image_only_first_user_does_not_promote_later_prompt(tmp_path, monkeypatch):
    cwd = tmp_path / "p"
    cwd.mkdir()
    root = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    sid = "12345678-1234-1234-1234-123456789abc"
    folder = root / "projects" / __import__("re").sub(r"[^a-zA-Z0-9]", "-", str(cwd))
    save(folder / f"{sid}.jsonl", [
        {"type": "user", "uuid": "a", "parentUuid": None, "cwd": str(cwd), "message": {"role": "user", "content": [{"type": "image"}]}},
        {"type": "user", "uuid": "b", "parentUuid": "a", "cwd": str(cwd), "message": {"role": "user", "content": "later"}},
    ])
    assert session_import.read_claude(str(cwd), sid)["prompt"] == ""


def test_claude_angle_bracket_user_messages_are_real(tmp_path, monkeypatch):
    cwd = tmp_path / "p"
    cwd.mkdir()
    root = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    sid = "12345678-1234-1234-1234-123456789abc"
    folder = root / "projects" / __import__("re").sub(r"[^a-zA-Z0-9]", "-", str(cwd))
    save(folder / f"{sid}.jsonl", [
        {"type": "user", "uuid": "a", "parentUuid": None, "cwd": str(cwd), "message": {"role": "user", "content": "<task>Fix bug</task>"}},
        {"type": "assistant", "uuid": "b", "parentUuid": "a", "cwd": str(cwd), "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "old answer"}]}},
        {"type": "user", "uuid": "c", "parentUuid": "b", "cwd": str(cwd), "message": {"role": "user", "content": "<task>More work</task>"}},
    ])
    result = session_import.read_claude(str(cwd), sid)
    assert result["prompt"] == "<task>Fix bug</task>"
    assert result["answer"] == ""


def test_claude_ambiguous_branch_returns_empty_text(tmp_path, monkeypatch):
    cwd = tmp_path / "p"
    cwd.mkdir()
    root = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    sid = "12345678-1234-1234-1234-123456789abc"
    folder = root / "projects" / __import__("re").sub(r"[^a-zA-Z0-9]", "-", str(cwd))
    file = folder / f"{sid}.jsonl"
    save(file, [
        {"type": "user", "uuid": "a", "parentUuid": None, "cwd": str(cwd), "message": {"role": "user", "content": "question"}},
        {"type": "assistant", "uuid": "b", "parentUuid": "a", "cwd": str(cwd), "message": {"role": "assistant"}},
        {"type": "assistant", "uuid": "c", "parentUuid": "a", "cwd": str(cwd), "message": {"role": "assistant"}},
    ])
    result = session_import.read_claude(str(cwd), sid)
    assert result["prompt"] == result["answer"] == ""


def test_invalid_claude_session_id_message(tmp_path):
    with pytest.raises(HiveError) as error:
        session_import.read_claude(str(tmp_path), "not-an-id")
    assert str(error.value) == "This is not a valid Claude session ID."


def test_claude_discovery_budget_is_shared_across_project_directories(tmp_path, monkeypatch):
    root = tmp_path / "claude" / "projects"
    for index in range(3):
        folder = root / f"project-{index}"
        folder.mkdir(parents=True)
        (folder / "one").write_text("")
        (folder / "two").write_text("")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root.parent))
    monkeypatch.setattr(session_import, "MAX_DISCOVERY_ENTRIES", 5)
    with pytest.raises(HiveError, match="Too many"):
        session_import.read_claude(str(tmp_path), "12345678-1234-1234-1234-123456789abc")


def test_claude_native_project_and_complete_answer(tmp_path, monkeypatch):
    cwd = tmp_path / "project"
    cwd.mkdir()
    root = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    sid = "12345678-1234-1234-1234-123456789abc"
    folder = root / "projects" / __import__("re").sub(r"[^a-zA-Z0-9]", "-", str(cwd))
    file = folder / f"{sid}.jsonl"
    save(file, [
        {"type": "user", "uuid": "a", "parentUuid": None, "cwd": str(cwd), "sessionId": sid,
         "message": {"role": "user", "content": "question"}},
        {"type": "assistant", "uuid": "b", "parentUuid": "a", "cwd": str(cwd), "sessionId": sid,
         "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "answer"}]}},
    ])
    before = file.read_bytes()
    result = session_import.read_claude(str(cwd), sid)
    assert (result["prompt"], result["answer"]) == ("question", "answer")
    assert file.read_bytes() == before
    copy = root / "projects" / "different" / f"{sid}.jsonl"
    save(copy, [])
    with pytest.raises(HiveError, match="ambiguous"):
        session_import.read_claude(str(cwd), sid)
