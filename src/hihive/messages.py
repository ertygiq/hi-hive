"""One authoritative conversation per task (see TASK_STORAGE.md)."""

import hashlib
import uuid

from . import HiveError

ROLES = ("user", "assistant", "system")


def message(role, kind, content, **facts):
    return {"id": str(uuid.uuid4()), "role": role, "kind": kind, "content": content, **facts}


def initial(task):
    return next(m for m in task["messages"] if m["role"] == "user" and m["kind"] == "initial")


def output(task):
    return "\n\n".join(m["content"] for m in task["messages"]
                       if m["role"] == "assistant" and m["kind"] in ("initial", "continue"))


def reviews(task):
    return [m for m in task["messages"] if m["role"] == "user" and m["kind"] == "review"]


def schedule_reviews(task, prompts):
    for prompt in prompts:
        task["messages"].append(message("user", "review", prompt, review_id=str(uuid.uuid4()), delivery="scheduled"))


def outstanding(task):
    return [m for m in task["messages"] if m["role"] == "user" and m.get("settled") is False]


def tombstone(entry):
    """An ID/hash receipt that blocks a late duplicate without keeping the text."""
    return {"id": entry["id"], "content_sha256": content_hash(entry["content"]),
            "sequence": entry.get("sequence"), "status": entry.get("delivery"), "settled": True}


def content_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def restore(raw):
    """Validate stored messages. Fails instead of discarding a conversation."""
    if not isinstance(raw, list):
        raise HiveError("Invalid stored message")
    messages = []
    for entry in raw:
        if not (isinstance(entry, dict) and isinstance(entry.get("id"), str) and isinstance(entry.get("content"), str)
                and entry.get("role") in ROLES and entry.get("kind")):
            raise HiveError("Invalid stored message")
        messages.append(dict(entry))
    if len({m["id"] for m in messages}) != len(messages):
        raise HiveError("Duplicate stored message IDs")
    if sum(1 for m in messages if m["role"] == "user" and m["kind"] == "initial") != 1:
        raise HiveError("Task must have one initial prompt")
    return messages
