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

Two features in this gateway are unavailable to FoxESS cloud users:

- A writable `Active Power Limit` Home Assistant slider that drives a
  local Modbus write straight to the inverter. No cloud round-trip.
- Faster-than-cloud telemetry polling, by injecting local Modbus reads
  at any cadence (5 s tested) and stripping the responses from the
  bytes forwarded to FoxCloud.

Both are off by default. See *Inverter Control (opt-in)* below for how
to enable them and what they do on the wire.

## Networking Model

The Pi Zero W has one Wi-Fi radio. AP mode and client mode must use the same
radio channel in practice. The installer keeps `wlan0` managed by
NetworkManager, creates a virtual `ap0` interface for the inverter AP, and marks
only `ap0` unmanaged.

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
--foxess-cloud-ip 203.0.113.10
--foxess-cloud-host api.example.invalid
--dry-run
--preview-dir /tmp/foxess-install-preview
--skip-app-copy
```

Fresh installs leave relay disabled. Re-running the installer preserves an
existing `relay.enabled=true` setting unless `--no-relay` is passed.

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

This drops into an interactive loop: it scans for nearby M1 inverters,
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

## Inverter Control (opt-in)

Off by default. Enable to expose a writable `Active Power Limit` slider
(0–100%) in Home Assistant and to inject periodic Modbus reads of the
input-register telemetry block (faster updates than the inverter's own
~90s push cadence). Responses to these injected requests are stripped
from the bytes forwarded to FoxCloud, so the cloud sees no extra traffic.

```bash
sudo ./installer/install_pi_zero_gateway.sh \
  --enable-inverter-control \
  --inverter-control-poll-interval 30
```

To turn it off again:

```bash
sudo ./installer/install_pi_zero_gateway.sh --disable-inverter-control
```

What it does on the wire:

- Writes to `Active Power Limit` (Modbus holding register `0xCA5A`,
  slave 1, value 0–100 in whole percent) when HA publishes to
  `foxess_m1/<serial>/active_power_limit/set`.
- Reads `0x277E` for 28 input registers every poll interval.
- Each injected request uses a 4-byte envelope device field starting
  with `0x7f`, so the inverter's echoed response is recognisable as
  ours regardless of the bit-7 toggle the inverter applies, and is
  filtered out of the upstream forward.

Operational notes:

- The inverter handles one Modbus transaction at a time. Bursty writes
  (e.g. driving the slider rapidly) can briefly time out cloud-side
  reads — this is normal and self-corrects within a few seconds.
- Writes change the inverter's runtime config and are persisted. Don't
  drive the slider faster than the inverter can settle (a few seconds
  between changes is plenty).
- Polling more often than every ~10 seconds is unlikely to give you
  meaningfully fresher data and increases the chance of colliding with
  cloud-side reads.

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
