"""Per-harness facts that are not part of the live protocol: session files and cost."""

import glob
import json
import os
from datetime import datetime

from . import HiveError


class HarnessAdapter:
    def preassign_session_id(self) -> bool:
        return True

    def session_file(self, args, harness, session_id):
        """Path of the harness's own transcript for a session, or None."""
        return None

    def session_cost(self, path):
        """Money spent in a session, in USD, read from its transcript, or None."""
        return None


class ClaudeCodeAdapter(HarnessAdapter):
    pass


class PiAdapter(HarnessAdapter):
    def sessions_root(self, args, harness):
        """Root under which Pi keeps session files.

        Pi stores them at <root>/<encoded cwd>/<timestamp>_<session id>.jsonl.
        Resolution order: an explicit --session-dir arg, the harness's
        configured sessions_dir (for a Pi that runs elsewhere, e.g. inside a
        container with its own home), then Pi's default.
        """
        if "--session-dir" in args:
            index = args.index("--session-dir")
            if index + 1 < len(args) and args[index + 1]:
                return args[index + 1]
        configured = str((harness or {}).get("sessions_dir") or "").strip()
        if configured:
            return os.path.expanduser(configured)
        agent_dir = os.environ.get("PI_CODING_AGENT_DIR") or os.path.join(os.path.expanduser("~"), ".pi", "agent")
        return os.path.join(agent_dir, "sessions")

    def session_file(self, args, harness, session_id):
        # Looked up by id across every cwd subdirectory: the cwd Pi encodes into
        # the path is the one it saw, which differs from ours inside a container.
        session = str(session_id or "").strip()
        if not session:
            return None
        pattern = os.path.join(glob.escape(self.sessions_root(args, harness)), "**", f"*_{glob.escape(session)}.jsonl")
        matches = glob.glob(pattern, recursive=True)
        return matches[0] if matches else None

    def session_cost(self, path):
        """Sum of what every assistant turn in the session was billed.

        A forked session file starts with a verbatim copy of the parent's
        entries, which keep their original timestamps. Counting only entries at
        or after the session header's timestamp attributes each API call to the
        session that made it, so parent and fork never double-count.
        """
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as file:
                header = json.loads(file.readline())
                if not isinstance(header, dict) or header.get("type") != "session":
                    return None
                started = _parse_time(header.get("timestamp"))
                total = 0.0
                for line in file:
                    if '"role":"assistant"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    message = entry.get("message") if isinstance(entry, dict) else None
                    if entry.get("type") != "message" or not isinstance(message, dict) or message.get("role") != "assistant":
                        continue
                    stamp = _parse_time(entry.get("timestamp"))
                    if started and stamp and stamp < started:
                        continue
                    cost = ((message.get("usage") or {}).get("cost") or {}).get("total")
                    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                        total += cost
                return total
        except (OSError, ValueError, AttributeError, TypeError):
            return None


def _parse_time(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def adapter_for(harness_type):
    if harness_type == "claude-code":
        return ClaudeCodeAdapter()
    if harness_type == "pi":
        return PiAdapter()
    raise HiveError(f"Unknown harness type '{harness_type}'")
