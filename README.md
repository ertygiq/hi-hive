# hi-hive

A web UI and CLI for running coding agents (Claude Code, Pi) in parallel: batch tasks, review results, and follow up with agents, all from one place.



If you've ever needed to run the same agent task across a dozen inputs and then review every result — you know the pain. Switching between terminal tabs, losing track of which session was which, scrolling through output trying to find the one that matters. And then a week later, trying to find that one session where the agent found something interesting? Forget it.

hi-hive gives you a single place to manage all of it. Queue up tasks, watch them run in parallel, inspect results, and have follow-up conversations with any agent — without losing your mind or your session history.

And here's the part that really excites me: there's a CLI tool. Which means your agent can use it too. Give an agent access to `hihive` and it can spawn worker agents, wait for them to finish, read their results, send follow-ups — effectively delegating work to a team of agents and coordinating them. It's like giving your agent the ability to hire teammates.

## Supported platforms

- macOS: supported.
- Linux: not tested; the code uses only Unix features, but there is no background service (run `hihive serve`).
- Windows: not supported.
- Harnesses: Claude Code 2.1.283 or newer, Pi 0.87.1 or newer.

## How It Works

**Projects** keep unrelated work apart: each project has its own tasks, templates, review sections, and working directory, and a switcher in the header changes the project you see.
See [Projects](docs/features.md#projects), including how to keep a project on sandboxed harnesses.

**Templates** define reusable prompts with an `%{input}` placeholder. Optionally attach **review sections** — self-review questions the agent answers after completing the task.

Create a batch by picking a template and providing inputs. hi-hive creates one task per input, runs them in parallel (configurable concurrency), and collects results.

Once a task completes, you can:
- Inspect the output and review answers
- Send follow-up messages (chat-style conversation with the agent)
- View the agent's full session (tool calls, thinking, etc.) — requires [claude-code-lens](https://github.com/ertygiq/claude-code-lens) to be installed separately

### Under the Hood

Each task owns a persistent Claude stream-json or Pi RPC process with a generated session ID.
You can send steering messages while the task runs without waiting for an assistant reply.
Reviews and follow-ups reuse that conversation; Hive never launches a concurrent resume command for steering.
Several inputs can share one final answer.
Idle processes shut down and are reaped before a later follow-up resumes the saved session.

See [Live steering](LIVE_STEERING.md) for supported versions, message states, visible errors, durable diagnostics, and fast/opt-in live tests.

## Setup

Prerequisites: [uv](https://docs.astral.sh/uv/), [Claude Code](https://claude.ai/claude-code) or Pi.

```bash
uv tool install git+https://github.com/ertygiq/hi-hive
hihive service install   # macOS; on Linux run `hihive serve` instead
hihive open
```

**Warning:** agents run with full access to your computer; Claude Code starts with `--dangerously-skip-permissions`.
An agent can delete files or run harmful commands, for example after reading a malicious web page.
To limit this, remove the flag in Settings (gear icon) or run the agents in a sandbox.

Ports, logs, the foreground server, data location, and development: [docs/setup.md](docs/setup.md).

## CLI

hi-hive includes a command-line client, installed together with the server as `hihive`.
`hih` is the same command with a shorter name.

```bash
hihive open                        # open the web UI and log this browser in
hihive projects                    # list projects
hihive --project NAME status       # most commands work in one project (or set HIVE_PROJECT)
hihive status                      # queue/running/completed counts
hihive templates                   # list prompt templates
hihive start <template> <input>    # start task from template
hihive start -p "prompt"           # start with raw prompt
hihive queue                       # queued + running tasks
hihive results                     # completed tasks
hihive show <id>                   # task output
hihive show <id> --review          # review sections only
hihive show <id> --followup        # follow-up messages only
hihive show <id> --full            # everything
hihive wait <id>                   # block until task completes
hihive followup <id> "message"     # send follow-up message
hihive cancel <id>                 # cancel a task
hihive retry <id>                  # retry a task
```

Task IDs can be shortened to a unique prefix (e.g. `4f63e134` instead of the full UUID).
With more than one project, commands that list or start tasks need `--project NAME` or `HIVE_PROJECT`.
Agents that Hive starts work in their own task's project automatically.

The CLI connects to the server that uses `~/.hihive` (or `HIVE_HOME`), on whatever port it runs.

## More features

- Output actions: turn a custom tag in an agent's answer into a button that runs your command.
- Task cost: a Settings toggle shows what each task cost (Pi only).

Details: [docs/features.md](docs/features.md).

## License

MIT