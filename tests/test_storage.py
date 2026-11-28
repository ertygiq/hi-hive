import json
import os

import pytest

from hihive import HiveError
from hihive import messages as tm
from hihive.output_actions import OutputActionError, normalize
from hihive.pool import PoolManager, fill_template


def write_config(directory, **extra):
    path = os.path.join(directory, "config.json")
    with open(path, "w") as file:
        json.dump({"max_parallel": 2, "work_dir": directory, "default_harness": "pi",
                   "harnesses": [{"id": "pi", "name": "Pi", "type": "pi", "command": "pi", "args": []}], **extra}, file)
    return path


def make_pool(directory, **extra):
    return PoolManager(config_file=write_config(directory, **extra), persist_file=os.path.join(directory, "state.json"))


def first_project(pool):
    return next(iter(pool._projects))


# --- Stored conversations ---

def test_invalid_stored_conversation_fails_instead_of_being_discarded():
    with pytest.raises(HiveError):
        tm.restore([])
    first = tm.message("user", "initial", "BASE")
    with pytest.raises(HiveError):
        tm.restore([first, first])
    assert tm.restore([first]) == [first]


def test_old_state_schema_is_refused(test_dir):
    with open(os.path.join(test_dir, "state.json"), "w") as file:
        json.dump({"tasks": {}, "task_order": []}, file)
    with pytest.raises(HiveError, match="schema"):
        make_pool(test_dir)


def test_state_round_trip_marks_interrupted_work_unknown_not_resent(test_dir):
    pool = make_pool(test_dir)
    task_id = pool.add_task("BASE")
    task = pool._tasks[task_id]
    task["status"] = "running"
    task["messages"][0].update(delivery="accepted", settled=False, sequence=1)
    pool._save_state()
    restored = make_pool(test_dir).task_detail(task_id)
    assert restored["status"] == "failed"
    assert restored["failure_details"] == "Task was interrupted by server shutdown."
    first = restored["messages"][0]
    assert (first["delivery"], first["settled"]) == ("unknown", True)
    assert "not resent" in first["error"]


def test_planned_reviews_and_queued_prompt_are_not_in_flight():
    task = {"messages": [tm.message("user", "initial", "BASE", delivery="scheduled")]}
    tm.schedule_reviews(task, ["CHECK"])
    assert all(m["delivery"] == "scheduled" for m in task["messages"])
    assert tm.outstanding(task) == []


def test_template_input_placeholder():
    assert fill_template("Explain %{input} at 100%%", "x") == "Explain x at 100%"
    assert fill_template("50% of %{input}", "y") == "50% of y"


# --- Settings and templates ---

def test_new_review_section_can_be_attached_in_the_same_save(test_dir):
    pool = make_pool(test_dir)
    p = first_project(pool)
    pool.update_config({
        "review_sections": [{"id": "r1", "project_id": p, "name": "Check", "prompt": "Check it"}],
        "templates": [{"id": "t1", "project_id": p, "name": "explain", "template": "Explain %{input}",
                       "review_section_ids": ["r1", "gone"]}],
    })
    assert pool.config()["templates"] == [{"id": "t1", "project_id": p, "name": "explain", "template": "Explain %{input}",
                                           "review_section_ids": ["r1"], "color": None, "work_dir": None,
                                           "agent_args": [], "harness": None}]


def test_rejected_templates_leave_the_whole_config_unchanged(test_dir):
    pool = make_pool(test_dir)
    p = first_project(pool)
    pool.update_config({"templates": [{"id": "t1", "project_id": p, "name": "explain", "template": "A"}]})
    with pytest.raises(HiveError, match="already taken"):
        pool.update_config({"max_parallel": 9,
                            "templates": [{"id": "t1", "project_id": p, "name": "explain", "template": "A"},
                                          {"id": "t2", "project_id": p, "name": "Explain", "template": "B"}]})
    assert pool.config()["max_parallel"] == 2
    assert [t["id"] for t in pool.config()["templates"]] == ["t1"]


def test_omitted_templates_are_deleted(test_dir):
    pool = make_pool(test_dir)
    p = first_project(pool)
    pool.update_config({"templates": [{"id": "t1", "project_id": p, "name": "a", "template": "A"},
                                      {"id": "t2", "project_id": p, "name": "b", "template": "B"}]})
    pool.update_config({"templates": [{"id": "t2", "project_id": p, "name": "b", "template": "B"}]})
    assert [t["id"] for t in pool.config()["templates"]] == ["t2"]


# --- Task provenance and labels ---


