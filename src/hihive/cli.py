"""Command-line client for hi-hive, plus the `serve` and `service` commands."""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as package_version
from urllib.parse import quote, urlencode, urlsplit

from . import paths

FINISHED = ("completed", "failed", "cancelled")
NO_HARNESS = ("No harness is set up yet. Install Claude Code 2.1.283+ or Pi 0.87.1+, "
              "then run the installer again, or add a harness in Settings > All projects > Harnesses.")

USAGE = """\
CLI for hi-hive - parallel task runner using Claude Code and Pi harnesses.

Usage: hihive [--project NAME] <command> [args]
       hihive --version

Projects:
  Tasks, templates, review sections, and working directories belong to projects.
  Harnesses are configured globally and can be made available to selected projects.
  Commands that list or start tasks use the project named by --project (name or ID),
  else $HIVE_PROJECT, else the only project. With several
  projects and none named, project-scoped commands stop and list the available names.
  Project names are matched case-insensitively. A project ID may be shortened to a unique
  prefix of at least 4 characters; an exact name or ID wins over a prefix.
  Commands that take a task ID find it in any project, unless a project is named
  (--project or $HIVE_PROJECT): then the task must belong to it.

Commands:
  projects                    List projects (* = project other commands would use)
  status                      Show queue/running/completed counts
  templates                   List prompt templates
  harnesses                   List harnesses available in the project (* = default)
  start <template> [input] [--harness NAME]
                                      Start a task from a saved template (see `templates`).
                                      <input> replaces %{input} in the template body and is used as the label.
                                      Omit <input> for templates with no %{input}; the label falls back to the template name.
  start -p "prompt" [--label LABEL] [--harness NAME]
                                      Start a one-off task with a raw prompt (no template).
                                      Label defaults to the first 60 chars of the prompt.
  import <session-id> --harness NAME [--label LABEL]
                              Import an existing native harness session into Results.
  queue                       List queued and running tasks
  results [--limit N] [--all] [--no-pager]
                              List completed tasks (forks indented under their parent).
                              Default: last 20 root tasks (with all their forks). Use --limit N
                              or --all to override. Output is paged through $PAGER (or less)
                              when stdout is a TTY; use --no-pager or pipe to grep/head to filter.
  show <id> [--review|--followup|--full] [--no-header]
                              Show task content. Default: agent output only.
                              --review   show review sections instead
                              --followup show follow-up messages instead
                              --full     show output + reviews + follow-ups
                              --no-header suppress the label/status header
  wait <id>                   Wait for task to complete
  label <id> "new label"      Change a task's label
  followup <id> "message" [--confirm-session-ownership]
                                   Send a message to the agent (steers the agent if it is already working)
  followup <id> "message" --wait   Send and wait for the conversation run to finish; print its latest answer
  followup <id> --cancel           Stop the active conversation run, including all pending messages
  cancel <id>                 Cancel a task
  retry <id> [--harness NAME] Retry a task (refused if the task has forks)
  fork <id> "prompt" [--label LABEL] [--confirm-session-ownership]
                              Fork a finished task; resumes its session and branches with new prompt
  open                        Open the web UI and log this browser in

Server commands:
  serve [--port N] [--shell-env]
                              Run the server in the foreground (default port: $PORT or 4567).
                              --shell-env loads your login shell's environment first (used by the service)
  service install [--port N]  Install the login service (launchd on macOS, systemd on Linux)
  service uninstall           Stop and remove the login service
  service status              Show service status
  service restart             Restart the installed service

Notes:
  prompt — the message sent to the agent
  label  — short identifier shown in task lists; auto-derived from prompt if not given

Task IDs can be shortened to a unique prefix (e.g. "4f63e134" instead of full UUID).
Server data: ~/.hihive (override with HIVE_HOME); the CLI connects to the server that uses it.
Agents started by Hive get $HIVE_TOKEN, which limits them to their task's project; when it is
set, the CLI uses only that token and never reads ~/.hihive/token.
"""


