"""Tests for scripts/collect_git.py against a throwaway repository."""

import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "collect_git.py")
R = "aB3dE5fG7hJ9kL2mN4pQ6rS8tU0vW1xY"
TOKEN = "".join(("gh", "p_", R, "1234"))


class CollectGitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "dev@example.com")
        self.git("config", "user.name", "Dev")
        self.write("app.py", f'GITHUB_TOKEN = "{TOKEN}"\nprint("hi")\n')
        self.write(".env", f"PASSWORD={R}\n")
        self.write("big.txt", "x\n" * 600)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"Add app (token {TOKEN})")
        self.head = self.git("rev-parse", "--short", "HEAD").stdout.strip()

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=True)

    def write(self, name, content):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(content)

    def run_script(self, *args):
        return subprocess.run([sys.executable, SCRIPT, *args], cwd=self.repo, capture_output=True, text=True)

    def test_show_redacts_and_excludes(self):
        result = self.run_script("show", self.head)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(TOKEN, result.stdout)  # diff and commit message
        self.assertNotIn(R, result.stdout)      # .env content
        self.assertNotIn(".env", result.stdout)
        self.assertIn("[REDACTED:github-token]", result.stdout)
        self.assertIn("skipped big.txt (600 changed lines)", result.stdout)

    def test_log_redacts_commit_message(self):
        result = self.run_script("log", "main", "-n", "5", "--author", "dev@example.com")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(TOKEN, result.stdout)
        self.assertIn("app.py", result.stdout)

    def test_unstaged_diff(self):
        self.write("app.py", f'GITHUB_TOKEN = "{TOKEN}"\nprint("bye")\n')
        result = self.run_script("diff", "--file", "app.py")
        self.assertIn('+print("bye")', result.stdout)
        self.assertNotIn(TOKEN, result.stdout)

    def test_check_commit(self):
        result = self.run_script("check-commit", self.head)
        self.assertIn("author dev@example.com", result.stdout)
        self.assertEqual(self.run_script("check-commit", "deadbeef").returncode, 2)

    def test_check_file(self):
        self.assertEqual(self.run_script("check-file", "app.py").returncode, 0)
        self.assertEqual(self.run_script("check-file", ".env").returncode, 2)
        self.assertEqual(self.run_script("check-file", "missing.py").returncode, 2)

    def test_rejects_option_injection(self):
        result = self.run_script("log", "--output=/tmp/x")
        self.assertNotEqual(result.returncode, 0)
        result = self.run_script("show", "HEAD", "--file", "--output=/tmp/x")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
