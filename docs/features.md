# More features

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

Agent output is untrusted, for example when the agent reads web pages, so keep `pattern` strict.
Tags inside code blocks and inline code stay as text.
A command that runs longer than 60 seconds is left running and counts as started.

## Task cost

Settings has a "Show task cost" toggle, off by default.
When it is on, each task card and the task detail show what the harness billed for that task's session, read from the harness's own transcript.
Only Pi reports cost today; Claude Code tasks show nothing.
The number covers the whole session, so follow-ups and reviews are included.
A forked task counts only its own API calls, not the history it inherited from its parent.

Pi transcripts are looked up under `~/.pi/agent/sessions` by default.
If your Pi runs with a different home (for example inside a container), set the harness's "Sessions Directory" in Settings to where that Pi writes its sessions.
