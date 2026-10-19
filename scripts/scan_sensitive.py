#!/usr/bin/env python3
"""Find and redact secrets in text before it reaches the model or a draft.

Usage:
    scan_sensitive.py [FILE ...]          print redacted text (stdin if no file)
    scan_sensitive.py --check [FILE ...]  report findings; exit 1 if any
    add --json for machine-readable findings

Findings never include the secret itself, only its type and line number.
Standard library only.
"""

import argparse
import json
import math
import re
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass

# Specific formats first; the generic rules run last so they don't relabel
# something a specific rule already caught.
PATTERNS = [
    ("private-key", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
        r"[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}")),
    ("stripe-key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]+")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer-token", re.compile(r"(?i)(?<=bearer )[A-Za-z0-9\-._~+/]{20,}=*")),
]

# user:password@ inside a URL; only the password is redacted.
URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s/:@'\"]+:)([^\s/@'\"]+)(@)")

SECRET_NAME = (
    r"[A-Za-z0-9_.\-]*(?:password|passwd|pwd|secret|token|api[_\-]?key|apikey"
    r"|access[_\-]?key|auth[_\-]?key|private[_\-]?key|client[_\-]?secret|credentials?)"
    r"[A-Za-z0-9_.\-]*"
)
# name = "value" / name: 'value' / "name": "value"
QUOTED_ASSIGNMENT = re.compile(
    r"(?i)(" + SECRET_NAME + r"['\"]?\s*(?:=|:|=>)\s*)(['\"])([^'\"\s]{8,})(\2)")
# NAME=value at the start of a line (.env, shell, YAML); allows a diff +/- prefix.
BARE_ASSIGNMENT = re.compile(
    r"(?im)^([+\- ]?\s*(?:export\s+)?" + SECRET_NAME + r"[ \t]*(?:=|:)[ \t]*)([^\s'\"#]{8,})[ \t]*$")

PLACEHOLDERS = re.compile(
    r"(?i)^(?:x+|\*+|\.+|<.*>|\$\{.*\}|\$[A-Z_]+|%\(.*\)s|\{\{.*\}\}|null|none|true|false"
    r"|.*(?:changeme|example|placeholder|dummy|redacted|your[_\-]|xxx).*)$")


@dataclass
class Finding:
    type: str
    line: int


def entropy(value: str) -> float:
    """Shannon entropy in bits per character."""
    counts = {c: value.count(c) for c in set(value)}
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def looks_secret(value: str) -> bool:
    """True for random-looking values; False for placeholders, words, and code."""
    if PLACEHOLDERS.match(value) or "(" in value or value.startswith(("os.", "process.", "env.")):
        return False
    classes = sum(bool(re.search(p, value)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    return classes >= 2 and entropy(value) >= 3.0


# Given a match, return (finding type, replacement), or None to leave it alone.
Replacer = Callable[[re.Match[str]], tuple[str, str] | None]


def known_format(name: str) -> Replacer:
    # Keep the line count so later line numbers still match the input.
    return lambda m: (name, f"[REDACTED:{name}]" + "\n" * m.group(0).count("\n"))


def url_password(m: re.Match[str]) -> tuple[str, str] | None:
    return "url-password", m.group(1) + "[REDACTED:url-password]" + m.group(3)


def quoted_secret(m: re.Match[str]) -> tuple[str, str] | None:
    if not looks_secret(m.group(3)):
        return None
    return "generic-secret", m.group(1) + m.group(2) + "[REDACTED:generic-secret]" + m.group(4)


def bare_secret(m: re.Match[str]) -> tuple[str, str] | None:
    if not looks_secret(m.group(2)):
        return None
    return "generic-secret", m.group(1) + "[REDACTED:generic-secret]"


def redact(text: str) -> tuple[str, list[Finding]]:
    """Return (redacted_text, findings)."""
    findings: list[Finding] = []
    rules: list[tuple[re.Pattern[str], Replacer]] = [(p, known_format(n)) for n, p in PATTERNS]
    rules += [(URL_CREDENTIALS, url_password), (QUOTED_ASSIGNMENT, quoted_secret), (BARE_ASSIGNMENT, bare_secret)]

    for pattern, make in rules:
        current = text

        def replace(m: re.Match[str]) -> str:
            result = make(m)
            if result is None:
                return m.group(0)
            kind, replacement = result
            findings.append(Finding(kind, current.count("\n", 0, m.start()) + 1))
            return replacement

        text = pattern.sub(replace, current)

    findings.sort(key=lambda f: f.line)
    return text, findings


def scan(text: str) -> list[Finding]:
    """Return findings without changing the text."""
    return redact(text)[1]


def summary(findings: list[Finding], source: str | None = None) -> str:
    where = f" in {source}" if source else ""
    if not findings:
        return f"[diffstory] no secrets found{where}"
    items = ", ".join(f"{f.type} (line {f.line})" for f in findings)
    return f"[diffstory] redacted {len(findings)} secret(s){where}: {items}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", help="files to scan; stdin if none")
    parser.add_argument("--check", action="store_true", help="report only; exit 1 if anything is found")
    parser.add_argument("--json", action="store_true", help="print findings as JSON")
    args = parser.parse_args(argv)

    sources: list[tuple[str | None, str]] = []
    for path in args.files:
        with open(path, encoding="utf-8", errors="replace") as f:
            sources.append((path, f.read()))
    if not sources:
        sources = [(None, sys.stdin.read())]

    found_any = False
    report: list[dict[str, object]] = []
    for path, text in sources:
        redacted, findings = redact(text)
        found_any = found_any or bool(findings)
        if args.json:
            report.append({"source": path or "<stdin>", "findings": [asdict(f) for f in findings]})
        elif args.check:
            print(summary(findings, path).replace("redacted", "found"))
        else:
            sys.stdout.write(redacted)
            if not redacted.endswith("\n"):
                sys.stdout.write("\n")
            print(summary(findings, path))
    if args.json:
        print(json.dumps(report, indent=2))
    return 1 if args.check and found_any else 0


if __name__ == "__main__":
    sys.exit(main())
