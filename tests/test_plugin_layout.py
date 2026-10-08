"""The plugin's manifests and wiring, and the project-copy scripts kept in sync with it."""
import filecmp
import json
import os
import re
import unittest

from helpers import BIN, PLUGIN_ROOT, PLUGIN_SCRIPTS, RELAY_HOME, ROOT, load_relay_module


def rjson(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


HOOKS = {
    "SessionStart": "session-start",
    "UserPromptSubmit": "prompt",
    "Stop": "stop",
    "PreCompact": "pre-compact",
    "SessionEnd": "session-end",
}


class PluginLayoutTests(unittest.TestCase):
    def test_marketplace(self):
        m = rjson(os.path.join(ROOT, ".claude-plugin", "marketplace.json"))
        self.assertEqual(m["name"], "session-relay")
        self.assertEqual([(p["name"], p["source"]) for p in m["plugins"]],
                         [("session-relay", "./plugins/session-relay")])
        self.assertNotIn("version", m["plugins"][0])          # plugin.json owns the version

    def test_manifest_version_and_options(self):
        p = rjson(os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json"))
        self.assertEqual((p["name"], p["version"]), ("session-relay", "0.2.1"))
        relay = load_relay_module(os.path.join(PLUGIN_SCRIPTS, "relay.py"), "relay_layout")
        self.assertEqual(relay.RELAY_VERSION, p["version"])
        opts = p["userConfig"]
        defaults = rjson(os.path.join(PLUGIN_SCRIPTS, "config.json"))
        self.assertEqual(opts["wrap_up_threshold"]["default"], defaults["soft"])
        self.assertEqual(opts["handoff_threshold"]["default"], defaults["hard"])
        self.assertEqual(opts["activation"]["options"], list(relay.ACTIVATION_MODES))
        self.assertEqual(opts["activation"]["default"], defaults["activation"])
        # every option reaches relay.py as CLAUDE_PLUGIN_OPTION_<KEY>
        self.assertEqual(sorted(k.upper() for k in opts), sorted(relay.PLUGIN_OPTIONS))

    def test_hooks_and_module(self):
        h = rjson(os.path.join(PLUGIN_ROOT, "hooks", "hooks.json"))
        self.assertEqual(h["modules"], ["./meter.mjs"])
        self.assertTrue(os.path.isfile(os.path.join(PLUGIN_ROOT, "hooks", "meter.mjs")))
        self.assertEqual(set(h["hooks"]), set(HOOKS))
        for event, sub in HOOKS.items():
            (entry,) = h["hooks"][event]
            (hook,) = entry["hooks"]
            # exec form: no shell, so the plugin path needs no quoting
            self.assertEqual((hook["command"], hook["args"]),
                             ("python3", ["${CLAUDE_PLUGIN_ROOT}/scripts/relay.py", sub]))

    def test_license(self):
        p = rjson(os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json"))
        self.assertEqual(p["license"], "MIT")
        self.assertTrue(filecmp.cmp(os.path.join(ROOT, "LICENSE"), os.path.join(PLUGIN_ROOT, "LICENSE"),
                                    shallow=False), "plugin LICENSE differs: run tools/sync-legacy.sh")

    def test_no_tests_in_the_package(self):
        for dirpath, _, files in os.walk(PLUGIN_ROOT):
            for f in files:
                self.assertFalse(f.endswith((".test.ts", ".test.tsx")), os.path.join(dirpath, f))

    def test_skills(self):
        names = sorted(os.listdir(os.path.join(PLUGIN_ROOT, "skills")))
        self.assertEqual(names, ["enable", "status", "statusline"])
        for name in names:
            with open(os.path.join(PLUGIN_ROOT, "skills", name, "SKILL.md"), encoding="utf-8") as fh:
                text = fh.read()
            self.assertTrue(text.startswith("---\ndescription: "), name)
            # every command a skill runs is plugin mode, whatever the Bash tool's environment
            for cmd in re.findall(r'python3 "\$\{CLAUDE_PLUGIN_ROOT\}/scripts/relay.py" [^\n`]*', text):
                if "*" not in cmd:
                    self.assertIn('--plugin-root "${CLAUDE_PLUGIN_ROOT}"', cmd, name)

    def test_project_copy_matches_plugin_scripts(self):
        for f in ("relay.py", "launch.sh", "statusline.sh"):
            self.assertTrue(filecmp.cmp(os.path.join(PLUGIN_SCRIPTS, f), os.path.join(BIN, f), shallow=False),
                            f"{f} differs: run tools/sync-legacy.sh")
        self.assertTrue(filecmp.cmp(os.path.join(PLUGIN_ROOT, "hooks", "meter.mjs"),
                                    os.path.join(ROOT, "tests", "mod", "hooks", "meter.mjs"), shallow=False),
                        "tests/mod/hooks/meter.mjs differs: run tools/sync-legacy.sh")
        self.assertTrue(filecmp.cmp(os.path.join(PLUGIN_SCRIPTS, "handoff-template.md"),
                                    os.path.join(RELAY_HOME, "handoff-template.md"), shallow=False))
        plugin_cfg = rjson(os.path.join(PLUGIN_SCRIPTS, "config.json"))
        self.assertEqual(plugin_cfg.pop("activation"), "always")
        self.assertEqual(plugin_cfg, rjson(os.path.join(RELAY_HOME, "config.json")))


if __name__ == "__main__":
    unittest.main()
