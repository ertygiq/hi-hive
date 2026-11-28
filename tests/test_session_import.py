import json

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
    with pytest.raises(HiveError, match="not found"):
        session_import.read_pi(str(cwd), [], "id")


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
