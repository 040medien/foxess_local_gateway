#!/usr/bin/env python3
"""List closed/merged PRs whose file diffs still contain any deny-listed
string. Reads the deny-list at .claude/private-strings and uses `gh api`
to fetch each PR's patch.

Output: PR numbers + titles for affected PRs. The deny-listed values
themselves are never printed (only their lengths).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = "040medien/foxess_local_gateway"


def load_deny_list() -> list[str]:
    here = Path(__file__).resolve().parent.parent
    deny = here / "private-strings"
    if not deny.exists():
        return []
    return [
        line.strip()
        for line in deny.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def gh_json(args: list[str]) -> object:
    result = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def main() -> int:
    needles = load_deny_list()
    if not needles:
        print("empty deny-list, nothing to check", file=sys.stderr)
        return 1
    print(f"scanning closed PRs against {len(needles)} deny-list entries...", file=sys.stderr)

    prs = gh_json(["-X", "GET", f"/repos/{REPO}/pulls", "-f", "state=closed", "-f", "per_page=100"])
    affected: list[tuple[int, str, list[int]]] = []
    for pr in prs:
        number = pr["number"]
        title = pr["title"]
        files = gh_json([f"/repos/{REPO}/pulls/{number}/files"])
        hits_by_len: list[int] = []
        for f in files:
            patch = f.get("patch") or ""
            for needle in needles:
                if needle in patch:
                    hits_by_len.append(len(needle))
        if hits_by_len:
            affected.append((number, title, hits_by_len))

    if not affected:
        print("\nno affected closed PRs found")
        return 0

    print(f"\n{len(affected)} closed PRs still carry deny-list content in their diffs:")
    for number, title, hits in affected:
        unique_lens = sorted(set(hits))
        len_summary = ", ".join(f"{n}-char" for n in unique_lens)
        print(f"  #{number}  ({len_summary})  {title[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
