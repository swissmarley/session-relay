import os
import shutil
import stat
import subprocess
import time

from helpers import RelayTestCase, FIXTURES, LAUNCH_SH, wtext, rtext

GOOD = rtext(os.path.join(FIXTURES, "handoff_good.md"))


class LauncherTests(RelayTestCase):
    def setUp(self):
        super().setUp()
        self.handoff = os.path.join(self.paths.handoffs, "20261006T120000Z_sess-0001.md")
        wtext(self.handoff, GOOD)
        self.shim_dir = os.path.join(self.tmp, "shims")
        os.makedirs(self.shim_dir)
        self.calls = os.path.join(self.tmp, "calls.log")
        self.launched_dir = os.path.join(self.project, ".claude", "relay", "launched")
        self.run_dir = os.path.join(self.project, ".claude", "relay", "run")

    def shim(self, name, body):
        p = os.path.join(self.shim_dir, name)
        wtext(p, "#!/bin/sh\n" + body)
        os.chmod(p, 0o755)
        return p

    def launch(self, *extra, env=None, mode="file", generation="1", timeout=30):
        args = ["sh", LAUNCH_SH, "--cwd", self.project, "--handoff", self.handoff,
                "--parent", "sess-0001", "--child-id", "11111111-2222-3333-4444-555555555555",
                "--generation", generation, "--permission-mode", "acceptEdits",
                "--mode", mode, "--lock-wait", "1", "--cooldown", "0", *extra]
        e = dict(os.environ)
        e["PATH"] = self.shim_dir + ":" + os.environ.get("PATH", "")
        e.pop("TMUX", None); e.pop("TMUX_PANE", None)
        e["CLAUDE_RELAY_LOG"] = self.paths.log
        if env:
            e.update(env)
        return subprocess.run(args, capture_output=True, text=True, env=e, timeout=timeout)

    def runner_text(self):
        files = [f for f in os.listdir(self.run_dir) if f.endswith(".sh")]
        self.assertEqual(len(files), 1, files)
        return rtext(os.path.join(self.run_dir, files[0]))

    # ---- argument validation ----------------------------------------------- #
    def test_usage_errors(self):
        p = subprocess.run(["sh", LAUNCH_SH], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        p = subprocess.run(["sh", LAUNCH_SH, "--cwd", self.project, "--handoff", "/nope", "--parent", "x"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        p = subprocess.run(["sh", LAUNCH_SH, "--bogus"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)

    # ---- file mode (deferred) ---------------------------------------------- #
    def test_file_mode_writes_runner_next_command_and_marker(self):
        p = self.launch(mode="file")
        self.assertEqual(p.returncode, 4, p.stderr)
        self.assertTrue(p.stdout.startswith("deferred "))
        next_cmd = os.path.join(self.project, ".claude", "relay", "NEXT_COMMAND.txt")
        self.assertTrue(os.path.isfile(next_cmd))
        runner = self.runner_text()
        self.assertIn("unset CLAUDECODE", runner)
        self.assertIn("CLAUDE_CODE_[A-Za-z0-9_]*", runner)
        self.assertIn(f"cd '{self.project}' || exit 1", runner)
        self.assertIn("--session-id '11111111-2222-3333-4444-555555555555'", runner)
        self.assertIn("--permission-mode 'acceptEdits'", runner)
        self.assertIn("--name 'relay-g1'", runner)
        self.assertIn("exec 'claude'", runner)
        self.assertIn(self.handoff, runner)
        self.assertIn("generation 1", runner)
        self.assertNotIn("dangerously", runner)
        marker = os.path.join(self.launched_dir, os.path.basename(self.handoff) + ".done")
        self.assertTrue(os.path.isfile(marker))
        self.assertEqual(rtext(marker).splitlines()[0], "11111111-2222-3333-4444-555555555555")
        self.assertTrue(os.path.isfile(os.path.join(self.launched_dir, ".last_launch")))
        self.assertIn("launched generation 1", self.log_text())
        # the generated runner is itself valid POSIX shell
        p = subprocess.run(["sh", "-n", os.path.join(self.run_dir, os.listdir(self.run_dir)[0])],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_runner_quotes_awkward_paths(self):
        awkward = os.path.join(self.tmp, "we ird's dir")
        shutil.copytree(self.project, awkward, symlinks=True)
        self.handoff = os.path.join(awkward, ".claude", "handoffs", "20261006T120000Z_sess-0001.md")
        p = subprocess.run(["sh", LAUNCH_SH, "--cwd", awkward, "--handoff", self.handoff, "--parent", "p",
                            "--mode", "file", "--claude-bin", "/opt/my tools/claude"],
                           capture_output=True, text=True, env={**os.environ, "PATH": self.shim_dir + ":" + os.environ["PATH"]})
        self.assertEqual(p.returncode, 4, p.stderr)
        run_dir = os.path.join(awkward, ".claude", "relay", "run")
        runner = rtext(os.path.join(run_dir, os.listdir(run_dir)[0]))
        self.assertIn("cd 'we ird'\\''s dir'".replace("we ird", os.path.join(self.tmp, "we ird")), runner)
        self.assertIn("exec '/opt/my tools/claude'", runner)
        self.assertEqual(subprocess.run(["sh", "-n", os.path.join(run_dir, os.listdir(run_dir)[0])]).returncode, 0)

    def test_relay_dir_argument(self):
        relay_dir = os.path.join(self.project, ".claude", "session-relay")
        lock = os.path.join(relay_dir, "lock")
        p = self.launch("--relay-dir", relay_dir)
        self.assertEqual(p.returncode, 4, p.stderr)
        self.assertEqual(p.stdout.strip(), "deferred " + os.path.join(relay_dir, "NEXT_COMMAND.txt"))
        self.assertEqual(len(os.listdir(os.path.join(relay_dir, "run"))), 1)
        self.assertTrue(os.path.isfile(os.path.join(relay_dir, "launched",
                                                    os.path.basename(self.handoff) + ".done")))
        self.assertFalse(os.path.exists(lock))
        self.assertEqual(os.listdir(self.run_dir), [])            # default folder untouched
        os.mkdir(lock)                                            # the lock follows --relay-dir too
        os.remove(os.path.join(relay_dir, "launched", os.path.basename(self.handoff) + ".done"))
        self.assertEqual(self.launch("--relay-dir", relay_dir).returncode, 5)

    def test_child_id_generated_when_missing(self):
        p = subprocess.run(["sh", LAUNCH_SH, "--cwd", self.project, "--handoff", self.handoff, "--parent", "p",
                            "--mode", "file"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 4)
        files = os.listdir(self.run_dir)
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"^[0-9a-f-]{36}\.sh$")

    # ---- idempotency, limits, cooldown, lock --------------------------------- #
    def test_idempotent_per_handoff(self):
        self.assertEqual(self.launch().returncode, 4)
        p = self.launch()
        self.assertEqual(p.returncode, 0)
        self.assertTrue(p.stdout.startswith("already-launched 11111111"))
        self.assertEqual(len(os.listdir(self.run_dir)), 1)

    def test_generation_limit(self):
        self.shim("osascript", 'printf "%s\\n" "$*" >> "' + self.calls + '"\n')
        p = self.launch("--max-generations", "3", generation="4")
        self.assertEqual(p.returncode, 2)
        self.assertIn("exceeds max_generations", p.stdout)
        self.assertIn("Generation limit", rtext(self.calls))
        self.assertFalse(os.path.exists(os.path.join(self.launched_dir, ".last_launch")))
        p = self.launch("--max-generations", "3", generation="3")
        self.assertEqual(p.returncode, 4)

    def test_cooldown(self):
        wtext(os.path.join(self.launched_dir, ".last_launch"), str(int(time.time())) + "\n")
        p = self.launch("--cooldown", "60")
        self.assertEqual(p.returncode, 3)
        self.assertIn("cooldown:", p.stdout)
        wtext(os.path.join(self.launched_dir, ".last_launch"), str(int(time.time()) - 120) + "\n")
        p = self.launch("--cooldown", "60")
        self.assertEqual(p.returncode, 4)

    def test_lock_busy_and_stale_reclaim(self):
        lock = os.path.join(self.project, ".claude", "relay", "lock")
        os.mkdir(lock)
        t0 = time.time()
        p = self.launch()
        self.assertEqual(p.returncode, 5)
        self.assertIn("lock busy", p.stdout)
        self.assertLess(time.time() - t0, 10)
        old = time.time() - 1000
        os.utime(lock, (old, old))
        p = self.launch("--lock-stale", "300")
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertIn("reclaiming stale lock", self.log_text())
        self.assertFalse(os.path.exists(lock))     # released on exit

    def test_lock_mtime_with_bsd_stat(self):
        # BSD stat (macOS): -c is unknown, -f %m prints epoch seconds.
        real = shutil.which("stat")
        self.shim("stat", 'if [ "$1" = "-c" ]; then echo "stat: illegal option -- c" >&2; exit 1; fi\n'
                          'shift 2; exec ' + real + ' -c %Y "$@"\n')
        lock = os.path.join(self.project, ".claude", "relay", "lock")
        os.mkdir(lock)
        self.assertEqual(self.launch().returncode, 5)            # fresh lock: busy, not a crash
        old = time.time() - 1000
        os.utime(lock, (old, old))
        self.assertEqual(self.launch("--lock-stale", "300").returncode, 4)

    def test_lock_mtime_ignores_non_numeric_stat_output(self):
        # GNU stat -f prints file-system status: never let it reach the arithmetic.
        self.shim("stat", 'printf "  File: \\"x\\"\\n    ID: 0 Namelen: 255\\n"\n')
        os.mkdir(os.path.join(self.project, ".claude", "relay", "lock"))
        p = self.launch()
        self.assertIn(p.returncode, (4, 5), p.stderr)            # never 2 ("generation limit")
        self.assertNotIn("arithmetic", p.stderr)

    def test_child_id_falls_back_when_uuidgen_is_unusable(self):
        self.shim("uuidgen", 'echo not-a-uuid\n')
        p = subprocess.run(["sh", LAUNCH_SH, "--cwd", self.project, "--handoff", self.handoff, "--parent", "p",
                            "--mode", "file"], capture_output=True, text=True,
                           env={**os.environ, "PATH": self.shim_dir + ":" + os.environ["PATH"]})
        self.assertEqual(p.returncode, 4, p.stderr)
        self.assertRegex(os.listdir(self.run_dir)[0],
                         r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.sh$")

    def test_lock_released_on_every_exit(self):
        self.launch("--max-generations", "0", generation="1")
        self.assertFalse(os.path.exists(os.path.join(self.project, ".claude", "relay", "lock")))

    # ---- dry run ------------------------------------------------------------- #
    def test_dry_run_plans_but_does_not_launch(self):
        p = self.launch("--dry-run", mode="file")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.startswith("dry-run: would launch via file"))
        self.assertEqual(len(os.listdir(self.run_dir)), 1)       # runner written for inspection
        self.assertFalse([f for f in os.listdir(self.launched_dir) if not f.startswith(".")])
        self.assertFalse(os.path.exists(os.path.join(self.project, ".claude", "relay", "NEXT_COMMAND.txt")))

    # ---- tmux / terminal via shims ------------------------------------------ #
    def test_tmux_mode_uses_new_window(self):
        self.shim("tmux", 'printf "%s\\n" "$*" >> "' + self.calls + '"\nexit 0\n')
        p = self.launch(mode="tmux")
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "tmux"), p.stderr)
        call = rtext(self.calls).strip()
        self.assertTrue(call.startswith("new-window -n relay-g1 -c " + self.project + " sh '"), call)
        self.assertNotIn("kill-pane", call)

    def test_tmux_close_old_kills_parent_pane(self):
        self.shim("tmux", 'printf "%s\\n" "$*" >> "' + self.calls + '"\nexit 0\n')
        p = self.launch("--close-old", mode="tmux", env={"TMUX": "/tmp/x,1,0", "TMUX_PANE": "%7",
                                                          "RELAY_CLOSE_DELAY": "0"})
        self.assertEqual(p.returncode, 0)
        time.sleep(0.5)
        self.assertIn("kill-pane -t %7", rtext(self.calls))

    def test_tmux_failure_falls_back(self):
        self.shim("tmux", 'exit 1\n')
        self.shim("osascript", 'printf "%s\\n" "$*" >> "' + self.calls + '"\nexit 0\n')
        self.shim("uname", 'echo Darwin\n')
        p = self.launch(mode="tmux")
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "terminal"), p.stderr)
        self.assertIn('tell application "Terminal"', rtext(self.calls))
        self.assertIn("do script", rtext(self.calls))

    def test_terminal_mode_iterm_and_failure(self):
        self.shim("osascript", 'printf "%s\\n" "$*" >> "' + self.calls + '"\nexit 0\n')
        p = self.launch("--terminal-app", "iTerm", mode="terminal")
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "terminal"))
        self.assertIn('tell application "iTerm"', rtext(self.calls))
        os.remove(self.calls)
        os.remove(os.path.join(self.launched_dir, os.path.basename(self.handoff) + ".done"))
        self.shim("osascript", 'exit 1\n')
        p = self.launch(mode="terminal")
        self.assertEqual(p.returncode, 4)
        self.assertTrue(p.stdout.startswith("deferred"))

    def test_auto_detection(self):
        # tmux server reachable -> tmux
        self.shim("tmux", 'printf "%s\\n" "$*" >> "' + self.calls + '"\nexit 0\n')
        p = self.launch(mode="auto")
        self.assertEqual(p.stdout.strip(), "tmux")
        os.remove(os.path.join(self.launched_dir, os.path.basename(self.handoff) + ".done"))
        # no tmux server, macOS with osascript -> terminal
        self.shim("tmux", 'exit 1\n')
        self.shim("osascript", 'exit 0\n')
        self.shim("uname", 'echo Darwin\n')
        p = self.launch(mode="auto")
        self.assertEqual(p.stdout.strip(), "terminal")
        os.remove(os.path.join(self.launched_dir, os.path.basename(self.handoff) + ".done"))
        # not macOS, no tmux -> file
        self.shim("uname", 'echo Linux\n')
        p = self.launch(mode="auto")
        self.assertEqual(p.returncode, 4)

    def test_shellcheck_clean(self):
        if not shutil.which("shellcheck"):
            self.skipTest("shellcheck not installed")
        for script, shell in ((LAUNCH_SH, "sh"), (os.path.join(os.path.dirname(LAUNCH_SH), "statusline.sh"), "bash")):
            p = subprocess.run(["shellcheck", "-s", shell, script], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout)
        self.launch(mode="file")
        runner = os.path.join(self.run_dir, os.listdir(self.run_dir)[0])
        p = subprocess.run(["shellcheck", "-s", "sh", runner], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout)
