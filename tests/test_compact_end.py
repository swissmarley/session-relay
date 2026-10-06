import json
import os
import shutil
import time

from helpers import RelayTestCase, FIXTURES, rtext


class PreCompactTests(RelayTestCase):
    def setUp(self):
        super().setUp()
        self.transcript = os.path.join(self.tmp, "sess-0001.jsonl")
        shutil.copy(os.path.join(FIXTURES, "transcript.jsonl"), self.transcript)

    def backups(self):
        return sorted(d for d in os.listdir(self.paths.backups) if not d.startswith("."))

    def test_backup_and_snapshot(self):
        self.write_meter_state("sess-0001", 77.0)
        st = self.relay.load_relay_state(self.paths, "sess-0001")
        st.update(generation=2, parent="p1", handoff_path="/h.md"); self.relay.save_relay_state(self.paths, st)
        rc, data, out, err = self.run_hook("pre-compact", self.payload("PreCompact", trigger="auto",
                                                                      custom_instructions="keep tests"))
        self.assertEqual((rc, out.strip()), (0, ""), err)
        dirs = self.backups()
        self.assertEqual(len(dirs), 1)
        self.assertRegex(dirs[0], r"^\d{8}T\d{6}Z_sess-0001$")
        d = os.path.join(self.paths.backups, dirs[0])
        self.assertEqual(rtext(os.path.join(d, "transcript.jsonl")), rtext(self.transcript))
        snap = json.load(open(os.path.join(d, "snapshot.json")))
        self.assertEqual(snap["trigger"], "auto")
        self.assertEqual(snap["custom_instructions"], "keep tests")
        self.assertTrue(snap["transcript_copied"])
        self.assertEqual(snap["meter"]["used_pct"], 77.0)
        self.assertEqual((snap["generation"], snap["parent"], snap["handoff_path"]), (2, "p1", "/h.md"))
        self.assertEqual(snap["git_branch"], "main")
        self.assertTrue(snap["git_head"])
        e = self.ledger()[-1]
        self.assertEqual((e["event"], e["trigger"], e["used_pct"], e["generation"]), ("pre_compact", "auto", 77.0, 2))
        self.assertEqual(e["backup"], d)

    def test_missing_transcript_still_snapshots(self):
        rc, data, out, err = self.run_hook("pre-compact", self.payload("PreCompact", session_id="sess-x",
                                                                      transcript_path="/nonexistent.jsonl", trigger="manual"))
        self.assertEqual(rc, 0, err)
        d = os.path.join(self.paths.backups, self.backups()[0])
        self.assertFalse(os.path.exists(os.path.join(d, "transcript.jsonl")))
        self.assertFalse(json.load(open(os.path.join(d, "snapshot.json")))["transcript_copied"])

    def test_prune_keeps_newest_n_and_ignores_foreign_dirs(self):
        for i in range(12):
            os.makedirs(os.path.join(self.paths.backups, f"202501{i+1:02d}T000000Z_old"))
        os.makedirs(os.path.join(self.paths.backups, "keep-me"))
        self.set_config(backups_keep=5)
        self.run_hook("pre-compact", self.payload("PreCompact", trigger="auto"))
        dirs = self.backups()
        self.assertIn("keep-me", dirs)
        relay_dirs = [d for d in dirs if d != "keep-me"]
        self.assertEqual(len(relay_dirs), 5)
        self.assertTrue(relay_dirs[-1].endswith("_sess-0001"))         # newest kept
        self.assertEqual(relay_dirs[0], "20250109T000000Z_old")        # 1..8 removed
        self.assertEqual(self.ledger()[-1]["pruned"], 8)

    def test_two_backups_in_same_second_do_not_collide(self):
        self.run_hook("pre-compact", self.payload("PreCompact", trigger="auto"))
        self.run_hook("pre-compact", self.payload("PreCompact", trigger="auto"))
        self.assertEqual(len(self.backups()), 2)


class SessionEndTests(RelayTestCase):
    def test_finalizes_state_and_ledger(self):
        self.write_meter_state("sess-0001", 61.0)
        st = self.relay.load_relay_state(self.paths, "sess-0001")
        st.update(generation=1, parent="p0", launched=True, child_session_id="c1",
                  launch_event="handoff_launched", handoff_path="/h.md", status="active")
        self.relay.save_relay_state(self.paths, st)
        t0 = time.time()
        rc, data, out, err = self.run_hook("session-end", self.payload("SessionEnd", reason="prompt_input_exit"))
        self.assertLess(time.time() - t0, 1.5)
        self.assertEqual((rc, out.strip()), (0, ""), err)
        st = self.relay.load_relay_state(self.paths, "sess-0001")
        self.assertEqual((st["status"], st["end_reason"]), ("ended", "prompt_input_exit"))
        self.assertTrue(st["ended_at"])
        e = self.ledger()[-1]
        self.assertEqual(e["event"], "session_end")
        self.assertEqual((e["reason"], e["generation"], e["parent"], e["child"], e["used_pct"]),
                         ("prompt_input_exit", 1, "p0", "c1", 61.0))
        self.assertTrue(e["launched"])

    def test_end_without_state_is_fine(self):
        rc, data, out, err = self.run_hook("session-end", self.payload("SessionEnd", session_id="fresh", reason="other"))
        self.assertEqual((rc, out.strip()), (0, ""), err)
        self.assertEqual(self.ledger()[-1]["session_id"], "fresh")
        self.assertEqual(self.relay.load_relay_state(self.paths, "fresh")["generation"], 0)
