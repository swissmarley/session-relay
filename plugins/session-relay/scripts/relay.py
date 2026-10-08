#!/usr/bin/env python3
"""Session Relay for Claude Code.

One stdlib-only script that implements every hook of the relay plus the helpers
the hooks share (meter fallback, handoff validator, ledger, locks).

Hook subcommands (read the hook JSON payload on stdin):
    prompt          UserPromptSubmit  -> soft trigger (additionalContext once per session)
    stop            Stop              -> hard trigger (block for a handoff, then launch)
    session-start   SessionStart      -> inject + consume the pending handoff
    pre-compact     PreCompact        -> transcript backup + snapshot
    session-end     SessionEnd        -> finalize the ledger entry

Utility subcommands:
    meter --session-id S --transcript T [--cwd D]   print the meter reading as JSON
    validate HANDOFF.md                             exit 0 if valid, 1 if not; prints a report
    reset --session-id S [--cwd D]                  re-arm a session after a launch
    status [--cwd D] [--session-id S]               print relay config/state summary
    enable | disable [--cwd D]                      plugin: switch the relay on/off for a project
    install-statusline [--force | --remove]         plugin: set up the optional status line
Every utility accepts --plugin-root DIR, which has the same effect as CLAUDE_PLUGIN_ROOT.

Two layouts, one script:
  * Project-copy install (install.sh): code in .claude/relay/bin, data in .claude/state,
    .claude/handoffs, .claude/backups and .claude/relay. This is the default.
  * Plugin (only when CLAUDE_PLUGIN_ROOT is set): code in the plugin's scripts/ folder
    (read-only), every per-project file in .claude/session-relay/, per-user files
    (config, meter readings, status line copy) in ~/.claude/session-relay/.

Design rules:
  * Never block the user because of a relay bug: every hook wraps its body and
    exits 0 on internal errors. Only `stop` emits an intentional block decision.
  * Only JSON control output goes to stdout. Diagnostics go to relay.log.
  * All writes are atomic (temp file + os.replace). Shared steps take a mkdir lock.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from typing import Any, Dict, Optional

RELAY_VERSION = "0.2.1"

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "soft": 40,
    "hard": 50,
    "max_generations": 8,
    "cooldown_seconds": 60,
    "stale_seconds": 120,
    "dry_run": False,
    "close_old_session": False,
    "max_handoff_words": 2500,
    "max_block_attempts": 2,
    "inject_budget_chars": 16000,
    "backups_keep": 10,
    "launcher": {
        "mode": "auto",
        "terminal_app": "Terminal",
        "claude_bin": "claude",
        "allow_bypass_inherit": False,
        "lock_stale_seconds": 300,
    },
}

PLUGIN_DIR_NAME = "session-relay"       # <project>/.claude/<this> and ~/.claude/<this>
PLUGIN_ID = "session-relay@session-relay"
PROJECT_GITIGNORE = "*\n!config.json\n"
ACTIVATION_MODES = ("always", "opt-in")
# /config options declared in plugin.json (userConfig) -> config keys. Claude Code
# exports each one to hook processes as CLAUDE_PLUGIN_OPTION_<KEY>.
PLUGIN_OPTIONS = {
    "WRAP_UP_THRESHOLD": "soft",
    "HANDOFF_THRESHOLD": "hard",
    "ACTIVATION": "activation",
}
LEGACY_HOOK_MARK = "/relay/bin/relay.py"
METER_KEEP_SECONDS = 7 * 24 * 3600
METER_PRUNE_EVERY = 3600          # seconds between two sweeps of old meter readings
METER_KEEP_FILES = 3              # newest readings kept per session
METER_READ_RETRIES = 3
METER_RETRY_SLEEP = 0.025

LOG_MAX_BYTES = 1_000_000
LOG_KEEP = 3
LOCK_WAIT_SECONDS = 5.0
GIT_TIMEOUT = 3


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #

def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def iso_now() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def compact_ts(now: Optional[_dt.datetime] = None) -> str:
    """UTC timestamp safe for file names, e.g. 20261006T124201Z."""
    return (now or utc_now()).strftime("%Y%m%dT%H%M%SZ")


def parse_iso(ts: Any) -> Optional[float]:
    """Parse an ISO-8601 UTC timestamp (or epoch number) to epoch seconds."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.timestamp()
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def env_plugin_root() -> Optional[str]:
    """The plugin root when relay.py runs from the plugin, else None."""
    v = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    return os.path.abspath(v) if v else None


def home_dir() -> str:
    """The home folder as Claude Code's own process sees it (the mod uses the same rule)."""
    return os.environ.get("HOME") or os.environ.get("USERPROFILE") or os.path.expanduser("~")


def config_dir() -> str:
    """Claude Code's config folder: CLAUDE_CONFIG_DIR, else ~/.claude (the mod uses the same rule)."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home_dir(), ".claude")


class Paths:
    """Where code lives (relay_home) and where this project's data lives (project).

    Plugin mode (CLAUDE_PLUGIN_ROOT set) keeps every per-project file in
    .claude/session-relay/ and per-user files in ~/.claude/session-relay/.
    Otherwise the project-copy layout is used, exactly as before.
    """

    def __init__(self, project_dir: str, relay_home: Optional[str] = None,
                 plugin_root: Optional[str] = None):
        self.project = os.path.abspath(project_dir)
        self.plugin_root = os.path.abspath(plugin_root) if plugin_root else env_plugin_root()
        self.plugin = self.plugin_root is not None
        here = os.path.dirname(os.path.abspath(__file__))
        self.claude = os.path.join(self.project, ".claude")
        self.memory = os.path.join(self.claude, "memory")
        if self.plugin:
            self.relay_home = os.path.abspath(relay_home or here)      # the plugin's scripts/
            self.relay = os.path.join(self.claude, PLUGIN_DIR_NAME)
            self.state = os.path.join(self.relay, "state")
            self.handoffs = os.path.join(self.relay, "handoffs")
            self.backups = os.path.join(self.relay, "backups")
            self.user_dir = os.path.join(config_dir(), PLUGIN_DIR_NAME)
            self.launch_sh = os.path.join(self.relay_home, "launch.sh")
        else:
            self.relay_home = os.path.abspath(relay_home or os.path.dirname(here))
            self.relay = os.path.join(self.claude, "relay")
            self.state = os.path.join(self.claude, "state")
            self.handoffs = os.path.join(self.claude, "handoffs")
            self.backups = os.path.join(self.claude, "backups")
            self.user_dir = None
            self.launch_sh = os.path.join(self.relay_home, "bin", "launch.sh")
        self.ledger = os.path.join(self.relay, "ledger.jsonl")
        self.log = os.path.join(self.relay, "relay.log")
        self.lock = os.path.join(self.relay, "lock")
        self.launched = os.path.join(self.relay, "launched")
        self.run = os.path.join(self.relay, "run")
        self.next_command = os.path.join(self.relay, "NEXT_COMMAND.txt")

    @property
    def template(self) -> str:
        if self.plugin:
            cands = (os.path.join(self.user_dir, "handoff-template.md"),
                     os.path.join(self.relay_home, "handoff-template.md"))
        else:
            cands = (os.path.join(self.relay, "handoff-template.md"),
                     os.path.join(self.relay_home, "handoff-template.md"))
        for cand in cands:
            if os.path.isfile(cand):
                return cand
        return os.path.join(self.relay_home, "handoff-template.md")

    @property
    def config_file(self) -> str:
        """Legacy: the one config file. Plugin: the project layer (see config_layers)."""
        env = os.environ.get("CLAUDE_RELAY_CONFIG")
        if self.plugin:
            return env or self.project_config
        if env:
            return env
        for cand in (
            os.path.join(self.relay, "config.json"),
            os.path.join(self.relay_home, "config.json"),
        ):
            if os.path.isfile(cand):
                return cand
        return os.path.join(self.relay, "config.json")

    @property
    def project_config(self) -> str:
        return os.path.join(self.relay, "config.json")

    @property
    def user_config(self) -> Optional[str]:
        return os.path.join(self.user_dir, "config.json") if self.user_dir else None

    @property
    def default_config(self) -> str:
        return os.path.join(self.relay_home, "config.json")

    def meter_state(self, session_id: str) -> str:
        """Status line reading. Plugin: per user, so it never touches a project."""
        if self.plugin:
            return os.path.join(self.user_dir, "statusline", f"{safe_id(session_id)}.json")
        return os.path.join(self.state, f"{safe_id(session_id)}.json")

    def mod_meter(self, session_id: str) -> Optional[str]:
        """Single-file reading (the 0.2.0 mod's format; still read, never written)."""
        if not self.plugin:
            return None
        return os.path.join(self.user_dir, "meter", f"{safe_id(session_id)}.json")

    def mod_meter_dir(self, session_id: str) -> Optional[str]:
        """The mod's readings for one session: one new file per write, never rewritten."""
        if not self.plugin:
            return None
        return os.path.join(self.user_dir, "meter", safe_id(session_id))

    def relay_state(self, session_id: str) -> str:
        return os.path.join(self.state, f"{safe_id(session_id)}.relay.json")

    def ensure_dirs(self) -> None:
        if self.plugin:
            for d in (self.relay, self.state, self.handoffs, self.backups, self.launched, self.run):
                os.makedirs(d, exist_ok=True)
            gi = os.path.join(self.relay, ".gitignore")
            if not os.path.exists(gi):
                atomic_write(gi, PROJECT_GITIGNORE)   # git status stays clean; config.json is shareable
            return
        for d in (self.relay, self.state, self.handoffs, self.backups, self.memory,
                  self.launched, self.run):
            os.makedirs(d, exist_ok=True)


def safe_id(value: Any) -> str:
    """Restrict an identifier to characters that are safe in a file name."""
    s = str(value or "unknown")
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)[:128]


