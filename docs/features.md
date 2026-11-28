# More features

## Projects

A project groups tasks, templates, review sections, and a working directory.
The switcher next to the hi-hive name changes the project you see; queue, results, and search show only that project.
The worker limit is shared, so the Workers panel also says how many tasks run in other projects.

On first start hi-hive creates one project, "My project".
"+ New project…" in the switcher asks only for a name and a working directory.
Project settings (name, working directory, harnesses, templates, review sections) are in the "Project" tab of Settings; what all projects share is in the "All projects" tab.

### Which harnesses a project can use

Harnesses are defined once, under Settings → All projects.
Each harness has an "Available in" setting: "All projects" (the default; such a harness is called shared) or "Only selected projects".
Each project has a "Use shared harnesses" switch, on by default.
A harness is available in a project when the harness selects that project, or when the harness is shared and the project uses shared harnesses.

So nothing needs configuring when every project should use every harness.

### A project for sandboxed agents

To keep a project on sandboxed harnesses, for example agents wrapped in Docker:

1. Add the sandboxed harnesses and set their "Available in" to "Only selected projects", with only that project ticked.
2. In that project's settings, turn off "Use shared harnesses".

The project then offers only the sandboxed harnesses, a shared harness added later stays out of it, and the sandboxed harnesses do not appear in other projects.

Every agent that hi-hive starts gets its own token in the `HIVE_TOKEN` environment variable.
The token works only for that task's project and stops working when the agent process exits.
With it, an agent can list, start, steer, and manage tasks in its own project, using only that project's harnesses.
It cannot see other projects, read or change settings, or run output actions.
When `HIVE_TOKEN` is set, the `hihive` CLI uses only that token and never falls back to `~/.hihive/token`.

If a sandboxed agent reaches `hihive` through a program that runs it on the host for the container (a bridge), the bridge must pass the agent's `HIVE_TOKEN` to `hihive`.
When the variable is missing or empty, the bridge must refuse to run `hihive`; otherwise `hihive` would use the full token.
Agents running directly on the host can read `~/.hihive/token`, so this limit protects only agents that cannot read that file, such as containers that do not mount `~/.hihive/`.
See [adr/0003-projects-and-scoped-tokens.md](adr/0003-projects-and-scoped-tokens.md) for the design.

## Output actions

Output actions turn a custom tag in an agent's answer into a button.
When you click the button, hi-hive runs a command from your config with the tag's value.
Nothing runs until you click, and a successful run shows nothing; hi-hive shows a message only when the command fails.

Add actions to `~/.hihive/config.json` (there is no Settings UI for them yet) and restart hi-hive:

```json
"output_actions": [
  {
    "tag": "run-id",
    "label": "Open run",
    "pattern": "[a-z0-9]{4}",
    "command": "/path/to/your-tool",
    "args": ["show", "{value}"]
  }
]
```

Then tell the agent, for example in a template, to print the value as `<run-id>ab12</run-id>`.

- `tag`: the tag name, using lowercase letters, digits and dashes.
- `pattern`: a regular expression the whole value must match. hi-hive rejects any other value.
- `command` and `args`: what hi-hive runs. `{value}` in an argument is replaced by the tag's value. hi-hive runs the command directly, without a shell.
- `label`: the tooltip on the button.
- `only` (optional): a list of project IDs; the button then appears only in those projects. You can also set this in Settings → All projects → Output actions.

Agent output is untrusted, for example when the agent reads web pages, so keep `pattern` strict.
Tags inside code blocks and inline code stay as text.
A command that runs longer than 60 seconds is left running and counts as started.

## Import a session

In Add task, choose **Import a session**, select a native Pi or Claude Code harness, and enter the exact session ID.
Review the preview and label before importing.
The imported task appears in Results, and you can send follow-ups or fork it.
Close the session in other terminals before sending a follow-up.
Deleting the imported Hive task never deletes the original harness session.

From the CLI, run `hihive import SESSION_ID --harness NAME` in the selected project.
For the first follow-up or fork, close the session in other terminals and add `--confirm-session-ownership` to confirm that Hive will use it from now on.
Session import is available only to the user, not to agent tokens.

## Task cost

Settings has a "Show task cost" toggle, off by default.
When it is on, each task card and the task detail show what the harness billed for that task's session, read from the harness's own transcript.
Only Pi reports cost today; Claude Code tasks show nothing.
The number covers the whole session, so follow-ups and reviews are included.
A forked task counts only its own API calls, not the history it inherited from its parent.

Pi transcripts are looked up under `~/.pi/agent/sessions` by default.
If your Pi runs with a different home (for example inside a container), set the harness's "Sessions Directory" in Settings to where that Pi writes its sessions.
