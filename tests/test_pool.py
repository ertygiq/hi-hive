import json
import os
import sys
import uuid
from datetime import UTC, datetime

import pytest

from hihive import HiveError
from hihive import messages as tm
from hihive.pool import PoolManager
from conftest import FIXTURE, touch, wait_until


class Pool:
    """A pool whose only harness is the fake Pi harness."""

    def __init__(self, directory):
        self.dir = directory
        self.config = os.path.join(directory, "config.json")
        self.persist = os.path.join(directory, "state.json")
        self.write_config("pi")
        self.pool = PoolManager(config_file=self.config, persist_file=self.persist)
        self.pool.start()
        self.task = self.pool.add_task("BASE")
        self.wait(lambda: os.path.exists(self.path("started")))

    def write_config(self, kind, scenario=None):
        args = [FIXTURE, "--fake-kind", kind, "--fake-root", self.dir] + (["--fake-case", scenario] if scenario else [])
        with open(self.config, "w") as file:
            json.dump({"max_parallel": 1, "work_dir": self.dir, "default_harness": "pi",
                       "harnesses": [{"id": "pi", "name": "Pi", "type": kind, "command": sys.executable,
                                      "args": args}]}, file)

    def restart(self, start=False):
        self.pool.stop()
        self.pool = PoolManager(config_file=self.config, persist_file=self.persist)
        if start:
            self.pool.start()

    def path(self, name):
        return os.path.join(self.dir, name)

    def wait(self, condition):
        return wait_until(condition, 10, describe=lambda: repr(self.detail()))

    def detail(self, task_id=None):
        return self.pool.task_detail(task_id or self.task)

    def messages(self):
        return self.detail()["messages"]

    def answers(self, kind=None):
        return [m for m in self.messages() if m["role"] == "assistant" and (not kind or m["kind"] == kind)]

    def find(self, message_id):
        return next(m for m in self.messages() if m["id"] == message_id)

    def internal(self, task_id=None):
        return self.pool._tasks[task_id or self.task]

    def settle(self):
        touch(self.dir, "release")
        self.wait(lambda: self.detail()["status"] == "completed")

    def result_card(self):
        return next((t for t in self.pool.state()["results"] if t["id"] == self.task), None)

    def completed_card(self):
        self.settle()
        with self.pool._lock:
            self.internal()["completed_at"] = datetime(2020, 1, 1, tzinfo=UTC)
        return self.result_card()

    def assert_result_placement(self, original):
        card = self.result_card()
        assert card is not None
        assert card["completed_at"] == original["completed_at"]
        assert not any(t["id"] == self.task for t in self.pool.state()["workers"])
        return card

    def inputs(self):
        with open(self.path("inputs")) as file:
            return [json.loads(line) for line in file]


@pytest.fixture
def p(test_dir):
    pool = Pool(test_dir)
    yield pool
    pool.pool.stop()


def test_completed_task_with_idle_process_is_deleted_in_one_call(p):
    p.settle()
    runner = p.internal()["runner"]
    assert runner.state == "idle"
    assert p.pool.remove_task(p.task) is True
    assert runner.state == "exited"
    assert p.detail() is None
    p.pool.stop()
    with open(p.persist) as file:
        assert p.task not in json.load(file)["tasks"]


def test_deleting_active_task_warns_without_stopping_it(p):
    runner = p.internal()["runner"]
    assert p.pool.remove_task(p.task) == "running_in_subtree"
    assert runner.state == "active"
    assert p.detail()["status"] == "running"
    p.settle()


def test_completed_parent_cannot_be_deleted_until_active_fork_finishes(p):
    p.settle()
    os.remove(p.path("release"))
    child = p.pool.fork_task(parent_id=p.task, prompt="CHILD")
    p.wait(lambda: p.detail(child)["runner_state"] == "active")
    assert p.pool.remove_task(p.task) == "running_in_subtree"
    assert p.detail()["runner_state"] == "idle"
    assert p.detail(child)["runner_state"] == "active"
    touch(p.dir, "release")
    p.wait(lambda: p.detail(child)["status"] == "completed")
    assert p.pool.remove_task(p.task) is True
    assert p.detail() is None
    assert p.detail(child) is None