def resolve_project_dir(payload: Optional[Dict[str, Any]] = None,
                        explicit: Optional[str] = None) -> str:
    """CLAUDE_PROJECT_DIR wins, then the payload cwd, then the process cwd."""
    if explicit:
        return explicit
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return env
    if payload:
        for key in ("cwd",):
            v = payload.get(key)
            if isinstance(v, str) and v:
                return v
        ws = payload.get("workspace")
        if isinstance(ws, dict) and isinstance(ws.get("project_dir"), str):
            return ws["project_dir"]
    return os.getcwd()


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_config_file(paths: Paths, path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:  # bad JSON must not break the session
        log(paths, "WARN", f"config unreadable, using defaults: {exc}", path=path)
        return {}
    if not isinstance(data, dict):
        log(paths, "WARN", "config is not a JSON object, ignored", path=path)
        return {}
    return data


def plugin_option_values() -> Dict[str, Any]:
    """The /config values (plugin userConfig) Claude Code exported to this hook."""
    out: Dict[str, Any] = {}
    for key, cfg_key in PLUGIN_OPTIONS.items():
        raw = os.environ.get("CLAUDE_PLUGIN_OPTION_" + key)
        if raw is not None and str(raw).strip() != "":
            out[cfg_key] = str(raw).strip()
    return out


def config_layers(paths: Paths) -> list:
    """[(label, path or None, values)] from lowest to highest precedence."""
    if not paths.plugin:
        return [("config", paths.config_file, _read_config_file(paths, paths.config_file))]
    layers = [
        ("plugin defaults", paths.default_config, _read_config_file(paths, paths.default_config)),
        ("/config", None, plugin_option_values()),
        ("user", paths.user_config, _read_config_file(paths, paths.user_config)),
        ("project", paths.project_config, _read_config_file(paths, paths.project_config)),
    ]
    env = os.environ.get("CLAUDE_RELAY_CONFIG")
    if env:
        layers.append(("CLAUDE_RELAY_CONFIG", env, _read_config_file(paths, env)))
    return layers


def load_config(paths: Paths) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if paths.plugin:
        cfg["activation"] = "always"
    for _, _, data in config_layers(paths):
        cfg = _deep_merge(cfg, data)
    if os.environ.get("CLAUDE_RELAY_DISABLE") == "1":
        cfg["enabled"] = False
    # sanity clamps
    try:
        cfg["soft"] = float(cfg["soft"])
        cfg["hard"] = float(cfg["hard"])
    except (TypeError, ValueError):
        cfg["soft"], cfg["hard"] = 40.0, 50.0
    if cfg["hard"] < cfg["soft"]:
        cfg["hard"] = cfg["soft"]
    if paths.plugin:
        mode = str(cfg.get("activation") or "always").strip().lower()
        if mode not in ACTIVATION_MODES:
            log(paths, "WARN", f"unknown activation {mode!r}, using 'always'")
            mode = "always"
        cfg["activation"] = mode
    return cfg


def activation_state(paths: Paths, cfg: Dict[str, Any]) -> tuple:
    """(active, reason). In opt-in mode a project must enable itself explicitly."""
    if not paths.plugin:
        return True, "project-copy install"
    if cfg.get("activation") != "opt-in":
        return True, "activation: always"
    proj = read_json(paths.project_config, default=None)
    if isinstance(proj, dict) and proj.get("enabled") is True:
        return True, "activation: opt-in, enabled for this project"
    return False, "activation: opt-in, not enabled for this project (run /session-relay:enable)"


def legacy_install(paths: Paths) -> Optional[str]:
    """Settings file that still wires the project-copy relay, if any (plugin mode only).

    The plugin stands down there, so the two never handle the same session twice.
    """
    if not paths.plugin:
        return None
    for path in (os.path.join(paths.claude, "settings.json"),
                 os.path.join(paths.claude, "settings.local.json"),
                 os.path.join(config_dir(), "settings.json")):
        if LEGACY_HOOK_MARK in read_text(path):
            return path
    return None


# --------------------------------------------------------------------------- #
# Logging (file only; stdout is reserved for hook JSON)
# --------------------------------------------------------------------------- #

def _rotate(path: str) -> None:
    try:
        if os.path.getsize(path) < LOG_MAX_BYTES:
            return
    except OSError:
        return
    for i in range(LOG_KEEP - 1, 0, -1):
        src = f"{path}.{i}"
        dst = f"{path}.{i + 1}"
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    try:
        os.replace(path, f"{path}.1")
    except OSError:
        pass


def log(paths: Optional[Paths], level: str, msg: str, **kv: Any) -> None:
    line = f"{iso_now()} {level:5s} pid={os.getpid()} {msg}"
    if kv:
        extras = " ".join(f"{k}={json.dumps(v, default=str)}" for k, v in kv.items())
        line = f"{line} {extras}"
    if os.environ.get("CLAUDE_RELAY_DEBUG") == "1":
        sys.stderr.write(line + "\n")
    if paths is None:
        return
    if paths.plugin and not os.path.isdir(paths.relay):
        return  # plugin mode: never create the project folder just to log
    try:
        os.makedirs(os.path.dirname(paths.log), exist_ok=True)
        _rotate(paths.log)
        with open(paths.log, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Atomic IO, JSON helpers
# --------------------------------------------------------------------------- #

def atomic_write(path: str, text: str, mode: int = 0o644) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: str, data: Any) -> None:
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def read_text(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return default


def append_line(path: str, line: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# --------------------------------------------------------------------------- #
# mkdir lock with stale timeout
# --------------------------------------------------------------------------- #

class LockTimeout(RuntimeError):
    pass


class Lock:
    """Atomic directory lock. Stale locks (older than stale_seconds) are reclaimed."""

    def __init__(self, path: str, stale_seconds: float = 300.0,
                 wait_seconds: float = LOCK_WAIT_SECONDS, paths: Optional[Paths] = None):
        self.path = path
        self.stale = stale_seconds
        self.wait = wait_seconds
        self.paths = paths
        self.held = False

    def _is_stale(self) -> bool:
        try:
            age = time.time() - os.stat(self.path).st_mtime
        except OSError:
            return False
        return age > self.stale

    def acquire(self) -> "Lock":
        deadline = time.time() + self.wait
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        while True:
            try:
                os.mkdir(self.path)
                self.held = True
                try:
                    with open(os.path.join(self.path, "owner"), "w", encoding="utf-8") as fh:
                        fh.write(f"{os.getpid()} {iso_now()}\n")
                except OSError:
                    pass
                return self
            except FileExistsError:
                if self._is_stale():
                    log(self.paths, "WARN", "reclaiming stale lock", lock=self.path)
                    _rm_lock_dir(self.path)
                    continue
                if time.time() >= deadline:
                    raise LockTimeout(f"lock busy: {self.path}")
                time.sleep(0.05)

    def release(self) -> None:
        if self.held:
            _rm_lock_dir(self.path)
            self.held = False

    def __enter__(self) -> "Lock":
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        self.release()


def _rm_lock_dir(path: str) -> None:
    try:
        for name in os.listdir(path):
            try:
                os.unlink(os.path.join(path, name))
            except OSError:
                pass
        os.rmdir(path)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Per-session relay state
# --------------------------------------------------------------------------- #

def load_relay_state(paths: Paths, session_id: str) -> Dict[str, Any]:
    data = read_json(paths.relay_state(session_id), default=None)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("session_id", session_id)
    data.setdefault("generation", 0)
    data.setdefault("parent", None)
    data.setdefault("block_attempts", 0)
    data.setdefault("launched", False)
    return data


def save_relay_state(paths: Paths, state: Dict[str, Any]) -> None:
    state["updated_at"] = iso_now()
    write_json(paths.relay_state(state["session_id"]), state)


# --------------------------------------------------------------------------- #
# Git helpers (best effort, never raise)
# --------------------------------------------------------------------------- #

def _git(cwd: str, *args: str, timeout: int = GIT_TIMEOUT, strip: bool = True) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
            check=False,
        )
        if out.returncode != 0:
            return ""
        return out.stdout.strip() if strip else out.stdout.rstrip("\n")
    except (OSError, subprocess.SubprocessError):
        return ""


def git_head(cwd: str) -> str:
    return _git(cwd, "rev-parse", "--short", "HEAD")


def git_branch(cwd: str) -> str:
    return _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #

def ledger_append(paths: Paths, event: str, session_id: str, *, parent: Any = None,
                  generation: Any = 0, used_pct: Any = None, handoff_path: Any = None,
                  **extra: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "ts": iso_now(),
        "session_id": session_id,
        "parent": parent,
        "generation": generation,
        "event": event,
        "used_pct": used_pct,
        "handoff_path": handoff_path,
        "git_head": git_head(paths.project) or None,
    }
    entry.update({k: v for k, v in extra.items() if v is not None})
    try:
        append_line(paths.ledger, json.dumps(entry, sort_keys=True))
    except OSError as exc:
        log(paths, "ERROR", f"ledger append failed: {exc}")
    return entry


def ledger_tail(paths: Paths, n: int = 3) -> list:
    text = read_text(paths.ledger)
    out = []
    for line in text.splitlines()[-max(n, 0) * 4:]:  # cheap over-read, then filter
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out[-n:] if n > 0 else []


# --------------------------------------------------------------------------- #
# Hook payload IO
# --------------------------------------------------------------------------- #

def read_payload() -> Dict[str, Any]:
    """Read the hook JSON from stdin. Tolerates empty or invalid input."""
    try:
        if sys.stdin is None or sys.stdin.closed:
            return {}
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def emit(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
    sys.stdout.flush()


def new_uuid() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- #
# Meter: mod reading (plugin) first, statusLine state second, transcript fallback last
# --------------------------------------------------------------------------- #

TRANSCRIPT_TAIL_BYTES = 256 * 1024
DEFAULT_WINDOW = 200_000
LARGE_WINDOW = 1_000_000


def infer_window(model: Optional[str], input_total: Optional[int] = None) -> int:
    """Best-effort window size when no statusLine state recorded it.

    The transcript's `message.model` is the API id (e.g. claude-opus-5-5) and carries
    no `[1m]` marker even when the 1M variant is selected, so: trust an explicit
    marker, the Sonnet 5 family (always 1M), or a token count that only fits in 1M.
    Everything else defaults to 200k. Callers mark the result `approx`.
    """
    m = (model or "").lower()
    if "[1m]" in m or m.endswith("-1m") or "-1m-" in m:
        return LARGE_WINDOW
    if re.search(r"sonnet-5(?:[-.]|$)", m):
        return LARGE_WINDOW
    if input_total is not None and input_total > DEFAULT_WINDOW * 0.95:
        return LARGE_WINDOW
    return DEFAULT_WINDOW


def _usage_from_line(line: bytes) -> Optional[Dict[str, Any]]:
    if b'"assistant"' not in line or b'"usage"' not in line:
        return None
    try:
        obj = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None
    if obj.get("isSidechain") is True:
        return None
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict) or usage.get("input_tokens") is None:
        return None

    def _i(k: str) -> int:
        v = usage.get(k)
        return int(v) if isinstance(v, (int, float)) else 0

    total = _i("input_tokens") + _i("cache_creation_input_tokens") + _i("cache_read_input_tokens")
    return {
        "input_total": total,
        "input_tokens": _i("input_tokens"),
        "cache_creation_input_tokens": _i("cache_creation_input_tokens"),
        "cache_read_input_tokens": _i("cache_read_input_tokens"),
        "output_tokens": _i("output_tokens"),
        "model": msg.get("model"),
        "timestamp": obj.get("timestamp"),
        "request_id": obj.get("requestId"),
    }


def transcript_usage(transcript_path: Optional[str],
                     tail_bytes: int = TRANSCRIPT_TAIL_BYTES) -> Optional[Dict[str, Any]]:
    """Latest main-thread assistant usage from a transcript JSONL (tail scan first)."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            start = max(0, size - tail_bytes)
            fh.seek(start)
            chunk = fh.read()
            lines = chunk.split(b"\n")
            if start > 0 and lines:
                lines = lines[1:]  # drop the partial first line
            for line in reversed(lines):
                hit = _usage_from_line(line)
                if hit:
                    return hit
            if start == 0:
                return None
            # Nothing in the tail: scan the whole file once (bounded by file size).
            fh.seek(0)
            last = None
            for line in fh:
                hit = _usage_from_line(line)
                if hit:
                    last = hit
            return last
    except OSError:
        return None


def mark_meter_stale(paths: Paths, session_id: Optional[str]) -> None:
    if not session_id:
        return
    for path in [*mod_meter_files(paths, session_id), paths.mod_meter(session_id),
                 paths.meter_state(session_id)]:
        st = read_json(path, default=None) if path else None
        if not isinstance(st, dict):
            continue
        st["stale"] = True
        st["stale_marked_at"] = iso_now()
        write_json(path, st)


def mod_meter_files(paths: Paths, session_id: Optional[str]) -> list:
    """The mod's reading files for a session, newest first (names start with epoch ms)."""
    d = paths.mod_meter_dir(session_id) if session_id else None
    try:
        names = os.listdir(d) if d else []
    except OSError:
        return []
    return [os.path.join(d, n) for n in sorted(names, reverse=True)
            if n.endswith(".json") and not n.startswith(".")]


def read_meter_json(path: str) -> tuple:
    """(data or None, unreadable). A file that exists but does not parse is retried:
    the mod's $.fs.write is not atomic, so a reader can meet a half-written file."""
    for attempt in range(METER_READ_RETRIES):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return (data, False) if isinstance(data, dict) else (None, True)
        except FileNotFoundError:
            return None, False
        except (OSError, ValueError):
            if attempt + 1 < METER_READ_RETRIES:
                time.sleep(METER_RETRY_SLEEP)
    return None, True


def read_mod_meter(paths: Paths, session_id: Optional[str]) -> tuple:
    """(newest parseable mod reading or None, unreadable). `unreadable` is True when a
    newer reading exists but cannot be parsed, so the caller knows the mod is there."""
    unreadable = False
    files = mod_meter_files(paths, session_id)
    legacy = paths.mod_meter(session_id) if session_id else None
    for path in files[:METER_KEEP_FILES + 2] + ([legacy] if legacy else []):
        data, bad = read_meter_json(path)
        if data is not None:
            return data, unreadable
        unreadable = unreadable or bad
    return None, unreadable


def _meter_file(path: Optional[str], stale_after: float, data: Any = None) -> tuple:
    """(data or None, age seconds or None, fresh) for one meter file (or parsed data)."""
    st = data if data is not None else (read_json(path, default=None) if path else None)
    if not isinstance(st, dict):
        return None, None, False
    ts = parse_iso(st.get("ts"))
    age = (time.time() - ts) if ts is not None else None
    fresh = (age is not None and age <= stale_after and not st.get("stale")
             and isinstance(st.get("used_pct"), (int, float)))
    return st, age, fresh


def _window_of(st: Optional[Dict[str, Any]]) -> Optional[float]:
    w = st.get("window_size") if st else None
    return w if isinstance(w, (int, float)) and not isinstance(w, bool) and w > 0 else None


def read_meter(paths: Paths, cfg: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """Current context usage for the session in the payload.

    Returns a dict with: used_pct (float|None), window_size (int|None), source
    ("mod" | "statusline" | "transcript" | "stale-mod" | "stale-statusline" | "none"),
    approx (bool), age_s (float|None), model (str|None), input_total (int|None).
    The "mod" readings exist in plugin mode only (hooks/meter.mjs).
    """
    sid = payload.get("session_id")
    stale_after = float(cfg.get("stale_seconds", 120))
    mod_data, mod_unreadable = read_mod_meter(paths, sid) if paths.plugin else (None, False)
    mod, mod_age, mod_fresh = _meter_file(None, stale_after, data=mod_data)
    if mod_fresh:
        return {
            "used_pct": float(mod["used_pct"]),
            "window_size": mod.get("window_size"),
            "source": "mod",
            "approx": False,
            "age_s": round(mod_age, 1),
            "model": mod.get("model"),
            "input_total": mod.get("input_total"),
        }
    st, age, fresh = _meter_file(paths.meter_state(sid) if sid else None, stale_after)
    if fresh:
        return {
            "used_pct": float(st["used_pct"]),
            "window_size": st.get("window_size"),
            "source": "statusline",
            "approx": bool(st.get("approx", False)),
            "age_s": round(age, 1),
            "model": st.get("model"),
            "input_total": st.get("input_total"),
        }

    # The mod knows the session's real window even when it has no percentage yet,
    # which is what lets the transcript estimate tell 1M from 200k.
    window_hint = _window_of(mod) or (st.get("window_size") if st else None)
    model_hint = (mod or {}).get("model") or (st.get("model") if st else None)
    tr = transcript_usage(payload.get("transcript_path"))
    if tr:
        model = tr.get("model") or model_hint
        known = isinstance(window_hint, (int, float)) and window_hint > 0
        window = window_hint if known else infer_window(model, tr["input_total"])
        pct = 100.0 * tr["input_total"] / float(window)
        out = {
            "used_pct": round(pct, 1),
            "window_size": int(window),
            "source": "transcript",
            "approx": True,
            "age_s": round(age, 1) if age is not None else None,
            "model": model,
            "input_total": tr["input_total"],
        }
        if mod_unreadable and not known:
            # The mod is writing but its reading could not be parsed: a guessed window
            # (200k for a 1M session) must not trigger anything this turn.
            out["unreliable"] = "mod reading unreadable and window unknown"
        return out

    for data, data_age, source in ((mod, mod_age, "stale-mod"), (st, age, "stale-statusline")):
        if data is not None and isinstance(data.get("used_pct"), (int, float)):
            return {
                "used_pct": float(data["used_pct"]),
                "window_size": data.get("window_size"),
                "source": source,
                "approx": True,
                "age_s": round(data_age, 1) if data_age is not None else None,
                "model": data.get("model"),
                "input_total": data.get("input_total"),
            }

    return {"used_pct": None, "window_size": window_hint, "source": "none", "approx": True,
            "age_s": age, "model": model_hint, "input_total": None}


def cmd_meter(argv: list) -> int:
    sid = _arg(argv, "--session-id") or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    paths = Paths(resolve_project_dir(explicit=_arg(argv, "--cwd")))
    cfg = load_config(paths)
    payload = {"session_id": sid, "transcript_path": _arg(argv, "--transcript"),
               "cwd": paths.project}
    print(json.dumps(read_meter(paths, cfg, payload), indent=2, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# Handoff documents: template, parsing, validation, redaction, mechanical fallback
# --------------------------------------------------------------------------- #

FRONT_MATTER_KEYS = ["session_id", "parent_session_id", "generation", "created_at",
                     "git_branch", "git_head", "status"]

# (name, heading regex, minimum non-template characters)
HANDOFF_SECTIONS = [
    ("objective", r"objective", 10),
    ("current_state", r"current\s+state", 10),
    ("in_progress", r"in\s+progress", 10),
    ("remaining_plan", r"remaining\s+plan", 10),
    ("decisions", r"decisions", 10),
    ("gotchas", r"gotchas|failing\s+tests|open\s+questions|quirks", 10),
    ("key_files", r"key\s+files", 10),
    ("constraints", r"constraints|user\s+preferences", 10),
    ("memory_updates", r"memory\s+updates", 3),
]

SECRET_PATTERNS = [
    # (compiled regex, group index to redact; 0 = whole match)
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), 0),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"), 0),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), 0),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), 0),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), 0),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"), 0),
    (re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._\-]{20,})"), 1),
    (re.compile(r"(?i)\b(?:api[_\-]?key|secret[_\-]?key|secret|access[_\-]?token|auth[_\-]?token|"
                r"token|password|passwd|pwd|client[_\-]?secret)\b\s*[:=]\s*[\"']?([^\s\"'`,;]{8,})"), 1),
    (re.compile(r"(?m)^\s*(?:export\s+)?[A-Z][A-Z0-9_]{2,}=[\"']?([^\s\"']{16,})[\"']?\s*$"), 1),
]

