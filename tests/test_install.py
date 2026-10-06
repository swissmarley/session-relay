import json
import os
import shutil
import subprocess

from helpers import RelayTestCase, BIN, wtext, rtext

INSTALL = os.path.join(BIN, "install.sh")
UNINSTALL = os.path.join(BIN, "uninstall.sh")


class InstallTests(RelayTestCase):
    def setUp(self):
        super().setUp()
        # a second, clean project that does not yet contain the relay files
        self.target = os.path.join(self.tmp, "target")
        os.makedirs(self.target)
        subprocess.run(["git", "init", "-q"], cwd=self.target, check=True)
        self.fake_home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.fake_home, ".claude"))

    def sh(self, script, *args, home=None):
        env = dict(os.environ)
        env["HOME"] = home or self.fake_home
        return subprocess.run(["sh", script, *args], capture_output=True, text=True, env=env)

    def settings(self, path):
        with open(path) as fh:
            return json.load(fh)

    def relay_cmds(self, data):
        out = []
        for ev, entries in data.get("hooks", {}).items():
            for e in entries:
                for h in e.get("hooks", []):
                    if "/relay/bin/" in h.get("command", ""):
                        out.append((ev, h["command"], h.get("timeout")))
        return sorted(out)

    # ---- project install -------------------------------------------------- #
    def test_project_install_merges_non_destructively(self):
        sp = os.path.join(self.target, ".claude", "settings.json")
        os.makedirs(os.path.dirname(sp))
        wtext(sp, json.dumps({
            "model": "opus", "permissions": {"allow": ["Bash(git *)"]},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}],
                      "PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "prettier"}]}]},
        }, indent=2))
        wtext(os.path.join(self.target, "CLAUDE.md"), "# My project\n\nRules here.\n")
        p = self.sh(INSTALL, "--project-dir", self.target)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        data = self.settings(sp)
        # untouched keys
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["permissions"], {"allow": ["Bash(git *)"]})
        self.assertEqual(data["hooks"]["PostToolUse"][0]["hooks"][0]["command"], "prettier")
        self.assertEqual(data["hooks"]["Stop"][0]["hooks"][0]["command"], "echo mine")
        # relay entries
        cmds = self.relay_cmds(data)
        self.assertEqual([c[0] for c in cmds], ["PreCompact", "SessionEnd", "SessionStart", "Stop", "UserPromptSubmit"])
        stop = [c for c in cmds if c[0] == "Stop"][0]
        self.assertEqual(stop[1], 'python3 "${CLAUDE_PROJECT_DIR:-.}"/.claude/relay/bin/relay.py stop')
        self.assertEqual(stop[2], 120)
        self.assertEqual(data["statusLine"]["command"], 'bash "${CLAUDE_PROJECT_DIR:-.}"/.claude/relay/bin/statusline.sh')
        # files copied, config present, memory seeded, CLAUDE.md pointer, gitignore, backup
        for f in ("relay.py", "statusline.sh", "launch.sh", "install.sh", "uninstall.sh"):
            self.assertTrue(os.path.isfile(os.path.join(self.target, ".claude", "relay", "bin", f)), f)
        self.assertTrue(os.path.isfile(os.path.join(self.target, ".claude", "relay", "config.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.target, ".claude", "relay", "handoff-template.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.target, ".claude", "memory", "INDEX.md")))
        self.assertEqual(rtext(os.path.join(self.target, "CLAUDE.md")), "# My project\n\nRules here.\n\n@.claude/memory/INDEX.md\n")
        self.assertIn(".claude/state/", rtext(os.path.join(self.target, ".gitignore")))
        backups = [f for f in os.listdir(os.path.dirname(sp)) if f.startswith("settings.json.bak.")]
        self.assertEqual(len(backups), 1)
        # the merged file is still valid JSON the hook runner can read, and relay.py runs from the copy
        q = subprocess.run(["python3", os.path.join(self.target, ".claude", "relay", "bin", "relay.py"), "status", "--cwd", self.target],
                           capture_output=True, text=True)
        self.assertEqual(q.returncode, 0, q.stderr)

    def test_install_is_idempotent(self):
        self.sh(INSTALL, "--project-dir", self.target)
        sp = os.path.join(self.target, ".claude", "settings.json")
        before = rtext(sp)
        p = self.sh(INSTALL, "--project-dir", self.target)
        self.assertEqual(p.returncode, 0)
        self.assertIn("already installed", p.stdout)
        self.assertEqual(rtext(sp), before)
        self.assertEqual(rtext(os.path.join(self.target, "CLAUDE.md")).count("@.claude/memory/INDEX.md"), 1)
        self.assertEqual(rtext(os.path.join(self.target, ".gitignore")).count(".claude/state/"), 1)
        self.assertEqual(len(self.relay_cmds(self.settings(sp))), 5)

    def test_existing_config_and_statusline_are_preserved(self):
        os.makedirs(os.path.join(self.target, ".claude", "relay"))
        wtext(os.path.join(self.target, ".claude", "relay", "config.json"), '{"soft": 11, "hard": 22}\n')
        sp = os.path.join(self.target, ".claude", "settings.json")
        wtext(sp, json.dumps({"statusLine": {"type": "command", "command": "~/my-status.sh"}}))
        p = self.sh(INSTALL, "--project-dir", self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("statusLine already set", p.stderr)
        self.assertEqual(self.settings(sp)["statusLine"]["command"], "~/my-status.sh")
        self.assertEqual(json.load(open(os.path.join(self.target, ".claude", "relay", "config.json")))["soft"], 11)

    def test_invalid_settings_json_is_refused(self):
        sp = os.path.join(self.target, ".claude", "settings.json")
        os.makedirs(os.path.dirname(sp))
        wtext(sp, "{ not json")
        p = self.sh(INSTALL, "--project-dir", self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("not valid JSON", p.stderr)
        self.assertEqual(rtext(sp), "{ not json")

    def test_dry_run_changes_nothing(self):
        p = self.sh(INSTALL, "--project-dir", self.target, "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("[dry-run]", p.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.target, ".claude", "settings.json")))
        self.assertFalse(os.path.exists(os.path.join(self.target, "CLAUDE.md")))

    def test_flags_skip_claude_md_statusline_gitignore(self):
        p = self.sh(INSTALL, "--project-dir", self.target, "--no-claude-md", "--no-statusline", "--no-gitignore")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.target, "CLAUDE.md")))
        self.assertFalse(os.path.exists(os.path.join(self.target, ".gitignore")))
        self.assertNotIn("statusLine", self.settings(os.path.join(self.target, ".claude", "settings.json")))

    # ---- user install ---------------------------------------------------- #
    def test_user_install_uses_home_paths(self):
        us = os.path.join(self.fake_home, ".claude", "settings.json")
        wtext(us, json.dumps({"model": "opus[1m]"}))
        p = self.sh(INSTALL, "--user", "--project-dir", self.target)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        data = self.settings(us)
        self.assertEqual(data["model"], "opus[1m]")
        cmds = self.relay_cmds(data)
        self.assertEqual(len(cmds), 5)
        self.assertTrue(all('"$HOME"/.claude/relay/bin/relay.py' in c[1] for c in cmds), cmds)
        self.assertEqual(data["statusLine"]["command"], 'bash "$HOME"/.claude/relay/bin/statusline.sh')
        self.assertTrue(os.path.isfile(os.path.join(self.fake_home, ".claude", "relay", "bin", "relay.py")))
        self.assertTrue(os.path.isfile(os.path.join(self.fake_home, ".claude", "relay", "config.json")))
        self.assertFalse(os.path.exists(os.path.join(self.target, ".claude", "settings.json")))
        self.assertTrue(os.path.isdir(os.path.join(self.target, ".claude", "memory")))   # data stays per project
        # hooks from the user copy resolve the project from the payload cwd
        q = subprocess.run(["python3", os.path.join(self.fake_home, ".claude", "relay", "bin", "relay.py"), "status", "--cwd", self.target],
                           capture_output=True, text=True, env={**os.environ, "CLAUDE_RELAY_CONFIG": ""})
        info = json.loads(q.stdout)
        self.assertEqual(info["project"], self.target)
        self.assertEqual(info["relay_home"], os.path.join(self.fake_home, ".claude", "relay"))

    # ---- uninstall --------------------------------------------------------- #
    def test_uninstall_removes_only_relay_entries(self):
        sp = os.path.join(self.target, ".claude", "settings.json")
        os.makedirs(os.path.dirname(sp))
        wtext(sp, json.dumps({"model": "opus",
                              "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}))
        wtext(os.path.join(self.target, "CLAUDE.md"), "# My project\n")
        self.sh(INSTALL, "--project-dir", self.target)
        self.assertEqual(len(self.relay_cmds(self.settings(sp))), 5)
        # runtime data to purge later
        os.makedirs(os.path.join(self.target, ".claude", "state"), exist_ok=True)
        wtext(os.path.join(self.target, ".claude", "state", "x.json"), "{}")
        wtext(os.path.join(self.target, ".claude", "memory", "decisions.md"), "# keep\n- important\n")
        p = self.sh(UNINSTALL, "--project-dir", self.target)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        data = self.settings(sp)
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["hooks"], {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]})
        self.assertNotIn("statusLine", data)
        self.assertEqual(rtext(os.path.join(self.target, "CLAUDE.md")), "# My project\n\n")
        self.assertTrue(os.path.exists(os.path.join(self.target, ".claude", "state", "x.json")))  # no purge
        p = self.sh(UNINSTALL, "--project-dir", self.target, "--purge")
        self.assertEqual(p.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.target, ".claude", "state")))
        self.assertEqual(rtext(os.path.join(self.target, ".claude", "memory", "decisions.md")), "# keep\n- important\n")
        # a second uninstall is a no-op
        p = self.sh(UNINSTALL, "--project-dir", self.target)
        self.assertIn("no relay entries found", p.stdout)

    def test_user_uninstall(self):
        self.sh(INSTALL, "--user", "--project-dir", self.target)
        us = os.path.join(self.fake_home, ".claude", "settings.json")
        p = self.sh(UNINSTALL, "--user", "--project-dir", self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.settings(us), {})
        self.assertFalse(os.path.exists(os.path.join(self.fake_home, ".claude", "relay")))
