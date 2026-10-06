import os
import shutil

from helpers import RelayTestCase, FIXTURES


class PromptHookTests(RelayTestCase):
    def prompt(self, sid="sess-0001", **extra):
        return self.run_hook("prompt", self.payload("UserPromptSubmit", session_id=sid,
                                                    prompt="continue please", **extra))

    def test_below_soft_is_silent(self):
        self.write_meter_state("sess-0001", 39.9)
        rc, data, out, err = self.prompt()
        self.assertEqual((rc, out.strip()), (0, ""))
        self.assertEqual(self.ledger(), [])

    def test_in_band_injects_once(self):
        self.write_meter_state("sess-0001", 42.0)
        rc, data, out, err = self.prompt()
        self.assertEqual(rc, 0, err)
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("42%", ctx)
        self.assertIn("finish the current logical unit", ctx)
        self.assertIn("50%", ctx)
        self.assertNotIn("approximate", ctx)
        st = self.relay.load_relay_state(self.paths, "sess-0001")
        self.assertTrue(st["soft_notified_at"])
        self.assertEqual([e["event"] for e in self.ledger()], ["soft_trigger"])
        # second prompt in the band: nothing, even when usage grew
        self.write_meter_state("sess-0001", 47.0)
        rc, data, out, err = self.prompt()
        self.assertEqual((rc, out.strip()), (0, ""))
        self.assertEqual(len(self.ledger()), 1)

    def test_at_or_above_hard_is_left_to_stop_hook(self):
        self.write_meter_state("sess-0001", 50.0)
        rc, data, out, err = self.prompt()
        self.assertEqual((rc, out.strip()), (0, ""))
        self.write_meter_state("sess-0001", 88.0)
        rc, data, out, err = self.prompt()
        self.assertEqual((rc, out.strip()), (0, ""))

    def test_boundaries_inclusive_exclusive(self):
        self.write_meter_state("sess-a", 40.0)
        rc, data, _, _ = self.prompt(sid="sess-a")
        self.assertIsNotNone(data)
        self.write_meter_state("sess-b", 49.99)
        rc, data, _, _ = self.prompt(sid="sess-b")
        self.assertIsNotNone(data)

    def test_per_session_tracking(self):
        self.write_meter_state("sess-1", 45.0)
        self.write_meter_state("sess-2", 45.0)
        self.assertIsNotNone(self.prompt(sid="sess-1")[1])
        self.assertIsNotNone(self.prompt(sid="sess-2")[1])
        self.assertIsNone(self.prompt(sid="sess-1")[1])

    def test_fallback_meter_marks_approximate(self):
        transcript = os.path.join(self.tmp, "sess-0001.jsonl")
        shutil.copy(os.path.join(FIXTURES, "transcript.jsonl"), transcript)   # 90k tokens
        self.write_meter_state("sess-0001", 10.0, window_size=200000, age_seconds=999)  # stale
        rc, data, out, err = self.prompt()                                      # 45% via transcript
        self.assertIsNotNone(data, err)
        self.assertIn("approximate reading", data["hookSpecificOutput"]["additionalContext"])

    def test_unknown_usage_is_silent(self):
        rc, data, out, err = self.prompt(sid="sess-unknown")
        self.assertEqual((rc, out.strip()), (0, ""))

    def test_custom_thresholds(self):
        self.set_config(soft=5, hard=9)
        self.write_meter_state("sess-0001", 6.0)
        self.assertIsNotNone(self.prompt()[1])

    def test_disabled_and_launched_are_silent(self):
        self.write_meter_state("sess-0001", 45.0)
        rc, data, out, _ = self.prompt(sid="sess-0001") if False else (0, None, "", "")
        st = self.relay.load_relay_state(self.paths, "sess-0001")
        st["launched"] = True
        self.relay.save_relay_state(self.paths, st)
        rc, data, out, err = self.prompt()
        self.assertEqual((rc, out.strip()), (0, ""))
        rc, data, out, err = self.run_hook("prompt", self.payload("UserPromptSubmit", session_id="sess-9"),
                                           env={"CLAUDE_RELAY_DISABLE": "1"})
        self.assertEqual((rc, out.strip()), (0, ""))