REDACTED = "[REDACTED]"


def handoff_filename(session_id: str, now: Optional[_dt.datetime] = None) -> str:
    return f"{compact_ts(now)}_{safe_id(session_id)}.md"


def find_handoffs_for_session(paths: Paths, session_id: str) -> list:
    """Handoff files written by this session, newest first."""
    if not os.path.isdir(paths.handoffs):
        return []
    suffix = f"_{safe_id(session_id)}.md"
    out = [os.path.join(paths.handoffs, f) for f in os.listdir(paths.handoffs)
           if f.endswith(suffix) and not f.startswith(".")]
    out.sort(key=lambda p: (os.path.basename(p), os.path.getmtime(p)), reverse=True)
    return out


def parse_front_matter(text: str) -> tuple:
    """Return (front_matter_dict or None, body)."""
    m = re.match(r"^﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", text, re.S)
    if not m:
        return None, text
    fm: Dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip().strip("\"'")
    return fm, text[m.end():]


def render_front_matter(fm: Dict[str, Any]) -> str:
    lines = ["---"]
    for k in FRONT_MATTER_KEYS:
        lines.append(f"{k}: {fm.get(k, '')}")
    for k, v in fm.items():
        if k not in FRONT_MATTER_KEYS:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def split_sections(body: str) -> list:
    """[(heading, body_text)] for every '##'/'###' heading; text before the first heading is dropped."""
    sections = []
    current: Optional[list] = None
    in_fence = False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
        m = re.match(r"^\s{0,3}(#{2,3})\s+(.*?)\s*#*\s*$", line) if not in_fence else None
        if m:
            if current is not None:
                sections.append((current[0], "\n".join(current[1])))
            current = [m.group(2), []]
        elif current is not None:
            current[1].append(line)
    if current is not None:
        sections.append((current[0], "\n".join(current[1])))
    return sections


