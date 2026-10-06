import json
import os
import threading
import time

from helpers import RelayTestCase


class FoundationTests(RelayTestCase):
    def test_config_defaults_and_override(self):
        cfg = self.relay.load_config(self.paths)
        self.assertEqual(cfg["soft"], 40)
        self.assertEqual(cfg["hard"], 50)
        self.set_config(soft=10, hard=20, launcher={"mode": "file"})
        cfg = self.relay.load_config(self.paths)
        self.assertEqual((cfg["soft"], cfg["hard"]), (10, 20))
        self.assertEqual(cfg["launcher"]["mode"], "file")
        self.assertEqual(cfg["launcher"]["claude_bin"], "claude")  # deep merge keeps defaults

    def test_kill_switch_env(self):
        os.environ["CLAUDE_RELAY_DISABLE"] = "1"
        cfg = self.relay.load_config(self.paths)
        self.assertFalse(cfg["enabled"])

    def test_bad_config_falls_back_to_defaults(self):
        open(self.config_path, "w").write("{not json")
        cfg = self.relay.load_config(self.paths)
        self.assertEqual(cfg["hard"], 50)
        self.assertIn("config unreadable", self.log_text())

    def test_atomic_write_and_json_roundtrip(self):
        p = os.path.join(self.project, "a", "b.json")
        self.relay.write_json(p, {"x": 1})
        self.assertEqual(self.relay.read_json(p), {"x": 1})
        self.assertFalse([f for f in os.listdir(os.path.dirname(p)) if f.startswith(".tmp-")])

    def test_lock_is_exclusive_and_reclaims_stale(self):
        lock_path = os.path.join(self.paths.relay, "lock")
        with self.relay.Lock(lock_path, stale_seconds=300, wait_seconds=0.2):
            with self.assertRaises(self.relay.LockTimeout):
                self.relay.Lock(lock_path, stale_seconds=300, wait_seconds=0.2).acquire()
        self.assertFalse(os.path.exists(lock_path))
        # stale lock: pretend it is old
        os.mkdir(lock_path)
        old = time.time() - 1000
        os.utime(lock_path, (old, old))
        with self.relay.Lock(lock_path, stale_seconds=300, wait_seconds=0.2, paths=self.paths) as lk:
            self.assertTrue(lk.held)
        self.assertIn("reclaiming stale lock", self.log_text())

    def test_lock_serializes_threads(self):
        lock_path = os.path.join(self.paths.relay, "lock")
        counter = {"n": 0, "max": 0}
        def work():
            with self.relay.Lock(lock_path, wait_seconds=5):
                counter["n"] += 1
                counter["max"] = max(counter["max"], counter["n"])
                time.sleep(0.02)
                counter["n"] -= 1
        ts = [threading.Thread(target=work) for _ in range(6)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(counter["max"], 1)

    def test_ledger_append_and_tail(self):
        for i in range(5):
            self.relay.ledger_append(self.paths, f"e{i}", "s", generation=i)
        tail = self.relay.ledger_tail(self.paths, 3)
        self.assertEqual([e["event"] for e in tail], ["e2", "e3", "e4"])
        self.assertTrue(all("git_head" in e for e in tail))

    def test_log_rotation(self):
        self.relay.LOG_MAX_BYTES = 2000
        for i in range(200):
            self.relay.log(self.paths, "INFO", "x" * 50)
        self.assertTrue(os.path.exists(self.paths.log + ".1"))

    def test_hooks_never_fail_loudly(self):
        for name in ("prompt", "stop", "session-start", "pre-compact", "session-end"):
            rc, data, out, err = self.run_hook(name, {})
            self.assertEqual(rc, 0, (name, err))
            self.assertEqual(out.strip(), "")

    def test_disabled_relay_is_noop(self):
        self.write_meter_state("sess-0001", 99)
        rc, data, out, err = self.run_hook("stop", self.payload("Stop"),
                                           env={"CLAUDE_RELAY_DISABLE": "1"})
        self.assertEqual((rc, out.strip()), (0, ""))
        self.assertIn("relay disabled", self.log_text())

    def test_reset_subcommand(self):
        st = self.relay.load_relay_state(self.paths, "sess-0001")
        st["launched"] = True; st["block_attempts"] = 2
        self.relay.save_relay_state(self.paths, st)
        self.relay.main(["reset", "--session-id", "sess-0001", "--cwd", self.project])
        st = self.relay.load_relay_state(self.paths, "sess-0001")
        self.assertEqual((st["launched"], st["block_attempts"]), (False, 0))

    def test_resolve_project_dir_precedence(self):
        del os.environ["CLAUDE_PROJECT_DIR"]
        self.assertEqual(self.relay.resolve_project_dir({"cwd": "/a"}), "/a")
        os.environ["CLAUDE_PROJECT_DIR"] = "/b"
        self.assertEqual(self.relay.resolve_project_dir({"cwd": "/a"}), "/b")
