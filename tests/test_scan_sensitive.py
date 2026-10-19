"""Tests for scripts/scan_sensitive.py. Run: python3 -m unittest discover tests

Fake keys are assembled at runtime so this file never contains a string that
looks like a real secret to GitHub's push protection.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from scan_sensitive import check_draft, redact, scan  # noqa: E402

R = "aB3dE5fG7hJ9kL2mN4pQ6rS8tU0vW1xY"  # random-looking filler


def fake(*parts):
    return "".join(parts)


class DetectsKnownFormats(unittest.TestCase):
    cases = {
        "aws-access-key": fake("AK", "IA", "Z7QK3M2X9PLW4RTB"),
        "github-token": fake("gh", "p_", R, "1234"),
        "gitlab-token": fake("gl", "pat-", R[:20]),
        "anthropic-key": fake("sk-", "ant-", "api03-", R),
        "openai-key": fake("sk-", "proj-", R),
        "stripe-key": fake("sk", "_live_", R[:24]),
        "slack-token": fake("xo", "xb-", "1234567890-", R[:16]),
        "slack-webhook": fake("https://hooks.", "slack.com/services/", "T000/B000/", R[:24]),
        "google-api-key": fake("AI", "za", R, "abc"),
        "jwt": fake("ey", "J", R[:16], ".ey", "J", R[:16], ".", R[:16]),
        "stripe-webhook-secret": fake("wh", "sec_", R),
        "discord-webhook": fake("https://discord.", "com/api/webhooks/", "123456/", R),
        "google-oauth-secret": fake("GOC", "SPX-", R),
        "huggingface-token": fake("h", "f_", R),
        "groq-key": fake("gs", "k_", R, R[:12]),
        "xai-key": fake("xa", "i-", R, R[:12]),
        "replicate-token": fake("r8", "_", R),
        "npm-token": fake("np", "m_", R, "abcd"),
        "sendgrid-key": fake("S", "G.", R[:22], ".", R),
        "twilio-key": fake("S", "K", "0123456789abcdef0123456789abcdef"),
        "mailgun-key": fake("ke", "y-", "0123456789abcdef0123456789abcdef"),
        "telegram-bot-token": fake("123456789", ":AA", R, "a"),
    }

    def test_each_format(self):
        for kind, secret in self.cases.items():
            with self.subTest(kind=kind):
                text, findings = redact(f"value = {secret}\n")
                self.assertNotIn(secret, text)
                self.assertEqual([f.type for f in findings], [kind])

    def test_private_key_block(self):
        block = fake("-----BEGIN ", "RSA PRIVATE KEY-----\n", R, "\n", R, "\n-----END ", "RSA PRIVATE KEY-----")
        text, findings = redact(f"before\n{block}\nafter\n")
        self.assertNotIn(R, text)
        self.assertEqual(findings[0].type, "private-key")
        # Line count is preserved, so "after" stays on line 6.
        self.assertEqual(text.splitlines()[5], "after")

    def test_header_tokens_keep_their_prefix(self):
        for line, kind, kept in ((f"Authorization:  Bearer {R}", "bearer-token", "Authorization:  Bearer "),
                                 (f"Authorization: Basic {R}==", "basic-auth", "Authorization: Basic "),
                                 (fake("DefaultEndpointsProtocol=https;Account", "Key=", R, "=="),
                                  "azure-storage-key", "AccountKey=")):
            with self.subTest(kind=kind):
                text, findings = redact(line)
                self.assertEqual(text, f"{kept}[REDACTED:{kind}]" if kind != "azure-storage-key"
                                 else f"DefaultEndpointsProtocol=https;AccountKey=[REDACTED:{kind}]")
                self.assertEqual([f.type for f in findings], [kind])


class DetectsGenericSecrets(unittest.TestCase):
    def test_quoted_assignment(self):
        for line in (f'API_KEY = "{R}"', f"password: '{R}'", f'"client_secret": "{R}"', f'db_pwd => "{R}"'):
            with self.subTest(line=line):
                text, findings = redact(line)
                self.assertNotIn(R, text)
                self.assertEqual(findings[0].type, "generic-secret")

    def test_env_file_line_in_diff(self):
        text, findings = redact(f"+DATABASE_PASSWORD={R}\n")
        self.assertEqual(text, "+DATABASE_PASSWORD=[REDACTED:generic-secret]\n")
        self.assertEqual(len(findings), 1)

    def test_assignment_forms(self):
        cases = [
            f"  db_password: {R}  # prod",     # YAML, trailing comment
            f"-API_KEY={R}",                   # removed line in a diff
            f"export SECRET_TOKEN={R}",
            f"curl --api-key {R} https://x",   # command-line flag
            f"https://x.io/cb?access_token={R}&page=2",
            f"token := `{R}`",                 # Go
        ]
        for line in cases:
            with self.subTest(line=line):
                text, findings = redact(line)
                self.assertNotIn(R, text)
                self.assertEqual([f.type for f in findings], ["generic-secret"])

    def test_password_fields_are_lenient(self):
        # People choose passwords, so short, low-entropy ones still count.
        for line in ("DB_PASS=Winter2026", "password = 'hunter2!'", "pwd: s3cret@1"):
            with self.subTest(line=line):
                self.assertEqual([f.type for f in scan(line)], ["generic-secret"])

    def test_url_password(self):
        text, findings = redact(f"url = postgres://admin:{R}@db.internal:5432/app")
        self.assertIn("postgres://admin:[REDACTED:url-password]@db.internal", text)
        self.assertEqual(findings[0].type, "url-password")


class IgnoresHarmlessText(unittest.TestCase):
    def test_no_false_positives(self):
        harmless = [
            'password = "changeme"',
            'API_KEY = "your_api_key_here"',
            'token = os.getenv("TOKEN")',
            "SECRET_KEY=${SECRET_KEY}",
            'password = "<password>"',
            "token_count = 12345678",
            "We fixed the date range so --since keeps commits from the start date.",
            "def get_secret_name(self):",
            "https://example.com/path?page=2",
            'password = "password"',
            "password: str",
            "TOKEN=${{ secrets.GH_TOKEN }}",
            "api_key = settings.API_KEY",
            'api_key = os.environ["API_KEY"]',
            "max_tokens: 4096",
            "compass = north",
            "postgres://user:password@localhost/db",
            "Basic setup instructions follow",
        ]
        for line in harmless:
            with self.subTest(line=line):
                self.assertEqual(scan(line), [])


class Reporting(unittest.TestCase):
    def test_findings_have_line_numbers_not_values(self):
        secret = fake("gh", "p_", R, "1234")
        findings = scan(f"line one\nline two\ntoken {secret}\n")
        self.assertEqual(findings[0].line, 3)
        self.assertNotIn(secret, repr(findings))

    def test_redaction_is_idempotent(self):
        once, _ = redact(f'API_KEY = "{R}"')
        twice, findings = redact(once)
        self.assertEqual(once, twice)
        self.assertEqual(findings, [])


class ChecksDrafts(unittest.TestCase):
    def test_flags_internal_details(self):
        draft = ("Fixed the bug in collect_git.py (6c02caa) for ys@corp.com.\n"
                 f"It ran on api.internal, 10.0.0.12, and localhost:8080 with {R}.\n")
        kinds = {f.type: f for f in check_draft(draft)}
        self.assertEqual(set(kinds), {"file-name", "commit-hash", "email", "internal-host",
                                      "ip-address", "high-entropy-string"})
        self.assertEqual(kinds["file-name"].excerpt, "collect_git.py")
        self.assertEqual(kinds["high-entropy-string"].line, 2)

    def test_secrets_have_no_excerpt(self):
        findings = check_draft(fake("token: gh", "p_", R, "1234"))
        self.assertEqual([(f.type, f.excerpt) for f in findings], [("github-token", "")])

    def test_clean_draft(self):
        draft = ("Built it with Next.js and Node.js on Python 3.12. "
                 "The diff kept the post honest: 3 commits, 120 lines, version 1.2.3.")
        self.assertEqual(check_draft(draft), [])


if __name__ == "__main__":
    unittest.main()
