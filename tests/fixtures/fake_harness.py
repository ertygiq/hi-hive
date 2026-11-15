"""A fake Pi RPC or Claude stream-json harness driven by files in --fake-root."""

import json
import os
import sys
import threading
import time


def option(name):
    if name in sys.argv:
        index = sys.argv.index(name)
        if index + 1 < len(sys.argv):
            return sys.argv[index + 1]
    return None


kind = option("--fake-kind") or "pi"
if "--version" in sys.argv:
    if option("--fake-case") == "old-version":
        print("0.1.0")
    else:
        print("0.87.1" if kind == "pi" else "2.1.283 (Claude Code)")
    sys.exit(0)

root = option("--fake-root")
scenario = option("--fake-case")
session = option("--session-id") or option("--resume")


def path(name):
    return os.path.join(root, name)


def exists(name):
    return os.path.exists(path(name))


def wait_for_file(name):
    while not exists(name):
        time.sleep(0.01)


with open(path("pid"), "w") as file:
    file.write(str(os.getpid()))

lock = threading.Lock()
state = {"active": False, "worker": None}
queue = []
history = []


def emit(event):
    os.write(1, (json.dumps(event) + "\n").encode())


def work():
    with lock:
        if kind == "pi":
            emit({"type": "agent_start"})
        else:
            emit({"type": "system", "subtype": "init", "session_id": session,
                  "capabilities": [] if scenario == "no-capability" else ["msg_lifecycle_v1"]})
    with open(path("hive_token"), "w") as file:
        file.write(os.environ.get("HIVE_TOKEN", ""))
    with open(path("started"), "w") as file:
        file.write("yes")
    if scenario == "unknown-event":
        with lock:
            emit({"type": "future_protocol_event", "api_key": "sk-secret-test", "prompt": "PRIVATE_PROMPT"})
    elif scenario == "claude-notices":
        with lock:
            emit({"type": "system", "subtype": "informational", "session_id": session, "content": "Harness information"})
            emit({"type": "system", "subtype": "model_refusal_fallback", "session_id": session,
                  "content": "Harness switched model"})
            for _ in range(2):
                emit({"type": "system", "subtype": "thinking_tokens", "session_id": session, "thinking_tokens": 42})
    elif scenario == "malformed":
        os.write(1, b'{"secret":"sk-secret-test", BROKEN PRIVATE_PROMPT\n')
    elif scenario == "stdout-eof":
        os.close(1)
    if scenario == "stderr":
        os.write(2, b"diagnostic " * 20_000)
    wait_for_file("release")
    with lock:
        if scenario == "split-result" and len(queue) > 1:
            batches = [[queue.pop(0)], list(queue)]
        else:
            batches = [list(queue)]
        queue.clear()
        for batch in batches:
            ids = [message_id for message_id, _ in batch]
            for message_id, message in batch:
                history.append(message)
                if kind == "pi":
                    emit({"type": "message_end", "message": {"role": "user", "content": message}})
                else:
                    emit({"type": "command_lifecycle", "command_uuid": message_id, "state": "started",
                          "session_id": session})
            text = " ".join(history)
            if kind == "pi":
                emit({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": text}],
                                                         "stopReason": "stop"}})
            else:
                if scenario == "background-activity":
                    emit({"type": "system", "subtype": "background_tasks_changed",
                          "tasks": [{"task_id": "one"}, {"task_id": "two"}]})
                emit({"type": "assistant", "session_id": session, "message": {"content": [{"type": "text", "text": text}]}})
                result = {"type": "result", "uuid": "-".join(ids), "user_message_uuids": ids, "result": text,
                          "session_id": session, "queued_turn_count": 0}
                emit(result)
                if scenario == "duplicate-result":
                    emit(result)
                for message_id in ids:
                    emit({"type": "command_lifecycle", "command_uuid": message_id, "state": "completed",
                          "session_id": session})
        state["active"] = False
        if kind == "pi":
            emit({"type": "agent_settled"})
        if scenario == "clean-exit":
            os._exit(0)
    if scenario == "background-activity":
        wait_for_file("background-finished")
        with lock:
            emit({"type": "system", "subtype": "background_tasks_changed", "tasks": []})
            emit({"type": "system", "subtype": "init", "session_id": session, "capabilities": ["msg_lifecycle_v1"]})
        wait_for_file("background-reply")
        with lock:
            emit({"type": "result", "uuid": "background-reply", "origin": {"kind": "task-notification"},
                  "result": "Background finished"})


for line in sys.stdin.buffer:
    event = json.loads(line)
    with open(path("inputs"), "ab") as file:
        file.write(line)
    with open(path("input_times"), "a") as file:
        file.write(json.dumps({"id": event.get("id") or event.get("uuid"), "type": event.get("type"),
                               "at": time.monotonic()}) + "\n")
    with lock:
        if kind == "claude-code" and event.get("type") == "control_request":
            if scenario != "lost-idle-probe":
                emit({"type": "control_response", "response": {
                    "subtype": "success", "request_id": event["request_id"],
                    "response": {"session_state": "running" if state["active"] else "idle"},
                    "pending_permission_requests": [], "pending_user_dialog_requests": []}})
                emit({"type": "system", "subtype": "background_tasks_changed", "tasks": []})
            continue
        if kind == "pi" and event.get("type") != "prompt":
            data = None
            if event.get("type") == "get_state":
                data = {"sessionId": session, "isStreaming": state["active"], "pendingMessageCount": len(queue)}
            emit({"type": "response", "id": event.get("id"), "command": event.get("type"), "success": True, "data": data})
            continue
        message_id = event.get("id") or event.get("uuid")
        message = event["message"]
        text = message["content"] if isinstance(message, dict) else message
        if text == "CRASH":
            os.write(2, b"intentional crash\n")
            os._exit(7)
        if text == "CLOSE_STDIN":
            os.close(0)
            time.sleep(30)
            continue
        reject = text == "REJECT"
        if text == "NO_ACK":
            pass  # Deliberately omit the transport receipt.
        elif kind == "pi":
            emit({"type": "response", "id": message_id, "command": "prompt", "success": not reject,
                  "error": "rejected" if reject else None})
        else:
            emit({"type": "command_lifecycle", "command_uuid": message_id, "state": "rejected" if reject else "queued",
                  "session_id": session})
        if reject:
            continue
        queue.append((message_id, text))
        if state["active"]:
            continue
        state["active"] = True
        if text == "REVIEW_GATE":
            if exists("release"):
                os.remove(path("release"))
            with open(path("review-started"), "w") as file:
                file.write("yes")
        state["worker"] = threading.Thread(target=work, daemon=True)
        state["worker"].start()

if state["worker"]:
    state["worker"].join()
