#!/usr/bin/env python3
"""Read git history and diffs for DiffStory, with secrets redacted.

Every command runs with the skill's excludes and safety flags, skips files
over the size limit, and passes its output through scan_sensitive.redact,
so the model never sees a raw secret.

Usage:
    collect_git.py check-commit <hash>
    collect_git.py check-file <file>
    collect_git.py log <branch|A..B> [-n N] [--since DATE] [--until DATE] [--author EMAIL ...] [--file F]
    collect_git.py show <hash> [--stat] [--file F]
    collect_git.py diff [--staged] [--stat] [--file F]

Standard library only. Read-only: never writes to the repository.
"""

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from collections.abc import Iterable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan_sensitive import redact, summary  # noqa: E402

# Same list as $EXCLUDES in SKILL.md.
EXCLUDE_GLOBS = [
    "**/.env*", "**/*.pem", "**/*.key", "**/*.p12", "**/id_rsa*", "**/id_ed25519*",
    "**/*credential*", "**/creds*", "**/*secret*", "**/*.lock", "**/package-lock.json",
    "**/pnpm-lock.yaml", "**/node_modules/**", "**/vendor/**", "**/dist/**", "**/build/**",
    "**/*.min.js",
]
EXCLUDES = [f":(exclude,glob){g}" for g in EXCLUDE_GLOBS]
MAX_FILE_LINES = 500
SAFE_FLAGS = ["--no-color", "--no-ext-diff", "--no-textconv"]
REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*(?:\.\.[A-Za-z0-9][A-Za-z0-9._/\-]*)?$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2})?$|^\d+ (?:day|week|month|year)s? ago$")


class CollectError(Exception):
    pass


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], capture_output=True, text=True, errors="replace")
    if check and result.returncode != 0:
        raise CollectError(result.stderr.strip() or f"git {args[0]} failed")
    return result


def is_excluded(path: str) -> bool:
    parts = path.split("/")
    for glob in EXCLUDE_GLOBS:
        pattern = glob.removeprefix("**/")
        if pattern.endswith("/**"):
            if pattern[:-3] in parts[:-1]:
                return True
        elif fnmatch.fnmatchcase(parts[-1], pattern):
            return True
    return False


def require_revision(value: str) -> str:
    # Rejects anything that git could read as an option, such as --output=<file>.
    if not REVISION.match(value):
        raise CollectError(f"not a valid branch, hash, or range: {value!r}")
    for side in value.split(".."):
        if git("rev-parse", "--verify", "--quiet", f"{side}^{{commit}}", check=False).returncode != 0:
            raise CollectError(f"unknown revision: {side}")
    return value


def require_file(path: str) -> str:
    if path.startswith("-") or is_excluded(path):
        raise CollectError(f"file is excluded: {path}")
    if git("ls-files", "--error-unmatch", "--", path, check=False).returncode != 0:
        raise CollectError(f"file is not tracked by git: {path}")
    return path


def pathspec(file: str | None = None) -> list[str]:
    return ["--", require_file(file)] if file else ["--", ".", *EXCLUDES]


def large_files(numstat_args: list[str], spec: list[str]) -> list[tuple[str, str]]:
    """Files over MAX_FILE_LINES changed lines, or binary, from --numstat."""
    skipped: list[tuple[str, str]] = []
    for line in git(*numstat_args, "--numstat", "--format=", *spec).stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            continue
        added, deleted, path = fields
        if added == "-":
            skipped.append((path, "binary"))
        elif int(added) + int(deleted) > MAX_FILE_LINES:
            skipped.append((path, f"{int(added) + int(deleted)} changed lines"))
    return skipped


def without(spec: list[str], skipped: list[tuple[str, str]]) -> list[str]:
    return spec + [f":(exclude,literal){path}" for path, _ in skipped]


def emit(text: str, notes: Iterable[str] = ()) -> None:
    redacted, findings = redact(text)
    sys.stdout.write(redacted if redacted.endswith("\n") or not redacted else redacted + "\n")
    for note in notes:
        print(f"[diffstory] {note}")
    print(summary(findings))


def check_commit(args: argparse.Namespace) -> None:
    require_revision(args.hash)
    out = git("show", "-s", "--format=%h|%ae|%ad|%s", "--date=short", args.hash).stdout.strip()
    short, email, date, subject = out.split("|", 3)
    emit(f"commit {short}\nauthor {email}\ndate {date}\nsubject {subject}\n")


def check_file(args: argparse.Namespace) -> None:
    require_file(args.file)
    print(f"[diffstory] ok: {args.file} is tracked and not excluded")


def log(args: argparse.Namespace) -> None:
    require_revision(args.revision)
    cmd = ["log", args.revision, "--no-merges", "--date=short"]
    if args.n is not None:
        cmd.append(f"-n{args.n}")
    for flag, value in (("--since", args.since), ("--until", args.until)):
        if value:
            if not DATE.match(value):
                raise CollectError(f"{flag} must be YYYY-MM-DD, YYYY-MM-DDTHH:MM:SS, or 'N weeks ago'")
            cmd.append(f"{flag}={value}")
    authors: list[str] = args.author or []
    for email in authors:
        if email.startswith("-"):
            raise CollectError(f"invalid author: {email!r}")
        cmd.append(f"--author={email}")
    cmd += ["--stat", "--format=%h %ad %ae %s%n%b"]
    emit(git(*cmd, *pathspec(args.file)).stdout)


def show(args: argparse.Namespace) -> None:
    require_revision(args.hash)
    spec = pathspec(args.file)
    if args.stat:
        emit(git("show", args.hash, "--stat", "--format=%h %ad %ae %s", "--date=short", *spec).stdout)
        return
    skipped = large_files(["show", args.hash, *SAFE_FLAGS], spec)
    out = git("show", args.hash, *SAFE_FLAGS, "--format=%h %s%n%b", *without(spec, skipped)).stdout
    emit(out, [f"skipped {path} ({why})" for path, why in skipped])


def diff(args: argparse.Namespace) -> None:
    base = ["diff", *(["--staged"] if args.staged else [])]
    spec = pathspec(args.file)
    if args.stat:
        emit(git(*base, "--stat", *spec).stdout)
        return
    skipped = large_files([*base, *SAFE_FLAGS], spec)
    emit(git(*base, *SAFE_FLAGS, *without(spec, skipped)).stdout,
         [f"skipped {path} ({why})" for path, why in skipped])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check-commit", help="verify a commit exists and show its author")
    p.add_argument("hash")
    p.set_defaults(run=check_commit)

    p = sub.add_parser("check-file", help="verify a file is tracked and not excluded")
    p.add_argument("file")
    p.set_defaults(run=check_file)

    p = sub.add_parser("log", help="commit overview with --stat")
    p.add_argument("revision", help="branch, hash, or A..B range")
    p.add_argument("-n", type=int)
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--author", action="append", help="repeat for each identity")
    p.add_argument("--file")
    p.set_defaults(run=log)

    p = sub.add_parser("show", help="one commit's diff")
    p.add_argument("hash")
    p.add_argument("--stat", action="store_true")
    p.add_argument("--file")
    p.set_defaults(run=show)

    p = sub.add_parser("diff", help="uncommitted changes")
    p.add_argument("--staged", action="store_true")
    p.add_argument("--stat", action="store_true")
    p.add_argument("--file")
    p.set_defaults(run=diff)

    args = parser.parse_args(argv)
    try:
        args.run(args)
    except CollectError as error:
        print(f"[diffstory] error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
