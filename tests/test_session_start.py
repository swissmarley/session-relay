import json
import os
import subprocess
import sys
import threading

from helpers import RelayTestCase, FIXTURES, RELAY_PY, wtext, rtext

GOOD = rtext(os.path.join(FIXTURES, "handoff_good.md"))


class SessionStartTests(RelayTestCase):
    def start(self, sid="child-1", source="startup"):
        return self.run_hook("session-start", self.payload("SessionStart", session_id=sid, source=source))

    def handoff(self, name="20261006T120000Z_sess-parent-1.md", text=GOOD, **fm_extra):
        p = os.path.join(self.paths.handoffs, name)
        if fm_extra:
            fm, body = self.relay.parse_front_matter(text)
            fm.update(fm_extra)
            text = self.relay.render_front_matter(fm) + "\n" + body
        wtext(p, text)
        return p

    def prelink(self, sid, path, generation=1, parent="sess-parent-1"):
        self.relay.save_relay_state(self.paths, {"session_id": sid, "generation": generation, "parent": parent,
                                                 "handoff_path": path, "status": "launching",
                                                 "block_attempts": 0, "launched": False})

    def ctx(self, data):
        self.assertIsNotNone(data)
        hso = data["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "SessionStart")
        return hso["additionalContext"]

    def test_prelinked_startup_consumes_and_injects(self):
        path = self.handoff(launched_child="child-1")
        self.prelink("child-1", path)
        self.relay.save_relay_state(self.paths, {"session_id": "sess-parent-1", "generation": 0,
                                                 "launched": True, "child_session_id": "child-1"})
        for i in range(2):
            self.relay.ledger_append(self.paths, f"e{i}", "sess-parent-1", generation=0)
        rc, data, out, err = self.start()
        self.assertEqual(rc, 0, err)
        c = self.ctx(data)
        self.assertIn("generation 1", c)
        self.assertIn("parent session: sess-parent-1", c)
        self.assertIn(path, c)
        self.assertIn("## 3. In progress", c)                 # handoff body
        self.assertIn("Promote the items", c)                 # instructions
        self.assertIn("## Recent relay ledger", c)
        self.assertIn("e1 session=sess-par", c)
        self.assertIn("decisions.md", c)                      # memory index
        self.assertLessEqual(len(c), 16000)
        fm, _ = self.relay.parse_front_matter(rtext(path))
        self.assertEqual((fm["status"], fm["consumed_by"]), ("consumed", "child-1"))
        self.assertTrue(fm["consumed_at"].endswith("Z"))
        st = self.relay.load_relay_state(self.paths, "child-1")
        self.assertEqual((st["status"], st["generation"], st["parent"]), ("active", 1, "sess-parent-1"))
        pst = self.relay.load_relay_state(self.paths, "sess-parent-1")
        self.assertEqual(pst["child_session_id"], "child-1")
        self.assertTrue(pst["child_started_at"])
        self.assertEqual(self.ledger()[-1]["event"], "handoff_consumed")
        self.assertEqual(self.ledger()[-1]["generation"], 1)

    def test_consume_once(self):
        path = self.handoff(launched_child="child-1")
        self.prelink("child-1", path)
        self.assertIsNotNone(self.start()[1])
        self.assertIsNone(self.start()[1])                   # same session again: consumed already
        self.assertIsNone(self.start(sid="child-2")[1])      # another session: nothing pending
        self.assertEqual([e["event"] for e in self.ledger()], ["handoff_consumed"])

    def test_unlinked_pending_handoff_is_picked_newest_first(self):
        old = self.handoff(name="20261005T000000Z_sess-old.md", session_id="sess-old", generation="2")
        new = self.handoff(name="20261006T000000Z_sess-new.md", session_id="sess-new", generation="4")
        rc, data, _, _ = self.start(sid="manual-1")
        c = self.ctx(data)
        self.assertIn(new, c)
        st = self.relay.load_relay_state(self.paths, "manual-1")
        self.assertEqual((st["generation"], st["parent"]), (5, "sess-new"))
        self.assertEqual(self.relay.parse_front_matter(rtext(old))[0]["status"], "pending")
        rc, data, _, _ = self.start(sid="manual-2")
        self.assertIn(old, self.ctx(data))

    def test_linked_to_another_child_is_skipped_until_orphaned(self):
        import datetime
        fresh = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        path = self.handoff(launched_child="child-x", created_at=fresh)
        self.assertIsNone(self.start(sid="stranger")[1])
        self.handoff(launched_child="child-x", created_at="2026-10-06T00:00:00Z")
        old = 0
        os.utime(path, (old, old))
        self.assertIsNotNone(self.start(sid="stranger")[1])  # orphan recovery

    def test_budget_truncates_handoff_body(self):
        self.set_config(inject_budget_chars=6000)
        big = GOOD.replace("## 9. Memory updates", ("filler line of text\n" * 600) + "## 9. Memory updates")
        path = self.handoff(text=big, launched_child="child-1")
        self.prelink("child-1", path)
        c = self.ctx(self.start()[1])
        self.assertLessEqual(len(c), 6000 + 100)
        self.assertIn("truncated to fit the context budget", c)
        self.assertIn(path, c)
        self.assertIn("## Project memory index", c)         # index survives truncation

    def test_secrets_redacted_in_injection_and_file(self):
        leaky = GOOD.replace("## 9. Memory updates\n", "## 9. Memory updates\n\nTOKEN=abcdefghijklmnopqrstuvwxyz\n")
        path = self.handoff(text=leaky, launched_child="child-1")
        self.prelink("child-1", path)
        c = self.ctx(self.start()[1])
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", c)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", rtext(path))
        self.assertEqual(self.ledger()[-1]["redactions"], 1)

    def test_resume_and_compact_inject_pointer_only(self):
        path = self.handoff(launched_child="child-1")
        self.prelink("child-1", path)
        self.start()
        self.write_meter_state("child-1", 48.0)
        rc, data, _, _ = self.start(source="resume")
        c = self.ctx(data)
        self.assertIn("reminder (resume)", c)
        self.assertIn(path, c)
        self.assertLess(len(c), 600)
        self.assertNotIn("## 3. In progress", c)
        rc, data, _, _ = self.start(source="compact")
        self.assertIn("reminder (compact)", self.ctx(data))
        st = json.load(open(self.paths.meter_state("child-1")))
        self.assertTrue(st["stale"])
        self.assertEqual(self.relay.parse_front_matter(rtext(path))[0]["status"], "consumed")

    def test_resume_of_non_relay_session_is_silent(self):
        self.assertIsNone(self.start(sid="plain", source="resume")[1])
        self.assertIsNone(self.start(sid="plain", source="clear")[1])
        self.assertIsNone(self.start(sid="plain", source="startup")[1])

    def test_concurrent_startups_consume_exactly_once(self):
        path = self.handoff()
        results = {}
        def go(sid):
            results[sid] = self.start(sid=sid)[1]
        ts = [threading.Thread(target=go, args=(f"c{i}",)) for i in range(6)]
        [t.start() for t in ts]; [t.join() for t in ts]
        winners = [sid for sid, d in results.items() if d is not None]
        self.assertEqual(len(winners), 1, winners)
        fm, _ = self.relay.parse_front_matter(rtext(path))
        self.assertEqual(fm["consumed_by"], winners[0])
        self.assertEqual(len([e for e in self.ledger() if e["event"] == "handoff_consumed"]), 1)

    def test_disabled(self):
        path = self.handoff(launched_child="child-1")
        self.prelink("child-1", path)
        rc, data, out, err = self.run_hook("session-start", self.payload("SessionStart", session_id="child-1", source="startup"),
                                           env={"CLAUDE_RELAY_DISABLE": "1"})
        self.assertEqual((rc, out.strip()), (0, ""))
        self.assertEqual(self.relay.parse_front_matter(rtext(path))[0]["status"], "pending")
