# Setup details

## Install

Install from GitHub:

```bash
uv tool install git+https://github.com/ertygiq/hi-hive
```

This installs the `hihive` command and `hih`, the same command with a shorter name.
Use Claude Code 2.1.283 or newer, or Pi 0.87.1 or newer.

## Background service

```bash
hihive service install              # port 4567
hihive service install --port 8080  # another port
hihive service status
hihive service restart
hihive service uninstall
```

On macOS, these commands manage a launchd user service.
Logs are in `~/.hihive/logs/`.

On Linux systems with systemd, these commands manage a user service.
This setup has been tested on Ubuntu 24.04.
Other modern Linux distributions may also work, but have not been tested yet.
The unit is installed under `~/.config/systemd/user/`, starts with the user session, restarts after failure, and keeps the server and all child processes in one control group.
Read its logs with `journalctl --user -u com.hihive.service -f`.
Users who need the service before login can configure systemd user lingering separately with their system administrator.

Both service backends run the installed `hihive` executable directly and load the login shell environment once at startup, so agents and output actions can find the same commands as in a terminal.
Restart the service after changing shell setup.
The Settings restart button appears only when the server reports that launchd or systemd supervises it.

## Foreground server

Run the server in a terminal instead of as a service:

```bash
hihive serve              # http://localhost:4567
hihive serve --port 8080  # or PORT=8080 hihive serve
```

The server records its port in `~/.hihive/server.lock`, so the CLI finds it on any port.
Only one server can use a data folder at a time; a second one exits with a message.
The Settings restart button is hidden in foreground mode because no service manager would bring the process back.

## Opening from Linux or SSH

`hihive open` uses the desktop browser when one is available.
In a headless or SSH session it prints a loopback-only SSH tunnel example and the login URL.
That URL grants full hi-hive access.
Keep it private and do not paste it into logs, issues, or chat messages.
The server still binds only to `127.0.0.1`; Linux support does not expose hi-hive publicly.

## Sleep prevention

On macOS, sleep prevention uses `caffeinate`.
On Linux, sleep prevention uses `systemd-inhibit` and requires an active `systemd-logind` session.
The existing five-minute grace period applies on both platforms.

## Logging in

Every API request needs the secret hi-hive keeps in `~/.hihive/token`.
The `hihive` CLI sends it automatically.
Agents started by hi-hive get a separate token limited to their task's project instead; see [Projects](features.md#a-project-for-sandboxed-agents).
`hihive open` logs your browser in with a one-time link; a browser that isn't logged in shows a "Login required" message.
See [adr/0002-local-authentication.md](adr/0002-local-authentication.md) for why.

## Data location

hi-hive keeps its settings in `~/.hihive/config.json` and its tasks in `~/.hihive/data/`.
Set `HIVE_HOME` to use another folder.
On first start, hi-hive creates `config.json` from the defaults, and a first project named "My project" that uses the `work_dir` from `config.json` as its working directory.
Configure harnesses, projects, and templates in the settings panel (gear icon).

## Development

```bash
git clone https://github.com/ertygiq/hi-hive.git
cd hi-hive
uv tool install --editable .   # the hihive command runs your checkout
./tests/run                     # Python and UI tests
```
