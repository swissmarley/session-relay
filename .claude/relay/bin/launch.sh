#!/bin/sh
# Session Relay launcher (POSIX sh).
#
# Starts a fresh interactive Claude Code session in the same project that reads the
# handoff and continues the work. Called by relay.py's Stop hook; safe to run by hand.
#
# Exit codes (the Stop hook maps them to ledger events):
#   0  launched (stdout names the method: tmux | terminal | already-launched)
#   1  usage or internal error
#   2  generation limit reached (chain stopped, user notified)
#   3  cooldown active (the hook retries at the next stop)
#   4  deferred: no terminal available, command written to .claude/relay/NEXT_COMMAND.txt
#   5  lock busy
#
# Detection order in --mode auto: tmux (inside tmux or a server is running) ->
# Terminal.app / iTerm2 via osascript on macOS -> NEXT_COMMAND.txt + desktop notification.
#
# Permission handling: the child starts with --permission-mode <value passed by the hook>,
# which is the parent's mode (bypassPermissions only when explicitly allowed in config).
# --dangerously-skip-permissions is never added here.

set -u

usage() {
  cat <<'EOF'
usage: launch.sh --cwd DIR --handoff FILE --parent ID [--child-id UUID] [--generation N]
                 [--permission-mode MODE] [--max-generations N] [--cooldown SECONDS]
                 [--mode auto|tmux|terminal|file] [--terminal-app Terminal|iTerm]
                 [--claude-bin PATH] [--lock-stale SECONDS] [--lock-wait SECONDS]
                 [--close-old] [--dry-run]
EOF
}

cwd=""; handoff=""; parent=""; child=""; generation=1; perm="default"
max_gen=8; cooldown=60; lmode="auto"; term_app="Terminal"; claude_bin="claude"
lock_stale=300; lock_wait=5; close_old=0; dry_run=0

while [ $# -gt 0 ]; do
  case "$1" in
    --cwd) cwd=$2; shift 2 ;;
    --handoff) handoff=$2; shift 2 ;;
    --parent) parent=$2; shift 2 ;;
    --child-id) child=$2; shift 2 ;;
    --generation) generation=$2; shift 2 ;;
    --permission-mode) perm=$2; shift 2 ;;
    --max-generations) max_gen=$2; shift 2 ;;
    --cooldown) cooldown=$2; shift 2 ;;
    --mode) lmode=$2; shift 2 ;;
    --terminal-app) term_app=$2; shift 2 ;;
    --claude-bin) claude_bin=$2; shift 2 ;;
    --lock-stale) lock_stale=$2; shift 2 ;;
    --lock-wait) lock_wait=$2; shift 2 ;;
    --close-old) close_old=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'launch.sh: unknown argument %s\n' "$1" >&2; usage >&2; exit 1 ;;
  esac
done

if [ -z "$cwd" ] || [ ! -d "$cwd" ]; then printf 'launch.sh: --cwd must be an existing directory\n' >&2; exit 1; fi
if [ -z "$handoff" ] || [ ! -f "$handoff" ]; then printf 'launch.sh: --handoff must be an existing file\n' >&2; exit 1; fi
if [ -z "$parent" ]; then printf 'launch.sh: --parent is required\n' >&2; exit 1; fi
case "$generation" in ''|*[!0-9]*) printf 'launch.sh: --generation must be an integer\n' >&2; exit 1 ;; esac
case "$perm" in
  default|manual|acceptEdits|plan|auto|dontAsk|bypassPermissions) ;;
  *) printf 'launch.sh: unknown permission mode %s, using default\n' "$perm" >&2; perm="default" ;;
esac
if [ -z "$child" ]; then
  child=$(uuidgen 2>/dev/null | tr '[:upper:]' '[:lower:]') || child=""
  [ -z "$child" ] && child="relay-$(date +%s)-$$"
fi

relay_dir="$cwd/.claude/relay"
launched_dir="$relay_dir/launched"
run_dir="$relay_dir/run"
lock_dir="$relay_dir/lock"
last_file="$launched_dir/.last_launch"
next_cmd="$relay_dir/NEXT_COMMAND.txt"
log_file="${CLAUDE_RELAY_LOG:-$relay_dir/relay.log}"
mkdir -p "$launched_dir" "$run_dir" 2>/dev/null || true

