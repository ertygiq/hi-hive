import copy
import json
import os
import uuid

import pytest
from starlette.testclient import TestClient

from hihive.pool import PoolManager
from hihive.server import create_app

TRUSTED = {"Origin": "http://localhost"}
TOKEN = "test-secret"


class Api:
    def __init__(self, directory):
        config = os.path.join(directory, "config.json")
        with open(config, "w") as file:
            json.dump({"max_parallel": 1, "work_dir": directory, "default_harness": "pi",
                       "harnesses": [{"id": "pi", "name": "Pi", "type": "pi", "command": "pi", "args": []}]}, file)
        self.pool = PoolManager(config_file=config, persist_file=os.path.join(directory, "state.json"))
        self.client = TestClient(create_app(self.pool, TOKEN, manage_pool=False), base_url="http://localhost",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
        self.task_id = self.pool.add_task("base")
        self.task = self.pool._tasks[self.task_id]
        self.task["status"] = "failed"

    def chat(self, body):
        return self.client.post(f"/api/tasks/{self.task_id}/chat", json=body, headers=TRUSTED)


@pytest.fixture
def api(test_dir):
    return Api(test_dir)


def test_done_is_independent_of_execution_status_and_unmarking_succeeds(api):
    for status in ("queued", "running", "reviewing", "completed", "failed", "cancelled"):
        api.task["status"] = status
        for expected in (True, False):
            before = copy.deepcopy({k: v for k, v in api.task.items() if k != "done"})
            response = api.client.post(f"/api/tasks/{api.task_id}/done", json={}, headers=TRUSTED)
            assert response.status_code == 200
            assert response.json()["done"] is expected
            assert {k: v for k, v in api.task.items() if k != "done"} == before


def test_state_poll_skips_the_snapshot_only_while_unchanged(api):
    first = api.client.get("/api/state").json()
    since = f"{first['instance']}:{first['version']}"
    unchanged = api.client.get(f"/api/state?since={since}")
    assert unchanged.status_code == 204
    assert unchanged.content == b""
    api.pool.add_task("change")
    changed = api.client.get(f"/api/state?since={since}")
    assert changed.status_code == 200
    assert changed.json()["version"] > first["version"]
    assert api.client.get(f"/api/state?since=another-run:{first['version']}").status_code == 200


def test_done_for_missing_task_returns_not_found(api):
    response = api.client.post("/api/tasks/missing/done", json={}, headers=TRUSTED)
    assert response.status_code == 404
    assert response.json()["error"] == "Task not found"


def test_delete_running_task_returns_warning_without_cancelling(api):
    api.task["status"] = "running"
    response = api.client.delete(f"/api/tasks/{api.task_id}", headers=TRUSTED)
    assert response.status_code == 409
    assert "still working" in response.json()["error"]
    assert api.task["status"] == "running"
    assert not api.task["cancel_requested"]


def test_delete_completed_task_succeeds(api):
    api.task["status"] = "completed"
    response = api.client.delete(f"/api/tasks/{api.task_id}", headers=TRUSTED)
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert api.pool.task_detail(api.task_id) is None


def test_closing_runner_rejection_explains_that_the_process_is_stopping(api):
    class ClosingRunner:
        state = "closing"

        def drain(self):
            return []

    api.task["session_id"] = str(uuid.uuid4())
    api.task["runner"] = ClosingRunner()
    response = api.chat({"message": "hello"})
    assert response.status_code == 409
    assert response.json()["error"] == "The agent process is stopping; try again after it exits"
    api.task["runner"] = None


def test_unresolved_delivery_without_a_runner_has_a_specific_error(api):
    api.task["session_id"] = str(uuid.uuid4())
    api.task["messages"].append({"id": str(uuid.uuid4()), "role": "user", "kind": "follow_up", "content": "prior",
                                 "delivery": "accepted", "settled": False})
    response = api.chat({"message": "hello"})
    assert response.status_code == 409
    assert response.json()["error"] == "Previous messages have unresolved delivery; cannot resume the agent session yet"


def test_cancel_without_active_work_reports_a_conflict(api):
    response = api.client.post(f"/api/tasks/{api.task_id}/chat/cancel", json={}, headers=TRUSTED)
    assert response.status_code == 409
    assert response.json()["error"] == "No active conversation run to stop"


def test_validation_errors_are_json_for_the_ui(api):
    response = api.chat({"message": ""})
    assert response.status_code == 422
    assert "application/json" in response.headers["content-type"]
    assert "Message must contain text" in response.json()["error"]
    response = api.chat({"message": "hello", "message_id": 7})
    assert response.status_code == 422
    assert response.json()["error"] == "Invalid message ID"


def test_unconfigured_output_action_is_not_found(api):
    response = api.client.post("/api/output_actions/run-id/run", json={"value": "ab12"}, headers=TRUSTED)
    assert response.status_code == 404
    assert "not configured" in response.json()["error"]


def test_non_object_json_body_has_a_client_visible_error(api):
    response = api.chat(["not", "an", "object"])
    assert response.status_code == 400
    assert response.json()["error"] == "JSON body must be an object"


def test_duplicate_unknown_message_returns_acceptance_without_a_reply(api):
    message_id = str(uuid.uuid4())
    api.task["messages"].append({"id": message_id, "role": "user", "kind": "follow_up", "content": "prior",
                                 "sequence": 1, "delivery": "unknown", "settled": True})
    response = api.chat({"message": "prior", "message_id": message_id})
    assert response.status_code == 202
    data = response.json()
    assert data["message"]["status"] == "unknown"
    assert "assistant" not in data


@pytest.mark.parametrize("headers, status", [
    ({"Host": "evil.example"}, 403),
    ({}, 403),
    ({"Origin": "http://evil.example"}, 403),
    ({"Origin": "http://localhost:9999"}, 403),
    ({"Referer": "http://localhost/page"}, 200),
    ({"Origin": "http://localhost"}, 200),
])
def test_host_and_origin_checks_block_rebinding_and_cross_site_requests(api, headers, status):
    assert api.client.post("/api/control/pause", json={}, headers=headers).status_code == status


def test_state_changing_requests_require_json(api):
    response = api.client.post("/api/control/pause", content="{}", headers={**TRUSTED, "Content-Type": "text/plain"})
    assert response.status_code == 415
    assert response.json()["error"] == "Content-Type must be application/json"


def test_ui_is_served_without_caching(api):
    for path in ("/", "/app.js", "/style.css"):
        response = api.client.get(path)
        assert response.status_code == 200
        assert "no-store" in response.headers["cache-control"]


def test_api_requires_the_secret(api):
    for headers in ({"Authorization": ""}, {"Authorization": "Bearer wrong"}):
        response = api.client.get("/api/state", headers=headers)
        assert response.status_code == 401
        assert "hihive open" in response.json()["error"]
    # Faking Host and Origin, as a Docker container can, is not enough.
    response = api.client.post("/api/control/pause", json={}, headers={**TRUSTED, "Authorization": ""})
    assert response.status_code == 401
    assert api.pool.state()["paused"] is False


def test_login_link_sets_a_cookie_that_authorizes_the_browser(api):
    browser = TestClient(api.client.app, base_url="http://localhost")
    assert browser.get("/").status_code == 200  # The page itself holds no secret.
    assert browser.get("/api/state").status_code == 401
    page = browser.get("/login")
    assert page.status_code == 200 and "/login.js" in page.text
    assert browser.post("/login", json={"token": "wrong"}, headers=TRUSTED).status_code == 403
    assert browser.post("/login", json={"token": TOKEN}).status_code == 403  # Cross-site requests are refused.
    login = browser.post("/login", json={"token": TOKEN}, headers=TRUSTED)
    assert login.status_code == 204
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert browser.get("/api/state").status_code == 200


def test_secret_file_is_created_once_and_readable_only_by_the_user(test_dir, monkeypatch):
    from hihive import paths
    monkeypatch.setenv("HIVE_HOME", test_dir)
    token = paths.ensure_token()
    assert paths.ensure_token() == token == paths.read_token()
    assert os.stat(paths.token_file()).st_mode & 0o777 == 0o600
