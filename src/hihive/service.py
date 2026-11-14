"""Run the server as a macOS LaunchAgent: started at login and restarted if it crashes."""

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import paths

LABEL = "com.hihive"
PLIST_PATH = Path("~/Library/LaunchAgents").expanduser() / f"{LABEL}.plist"


def _domain():
    return f"gui/{os.getuid()}"


def _loaded():
    result = subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True)
    return result.returncode == 0


def _bootout():
    if not _loaded():
        return
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], check=False)
    # Wait until launchd has torn the service down; bootstrapping too early
    # fails with "Bootstrap failed: 5: Input/output error".
    for _ in range(20):
        if not _loaded():
            return
        time.sleep(0.3)


def _executable():
    """The installed hihive command that launchd should run."""
    found = shutil.which("hihive")
    if found:
        return os.path.realpath(found)
    candidate = Path(sys.argv[0]).resolve()
    if candidate.name in ("hihive", "hih") and candidate.exists():
        return str(candidate)
    raise SystemExit("Cannot find the installed 'hihive' command. Install it first: uv tool install .")


def install(port=None):
    if sys.platform != "darwin":
        raise SystemExit("hihive service supports macOS launchd only; run `hihive serve` instead.")
    port = port or int(os.environ.get("PORT") or paths.DEFAULT_PORT)
    logs = paths.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    environment = {
        "PORT": str(port),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    }
    if os.environ.get("HIVE_HOME"):
        environment["HIVE_HOME"] = os.environ["HIVE_HOME"]
    plist = {
        "Label": LABEL,
        # launchd gives services a minimal environment. --shell-env makes the
        # server load the user's login shell environment, so agents and output
        # actions find the same commands and variables as in a terminal.
        "ProgramArguments": [_executable(), "serve", "--shell-env"],
        "WorkingDirectory": str(paths.hive_home()),
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(logs / "server.log"),
        "StandardErrorPath": str(logs / "server.err"),
    }
    paths.hive_home().mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PLIST_PATH, "wb") as file:
        plistlib.dump(plist, file)
    print(f"Wrote {PLIST_PATH}")
    _bootout()
    subprocess.run(["launchctl", "bootstrap", _domain(), str(PLIST_PATH)], check=True)
    print(f"Started {LABEL} on port {port}")
    print()
    print("Logs:")
    print(f"  tail -f {logs / 'server.log'} {logs / 'server.err'}")
    print()
    print("Restart after code changes:")
    print(f"  launchctl kickstart -k {_domain()}/{LABEL}")
    print()
    print("The server loads your shell's environment when it starts; restart it after changing your shell setup.")
    print("Uninstall:")
    print("  hihive service uninstall")


def uninstall():
    if _loaded():
        _bootout()
        print(f"Stopped {LABEL}")
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
        print(f"Removed {PLIST_PATH}")