log() {
  printf '%s INFO  pid=%s launch: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" "$*" >>"$log_file" 2>/dev/null || true
}

notify() {
  # Desktop notification, best effort. Message is sanitised for AppleScript.
  msg=$(printf '%s' "$1" | sed 's/["\\]//g')
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"$msg\" with title \"Session Relay\"" >/dev/null 2>&1 || true
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send "Session Relay" "$msg" >/dev/null 2>&1 || true
  fi
}

# Single-quote a value for inclusion in a generated shell script.
shq() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

mtime_of() {
  stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0
}

# ---- lock (mkdir is atomic; stale locks are reclaimed) -----------------------
tries=0
max_tries=$((lock_wait * 5))
while ! mkdir "$lock_dir" 2>/dev/null; do
  if [ -d "$lock_dir" ]; then
    age=$(( $(date +%s) - $(mtime_of "$lock_dir") ))
    if [ "$age" -gt "$lock_stale" ]; then
      log "reclaiming stale lock (age ${age}s)"
      rm -rf "$lock_dir"
      continue
    fi
  fi
  tries=$((tries + 1))
  if [ "$tries" -ge "$max_tries" ]; then
    printf 'lock busy: %s\n' "$lock_dir"
    log "lock busy, giving up"
    exit 5
  fi
  sleep 0.2
done
printf '%s\n' "$$" >"$lock_dir/owner" 2>/dev/null || true
trap 'rm -rf "$lock_dir"' EXIT INT TERM HUP

# ---- idempotency: never launch twice for the same handoff ---------------------
marker="$launched_dir/$(basename "$handoff").done"
if [ -f "$marker" ]; then
  printf 'already-launched %s\n' "$(head -n 1 "$marker" 2>/dev/null)"
  log "already launched for $(basename "$handoff"); nothing to do"
  exit 0
fi

# ---- limits -------------------------------------------------------------------
if [ "$generation" -gt "$max_gen" ]; then
  printf 'generation %s exceeds max_generations %s\n' "$generation" "$max_gen"
  log "generation limit reached ($generation > $max_gen)"
  notify "Generation limit ($max_gen) reached; relay chain stopped. Handoff: $(basename "$handoff")"
  exit 2
fi
if [ -f "$last_file" ]; then
  last=$(head -n 1 "$last_file" 2>/dev/null)
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  elapsed=$(( $(date +%s) - last ))
  if [ "$elapsed" -lt "$cooldown" ]; then
    printf 'cooldown: %ss remaining\n' "$((cooldown - elapsed))"
    log "cooldown active (${elapsed}s < ${cooldown}s)"
    exit 3
  fi
fi

# ---- runner script -----------------------------------------------------------
bootstrap="Session Relay handoff: you are generation $generation, continuing a previous session's work in this project. Read the handoff at $handoff (the SessionStart hook also injected it). Confirm your understanding in 3 lines, promote its 'Memory updates' items into .claude/memory/ (deduplicated), then continue with the very next concrete action from section 3."
runner="$run_dir/$child.sh"
{
  printf '#!/bin/sh\n'
  printf '# Generated by Session Relay: starts generation %s for %s\n' "$generation" "$cwd"
  printf '# Parent session: %s   Handoff: %s\n' "$parent" "$handoff"
  printf '# Nested-session markers are removed so the child is a first-class session\n'
  printf '# (auto memory stays on). Variable names are [A-Za-z0-9_] only, so word splitting is safe.\n'
  printf '# shellcheck disable=SC2046\n'
  # shellcheck disable=SC2016  # the $(...) is meant for the generated script, not this one
  printf 'for v in $(env | sed -n '"'"'s/^\\(CLAUDE_CODE_[A-Za-z0-9_]*\\)=.*/\\1/p'"'"'); do unset "$v"; done\n'
  printf 'unset CLAUDECODE CLAUDE_PROJECT_DIR CLAUDE_RELAY_LAUNCHER CLAUDE_RELAY_LOG\n'
  printf 'cd %s || exit 1\n' "$(shq "$cwd")"
  printf 'printf %s\n' "$(shq "Session Relay: generation $generation (parent ${parent}). Handoff: $handoff\n")"
  printf 'exec %s --session-id %s --permission-mode %s --name %s %s\n' \
    "$(shq "$claude_bin")" "$(shq "$child")" "$(shq "$perm")" "$(shq "relay-g$generation")" "$(shq "$bootstrap")"
} >"$runner"
chmod 755 "$runner" 2>/dev/null || true