def _norm_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip().lower())


def template_lines(paths: Paths) -> set:
    """Normalized instruction lines of the template, so copied boilerplate does not count."""
    text = read_text(paths.template)
    _, body = parse_front_matter(text)
    out = set()
    for _, sec in split_sections(body):
        for line in sec.splitlines():
            n = _norm_line(line)
            if n:
                out.add(n)
    return out


def count_words(text: str) -> int:
    return len(re.findall(r"\S+", text))


def redact_secrets(text: str) -> tuple:
    """Return (redacted_text, [descriptions]). Values are replaced, key names kept."""
    hits = []
    out = text
    for rx, group in SECRET_PATTERNS:
        def _sub(m: "re.Match", _g: int = group, _rx: "re.Pattern" = rx) -> str:
            whole = m.group(0)
            val = m.group(_g) if _g else whole
            if REDACTED in whole or not val:
                return whole
            line_no = out.count("\n", 0, m.start()) + 1
            hits.append(f"line {line_no}: {_rx.pattern[:40]}...")
            if _g:
                s, e = m.start(_g) - m.start(), m.end(_g) - m.start()
                return whole[:s] + REDACTED + whole[e:]
            return REDACTED
        out = rx.sub(_sub, out)
    return out, hits


def validate_handoff(text: str, paths: Paths, max_words: int = 2500,
                     require_status: Optional[str] = None) -> Dict[str, Any]:
    """Structural validation. Returns {ok, errors, warnings, word_count, sections, front_matter}."""
    errors, warnings = [], []
    fm, body = parse_front_matter(text)
    if fm is None:
        errors.append("front matter missing: the file must start with a '---' block containing "
                      + ", ".join(FRONT_MATTER_KEYS))
        fm = {}
    else:
        missing = [k for k in FRONT_MATTER_KEYS if not str(fm.get(k, "")).strip()]
        if missing:
            errors.append("front matter missing keys: " + ", ".join(missing))
        if require_status and fm.get("status") != require_status:
            errors.append(f"front matter status must be '{require_status}' (found '{fm.get('status')}')")

    tmpl = template_lines(paths)
    found: Dict[str, bool] = {name: False for name, _, _ in HANDOFF_SECTIONS}
    sections = split_sections(body)
    for name, rx, min_chars in HANDOFF_SECTIONS:
        for heading, sec_body in sections:
            if re.search(rx, heading, re.I):
                content = [l for l in sec_body.splitlines()
                           if _norm_line(l) and _norm_line(l) not in tmpl]
                chars = sum(len(re.sub(r"\s+", "", l)) for l in content)
                if chars >= min_chars:
                    found[name] = True
                else:
                    errors.append(f"section '{heading}' is empty (only template text)")
                break
        else:
            errors.append(f"missing section: {name.replace('_', ' ')}")

    wc = count_words(body)
    if wc > max_words * 1.1:
        errors.append(f"too long: {wc} words (limit {max_words})")
    elif wc > max_words:
        warnings.append(f"long: {wc} words (target under {max_words})")

    _, secret_hits = redact_secrets(body)
    if secret_hits:
        warnings.append("possible secrets were redacted: " + "; ".join(secret_hits[:5]))

    return {"ok": not errors, "errors": errors, "warnings": warnings, "word_count": wc,
            "sections": found, "front_matter": fm}


def load_and_sanitize_handoff(path: str) -> tuple:
    """Read a handoff, redact secrets in place (atomically) if needed. Returns (text, hits)."""
    text = read_text(path)
    clean, hits = redact_secrets(text)
    if hits and clean != text:
        atomic_write(path, clean)
    return clean, hits


def set_front_matter_status(path: str, status: str, **extra: Any) -> bool:
    """Atomically rewrite the front matter status (and extra keys). True if changed."""
    text = read_text(path)
    fm, body = parse_front_matter(text)
    if fm is None:
        return False
    fm["status"] = status
    for k, v in extra.items():
        fm[k] = v
    atomic_write(path, render_front_matter(fm) + "\n" + body.lstrip("\n"))
    return True


def render_template(paths: Paths, fm: Dict[str, Any]) -> str:
    text = read_text(paths.template)
    for k in FRONT_MATTER_KEYS:
        text = text.replace("{{" + k + "}}", str(fm.get(k, "")))
    return text


def _truncate_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    return " ".join(words[:max_words]).strip() + " […truncated]"


def _git_block(cwd: str, args: list, max_lines: int) -> str:
    out = _git(cwd, *args, timeout=10, strip=False)  # keep porcelain's leading spaces
    if not out.strip():
        return "(none)"
    lines = out.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"… {len(lines) - max_lines} more lines"]
    return "\n".join(lines)


def first_user_prompt(transcript_path: Optional[str], max_words: int = 150) -> str:
    if not transcript_path or not os.path.isfile(transcript_path):
        return ""
    try:
        with open(transcript_path, "rb") as fh:
            for _ in range(400):
                line = fh.readline()
                if not line:
                    break
                if b'"user"' not in line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if obj.get("type") != "user" or obj.get("isSidechain") or obj.get("isMeta"):
                    continue
                msg = obj.get("message", {})
                content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content
                                       if isinstance(c, dict) and c.get("type") == "text")
                if isinstance(content, str) and content.strip():
                    return _truncate_words(content, max_words)
    except OSError:
        pass
    return ""


def mechanical_handoff(paths: Paths, cfg: Dict[str, Any], payload: Dict[str, Any],
                       state: Dict[str, Any], attempts: int) -> str:
    """A handoff built from git state and the last assistant message. Always validates."""
    cwd = paths.project
    sid = payload.get("session_id", "unknown")
    last = _truncate_words(str(payload.get("last_assistant_message") or ""), 400) or "(not available)"
    first = first_user_prompt(payload.get("transcript_path")) or "(not available)"
    status = _git_block(cwd, ["status", "--porcelain=v1"], 40)
    diffstat = _git_block(cwd, ["diff", "--stat"], 30)
    commits = _git_block(cwd, ["log", "--oneline", "-10"], 10)
    excludes = ([":(exclude).claude/" + PLUGIN_DIR_NAME] if paths.plugin
                else [":(exclude).claude/handoffs", ":(exclude).claude/backups"])
    todos = _git_block(cwd, ["grep", "-n", "-I", "-E", "(TODO|FIXME|XXX)", "--", ".", *excludes], 25)
    changed = sorted({l[3:].strip() for l in status.splitlines() if len(l) > 3 and not l.startswith("…")})
    key_files = "\n".join(f"- `{f}` — modified or untracked in the working tree" for f in changed[:30]) \
        or "- (no modified files; see the commit list above)"
    fm = {
        "session_id": sid,
        "parent_session_id": state.get("parent") or "none",
        "generation": state.get("generation", 0),
        "created_at": iso_now(),
        "git_branch": git_branch(cwd) or "unknown",
        "git_head": git_head(cwd) or "unknown",
        "status": "pending",
        "mechanical": "true",
    }
    body = f"""
# Session handoff (mechanical fallback)

This handoff was generated automatically by the Session Relay after {attempts} failed
attempt(s) to obtain a written handoff from the previous session. Treat every section as
a starting point to reconstruct context, not as verified fact.

## 1. Objective and Definition of Done

The previous session's objective was not captured. The first user prompt of that session was:

> {first}

Definition of done: unknown. Re-derive it from the prompt above, the git history, and the
last assistant message in section 3, then confirm it with the user before large changes.

## 2. Current state (done and verified)

Nothing in this section was verified by the relay. Working tree (`git status --porcelain`):

```
{status}
```

Uncommitted changes (`git diff --stat`):

```
{diffstat}
```

Recent commits (`git log --oneline -10`):

```
{commits}
```

## 3. In progress

The last message the previous session produced before the handoff was forced:

> {last}

Very next concrete action: read `git diff`, run the project's test command if one exists,
and continue the work described in the message above.

## 4. Remaining plan

1. Review the uncommitted diff and the last assistant message to reconstruct the task.
2. Run the test suite or build to establish the current state.
3. Continue the task; write a proper handoff yourself when the relay asks for one.

## 5. Decisions made and approaches rejected

Not captured by the mechanical fallback. Check `.claude/memory/decisions.md` and recent
commit messages before changing course.

## 6. Gotchas, failing tests, environment quirks, open questions

- This handoff is mechanical: the previous session did not write one in {attempts} attempt(s).
- Open TODO/FIXME markers in the tree (`git grep`):

```
{todos}
```

## 7. Key files and commands

{key_files}

Commands: `git status`, `git diff`, `git log --oneline -20`, plus the project's own test/build commands.

## 8. Constraints and user preferences stated this session

Not captured. Check `CLAUDE.md`, `.claude/memory/conventions.md` and ask the user if unsure.

## 9. Memory updates

none
"""
    return render_front_matter(fm) + body.lstrip("\n")