class Exit(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def puts(text=""):
    """Print like Ruby's puts: no extra newline when the text already ends with one."""
    text = "" if text is None else str(text)
    sys.stdout.write(text if text.endswith("\n") else text + "\n")


class HiHiveCLI:
    def __init__(self):
        self.base_url = paths.server_url()
        # An agent's token must never fall back to the full token in ~/.hihive/token.
        self.agent_token = "HIVE_TOKEN" in os.environ
        self.token = os.environ["HIVE_TOKEN"].strip() if self.agent_token else paths.read_token()
        self.project_arg = None
        self._all_tasks = None
        self._projects = None
        self._project = None

    def run(self, argv):
        argv = list(argv)
        if argv == ["--version"]:
            puts(version_text())
            return
        self.project_arg = self.take_named_arg(argv, "--project")
        command = argv.pop(0) if argv else None
        if self.agent_token and not self.token and command not in ("help", "-h", "--help", None):
            self.die("HIVE_TOKEN is set but empty. Unset it to use the token in ~/.hihive/token.")
        commands = {
            "projects": lambda: self.cmd_projects(),
            "status": lambda: self.cmd_status(),
            "templates": lambda: self.cmd_templates(),
            "harnesses": lambda: self.cmd_harnesses(),
            "start": lambda: self.cmd_start(argv),
            "import": lambda: self.cmd_import(argv),
            "queue": lambda: self.cmd_queue(),
            "results": lambda: self.cmd_results(argv),
            "show": lambda: self.cmd_show(argv),
            "wait": lambda: self.cmd_wait(argv),
            "label": lambda: self.cmd_label(argv),
            "followup": lambda: self.cmd_followup(argv),
            "cancel": lambda: self.cmd_cancel(argv),
            "retry": lambda: self.cmd_retry(argv),
            "fork": lambda: self.cmd_fork(argv),
            "open": lambda: self.cmd_open(),
        }
        if command in commands:
            commands[command]()
        elif command in ("help", "-h", "--help", None):
            sys.stdout.write(USAGE)
        else:
            self.die(f"Unknown command: {command}. Run 'hihive help' for usage.")

    # --- Commands ---

    def cmd_projects(self):
        """* marks the project other commands would use now, however it was chosen."""
        projects = self.projects()
        wanted = self.project_wanted()
        current, problem = self.find_project(wanted) if wanted else (projects[0] if len(projects) == 1 else None, None)
        for project in projects:
            marker = "*" if current and project["id"] == current["id"] else " "
            puts(f"{marker} {project['name']}  ({project['id'][:8]})  {project.get('work_dir') or ''}".rstrip())
        if problem:
            sys.stderr.write(problem + "\n")

    def cmd_status(self):
        state = self.project_state()
        stats = state["stats"]
        puts(f"Project:   {self.project()['name']}")
        puts(f"Paused:    {'yes' if state.get('paused') else 'no'}")
        puts(f"Queued:    {stats['queued']}")
        puts(f"Running:   {stats['running']}")
        puts(f"Completed: {stats['completed']}")
        puts(f"Failed:    {stats['failed']}")
        puts(f"Cancelled: {stats['cancelled']}")

    def cmd_templates(self):
        templates = self.project()["templates"]
        if not templates:
            puts("No templates configured.")
            return
        for template in templates:
            puts(f"\033[1m{template['name']}\033[0m")
            for line in template["template"].splitlines(keepends=True):
                puts(f"  {line}")
            puts()

    def cmd_harnesses(self):
        project = self.project()
        harnesses = project["harnesses"]
        if not harnesses:
            puts(f"No harnesses are available in project '{project['name']}'.")
            return
        for harness in harnesses:
            marker = "*" if harness["id"] == project.get("default_harness") else " "
            puts(f"{marker} {harness['name']}")

    def cmd_start(self, args):
        label = self.take_named_arg(args, "--label")
        project = self.project()
        if not project["harnesses"]:
            self.die(NO_HARNESS)
        harness = self.resolve_harness_name(self.take_named_arg(args, "--harness"), project["harnesses"])
        if "-p" in args:
            index = args.index("-p")
            if index + 1 >= len(args):
                self.die("Missing prompt after -p")
            body = {"prompt": args[index + 1], "label": label, "project_id": project["id"]}
            if harness:
                body["harness"] = harness
            puts(self.post("/api/tasks", body)["id"])
            return
        if label:
            self.die("--label is only supported with -p (template tasks derive label from input or template name)")
        if not args:
            self.die("Usage: hihive start <template> [input]  (run `hihive templates` to list available templates)")
        template = self.match_template(args.pop(0), project)
        body = {"template_id": template["id"], "inputs": [" ".join(args)]}
        if harness:
            body["harness"] = harness
        puts(self.post("/api/tasks/batch", body)["ids"][0])

    def cmd_import(self, args):
        if self.agent_token:
            self.die('Session import is available only to the user, not agent tokens.')
        label = self.take_named_arg(args, '--label')
        harness_name = self.take_named_arg(args, '--harness')
        if len(args) != 1 or not harness_name:
            self.die('Usage: hihive import <session-id> --harness NAME [--label LABEL]')
        project = self.project()
        harness = self.resolve_harness_name(harness_name, project['harnesses'])
        body = {'project_id': project['id'], 'harness_id': harness, 'session_id': args[0]}
        preview = self.post('/api/session-import/preview', body)
        token = preview.get('preview_token')
        if not isinstance(token, str) or not token:
            self.die('Could not verify the session. Search again.')
        result = self.post('/api/session-import', {**body, 'preview_token': token, 'label': label})
        puts(result['id'])
        sys.stderr.write('Close this session in other terminals before sending a follow-up.\n')

    def cmd_queue(self):
        state = self.project_state()
        tasks = (state.get("queue") or []) + (state.get("workers") or [])
        puts(f"Project: {self.project()['name']}")
        if not tasks:
            puts("No queued or running tasks.")
            return
        for task in tasks:
            elapsed = ""
            if task.get("started_at"):
                seconds = int(time.time() - datetime.fromisoformat(task["started_at"]).timestamp())
                elapsed = f"  {format_duration(seconds)}"
            puts(f"{short_id(task['id'])}  {pad(task['status'], 10)}  "
                 f"{truncate(self.task_display_label(task), 50)}{elapsed}")

    def cmd_results(self, args):
        no_pager = _remove(args, "--no-pager")
        show_all = _remove(args, "--all")
        limit_raw = self.take_named_arg(args, "--limit")
        limit = None if show_all else (_to_int(limit_raw) if limit_raw else 20)
        if limit is not None and limit <= 0:
            limit = None

        tasks = self.project_state().get("results") or []
        heading = f"Project: {self.project()['name']}"
        if not tasks:
            puts(heading)
            puts("No results.")
            return
        by_id = {task["id"]: task for task in tasks}
        children = {}
        roots = []
        for task in tasks:
            if task.get("parent_task_id") and task["parent_task_id"] in by_id:
                children.setdefault(task["parent_task_id"], []).append(task)
            else:
                roots.append(task)
        for group in children.values():
            group.sort(key=lambda t: t.get("created_at") or "")
        roots.sort(key=lambda t: t.get("completed_at") or t.get("created_at") or "", reverse=True)
        total_roots = len(roots)
        if limit is not None:
            roots = roots[:limit]

        def print_tree(task, depth):
            stamp = task.get("completed_at") or task.get("created_at")
            when = format_time(stamp) if stamp else ""
            summary = f"  {truncate(task['summary'], 60)}" if task.get("summary") else ""
            indent = ("   " * (depth - 1)) + "└─ " if depth > 0 else ""
            puts(f"{short_id(task['id'])}  {pad(task['status'], 10)}  {when}  {indent}"
                 f"{truncate(self.task_display_label(task), 40)}{summary}")
            for child in children.get(task["id"], []):
                print_tree(child, depth + 1)

        def render():
            puts(heading)
            for root in roots:
                print_tree(root, 0)
            if limit is not None and total_roots > limit:
                puts(f"... showing {limit} of {total_roots} root tasks. Use --all or --limit N to see more.")

        if not no_pager and sys.stdout.isatty():
            with_pager(render)
        else:
            render()

    def cmd_show(self, args):
        mode = "output"
        no_header = False
        remaining = []
        for arg in args:
            if arg in ("--review", "--followup", "--full"):
                mode = arg[2:]
            elif arg == "--no-header":
                no_header = True
            elif arg.startswith("-"):
                self.die(f"Unknown flag: {arg}")
            else:
                remaining.append(arg)
        if not remaining:
            self.die("Usage: hihive show <id> [--review|--followup|--full] [--no-header]")
        task_id = self.resolve_task_id(remaining[0])
        task = self.get(f"/api/tasks/{quote(task_id, safe='')}")
        messages = task.get("messages") or []

        if not no_header:
            puts(f"{self.task_display_label(task)}  [{task['status']}]")
            if task.get("parent_task_id"):
                parent = self.lookup_task(task["parent_task_id"])
                description = self.task_display_label(parent) if parent else "(parent missing)"
                puts(f"Forked from: {short_id(task['parent_task_id'])}  {description}")
            if task["status"] not in FINISHED:
                puts("output so far:")
            puts()

        if mode == "output":
            answers = [m["content"] for m in messages
                       if m.get("role") == "assistant" and m.get("kind") in ("initial", "continue")]
            puts("\n\n".join(answers) if answers else "(no output)")
        elif mode == "review":
            history = [m for m in messages if m.get("kind") == "review" and m.get("delivery") != "scheduled"]
            print_messages(history) if history else puts("(no review)")
        elif mode == "followup":
            history = [m for m in messages if m.get("delivery") != "scheduled"]
            first = next((i for i, m in enumerate(history) if m.get("role") == "user"
                          and m.get("kind") in ("follow_up", "steering", "continue")), None)
            print_messages(history[first:]) if first is not None else puts("(no follow-up messages)")
        else:
            print_messages([m for m in messages if m.get("delivery") != "scheduled"])

    def cmd_wait(self, args):
        if not args:
            self.die("Usage: hihive wait <id>")
        task_id = self.resolve_task_id(args[0])
        try:
            task = self.get(f"/api/tasks/{quote(task_id, safe='')}")
            while task["status"] not in FINISHED:
                self.sleep(2)
                task = self.get(f"/api/tasks/{quote(task_id, safe='')}")
        except KeyboardInterrupt:
            sys.stdout.write("\r\033[K")
            raise Exit(130) from None
        raise Exit(0 if task["status"] == "completed" else 1)

    def cmd_label(self, args):
        if len(args) < 2:
            self.die('Usage: hihive label <id> "new label"')
        task_id = self.resolve_task_id(args.pop(0))
        result = self.post(f"/api/tasks/{quote(task_id, safe='')}/label", {"label": " ".join(args)})
        puts(f"Changed label for {short_id(task_id)}: {result['label']}")

    def cmd_followup(self, args):
        if _remove(args, "--cancel"):
            if not args:
                self.die("Usage: hihive followup <id> --cancel")
            task_id = self.resolve_task_id(args[0])
            self.post(f"/api/tasks/{quote(task_id, safe='')}/chat/cancel", {"msg_id": None})
            puts(f"Requested stop of the active conversation run for {short_id(task_id)}")
            return

        wait = _remove(args, "--wait")
        confirmed = _remove(args, "--confirm-session-ownership")
        if len(args) < 2:
            self.die('Usage: hihive followup <id> "message" [--wait]')
        task_id = self.resolve_task_id(args.pop(0))
        message = " ".join(args)
        path = f"/api/tasks/{quote(task_id, safe='')}"
        task = self.get(path)
        prior_ids = {m.get("id") for m in task.get("messages") or []}
        self.require_session_ownership(task, confirmed)
        body = {"message": message}
        if confirmed:
            body['imported_session_confirmed'] = True
        receipt = self.post(f"{path}/chat", body)["message"]
        if not wait:
            puts(f"Message accepted ({receipt['id']}). Shared output will appear in: "
                 f"hihive show {short_id(task_id)} --followup")
            return

        # Multiple inputs can share an answer. Wait for settlement, not a larger
        # history count (which could be another user message or a warning).
        while True:
            self.sleep(2)
            task = self.get(path)
            history = task.get("messages") or []
            entry = next((m for m in history if m.get("id") == receipt["id"]), None)
            if entry and entry.get("delivery") in ("rejected", "failed", "unknown"):
                self.die(f"Message {entry['delivery']}: {entry.get('error') or 'See task diagnostics'}")
            if task.get("status") in ("failed", "cancelled"):
                self.die(task.get("failure_details") or f"Task {task['status']}")
            if entry and entry.get("settled") and task.get("status") == "completed":
                final = next((m for m in reversed(history)
                              if m.get("role") == "assistant" and m.get("id") not in prior_ids), None)
                puts(final["content"] if final else "Run settled without a separate assistant answer for this input.")
                return

    def cmd_cancel(self, args):
        if not args:
            self.die("Usage: hihive cancel <id>")
        task_id = self.resolve_task_id(args[0])
        self.post(f"/api/tasks/{quote(task_id, safe='')}/cancel")
        puts(f"Cancelled {short_id(task_id)}")

    def cmd_retry(self, args):
        harness_name = self.take_named_arg(args, "--harness")
        if not args:
            self.die("Usage: hihive retry <id> [--harness NAME]")
        task_id = self.resolve_task_id(args[0])
        task_project = next((p for p in self.projects() if p["id"] == self.lookup_task(task_id).get("project_id")), None)
        harness = self.resolve_harness_name(harness_name, task_project["harnesses"] if task_project else [])
        self.post(f"/api/tasks/{quote(task_id, safe='')}/retry", {"harness": harness} if harness else {})
        puts(f"Retried {short_id(task_id)}")

    def cmd_fork(self, args):
        confirmed = _remove(args, "--confirm-session-ownership")
        label = self.take_named_arg(args, "--label")
        if len(args) < 2:
            self.die('Usage: hihive fork <id> "prompt" [--label LABEL]')
        task_id = self.resolve_task_id(args.pop(0))
        task = self.lookup_task(task_id)
        self.require_session_ownership(task, confirmed)
        body = {"prompt": " ".join(args), "label": label}
        if confirmed:
            body['imported_session_confirmed'] = True
        result = self.post(f"/api/tasks/{quote(task_id, safe='')}/fork", body)
        puts(result["id"])

    def require_session_ownership(self, task, confirmed):
        if task and task.get('imported_session') and not task.get('imported_session_confirmed'):
            if self.agent_token:
                self.die('The user must confirm ownership of this imported session before agents can use it.')
            if not confirmed:
                self.die('Hive will keep using this session. Close it in other terminals and keep it closed while the task is in Hive. '
                         'To confirm, repeat with --confirm-session-ownership.')

    def cmd_open(self):
        """Check health, then open the UI with a login link."""
        if self.agent_token:
            self.die("`hihive open` needs the full token; it does not work with HIVE_TOKEN.")
        if not self.token or not self.probe_health():
            self.die("hi-hive is not running. Start it:\n"
                     "  hihive serve                 (keeps that terminal busy)\n"
                     "  or in the background:  hihive service install")
        # A fragment is never sent to the server; the login page removes it from the address bar.
        url = f"{self.base_url.rstrip('/')}/login#{urlencode({'token': self.token})}"
        import webbrowser

        headless = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))
        if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            headless = True
        if headless:
            port = urlsplit(self.base_url).port or paths.DEFAULT_PORT
            puts("hi-hive is running. On your computer, open an SSH tunnel:")
            puts(f"  ssh -N -L {port}:127.0.0.1:{port} <user>@<hi-hive-host>")
            puts("Then open this login URL on your computer:")
            puts(url)
            puts("This URL grants full hi-hive access. Keep it private and do not paste it into logs or issues.")
        elif webbrowser.open(url):
            puts(f"Opened {self.base_url} in your browser (logged in).")
        else:
            self.die("hi-hive is running, but the browser could not be opened. Run `hihive open` again, or open it from another desktop terminal.")

    def probe_health(self):
        request = urllib.request.Request(self.base_url.rstrip("/") + "/api/config")
        request.add_header("Origin", self.base_url)
        request.add_header("Authorization", f"Bearer {self.token or ''}")
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status == 200
        except (urllib.error.URLError, urllib.error.HTTPError, ConnectionError, TimeoutError):
            return False

    # --- HTTP ---

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, {} if body is None else body)

    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base_url.rstrip("/") + path, data=data, method=method)
        request.add_header("Origin", self.base_url)
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as failure:
            text = failure.read().decode("utf-8", "replace")
            try:
                message = json.loads(text)["error"]
            except (ValueError, KeyError, TypeError):
                message = text
            self.die(f"API error ({failure.code}): {message}")
        except (urllib.error.URLError, ConnectionError) as failure:
            reason = getattr(failure, "reason", failure)
            if isinstance(reason, ConnectionRefusedError) or isinstance(failure, ConnectionRefusedError):
                self.die(f"Cannot connect to hi-hive at {self.base_url}. Is the server running?")
            self.die(f"Cannot reach hi-hive at {self.base_url}: {reason}")

    def sleep(self, seconds):
        time.sleep(seconds)

    # --- Resolution ---

    def take_named_arg(self, args, name):
        """Remove "--name VALUE" from args and return VALUE, or None if absent."""
        if name not in args:
            return None
        index = args.index(name)
        value = args[index + 1] if index + 1 < len(args) else None
        if value is None or value.startswith("-"):
            self.die(f"Missing value after {name}")
        del args[index:index + 2]
        return value

    def resolve_task_id(self, prefix):
        """A named project (--project or $HIVE_PROJECT) limits the task to that project."""
        matches = [task for task in self.all_tasks() if task["id"].startswith(prefix)]
        if not matches:
            self.die(f"No task matching '{prefix}'")
        if self.project_named():
            project = self.project()
            in_project = [t for t in matches if t.get("project_id") == project["id"]]
            if not in_project:
                names = {p["id"]: p["name"] for p in self.projects()}
                found = ", ".join(sorted({f"'{names.get(t.get('project_id'), t.get('project_id'))}'" for t in matches}))
                self.die(f"Task '{prefix}' is in project {found}, not '{project['name']}'. "
                         "Name that project, or leave out --project and HIVE_PROJECT.")
            matches = in_project
        if len(matches) > 1:
            listing = "\n".join(f"  {short_id(t['id'])}  {t.get('label')}" for t in matches)
            self.die(f"Ambiguous ID '{prefix}', matches:\n{listing}")
        return matches[0]["id"]

    def lookup_task(self, task_id):
        return next((task for task in self.all_tasks() if task["id"] == task_id), None)

    def all_tasks(self):
        if self._all_tasks is None:
            state = self.get("/api/state")
            self._all_tasks = (state.get("queue") or []) + (state.get("workers") or []) + (state.get("results") or [])
        return self._all_tasks

    def template_names(self):
        return {t["id"]: t["name"] for p in self.projects() for t in p["templates"]}

    # --- Projects ---

    def projects(self):
        if self._projects is None:
            self._projects = self.get("/api/projects")["projects"]
        return self._projects

    def project_wanted(self):
        return self.project_arg or os.environ.get("HIVE_PROJECT")

    def project_named(self):
        return bool(self.project_wanted())

    def find_project(self, wanted):
        """(project, None), or (None, why not): an exact name or ID wins over an ID prefix of 4+ characters."""
        projects = self.projects()
        exact = [p for p in projects if wanted.lower() == p["name"].lower() or wanted == p["id"]]
        matches = exact or [p for p in projects if len(wanted) >= 4 and p["id"].startswith(wanted)]
        if len(matches) == 1:
            return matches[0], None
        if matches:
            listing = "\n".join(f"  {p['name']}  ({p['id'][:8]})" for p in matches)
            return None, (f"Ambiguous project '{wanted}', matches:\n{listing}\n"
                          "Use a longer ID prefix or the project name.")
        listing = "\n".join(f"  {p['name']}" for p in projects)
        return None, f"No project matching '{wanted}'. Available:\n{listing}"

    def project(self):
        """The project from --project, else $HIVE_PROJECT, else the only project."""
        if self._project is None:
            projects = self.projects()
            wanted = self.project_wanted()
            listing = "\n".join(f"  {p['name']}" for p in projects)
            if wanted:
                self._project, problem = self.find_project(wanted)
                if problem:
                    self.die(problem)
            elif len(projects) == 1:
                self._project = projects[0]
            else:
                self.die(f"Name a project with --project NAME or HIVE_PROJECT. Available:\n{listing}")
        return self._project

    def project_state(self):
        return self.get(f"/api/state?project={quote(self.project()['id'], safe='')}")

    def task_display_label(self, task):
        raw = re.sub(r"\s+", " ", task.get("label") or "").strip()
        template = self.template_names().get(task["template_id"]) if task.get("template_id") else None
        return f"[{template}] {raw}" if template else raw

    def match_template(self, name, project):
        templates = project["templates"]
        matches = [t for t in templates if name.lower() in t["name"].lower()]
        if not matches:
            listing = "\n".join(f"  {t['name']}" for t in templates)
            self.die(f"No template matching '{name}'. Available:\n{listing}")
        if len(matches) > 1:
            listing = "\n".join(f"  {t['name']}" for t in matches)
            self.die(f"Ambiguous template '{name}', matches:\n{listing}")
        return matches[0]

    def resolve_harness_name(self, name, harnesses):
        if not name:
            return None
        match = next((h for h in harnesses if str(h.get("name") or "").lower() == name.lower()), None)
        if match:
            return match["id"]
        listing = "\n".join(f"  {h['name']}" for h in harnesses)
        self.die(f"No harness named '{name}' in this project. Available:\n{listing}")

    def die(self, message):
        sys.stderr.write(message if message.endswith("\n") else message + "\n")
        raise Exit(1)


