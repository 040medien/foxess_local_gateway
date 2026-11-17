# Experimental KH7 relay monitoring

**Date** November 17, 2026

**Status** Experimental implementation

**Issue** [KH hybrid inverter #63](https://github.com/040medien/foxess_local_gateway/issues/63)

## Goal

In relay mode, recognize the reported FoxESS KH7 and show in Home Assistant
whether its live-data frames are arriving. Record the identified frames for
correlation with readings from the inverter. FoxCloud continues to handle the
session and receive the original frames.

## Scope and evidence

Replaying the reporter's capture through the current parser produced 78
inverter-to-cloud and 35 cloud-to-inverter frames with valid checksums. The
inverter sent a 141-byte product-info candidate and a 425-byte live-data
candidate at approximately five-minute intervals. The 141-byte payload contains
the model label `KH7`; both blocks carry the same 15-byte inverter identifier.
The capture contains a separate 135-byte frame every 30 seconds. The gateway's
existing M1/Q1 bootstrap and 238-byte telemetry decoder do not recognize these
messages.

The capture includes two instances of the 425-byte block and lacks paired
readings for those instances. It supports device identification and frame freshness. Publish candidate PV
and AC readings as experimental diagnostics for comparison with FoxCloud.
The field offsets, units, and signs still need confirmation.

## Design

- Observe valid inverter-to-cloud frames at the existing relay entry point.
  Identify the KH7 using the `7f7f` family, device type byte `0x01`, 141-byte
  payload, and exact KH7 model field. Validate the leading identifier as a
  15-byte ASCII serial suitable for MQTT topics.
- Attribute `7f7f` frames with type byte `0x02` and a 425-byte payload only
  when their serial matches a KH7 identity observed on the same connection.
  The trailing device bytes and function byte vary within the capture. Replay
  the product and live-data pair in capture order with varying envelope bytes.
- Publish the device's existing Home Assistant **Telemetry Connected**
  diagnostic from accepted live-data frames. Use the existing gateway and
  inverter availability topics and telemetry watchdog, with enough time for
  the observed five-minute interval plus jitter. The diagnostic represents
  receipt of the 425-byte block. A 30-second heartbeat alone does not refresh it.
- Publish candidate measurements as diagnostic entities without long-term statistics. See the [validation guide](../KH7_VALIDATION.md) for offsets and comparison steps.
- Emit structured identification and sample events. Keep raw payload evidence
  in the existing capture logs for measurement correlation. Use synthetic serials
  and frame data in committed tests.
- Relay mode supplies the handshake. The KH7 monitoring path is read-only and
  applies to relay traffic for the observed KH7 layout.

## Other approaches

A first release with electrical sensors would provide immediate energy
monitoring. It depends on paired FoxCloud, inverter display, or Modbus readings
to validate the fields published to Home Assistant. Comparing captures across
changing PV input, battery charge and discharge, and grid import and export
would resolve that dependency.

Direct Modbus monitoring would use a separate connection and a firmware-specific
register map. It would require access to the inverter's Modbus interface and
would not monitor the relay stream reported in the issue.

## Verification

Replay synthetic frames through the relay path and verify the original bytes
reach the upstream writer. Verify MQTT device discovery, freshness, transition
to stale, and recovery. Verify CRC failures, missing identification, serial
mismatches, unsupported models, and local-only sessions produce no KH7 MQTT
state. Replay the private capture locally to check that both observed live-data
frames are attributed to the KH7. Run the gateway test suite and verify the
existing behavior on the live M1 gateway before opening the PR.
