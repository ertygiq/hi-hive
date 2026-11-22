"""Projects, harness availability, and agent tokens limited to one project (docs/adr/0003)."""

import json
import os
import sys

import pytest
from starlette.testclient import TestClient

from hihive import HiveError
from hihive.cli import Exit, HiHiveCLI, USAGE
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


def test_config_rejects_leaving_any_project_without_a_harness(setup):
    before = setup.pool.config()
    with pytest.raises(HiveError, match="none available in 'Sandbox'"):
        setup.pool.update_config({"harnesses": [harness("host", only=[setup.main])], "max_parallel": 9})
    assert setup.pool.config()["max_parallel"] == before["max_parallel"]
    assert [h["id"] for h in setup.pool.config()["harnesses"]] == ["host", "docker"]


def test_every_agent_start_rechecks_availability(setup):
    """A task queued before its harness was limited elsewhere must not start."""
    task = setup.pool.add_task("queued", project_id=setup.main)
    # Bypass config validation to simulate availability changing outside a config save.
    setup.pool._harnesses = [harness("host", only=[setup.sandbox]),
                             harness("docker", only=[setup.sandbox])]
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
    assert setup.pool.delete_project(setup.sandbox) == "active_tasks"
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
        assert s.pool.token_context(token) == {"project_id": s.sandbox, "task_id": task}
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


def test_agent_token_cannot_issue_browser_login_ticket(api):
    response = api.call('POST', '/api/login-ticket', api.agent, json={})
    assert response.status_code == 403
    assert 'ticket' not in response.json()
    api.client.cookies.set('hive_token', TOKEN)
    response = api.call('POST', '/api/login-ticket', api.agent, json={})
    assert response.status_code == 403
    assert 'ticket' not in response.json()


def test_an_agent_token_sees_and_starts_tasks_only_in_its_project(api, setup):
    own = setup.pool.add_task("own", project_id=setup.sandbox)
    other = setup.pool.add_task("other", project_id=setup.main)
    state = api.call("GET", "/api/state", api.agent).json()
    assert [t["id"] for t in state["queue"]] == [own]
    assert api.call("GET", f"/api/tasks/{other}", api.agent).status_code == 404
    assert api.call("POST", f"/api/tasks/{other}/cancel", api.agent, json={}).status_code == 404
    relabelled = api.call("POST", f"/api/tasks/{own}/label", api.agent, json={"label": "Agent label"})
    assert relabelled.status_code == 200
    assert setup.pool.task_detail(own)["label"] == "Agent label"
    assert api.call("GET", f"/api/state?project={setup.main}", api.agent).status_code == 403
    created = api.call("POST", "/api/tasks", api.agent, json={"prompt": "child"})
    assert setup.pool._tasks[created.json()["id"]]["project_id"] == setup.sandbox
    assert setup.pool._tasks[created.json()["id"]]["created_by_task_id"] == "agent-task"
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
    ("POST", "/label", {"label": "x"}), ("POST", "/chat", {"message": "x"}),
    ("POST", "/chat/cancel", {"msg_id": None}), ("POST", "/stop-work", {"kind": "background", "token": "x"})])
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


