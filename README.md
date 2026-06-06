# FoxESS M1 Local Gateway

Local MQTT/Home Assistant gateway for FoxESS M1 (and potentially Q1) microinverters, designed to run
on a Raspberry Pi (tested with a Zero W) running Raspberry Pi OS Lite (Debian Trixie).

The gateway creates a small inverter-only Wi-Fi network, accepts the inverter's
local connection (TCP/14431), decodes pushed telemetry, and publishes it to MQTT
with Home Assistant discovery while being connected to your regular wifi as a
client. No FoxESS Cloud API key is needed.

![FoxESS local gateway architecture](docs/images/illustration.png)

## Why Use This

- No data leaves your network after initial setup.
- Optional relay mode to the Fox ESS cloud is supported.
- Inverter telemetry updates every 90 seconds.
- No polling of data from the FoxESS Cloud API which limits you to one update every 5 minutes.
- MQTT auto discovery creates Home Assistant sensors automatically, neatly bundled into one
  device per inverter.
- No Home Assistant HACS Add-ons needed
- Continue using your inverter even if the FoxESS cloud changes, is offline or gets disabled.
- Works with multiple inverters connected through the Pi AP at the same time, even if they use
  their own mesh network.

## What This Does

The Raspberry Pi runs three pieces:

- `hostapd`/`dnsmasq` for an inverter-only Wi-Fi AP.
- A narrow nftables redirect for TCP/14431 traffic from the inverter AP subnet.
- `foxess-local-cloud`, a Python daemon that decodes telemetry and publishes
  MQTT state.

Default topology:

```text
Home LAN / MQTT broker
        ^
        | wlan0
Raspberry Pi Zero W
        | ap0: FoxESS-Local
        v
FoxESS inverter Wi-Fi
```
## Screenshots

![MQTT auto discovered entities in Home Assistant](docs/images/screenshot_entities.png)
![Example graph in Home Assistant](docs/images/screenshot_graph.png)


## What You need
- A Raspberry Pi (old Zero W is fine) in reception range of your inverter(s) and your home wifi
- A recent wifi-only Fox ESS Solar inverter, e.g.
  - M1-600-E
  - M1-800-E
  - M1-1000-E
  - M1-1200-E
  - Q1-1600-E (not confirmed)
  - Q1-2000-E (not confirmed)
  - Q1-2400-E (not confirmed)
  - Q1-2500-E (not confirmed)
- The FoxCloud 2.0 app for initial wifi connection (alternatively try the Solakon App)
- Home Assistant with the Mosquitto Broker app

## Tested

Tested:

- Raspberry Pi Zero W running Raspberry Pi OS Lite (Trixie).
- FoxESS M1-800-E microinverter.
- M1 two-string PV telemetry:
  - PV power, voltage, and current for PV1/PV2.
  - AC power, voltage, current, and frequency.
  - inverter temperature.
  - total generation.
- MQTT retain and Home Assistant MQTT discovery.
- Optional cloud relay mode while still decoding local telemetry.

Not yet tested (please let me know if you were able to test it):

- Brand-new, never-commissioned inverter setup without using the FoxESS app.
- Q1 devices with four PV inputs. The decoder has model-aware PV3/PV4 support,
  but this still needs validation on a real Q1 inverter.
- Other FoxESS inverter families (likely won’t work).
- Active polling to enable faster than the inverter's 90 seconds telemetry cadence.

## Home Assistant Prerequisites

