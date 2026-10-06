# Session Relay: step-by-step setup and usage guide

This guide takes you from a clean machine to a working relay chain, then covers daily use,
tuning, inspection and removal. For the architecture, the full configuration reference and
known limitations, see [README.md](README.md).

Commands are written for macOS or Linux and are meant to be run from the project root.

---

## Part 1: Set up

### Step 1. Check the prerequisites

```bash
claude --version && jq --version && python3 --version && sh -c 'echo sh ok' && bash --version | head -1
```

You need Claude Code 2.1.x, `jq` (any 1.6+), Python 3.9 or newer, and `sh`/`bash`.
`tmux` is optional. On macOS the launcher can use Terminal.app or iTerm2; on Linux without
tmux it writes a command file instead of opening a window.

If `jq` is missing on macOS:

```bash
brew install jq
```

### Step 2. Get the relay files

Option A, this repository is your project: nothing to do, the files are already in
`.claude/relay/`.

Option B, add the relay to another project: copy the relay directory into that project.

```bash
cp -R .claude/relay /path/to/your-project/.claude/relay
```

Option C, install once for every project on this machine (user-level): skip the copy and
use `--user` in the next step; the installer copies the files to `~/.claude/relay/`.

### Step 3. Run the installer

Project-level install (recommended; the wiring is committed with the project so teammates
get it too):

```bash
sh .claude/relay/bin/install.sh
```

Project-level install into another project:

```bash
sh .claude/relay/bin/install.sh --project-dir /path/to/your-project
```

User-level install (applies to all projects; per-project data such as handoffs, state and
memory still live inside each project):

```bash
sh .claude/relay/bin/install.sh --user --project-dir /path/to/your-project
```

The installer prints what it changes. It always:

- backs up the target `settings.json` to `settings.json.bak.<UTC timestamp>` first;
- adds the five hooks (`SessionStart`, `UserPromptSubmit`, `Stop`, `PreCompact`,
  `SessionEnd`) and the `statusLine` entry, without touching anything else in the file;
- seeds `.claude/memory/` with `INDEX.md`, `decisions.md`, `gotchas.md`, `conventions.md`;
- appends the single line `@.claude/memory/INDEX.md` to `CLAUDE.md` (creating the file if
  it does not exist);
- adds the runtime paths to `.gitignore` (project installs only).

Useful flags: `--dry-run` to preview, `--no-claude-md`, `--no-statusline`,
`--no-gitignore`. Running the installer again is safe; it reports "already installed".

If you already had your own `statusLine`, the installer leaves it alone and prints the
command to use if you want the relay meter instead. Without the relay status line the
hooks still work: they fall back to reading the transcript, with readings marked approximate.

### Step 4. Verify the wiring

```bash
cat .claude/settings.json
```

You should see one entry per hook event whose command ends in `relay.py <subcommand>`,
plus a `statusLine` whose command ends in `statusline.sh`. Then run the built-in
self-checks:

```bash
python3 .claude/relay/bin/relay.py status
```

```bash
cd tests && python3 -m unittest
```

### Step 5. Start a session and look at the status line

Open a **new** Claude Code session in the project from a terminal:

```bash
claude
```

If this is the first time you open the folder, accept the workspace trust prompt; hooks in
a project's `.claude/settings.json` only run in trusted workspaces. After the first
response the status line at the bottom reads something like:

```
[Opus] ctx 3% g0
```

`ctx` is the context percentage; `g0` is the relay generation (0 means this session was
not started by the relay). When usage passes the soft threshold the line gains `~soft`,
past the hard threshold `!hard`. If the environment variable `CLAUDE_RELAY_DISABLE=1` is
set, it shows `relay off`.

A session that was already running when you installed picks the hooks up automatically,
because Claude Code reloads settings files when they change.

---

## Part 2: See it work

### Step 6. Run the simulation (no real session is started)

```bash
bash tests/e2e/run_simulation.sh
```

