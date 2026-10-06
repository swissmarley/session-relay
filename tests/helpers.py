"""Shared helpers for the relay test-suite (python3 -m unittest, stdlib only)."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RELAY_HOME = os.path.join(ROOT, ".claude", "relay")
BIN = os.path.join(RELAY_HOME, "bin")
RELAY_PY = os.path.join(BIN, "relay.py")
STATUSLINE_SH = os.path.join(BIN, "statusline.sh")
LAUNCH_SH = os.path.join(BIN, "launch.sh")
FIXTURES = os.path.join(HERE, "fixtures")


def load_relay_module():
    spec = importlib.util.spec_from_file_location("relay", RELAY_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RelayTestCase(unittest.TestCase):
    """A temp project with its own .claude tree, isolated env, and a config override."""

    def setUp(self):
        self.relay = load_relay_module()
        self.tmp = tempfile.mkdtemp(prefix="relay-test-")
        self.project = os.path.join(self.tmp, "project")
        os.makedirs(self.project)
        # copy the relay code + template + config into the temp project (project-level install)
        dst = os.path.join(self.project, ".claude", "relay")
        shutil.copytree(RELAY_HOME, dst, ignore=shutil.ignore_patterns(
            "ledger.jsonl", "relay.log*", "lock", "launched", "run", "NEXT_COMMAND.txt", "__pycache__"))
        shutil.copytree(os.path.join(ROOT, ".claude", "memory"),
                        os.path.join(self.project, ".claude", "memory"))
        self.paths = self.relay.Paths(self.project, relay_home=dst)
        self.paths.ensure_dirs()
        self.config_path = os.path.join(dst, "config.json")
        self._env_backup = dict(os.environ)
        for k in list(os.environ):
            if k.startswith("CLAUDE_") or k == "CLAUDECODE":
                del os.environ[k]
        os.environ["CLAUDE_RELAY_CONFIG"] = self.config_path
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.project, check=True)
        subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "init"], cwd=self.project, check=True)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------- #
    def set_config(self, **overrides):
        cfg = json.load(open(self.config_path))
        for k, v in overrides.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
        json.dump(cfg, open(self.config_path, "w"), indent=2)
        return cfg

    def run_hook(self, name, payload, env=None, timeout=30):
        e = dict(os.environ)
        if env:
            e.update(env)
        proc = subprocess.run([sys.executable, RELAY_PY, name], input=json.dumps(payload),
                              capture_output=True, text=True, env=e, cwd=self.project,
                              timeout=timeout)
        out = proc.stdout.strip()
        data = None
        if out.startswith("{"):
            try:
                data = json.loads(out)
            except ValueError:
                data = None
        return proc.returncode, data, proc.stdout, proc.stderr

    def payload(self, event, session_id="sess-0001", **extra):
        p = {
            "session_id": session_id,
            "transcript_path": os.path.join(self.tmp, f"{session_id}.jsonl"),
            "cwd": self.project,
            "hook_event_name": event,
            "permission_mode": "default",
        }
        p.update(extra)
        return p

    def write_meter_state(self, session_id, used_pct, window_size=200000, age_seconds=0,
                          **extra):
        import datetime
        ts = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(seconds=age_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = {"session_id": session_id, "used_pct": used_pct, "window_size": window_size,
                "ts": ts, "model": "claude-opus-5-5", "approx": False}
        data.update(extra)
        self.relay.write_json(self.paths.meter_state(session_id), data)
        return data

    def ledger(self):
        text = self.relay.read_text(self.paths.ledger)
        return [json.loads(l) for l in text.splitlines() if l.strip()]

    def log_text(self):
        return self.relay.read_text(self.paths.log)
