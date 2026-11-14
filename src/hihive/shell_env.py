"""Load the user's terminal environment into a server started without one (see docs/adr/0001-agent-environment.md).

launchd starts services with a minimal environment, so agents and output
actions would miss what the user's shell sets up (PATH entries from nvm,
GEM_HOME from RVM, exported API keys, ...). Like IDEs launched from the Dock,
Hive runs the user's shell once as a login and interactive shell, which reads
all its startup files, and adopts the resulting environment.
"""

import json
import os
import pwd
import shlex
import signal
import subprocess
import sys
import uuid

TIMEOUT = 10  # seconds
# Values that describe the probe shell itself, not the user's setup.
SHELL_SESSION_VARS = ("PWD", "OLDPWD", "SHLVL", "_")
# Hive's own settings from the service definition win over the shell's values.
HIVE_VARS = ("PORT", "HIVE_HOME", "HIVE_CONFIG_FILE", "HIVE_STATE_FILE")


def user_shell():
    return os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"


def read_login_shell_env(shell=None, timeout=TIMEOUT):
    """The environment of a login + interactive shell. Raises RuntimeError on failure."""
    shell = shell or user_shell()
    marker = uuid.uuid4().hex
    dump = f"{shlex.quote(sys.executable)} -I -c 'import json, os; print(json.dumps(dict(os.environ)))'"
    # Startup files may print anything; markers separate that from the dump.
    command = f"echo {marker}; {dump}; echo {marker}"
    process = subprocess.Popen([shell, "-l", "-i", "-c", command], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise RuntimeError(f"{shell} did not finish loading within {timeout} seconds") from None
    parts = stdout.decode("utf-8", "replace").split(marker)
    if len(parts) < 3:
        detail = stderr.decode("utf-8", "replace").strip()[-300:]
        raise RuntimeError(f"{shell} exited with status {process.returncode} before printing its environment"
                           + (f": {detail}" if detail else ""))
    try:
        env = json.loads(parts[1])
    except ValueError:
        raise RuntimeError(f"{shell} printed an unreadable environment") from None
    for key in SHELL_SESSION_VARS:
        env.pop(key, None)
    return env


def adopt_login_shell_env(shell=None, timeout=TIMEOUT):
    """Merge the shell environment into this process. Returns a line for the log.

    On failure the current environment stays as it is, so Hive still starts.
    """
    shell = shell or user_shell()
    try:
        env = read_login_shell_env(shell, timeout)
    except (OSError, RuntimeError) as error:
        return f"[env] warning: could not load the {shell} login environment ({error}); using the service environment"
    kept = {key: os.environ[key] for key in HIVE_VARS if key in os.environ}
    os.environ.update(env)
    os.environ.update(kept)
    return f"[env] loaded {len(env)} variables from the {shell} login environment"
