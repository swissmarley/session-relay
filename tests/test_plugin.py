"""Plugin mode: relay.py run from plugins/session-relay with CLAUDE_PLUGIN_ROOT set."""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from helpers import (FIXTURES, PLUGIN_RELAY_PY, PLUGIN_ROOT, PLUGIN_SCRIPTS, PLUGIN_STATUSLINE_SH,
                     load_relay_module, rtext, wtext)

GOOD = rtext(os.path.join(FIXTURES, "handoff_good.md"))
FAKE = os.path.join(FIXTURES, "fake_launch.sh")


def rjson(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def iso(age_seconds=0):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=age_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class PluginTestCase(unittest.TestCase):
    """A temp project and a temp HOME; relay.py loaded from the plugin's scripts/."""

    def setUp(self):
        self.relay = load_relay_module(PLUGIN_RELAY_PY, "relay_plugin")
        self.tmp = tempfile.mkdtemp(prefix="relay-plugin-test-")
        self.project = os.path.join(self.tmp, "project")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.project)
        os.makedirs(os.path.join(self.home, ".claude"))
        self._env_backup = dict(os.environ)
        for k in list(os.environ):
            if k.startswith("CLAUDE_") or k == "CLAUDECODE":
                del os.environ[k]
        os.environ.pop("USERPROFILE", None)
        os.environ["HOME"] = self.home
        os.environ["CLAUDE_PLUGIN_ROOT"] = PLUGIN_ROOT
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        os.environ["CLAUDE_RELAY_LAUNCHER"] = FAKE
        self.launch_log = os.path.join(self.tmp, "launch.args")
        os.environ["FAKE_LAUNCH_LOG"] = self.launch_log
        git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.project, check=True)
        wtext(os.path.join(self.project, "app.py"), "print('hi')\n")
        subprocess.run(git + ["add", "-A"], cwd=self.project, check=True)
        subprocess.run(git + ["commit", "-q", "-m", "init"], cwd=self.project, check=True)
        self.paths = self.relay.Paths(self.project)
        self.data = os.path.join(self.project, ".claude", "session-relay")
        self.user_dir = os.path.join(self.home, ".claude", "session-relay")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------- #
    def run_hook(self, name, payload, env=None):
        e = dict(os.environ)
        e.update(env or {})
        p = subprocess.run([sys.executable, PLUGIN_RELAY_PY, name], input=json.dumps(payload),
                           capture_output=True, text=True, env=e, cwd=self.project, timeout=30)
        out = p.stdout.strip()
        return p.returncode, (json.loads(out) if out.startswith("{") else None), p.stdout, p.stderr

    def util(self, *args, env=None):
        e = dict(os.environ)
        e.update(env or {})
        return subprocess.run([sys.executable, PLUGIN_RELAY_PY, *args], capture_output=True,
                              text=True, env=e, cwd=self.project, timeout=30)

    def payload(self, event, sid="sess-0001", **extra):
        p = {"session_id": sid, "transcript_path": os.path.join(self.tmp, f"{sid}.jsonl"),
             "cwd": self.project, "hook_event_name": event, "permission_mode": "default"}
        p.update(extra)
        return p

    def stop(self, sid="sess-0001", **extra):
        extra.setdefault("last_assistant_message", "Edited app.py.")
        extra.setdefault("stop_hook_active", False)
        return self.run_hook("stop", self.payload("Stop", sid, **extra))

    def write_json(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        wtext(path, json.dumps(data))

    def mod_meter(self, sid, pct, window=200000, age=0, **extra):
        """A reading as hooks/meter.mjs writes it: a new <epoch ms>-<random>.json file."""
        data = {"session_id": sid, "used_pct": pct, "window_size": window, "ts": iso(age),
                "source": "mod", "approx": False}
        data.update(extra)
        self._seq = getattr(self, "_seq", 0) + 1
        path = os.path.join(self.user_dir, "meter", sid,
                            "%013d-%08x.json" % (int(time.time() * 1000) + self._seq, self._seq))
        self.write_json(path, data)
        return path

    def statusline_state(self, sid, pct, window=200000, age=0):
        self.write_json(os.path.join(self.user_dir, "statusline", f"{sid}.json"),
                        {"session_id": sid, "used_pct": pct, "window_size": window, "ts": iso(age),
                         "model": "claude-opus-5-5", "approx": False})

    def user_config(self, **values):
        self.write_json(os.path.join(self.user_dir, "config.json"), values)

    def project_config(self, **values):
        self.write_json(os.path.join(self.data, "config.json"), values)

    def git_status(self):
        return subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                              cwd=self.project, capture_output=True, text=True, check=True).stdout

    def ledger(self):
        return [json.loads(l) for l in self.relay.read_text(os.path.join(self.data, "ledger.jsonl")).splitlines()
                if l.strip()]