def test_active_routing_two_messages_and_normal_follow_up(p):
    with open(p.path("pid")) as file:
        pid = file.read()
    a = p.pool.send_chat(p.task, "ONE")
    b = p.pool.send_chat(p.task, "TWO")
    assert a["status"] == "accepted"
    assert b["sequence"] > a["sequence"]
    assert p.answers() == []
    p.wait(lambda: all(m.get("harness_accepted") for m in p.messages() if m["role"] == "user"))
    p.settle()
    assert [m["kind"] for m in p.messages()] == ["initial", "steering", "steering", "initial"]
    assert len(p.answers()) == 1
    assert "ONE TWO" in p.answers()[0]["content"]
    assert p.result_card()["latest_follow_up_reply_at"] is None
    p.pool.send_chat(p.task, "FOLLOW_UP")
    p.wait(lambda: p.detail()["status"] == "completed" and len(p.answers()) == 2)
    assert p.answers()[-1]["kind"] == "follow_up"
    expected = datetime.fromisoformat(p.answers()[-1]["timestamp"]).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert p.result_card()["latest_follow_up_reply_at"] == expected
    assert p.detail()["latest_follow_up_reply_at"] == expected
    with open(p.path("pid")) as file:
        assert file.read() == pid


def test_follow_up_keeps_result_placement_during_work_after_reply_and_restart(p):
    original = p.completed_card()
    p.pool.send_chat(p.task, "REVIEW_GATE")
    p.wait(lambda: os.path.exists(p.path("review-started")))
    card = p.assert_result_placement(original)
    assert card["status"] == "completed"
    assert card["chat_pending"]
    assert p.task in p.pool.search_result_ids("REVIEW_GATE")["ids"]
    assert p.detail()["status"] == "running"  # Execution and board placement are independent.
    assert sum(1 for t in p.pool._tasks.values() if t["status"] in ("running", "reviewing")) == 1
    assert p.pool.toggle_done(p.task) is True
    assert p.detail()["status"] == "running"
    p.pool.send_chat(p.task, "MORE")
    assert p.detail()["done"]
    p.assert_result_placement(original)
    p.settle()
    assert not p.assert_result_placement(original)["chat_pending"]
    p.restart()
    p.assert_result_placement(original)
    assert p.detail()["done"]


def test_resumed_follow_up_and_cancellation_keep_result_placement(p):
    original = p.completed_card()
    p.internal()["runner"].close("Test idle expiry")
    p.wait(lambda: p.detail()["runner_state"] == "exited")
    assert p.pool.toggle_done(p.task) is True
    p.pool.send_chat(p.task, "REVIEW_GATE")
    assert p.detail()["done"]
    p.wait(lambda: os.path.exists(p.path("review-started")))
    p.assert_result_placement(original)
    assert p.pool.cancel_chat(p.task)
    assert p.assert_result_placement(original)["status"] == "cancelled"
    p.wait(lambda: p.detail()["runner_state"] == "exited")
    assert p.pool.continue_task(p.task)
    assert p.detail()["done"]
    assert p.result_card() is None
    assert any(t["id"] == p.task for t in p.pool.state()["workers"])
    p.settle()
    assert p.result_card()["completed_at"] != original["completed_at"]


def test_failed_follow_up_keeps_position_but_retry_clears_it(p):
    original = p.completed_card()
    p.pool.send_chat(p.task, "CRASH")
    p.wait(lambda: p.detail()["status"] == "failed" and p.detail()["runner_state"] == "exited")
    assert p.assert_result_placement(original)["status"] == "failed"
    assert p.result_card()["latest_follow_up_reply_at"] is None
    assert p.pool.toggle_done(p.task) is True
    assert p.pool.retry_task(p.task)
    assert p.detail()["done"]
    assert p.result_card() is None
    assert any(t["id"] == p.task for t in p.pool.state()["queue"])


def test_canonical_storage_has_no_conversation_or_diagnostic_copies(p):
    receipt = p.pool.send_chat(p.task, "ONE")
    p.wait(lambda: p.find(receipt["id"]).get("harness_accepted"))
    p.settle()
    p.pool.stop()
    with open(p.persist) as file:
        state = json.load(file)
    assert state["schema_version"] == 3
    task = state["tasks"][p.task]
    for key in ("prompt", "output", "output_in_chat", "review_prompts", "review_output", "review_entries",
                "chat_history", "live_messages", "diagnostics"):
        assert key not in task
        if key != "diagnostics":
            assert key not in p.detail()
    assert sum(1 for m in task["messages"] if m["content"] == "BASE") == 1
    assert sum(1 for m in task["messages"] if m["role"] == "assistant") == 1
    assert p.task in p.pool.search_result_ids("ONE")["ids"]


