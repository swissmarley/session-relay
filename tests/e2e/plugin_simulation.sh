#!/usr/bin/env bash
# Session Relay end-to-end simulation, plugin mode.
#
# Drives the REAL plugin (plugins/session-relay) through a whole relay chain in a
# throw-away project and a throw-away HOME. Every hook runs through the exact command
# string in hooks/hooks.json, with CLAUDE_PLUGIN_ROOT, CLAUDE_PROJECT_DIR and the
# /config options exported the way Claude Code exports them to plugin hooks. The meter
# mod's readings are written in the format hooks/meter.mjs writes (its own behaviour is
# covered by `claude plugin test plugins/session-relay`). No Claude session is started:
# the launcher runs in "file" mode.
#
# Covered: opt-in (nothing runs or is created until /session-relay:enable), memory
# injection without touching CLAUDE.md, the mod reading winning over the transcript
# estimate (a 1M window the transcript would read as 200k), the handoff chain, and a
# clean `git status` from start to finish.
#
#   tests/e2e/plugin_simulation.sh          run it
#   tests/e2e/plugin_simulation.sh --keep   keep the temp project and HOME for inspection
#
# Exits non-zero at the first failed check.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PLUGIN="$ROOT/plugins/session-relay"
KEEP=0
for a in "$@"; do
  case "$a" in
    --keep) KEEP=1 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "unknown argument: $a" >&2; exit 1 ;;
  esac
done

TMP=$(mktemp -d "${TMPDIR:-/tmp}/relay-plugin-e2e.XXXXXX")
PROJECT="$TMP/project"
OUT="$TMP/last-output.json"
cleanup() { if [ "$KEEP" -eq 1 ]; then echo "kept: $TMP"; else rm -rf "$TMP"; fi; }
trap cleanup EXIT

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m  %s\n' "$*"; }
fail() { printf '   \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
check() { # check <description> <command...>
  local d=$1; shift
  if "$@" >/dev/null 2>&1; then ok "$d"; else fail "$d"; fi
}
jcheck() { # jcheck <description> <jq boolean expression over $OUT>
  local d=$1 expr=$2
  if [ -s "$OUT" ] && jq -e "$expr" "$OUT" >/dev/null 2>&1; then ok "$d"; else fail "$d"; fi
}
clean_git() { [ -z "$(git -C "$PROJECT" status --porcelain --untracked-files=all)" ]; }

# --- isolated environment, as Claude Code sets it up for a plugin hook --------------
for v in $(env | sed -n 's/^\(CLAUDE_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$v"; done
unset CLAUDECODE TMUX TMUX_PANE USERPROFILE
export HOME="$TMP/home"
export CLAUDE_PLUGIN_ROOT="$PLUGIN"
export CLAUDE_PROJECT_DIR="$PROJECT"
# /config values: wrap up at 5 %, hand off at 10 %
export CLAUDE_PLUGIN_OPTION_WRAP_UP_THRESHOLD=5
export CLAUDE_PLUGIN_OPTION_HANDOFF_THRESHOLD=10
export CLAUDE_PLUGIN_OPTION_ACTIVATION=always
USER_DIR="$HOME/.claude/session-relay"
DATA="$PROJECT/.claude/session-relay"
RELAY="$PLUGIN/scripts/relay.py"

hook() { # hook <Event> <json payload>: run the command hooks.json registers for <Event>
  local cmd
  cmd=$(jq -r --arg ev "$1" '.hooks[$ev][0].hooks[0].command' "$PLUGIN/hooks/hooks.json")
  printf '%s' "$2" | sh -c "$cmd"
}

step "1. Plugin manifests, a temp HOME and a temp project"
mkdir -p "$HOME/.claude" "$PROJECT"
# Where Claude Code keeps the /config values (and exports them to hooks as above).
jq -n '{pluginConfigs: {"session-relay@session-relay": {options:
  {wrap_up_threshold: 5, handoff_threshold: 10, activation: "always"}}}}' >"$HOME/.claude/settings.json"
