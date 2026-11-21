# Install hi-hive

## One-line install

The installer supports macOS and Ubuntu-tested Linux.
It installs only for the current user, refuses root, and never uses `sudo`.

```sh
curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/ertygiq/hi-hive/main/install.sh | sh
```

To inspect the installer before running it:

```sh
curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/ertygiq/hi-hive/main/install.sh -o install-hi-hive.sh
less install-hi-hive.sh
sh install-hi-hive.sh
```

The one-line command downloads the current installer from `main` and immediately runs it as your user.
HTTPS protects the download in transit, but you still trust the repository and download host to provide the installer script.
The installer script runs before it can resolve the exact package commit.
The exact `main@SHA` protects the hi-hive source archive, not the already-running installer script.
To review the script first, use the download, inspect, and run commands above, or use the manual uv instructions below.

After the installer starts, it resolves `main` once to an exact 40-character commit SHA, shows that SHA, and installs only that immutable source archive.
Until versioned releases exist, the displayed identity has two parts: package version `0.2.0` and source `main@SHA`.

## What the installer does

The installer:

1. Checks the operating system, CPU, current user, home directory, and background-service availability.
2. Reuses a compatible `uv` already installed for the user.
3. If `uv` is missing, downloads a fixed uv release from GitHub, verifies its built-in SHA-256, and installs standard `uv` and `uvx` in the normal user executable directory.
4. Uses `uv` to select Python 3.14.x.
5. Reuses a suitable system or uv-managed Python when possible, or lets uv download a managed Python 3.14 without changing the system Python.
6. Installs hi-hive into uv's configured tool directory and puts `hihive` and `hih` in uv's configured tool-bin directory.
7. Detects supported Claude Code and Pi executables in the same login-shell environment used by the background service.
8. Configures only detected commands that meet the supported minimum versions.
9. Optionally installs a per-user launchd or systemd user service and waits for authenticated health before offering to open the browser.

The installer never installs Claude Code or Pi, checks their authentication, or copies their credentials.
Claude Code 2.1.283 or newer and Pi 0.87.1 or newer are supported.
If neither is available, hi-hive still starts, but it cannot run tasks.
After installing Claude Code or Pi, rerun the installer to add it, or add it manually in Settings.

## Questions and non-interactive defaults

In an interactive terminal, the installer asks only when a choice is needed.

- If uv's tool-bin directory is absent from `PATH`, adding a marked reversible profile block defaults to yes.
- When a background service is available, background mode defaults to the recommended choice.
- On a rerun, a newly found supported harness type is offered without changing current harness settings.
- After a background service passes health checks on a desktop, opening the browser defaults to yes.

When output is not a terminal, or with `--plain`, the installer asks no questions.
It adds the PATH block and selects background mode when available unless `--no-modify-path` or `--no-service` is set.
It never opens a browser in plain mode.
Plain reruns report newly found harnesses but never add them.

## Flags

```text
--yes                 Accept recommended defaults without questions
--plain               Use plain non-interactive output
--no-modify-path      Do not edit shell startup files
--no-service          Install without a background service
--port PORT           Use another server port
--force, --repair     Reinstall even when the recorded commit is current
--uninstall           Remove the service, tool, PATH block, and install record
--help                Show installer help
```

The installer validates ports from 1 through 65535.
It uses private temporary directories and removes them on success, failure, or interruption.

## PATH changes

The installer uses the tool-bin path reported by `uv tool dir --bin`.
It uses absolute command paths during the current run, so declining the PATH change does not break installation.

When accepted, the installer adds exactly one marked block to the startup file for zsh, bash, or POSIX sh:

```sh
# >>> hi-hive installer PATH >>>
...
# <<< hi-hive installer PATH <<<
```

Rerunning the installer does not duplicate the block.
Uninstall removes only that exact installer-owned block.
The installer refuses partial, duplicated, or symbolic-link profile targets instead of guessing how to edit them.

## Updates and repair

Run the same one-line command again to check `main`.
The installer compares the newly resolved exact commit SHA with its recorded SHA.
If the SHA is unchanged and the command exists, it skips package installation.
Use `--repair` or `--force` to reinstall the same SHA.

Before an update, the installer copies only `config.json` and `data/state.json` into a timestamped folder under the hi-hive home directory.
It never backs up or prints the login token.
Existing settings, projects, task data, logs, and user edits are preserved.
Existing harness entries are never removed, renamed, reordered, or changed automatically.
An interactive rerun may offer to add a harness for a newly detected supported Claude Code or Pi command; plain reruns do not change harness settings.
Custom commands remain configured and are not executed by detection.
If a background service fails its authenticated health check during an update, the installer stops that service, restores the previous package and saved configuration state, and reports any rollback failure.
If hi-hive is running in a terminal during an update, the installer leaves that process alone and tells you to stop it before starting the new version.

## Run in a terminal instead of the background

Choose terminal mode in the installer or pass `--no-service`.
Then use two terminals:

```sh
hihive serve
```

Keep that terminal open.
Closing it or pressing Ctrl-C stops hi-hive.
In a second terminal, run:

```sh
hihive open
```

To switch to background mode later, first stop `hihive serve` with Ctrl-C, then run:

```sh
hihive service install
```

Linux service installation requires a working systemd user session.
The installer never enables systemd lingering automatically.
See [setup details](setup.md) for SSH and service logs.

## Manual installation with uv

If you prefer to manage uv and the service yourself:

```sh
uv tool install --python 3.14 git+https://github.com/ertygiq/hi-hive
hihive setup-harnesses --fresh
hihive service install
hihive open
```

For a reproducible manual install, replace the source with an exact commit SHA:

```sh
uv tool install --python 3.14 https://github.com/ertygiq/hi-hive/archive/COMMIT_SHA.tar.gz
```

## Uninstall

Download the installer, inspect it if desired, and run:

```sh
curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/ertygiq/hi-hive/main/install.sh -o install-hi-hive.sh
less install-hi-hive.sh
sh install-hi-hive.sh --uninstall
```

Uninstall removes the managed background service, the uv tool, the installer-owned PATH block, and the install record.
It keeps `~/.hihive`, or the directory selected by `HIVE_HOME`, including settings, tasks, logs, and the login token.
Delete that data separately only when you are certain it is no longer needed.

The installer does not remove uv because uv may be used by other applications.
