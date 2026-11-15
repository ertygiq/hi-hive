"""Projects, where harnesses and output actions are available, and tokens that limit agents to one project.

See docs/adr/0003-projects-and-scoped-tokens.md.
"""

import hashlib
import secrets
import uuid

from . import HiveError

FIRST_PROJECT_NAME = "My project"


def normalize_only(value):
    """Where a harness or output action is available: None for all projects, else a list of project IDs."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise HiveError("'only' must be a list of project IDs or null")
    return list(dict.fromkeys(str(item) for item in value))


def available_in(item, project):
    """A harness or output action is available in a project when it lists that project,
    or when it is shared (no list) and the project uses shared ones."""
    only = item.get("only")
    if only is None:
        return bool(project.get("use_shared_harnesses", True))
    return project["id"] in only


def output_action_available_in(action, project):
    """Output actions have no per-project switch: shared ones appear everywhere."""
    only = action.get("only")
    return only is None or project["id"] in only


def project_record(id=None, name=None, color=None, work_dir=None, use_shared_harnesses=True, default_harness=None):
    name = " ".join(str(name or "").split())
    if not name:
        raise HiveError("Project name cannot be empty")
    work_dir = str(work_dir or "").strip()
    if not work_dir:
        raise HiveError(f"Project '{name}' needs a working directory")
    return {
        "id": str(id or uuid.uuid4()),
        "name": name[:80],
        "color": color,
        "work_dir": work_dir,
        "use_shared_harnesses": use_shared_harnesses is not False,
        "default_harness": str(default_harness) if default_harness else None,
    }


def validate_project_names(projects):
    seen = set()
    for project in projects:
        key = project["name"].lower()
        if key in seen:
            raise HiveError(f"Project name '{project['name']}' is already taken")
        seen.add(key)


class AgentTokens:
    """Tokens Hive gives to agent processes it starts, each limited to the task's project.

    Held in memory only and keyed by a hash, so a lookup's timing reveals nothing about valid tokens.
    The caller holds the pool lock.
    """

    def __init__(self):
        self._by_hash = {}

    def issue(self, project_id, task_id, runner):
        token = secrets.token_urlsafe(32)
        self._by_hash[_digest(token)] = {"project_id": project_id, "task_id": task_id, "runner": runner}
        return token

    def lookup(self, token):
        """The project ID a token is limited to, or None for an unknown token."""
        entry = self._by_hash.get(_digest(token)) if token else None
        return entry["project_id"] if entry else None

    def revoke_runner(self, runner):
        for key in [k for k, v in self._by_hash.items() if v["runner"] is runner]:
            del self._by_hash[key]

    def revoke_exited(self):
        for key in [k for k, v in self._by_hash.items() if v["runner"].state == "exited"]:
            del self._by_hash[key]

    def revoke_all(self):
        self._by_hash.clear()


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()
