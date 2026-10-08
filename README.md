# Session Relay for Claude Code

Session Relay keeps long Claude Code tasks going when the context window fills up.
It measures context use, tells Claude to wrap up at **40 %**, makes Claude write a
structured handoff at **50 %**, and then starts a fresh interactive session in the same
project that reads the handoff and continues the work. No user intervention is needed,
and every session in the chain is linked to its parent in a ledger.

```
 generation 0                              generation 1                       generation 2
┌─────────────────────┐   handoff file   ┌─────────────────────┐   handoff   ┌────────────
│ claude …            │ ───────────────▶ │ claude --session-id │ ──────────▶ │ claude …
│ 40%: wrap up        │  new terminal /  │ reads handoff,      │             │
│ 50%: write handoff  │  tmux window     │ promotes memory,    │             │
│      launch child   │                  │ continues           │             │
└─────────────────────┘                  └─────────────────────┘             └────────────
```

This repository is a Claude Code **plugin marketplace** with one plugin, `session-relay`.
Install it once at user scope and it works in every project, without copying anything
into a project's `.claude` folder. The older project-copy install (`install.sh`) still
works; see [Project-copy install](#project-copy-install-backwards-compatible).

## Install

```bash
claude plugin marketplace add swissmarley/session-relay
claude plugin install session-relay@session-relay
```

`claude plugin install` installs at user scope by default. Start a new Claude Code
session (or run `/reload-plugins` in an open one) and the relay is active in every project.
To change the thresholds or the activation mode, run `/config` (or
`/plugin configure session-relay@session-relay`):

| `/config` option | Default | Meaning |
| --- | --- | --- |
| Wrap-up threshold (%) | 40 | Context use at which Claude is told once to wrap up |
| Handoff threshold (%) | 50 | Context use at which the Stop hook asks for a handoff and launches the next session |
| Activation | `always` | `always`: every project. `opt-in`: only projects that ran `/session-relay:enable` |

Requirements: Claude Code **2.1.271** or later (the plugin's `/config` options use fixed
choices), `python3` 3.9+ on `PATH`, `git`, and POSIX `sh`. The exact context meter is a
small mod that needs Claude Code **2.1.287** or later in the terminal (**2.1.286** in the
Desktop app); see [Context meter](#context-meter). `jq` is needed only for the optional
status line. `tmux` is optional; on macOS the launcher can also use Terminal.app or iTerm2.

The plugin adds three commands:

| Command | What it does |
| --- | --- |
| `/session-relay:status` | Whether the relay is active here, thresholds and where each came from, the current context reading, recent handoffs |
| `/session-relay:enable` | Turn the relay on for this project (`/session-relay:enable off` turns it off) |
| `/session-relay:statusline` | Set up the optional status line (`/session-relay:statusline remove` undoes it) |

## How it works

1. A **meter mod** (`hooks/meter.mjs`) copies Claude Code's own context figures to
   `~/.claude/session-relay/meter/<session_id>.json` after every turn.
2. A **UserPromptSubmit hook** injects a one-time wrap-up notice between the two thresholds.
3. A **Stop hook** at the handoff threshold blocks the end of the turn and asks Claude to
   write `.claude/session-relay/handoffs/<UTC>_<session>.md` from a nine-section template.
   It validates the file, redacts secrets, and after two failed attempts writes a
   mechanical handoff from git state so the chain never silently breaks.
4. A **launcher** opens the next session in a new tmux window or Terminal.app window (or
   writes a ready-to-run command to `.claude/session-relay/NEXT_COMMAND.txt` when neither
   is available), inheriting the parent's permission mode and never adding
   `--dangerously-skip-permissions` on its own.
5. A **SessionStart hook** in the child injects the handoff, the last ledger entries and the
   project memory index, then marks the handoff consumed. The child promotes durable facts
   into `.claude/memory/`.
6. **PreCompact** and **SessionEnd** hooks back up transcripts and finalize the ledger.

Everything is plain shell, one Python 3 standard-library script and one small JavaScript
mod.

### Context meter

The relay reads context use from three sources, in this order:

1. **The meter mod** (exact). Plugins can't set `statusLine`, and the transcript can't tell
   a 1M-token context window from a 200k one, so the mod reads
   `$.session.usage().context` from Claude Code's mod API and writes it after every turn
   and right before the relay's Stop and UserPromptSubmit hooks run.
2. **The status line state** (exact), when the optional status line is installed.
3. **The transcript** (estimate, marked approximate). When the mod has reported the
   window size but no percentage yet (a fresh or just-compacted session), the estimate
   uses that real window size.

On Claude Code older than 2.1.287 the mod doesn't load and the relay uses sources 2 and 3.
To check which source is in use, run `/session-relay:status`.

### Optional status line

Plugins can't set `statusLine`, so `/session-relay:statusline` does it for you: it copies
`statusline.sh` to `~/.claude/session-relay/statusline.sh`, backs up
`~/.claude/settings.json`, and sets:

```json
"statusLine": { "type": "command", "command": "bash \"$HOME/.claude/session-relay/statusline.sh\" --plugin", "padding": 0 }
```

The line reads `[Opus] ctx 42% g1 ~soft`: the context percentage, the relay generation
(`g0` is a session the relay didn't start), and `~soft` or `!hard` past each threshold. If
you already have a status line, the command shows it and asks before replacing it. The
status line needs `jq`. To do it by hand instead, copy
`plugins/session-relay/scripts/statusline.sh` to that path and add the JSON above.

### Opt-in mode

By default the relay runs in every project. To run it only where you ask for it, set
**Activation** to `opt-in` in `/config`, or put this in `~/.claude/session-relay/config.json`:

```json
{ "activation": "opt-in" }
```

In opt-in mode the hooks do nothing, and create nothing, in a project until you run
`/session-relay:enable` there. That writes `{"enabled": true}` to
`.claude/session-relay/config.json`, the one file in that folder git doesn't ignore, so you
can commit it and your team gets the same choice. In `always` mode,
`/session-relay:enable off` switches one project off the same way.

### Configuration

Settings are merged from four layers; a later layer wins key by key:

1. plugin defaults: `plugins/session-relay/scripts/config.json` (read-only)
2. `/config` options (wrap-up threshold, handoff threshold, activation)
3. `~/.claude/session-relay/config.json`: your settings for every project
4. `<project>/.claude/session-relay/config.json`: one project's settings

Every key from the [configuration reference](docs/session-relay/README.md#configuration-clauderelayconfigjson)
works in layers 3 and 4, for example `{"launcher": {"mode": "tmux"}, "max_generations": 4}`.
The kill switch `CLAUDE_RELAY_DISABLE=1` turns every hook off.

### Where files go

| Path | Contents |
| --- | --- |
| `<project>/.claude/session-relay/` | Everything the relay keeps for one project: `state/`, `handoffs/`, `backups/`, `ledger.jsonl`, `relay.log`, `lock/`, `launched/`, `run/`, `NEXT_COMMAND.txt`, and `config.json` if you create one |
| `<project>/.claude/session-relay/.gitignore` | Written on first use with `*` and `!config.json`, so `git status` stays clean |
| `<project>/.claude/memory/` | Project memory (`INDEX.md`, `decisions.md`, `gotchas.md`, `conventions.md`), created only when a session writes memory |
| `~/.claude/session-relay/` | Your `config.json`, the meter readings (`meter/`, `statusline/`), and the status line copy |

The plugin never edits `CLAUDE.md`. Instead, when `.claude/memory/INDEX.md` exists, the
SessionStart hook injects it at the start of every session, after `/clear`, and after
compaction.

## Migrating from the project-copy install

If you installed Session Relay with `install.sh` before, switch each project over like this:

1. Remove the old wiring (run it in each project you installed it in, and add `--user` for
   a user-level install). It removes the relay hooks, the status line and the `CLAUDE.md`
   pointer line, and keeps `.claude/memory/` and your handoffs:

   ```bash
   sh .claude/relay/bin/uninstall.sh
   ```

2. Install the plugin:

   ```bash
   claude plugin marketplace add swissmarley/session-relay
   claude plugin install session-relay@session-relay
   ```

3. Move existing handoffs to the plugin's folder, so a pending one is still picked up:

   ```bash
   mkdir -p .claude/session-relay/handoffs
   mv .claude/handoffs/*.md .claude/session-relay/handoffs/
   ```

4. Optional: run `uninstall.sh --purge` to delete the old runtime data, delete
   `.claude/relay/`, and remove the old relay entries from `.gitignore`.

Until step 1 is done, the plugin stands down in any project (or for any user) whose
settings still run `.claude/relay/bin/relay.py`, so a session is never handled twice.
`/session-relay:status` tells you when that happens.

## Limits

- **Windows**: hooks run through Git Bash, and `python3` must be on `PATH` (the Microsoft
  Store `python3` stub isn't enough). The launcher can't open a window on Windows, so the
  next session's command is written to `NEXT_COMMAND.txt`.
- **Cloud sessions** (claude.ai/code): plugins don't load there, so the relay doesn't run.
- **Desktop app WSL sessions**: plugins aren't available in WSL sessions, so the relay
  doesn't run there.
- The parent window stays open after a launch, the child is a CLI session even when the
  parent ran in the Desktop app, and secret redaction is pattern-based. See the
  [known limitations](docs/session-relay/README.md#known-limitations).

## Project-copy install (backwards compatible)

The original install copies the relay into a project's `.claude` folder and wires it in
`.claude/settings.json`:

```bash
sh .claude/relay/bin/install.sh                 # this project
sh .claude/relay/bin/install.sh --user --project-dir /path/to/project   # user-level
```

It behaves exactly as before: data in `.claude/state/`, `.claude/handoffs/`,
`.claude/backups/` and `.claude/relay/`, config in `.claude/relay/config.json`, and a
memory pointer line in `CLAUDE.md`. The [step-by-step setup guide](docs/session-relay/SETUP.md)
and the [reference](docs/session-relay/README.md) describe it in full.

## Repository layout

```
.claude-plugin/marketplace.json            the marketplace ("session-relay")
plugins/session-relay/
  .claude-plugin/plugin.json               plugin manifest, version, /config options
  hooks/hooks.json                         the five hooks and the meter mod
  hooks/meter.mjs                          meter mod: exact context use from $.session.usage()
  scripts/relay.py                         all hooks, meter fallback, validator, ledger, utilities
  scripts/launch.sh                        launcher (tmux / Terminal.app / NEXT_COMMAND.txt)
  scripts/statusline.sh                    optional status line
  scripts/handoff-template.md              the handoff template
  scripts/config.json                      plugin defaults
  skills/{status,enable,statusline}/       the three commands
  tests/meter.test.ts                      mod tests (claude plugin test)
.claude/relay/                             project-copy install (identical scripts, install.sh, uninstall.sh)
.claude/settings.json                      this repository's own project-copy wiring
.claude/memory/                            durable project memory
tools/sync-legacy.sh                       copies the plugin scripts into .claude/relay
docs/session-relay/                        guide, reference, research notes
tests/                                     unit tests, fixtures, end-to-end simulations
```

`plugins/session-relay/scripts/` is the source of truth. After changing a script there,
run `sh tools/sync-legacy.sh`; `tests/test_plugin_layout.py` fails while the two copies differ.

## Tests

```bash
cd tests && python3 -m unittest -v                       # unit tests (both layouts)
bash tests/e2e/run_simulation.sh                         # project-copy chain
bash tests/e2e/run_simulation.sh --mechanical            # ... when no handoff is written
bash tests/e2e/plugin_simulation.sh                      # plugin chain
claude plugin test plugins/session-relay                 # meter mod
claude plugin validate --strict plugins/session-relay    # plugin manifest, hooks, mod
claude plugin validate --strict .                        # marketplace
```

The simulations run the real scripts with the launcher in `file` mode, so no terminal
window opens. Opening a real terminal window, Desktop-app behaviour and the nested-session
environment scrub are documented as unverified in
[RESEARCH.md](docs/session-relay/RESEARCH.md).

## Documentation

| Document | Contents |
| --- | --- |
| [docs/session-relay/SETUP.md](docs/session-relay/SETUP.md) | Step-by-step setup of the project-copy install, daily use, tuning, uninstall |
| [docs/session-relay/README.md](docs/session-relay/README.md) | Architecture diagram, configuration reference, permission trade-off, troubleshooting, known limitations |
| [docs/session-relay/RESEARCH.md](docs/session-relay/RESEARCH.md) | What was verified against the Claude Code docs and the installed version, and what was not |