The script builds a throw-away project, installs the relay with thresholds of 1 % and 2 %,
and pushes the exact JSON payloads Claude Code would send through the real scripts. You
will see the soft notice, the Stop hook blocking with the template, a handoff being
accepted, the child session being pre-linked, the SessionStart injection, the consume-once
check, a PreCompact backup, and finally the ledger. The launcher runs in `file` mode so no
windows open. The variant below shows what happens when the session never writes a
handoff:

```bash
bash tests/e2e/run_simulation.sh --mechanical
```

### Step 7. Trigger the live chain on a trivial task

This opens a real new window on your machine, so do it when you can watch.

1. Lower the thresholds so the very first turn crosses them:

   ```bash
   python3 - <<'EOF'
   import json; p=".claude/relay/config.json"; c=json.load(open(p)); c.update(soft=1, hard=1); json.dump(c, open(p,"w"), indent=2)
   EOF
   ```

2. Start a session **from a terminal** (a tmux window or Terminal.app window will open
   next to it) and give it something small:

   ```bash
   claude "List the files in this project and summarize what the relay does."
   ```

3. Watch the sequence:
   - Claude answers. At the end of the turn the Stop hook blocks and shows the handoff
     request; Claude writes `.claude/handoffs/<UTC>_<session-id>.md`.
   - Claude stops again. The hook validates the file and launches. A system message in the
     transcript says `continuing in a new session <id> (generation 1)` and names the method
     (`tmux` or `terminal`). macOS asks for Automation permission the first time
     Terminal.app is driven; allow it and, if the launch was skipped, send another short
     prompt so the next stop retries.
   - A new window opens running `claude --session-id … --permission-mode … --name relay-g1`
     with the bootstrap prompt. Its first reply confirms the objective in three lines,
     promotes the handoff's "Memory updates" into `.claude/memory/`, and continues with the
     next action from the handoff.

4. Check the trail:

   ```bash
   tail -n 5 .claude/relay/ledger.jsonl
   ```

   Expect `handoff_requested`, `handoff_launched`, `handoff_consumed` in that order, and
   `status: consumed` at the top of the handoff file.

5. Restore the real thresholds:

   ```bash
   git checkout .claude/relay/config.json
   ```

   (or set `soft` back to 40 and `hard` to 50 by hand).

If no window appeared, the ledger says why: `launch_deferred` means no terminal could be
opened and the command to run is in `.claude/relay/NEXT_COMMAND.txt`; `launch_cooldown`
means a launch happened less than `cooldown_seconds` ago and the next stop will retry;
`launch_failed` carries the launcher's error text.

---

## Part 3: Daily use

### What you will notice during normal work

- **Below 40 %:** nothing. The status line just shows the percentage.
- **40 % to 50 %:** once, at your next prompt, Claude receives a notice to finish the
  current unit of work, avoid large reads and new investigations, and keep notes. You will
  see Claude's behaviour change; the notice itself is not shown to you.
- **At 50 %, end of turn:** Claude is blocked from stopping until it writes the handoff.
  You can keep watching or type nothing. When the handoff is accepted the new window opens
  and the old session idles. The old window stays open; close it when you like, or set
  `close_old_session: true` if you work in tmux.
- **In the new window:** read the three-line confirmation. If it is wrong, correct it
  there; the handoff file is in `.claude/handoffs/` if you want to read it yourself.
- **Compaction:** if Claude Code compacts before 50 % (rare with the default thresholds),
  a backup of the transcript is written to `.claude/backups/` and the relay keeps working
  with the compacted session.

### Continuing by hand

When the launcher could not open a window, run the command it left behind:

```bash
cat .claude/relay/NEXT_COMMAND.txt
```

Each relayed session is named `relay-g<N>`, so you can also come back to it later with
`claude --resume relay-g1`.

### Where things are

| Path | What |
| --- | --- |
| `.claude/handoffs/` | handoff documents, newest last; front matter shows `status`, `consumed_by`, lineage |
| `.claude/relay/ledger.jsonl` | one JSON line per event across the whole chain |
| `.claude/relay/relay.log` | diagnostics, rotated at 1 MB |
| `.claude/state/<session>.json` | meter state written by the status line |
| `.claude/state/<session>.relay.json` | hook state: attempts, generation, parent, child, launched |
| `.claude/backups/` | PreCompact snapshots, last 10 |
| `.claude/memory/` | durable project memory, committed |