check "plugin.json is version 0.2.0" jq -e '.version=="0.2.0"' "$PLUGIN/.claude-plugin/plugin.json"
check "hooks.json registers the five hooks and the meter mod" \
  jq -e '(.hooks|keys|sort)==["PreCompact","SessionEnd","SessionStart","Stop","UserPromptSubmit"] and .modules==["./meter.mjs"]' \
  "$PLUGIN/hooks/hooks.json"
(
  cd "$PROJECT" && git init -q -b main
  printf 'print("hello")\n' >app.py
  mkdir -p .claude/memory
  printf '# Project memory index\n\n- [decisions.md](decisions.md) — use tabs, never spaces\n' >.claude/memory/INDEX.md
  printf '# Decisions\n\n- Use tabs, never spaces.\n' >.claude/memory/decisions.md
  git add -A && git -c user.email=e2e@example.com -c user.name=e2e commit -q -m init
)
cp "$ROOT/tests/fixtures/handoff_good.md" "$TMP/handoff_good.md"
check "git status starts clean" clean_git

# User config: opt-in, launcher in file mode, no cooldown (the /config option above says
# "always"; the user config is a higher layer and wins).
mkdir -p "$USER_DIR"
printf '{"activation": "opt-in", "cooldown_seconds": 0, "launcher": {"mode": "file"}}\n' >"$USER_DIR/config.json"

PARENT="e2e-parent-$(date +%s)"
TRANSCRIPT="$TMP/$PARENT.jsonl"
cp "$ROOT/tests/fixtures/transcript.jsonl" "$TRANSCRIPT"   # 90k input tokens: 45 % if read as a 200k window

payload() { # payload <event> [extra json object] [session id]
  local event=$1 extra=${2:-{\}} sid=${3:-$PARENT}
  jq -n -c --arg sid "$sid" --arg tp "$TRANSCRIPT" --arg cwd "$PROJECT" --arg ev "$event" --argjson extra "$extra" \
    '{session_id:$sid, transcript_path:$tp, cwd:$cwd, hook_event_name:$ev, permission_mode:"acceptEdits"} + $extra'
}
mod_meter() { # mod_meter <percent|null> <window>: what hooks/meter.mjs writes for $PARENT
  mkdir -p "$USER_DIR/meter"
  jq -n -c --arg sid "$PARENT" --argjson pct "$1" --argjson win "$2" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{session_id:$sid, used_pct:$pct, window_size:$win,
      input_total:(if $pct == null then null else ($pct * $win / 100 | floor) end),
      ts:$ts, source:"mod", approx:false}' >"$USER_DIR/meter/$PARENT.json"
}

step "2. Opt-in: before /session-relay:enable no hook does anything"
mod_meter 50 1000000
for ev in SessionStart UserPromptSubmit Stop PreCompact SessionEnd; do
  out=$(hook "$ev" "$(payload "$ev" '{"source":"startup","stop_hook_active":false,"prompt":"hi","trigger":"auto","reason":"other"}')")
  check "$ev prints nothing" test -z "$out"
done
check "no .claude/session-relay folder was created" test ! -e "$DATA"
check "git status still clean" clean_git

step "3. /session-relay:enable (the skill runs relay.py enable)"
python3 "$RELAY" enable --plugin-root "$PLUGIN" --cwd "$PROJECT" >"$OUT"
jcheck "enabled for this project" '.enabled==true and .active==true and .activation=="opt-in"'
check ".gitignore holds * and !config.json" bash -c "[ \"\$(cat '$DATA/.gitignore')\" = \"\$(printf '*\n!config.json')\" ]"
check "only config.json shows up in git" \
  bash -c "[ \"\$(git -C '$PROJECT' status --porcelain --untracked-files=all)\" = '?? .claude/session-relay/config.json' ]"
