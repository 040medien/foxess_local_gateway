#!/bin/sh
set -eu

AP_CONF=/etc/foxess-local-cloud/pi-ap.conf
DAEMON_CONF=/etc/foxess-local-cloud/config.json
WIFI_CREDENTIALS=/etc/foxess-local-cloud/wifi-credentials.txt
EVENT_LOG=/var/log/foxess-local-cloud/events.jsonl
NFT_TABLE=foxess_local_cloud

find_cmd() {
  name=$1
  shift
  if command -v "$name" >/dev/null 2>&1; then
    command -v "$name"
    return 0
  fi
  for candidate in "$@"; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  printf ''
}

section() {
  printf '\n== %s ==\n' "$1"
}

status_line() {
  service=$1
  if [ -n "$SYSTEMCTL" ]; then
    state=$("$SYSTEMCTL" is-active "$service" 2>/dev/null || true)
    enabled=$("$SYSTEMCTL" is-enabled "$service" 2>/dev/null || true)
    printf '%-28s active=%-8s enabled=%s\n' "$service" "${state:-unknown}" "${enabled:-unknown}"
  fi
}

IW=$(find_cmd iw /usr/sbin/iw /sbin/iw)
IP=$(find_cmd ip /usr/sbin/ip /sbin/ip /usr/bin/ip)
NFT=$(find_cmd nft /usr/sbin/nft /sbin/nft /usr/bin/nft)
SS=$(find_cmd ss /usr/sbin/ss /sbin/ss /usr/bin/ss)
SYSTEMCTL=$(find_cmd systemctl /usr/bin/systemctl /bin/systemctl)
PYTHON=$(find_cmd python3 /usr/bin/python3 /bin/python3)

if [ -r "$AP_CONF" ]; then
  # shellcheck disable=SC1090
  . "$AP_CONF"
else
  AP_IFACE=ap0
  STA_IFACE=wlan0
  AP_ADDRESS=unknown
  AP_SUBNET=unknown
  FOXESS_CLOUD_IPS=
  ENABLE_NAT=unknown
  ENABLE_REDIRECT=unknown
fi

section "Services"
status_line foxess-pi-ap.service
status_line foxess-hostapd.service
status_line foxess-local-cloud.service
status_line dnsmasq.service

section "Interfaces"
if [ -n "$IP" ]; then
  "$IP" -br addr show "$STA_IFACE" 2>/dev/null || true
  "$IP" -br addr show "$AP_IFACE" 2>/dev/null || true
else
  echo "ip command not found"
fi
if [ -n "$IW" ]; then
  "$IW" dev "$AP_IFACE" info 2>/dev/null | sed 's/^/ap: /' || true
  "$IW" dev "$STA_IFACE" info 2>/dev/null | sed 's/^/sta: /' || true
fi

section "AP Clients"
if [ -n "$IW" ]; then
  stations=$("$IW" dev "$AP_IFACE" station dump 2>/dev/null || true)
  if [ -n "$stations" ]; then
    printf '%s\n' "$stations"
  else
    echo "no associated stations"
  fi
else
  echo "iw command not found"
fi

section "DHCP Leases"
if [ -r /var/lib/misc/dnsmasq.leases ]; then
  cat /var/lib/misc/dnsmasq.leases
else
  echo "lease file not readable"
fi

section "Redirects"
printf 'redirect_enabled=%s nat_enabled=%s ap_subnet=%s tcp_port=14431 relay_upstream_hints="%s"\n' "$ENABLE_REDIRECT" "$ENABLE_NAT" "$AP_SUBNET" "$FOXESS_CLOUD_IPS"
if [ -n "$NFT" ]; then
  "$NFT" list table inet "$NFT_TABLE" 2>/dev/null || echo "nft table missing or not readable"
else
  echo "nft command not found"
fi

section "Daemon Listener"
if [ -n "$SS" ]; then
  "$SS" -ltnp 2>/dev/null | grep ':14431' || echo "TCP/14431 listener not found"
else
  echo "ss command not found"
fi

section "Daemon Config"
if [ -r "$DAEMON_CONF" ] && [ -n "$PYTHON" ]; then
  "$PYTHON" - "$DAEMON_CONF" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
mqtt = data.get("mqtt", {})
relay = data.get("relay", {})
print(f"mqtt_host={mqtt.get('host') or 'disabled'} mqtt_port={mqtt.get('port', '')} mqtt_user={'set' if mqtt.get('username') else 'unset'}")
print(f"relay_enabled={relay.get('enabled')} upstreams={','.join((relay.get('upstreams') or {}).keys())}")
print(f"devices={','.join((data.get('devices') or {}).keys())}")
PY
else
  echo "daemon config not readable"
fi

section "Wi-Fi Credentials"
if [ -r "$WIFI_CREDENTIALS" ]; then
  sed -n 's/^SSID=/SSID=/p; s/^Passphrase=.*/Passphrase=<stored in \/etc\/foxess-local-cloud\/wifi-credentials.txt>/p; s/^Gateway=/Gateway=/p; s/^Subnet=/Subnet=/p; s/^Generated=/Generated=/p' "$WIFI_CREDENTIALS"
else
  echo "$WIFI_CREDENTIALS not readable"
fi

section "Recent Events"
if [ -r "$EVENT_LOG" ]; then
  tail -n 20 "$EVENT_LOG"
else
  echo "event log not readable or not created yet"
fi
