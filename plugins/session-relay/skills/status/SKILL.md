---
description: Show whether Session Relay is active in this project, its thresholds and config layers, the current context reading and recent handoffs. Use when the user asks about the relay, its status, thresholds, or why it did or did not hand off.
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay.py" status *)
---

# Session Relay status

Relay status for this project and session:

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay.py" status --plugin-root "${CLAUDE_PLUGIN_ROOT}" --cwd "${CLAUDE_PROJECT_DIR}" --session-id "${CLAUDE_SESSION_ID}"`

If no JSON appears above (shell commands in skills can be turned off), run that command
yourself with the Bash tool.

Summarize the JSON for the user in a few short lines:

- **Active here?** `active` and the `activation` reason. If `legacy_install` is set, the
  project-copy relay is still wired in that settings file and the plugin stands down;
  tell the user to run that install's `uninstall.sh` (see the README's migration notes).
- **Thresholds**: `config.soft` (wrap-up) and `config.hard` (handoff), and which entry in
  `config_layers` set them (plugin defaults < `/config` < user config < project config).
- **Context now**: `meter.used_pct` of `meter.window_size` tokens and `meter.source`
  (`mod` is exact, `statusline` is exact, `transcript` is an estimate).
- **Chain**: the last `ledger_tail` events and the newest entries in `handoffs`.
- **Status line**: `statusline` (`installed`, `other` or `not set`); suggest
  `/session-relay:statusline` when it is not set.

Mention `data_dir` only if the user asks where files are kept. Do not change anything.