def cmd_validate(argv: list) -> int:
    if not argv or not os.path.isfile(argv[0]):
        print("usage: relay.py validate <handoff.md> [--cwd <dir>] [--max-words N]", file=sys.stderr)
        return 2
    paths = Paths(resolve_project_dir(explicit=_arg(argv, "--cwd")))
    cfg = load_config(paths)
    max_words = int(_arg(argv, "--max-words", str(cfg.get("max_handoff_words", 2500))))
    report = validate_handoff(read_text(argv[0]), paths, max_words=max_words)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


# --------------------------------------------------------------------------- #
# Hook handlers (filled in by later steps)
# --------------------------------------------------------------------------- #

SOFT_CONTEXT = (
    "Session Relay notice: this session's context window is at {pct:.0f}% "
    "(soft threshold {soft:.0f}%, handoff threshold {hard:.0f}%{approx}). "
    "Wrap up gracefully from here: finish the current logical unit of work; avoid large "
    "file reads, broad searches, or new sub-investigations; keep short notes of open threads, "
    "decisions, and the exact next action; and expect a handoff request soon. When usage "
    "reaches {hard:.0f}%, a Stop hook will ask you to write a structured handoff file and the "
    "work will continue automatically in a fresh session."
)


def hook_prompt(paths: Paths, cfg: Dict[str, Any], payload: Dict[str, Any]) -> int:
    """UserPromptSubmit: inject wrap-up guidance once per session in [soft, hard)."""
    sid = payload.get("session_id")
    if not sid:
        return 0
    meter = read_meter(paths, cfg, payload)
    pct = meter.get("used_pct")
    log(paths, "DEBUG", "prompt: meter", session_id=sid, used_pct=pct,
        source=meter.get("source"), approx=meter.get("approx"))
    if pct is None:
        return 0
    if meter.get("unreliable"):
        log(paths, "WARN", "prompt: skipped, " + meter["unreliable"], session_id=sid, used_pct=pct)
        return 0
    state = load_relay_state(paths, sid)
    if state.get("launched"):
        return 0  # this session already handed off; nothing more to nudge
    soft, hard = float(cfg["soft"]), float(cfg["hard"])
    if not (soft <= pct < hard) or state.get("soft_notified_at"):
        return 0
    state["soft_notified_at"] = iso_now()
    state["soft_notified_pct"] = pct
    save_relay_state(paths, state)
    ledger_append(paths, "soft_trigger", sid, parent=state.get("parent"),
                  generation=state.get("generation"), used_pct=pct,
                  source=meter.get("source"), approx=meter.get("approx"))
    text = SOFT_CONTEXT.format(
        pct=pct, soft=soft, hard=hard,
        approx=", approximate reading" if meter.get("approx") else "",
    )
    emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                 "additionalContext": text}})
    log(paths, "INFO", "prompt: soft trigger injected", session_id=sid, used_pct=pct)
    return 0


BLOCK_REASON_FIRST = """Session Relay: this session's context window is at {pct:.0f}% \
(handoff threshold {hard:.0f}%{approx}). Before you stop, write a handoff so that a fresh \
session can continue this work without any prior context.

Do exactly this now, then end your turn:
1. Create the file `{path}` using the skeleton below. Keep the front matter exactly as given.
2. Replace the instructions under every one of the nine headings with real content for a \
reader with ZERO prior context: concrete file paths, function names, commands, exact error \
messages, the very next action. Stay under {max_words} words. Never include secrets \
(tokens, API keys, .env contents): refer to them by name only.
3. Do not start new work and do not run large commands. Once the file is written, stop.

A new session will be launched automatically and will read this file.

--- skeleton (copy, then fill in) ---
{template}"""

BLOCK_REASON_MISSING = """Session Relay: no handoff file was found for this session \
(expected `{path}`). This is the last request: write the handoff now with the nine \
sections (Objective and Definition of Done; Current state; In progress; Remaining plan; \
Decisions; Gotchas/open questions; Key files and commands; Constraints and user preferences; \
Memory updates) and this exact front matter, then stop:

{front_matter}
If the file is still missing after this turn, a minimal mechanical handoff will be \
generated from git state instead."""

BLOCK_REASON_INVALID = """Session Relay: the handoff at `{path}` is not valid yet:
{errors}

Fix exactly these problems in that file (keep the front matter and `status: pending`), \
then stop. This is the last retry; if the file still does not validate, a minimal \
mechanical handoff will be generated from git state instead."""


def _permission_mode_for_child(cfg: Dict[str, Any], payload: Dict[str, Any], paths: Paths) -> str:
    mode = str(payload.get("permission_mode") or "default")
    if mode == "manual":
        mode = "default"
    if mode == "bypassPermissions" and not cfg.get("launcher", {}).get("allow_bypass_inherit"):
        log(paths, "WARN", "parent runs in bypassPermissions; child downgraded to default "
                           "(set launcher.allow_bypass_inherit=true to inherit it)")
        mode = "default"
    return mode


LAUNCH_EVENTS = {0: "handoff_launched", 2: "limit_reached", 3: "launch_cooldown",
                 4: "launch_deferred"}


def launch_child(paths: Paths, cfg: Dict[str, Any], payload: Dict[str, Any],
                 state: Dict[str, Any], handoff_path: str, meter: Dict[str, Any],
                 mechanical: bool = False) -> Dict[str, Any]:
    """Pre-link a child session, run the launcher, record the outcome. Returns a summary."""
    sid = state["session_id"]
    generation = int(state.get("generation") or 0) + 1
    max_gen = int(cfg.get("max_generations", 8))
    common = dict(parent=sid, generation=generation, used_pct=meter.get("used_pct"),
                  handoff_path=handoff_path)

    if generation > max_gen:
        ledger_append(paths, "limit_reached", sid, reason=f"generation {generation} > max {max_gen}",
                      **common)
        state.update(launched=True, launch_rc=2, launch_event="limit_reached",
                     handoff_path=handoff_path)
        save_relay_state(paths, state)
        return {"event": "limit_reached", "rc": 2, "child": None,
                "message": f"Session Relay: generation limit ({max_gen}) reached; the chain "
                           f"stops here. Handoff kept at {handoff_path}."}

    child = new_uuid()
    mode = _permission_mode_for_child(cfg, payload, paths)
    child_state = {
        "session_id": child, "generation": generation, "parent": sid,
        "handoff_path": handoff_path, "status": "launching", "created_at": iso_now(),
        "block_attempts": 0, "launched": False, "permission_mode": mode,
    }
    save_relay_state(paths, child_state)
    try:
        set_front_matter_status(handoff_path, "pending", launched_child=child)
    except OSError:
        pass

    launcher = os.environ.get("CLAUDE_RELAY_LAUNCHER") or paths.launch_sh
    lcfg = cfg.get("launcher", {})
    cmd = ["sh", launcher,
           "--cwd", paths.project, "--relay-dir", paths.relay,
           "--handoff", handoff_path, "--parent", sid,
           "--child-id", child, "--generation", str(generation),
           "--permission-mode", mode,
           "--max-generations", str(max_gen),
           "--cooldown", str(int(cfg.get("cooldown_seconds", 60))),
           "--mode", str(lcfg.get("mode", "auto")),
           "--terminal-app", str(lcfg.get("terminal_app", "Terminal")),
           "--claude-bin", str(lcfg.get("claude_bin", "claude")),
           "--lock-stale", str(int(lcfg.get("lock_stale_seconds", 300)))]
    if cfg.get("close_old_session"):
        cmd.append("--close-old")
    if cfg.get("dry_run"):
        cmd.append("--dry-run")

    if not os.path.isfile(launcher):
        ledger_append(paths, "launch_failed", sid, reason=f"launcher not found: {launcher}", **common)
        state.update(launched=False, launch_rc=127, launch_event="launch_failed",
                     handoff_path=handoff_path, child_session_id=child)
        save_relay_state(paths, state)
        return {"event": "launch_failed", "rc": 127, "child": child,
                "message": f"Session Relay: launcher missing at {launcher}; handoff kept at {handoff_path}."}

    env = dict(os.environ)
    env["CLAUDE_RELAY_LOG"] = paths.log
    if not paths.plugin:
        env.setdefault("CLAUDE_RELAY_CONFIG", paths.config_file)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=env,
                              cwd=paths.project, check=False)
        rc, out, err = proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        rc, out, err = 1, "", str(exc)

    event = "launch_dry_run" if cfg.get("dry_run") and rc == 0 else LAUNCH_EVENTS.get(rc, "launch_failed")
    if mechanical and event == "handoff_launched":
        ledger_append(paths, "mechanical_handoff", sid, **common)
    ledger_append(paths, event, sid, child=child, launcher_rc=rc, launcher_out=out or None,
                  launcher_err=(err[:500] or None), permission_mode=mode, **common)
    chain_done = event in ("handoff_launched", "launch_dry_run", "limit_reached", "launch_deferred")
    state.update(launched=chain_done, launch_rc=rc, launch_event=event, handoff_path=handoff_path,
                 child_session_id=child, launched_at=iso_now() if chain_done else None)
    save_relay_state(paths, state)
    log(paths, "INFO", f"stop: launcher finished", session_id=sid, event=event, rc=rc, out=out, err=err[:300])

    short = child[:8]
    messages = {
        "handoff_launched": f"Session Relay: handoff written; continuing in a new session {short} "
                            f"(generation {generation}) — {out or 'launched'}.",
        "launch_dry_run": f"Session Relay (dry run): would launch generation {generation} as {short}; {out}",
        "limit_reached": f"Session Relay: generation limit reached; chain stopped. {out}",
        "launch_cooldown": f"Session Relay: launch skipped (cooldown); will retry at the next stop. {out}",
        "launch_deferred": f"Session Relay: could not open a terminal; run the command in "
                           f"{paths.next_command} to continue (generation {generation}).",
        "launch_failed": f"Session Relay: launcher failed (rc {rc}): {err[:200] or out}. Handoff kept at {handoff_path}.",
    }
    return {"event": event, "rc": rc, "child": child, "message": messages.get(event, out)}


