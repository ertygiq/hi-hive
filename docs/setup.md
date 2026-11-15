# Setup details

## Install

Install from GitHub:

```bash
uv tool install git+https://github.com/ertygiq/hi-hive
```

This installs the `hihive` command and `hih`, the same command with a shorter name.

## Background service (macOS)

```bash
hihive service install              # port 4567
hihive service install --port 8080  # another port
hihive service uninstall
```

The service starts at login and restarts if it crashes.
When it starts, it loads your login shell's environment (all your shell startup files), so agents and output actions find the same commands and variables as in your terminal.
Restart the service after changing your shell setup: `launchctl kickstart -k gui/$(id -u)/com.hihive`.
Logs are in `~/.hihive/logs/`.

## Foreground server

Run the server in a terminal instead of as a service (the only way on Linux):

```bash
hihive serve              # http://localhost:4567
hihive serve --port 8080  # or PORT=8080 hihive serve
```

The server records its port in `~/.hihive/server.lock`, so the CLI finds it on any port.
Only one server can use a data folder at a time; a second one exits with a message.

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
