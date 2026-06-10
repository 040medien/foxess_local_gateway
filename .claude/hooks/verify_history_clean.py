#!/usr/bin/env python3
"""Verify the redacted private strings no longer appear anywhere in git
history. Reads the deny-list and greps every reachable object."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    here = Path(__file__).resolve().parent.parent
    deny = here / "private-strings"
    if not deny.exists():
        print("no deny-list", file=sys.stderr)
        return 1
    needles = [
        line.strip()
        for line in deny.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    bad = 0
    for needle in needles:
        result = subprocess.run(
            ["git", "log", "--all", "--oneline", "-S", needle],
            capture_output=True,
            text=True,
        )
        n = len(result.stdout.strip().splitlines()) if result.stdout.strip() else 0
        if n:
            bad += n
            print(f"  STILL PRESENT in {n} commits: <{len(needle)}-char private value>")
        else:
            print(f"  clean: <{len(needle)}-char private value>")
    print(f"\nTotal remaining leaks: {bad}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
