"""HTTP API and static UI."""

import contextlib
import hmac
import json
import os
import signal
import threading
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import HiveError
from .diagnostics import redact
from .paths import STATIC_DIR, hive_home

JSON_METHODS = {"POST", "PUT", "PATCH"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}
COOKIE = "hive_token"
COOKIE_MAX_AGE = 400 * 24 * 3600  # the longest lifetime browsers accept


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def error(status, message):
    return JSONResponse({"error": message}, status_code=status)


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.update(NO_CACHE)
        return response


class RequestGuard:
    """Validate Host on every request to block DNS rebinding, require a
    trusted browser origin for state-changing requests to block CSRF, and
    require the secret on every API request.

    Host and Origin checks alone do not stop local processes such as Docker
    containers, which can fake both headers (see docs/adr/0002-local-authentication.md).

    The secret is either the full token or an agent token that Hive gave an
    agent process; an agent token limits the request to one project
    (see docs/adr/0003-projects-and-scoped-tokens.md). The handlers read the
    limit from scope["hive_project"]: None means full access.
    """

    def __init__(self, app, pool, token):
        self.app = app
        self.pool = pool
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        host, port = _request_host(request)
        allowed = self.pool.allowed_hosts()
        response = None
        if host not in allowed:
            response = error(403, f"Blocked: untrusted Host header '{host}'")
        elif request.method in UNSAFE_METHODS and not _trusted_origin(request, allowed, port):
            response = error(403, "Blocked: untrusted request origin")
        elif request.url.path.startswith("/api/") and not self._authorize(scope, request):
            response = error(401, "Missing or wrong Hive token. The CLI reads it from ~/.hihive/token "
                                  "(or HIVE_TOKEN); log in a browser with `hihive open`.")
        elif request.method in JSON_METHODS and _media_type(request) != "application/json":
            response = error(415, "Content-Type must be application/json")
        if response:
            await response(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    def _authorize(self, scope, request):
        """The CLI sends a bearer token; the browser sends the login cookie, which only holds the full token."""
        header = request.headers.get("authorization") or ""
        supplied = header[7:] if header.lower().startswith("bearer ") else request.cookies.get(COOKIE, "")
        if _same_secret(supplied, self.token):
            scope["hive_project"] = None
            return True
        project = self.pool.token_project(supplied) if header else None
        if project:
            scope["hive_project"] = project
            return True
        return False


def _same_secret(supplied, token):
    return bool(supplied) and hmac.compare_digest(supplied.encode(), token.encode())


def _request_host(request):
    """Host name and effective port of the request, from its Host header."""
    header = request.headers.get("host") or ""
    scheme = request.url.scheme
    parts = urlsplit(f"//{header}")
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if scheme == "https" else 80
    return parts.hostname or "", port


def _media_type(request):
    return (request.headers.get("content-type") or "").split(";")[0].strip().lower()


def _trusted_origin(request, allowed, request_port):
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if value:
            return _origin_allowed(value, allowed, request_port)
    return False


def _origin_allowed(value, allowed, request_port):
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return False
    if not parts.hostname or parts.scheme not in ("http", "https"):
        return False
    port = port or (443 if parts.scheme == "https" else 80)
    return parts.hostname in allowed and port == request_port


async def _body(request):
    try:
        body = json.loads(await request.body())
    except ValueError:
        raise HttpError(400, "Invalid JSON") from None
    if not isinstance(body, dict):
        raise HttpError(400, "JSON body must be an object")
    return body


AGENT_REFUSED = "Agents started by Hive can only work with tasks in their own project."


def create_app(pool, token, manage_pool=True):
    """The ASGI app. With manage_pool, the app starts and stops the pool."""

    def run(handler, agents=False):
        """agents: whether a request with an agent token may use this route at all."""
        async def endpoint(request):
            if not agents and request.scope.get("hive_project"):
                return error(403, AGENT_REFUSED)
            try:
                body = await _body(request) if request.method in JSON_METHODS else None
                # Pool calls block on its lock and may wait for runners; keep them
                # off the event loop.
                return await run_in_threadpool(handler, request, body)
            except HttpError as failure:
                return error(failure.status, failure.message)
            except HiveError as failure:
                return error(500, str(failure))
        return endpoint

    def login_page(request):
        """The page a `hihive open` link opens; its script reads the secret from the URL fragment."""
        return FileResponse(STATIC_DIR / "login.html", headers=NO_CACHE)

    def login(request, body):
        """Exchange the secret for an HttpOnly cookie."""
        if not _same_secret(str(body.get("token") or ""), token):
            return error(403, "Wrong Hive token")
        response = Response(status_code=204)
        response.set_cookie(COOKIE, token, max_age=COOKIE_MAX_AGE, httponly=True, samesite="strict")
        return response

    def index(request):
        return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)

    def project_for(request, requested):
        """The project a request works in: the agent's own, or the one named (None for all)."""
        limit = request.scope.get("hive_project")
        requested = str(requested or "").strip() or None
        if limit:
            if requested and requested != limit:
                raise HttpError(403, AGENT_REFUSED)
            return limit
        if requested and not pool.has_project(requested):
            raise HttpError(404, "Project not found")
        return requested

    def task_id_for(request):
        """The task in the path; an agent sees tasks of other projects as missing."""
        task_id = request.path_params["id"]
        limit = request.scope.get("hive_project")
        if limit and pool.task_project(task_id) != limit:
            raise HttpError(404, "Not found")
        return task_id

    def state(request, _body):
        project = project_for(request, request.query_params.get("project"))
        snapshot = pool.state_since(request.query_params.get("since"), project)
        return Response(status_code=204) if snapshot is None else JSONResponse(snapshot)

    def task_detail(request, _body):
        detail = pool.task_detail(task_id_for(request))
        return JSONResponse(detail) if detail else error(404, "Not found")

    def search(request, _body):
        project = project_for(request, request.query_params.get("project"))
        return JSONResponse(pool.search_result_ids(request.query_params.get("q") or "", project))

    def projects(request, _body):
        return JSONResponse({"projects": pool.projects(request.scope.get("hive_project"))})

    def create_project(request, body):
        try:
            project_id = pool.create_project(name=body.get("name"), work_dir=body.get("work_dir"),
                                             color=body.get("color"))
        except HiveError as failure:
            return error(400, str(failure))
        return JSONResponse({"id": project_id})

    def backup(_request, _body):
        content = pool.backup_zip()
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
        return Response(content, media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="hi-hive-backup-{stamp}.zip"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        })

    def delete_project(request, _body):
        result = pool.delete_project(request.path_params["id"])
        if result == "last_project":
            return error(409, "Hive needs at least one project. Create another project before deleting this one.")
        if result == "active_tasks":
            return error(409, "This project has queued or running tasks. Cancel them before deleting the project.")
        if not result:
            return error(404, "Project not found")
        return JSONResponse({"ok": True})

    def add_task(request, body):
        project = project_for(request, body.get("project_id"))
        task_id = pool.add_task(prompt=body.get("prompt"), label=body.get("label"), harness=body.get("harness"),
                                project_id=project)
        return JSONResponse({"id": task_id})

    def add_batch(request, body):
        limit = request.scope.get("hive_project")
        if limit and pool.template_project(body.get("template_id")) != limit:
            return error(404, "Template not found")
        ids = pool.add_tasks_batch(inputs=body.get("inputs"), template_id=body.get("template_id"),
                                   harness=body.get("harness"))
        return JSONResponse({"ids": ids})

    def delete_task(request, _body):
        result = pool.remove_task(task_id_for(request))
        if result == "running_in_subtree":
            return error(409, "This task or one of its forks is still working. Cancel it before deleting.")
        if result == "cleanup_pending":
            return error(409, "The agent process is still shutting down. The task has not been deleted; "
                              "try again shortly.")
        return JSONResponse({"ok": True})

    def fork(request, body):
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            return error(400, "Prompt required")
        task_id = pool.fork_task(parent_id=task_id_for(request), prompt=prompt, label=body.get("label"))
        if task_id == "missing_session":
            return error(400, "Parent task has no agent session id; fork is unavailable")
        if not task_id:
            return error(409, "Parent task cannot be forked in its current state")
        return JSONResponse({"id": task_id})

    def cancel(request, _body):
        if not pool.cancel_task(task_id_for(request)):
            return error(409, "Task cannot be cancelled in its current state")
        return JSONResponse({"ok": True})

    def cancel_all(request, body):
        project = project_for(request, body.get("project_id"))
        return JSONResponse({"ok": True, "cancelled": pool.cancel_all_tasks(project)})

    def continue_task(request, _body):
        if not pool.continue_task(task_id_for(request)):
            return error(409, "Task cannot be continued in its current state")
        return JSONResponse({"ok": True})

    def retry(request, body):
        result = pool.retry_task(task_id_for(request), harness=body.get("harness"))
        if result == "has_children":
            return error(409, "Cannot retry a task that has forks; delete forks first")
        if not result:
            return error(409, "Task cannot be retried in its current state")
        return JSONResponse({"ok": True})

    def done(request, _body):
        result = pool.toggle_done(task_id_for(request))
        if result is None:
            return error(404, "Task not found")
        return JSONResponse({"ok": True, "done": result})

    def chat(request, body):
        task_id = task_id_for(request)  # outside the try: a refused task must stay a 404, not become a 500
        try:
            result = pool.send_chat(task_id, body.get("message"), message_id=body.get("message_id"))
        except Exception as failure:
            status = 422 if isinstance(failure, (HiveError, ValueError)) else 500
            prompt = str(body.get("message") or "")
            return error(status, redact(str(failure), prompts=[prompt]))
        if result == "missing_session":
            return error(400, "Task has no agent session id; follow-up is unavailable")
        if result == "runner_closing":
            return error(409, "The agent process is stopping; try again after it exits")
        if result == "unresolved_inputs":
            return error(409, "Previous messages have unresolved delivery; cannot resume the agent session yet")
        if not result:
            return error(400, "Cannot chat with this task")
        return JSONResponse({"ok": True, "message": result}, status_code=202)

    def chat_cancel(request, body):
        if not pool.cancel_chat(task_id_for(request), body.get("msg_id")):
            return error(409, "No active conversation run to stop")
        return JSONResponse({"ok": True})

    def reorder(request, body):
        pool.reorder_queue(body.get("order"), project_for(request, body.get("project_id")))
        return JSONResponse({"ok": True})

    def pause(request, _body):
        pool.pause()
        return JSONResponse({"paused": True})

    def resume(request, _body):
        pool.resume()
        return JSONResponse({"paused": False})

    def restart(request, _body):
        def stop_server():
            time.sleep(0.3)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=stop_server, daemon=True).start()
        return JSONResponse({"ok": True})

    def get_config(request, _body):
        return JSONResponse({**pool.config(), "hive_home": str(hive_home())})

    def put_config(request, body):
        try:
            pool.update_config(body)
        except HiveError as failure:
            return error(400, str(failure))
        return JSONResponse({"ok": True})

    def output_action(request, body):
        tag = request.path_params["tag"]
        result = pool.run_output_action(tag, body.get("value"))
        if result == "not_found":
            return error(404, f"Output action '{tag}' is not configured")
        if result:
            return error(422, result)
        return JSONResponse({"ok": True})

    def save_template(request, body):
        try:
            template_id = pool.save_template(
                id=body.get("id"), project_id=body.get("project_id"), name=body.get("name"),
                template=body.get("template"),
                review_section_ids=body.get("review_section_ids") or [], color=body.get("color"),
                work_dir=body.get("work_dir"), agent_args=body.get("agent_args"), harness=body.get("harness"))
        except HiveError as failure:
            return error(409, str(failure))
        return JSONResponse({"id": template_id})

    def delete_template(request, _body):
        pool.delete_template(request.path_params["id"])
        return JSONResponse({"ok": True})

    routes = [
        Route("/", index),
        Route("/login", login_page, methods=["GET"]),
        Route("/login", run(login), methods=["POST"]),
        # Routes with agents=True accept agent tokens; each limits them to the agent's project.
        Route("/api/state", run(state, agents=True)),
        Route("/api/search", run(search, agents=True)),
        Route("/api/projects", run(projects, agents=True), methods=["GET"]),
        Route("/api/projects", run(create_project), methods=["POST"]),
        Route("/api/backup", run(backup), methods=["POST"]),
        Route("/api/projects/{id}", run(delete_project), methods=["DELETE"]),
        Route("/api/tasks", run(add_task, agents=True), methods=["POST"]),
        Route("/api/tasks/batch", run(add_batch, agents=True), methods=["POST"]),
        Route("/api/tasks/cancel_all", run(cancel_all, agents=True), methods=["POST"]),
        Route("/api/tasks/{id}", run(task_detail, agents=True), methods=["GET"]),
        Route("/api/tasks/{id}", run(delete_task, agents=True), methods=["DELETE"]),
        Route("/api/tasks/{id}/fork", run(fork, agents=True), methods=["POST"]),
        Route("/api/tasks/{id}/cancel", run(cancel, agents=True), methods=["POST"]),
        Route("/api/tasks/{id}/continue", run(continue_task, agents=True), methods=["POST"]),
        Route("/api/tasks/{id}/retry", run(retry, agents=True), methods=["POST"]),
        Route("/api/tasks/{id}/done", run(done, agents=True), methods=["POST"]),
        Route("/api/tasks/{id}/chat", run(chat, agents=True), methods=["POST"]),
        Route("/api/tasks/{id}/chat/cancel", run(chat_cancel, agents=True), methods=["POST"]),
        Route("/api/queue/reorder", run(reorder), methods=["POST"]),
        Route("/api/control/pause", run(pause), methods=["POST"]),
        Route("/api/control/resume", run(resume), methods=["POST"]),
        Route("/api/control/restart", run(restart), methods=["POST"]),
        Route("/api/config", run(get_config), methods=["GET"]),
        Route("/api/config", run(put_config), methods=["PUT"]),
        Route("/api/output_actions/{tag}/run", run(output_action), methods=["POST"]),
        Route("/api/templates", run(save_template), methods=["POST"]),
        Route("/api/templates/{id}", run(delete_template), methods=["DELETE"]),
        Mount("/", NoCacheStaticFiles(directory=STATIC_DIR)),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        if manage_pool:
            pool.start()
        try:
            yield
        finally:
            if manage_pool:
                await run_in_threadpool(pool.stop)

    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(RequestGuard, pool=pool, token=token)
    return app
