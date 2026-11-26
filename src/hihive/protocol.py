"""Wire protocols of the live harnesses: Pi RPC and Claude Code stream-json."""

import re
import time
import uuid

from . import HiveError

_VERSION = re.compile(r"\b\d+\.\d+\.\d+\b")
RESERVED_ARGS = {"-p", "--print", "--mode", "--input-format", "--output-format", "--session", "--session-id",
                 "--fork", "--fork-session", "-r", "--resume", "-c", "--continue", "--no-session"}
IDLE_BACKGROUND = {"task_count": 0, "preparing_reply": False}


def _version_tuple(text):
    return tuple(int(part) for part in text.split("."))


def find_version(text):
    match = _VERSION.search(text)
    return match.group(0) if match else None


class LiveProtocol:
    input_interval = 0

    def idle_shutdown_ready(self, runner) -> bool:
        return True

    def background_activity(self) -> dict:
        return dict(IDLE_BACKGROUND)

    def validate_args(self, args):
        for arg in args:
            if arg.split("=")[0] in RESERVED_ARGS:
                raise HiveError(f"Hive owns protocol/session flags; remove {arg} from harness args")

    def text(self, content) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            raise HiveError("Assistant content must be text or an array of content blocks")
        parts = []
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                raise HiveError("Assistant content block must have a string type")
            if block["type"] != "text":
                continue
            if not isinstance(block.get("text"), str):
                raise HiveError("Assistant text block must contain string text")
            parts.append(block["text"])
        return "\n".join(parts)


class PiRpcProtocol(LiveProtocol):
    # Pi 0.87.1 can acknowledge but lose prompts sent together at startup/idle.
    # Space inputs as a minimal workaround, not a delivery guarantee (LIMITATIONS.md).
    input_interval = 1.0

    KNOWN_EVENTS = {
        "agent_start", "agent_end", "turn_start", "turn_end", "message_start", "message_update",
        "tool_execution_start", "tool_execution_update", "tool_execution_end", "queue_update",
        "entry_appended", "session_info_changed", "thinking_level_changed", "compaction_start", "compaction_end",
        "auto_retry_start", "auto_retry_end", "summarization_retry_scheduled", "summarization_retry_attempt_start",
        "summarization_retry_finished", "bash_execution_update",
    }

    def __init__(self):
        self._state_ready = False
        self._steering_ready = False
        self._follow_ready = False

    def check_version(self, text):
        version = find_version(text)
        if not (version and _version_tuple(version) >= (0, 87, 1)):
            raise HiveError("Live Pi requires 0.87.1 or newer")

    def argv(self, command, args, session_id, resume=False, parent=None):
        self.validate_args(args)
        return [command, *args, "--mode", "rpc", "--session-id", session_id, *(["--fork", parent] if parent else [])]

    def boot(self, runner):
        runner.control({"type": "get_state", "id": "hive-ready"})
        runner.control({"type": "set_steering_mode", "mode": "all", "id": "hive-steering-mode"})
        runner.control({"type": "set_follow_up_mode", "mode": "all", "id": "hive-follow-mode"})

    def input(self, input_id, content):
        # Unlike direct steer, prompt starts work if the process became idle.
        return {"id": input_id, "type": "prompt", "message": content, "streamingBehavior": "steer"}

    def _check_ready(self, runner):
        if self._state_ready and self._steering_ready and self._follow_ready:
            runner.ready()

    def event(self, runner, event):
        kind = event["type"]
        if kind == "response":
            event_id = event.get("id")
            if event_id == "hive-ready":
                data = event.get("data") or {}
                if not (event.get("success") and "pendingMessageCount" in data and "isStreaming" in data):
                    raise HiveError("Pi RPC lacks required state capabilities")
                if "sessionId" not in data:
                    raise HiveError("Pi RPC state lacks sessionId")
                runner.session(data["sessionId"])
                self._state_ready = True
                self._check_ready(runner)
            elif event_id in ("hive-steering-mode", "hive-follow-mode"):
                if not event.get("success"):
                    raise HiveError("Pi RPC queue configuration rejected")
                if event_id == "hive-steering-mode":
                    self._steering_ready = True
                else:
                    self._follow_ready = True
                self._check_ready(runner)
            elif str(event_id or "").startswith("hive-settled-"):
                if event.get("success"):
                    runner.pi_settled(event_id, event.get("data") or {})
            else:
                success = bool(event.get("success"))
                runner.delivery(event_id, "accepted" if success else "rejected",
                                error=event.get("error"), harness_accepted=success)
        elif kind == "message_end":
            message = event.get("message") or {}
            if message.get("role") == "assistant":
                runner.answer(self.text(message.get("content")))
                failed = message.get("stopReason") in ("error", "aborted")
                runner.run_error((message.get("errorMessage") or message.get("stopReason")) if failed else None)
        elif kind in ("compaction_start", "compaction_end"):
            runner.pi_compaction(kind == "compaction_start")
        elif kind == "agent_settled":
            runner.probe_pi_settlement()
        elif kind == "extension_ui_request":
            # No interactive approval UI is implemented. Fail closed instead of hanging.
            raise HiveError("Pi requested interactive extension UI; use a noninteractive harness configuration")
        elif kind in self.KNOWN_EVENTS:
            pass  # Known progress records do not prove per-input delivery or settlement.
        else:
            runner.unknown_event(event)


