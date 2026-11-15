"""Projects, harness availability, and agent tokens limited to one project (docs/adr/0003)."""

import json
import os
import sys

import pytest
from starlette.testclient import TestClient

from hihive import HiveError
from hihive.cli import Exit, HiHiveCLI
from hihive.pool import PoolManager
from hihive.server import create_app
from conftest import FIXTURE, wait_until

TOKEN = "full-secret"
TRUSTED = {"Origin": "http://localhost"}


def write_config(directory, harnesses):
    path = os.path.join(directory, "config.json")
    with open(path, "w") as file:
        json.dump({"max_parallel": 2, "work_dir": directory, "default_harness": harnesses[0]["id"],
                   "harnesses": harnesses}, file)
    return path


def harness(id, only=None, root=None):
    args = [FIXTURE, "--fake-kind", "pi", "--fake-root", root] if root else []
    return {"id": id, "name": id, "type": "pi", "command": sys.executable if root else id, "args": args,
            "only": only}


class Setup:
    """Two projects: "Main" uses the shared harnesses; "Sandbox" uses only its own docker harness."""

    def __init__(self, directory, root=None):
        self.dir = directory
        self.pool = PoolManager(config_file=write_config(directory, [harness("host", root=root)]),
                                persist_file=os.path.join(directory, "state.json"))
        self.main = next(iter(self.pool._projects))
        self.sandbox = self.pool.create_project("Sandbox", directory)
        harnesses = [harness("host", root=root), harness("docker", only=[self.sandbox], root=root)]
        projects = [{**self.pool._projects[self.sandbox], "use_shared_harnesses": False}]
        self.pool.update_config({"harnesses": harnesses, "projects": projects})


@pytest.fixture
def setup(test_dir):
    return Setup(test_dir)


def test_first_start_creates_one_ordinary_project_from_the_old_settings(test_dir):
    pool = PoolManager(config_file=write_config(test_dir, [harness("pi")]),
                       persist_file=os.path.join(test_dir, "state.json"))
    [project] = pool.config()["projects"]
    assert (project["name"], project["work_dir"], project["default_harness"]) == ("My project", test_dir, "pi")
    assert pool.add_task("no project named needed with one project")


def test_a_harness_is_available_where_it_is_listed_or_shared_with_projects_that_use_shared(setup):
    projects = {p["id"]: p for p in setup.pool.config()["projects"]}
    assert projects[setup.main]["available_harnesses"] == ["host"]
    assert projects[setup.sandbox]["available_harnesses"] == ["docker"]
    assert projects[setup.sandbox]["default_harness"] == "docker"
    # A shared harness added later stays out of a project that does not use shared harnesses.
    setup.pool.update_config({"harnesses": [*setup.pool._harnesses, harness("later")]})
    projects = {p["id"]: p for p in setup.pool.config()["projects"]}
    assert projects[setup.main]["available_harnesses"] == ["host", "later"]
    assert projects[setup.sandbox]["available_harnesses"] == ["docker"]


def test_tasks_use_only_harnesses_available_in_their_project(setup):
    with pytest.raises(HiveError, match="Name a project"):
        setup.pool.add_task("which project?")
    task = setup.pool.add_task("default harness", project_id=setup.sandbox)
    assert setup.pool._tasks[task]["harness"] == "docker"
    with pytest.raises(HiveError, match="not available in project 'Sandbox'"):
        setup.pool.add_task("escape", harness="host", project_id=setup.sandbox)
    with pytest.raises(HiveError, match="not available in project 'My project'"):
        setup.pool.add_task("wrong way", harness="docker", project_id=setup.main)
    setup.pool._tasks[task]["status"] = "failed"
    with pytest.raises(HiveError, match="not available"):
        setup.pool.retry_task(task, harness="host")


def test_every_agent_start_rechecks_availability(setup):
    """A task queued before its harness was limited elsewhere must not start."""
    task = setup.pool.add_task("queued", project_id=setup.main)
    setup.pool.update_config({"harnesses": [harness("host", only=[setup.sandbox]),
                                            harness("docker", only=[setup.sandbox])]})
    setup.pool._spawn_task(setup.pool._tasks[task])
    assert setup.pool._tasks[task]["status"] == "failed"
    assert "not available" in setup.pool._tasks[task]["failure_details"]


def test_templates_and_review_sections_belong_to_one_project(setup):
    setup.pool.update_config({
        "review_sections": [{"id": "r1", "project_id": setup.main, "name": "Check", "prompt": "Check"}],
        "templates": [{"id": "t1", "project_id": setup.sandbox, "name": "scan", "template": "Scan %{input}",
                       "review_section_ids": ["r1"]},
                      {"id": "t2", "project_id": setup.main, "name": "scan", "template": "Same name, other project"}],
    })
    assert setup.pool._templates["t1"]["review_section_ids"] == []
    [task] = setup.pool.add_tasks_batch(["x"], "t1")
    assert setup.pool._tasks[task]["project_id"] == setup.sandbox


