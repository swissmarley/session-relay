# Session Relay — Phase 0 research

Date: 2026-10-06. Everything marked **VERIFIED** was read from the official docs at
https://code.claude.com/docs/en/ (hooks, hooks-guide, statusline, settings, cli-reference,
permission-modes, sessions, memory, context-window) or executed on this machine. Everything
marked **UNVERIFIED** or **INFERRED** is called out explicitly and the design does not depend
on it, or depends on it only behind a fallback.

## 1. Installed versions and runtime (VERIFIED by execution)

| Item | Value |
| --- | --- |
| `claude --version` (PATH) | `2.1.280` at `~/.local/bin/claude` → `~/.local/share/claude/versions/2.1.280` |
| Binary hosting *this* session | `$CLAUDE_CODE_EXECPATH` = Desktop app bundle, reports `2.1.288` |
| Entry point of this session | `CLAUDE_CODE_ENTRYPOINT=claude-desktop`, `CLAUDE_CODE_CHILD_SESSION=1` |
| Recent transcripts on disk | written by `2.1.281`–`2.1.289` (`claude-desktop`, `claude-vscode`) |
| jq | `1.7.1` (`/opt/homebrew`) |
| python3 | `3.9.6` (Xcode), stdlib only, **no pytest** |
| tmux | `3.6a` installed, **no server running** right now |
| shellcheck, bats, terminal-notifier | **not installed** |
| osascript, Terminal.app, uuidgen, git 2.54 | present |
| Parse cost of a statusLine payload | jq `3 ms`, python3 `29 ms` |

Consequences:
- Hooks and the statusLine run under whichever binary hosts the session (2.1.288 in Desktop, 2.1.280 in a plain terminal). Any doc feature gated above 2.1.280 must be treated as optional.
- The launcher starts `claude` from `PATH` by default (2.1.280). It can be pointed at `$CLAUDE_CODE_EXECPATH` via config for parity with the parent.
- Tests will use `python3 -m unittest` (stdlib) rather than pytest/bats. Shellcheck will be run if the user installs it (`brew install shellcheck`); the scripts are written to its rules regardless.

## 2. Hooks (VERIFIED from docs/en/hooks and docs/en/hooks-guide)

### Events used by the relay and their matchers

| Event | Matcher values | Can block? | Accepts `additionalContext`? |
| --- | --- | --- | --- |
| `SessionStart` | `startup`, `resume`, `clear`, `compact`, `fork` | No (exit 2 shows stderr to user) | Yes |
| `UserPromptSubmit` | none (always fires) | Yes (`decision: "block"`) | Yes |
| `Stop` | none | Yes (`decision: "block"`) | Yes |
| `PreCompact` | `manual`, `auto` | Yes | No |
| `SessionEnd` | `clear`, `resume`, `logout`, `prompt_input_exit`, `other` | No | No |

### Common stdin fields (all events)

`session_id`, `transcript_path`, `cwd`, `hook_event_name`, `permission_mode`, `prompt_id`,
`scratchpad_dir` (optional), `effort.level`.

`permission_mode` values: `"default"`, `"plan"`, `"acceptEdits"`, `"auto"`, `"dontAsk"`,
`"bypassPermissions"`. Manual mode arrives as `"default"`, never `"manual"`. Docs: "Not all
events receive this field" — the relay treats it as optional.

### Event-specific stdin fields

- `Stop`: `stop_hook_active` (bool), `last_assistant_message` (full text of the final response).
- `SessionStart`: `source`, optional `model`, `agent_type`, `session_title`; on `resume` only (v2.1.251+): `seconds_since_last_response`, `context_tokens`, `prompt_cache_likely_expired`, `estimated_cache_write_usd`.
- `UserPromptSubmit`: `prompt`.
- `PreCompact`: `trigger` (`manual`|`auto`), `custom_instructions`.
- `SessionEnd`: `reason`.

### Exit codes

- `0`: success. If stdout starts with `{` and ends with `}` it is parsed as JSON control output. For `UserPromptSubmit` and `SessionStart`, **plain-text stdout on exit 0 is added to Claude's context**.
- `2`: blocking error. `Stop`: blocks the stop and Claude continues, stderr shown to Claude. `UserPromptSubmit`: blocks the prompt, stderr shown to Claude. `SessionStart`: non-blocking, stderr shown to user. `PreCompact`: blocks compaction.
- Other non-zero: non-blocking; transcript shows a `<hook> hook error` notice with the first stderr line. JSON on stdout is still honored if valid.

### JSON output schema

Universal fields: `continue` (bool), `stopReason`, `suppressOutput`, `systemMessage`.