class ModeDetectionTests(PluginTestCase):
    def test_plugin_layout_only_with_plugin_root(self):
        p = self.relay.Paths(self.project)
        self.assertTrue(p.plugin)
        self.assertEqual(p.relay, self.data)
        self.assertEqual(p.handoffs, os.path.join(self.data, "handoffs"))
        self.assertEqual(p.ledger, os.path.join(self.data, "ledger.jsonl"))
        self.assertEqual(p.lock, os.path.join(self.data, "lock"))
        self.assertEqual(p.launch_sh, os.path.join(PLUGIN_SCRIPTS, "launch.sh"))
        self.assertEqual(p.mod_meter("s/1"), os.path.join(self.user_dir, "meter", "s_1.json"))
        del os.environ["CLAUDE_PLUGIN_ROOT"]
        legacy = self.relay.Paths(self.project)
        self.assertFalse(legacy.plugin)
        self.assertEqual(legacy.handoffs, os.path.join(self.project, ".claude", "handoffs"))
        self.assertEqual(legacy.relay, os.path.join(self.project, ".claude", "relay"))
        self.assertIsNone(legacy.mod_meter("s"))

    def test_plugin_root_argument(self):
        del os.environ["CLAUDE_PLUGIN_ROOT"]
        info = json.loads(self.util("status", "--cwd", self.project).stdout)
        self.assertEqual(info["mode"], "project-copy")
        info = json.loads(self.util("status", "--cwd", self.project, "--plugin-root", PLUGIN_ROOT).stdout)
        self.assertEqual(info["mode"], "plugin")
        # an unsubstituted placeholder is ignored rather than taken as a path
        info = json.loads(self.util("status", "--plugin-root", "${CLAUDE_PLUGIN_ROOT}").stdout)
        self.assertEqual(info["mode"], "project-copy")


