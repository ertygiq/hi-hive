# Projects, and agents limited to their own project

Until now Hive kept one list of tasks, templates, and review sections.
Users who run Hive for unrelated work (different apps, different kinds of tasks) want that work kept apart, so the results list, search, and templates show only what belongs together.

There is also a security gap that ADR 0002 left open.
A dockerized agent can reach the `hihive` CLI through a bridge, a program on the host that runs `hihive` for the container.
The CLI on the host reads `~/.hihive/token`, so the container gets full access.
If the user also configures harnesses that run directly on the host, a dockerized agent can start one of them with `hihive add --harness <host harness>`, and that agent then works outside the sandbox.

So this ADR adds projects, and limits each agent to the project it was started in.

## Projects

A project owns its tasks, templates, review sections, working directory, and default harness.
The UI shows one project at a time; a switcher in the header changes it, and the UI remembers the last project opened.
Queue, results, and search show only the current project.
The worker limit (`max_parallel`) stays global, because it protects the machine, not a project.

There is no special default project.
On first start Hive creates an ordinary project named "My project", which the user can rename or delete.
Hive only requires that at least one project exists.

Creating a project asks only for a name and a working directory.

## Which harnesses a project can use

Harnesses are defined once, in global settings.
Each harness has an "Available in" setting, stored as `only`:

- `null` (the default): available in all projects; such a harness is called shared.
- a list of project IDs: available only in those projects.

Each project has one switch, "Use shared harnesses", stored as `use_shared_harnesses` (default `true`).

A harness is available in a project when the harness lists that project, or when the harness is shared and the project uses shared harnesses.

For the common case nothing needs configuring: every harness is shared, every project uses shared harnesses.
For a sandboxed project, the user sets the dockerized harnesses to "Only" that project and turns off "Use shared harnesses" in it.
A shared harness added later then stays out of the sandboxed project automatically, and the dockerized harnesses do not appear in other projects.

The project's default harness, the template harness, and every harness named in a request must be available in the task's project, or the server rejects the request.

Output actions use the same "Available in" setting, without a per-project switch.
They need no stronger rule because agents cannot run them (see below).

## Project-scoped tokens

When Hive starts an agent process, it creates a random token (`secrets.token_urlsafe(32)`) and passes it in the process's environment as `HIVE_TOKEN`.
Hive keeps the token only in memory, mapped to the task's project and task ID; it is never written to disk.
Hive removes the token when it sees the agent process exit, so a resumed or restarted agent gets a new one, and restarting Hive cancels all of them.
Tokens never appear in logs or runner diagnostics, and the server compares them in constant time, as it does the full token.

The server accepts either the full token from `~/.hihive/token` or a scoped token.
A scoped token allows:

- reading, starting, steering, retrying, forking, cancelling, and deleting tasks in its project, including "cancel all" limited to that project;
- harnesses and templates available in its project.

A scoped token is refused for:

- anything in another project;
- reading or changing configuration, including harnesses (changing a harness's `command` would run any program on the host);
- creating or changing templates and review sections (they set `agent_args` and `work_dir`);
- output actions, because they run host commands;
- global controls: pause, resume, restart, and reordering the queue;
- creating or deleting projects.

## CLI

When `HIVE_TOKEN` is set, the CLI uses only that token.
It never falls back to `~/.hihive/token`, and no command-line option changes the token, because a bridge passes arguments chosen by the container.

For commands that need a project, the CLI takes it from:

1. `--project <name or id>`;
2. else the `HIVE_PROJECT` variable;
3. else the token's project, when using a scoped token;
4. else the only project, if exactly one exists;
5. else it fails and lists the projects.

With a scoped token, naming another project fails.
Commands that take a task ID need no project, because task IDs are unique across projects.
When a project is named with `--project` or `HIVE_PROJECT`, the task must belong to it; otherwise the command fails and names the task's project.
A named project is an instruction, so ignoring it would hide mistakes such as a task ID from the wrong project.

## What a bridge must do

A bridge that runs `hihive` for a container must pass `HIVE_TOKEN` from the agent's environment to `hihive`, and to no other command.
If `HIVE_TOKEN` is missing or empty (for example, when the user starts the dockerized agent by hand), the bridge must refuse to run `hihive` instead of running it without the variable, which would fall back to full access.

## Considered Options

- **Projects as a UI filter only, without scoped tokens.**
  Rejected: the data is separated, but a dockerized agent could still start a host harness through the bridge.
- **Each project defines its own harnesses.**
  Rejected: most users want the same harnesses everywhere and would have to repeat them in each new project.
- **Each project stores a list of allowed harnesses, with an "opt-in" flag on harnesses that new projects leave out.**
  Rejected: it gives the user a checklist per project plus a flag with an abstract name, and a stored list silently misses harnesses added later.
  The chosen design has the same power with two settings that both default to "all".
- **One fixed token per project, stored on disk.**
  Rejected: it never expires, and it cannot tell which task started which.
- **Separate Hive instances per project.**
  Rejected: ADR 0002 shows a container can reach every Hive port on the Mac, so separate instances do not limit access, and the user loses one view of all work.
- **The CLI uses the project currently open in the UI.**
  Rejected: switching projects in the browser would silently change where terminal commands go.

## Consequences

- Inside a container, `hihive` works only when the bridge passes `HIVE_TOKEN`, and only for that agent's project.
- Agents running on the host can still read `~/.hihive/token` and get full access; scoping protects only agents that cannot read it, such as containers that do not mount `~/.hihive/`.
- A container that reaches Hive directly through `host.docker.internal` with its scoped token gets the same limited access as through the bridge, so the bridge is no longer the security boundary for Hive.
- Moving tasks between projects, and all configuration, require the full token.
- A project with "Use shared harnesses" turned off does not receive new shared harnesses; the user must set them to "Only" that project.
- `state.json` moves to `schema_version: 3`.
  Hive ships no migration from version 2 and refuses to start with an older file.