def test_duplicate_prevention_survives_restart_without_resending(p):
    message_id = str(uuid.uuid4())
    first = p.pool.send_chat(p.task, "ONCE", message_id=message_id)
    assert p.pool.send_chat(p.task, "ONCE", message_id=message_id)["id"] == first["id"]
    with pytest.raises(HiveError):
        p.pool.send_chat(p.task, "DIFFERENT", message_id=message_id)
    p.restart()
    assert p.pool.send_chat(p.task, "ONCE", message_id=message_id)["status"] in ("unknown", "failed")
    assert sum(1 for event in p.inputs() if event.get("id") == message_id) <= 1


def test_cancel_stops_initial_and_follow_up_runs_with_all_steering_inputs(p):
    for follow_up in (False, True):
        if follow_up:
            assert p.pool.continue_task(p.task)
            p.settle()
            p.pool.send_chat(p.task, "REVIEW_GATE")
            p.wait(lambda: os.path.exists(p.path("review-started")))
        receipt = p.pool.send_chat(p.task, "MORE")
        runner = p.internal()["runner"]
        assert p.pool.cancel_chat(p.task)
        assert p.detail()["status"] == "cancelled"
        p.wait(lambda: runner.state == "exited")
        p.wait(lambda: p.find(receipt["id"])["settled"])
        assert tm.outstanding(p.internal()) == []
        assert not p.pool.cancel_chat(p.task)


def test_background_badges_update_board_version_without_changing_result_placement(p):
    p.pool.stop()
    p.write_config("claude-code", scenario="background-activity")
    p.persist = p.path("background-state.json")
    p.pool = PoolManager(config_file=p.config, persist_file=p.persist)
    p.pool.start()
    p.task = p.pool.add_task("BASE")
    p.settle()
    p.wait(lambda: p.detail()["background_activity"]["task_count"] == 2)
    original = p.result_card()
    assert original["background_activity"] == {"task_count": 2, "preparing_reply": False}
    version = p.pool.state()["version"]
    touch(p.dir, "background-finished")
    p.wait(lambda: p.detail()["background_activity"]["preparing_reply"])
    p.wait(lambda: p.pool.state()["version"] > version)
    card = p.result_card()
    assert card["background_activity"]["preparing_reply"]
    assert card["completed_at"] == original["completed_at"]
    assert card["status"] == "completed"
    touch(p.dir, "background-reply")
    p.wait(lambda: any(m["content"] == "Background finished" for m in p.answers()))
    assert not p.detail()["background_activity"]["preparing_reply"]
    assert p.result_card()["completed_at"] == original["completed_at"]
    p.pool.stop()
    with open(p.persist) as file:
        assert "background_activity" not in json.load(file)["tasks"][p.task]
    p.pool = PoolManager(config_file=p.config, persist_file=p.persist)
    assert p.detail()["background_activity"] == {"task_count": 0, "preparing_reply": False}


def test_background_reply_and_failure_notice_are_persisted_in_conversation(p):
    p.settle()
    runner = p.internal()["runner"]
    runner.claude_background_result("background-result", "Background finished", None)
    runner.claude_background_result("background-result", "Background finished", None)
    runner.claude_background_result("background-failure", "", "Background failed")
    p.wait(lambda: any("Background task failed:" in m["content"] for m in p.messages()))
    assert sum(1 for m in p.answers() if m["content"] == "Background finished") == 1
    assert p.answers()[-1]["input_ids"] is None
    assert p.detail()["status"] == "completed"
    p.pool.stop()
    with open(p.persist) as file:
        saved = json.load(file)["tasks"][p.task]["messages"]
    assert sum(1 for m in saved if m["content"] == "Background finished") == 1
    assert any(m["role"] == "system" and "Background task failed:" in m["content"] for m in saved)


def test_failure_and_cancellation(p):
    p.pool.send_chat(p.task, "CRASH")
    p.wait(lambda: p.detail()["status"] == "failed" and p.detail()["runner_state"] == "exited")
    assert "Harness exited" in p.detail()["failure_details"]
    assert any(m["role"] == "system" and "Runner stopped:" in m["content"] for m in p.messages())
    assert p.answers() == []
    assert next(m for m in p.messages() if m["role"] == "user")["delivery"] == "unknown"
    touch(p.dir, "release")
    assert p.pool.continue_task(p.task)
    p.wait(lambda: p.detail()["status"] == "completed")
    assert p.answers()[-1]["kind"] == "continue"


def test_validation_error_does_not_poison_active_runner(p):
    with pytest.raises(HiveError):
        p.pool.send_chat(p.task, "")
    with pytest.raises(HiveError):
        p.pool.send_chat(p.task, "x", message_id="bad")
    p.pool.send_chat(p.task, "VALID")
    p.settle()
    assert p.detail()["status"] == "completed"


