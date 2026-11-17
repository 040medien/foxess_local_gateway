# Validate a KH7 in relay mode

The experimental KH7 sensors use the FoxESS datalogger's existing Wi-Fi
connection. Keep cloud relay enabled so the same inverter samples reach
FoxCloud and the local gateway. Configure MQTT as usual. After the gateway
recognizes a KH7 product-info frame and receives live data, Home Assistant
discovers the KH7 device and its **Experimental** diagnostic sensors. The two
live-data uploads in the original capture were about five minutes apart.

1. In Home Assistant, open the KH7 device under the MQTT integration.
2. Open the device's history in FoxCloud or the app and record the timestamps
   and values. Note the time zone and prefer historical points over the app's
   current display.
3. On the Pi, find the same samples by timestamp. The `ts` value is Unix time
   in seconds.

   ```sh
   sudo journalctl -u foxess-local-cloud --since '1 hour ago' --no-pager \
     | grep '"event":"telemetry"' | grep '"model":"KH7"'
   ```

4. Compare several points, including changes in solar output and grid direction.
   Report the timestamp, time zone, app field name and value, and matching
   diagnostic field and value. Remove serial numbers and account information
   before sharing.

Offsets below are zero-based within the 425-byte payload. All readings are
candidates. The two frequency candidates are close together in the capture,
and the AC power channels and sign conventions still need to be established.

| Candidate | Byte offsets | Scale |
| --- | --- | --- |
| PV1 voltage, current, power | 33, 35, 37 | 0.1 V, 0.1 A, 1 W |
| PV2 voltage, current, power | 39, 41, 43 | 0.1 V, 0.1 A, 1 W |
| Total PV power | 57 | 1 W |
| AC voltage candidates | 51, 61 | 0.1 V |
| AC frequency candidates | 59, 69 | 0.01 Hz |
| AC power candidates | 71, 75 | Signed 32-bit, 1 W |

In both original samples, the word at offset 57 equals the sum of the
candidate PV1 and PV2 power words. This is a check on the mapping to compare
against FoxCloud. Battery measurements need more evidence before they can
be exposed. The gateway waits at least six minutes before marking KH7
telemetry stale when the freshness timeout is enabled.
