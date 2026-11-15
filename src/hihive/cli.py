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
from urllib.parse import quote, urlencode

from . import paths

FINISHED = ("completed", "failed", "cancelled")

USAGE = """\
CLI for hi-hive - parallel task runner for coding agents (Claude Code, Pi).

Usage: hihive [--project NAME] <command> [args]

Projects:
  Tasks, templates, and harnesses belong to a project. Commands that list or start
  tasks use the project named by --project (name or ID), else $HIVE_PROJECT, else the
  only project. With several projects and none named, the command lists them and stops.
  Commands that take a task ID work in any project.

Commands:
  projects                    List projects
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
  followup <id> "message"          Send a message to the agent (steers the agent if it is already working)
  followup <id> "message" --wait   Send and wait for the conversation run to finish; print its latest answer
  followup <id> --cancel           Stop the active conversation run, including all pending messages
  cancel <id>                 Cancel a task
  retry <id> [--harness NAME] Retry a task (refused if the task has forks)
  fork <id> "prompt" [--label LABEL]  Fork a finished task; resumes its session and branches with new prompt
  open                        Open the web UI and log this browser in

Server commands:
  serve [--port N] [--shell-env]
                              Run the server in the foreground (default port: $PORT or 4567).
                              --shell-env loads your login shell's environment first (used by the service)
  service install [--port N]  Run the server in the background at login (macOS launchd)
  service uninstall           Stop the background server and remove it from login

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
            "queue": lambda: self.cmd_queue(),
            "results": lambda: self.cmd_results(argv),
            "show": lambda: self.cmd_show(argv),
            "wait": lambda: self.cmd_wait(argv),
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
        current = self.project_arg or os.environ.get("HIVE_PROJECT")
        for project in self.projects():
            marker = "*" if current and self.project_matches(project, current) else " "
            puts(f"{marker} {project['name']}  ({project['id'][:8]})  {project.get('work_dir') or ''}".rstrip())

    def cmd_status(self):
        state = self.project_state()
        stats = state["stats"]
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

    def cmd_queue(self):
        state = self.project_state()
        tasks = (state.get("queue") or []) + (state.get("workers") or [])
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
        if not tasks:
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

    def cmd_followup(self, args):
        if _remove(args, "--cancel"):
            if not args:
                self.die("Usage: hihive followup <id> --cancel")
            task_id = self.resolve_task_id(args[0])
            self.post(f"/api/tasks/{quote(task_id, safe='')}/chat/cancel", {"msg_id": None})
            puts(f"Requested stop of the active conversation run for {short_id(task_id)}")
            return

        wait = _remove(args, "--wait")
        if len(args) < 2:
            self.die('Usage: hihive followup <id> "message" [--wait]')
        task_id = self.resolve_task_id(args.pop(0))
        message = " ".join(args)
        path = f"/api/tasks/{quote(task_id, safe='')}"
        task = self.get(path)
        prior_ids = {m.get("id") for m in task.get("messages") or []}
        receipt = self.post(f"{path}/chat", {"message": message})["message"]
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
        label = self.take_named_arg(args, "--label")
        if len(args) < 2:
            self.die('Usage: hihive fork <id> "prompt" [--label LABEL]')
        task_id = self.resolve_task_id(args.pop(0))
        result = self.post(f"/api/tasks/{quote(task_id, safe='')}/fork", {"prompt": " ".join(args), "label": label})
        puts(result["id"])

    def cmd_open(self):
        """Open the UI with a login link, so this browser gets the login cookie."""
        if self.agent_token:
            self.die("`hihive open` needs the full token; it does not work with HIVE_TOKEN.")
        if not self.token:
            self.die(f"No Hive token at {paths.token_file()}. Start the server first (hihive serve).")
        # A fragment is never sent to the server; the login page removes it from the address bar.
        url = f"{self.base_url.rstrip('/')}/login#{urlencode({'token': self.token})}"
        import webbrowser

        if not webbrowser.open(url):
            puts(f"Open this link in your browser: {url}")

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
        matches = [task for task in self.all_tasks() if task["id"].startswith(prefix)]
        if not matches:
            self.die(f"No task matching '{prefix}'")
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

    @staticmethod
    def project_matches(project, name):
        return name.lower() == project["name"].lower() or project["id"] == name or (
            len(name) >= 4 and project["id"].startswith(name))

    def project(self):
        """The project from --project, else $HIVE_PROJECT, else the only project."""
        if self._project is None:
            projects = self.projects()
            wanted = self.project_arg or os.environ.get("HIVE_PROJECT")
            listing = "\n".join(f"  {p['name']}" for p in projects)
            if wanted:
                matches = [p for p in projects if self.project_matches(p, wanted)]
                if len(matches) != 1:
                    self.die(f"No project matching '{wanted}'. Available:\n{listing}")
                self._project = matches[0]
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
    lock = paths.lock_home(port)  # held open until the process exits
    config = paths.config_file()
    paths.ensure_config(config)
    pool = PoolManager(config_file=config, persist_file=paths.state_file())
    token = paths.ensure_token()
    print(f"Starting hi-hive on http://localhost:{port} (run `hihive open` to open the UI)", flush=True)
    uvicorn.run(create_app(pool, token), host="127.0.0.1", port=port, log_level="warning")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] == "serve":
            serve(argv[1:])
        elif argv and argv[0] == "service":
            from . import service

            action = argv[1] if len(argv) > 1 else None
            if action == "install":
                service.install(_port_option(argv[2:]))
            elif action == "uninstall":
                service.uninstall()
            else:
                raise SystemExit("Usage: hihive service install [--port N] | hihive service uninstall")
        else:
            HiHiveCLI().run(argv)
    except Exit as done:
        sys.exit(done.code)
    except BrokenPipeError:
        sys.exit(0)


if __name__ == "__main__":
    main()
