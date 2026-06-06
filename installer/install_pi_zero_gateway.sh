#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRY_RUN=0
NONINTERACTIVE=0
PREVIEW_DIR=
SKIP_APP_COPY=0
INSTALL_PREFIX=/opt/foxess-local-cloud
CONFIG_DIR=/etc/foxess-local-cloud
STATE_DIR=/var/lib/foxess-local-cloud
LOG_DIR=/var/log/foxess-local-cloud
WIFI_CREDENTIALS_FILE=$CONFIG_DIR/wifi-credentials.txt

AP_SSID=FoxESS-Local
AP_PASSPHRASE=
AP_PASSPHRASE_GENERATED=0
AP_IFACE=ap0
STA_IFACE=wlan0
AP_ADDRESS=192.168.50.1
AP_PREFIX=24
AP_DHCP_START=192.168.50.20
AP_DHCP_END=192.168.50.80
AP_DHCP_LEASE=12h
AP_CHANNEL=auto
MQTT_HOST=
MQTT_HOST_SET=0
MQTT_PORT=1883
MQTT_PORT_SET=0
MQTT_USERNAME=
MQTT_USERNAME_SET=0
MQTT_PASSWORD=
MQTT_PASSWORD_SET=0
DISABLE_MQTT=0
PUBLISH_MIN_INTERVAL_SECONDS=0
ENABLE_NAT=1
ENABLE_REDIRECT=1
RELAY_ENABLED=false
RELAY_ENABLED_SET=0
FOXESS_CLOUD_IPS="8.209.116.72 47.91.86.144"
FOXESS_CLOUD_HOSTS="foxesscloud.com"

usage() {
  cat <<'EOF'
Usage: sudo installer/install_pi_zero_gateway.sh [options]

Configures a Raspberry Pi Zero W as a FoxESS inverter Wi-Fi gateway:
  - creates a virtual AP interface, usually ap0
  - runs hostapd/dnsmasq for inverter Wi-Fi and DHCP
  - redirects AP-client TCP/14431 traffic to the local daemon
  - installs foxess_local_cloud as a systemd service

Options:
  --dry-run                         Render files under ./build/pi-install-preview only
  --preview-dir DIR                 Dry-run output directory
  --skip-app-copy                   Dry-run only: render config without copying app tree
  --non-interactive                 Do not prompt; require passwords via flags/env
  --ap-ssid SSID                    Inverter-only Wi-Fi SSID
  --ap-passphrase PASSPHRASE        WPA2 passphrase, 8+ chars; generated if omitted
  --ap-address IP                   AP gateway IP, default 192.168.50.1
  --ap-prefix CIDR                  AP prefix length, default 24
  --ap-dhcp-start IP                DHCP range start, default 192.168.50.20
  --ap-dhcp-end IP                  DHCP range end, default 192.168.50.80
  --ap-dhcp-lease LEASE             DHCP lease time, default 12h
  --ap-channel CHANNEL|auto         Hostapd channel; auto reads current wlan0 channel
  --sta-iface IFACE                 Upstream Wi-Fi interface, default wlan0
  --ap-iface IFACE                  Virtual AP interface, default ap0
  --mqtt-host HOST                  Enable MQTT publishing to this host
  --mqtt-port PORT                  MQTT port, default 1883
  --mqtt-username USER              MQTT username
  --mqtt-password PASS              MQTT password
  --no-mqtt                         Clear MQTT host/credentials in generated config
  --publish-min-interval SECONDS    Optional MQTT publish throttle
  --relay                           Enable daemon relay mode in generated config
  --no-relay                        Disable daemon relay mode even if existing config enables it
  --foxess-cloud-ip IP              Add a FoxESS relay upstream hint; repeatable
  --foxess-cloud-host HOST          Resolve this host as a FoxESS relay upstream hint; repeatable
  --no-nat                          Disable NAT from inverter AP to upstream Wi-Fi
  --no-redirect                     Disable local TCP/14431 redirect rules
  --help                            Show this help

Environment alternatives:
  FOXESS_AP_PASSPHRASE, FOXESS_MQTT_PASSWORD
EOF
}

die() {
  echo "error: $*" >&2
  exit 1
}

