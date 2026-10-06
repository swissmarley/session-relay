#!/bin/sh
# Session Relay uninstaller.
#
#   uninstall.sh                    remove relay hooks/statusLine from <project>/.claude/settings.json
#   uninstall.sh --user             remove them from ~/.claude/settings.json (and ~/.claude/relay/)
#   uninstall.sh --project-dir DIR  choose the project
#   uninstall.sh --purge            also delete runtime data: .claude/state, backups, handoffs,
#                                   ledger, log, lock, launched, run. Never deletes .claude/memory.
#   uninstall.sh --dry-run
#
# Only entries whose command points into a relay/bin directory are removed; every other
# hook, setting and CLAUDE.md line is left exactly as it was. A backup is written first.

set -u

here=$(cd "$(dirname "$0")" && pwd)
relay_src=$(dirname "$here")
mode="project"; project_dir=""; purge=0; dry_run=0

while [ $# -gt 0 ]; do
  case "$1" in
    --user) mode="user"; shift ;;
    --project-dir) project_dir=$2; shift 2 ;;
    --purge) purge=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) printf 'uninstall.sh: unknown argument %s\n' "$1" >&2; exit 1 ;;
  esac
done

if [ -z "$project_dir" ]; then
  project_dir=$(dirname "$(dirname "$relay_src")")
fi
project_dir=$(cd "$project_dir" 2>/dev/null && pwd) || { printf 'uninstall.sh: project dir not found\n' >&2; exit 1; }

if [ "$mode" = "user" ]; then
  settings="$HOME/.claude/settings.json"
else
  settings="$project_dir/.claude/settings.json"
fi

say() { printf '%s\n' "$*"; }
run() { if [ "$dry_run" -eq 1 ]; then say "  [dry-run] $*"; else "$@"; fi; }

say "Session Relay uninstall ($mode)"
say "  project:  $project_dir"
say "  settings: $settings"

if [ -f "$settings" ]; then
  backup="$settings.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  run cp "$settings" "$backup"
  say "  backup:   $backup"
  RELAY_SETTINGS="$settings" RELAY_DRY_RUN="$dry_run" python3 - <<'PY'
import json, os, sys, tempfile

settings = os.environ["RELAY_SETTINGS"]
dry = os.environ["RELAY_DRY_RUN"] == "1"
MARK = "/relay/bin/"
with open(settings, encoding="utf-8") as fh:
    raw = fh.read().strip()
try:
    data = json.loads(raw) if raw else {}
except ValueError as exc:
    print(f"uninstall.sh: {settings} is not valid JSON ({exc}); not touching it", file=sys.stderr)
    sys.exit(1)

removed = []
hooks = data.get("hooks")
if isinstance(hooks, dict):
    for event in list(hooks):
        kept = []
        for entry in hooks.get(event) or []:
            if not isinstance(entry, dict):
                kept.append(entry); continue
            hs = [h for h in entry.get("hooks", []) if not (isinstance(h, dict) and MARK in str(h.get("command", "")))]
            if len(hs) != len(entry.get("hooks", [])):
                removed.append(f"hooks.{event}")
            if hs:
                entry["hooks"] = hs
                kept.append(entry)
            elif not entry.get("hooks"):
                kept.append(entry)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del data["hooks"]
sl = data.get("statusLine")
if isinstance(sl, dict) and MARK in str(sl.get("command", "")):
    del data["statusLine"]
    removed.append("statusLine")

if not removed:
    print("  settings: no relay entries found")
    sys.exit(0)
print("  settings: removing " + ", ".join(sorted(set(removed))))
if dry:
    sys.exit(0)
fd, tmp = tempfile.mkstemp(prefix=".settings-", dir=os.path.dirname(settings))
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
os.replace(tmp, settings)
PY
  rc=$?
  [ "$rc" -ne 0 ] && exit "$rc"
else
  say "  settings: file not found, nothing to remove"
fi

# CLAUDE.md pointer line
claude_md="$project_dir/CLAUDE.md"
pointer="@.claude/memory/INDEX.md"
if [ -f "$claude_md" ] && grep -qxF "$pointer" "$claude_md"; then
  if [ "$dry_run" -eq 1 ]; then
    say "  [dry-run] remove '$pointer' from $claude_md"
  else
    tmp="$claude_md.tmp.$$"
    grep -vxF "$pointer" "$claude_md" >"$tmp" && mv -f "$tmp" "$claude_md"
    say "  CLAUDE.md: pointer removed"
  fi
fi

if [ "$mode" = "user" ] && [ -d "$HOME/.claude/relay" ]; then
  run rm -rf "$HOME/.claude/relay"
  say "  removed ~/.claude/relay"
fi

if [ "$purge" -eq 1 ]; then
  for p in state backups handoffs relay/ledger.jsonl relay/relay.log relay/relay.log.1 relay/relay.log.2 \
           relay/relay.log.3 relay/lock relay/launched relay/run relay/NEXT_COMMAND.txt; do
    target="$project_dir/.claude/$p"
    [ -e "$target" ] && run rm -rf "$target"
  done
  say "  purged runtime data (memory kept)"
fi

say "Done. Restart Claude Code sessions for the change to take effect."
exit 0