# --- Formatting ---

def version_text():
    try:
        installed = package_version("hi-hive")
    except PackageNotFoundError:
        installed = "0.2.0"
    source = None
    try:
        record = json.loads(paths.installer_record_file().read_text(encoding="utf-8"))
        commit = str(record.get("commit") or "")
        if re.fullmatch(r"[0-9a-f]{40}", commit):
            source = f"main@{commit}"
    except (OSError, ValueError, TypeError):
        pass
    return f"hi-hive {installed}" + (f" ({source})" if source else "")


def print_messages(messages):
    for message in messages:
        if message.get("kind") == "review":
            label = "review question" if message.get("role") == "user" else "review answer"
        else:
            label = message.get("role")
        puts(f"[{label}] {message.get('content')}")
        puts()


def with_pager(render):
    """Pipe stdout through $PAGER (or less -FRX). Falls through if the pager is missing."""
    pager = os.environ.get("PAGER") or "less"
    env = dict(os.environ)
    if pager.startswith("less"):
        env["LESS"] = "FRX"
    try:
        process = subprocess.Popen(pager, shell=True, stdin=subprocess.PIPE, env=env, text=True)
    except OSError:
        render()
        return
    original = sys.stdout
    sys.stdout = process.stdin
    try:
        render()
    except BrokenPipeError:
        pass  # The user quit the pager.
    finally:
        sys.stdout = original
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        process.wait()


