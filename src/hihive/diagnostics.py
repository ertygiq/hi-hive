"""Runner diagnostics journal.

Store protocol structure and fingerprints, never raw wire records, stderr,
model output, credentials, or input text. User-visible errors are redacted too.
"""

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import UTC, datetime

_CREDENTIAL = re.compile(r"(?:Bearer\s+|\bsk-|\bgh[pousr]_|\bsk_ant_)[A-Za-z0-9_.\-]+", re.IGNORECASE)
_CREDENTIAL_FIELD = re.compile(
    r"([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization)[\"']?\s*[:=]\s*)[^\s,}]+",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_TOKEN = re.compile(r"[a-zA-Z0-9_.:-]{1,64}")


def utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def redact(text, prompts=()) -> str:
    result = "" if text is None else str(text)
    for prompt in sorted((p for p in prompts if p), key=len, reverse=True):
        result = result.replace(prompt, "[input omitted]")
    result = _CREDENTIAL.sub("[credential omitted]", result)
    result = _CREDENTIAL_FIELD.sub(r"\1[credential omitted]", result)
    return _CONTROL.sub("", result)[:500]


def _fingerprint(context):
    data = context if isinstance(context, bytes) else str(context).encode("utf-8", "surrogateescape")
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _token(value):
    if not value:
        return None
    value = str(value)
    return redact(value) if _TOKEN.fullmatch(value) else "[unrecognized token]"


class RunnerDiagnostics:
    def __init__(self, task_id, session_id, harness, path=None):
        self.identity = {"task_id": task_id, "session_id": session_id, "harness": harness,
                         "runner_id": str(uuid.uuid4())}
        self.binary_version = None
        self._sequence = 0
        self._path = path
        self._lock = threading.Lock()

    def record(self, kind, state, input_id=None, input_ids=None, result_id=None, event=None,
               context=None, error=None) -> dict:
        with self._lock:
            self._sequence += 1
            origin = event.get("origin") if event else None
            record = {
                **self.identity,
                "timestamp": utc_timestamp(),
                "order": self._sequence,
                "kind": kind,
                "binary_version": self.binary_version,
                "runner_state": state,
                "hive_message_id": input_id,
                "harness_input_id": input_id,
                "contributing_input_ids": input_ids,
                "result_id": result_id,
                "event_type": _token(event and event.get("type")),
                "event_subtype": _token(event and event.get("subtype")),
                "event_origin": _token(origin.get("kind") if isinstance(origin, dict) else None),
                "error": error,
                "context": _fingerprint(context) if context is not None else None,
            }
            record = {key: value for key, value in record.items() if value is not None}
            if self._path:
                try:
                    os.makedirs(os.path.dirname(self._path), exist_ok=True)
                    fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    try:
                        os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode())
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                except OSError as error:
                    record["persistence_error"] = f"Cannot persist runner diagnostics ({type(error).__name__})"
            return record
