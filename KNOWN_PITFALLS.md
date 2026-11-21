# Things not to break

- **Never put backticks inside double-quoted shell messages.** Bash executes backticks as command substitution, even inside double quotes.
  An agent sending a review message with backticks around `hihive service uninstall` accidentally executed that command and stopped Hive and its agents.
  Pass message text as a literal argument without shell interpretation, or use safe shell quoting such as a single-quoted heredoc.

- **Isolate child environments.** Variables set by Hive's own runtime (formerly Ruby/Bundler, now uv/virtualenv) leaked into agents and broke tools they launched.
  Remove them in `child_process_env` before starting agents or output actions.
- **Clear inherited Pi session environment variables.** When Hive inherits `PI_SESSION_ID` and `PI_SESSION_FILE` from a parent Pi process, remove them before launching new agents so they don't inherit the parent Pi session's identity.
- **Track agent exit separately from output-pipe closure.** A tool-started SOCKS proxy once kept the pipe open after the agent exited, leaving a follow-up pending forever.
  Check process exit independently, then briefly drain remaining output and close the pipe.
- **Test Docker wrappers, not just bare agents.** Wrappers previously lost stdin or required a terminal that Hive didn't provide.
- **One active agent process per conversation.** Launching a separate process for each follow-up allowed two processes to use the same saved session concurrently, causing races.
  Send steering messages and reviews to the existing process; wait for it to exit and be reaped before starting a replacement.
- **Space messages sent to Pi by at least one second.** When Hive sent several messages together, Pi reported accepting them but left some out of its conversation.
  The delay reduced message loss in testing, but does not guarantee delivery.
- **Don't automatically resend messages after a lost connection.** The agent may already have received and acted on them, so resending could repeat the work.
  Reuse the original message ID when retrying an HTTP request so Hive recognizes a duplicate submission.
- **Don't close Claude just because it finished replying.** Background commands and Monitor can still be running; check for that work before closing an idle process.
  Hive previously rejected background-completion replies because they weren't tied to a user message; display those replies too.
- **Show actual failures without filling chat with debug details.** Errors previously left follow-ups pending or appeared only in the browser's network tab.
  Show the error in the UI and CLI, preserve unsent text, and keep routine technical events in logs.
- **Display agent output as text, not executable HTML.** Agents may print HTML examples or attack payloads; decoding `&lt;` must not turn them into real HTML elements.
  Action buttons must run only configured commands after a click, passing validated values as arguments rather than inserting them into shell commands.
- **The launchd service starts with an almost empty environment.** Without the variables a terminal has, output actions failed (for example, Ruby tools could not find their gems without `GEM_HOME`).
  The service runs `hihive serve --shell-env`, which loads the login shell's environment once at startup and passes it to agents and output actions.
  Start agents directly, not through a shell: text that shell startup files print would mix with the agent's protocol output on stdout and break it.
- **Listening on localhost and checking Host and Origin don't stop local programs.** On Docker Desktop for Mac (tested), a container reaches Hive through `host.docker.internal`, Hive sees a local connection, and the container can fake both headers.
  Other setups can do the same, for example a container started with `--network host` on Linux.
  Every API request must carry the secret from `~/.hihive/token` (see `docs/adr/0002-local-authentication.md`).
- **The launchd service runs the editable checkout, and `KeepAlive` restarts it with whatever code is checked out.** A restart after a state schema change made the server refuse the old `state.json` and fail again on every restart.
  Before changing the schema, stop the service with `launchctl bootout`, back up and migrate the file, then start the service again.
- **New API routes refuse agent tokens by default** (`run(handler)` in `src/hihive/server.py`). Set `agents=True` only after the route limits the request to the agent's project with `task_id_for` or `project_for`.
  Otherwise an agent can read or change tasks in other projects if it learns their IDs (an IDOR between projects); see `docs/adr/0003-projects-and-scoped-tokens.md`.
- **Build backups from a fixed file allowlist, never by copying the whole Hive home folder.**
  Include only the intended restore files so the login token, logs, diagnostics, and temporary files stay out of the archive.
  Treat the archive as sensitive because configuration and conversations may contain credentials, source code, or personal information.