class ProjectFolderTests(PluginTestCase):
    def test_first_hook_creates_one_ignored_folder(self):
        self.mod_meter("sess-0001", 10.0)
        rc, data, out, err = self.run_hook("prompt", self.payload("UserPromptSubmit", prompt="hi"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(rtext(os.path.join(self.data, ".gitignore")), "*\n!config.json\n")
        for d in ("state", "handoffs", "backups", "launched", "run"):
            self.assertTrue(os.path.isdir(os.path.join(self.data, d)), d)
        self.assertEqual(os.listdir(os.path.join(self.project, ".claude")), ["session-relay"])
        self.assertFalse(os.path.exists(os.path.join(self.project, "CLAUDE.md")))
        self.assertEqual(self.git_status(), "")

    def test_git_status_clean_after_a_full_chain(self):
        self.mod_meter("sess-0001", 55.0)
        _, data, _, _ = self.stop()
        self.assertEqual(data["decision"], "block")
        path = data["reason"].split("`")[1]
        self.assertTrue(path.startswith(os.path.join(self.data, "handoffs")), path)
        wtext(path, GOOD.replace("session_id: sess-parent-1", "session_id: sess-0001"))
        _, data, _, _ = self.stop(stop_hook_active=True)
        self.assertIn("systemMessage", data)
        self.run_hook("pre-compact", self.payload("PreCompact", trigger="auto"))
        self.run_hook("session-end", self.payload("SessionEnd", reason="other"))
        self.assertEqual(self.git_status(), "")
        # a shared config.json is the one file git sees
        self.project_config(enabled=True)
        self.assertEqual(self.git_status().strip(), "?? .claude/session-relay/config.json")

    def test_existing_gitignore_is_kept(self):
        os.makedirs(self.data)
        wtext(os.path.join(self.data, ".gitignore"), "custom\n")
        self.run_hook("prompt", self.payload("UserPromptSubmit"))
        self.assertEqual(rtext(os.path.join(self.data, ".gitignore")), "custom\n")


class ConfigLayerTests(PluginTestCase):
    def cfg(self):
        return self.relay.load_config(self.relay.Paths(self.project))

    def test_defaults(self):
        cfg = self.cfg()
        self.assertEqual((cfg["soft"], cfg["hard"], cfg["activation"]), (40, 50, "always"))
        self.assertEqual(cfg["launcher"]["claude_bin"], "claude")

    def test_precedence_defaults_options_user_project(self):
        os.environ["CLAUDE_PLUGIN_OPTION_WRAP_UP_THRESHOLD"] = "30"
        os.environ["CLAUDE_PLUGIN_OPTION_HANDOFF_THRESHOLD"] = "45"
        self.assertEqual((self.cfg()["soft"], self.cfg()["hard"]), (30, 45))
        self.user_config(soft=20, launcher={"mode": "file"})
        cfg = self.cfg()
        self.assertEqual((cfg["soft"], cfg["hard"], cfg["launcher"]["mode"]), (20, 45, "file"))
        self.assertEqual(cfg["launcher"]["claude_bin"], "claude")          # deep merge
        self.project_config(soft=25, hard=35)
        self.assertEqual((self.cfg()["soft"], self.cfg()["hard"]), (25, 35))
        layers = [label for label, _, _ in self.relay.config_layers(self.relay.Paths(self.project))]
        self.assertEqual(layers, ["plugin defaults", "/config", "user", "project"])

    def test_bad_values_fall_back(self):
        os.environ["CLAUDE_PLUGIN_OPTION_ACTIVATION"] = "sometimes"
        self.user_config(hard=10, soft=20)
        cfg = self.cfg()
        self.assertEqual(cfg["activation"], "always")
        self.assertEqual(cfg["hard"], 20)                                   # hard >= soft
        os.makedirs(self.data)
        wtext(os.path.join(self.data, "config.json"), "{broken")
        self.assertEqual(self.cfg()["soft"], 20)

    def test_kill_switch(self):
        self.mod_meter("sess-0001", 99.0)
        rc, data, out, err = self.run_hook("stop", self.payload("Stop"), env={"CLAUDE_RELAY_DISABLE": "1"})
        self.assertEqual((rc, out.strip()), (0, ""))
        self.assertFalse(os.path.exists(self.data))


class ActivationTests(PluginTestCase):
    def test_opt_in_does_nothing_until_enabled(self):
        self.user_config(activation="opt-in")
        self.mod_meter("sess-0001", 99.0)
        os.makedirs(os.path.join(self.project, ".claude", "memory"))
        wtext(os.path.join(self.project, ".claude", "memory", "INDEX.md"), "# idx\n")
        for name, event in (("session-start", "SessionStart"), ("prompt", "UserPromptSubmit"),
                            ("stop", "Stop"), ("pre-compact", "PreCompact"), ("session-end", "SessionEnd")):
            rc, data, out, err = self.run_hook(name, self.payload(event, source="startup"))
            self.assertEqual((rc, out.strip()), (0, ""), name)
        self.assertFalse(os.path.exists(self.data))

        p = self.util("enable", "--cwd", self.project)
        self.assertEqual(p.returncode, 0, p.stderr)
        res = json.loads(p.stdout)
        self.assertEqual((res["enabled"], res["active"], res["activation"]), (True, True, "opt-in"))
        self.assertEqual(rjson(os.path.join(self.data, "config.json")), {"enabled": True})
        self.mod_meter("sess-0001", 99.0)       # the SessionEnd above dropped the old reading
        _, data, _, _ = self.stop()
        self.assertEqual(data["decision"], "block")

        self.assertEqual(self.util("disable", "--cwd", self.project).returncode, 0)
        self.assertEqual(self.stop(sid="sess-0002")[2].strip(), "")

    def test_opt_in_from_config_option(self):
        self.mod_meter("sess-0001", 99.0)
        rc, data, out, err = self.run_hook("stop", self.payload("Stop"),
                                           env={"CLAUDE_PLUGIN_OPTION_ACTIVATION": "opt-in"})
        self.assertEqual(out.strip(), "")
        self.project_config(enabled=True)
        rc, data, out, err = self.run_hook("stop", self.payload("Stop", stop_hook_active=False),
                                           env={"CLAUDE_PLUGIN_OPTION_ACTIVATION": "opt-in"})
        self.assertEqual(data["decision"], "block")

    def test_always_mode_project_can_switch_itself_off(self):
        self.mod_meter("sess-0001", 99.0)
        self.util("disable", "--cwd", self.project)
        self.assertEqual(self.stop()[2].strip(), "")

    def test_enable_is_plugin_only(self):
        del os.environ["CLAUDE_PLUGIN_ROOT"]
        p = self.util("enable", "--cwd", self.project)
        self.assertEqual(p.returncode, 2)

    def test_stands_down_for_a_project_copy_install(self):
        self.mod_meter("sess-0001", 99.0)
        self.write_json(os.path.join(self.project, ".claude", "settings.json"), {"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": 'python3 "${CLAUDE_PROJECT_DIR:-.}"/.claude/relay/bin/relay.py stop'}]}]}})
        rc, data, out, err = self.stop()
        self.assertEqual((rc, out.strip()), (0, ""))
        self.assertFalse(os.path.exists(self.data))
        info = json.loads(self.util("status", "--cwd", self.project).stdout)
        self.assertFalse(info["active"])
        self.assertIn("standing down", info["activation"])

    def test_stands_down_for_a_user_level_copy(self):
        self.write_json(os.path.join(self.home, ".claude", "settings.json"), {"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": 'python3 "$HOME"/.claude/relay/bin/relay.py stop'}]}]}})
        self.mod_meter("sess-0001", 99.0)
        self.assertEqual(self.stop()[2].strip(), "")


