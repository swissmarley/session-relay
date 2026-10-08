---
description: Turn Session Relay on for the current project (needed when activation is opt-in). Pass "off" to turn it off for this project.
disable-model-invocation: true
argument-hint: "[off]"
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay.py" enable *), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay.py" disable *)
---

# Enable Session Relay for this project

Arguments: `$ARGUMENTS`

If the arguments are `off`, `disable` or `no`, run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay.py" disable --plugin-root "${CLAUDE_PLUGIN_ROOT}" --cwd "${CLAUDE_PROJECT_DIR}"
```

Otherwise run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay.py" enable --plugin-root "${CLAUDE_PLUGIN_ROOT}" --cwd "${CLAUDE_PROJECT_DIR}"
```

The command writes `"enabled": true` (or `false`) to `.claude/session-relay/config.json`
in the project and prints JSON. Report in two or three lines:

- whether the relay is now active here (`active`, `reason`);
- that the setting lives in `.claude/session-relay/config.json`, the one file in that
  folder git does not ignore, so the user can commit it to share the choice with the team;
- if `activation` is `always`, that the relay already runs in every project and this
  file now records the project's choice explicitly (`off` turns it off here).

The hooks pick the change up at the next prompt; no restart is needed.