- Install the Home Assistant [Mosquitto Broker app](https://www.home-assistant.io/integrations/mqtt/)
- Create additional mqtt credentials in Mosquitto for this gateway (to be used during the install described below)
- Note your MQTT broker IP address or host name.

## Install On Raspberry Pi

Start with Raspberry Pi OS Lite (Trixie). Configure normal Wi-Fi and SSH using Raspberry
Pi Imager, then ssh into the pi and clone this repository:

```bash
git clone https://github.com/040medien/foxess_local_gateway
cd foxess_local_gateway
```

Then run:

```bash
sudo ./installer/install_pi_zero_gateway.sh \
  --ap-ssid FoxESS-Local \
  --mqtt-host mqtt-broker.local \
  --mqtt-username foxess \
  --mqtt-password 'your-mqtt-password'
```

If `--ap-passphrase` is omitted, the installer generates a random 32-character
Wi-Fi passphrase for the inverter-facing wifi and stores it in:

```text
/etc/foxess-local-cloud/wifi-credentials.txt
```

The installer prints the passphrase every time it runs.

Useful options:

```text
--ap-passphrase PASSPHRASE
--ap-channel 6
--mqtt-host HOST
--mqtt-username USER
--mqtt-password PASS
--no-mqtt
--relay
--no-relay
--foxess-cloud-ip IP
--foxess-cloud-host HOST
--dry-run
```

Fresh installs keep cloud relay disabled. With relay disabled, telemetry remains
local. When installed with `--relay`, the gateway forwards the decrypted session 
to FoxESS Cloud while still publishing MQTT locally.

The redirect is intentionally broad within the inverter AP: any AP client
connection to TCP/14431 is sent to the local daemon. That avoids depending on a
fixed FoxESS cloud IP or DNS answer, while still leaving the rule scoped to the
isolated inverter Wi-Fi subnet and the FoxESS telemetry port.

## Connect An Inverter

After installation:

```bash
sudo foxess-gateway-status
```

Note the AP SSID and passphrase.

For an already-installed inverter, use the FoxESS or Solakon mobile app
to change its Wi-Fi network to the Pi AP. For a new inverter, put its Wi-Fi
into its normal pairing/config mode and enter the Pi AP SSID and passphrase
when prompted.

The out-of-box path for a never-cloud-paired inverter has not been validated
yet. If initial commissioning requires the FoxESS app and an account, complete
that step first while having Cloud Relay mode turned on (see below), and provide
the Wi-Fi configuration of the Pi AP. You can then turn the Cloud Relay off again.

## Home Assistant

The gateway publishes Home Assistant MQTT discovery configs after the first
telemetry frame from each inverter serial.

Example topics:

```text
foxess_m1/<serial>/state
foxess_m1/<serial>/0/power
foxess_m1/<serial>/0/ac/power
foxess_m1/<serial>/0/ac/export_power
foxess_m1/<serial>/0/ac/voltage
foxess_m1/<serial>/0/temperature
foxess_m1/<serial>/1/power
foxess_m1/<serial>/2/power
```

To change the friendly device names, edit:

```text
/etc/foxess-local-cloud/config.json
```

Example:

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

## Status And Logs

```bash
sudo foxess-gateway-status
systemctl status foxess-pi-ap foxess-hostapd foxess-local-cloud
journalctl -u foxess-local-cloud -n 100 --no-pager
tail -f /var/log/foxess-local-cloud/events.jsonl
```

Expected daemon events:

```text
listen
connect
registration
bootstrap_ack
product_info
telemetry
mqtt_connected
```

## Configuration

Main config:

```text
/etc/foxess-local-cloud/config.json
```

AP/redirect config:

```text
/etc/foxess-local-cloud/pi-ap.conf
```

Generated cert/key:

```text
/var/lib/foxess-local-cloud/
```

Logs:

```text
/var/log/foxess-local-cloud/events.jsonl
```

## Cloud Relay

Relay mode is optional.

Use relay mode when you want the FoxESS Cloud/app to keep receiving data while
you validate the local gateway:

```bash
sudo ./installer/install_pi_zero_gateway.sh --relay
```

Disable it for local-only operation:

```bash
sudo ./installer/install_pi_zero_gateway.sh --no-relay
```

## FAQ

### Why does this work?

The inverter's communication with FoxESS Cloud is TLS-encrypted, but the
inverter does not validate the server certificate. That means it accepts
any cert, including a self-signed one we generate locally. The Pi
presents its own cert with the same subject as the FoxESS cloud cert
(`CN=monitor`), terminates the TLS session, decodes the binary
telemetry, and (in relay mode) re-encrypts and forwards to FoxESS Cloud.

The binary protocol itself was reverse-engineered by capturing the
cleartext stream and correlating fields with what the FoxESS app and
Modbus implementations expose. A handful of register offsets in each
238-byte telemetry frame still don't have confirmed semantics — they
are logged as `raw_u16_*` so they can be investigated as inverters
accumulate more lifetime energy or hit error states.

### Will it run in Docker or as a Home Assistant add-on?

The service code (`foxess_local_cloud`) is plain Python and will run
anywhere Python 3.10+ runs, including a container or HA add-on. The
catch is the **redirect** layer: the inverter wants to connect to a
FoxESS cloud IP on TCP/14431, so something on your network has to
intercept that traffic and route it to the daemon.

This project handles that on a Raspberry Pi by creating an isolated
Wi-Fi AP for the inverter and using nftables to redirect AP-side
TCP/14431 to the local daemon. If you run the daemon elsewhere, you
would have to provide the redirect yourself — for example with an
OpenWRT router (policy-based routing plus DNAT) or a managed switch
with VLAN/policy routing. There is no plug-and-play HA add-on path
today.

## Development

Run tests:

```bash
python3 -m unittest
```

Installer dry-run without touching the system:

```bash
./installer/install_pi_zero_gateway.sh \
  --dry-run \
  --preview-dir /tmp/foxess-preview \
  --skip-app-copy \
  --non-interactive
```

## Uninstall

```bash
sudo ./installer/uninstall_pi_zero_gateway.sh
```

The uninstall script removes services, AP config, redirect setup, and the app
under `/opt`. It intentionally leaves config, generated certs, and logs under
`/etc`, `/var/lib`, and `/var/log` so you can inspect or reuse them.