def short_id(task_id):
    return task_id[:8]


def pad(text, width):
    return str("" if text is None else text).ljust(width)


def format_time(stamp):
    moment = datetime.fromisoformat(stamp).astimezone()
    today = datetime.now().astimezone()
    if moment.date() == today.date():
        return moment.strftime("%H:%M")
    if moment.year == today.year:
        return moment.strftime("%b %d %H:%M")
    return moment.strftime("%Y-%m-%d %H:%M")


def truncate(text, width):
    if not text:
        return ""
    return f"{text[:width - 3]}..." if len(text) > width else text


def format_duration(seconds):
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _remove(args, flag):
    if flag in args:
        args.remove(flag)
        return True
    return False


def _to_int(value):
    match = re.match(r"\s*[-+]?\d+", value)
    return int(match.group(0)) if match else 0


# --- Server commands ---

def _port_option(args):
    if "--port" not in args:
        return None
    index = args.index("--port")
    value = args[index + 1] if index + 1 < len(args) else ""
    if not value.isdigit():
        raise SystemExit(f"Invalid port: {value}")
    del args[index:index + 2]
    return int(value)


def serve(args):
    shell_env = _remove(args, "--shell-env")
    port = _port_option(args) or int(os.environ.get("PORT") or paths.DEFAULT_PORT)
    if args:
        raise SystemExit(f"Unknown argument: {args[0]}")
    import uvicorn

    from .pool import PoolManager
    from .server import create_app

    if shell_env:
        from .shell_env import adopt_login_shell_env

        print(adopt_login_shell_env(), flush=True)
    try:
        lock = paths.lock_home(port)  # held open until the process exits
    except SystemExit:
        running_port = urlsplit(paths.server_url()).port or paths.DEFAULT_PORT
        raise SystemExit(f"hi-hive is already running on port {running_port}. Open it with:  hihive open\n"
                         "To run a second, separate hi-hive, set HIVE_HOME to another folder.") from None
    config = paths.config_file()
    if not config.exists():
        setup_coding_agents(["--fresh", "--plain"])
    pool = PoolManager(config_file=config, persist_file=paths.state_file())
    token = paths.ensure_token()
    print(f"hi-hive is running on http://localhost:{port}", flush=True)
    print("Keep this terminal open. Closing it or pressing Ctrl-C stops hi-hive.", flush=True)
    print("To open it, run in another terminal:  hihive open", flush=True)
    print("To run it in the background instead: press Ctrl-C, then run  hihive service install", flush=True)
    uvicorn.run(create_app(pool, token), host="127.0.0.1", port=port, log_level="warning")


