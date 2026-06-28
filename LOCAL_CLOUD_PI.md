# Raspberry Pi Gateway Runbook

This runbook installs `foxess-m1-local-gateway` on a Raspberry Pi Zero W.

The Pi joins your normal LAN as a Wi-Fi client and simultaneously creates an
inverter-only Wi-Fi AP:

```text
LAN / MQTT broker <-> wlan0  Raspberry Pi Zero W  ap0 <-> FoxESS inverter
```

The inverter connects to the Pi AP. TCP/14431 connections from AP clients are
redirected to the local daemon, which decodes pushed telemetry and publishes
MQTT state.

A writable `Active Power Limit` slider for Home Assistant is enabled
by default — see *Inverter Control* below.

## Networking Model

The Pi Zero W has one Wi-Fi radio. AP mode and client mode must use the same
radio channel in practice. The installer keeps `wlan0` managed by
NetworkManager, creates a virtual `ap0` interface for the inverter AP, and marks
only `ap0` unmanaged.

Because it is one radio, `wlan0` (home) and `ap0` (inverter) share a single
channel — you cannot run the AP on 2.4 GHz while `wlan0` is associated to 5 GHz.
FoxESS inverters are 2.4 GHz-only, so the home Wi-Fi must also be on the 2.4 GHz
band. The installer auto-detects the channel from `wlan0` and refuses a 5 GHz
channel; it also sets a `country_code` (auto-detected, or `--ap-country DE`) so
channels 12/13 are usable. A Pi Zero W / Zero 2 W has a 2.4 GHz-only radio and
avoids this entirely. If you must keep the home Wi-Fi on 5 GHz, give the Pi an
Ethernet uplink (which frees the radio for a pure 2.4 GHz AP) or add a second
Wi-Fi adapter.

Raspberry Pi OS Lite is recommended.

## Install

Configure the Pi's normal Wi-Fi and SSH with Raspberry Pi Imager first. Copy the
repository to the Pi and run:

```bash
sudo ./installer/install_pi_zero_gateway.sh \
  --ap-ssid FoxESS-Local \
  --mqtt-host mqtt-broker.local \
  --mqtt-username foxess \
  --mqtt-password 'your-mqtt-password'
```

If `--ap-passphrase` is omitted, the installer generates a random 32-character
WPA2 passphrase and saves it here:

```text
/etc/foxess-local-cloud/wifi-credentials.txt
```

The installer prints the AP passphrase whenever it runs. On reinstall, an
existing valid passphrase is reused unless `--ap-passphrase` is supplied.

Useful options:

```text
--ap-passphrase PASSPHRASE
--mqtt-host HOST
--mqtt-username USER
--mqtt-password PASS
--no-mqtt
--publish-min-interval SECONDS
--relay
--no-relay
--ap-address 192.168.50.1
--ap-dhcp-start 192.168.50.20
--ap-dhcp-end 192.168.50.80
--ap-channel 6
--ap-country DE
--foxess-cloud-ip 203.0.113.10
--foxess-cloud-host api.example.invalid
--dry-run
--preview-dir /tmp/foxess-install-preview
--skip-app-copy
--app-only
```

Fresh installs leave relay disabled. Re-running the installer preserves an
existing `relay.enabled=true` setting unless `--no-relay` is passed.

### Upgrading the daemon code

A full re-run reconfigures the AP, nftables, and config. When you only changed
the daemon code (for example testing a feature branch), use the fast path:

```bash
sudo ./installer/install_pi_zero_gateway.sh --app-only
```

It syncs the app tree into `/opt/foxess-local-cloud`, reinstalls Python
dependencies only if `requirements.txt` changed, and restarts just the daemon —
leaving the inverter AP, nftables redirect, and `/etc` config untouched. It
requires an existing full install. Repeat redeploys take a couple of seconds.

## Connect The Inverter

Check the AP details:

```bash
sudo foxess-gateway-status
```

For an already commissioned inverter, you can either provision the Pi AP
credentials over Bluetooth (preferred) or use the FoxCloud app's Wi-Fi
configuration flow.

### Bluetooth provisioning from the Pi

```bash
sudo foxess-ble-provision
```

This drops into an interactive loop: it scans for nearby M1/Q1 inverters,
shows them with signal strength, and provisions whichever ones you pick.
The Pi AP SSID and passphrase are read from `wifi-credentials.txt`. The
loop stays running until you press Enter to quit, so you can provision
several inverters in one session.

Other useful subcommands:

```bash
sudo foxess-ble-provision scan                          # list nearby M1s
sudo foxess-ble-provision networks AA:BB:CC:DD:EE:FF    # list WiFi networks visible to that inverter
sudo foxess-ble-provision provision <mac> --yes         # one-shot, scripted setup
```

