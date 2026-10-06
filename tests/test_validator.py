import json
import os
import subprocess

from helpers import RelayTestCase, FIXTURES, RELAY_PY, wtext, rtext

GOOD = rtext(os.path.join(FIXTURES, "handoff_good.md"))


class ValidatorTests(RelayTestCase):
    def validate(self, text, **kw):
        return self.relay.validate_handoff(text, self.paths, **kw)

    def test_good_handoff_passes(self):
        r = self.validate(GOOD)
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(r["errors"], [])
        self.assertTrue(all(r["sections"].values()), r["sections"])
        self.assertEqual(r["front_matter"]["session_id"], "sess-parent-1")

    def test_missing_front_matter(self):
        _, body = self.relay.parse_front_matter(GOOD)
        r = self.validate(body)
        self.assertFalse(r["ok"])
        self.assertTrue(any("front matter missing" in e for e in r["errors"]))

    def test_missing_front_matter_keys_and_status(self):
        text = GOOD.replace("git_head: abc1234\n", "").replace("status: pending", "status: draft")
        r = self.validate(text, require_status="pending")
        self.assertIn("front matter missing keys: git_head", r["errors"])
        self.assertTrue(any("status must be 'pending'" in e for e in r["errors"]))

    def test_missing_section_is_named(self):
        text = GOOD.split("## 7. Key files and commands")[0] + "## 8. Constraints and user preferences stated this session\n\n- x y z long enough\n\n## 9. Memory updates\n\nnone\n"
        r = self.validate(text)
        self.assertFalse(r["ok"])
        self.assertIn("missing section: key files", r["errors"])

    def test_template_boilerplate_counts_as_empty(self):
        fm = {"session_id": "s", "parent_session_id": "none", "generation": 0,
              "created_at": "x", "git_branch": "main", "git_head": "abc", "status": "pending"}
        text = self.relay.render_template(self.paths, fm)
        r = self.validate(text)
        self.assertFalse(r["ok"])
        empties = [e for e in r["errors"] if "is empty" in e]
        self.assertEqual(len(empties), 9, r["errors"])

    def test_memory_updates_none_is_enough(self):
        text = GOOD.split("## 9. Memory updates")[0] + "## 9. Memory updates\n\nnone\n"
        self.assertTrue(self.validate(text)["ok"])

    def test_word_limit(self):
        padding = "\n\n" + ("word " * 3000)
        text = GOOD.replace("## 9. Memory updates", padding + "\n\n## 9. Memory updates")
        r = self.validate(text, max_words=2500)
        self.assertFalse(r["ok"])
        self.assertTrue(any("too long" in e for e in r["errors"]))
        text = GOOD.replace("## 9. Memory updates", "\n\n" + ("word " * 2300) + "\n\n## 9. Memory updates")
        r = self.validate(text, max_words=2500)
        self.assertTrue(r["ok"])
        self.assertTrue(any("long:" in w for w in r["warnings"]))

    def test_headings_matched_by_keyword_not_number(self):
        text = GOOD.replace("## 7. Key files and commands", "### Key Files").replace(
            "## 2. Current state (done and verified)", "## Current State")
        self.assertTrue(self.validate(text)["ok"])

    def test_fenced_code_headings_ignored(self):
        text = GOOD.replace("## 3. In progress", "## 3. In progress\n\n```\n## not a heading\n```")
        self.assertTrue(self.validate(text)["ok"])

    def test_validate_cli(self):
        good = os.path.join(self.tmp, "good.md"); wtext(good, GOOD)
        bad = os.path.join(self.tmp, "bad.md"); wtext(bad, "# nothing\n")
        p = subprocess.run(["python3", RELAY_PY, "validate", good, "--cwd", self.project], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(json.loads(p.stdout)["ok"])
        p = subprocess.run(["python3", RELAY_PY, "validate", bad, "--cwd", self.project], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)


class RedactionTests(RelayTestCase):
    def test_known_token_shapes(self):
        text = ("key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 here\n"
                "aws AKIAABCDEFGHIJKLMNOP\n"
                "gh ghp_abcdefghijklmnopqrstuvwxyz0123456789\n"
                "slack xoxb-123456789012-abcdefghijkl\n"
                "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c\n"
                "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123\n"
                "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\n")
        out, hits = self.relay.redact_secrets(text)
        self.assertEqual(len(hits), 7, hits)
        for needle in ("sk-ant-", "AKIA", "ghp_", "xoxb-", "eyJ", "MIIE", "abcdefghijklmnopqrstuvwxyz0123"):
            self.assertNotIn(needle, out)
        self.assertIn("Authorization: Bearer [REDACTED]", out)

    def test_key_value_and_env_lines_keep_names(self):
        text = ("DATABASE_URL=postgres://user:hunter2hunter2@db.example.com/app\n"
                "api_key: 0123456789abcdef\n"
                "password = \"correct-horse-battery\"\n"
                "MAX_RETRIES=3\n"
                "The token field is in `config.py`.\n")
        out, hits = self.relay.redact_secrets(text)
        self.assertIn("DATABASE_URL=[REDACTED]", out)
        self.assertIn("api_key: [REDACTED]", out)
        self.assertIn("password = \"[REDACTED]\"", out)
        self.assertIn("MAX_RETRIES=3", out)
        self.assertIn("The token field is in `config.py`.", out)

    def test_clean_text_untouched(self):
        out, hits = self.relay.redact_secrets(GOOD)
        self.assertEqual((out, hits), (GOOD, []))

    def test_load_and_sanitize_rewrites_file_atomically(self):
        p = os.path.join(self.tmp, "h.md")
        wtext(p, GOOD.replace("none\n", "none\nTOKEN=abcdefghijklmnopqrstuvwxyz\n", 1))
        text, hits = self.relay.load_and_sanitize_handoff(p)
        self.assertEqual(len(hits), 1)
        self.assertIn("TOKEN=[REDACTED]", rtext(p))
        self.assertFalse([f for f in os.listdir(self.tmp) if f.startswith(".tmp-")])


class MechanicalHandoffTests(RelayTestCase):
    def test_mechanical_handoff_validates_and_carries_context(self):
        wtext(os.path.join(self.project, "todo.py"), "# TODO: finish me\nx = 1\n")
        subprocess.run(["git", "add", "todo.py"], cwd=self.project, check=True)
        subprocess.run(["git", "-c", "user.email=t@e.com", "-c", "user.name=t", "commit", "-q", "-m", "add todo"],
                       cwd=self.project, check=True)
        with open(os.path.join(self.project, "todo.py"), "a") as fh:
            fh.write("y = 2\n")
        wtext(os.path.join(self.project, "new.txt"), "hi\n")
        transcript = os.path.join(self.tmp, "t.jsonl")
        wtext(transcript, json.dumps({"type": "user", "isSidechain": False,
            "message": {"role": "user", "content": "Please build the relay thing"}}) + "\n")
        payload = self.payload("Stop", last_assistant_message="I was about to edit launch.sh " + "blah " * 600,
                               transcript_path=transcript)
        state = self.relay.load_relay_state(self.paths, "sess-0001")
        state["generation"] = 2; state["parent"] = "sess-0000"
        text = self.relay.mechanical_handoff(self.paths, self.relay.load_config(self.paths), payload, state, attempts=2)
        r = self.relay.validate_handoff(text, self.paths, require_status="pending")
        self.assertTrue(r["ok"], r["errors"])
        self.assertLess(r["word_count"], 2500)
        self.assertEqual(r["front_matter"]["generation"], "2")
        self.assertEqual(r["front_matter"]["parent_session_id"], "sess-0000")
        self.assertEqual(r["front_matter"]["mechanical"], "true")
        self.assertIn("Please build the relay thing", text)
        self.assertIn("I was about to edit launch.sh", text)
        self.assertIn("[…truncated]", text)
        self.assertIn("TODO: finish me", text)
        self.assertIn("`todo.py`", text)
        self.assertIn("`new.txt`", text)
        self.assertIn("add todo", text)

    def test_mechanical_handoff_without_git_or_transcript(self):
        import shutil
        shutil.rmtree(os.path.join(self.project, ".git"))
        payload = self.payload("Stop")
        state = self.relay.load_relay_state(self.paths, "sess-0001")
        text = self.relay.mechanical_handoff(self.paths, self.relay.load_config(self.paths), payload, state, attempts=2)
        r = self.relay.validate_handoff(text, self.paths)
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(r["front_matter"]["git_head"], "unknown")

    def test_front_matter_status_flip_is_atomic_and_keeps_body(self):
        p = os.path.join(self.tmp, "h.md"); wtext(p, GOOD)
        self.assertTrue(self.relay.set_front_matter_status(p, "consumed", consumed_by="child-1"))
        fm, body = self.relay.parse_front_matter(rtext(p))
        self.assertEqual((fm["status"], fm["consumed_by"]), ("consumed", "child-1"))
        self.assertEqual(fm["session_id"], "sess-parent-1")
        self.assertIn("## 9. Memory updates", body)
        self.assertTrue(self.relay.validate_handoff(rtext(p), self.paths)["ok"])
