#!/usr/bin/env bash
# Session Relay: status line + context meter.
#
# Reads the statusLine JSON on stdin, writes .claude/state/<session_id>.json
# atomically (temp file + mv), and prints one compact line:
#   [Opus] ctx 42% g1          (g = relay generation; markers: ~soft / !hard)
#
# With --plugin (the copy /session-relay:statusline installs in
# ${CLAUDE_CONFIG_DIR:-~/.claude}/session-relay/) the reading goes to
# <config dir>/session-relay/statusline/<session_id>.json instead, so a project is never
# touched, and thresholds use relay.py's layering: the plugin's /config options, then
# <config dir>/session-relay/config.json, then <project>/.claude/session-relay/config.json,
# then $CLAUDE_RELAY_CONFIG.
#
# Budget: < 50 ms. jq is used for every JSON step; python is never started here.
# This script must never fail loudly: every error path prints something and exits 0.

set -u

mode="project"
[ "${1:-}" = "--plugin" ] && mode="plugin"

input=$(cat 2>/dev/null) || input=""
if [ -z "$input" ]; then
  printf '[relay] no status data\n'
  exit 0
fi
if ! command -v jq >/dev/null 2>&1; then
  printf '[relay] jq not found\n'
  exit 0
fi

# One jq pass extracts everything. Fields are joined with the ASCII unit separator
# (0x1f): unlike tabs it is not whitespace, so `read` keeps empty fields in place.
US=$'\x1f'
extracted=$(printf '%s' "$input" | jq -r '
  [ (.session_id // ""),
    (.model.id // ""),
    (.model.display_name // .model.id // "?"),
    (.cwd // ""),
    (.workspace.project_dir // ""),
    (.context_window.used_percentage // "null"),
    (.context_window.context_window_size // "null"),
    (.context_window.total_input_tokens // "null")
  ] | map(tostring) | join("\u001f")' 2>/dev/null) || extracted=""
if [ -z "$extracted" ]; then
  printf '[relay] unreadable status data\n'
  exit 0
fi
IFS="$US" read -r sid model_id model_name cwd project_dir used_pct window total_in <<<"$extracted"

# Project root: hooks use CLAUDE_PROJECT_DIR, so prefer it for consistency.
root="${CLAUDE_PROJECT_DIR:-${project_dir:-$cwd}}"
[ -z "$root" ] && root="$PWD"
if [ "$mode" = "plugin" ]; then
  config_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  user_dir="$config_dir/session-relay"
  state_dir="$user_dir/statusline"
  relay_state_dir="$root/.claude/session-relay/state"
else
  state_dir="$root/.claude/state"
  relay_state_dir="$state_dir"
  relay_dir="$root/.claude/relay"
fi

# Percentage: prefer the pre-computed used_percentage; otherwise derive it.
# A zero total means "no API response yet" (or just after /compact), so it stays unknown.
pct="$used_pct"
if [ "$pct" = "null" ] && [ "$total_in" != "null" ] && [ "$total_in" != "0" ] \
   && [ "$window" != "null" ] && [ "$window" != "0" ]; then
  pct=$(awk -v t="$total_in" -v w="$window" 'BEGIN { printf "%.1f", (t * 100) / w }')
fi

# Write the state file early and atomically: Claude Code may cancel this script
# when a newer update arrives, and readers must never see a partial file.
if [ -n "$sid" ]; then
  mkdir -p "$state_dir" 2>/dev/null || true
  tmp=$(mktemp "$state_dir/.tmp-XXXXXX" 2>/dev/null) || tmp=""
  if [ -n "$tmp" ]; then
    if jq -n -c \
        --arg sid "$sid" \
        --arg model "$model_id" \
        --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --argjson pct "$( [ "$pct" = "null" ] && echo null || echo "$pct" )" \
        --argjson window "$( [ "$window" = "null" ] && echo null || echo "$window" )" \
        --argjson total "$( [ "$total_in" = "null" ] && echo null || echo "$total_in" )" \
        '{session_id:$sid, used_pct:$pct, window_size:$window, input_total:$total,
          model:$model, ts:$ts, approx:false, source:"statusline"}' \
        >"$tmp" 2>/dev/null; then
      mv -f "$tmp" "$state_dir/$sid.json" 2>/dev/null || rm -f "$tmp"
    else
      rm -f "$tmp"
    fi
  fi
fi

# Generation + thresholds for the display (both optional, both one jq call).
gen=0
if [ -n "$sid" ] && [ -f "$relay_state_dir/$sid.relay.json" ]; then
  gen=$(jq -r '.generation // 0' "$relay_state_dir/$sid.relay.json" 2>/dev/null) || gen=0
fi
soft=40; hard=50
if [ "$mode" = "plugin" ]; then
  # Same precedence as relay.py: defaults < /config < user config < project config
  # < CLAUDE_RELAY_CONFIG. The /config values sit under pluginConfigs["session-relay@<marketplace>"],
  # whatever the marketplace is called.
  settings="$config_dir/settings.json"; ucfg="$user_dir/config.json"
  pcfg="$root/.claude/session-relay/config.json"; ecfg="${CLAUDE_RELAY_CONFIG:-}"
  [ -f "$settings" ] || settings=/dev/null
  [ -f "$ucfg" ] || ucfg=/dev/null
  [ -f "$pcfg" ] || pcfg=/dev/null
  { [ -n "$ecfg" ] && [ -f "$ecfg" ]; } || ecfg=/dev/null
  IFS="$US" read -r soft hard <<<"$(jq -n -r --slurpfile s "$settings" --slurpfile u "$ucfg" \
      --slurpfile p "$pcfg" --slurpfile e "$ecfg" '
    ([($s[0].pluginConfigs // {}) | to_entries[]
      | select(.key | startswith("session-relay@")) | .value.options // {}] | first // {}) as $o
    | [ ($e[0].soft // $p[0].soft // $u[0].soft // $o.wrap_up_threshold // 40),
        ($e[0].hard // $p[0].hard // $u[0].hard // $o.handoff_threshold // 50) ]
    | map(tostring) | join("\u001f")' 2>/dev/null)" || { soft=40; hard=50; }
  [ -z "$soft" ] && soft=40
  [ -z "$hard" ] && hard=50
  cfg=""
else
  cfg="${CLAUDE_RELAY_CONFIG:-$relay_dir/config.json}"
fi
if [ -n "$cfg" ] && [ -f "$cfg" ]; then
  IFS="$US" read -r soft hard <<<"$(jq -r '[(.soft // 40), (.hard // 50)] | map(tostring) | join("\u001f")' "$cfg" 2>/dev/null)" || { soft=40; hard=50; }
  [ -z "$soft" ] && soft=40
  [ -z "$hard" ] && hard=50
fi

marker=""
shown="?"
if [ "$pct" != "null" ] && [ -n "$pct" ]; then
  shown=$(awk -v p="$pct" 'BEGIN { printf "%d", p }')
  marker=$(awk -v p="$pct" -v s="$soft" -v h="$hard" 'BEGIN { if (p >= h) print "!hard"; else if (p >= s) print "~soft"; }')
fi
if [ "${CLAUDE_RELAY_DISABLE:-0}" = "1" ]; then
  marker="relay off"
fi

if [ -n "$marker" ]; then
  printf '[%s] ctx %s%% g%s %s\n' "$model_name" "$shown" "$gen" "$marker"
else
  printf '[%s] ctx %s%% g%s\n' "$model_name" "$shown" "$gen"
fi
exit 0