class PluginMeterTests(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.transcript = os.path.join(self.tmp, "sess-tr.jsonl")
        shutil.copy(os.path.join(FIXTURES, "transcript.jsonl"), self.transcript)   # 90k input tokens
        self.cfg = self.relay.load_config(self.paths)

    def read(self, sid="sess-tr"):
        return self.relay.read_meter(self.paths, self.cfg, {"session_id": sid, "transcript_path": self.transcript})

    def test_mod_reading_wins(self):
        self.mod_meter("sess-tr", 9.0, window=1000000)
        self.statusline_state("sess-tr", 33.0)
        m = self.read()
        self.assertEqual((m["source"], m["used_pct"], m["window_size"], m["approx"]), ("mod", 9.0, 1000000, False))

    def test_statusline_next_then_transcript(self):
        self.statusline_state("sess-tr", 33.0)
        self.assertEqual(self.read()["source"], "statusline")
        self.statusline_state("sess-tr", 33.0, age=999)
        m = self.read()
        self.assertEqual((m["source"], m["used_pct"]), ("transcript", 45.0))     # 90k / 200k guess

    def test_mod_window_fixes_the_transcript_estimate(self):
        # Right after /compact the mod knows the window but not the percentage yet.
        self.mod_meter("sess-tr", None, window=1000000)
        m = self.read()
        self.assertEqual((m["source"], m["used_pct"], m["window_size"]), ("transcript", 9.0, 1000000))

    def test_stale_mod_reading(self):
        self.mod_meter("sess-tr", 60.0, age=999)
        self.assertEqual(self.read()["source"], "transcript")
        m = self.relay.read_meter(self.paths, self.cfg, {"session_id": "sess-tr", "transcript_path": "/nope"})
        self.assertEqual((m["source"], m["used_pct"]), ("stale-mod", 60.0))

    def test_compaction_marks_the_mod_reading_stale(self):
        self.mod_meter("sess-tr", 60.0)
        self.run_hook("session-start", self.payload("SessionStart", "sess-tr", source="compact"))
        (name,) = os.listdir(os.path.join(self.user_dir, "meter", "sess-tr"))
        st = rjson(os.path.join(self.user_dir, "meter", "sess-tr", name))
        self.assertTrue(st["stale"])

    def test_stop_uses_the_mod_reading_over_the_transcript(self):
        # The transcript alone says 45 % (below 50); the mod says 52 % of a 200k window.
        self.mod_meter("sess-tr", 52.0)
        rc, data, out, err = self.run_hook("stop", self.payload("Stop", "sess-tr", transcript_path=self.transcript,
                                                                 stop_hook_active=False))
        self.assertEqual(data["decision"], "block", err)
        self.assertIn("52%", data["reason"])
        self.assertNotIn("approximate", data["reason"])
        self.assertEqual(self.ledger()[-1]["used_pct"], 52.0)

    def test_session_end_drops_the_readings_and_old_files_are_pruned(self):
        self.mod_meter("sess-tr", 10.0)
        self.statusline_state("sess-tr", 10.0)
        self.run_hook("session-end", self.payload("SessionEnd", "sess-tr", reason="other"))
        self.assertFalse(os.path.exists(os.path.join(self.user_dir, "meter", "sess-tr")))
        self.assertFalse(os.path.exists(os.path.join(self.user_dir, "statusline", "sess-tr.json")))
        old_file = self.mod_meter("old", 1.0)
        for _ in range(5):
            self.mod_meter("new", 1.0)
        old = time.time() - 8 * 24 * 3600
        os.utime(old_file, (old, old))
        self.assertEqual(self.relay.prune_meter_files(self.paths), 3)    # 1 old + 2 beyond the newest 3
        self.assertEqual([n for n in os.listdir(os.path.join(self.user_dir, "meter")) if not n.startswith(".")],
                         ["new"])
        self.assertEqual(len(os.listdir(os.path.join(self.user_dir, "meter", "new"))), 3)


class MemoryTests(PluginTestCase):
    def index(self, text="# Project memory index\n\n- [decisions.md](decisions.md) — decisions\n"):
        os.makedirs(os.path.join(self.project, ".claude", "memory"), exist_ok=True)
        wtext(os.path.join(self.project, ".claude", "memory", "INDEX.md"), text)

    def start(self, sid="sess-new", source="startup"):
        return self.run_hook("session-start", self.payload("SessionStart", sid, source=source))

    def test_no_index_no_injection_and_no_memory_folder(self):
        rc, data, out, err = self.start()
        self.assertEqual((rc, out.strip()), (0, ""), err)
        self.assertFalse(os.path.exists(os.path.join(self.project, ".claude", "memory")))

    def test_index_injected_at_start_clear_and_compact_not_resume(self):
        self.index()
        for source in ("startup", "clear", "compact"):
            _, data, _, _ = self.start(source=source)
            ctx = data["hookSpecificOutput"]["additionalContext"]
            self.assertIn("injected by Session Relay", ctx, source)
            self.assertIn("[decisions.md](decisions.md)", ctx, source)
            self.assertIn("Do not copy this index into `CLAUDE.md`", ctx)
        self.assertEqual(self.start(source="resume")[2].strip(), "")
        self.assertFalse(os.path.exists(os.path.join(self.project, "CLAUDE.md")))

    def test_handoff_injection_carries_index_once_and_plugin_rule(self):
        self.index()
        handoffs = os.path.join(self.data, "handoffs")
        os.makedirs(handoffs)
        wtext(os.path.join(handoffs, "20261006T120000Z_sess-parent-1.md"), GOOD)
        _, data, _, err = self.start()
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("generation 1", ctx, err)
        self.assertEqual(ctx.count("[decisions.md](decisions.md)"), 1)
        self.assertIn("Do not edit `CLAUDE.md`: Session Relay injects the memory index", ctx)
        self.assertIn("create the folder or a file only when there is something to write", ctx)

    def test_pointer_and_index_share_one_output(self):
        self.index()
        hp = os.path.join(self.data, "handoffs", "h.md")
        os.makedirs(os.path.dirname(hp))
        wtext(hp, GOOD)
        self.paths.ensure_dirs()
        self.relay.save_relay_state(self.paths, {"session_id": "sess-c", "generation": 2, "parent": "p",
                                                 "handoff_path": hp, "status": "active"})
        rc, data, out, err = self.start(sid="sess-c", source="compact")
        self.assertEqual(out.count("\n"), 1)                              # one JSON line
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("reminder (compact)", ctx)
        self.assertIn("injected by Session Relay", ctx)


class LauncherWiringTests(PluginTestCase):
    def test_launcher_gets_the_plugin_folder(self):
        self.mod_meter("sess-0001", 55.0)
        self.paths.ensure_dirs()
        wtext(os.path.join(self.data, "handoffs", self.relay.handoff_filename("sess-0001")),
              GOOD.replace("session_id: sess-parent-1", "session_id: sess-0001"))
        _, data, _, err = self.stop()
        args = rtext(self.launch_log).split("\n")
        self.assertEqual(args[args.index("--relay-dir") + 1], self.data)
        self.assertEqual(args[args.index("--cwd") + 1], self.project)
        self.assertEqual(self.ledger()[-1]["event"], "handoff_launched")

    def test_real_launcher_in_file_mode_keeps_everything_in_the_folder(self):
        del os.environ["CLAUDE_RELAY_LAUNCHER"]
        self.user_config(cooldown_seconds=0, launcher={"mode": "file"})
        self.mod_meter("sess-0001", 55.0)
        self.paths.ensure_dirs()
        wtext(os.path.join(self.data, "handoffs", self.relay.handoff_filename("sess-0001")),
              GOOD.replace("session_id: sess-parent-1", "session_id: sess-0001"))
        _, data, _, err = self.stop()
        self.assertIn("NEXT_COMMAND.txt", data["systemMessage"], err)
        self.assertTrue(os.path.isfile(os.path.join(self.data, "NEXT_COMMAND.txt")))
        self.assertEqual(len(os.listdir(os.path.join(self.data, "run"))), 1)
        self.assertEqual(sorted(os.listdir(os.path.join(self.project, ".claude"))), ["session-relay"])
        self.assertEqual(self.git_status(), "")


class StatusLineInstallTests(PluginTestCase):
    def settings(self):
        return rjson(os.path.join(self.home, ".claude", "settings.json"))

    def test_install_conflict_force_remove(self):
        sp = os.path.join(self.home, ".claude", "settings.json")
        wtext(sp, json.dumps({"model": "opus[1m]"}))
        p = self.util("install-statusline")
        self.assertEqual(p.returncode, 0, p.stderr)
        res = json.loads(p.stdout)
        self.assertEqual(res["status"], "installed")
        self.assertTrue(os.path.isfile(res["backup"]))
        script = os.path.join(self.user_dir, "statusline.sh")
        self.assertEqual(rtext(script), rtext(PLUGIN_STATUSLINE_SH))
        self.assertTrue(os.access(script, os.X_OK))
        data = self.settings()
        self.assertEqual(data["model"], "opus[1m]")
        self.assertEqual(data["statusLine"]["command"],
                         'bash "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/session-relay/statusline.sh" --plugin')
        self.assertEqual(list(data), ["model", "statusLine"])                 # key order kept
        self.assertEqual(json.loads(self.util("install-statusline").stdout)["status"], "unchanged")
        info = json.loads(self.util("status").stdout)
        self.assertEqual(info["statusline"], "installed")

        data["statusLine"] = {"type": "command", "command": "~/mine.sh"}
        wtext(sp, json.dumps(data))
        p = self.util("install-statusline")
        self.assertEqual(p.returncode, 3)
        self.assertEqual(json.loads(p.stdout)["existing"], "~/mine.sh")
        self.assertEqual(self.settings()["statusLine"]["command"], "~/mine.sh")
        p = self.util("install-statusline", "--force")
        self.assertEqual(json.loads(p.stdout)["replaced"], "~/mine.sh")

        p = self.util("install-statusline", "--remove")
        self.assertEqual(json.loads(p.stdout)["status"], "removed")
        self.assertEqual(self.settings()["statusLine"]["command"], "~/mine.sh")   # restored
        self.assertFalse(os.path.exists(script))
        self.assertFalse(os.path.exists(os.path.join(self.user_dir, "statusline.replaced.json")))
        self.util("install-statusline", "--force")
        self.util("install-statusline", "--remove")
        self.assertEqual(self.settings()["statusLine"]["command"], "~/mine.sh")
        self.util("install-statusline", "--remove")                       # not ours: left alone
        self.assertEqual(self.settings()["statusLine"]["command"], "~/mine.sh")

    def test_installed_copy_is_refreshed_when_the_plugin_version_changes(self):
        self.util("install-statusline")
        script = os.path.join(self.user_dir, "statusline.sh")
        stamp = os.path.join(self.user_dir, "statusline.version")
        self.assertEqual(rtext(stamp).strip(), self.relay.RELAY_VERSION)
        wtext(script, "old copy\n")
        self.run_hook("session-start", self.payload("SessionStart", source="startup"))
        self.assertEqual(rtext(script), "old copy\n")                       # same version: kept
        wtext(stamp, "0.0.1\n")
        self.run_hook("session-start", self.payload("SessionStart", source="startup"))
        self.assertEqual(rtext(script), rtext(PLUGIN_STATUSLINE_SH))
        self.assertEqual(rtext(stamp).strip(), self.relay.RELAY_VERSION)

    def test_refuses_invalid_settings(self):
        sp = os.path.join(self.home, ".claude", "settings.json")
        wtext(sp, "{nope")
        p = self.util("install-statusline")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(rtext(sp), "{nope")


class PluginStatusLineScriptTests(PluginTestCase):
    def run_sl(self, fixture="statusline_full.json"):
        e = dict(os.environ)
        return subprocess.run(["bash", PLUGIN_STATUSLINE_SH, "--plugin"],
                              input=rtext(os.path.join(FIXTURES, fixture)), capture_output=True,
                              text=True, env=e, timeout=10)

    def test_writes_per_user_and_reads_layered_thresholds(self):
        p = self.run_sl()
        self.assertEqual(p.stdout.strip(), "[Opus] ctx 42% g0 ~soft")
        st = rjson(os.path.join(self.user_dir, "statusline", "sess-full.json"))
        self.assertEqual((st["used_pct"], st["window_size"]), (42.5, 200000))
        self.assertFalse(os.path.exists(os.path.join(self.project, ".claude")))
        self.write_json(os.path.join(self.home, ".claude", "settings.json"), {"pluginConfigs": {
            "session-relay@session-relay": {"options": {"wrap_up_threshold": 30, "handoff_threshold": 42}}}})
        self.assertEqual(self.run_sl().stdout.strip(), "[Opus] ctx 42% g0 !hard")
        self.user_config(soft=45, hard=60)
        self.assertEqual(self.run_sl().stdout.strip(), "[Opus] ctx 42% g0")
        self.project_config(soft=10)
        self.assertEqual(self.run_sl().stdout.strip(), "[Opus] ctx 42% g0 ~soft")
        self.write_json(os.path.join(self.data, "state", "sess-full.relay.json"), {"generation": 3})
        self.assertIn(" g3 ", self.run_sl().stdout)

    def test_any_marketplace_key_and_relay_config_override(self):
        self.write_json(os.path.join(self.home, ".claude", "settings.json"), {"pluginConfigs": {
            "session-relay@my-fork": {"options": {"wrap_up_threshold": 30, "handoff_threshold": 42}}}})
        self.assertEqual(self.run_sl().stdout.strip(), "[Opus] ctx 42% g0 !hard")
        override = os.path.join(self.tmp, "override.json")
        self.write_json(override, {"soft": 80, "hard": 90})
        os.environ["CLAUDE_RELAY_CONFIG"] = override
        self.assertEqual(self.run_sl().stdout.strip(), "[Opus] ctx 42% g0")

    def test_relay_reads_what_the_status_line_wrote(self):
        self.run_sl()
        m = self.relay.read_meter(self.paths, self.relay.load_config(self.paths),
                                  {"session_id": "sess-full", "transcript_path": "/nope"})
        self.assertEqual((m["source"], m["used_pct"]), ("statusline", 42.5))


class StatusCommandTests(PluginTestCase):
    def test_status_does_not_create_anything(self):
        info = json.loads(self.util("status", "--session-id", "sess-x").stdout)
        self.assertEqual(info["mode"], "plugin")
        self.assertTrue(info["active"])
        self.assertEqual(info["data_dir"], self.data)
        self.assertEqual(info["meter"]["source"], "none")
        self.assertEqual([l["layer"] for l in info["config_layers"]], ["plugin defaults", "/config", "user", "project"])
        self.assertFalse(os.path.exists(os.path.join(self.project, ".claude")))
        self.assertFalse(os.path.exists(self.user_dir))


if __name__ == "__main__":
    unittest.main()


class TornMeterFileTests(PluginTestCase):
    """The mod's $.fs.write is not atomic: a reader can meet a half-written file."""

    def setUp(self):
        super().setUp()
        self.transcript = os.path.join(self.tmp, "sess-tr.jsonl")
        shutil.copy(os.path.join(FIXTURES, "transcript.jsonl"), self.transcript)   # 90k input tokens
        self.cfg = self.relay.load_config(self.paths)

    def read(self):
        return self.relay.read_meter(self.paths, self.cfg, {"session_id": "sess-tr", "transcript_path": self.transcript})

    def newest(self, text):
        d = os.path.join(self.user_dir, "meter", "sess-tr")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "9999999999999-ffffffff.json")
        wtext(path, text)
        return path

    def stop(self):
        return self.run_hook("stop", self.payload("Stop", "sess-tr", transcript_path=self.transcript,
                                                  stop_hook_active=False))

    def test_intact_reading_of_a_1m_session(self):
        self.mod_meter("sess-tr", 9.0, window=1000000)
        m = self.read()
        self.assertEqual((m["source"], m["used_pct"]), ("mod", 9.0))

    def test_torn_newest_file_falls_back_to_the_previous_reading(self):
        self.mod_meter("sess-tr", 9.0, window=1000000)
        self.newest('{"session_id": "sess-tr", "used_pct": 9')
        m = self.read()
        self.assertEqual((m["source"], m["used_pct"], m["window_size"]), ("mod", 9.0, 1000000))

    def test_torn_file_is_retried(self):
        path = self.newest("")
        calls = []
        real_sleep = self.relay.time.sleep

        def sleep(sec):
            calls.append(sec)
            if len(calls) == 1:   # the writer finishes while the reader waits
                wtext(path, json.dumps({"session_id": "sess-tr", "used_pct": 9.0, "window_size": 1000000,
                                        "ts": iso(), "source": "mod"}))
        self.relay.time.sleep = sleep
        try:
            m = self.read()
        finally:
            self.relay.time.sleep = real_sleep
        self.assertEqual(calls, [0.025])
        self.assertEqual((m["source"], m["used_pct"]), ("mod", 9.0))

    def test_only_a_torn_file_never_triggers_a_handoff_from_a_guessed_window(self):
        # The repro: an intact file says 9 % of 1M; the transcript alone reads 45 % of a
        # guessed 200k window, which is over the 40 % handoff threshold used here.
        self.user_config(soft=30, hard=40)
        self.newest('{"session_id": "sess-tr", "used_')
        m = self.relay.read_meter(self.paths, self.relay.load_config(self.paths),
                                  {"session_id": "sess-tr", "transcript_path": self.transcript})
        self.assertEqual((m["source"], m["used_pct"]), ("transcript", 45.0))
        self.assertIn("unreliable", m)
        rc, data, out, err = self.stop()
        self.assertEqual((rc, out.strip()), (0, ""), err)
        self.assertIn("handoff skipped this turn", rtext(os.path.join(self.data, "relay.log")))
        rc, data, out, err = self.run_hook("prompt", self.payload("UserPromptSubmit", "sess-tr",
                                                                  transcript_path=self.transcript))
        self.assertEqual(out.strip(), "")

    def test_empty_file_behaves_like_a_torn_one(self):
        self.user_config(soft=30, hard=40)
        self.newest("")
        self.assertIn("unreliable", self.read())
        self.assertEqual(self.stop()[2].strip(), "")

    def test_torn_file_with_a_known_window_uses_it(self):
        self.user_config(soft=30, hard=40)
        self.newest("{")
        self.statusline_state("sess-tr", 9.0, window=1000000, age=999)    # stale, but knows the window
        m = self.read()
        self.assertEqual((m["source"], m["used_pct"], m["window_size"]), ("transcript", 9.0, 1000000))
        self.assertNotIn("unreliable", m)

    def test_missing_file_keeps_the_old_fallback(self):
        # No mod at all (Claude Code < 2.1.287): the transcript estimate acts as before.
        self.user_config(soft=30, hard=40)
        m = self.read()
        self.assertEqual((m["source"], m["used_pct"]), ("transcript", 45.0))
        self.assertNotIn("unreliable", m)
        self.assertEqual(self.stop()[1]["decision"], "block")

    def test_single_file_format_is_still_read(self):
        self.write_json(os.path.join(self.user_dir, "meter", "sess-tr.json"),
                        {"session_id": "sess-tr", "used_pct": 9.0, "window_size": 1000000, "ts": iso()})
        self.assertEqual(self.read()["source"], "mod")


