# Agent instructions

This is a public project; do not put personal information or secrets in tracked files or files intended for commit.

Always read [KNOWN_PITFALLS.md](KNOWN_PITFALLS.md) before working on this project.
After completing a task or batch of tasks, suggest additions to that file if you discovered a non-obvious pitfall worth preserving.
Ask the user before adding them.

Before writing or editing user-facing documentation, read [DOCUMENTATION_GUIDELINES.md](DOCUMENTATION_GUIDELINES.md).
Update that file when the project's documentation audience, structure, or level-of-detail decisions change.

Use the domain language in [CONTEXT.md](CONTEXT.md).
In user-facing text, call a configured way to start Claude Code or Pi a **harness**, never a coding agent.

## Testing

During implementation, developers run focused tests only.
Do not run `./tests/run` unless the Team lead assigns final validation or a broad failure requires it.

After all changes are integrated, the Team lead assigns one validation owner to run `./tests/run` once on the integrated branch.
The Team lead must verify that result before reporting completion.
Delegate long validation runs so the Team lead remains available to the user.

Let Ubuntu CI run in the background; wait for it only before merging or when investigating a CI failure.

`./tests/run` runs the deterministic Python and UI tests.
Real-harness tests are skipped unless `HIVE_LIVE_SMOKE=1` is set.

When changing harness communication, steering, session lifecycle, or conversation storage, also run:

```sh
HIVE_LIVE_SMOKE=1 uv run pytest tests/test_live_smoke.py
```

These tests use real Pi and Claude Code processes, require existing authentication, and spend model tokens.
Run required live tests once on the final integrated branch.
Repeat them only when later changes affect harness communication, steering, session lifecycle, or conversation storage, or when investigating a failure.
They check steering during active work and follow-up recall in the same process.
They exercise `LiveRunner` directly, so keep the storage, API, CLI, and UI tests in the validation too.

Before running live tests, read [LIVE_STEERING.md](LIVE_STEERING.md#tests) for isolation, timeouts, and command/model overrides.
The default live tests use `pi` and `claude` from PATH, not the wrappers configured in `config.json`.
Report each harness's result explicitly; skipped or failed live tests are not a passing live validation.
If authentication or executables are unavailable, report the blocker rather than silently skipping the tests.