If the BLE link drops mid-flow, run the command again — the inverter
aggressively closes idle BLE connections and may need 1–2 retries before
one survives long enough to complete the handshake.

### FoxCloud app fallback

Use the FoxCloud app's normal Wi-Fi configuration flow to change the
inverter's Wi-Fi network to the Pi AP SSID and passphrase.

For a new, never-cloud-paired inverter, the project has not yet validated
a fully cloud-free first commissioning. If your unit ships requiring the
FoxCloud app to do its initial association, complete that first with Cloud
Relay turned on; then switch the inverter to the Pi AP via either method
above.

## Installed Files

```text
/opt/foxess-local-cloud/                         application checkout
/opt/foxess-local-cloud/venv/                    Python virtualenv
/etc/foxess-local-cloud/config.json              daemon config
/etc/foxess-local-cloud/pi-ap.conf               AP/redirect config
/etc/foxess-local-cloud/wifi-credentials.txt     generated/provided AP key
/etc/dnsmasq.d/foxess-local-cloud.conf           inverter DHCP/DNS
/etc/hostapd/hostapd-foxess.conf                 inverter AP
/etc/NetworkManager/conf.d/foxess-local-cloud.conf
/usr/local/sbin/foxess-pi-ap                     AP/redirect helper
/usr/local/sbin/foxess-gateway-status           diagnostics
/etc/logrotate.d/foxess-local-cloud             event log rotation
/var/lib/foxess-local-cloud/                     generated cert/key
/var/log/foxess-local-cloud/events.jsonl         daemon events
```

Systemd units:

```text
foxess-pi-ap.service
foxess-hostapd.service
foxess-local-cloud.service
```

## Redirects

The AP helper creates an nftables table named `foxess_local_cloud`.

Default redirects:

```text
source:      inverter AP subnet, default 192.168.50.0/24
destination: any TCP/14431 destination reached by AP clients
target:      local daemon port 14431
```

`--foxess-cloud-ip` and `--foxess-cloud-host` are relay upstream hints for the
daemon config and status output. They do not limit which AP-client TCP/14431
connections are redirected.

NAT is enabled by default for non-redirected inverter traffic so relay mode can
reach the upstream LAN.

### Pinning the inverter IP (optional)

The gateway does not need a fixed inverter IP — the inverter dials the daemon and
the redirect matches the whole AP subnet, so a changing DHCP lease doesn't affect
telemetry or control. The daemon also enables TCP keepalive on the inverter
connection (`inverter_tcp_keepalive_seconds`, default 30 s) to keep a marginal
link warm and detect drops quickly.

If you still want a stable address (e.g. to run an external `arping` keepalive at
a known IP), add a dnsmasq reservation. Find the inverter's MAC from a current
lease (`cat /var/lib/misc/dnsmasq.leases` or `ip neigh show dev ap0`), then add a
line to `/etc/dnsmasq.d/foxess-local-cloud.conf`:

```text
dhcp-host=AA:BB:CC:DD:EE:FF,192.168.50.50
```

and `sudo systemctl restart dnsmasq`.

## Check Status

```bash
sudo foxess-gateway-status
sudo foxess-pi-ap status
systemctl status foxess-pi-ap foxess-hostapd foxess-local-cloud
journalctl -u foxess-local-cloud -n 100 --no-pager
tail -f /var/log/foxess-local-cloud/events.jsonl
```

`foxess-gateway-status` masks the stored Wi-Fi passphrase and reports service
state, AP/STA interfaces, associated inverter clients, DHCP leases, redirect
counters, the TCP/14431 listener, MQTT/relay config, and recent daemon events.

Expected daemon events:

```text
listen
connect
registration
bootstrap_ack
module_info
product_info
telemetry
mqtt_connected
```

### hostapd fails: "Name not unique on network"

If `foxess-hostapd` will not start and the log shows:

```text
Could not set interface ap0 flags (UP): Name not unique on network
nl80211 driver initialization failed.
```

the AP interface is sharing the station interface's MAC address. Both share one
radio, and the Wi-Fi firmware sorts received frames to a virtual interface by
destination MAC, so it refuses to bring up `ap0` with a duplicate address.
`foxess-pi-ap` detects this and gives `ap0` a distinct MAC automatically (only
when it collides — chips that already assign `ap0` a distinct MAC keep their
existing BSSID). Confirm it took with:

```bash
ip link show wlan0   # link/ether b8:27:eb:...
ip link show ap0     # link/ether ba:27:eb:... (must differ from wlan0)
```

If they still match, re-run `sudo foxess-pi-ap restart` and check the logs.

## MQTT And Friendly Names

MQTT credentials can be supplied during install:

