#!/bin/sh
set -eu

CONF=/etc/foxess-local-cloud/pi-ap.conf
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
  echo "missing required command: $name" >&2
  exit 1
}

if [ ! -r "$CONF" ]; then
  echo "missing $CONF" >&2
  exit 1
fi

# shellcheck disable=SC1090
. "$CONF"

IW=$(find_cmd iw /usr/sbin/iw /sbin/iw)
IP=$(find_cmd ip /usr/sbin/ip /sbin/ip /usr/bin/ip)
NFT=$(find_cmd nft /usr/sbin/nft /sbin/nft /usr/bin/nft)
SYSCTL=$(find_cmd sysctl /usr/sbin/sysctl /sbin/sysctl /usr/bin/sysctl)
SYSTEMCTL=$(find_cmd systemctl /usr/bin/systemctl /bin/systemctl)

start_ap_iface() {
  if ! "$IW" dev "$AP_IFACE" info >/dev/null 2>&1; then
    "$IW" phy phy0 interface add "$AP_IFACE" type __ap
  fi
  "$IP" link set "$AP_IFACE" up
  "$IP" addr flush dev "$AP_IFACE"
  "$IP" addr add "$AP_CIDR" dev "$AP_IFACE"
}

stop_ap_iface() {
  "$IP" addr flush dev "$AP_IFACE" 2>/dev/null || true
  "$IP" link set "$AP_IFACE" down 2>/dev/null || true
  "$IW" dev "$AP_IFACE" del 2>/dev/null || true
}

start_nft() {
  "$NFT" delete table inet "$NFT_TABLE" 2>/dev/null || true
  "$NFT" add table inet "$NFT_TABLE"
  "$NFT" "add chain inet $NFT_TABLE prerouting { type nat hook prerouting priority dstnat; policy accept; }"
  "$NFT" "add chain inet $NFT_TABLE postrouting { type nat hook postrouting priority srcnat; policy accept; }"

  if [ "$ENABLE_REDIRECT" = "1" ]; then
    "$NFT" add rule inet "$NFT_TABLE" prerouting iifname "$AP_IFACE" ip saddr "$AP_SUBNET" tcp dport 14431 counter redirect to :"$DAEMON_PORT"
  fi

  if [ "$ENABLE_NAT" = "1" ]; then
    "$NFT" add rule inet "$NFT_TABLE" postrouting oifname "$STA_IFACE" ip saddr "$AP_SUBNET" counter masquerade
  fi
}

stop_nft() {
  "$NFT" delete table inet "$NFT_TABLE" 2>/dev/null || true
}

start_services() {
  "$SYSTEMCTL" restart dnsmasq
}

stop_services() {
  "$SYSTEMCTL" restart dnsmasq 2>/dev/null || true
}

case "${1:-start}" in
  start)
    "$SYSCTL" -w net.ipv4.ip_forward=1 >/dev/null
    start_ap_iface
    start_nft
    start_services
    ;;
  stop)
    stop_services
    stop_nft
    stop_ap_iface
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    "$IW" dev "$AP_IFACE" info || true
    "$IP" addr show dev "$AP_IFACE" || true
    "$NFT" list table inet "$NFT_TABLE" || true
    "$SYSTEMCTL" --no-pager --full status foxess-hostapd dnsmasq foxess-local-cloud || true
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status}" >&2
    exit 2
    ;;
esac
