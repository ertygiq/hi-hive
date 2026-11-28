import copy
import io
import json
import os
import re
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.testclient import TestClient

from hihive.pool import PoolManager
from hihive.server import LoginTickets, create_app

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


def test_session_import_routes_refuse_agent_tokens(api):
    project = api.task["project_id"]
    runner = type("Runner", (), {"state": "idle"})()
    token = api.pool._agent_tokens.issue(project, api.task_id, runner)
    headers = {**TRUSTED, "Authorization": f"Bearer {token}"}
    for path in ("/api/session-import/preview", "/api/session-import"):
        result = api.client.post(path, json={"project_id": project, "harness_id": "pi", "session_id": "id"}, headers=headers)
        assert result.status_code == 403


def test_session_import_preview_and_create(api, monkeypatch, tmp_path, mock_native):
    from hihive import session_import
    monkeypatch.setattr(session_import, "native_harness", lambda harness: True)
    monkeypatch.setattr("hihive.pool.shutil.which", lambda command: __import__("sys").executable)
    path = tmp_path / "session.jsonl"
    path.write_text('{"type":"session"}\n')
    monkeypatch.setattr(session_import, "read_pi", lambda cwd, args, sid: {
        "session_id": sid, "prompt": "question", "answer": "answer", "message_count": None,
        "last_activity": None, "source": session_import._read(path)[1]})
    body = {"project_id": api.task["project_id"], "harness_id": "pi", "session_id": "session-id", "label": "My session"}
    preview = api.client.post("/api/session-import/preview", json=body, headers=TRUSTED)
    assert preview.status_code == 200
    assert {k: v for k, v in preview.json().items() if k != "preview_token"} == {
        "session_id": "session-id", "prompt": "question", "answer": "answer", "message_count": None, "last_activity": None}
    body["preview_token"] = preview.json()["preview_token"]
    result = api.client.post("/api/session-import", json=body, headers=TRUSTED)
    assert result.status_code == 201
    task = api.pool.task_detail(result.json()["id"])
    assert task["imported_session"] and task["label"] == "My session"
    assert task["started_at"] == task["completed_at"] and not task["imported_session_confirmed"]
    url = f"/api/tasks/{task['id']}/chat"
    assert api.client.post(url, json={"message": "follow-up"}, headers=TRUSTED).status_code == 409
    runner = type("Runner", (), {"state": "idle"})()
    token = api.pool._agent_tokens.issue(task["project_id"], api.task_id, runner)
    agent_headers = {**TRUSTED, "Authorization": f"Bearer {token}"}
    assert api.client.post(url, json={"message": "follow-up", "imported_session_confirmed": True}, headers=agent_headers).status_code == 403
    assert api.client.post("/api/session-import", json=body, headers=TRUSTED).status_code == 409
    assert path.read_text() == '{"type":"session"}\n'


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


def test_task_label_is_cleaned_and_missing_or_empty_labels_are_rejected(api):
    response = api.client.post(f"/api/tasks/{api.task_id}/label", json={"label": "  New\nlabel  "}, headers=TRUSTED)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "label": "New label"}
    assert api.pool.task_detail(api.task_id)["label"] == "New label"

    response = api.client.post(f"/api/tasks/{api.task_id}/label", json={"label": "  "}, headers=TRUSTED)
    assert response.status_code == 400
    assert response.json()["error"] == "Label cannot be empty"
    response = api.client.post("/api/tasks/missing/label", json={"label": "new"}, headers=TRUSTED)
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


def test_foreground_server_refuses_restart_and_reports_it_unavailable(api, monkeypatch):
    monkeypatch.delenv("HIVE_SUPERVISOR", raising=False)
    config = api.client.get("/api/config").json()
    assert config["restart_available"] is False
    assert config["service_manager"] is None
    response = api.client.post("/api/control/restart", json={}, headers=TRUSTED)
    assert response.status_code == 409
    assert "installed service" in response.json()["error"]


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
    responses = {}
    for path in ("/", "/app.js", "/style.css"):
        response = api.client.get(path)
        responses[path] = response.text
        assert response.status_code == 200
        assert "no-store" in response.headers["cache-control"]
    assert "to-review-btn" not in responses["/"]
    assert "showToReview" not in responses["/app.js"]
    assert ".to-review" not in responses["/style.css"]