def _coding_agent_line(agent, result):
    configured = next((item for item in result["configured"] if item.get("type") == agent["id"]), None)
    default = bool(result["fresh"] and configured and configured.get("id") == result.get("default_harness"))
    added = agent["id"] in result["added"]
    command = str(configured.get("command") or "") if configured else ""
    detected_path = str(agent.get("path") or "")
    custom = bool(configured and command not in (agent["command"], detected_path))
    if custom:
        external = "not checked (custom command)"
        inside = "already added" + (" (default)" if default else "")
    else:
        external = (f"{agent.get('version')}  {detected_path}" if agent["status"] in ("usable", "too_old")
                    else "not found" if agent["status"] == "not_found"
                    else "not checked (login-shell environment unavailable)" if agent.get("environment_error")
                    else f"found at {detected_path}; version check failed")
        if configured:
            if agent["status"] == "usable":
                inside = "already added" + (" (default)" if default else "")
            elif agent["status"] == "too_old":
                inside = f"already added: its tasks fail until you update it (needs {agent['minimum']}+)"
            else:
                inside = "already added: its tasks fail until you install it"
        elif added:
            inside = "added" + (" (default)" if default else "")
        elif agent["status"] == "too_old":
            inside = f"not added: too old (needs {agent['minimum']}+)"
        else:
            inside = "not added"
    return f"  {agent['name']:<14} {external:<52} {inside}"


