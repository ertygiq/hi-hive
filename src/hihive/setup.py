"""First-install detection and configuration of supported coding agents."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import paths
from .shell_env import read_login_shell_env

AGENTS = {
    "claude-code": {
        "name": "Claude Code", "command": "claude", "minimum": (2, 1, 283),
        "icon": "◆", "color": "slate", "args": ["--dangerously-skip-permissions"],
    },
    "pi": {
        "name": "Pi", "command": "pi", "minimum": (0, 87, 1),
        "icon": "✦", "color": "purple", "args": [],
    },
}


def _version(text):
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", text or "")
    return tuple(int(part) for part in match.groups()) if match else None


def _version_text(version):
    return ".".join(str(part) for part in version) if version else None


def detect_coding_agents(shell=None, timeout=5, skip_types=None):
    """Detect only supported commands in the same login-shell environment as the service."""
    try:
        env = read_login_shell_env(shell=shell)
        environment_error = None
    except (OSError, RuntimeError) as error:
        env = None
        environment_error = str(error)
    detected = []
    skip_types = set(skip_types or [])
    for agent_id, spec in AGENTS.items():
        if agent_id in skip_types:
            detected.append({"id": agent_id, "name": spec["name"], "command": spec["command"],
                             "minimum": _version_text(spec["minimum"]), "path": None,
                             "status": "custom", "environment_error": environment_error})
            continue
        if env is None:
            detected.append({"id": agent_id, "name": spec["name"], "command": spec["command"],
                             "minimum": _version_text(spec["minimum"]), "path": None,
                             "status": "unavailable", "environment_error": environment_error,
                             "error": "login-shell environment unavailable"})
            continue
        executable = shutil.which(spec["command"], path=env.get("PATH"))
        record = {"id": agent_id, "name": spec["name"], "command": spec["command"],
                  "minimum": _version_text(spec["minimum"]), "path": executable,
                  "environment_error": environment_error}
        if not executable:
            record["status"] = "not_found"
            detected.append(record)
            continue
        try:
            result = subprocess.run([executable, "--version"], env=env, capture_output=True,
                                    text=True, timeout=timeout, check=False)
            version = _version(f"{result.stdout}\n{result.stderr}")
        except (OSError, subprocess.TimeoutExpired) as error:
            record.update(status="unavailable", error=str(error))
        else:
            record["version"] = _version_text(version)
            if result.returncode != 0 or version is None:
                record.update(status="unavailable", error="version command failed or returned no version")
            elif version < spec["minimum"]:
                record["status"] = "too_old"
            else:
                record["status"] = "usable"
        detected.append(record)
    return detected


def _record(agent):
    spec = AGENTS[agent["id"]]
    return {
        "id": agent["id"], "name": spec["name"], "icon": spec["icon"], "color": spec["color"],
        "type": agent["id"], "command": agent["path"], "args": list(spec["args"]),
        **({"sessions_dir": None} if agent["id"] == "pi" else {}),
    }


def _write_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)
            file.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def configure_detected_agents(mode, accepted=None, shell=None):
    """Configure detected agents for a fresh config, or add explicitly accepted types."""
    config_path = paths.config_file()
    fresh = not config_path.exists()
    if mode == "fresh" and not fresh:
        raise ValueError(f"Existing settings at {config_path} were not changed")
    if fresh:
        with open(paths.DEFAULT_CONFIG, encoding="utf-8") as file:
            config = json.load(file)
    else:
        try:
            with open(config_path, encoding="utf-8") as file:
                config = json.load(file)
        except (OSError, ValueError) as error:
            raise ValueError(f"Cannot read existing settings at {config_path}: {error}") from None
        if not isinstance(config, dict) or not isinstance(config.get("harnesses", []), list):
            raise ValueError(f"Existing settings at {config_path} have an invalid coding-agent list")

    custom_types = {item.get("type") for item in config.get("harnesses", []) if isinstance(item, dict)
                    and Path(str(item.get("command") or "")).name != AGENTS.get(item.get("type"), {}).get("command")}
    detected = detect_coding_agents(shell=shell, skip_types=custom_types)
    usable = [agent for agent in detected if agent["status"] == "usable"]
    configured = config.setdefault("harnesses", [])
    initial_count = len(configured)
    existing_types = {item.get("type") for item in configured if isinstance(item, dict)}
    existing_ids = {item.get("id") for item in configured if isinstance(item, dict)}
    existing_names = {str(item.get("name") or "").casefold() for item in configured if isinstance(item, dict)}
    wanted = {agent["id"] for agent in usable} if fresh else set(accepted or [])
    added = []
    conflicts = []
    for agent in usable:
        if agent["id"] not in wanted or agent["id"] in existing_types:
            continue
        spec = AGENTS[agent["id"]]
        if agent["id"] in existing_ids or spec["name"].casefold() in existing_names:
            conflicts.append(agent["id"])
            continue
        configured.append(_record(agent))
        existing_types.add(agent["id"])
        added.append(agent["id"])

    if fresh:
        config["default_harness"] = "claude-code" if "claude-code" in added else ("pi" if "pi" in added else None)
    elif initial_count == 0 and added:
        config["default_harness"] = added[0]
    if fresh or added:
        _write_json_atomic(config_path, config)
    return {"fresh": fresh, "config_path": str(config_path), "detected": detected,
            "configured": configured, "configured_types": [item.get("type") for item in configured if isinstance(item, dict)],
            "default_harness": config.get("default_harness"), "initial_count": initial_count,
            "added": added, "conflicts": conflicts}


def unconfigured_usable(result):
    configured = set(result["configured_types"])
    return [agent for agent in result["detected"]
            if agent["status"] == "usable" and agent["id"] not in configured]