```json
{"decision": "block", "reason": "shown to Claude"}
{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}
{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "..."}}
{"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "..."}}
```

`additionalContext` is injected as a system reminder Claude reads as plain text. Build JSON with `jq`/`python -c json.dumps`, never string concatenation (docs troubleshooting section).

### Loop guards (VERIFIED)

- `stop_hook_active` is `true` when the Stop hook already caused a continuation in this turn chain.
- "Claude Code overrides a Stop hook after it blocks eight times in a row with no tool call from Claude in between." The relay never relies on this; it caps itself at 2 block attempts per session.
- `Stop` fires whenever Claude finishes responding (end of turn), **not** mid-tool and **not** on user interrupts (`Esc`/`Ctrl+C`). API errors fire `StopFailure` instead.

### Timeouts (VERIFIED)

- Default `600 s` for `command` hooks; `UserPromptSubmit` is lowered to `30 s`.
- `SessionEnd` hooks share a `1.5 s` budget, raised to the hook's own `timeout` up to `60 s`. The relay's SessionEnd hook sets `"timeout": 10` and does only an append.
- Per-hook `"timeout"` is in seconds.

### Execution environment (VERIFIED)

- Shell-form command hooks run via `sh -c` on macOS. Scripts are invoked as `bash "$CLAUDE_PROJECT_DIR/.claude/relay/bin/<name>.sh"` to avoid depending on the executable bit or on `sh` semantics.
- Hooks run in the session's current directory with Claude Code's environment; `$CLAUDE_PROJECT_DIR` is the project root where the session started.
- All matching hooks run in parallel; an identical handler defined in several settings files runs once.
- Settings files are watched and hooks reload on edit; `/hooks` is a read-only browser.
- Hooks in a project's `.claude/settings.json` require the workspace to be trusted.
- `transcript_path` is written asynchronously and may lag; for the final assistant text use `last_assistant_message`.
- `SessionStart` hooks at startup run in the background; Claude's first response waits for them.
- `CLAUDE_ENV_FILE` lets SessionStart hooks export env vars to later Bash commands (not needed).

### Settings `hooks` shape

```json
{"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "bash \"$CLAUDE_PROJECT_DIR\"/.claude/relay/bin/stop.sh", "timeout": 120}]}]}}
```

Fields available per handler: `type`, `command`, `args` (exec form, no shell), `timeout`, `statusMessage`, `if`, `async`, `shell`.

## 3. statusLine (VERIFIED from docs/en/statusline)

Settings shape:

```json
{"statusLine": {"type": "command", "command": "bash \"$HOME/.../statusline.sh\"", "padding": 0, "refreshInterval": 5}}
```

`refreshInterval` (seconds, min 1) is optional and re-runs the command on a timer in addition to events. Updates are debounced at `300 ms`; an in-flight script is cancelled when a new update triggers (so the script must write its state file atomically and early).

stdin JSON fields relevant to the meter (exact names):

| Field | Meaning |
| --- | --- |
| `session_id`, `transcript_path`, `cwd`, `version` | identity |
| `model.id`, `model.display_name` | model |
| `workspace.current_dir`, `workspace.project_dir` | dirs |
| `context_window.total_input_tokens` | `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` from the most recent API response |
| `context_window.total_output_tokens` | output tokens of the most recent response |
| `context_window.context_window_size` | `200000` by default, `1000000` for extended-context models |
| `context_window.used_percentage` | pre-calculated, **input-only** formula above; **may be `null` early in the session** |
| `context_window.remaining_percentage` | may be `null` early |
| `context_window.current_usage.{input_tokens,cache_creation_input_tokens,cache_read_input_tokens,output_tokens}` | **`null` before the first API call and again right after `/compact` until the next call** |
| `exceeds_200k_tokens` | fixed 200k threshold, not window-relative |
| `session_name` | name or generated title |

The meter therefore: uses `used_percentage` when non-null; otherwise computes `100 * total_input_tokens / context_window_size` when `total_input_tokens > 0`; otherwise falls back to the transcript. UNVERIFIED: whether `$CLAUDE_PROJECT_DIR` is exported to the statusLine command — the meter uses `cwd` / `workspace.project_dir` from the JSON instead.

## 4. Transcript JSONL (VERIFIED by inspecting real files)

Path: `~/.claude/projects/<project>/<session_id>.jsonl` where `<project>` is the working-directory path with every non-alphanumeric character replaced by `-` (docs/en/sessions). Subagent transcripts live in a sibling directory `<session_id>/subagents/agent-*.jsonl`, so the main file contains only `isSidechain: false` lines (13 856 of 13 856 sampled assistant lines).

