import json
import os
import shutil
import statistics
import subprocess
import time

from helpers import RelayTestCase, FIXTURES, STATUSLINE_SH


class StatusLineScriptTests(RelayTestCase):
    def run_sl(self, fixture, env=None, stdin=None):
        e = dict(os.environ)
        e["CLAUDE_PROJECT_DIR"] = self.project
        if env:
            e.update(env)
        data = stdin if stdin is not None else open(os.path.join(FIXTURES, fixture)).read()
        t0 = time.perf_counter()
        p = subprocess.run(["/bin/bash", STATUSLINE_SH], input=data, capture_output=True,
                           text=True, env=e, timeout=10)
        return p, (time.perf_counter() - t0) * 1000

    def state(self, sid):
        return json.load(open(self.paths.meter_state(sid)))

    def test_full_payload_writes_state_and_prints_line(self):
        p, ms = self.run_sl("statusline_full.json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "[Opus] ctx 42% g0 ~soft")
        st = self.state("sess-full")
        self.assertEqual(st["used_pct"], 42.5)
        self.assertEqual(st["window_size"], 200000)
        self.assertEqual(st["model"], "claude-opus-5-5")
        self.assertFalse(st["approx"])
        self.assertTrue(st["ts"].endswith("Z"))
        self.assertFalse([f for f in os.listdir(self.paths.state) if f.startswith(".tmp-")])

    def test_null_pct_is_derived_from_totals(self):
        p, _ = self.run_sl("statusline_null_pct.json")
        self.assertEqual(p.stdout.strip(), "[Opus] ctx 25% g0")
        self.assertEqual(self.state("sess-nullpct")["used_pct"], 25.0)

    def test_empty_context_prints_unknown_and_null_pct(self):
        p, _ = self.run_sl("statusline_empty.json")
        self.assertEqual(p.stdout.strip(), "[Fable] ctx ?% g0")
        st = self.state("sess-empty")
        self.assertIsNone(st["used_pct"])
        self.assertEqual(st["window_size"], 1000000)

    def test_hard_marker_and_generation_from_relay_state(self):
        self.relay.write_json(self.paths.relay_state("sess-1m"),
                              {"session_id": "sess-1m", "generation": 3})
        p, _ = self.run_sl("statusline_1m.json")
        self.assertEqual(p.stdout.strip(), "[Opus] ctx 52% g3 !hard")

    def test_thresholds_come_from_config(self):
        self.set_config(soft=10, hard=90)
        p, _ = self.run_sl("statusline_full.json")
        self.assertIn("~soft", p.stdout)
        self.set_config(soft=80, hard=90)
        p, _ = self.run_sl("statusline_full.json")
        self.assertEqual(p.stdout.strip(), "[Opus] ctx 42% g0")

    def test_kill_switch_marker(self):
        p, _ = self.run_sl("statusline_full.json", env={"CLAUDE_RELAY_DISABLE": "1"})
        self.assertIn("relay off", p.stdout)

    def test_never_fails_loudly(self):
        p, _ = self.run_sl("statusline_full.json", stdin="")
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "[relay] no status data"))
        p, _ = self.run_sl("statusline_full.json", stdin="{not json")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "[relay] unreadable status data")
        # a PATH with every tool the script needs except jq (macOS ships /usr/bin/jq)
        nojq = os.path.join(self.tmp, "nojq-bin")
        os.makedirs(nojq)
        for tool in ("cat", "awk", "date", "mktemp", "mkdir", "mv", "rm", "printf"):
            src = shutil.which(tool)
            if src:
                os.symlink(src, os.path.join(nojq, tool))
        p, _ = self.run_sl("statusline_full.json", env={"PATH": nojq})
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "[relay] jq not found"))

    def test_speed_budget_50ms(self):
        samples = []
        for _ in range(15):
            _, ms = self.run_sl("statusline_full.json")
            samples.append(ms)
        med = statistics.median(samples)
        print(f"\n  statusline median {med:.1f} ms, max {max(samples):.1f} ms")
        self.assertLess(med, 50.0)


