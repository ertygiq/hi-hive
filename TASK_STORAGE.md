# Task storage

`data/state.json` uses `schema_version: 3`.
Each task has one authoritative `messages` list containing prompts and answers.

## Projects

`projects` maps project IDs to `{id, name, color, work_dir, use_shared_harnesses, default_harness}`.
Every task, template, and review section has a `project_id` naming one of them.
A template uses only review sections of its own project, and template names are unique within a project.
Harnesses and output actions live in `config.json`; their `only` field is `null` (all projects) or a list of project IDs.
See [adr/0003-projects-and-scoped-tokens.md](docs/adr/0003-projects-and-scoped-tokens.md).
Task settings, lifecycle timestamps, session ID, and failure details remain task fields.

## Messages

```json
{
  "id": "task-id",
  "label": "Example",
  "status": "completed",
  "session_id": "session-id",
  "messages": [
    {
      "id": "input-id",
      "role": "user",
      "kind": "initial",
      "content": "Explain this code",
      "delivery": "accepted",
      "settled": true
    },
    {
      "id": "answer-id",
      "role": "assistant",
      "kind": "initial",
      "content": "The code..."
    }
  ]
}
```

Roles are `user`, `assistant`, and `system`.
Kinds are `initial`, `steering`, `follow_up`, `continue`, `review`, and `notice`.
An assistant's kind identifies the stage that produced the answer, not an exclusive association with one input.
An initial or review answer can also address steering messages.
When the harness provides reliable input associations, the answer preserves them as `input_ids`.

Each self-review prompt and its answers share a `review_id`.
Steering during a review also carries that review ID.
Planned review prompts are stored once as user messages with `delivery: scheduled`.
On submission, a planned prompt moves to its actual position in the conversation and receives a timestamp, sequence, and delivery state.
Scheduled messages are not in-flight messages and are hidden from the output conversation until submitted.

Submitted inputs hold their own delivery facts, including `delivery`, `settled`, `harness_accepted`, `written`, and any error.
Message IDs support delivery tracking and idempotent HTTP retries.
Retrying an entire task resets its conversation, but preserves ID/hash receipts in `message_receipts` so delayed HTTP retries cannot submit old messages into the new session.
These receipts contain no prompt or answer text.

## API, UI, and CLI

`GET /api/tasks/:id` returns `messages`; the old full-detail conversation fields are removed.
`GET /api/state` still derives compact task-list fields such as `prompt`, `summary`, and `output_tail` without storing extra copies.
It returns every project's tasks, each with its `project_id`, unless `?project=ID` limits it to one; the UI filters by the current project itself.
`GET /api/projects` returns each project with its available harnesses (without commands) and its templates; an agent token sees only its own project.
The chat POST response retains its `status` receipt field for compatibility; stored messages call that field `delivery`.

The UI keeps the initial prompt in its prompt tab and displays answers, steering, reviews, and follow-ups in conversation order.
Self-review prompts and responses have explicit labels.
`hihive show` selects initial and continuation answers, `--review` selects reviews, `--followup` shows the conversation from the first additional user input onward, and `--full` shows the entire submitted conversation once.

## Diagnostics

Technical records are stored only in `data/runner-diagnostics/<task-id>.jsonl`.
The task detail API reads the latest 200 records from that journal; no diagnostic records are persisted inside tasks.
Missing journals return an empty diagnostic list.
Unreadable or malformed journals produce diagnostic errors, not fabricated conversation replies.

## Older state files

Hive loads only `schema_version: 3` state files and refuses to start with an older one.
Hive ships no migration from version 2: version 3 came while hi-hive had a single user, whose file was migrated by hand.
To upgrade a version 2 file yourself, add a project to `projects` and set its ID as `project_id` on every task, template, and review section.
On load, inputs that were still in flight become unknown and are never automatically resent.
