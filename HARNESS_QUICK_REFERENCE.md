# How harness replies reach the conversation

## Claude: one reply, two events

Claude sends one JSON object per stdout line (JSONL).
Hive parses each line and checks `type`.
Example, with unrelated fields omitted:

```jsonl
{"type":"user","uuid":"msg-1","message":{"role":"user","content":"Say hello"}}
{"type":"assistant","message":{"content":[{"type":"text","text":"Hello!"}]}}
{"type":"result","uuid":"result-1","user_message_uuids":["msg-1"],"result":"Hello!"}
```

- **Hive sends `user`:** the input has an ID.
- **Claude sends `assistant`:** Hive saves the text in `@answer`, not in the UI yet.
- **Claude sends `result`:** Hive validates the result and publishes one reply for storage and display.

The text can appear in both events.
Non-empty `result` text replaces the saved assistant text before publication.
Tool-using turns can contain several assistant events.

## What do the input IDs do?

`user_message_uuids` tells Hive which user inputs a normal Claude result covers.
Hive then:

- Marks those inputs `included` and `settled`, and records their result ID.
- Stops waiting for those inputs; when none remain outstanding, the task can complete.
- Saves the reply's `input_ids` in `data/state.json`; the UI does not use these IDs to arrange replies.

One result can cover several inputs without creating several copies of the reply.

## What changes for background completion?

Claude can report completion without another user input:

```jsonl
{"type":"result","uuid":"result-2","origin":{"kind":"task-notification"},"result":"The background sleep finished."}
```

Hive publishes that result's text once, deduplicated by result UUID, without input associations.
Hive does not settle pending user inputs because a background task finished.
Background failures also get a conversation notice.

**Process lifetime:** background work keeps Claude alive, including Monitor.
After the work and completion reply finish, Hive waits a fresh 60 seconds.
Before closing, Hive asks Claude for session state and a background-task snapshot; `idle` alone is not enough.
A failed state query keeps the process alive and shows a notice.

**UI:** `Background work` means tasks are still running; `Preparing reply` means Claude is producing the completion reply.
Claude pushes activity updates immediately; Hive does not wait for the 60-second shutdown check to show these badges.
An alive but idle process has no work badge.

**The old bug:** Hive required non-empty input IDs on every result, rejected background completions, and stopped before publishing the reply.

## How is Pi different?

Pi does not identify which inputs an assistant reply answers.
Hive saves text from `message_end`, waits for `agent_settled`, then asks Pi for its state.
When Pi reports no streaming and no queued messages, Hive publishes the saved reply and settles the acknowledged inputs covered by that check.
Pi replies have no per-input association.

## What becomes a warning or error?

- **Unknown Claude events:** diagnostics only; no chat warning.
- **Unknown Pi events:** diagnostics plus a chat warning.
- **Broken required fields or transport failure:** an explicit error, not silently ignored.
- **Fatal runner errors:** task failure details plus a `Runner stopped: ...` conversation notice.

Diagnostics: `data/runner-diagnostics/<task-id>.jsonl`.
They contain event metadata and fingerprints, not full incoming JSON or reply text.
Full details: [LIVE_STEERING.md](LIVE_STEERING.md).