( cd "$PROJECT" && git add .claude/session-relay/config.json && git -c user.email=e2e@example.com -c user.name=e2e commit -q -m "enable session relay" )
check "after committing the choice, git status is clean" clean_git

step "4. Session start: the memory index is injected, CLAUDE.md untouched"
hook SessionStart "$(payload SessionStart '{"source":"startup"}')" >"$OUT"
jcheck "INDEX.md injected" '.hookSpecificOutput.additionalContext | (test("injected by Session Relay") and test("use tabs, never spaces"))'
check "no CLAUDE.md created" test ! -e "$PROJECT/CLAUDE.md"

step "5. The mod's reading wins over the transcript estimate"
rm -f "$USER_DIR/meter/$PARENT.json"     # no mod reading yet (e.g. Claude Code older than 2.1.287)
python3 "$RELAY" meter --plugin-root "$PLUGIN" --session-id "$PARENT" --transcript "$TRANSCRIPT" --cwd "$PROJECT" >"$OUT"
jcheck "transcript alone: 45 % of a guessed 200k window" '.source=="transcript" and .used_pct==45 and .window_size==200000'
mod_meter null 1000000
python3 "$RELAY" meter --plugin-root "$PLUGIN" --session-id "$PARENT" --transcript "$TRANSCRIPT" --cwd "$PROJECT" >"$OUT"
jcheck "mod window without a percentage: the estimate uses the real 1M window" '.source=="transcript" and .used_pct==9 and .window_size==1000000'
mod_meter 9 1000000
hook UserPromptSubmit "$(payload UserPromptSubmit '{"prompt":"continue"}')" >"$OUT"
jcheck "9 % (mod): wrap-up notice injected (soft 5 %)" '.hookSpecificOutput.additionalContext | test("Wrap up gracefully") and (test("approximate") | not)'
out=$(hook Stop "$(payload Stop '{"stop_hook_active":false,"last_assistant_message":"working"}')")
check "9 % (mod) is below the 10 % handoff threshold: Stop does not block, although the transcript says 45 %" test -z "$out"

step "6. Mod reading 12 %: Stop blocks and asks for a handoff"
mod_meter 12 1000000
hook Stop "$(payload Stop '{"stop_hook_active":false,"last_assistant_message":"I was editing app.py"}')" >"$OUT"
jcheck "decision=block at 12 %" '.decision=="block" and (.reason|test("12%"))'
# shellcheck disable=SC2016  # backticks are literal characters in the grep pattern
HANDOFF=$(jq -r '.reason' "$OUT" | grep -o '`[^`]*_'"$PARENT"'\.md`' | head -1 | tr -d '`')
echo "   requested handoff path: $HANDOFF"
check "handoff goes to .claude/session-relay/handoffs" bash -c "case '$HANDOFF' in '$DATA/handoffs/'*) true;; *) false;; esac"
check "ledger lives in .claude/session-relay" test -f "$DATA/ledger.jsonl"

step "7. The session writes the handoff and stops again: the next session is launched"
sed "s/session_id: sess-parent-1/session_id: $PARENT/" "$TMP/handoff_good.md" >"$HANDOFF"
check "handoff validates" python3 "$RELAY" validate "$HANDOFF" --plugin-root "$PLUGIN" --cwd "$PROJECT"
hook Stop "$(payload Stop '{"stop_hook_active":true,"last_assistant_message":"Handoff written."}')" >"$OUT"
jcheck "stop no longer blocks" 'has("decision")|not'
echo "   systemMessage: $(jq -r '.systemMessage' "$OUT")"
check "NEXT_COMMAND.txt in .claude/session-relay (file mode)" test -f "$DATA/NEXT_COMMAND.txt"
CHILD=$(jq -r '.child_session_id' "$DATA/state/$PARENT.relay.json")
echo "   child session id: $CHILD"
check "runner script in .claude/session-relay/run" test -f "$DATA/run/$CHILD.sh"
check "runner never skips permissions" bash -c "! grep -q dangerously '$DATA/run/$CHILD.sh'"
check "git status clean during the chain" clean_git

