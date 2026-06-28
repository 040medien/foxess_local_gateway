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

log() {
  echo "foxess-pi-ap: $*" >&2
}

iface_mac() {
  "$IP" link show "$1" 2>/dev/null | awk '/link\/ether/ {print $2; exit}'
}

# A single-radio Pi shares one channel between the station interface and the AP.
# FoxESS inverters are 2.4 GHz-only, so if the home Wi-Fi has put the radio on a
# 5 GHz channel the AP cannot serve them. Warn loudly rather than fail, so the
# diagnosis is visible in the journal if hostapd then misbehaves.
warn_if_sta_5ghz() {
  sta_ch=$("$IW" dev "$STA_IFACE" info 2>/dev/null | awk '/channel/ {print $2; exit}')
  [ -n "$sta_ch" ] || return 0
  if [ "$sta_ch" -gt 14 ] 2>/dev/null; then
    log "WARNING: $STA_IFACE is on 5 GHz (channel $sta_ch); a single-radio Pi cannot also run a 2.4 GHz inverter AP. Move the home Wi-Fi to 2.4 GHz, use an Ethernet uplink, or add a second Wi-Fi adapter."
  fi
}

# The AP interface shares one radio with the station interface, and the Wi-Fi
# firmware demultiplexes received frames by destination MAC. If the AP vif
# inherits the station's MAC verbatim, the kernel refuses to bring it up
# ("Could not set interface ap0 flags (UP): Name not unique on network",
# ENOTUNIQ) and hostapd never starts.
#
# Only act when the two MACs are actually identical. Most brcmfmac firmwares
# auto-derive a distinct MAC for the AP vif; we leave those untouched so an
# already-provisioned AP keeps its BSSID and inverters do not have to
# re-associate. When they do collide, assign the AP a distinct,
# locally-administered MAC while the interface is down.
#
# Derivation: set the locally-administered bit (0x02) on the first octet AND
# flip the 0x02 bit of the last octet. Touching the last octet guarantees the
# result differs from the station MAC even if it was already locally
# administered, and from the station-MAC-with-LA-bit value some drivers hand to
# a P2P-device wdev.
set_ap_mac() {
  sta_mac=$(iface_mac "$STA_IFACE")
  if [ -z "$sta_mac" ]; then
    log "could not read $STA_IFACE MAC; leaving $AP_IFACE MAC unchanged"
    return 0
  fi
  [ "$(iface_mac "$AP_IFACE")" = "$sta_mac" ] || return 0

  first=$(printf '%s' "$sta_mac" | cut -d: -f1)
  mid=$(printf '%s' "$sta_mac" | cut -d: -f2-5)
  last=$(printf '%s' "$sta_mac" | cut -d: -f6)
  new_first=$(printf '%02x' "$(( 0x$first | 0x02 ))")
  new_last=$(printf '%02x' "$(( 0x$last ^ 0x02 ))")
  ap_mac="$new_first:$mid:$new_last"

  log "$AP_IFACE shares $STA_IFACE MAC ($sta_mac); assigning distinct MAC $ap_mac"
  if ! "$IP" link set "$AP_IFACE" address "$ap_mac" 2>/dev/null; then
    log "WARNING: failed to set $AP_IFACE MAC to $ap_mac; hostapd may fail to start"
    return 0
  fi
  if [ "$(iface_mac "$AP_IFACE")" = "$sta_mac" ]; then
    log "WARNING: $AP_IFACE MAC still equals $STA_IFACE; hostapd may fail to start"
  fi
}

start_ap_iface() {
  if ! "$IW" dev "$AP_IFACE" info >/dev/null 2>&1; then
    phy=$("$IW" dev "$STA_IFACE" info 2>/dev/null | awk '/wiphy/ {print "phy" $2; exit}')
    if [ -z "$phy" ]; then
      echo "cannot determine Wi-Fi PHY for $STA_IFACE" >&2
      exit 1
    fi
    "$IW" phy "$phy" interface add "$AP_IFACE" type __ap
  fi
  "$IP" link set "$AP_IFACE" down
  set_ap_mac
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
    warn_if_sta_5ghz
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