### Handy commands

Show the effective configuration and handoffs:

```bash
python3 .claude/relay/bin/relay.py status
```

Read the meter for a session from its transcript (the fallback path):

```bash
python3 .claude/relay/bin/relay.py meter --session-id <sid> --transcript ~/.claude/projects/<project>/<sid>.jsonl
```

Validate a handoff you edited by hand:

```bash
python3 .claude/relay/bin/relay.py validate .claude/handoffs/<file>.md
```

Re-arm a session that already launched (for example you closed the child window and want
the next stop to launch again):

```bash
python3 .claude/relay/bin/relay.py reset --session-id <sid>
```

---

## Part 4: Tune it

Edit `.claude/relay/config.json` (or `~/.claude/relay/config.json` for a user install when
the project has none). Changes apply to the next hook run; no restart needed.

| Goal | Setting |
| --- | --- |
| Hand off earlier or later | `soft`, `hard` (percent of the context window) |
| Limit the chain length | `max_generations` (default 8) |
| Avoid rapid relaunches | `cooldown_seconds` (default 60) |
| Try it without launching | `dry_run: true` (blocks, handoffs and ledger still happen) |
| Always use tmux / a terminal / a command file | `launcher.mode`: `tmux`, `terminal`, `file` |
| Use iTerm2 instead of Terminal.app | `launcher.terminal_app: "iTerm"` |
| Close the parent tmux pane after launch | `close_old_session: true` |
| Use the Desktop app's bundled CLI for children | `launcher.claude_bin` set to the value of `$CLAUDE_CODE_EXECPATH` |
| Let a bypass-permissions parent spawn a bypass child | `launcher.allow_bypass_inherit: true` (off by default on purpose) |
| Shorter or longer handoffs | `max_handoff_words` |
| Bigger or smaller injection into the child | `inject_budget_chars` (16000 ≈ 4k tokens) |

Permission modes: the child starts in the parent's mode. If you want the chain to run
hands-off, run the parent in `auto` mode (`claude --permission-mode auto`); a parent in
Manual mode produces a child that will ask before running commands. The relay never adds
`--dangerously-skip-permissions` by itself.

Turn the relay off temporarily without uninstalling:

```bash
export CLAUDE_RELAY_DISABLE=1
```

or set `"enabled": false` in the config.

---

## Part 5: Remove it

Project install:

```bash
sh .claude/relay/bin/uninstall.sh
```

User install:

```bash
sh .claude/relay/bin/uninstall.sh --user --project-dir /path/to/your-project
```

Add `--purge` to also delete runtime data (`state`, `backups`, `handoffs`, ledger, log).
`.claude/memory/` is never deleted. Only entries whose command points into a `relay/bin`
directory are removed from `settings.json`; your other hooks and settings stay as they
were, and a backup is written first.

---

## Quick troubleshooting

| Symptom | Check |
| --- | --- |
| Status line never appears | `jq` installed? Is another `statusLine` set at a higher-precedence scope (`~/.claude/settings.json` for a project install is lower; `.claude/settings.local.json` is higher)? |
| Nothing happens at 50 % | `grep "stop: meter" .claude/relay/relay.log` shows the reading the hook saw; `used_pct=null` means no meter data; `relay disabled` means the kill switch is on |
| Claude is blocked repeatedly | at most twice per session by design; the log shows `handoff_invalid` with the validator's errors |
| New window never opened | last ledger line: `launch_deferred` → run `NEXT_COMMAND.txt`; `launch_cooldown` → wait; `launch_failed` → read `launcher_err` |
| Child started without the handoff | `grep "no pending handoff" .claude/relay/relay.log`; check the handoff's `status:` and `launched_child:` lines |
| Hooks not running at all | `/hooks` inside the session lists what is configured; project hooks need a trusted workspace; `claude --debug` shows hook output |

More detail is in the [troubleshooting](README.md#troubleshooting) and
[known limitations](README.md#known-limitations) sections of the reference.
