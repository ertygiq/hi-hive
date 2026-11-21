"""Install and manage hi-hive as a per-user background service."""

import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
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


def available():
    selected = manager()
    if selected == "launchd":
        return bool(shutil.which("launchctl") and subprocess.run(
            ["launchctl", "print", _domain()], capture_output=True, check=False).returncode == 0)
    if not shutil.which("systemctl"):
        return False
    return subprocess.run(["systemctl", "--user", "show-environment"],
                          capture_output=True, check=False).returncode == 0


def _logs_hint():
    if manager() == "launchd":
        return f"Logs: {paths.logs_dir() / 'server.log'} and {paths.logs_dir() / 'server.err'}"
    return f"Logs: journalctl --user -u {UNIT_NAME} -f"


def _server_config(timeout=0.5):
    token = paths.read_token()
    if not token:
        return None
    request = urllib.request.Request(paths.server_url().rstrip("/") + "/api/config")
    request.add_header("Origin", paths.server_url())
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            import json
            return json.loads(response.read())
    except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError):
        return None


def _wait_for_health(selected, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        config = _server_config()
        if config and config.get("service_manager") == selected:
            return True
        time.sleep(0.25)
    return False


def _cleanup_failed_service(selected):
    try:
        if selected == "launchd":
            _uninstall_launchd(quiet=True)
        else:
            _uninstall_systemd(quiet=True)
    except (OSError, subprocess.CalledProcessError):
        pass


def install(port=None):
    selected = manager()
    if not available():
        detail = "no systemd user session" if selected == "systemd" else "no logged-in launchd GUI session"
        raise SystemExit(f"The background service is not available here ({detail}).\n"
                         "Run hi-hive in a terminal instead:  hihive serve")
    port = port or int(os.environ.get("PORT") or paths.DEFAULT_PORT)
    current = _server_config()
    if current and current.get("service_manager") not in ("launchd", "systemd"):
        running_port = paths.server_url().rsplit(":", 1)[-1]
        raise SystemExit(f"hi-hive is already running in a terminal (port {running_port}). "
                         "Stop it there with Ctrl-C, then run this again.")
    try:
        if selected == "launchd":
            _install_launchd(port)
        else:
            _install_systemd(port)
    except subprocess.CalledProcessError as error:
        _cleanup_failed_service(selected)
        raise SystemExit(f"Could not start the background service ({selected}, exit {error.returncode}).\n"
                         f"{_logs_hint()}\nRun hi-hive in a terminal instead:  hihive serve") from None
    if not _wait_for_health(selected):
        _cleanup_failed_service(selected)
        raise SystemExit(f"The background service started, but hi-hive did not answer on port {port} within 10 s.\n"
                         f"{_logs_hint()}\n"
                         "Run hi-hive in a terminal instead:  hihive serve")
    print(f"hi-hive is running in the background on http://localhost:{port} and starts at login.")
    print("Open it:  hihive open")
    print(_logs_hint())


def uninstall():
    if manager() == "launchd":
        _uninstall_launchd()
    else:
        _uninstall_systemd()
    print(f"hi-hive is no longer running and will not start at login. Your data in {paths.hive_home()} is kept.")
    print("Start it yourself:            hihive serve")
    print("Or turn the service back on:  hihive service install")


def status():
    selected = manager()
    if not available():
        raise SystemExit("Background service: unavailable. Run hi-hive in a terminal with:  hihive serve")
    installed = launchd_path().exists() if selected == "launchd" else systemd_path().exists()
    if not installed:
        print(f"Background service: not installed ({selected})")
        return
    if selected == "launchd":
        running = _launchd_loaded()
    else:
        running = subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT_NAME], check=False).returncode == 0
    config = _server_config() if running else None
    state = "running" if config and config.get("service_manager") == selected else ("starting" if running else "stopped")
    port = paths.server_url().rsplit(":", 1)[-1] if config else "-"
    print(f"Background service: installed, {state}, port {port} ({selected})")


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


def _service_directory_root(directory):
    directory = Path(directory)
    home = Path.home()
    if directory == home or home in directory.parents:
        return home
    configured = os.environ.get("XDG_CONFIG_HOME")
    if configured:
        root = Path(configured)
        if not root.is_absolute():
            raise SystemExit(f"XDG_CONFIG_HOME must be absolute: {root}")
        if directory == root or root in directory.parents:
            return root
    raise SystemExit(f"Service directory must be inside HOME or XDG_CONFIG_HOME: {directory}")


def _validate_service_directory(directory, create=False):
    directory = Path(directory)
    root = _service_directory_root(directory)
    home = Path.home()
    external = root != home
    anchor = Path(directory.anchor) if external else home
    current = directory
    while True:
        if current.is_symlink():
            raise SystemExit(f"Service directory must not contain symbolic links: {current}")
        if current.exists():
            details = current.stat()
            if not stat.S_ISDIR(details.st_mode):
                raise SystemExit(f"Service directory path is not a directory: {current}")
            inside_managed_root = current == root or root in current.parents
            writable = bool(details.st_mode & 0o022)
            root_owned_sticky = details.st_uid == 0 and bool(details.st_mode & stat.S_ISVTX)
            if writable and (inside_managed_root or not external or not root_owned_sticky):
                raise SystemExit(f"Service directory must not be group/world-writable: {current}")
            allowed_owners = (os.getuid(),) if inside_managed_root or not external else (0, os.getuid())
            if details.st_uid not in allowed_owners:
                owner = "the current user" if len(allowed_owners) == 1 else "the current user or root"
                raise SystemExit(f"Service directory must be owned by {owner}: {current}")
        if current == anchor:
            break
        current = current.parent
    if create:
        previous_umask = os.umask(0o077)
        try:
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        finally:
            os.umask(previous_umask)
        _validate_service_directory(directory)


def _validate_service_target(path):
    path = Path(path)
    _validate_service_directory(path.parent)
    if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.stat().st_mode)):
        raise SystemExit(f"Service file must be a regular file, not a symlink or special file: {path}")
    if path.exists():
        target = path.stat()
        if target.st_uid != os.getuid() or target.st_mode & 0o022:
            raise SystemExit(f"Service file must be user-owned and not group/world-writable: {path}")


def _atomic_service_file(path, content):
    path = Path(path)
    _validate_service_directory(path.parent, create=True)
    _validate_service_target(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


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
    _atomic_service_file(path, plistlib.dumps(plist))
    print(f"Wrote {path}")
    _launchd_bootout()
    subprocess.run(["launchctl", "bootstrap", _domain(), str(path)], check=True)


def _uninstall_launchd(quiet=False):
    path = launchd_path()
    _validate_service_target(path)
    if _launchd_loaded():
        _launchd_bootout()
        if not quiet:
            print(f"Stopped {LABEL}")
    if path.exists():
        path.unlink()
        if not quiet:
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
        raise SystemExit("systemctl is required for the Linux user service; run `hihive serve` instead.")


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
    _atomic_service_file(path, _systemd_unit(port).encode())
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", UNIT_NAME], check=True)
    # Restart also starts an inactive unit and always applies a rewritten unit.
    subprocess.run(["systemctl", "--user", "restart", UNIT_NAME], check=True)
    print(f"Wrote {path}")


def _uninstall_systemd(quiet=False):
    _require_systemctl()
    path = systemd_path()
    _validate_service_target(path)
    subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False)
    if path.exists():
        path.unlink()
        if not quiet:
            print(f"Removed {path}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
