"""Tests for scripts/save_drafts.py, saving into a throwaway DIFFSTORY_HOME."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "save_drafts.py")
R = "aB3dE5fG7hJ9kL2mN4pQ6rS8tU0vW1xY"
TOKEN = "".join(("gh", "p_", R, "1234"))
CARD = {"title": "Date-only ranges drop commits", "lesson": "Pin the time of day."}
LONG_POST = " ".join(["word"] * 120)


class SaveDraftsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = os.path.realpath(self.tmp.name)
        self.home = os.path.join(root, "home")
        self.repo = os.path.join(root, "my repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, payload, cwd=None):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run([sys.executable, SCRIPT], input=raw, cwd=cwd or self.repo, capture_output=True,
                              text=True, env={**os.environ, "DIFFSTORY_HOME": self.home})

    def saved(self, name="my-repo", file="x.json"):
        with open(os.path.join(self.home, name, file)) as f:
            return json.load(f)

    def test_saves_and_appends(self):
        out = self.run_script({"platform": "x", "card": CARD, "drafts": ["One insight.", "Another one."]})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("saved 2 x draft(s) as entry 1", out.stdout)
        self.run_script({"platform": "x", "card": CARD, "drafts": ["Third."]})
        data = self.saved()
        self.assertEqual(data["repository"], self.repo)
        self.assertEqual([e["id"] for e in data["entries"]], [1, 2])
        self.assertEqual(data["entries"][0]["drafts"][0], {"text": "One insight.", "characters": 12, "flags": []})
        self.assertEqual(data["entries"][0]["card"], CARD)
        self.assertEqual(os.stat(os.path.join(self.home, "my-repo", "x.json")).st_mode & 0o777, 0o600)

    def test_linkedin_counts_words(self):
        out = self.run_script({"platform": "linkedin", "card": CARD, "drafts": [LONG_POST, "Too short."]})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("draft 2 outside 100-200 words", out.stdout)
        drafts = self.saved(file="linkedin.json")["entries"][0]["drafts"]
        self.assertEqual([d["words"] for d in drafts], [120, 2])

    def test_runs_from_a_subdirectory(self):
        sub = os.path.join(self.repo, "src")
        os.makedirs(sub)
        self.assertEqual(self.run_script({"platform": "x", "card": CARD, "drafts": ["Hi."]}, cwd=sub).returncode, 0)
        self.assertEqual(len(self.saved()["entries"]), 1)

    def test_refuses_secrets(self):
        for payload in ({"platform": "x", "card": CARD, "drafts": ["Fine.", f"Token {TOKEN}"]},
                        {"platform": "x", "card": {**CARD, "lesson": f"Use {TOKEN}"}, "drafts": ["Fine."]}):
            with self.subTest(payload=payload):
                out = self.run_script(payload)
                self.assertEqual(out.returncode, 1)
                self.assertIn("secret", out.stderr)
                self.assertNotIn(TOKEN, out.stderr + out.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.home, "my-repo", "x.json")))

    def test_keeps_other_flags(self):
        out = self.run_script({"platform": "x", "card": CARD, "drafts": ["Fixed in collect_git.py today."]})
        self.assertIn("1 with flags", out.stdout)
        flags = self.saved()["entries"][0]["drafts"][0]["flags"]
        self.assertEqual(flags, [{"type": "file-name", "text": "collect_git.py"}])

    def test_refuses_long_x_draft(self):
        self.assertEqual(self.run_script({"platform": "x", "card": CARD, "drafts": ["a" * 281]}).returncode, 1)
        self.assertEqual(self.run_script({"platform": "x", "card": CARD, "drafts": ["a" * 280]}).returncode, 0)

    def test_rejects_bad_input(self):
        for payload in ("not json", "[]", {"platform": "threads", "card": CARD, "drafts": ["x"]},
                        {"platform": "x", "card": {"title": "t"}, "drafts": ["x"]},
                        {"platform": "x", "card": CARD, "drafts": []},
                        {"platform": "x", "card": CARD, "drafts": ["a", "b", "c", "d"]},
                        {"platform": "x", "card": CARD, "drafts": ["  "]}):
            with self.subTest(payload=payload):
                self.assertEqual(self.run_script(payload).returncode, 2)

    def test_never_overwrites_a_broken_file(self):
        os.makedirs(os.path.join(self.home, "my-repo"))
        path = os.path.join(self.home, "my-repo", "x.json")
        with open(path, "w") as f:
            f.write("{broken")
        out = self.run_script({"platform": "x", "card": CARD, "drafts": ["Hi."]})
        self.assertEqual(out.returncode, 2)
        with open(path) as f:
            self.assertEqual(f.read(), "{broken")

    def test_same_name_repositories_stay_apart(self):
        self.run_script({"platform": "x", "card": CARD, "drafts": ["First repo."]})
        other = os.path.join(os.path.dirname(self.repo), "elsewhere", "my repo")
        os.makedirs(other)
        subprocess.run(["git", "init", "-q", other], check=True)
        out = self.run_script({"platform": "x", "card": CARD, "drafts": ["Second repo."]}, cwd=other)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(self.saved()["entries"]), 1)
        folders = sorted(os.listdir(self.home))
        self.assertEqual(len(folders), 2)
        self.assertTrue(folders[1].startswith("my-repo-"))

    def test_never_writes_to_the_repository(self):
        self.run_script({"platform": "x", "card": CARD, "drafts": ["Hi."]})
        status = subprocess.run(["git", "status", "--porcelain", "--ignored"], cwd=self.repo,
                                capture_output=True, text=True, check=True).stdout
        self.assertEqual(status, "")


if __name__ == "__main__":
    unittest.main()
