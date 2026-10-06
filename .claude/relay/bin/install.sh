#!/bin/sh
# Session Relay installer.
#
# Wires the relay hooks and status line into a Claude Code settings.json without
# touching anything else in that file (a timestamped backup is written first).
#
#   install.sh                      project install: <project>/.claude/settings.json
#   install.sh --user               user install:    ~/.claude/settings.json (+ ~/.claude/relay/)
#   install.sh --project-dir DIR    choose the project (default: the one containing this script)
#   install.sh --no-claude-md       do not add the memory-index pointer line to CLAUDE.md
#   install.sh --no-statusline      do not configure the statusLine
#   install.sh --no-gitignore       do not add runtime paths to .gitignore
#   install.sh --dry-run            print what would change
#
# Re-running is safe: existing relay entries are recognised and not duplicated.

set -u

here=$(cd "$(dirname "$0")" && pwd)
relay_src=$(dirname "$here")          # .../.claude/relay (this checkout)
mode="project"
project_dir=""
want_claude_md=1
want_statusline=1
want_gitignore=1
dry_run=0

while [ $# -gt 0 ]; do
  case "$1" in
    --user) mode="user"; shift ;;
    --project-dir) project_dir=$2; shift 2 ;;
    --no-claude-md) want_claude_md=0; shift ;;
    --no-statusline) want_statusline=0; shift ;;
    --no-gitignore) want_gitignore=0; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) printf 'install.sh: unknown argument %s\n' "$1" >&2; exit 1 ;;
  esac
done

if [ -z "$project_dir" ]; then
  project_dir=$(dirname "$(dirname "$relay_src")")