def test_deleting_a_project_removes_its_work_and_its_place_in_harness_lists(setup):
    task = setup.pool.add_task("gone", project_id=setup.sandbox)
    setup.pool._tasks[task]["status"] = "completed"
    keep = setup.pool.add_task("kept", project_id=setup.main)
    assert setup.pool.delete_project(setup.sandbox) is True
    assert set(setup.pool._tasks) == {keep}
    assert setup.pool._harnesses[1]["only"] == []
    assert setup.pool.delete_project(setup.main) == "last_project"


def test_queue_reorder_moves_only_the_projects_queued_tasks(setup):
    a1 = setup.pool.add_task("a1", project_id=setup.main)
    b1 = setup.pool.add_task("b1", project_id=setup.sandbox)
    a2 = setup.pool.add_task("a2", project_id=setup.main)
    assert setup.pool.reorder_queue([a2, a1], setup.main)
    assert setup.pool._task_order == [a2, b1, a1]
    assert not setup.pool.reorder_queue([a1], setup.main)


def test_projects_survive_a_restart(setup):
    setup.pool._save_state()
    restarted = PoolManager(config_file=os.path.join(setup.dir, "config.json"),
                            persist_file=os.path.join(setup.dir, "state.json"))
    assert [p["name"] for p in restarted.config()["projects"]] == ["My project", "Sandbox"]
    assert restarted._projects[setup.sandbox]["use_shared_harnesses"] is False


# --- Agent tokens ---

def test_each_agent_gets_its_own_token_that_ends_when_the_process_exits(test_dir):
    s = Setup(test_dir, root=test_dir)
    s.pool.start()
    try:
        task = s.pool.add_task("BASE", project_id=s.sandbox)
        token_file = os.path.join(test_dir, "hive_token")
        token = wait_until(lambda: os.path.exists(os.path.join(test_dir, "started"))
                           and open(token_file).read(), 10)
        assert token != TOKEN
        assert s.pool.token_project(token) == s.sandbox
        s.pool.cancel_task(task)
        runner = s.pool._tasks[task]["runner"]
        runner.join()
        wait_until(lambda: s.pool.token_project(token) is None, 5)
    finally:
        s.pool.stop()


class Api:
    def __init__(self, setup):
        self.setup = setup
        self.client = TestClient(create_app(setup.pool, TOKEN, manage_pool=False), base_url="http://localhost")
        # An agent token for the sandbox project, as Hive would give a sandbox agent.
        self.agent = setup.pool._agent_tokens.issue(setup.sandbox, "agent-task", runner=type("R", (), {"state": "idle"})())

    def call(self, method, path, token, **kwargs):
        return self.client.request(method, path, headers={"Authorization": f"Bearer {token}", **TRUSTED}, **kwargs)


@pytest.fixture
def api(setup):
    return Api(setup)


def test_an_agent_token_sees_and_starts_tasks_only_in_its_project(api, setup):
    own = setup.pool.add_task("own", project_id=setup.sandbox)
    other = setup.pool.add_task("other", project_id=setup.main)
    state = api.call("GET", "/api/state", api.agent).json()
    assert [t["id"] for t in state["queue"]] == [own]
    assert api.call("GET", f"/api/tasks/{other}", api.agent).status_code == 404
    assert api.call("POST", f"/api/tasks/{other}/cancel", api.agent, json={}).status_code == 404
    assert api.call("GET", f"/api/state?project={setup.main}", api.agent).status_code == 403
    created = api.call("POST", "/api/tasks", api.agent, json={"prompt": "child"})
    assert setup.pool._tasks[created.json()["id"]]["project_id"] == setup.sandbox
    assert api.call("POST", "/api/tasks", api.agent, json={"prompt": "x", "project_id": setup.main}).status_code == 403
    escape = api.call("POST", "/api/tasks", api.agent, json={"prompt": "x", "harness": "host"})
    assert escape.status_code >= 400 and "not available" in escape.json()["error"]
    [project] = api.call("GET", "/api/projects", api.agent).json()["projects"]
    assert project["id"] == setup.sandbox
    assert [h["id"] for h in project["harnesses"]] == ["docker"]
    assert "command" not in project["harnesses"][0]


@pytest.mark.parametrize("method,path,body", [
    ("GET", "", None), ("DELETE", "", None), ("POST", "/fork", {"prompt": "x"}), ("POST", "/cancel", {}),
    ("POST", "/continue", {}), ("POST", "/retry", {}), ("POST", "/done", {}),
    ("POST", "/chat", {"message": "x"}), ("POST", "/chat/cancel", {"msg_id": None})])
