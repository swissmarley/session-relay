#!/usr/bin/env bash
# Session Relay end-to-end simulation.
#
# Drives the REAL relay scripts through a whole relay chain in a throw-away project,
# feeding them the JSON payloads Claude Code would send, with thresholds at 1-2 % so a
# trivial amount of fake context triggers everything. No Claude session is started:
# the launcher runs in "file" mode, so the result is a ready-to-run command in
# NEXT_COMMAND.txt instead of a new terminal window.
#
#   tests/e2e/run_simulation.sh              full chain with a written handoff
#   tests/e2e/run_simulation.sh --mechanical the previous session never writes one
#   tests/e2e/run_simulation.sh --keep       keep the temp project for inspection
#
# Exits non-zero at the first failed check.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
RELAY_SRC="$ROOT/.claude/relay"
MECHANICAL=0
KEEP=0
for a in "$@"; do
  case "$a" in
    --mechanical) MECHANICAL=1 ;;
    --keep) KEEP=1 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown argument: $a" >&2; exit 1 ;;
  esac
done

TMP=$(mktemp -d "${TMPDIR:-/tmp}/relay-e2e.XXXXXX")
PROJECT="$TMP/project"
OUT="$TMP/last-output.json"
cleanup() { if [ "$KEEP" -eq 1 ]; then echo "kept: $PROJECT"; else rm -rf "$TMP"; fi; }
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

# --- isolated environment: no inherited Claude Code variables -------------------
for v in $(env | sed -n 's/^\(CLAUDE_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$v"; done
unset CLAUDECODE TMUX TMUX_PANE
export CLAUDE_PROJECT_DIR="$PROJECT"

step "1. Temp project with the relay installed (thresholds soft=1 hard=2, launcher mode=file)"
mkdir -p "$PROJECT"
( cd "$PROJECT" && git init -q -b main && git -c user.email=e2e@example.com -c user.name=e2e commit -q --allow-empty -m init )
sh "$RELAY_SRC/bin/install.sh" --project-dir "$PROJECT" >/dev/null
CFG="$PROJECT/.claude/relay/config.json"
python3 - "$CFG" <<'PY'
import json, sys
p = sys.argv[1]
cfg = json.load(open(p))
cfg.update(soft=1, hard=2, cooldown_seconds=0)
cfg["launcher"]["mode"] = "file"
json.dump(cfg, open(p, "w"), indent=2)
PY
export CLAUDE_RELAY_CONFIG="$CFG"
RELAY="$PROJECT/.claude/relay/bin/relay.py"
SL="$PROJECT/.claude/relay/bin/statusline.sh"
check "settings.json has the five hooks + statusLine" \
  python3 -c "import json,sys; d=json.load(open('$PROJECT/.claude/settings.json')); assert set(d['hooks'])=={'SessionStart','UserPromptSubmit','Stop','PreCompact','SessionEnd'} and 'statusLine' in d"

PARENT="e2e-parent-$(date +%s)"
TRANSCRIPT="$TMP/$PARENT.jsonl"
cp "$ROOT/tests/fixtures/transcript.jsonl" "$TRANSCRIPT"   # 90k tokens of fake usage for the fallback meter

payload() { # payload <event> [extra json object]
  local event=$1 extra=${2:-{\}}
  jq -n -c --arg sid "$PARENT" --arg tp "$TRANSCRIPT" --arg cwd "$PROJECT" --arg ev "$event" --argjson extra "$extra" \
    '{session_id:$sid, transcript_path:$tp, cwd:$cwd, hook_event_name:$ev, permission_mode:"acceptEdits"} + $extra'
}
status_payload() { # status_payload <used_pct>
  jq -n -c --arg sid "$PARENT" --arg tp "$TRANSCRIPT" --arg cwd "$PROJECT" --argjson pct "$1" \
    '{session_id:$sid, transcript_path:$tp, cwd:$cwd, model:{id:"claude-opus-5-5",display_name:"Opus"},
      workspace:{project_dir:$cwd}, context_window:{total_input_tokens:(($pct*2000)|floor), total_output_tokens:10,
      context_window_size:200000, used_percentage:$pct, remaining_percentage:(100-$pct)}}'
}

