# Claude background-state findings

## What Claude reports

Tested with Claude Code **2.1.283**, using the first-party Agent SDK **0.3.278** type definitions as the protocol reference.

- An `initialize` control request returns `response.session_state` and pending permission/dialog requests.
- After the response, Claude sends `background_tasks_changed` with the full current task list.
- `session_state: idle` does **not** mean no background work: the live test returned idle while `sleep 15` was still running.
- `background_tasks_changed.tasks` replaces the previous list; it is not a list of additions.
- Monitor uses this same list: the tested command monitor appeared as `task_type: local_bash`.
- Monitor completion and explicit TaskStop both removed the monitor from the list.

The SDK also declares `session_state_changed` events, but the tested CLI did not emit them in these runs.
Hive handles them when present and does not depend on them being emitted.

## Shutdown rule

Keep the process alive while background tasks exist or Claude is producing a reply.
When background work ends or a reply finishes, restart the idle timer.
After the timer expires, request state and wait for both the response and its following task snapshot.
Close only when the session is idle, there are no pending permission/dialog requests, the task list is empty, and no newer activity invalidated the query.

A failed or unanswered query disables automatic idle shutdown for that process and produces one visible notice.
Explicit stop still works.
All listed tasks keep the process alive, including `ambient` tasks: hiding an activity indicator is not evidence that killing the task is safe.

## Evidence and limits

- [First-party SDK 0.3.278 package](https://www.npmjs.com/package/@anthropic-ai/claude-agent-sdk/v/0.3.278): `sdk.d.ts`, `Query.reinitialize`, `SDKBackgroundTasksChangedMessage`, `SDKSessionStateChangedMessage`, and `SDKControlRequestInner`.
- [Published type definitions](https://unpkg.com/@anthropic-ai/claude-agent-sdk@0.3.278/sdk.d.ts): background snapshots document replacement semantics and the snapshot after repeated initialization.
- [Monitor tool schema](https://unpkg.com/@anthropic-ai/claude-agent-sdk@0.3.278/sdk-tools.d.ts): `MonitorInput` describes command exit and timeout behavior.
- Reproducible live checks: `tests/test_live_background.py` covers background Bash, Monitor completion, and explicit Monitor stop, with a one-second idle timeout and longer-lived work.

No general `get_state` control request was found in this SDK's declared request union.
The `background_tasks` control request is **not a query**: it moves foreground work into the background.
The tests cover command-based Monitor, not WebSocket monitors, scheduled wakeups, or externally arriving work.
A state snapshot is not an atomic promise that no new work can arrive afterward.
