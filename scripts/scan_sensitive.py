#!/usr/bin/env python3
"""Find and redact secrets in text before it reaches the model or a draft.

Usage:
    scan_sensitive.py [FILE ...]          print redacted text (stdin if no file)
    scan_sensitive.py --check [FILE ...]  report secrets; exit 1 if any
    scan_sensitive.py --draft [FILE ...]  check a post draft for secrets and
                                          internal details (commit hashes, file
                                          names, emails, hosts); exit 1 if any
    add --json for machine-readable findings

Findings never include a secret itself, only its type and line number.
Standard library only.
"""

import argparse
import json
import math
import re
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass

# Known formats. Specific ones come first so a generic rule never relabels
# something a specific rule already caught. When a pattern has a "secret"
# group, only that group is redacted and the rest of the match is kept.
PATTERNS = [
    ("private-key", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
        r"[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}")),
    ("stripe-key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("stripe-webhook-secret", re.compile(r"\bwhsec_[A-Za-z0-9]{24,}")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]+")),
    ("discord-webhook", re.compile(
        r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_\-]+")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("google-oauth-secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_\-]{20,}")),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("groq-key", re.compile(r"\bgsk_[A-Za-z0-9]{40,}")),
    ("xai-key", re.compile(r"\bxai-[A-Za-z0-9]{40,}")),
    ("replicate-token", re.compile(r"\br8_[A-Za-z0-9]{30,}")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("pypi-token", re.compile(r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_\-]{50,}")),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}")),
    ("twilio-key", re.compile(r"\bSK[0-9a-fA-F]{32}\b")),
    ("mailgun-key", re.compile(r"\bkey-[0-9a-f]{32}\b")),
    ("digitalocean-token", re.compile(r"\bdo[opr]_v1_[a-f0-9]{64}\b")),
    ("shopify-token", re.compile(r"\bshp(?:at|ca|pa|ss)_[a-fA-F0-9]{32}\b")),
    ("telegram-bot-token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{33}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("azure-storage-key", re.compile(r"(?i)\b(?:AccountKey|SharedAccessKey)=(?P<secret>[A-Za-z0-9+/]{20,}={0,2})")),
    ("bearer-token", re.compile(r"(?i)\bbearer[ \t]+(?P<secret>[A-Za-z0-9\-._~+/]{20,}=*)")),
    ("basic-auth", re.compile(r"(?i)\bbasic[ \t]+(?P<secret>[A-Za-z0-9+/]{16,}={0,2})(?![A-Za-z0-9+/=])")),
]

# A variable or key whose name says it holds a secret. The name parts are
# bounded so long identifiers cannot make the regex backtrack for long.
NAME_PART = r"[A-Za-z0-9_.\-]{0,40}"
SECRET_WORD = (
    r"(?:pass(?:word|wd|phrase)?|pwd|secret|token|api[_\-]?key|apikey|access[_\-]?key"
    r"|auth[_\-]?key|private[_\-]?key|signing[_\-]?key|encryption[_\-]?key|master[_\-]?key"
    r"|session[_\-]?key|client[_\-]?secret|credentials?|dsn|conn(?:ection)?[_\-]?str(?:ing)?)"
)
SECRET_NAME = r"(?<![A-Za-z0-9_.])" + NAME_PART + SECRET_WORD + NAME_PART
PASSWORD_NAME = re.compile(r"(?i)pass|pwd")

# name = "value" / name: 'value' / "name": "value" / name := `value`
QUOTED_ASSIGNMENT = re.compile(
    r"(?i)(?P<name>" + SECRET_NAME + r")['\"]?[ \t]*(?:=|:|=>|:=)[ \t]*"
    r"(?P<quote>['\"`])(?P<secret>[^'\"`\s]{6,})(?P=quote)")
# NAME=value at the start of a line (.env, shell, YAML, INI); allows a diff
# +/- prefix and a trailing # comment.
BARE_ASSIGNMENT = re.compile(
    r"(?im)^[+\- ]?[ \t]*(?:export[ \t]+)?(?P<name>" + SECRET_NAME + r")[ \t]*(?:=|:)[ \t]*"
    r"(?P<secret>[^\s'\"`#]{6,})(?=[ \t]*(?:#.*)?$)")