step "2. statusLine meter at 0.5 % (below both thresholds)"
line=$(status_payload 0.5 | bash "$SL"); echo "   status line: $line"
check "state file written atomically" test -f "$PROJECT/.claude/state/$PARENT.json"
check "prompt hook silent below soft" test -z "$(payload UserPromptSubmit '{"prompt":"hi"}' | python3 "$RELAY" prompt)"
check "stop hook silent below hard"   test -z "$(payload Stop '{"stop_hook_active":false,"last_assistant_message":"done"}' | python3 "$RELAY" stop)"

step "3. Soft trigger at 1.5 %"
line=$(status_payload 1.5 | bash "$SL"); echo "   status line: $line"
payload UserPromptSubmit '{"prompt":"continue"}' | python3 "$RELAY" prompt >"$OUT"
jcheck "additionalContext injected once" '.hookSpecificOutput.additionalContext | test("Wrap up gracefully")'
check "second prompt is silent" test -z "$(payload UserPromptSubmit '{"prompt":"more"}' | python3 "$RELAY" prompt)"

step "4. Hard trigger at 2.5 %: Stop hook blocks and asks for a handoff"
line=$(status_payload 2.5 | bash "$SL"); echo "   status line: $line"
payload Stop '{"stop_hook_active":false,"last_assistant_message":"I was editing src/app.py"}' | python3 "$RELAY" stop >"$OUT"
jcheck "decision=block" '.decision=="block"'
# shellcheck disable=SC2016  # backticks are literal characters in the grep pattern
HANDOFF=$(jq -r '.reason' "$OUT" | grep -o '`[^`]*_'"$PARENT"'\.md`' | head -1 | tr -d '`')
echo "   requested handoff path: $HANDOFF"
echo "   reason (first lines):"; jq -r '.reason' "$OUT" | head -4 | sed 's/^/     | /'

if [ "$MECHANICAL" -eq 1 ]; then
  step "5m. The session never writes the handoff: second block, then mechanical fallback"
  payload Stop '{"stop_hook_active":true,"last_assistant_message":"still working"}' | python3 "$RELAY" stop >"$OUT"
  jcheck "second block mentions the missing file" '.decision=="block" and (.reason|test("no handoff file"))'
  payload Stop '{"stop_hook_active":true,"last_assistant_message":"I was editing src/app.py and running pytest -x"}' | python3 "$RELAY" stop >"$OUT"
  jcheck "third stop does not block" 'has("decision")|not'
  check "mechanical handoff written at the proposed path" test -f "$HANDOFF"
  check "it carries the last assistant message" grep -q "running pytest -x" "$HANDOFF"
  check "it validates" python3 "$RELAY" validate "$HANDOFF" --cwd "$PROJECT"
else
  step "5. Simulate the session writing a valid handoff, then stopping again"
  sed "s/session_id: sess-parent-1/session_id: $PARENT/" "$ROOT/tests/fixtures/handoff_good.md" >"$HANDOFF"
  check "handoff validates" python3 "$RELAY" validate "$HANDOFF" --cwd "$PROJECT"
  payload Stop '{"stop_hook_active":true,"last_assistant_message":"Handoff written."}' | python3 "$RELAY" stop >"$OUT"
  jcheck "stop no longer blocks" 'has("decision")|not'
fi
echo "   systemMessage: $(jq -r '.systemMessage' "$OUT")"
check "launcher deferred to NEXT_COMMAND.txt (file mode)" test -f "$PROJECT/.claude/relay/NEXT_COMMAND.txt"
CHILD=$(jq -r '.child_session_id' "$PROJECT/.claude/state/$PARENT.relay.json")
echo "   child session id: $CHILD"
check "child pre-linked (generation 1, parent set)" \
  bash -c "jq -e '.generation==1 and .parent==\"$PARENT\" and .handoff_path==\"$HANDOFF\"' '$PROJECT/.claude/state/$CHILD.relay.json'"