json_string_or_null() {
  local value=$1
  if [[ -z "$value" ]]; then
    printf 'null'
  else
    python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$value"
  fi
}

json_relay_upstreams() {
  python3 - "$FOXESS_CLOUD_IPS" <<'PY'
import json
import sys

print(json.dumps({ip: f"{ip}:14431" for ip in sys.argv[1].split()}, indent=6))
PY
}

dnsmasq_address_lines() {
  python3 - "$FOXESS_CLOUD_HOSTS" "$AP_ADDRESS" <<'PY'
import sys

hosts, address = sys.argv[1:3]
for host in hosts.split():
    print(f"address=/{host}/{address}")
PY
}

existing_mqtt_field() {
  local field=$1 config_path=${2:-$CONFIG_DIR/config.json}
  python3 - "$field" "$config_path" <<'PY'
import json
import sys
from pathlib import Path

field, path = sys.argv[1:3]
try:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    raise SystemExit(0)
value = (data.get("mqtt") or {}).get(field)
if value is not None:
    print(value)
PY
}

preserve_existing_mqtt_config() {
  local existing_config=${FOXESS_EXISTING_CONFIG:-$CONFIG_DIR/config.json}
  local existing_value

  if [[ "$DISABLE_MQTT" = 1 ]]; then
    MQTT_HOST=
    MQTT_USERNAME=
    MQTT_PASSWORD=
    return 0
  fi
  [[ -r "$existing_config" ]] || return 0

  if [[ "$MQTT_HOST_SET" != 1 && -z "$MQTT_HOST" ]]; then
    MQTT_HOST=$(existing_mqtt_field host "$existing_config")
  fi
  if [[ "$MQTT_PORT_SET" != 1 ]]; then
    existing_value=$(existing_mqtt_field port "$existing_config")
    [[ -n "$existing_value" ]] && MQTT_PORT=$existing_value
  fi
  if [[ "$MQTT_USERNAME_SET" != 1 && -z "$MQTT_USERNAME" ]]; then
    MQTT_USERNAME=$(existing_mqtt_field username "$existing_config")
  fi
  if [[ "$MQTT_PASSWORD_SET" != 1 && -z "$MQTT_PASSWORD" ]]; then
    MQTT_PASSWORD=$(existing_mqtt_field password "$existing_config")
  fi
}

existing_relay_enabled() {
  local config_path=${1:-$CONFIG_DIR/config.json}
  python3 - "$config_path" <<'PY'
import json
import sys
from pathlib import Path

try:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    raise SystemExit(0)
enabled = (data.get("relay") or {}).get("enabled")
if enabled is True:
    print("true")
elif enabled is False:
    print("false")
PY
}

existing_devices_json() {
  local config_path=${1:-$CONFIG_DIR/config.json}
  python3 - "$config_path" <<'PY'
import json
import sys
from pathlib import Path

try:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    print("{}")
    raise SystemExit(0)
devices = data.get("devices") or {}
if not isinstance(devices, dict):
    devices = {}
print(json.dumps({str(key): str(value) for key, value in devices.items()}, indent=4))
PY
}

preserve_existing_relay_config() {
  local existing_config=${FOXESS_EXISTING_CONFIG:-$CONFIG_DIR/config.json}
  local existing_value
  [[ "$RELAY_ENABLED_SET" != 1 ]] || return 0
  [[ -r "$existing_config" ]] || return 0
  existing_value=$(existing_relay_enabled "$existing_config")
  [[ -n "$existing_value" ]] && RELAY_ENABLED=$existing_value
  return 0
}

validate_json_number() {
  local name=$1 value=$2
  python3 - "$name" "$value" <<'PY'
import sys
name, value = sys.argv[1:3]
try:
    float(value)
except ValueError:
    raise SystemExit(f"{name} must be numeric: {value}")
PY
}

validate_ipv4_subnet() {
  python3 - "$AP_ADDRESS" "$AP_PREFIX" "$AP_DHCP_START" "$AP_DHCP_END" <<'PY'
import ipaddress
import sys
address, prefix, dhcp_start, dhcp_end = sys.argv[1:5]
iface = ipaddress.ip_interface(f"{address}/{prefix}")
network = iface.network
start = ipaddress.ip_address(dhcp_start)
end = ipaddress.ip_address(dhcp_end)
if iface.ip == network.network_address or iface.ip == network.broadcast_address:
    raise SystemExit(f"AP address cannot be network/broadcast address: {iface.ip}")
if start not in network or end not in network:
    raise SystemExit("DHCP range must be inside AP subnet")
if int(start) > int(end):
    raise SystemExit("DHCP start must be <= DHCP end")
print(str(network))
PY
}

