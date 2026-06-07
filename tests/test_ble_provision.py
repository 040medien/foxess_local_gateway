"""Unit tests for ``foxess_local_cloud.ble_provision``.

These tests cover the pure frame-encoding/decoding helpers (no BLE required).
The captured byte fixtures originate from an HCI snoop of a FoxCloud-compatible
app provisioning a real M1 inverter; sensitive material (the actual WPA
passphrase) is **not** used here.
"""

from __future__ import annotations

import unittest

from foxess_local_cloud import protocol as p
from foxess_local_cloud.ble_provision import (
    FN_COMMIT,
    FN_SCAN,
    FN_SELECT,
    FN_SET_CREDS,
    MFG_SIG,
    DiscoveredInverter,
    ScannedNetwork,
    derive_device_tail,
    make_commit,
    make_scan_request,
    make_select_request,
    make_set_credentials,
    parse_scan_response,
)


# Device tail observed in a captured BLE session. Used as a test fixture
# so the encoder's output can be compared to the captured bytes byte-for-byte.
CAPTURED_TAIL = b"\x6a\x25\x28"


def _registration_frame_with_tail(tail: bytes, serial: str = "60M2801056BR589") -> p.Frame:
    """Build a registration frame whose bootstrap-derived tail equals ``tail``."""
    raw_tail_int = (int.from_bytes(tail, "big") + 0x71) & 0xFFFFFF
    device = b"\x2a" + raw_tail_int.to_bytes(3, "big")
    serial_bytes = serial.encode("ascii")
    payload = (
        b"\x01\x00\x01\x31"
        + bytes([len(serial_bytes)])
        + serial_bytes
        + b"\x05v1.31\x00"
    ).ljust(28, b"\x00")
    raw = p.make_frame(b"\x7e\x7e", device, 0x8F, payload, b"\xe7\xe7")
    return p.parse_frame(raw)


class TestDeviceTailDerivation(unittest.TestCase):
    def test_matches_captured_value(self):
        registration = _registration_frame_with_tail(CAPTURED_TAIL)
        self.assertEqual(derive_device_tail(registration), CAPTURED_TAIL)


class TestFrameEncoders(unittest.TestCase):
    def test_scan_request_matches_captured_bytes(self):
        expected = bytes.fromhex("7f7f3a6a2528a1000106cc3bf7f7")
        self.assertEqual(make_scan_request(CAPTURED_TAIL), expected)

    def test_select_request_matches_captured_bytes(self):
        expected = bytes.fromhex("7f7f3a6a2528ad0001098f6ff7f7")
        self.assertEqual(make_select_request(CAPTURED_TAIL), expected)

    def test_commit_matches_captured_bytes(self):
        expected = bytes.fromhex("7f7f3a6a2528b1000104493af7f7")
        self.assertEqual(make_commit(CAPTURED_TAIL), expected)

    def test_set_credentials_round_trips(self):
        ssid = "TestNetwork"
        passphrase = "passphrase01"
        frame_bytes = make_set_credentials(CAPTURED_TAIL, ssid, passphrase)
        parsed = p.parse_frame(frame_bytes)
        self.assertEqual(parsed.start, b"\x7f\x7f")
        self.assertEqual(parsed.end, b"\xf7\xf7")
        self.assertEqual(parsed.device, b"\x3b" + CAPTURED_TAIL)
        self.assertEqual(parsed.func, FN_SET_CREDS)
        self.assertTrue(parsed.valid_crc)
        self.assertEqual(
            parsed.payload,
            MFG_SIG
            + bytes([len(ssid)]) + ssid.encode()
            + bytes([len(passphrase)]) + passphrase.encode(),
        )

    def test_set_credentials_envelope_matches_capture(self):
        """For the SSID actually broadcast by our Pi (`FoxESS-Local`, 12 bytes)
        and a 32-byte passphrase, the encoded envelope (everything except the
        passphrase bytes and the CRC) matches what the reference mobile app produced."""
        ssid = "FoxESS-Local"
        passphrase = "a" * 32
        frame_bytes = make_set_credentials(CAPTURED_TAIL, ssid, passphrase)
        # Compare the leading header + SSID block byte-for-byte against
        # what was captured. Everything up to and including the SSID is
        # deterministic; the captured tail (passphrase + CRC) differs only
        # in the passphrase content and the recomputed CRC.
        capture_header_hex = (
            "7f7f"             # start
            "3b6a2528"         # device (alt high byte 0x3b for set-creds)
            "ae"               # func
            "0032"             # payload length BE = 50
            "03f3ba40"         # mfg signature
            "0c"               # ssid_len = 12
            "466f784553532d4c6f63616c"  # "FoxESS-Local"
            "20"               # pw_len = 32
        )
        prefix = bytes.fromhex(capture_header_hex)
        self.assertTrue(
            frame_bytes.startswith(prefix),
            f"frame envelope did not match capture: got {frame_bytes[:len(prefix)].hex()}",
        )
        self.assertTrue(frame_bytes.endswith(b"\xf7\xf7"))

    def test_ssid_too_long_rejected(self):
        with self.assertRaises(ValueError):
            make_set_credentials(CAPTURED_TAIL, "x" * 33, "passphrase01")

    def test_ssid_empty_rejected(self):
        with self.assertRaises(ValueError):
            make_set_credentials(CAPTURED_TAIL, "", "passphrase01")

    def test_passphrase_too_short_rejected(self):
        with self.assertRaises(ValueError):
            make_set_credentials(CAPTURED_TAIL, "Network", "short")

    def test_passphrase_too_long_rejected(self):
        with self.assertRaises(ValueError):
            make_set_credentials(CAPTURED_TAIL, "Network", "x" * 64)

    def test_scan_request_is_parseable(self):
        parsed = p.parse_frame(make_scan_request(CAPTURED_TAIL))
        self.assertTrue(parsed.valid_crc)
        self.assertEqual(parsed.func, FN_SCAN)
        self.assertEqual(parsed.payload, b"\x06")

    def test_select_request_is_parseable(self):
        parsed = p.parse_frame(make_select_request(CAPTURED_TAIL))
        self.assertTrue(parsed.valid_crc)
        self.assertEqual(parsed.func, FN_SELECT)
        self.assertEqual(parsed.payload, b"\x09")

    def test_commit_is_parseable(self):
        parsed = p.parse_frame(make_commit(CAPTURED_TAIL))
        self.assertTrue(parsed.valid_crc)
        self.assertEqual(parsed.func, FN_COMMIT)
        self.assertEqual(parsed.payload, b"\x04")