```bash
sudo ./installer/install_pi_zero_gateway.sh \
  --mqtt-host mqtt-broker.local \
  --mqtt-username foxess \
  --mqtt-password 'replace-with-your-mqtt-password'
```

They are stored in:

```text
/etc/foxess-local-cloud/config.json
```

To assign a friendly name after the first telemetry identifies the serial:

```json
{
  "devices": {
    "YOUR_INVERTER_SERIAL": "PV Inverter"
  }
}
```

Then restart:

```bash
sudo systemctl restart foxess-local-cloud
```

On reinstall, omitted MQTT host/port/username/password values are preserved from
the existing config. Use `--no-mqtt` to clear MQTT settings explicitly.

Before overwriting the daemon config, the installer saves:

```text
/etc/foxess-local-cloud/config.json.bak
```

## Relay Mode

Relay mode forwards decrypted inverter traffic to the real FoxESS endpoint while
still publishing local MQTT telemetry. Use it when you want the FoxESS app/cloud
to continue receiving data during validation.

Disable relay for fully local operation:

```bash
sudo ./installer/install_pi_zero_gateway.sh --no-relay
```

## Inverter Control

Enabled by default. Exposes a writable `Active Power Limit` slider
(0–100 %) in Home Assistant. No periodic polling — the daemon reads
the current value once on the first telemetry frame so HA shows the
live setpoint as soon as the inverter is producing, then writes
whatever HA's slider sets and the response is stripped from the
bytes forwarded to FoxCloud.

To turn it off:

```bash
sudo ./installer/install_pi_zero_gateway.sh --disable-inverter-control
```

To re-enable after disabling:

```bash
sudo ./installer/install_pi_zero_gateway.sh --enable-inverter-control
```

What it does on the wire:

- Writes to `Active Power Limit` (Modbus holding register `0xCA5A`,
  slave 1, value 0–100 in whole percent) when HA publishes to
  `foxess_m1/<serial>/active_power_limit/set`.
- Reads `0xCA5A` once on the first telemetry frame to populate the HA state.
- Waits for the inverter's write-acknowledgement (configurable via
  `inverter_control.write_timeout_seconds`, default 3 s; round-trip is
  ~0.3 s on a healthy link) and publishes the outcome to
  `foxess_m1/<serial>/active_power_limit/result` (`confirmed` / `rejected` /
  `timeout` / `no_connection` / `error`), exposed as an `Active Power Limit Result`
  diagnostic sensor. The setpoint state is only updated on `confirmed`, so a
  control loop (e.g. Nulleinspeisung from your own fast power source) can tell
  whether a curtailment actually landed. See issue #43.
- Re-applies a setpoint that couldn't be confirmed. If a write returns
  anything but `confirmed` (typically `no_connection` when the inverter has
  dropped its session on a weak link), the desired value is kept and
  automatically re-sent once the inverter reconnects and its session settles
  (first telemetry frame). The latest setpoint wins, and a confirmed write
  stops the retry, so it isn't re-sent on every reconnect.
- Each injected request uses a 4-byte envelope device field with
  byte[3] = `0xAA` (our self-chosen transaction-stream marker); the
  inverter accepts it in parallel with the cloud's marker, and the
  echoed response is filtered out of the upstream forward.

Operational notes:

- The inverter handles one Modbus transaction at a time. Bursty
  writes (driving the slider rapidly) can briefly time out cloud-side
  reads — this is normal and self-corrects within a few seconds.
- Writes change the inverter's runtime config and are persisted to
  flash. Don't drive the slider faster than the inverter can settle
  (a few seconds between changes is plenty).

Only `Active Power Limit` is exposed for write — see the README FAQ
for why other installer-portal settings aren't, and why Modbus
polling doesn't give faster-than-push telemetry.

## Rollback

```bash
sudo ./installer/uninstall_pi_zero_gateway.sh
```

The uninstall script removes services, AP config, redirect setup, and the
application under `/opt`. It intentionally leaves config, generated certs, and
logs in:

```text
/etc/foxess-local-cloud
/var/lib/foxess-local-cloud
/var/log/foxess-local-cloud
```

Remove those manually if you want a full data wipe.

## Known Limitations

- The Pi Zero W has one Wi-Fi radio. If the upstream AP changes channel, the
  inverter AP may need a restart or a fixed upstream channel.
- The daemon publishes pushed telemetry frames. It does not actively poll the
  inverter faster than the inverter's own cadence.
- Q1 PV3/PV4 decoding exists in code but has not been validated on a real Q1
  inverter.
- A fully cloud-free first commissioning flow for a new inverter has not yet
  been validated.
- Relay mode depends on Linux `SO_ORIGINAL_DST` being available for redirected
  sockets. If original destination lookup fails and multiple upstreams are
  configured, the daemon logs `relay_no_upstream`.
