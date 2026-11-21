"""Task pool: queue, scheduling, persistence, and the runners' event handling."""

import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from datetime import UTC, datetime

from . import HiveError
from . import messages as tm
from . import output_actions
from . import projects as pj
from .diagnostics import redact
from .harness import adapter_for
from .protocol import IDLE_BACKGROUND, protocol_for
from .runner import LiveRunner, valid_message

SAVE_INTERVAL = 2  # seconds between auto-saves (debounced)
SLEEP_PREVENTION_GRACE_SECS = 5 * 60
COST_REFRESH_INTERVAL = 5  # seconds between transcript re-reads for a running task
LEGACY_TASK_HARNESS_ID = "claude-code"
ACTIVE = ("running", "reviewing")
FINISHED = ("completed", "failed", "cancelled")
TEMPLATE_FIELDS = ("id", "project_id", "name", "template", "review_section_ids", "color", "work_dir", "agent_args",
                   "harness")
PROJECT_FIELDS = ("id", "name", "color", "work_dir", "use_shared_harnesses", "default_harness")
SCHEMA_VERSION = 3
MESSAGE_ID = re.compile(r"[0-9a-f-]{36}", re.IGNORECASE)
WHITESPACE = re.compile(r"[ \t\r\n\f\v]+")
ANSI = re.compile(r"\x1b(?:\[[0-9;?<>=]*[a-zA-Z]|\][^\x07]*\x07)")
DECORATION = re.compile(r"[-*_`#>\s]+")
FAILURE_WORDS = re.compile(r"\b(error|failed|exception|spawn error|not found|denied)\b", re.IGNORECASE)
TEMPLATE_INPUT = re.compile(r"%(%|\{input\})")

# Code-defined harness types. A harness config row carries one of these ids in its type field.
HARNESS_TYPES = [{"id": "claude-code", "label": "Claude Code"}, {"id": "pi", "label": "Pi"}]

# Variables a parent agent or Hive's own launcher sets that must not reach the
# agents Hive starts (see KNOWN_PITFALLS.md). Hive sets HIVE_TOKEN and HIVE_TASK_ID
# itself for each agent; inherited values could identify another task or project.
INHERITED_AGENT_VARS = ("PI_SESSION_ID", "PI_SESSION_FILE", "PI_PROVIDER", "PI_MODEL", "PI_REASONING_LEVEL",
                        "HIVE_TOKEN", "HIVE_PROJECT", "HIVE_TASK_ID")
PYTHON_LAUNCH_VARS = ("PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__", "UV_RUN_RECURSION_DEPTH")


def now():
    return datetime.now().astimezone()


def iso(moment):
    """Seconds-precision timestamp with offset, or None."""
    return moment.isoformat(timespec="seconds") if moment else None


def iso_micro(moment=None):
    return (moment or now()).isoformat(timespec="microseconds")


def parse_time(value):
    return datetime.fromisoformat(value) if value else None


def utc_micro(value):
    return parse_time(value).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def fill_template(template, value):
    """Replace %{input} with the input and %% with %. Other text stays as written."""
    return TEMPLATE_INPUT.sub(lambda m: "%" if m.group(1) == "%" else value, template)


def normalize_args(args):
    if args is None:
        return []
    if isinstance(args, list):
        return [str(arg) for arg in args]
    return str(args).split()


def child_process_env():
    """Overrides for the environment of processes Hive starts; None removes a variable.

    Running Hive through uv or a virtualenv sets variables that would make
    Python tools launched by agents use Hive's environment instead of their own.
    Remove only what Hive's own startup adds; see docs/adr/0001-agent-environment.md.
    """
    overrides = {key: None for key in (*INHERITED_AGENT_VARS, *PYTHON_LAUNCH_VARS)}
    venv = os.environ.get("VIRTUAL_ENV")
    if venv and os.path.realpath(venv) == os.path.realpath(sys.prefix):
        overrides["VIRTUAL_ENV"] = None
        overrides["VIRTUAL_ENV_PROMPT"] = None
        venv_bin = os.path.join(venv, "bin")
        path = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry != venv_bin]
        overrides["PATH"] = os.pathsep.join(path)
    return overrides


def full_env(overrides):
    env = dict(os.environ)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


