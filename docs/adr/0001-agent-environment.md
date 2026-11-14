# Agents and output actions get the user's terminal environment

Processes that Hive starts (agents, their version checks, and output actions) must behave as if the user started them in a new terminal window.
The launchd service starts with only the variables macOS gives every session (`HOME`, `USER`, `LOGNAME`, `SHELL`, `TMPDIR`, a basic `PATH`, `SSH_AUTH_SOCK`), so tools failed, for example Ruby tools without `GEM_HOME`.
A terminal starts from those same session variables, and then the shell's startup files (`~/.zprofile`, `~/.zshrc`, `~/.zlogin`) add the rest.

So the service runs `hihive serve --shell-env`: at startup, Hive runs `$SHELL -l -i -c` once, reads the environment that shell ends up with, and adds it to its own.
Hive then removes only the variables that its own startup adds (Python and uv variables, and Pi session variables when Hive itself runs inside Pi), in `child_process_env` in `src/hihive/pool.py`.

## Considered Options

- **Save `PATH` when the service is installed.**
  Rejected: the copy goes stale when the user's setup changes, and it covers only `PATH`, not variables like `GEM_HOME`.
- **Start every agent and action through a login shell.**
  Rejected: text that startup files print would mix with the agent's protocol output on stdout and break it, and every start would pay the shell's load time.
- **Start the shell from a fixed list of session variables instead of Hive's own environment, so no remove-list is needed.**
  Rejected: macOS has no command that returns the environment a new terminal starts with, so Hive would have to hard-code that list, and macOS could change it without us noticing.
  The remove-list names only what Hive's own startup adds, which changes only when we change how Hive runs.

## Consequences

- Changes to the user's shell setup reach agents only after the service restarts.
- A variable exported by hand in one terminal does not reach agents, just as it would not reach a new terminal window.
- If the shell fails or takes longer than 10 seconds, Hive starts with its own environment and logs a warning.