def test_imported_session_survives_task_deletion(test_dir, monkeypatch, mock_native):
    from hihive import session_import
    pool = make_pool(test_dir)
    project = first_project(pool)
    monkeypatch.setattr(session_import, "native_harness", lambda harness: True)
    monkeypatch.setattr("hihive.pool.shutil.which", lambda command: __import__("sys").executable)
    source = os.path.join(test_dir, "source.jsonl")
    original = b'{"type":"session"}\n'
    with open(source, "wb") as file:
        file.write(original)
    monkeypatch.setattr(session_import, "read_pi", lambda cwd, args, sid: {
        "session_id": sid, "prompt": "question", "answer": "answer", "message_count": None,
        "last_activity": None, "source": session_import._read(source)[1]})
    existing = pool.add_task("already running")
    pool._tasks[existing]["session_id"] = "session-id"
    with pytest.raises(HiveError, match="already belongs"):
        pool.preview_session_import(project, "pi", "session-id")
    pool.remove_task(existing)
    token = pool.preview_session_import(project, "pi", "session-id")["preview_token"]
    task_id = pool.import_session(project, "pi", "session-id", preview_token=token)
    assert pool.task_detail(task_id)["imported_session"]
    pool._verify_import_source(pool._tasks[task_id])
    runner = type("Runner", (), {"state": "exited"})()
    pool._apply_runner_event(pool._tasks[task_id], runner, "exited", {})
    assert pool._tasks[task_id]["import_interrupted"]
    pool._verify_import_source(pool._tasks[task_id])
    with open(source, "a") as file:
        file.write('{"type":"custom"}\n')
    with pytest.raises(HiveError, match="interrupted work"):
        pool._verify_import_source(pool._tasks[task_id])
    with open(source, "wb") as file:
        file.write(original)
    # Restore metadata only to continue exercising harness-configuration checks.
    pool._tasks[task_id]["imported_source"].update(session_import._read(source)[1])
    pool._harnesses[0]["args"] = ["--changed"]
    with pytest.raises(HiveError, match="changed"):
        pool._verify_import_source(pool._tasks[task_id])
    pool._harnesses[0]["args"] = []
    assert pool.retry_task(task_id) is False
    with pytest.raises(HiveError, match="already belongs"):
        pool.import_session(project, "pi", "session-id", preview_token=token)
    pool._save_state()
    restored = make_pool(test_dir)
    assert restored.task_detail(task_id)["imported_session"]
    assert restored.remove_task(task_id) is True
    with open(source, "rb") as file:
        assert file.read() == original


@pytest.mark.parametrize("reason", ["stop", "cancel", "transport_failure", "unsettled_exit"])
@pytest.mark.parametrize("changed", [False, True])
def test_interrupted_import_quarantines_only_changed_source(test_dir, monkeypatch, reason, changed, mock_native):
    from hihive import session_import
    pool = make_pool(test_dir)
    project = first_project(pool)
    monkeypatch.setattr(session_import, "native_harness", lambda harness: True)
    monkeypatch.setattr("hihive.pool.shutil.which", lambda command: __import__("sys").executable)
    source = os.path.join(test_dir, "session.jsonl")
    with open(source, "w") as file:
        file.write('{"type":"session"}\n')
    monkeypatch.setattr(session_import, "read_pi", lambda cwd, args, sid: {
        "session_id": sid, "prompt": "", "answer": "", "message_count": None,
        "last_activity": None, "source": session_import._read(source)[1]})
    token = pool.preview_session_import(project, "pi", "session-id")["preview_token"]
    task_id = pool.import_session(project, "pi", "session-id", preview_token=token)
    task = pool._tasks[task_id]
    runner = type("Runner", (), {"state": "exited"})()
    if reason in ("stop", "cancel"):
        task["cancel_requested"] = True
        assert not pool._apply_runner_event(task, runner, "settled", {})
    if reason == "transport_failure":
        pool._apply_runner_event(task, runner, "error", "transport lost")
    if changed:
        with open(source, "a") as file:
            file.write('{"type":"custom"}\n')
    pool._apply_runner_event(task, runner, "exited", {})
    if changed:
        with pytest.raises(HiveError, match="changed during interrupted work"):
            pool._verify_import_source(task)
    else:
        pool._verify_import_source(task)