prepare_preview_dir() {
  local target=$1 marker
  [[ -n "$target" && "$target" = /* ]] || die "preview dir must resolve to an absolute path"
  case "$target" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/sys|/tmp|/usr|/var)
      die "refusing unsafe preview dir: $target"
      ;;
  esac
  marker="$target/.foxess-local-cloud-preview"
  if [[ -e "$target" && ! -e "$marker" ]]; then
    if find "$target" -mindepth 1 -maxdepth 1 | read -r _; then
      die "preview dir exists and is not marked as disposable: $target"
    fi
  fi
  rm -rf "$target"
  mkdir -p "$target"
  printf 'FoxESS local cloud installer preview directory\n' >"$marker"
}

unique_words() {
  python3 - "$@" <<'PY'
import sys

seen = set()
for value in sys.argv[1:]:
    for item in value.split():
        if item and item not in seen:
            print(item)
            seen.add(item)
PY
}

resolve_cloud_ips() {
  python3 - "$FOXESS_CLOUD_IPS" "$FOXESS_CLOUD_HOSTS" <<'PY'
import ipaddress
import socket
import sys

explicit, hosts = sys.argv[1:3]
seen: set[str] = set()

def emit(ip: str) -> None:
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return
    if parsed.version != 4:
        return
    value = str(parsed)
    if value not in seen:
        seen.add(value)
        print(value)

for item in explicit.split():
    emit(item)

for host in hosts.split():
    try:
        infos = socket.getaddrinfo(host, 14431, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        continue
    for info in infos:
        emit(info[4][0])
PY
}

generate_ap_passphrase() {
  python3 - <<'PY'
import secrets

print(secrets.token_hex(16))
PY
}

existing_wifi_passphrase() {
  local credentials_path=${FOXESS_EXISTING_WIFI_CREDENTIALS:-$WIFI_CREDENTIALS_FILE}
  if [[ "$DRY_RUN" = 1 && -z "${FOXESS_EXISTING_WIFI_CREDENTIALS:-}" ]]; then
    return 0
  fi
  python3 - "$credentials_path" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    lines = path.read_text(encoding="utf-8").splitlines()
except (FileNotFoundError, PermissionError):
    raise SystemExit(0)
for line in lines:
    if not line.startswith("Passphrase="):
        continue
    value = line.split("=", 1)[1]
    if 8 <= len(value) <= 63:
        print(value)
    raise SystemExit(0)
PY
}

render_template() {
  local src=$1 dst=$2
  python3 - "$src" "$dst" <<'PY'
import os
import string
import sys
src, dst = sys.argv[1:3]
text = open(src, encoding="utf-8").read()
rendered = string.Template(text).safe_substitute(os.environ)
open(dst, "w", encoding="utf-8").write(rendered)
PY
}

run() {
  if [[ "$DRY_RUN" = 1 ]]; then
    echo "+ $*"
  else
    "$@"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --preview-dir) PREVIEW_DIR=${2:?}; shift 2 ;;
    --skip-app-copy) SKIP_APP_COPY=1; shift ;;
    --non-interactive) NONINTERACTIVE=1; shift ;;
    --ap-ssid) AP_SSID=${2:?}; shift 2 ;;
    --ap-passphrase) AP_PASSPHRASE=${2:?}; shift 2 ;;
    --ap-address) AP_ADDRESS=${2:?}; shift 2 ;;
    --ap-prefix) AP_PREFIX=${2:?}; shift 2 ;;
    --ap-dhcp-start) AP_DHCP_START=${2:?}; shift 2 ;;
    --ap-dhcp-end) AP_DHCP_END=${2:?}; shift 2 ;;
    --ap-dhcp-lease) AP_DHCP_LEASE=${2:?}; shift 2 ;;
    --ap-channel) AP_CHANNEL=${2:?}; shift 2 ;;
    --sta-iface) STA_IFACE=${2:?}; shift 2 ;;
    --ap-iface) AP_IFACE=${2:?}; shift 2 ;;
    --mqtt-host) MQTT_HOST=${2:?}; MQTT_HOST_SET=1; shift 2 ;;
    --mqtt-port) MQTT_PORT=${2:?}; MQTT_PORT_SET=1; shift 2 ;;
    --mqtt-username) MQTT_USERNAME=${2:?}; MQTT_USERNAME_SET=1; shift 2 ;;
    --mqtt-password) MQTT_PASSWORD=${2:?}; MQTT_PASSWORD_SET=1; shift 2 ;;
    --no-mqtt) DISABLE_MQTT=1; shift ;;
    --publish-min-interval) PUBLISH_MIN_INTERVAL_SECONDS=${2:?}; shift 2 ;;
    --relay) RELAY_ENABLED=true; RELAY_ENABLED_SET=1; shift ;;
    --no-relay) RELAY_ENABLED=false; RELAY_ENABLED_SET=1; shift ;;
    --foxess-cloud-ip) FOXESS_CLOUD_IPS="$FOXESS_CLOUD_IPS ${2:?}"; shift 2 ;;
    --foxess-cloud-host) FOXESS_CLOUD_HOSTS="$FOXESS_CLOUD_HOSTS ${2:?}"; shift 2 ;;
    --no-nat) ENABLE_NAT=0; shift ;;
    --no-redirect) ENABLE_REDIRECT=0; shift ;;
    --help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

AP_PASSPHRASE=${AP_PASSPHRASE:-${FOXESS_AP_PASSPHRASE:-}}
MQTT_PASSWORD=${MQTT_PASSWORD:-${FOXESS_MQTT_PASSWORD:-}}
if [[ -n "${FOXESS_MQTT_PASSWORD+x}" && "$MQTT_PASSWORD_SET" != 1 ]]; then
  MQTT_PASSWORD_SET=1
fi

if [[ "$DRY_RUN" != 1 && "$(id -u)" != 0 ]]; then
  die "run as root, or use --dry-run"
fi

preserve_existing_mqtt_config
preserve_existing_relay_config

if [[ -z "$AP_PASSPHRASE" ]]; then
  AP_PASSPHRASE=$(existing_wifi_passphrase)
  if [[ -z "$AP_PASSPHRASE" ]]; then
    AP_PASSPHRASE=$(generate_ap_passphrase)
    AP_PASSPHRASE_GENERATED=1
  fi
fi
[[ ${#AP_PASSPHRASE} -ge 8 ]] || die "AP passphrase must be at least 8 characters"
[[ ${#AP_PASSPHRASE} -le 63 ]] || die "AP passphrase must be 63 characters or fewer"
[[ -n "$AP_SSID" && ${#AP_SSID} -le 32 ]] || die "AP SSID must be 1..32 characters"
validate_json_number "publish minimum interval" "$PUBLISH_MIN_INTERVAL_SECONDS"

if [[ "$AP_CHANNEL" = auto ]]; then
  AP_CHANNEL=$({ iw dev "$STA_IFACE" info 2>/dev/null || true; } | awk '/channel/ {print $2; exit}')
  AP_CHANNEL=${AP_CHANNEL:-6}
fi

AP_SUBNET=$(validate_ipv4_subnet)
AP_CIDR="$AP_ADDRESS/$AP_PREFIX"
FOXESS_CLOUD_IPS=$(resolve_cloud_ips | paste -sd' ' -)
FOXESS_CLOUD_HOSTS=$(unique_words "$FOXESS_CLOUD_HOSTS" | paste -sd' ' -)
[[ -n "$FOXESS_CLOUD_IPS" ]] || die "no FoxESS cloud IPv4 addresses configured or resolvable"

MQTT_USERNAME_JSON=$(json_string_or_null "$MQTT_USERNAME")
MQTT_PASSWORD_JSON=$(json_string_or_null "$MQTT_PASSWORD")
RELAY_UPSTREAMS_JSON=$(json_relay_upstreams)
DEVICES_JSON=$(existing_devices_json "${FOXESS_EXISTING_CONFIG:-$CONFIG_DIR/config.json}")
DNSMASQ_ADDRESS_LINES=$(dnsmasq_address_lines)
export AP_SSID AP_PASSPHRASE AP_PASSPHRASE_GENERATED AP_IFACE STA_IFACE AP_ADDRESS AP_CIDR AP_SUBNET AP_CHANNEL
export AP_DHCP_START AP_DHCP_END AP_DHCP_LEASE FOXESS_CLOUD_IPS ENABLE_NAT ENABLE_REDIRECT
export DAEMON_PORT=14431 MQTT_HOST MQTT_PORT MQTT_USERNAME_JSON MQTT_PASSWORD_JSON
export PUBLISH_MIN_INTERVAL_SECONDS RELAY_ENABLED RELAY_UPSTREAMS_JSON DNSMASQ_ADDRESS_LINES
export DEVICES_JSON

if [[ "$DRY_RUN" = 1 ]]; then
  TARGET="${PREVIEW_DIR:-$ROOT_DIR/build/pi-install-preview}"
  if [[ "$TARGET" != /* ]]; then
    TARGET="$ROOT_DIR/$TARGET"
  fi
  prepare_preview_dir "$TARGET"
  mkdir -p "$TARGET"/{etc/foxess-local-cloud,etc/dnsmasq.d,etc/hostapd,etc/NetworkManager/conf.d,etc/systemd/system,usr/local/sbin,opt}
else
  TARGET=
fi

dest() {
  if [[ "$DRY_RUN" = 1 ]]; then
    printf '%s/%s' "$TARGET" "$1"
  else
    printf '/%s' "$1"
  fi
}

install_file() {
  local src=$1 dst=$2 mode=${3:-0644}
  if [[ "$DRY_RUN" = 1 ]]; then
    mkdir -p "$(dirname "$(dest "$dst")")"
    cp "$src" "$(dest "$dst")"
    chmod "$mode" "$(dest "$dst")"
  else
    install -D -m "$mode" "$src" "/$dst"
  fi
}

render_to() {
  local tmpl=$1 dst=$2 mode=${3:-0644}
  local out
  out=$(dest "$dst")
  mkdir -p "$(dirname "$out")"
  render_template "$tmpl" "$out"
  chmod "$mode" "$out"
}

echo "Installing FoxESS local cloud gateway"
echo "  AP:        $AP_SSID on $AP_IFACE $AP_CIDR channel $AP_CHANNEL"
if [[ "$AP_PASSPHRASE_GENERATED" = 1 ]]; then
  echo "  AP key:    generated; saved to $WIFI_CREDENTIALS_FILE"
else
  echo "  AP key:    provided; saved to $WIFI_CREDENTIALS_FILE"
fi
echo "  Upstream:  $STA_IFACE"
echo "  Redirect:  $ENABLE_REDIRECT for AP-client tcp/14431"
echo "  Upstreams: $FOXESS_CLOUD_IPS"
echo "  MQTT:      ${MQTT_HOST:-disabled}"
echo "  Relay:     $RELAY_ENABLED"

if [[ "$DRY_RUN" != 1 ]]; then
  apt-get update
  apt-get install -y python3 python3-venv openssl hostapd dnsmasq nftables iw rsync logrotate
  systemctl unmask hostapd || true
  systemctl stop foxess-local-cloud.service foxess-hostapd.service foxess-pi-ap.service 2>/dev/null || true
  systemctl stop hostapd dnsmasq || true
  id foxess >/dev/null 2>&1 || useradd --system --home "$STATE_DIR" --shell /usr/sbin/nologin foxess
  mkdir -p "$INSTALL_PREFIX" "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR"
  chown foxess:foxess "$STATE_DIR" "$LOG_DIR"
fi

if [[ "$SKIP_APP_COPY" = 1 && "$DRY_RUN" != 1 ]]; then
  die "--skip-app-copy is only valid with --dry-run"
fi

if [[ "$DRY_RUN" = 1 && "$SKIP_APP_COPY" != 1 ]]; then
  mkdir -p "$(dest "${INSTALL_PREFIX#/}")"
  rsync -a \
    --exclude .git \
    --exclude .DS_Store \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude build \
    --exclude venv \
    "$ROOT_DIR/" "$(dest "${INSTALL_PREFIX#/}")/"
elif [[ "$DRY_RUN" != 1 ]]; then
  rsync -a --delete \
    --exclude .git \
    --exclude .DS_Store \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude build \
    --exclude venv \
    "$ROOT_DIR/" "$INSTALL_PREFIX/"
  python3 -m venv "$INSTALL_PREFIX/venv"
  "$INSTALL_PREFIX/venv/bin/pip" install --upgrade pip
  "$INSTALL_PREFIX/venv/bin/pip" install -r "$INSTALL_PREFIX/requirements.txt"
  chown -R root:root "$INSTALL_PREFIX"
fi

if [[ "$DRY_RUN" != 1 && -f "$CONFIG_DIR/config.json" ]]; then
  cp "$CONFIG_DIR/config.json" "$CONFIG_DIR/config.json.bak"
  chmod 0600 "$CONFIG_DIR/config.json.bak"
fi

# Tighten permissions on any leftover config backups so MQTT credentials
# inside them are not group-readable. Pattern matches both the current
# .bak file and historical .before-* names from earlier installer versions.
if [[ "$DRY_RUN" != 1 ]]; then
  shopt -s nullglob
  for bak in "$CONFIG_DIR"/config.json.bak "$CONFIG_DIR"/config.json.before-*; do
    [[ -f "$bak" ]] && chmod 0600 "$bak" && chown root:root "$bak"
  done
  shopt -u nullglob
fi

render_to "$ROOT_DIR/installer/templates/config.json.template" "${CONFIG_DIR#/}/config.json" 0640
render_to "$ROOT_DIR/installer/templates/wifi-credentials.txt.template" "${WIFI_CREDENTIALS_FILE#/}" 0600
render_to "$ROOT_DIR/installer/templates/foxess-pi-ap.conf.template" "${CONFIG_DIR#/}/pi-ap.conf" 0644
render_to "$ROOT_DIR/installer/templates/dnsmasq-foxess.conf.template" "etc/dnsmasq.d/foxess-local-cloud.conf" 0644
render_to "$ROOT_DIR/installer/templates/hostapd-foxess.conf.template" "etc/hostapd/hostapd-foxess.conf" 0600
render_to "$ROOT_DIR/installer/templates/networkmanager-foxess.conf.template" "etc/NetworkManager/conf.d/foxess-local-cloud.conf" 0644
install_file "$ROOT_DIR/installer/templates/foxess-pi-ap.sh" "usr/local/sbin/foxess-pi-ap" 0755
install_file "$ROOT_DIR/installer/templates/foxess-gateway-status.sh" "usr/local/sbin/foxess-gateway-status" 0755
install_file "$ROOT_DIR/installer/templates/logrotate-foxess-local-cloud.conf" "etc/logrotate.d/foxess-local-cloud" 0644
install_file "$ROOT_DIR/installer/systemd/foxess-pi-ap.service" "etc/systemd/system/foxess-pi-ap.service" 0644
install_file "$ROOT_DIR/installer/systemd/foxess-hostapd.service" "etc/systemd/system/foxess-hostapd.service" 0644
install_file "$ROOT_DIR/installer/systemd/foxess-local-cloud.service" "etc/systemd/system/foxess-local-cloud.service" 0644

if [[ "$DRY_RUN" != 1 ]]; then
  chown root:foxess "$CONFIG_DIR/config.json"
  chown root:root "$WIFI_CREDENTIALS_FILE"
  systemctl reload-or-restart NetworkManager 2>/dev/null || true
  systemctl daemon-reload
  systemctl enable foxess-pi-ap.service foxess-hostapd.service foxess-local-cloud.service
  systemctl restart foxess-pi-ap.service
  systemctl restart foxess-hostapd.service
  systemctl restart foxess-local-cloud.service
  systemctl --no-pager --full status foxess-pi-ap.service foxess-hostapd.service foxess-local-cloud.service || true
else
  echo "Dry-run files rendered under $TARGET"
fi

cat <<EOF

FoxESS inverter Wi-Fi:
  SSID:       $AP_SSID
  Passphrase: $AP_PASSPHRASE
  Saved:      $WIFI_CREDENTIALS_FILE
EOF