# ---- method detection ---------------------------------------------------------
have_tmux_server() {
  command -v tmux >/dev/null 2>&1 || return 1
  if [ -n "${TMUX:-}" ]; then return 0; fi
  tmux ls >/dev/null 2>&1
}
is_macos() { [ "$(uname -s 2>/dev/null)" = "Darwin" ]; }

method=""
case "$lmode" in
  tmux|terminal|file) method="$lmode" ;;
  auto|*)
    if have_tmux_server; then method="tmux"
    elif is_macos && command -v osascript >/dev/null 2>&1; then method="terminal"
    else method="file"
    fi ;;
esac

if [ "$dry_run" -eq 1 ]; then
  printf 'dry-run: would launch via %s: sh %s\n' "$method" "$runner"
  log "dry-run via $method, runner $runner"
  exit 0
fi

record_launch() {
  printf '%s\n%s\n%s\n' "$child" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >"$marker"
  date +%s >"$last_file"
  log "launched generation $generation child $child via $1 (handoff $(basename "$handoff"))"
}

launch_tmux() {
  command -v tmux >/dev/null 2>&1 || return 1
  tmux new-window -n "relay-g$generation" -c "$cwd" "sh $(shq "$runner")" >/dev/null 2>&1 || return 1
  if [ "$close_old" -eq 1 ] && [ -n "${TMUX_PANE:-}" ]; then
    # Give the hook time to return its result before the parent pane disappears.
    ( sleep "${RELAY_CLOSE_DELAY:-2}"; tmux kill-pane -t "$TMUX_PANE" >/dev/null 2>&1 ) &
  fi
  return 0
}

launch_terminal() {
  command -v osascript >/dev/null 2>&1 || return 1
  # Escape for an AppleScript string literal; the runner path is single-quoted for the shell.
  esc=$(printf 'sh %s' "$(shq "$runner")" | sed 's/\\/\\\\/g; s/"/\\"/g')
  case "$term_app" in
    iTerm|iTerm2)
      osascript \
        -e 'tell application "iTerm"' \
        -e 'activate' \
        -e 'set w to (create window with default profile)' \
        -e "tell current session of w to write text \"$esc\"" \
        -e 'end tell' >/dev/null 2>&1 || return 1 ;;
    *)
      osascript \
        -e 'tell application "Terminal"' \
        -e "do script \"$esc\"" \
        -e 'activate' \
        -e 'end tell' >/dev/null 2>&1 || return 1 ;;
  esac
  return 0
}

launch_file() {
  {
    printf '# Session Relay: the next session could not be opened automatically.\n'
    printf '# Run this in a terminal to continue generation %s:\n' "$generation"
    printf 'sh %s\n' "$(shq "$runner")"
  } >"$next_cmd"
  notify "Could not open a terminal. Run the command in .claude/relay/NEXT_COMMAND.txt to continue (generation $generation)."
}

case "$method" in
  tmux)
    if launch_tmux; then record_launch tmux; printf 'tmux\n'; exit 0; fi
    log "tmux launch failed, falling back"
    if is_macos && launch_terminal; then record_launch terminal; printf 'terminal\n'; exit 0; fi
    launch_file; record_launch file; printf 'deferred %s\n' "$next_cmd"; exit 4 ;;
  terminal)
    if launch_terminal; then record_launch terminal; printf 'terminal\n'; exit 0; fi
    log "terminal launch failed, falling back to file"
    launch_file; record_launch file; printf 'deferred %s\n' "$next_cmd"; exit 4 ;;
  file|*)
    launch_file; record_launch file; printf 'deferred %s\n' "$next_cmd"; exit 4 ;;
esac
