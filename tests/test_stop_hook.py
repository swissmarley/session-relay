import json
import os
import re
import uuid

from helpers import RelayTestCase, FIXTURES, wtext, rtext

GOOD = rtext(os.path.join(FIXTURES, "handoff_good.md"))
FAKE = os.path.join(FIXTURES, "fake_launch.sh")


class StopHookTests(RelayTestCase):
    def setUp(self):
        super().setUp()
        self.launch_log = os.path.join(self.tmp, "launch.args")
        os.environ["CLAUDE_RELAY_LAUNCHER"] = FAKE
        os.environ["FAKE_LAUNCH_LOG"] = self.launch_log

    def stop(self, sid="sess-0001", **extra):
        extra.setdefault("last_assistant_message", "I finished editing foo.py and ran the tests.")
        extra.setdefault("stop_hook_active", False)
        return self.run_hook("stop", self.payload("Stop", session_id=sid, **extra))

    def launch_args(self):
        if not os.path.exists(self.launch_log):
            return None
        return rtext(self.launch_log).split("\n")

    def write_handoff(self, sid="sess-0001", text=GOOD, name=None):
        name = name or self.relay.handoff_filename(sid)
        p = os.path.join(self.paths.handoffs, name)
        wtext(p, text.replace("session_id: sess-parent-1", f"session_id: {sid}"))
        return p

    def state(self, sid="sess-0001"):
        return self.relay.load_relay_state(self.paths, sid)

    # ---- below threshold ------------------------------------------------- #
    def test_below_hard_is_silent(self):
        self.write_meter_state("sess-0001", 49.9)
        rc, data, out, err = self.stop()
        self.assertEqual((rc, out.strip()), (0, ""))
        self.assertEqual(self.state()["block_attempts"], 0)
        self.assertEqual(self.ledger(), [])

    def test_unknown_usage_is_silent(self):
        rc, data, out, err = self.stop(sid="sess-nometer")
        self.assertEqual((rc, out.strip()), (0, ""))

    # ---- block flow -------------------------------------------------------- #
    def test_first_block_contains_template_and_path(self):
        self.write_meter_state("sess-0001", 55.0)
        rc, data, out, err = self.stop()
        self.assertEqual(rc, 0, err)
        self.assertEqual(data["decision"], "block")
        reason = data["reason"]
        self.assertIn("55%", reason)
        m = re.search(r"`([^`]+_sess-0001\.md)`", reason)
        self.assertIsNotNone(m, reason)
        self.assertTrue(m.group(1).startswith(self.paths.handoffs))
        self.assertIn("session_id: sess-0001", reason)
        self.assertIn("status: pending", reason)
        self.assertIn("## 9. Memory updates", reason)
        self.assertIn("git_branch: main", reason)
        st = self.state()
        self.assertEqual(st["block_attempts"], 1)
        self.assertEqual(st["handoff_path_proposed"], m.group(1))
        self.assertEqual([e["event"] for e in self.ledger()], ["handoff_requested"])

    def test_second_block_when_still_missing_then_mechanical(self):
        self.write_meter_state("sess-0001", 60.0)
        self.stop()
        rc, data, out, err = self.stop(stop_hook_active=True)
        self.assertEqual(data["decision"], "block")
        self.assertIn("no handoff file was found", data["reason"])
        self.assertIn("session_id: sess-0001", data["reason"])
        self.assertEqual(self.state()["block_attempts"], 2)
        # third stop: never block again; mechanical handoff + launch
        rc, data, out, err = self.stop(stop_hook_active=True)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("decision", data or {})
        self.assertIn("Mechanical handoff generated", data["systemMessage"])
        st = self.state()
        self.assertTrue(st["launched"])
        self.assertTrue(st["mechanical"])
        path = st["handoff_path"]
        self.assertTrue(os.path.isfile(path))
        text = rtext(path)
        self.assertIn("mechanical: true", text)
        self.assertIn("I finished editing foo.py", text)
        self.assertTrue(self.relay.validate_handoff(text, self.paths, require_status="pending")["ok"])
        events = [e["event"] for e in self.ledger()]
        self.assertEqual(events, ["handoff_requested", "handoff_requested_again",
                                  "mechanical_handoff", "handoff_launched"])
        args = self.launch_args()
        self.assertIn("--handoff", args)
        self.assertEqual(args[args.index("--generation") + 1], "1")
        self.assertEqual(args[args.index("--permission-mode") + 1], "default")
        self.assertEqual(args[args.index("--parent") + 1], "sess-0001")
        child = args[args.index("--child-id") + 1]
        uuid.UUID(child)
        cst = self.relay.load_relay_state(self.paths, child)
        self.assertEqual((cst["generation"], cst["parent"], cst["handoff_path"]), (1, "sess-0001", path))
        # fourth stop: idempotent
        rc, data, out, err = self.stop()
        self.assertEqual((rc, out.strip()), (0, ""))
        self.assertEqual(len(self.ledger()), 4)

    def test_valid_handoff_after_first_block_launches(self):
        self.write_meter_state("sess-0001", 52.0)
        self.stop()
        path = self.write_handoff()
        rc, data, out, err = self.stop(stop_hook_active=True)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("decision", data)
        self.assertIn("continuing in a new session", data["systemMessage"])
        self.assertIn("launched fake", data["systemMessage"])
        st = self.state()
        self.assertTrue(st["launched"])
        self.assertEqual(st["handoff_path"], path)
        self.assertEqual([e["event"] for e in self.ledger()], ["handoff_requested", "handoff_launched"])
        fm, _ = self.relay.parse_front_matter(rtext(path))
        self.assertEqual(fm["launched_child"], st["child_session_id"])
        self.assertEqual(fm["status"], "pending")

    def test_valid_handoff_without_prior_block_launches_immediately(self):
        self.write_meter_state("sess-0001", 52.0)
        self.write_handoff()
        rc, data, out, err = self.stop()
        self.assertNotIn("decision", data)
        self.assertTrue(self.state()["launched"])

    def test_invalid_handoff_gets_specific_errors_then_mechanical(self):
        self.write_meter_state("sess-0001", 52.0)
        self.stop()
        bad = GOOD.split("## 7. Key files and commands")[0]
        path = self.write_handoff(text=bad)
        rc, data, out, err = self.stop(stop_hook_active=True)
        self.assertEqual(data["decision"], "block")
        self.assertIn("missing section: key files", data["reason"])
        self.assertIn(path, data["reason"])
        self.assertEqual(self.ledger()[-1]["event"], "handoff_invalid")
        # still invalid -> mechanical replaces nothing but a new mechanical file is written
        rc, data, out, err = self.stop(stop_hook_active=True)
        self.assertNotIn("decision", data)
        self.assertTrue(self.state()["launched"])
        self.assertIn("mechanical_handoff", [e["event"] for e in self.ledger()])

    def test_invalid_then_fixed_launches(self):
        self.write_meter_state("sess-0001", 52.0)
        self.stop()
        path = self.write_handoff(text=GOOD.replace("## 9. Memory updates", "## 9. Memory updatez"))
        rc, data, _, _ = self.stop(stop_hook_active=True)
        self.assertEqual(data["decision"], "block")
        wtext(path, rtext(path).replace("updatez", "updates"))
        rc, data, _, _ = self.stop(stop_hook_active=True)
        self.assertNotIn("decision", data)
        self.assertTrue(self.state()["launched"])

    def test_secrets_are_redacted_before_launch(self):
        self.write_meter_state("sess-0001", 52.0)
        path = self.write_handoff(text=GOOD.replace("none\n", "- API_KEY=abcdefghijklmnopqrstuvwxyz1234\n", 1))
        self.stop()
        self.assertIn("API_KEY=[REDACTED]", rtext(path))
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz1234", rtext(path))
        self.assertTrue(self.state()["launched"])

    # ---- launcher outcomes ------------------------------------------------- #
    def test_dry_run_does_not_call_launcher(self):
        self.set_config(dry_run=True)
        self.write_meter_state("sess-0001", 52.0)
        self.write_handoff()
        rc, data, out, err = self.stop()
        self.assertIn("dry run", data["systemMessage"].lower())
        args = self.launch_args()
        self.assertIn("--dry-run", args)         # the launcher is still asked to plan in dry-run mode
        self.assertEqual(self.ledger()[-1]["event"], "launch_dry_run")
        self.assertTrue(self.state()["launched"])

    def test_bypass_is_downgraded_unless_allowed(self):
        self.write_meter_state("sess-0001", 52.0)
        self.write_handoff()
        self.stop(permission_mode="bypassPermissions")
        args = self.launch_args()
        self.assertEqual(args[args.index("--permission-mode") + 1], "default")
        self.assertIn("downgraded", self.log_text())
        self.set_config(launcher={"allow_bypass_inherit": True})
        self.relay.main(["reset", "--session-id", "sess-0001", "--cwd", self.project])
        self.stop(permission_mode="bypassPermissions")
        args = self.launch_args()
        self.assertEqual(args[args.index("--permission-mode") + 1], "bypassPermissions")

    def test_permission_mode_is_inherited(self):
        self.write_meter_state("sess-0001", 52.0)
        self.write_handoff()
        self.stop(permission_mode="acceptEdits")
        args = self.launch_args()
        self.assertEqual(args[args.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(args[args.index("--max-generations") + 1], "8")
        self.assertEqual(args[args.index("--cooldown") + 1], "60")
        self.assertEqual(args[args.index("--mode") + 1], "auto")

    def test_max_generations_stops_chain_without_launch(self):
        st = self.state(); st["generation"] = 8; st["parent"] = "p"; self.relay.save_relay_state(self.paths, st)
        self.write_meter_state("sess-0001", 52.0)
        self.write_handoff()
        rc, data, out, err = self.stop()
        self.assertIn("generation limit", data["systemMessage"])
        self.assertIsNone(self.launch_args())
        self.assertEqual(self.ledger()[-1]["event"], "limit_reached")
        self.assertTrue(self.state()["launched"])     # chain stopped; no more prompts

    def test_launcher_cooldown_retries_next_stop(self):
        self.write_meter_state("sess-0001", 52.0)
        self.write_handoff()
        rc, data, out, err = self.stop(**{}) if False else (None, None, None, None)
        os.environ["FAKE_LAUNCH_RC"] = "3"
        rc, data, out, err = self.stop()
        self.assertIn("cooldown", data["systemMessage"])
        self.assertFalse(self.state()["launched"])
        self.assertEqual(self.ledger()[-1]["event"], "launch_cooldown")
        os.environ["FAKE_LAUNCH_RC"] = "0"
        rc, data, out, err = self.stop()
        self.assertTrue(self.state()["launched"])
        self.assertEqual(self.ledger()[-1]["event"], "handoff_launched")

    def test_launcher_deferred_and_failed(self):
        self.write_meter_state("sess-0001", 52.0)
        self.write_handoff()
        os.environ["FAKE_LAUNCH_RC"] = "4"
        rc, data, out, err = self.stop()
        self.assertIn("NEXT_COMMAND.txt", data["systemMessage"])
        self.assertTrue(self.state()["launched"])
        self.assertEqual(self.ledger()[-1]["event"], "launch_deferred")
        self.relay.main(["reset", "--session-id", "sess-0001", "--cwd", self.project])
        os.environ["FAKE_LAUNCH_RC"] = "9"
        rc, data, out, err = self.stop()
        self.assertIn("launcher failed", data["systemMessage"])
        self.assertFalse(self.state()["launched"])
        self.assertEqual(self.ledger()[-1]["event"], "launch_failed")

    def test_missing_launcher_is_reported_not_fatal(self):
        os.environ["CLAUDE_RELAY_LAUNCHER"] = "/nonexistent/launch.sh"
        self.write_meter_state("sess-0001", 52.0)
        self.write_handoff()
        rc, data, out, err = self.stop()
        self.assertEqual(rc, 0)
        self.assertIn("launcher missing", data["systemMessage"])
        self.assertEqual(self.ledger()[-1]["event"], "launch_failed")

    def test_generation_and_parent_propagate(self):
        st = self.state(); st["generation"] = 3; st["parent"] = "sess-gen2"; self.relay.save_relay_state(self.paths, st)
        self.write_meter_state("sess-0001", 52.0)
        rc, data, _, _ = self.stop()
        self.assertIn("parent_session_id: sess-gen2", data["reason"])
        self.assertIn("generation: 3", data["reason"])
        self.write_handoff(text=GOOD.replace("parent_session_id: none", "parent_session_id: sess-gen2").replace("generation: 0", "generation: 3"))
        self.stop()
        args = self.launch_args()
        self.assertEqual(args[args.index("--generation") + 1], "4")
        self.assertEqual(self.ledger()[-1]["generation"], 4)

    def test_internal_error_never_blocks(self):
        self.write_meter_state("sess-0001", 52.0)
        os.chmod(self.paths.handoffs, 0o000)
        try:
            rc, data, out, err = self.stop()
            self.assertEqual(rc, 0)
            self.assertEqual(out.strip(), "")
            self.assertIn("internal error", self.log_text())
        finally:
            os.chmod(self.paths.handoffs, 0o755)