RUNNER="$PROJECT/.claude/relay/run/$CHILD.sh"
check "runner script generated" test -f "$RUNNER"
check "runner inherits permission mode and names the session" grep -q -- "--permission-mode 'acceptEdits' --name 'relay-g1'" "$RUNNER"
check "runner never skips permissions" bash -c "! grep -q dangerously '$RUNNER'"
echo "   NEXT_COMMAND.txt:"; sed 's/^/     | /' "$PROJECT/.claude/relay/NEXT_COMMAND.txt"
check "parent will not launch twice" test -z "$(payload Stop '{"stop_hook_active":false,"last_assistant_message":"x"}' | python3 "$RELAY" stop)"

step "6. Child session starts: SessionStart consumes the handoff exactly once"
child_payload() { jq -n -c --arg sid "$CHILD" --arg cwd "$PROJECT" --arg src "$1" \
  '{session_id:$sid, transcript_path:"/nonexistent.jsonl", cwd:$cwd, hook_event_name:"SessionStart", source:$src}'; }
child_payload startup | python3 "$RELAY" session-start >"$OUT"
jcheck "handoff + ledger + memory index injected" \
  '.hookSpecificOutput.additionalContext | (test("generation 1") and test("Recent relay ledger") and test("memory index"))'
chars=$(jq -r '.hookSpecificOutput.additionalContext | length' "$OUT"); echo "   injected chars: $chars (budget 16000)"
check "injection within budget" test "$chars" -le 16000
check "status flipped to consumed" grep -q "^status: consumed" "$HANDOFF"
check "consumed_by recorded" grep -q "^consumed_by: $CHILD" "$HANDOFF"
check "second startup injects nothing" test -z "$(child_payload startup | python3 "$RELAY" session-start)"
child_payload resume | python3 "$RELAY" session-start >"$OUT"
jcheck "resume gets a short pointer" '.hookSpecificOutput.additionalContext | test("reminder \\(resume\\)") and (length < 600)'
line=$(jq -n -c --arg sid "$CHILD" --arg cwd "$PROJECT" '{session_id:$sid,cwd:$cwd,model:{display_name:"Opus"},context_window:{used_percentage:3,context_window_size:200000,total_input_tokens:6000}}' | bash "$SL")
echo "   child status line: $line"
check "status line shows generation 1" bash -c "echo '$line' | grep -q ' g1'"

step "7. PreCompact backup and SessionEnd in the child"
jq -n -c --arg sid "$CHILD" --arg cwd "$PROJECT" --arg tp "$TRANSCRIPT" '{session_id:$sid,cwd:$cwd,transcript_path:$tp,hook_event_name:"PreCompact",trigger:"auto"}' | python3 "$RELAY" pre-compact
check "backup directory with transcript + snapshot" bash -c "ls -d '$PROJECT'/.claude/backups/*_$CHILD && test -f '$PROJECT'/.claude/backups/*_$CHILD/snapshot.json"
jq -n -c --arg sid "$CHILD" --arg cwd "$PROJECT" '{session_id:$sid,cwd:$cwd,hook_event_name:"SessionEnd",reason:"other"}' | python3 "$RELAY" session-end
check "session_end recorded" bash -c "tail -n 1 '$PROJECT/.claude/relay/ledger.jsonl' | jq -e '.event==\"session_end\" and .generation==1'"

step "8. Ledger (lineage across the chain)"
jq -r '"   \(.ts)  \(.event | . + " " * (24 - length))  session=\(.session_id[0:12])  gen=\(.generation)  used=\(.used_pct)"' "$PROJECT/.claude/relay/ledger.jsonl"

step "9. relay.log tail"
tail -n 5 "$PROJECT/.claude/relay/relay.log" | sed 's/^/   /'

printf '\n\033[32mE2E simulation passed.\033[0m\n'
printf 'To continue the simulated chain for real you would run:  sh %s\n' "$RUNNER"
