#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" != 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

systemctl disable --now foxess-local-cloud.service foxess-hostapd.service foxess-pi-ap.service 2>/dev/null || true
/usr/local/sbin/foxess-pi-ap stop 2>/dev/null || true
rm -f /etc/systemd/system/foxess-local-cloud.service
rm -f /etc/systemd/system/foxess-hostapd.service
rm -f /etc/systemd/system/foxess-pi-ap.service
rm -f /usr/local/sbin/foxess-pi-ap
rm -f /usr/local/sbin/foxess-gateway-status
rm -f /etc/logrotate.d/foxess-local-cloud
rm -f /etc/dnsmasq.d/foxess-local-cloud.conf
rm -f /etc/hostapd/hostapd-foxess.conf
rm -f /etc/NetworkManager/conf.d/foxess-local-cloud.conf
rm -rf /opt/foxess-local-cloud
systemctl daemon-reload
systemctl restart dnsmasq 2>/dev/null || true

cat <<'EOF'
Removed FoxESS local cloud services and AP integration.

Left in place:
  /etc/foxess-local-cloud
  /var/lib/foxess-local-cloud
  /var/log/foxess-local-cloud

Remove those manually if you also want to delete config, certificates, and logs.
EOF