Where `usage` lives (from a 2.1.288 transcript):

```
.type == "assistant"
.message.model                          "claude-opus-5-5"
.message.usage.input_tokens             4
.message.usage.cache_creation_input_tokens  2832
.message.usage.cache_read_input_tokens  498344
.message.usage.output_tokens            254
.timestamp, .sessionId, .version, .entrypoint, .requestId, .apiBlockIndex
```

Facts that matter for the fallback meter:
- One API response produces **several** `assistant` lines (one per content block, same `requestId`, identical `usage`). "Latest usage" = the last `assistant` line that has a non-null `message.usage`.
- Context fill = `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` of that line, divided by the window size. The window size is **not** in the transcript; the fallback infers it from `model` (`[1m]` / extended-context models → 1 000 000, else 200 000) unless a fresh statusLine state file recorded `window_size`. Hence the fallback is marked `approx: true`.
- Compaction is visible as `{"type":"system","subtype":"compact_boundary","compactMetadata":...}` followed by `{"type":"user","isCompactSummary":true,...}`. No special handling is needed: the next assistant `usage` already reflects the compacted context.
- Other line types present: `ai-title`, `atis-latch`, `attachment`, `cost-state`, `file-history-delta`, `file-history-snapshot`, `last-prompt`, `mode`, `queue-operation`, `system`, `user`. The docs state the format "is internal to Claude Code and changes between versions" — the transcript meter is a fallback only.
- Observed data point: a 1M-window Opus session at `cache_read 498 344` ≈ 50 % used.
- Reading the tail is enough: the file can be ~10 MB; the fallback reads the last 256 KB and scans backwards.

## 5. CLI flags (VERIFIED from docs + `claude --help` on 2.1.280)

Present in 2.1.280 `--help`: `--permission-mode` (choices `acceptEdits`, `auto`, `bypassPermissions`, `manual`(= `default`), `plan`, `dontAsk`), `--session-id <uuid>`, `--name`, `--append-system-prompt`, `--resume`, `--continue`, `--fork-session`, `--print`, `--settings`, `--setting-sources`, `--model`, `--effort`, `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`.

- `claude "prompt"` starts an **interactive** session with an initial prompt. `-p` is non-interactive and is not used for the relayed session.
- `--session-id <uuid>` lets the launcher pre-assign the child session id so the parent can write parent/child linkage **before** the child starts.
- `--resume <id|name|absolute transcript path>`; `--fork-session` creates a new id when resuming. The relay does not resume; it starts fresh and injects the handoff.
- `--desktop` (open the Desktop app on this directory / session) requires **v2.1.285+** → not available from the PATH CLI 2.1.280. UNVERIFIED on the bundled 2.1.288 binary; offered as an opt-in launcher mode only.

### Permission mode at start (VERIFIED from docs/en/permission-modes)

Order: `--permission-mode` / `--dangerously-skip-permissions` → `permissions.defaultMode` from settings (**`auto` and `bypassPermissions` are ignored when set in `.claude/settings.json` or `.claude/settings.local.json`**; they work from user/managed settings or the flag) → built-in default (`auto` for interactive terminal sessions on v2.1.283+, otherwise plan-dependent). The Desktop app remembers the mode per folder. `--permission-mode auto` falls back to Manual when auto mode is disabled/unavailable for the account.

Design consequence: the launcher passes `--permission-mode <parent permission_mode>` taken from the Stop hook's stdin, mapping `default` → `default`. It never adds `bypassPermissions` unless the parent was already in it **and** `config.allow_bypass_inherit=true`; the README documents the trade-off.

## 6. Settings precedence and merging (VERIFIED from docs/en/settings)

Highest first: managed → `--settings` → `.claude/settings.local.json` → `.claude/settings.json` → `~/.claude/settings.json`. Arrays (including `hooks` entries and `permissions.allow`) **merge across scopes**; identical hook handlers are de-duplicated. `statusLine` is a single object key, so the highest-precedence file that sets it wins (INFERRED from "lists merge instead of overriding"; the current `~/.claude/settings.json` has no `statusLine`, so no conflict today). Settings hot-reload; `ConfigChange` fires on edits. `.claude/settings.local.json` is added to the global git excludes only when Claude Code creates it; a hand-made one must be gitignored manually. Transcript retention `cleanupPeriodDays` defaults to 30.

## 7. Memory and CLAUDE.md (VERIFIED from docs/en/memory)