step "8. The child starts: handoff consumed once, with the memory index"
child() { jq -n -c --arg sid "$CHILD" --arg cwd "$PROJECT" --arg src "$1" \
  '{session_id:$sid, transcript_path:"/nonexistent.jsonl", cwd:$cwd, hook_event_name:"SessionStart", source:$src}'; }
hook SessionStart "$(child startup)" >"$OUT"
jcheck "handoff, ledger and memory index injected in one context" \
  '.hookSpecificOutput.additionalContext | (test("generation 1") and test("Recent relay ledger") and test("use tabs, never spaces"))'
# shellcheck disable=SC2016  # backticks are literal characters in the jq pattern
jcheck "the child is told not to edit CLAUDE.md" '.hookSpecificOutput.additionalContext | test("Do not edit `CLAUDE.md`")'
check "status flipped to consumed" grep -q "^status: consumed" "$HANDOFF"
hook SessionStart "$(child startup)" >"$OUT"
jcheck "a second startup gets the index only" \
  '.hookSpecificOutput.additionalContext | (test("injected by Session Relay") and (test("generation 1") | not))'

step "9. PreCompact and SessionEnd"
hook PreCompact "$(payload PreCompact '{"trigger":"auto"}' "$CHILD")"
check "backup in .claude/session-relay/backups" bash -c "ls -d '$DATA'/backups/*_$CHILD"
hook SessionEnd "$(payload SessionEnd '{"reason":"other"}')"
check "the parent's meter reading is dropped at session end" test ! -e "$USER_DIR/meter/$PARENT.json"
check "session_end recorded" bash -c "tail -n 1 '$DATA/ledger.jsonl' | jq -e '.event==\"session_end\"'"

step "10. Status, status line, and the project at the end"
python3 "$RELAY" status --plugin-root "$PLUGIN" --cwd "$PROJECT" >"$OUT"
jcheck "status: plugin mode, active, opt-in" '.mode=="plugin" and .active==true and .config.activation=="opt-in" and .config.hard==10'
python3 "$RELAY" install-statusline --plugin-root "$PLUGIN" >"$OUT"
jcheck "/session-relay:statusline wires statusLine in user settings" '.status=="installed"'
check "statusLine points at the per-user copy, other settings kept" \
  jq -e '.pluginConfigs["session-relay@session-relay"].options.handoff_threshold==10' "$HOME/.claude/settings.json"
# shellcheck disable=SC2016  # $HOME is literal: the settings file stores it unexpanded
check "statusLine command" \
  jq -e '.statusLine.command=="bash \"$HOME/.claude/session-relay/statusline.sh\" --plugin"' "$HOME/.claude/settings.json"
line=$(jq -n -c --arg sid "$CHILD" --arg cwd "$PROJECT" '{session_id:$sid,cwd:$cwd,model:{display_name:"Opus"},context_window:{used_percentage:7,context_window_size:1000000,total_input_tokens:70000}}' \
  | bash "$USER_DIR/statusline.sh" --plugin)
echo "   child status line: $line"
check "status line shows generation 1 and the wrap-up marker" bash -c "echo '$line' | grep -q ' g1 ~soft'"
check "CLAUDE.md never created" test ! -e "$PROJECT/CLAUDE.md"
check ".claude/memory unchanged" git -C "$PROJECT" diff --quiet -- .claude/memory
check "git status clean at the end" clean_git
echo "   .claude/session-relay:"; (cd "$DATA" && find . -maxdepth 1 | sort | sed 's/^/     /')

step "11. Ledger"
jq -r '"   \(.ts)  \(.event | . + " " * (24 - length))  session=\(.session_id[0:12])  gen=\(.generation)  used=\(.used_pct)"' "$DATA/ledger.jsonl"

printf '\n\033[32mPlugin E2E simulation passed.\033[0m\n'
