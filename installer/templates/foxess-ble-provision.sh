#!/bin/bash
# Wrapper that runs the foxess-ble-provision tool inside the daemon's venv.
# Installed as /usr/local/sbin/foxess-ble-provision by install_pi_zero_gateway.sh.
exec /opt/foxess-local-cloud/venv/bin/python3 \
  /opt/foxess-local-cloud/tools/foxess-ble-provision "$@"