fi
project_dir=$(cd "$project_dir" 2>/dev/null && pwd) || { printf 'install.sh: project dir not found\n' >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || { printf 'install.sh: python3 is required\n' >&2; exit 1; }
command -v jq >/dev/null 2>&1 || printf 'install.sh: warning: jq not found; the status line needs it\n' >&2

if [ "$mode" = "user" ]; then
  relay_dst="$HOME/.claude/relay"
  settings="$HOME/.claude/settings.json"
  prefix="\"\$HOME\"/.claude/relay/bin"
else
  relay_dst="$project_dir/.claude/relay"
  settings="$project_dir/.claude/settings.json"
  prefix="\"\${CLAUDE_PROJECT_DIR:-.}\"/.claude/relay/bin"
fi

say() { printf '%s\n' "$*"; }
run() { if [ "$dry_run" -eq 1 ]; then say "  [dry-run] $*"; else "$@"; fi; }

say "Session Relay install ($mode)"
say "  project:  $project_dir"
say "  relay:    $relay_dst"
say "  settings: $settings"

# 1. Relay files (bin, template; config only if absent).
if [ "$relay_dst" != "$relay_src" ]; then
  run mkdir -p "$relay_dst/bin"
  for f in relay.py statusline.sh launch.sh install.sh uninstall.sh; do
    run cp "$relay_src/bin/$f" "$relay_dst/bin/$f"
  done
  run cp "$relay_src/handoff-template.md" "$relay_dst/handoff-template.md"
  if [ ! -f "$relay_dst/config.json" ]; then
    run cp "$relay_src/config.json" "$relay_dst/config.json"
  fi
fi

# 2. Per-project data directories and memory seed (project-local even for --user installs).
for d in state handoffs backups memory; do
  run mkdir -p "$project_dir/.claude/$d"
done
seed() {
  f="$project_dir/.claude/memory/$1"
  if [ ! -f "$f" ]; then
    if [ "$dry_run" -eq 1 ]; then say "  [dry-run] seed $f"; else printf '%s\n' "$2" >"$f"; fi
  fi
}
seed INDEX.md "# Project memory index

Durable, project-specific knowledge promoted from session handoffs by the Session Relay.
Keep this index short (one line per topic file). Detail lives in the topic files.

- [decisions.md](decisions.md) — architectural and product decisions, with reasons
- [gotchas.md](gotchas.md) — failure modes, environment quirks, things that bit us
- [conventions.md](conventions.md) — naming, layout, testing and workflow conventions"
seed decisions.md "# Decisions

One bullet per decision: what was decided, why, and (when relevant) what was rejected.
Append new entries at the end; do not rewrite history. Deduplicate before adding."
seed gotchas.md "# Gotchas

One bullet per gotcha: symptom, cause, fix or workaround. Deduplicate before adding."
seed conventions.md "# Conventions

One bullet per convention: what we do and where it applies. Deduplicate before adding."

# 3. settings.json merge (backup first, atomic write, idempotent).
if [ -f "$settings" ]; then
  backup="$settings.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  run cp "$settings" "$backup"
  say "  backup:   $backup"
fi
RELAY_PREFIX="$prefix" RELAY_SETTINGS="$settings" RELAY_WANT_STATUSLINE="$want_statusline" \
RELAY_DRY_RUN="$dry_run" python3 - <<'PY'
import json, os, sys, tempfile

settings = os.environ["RELAY_SETTINGS"]
prefix = os.environ["RELAY_PREFIX"]
want_sl = os.environ["RELAY_WANT_STATUSLINE"] == "1"
dry = os.environ["RELAY_DRY_RUN"] == "1"
MARK = "/relay/bin/"

hooks = {
    "SessionStart": ("session-start", 30),
    "UserPromptSubmit": ("prompt", 10),
    "Stop": ("stop", 120),
    "PreCompact": ("pre-compact", 60),
    "SessionEnd": ("session-end", 10),
}

data = {}
if os.path.isfile(settings):
    with open(settings, encoding="utf-8") as fh:
        raw = fh.read().strip()
    if raw:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            print(f"install.sh: {settings} is not valid JSON ({exc}); not touching it", file=sys.stderr)
            sys.exit(1)
if not isinstance(data, dict):
    print("install.sh: settings root must be an object", file=sys.stderr)
    sys.exit(1)

changed = []
hk = data.setdefault("hooks", {})
for event, (sub, timeout) in hooks.items():
    cmd = f'python3 {prefix}/relay.py {sub}'
    entries = hk.setdefault(event, [])
    present = any(
        isinstance(e, dict) and any(
            isinstance(h, dict) and MARK in str(h.get("command", "")) and str(h.get("command", "")).endswith(" " + sub)
            for h in e.get("hooks", []))
        for e in entries)
    if not present:
        entries.append({"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]})
        changed.append(f"hooks.{event}")

if want_sl:
    sl = data.get("statusLine")
    ours = {"type": "command", "command": f'bash {prefix}/statusline.sh', "padding": 0}
    if not sl:
        data["statusLine"] = ours
        changed.append("statusLine")
    elif MARK in str(sl.get("command", "")):
        if sl != ours:
            data["statusLine"] = ours
            changed.append("statusLine (updated)")
    else:
        print(f"install.sh: statusLine already set to {sl.get('command')!r}; leaving it. "
              f"To use the relay meter, set it to: {ours['command']}", file=sys.stderr)

if not changed:
    print("  settings: already installed, nothing to change")
    sys.exit(0)
print("  settings: adding " + ", ".join(changed))
if dry:
    sys.exit(0)
os.makedirs(os.path.dirname(settings), exist_ok=True)
fd, tmp = tempfile.mkstemp(prefix=".settings-", dir=os.path.dirname(settings))
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
os.replace(tmp, settings)
PY
rc=$?
[ "$rc" -ne 0 ] && exit "$rc"

# 4. CLAUDE.md pointer (one import line; everything else untouched).
if [ "$want_claude_md" -eq 1 ]; then
  claude_md="$project_dir/CLAUDE.md"
  pointer="@.claude/memory/INDEX.md"
  if [ -f "$claude_md" ] && grep -qxF "$pointer" "$claude_md"; then
    say "  CLAUDE.md: pointer present"
  elif [ "$dry_run" -eq 1 ]; then
    say "  [dry-run] append '$pointer' to $claude_md"
  else
    if [ -f "$claude_md" ]; then
      printf '\n%s\n' "$pointer" >>"$claude_md"
    else
      printf '# Project instructions\n\n%s\n' "$pointer" >"$claude_md"
    fi
    say "  CLAUDE.md: pointer added"
  fi
fi

# 5. .gitignore for runtime data (project installs only).
if [ "$mode" = "project" ] && [ "$want_gitignore" -eq 1 ]; then
  gi="$project_dir/.gitignore"
  if [ -f "$gi" ] && grep -q '^\.claude/state/$' "$gi"; then
    say "  .gitignore: relay entries present"
  elif [ "$dry_run" -eq 1 ]; then
    say "  [dry-run] append relay entries to $gi"
  else
    {
      printf '\n# Session Relay runtime data\n'
      printf '.claude/state/\n.claude/backups/\n.claude/handoffs/\n'
      printf '.claude/relay/relay.log*\n.claude/relay/lock/\n.claude/relay/launched/\n'
      printf '.claude/relay/run/\n.claude/relay/NEXT_COMMAND.txt\n.claude/relay/ledger.jsonl\n'
      printf '.claude/settings.json.bak.*\n'
    } >>"$gi"
    say "  .gitignore: relay entries added"
  fi
fi

say ""
say "Done. Start a new Claude Code session in $project_dir (hooks load at startup)."
say "Kill switch: CLAUDE_RELAY_DISABLE=1. Config: $relay_dst/config.json. Docs: docs/session-relay/README.md"
exit 0