def hook_stop(paths: Paths, cfg: Dict[str, Any], payload: Dict[str, Any]) -> int:
    """Stop: at >= hard, demand a handoff (max 2 blocks), then launch the next session."""
    sid = payload.get("session_id")
    if not sid:
        return 0
    state = load_relay_state(paths, sid)
    meter = read_meter(paths, cfg, payload)
    pct = meter.get("used_pct")
    active = bool(payload.get("stop_hook_active"))
    log(paths, "DEBUG", "stop: meter", session_id=sid, used_pct=pct, source=meter.get("source"),
        approx=meter.get("approx"), stop_hook_active=active, attempts=state.get("block_attempts"),
        launched=state.get("launched"))
    if state.get("launched"):
        return 0
    hard = float(cfg["hard"])
    if pct is None or pct < hard:
        return 0
    if meter.get("unreliable"):
        log(paths, "WARN", "stop: handoff skipped this turn, " + meter["unreliable"],
            session_id=sid, used_pct=pct)
        return 0

    max_attempts = int(cfg.get("max_block_attempts", 2))
    max_words = int(cfg.get("max_handoff_words", 2500))
    attempts = int(state.get("block_attempts") or 0)
    proposed = state.get("handoff_path_proposed") or os.path.join(paths.handoffs, handoff_filename(sid))
    existing = find_handoffs_for_session(paths, sid)

    # 1. A valid handoff exists -> launch.
    report = None
    if existing:
        path = existing[0]
        text, hits = load_and_sanitize_handoff(path)
        if hits:
            log(paths, "WARN", "stop: redacted possible secrets in handoff", path=path, hits=hits)
        report = validate_handoff(text, paths, max_words=max_words, require_status="pending")
        if report["ok"]:
            fm = report.get("front_matter") or {}
            if fm.get("mechanical") == "true":
                state["mechanical"] = True
            result = launch_child(paths, cfg, payload, state, path, meter,
                                  mechanical=bool(state.get("mechanical")))
            out: Dict[str, Any] = {}
            if result.get("message"):
                out["systemMessage"] = result["message"]
            if out:
                emit(out)
            return 0

    # 2. Still allowed to ask Claude -> block with a precise reason.
    fm = {
        "session_id": sid, "parent_session_id": state.get("parent") or "none",
        "generation": state.get("generation", 0), "created_at": iso_now(),
        "git_branch": git_branch(paths.project) or "unknown",
        "git_head": git_head(paths.project) or "unknown", "status": "pending",
    }
    if attempts < max_attempts:
        state["block_attempts"] = attempts + 1
        state["handoff_path_proposed"] = proposed
        state["last_block_at"] = iso_now()
        save_relay_state(paths, state)
        if report is not None and existing:
            reason = BLOCK_REASON_INVALID.format(
                path=existing[0], errors="\n".join(f"- {e}" for e in report["errors"]))
            event = "handoff_invalid"
        elif attempts == 0:
            reason = BLOCK_REASON_FIRST.format(
                pct=pct, hard=hard, approx=" (approximate reading)" if meter.get("approx") else "",
                path=proposed, max_words=max_words, template=render_template(paths, fm))
            event = "handoff_requested"
        else:
            reason = BLOCK_REASON_MISSING.format(path=proposed, front_matter=render_front_matter(fm))
            event = "handoff_requested_again"
        ledger_append(paths, event, sid, parent=state.get("parent"), generation=state.get("generation"),
                      used_pct=pct, handoff_path=existing[0] if existing else proposed,
                      attempt=attempts + 1, stop_hook_active=active,
                      errors=(report or {}).get("errors") or None)
        log(paths, "INFO", f"stop: blocking ({event})", session_id=sid, attempt=attempts + 1, used_pct=pct)
        emit({"decision": "block", "reason": reason})
        return 0

    # 3. Out of attempts -> mechanical handoff, then launch.
    text = mechanical_handoff(paths, cfg, payload, state, attempts)
    atomic_write(proposed, text)
    state["mechanical"] = True
    save_relay_state(paths, state)
    log(paths, "WARN", "stop: wrote mechanical handoff", session_id=sid, path=proposed)
    result = launch_child(paths, cfg, payload, state, proposed, meter, mechanical=True)
    out = {}
    if result.get("message"):
        out["systemMessage"] = ("Mechanical handoff generated (no valid handoff after "
                                f"{attempts} attempts). " + result["message"])
    if out:
        emit(out)
    return 0


ORPHAN_LINK_SECONDS = 300  # a handoff pre-linked to a child that never started becomes claimable

SESSION_START_INTRO = """# Session Relay: you are continuing a previous session

You are generation {generation} in a relay chain (parent session: {parent}). The previous \
session ran out of context and wrote the handoff below at `{path}`. Treat it as your \
starting context; you have no other memory of that session.

Do this first, briefly:
1. Confirm your understanding of the objective and the next action in at most 3 lines.
2. {memory_rule}
3. Continue with the "very next concrete action" from the In progress section. Keep notes \
as you go: this session will also be asked for a handoff when its context fills up.
"""

MEMORY_RULE = (
    "Promote the items under \"Memory updates\" into `.claude/memory/decisions.md`, "
    "`gotchas.md` or `conventions.md` (append one bullet each; skip anything already present), "
    "and keep `.claude/memory/INDEX.md` current. Do not edit `CLAUDE.md` beyond its single "
    "memory-index pointer line without asking the user."
)

MEMORY_RULE_PLUGIN = (
    "Promote the items under \"Memory updates\" into `.claude/memory/decisions.md`, "
    "`gotchas.md` or `conventions.md` (append one bullet each; skip anything already present; "
    "create the folder or a file only when there is something to write), and keep "
    "`.claude/memory/INDEX.md` current with one line per topic file. Do not edit `CLAUDE.md`: "
    "Session Relay injects the memory index at every session start."
)

MEMORY_INDEX_CONTEXT = """# Project memory (.claude/memory/INDEX.md, injected by Session Relay)

{index}

The topic files listed above are in `.claude/memory/`; read one when it is relevant. When \
you learn a durable project fact, add one bullet to the matching topic file and keep \
INDEX.md current. Do not copy this index into `CLAUDE.md`.
"""

SESSION_START_POINTER = (
    "Session Relay reminder ({source}): this session is generation {generation} of a relay "
    "chain (parent {parent}). The handoff you started from is at `{path}`; re-read it if "
    "context was lost. Keep notes for the next handoff."
)


def list_handoffs(paths: Paths) -> list:
    if not os.path.isdir(paths.handoffs):
        return []
    files = [os.path.join(paths.handoffs, f) for f in os.listdir(paths.handoffs)
             if f.endswith(".md") and not f.startswith(".")]
    files.sort(key=lambda p: os.path.basename(p), reverse=True)  # name starts with UTC ts
    return files


def find_pending_handoff(paths: Paths, session_id: str, state: Dict[str, Any]) -> Optional[str]:
    """The handoff this session should consume: its pre-linked one, else the newest pending."""
    linked = state.get("handoff_path")
    if linked and os.path.isfile(linked):
        fm, _ = parse_front_matter(read_text(linked))
        if fm and fm.get("status") == "pending":
            return linked
        return None  # linked but already consumed (or malformed): nothing to do
    now = time.time()
    for path in list_handoffs(paths):
        fm, _ = parse_front_matter(read_text(path))
        if not fm or fm.get("status") != "pending":
            continue
        other = fm.get("launched_child")
        if other and other != session_id:
            created = parse_iso(fm.get("created_at"))
            age = (now - created) if created else None
            try:
                age = now - os.path.getmtime(path) if age is None else age
            except OSError:
                pass
            if age is not None and age < ORPHAN_LINK_SECONDS:
                continue  # meant for a child that is still starting up
        return path
    return None


def _ledger_lines(entries: list) -> str:
    out = []
    for e in entries:
        out.append(f"- {e.get('ts')} {e.get('event')} session={str(e.get('session_id'))[:8]} "
                   f"gen={e.get('generation')} used={e.get('used_pct')}")
    return "\n".join(out) if out else "- (empty)"


def memory_index_text(paths: Paths, limit: int = 2000) -> str:
    index = read_text(os.path.join(paths.memory, "INDEX.md")).strip()
    return index[:limit] + (" […]" if len(index) > limit else "") if index else ""


def memory_context(paths: Paths) -> str:
    """Plugin mode's stand-in for the CLAUDE.md pointer: the index, when there is one."""
    index = memory_index_text(paths)
    return MEMORY_INDEX_CONTEXT.format(index=index) if index else ""


def prune_meter_files(paths: Paths, keep_seconds: float = METER_KEEP_SECONDS) -> int:
    """Drop per-session meter readings nobody has written for a week (plugin mode)."""
    if not paths.plugin:
        return 0
    removed = 0
    cutoff = time.time() - keep_seconds
    for sub in ("meter", "statusline"):
        d = os.path.join(paths.user_dir, sub)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            full = os.path.join(d, name)
            try:
                if os.path.isdir(full):
                    files = sorted(os.listdir(full), reverse=True)
                    for i, f in enumerate(files):
                        fp = os.path.join(full, f)
                        if i >= METER_KEEP_FILES or os.path.getmtime(fp) < cutoff:
                            os.unlink(fp)
                            removed += 1
                    if not os.listdir(full):
                        os.rmdir(full)
                elif name.endswith(".json") and os.path.getmtime(full) < cutoff:
                    os.unlink(full)
                    removed += 1
            except OSError:
                pass
    return removed