def setup_coding_agents(args):
    from .setup import configure_detected_agents, unconfigured_usable

    fresh = _remove(args, "--fresh")
    offer = _remove(args, "--offer")
    plain = _remove(args, "--plain")
    if args or fresh == offer:
        raise SystemExit("Usage: hihive setup-harnesses (--fresh | --offer) [--plain]")
    try:
        result = configure_detected_agents("fresh") if fresh else None
        preview = configure_detected_agents("existing", accepted=[]) if not fresh else None
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
    if not fresh:
        candidates = unconfigured_usable(preview)
        accepted = []
        for agent in candidates:
            take = False
            if not plain:
                try:
                    with open("/dev/tty", "r+", encoding="utf-8", buffering=1) as terminal:
                        terminal.write(f"Add {agent['name']} {agent['version']} ({agent['path']}) to hi-hive? "
                                       "Your current harness settings stay as they are. [Y/n] ")
                        answer = terminal.readline().strip().lower()
                    take = answer in ("", "y", "yes")
                except OSError:
                    pass
            if take:
                accepted.append(agent["id"])
        try:
            result = configure_detected_agents("existing", accepted=accepted)
        except (OSError, ValueError) as error:
            raise SystemExit(str(error)) from None
    puts("Harnesses       On this computer                                     In hi-hive")
    for agent in result["detected"]:
        puts(_coding_agent_line(agent, result))
    puts("  hi-hive does not install these or check that you are logged in.")
    too_old = [agent for agent in result["detected"]
               if agent["status"] == "too_old" and agent["id"] not in result["configured_types"]]
    if too_old:
        names = " and ".join("Claude Code (claude update)" if agent["id"] == "claude-code" else "Pi"
                             for agent in too_old)
        puts(f"  To add {'it' if len(too_old) == 1 else 'them'}: update {names}, then run this installer again.")
    environment_error = next((agent.get("environment_error") for agent in result["detected"]
                              if agent.get("environment_error")), None)
    if environment_error:
        puts(f"  Harnesses could not be checked because the login-shell environment failed: {environment_error}")
    if not result["configured_types"]:
        usable_declined = [agent["name"] for agent in result["detected"] if agent["status"] == "usable"]
        if environment_error:
            puts("  hi-hive has no harness yet, so it cannot run tasks. Fix the login shell, then run this installer again.")
        elif usable_declined:
            puts("  hi-hive has no harness yet, so it cannot run tasks. "
                 f"Run this installer again to add a harness for {' or '.join(usable_declined)}.")
        else:
            puts("  hi-hive has no harness yet, so it cannot run tasks. "
                 "Install Claude Code 2.1.283+ or Pi 0.87.1+, then run this installer again.")
    if plain and not fresh:
        for agent in unconfigured_usable(result):
            puts(f"  {agent['name']} {agent['version']} found, not added "
                 "(no terminal to ask; run the installer in a terminal to add it).")
    return result