def test_agent_created_task_provenance_comes_only_from_the_token(api, setup, monkeypatch):
    creator = setup.pool.add_task("creator", project_id=setup.sandbox)
    runner = type("R", (), {"state": "idle"})()
    agent = setup.pool._agent_tokens.issue(setup.sandbox, creator, runner)
    monkeypatch.setenv("HIVE_TASK_ID", "spoofed-environment-task")

    created = api.call("POST", "/api/tasks", agent,
                       json={"prompt": "child", "created_by_task_id": "spoofed-body-task"}).json()["id"]
    assert setup.pool.task_detail(created)["created_by_task_id"] == creator

    human = api.call("POST", "/api/tasks", TOKEN,
                     json={"prompt": "human", "project_id": setup.sandbox,
                           "created_by_task_id": "spoofed-body-task"}).json()["id"]
    assert setup.pool.task_detail(human)["created_by_task_id"] is None

    setup.pool.update_config({"templates": [
        {"id": "batch", "project_id": setup.sandbox, "name": "Batch", "template": "Do %{input}"}]})
    batch = api.call("POST", "/api/tasks/batch", agent,
                     json={"template_id": "batch", "inputs": ["one", "two"],
                           "created_by_task_id": "spoofed-body-task"}).json()["ids"]
    assert [setup.pool.task_detail(task_id)["created_by_task_id"] for task_id in batch] == [creator, creator]

    target = setup.pool.add_task("fork target", project_id=setup.sandbox)
    setup.pool._tasks[target]["status"] = "completed"
    setup.pool._tasks[target]["session_id"] = "session"
    forked = api.call("POST", f"/api/tasks/{target}/fork", agent,
                      json={"prompt": "forked", "created_by_task_id": "spoofed-body-task"}).json()["id"]
    detail = setup.pool.task_detail(forked)
    assert detail["parent_task_id"] == target
    assert detail["created_by_task_id"] == creator

    child_agent = setup.pool._agent_tokens.issue(setup.sandbox, created, runner)
    grandchild = api.call("POST", "/api/tasks", child_agent, json={"prompt": "grandchild"}).json()["id"]
    assert setup.pool.task_detail(grandchild)["created_by_task_id"] == created

    assert setup.pool.remove_task(creator) is True
    assert setup.pool.task_detail(created) is not None
    assert setup.pool.task_detail(forked) is not None


def test_an_agent_token_cannot_run_another_projects_template(api, setup):
    setup.pool.update_config({"templates": [
        {"id": "t-main", "project_id": setup.main, "name": "main", "template": "Main %{input}"}]})
    response = api.call("POST", "/api/tasks/batch", api.agent, json={"template_id": "t-main", "inputs": ["x"]})
    assert response.status_code == 404
    assert setup.pool._tasks == {}


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/config"), ("PUT", "/api/config"), ("POST", "/api/output_actions/x/run"),
    ("POST", "/api/templates"), ("POST", "/api/projects"), ("POST", "/api/backup"),
    ("POST", "/api/control/pause"),
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

def test_cli_help_explains_project_ownership_and_global_harnesses():
    assert "Tasks, templates, review sections, and working directories belong to projects." in USAGE
    assert "Harnesses are configured globally and can be made available to selected projects." in USAGE
    assert 'label <id> "new label"' in USAGE


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


class TaskCLI(ProjectsCLI):
    """Main has task aaaa-1 and bbbb-main; Sandbox has bbbb-sandbox."""

    def get(self, path):
        if path == "/api/state":
            return {"queue": [], "workers": [], "results": [
                {"id": "aaaa-1", "project_id": "aaaa1111"},
                {"id": "bbbb-main", "project_id": "aaaa1111"},
                {"id": "bbbb-sandbox", "project_id": "bbbb2222"}]}
        return super().get(path)


def test_task_ids_work_in_any_project_when_none_is_named(monkeypatch):
    monkeypatch.delenv("HIVE_PROJECT", raising=False)
    assert TaskCLI(PROJECTS).resolve_task_id("aaaa") == "aaaa-1"
    with pytest.raises(Exit):
        TaskCLI(PROJECTS).resolve_task_id("bbbb")


@pytest.mark.parametrize("named_by", ["option", "variable"])
def test_a_named_project_rejects_task_ids_from_other_projects(monkeypatch, capsys, named_by):
    monkeypatch.delenv("HIVE_PROJECT", raising=False)

    def cli():
        c = TaskCLI(PROJECTS)
        if named_by == "option":
            c.project_arg = "Sandbox"
        else:
            monkeypatch.setenv("HIVE_PROJECT", "Sandbox")
        return c

    with pytest.raises(Exit):
        cli().resolve_task_id("aaaa")
    assert "Task 'aaaa' is in project 'Main', not 'Sandbox'" in capsys.readouterr().err
    # The named project also settles an ID prefix shared with another project.
    assert cli().resolve_task_id("bbbb") == "bbbb-sandbox"


