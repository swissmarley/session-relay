---
description: Set up the optional Session Relay status line (context %, relay generation, wrap-up and handoff markers). Plugins cannot set statusLine, so this copies the script to ~/.claude/session-relay/ and adds statusLine to the user settings. Pass "remove" to undo.
disable-model-invocation: true
argument-hint: "[remove]"
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay.py" install-statusline *)
---

# Session Relay status line

Arguments: `$ARGUMENTS`

The status line is optional: the relay's meter mod already gives the hooks an exact
reading. It shows `[Model] ctx 42% g1 ~soft` and is a second source when the mod cannot
load (Claude Code older than v2.1.287). It needs `jq`.

If the arguments are `remove`, run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay.py" install-statusline --remove --plugin-root "${CLAUDE_PLUGIN_ROOT}"
```

Otherwise run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay.py" install-statusline --plugin-root "${CLAUDE_PLUGIN_ROOT}"
```

It copies `statusline.sh` to `~/.claude/session-relay/statusline.sh`, backs up
`~/.claude/settings.json`, and sets `statusLine` to
`bash "$HOME/.claude/session-relay/statusline.sh" --plugin`. It prints JSON:

- `status: installed` or `unchanged`: tell the user the status line appears after the
  next response (or in a new session). Mention `warning` if present.
- `status: conflict`: another status line is configured (`existing`). Show the user that
  command and ask whether to replace it. Only if they agree, rerun the command with
  `--force` added; the backup keeps their old setting.
- `status: removed` or `not installed`: report it.

Never edit `~/.claude/settings.json` by hand for this.
