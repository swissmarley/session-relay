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
    status [--cwd D]                                print relay config/state summary

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
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from typing import Any, Dict, Optional

RELAY_VERSION = "0.1.0"

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

class Paths:
    """Where code lives (relay_home) and where this project's data lives (project)."""

    def __init__(self, project_dir: str, relay_home: Optional[str] = None):
        self.project = os.path.abspath(project_dir)
        self.relay_home = os.path.abspath(
            relay_home or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self.claude = os.path.join(self.project, ".claude")
        self.relay = os.path.join(self.claude, "relay")
        self.state = os.path.join(self.claude, "state")
        self.handoffs = os.path.join(self.claude, "handoffs")
        self.backups = os.path.join(self.claude, "backups")
        self.memory = os.path.join(self.claude, "memory")
        self.ledger = os.path.join(self.relay, "ledger.jsonl")
        self.log = os.path.join(self.relay, "relay.log")
        self.lock = os.path.join(self.relay, "lock")
        self.launched = os.path.join(self.relay, "launched")
        self.run = os.path.join(self.relay, "run")
        self.next_command = os.path.join(self.relay, "NEXT_COMMAND.txt")
        self.launch_sh = os.path.join(self.relay_home, "bin", "launch.sh")

    @property
    def template(self) -> str:
        for cand in (
            os.path.join(self.relay, "handoff-template.md"),
            os.path.join(self.relay_home, "handoff-template.md"),
        ):
            if os.path.isfile(cand):
                return cand
        return os.path.join(self.relay_home, "handoff-template.md")

    @property
    def config_file(self) -> str:
        env = os.environ.get("CLAUDE_RELAY_CONFIG")
        if env:
            return env
        for cand in (
            os.path.join(self.relay, "config.json"),
            os.path.join(self.relay_home, "config.json"),
        ):
            if os.path.isfile(cand):
                return cand
        return os.path.join(self.relay, "config.json")

    def meter_state(self, session_id: str) -> str:
        return os.path.join(self.state, f"{safe_id(session_id)}.json")

    def relay_state(self, session_id: str) -> str:
        return os.path.join(self.state, f"{safe_id(session_id)}.relay.json")

    def ensure_dirs(self) -> None:
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


def load_config(paths: Paths) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(paths.config_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            cfg = _deep_merge(cfg, data)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:  # bad JSON must not break the session
        log(paths, "WARN", f"config unreadable, using defaults: {exc}")
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
    return cfg


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

def _git(cwd: str, *args: str, timeout: int = GIT_TIMEOUT) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
            check=False,
        )
        if out.returncode != 0:
            return ""
        return out.stdout.strip()
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
# Meter: statusLine state first, transcript fallback second
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
    st = read_json(paths.meter_state(session_id), default=None)
    if not isinstance(st, dict):
        return
    st["stale"] = True
    st["stale_marked_at"] = iso_now()
    write_json(paths.meter_state(session_id), st)


def read_meter(paths: Paths, cfg: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """Current context usage for the session in the payload.

    Returns a dict with: used_pct (float|None), window_size (int|None), source
    ("statusline" | "transcript" | "stale-statusline" | "none"), approx (bool),
    age_s (float|None), model (str|None), input_total (int|None).
    """
    sid = payload.get("session_id")
    stale_after = float(cfg.get("stale_seconds", 120))
    st = read_json(paths.meter_state(sid), default=None) if sid else None
    st = st if isinstance(st, dict) else None

    age: Optional[float] = None
    if st is not None:
        ts = parse_iso(st.get("ts"))
        age = (time.time() - ts) if ts is not None else None
        fresh = (age is not None and age <= stale_after and not st.get("stale")
                 and isinstance(st.get("used_pct"), (int, float)))
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

    window_hint = st.get("window_size") if st else None
    model_hint = st.get("model") if st else None
    tr = transcript_usage(payload.get("transcript_path"))
    if tr:
        model = tr.get("model") or model_hint
        window = window_hint if isinstance(window_hint, (int, float)) and window_hint > 0 \
            else infer_window(model, tr["input_total"])
        pct = 100.0 * tr["input_total"] / float(window)
        return {
            "used_pct": round(pct, 1),
            "window_size": int(window),
            "source": "transcript",
            "approx": True,
            "age_s": round(age, 1) if age is not None else None,
            "model": model,
            "input_total": tr["input_total"],
        }

    if st is not None and isinstance(st.get("used_pct"), (int, float)):
        return {
            "used_pct": float(st["used_pct"]),
            "window_size": st.get("window_size"),
            "source": "stale-statusline",
            "approx": True,
            "age_s": round(age, 1) if age is not None else None,
            "model": st.get("model"),
            "input_total": st.get("input_total"),
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


def hook_stop(paths: Paths, cfg: Dict[str, Any], payload: Dict[str, Any]) -> int:
    return 0


def hook_session_start(paths: Paths, cfg: Dict[str, Any], payload: Dict[str, Any]) -> int:
    return 0


def hook_pre_compact(paths: Paths, cfg: Dict[str, Any], payload: Dict[str, Any]) -> int:
    return 0


def hook_session_end(paths: Paths, cfg: Dict[str, Any], payload: Dict[str, Any]) -> int:
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
    print(json.dumps(info, indent=2, sort_keys=True))
    return 0


def cmd_reset(argv: list) -> int:
    sid = _arg(argv, "--session-id") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        print("usage: relay.py reset --session-id <id> [--cwd <dir>]", file=sys.stderr)
        return 2
    paths = Paths(resolve_project_dir(explicit=_arg(argv, "--cwd")))
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
}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def run_hook(name: str) -> int:
    payload = read_payload()
    paths: Optional[Paths] = None
    try:
        paths = Paths(resolve_project_dir(payload))
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
    if cmd in HOOKS:
        return run_hook(cmd)
    if cmd in UTILS:
        return UTILS[cmd](rest)
    print(f"relay.py: unknown subcommand {cmd!r}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