def maybe_prune_meter_files(paths: Paths) -> None:
    """Sweep old readings from any hook, active project or not, at most once an hour."""
    if not paths.plugin:
        return
    stamp = os.path.join(paths.user_dir, "meter", ".last-prune")
    try:
        if time.time() - os.path.getmtime(stamp) < METER_PRUNE_EVERY:
            return
    except OSError:
        if not os.path.isdir(os.path.dirname(stamp)):
            return  # the mod never wrote anything: nothing to sweep, nothing to create
    prune_meter_files(paths)
    try:
        with open(stamp, "w", encoding="utf-8") as fh:
            fh.write(iso_now() + "\n")
    except OSError:
        pass


def drop_session_meter(paths: Paths, session_id: Optional[str]) -> None:
    """Remove a session's per-user readings (the mod writes new ones if it resumes)."""
    if not paths.plugin or not session_id:
        return
    for path in [*mod_meter_files(paths, session_id), paths.mod_meter(session_id),
                 paths.meter_state(session_id)]:
        try:
            os.unlink(path)
        except OSError:
            pass
    try:
        os.rmdir(paths.mod_meter_dir(session_id))
    except OSError:
        pass


def refresh_statusline_copy(paths: Paths) -> bool:
    """Re-copy the installed status line when the plugin version changed."""
    if not paths.plugin:
        return False
    dst = os.path.join(paths.user_dir, "statusline.sh")
    stamp = os.path.join(paths.user_dir, "statusline.version")
    if not os.path.isfile(dst) or read_text(stamp).strip() == RELAY_VERSION:
        return False
    try:
        atomic_write(dst, read_text(os.path.join(paths.relay_home, "statusline.sh")), mode=0o755)
        atomic_write(stamp, RELAY_VERSION + "\n")
        return True
    except OSError:
        return False


def build_injection(paths: Paths, cfg: Dict[str, Any], session_id: str, handoff_path: str,
                    text: str, generation: int, parent: str) -> str:
    budget = int(cfg.get("inject_budget_chars", 16000))
    fm, body = parse_front_matter(text)
    intro = SESSION_START_INTRO.format(generation=generation, parent=parent, path=handoff_path,
                                       memory_rule=MEMORY_RULE_PLUGIN if paths.plugin else MEMORY_RULE)
    ledger = "## Recent relay ledger\n" + _ledger_lines(ledger_tail(paths, 3)) + "\n"
    index = memory_index_text(paths)
    memory = "## Project memory index (.claude/memory/INDEX.md)\n" + \
        (index or "(no memory index yet)") + "\n"
    fixed = len(intro) + len(ledger) + len(memory) + 200
    room = max(budget - fixed, 1000)
    handoff = "## Handoff\n" + body.strip()
    if len(handoff) > room:
        handoff = handoff[:room].rstrip() + f"\n\n[… truncated to fit the context budget; read the full file at {handoff_path}]"
    return "\n".join([intro, handoff, "", ledger, memory]).strip() + "\n"


def _emit_session_context(text: str) -> None:
    emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})


def hook_session_start(paths: Paths, cfg: Dict[str, Any], payload: Dict[str, Any]) -> int:
    """SessionStart: consume the pending handoff on startup; brief pointer otherwise.

    Plugin mode also injects .claude/memory/INDEX.md (when it exists) in place of the
    CLAUDE.md pointer the project-copy install adds; a resumed session already has it.
    """
    sid = payload.get("session_id")
    if not sid:
        return 0
    source = str(payload.get("source") or "startup")
    state = load_relay_state(paths, sid)
    if source == "compact":
        mark_meter_stale(paths, sid)  # the next hard check must not trust pre-compact numbers
    memory = memory_context(paths) if paths.plugin and source != "resume" else ""
    if paths.plugin and source == "startup" and refresh_statusline_copy(paths):
        log(paths, "INFO", "status line copy refreshed", version=RELAY_VERSION)

    if source != "startup":
        parts = []
        hp = state.get("handoff_path")
        if hp and state.get("status") == "active" and os.path.isfile(hp):
            parts.append(SESSION_START_POINTER.format(source=source, generation=state.get("generation"),
                                                      parent=state.get("parent"), path=hp))
            log(paths, "INFO", f"session-start({source}): pointer injected", session_id=sid)
        if memory:
            parts.append(memory)
        if parts:
            _emit_session_context("\n\n".join(parts))
        return 0

    stale = int(cfg.get("launcher", {}).get("lock_stale_seconds", 300))
    with Lock(paths.lock, stale_seconds=stale, paths=paths):
        hp = find_pending_handoff(paths, sid, state)
        if not hp:
            log(paths, "DEBUG", "session-start: no pending handoff", session_id=sid)
            if memory:
                _emit_session_context(memory)
                log(paths, "INFO", "session-start: memory index injected", session_id=sid,
                    chars=len(memory))
            return 0
        text, hits = load_and_sanitize_handoff(hp)
        fm, _ = parse_front_matter(text)
        parent = str(fm.get("session_id") or state.get("parent") or "unknown")
        if state.get("handoff_path") == hp and state.get("generation"):
            generation = int(state["generation"])
        else:
            try:
                generation = int(fm.get("generation") or 0) + 1
            except ValueError:
                generation = 1
        set_front_matter_status(hp, "consumed", consumed_by=sid, consumed_at=iso_now())
        state.update(handoff_path=hp, parent=parent, generation=generation, status="active",
                     consumed_at=iso_now(), block_attempts=0, launched=False)
        save_relay_state(paths, state)

    ledger_append(paths, "handoff_consumed", sid, parent=parent, generation=generation,
                  handoff_path=hp, redactions=len(hits) or None)
    if parent and parent != "unknown":
        pstate = read_json(paths.relay_state(parent), default=None)
        if isinstance(pstate, dict):
            pstate["child_started_at"] = iso_now()
            pstate["child_session_id"] = sid
            save_relay_state(paths, pstate)
    context = build_injection(paths, cfg, sid, hp, text, generation, parent)  # includes the index
    _emit_session_context(context)
    log(paths, "INFO", "session-start: handoff consumed", session_id=sid, handoff=hp,
        generation=generation, parent=parent, chars=len(context))
    return 0


BACKUP_DIR_RX = re.compile(r"^\d{8}T\d{6}Z_")


def prune_backups(paths: Paths, keep: int) -> list:
    """Remove the oldest backup directories beyond `keep`. Returns the removed paths."""
    if not os.path.isdir(paths.backups):
        return []
    dirs = sorted(d for d in os.listdir(paths.backups)
                  if BACKUP_DIR_RX.match(d) and os.path.isdir(os.path.join(paths.backups, d)))
    removed = []
    for d in dirs[:-keep] if keep > 0 else dirs:
        full = os.path.join(paths.backups, d)
        try:
            shutil.rmtree(full)
            removed.append(full)
        except OSError as exc:
            log(paths, "WARN", f"backup prune failed: {exc}", path=full)
    return removed


def hook_pre_compact(paths: Paths, cfg: Dict[str, Any], payload: Dict[str, Any]) -> int:
    """PreCompact: copy the transcript and write a snapshot, keep the last N backups."""
    sid = payload.get("session_id") or "unknown"
    state = load_relay_state(paths, sid)
    meter = read_meter(paths, cfg, payload)
    dest = os.path.join(paths.backups, f"{compact_ts()}_{safe_id(sid)}")
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(paths.backups, f"{compact_ts()}_{safe_id(sid)}-{n}")
        n += 1
    os.makedirs(dest, exist_ok=True)
    tp = payload.get("transcript_path")
    copied = False
    if tp and os.path.isfile(tp):
        try:
            shutil.copy2(tp, os.path.join(dest, "transcript.jsonl"))
            copied = True
        except OSError as exc:
            log(paths, "WARN", f"transcript copy failed: {exc}", path=tp)
    snapshot = {
        "ts": iso_now(), "session_id": sid, "trigger": payload.get("trigger"),
        "custom_instructions": payload.get("custom_instructions"),
        "transcript_path": tp, "transcript_copied": copied, "meter": meter,
        "generation": state.get("generation"), "parent": state.get("parent"),
        "handoff_path": state.get("handoff_path"), "cwd": paths.project,
        "git_head": git_head(paths.project) or None, "git_branch": git_branch(paths.project) or None,
        "git_status": _git_block(paths.project, ["status", "--porcelain=v1"], 50),
        "permission_mode": payload.get("permission_mode"),
    }
    write_json(os.path.join(dest, "snapshot.json"), snapshot)
    removed = prune_backups(paths, int(cfg.get("backups_keep", 10)))
    ledger_append(paths, "pre_compact", sid, parent=state.get("parent"),
                  generation=state.get("generation"), used_pct=meter.get("used_pct"),
                  handoff_path=state.get("handoff_path"), backup=dest,
                  trigger=payload.get("trigger"), pruned=len(removed) or None)
    log(paths, "INFO", "pre-compact: backup written", session_id=sid, dest=dest, copied=copied,
        pruned=len(removed))
    return 0


def hook_session_end(paths: Paths, cfg: Dict[str, Any], payload: Dict[str, Any]) -> int:
    """SessionEnd: finalize this session's ledger entry and state (must stay fast)."""
    sid = payload.get("session_id")
    if not sid:
        return 0
    state = load_relay_state(paths, sid)
    meter = read_meter(paths, cfg, payload)
    reason = payload.get("reason")
    state["ended_at"] = iso_now()
    state["end_reason"] = reason
    if state.get("status") not in ("launching",):
        state["status"] = "ended"
    save_relay_state(paths, state)
    ledger_append(paths, "session_end", sid, parent=state.get("parent"),
                  generation=state.get("generation"), used_pct=meter.get("used_pct"),
                  handoff_path=state.get("handoff_path"), reason=reason,
                  launched=bool(state.get("launched")) or None,
                  child=state.get("child_session_id"), launch_event=state.get("launch_event"))
    drop_session_meter(paths, sid)
    log(paths, "INFO", "session-end", session_id=sid, reason=reason, launched=state.get("launched"))
    return 0