class PoolManager:
    def __init__(self, config_file, persist_file):
        self.config_file, self.persist_file = str(config_file), str(persist_file)
        self._tasks = {}
        self._task_order = []
        self._max_parallel = None
        # Read from config.json only to create the first project.
        self._work_dir = None
        self._session_viewer_url = None
        self._allowed_hosts = None
        self._prevent_sleep_when_running = False
        self._show_task_cost = False
        self._sleep_preventer = None
        self._sleep_preventer_grace_until = None
        self._sleep_preventer_disabled = False
        self._harnesses = []
        self._default_harness = None
        self._projects = {}
        self._review_sections = {}
        self._templates = {}
        self._output_actions = []
        self._agent_tokens = pj.AgentTokens()
        self._paused = False
        self._lock = threading.RLock()
        self._running = False
        self._monitor_thread = None
        self.version = 0
        # Versions restart after a reboot; the instance ID tells two runs apart.
        self._instance_id = str(uuid.uuid4())
        self._last_saved_version = 0
        self._last_save_time = time.monotonic()

        self._load_config()
        self._load_state()
        self._ensure_harnesses()
        self._output_actions = output_actions.normalize_all(self._output_actions)
        if not self._projects:
            self._create_first_project()

    def start(self):
        self._running = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, name="pool-monitor", daemon=True)
        self._monitor_thread.start()
        threading.Thread(target=self._backfill_task_costs, name="cost-backfill", daemon=True).start()

    def stop(self):
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(5)
        with self._lock:
            for task in self._tasks.values():
                if task["status"] not in ACTIVE:
                    continue
                task["cancel_requested"] = True
                task["status"] = "failed"
                task["failure_details"] = "Task was interrupted by server shutdown."
                task["completed_at"] = now()
                task.pop("pending_review", None)
            runners = [task["runner"] for task in self._tasks.values() if task.get("runner")]
        for runner in runners:
            runner.close("Server shutdown")
        for runner in runners:
            runner.join()
        with self._lock:
            self._agent_tokens.revoke_all()
            self._poll_live_runners()
            self._stop_sleep_preventer()
            self._save_state()

    # --- State ---

    def state(self, project_id=None):
        with self._lock:
            return self._state_unlocked(project_id)

    def state_since(self, since, project_id=None):
        """None when `since` ("instance:version") names the current state.

        With project_id, the snapshot holds only that project's tasks.
        """
        with self._lock:
            return None if since == f"{self._instance_id}:{self.version}" else self._state_unlocked(project_id)

    def task_detail(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            return self._serialize_task(task, full=True) if task else None

    def search_result_ids(self, query, project_id=None):
        """IDs of finished tasks whose conversation contains the query, case-insensitively.

        Prompt and label matching stays client-side; this covers only the text
        that the state payload does not carry.
        """
        needle = str(query or "").lower()
        with self._lock:
            ids = []
            if needle:
                for task_id in self._task_order:
                    task = self._tasks[task_id]
                    if project_id and task["project_id"] != project_id:
                        continue
                    if not (task.get("result_placement") or task["status"] in FINISHED):
                        continue
                    if any(m["role"] in ("user", "assistant") and needle in m["content"].lower()
                           for m in task["messages"]):
                        ids.append(task_id)
            return {"version": self.version, "ids": ids}

    # --- Task management ---

    def add_task(self, prompt, label=None, harness=None, project_id=None, created_by_task_id=None):
        """Without project_id, the task goes to the only project; with several projects one must be named."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise HiveError("Prompt required")
        with self._lock:
            project = self._project_for_new_task(project_id)
            task_id = str(uuid.uuid4())
            harness_id = self._resolve_new_task_harness_id(project, harness=harness)
            task = self._build_task(task_id, prompt=prompt, label=_clean_label(label) or _derive_label(prompt),
                                    created_at=now(), harness=harness_id, project_id=project["id"],
                                    created_by_task_id=created_by_task_id,
                                    session_id=self._initial_session_id(harness_id))
            self._tasks[task_id] = task
            self._task_order.append(task_id)
            self._bump_version()
            return task_id

    def set_task_label(self, task_id, label):
        cleaned = _clean_label(label)
        if not cleaned:
            raise HiveError("Label cannot be empty")
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            task["label"] = cleaned
            self._bump_version()
            return cleaned

    def fork_task(self, parent_id, prompt, label=None, created_by_task_id=None):
        """New task ID, None if the parent cannot fork, or "missing_session"."""
        with self._lock:
            parent = self._tasks.get(parent_id)
            if not parent or parent["status"] not in FINISHED:
                return None
            if not str(parent.get("session_id") or "").strip():
                return "missing_session"
            task_id = str(uuid.uuid4())
            harness_id = _task_harness_id(parent)
            self._require_available(self._find_harness(harness_id), self._projects[parent["project_id"]])
            task = self._build_task(task_id, prompt=prompt, label=_clean_label(label) or _derive_label(prompt),
                                    created_at=now(), color=parent.get("color"), template_id=parent.get("template_id"),
                                    parent_task_id=parent_id, created_by_task_id=created_by_task_id,
                                    harness=harness_id, project_id=parent["project_id"],
                                    session_id=self._initial_session_id(harness_id))
            self._tasks[task_id] = task
            self._task_order.append(task_id)
            self._bump_version()
            return task_id

    def add_tasks_batch(self, inputs, template_id, harness=None, created_by_task_id=None):
        with self._lock:
            template = self._templates.get(template_id)
            if not template:
                raise HiveError("Template not found")
            project = self._projects[template["project_id"]]
            harness_id = self._resolve_new_task_harness_id(project, harness=harness, template_id=template_id)
            ids = []
            for value in inputs or []:
                value = "" if value is None else str(value)
                prompt = fill_template(template["template"], value)
                review_prompts = [self._review_sections[section]["prompt"]
                                  for section in template.get("review_section_ids") or []
                                  if section in self._review_sections]
                task_id = str(uuid.uuid4())
                task = self._build_task(task_id, prompt=prompt,
                                        label=_clean_label(value) or _clean_label(template["name"]) or _derive_label(prompt),
                                        created_at=now(), review_prompts=review_prompts, color=template.get("color"),
                                        template_id=template_id, harness=harness_id, project_id=project["id"],
                                        created_by_task_id=created_by_task_id,
                                        session_id=self._initial_session_id(harness_id))
                self._tasks[task_id] = task
                self._task_order.append(task_id)
                ids.append(task_id)
            self._bump_version()
            return ids

    def remove_task(self, task_id):
        """True, False if missing, "running_in_subtree", or "cleanup_pending"."""
        while True:
            with self._lock:
                if task_id not in self._tasks:
                    return False
                subtree_ids = [task_id, *self._descendant_ids(task_id)]
                subtree = [self._tasks[i] for i in subtree_ids if i in self._tasks]
                if any(t["status"] in ACTIVE or (t.get("runner") and t["runner"].state in ("starting", "active"))
                       for t in subtree):
                    return "running_in_subtree"
                live = [t["runner"] for t in subtree if t.get("runner") and t["runner"].state != "exited"]
                if not live:
                    for i in subtree_ids:
                        self._tasks.pop(i, None)
                        if i in self._task_order:
                            self._task_order.remove(i)
                    self._bump_version()
                    return True
                for runner in live:
                    runner.close("Task removed")
            # Wait outside the pool lock so other tasks can keep running.
            # Recheck the subtree afterward: another request may have added work.
            for runner in live:
                runner.join()
            if any(runner.state != "exited" for runner in live):
                return "cleanup_pending"

    def cancel_task(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or not self._cancel_unlocked(task):
                return False
            self._bump_version()
            return True

    def cancel_all_tasks(self, project_id=None):
        with self._lock:
            changed = sum(1 for task_id in self._task_order
                          if (not project_id or self._tasks[task_id]["project_id"] == project_id)
                          and self._cancel_unlocked(self._tasks[task_id]))
            if changed:
                self._bump_version()
            return changed

    def _cancel_unlocked(self, task):
        if task["status"] == "queued":
            task["status"] = "cancelled"
            task["completed_at"] = now()
            return True
        if task["status"] in ACTIVE:
            if task.get("runner"):
                task["runner"].close("Task cancelled")
            task["status"] = "cancelled"
            task["completed_at"] = now()
            task["cancel_requested"] = True
            return True
        return False

    def retry_task(self, task_id, harness=None):
        """True, False if not retryable, or "has_children"."""
        with self._lock:
            self._poll_live_runners()
            task = self._tasks.get(task_id)
            if not task or task["status"] not in FINISHED:
                return False
            if any(t.get("parent_task_id") == task_id for t in self._tasks.values()):
                return "has_children"
            harness_id = self._find_harness(harness)["id"] if str(harness or "").strip() else _task_harness_id(task)
            self._require_available(self._find_harness(harness_id), self._projects[task["project_id"]])
            runner = task.get("runner")
            if runner and runner.state != "exited":
                runner.close("Retry requested")
                runner.join()
                if runner.state != "exited":
                    return False
                self._poll_live_runners()
            task["runner"] = None
            # Keep only ID/hash receipts, not old message text, across retries.
            task.setdefault("message_receipts", []).extend(
                tm.tombstone(m) for m in task["messages"] if m["role"] == "user" and m.get("sequence"))
            prompt = tm.initial(task)["content"]
            review_prompts = [m["content"] for m in tm.reviews(task)]
            task["messages"] = [tm.message("user", "initial", prompt, delivery="scheduled")]
            tm.schedule_reviews(task, review_prompts)
            task["status"] = "queued"
            task.pop("result_placement", None)
            task["session_id"] = self._initial_session_id(harness_id)
            task["exit_status"] = None
            task["started_at"] = None
            task["completed_at"] = None
            task.pop("active_review_id", None)
            task.pop("pending_review", None)
            task["cancel_requested"] = False
            task["failure_details"] = None
            task["cost"] = None
            task["session_file"] = None
            task["cost_refreshed_at"] = None
            task["harness"] = harness_id
            self._bump_version()
            return True

    def toggle_done(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            task["done"] = not task.get("done")
            self._bump_version()
            return task["done"]

    def continue_task(self, task_id):
        with self._lock:
            # Exit/reaping can precede the monitor's final event drain. Preserve
            # the old delivery ledger before replacing its runner.
            self._poll_live_runners()
            task = self._tasks.get(task_id)
            if not task or task["status"] not in ("failed", "cancelled"):
                return False
            if task.get("runner") and task["runner"].state != "exited":
                return False
            if not task.get("session_id"):
                return False
            task["cancel_requested"] = False
            self._start_live_runner(task, "continue", kind="continue", resume=True)
            return True

    def reorder_queue(self, ordered_ids, project_id=None):
        """Reorder the queued tasks (of one project, when given); other tasks keep their places."""
        with self._lock:
            queued = [i for i in self._task_order if self._tasks[i]["status"] == "queued"
                      and (not project_id or self._tasks[i]["project_id"] == project_id)]
            if not isinstance(ordered_ids, list) or sorted(map(str, ordered_ids)) != sorted(queued):
                return False
            moving = set(queued)
            others = [i for i in self._task_order if i not in moving]
            # Running tasks first, then the queue, then finished tasks. The reordered
            # tasks take the queue positions they held, so other projects' queues keep theirs.
            running = [i for i in others if self._tasks[i]["status"] in ACTIVE]
            done = [i for i in others if self._tasks[i]["status"] not in ACTIVE and self._tasks[i]["status"] != "queued"]
            slots = iter(str(i) for i in ordered_ids)
            queue = [next(slots) if i in moving else i
                     for i in self._task_order if self._tasks[i]["status"] == "queued"]
            self._task_order = running + queue + done
            self._bump_version()
            return True

    # --- Controls ---

    def pause(self):
        with self._lock:
            self._paused = True
            self._bump_version()

    def resume(self):
        with self._lock:
            self._paused = False
            self._bump_version()

    # --- Config ---

    def config(self):
        with self._lock:
            return {
                "max_parallel": self._max_parallel,
                "session_viewer_url": self._session_viewer_url,
                "allowed_hosts": self._allowed_hosts,
                "prevent_sleep_when_running": self._prevent_sleep_when_running,
                "sleep_prevention_available": (_sleep_preventer_command() is not None
                                               and not self._sleep_preventer_disabled),
                "sleep_prevention_requirement": self._sleep_prevention_requirement(),
                "show_task_cost": self._show_task_cost,
                "projects": [self._project_summary(p) for p in self._projects.values()],
                "harnesses": self._harnesses,
                "harness_types": HARNESS_TYPES,
                "review_sections": list(self._review_sections.values()),
                "templates": list(self._templates.values()),
                "output_actions": self._output_actions,
                "paused": self._paused,
            }

    def allowed_hosts(self):
        with self._lock:
            # An empty list falls back to the defaults; blocking every host would lock the user out of the UI, including Settings.
            return self._allowed_hosts or ["localhost", "127.0.0.1"]

    def update_config(self, params):
        with self._lock:
            # Templates are validated before anything changes, so a rejected
            # Settings save leaves the whole configuration untouched.
            projects = None
            if params.get("projects") is not None:
                projects = dict(self._projects)
                for update in params["projects"]:
                    if str(update.get("id")) not in projects:
                        raise HiveError("Unknown project; create projects with POST /api/projects")
                    record = pj.project_record(**{k: update.get(k) for k in PROJECT_FIELDS if k in update})
                    projects[record["id"]] = record
                pj.validate_project_names(projects.values())
            harnesses = None
            if _given(params, "harnesses"):
                harnesses = [_normalize_harness(h) for h in params["harnesses"]]
                _validate_harness_names(harnesses)
                if not harnesses:
                    raise HiveError("At least one harness is required")
            actions = None
            if params.get("output_actions") is not None:
                try:
                    actions = output_actions.normalize_all(params["output_actions"])
                except ValueError as error:
                    raise HiveError(str(error)) from None
            review_sections = None
            if params.get("review_sections") is not None:
                review_sections = {}
                for section in params["review_sections"]:
                    if not (section.get("id") and section.get("prompt")):
                        continue
                    section_id = str(section["id"])
                    review_sections[section_id] = _review_section_record(section, projects or self._projects)
            templates = None
            if params.get("templates") is not None:
                templates = [_template_record(**{k: t.get(k) for k in TEMPLATE_FIELDS if k in t})
                             for t in params["templates"]]
                for template in templates:
                    if template["project_id"] not in (projects or self._projects):
                        raise HiveError(f"Template '{template['name']}' belongs to an unknown project")
                _validate_template_names(templates)
            proposed_harnesses = harnesses if harnesses is not None else self._harnesses
            proposed_projects = projects if projects is not None else self._projects
            unavailable = [p["name"] for p in proposed_projects.values()
                           if not any(pj.available_in(h, p) for h in proposed_harnesses)]
            if unavailable:
                names = ", ".join(f"'{name}'" for name in unavailable)
                raise HiveError(f"Every project needs at least one available harness; none available in {names}")
            config_changed = False
            if _given(params, "max_parallel"):
                self._max_parallel = max(_to_int(params["max_parallel"]), 1)
                config_changed = True
            if _given(params, "session_viewer_url"):
                self._session_viewer_url = params["session_viewer_url"]
                config_changed = True
            if _given(params, "allowed_hosts"):
                self._allowed_hosts = params["allowed_hosts"]
                config_changed = True
            if "prevent_sleep_when_running" in params:
                requested = bool(params["prevent_sleep_when_running"])
                if requested and (_sleep_preventer_command() is None or self._sleep_preventer_disabled):
                    raise HiveError(self._sleep_prevention_requirement())
                self._prevent_sleep_when_running = requested
                if not requested:
                    self._stop_sleep_preventer()
                config_changed = True
            if "show_task_cost" in params:
                self._show_task_cost = bool(params["show_task_cost"])
                config_changed = True
            if harnesses is not None:
                self._harnesses = harnesses
                config_changed = True
            if actions is not None:
                self._output_actions = actions
                config_changed = True
            if config_changed:
                self._save_config()
            if projects is not None:
                self._projects = projects
            if review_sections is not None:
                self._review_sections = review_sections
            if templates is not None:
                self._templates = {t["id"]: t for t in templates}
            if review_sections is not None or templates is not None:
                for template in self._templates.values():
                    template["review_section_ids"] = self._template_section_ids(template)
            self._bump_version()

    def save_template(self, **fields):
        with self._lock:
            record = _template_record(**fields)
            if record["project_id"] not in self._projects:
                raise HiveError("Template needs an existing project")
            record["review_section_ids"] = self._template_section_ids(record)
            _validate_template_names([t for t in self._templates.values() if t["id"] != record["id"]] + [record])
            self._templates[record["id"]] = record
            self._bump_version()
            return record["id"]

    def delete_template(self, template_id):
        with self._lock:
            self._templates.pop(template_id, None)
            self._bump_version()

    # --- Projects ---

    def create_project(self, name, work_dir, color=None):
        with self._lock:
            record = pj.project_record(name=name, work_dir=work_dir, color=color)
            pj.validate_project_names([*self._projects.values(), record])
            self._projects[record["id"]] = record
            self._bump_version()
            return record["id"]

    def delete_project(self, project_id):
        """True, False if missing, "last_project", or "active_tasks".

        Deletes the project's tasks, templates, and review sections, and removes
        the project from harnesses and output actions limited to it.
        """
        with self._lock:
            if project_id not in self._projects:
                return False
            if len(self._projects) == 1:
                return "last_project"
            ids = [i for i in self._task_order if self._tasks[i]["project_id"] == project_id]
            if any(self._tasks[i]["status"] == "queued" or self._tasks[i]["status"] in ACTIVE
                   or (self._tasks[i].get("runner") and self._tasks[i]["runner"].state != "exited") for i in ids):
                return "active_tasks"
            for task_id in ids:
                self._tasks.pop(task_id)
                self._task_order.remove(task_id)
            self._templates = {k: t for k, t in self._templates.items() if t["project_id"] != project_id}
            self._review_sections = {k: s for k, s in self._review_sections.items()
                                     if s["project_id"] != project_id}
            for item in [*self._harnesses, *self._output_actions]:
                if item.get("only") is not None and project_id in item["only"]:
                    item["only"] = [i for i in item["only"] if i != project_id]
            del self._projects[project_id]
            self._save_config()
            self._bump_version()
            return True

    def projects(self, project_id=None):
        """What an agent or the CLI needs to know about each project; only one project with project_id.

        Harness commands are left out: a project-scoped caller has no use for them.
        """
        with self._lock:
            chosen = [self._projects[project_id]] if project_id else list(self._projects.values())
            result = []
            for project in chosen:
                harnesses = self._available_harnesses(project)
                result.append({
                    **self._project_summary(project),
                    "harnesses": [{k: h[k] for k in ("id", "name", "icon", "color", "type")} for h in harnesses],
                    "templates": [{k: t[k] for k in ("id", "name", "template", "harness", "color")}
                                  for t in self._templates.values() if t["project_id"] == project["id"]],
                })
            return result

    def task_project(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            return task["project_id"] if task else None

    def template_project(self, template_id):
        with self._lock:
            template = self._templates.get(template_id)
            return template["project_id"] if template else None

    def has_project(self, project_id):
        with self._lock:
            return project_id in self._projects

    def token_project(self, token):
        """The project an agent token is limited to, or None if the token is not an agent token."""
        with self._lock:
            return self._agent_tokens.lookup(token)

    def token_context(self, token):
        """Trusted project and creator-task identity for an agent token."""
        with self._lock:
            return self._agent_tokens.context(token)

    def backup_zip(self):
        """A consistent ZIP snapshot of the two allowlisted restore files."""
        with self._lock:
            self._save_state(strict=True)
            try:
                with open(self.config_file, "rb") as file:
                    config = file.read()
                with open(self.persist_file, "rb") as file:
                    state = file.read()
            except OSError as error:
                raise HiveError(f"Cannot read backup files: {error}") from error
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
                output.writestr("config.json", config)
                output.writestr("data/state.json", state)
            return archive.getvalue()

    def run_output_action(self, tag, value):
        """"not_found", None on success, or the failure message."""
        with self._lock:
            action = next((a for a in self._output_actions if a["tag"] == str(tag)), None)
        if not action:
            return "not_found"
        return output_actions.run(action, value, env=full_env(child_process_env()))

    # --- Chat ---

    def send_chat(self, task_id, message, message_id=None):
        """Receipt dict, None if chat is impossible, or a reason string."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            self._poll_live_runners()
            if message_id is not None:
                if not (isinstance(message_id, str) and MESSAGE_ID.fullmatch(message_id)):
                    raise HiveError("Invalid message ID")
                previous = next((m for m in task["messages"] if m["id"] == message_id), None)
                if previous:
                    if not (previous["role"] == "user" and previous["content"] == message):
                        raise HiveError("Message ID already used for different text")
                    return _message_receipt(previous)
                previous = next((m for m in task.get("message_receipts") or [] if m["id"] == message_id), None)
                if previous:
                    if not (isinstance(message, str) and previous["content_sha256"] == tm.content_hash(message)):
                        raise HiveError("Message ID already used for different text")
                    return {k: previous.get(k) for k in ("id", "sequence", "status", "settled")}
            if task["status"] not in ("running", "reviewing", "completed", "failed"):
                return None
            if not str(task.get("session_id") or "").strip():
                return "missing_session"
            runner = task.get("runner")
            if runner and runner.state == "closing":
                return "runner_closing"
            # Board placement belongs to the finished task, not subsequent chat turns.
            if task["status"] in ("completed", "failed") and not task.get("result_placement"):
                task["result_placement"] = {"status": task["status"], "completed_at": iso(task.get("completed_at"))}
            if runner and runner.state != "exited":
                runner_state = runner.state
                if runner_state == "idle":
                    task["runner_kind"] = "chat"
                kind = "follow_up" if runner_state == "idle" else "steering"
                receipt = self._record_live_input(task, runner, message, message_id or str(uuid.uuid4()), kind)
                task["status"] = "reviewing" if task.get("runner_kind") == "review" else "running"
                task["completed_at"] = None
                task["failure_details"] = None
                self._bump_version()
                return _message_receipt(receipt)
            if tm.outstanding(task):
                return "unresolved_inputs"
            receipt = self._start_live_runner(task, message, kind="chat", resume=True, message_id=message_id)
            return _message_receipt(receipt)

    def cancel_chat(self, task_id, msg_id=None):
        """Stop the active conversation run.

        A live input can share model context and output with other inputs, so a
        matching pending message ID cancels the whole run.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            runner = task and task.get("runner")
            if not runner or runner.state not in ("starting", "active"):
                return False
            if msg_id and not any(m["id"] == msg_id for m in tm.outstanding(task)):
                return False
            runner.close("Conversation run cancelled")
            task["cancel_requested"] = True
            task["status"] = "cancelled"
            task["completed_at"] = now()
            self._bump_version()
            return True

    # --- Persistence ---

    def _bump_version(self):
        self.version += 1
        self._maybe_save()

    def _maybe_save(self):
        if self.version == self._last_saved_version:
            return
        if time.monotonic() - self._last_save_time < SAVE_INTERVAL:
            return
        self._save_state()

    def _save_state(self, strict=False):
        data = {
            "schema_version": SCHEMA_VERSION,
            "projects": self._projects,
            "tasks": {task_id: _persist_task(task) for task_id, task in self._tasks.items()},
            "task_order": self._task_order,
            "review_sections": self._review_sections,
            "templates": self._templates,
            "paused": self._paused,
        }
        try:
            _write_json_atomic(self.persist_file, data)
            self._last_saved_version = self.version
            self._last_save_time = time.monotonic()
        except OSError as error:
            print(f"[persist] save error: {error}", file=sys.stderr)
            if strict:
                raise HiveError(f"Cannot persist input safely: {error}") from error

    def _load_config(self):
        if not os.path.exists(self.config_file):
            return
        try:
            with open(self.config_file, encoding="utf-8") as file:
                raw = json.load(file)
        except (OSError, ValueError) as error:
            print(f"[config] load error: {error}", file=sys.stderr)
            return
        for key in ("max_parallel", "work_dir", "session_viewer_url", "allowed_hosts", "default_harness",
                    "output_actions"):
            if _given(raw, key):
                setattr(self, f"_{key}", raw[key])
        self._prevent_sleep_when_running = bool(raw.get("prevent_sleep_when_running"))
        self._show_task_cost = bool(raw.get("show_task_cost"))
        if _given(raw, "harnesses"):
            self._harnesses = [_normalize_harness(h) for h in raw["harnesses"]]

    def _save_config(self):
        # work_dir and default_harness now belong to projects; they stay in the
        # file only as the values for a first project.
        data = {
            "max_parallel": self._max_parallel,
            "work_dir": self._work_dir,
            "session_viewer_url": self._session_viewer_url,
            "allowed_hosts": self._allowed_hosts,
            "prevent_sleep_when_running": self._prevent_sleep_when_running,
            "show_task_cost": self._show_task_cost,
            "harnesses": self._harnesses,
            "default_harness": self._default_harness,
            "output_actions": self._output_actions,
        }
        try:
            _write_json_atomic(self.config_file, data)
        except OSError as error:
            print(f"[config] save error: {error}", file=sys.stderr)

    def _load_state(self):
        if not os.path.exists(self.persist_file):
            return
        with open(self.persist_file, encoding="utf-8") as file:
            raw = json.load(file)
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise HiveError(f"Unsupported state schema {raw.get('schema_version')}; "
                            f"only schema_version {SCHEMA_VERSION} state files can be loaded")
        for key, project in (raw.get("projects") or {}).items():
            record = pj.project_record(**{"id": key, **{k: project.get(k) for k in PROJECT_FIELDS if k != "id"}})
            self._projects[record["id"]] = record
        if not self._projects:
            raise HiveError("The state file has no projects")
        for key, section in (raw.get("review_sections") or {}).items():
            section_id = str(section.get("id") or key)
            self._review_sections[section_id] = _review_section_record({**section, "id": section_id}, self._projects)
        if "paused" in raw:
            self._paused = raw["paused"]
        for key, template in (raw.get("templates") or {}).items():
            template_id = template.get("id") or key
            if template.get("project_id") not in self._projects:
                raise HiveError(f"Template '{template.get('name')}' belongs to an unknown project")
            self._templates[key] = {
                "id": template_id,
                "project_id": template["project_id"],
                "name": template.get("name"),
                "template": template.get("template"),
                "review_section_ids": [str(i) for i in template.get("review_section_ids") or []
                                       if self._review_sections.get(str(i), {}).get("project_id")
                                       == template["project_id"]],
                "color": template.get("color"),
                "work_dir": template.get("work_dir"),
                "agent_args": normalize_args(template.get("agent_args")),
                "harness": template.get("harness"),
            }
        # Tasks that were running when the server died stay as interrupted
        # terminal states; they are not automatically restarted.
        self._task_order = [str(i) for i in raw.get("task_order") or []]
        for key, stored in (raw.get("tasks") or {}).items():
            if stored.get("project_id") not in self._projects:
                raise HiveError(f"Task {key} belongs to an unknown project")
            status = stored.get("status")
            if status in ACTIVE:
                status = "failed"
                stored["completed_at"] = stored.get("completed_at") or iso(now())
                stored["failure_details"] = stored.get("failure_details") or "Task was interrupted by server shutdown."
            task = self._build_task(
                key, messages=tm.restore(stored.get("messages")), label=stored.get("label"), status=status,
                session_id=stored["session_id"] if "session_id" in stored else str(uuid.uuid4()),
                exit_status=stored.get("exit_status"), started_at=parse_time(stored.get("started_at")),
                completed_at=parse_time(stored.get("completed_at")),
                created_at=parse_time(stored.get("created_at")) or now(),
                failure_details=stored.get("failure_details"), color=stored.get("color"),
                template_id=stored.get("template_id"), parent_task_id=stored.get("parent_task_id"),
                created_by_task_id=stored.get("created_by_task_id"),
                harness=stored.get("harness"), project_id=stored["project_id"])
            task["result_placement"] = stored.get("result_placement")
            task["done"] = bool(stored.get("done"))
            task["message_receipts"] = stored.get("message_receipts") or []
            for entry in task["messages"]:
                if entry.get("settled") is False or entry.get("delivery") == "pending":
                    entry.update(delivery="unknown", settled=True, error="Server restarted; delivery unknown, not resent")
            if task["failure_details"] is None and task["status"] in ("failed", "cancelled"):
                task["failure_details"] = _fallback_failure_details(task)
            self._tasks[key] = task
        self._task_order = [i for i in self._task_order if i in self._tasks]
        self.version = 1
        self._last_saved_version = self.version
        self._last_save_time = time.monotonic()
        print(f"[persist] loaded {len(self._tasks)} tasks, {len(self._templates)} templates", flush=True)

    # --- Serialization ---

    def _state_unlocked(self, project_id=None):
        queued, running, completed = [], [], []
        running_total = 0
        for task_id in self._task_order:
            task = self._tasks[task_id]
            if task["status"] in ACTIVE:
                running_total += 1
            if project_id and task["project_id"] != project_id:
                continue
            placement = "completed" if task.get("result_placement") else task["status"]
            if placement == "queued":
                queued.append(self._serialize_task(task))
            elif placement in ACTIVE:
                running.append(self._serialize_task(task, include_output_tail=True))
            elif placement in FINISHED:
                completed.append(self._serialize_task(task))
        return {
            "version": self.version,
            "instance": self._instance_id,
            "paused": self._paused,
            "max_parallel": self._max_parallel,
            # Workers across all projects: the worker limit is shared.
            "running_total": running_total,
            "prevent_sleep_when_running": self._prevent_sleep_when_running,
            "show_task_cost": self._show_task_cost,
            "queue": queued,
            "workers": running,
            "results": completed,
            "stats": {
                "queued": len(queued),
                "running": len(running),
                "completed": sum(1 for t in completed if t["status"] == "completed"),
                "failed": sum(1 for t in completed if t["status"] == "failed"),
                "cancelled": sum(1 for t in completed if t["status"] == "cancelled"),
            },
        }

    def _serialize_task(self, task, full=False, include_output_tail=False):
        runner = task.get("runner")
        messages = task["messages"]
        follow_up_replies = [utc_micro(m["timestamp"]) for m in messages
                             if m["role"] == "assistant" and m["kind"] == "follow_up" and m.get("timestamp")]
        last_answer = next((m for m in reversed(messages) if m["role"] == "assistant"), {})
        data = {
            "id": task["id"],
            "project_id": task["project_id"],
            "prompt": tm.initial(task)["content"],
            "label": task.get("label"),
            "status": task["status"],
            "session_id": task.get("session_id"),
            "runner_state": runner.state if runner else "exited",
            "background_activity": runner.background_activity() if runner else dict(IDLE_BACKGROUND),
            "exit_status": task.get("exit_status"),
            "started_at": iso(task.get("started_at")),
            "completed_at": iso(task.get("completed_at")),
            "created_at": iso(task.get("created_at")),
            "chat_count": sum(1 for m in messages if m["kind"] in ("follow_up", "steering", "continue")),
            "last_chat_at": _last_chat_timestamp(task),
            "latest_follow_up_reply_at": max(follow_up_replies) if follow_up_replies else None,
            "chat_pending": bool(tm.outstanding(task)),
            "has_review": any(m["role"] == "assistant" and m["kind"] == "review" for m in messages),
            "summary": _summarize_output(task.get("failure_details") or last_answer.get("content")),
            "color": task.get("color"),
            "template_id": task.get("template_id"),
            "parent_task_id": task.get("parent_task_id"),
            "created_by_task_id": task.get("created_by_task_id"),
            "harness": _task_harness_id(task),
            # Derived from the harness transcript, held in memory only.
            "cost": task.get("cost"),
            "done": bool(task.get("done")),
        }
        # Keep execution status accurate in task details and for scheduling.
        # Only board cards retain the original result's placement and completion time.
        placement = task.get("result_placement")
        if not full and placement:
            data["completed_at"] = placement.get("completed_at")
            if task["status"] in ACTIVE:
                data["status"] = placement.get("status")
        if full:
            del data["prompt"]
            data["messages"] = messages
            data["diagnostics"] = self._restore_diagnostics(task["id"])
            data["failure_details"] = task.get("failure_details")
        elif include_output_tail:
            text = tm.output(task)
            data["output_tail"] = text[-500:]
            data["output_length"] = len(text)
        return data

    # --- Monitor ---

    def _monitor_loop(self):
        while self._running:
            with self._lock:
                self._poll_live_runners()
                self._refresh_running_costs()
                if not self._paused:
                    self._fill_pool()
                self._update_sleep_preventer()
                self._maybe_save()
            time.sleep(0.3)

    def _fill_pool(self):
        active = sum(1 for task_id in self._task_order if self._tasks[task_id]["status"] in ACTIVE)
        while active < (self._max_parallel or 1):
            queued = next((i for i in self._task_order if self._tasks[i]["status"] == "queued"), None)
            if not queued:
                break
            self._spawn_task(self._tasks[queued])
            active += 1

    def _spawn_task(self, task):
        try:
            parent_id = task.get("parent_task_id")
            parent = self._tasks[parent_id]["session_id"] if parent_id else None
            first = tm.initial(task)
            self._start_live_runner(task, first["content"], kind="initial", parent=parent, message_id=first["id"])
        except Exception as error:
            task["status"] = "failed"
            task["completed_at"] = now()
            task["failure_details"] = f"Spawn error: {error}"
            self._bump_version()

    def _start_live_runner(self, task, message, kind, resume=False, parent=None, message_id=None):
        runner = None
        started = False
        try:
            if task.get("runner") and task["runner"].state != "exited":
                raise HiveError("Previous runner has not exited")
            if not valid_message(message):
                raise HiveError("Message must contain text and be at most 256 KiB")
            harness = self._task_harness(task)
            # Every start of an agent passes here (first run, retry, continue, follow-up, review),
            # so this one check keeps a project on its own harnesses.
            self._require_available(harness, self._projects[task["project_id"]])
            runner = LiveRunner(protocol=protocol_for(harness["type"]), command=harness["command"],
                                args=self._effective_agent_args(task), session_id=task["session_id"],
                                chdir=self._resolve_work_dir(task), env=child_process_env(), resume=resume,
                                parent=parent, task_id=task["id"], harness=harness["type"],
                                diagnostics_path=self._diagnostics_path(task["id"]))
            # These values identify this exact task and its project-scoped API access.
            # child_process_env removed any inherited values before they are set here.
            runner.env["HIVE_TOKEN"] = self._agent_tokens.issue(task["project_id"], task["id"], runner)
            runner.env["HIVE_TASK_ID"] = task["id"]
            if kind != "chat":
                task.pop("result_placement", None)
            task["runner"] = runner
            task["runner_kind"] = kind
            task["status"] = "running"
            task["started_at"] = task.get("started_at") or now()
            task["completed_at"] = None
            task["failure_details"] = None
            task["cancel_requested"] = False
            receipt = self._record_live_input(task, runner, message, message_id or str(uuid.uuid4()),
                                              "follow_up" if kind == "chat" else kind)
            runner.start()
            started = True
            self._ensure_sleep_preventer()
            self._bump_version()
            return receipt
        except Exception as error:
            if runner and not started:
                self._agent_tokens.revoke_runner(runner)
                task["runner"] = None
            task["status"] = "failed"
            task["failure_details"] = redact(str(error))
            task["completed_at"] = now()
            self._bump_version()
            raise

    def _record_live_input(self, task, runner, message, message_id, kind):
        if not valid_message(message):
            raise HiveError("Message must contain text and be at most 256 KiB")
        entry = next((m for m in task["messages"] if m["id"] == message_id), None)
        if entry:
            if not (entry.get("delivery") == "scheduled" and entry["content"] == message):
                raise HiveError("Input has already been submitted")
            task["messages"].remove(entry)
        else:
            entry = tm.message("user", kind, message, id=message_id)
        sequence = max((m["sequence"] for m in task["messages"] if m.get("sequence")), default=0) + 1
        entry.update(session_id=task["session_id"], sequence=sequence, delivery="accepted", settled=False,
                     timestamp=iso_micro())
        if task.get("runner_kind") == "review":
            entry["review_id"] = task.get("active_review_id")
        task["messages"].append(entry)
        # Persist intent before writing. Restart never blindly replays uncertain input.
        try:
            self._save_state(strict=True)
            runner.submit(message, id=message_id)
        except Exception as error:
            entry.update(delivery="rejected", settled=True, error=str(error))
            self._bump_version()
            self._save_state()
            raise
        return entry

    def _next_review(self, task, runner):
        review = next((m for m in tm.reviews(task) if m.get("delivery") == "scheduled"), None)
        if not review:
            return
        task["runner_kind"] = "review"
        task["active_review_id"] = review.get("review_id")
        task["status"] = "reviewing"
        task["completed_at"] = None
        if runner.state == "idle":
            self._record_live_input(task, runner, review["content"], review["id"], "review")
        else:
            task["pending_review"] = review["id"]

    def _poll_live_runners(self):
        self._agent_tokens.revoke_exited()
        for task in list(self._tasks.values()):
            runner = task.get("runner")
            if not runner:
                continue
            try:
                for kind, data in runner.drain():
                    if self._apply_runner_event(task, runner, kind, data):
                        self._bump_version()
            except Exception as error:
                runner.close("Hive could not apply runner events")
                task["status"] = "failed"
                task["failure_details"] = f"Runner event handling failed: {redact(str(error))}"
                task["completed_at"] = now()
                self._bump_version()

    def _apply_runner_event(self, task, runner, kind, data):
        """Apply one runner event. Returns False when the event changed nothing visible."""
        if kind == "diagnostic":
            return False  # RunnerDiagnostics already wrote this event to the journal.
        if kind == "background_activity":
            return True  # Read live from the runner when serializing; only the version changes.
        if kind == "warning":
            task["messages"].append(tm.message("system", "notice", data, timestamp=iso_micro()))
        elif kind == "delivery":
            entry = next((m for m in task["messages"] if m["id"] == data["id"]), None)
            if not entry:
                raise HiveError(f"Unknown delivery message {data['id']}")
            # The canonical message owns content and ordering; events update delivery only.
            for key in ("settled", "harness_accepted", "written", "error", "result_id"):
                if key in data:
                    entry[key] = data[key]
            if data.get("status"):
                entry["delivery"] = data["status"]
        elif kind == "output":
            if any(m["id"] == data["id"] for m in task["messages"]):
                return False
            answer_kind = "follow_up" if task.get("runner_kind") == "chat" else task.get("runner_kind")
            answer = tm.message("assistant", answer_kind, data["content"], id=data["id"],
                                input_ids=data["input_ids"], timestamp=iso_micro())
            if answer_kind == "review":
                answer["review_id"] = task.get("active_review_id")
            task["messages"].append(answer)
        elif kind == "settled":
            if task.get("cancel_requested"):
                return False
            # A later input may already have reactivated the same runner.
            if tm.outstanding(task):
                return False
            error = data.get("error")
            task["exit_status"] = 1 if error else 0
            task["status"] = "failed" if error else "completed"
            task["failure_details"] = error
            task["completed_at"] = now()
            self._refresh_task_cost(task)
            if task.get("runner_kind") in ("initial", "review") and not error:
                self._next_review(task, runner)
        elif kind == "error":
            if task.get("cancel_requested"):
                task["messages"].append(tm.message("system", "notice", f"Runner cleanup error: {data}",
                                                   timestamp=iso_micro()))
            else:
                task["messages"].append(tm.message("system", "notice", f"Runner stopped: {data}",
                                                   timestamp=iso_micro()))
                task["failure_details"] = data
                task["status"] = "failed"
            task["completed_at"] = now()
        elif kind == "exited":
            if task.get("pending_review") and not task.get("cancel_requested"):
                review_id = task.pop("pending_review")
                review = next((m for m in task["messages"] if m["id"] == review_id), None)
                if not review:
                    raise HiveError("Scheduled review not found")
                self._start_live_runner(task, review["content"], kind="review", resume=True, message_id=review["id"])
                task["status"] = "reviewing"
            elif task["status"] in ACTIVE:
                task["status"] = "failed"
                task["failure_details"] = task.get("failure_details") or "Harness exited before settlement"
                task["completed_at"] = now()
        return True

    # --- Diagnostics ---

    def _diagnostics_path(self, task_id):
        return os.path.join(os.path.dirname(self.persist_file), "runner-diagnostics", f"{task_id}.jsonl")

    def _restore_diagnostics(self, task_id):
        path = self._diagnostics_path(task_id)
        if not os.path.isfile(path):
            return []
        records = []
        try:
            with open(path, encoding="utf-8") as file:
                for line in file:
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        records.append({"kind": "journal_error", "task_id": task_id,
                                        "error": "Malformed diagnostic journal record; raw text omitted",
                                        "timestamp": iso(now())})
                    if len(records) > 200:
                        records.pop(0)
        except (OSError, UnicodeDecodeError) as error:
            return [{"kind": "journal_error", "task_id": task_id,
                     "error": f"Cannot read diagnostic journal ({type(error).__name__})", "timestamp": iso(now())}]
        return records

    # --- Tasks ---

    def _build_task(self, task_id, label, created_at, prompt=None, messages=None, status="queued", session_id=None,
                    exit_status=None, started_at=None, completed_at=None, review_prompts=(), failure_details=None,
                    color=None, template_id=None, parent_task_id=None, created_by_task_id=None,
                    harness=None, project_id=None):
        task = {
            "id": task_id,
            "project_id": project_id,
            "messages": messages if messages is not None else [
                tm.message("user", "initial", prompt, delivery="scheduled", timestamp=iso_micro(created_at))],
            "label": label,
            "status": status,
            "session_id": session_id,
            "exit_status": exit_status,
            "started_at": started_at,
            "completed_at": completed_at,
            "created_at": created_at,
            "failure_details": failure_details,
            "cancel_requested": False,
            "color": color,
            "template_id": template_id,
            "parent_task_id": parent_task_id,
            "created_by_task_id": str(created_by_task_id) if created_by_task_id else None,
            "harness": harness,
            "done": False,
        }
        tm.schedule_reviews(task, review_prompts)
        return task

    def _descendant_ids(self, task_id):
        result = []
        pending = [task_id]
        while pending:
            current = pending.pop(0)
            for child_id, task in self._tasks.items():
                if task.get("parent_task_id") == current and child_id not in result:
                    result.append(child_id)
                    pending.append(child_id)
        return result

    def _resolve_work_dir(self, task):
        """Per-template working directory, falling back to the project's."""
        template = self._templates.get(task.get("template_id")) if task.get("template_id") else None
        directory = (template or {}).get("work_dir") or self._projects[task["project_id"]]["work_dir"]
        if not str(directory or "").strip():
            raise HiveError("Working directory is not configured")
        directory = os.path.expanduser(directory)
        os.makedirs(directory, exist_ok=True)
        return directory

    def _effective_agent_args(self, task):
        """Harness args plus template extras, resolved at execution time."""
        template = self._templates.get(task.get("template_id")) if task.get("template_id") else None
        return list(self._task_harness(task).get("args") or []) + list((template or {}).get("agent_args") or [])

    # --- Harnesses ---

    def _resolve_new_task_harness_id(self, project, harness=None, template_id=None):
        """The named harness, else the template's, else the project's default; it must be available in the project."""
        explicit = str(harness or "").strip()
        template = self._templates.get(template_id) if template_id else None
        chosen = explicit or str((template or {}).get("harness") or "").strip()
        if chosen:
            found = self._find_harness(chosen)
            self._require_available(found, project)
            return found["id"]
        default = self._project_default_harness(project)
        if not default:
            raise HiveError(f"No harness is available in project '{project['name']}'")
        return default["id"]

    def _available_harnesses(self, project):
        return [h for h in self._harnesses if pj.available_in(h, project)]

    def _project_default_harness(self, project):
        """The project's chosen default while it is available, else the first available harness."""
        available = self._available_harnesses(project)
        chosen = next((h for h in available if h["id"] == project.get("default_harness")), None)
        return chosen or (available[0] if available else None)

    def _require_available(self, harness, project):
        if not pj.available_in(harness, project):
            raise HiveError(f"Harness '{harness['name']}' is not available in project '{project['name']}'")

    def _project_summary(self, project):
        default = self._project_default_harness(project)
        return {
            **project,
            "default_harness": default["id"] if default else None,
            "available_harnesses": [h["id"] for h in self._available_harnesses(project)],
            "output_actions": [a["tag"] for a in self._output_actions if pj.output_action_available_in(a, project)],
        }

    def _project_for_new_task(self, project_id):
        if project_id:
            project = self._projects.get(str(project_id))
            if not project:
                raise HiveError("Project not found")
            return project
        if len(self._projects) == 1:
            return next(iter(self._projects.values()))
        raise HiveError("Name a project: there is more than one")

    def _template_section_ids(self, template):
        """A template keeps only existing review sections of its own project."""
        return [i for i in template.get("review_section_ids") or []
                if self._review_sections.get(i, {}).get("project_id") == template["project_id"]]

    def _create_first_project(self):
        """On first start, one ordinary project from the old global settings in config.json."""
        record = pj.project_record(name=pj.FIRST_PROJECT_NAME, color="slate",
                                   work_dir=self._work_dir or os.path.join(os.path.expanduser("~"), "hihive"),
                                   default_harness=self._default_harness)
        self._projects[record["id"]] = record
        self._bump_version()
        self._save_state()

    def _find_harness(self, harness_id):
        harness = next((h for h in self._harnesses if h["id"] == str(harness_id)), None)
        if not harness:
            raise HiveError(f"Harness '{harness_id}' is not configured")
        return harness

    def _task_harness(self, task):
        return self._find_harness(_task_harness_id(task))

    def _initial_session_id(self, harness_id):
        return str(uuid.uuid4()) if adapter_for(self._find_harness(harness_id)["type"]).preassign_session_id() else None

    def _ensure_harnesses(self):
        """Require explicit harness config. Old tasks without a harness resolve to "claude-code"."""
        if not self._harnesses:
            raise HiveError("No harnesses configured")
        _validate_harness_names(self._harnesses)

    # --- Task cost ---
    # Cost is derived from the harness's own transcript, never stored: it is
    # re-read when it can change and rebuilt from the transcripts at startup.

    def _task_session_file(self, task):
        if not str(task.get("session_id") or "").strip():
            return None
        if not task.get("session_file"):
            harness = self._task_harness(task)
            task["session_file"] = adapter_for(harness["type"]).session_file(
                self._effective_agent_args(task), harness, task["session_id"])
        return task["session_file"]

    def _compute_task_cost(self, task):
        try:
            path = self._task_session_file(task)
            return adapter_for(self._task_harness(task)["type"]).session_cost(path) if path else None
        except Exception:
            return None

    def _refresh_task_cost(self, task):
        cost = self._compute_task_cost(task)
        task["cost_refreshed_at"] = time.monotonic()
        if cost != task.get("cost"):
            task["cost"] = cost
            self._bump_version()

    def _refresh_running_costs(self):
        moment = time.monotonic()
        for task_id in self._task_order:
            task = self._tasks[task_id]
            if task["status"] not in ACTIVE:
                continue
            if task.get("cost_refreshed_at") and moment - task["cost_refreshed_at"] < COST_REFRESH_INTERVAL:
                continue
            self._refresh_task_cost(task)

    def _backfill_task_costs(self):
        """One pass over every task at startup. Transcripts are read outside the lock."""
        try:
            with self._lock:
                ids = list(self._task_order)
            for task_id in ids:
                with self._lock:
                    task = self._tasks.get(task_id)
                if not task or task.get("cost") is not None:
                    continue
                cost = self._compute_task_cost(task)
                if cost is None:
                    continue
                with self._lock:
                    if task_id in self._tasks:
                        task["cost"] = cost
                        task["cost_refreshed_at"] = time.monotonic()
                        self._bump_version()
        except Exception as error:
            print(f"[cost] backfill error: {error}", file=sys.stderr)

    # --- Sleep prevention ---

    def _active_agent_work(self):
        return any(task["status"] in ACTIVE for task in self._tasks.values())

    def _sleep_prevention_requirement(self):
        requirement = _sleep_prevention_requirement()
        if self._sleep_preventer_disabled:
            return requirement + " The inhibitor exited unexpectedly; restart hi-hive to retry."
        return requirement

    def _ensure_sleep_preventer(self):
        command = _sleep_preventer_command()
        if not self._prevent_sleep_when_running or command is None or self._sleep_preventer_disabled:
            return
        if self._sleep_preventer:
            status = self._sleep_preventer.poll()
            if status is None:
                return
            self._sleep_preventer = None
            self._sleep_preventer_disabled = True
            print(f"[sleep] inhibitor exited with status {status}; disabled until hi-hive restarts", file=sys.stderr)
            return
        try:
            self._sleep_preventer = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                     start_new_session=True)
            self._sleep_preventer_grace_until = None
        except OSError as error:
            print(f"[sleep] failed to start sleep inhibitor; disabled until hi-hive restarts: {error}", file=sys.stderr)
            self._sleep_preventer = None
            self._sleep_preventer_disabled = True

    def _stop_sleep_preventer(self):
        if self._sleep_preventer and self._sleep_preventer.poll() is None:
            try:
                os.killpg(self._sleep_preventer.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self._sleep_preventer.wait(2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self._sleep_preventer.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self._sleep_preventer.wait()
        self._sleep_preventer = None
        self._sleep_preventer_grace_until = None

    def _update_sleep_preventer(self):
        if not self._prevent_sleep_when_running:
            self._stop_sleep_preventer()
            return
        if self._active_agent_work():
            self._sleep_preventer_grace_until = None
            self._ensure_sleep_preventer()
            return
        if self._sleep_preventer:
            if self._sleep_preventer_grace_until is None:
                self._sleep_preventer_grace_until = time.monotonic() + SLEEP_PREVENTION_GRACE_SECS
            if time.monotonic() >= self._sleep_preventer_grace_until:
                self._stop_sleep_preventer()


# --- Helpers ---

def _write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


def _persist_task(task):
    return {
        "id": task["id"],
        "label": task.get("label"),
        "status": task["status"],
        "session_id": task.get("session_id"),
        "exit_status": task.get("exit_status"),
        "started_at": iso(task.get("started_at")),
        "completed_at": iso(task.get("completed_at")),
        "created_at": iso(task.get("created_at")),
        "messages": task["messages"],
        "result_placement": task.get("result_placement"),
        "message_receipts": task.get("message_receipts") or [],
        "failure_details": task.get("failure_details"),
        "color": task.get("color"),
        "template_id": task.get("template_id"),
        "parent_task_id": task.get("parent_task_id"),
        "created_by_task_id": task.get("created_by_task_id"),
        "harness": task.get("harness"),
        "project_id": task["project_id"],
        "done": bool(task.get("done")),
    }


def _message_receipt(entry):
    return {"id": entry["id"], "sequence": entry.get("sequence"), "settled": entry.get("settled"),
            "status": entry.get("delivery")}


def _last_chat_timestamp(task):
    """Do not let the initial prompt's creation time replace the completion-time fallback."""
    stamps = [m["timestamp"] for m in task["messages"]
              if m.get("timestamp") and m.get("delivery") != "scheduled"
              and not (m["role"] == "user" and m["kind"] == "initial")]
    return max(stamps, key=parse_time) if stamps else None


def _task_harness_id(task):
    return str(task.get("harness") or "").strip() or LEGACY_TASK_HARNESS_ID


def _sleep_preventer_command():
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        return ["caffeinate", "-i", "-m", "-s"]
    if sys.platform.startswith("linux") and shutil.which("systemd-inhibit"):
        return ["systemd-inhibit", "--what=sleep:idle", "--who=hi-hive",
                "--why=Agents are running", "--mode=block", sys.executable, "-c",
                "import signal; signal.pause()"]
    return None


def _sleep_prevention_requirement():
    if sys.platform == "darwin":
        return "Requires macOS caffeinate on PATH."
    if sys.platform.startswith("linux"):
        return "Requires systemd-inhibit on PATH and an active systemd-logind session."
    return "Sleep prevention is unavailable on this platform."


def _derive_label(prompt):
    return WHITESPACE.sub(" ", str(prompt or "")).strip()[:60]


def _clean_label(label):
    if label is None:
        return None
    cleaned = WHITESPACE.sub(" ", str(label)).strip()
    return cleaned[:200] if cleaned else None


def _clean_output(text):
    return ANSI.sub("", str(text or "")).replace("\r", "").strip()


def _summarize_output(text):
    cleaned = _clean_output(text)
    if not cleaned:
        return None
    lines = [line.strip() for line in cleaned.split("\n")]
    lines = [line for line in lines if line and not DECORATION.fullmatch(line)]
    if not lines:
        return None
    preferred = next((line for line in reversed(lines) if FAILURE_WORDS.search(line)), None)
    summary = preferred or lines[0]
    return f"{summary[:157]}..." if len(summary) > 160 else summary


def _fallback_failure_details(task):
    cleaned = _clean_output(tm.output(task))
    if cleaned:
        return cleaned
    if task["status"] == "cancelled":
        return "Task was cancelled."
    if task.get("exit_status") is not None:
        return f"Task failed with exit code {task['exit_status']}."
    return "Task failed without a recorded error message."


def _given(params, key):
    """A value is given unless it is missing, null, or false (the config file's original semantics)."""
    return params.get(key) is not None and params.get(key) is not False


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _template_record(id=None, project_id=None, name=None, template=None, review_section_ids=None, color=None,
                     work_dir=None, agent_args=None, harness=None):
    name = str(name or "").strip()
    if not name:
        raise HiveError("Template name cannot be empty")
    if not str(template or "").strip():
        raise HiveError(f"Template '{name}' has no prompt")
    return {
        "id": str(id or uuid.uuid4()),
        "project_id": str(project_id or ""),
        "name": name,
        "template": template,
        "review_section_ids": [str(i) for i in review_section_ids or []],
        "color": color,
        "work_dir": work_dir,
        "agent_args": normalize_args(agent_args),
        "harness": harness,
    }


def _validate_template_names(templates):
    """Template names are unique within a project; the CLI finds templates by name."""
    seen = set()
    for template in templates:
        key = (template["project_id"], template["name"].lower())
        if key in seen:
            raise HiveError(f"Template name '{template['name']}' is already taken")
        seen.add(key)


def _normalize_harness(harness):
    harness_type = str(harness.get("type") or "")
    if harness_type not in [t["id"] for t in HARNESS_TYPES]:
        harness_type = HARNESS_TYPES[0]["id"]
    sessions_dir = str(harness.get("sessions_dir") or "").strip()
    return {
        "id": str(harness.get("id") or uuid.uuid4()),
        "name": str(harness.get("name") or ""),
        "icon": str(harness.get("icon") or ""),
        "color": harness.get("color"),
        "type": harness_type,
        "command": str(harness.get("command") or ""),
        "args": normalize_args(harness.get("args")),
        "sessions_dir": sessions_dir or None,
        "only": pj.normalize_only(harness.get("only")),
    }


def _validate_harness_names(harnesses):
    names = [h["name"].strip().lower() for h in harnesses]
    if len(set(names)) != len(names):
        raise HiveError("Harness names must be unique")


def _review_section_record(section, projects):
    project_id = str(section.get("project_id") or "")
    if project_id not in projects:
        raise HiveError(f"Review section '{section.get('name') or ''}' belongs to an unknown project")
    return {"id": str(section["id"]), "project_id": project_id, "name": str(section.get("name") or ""),
            "prompt": section.get("prompt")}
