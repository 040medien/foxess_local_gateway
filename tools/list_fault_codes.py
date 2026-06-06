#!/usr/bin/env python3
"""Print the FoxESS M1/Q microinverter fault code reference table.

Sourced from the FoxESS Q-Series User Manual V1.0.0. The M1 family
shares this numbering scheme.

Usage:
    python3 tools/list_fault_codes.py             # full table
    python3 tools/list_fault_codes.py 4156        # look up one code
    python3 tools/list_fault_codes.py 4156,4157   # look up a comma-set
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from foxess_local_cloud.telemetry import FAULT_CODE_NAMES, fault_code_message_for


def main() -> int:
    if len(sys.argv) > 1:
        query = sys.argv[1]
        message = fault_code_message_for(query)
        if not message:
            print(f"(empty input)")
            return 0
        print(f"{query}\t{message}")
        return 0

    groups: dict[str, list[tuple[str, str]]] = {
        "PV1 faults": [],
        "PV2 faults": [],
        "PV3 faults": [],
        "PV4 faults": [],
        "AC failures": [],
    }
    for code, name in sorted(FAULT_CODE_NAMES.items()):
        if name.startswith("PV1"):
            groups["PV1 faults"].append((code, name))
        elif name.startswith("PV2"):
            groups["PV2 faults"].append((code, name))
        elif name.startswith("PV3"):
            groups["PV3 faults"].append((code, name))
        elif name.startswith("PV4"):
            groups["PV4 faults"].append((code, name))
        else:
            groups["AC failures"].append((code, name))

    for group, entries in groups.items():
        print(f"\n== {group} ==")
        for code, name in entries:
            print(f"  {code}\t{name}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
