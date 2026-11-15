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
