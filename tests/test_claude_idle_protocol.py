from types import SimpleNamespace

import pytest

import hihive.protocol as protocol_module

from hihive import HiveError
from hihive.protocol import ClaudeStreamProtocol


class Runner:
    def __init__(self):
        self.activity_version = 0
        self.controls = []
        self.warnings = 0

    def control(self, record):
        self.controls.append(record)

    def touch_activity(self):
        self.activity_version += 1

    def idle_shutdown_uncertain(self):
        self.warnings += 1


@pytest.fixture
def protocol():
    return ClaudeStreamProtocol()


@pytest.fixture
def runner():
    return Runner()


def probe(protocol, runner):
    assert not protocol.idle_shutdown_ready(runner)
    return runner.controls[-1]["request_id"]


def response(protocol, runner, request_id, state="idle"):
    protocol.idle_probe_response(runner, {"response": {
        "subtype": "success", "request_id": request_id, "response": {"session_state": state},
        "pending_permission_requests": [], "pending_user_dialog_requests": []}})


def snapshot(protocol, runner, *ids):
    protocol.background_tasks_changed(runner, {"tasks": [{"task_id": i} for i in ids]})


def test_idle_response_requires_a_subsequent_empty_snapshot(protocol, runner):
    request_id = probe(protocol, runner)
    snapshot(protocol, runner)
    response(protocol, runner, request_id)
    assert not protocol.idle_shutdown_ready(runner), "An earlier snapshot is not proof for this query"
    snapshot(protocol, runner)
    assert protocol.idle_shutdown_ready(runner)


def test_new_input_invalidates_a_successful_probe(protocol, runner):
    request_id = probe(protocol, runner)
    response(protocol, runner, request_id)
    snapshot(protocol, runner)
    runner.touch_activity()
    probe(protocol, runner)
    assert runner.controls[-1]["request_id"] != request_id


def test_snapshot_replaces_tasks_and_never_ignores_ambient_work(protocol, runner):
    snapshot(protocol, runner, "one", "two")
    assert not protocol.idle_shutdown_ready(runner)
    snapshot(protocol, runner, "two")
    assert not protocol.idle_shutdown_ready(runner)
    protocol.background_tasks_changed(runner, {"tasks": [{"task_id": "watcher", "ambient": True}]})
    assert not protocol.idle_shutdown_ready(runner)
    snapshot(protocol, runner)
    request_id = probe(protocol, runner)
    response(protocol, runner, request_id)
    snapshot(protocol, runner)
    assert protocol.idle_shutdown_ready(runner)


def test_running_or_waiting_for_action_is_not_safe_to_close(protocol, runner):
    for state in ("running", "requires_action"):
        request_id = probe(protocol, runner)
        response(protocol, runner, request_id, state=state)
        snapshot(protocol, runner)
        assert not protocol.idle_shutdown_ready(runner)


def test_bad_probe_response_disables_idle_shutdown_without_killing_work(protocol, runner):
    request_id = probe(protocol, runner)
    protocol.idle_probe_response(runner, {"response": {"subtype": "error", "request_id": request_id}})
    assert not protocol.idle_shutdown_ready(runner)
    assert runner.warnings == 1


def test_missing_probe_response_disables_shutdown_at_five_seconds(protocol, runner, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(protocol_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    probe(protocol, runner)
    now[0] = 104.99
    assert not protocol.idle_shutdown_ready(runner)
    assert runner.warnings == 0
    now[0] = 105.0
    assert not protocol.idle_shutdown_ready(runner)
    assert runner.warnings == 1
    now[0] = 120.0
    assert not protocol.idle_shutdown_ready(runner)
    assert runner.warnings == 1


def test_malformed_task_snapshot_is_not_treated_as_empty(protocol, runner):
    with pytest.raises(HiveError):
        protocol.background_tasks_changed(runner, {"tasks": None})
    with pytest.raises(HiveError):
        protocol.background_tasks_changed(runner, {"tasks": [{}]})