class ClaudeStreamProtocol(LiveProtocol):
    idle_probe_timeout = 5.0

    KNOWN_SYSTEM_SUBTYPES = {
        "thinking_tokens", "status", "compact_boundary", "hook_started", "hook_progress", "hook_response",
        "task_started", "task_progress", "task_notification", "api_retry", "local_command", "turn_duration",
    }
    LIFECYCLE = {"queued": "accepted", "started": "delivered", "rejected": "rejected", "failed": "failed"}

    def __init__(self):
        self._turn_active = False
        self._background_tasks = []
        self._background_reply_pending = False
        self._idle_shutdown_disabled = False
        self._idle_probe = None

    def background_activity(self):
        return {
            "task_count": len(self._background_tasks),
            "preparing_reply": bool(self._background_reply_pending and self._turn_active and not self._background_tasks),
        }

    def idle_shutdown_ready(self, runner):
        """Ask Claude whether it is idle before closing it.

        initialize is also a state query: the response carries session_state,
        followed by a background_tasks_changed snapshot. Neither alone proves
        quiescence.
        """
        if self._idle_shutdown_disabled or self._turn_active or self._background_tasks:
            return False
        if self._idle_probe and self._idle_probe["activity_version"] != runner.activity_version:
            self._idle_probe = None
        if self._idle_probe:
            if self._idle_probe.get("idle") and self._idle_probe.get("snapshot"):
                return True
            if time.monotonic() - self._idle_probe["started"] >= self.idle_probe_timeout:
                self._idle_shutdown_disabled = True
                runner.idle_shutdown_uncertain()
            return False
        probe_id = f"hive-idle-{uuid.uuid4()}"
        self._idle_probe = {"id": probe_id, "activity_version": runner.activity_version, "started": time.monotonic()}
        runner.control({"type": "control_request", "request_id": probe_id, "request": {"subtype": "initialize"}})
        return False

    def background_tasks_changed(self, runner, event):
        tasks = event.get("tasks")
        valid = isinstance(tasks, list) and all(
            isinstance(task, dict) and isinstance(task.get("task_id"), str) and task["task_id"] for task in tasks)
        if not valid:
            raise HiveError("Claude background task snapshot must contain an array of task IDs")
        ids = sorted(task["task_id"] for task in tasks)
        if self._background_tasks != ids:
            runner.touch_activity()
        if self._background_tasks and not ids:
            self._background_reply_pending = True
        self._background_tasks = ids
        if self._idle_probe and self._idle_probe.get("responded"):
            self._idle_probe["snapshot"] = True
            if not (self._idle_probe.get("idle") and not ids):
                self._idle_probe = None
                runner.touch_activity()

    def idle_probe_response(self, runner, event):
        response = event.get("response")
        if not (isinstance(response, dict) and self._idle_probe and response.get("request_id") == self._idle_probe["id"]):
            runner.unknown_event(event, visible=False)
            return
        data = response.get("response")
        if not (response.get("subtype") == "success" and isinstance(data, dict)
                and data.get("session_state") in ("idle", "running", "requires_action")):
            self._idle_shutdown_disabled = True
            runner.idle_shutdown_uncertain()
            return
        self._idle_probe["responded"] = True
        self._idle_probe["idle"] = (data["session_state"] == "idle"
                                    and response.get("pending_permission_requests") == []
                                    and response.get("pending_user_dialog_requests") == [])

    def check_version(self, text):
        version = find_version(text)
        if not (version and _version_tuple(version) >= (2, 1, 283)):
            raise HiveError("Live Claude requires Claude Code 2.1.283 or newer")

    def argv(self, command, args, session_id, resume=False, parent=None):
        self.validate_args(args)
        if parent:
            session = ["--resume", parent, "--fork-session", "--session-id", session_id]
        elif resume:
            session = ["--resume", session_id]
        else:
            session = ["--session-id", session_id]
        return [command, *args, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--replay-user-messages", *session]

    def boot(self, runner):
        # Init is emitted only after the first input. Further inputs wait for init.
        runner.ready(initial_only=True)

    def input(self, input_id, content):
        return {"type": "user", "uuid": input_id, "message": {"role": "user", "content": content},
                "parent_tool_use_id": None}

    def event(self, runner, event):
        if event.get("parent_tool_use_id"):
            return
        if event.get("session_id"):
            runner.session(event["session_id"])
        kind = event["type"]
        if kind == "system":
            subtype = event.get("subtype")
            if subtype == "init":
                capabilities = event.get("capabilities")
                if not isinstance(capabilities, list) or "msg_lifecycle_v1" not in capabilities:
                    raise HiveError("Claude lacks msg_lifecycle_v1 capability")
                self._turn_active = True
                runner.touch_activity()
                runner.ready()
            elif subtype == "background_tasks_changed":
                self.background_tasks_changed(runner, event)
            elif subtype == "session_state_changed":
                if event.get("state") not in ("idle", "running", "requires_action"):
                    raise HiveError("Claude session state is invalid")
                self._turn_active = event["state"] != "idle"
                if not self._turn_active:
                    self._background_reply_pending = False
                runner.touch_activity()
            elif subtype in ("informational", "model_refusal_fallback"):
                runner.harness_notice(event)
            elif subtype not in self.KNOWN_SYSTEM_SUBTYPES:
                runner.unknown_event(event, visible=False)
        elif kind == "control_response":
            self.idle_probe_response(runner, event)
        elif kind == "command_lifecycle":
            state = self.LIFECYCLE.get(event.get("state"))
            if state:
                runner.delivery(event.get("command_uuid"), state, harness_accepted=True)
            elif event.get("state") != "completed":
                runner.unknown_event(event, visible=False)
        elif kind == "assistant":
            self._turn_active = True
            runner.touch_activity()
            runner.answer(self.text((event.get("message") or {}).get("content")))
        elif kind == "result":
            result_id = event.get("uuid")
            if not isinstance(result_id, str) or not result_id:
                raise HiveError("Claude result lacks a result UUID")
            text = event.get("result")
            if text is not None and not isinstance(text, str):
                raise HiveError("Claude result text must be a string")
            ids = event.get("user_message_uuids")
            origin = event.get("origin")
            background = isinstance(origin, dict) and origin.get("kind") == "task-notification"
            if not background and not (isinstance(ids, list) and ids):
                raise HiveError("Claude result lacks plural input association")
            self._turn_active = False
            self._background_reply_pending = False
            runner.touch_activity()
            error = str(event.get("errors") or event.get("result") or "Claude run failed") if event.get("is_error") else None
            if background:
                runner.claude_background_result(result_id, text, error)
            else:
                runner.claude_result(result_id, ids, text, error)
        elif kind in ("user", "stream_event", "rate_limit_event"):
            pass  # Replay, streaming deltas and quota notices are not assistant answers.
        else:
            runner.unknown_event(event, visible=False)


def protocol_for(harness_type):
    if harness_type == "pi":
        return PiRpcProtocol()
    if harness_type == "claude-code":
        return ClaudeStreamProtocol()
    raise HiveError(f"Unsupported live harness: {harness_type}")
