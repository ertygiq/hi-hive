"""Install and manage hi-hive as a per-user background service."""

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import paths

LABEL = "com.hihive"
UNIT_NAME = f"{LABEL}.service"


def manager():
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    raise SystemExit("hihive service supports macOS launchd and Linux systemd user services; run `hihive serve` instead.")


def launchd_path():
    return Path("~/Library/LaunchAgents").expanduser() / f"{LABEL}.plist"


def systemd_path():
    return Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "systemd" / "user" / UNIT_NAME


def _domain():
    return f"gui/{os.getuid()}"


def _launchd_loaded():
    result = subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True)
    return result.returncode == 0


def _launchd_bootout():
    if not _launchd_loaded():
        return
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], check=False)
    for _ in range(20):
        if not _launchd_loaded():
            return
        time.sleep(0.3)


def _executable():
    """The absolute installed hihive command that a service should run directly."""
    found = shutil.which("hihive")
    if found:
        return os.path.realpath(found)
    candidate = Path(sys.argv[0]).resolve()
    if candidate.name in ("hihive", "hih") and candidate.exists():
        return str(candidate)
    raise SystemExit("Cannot find the installed 'hihive' command. Install it first: uv tool install .")


def install(port=None):
    selected = manager()
    port = port or int(os.environ.get("PORT") or paths.DEFAULT_PORT)
    if selected == "launchd":
        _install_launchd(port)
    else:
        _install_systemd(port)


def uninstall():
    if manager() == "launchd":
        _uninstall_launchd()
    else:
        _uninstall_systemd()


def status():
    if manager() == "launchd":
        result = subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"], check=False)
    else:
        _require_systemctl()
        result = subprocess.run(["systemctl", "--user", "status", UNIT_NAME, "--no-pager"], check=False)
    if result.returncode:
        raise SystemExit(result.returncode)


def restart():
    if manager() == "launchd":
        if not _launchd_loaded():
            raise SystemExit("hi-hive service is not installed or loaded")
        subprocess.run(["launchctl", "kickstart", "-k", f"{_domain()}/{LABEL}"], check=True)
    else:
        _require_systemctl()
        if not systemd_path().exists():
            raise SystemExit("hi-hive systemd user service is not installed")
        subprocess.run(["systemctl", "--user", "restart", UNIT_NAME], check=True)
    print(f"Restarted hi-hive ({manager()})")


def _install_launchd(port):
    logs = paths.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    environment = {
        "PORT": str(port),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "HIVE_SUPERVISOR": "launchd",
    }
    if os.environ.get("HIVE_HOME"):
        environment["HIVE_HOME"] = os.environ["HIVE_HOME"]
    plist = {
        "Label": LABEL,
        "ProgramArguments": [_executable(), "serve", "--shell-env"],
        "WorkingDirectory": str(paths.hive_home()),
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": True,
        "Umask": 0o077,
        "StandardOutPath": str(logs / "server.log"),
        "StandardErrorPath": str(logs / "server.err"),
    }
    paths.hive_home().mkdir(parents=True, exist_ok=True)
    path = launchd_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as file:
        plistlib.dump(plist, file)
    print(f"Wrote {path}")
    _launchd_bootout()
    subprocess.run(["launchctl", "bootstrap", _domain(), str(path)], check=True)
    print(f"Started {LABEL} on port {port} (launchd)")
    print(f"Logs: tail -f {logs / 'server.log'} {logs / 'server.err'}")


def _uninstall_launchd():
    if _launchd_loaded():
        _launchd_bootout()
        print(f"Stopped {LABEL}")
    path = launchd_path()
    if path.exists():
        path.unlink()
        print(f"Removed {path}")


def _systemd_quote(value):
    """Quote a non-command unit value; Environment= does not expand dollar signs."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def _systemd_path(value):
    """An absolute path using systemd unit-file escapes, not shell quoting."""
    escaped = []
    for byte in os.fsencode(value):
        character = chr(byte)
        if character.isascii() and (character.isalnum() or character in "/._-"):
            escaped.append(character)
        elif character == "%":
            escaped.append("%%")
        else:
            escaped.append(f"\\x{byte:02x}")
    return "".join(escaped)


def _systemd_working_directory(value):
    """Serialize WorkingDirectory using systemd runtime syntax, which keeps spaces raw."""
    path = os.fspath(value)
    if not os.path.isabs(path):
        raise SystemExit("The hi-hive working directory must be an absolute path")
    if any(ord(character) < 0x20 or ord(character) == 0x7f for character in path):
        raise SystemExit("The hi-hive working directory contains an unsupported control character")
    if "\\" in path or '"' in path:
        raise SystemExit("The hi-hive working directory contains a backslash or double quote unsupported by systemd")
    if path[-1:].isspace():
        raise SystemExit("The hi-hive working directory cannot end with whitespace")
    return path.replace("%", "%%")


def _require_systemctl():
    if not shutil.which("systemctl"):
        raise SystemExit("systemctl is required for the experimental Linux user service; run `hihive serve` instead.")


def _systemd_unit(port):
    environment = [f"PORT={port}", "HIVE_SUPERVISOR=systemd"]
    if os.environ.get("HIVE_HOME"):
        environment.append(f"HIVE_HOME={os.environ['HIVE_HOME']}")
    lines = [
        "[Unit]",
        "Description=hi-hive coding agent orchestrator",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={_systemd_path(_executable())} serve --shell-env",
        f"WorkingDirectory={_systemd_working_directory(paths.hive_home())}",
        *(f"Environment={_systemd_quote(item)}" for item in environment),
        "UMask=0077",
        "KillMode=control-group",
        "Restart=always",
        "RestartSec=1",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def _ensure_private_hive_home():
    # exist_ok preserves an existing directory's mode but still rejects a file.
    paths.hive_home().mkdir(parents=True, mode=0o700, exist_ok=True)


def _install_systemd(port):
    _require_systemctl()
    _ensure_private_hive_home()
    path = systemd_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_systemd_unit(port), encoding="utf-8")
    os.chmod(path, 0o600)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", UNIT_NAME], check=True)
    # Restart also starts an inactive unit and always applies a rewritten unit.
    subprocess.run(["systemctl", "--user", "restart", UNIT_NAME], check=True)
    print(f"Wrote {path}")
    print(f"Started {UNIT_NAME} on port {port} (experimental systemd user service)")
    print(f"Logs: journalctl --user -u {UNIT_NAME} -f")


def _uninstall_systemd():
    _require_systemctl()
    subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False)
    path = systemd_path()
    if path.exists():
        path.unlink()
        print(f"Removed {path}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