def test_an_agent_token_cannot_use_any_task_route_on_another_projects_task(api, setup, method, path, body):
    """Each task route (behind show, wait, followup, cancel, retry, fork) must check the task's project."""
    other = setup.pool.add_task("other", project_id=setup.main)
    setup.pool._tasks[other]["status"] = "failed"
    setup.pool._tasks[other]["session_id"] = "session"
    before = json.dumps(setup.pool.task_detail(other), sort_keys=True, default=str)
    tasks_before = set(setup.pool._tasks)
    response = api.call(method, f"/api/tasks/{other}{path}", api.agent, **({"json": body} if body is not None else {}))
    assert response.status_code == 404
    assert json.dumps(setup.pool.task_detail(other), sort_keys=True, default=str) == before
    assert set(setup.pool._tasks) == tasks_before
    # The same request with the full token reaches the task, so the 404 above comes from the project check.
    assert api.call(method, f"/api/tasks/{other}{path}", TOKEN,
                    **({"json": body} if body is not None else {})).status_code != 404 or method == "DELETE"


def test_an_agent_token_cannot_run_another_projects_template(api, setup):
    setup.pool.update_config({"templates": [
        {"id": "t-main", "project_id": setup.main, "name": "main", "template": "Main %{input}"}]})
    response = api.call("POST", "/api/tasks/batch", api.agent, json={"template_id": "t-main", "inputs": ["x"]})
    assert response.status_code == 404
    assert setup.pool._tasks == {}


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/config"), ("PUT", "/api/config"), ("POST", "/api/output_actions/x/run"),
    ("POST", "/api/templates"), ("POST", "/api/projects"), ("POST", "/api/control/pause"),
    ("POST", "/api/control/restart"), ("POST", "/api/queue/reorder")])
def test_an_agent_token_cannot_change_configuration_or_run_host_commands(api, method, path):
    response = api.call(method, path, api.agent, json={})
    assert response.status_code == 403
    assert "own project" in response.json()["error"]


def test_an_agent_token_cannot_log_in_a_browser_and_unknown_tokens_are_refused(api):
    assert api.client.post("/login", json={"token": api.agent}, headers=TRUSTED).status_code == 403
    assert api.call("GET", "/api/state", "made-up").status_code == 401
    cookie = TestClient(api.client.app, base_url="http://localhost", cookies={"hive_token": api.agent})
    assert cookie.get("/api/state").status_code == 401


def test_the_full_token_sees_all_projects_or_one(api, setup):
    setup.pool.add_task("a", project_id=setup.main)
    setup.pool.add_task("b", project_id=setup.sandbox)
    assert len(api.call("GET", "/api/state", TOKEN).json()["queue"]) == 2
    assert len(api.call("GET", f"/api/state?project={setup.main}", TOKEN).json()["queue"]) == 1
    assert api.call("GET", "/api/state?project=missing", TOKEN).status_code == 404


# --- CLI ---

class ProjectsCLI(HiHiveCLI):
    def __init__(self, projects):
        super().__init__()
        self.projects_payload = projects
        self.posts = []

    def get(self, path):
        assert path == "/api/projects"
        return {"projects": self.projects_payload}

    def post(self, path, body=None):
        self.posts.append((path, body))
        return {"id": "new"}


PROJECTS = [{"id": "aaaa1111", "name": "Main", "default_harness": "h", "templates": [],
             "harnesses": [{"id": "h", "name": "Host"}]},
            {"id": "bbbb2222", "name": "Sandbox", "default_harness": "d", "templates": [],
             "harnesses": [{"id": "d", "name": "Docker"}]}]


def test_cli_with_hive_token_never_reads_the_token_file(monkeypatch):
    monkeypatch.setenv("HIVE_TOKEN", "agent")
    monkeypatch.setattr("hihive.paths.read_token", lambda: pytest.fail("read the full token"))
    assert HiHiveCLI().token == "agent"
    monkeypatch.setenv("HIVE_TOKEN", "")
    with pytest.raises(Exit):
        HiHiveCLI().run(["status"])


def test_cli_project_comes_from_option_then_variable_and_must_be_named_when_several(monkeypatch, capsys):
    monkeypatch.delenv("HIVE_PROJECT", raising=False)
    cli = ProjectsCLI(PROJECTS)
    with pytest.raises(Exit):
        cli.project()
    assert "Name a project" in capsys.readouterr().err
    monkeypatch.setenv("HIVE_PROJECT", "sandbox")
    assert ProjectsCLI(PROJECTS).project()["id"] == "bbbb2222"
    cli = ProjectsCLI(PROJECTS)
    cli.project_arg = "aaaa"
    assert cli.project()["name"] == "Main"
    monkeypatch.delenv("HIVE_PROJECT")
    assert ProjectsCLI(PROJECTS[:1]).project()["name"] == "Main"


def test_cli_start_sends_the_project_and_finds_harnesses_only_in_it(monkeypatch, capsys):
    monkeypatch.setenv("HIVE_PROJECT", "Sandbox")
    cli = ProjectsCLI(PROJECTS)
    cli.cmd_start(["-p", "hello", "--harness", "docker"])
    assert cli.posts == [("/api/tasks", {"prompt": "hello", "label": None, "project_id": "bbbb2222",
                                         "harness": "d"})]
    with pytest.raises(Exit):
        ProjectsCLI(PROJECTS).cmd_start(["-p", "hello", "--harness", "host"])
    assert "No harness named 'host' in this project" in capsys.readouterr().err