# --password value / --api-key=value on a command line
CLI_FLAG = re.compile(r"(?i)(?<![\w-])--?(?P<name>" + SECRET_NAME + r")[ =](?P<secret>[^\s'\"`]{6,})")
# ?token=value / &api_key=value in a URL
URL_PARAM = re.compile(
    r"(?i)[?&;](?P<name>" + SECRET_NAME + r"|key|sig|signature)=(?P<secret>[^&\s'\"#]{6,})")
# user:password@ inside a URL; only the password is redacted.
URL_CREDENTIALS = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s/:@'\"]+:(?P<secret>[^\s/@'\"]+)@")

PLACEHOLDERS = re.compile(
    r"(?i)^(?:x+|\*+|\.+|-+|<.*>|\[.*\]|\$\{.*\}|\$[A-Z_][A-Z0-9_]*|%\(.*\)s|%s|\{\{.*\}\}|\{.*\}"
    r"|null|none|nil|true|false|undefined|required|optional|string|str|bool|int"
    r"|.*(?:changeme|change_me|example|placeholder|dummy|redacted|your[_\-]|xxx|\*\*\*).*)$")
# os.environ, settings.API_KEY, process.env.TOKEN, config.get(...)
CODE_REFERENCE = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+(?:\(.*)?$")


@dataclass
class Finding:
    type: str
    line: int
    excerpt: str = ""  # only for non-secret findings, such as a file name in a draft


def entropy(value: str) -> float:
    """Shannon entropy in bits per character."""
    counts = {c: value.count(c) for c in set(value)}
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def char_classes(value: str) -> int:
    return sum(bool(re.search(p, value)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))


def looks_secret(value: str, lenient: bool = False) -> bool:
    """True for values that look like real secrets; False for placeholders and code.

    lenient is for password fields: people choose those, so they are often short
    and low in entropy. Any non-placeholder that mixes character types counts.
    """
    if PLACEHOLDERS.match(value) or CODE_REFERENCE.match(value) or "(" in value:
        return False
    if lenient:
        return char_classes(value) >= 2
    return len(value) >= 8 and char_classes(value) >= 2 and entropy(value) >= 3.0 - 1e-9


# Given a match, return True to redact it.
Check = Callable[[re.Match[str]], bool]


def named_secret(m: re.Match[str]) -> bool:
    return looks_secret(m["secret"], lenient=bool(PASSWORD_NAME.search(m["name"])))


def url_password(m: re.Match[str]) -> bool:
    return looks_secret(m["secret"], lenient=True)


Rule = tuple[str, re.Pattern[str], Check | None]
RULES: list[Rule] = [(n, p, None) for n, p in PATTERNS]
RULES += [
    ("url-password", URL_CREDENTIALS, url_password),
    ("generic-secret", QUOTED_ASSIGNMENT, named_secret),
    ("generic-secret", BARE_ASSIGNMENT, named_secret),
    ("generic-secret", CLI_FLAG, named_secret),
    ("generic-secret", URL_PARAM, named_secret),
]


def redact(text: str) -> tuple[str, list[Finding]]:
    """Return (redacted_text, findings). Line numbers refer to the input text."""
    findings: list[Finding] = []
    for kind, pattern, check in RULES:
        current = text

        def replace(m: re.Match[str]) -> str:
            if check is not None and not check(m):
                return m.group(0)
            start, end = m.span("secret") if "secret" in pattern.groupindex else m.span()
            findings.append(Finding(kind, current.count("\n", 0, start) + 1))
            whole, offset = m.group(0), m.start()
            # Keep the line count so later line numbers still match the input.
            hidden = f"[REDACTED:{kind}]" + "\n" * current.count("\n", start, end)
            return whole[:start - offset] + hidden + whole[end - offset:]

        text = pattern.sub(replace, current)

    findings.sort(key=lambda f: f.line)
    return text, findings


def scan(text: str) -> list[Finding]:
    """Return findings without changing the text."""
    return redact(text)[1]


# Extra checks for public drafts: details that are not secrets but must not be
# posted (see "Keep evidence internal" in SKILL.md).
CODE_EXTENSIONS = (
    "py|pyi|ipynb|js|mjs|cjs|jsx|ts|tsx|go|rs|rb|java|kt|kts|swift|c|cc|cpp|h|hpp|cs|php|sh|bash|zsh"
    "|ps1|sql|yaml|yml|json|toml|ini|cfg|conf|env|lock|md|tf|tfvars|proto|gradle|xml|html|css|scss"
    "|vue|svelte|dart|scala|ex|exs|lua|r")
# Library names that look like file names; mentioning them is fine.
PUBLIC_NAMES = {
    "node.js", "next.js", "nuxt.js", "vue.js", "react.js", "three.js", "d3.js", "chart.js",
    "express.js", "ember.js", "backbone.js", "angular.js", "p5.js", "socket.io", "solid.js",
    "alpine.js", "moment.js", "day.js", "anime.js", "pixi.js", "babylon.js", "tensorflow.js",
    "brain.js", "ml5.js", "deno.js", "nest.js", "svelte.js", "htmx.js", "ollama.js",
}
DRAFT_PATTERNS: list[tuple[str, re.Pattern[str], Callable[[str], bool]]] = [
    ("commit-hash", re.compile(r"\b(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b"), lambda s: True),
    ("file-name", re.compile(
        r"(?<![\w.\-/@])(?:[\w.\-]+/)*[\w\-]+\.(?:" + CODE_EXTENSIONS + r")\b(?![.\w])", re.IGNORECASE),
        lambda s: s.lower() not in PUBLIC_NAMES),
    ("email", re.compile(r"\b[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)*\.[A-Za-z]{2,}\b"), lambda s: True),
    ("ip-address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        lambda s: all(int(part) <= 255 for part in s.split("."))),
    ("internal-host", re.compile(
        r"(?i)\blocalhost(?::\d+)?\b|\b[\w\-]+(?:\.[\w\-]+)*\.(?:internal|local|corp|lan|intranet)\b"),
        lambda s: True),
    ("high-entropy-string", re.compile(r"(?<![\w+/=\-])[A-Za-z0-9+/_\-]{20,}={0,2}"),
        lambda s: char_classes(s) >= 3 and entropy(s) >= 4.0),
]


def check_draft(text: str) -> list[Finding]:
    """Secrets plus internal details that must not appear in a public post."""
    redacted, findings = redact(text)
    for kind, pattern, keep in DRAFT_PATTERNS:
        for m in pattern.finditer(redacted):
            if keep(m.group(0)):
                findings.append(Finding(kind, redacted.count("\n", 0, m.start()) + 1, m.group(0)))
    findings.sort(key=lambda f: f.line)
    return findings


def summary(findings: list[Finding], source: str | None = None, verb: str = "redacted") -> str:
    where = f" in {source}" if source else ""
    noun = "issue(s)" if verb == "flagged" else "secret(s)"
    if not findings:
        return f"[diffstory] no {noun.removesuffix('(s)')}s found{where}"
    items = ", ".join(
        f"{f.type} {f.excerpt!r} (line {f.line})" if f.excerpt else f"{f.type} (line {f.line})" for f in findings)
    return f"[diffstory] {verb} {len(findings)} {noun}{where}: {items}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", help="files to scan; stdin if none")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report secrets only; exit 1 if any")
    mode.add_argument("--draft", action="store_true", help="check a post draft; exit 1 if anything is flagged")
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
        if args.draft:
            redacted, findings = text, check_draft(text)
        else:
            redacted, findings = redact(text)
        found_any = found_any or bool(findings)
        if args.json:
            report.append({"source": path or "<stdin>", "findings": [asdict(f) for f in findings]})
        elif args.draft:
            print(summary(findings, path, "flagged"))
        elif args.check:
            print(summary(findings, path, "found"))
        else:
            sys.stdout.write(redacted)
            if not redacted.endswith("\n"):
                sys.stdout.write("\n")
            print(summary(findings, path))
    if args.json:
        print(json.dumps(report, indent=2))
    return 1 if (args.check or args.draft) and found_any else 0


if __name__ == "__main__":
    sys.exit(main())