class TestScanResponseParser(unittest.TestCase):
    def _scan_response_frame(self, networks: list[tuple[str, int]]) -> p.Frame:
        body = b"\x06" + bytes([len(networks)])
        for ssid, rssi in networks:
            ssid_bytes = ssid.encode()
            rec_len = 1 + len(ssid_bytes)
            rssi_byte = rssi & 0xFF
            body += bytes([rec_len, rssi_byte]) + ssid_bytes
        raw = p.make_frame(b"\x7f\x7f", b"\x3a" + CAPTURED_TAIL, FN_SCAN, body, b"\xf7\xf7")
        return p.parse_frame(raw)

    def test_parses_single_network(self):
        frame = self._scan_response_frame([("FoxESS-Local", -54)])
        nets = parse_scan_response(frame)
        self.assertEqual(nets, [ScannedNetwork(ssid="FoxESS-Local", rssi=-54)])

    def test_parses_multiple_networks(self):
        frame = self._scan_response_frame([
            ("AP-A", -50),
            ("AP-B-longer-name", -75),
            ("AP-C", -90),
        ])
        nets = parse_scan_response(frame)
        self.assertEqual([n.ssid for n in nets], ["AP-A", "AP-B-longer-name", "AP-C"])
        self.assertEqual([n.rssi for n in nets], [-50, -75, -90])

    def test_empty_list_returns_empty(self):
        frame = self._scan_response_frame([])
        self.assertEqual(parse_scan_response(frame), [])

    def test_wrong_family_rejected(self):
        raw = p.make_frame(b"\x7e\x7e", b"\x00" * 4, FN_SCAN, b"\x06\x00", b"\xe7\xe7")
        frame = p.parse_frame(raw)
        with self.assertRaises(ValueError):
            parse_scan_response(frame)

    def test_wrong_func_rejected(self):
        raw = p.make_frame(b"\x7f\x7f", b"\x00" * 4, 0xAB, b"\x06\x00", b"\xf7\xf7")
        frame = p.parse_frame(raw)
        with self.assertRaises(ValueError):
            parse_scan_response(frame)

    def test_truncated_records_dropped_gracefully(self):
        # Declare 2 records but only provide bytes for 1
        body = b"\x06\x02\x05\xc1abcd"
        raw = p.make_frame(b"\x7f\x7f", b"\x3a" + CAPTURED_TAIL, FN_SCAN, body, b"\xf7\xf7")
        frame = p.parse_frame(raw)
        nets = parse_scan_response(frame)
        self.assertEqual([n.ssid for n in nets], ["abcd"])

    def test_positive_rssi_decoded_as_unsigned(self):
        # Some near-field captures show positive RSSI bytes (0..127)
        frame = self._scan_response_frame([("CloseAP", 10)])
        nets = parse_scan_response(frame)
        self.assertEqual(nets[0].rssi, 10)


class TestDiscoveredInverter(unittest.TestCase):
    def test_serial_extracted_from_name(self):
        d = DiscoveredInverter(address="90:E5:B1:44:05:CE", name="MI_60M28010563R686", rssi=-45)
        self.assertEqual(d.serial, "60M28010563R686")

    def test_serial_empty_when_name_not_mi_prefixed(self):
        d = DiscoveredInverter(address="00:11:22:33:44:55", name="OtherDevice", rssi=-60)
        self.assertEqual(d.serial, "")


if __name__ == "__main__":
    unittest.main()
