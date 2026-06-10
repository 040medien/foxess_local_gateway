#!/usr/bin/env python3
"""Generate a git-filter-repo replacements file from .claude/private-strings.

Reads each private literal and pairs it with a synthetic placeholder so
the rewrite leaves clearly-non-real values in their place. The placeholder
shapes are stable across runs so re-running filter-repo is idempotent.

Output is written to a path passed as argv[1] (defaults to a temp file).
The output file contains the private strings (one side of each rewrite
rule), so it must live OUTSIDE the repository and should be deleted after
the rewrite completes.

This script contains NO private literals — they are read from the
gitignored deny-list at runtime.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

RULES: list[tuple[str, str]] = [
    (r"^60[A-Z0-9]{13}$", "60TESTSERIAL000"),
    (r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$", "AA:BB:CC:DD:EE:FF"),
    (r".*", "PrivateAlias"),
]


def synthetic_for(value: str, counters: dict[str, int]) -> str:
    for pattern, base in RULES:
        if re.match(pattern, value):
            n = counters.setdefault(base, 0)
            counters[base] = n + 1
            return base if n == 0 else f"{base}{n}"
    return "PrivateAlias"


def main() -> int:
    here = Path(__file__).resolve().parent.parent
    deny = here / "private-strings"
    if not deny.exists():
        print(f"no deny-list at {deny}", file=sys.stderr)
        return 1
    entries = [
        line.strip()
        for line in deny.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/foxess-redact.txt")
    counters: dict[str, int] = {}
    lines = [f"{entry}==>{synthetic_for(entry, counters)}" for entry in entries]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(lines)} replacement rules to {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
