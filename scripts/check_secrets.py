#!/usr/bin/env python3
"""Fail the build if a credential looks committed.

Runs in CI on every push. Cheap insurance: the cost of a leaked Supabase
service key in a public hackathon repo is your whole database, and the usual
way it happens is someone pasting a key into a notebook "just to test".
"""
from __future__ import annotations

import re
import subprocess
import sys

PATTERNS = {
    "Duffel token":        re.compile(r"duffel_(test|live)_[A-Za-z0-9_\-]{20,}"),
    "Groq key":            re.compile(r"gsk_[A-Za-z0-9]{40,}"),
    "Supabase JWT":        re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\."),
    "Supabase URL+key":    re.compile(r"https://[a-z0-9]{20}\.supabase\.co"),
    "AWS access key":      re.compile(r"AKIA[0-9A-Z]{16}"),
    "Generic assignment":  re.compile(
        r"(?i)(api[_-]?key|secret[_-]?key|service[_-]?key|password|token)"
        r"\s*[:=]\s*['\"][A-Za-z0-9_\-]{24,}['\"]"),
}

# Files that legitimately contain the *names* of secrets, not their values.
ALLOW = ("scripts/check_secrets.py", ".github/workflows/", "docs/", "README.md")


def tracked_files() -> list[str]:
    r = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
    return [f for f in r.stdout.splitlines() if f]


def main() -> int:
    hits = []
    for path in tracked_files():
        if path.startswith(ALLOW):
            continue
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for label, pat in PATTERNS.items():
            for m in pat.finditer(text):
                line = text[:m.start()].count("\n") + 1
                hits.append(f"{path}:{line}  {label}")

    if hits:
        print("Possible credentials in tracked files:\n")
        for h in hits:
            print(f"  {h}")
        print("\nMove these to GitHub/HF/Vercel secrets. If a key was ever "
              "committed, rotate it — removing the line does not remove it "
              "from git history.")
        return 1
    print(f"no credential patterns found in {len(tracked_files())} tracked files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
