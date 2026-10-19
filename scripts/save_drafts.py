#!/usr/bin/env python3
"""Save DiffStory post drafts to JSON files outside the repository.

Reads one JSON object on standard input:
    {"platform": "x" | "linkedin",
     "card": {"title": "...", "lesson": "..."},
     "drafts": ["first draft", "second draft"]}

Appends it as one entry to ~/.diffstory/<repo>/x.json or linkedin.json, where
<repo> is the name of the git repository the command runs in (or of the
current directory outside one), with a short hash added when another
repository of the same name already uses that folder. Set DIFFSTORY_HOME to
use another folder than ~/.diffstory.

Every draft is checked with scan_sensitive.check_draft first. A draft with a
secret is never written: the whole save is refused. Other flags, such as a
public tool name the user chose to keep, are saved with the draft. X drafts
over 280 characters are refused.

Usage:
    save_drafts.py <<'DIFFSTORY_DRAFTS'
    {"platform": "x", "card": {...}, "drafts": [...]}
    DIFFSTORY_DRAFTS

Exit status: 0 saved, 1 refused (a secret or an over-long draft),
2 bad input or the file could not be written (the reason is on stderr).
Standard library only. Never writes to the repository.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan_sensitive import check_draft, redact  # noqa: E402

FILES = {"x": "x.json", "linkedin": "linkedin.json"}
MAX_X_CHARS = 280
LINKEDIN_WORDS = (100, 200)
MAX_DRAFTS = 3


class Refused(Exception):
    """The drafts are unsafe or break a platform limit (exit 1)."""


class BadInput(Exception):
    """The input or the output file is unusable (exit 2)."""


def repo_name() -> tuple[str, str]:
    """The repository's folder name made safe for a path, and its full path."""
    try:
        top = subprocess.run(["git", "--no-pager", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        top = ""
    path = os.path.realpath(top or os.getcwd())
    name = re.sub(r"[^A-Za-z0-9._\-]", "-", os.path.basename(path)).strip(".-")
    return name or "no-repo", path


def drafts_folder() -> tuple[str, str]:
    """~/.diffstory/<repo>, or <repo>-<hash> when another repository of the same name has it."""
    name, repo_path = repo_name()
    home = os.environ.get("DIFFSTORY_HOME") or os.path.join(os.path.expanduser("~"), ".diffstory")
    folder = os.path.join(home, name)
    for file in FILES.values():
        owner = load(os.path.join(folder, file)).get("repository")
        if owner and owner != repo_path:
            digest = hashlib.sha256(repo_path.encode()).hexdigest()[:8]
            return os.path.join(home, f"{name}-{digest}"), repo_path
    return folder, repo_path


def parse(raw: str) -> tuple[str, dict[str, str], list[str]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise BadInput(f"input is not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise BadInput("input must be a JSON object")
    platform = data.get("platform")
    if platform not in FILES:
        raise BadInput('platform must be "x" or "linkedin"')
    card = data.get("card")
    if not isinstance(card, dict) or not all(isinstance(card.get(k), str) and card[k].strip()
                                             for k in ("title", "lesson")):
        raise BadInput("card needs a non-empty title and lesson")
    drafts = data.get("drafts")
    if (not isinstance(drafts, list) or not 1 <= len(drafts) <= MAX_DRAFTS
            or not all(isinstance(d, str) and d.strip() for d in drafts)):
        raise BadInput(f"drafts must be a list of 1-{MAX_DRAFTS} non-empty strings")
    return platform, {"title": card["title"].strip(), "lesson": card["lesson"].strip()}, \
        [d.strip() for d in drafts]


def check(platform: str, card: dict[str, str], drafts: list[str]) -> list[dict[str, object]]:
    """Each draft with its length and flags; raises Refused if any is unsafe or too long."""
    # The card is saved too, so it must be free of secrets as well.
    for field, text in card.items():
        if redact(text)[1]:
            raise Refused(f"card {field} contains a secret; remove it and save again")
    checked: list[dict[str, object]] = []
    for i, text in enumerate(drafts, 1):
        secrets = redact(text)[1]
        if secrets:
            kinds = ", ".join(sorted({f.type for f in secrets}))
            raise Refused(f"draft {i} contains a secret ({kinds}); nothing was saved")
        entry: dict[str, object] = {"text": text}
        if platform == "x":
            if len(text) > MAX_X_CHARS:
                raise Refused(f"draft {i} is {len(text)} characters, over the {MAX_X_CHARS} limit for X")
            entry["characters"] = len(text)
        else:
            entry["words"] = len(text.split())
        entry["flags"] = [{"type": f.type, "text": f.excerpt} for f in check_draft(text)]
        checked.append(entry)
    return checked


def load(path: str) -> dict[str, object]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise BadInput(f"{path} is not readable JSON ({e}); fix or move it, then save again") from None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise BadInput(f"{path} is not a DiffStory drafts file; fix or move it, then save again")
    return data


def write(path: str, data: dict[str, object]) -> None:
    """Write through a temporary file, so a crash never leaves half a file."""
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def save(raw: str) -> str:
    platform, card, drafts = parse(raw)
    checked = check(platform, card, drafts)
    folder, repo_path = drafts_folder()
    os.makedirs(folder, mode=0o700, exist_ok=True)
    path = os.path.join(folder, FILES[platform])

    data = load(path) or {"platform": platform, "repository": repo_path, "entries": []}
    entries = data["entries"]
    assert isinstance(entries, list)
    entries.append({
        "id": len(entries) + 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "card": card,
        "drafts": checked,
    })
    write(path, data)

    flagged = sum(1 for d in checked if d["flags"])
    note = f"; {flagged} with flags" if flagged else ""
    if platform == "linkedin":
        low, high = LINKEDIN_WORDS
        outside = [str(i) for i, d in enumerate(drafts, 1) if not low <= len(d.split()) <= high]
        if outside:
            note += f"; draft {', '.join(outside)} outside {low}-{high} words"
    return f"[diffstory] saved {len(checked)} {platform} draft(s) as entry {len(entries)} in {path}{note}"


def main() -> int:
    try:
        print(save(sys.stdin.read()))
    except Refused as e:
        print(f"[diffstory] refused: {e}", file=sys.stderr)
        return 1
    except (BadInput, OSError) as e:
        print(f"[diffstory] error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
