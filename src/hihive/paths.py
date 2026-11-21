"""Locations of Hive's configuration and data."""

import fcntl
import os
import secrets
import shutil
from pathlib import Path

DEFAULT_PORT = 4567
PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
DEFAULT_CONFIG = PACKAGE_DIR / "default_config.json"


def hive_home() -> Path:
    """Folder holding config.json, data/ and logs/. HIVE_HOME overrides it."""
    return Path(os.environ.get("HIVE_HOME") or "~/.hihive").expanduser()


def config_file() -> Path:
    override = os.environ.get("HIVE_CONFIG_FILE")
    return Path(override).expanduser() if override else hive_home() / "config.json"


def state_file() -> Path:
    override = os.environ.get("HIVE_STATE_FILE")
    return Path(override).expanduser() if override else hive_home() / "data" / "state.json"


def token_file() -> Path:
    """The secret that every API request must carry (see docs/adr/0002-local-authentication.md)."""
    return hive_home() / "token"


def read_token() -> str | None:
    try:
        return token_file().read_text().strip() or None
    except OSError:
        return None


def ensure_token() -> str:
    """Return the API secret, creating it readable only by the user on first start."""
    token = read_token()
    if token:
        return token
    path = token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(token + "\n")
    return token


def logs_dir() -> Path:
    return hive_home() / "logs"


def installer_state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser()
    return base / "hi-hive"


def installer_record_file() -> Path:
    return installer_state_dir() / "install.json"


def ensure_config(path: Path) -> None:
    """Create the config file from the packaged defaults on first start."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DEFAULT_CONFIG, path)


def server_lock_file() -> Path:
    return hive_home() / "server.lock"


def lock_home(port: int):
    """Hold an exclusive lock on the home folder while this server runs; return the open lock file.

    Two servers on one folder would overwrite each other's task data.
    The file also records the server's port, so the CLI finds the server without its own config.
    The OS releases the lock when the process exits, even after a crash.
    The caller must keep the returned file open.
    """
    home = hive_home()
    home.mkdir(parents=True, exist_ok=True)
    file = open(server_lock_file(), "a+", encoding="utf-8")
    try:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        file.seek(0)
        owner = file.read().strip() or "unknown"
        file.close()
        raise SystemExit(
            f"Another hi-hive server already uses {home} (port {owner}). "
            "Stop it first, or set HIVE_HOME to use another folder."
        ) from None
    file.seek(0)
    file.truncate()
    file.write(f"{port}\n")
    file.flush()
    return file



def server_url() -> str:
    """The URL of the server that uses this home folder, from the port it recorded at startup."""
    try:
        port = int(server_lock_file().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        port = DEFAULT_PORT
    return f"http://localhost:{port}"