def test_cancellation_blocks_resume_until_old_child_reaped(p):
    assert p.pool.cancel_task(p.task)
    assert p.detail()["status"] == "cancelled"
    assert not p.pool.continue_task(p.task)
    p.wait(lambda: p.detail()["runner_state"] == "exited")
    touch(p.dir, "release")
    assert p.pool.continue_task(p.task)
    p.wait(lambda: p.detail()["status"] == "completed")


def schedule_reviews(p, *prompts):
    with p.pool._lock:
        tm.schedule_reviews(p.internal(), prompts)


def test_reviews_are_serialized_and_identified_in_same_process(p):
    schedule_reviews(p, "REVIEW_ONE", "REVIEW_TWO")
    with open(p.path("pid")) as file:
        pid = file.read()
    p.settle()
    p.wait(lambda: p.detail()["status"] == "completed" and len(p.answers("review")) == 2)
    with open(p.path("pid")) as file:
        assert file.read() == pid
    assert len(p.answers("initial")) == 1
    groups = {}
    for message in p.messages():
        if message["kind"] == "review":
            groups.setdefault(message["review_id"], []).append(message["role"])
    assert len(groups) == 2
    assert all(roles == ["user", "assistant"] for roles in groups.values())
    p.pool.send_chat(p.task, "NORMAL_FOLLOW_UP")
    p.wait(lambda: p.detail()["status"] == "completed" and len(p.answers("follow_up")) == 1)
    assert len(p.answers("review")) == 2


def test_steering_during_review_has_one_shared_answer(p):
    schedule_reviews(p, "REVIEW_GATE")
    with open(p.path("pid")) as file:
        pid = file.read()
    touch(p.dir, "release")
    p.wait(lambda: os.path.exists(p.path("review-started")))
    receipt = p.pool.send_chat(p.task, "REVIEW_STEERING")
    p.wait(lambda: p.find(receipt["id"]).get("harness_accepted"))
    assert p.detail()["status"] == "reviewing"
    p.settle()
    with open(p.path("pid")) as file:
        assert file.read() == pid
    reviews = p.answers("review")
    assert len(reviews) == 1
    assert "REVIEW_STEERING" in reviews[0]["content"]
    assert reviews[0]["review_id"] == p.find(receipt["id"])["review_id"]


def test_late_duplicate_after_retry_uses_hash_receipt_not_copied_content(p):
    message_id = str(uuid.uuid4())
    p.pool.send_chat(p.task, "ONCE", message_id=message_id)
    p.wait(lambda: p.find(message_id).get("harness_accepted"))
    p.settle()
    p.pool.pause()
    assert p.pool.retry_task(p.task)
    assert p.detail()["status"] == "queued"
    assert p.pool.send_chat(p.task, "ONCE", message_id=message_id)["settled"]
    with pytest.raises(HiveError):
        p.pool.send_chat(p.task, "OTHER", message_id=message_id)
    assert p.detail()["status"] == "queued"
    p.restart()
    assert p.pool.send_chat(p.task, "ONCE", message_id=message_id)["settled"]
    assert len(p.messages()) == 1
    with open(p.persist) as file:
        assert "ONCE" not in file.read()
    assert sum(1 for event in p.inputs() if event.get("id") == message_id) == 1


def test_diagnostics_live_only_in_journal_and_survive_restart(p):
    p.pool.send_chat(p.task, "CRASH")
    p.wait(lambda: p.detail()["runner_state"] == "exited")
    journal = p.path(os.path.join("runner-diagnostics", f"{p.task}.jsonl"))
    with open(journal) as file:
        text = file.read()
    assert "child_exit" in text
    assert "CRASH" not in text
    p.restart()
    assert any(record["kind"] == "child_exit" for record in p.detail()["diagnostics"])
    with open(p.persist) as file:
        assert "diagnostics" not in json.load(file)["tasks"][p.task]
    os.remove(journal)
    assert p.detail()["diagnostics"] == []


def test_post_exit_follow_up_resumes_same_session(p):
    p.settle()
    session = p.detail()["session_id"]
    p.internal()["runner"].close("idle shutdown")
    p.wait(lambda: p.detail()["runner_state"] == "exited")
    p.pool.send_chat(p.task, "AFTER_EXIT")
    p.wait(lambda: p.detail()["status"] == "completed")
    assert p.detail()["session_id"] == session