class MeterFallbackTests(RelayTestCase):
    def setUp(self):
        super().setUp()
        self.transcript = os.path.join(self.tmp, "sess-tr.jsonl")
        shutil.copy(os.path.join(FIXTURES, "transcript.jsonl"), self.transcript)
        self.cfg = self.relay.load_config(self.paths)

    def pl(self, sid="sess-tr"):
        return {"session_id": sid, "transcript_path": self.transcript, "cwd": self.project}

    def test_transcript_usage_picks_latest_main_thread_line(self):
        u = self.relay.transcript_usage(self.transcript)
        self.assertEqual(u["input_total"], 90000)
        self.assertEqual(u["model"], "claude-opus-5-5")
        self.assertEqual(u["request_id"], "req_2")

    def test_transcript_tail_handles_partial_first_line(self):
        # tiny tail forces the "drop partial first line" path and the whole-file rescan
        u = self.relay.transcript_usage(self.transcript, tail_bytes=40)
        self.assertEqual(u["input_total"], 90000)

    def test_fresh_state_wins(self):
        self.write_meter_state("sess-tr", 33.3, window_size=200000)
        m = self.relay.read_meter(self.paths, self.cfg, self.pl())
        self.assertEqual((m["source"], m["used_pct"], m["approx"]), ("statusline", 33.3, False))

    def test_stale_state_prefers_transcript_and_marks_approx(self):
        self.write_meter_state("sess-tr", 33.3, window_size=200000, age_seconds=500)
        m = self.relay.read_meter(self.paths, self.cfg, self.pl())
        self.assertEqual(m["source"], "transcript")
        self.assertTrue(m["approx"])
        self.assertEqual(m["used_pct"], 45.0)          # 90000 / 200000 (window from state hint)
        self.assertEqual(m["window_size"], 200000)

    def test_null_pct_state_falls_back(self):
        self.write_meter_state("sess-tr", None, window_size=1000000)
        m = self.relay.read_meter(self.paths, self.cfg, self.pl())
        self.assertEqual(m["source"], "transcript")
        self.assertEqual(m["used_pct"], 9.0)           # 90000 / 1M via window hint

    def test_stale_flag_forces_fallback(self):
        self.write_meter_state("sess-tr", 60.0, window_size=200000)
        self.relay.mark_meter_stale(self.paths, "sess-tr")
        m = self.relay.read_meter(self.paths, self.cfg, self.pl())
        self.assertEqual(m["source"], "transcript")

    def test_window_inference_without_hint(self):
        self.assertEqual(self.relay.infer_window("claude-opus-5-5", 90000), 200000)
        self.assertEqual(self.relay.infer_window("claude-opus-5-5", 400000), 1000000)
        self.assertEqual(self.relay.infer_window("claude-sonnet-5-5", 1000), 1000000)
        self.assertEqual(self.relay.infer_window("opus[1m]", 1000), 1000000)
        m = self.relay.read_meter(self.paths, self.cfg, self.pl())   # no state file at all
        self.assertEqual((m["source"], m["used_pct"], m["window_size"]), ("transcript", 45.0, 200000))

    def test_no_state_no_transcript(self):
        m = self.relay.read_meter(self.paths, self.cfg, {"session_id": "nope", "transcript_path": "/nonexistent"})
        self.assertEqual((m["source"], m["used_pct"]), ("none", None))

    def test_stale_state_used_as_last_resort(self):
        self.write_meter_state("sess-x", 70.0, age_seconds=999)
        m = self.relay.read_meter(self.paths, self.cfg, {"session_id": "sess-x", "transcript_path": "/nonexistent"})
        self.assertEqual((m["source"], m["used_pct"], m["approx"]), ("stale-statusline", 70.0, True))

    def test_meter_cli(self):
        out = subprocess.run(["python3", self.relay.__file__, "meter", "--session-id", "sess-tr",
                              "--transcript", self.transcript, "--cwd", self.project],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out)["used_pct"], 45.0)