def test_import_token_binds_content_and_save_failure_rolls_back(test_dir, monkeypatch, mock_native):
    from hihive import session_import
    pool = make_pool(test_dir)
    project = first_project(pool)
    monkeypatch.setattr(session_import, "native_harness", lambda harness: True)
    monkeypatch.setattr("hihive.pool.shutil.which", lambda command: __import__("sys").executable)
    source = os.path.join(test_dir, "source.jsonl")
    with open(source, "w") as file:
        file.write('{"type":"session"}\n')
    def read_source(cwd, args, sid):
        assert not pool._lock._is_owned()
        return {"session_id": sid, "prompt": "question", "answer": "answer", "message_count": None,
                "last_activity": None, "source": session_import._read(source)[1]}
    monkeypatch.setattr(session_import, "read_pi", read_source)
    token = pool.preview_session_import(project, "pi", "session-id")["preview_token"]
    with open(source, "a") as file:
        file.write('{"type":"custom"}\n')
    with pytest.raises(HiveError, match="preview again"):
        pool.import_session(project, "pi", "session-id", preview_token=token)
    token = pool.preview_session_import(project, "pi", "session-id")["preview_token"]
    real_save = pool._save_state
    monkeypatch.setattr(pool, "_save_state", lambda strict=False: (_ for _ in ()).throw(HiveError("save failed")))
    with pytest.raises(HiveError, match="Could not save"):
        pool.import_session(project, "pi", "session-id", preview_token=token)
    assert not any(t.get("session_id") == "session-id" for t in pool._tasks.values())
    monkeypatch.setattr(pool, "_save_state", real_save)
    task_id = pool.import_session(project, "pi", "session-id", preview_token=token)
    assert pool.task_detail(task_id)["imported_session"]
    with open(source, "a") as file:
        file.write('{"type":"message"}\n')
    with pytest.raises(HiveError, match="changed"):
        pool._verify_import_source(pool._tasks[task_id])


def test_import_verification_runs_outside_pool_lock_and_rechecks_state(test_dir, monkeypatch, mock_native):
    from hihive import session_import
    pool = make_pool(test_dir)
    project = first_project(pool)
    monkeypatch.setattr(session_import, "native_harness", lambda harness: True)
    monkeypatch.setattr("hihive.pool.shutil.which", lambda command: __import__("sys").executable)
    source = os.path.join(test_dir, "session.jsonl")
    with open(source, "w") as file:
        file.write('{"type":"session"}\n')
    monkeypatch.setattr(session_import, "read_pi", lambda cwd, args, sid: {
        "session_id": sid, "prompt": "question", "answer": "answer", "message_count": None,
        "last_activity": None, "source": session_import._read(source)[1]})
    token = pool.preview_session_import(project, "pi", "session-id")["preview_token"]
    task_id = pool.import_session(project, "pi", "session-id", preview_token=token)
    original = pool._verify_import_snapshot
    def race(snapshot):
        assert not pool._lock._is_owned()
        original(snapshot)
        with pool._lock:
            pool._tasks[task_id]["imported_source"]["digest"] = "changed"
    monkeypatch.setattr(pool, "_verify_import_snapshot", race)
    with pytest.raises(HiveError, match="changed"):
        pool.send_chat(task_id, "follow-up", imported_session_confirmed=True)
    assert not pool._tasks[task_id].get("runner")


@pytest.mark.parametrize("verification_fails", [False, True])
def test_concurrent_imported_duplicate_returns_first_receipt(test_dir, monkeypatch, mock_native, verification_fails):
    import threading
    import uuid
    from concurrent.futures import ThreadPoolExecutor
    from hihive import session_import
    pool = make_pool(test_dir)
    project = first_project(pool)
    source = os.path.join(test_dir, "session.jsonl")
    with open(source, "w") as file:
        file.write('{"type":"session"}\n')
    monkeypatch.setattr(session_import, "read_pi", lambda cwd, args, sid: {
        "session_id": sid, "prompt": "question", "answer": "answer", "message_count": None,
        "last_activity": None, "source": session_import._read(source)[1]})
    token = pool.preview_session_import(project, "pi", "session-id")["preview_token"]
    task_id = pool.import_session(project, "pi", "session-id", preview_token=token)
    message_id = str(uuid.uuid4())
    waiting, release = threading.Event(), threading.Event()
    original = pool._verify_import_snapshot
    def verify(snapshot):
        if threading.current_thread().name.startswith("duplicate"):
            waiting.set()
            assert release.wait(5)
            if verification_fails:
                raise HiveError("Source changed during verification")
            return
        original(snapshot)
    monkeypatch.setattr(pool, "_verify_import_snapshot", verify)
    submissions = []
    def start(task, message, kind, resume, message_id):
        submissions.append(message_id)
        entry = tm.message("user", "follow_up", message, id=message_id, sequence=1, delivery="accepted", settled=False)
        task["messages"].append(entry)
        task["imported_source"]["digest"] = "changed"
        task["import_refresh_needed"] = True
        return entry
    monkeypatch.setattr(pool, "_start_live_runner", start)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="duplicate") as executor:
        duplicate = executor.submit(pool.send_chat, task_id, "hello", message_id, True)
        assert waiting.wait(5)
        first = pool.send_chat(task_id, "hello", message_id, True)
        release.set()
        second = duplicate.result(timeout=5)
    assert first == second
    assert submissions == [message_id]


