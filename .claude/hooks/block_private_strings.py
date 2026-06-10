#!/usr/bin/env python3
"""PreToolUse hook: block Edit/Write/Bash if any private string from
.claude/private-strings appears in the tool input.

The deny-list lives outside git (gitignored) so the literal strings are
never committed. The hook fails open: if it can't read the deny-list
it allows the operation (no list = nothing to block).

Hook output protocol: exit code 2 with a message on stderr deterministically
blocks the tool call per the Claude Code hook contract.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def load_deny_list() -> list[str]:
    here = Path(__file__).resolve().parent.parent
    deny_path = here / "private-strings"
    if not deny_path.exists():
        return []
    return [
        line.strip()
        for line in deny_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def fields_to_scan(payload: dict) -> list[tuple[str, str]]:
    tool_input = payload.get("tool_input") or {}
    # Exempt the deny-list itself: editing it is the legitimate place to
    # add/remove private strings. Without this carve-out the hook would
    # be self-blocking.
    target = tool_input.get("file_path", "")
    if isinstance(target, str) and target.endswith(".claude/private-strings"):
        return []
    out: list[tuple[str, str]] = []
    for key in ("new_string", "content", "command"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            out.append((key, value))
    return out


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    deny = load_deny_list()
    if not deny:
        return 0

    fields = fields_to_scan(payload)
    if not fields:
        return 0

    hits: list[tuple[str, int]] = []
    for field_name, content in fields:
        for needle in deny:
            if needle and needle in content:
                hits.append((field_name, len(needle)))
                break

    if not hits:
        return 0

    tool_name = payload.get("tool_name", "?")
    lines = [
        f"[private-strings hook] Blocking {tool_name}: deny-list match in tool input.",
        "Matched fields:",
    ]
    for field_name, needle_len in hits:
        lines.append(f"  - {field_name}: matched a {needle_len}-char private string")
    lines.append(
        "Edit out the private value (device alias or hardware serial). "
        "The deny-list at .claude/private-strings is gitignored — add/remove entries there."
    )
    print("\n".join(lines), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