def test_cancel_with_a_named_project_does_not_touch_another_projects_task(monkeypatch):
    monkeypatch.delenv("HIVE_PROJECT", raising=False)
    c = TaskCLI(PROJECTS)
    with pytest.raises(Exit):
        c.run(["--project", "Sandbox", "cancel", "aaaa"])
    assert c.posts == []


def test_label_resolves_a_short_id_inside_the_named_project(monkeypatch, capsys):
    monkeypatch.delenv("HIVE_PROJECT", raising=False)

    class LabelTaskCLI(TaskCLI):
        def post(self, path, body=None):
            self.posts.append((path, body))
            return {"ok": True, "label": "Renamed"}

    cli = LabelTaskCLI(PROJECTS)
    cli.run(["--project", "Sandbox", "label", "bbbb", "Renamed"])
    assert cli.posts == [("/api/tasks/bbbb-sandbox/label", {"label": "Renamed"})]
    assert "Changed label for bbbb-san: Renamed" in capsys.readouterr().out


TWINS = [{"id": "abcd1111", "name": "Frontend", "templates": [], "harnesses": []},
         {"id": "abcd2222", "name": "abcd", "templates": [], "harnesses": []},
         {"id": "abce3333", "name": "Backend", "templates": [], "harnesses": []}]


def test_an_ambiguous_project_prefix_says_so_and_an_exact_name_wins(monkeypatch, capsys):
    monkeypatch.delenv("HIVE_PROJECT", raising=False)
    cli = ProjectsCLI(TWINS)
    cli.project_arg = "abcd1"
    assert cli.project()["name"] == "Frontend"
    cli = ProjectsCLI(TWINS)
    cli.project_arg = "abcd"  # a project name, and also a prefix of two IDs
    assert cli.project()["id"] == "abcd2222"
    cli = ProjectsCLI(TWINS)
    cli.project_arg = "abc"
    with pytest.raises(Exit):
        cli.project()
    assert "No project matching 'abc'" in capsys.readouterr().err
    cli = ProjectsCLI([TWINS[0], TWINS[2], {"id": "abcd4444", "name": "Other", "templates": [], "harnesses": []}])
    cli.project_arg = "abcd"
    with pytest.raises(Exit):
        cli.project()
    error = capsys.readouterr().err
    assert "Ambiguous project 'abcd'" in error and "Frontend" in error and "Other" in error


def test_projects_marks_the_project_commands_would_use(monkeypatch, capsys):
    monkeypatch.delenv("HIVE_PROJECT", raising=False)
    ProjectsCLI(PROJECTS[:1]).cmd_projects()
    assert capsys.readouterr().out.startswith("* Main")
    ProjectsCLI(PROJECTS).cmd_projects()
    assert "*" not in capsys.readouterr().out
    monkeypatch.setenv("HIVE_PROJECT", "Sandbox")
    ProjectsCLI(PROJECTS).cmd_projects()
    assert [line[0] for line in capsys.readouterr().out.splitlines()] == [" ", "*"]


class ListingCLI(ProjectsCLI):
    def get(self, path):
        if path.startswith("/api/state?project="):
            return {"paused": False, "queue": [], "workers": [], "results": [],
                    "stats": {k: 0 for k in ("queued", "running", "completed", "failed", "cancelled")}}
        return super().get(path)


@pytest.mark.parametrize("command", [["status"], ["queue"], ["results", "--no-pager"]])
def test_listings_name_their_project_and_start_prints_only_the_id(monkeypatch, capsys, command):
    monkeypatch.setenv("HIVE_PROJECT", "Sandbox")
    ListingCLI(PROJECTS).run(command)
    assert capsys.readouterr().out.splitlines()[0].split() == ["Project:", "Sandbox"]
    ListingCLI(PROJECTS).run(["start", "-p", "hello"])
    assert capsys.readouterr().out == "new\n"