def test_backup_is_full_token_only_and_contains_exactly_the_allowlisted_snapshot(api, test_dir):
    excluded = {
        "token": "LOGIN-TOKEN-MARKER",
        "logs/server.log": "LOG-MARKER",
        "runner-diagnostics/task.jsonl": "DIAGNOSTIC-MARKER",
    }
    for relative, marker in excluded.items():
        path = os.path.join(test_dir, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as file:
            file.write(marker)
    response = api.client.post("/api/backup", json={}, headers=TRUSTED)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert re.fullmatch(r'attachment; filename="hi-hive-backup-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\.zip"',
                        response.headers["content-disposition"])
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ["config.json", "data/state.json"]
        assert archive.testzip() is None
        config_content = archive.read("config.json")
        state_content = archive.read("data/state.json")
        json.loads(config_content)
        state = json.loads(state_content)
        assert api.task_id in state["tasks"]
        assert not any(marker.encode() in config_content + state_content for marker in excluded.values())

    browser = TestClient(api.client.app, base_url="http://localhost")
    assert browser.post("/login", json={"token": TOKEN}, headers=TRUSTED).status_code == 204
    assert browser.post("/api/backup", json={}, headers=TRUSTED).status_code == 200

    unauthenticated = TestClient(api.client.app, base_url="http://localhost")
    assert unauthenticated.post("/api/backup", json={}, headers=TRUSTED).status_code == 401
    assert api.client.post("/api/backup", json={}).status_code == 403
    assert api.client.post("/api/backup", json={}, headers={"Origin": "http://evil.example"}).status_code == 403

    agent = api.pool._agent_tokens.issue(next(iter(api.pool._projects)), "agent", runner=type("R", (), {"state": "idle"})())
    refused = api.client.post("/api/backup", json={}, headers={**TRUSTED, "Authorization": f"Bearer {agent}"})
    assert refused.status_code == 403
    assert api.client.post("/api/backup", json={}, headers={**TRUSTED, "Authorization": "Bearer wrong"}).status_code == 401


def test_api_requires_the_secret(api):
    for headers in ({"Authorization": ""}, {"Authorization": "Bearer wrong"}):
        response = api.client.get("/api/state", headers=headers)
        assert response.status_code == 401
        assert "hihive open" in response.json()["error"]
    # Faking Host and Origin, as a Docker container can, is not enough.
    response = api.client.post("/api/control/pause", json={}, headers={**TRUSTED, "Authorization": ""})
    assert response.status_code == 401
    assert api.pool.state()["paused"] is False


def test_login_ticket_requires_full_cookie_and_same_origin(api):
    app = api.client.app
    anonymous = TestClient(app, base_url='http://localhost')
    bearer = TestClient(app, base_url='http://localhost', headers={'Authorization': f'Bearer {TOKEN}'})
    browser = TestClient(app, base_url='http://localhost', cookies={'hive_token': TOKEN})
    assert anonymous.post('/api/login-ticket', json={}, headers=TRUSTED).status_code == 401
    assert bearer.post('/api/login-ticket', json={}, headers=TRUSTED).status_code == 403
    assert browser.post('/api/login-ticket', json={}).status_code == 403
    assert browser.post('/api/login-ticket', json={}, headers={'Origin': 'http://127.0.0.1'}).status_code == 403
    assert browser.post('/api/login-ticket', json={}, headers={'Origin': 'https://localhost:80'}).status_code == 403
    assert browser.post('/api/login-ticket', json={}, headers={'Origin': 'http://evil.example'}).status_code == 403
    assert browser.post('/api/login-ticket', json={}, headers={'Origin': 'http://localhost', 'Host': 'evil.example'}).status_code == 403
    assert browser.post('/api/login-ticket', content='{}', headers=TRUSTED).status_code == 415
    response = browser.post('/api/login-ticket', json={}, headers=TRUSTED)
    assert response.status_code == 200
    assert TOKEN not in response.text
    assert re.fullmatch(r'[A-Za-z0-9_-]{43}', response.json()['ticket'])
    assert 'no-store' in response.headers['cache-control']
    assert response.headers['referrer-policy'] == 'no-referrer'


def test_login_ticket_redeems_once_replaces_previous_and_dies_on_restart(api):
    app = api.client.app
    browser = TestClient(app, base_url='http://localhost', cookies={'hive_token': TOKEN})
    other = TestClient(app, base_url='http://localhost')
    first = browser.post('/api/login-ticket', json={}, headers=TRUSTED).json()['ticket']
    second = browser.post('/api/login-ticket', json={}, headers=TRUSTED).json()['ticket']
    invalid = other.post('/login', json={'ticket': first}, headers=TRUSTED)
    assert invalid.status_code == 403
    assert invalid.json()['code'] == 'invalid_login_ticket'
    assert invalid.headers['referrer-policy'] == 'no-referrer'
    login = other.post('/login', json={'ticket': second}, headers=TRUSTED)
    assert login.status_code == 204
    assert 'httponly' in login.headers['set-cookie'].lower()
    assert 'samesite=strict' in login.headers['set-cookie'].lower()
    assert 'no-store' in login.headers['cache-control']
    assert other.post('/login', json={'ticket': second}, headers=TRUSTED).status_code == 403
    pending = browser.post('/api/login-ticket', json={}, headers=TRUSTED).json()['ticket']
    restarted = TestClient(create_app(api.pool, TOKEN, manage_pool=False), base_url='http://localhost')
    assert restarted.post('/login', json={'ticket': pending}, headers=TRUSTED).status_code == 403
    assert other.post('/login', json={'ticket': 'invalid'}, headers=TRUSTED).status_code == 403
    assert other.post('/login', json={'ticket': pending}).status_code == 403


def test_login_ticket_concurrent_http_redemption_has_one_winner(api, capsys):
    app = api.client.app
    issuer = TestClient(app, base_url='http://localhost', cookies={'hive_token': TOKEN})
    ticket = issuer.post('/api/login-ticket', json={}, headers=TRUSTED).json()['ticket']

    def redeem(_):
        client = TestClient(app, base_url='http://localhost')
        response = client.post('/login', json={'ticket': ticket}, headers=TRUSTED)
        return response.status_code, response.headers.get('set-cookie', ''), response.text

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(redeem, range(16)))
    assert sum(status == 204 for status, _, _ in outcomes) == 1
    assert sum(bool(cookie) for _, cookie, _ in outcomes) == 1
    assert all(ticket not in text and TOKEN not in text for _, _, text in outcomes)
    output = capsys.readouterr()
    assert ticket not in output.out + output.err
    assert TOKEN not in output.out + output.err


def test_login_ticket_expiry_boundary_and_concurrent_redemption():
    now = [100.0]
    tickets = LoginTickets(clock=lambda: now[0])
    first = tickets.issue()
    assert first not in repr(tickets.__dict__)
    now[0] = 399.999
    assert tickets.consume(first)
    ticket = tickets.issue()
    now[0] += 300
    assert not tickets.consume(ticket)
    newest = tickets.issue()
    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(tickets.consume, [newest] * 16))
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 15
    with ThreadPoolExecutor(max_workers=8) as executor:
        issued = list(executor.map(lambda _: tickets.issue(), range(16)))
    assert len(set(issued)) == 16
    assert sum(tickets.consume(value) for value in issued) == 1


def test_login_ticket_sets_secure_cookie_over_https(api):
    browser = TestClient(api.client.app, base_url='https://localhost', cookies={'hive_token': TOKEN})
    headers = {'Origin': 'https://localhost'}
    ticket = browser.post('/api/login-ticket', json={}, headers=headers).json()['ticket']
    other = TestClient(api.client.app, base_url='https://localhost')
    response = other.post('/login', json={'ticket': ticket}, headers=headers)
    assert response.status_code == 204
    assert 'secure' in response.headers['set-cookie'].lower()


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
