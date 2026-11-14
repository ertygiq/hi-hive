# Agent instructions

This is a public project; do not put personal information or secrets in tracked files or files intended for commit.

Always read [KNOWN_PITFALLS.md](KNOWN_PITFALLS.md) before working on this project.
After completing a task or batch of tasks, suggest additions to that file if you discovered a non-obvious pitfall worth preserving.
Ask the user before adding them.

## Testing

Run `./tests/run` for the deterministic Python and UI tests.
Real-harness tests are skipped unless `HIVE_LIVE_SMOKE=1` is set.

When changing harness communication, steering, session lifecycle, or conversation storage, also run:

```sh
HIVE_LIVE_SMOKE=1 uv run pytest tests/test_live_smoke.py
```

These tests use real Pi and Claude Code processes, require existing authentication, and spend model tokens.
They check steering during active work and follow-up recall in the same process.
They exercise `LiveRunner` directly, so keep the storage, API, CLI, and UI tests in the validation too.

Before running live tests, read [LIVE_STEERING.md](LIVE_STEERING.md#tests) for isolation, timeouts, and command/model overrides.
The default live tests use `pi` and `claude` from PATH, not the wrappers configured in `config.json`.
Report each harness's result explicitly; skipped or failed live tests are not a passing live validation.
If authentication or executables are unavailable, report the blocker rather than silently skipping the tests.