def test_created_by_task_id_persists_is_exposed_and_does_not_cascade(test_dir):
    pool = make_pool(test_dir)
    creator = pool.add_task("creator")
    child = pool.add_task("child", created_by_task_id=creator)
    human = pool.add_task("human")

    assert pool.task_detail(child)["created_by_task_id"] == creator
    assert pool.task_detail(human)["created_by_task_id"] is None
    assert next(task for task in pool.state()["queue"] if task["id"] == child)["created_by_task_id"] == creator

    pool.stop()
    restored = make_pool(test_dir)
    assert restored.task_detail(child)["created_by_task_id"] == creator
    assert restored.remove_task(creator) is True
    assert restored.task_detail(child) is not None


def test_task_label_uses_shared_normalization_and_persists(test_dir):
    pool = make_pool(test_dir)
    task_id = pool.add_task("prompt")
    version = pool.version

    assert pool.set_task_label(task_id, "  New\n\tlabel  ") == "New label"
    assert pool.task_detail(task_id)["label"] == "New label"
    assert pool.version == version + 1
    assert pool.set_task_label(task_id, "x" * 250) == "x" * 200
    with pytest.raises(HiveError, match="cannot be empty"):
        pool.set_task_label(task_id, " \n\t ")
    assert pool.set_task_label("missing", "label") is None

    pool.stop()
    restored = make_pool(test_dir)
    assert restored.task_detail(task_id)["label"] == "x" * 200


# --- Output actions ---

def value_action(directory, command=None, pattern="[a-z0-9]{4}"):
    import sys
    return {"tag": "run-id", "label": "Open run", "pattern": pattern, "command": command or sys.executable,
            "args": ["-c", "import sys; open(sys.argv[1], 'w').write(sys.argv[2])", os.path.join(directory, "marker"),
                     "id={value}"]}


def test_click_runs_the_command_with_the_value_as_one_argument(test_dir):
    assert make_pool(test_dir, output_actions=[value_action(test_dir)]).run_output_action("run-id", "ab12") is None
    with open(os.path.join(test_dir, "marker")) as file:
        assert file.read() == "id=ab12"


def test_output_actions_do_not_receive_an_inherited_task_id(test_dir, monkeypatch):
    import sys
    marker = os.path.join(test_dir, "task-id")
    action = {"tag": "run-id", "label": "Run", "pattern": ".+", "command": sys.executable,
              "args": ["-c", "import os,sys; open(sys.argv[1], 'w').write(os.environ.get('HIVE_TASK_ID', ''))",
                       marker]}
    monkeypatch.setenv("HIVE_TASK_ID", "wrong-inherited-id")

    assert make_pool(test_dir, output_actions=[action]).run_output_action("run-id", "value") is None
    with open(marker) as file:
        assert file.read() == ""


def test_value_must_fully_match_the_pattern(test_dir):
    error = make_pool(test_dir, output_actions=[value_action(test_dir)]).run_output_action("run-id", "ab12; touch x")
    assert "does not match the pattern" in error
    assert not os.path.exists(os.path.join(test_dir, "marker"))


def test_failure_reports_exit_status_and_output(test_dir):
    import sys
    action = {"tag": "run-id", "pattern": r"\w+", "command": sys.executable,
              "args": ["-c", "import sys; print('no run {value}', file=sys.stderr); sys.exit(3)"]}
    error = make_pool(test_dir, output_actions=[action]).run_output_action("run-id", "ab12")
    assert "exited with status 3" in error
    assert "no run ab12" in error


def test_missing_command_and_unknown_tag_are_reported(test_dir):
    pool = make_pool(test_dir, output_actions=[value_action(test_dir, command=os.path.join(test_dir, "missing tool"))])
    assert "Could not start" in pool.run_output_action("run-id", "ab12")
    assert pool.run_output_action("other", "ab12") == "not_found"


@pytest.mark.parametrize("action, message", [
    ({"tag": "Run ID", "command": "x", "pattern": "."}, "lowercase"),
    ({"tag": "run-id", "command": "", "pattern": "."}, "needs a command"),
    ({"tag": "run-id", "command": "x"}, "needs a pattern"),
    ({"tag": "run-id", "command": "x", "pattern": "("}, "invalid pattern"),
])
def test_invalid_output_action_stops_startup_with_a_clear_message(test_dir, action, message):
    with pytest.raises(OutputActionError, match=message):
        make_pool(test_dir, output_actions=[action])


def test_duplicate_output_action_tag_stops_startup(test_dir):
    with pytest.raises(OutputActionError, match="more than once"):
        make_pool(test_dir, output_actions=[value_action(test_dir), value_action(test_dir)])


def test_settings_save_keeps_output_actions_in_the_config_file(test_dir):
    pool = make_pool(test_dir, output_actions=[value_action(test_dir)])
    pool.update_config({"max_parallel": 3})
    with open(os.path.join(test_dir, "config.json")) as file:
        saved = json.load(file)
    assert saved["output_actions"] == [normalize(value_action(test_dir))]
    assert pool.config()["output_actions"][0]["tag"] == "run-id"