class MeterHousekeepingTests(PluginTestCase):
    """The mod writes in every project; cleanup must not depend on the relay being active."""

    def end(self, sid="sess-x", env=None):
        return self.run_hook("session-end", self.payload("SessionEnd", sid, reason="other"), env=env)

    def test_session_end_cleans_up_in_inactive_projects(self):
        meter = os.path.join(self.user_dir, "meter", "sess-x")
        self.user_config(activation="opt-in")                           # not enabled here
        self.mod_meter("sess-x", 5.0)
        self.end()
        self.assertFalse(os.path.exists(meter))
        self.assertFalse(os.path.exists(self.data))
        self.write_json(os.path.join(self.user_dir, "config.json"), {})
        self.project_config(enabled=False)                              # switched off here
        self.mod_meter("sess-x", 5.0)
        self.end()
        self.assertFalse(os.path.exists(meter))
        os.remove(os.path.join(self.data, "config.json"))
        self.write_json(os.path.join(self.project, ".claude", "settings.json"),
                        {"hooks": {"Stop": [{"hooks": [{"command": "python3 .claude/relay/bin/relay.py stop"}]}]}})
        self.mod_meter("sess-x", 5.0)                                   # project-copy wired
        self.end()
        self.assertFalse(os.path.exists(meter))

    def test_prune_runs_from_any_hook_at_most_hourly(self):
        self.user_config(activation="opt-in")
        stale = self.mod_meter("gone", 1.0)
        old = time.time() - 8 * 24 * 3600
        os.utime(stale, (old, old))
        self.run_hook("prompt", self.payload("UserPromptSubmit", "sess-y"))
        self.assertFalse(os.path.exists(os.path.join(self.user_dir, "meter", "gone")))
        stamp = os.path.join(self.user_dir, "meter", ".last-prune")
        self.assertTrue(os.path.isfile(stamp))
        stale = self.mod_meter("gone2", 1.0)
        os.utime(stale, (old, old))
        self.run_hook("prompt", self.payload("UserPromptSubmit", "sess-y"))
        self.assertTrue(os.path.exists(stale))                          # rate limited
        os.utime(stamp, (old, old))
        self.run_hook("prompt", self.payload("UserPromptSubmit", "sess-y"))
        self.assertFalse(os.path.exists(stale))

    def test_no_meter_folder_is_created_when_the_mod_never_wrote(self):
        self.run_hook("prompt", self.payload("UserPromptSubmit"))
        self.assertFalse(os.path.exists(os.path.join(self.user_dir, "meter")))


