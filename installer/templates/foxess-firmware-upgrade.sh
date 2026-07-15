#!/bin/bash
# Root-only wrapper for the deliberately separate firmware maintenance tool.
exec /opt/foxess-local-cloud/venv/bin/python3 \
  /opt/foxess-local-cloud/tools/foxess-firmware-upgrade "$@"