- Load order: `~/.claude/CLAUDE.md` → `./CLAUDE.md` or `./.claude/CLAUDE.md` (+ `CLAUDE.local.md`) from root down to cwd; subdirectory files load on demand. Target under 200 lines; a file up to 4 MiB loads in full. Block-level HTML comments are stripped.
- Imports: `@path/to/file`, relative to the importing file, max 4 hops, **skipped inside backticks/code fences**. An import that resolves outside the working directory triggers a one-time approval dialog.
- `.claude/rules/*.md` without `paths:` frontmatter load at launch with the same priority as `.claude/CLAUDE.md`.
- **Auto memory** (Claude's own notes) lives at `~/.claude/projects/<project>/memory/MEMORY.md` (first 200 lines / 25 KB loaded) — this is distinct from the project-level `.claude/memory/` the relay maintains. Auto memory is **turned off in a session that another Claude Code session started** (e.g. via the Bash tool). The launcher therefore scrubs `CLAUDECODE` and `CLAUDE_CODE_*` from the child's environment and launches through a fresh terminal/tmux shell so the child is a first-class session. UNVERIFIED: which exact variable Claude Code uses for that detection; scrubbing all of them is the safe superset.

## 8. Compaction (VERIFIED from docs/en/context-window)

Auto-compaction runs "as you approach the limit"; the exact per-model threshold is on docs/en/model-config (not fetched; UNVERIFIED numbers). The relay's thresholds (40 % / 50 %) sit far below any auto-compact point for both 200k and 1M windows, so the Stop flow normally runs before compaction. If compaction does happen first, `SessionStart(source=compact)` fires and the relay re-injects only a short pointer; `PreCompact` snapshots the transcript.

## 9. Desktop-app specifics observed in this session (VERIFIED by execution)

- This session is hosted by the Desktop app (`claude-desktop`, host session `local_…`). A session launched by the relay into Terminal.app/tmux is a **CLI** session; it will not appear inside the Desktop app's Code tab. The Desktop app can resume CLI sessions by id (docs/en/sessions), and `claude --desktop --resume <id>` exists on 2.1.285+, so an opt-in `launcher.mode = "desktop"` using `$CLAUDE_CODE_EXECPATH` is possible but UNVERIFIED and off by default.
- `CLAUDE_CODE_SESSION_ID` is exported to hook/Bash children and matches `session_id` in the transcript.

## 10. What could not be verified, and how the design copes

| Unverified | Mitigation |
| --- | --- |
| Whether the statusLine command runs with `$CLAUDE_PROJECT_DIR` | meter uses `cwd`/`workspace.project_dir` from stdin |
| Window size from the transcript alone | fallback infers from model id, flags `approx: true`, prefers a fresh state file's `window_size` |
| Exact auto-compact thresholds per model | thresholds 40/50 are below any documented point; PreCompact/compact-source hooks cover the other case |
| Which env var marks a "nested" session (auto memory off) | scrub all `CLAUDE_CODE_*` + `CLAUDECODE` and launch via a fresh shell |
| `claude --desktop` on the bundled binary | opt-in launcher mode, documented as experimental |
| `statusLine` precedence when both user and project set it | INFERRED object-override; user file has none today; install.sh warns if it finds one |
| Transcript format stability | fallback only; primary meter is the documented statusLine payload |

## Appendix A — statusLine payload example quoted from the docs

```json
{
  "cwd": "/current/working/directory",
  "session_id": "abc123...",
  "session_name": "my-session",
  "prompt_id": "550e8400-e29b-41d4-a716-446655440000",
  "transcript_path": "/path/to/transcript.jsonl",
  "model": {
    "id": "claude-opus-5-5",
    "display_name": "Opus"
  },
  "workspace": {
    "current_dir": "/current/working/directory",
    "project_dir": "/original/project/directory",
    "added_dirs": [],
    "git_worktree": "feature-xyz",
    "repo": {
      "host": "github.com",
      "owner": "anthropics",
      "name": "claude-code"
    }
  },
  "version": "2.1.90",
  "output_style": {
    "name": "default"
  },
  "cost": {
    "total_cost_usd": 0.01234,
    "total_duration_ms": 45000,
    "total_api_duration_ms": 2300,
    "total_lines_added": 156,
    "total_lines_removed": 23
  },
  "context_window": {
    "total_input_tokens": 15500,
    "total_output_tokens": 1200,
    "context_window_size": 200000,
    "used_percentage": 8,
    "remaining_percentage": 92,
    "current_usage": {
      "input_tokens": 8500,
      "output_tokens": 1200,
      "cache_creation_input_tokens": 5000,
      "cache_read_input_tokens": 2000
    }
  },
  "exceeds_200k_tokens": false,
  "prompt_cache": {
    "warm": true,
    "caching_observed": true,
    "ttl": "1h",
    ...
  }
}
```
