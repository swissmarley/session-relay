# Session Relay for Claude Code

Session Relay keeps long Claude Code tasks going when the context window fills up.
It measures context use from the status line, tells Claude to wrap up at **40 %**, makes
Claude write a structured handoff at **50 %**, and then starts a fresh interactive session
in the same project that reads the handoff and continues the work. No user intervention
is needed, and every session in the chain is linked to its parent in a ledger.

```
 generation 0                              generation 1                       generation 2
┌─────────────────────┐   handoff file   ┌─────────────────────┐   handoff   ┌────────────
│ claude …            │ ───────────────▶ │ claude --session-id │ ──────────▶ │ claude …
│ 40%: wrap up        │  new terminal /  │ reads handoff,      │             │
│ 50%: write handoff  │  tmux window     │ promotes memory,    │             │
│      launch child   │                  │ continues           │             │
└─────────────────────┘                  └─────────────────────┘             └────────────
```

## How it works

1. A **status-line script** (bash + jq, about 20 ms) records the context percentage for
   the current session in `.claude/state/`.
2. A **UserPromptSubmit hook** injects a one-time wrap-up notice between 40 % and 50 %.
3. A **Stop hook** at 50 % blocks the end of the turn and asks Claude to write
   `.claude/handoffs/<UTC>_<session>.md` from a nine-section template. It validates the
   file, redacts secrets, and after two failed attempts writes a mechanical handoff from
   git state so the chain never silently breaks.
4. A **launcher** opens the next session in a new tmux window or Terminal.app window
   (or writes a ready-to-run command when neither is available), inheriting the parent's
   permission mode and never adding `--dangerously-skip-permissions` on its own.
5. A **SessionStart hook** in the child injects the handoff, the last ledger entries and
   the project memory index, then marks the handoff consumed. The child promotes durable
   facts into `.claude/memory/`.
6. **PreCompact** and **SessionEnd** hooks back up transcripts and finalize the ledger.

Everything is plain shell plus one Python 3 standard-library script. The only extra
dependency is `jq`.

## Quick start

```bash
sh .claude/relay/bin/install.sh
```

Then start a new Claude Code session in the project. To see the whole chain without
starting any real session:

```bash
bash tests/e2e/run_simulation.sh
```

Full instructions, including the user-level install, the live test on a trivial task,
tuning and uninstalling, are in the **[step-by-step setup guide](docs/session-relay/SETUP.md)**.

## Documentation

| Document | Contents |
| --- | --- |
| [docs/session-relay/SETUP.md](docs/session-relay/SETUP.md) | Step-by-step setup, daily use, tuning, uninstall |
| [docs/session-relay/README.md](docs/session-relay/README.md) | Architecture diagram, configuration reference, permission trade-off, troubleshooting, known limitations |
| [docs/session-relay/RESEARCH.md](docs/session-relay/RESEARCH.md) | What was verified against the Claude Code docs and the installed version, and what was not |

## Repository layout

```
.claude/settings.json              hook and statusLine wiring (installed, committed)
.claude/relay/config.json          thresholds and switches
.claude/relay/handoff-template.md  the handoff template
.claude/relay/bin/relay.py         all hooks, meter fallback, validator, ledger, utilities
.claude/relay/bin/statusline.sh    context meter and status line
.claude/relay/bin/launch.sh        launcher (tmux / Terminal.app / NEXT_COMMAND.txt)
.claude/relay/bin/install.sh       installer (project or --user)
.claude/relay/bin/uninstall.sh     uninstaller
.claude/memory/                    durable project memory (INDEX, decisions, gotchas, conventions)
docs/session-relay/                guide, reference, research notes
tests/                             unit tests (python3 -m unittest), fixtures, e2e simulation
```

Runtime data (`.claude/state/`, `.claude/handoffs/`, `.claude/backups/`, the ledger and
the log) is gitignored.

## Requirements

- Claude Code 2.1.x (hooks, `statusLine`, `--session-id`, `--permission-mode`)
- `jq`, `python3` (3.9 or newer, standard library only), POSIX `sh`, `bash`
- macOS or Linux; `tmux` optional, Terminal.app or iTerm2 on macOS

## Tests

```bash
cd tests && python3 -m unittest -v
```

```bash
bash tests/e2e/run_simulation.sh --mechanical
```

## Status and limitations

The unit tests and the end-to-end simulation pass; the simulation runs the real scripts
with the launcher in `file` mode. Opening a real terminal window, Desktop-app behaviour
and the nested-session environment scrub are documented as unverified in
[RESEARCH.md](docs/session-relay/RESEARCH.md). The parent window stays open after a
launch, the child is a CLI session even when the parent ran in the Desktop app, and
secret redaction is pattern-based. See the known-limitations section of the
[reference](docs/session-relay/README.md#known-limitations).

Kill switch: `CLAUDE_RELAY_DISABLE=1`.