HOOKS = {
    "prompt": hook_prompt,
    "stop": hook_stop,
    "session-start": hook_session_start,
    "pre-compact": hook_pre_compact,
    "session-end": hook_session_end,
}


# --------------------------------------------------------------------------- #
# Utility subcommands
# --------------------------------------------------------------------------- #

def _arg(argv: list, name: str, default: Optional[str] = None) -> Optional[str]:
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def cmd_status(argv: list) -> int:
    paths = Paths(resolve_project_dir(explicit=_arg(argv, "--cwd")))
    cfg = load_config(paths)
    info = {
        "relay_version": RELAY_VERSION,
        "project": paths.project,
        "relay_home": paths.relay_home,
        "config_file": paths.config_file,
        "config": cfg,
        "ledger_entries": len([l for l in read_text(paths.ledger).splitlines() if l.strip()]),
        "handoffs": sorted(os.listdir(paths.handoffs)) if os.path.isdir(paths.handoffs) else [],
    }
    if paths.plugin:
        active, reason = activation_state(paths, cfg)
        legacy = legacy_install(paths)
        sid = _arg(argv, "--session-id") or os.environ.get("CLAUDE_CODE_SESSION_ID")
        sl = _read_settings(user_settings_path()).get("statusLine")
        info.update({
            "mode": "plugin",
            "plugin_root": paths.plugin_root,
            "data_dir": paths.relay,
            "user_dir": paths.user_dir,
            "active": bool(active and cfg.get("enabled", True) and not legacy),
            "activation": reason if cfg.get("enabled", True) else "disabled (enabled: false)",
            "legacy_install": legacy,
            "config_layers": [{"layer": label, "path": path, "values": values}
                              for label, path, values in config_layers(paths)],
            "memory_index": os.path.isfile(os.path.join(paths.memory, "INDEX.md")),
            "statusline": ("installed" if isinstance(sl, dict) and STATUSLINE_MARK in str(sl.get("command"))
                           else "other" if sl else "not set"),
            "ledger_tail": ledger_tail(paths, 3),
        })
        if legacy:
            info["active"] = False
            info["activation"] = (f"standing down: the project-copy relay is wired in {legacy}; "
                                  "run its uninstall.sh to switch to the plugin")
        if sid and not sid.startswith("${"):
            info["session_id"] = sid
            info["meter"] = read_meter(paths, cfg, {"session_id": sid,
                                                    "transcript_path": _arg(argv, "--transcript")})
    else:
        info["mode"] = "project-copy"
    print(json.dumps(info, indent=2, sort_keys=True))
    return 0


def cmd_enable(argv: list, on: bool = True) -> int:
    """Plugin: record enabled true/false in <project>/.claude/session-relay/config.json."""
    paths = Paths(resolve_project_dir(explicit=_arg(argv, "--cwd")))
    if not paths.plugin:
        print("relay.py: enable/disable is for the plugin; the project-copy install uses "
              ".claude/relay/config.json", file=sys.stderr)
        return 2
    paths.ensure_dirs()
    data = read_json(paths.project_config, default=None)
    if not isinstance(data, dict):
        if os.path.exists(paths.project_config):
            print(f"relay.py: {paths.project_config} is not a JSON object; not touching it",
                  file=sys.stderr)
            return 1
        data = {}
    data["enabled"] = on
    write_json(paths.project_config, data)
    cfg = load_config(paths)
    active, reason = activation_state(paths, cfg)
    log(paths, "INFO", "project " + ("enabled" if on else "disabled"))
    print(json.dumps({"project": paths.project, "config_file": paths.project_config,
                      "enabled": on, "activation": cfg.get("activation"),
                      "active": bool(active and cfg.get("enabled", True)), "reason": reason},
                     indent=2, sort_keys=True))
    return 0


STATUSLINE_MARK = PLUGIN_DIR_NAME + "/statusline.sh"


def user_settings_path() -> str:
    return os.path.join(config_dir(), "settings.json")


def _read_settings(path: str) -> Dict[str, Any]:
    data = read_json(path, default=None)
    return data if isinstance(data, dict) else {}


def cmd_install_statusline(argv: list) -> int:
    """Plugin: copy statusline.sh to ~/.claude/session-relay/ and wire statusLine.

    Plugins cannot set statusLine, so this edits ~/.claude/settings.json once (with a
    backup). Another tool's status line is kept unless --force; --remove undoes it.
    """
    paths = Paths(resolve_project_dir(explicit=_arg(argv, "--cwd")))
    if not paths.plugin:
        print("relay.py: install-statusline is for the plugin; install.sh wires the "
              "project-copy status line", file=sys.stderr)
        return 2
    force, remove = "--force" in argv, "--remove" in argv
    settings = user_settings_path()
    dst = os.path.join(paths.user_dir, "statusline.sh")
    command = 'bash "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/session-relay/statusline.sh" --plugin'
    ours = {"type": "command", "command": command, "padding": 0}
    raw = read_text(settings).strip()
    try:
        data = json.loads(raw) if raw else {}
    except ValueError as exc:
        print(f"relay.py: {settings} is not valid JSON ({exc}); not touching it", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print(f"relay.py: {settings} must hold a JSON object; not touching it", file=sys.stderr)
        return 1
    current = data.get("statusLine")
    cur_cmd = str(current.get("command", "")) if isinstance(current, dict) else str(current or "")
    is_ours = STATUSLINE_MARK in cur_cmd
    result: Dict[str, Any] = {"settings": settings, "script": dst, "command": command}

    sidecar = os.path.join(paths.user_dir, "statusline.replaced.json")
    if remove:
        if is_ours:
            previous = read_json(sidecar, default=None)
            if isinstance(previous, dict) and previous.get("statusLine"):
                data["statusLine"] = previous["statusLine"]      # what --force replaced
                result["restored"] = previous["statusLine"]
            else:
                del data["statusLine"]
        result["status"] = "removed" if is_ours else "not installed"
    elif current and not is_ours and not force:
        result.update(status="conflict", existing=cur_cmd,
                      hint="another status line is configured; rerun with --force to replace it "
                           "(a backup of settings.json is written first)")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 3
    else:
        src = os.path.join(paths.relay_home, "statusline.sh")
        atomic_write(dst, read_text(src), mode=0o755)
        atomic_write(os.path.join(paths.user_dir, "statusline.version"), RELAY_VERSION + "\n")
        if current and not is_ours:
            write_json(sidecar, {"statusLine": current, "replaced_at": iso_now()})
        result["replaced"] = cur_cmd if current and not is_ours else None
        result["status"] = "unchanged" if current == ours else "installed"
        data["statusLine"] = ours
        if not shutil.which("jq"):
            result["warning"] = "jq not found: the status line needs it to read its input"

    if result["status"] in ("installed", "removed"):
        if os.path.isfile(settings):
            backup = f"{settings}.bak.{compact_ts()}"
            shutil.copy2(settings, backup)
            result["backup"] = backup
        # Keep the user's key order and formatting style: no sort_keys here.
        atomic_write(settings, json.dumps(data, indent=2) + "\n")
    if remove:
        for path in (dst, sidecar, os.path.join(paths.user_dir, "statusline.version")):
            try:
                os.unlink(path)
            except OSError:
                pass
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_reset(argv: list) -> int:
    sid = _arg(argv, "--session-id") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        print("usage: relay.py reset --session-id <id> [--cwd <dir>]", file=sys.stderr)
        return 2
    paths = Paths(resolve_project_dir(explicit=_arg(argv, "--cwd")))
    if paths.plugin:
        paths.ensure_dirs()
    st = load_relay_state(paths, sid)
    st["launched"] = False
    st["block_attempts"] = 0
    st.pop("soft_notified_at", None)
    st.pop("child_session_id", None)
    save_relay_state(paths, st)
    log(paths, "INFO", "state reset", session_id=sid)
    print(f"reset {sid}")
    return 0


UTILS = {
    "status": cmd_status,
    "reset": cmd_reset,
    "meter": cmd_meter,
    "validate": cmd_validate,
    "enable": cmd_enable,
    "disable": lambda argv: cmd_enable(argv, on=False),
    "install-statusline": cmd_install_statusline,
}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def run_hook(name: str) -> int:
    payload = read_payload()
    paths: Optional[Paths] = None
    try:
        paths = Paths(resolve_project_dir(payload))
        if paths.plugin:
            # Per-user meter housekeeping happens whether or not this project is active:
            # the mod writes a reading every turn in every project.
            maybe_prune_meter_files(paths)
            if name == "session-end":
                drop_session_meter(paths, payload.get("session_id"))
            # Decide before creating anything: an inactive project stays untouched.
            if legacy_install(paths):
                return 0
            cfg = load_config(paths)
            if not activation_state(paths, cfg)[0]:
                return 0
            if cfg.get("enabled", True):
                paths.ensure_dirs()
        else:
            paths.ensure_dirs()
            cfg = load_config(paths)
        if not cfg.get("enabled", True):
            log(paths, "INFO", f"{name}: relay disabled, no-op",
                session_id=payload.get("session_id"))
            return 0
        return HOOKS[name](paths, cfg, payload)
    except Exception:  # noqa: BLE001 - a relay bug must never hurt the session
        log(paths, "ERROR", f"{name}: internal error\n{traceback.format_exc()}")
        return 0


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    root = _arg(rest, "--plugin-root")
    if root and not root.startswith("${"):   # an unsubstituted placeholder is not a path
        os.environ["CLAUDE_PLUGIN_ROOT"] = root
    if cmd in HOOKS:
        return run_hook(cmd)
    if cmd in UTILS:
        return UTILS[cmd](rest)
    print(f"relay.py: unknown subcommand {cmd!r}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