class ConfigDirTests(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.cfg_dir = os.path.join(self.tmp, "custom-claude")
        os.makedirs(self.cfg_dir)
        os.environ["CLAUDE_CONFIG_DIR"] = self.cfg_dir
        self.user_dir = os.path.join(self.cfg_dir, "session-relay")

    def test_user_files_follow_claude_config_dir(self):
        paths = self.relay.Paths(self.project)
        self.assertEqual(paths.user_dir, self.user_dir)
        self.user_config(soft=20, hard=33)
        self.assertEqual(self.relay.load_config(paths)["hard"], 33)
        self.mod_meter("sess-c", 61.0)
        rc, data, out, err = self.stop(sid="sess-c")
        self.assertEqual(data["decision"], "block", err)
        self.assertIn("61%", data["reason"])

    def test_statusline_install_and_legacy_check_use_it(self):
        p = self.util("install-statusline")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.cfg_dir, "settings.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.user_dir, "statusline.sh")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "settings.json")))
        # the installed command finds the copy through CLAUDE_CONFIG_DIR
        cmd = rjson(os.path.join(self.cfg_dir, "settings.json"))["statusLine"]["command"]
        out = subprocess.run(["sh", "-c", cmd], input=rtext(os.path.join(FIXTURES, "statusline_full.json")),
                             capture_output=True, text=True, env=dict(os.environ)).stdout
        self.assertEqual(out.strip(), "[Opus] ctx 42% g0 ~soft")
        self.assertTrue(os.path.isfile(os.path.join(self.user_dir, "statusline", "sess-full.json")))
        self.write_json(os.path.join(self.cfg_dir, "settings.json"), {"hooks": {"Stop": [{"hooks": [
            {"command": 'python3 "$HOME"/.claude/relay/bin/relay.py stop'}]}]}})
        self.assertIsNotNone(self.relay.legacy_install(self.relay.Paths(self.project)))