def health():
    client = HiHiveCLI()
    if client.token and client.probe_health():
        puts(f"hi-hive is healthy at {client.base_url}")
        return
    raise SystemExit(f"hi-hive is not running at {client.base_url}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] == "serve":
            serve(argv[1:])
        elif argv and argv[0] == "health":
            if len(argv) != 1:
                raise SystemExit("Usage: hihive health")
            health()
        elif argv and argv[0] == "setup-harnesses":
            setup_coding_agents(argv[1:])
        elif argv and argv[0] == "service":
            from . import service

            action = argv[1] if len(argv) > 1 else None
            if action == "install":
                service.install(_port_option(argv[2:]))
            elif action == "uninstall":
                service.uninstall()
            elif action == "status":
                service.status()
            elif action == "restart":
                service.restart()
            elif action == "available":
                if not service.available():
                    raise SystemExit(1)
            elif action == "installed":
                if len(argv) != 2:
                    raise SystemExit("Usage: hihive service installed")
                if not service.installed():
                    raise SystemExit(3)
            else:
                raise SystemExit("Usage: hihive service install [--port N] | uninstall | status | restart")
        else:
            HiHiveCLI().run(argv)
    except Exit as done:
        raise SystemExit(done.code)
    except BrokenPipeError:
        _silence_broken_stdout()
        raise SystemExit(0)
    finally:
        # A small output can stay buffered until interpreter shutdown, where an
        # EPIPE would otherwise produce an unhandled flush error and status 120.
        pending = sys.exception()
        try:
            sys.stdout.flush()
        except BrokenPipeError:
            _silence_broken_stdout()
            if pending is None:
                raise SystemExit(0)


def _silence_broken_stdout():
    # The buffer may still contain data; redirect the descriptor before Python
    # flushes it again during shutdown. Do not suppress other output errors.
    fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(fd, sys.stdout.fileno())
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
