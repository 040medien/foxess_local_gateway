#!/usr/bin/env python3
"""Run the local FoxESS M1/Q1 cloud emulator."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .config import load_config
from .server import FoxessLocalCloud


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()

    app = FoxessLocalCloud(load_config(args.config))
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        app.logger.close()
        app.mqtt.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
