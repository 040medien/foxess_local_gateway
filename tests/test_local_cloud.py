from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foxess_local_cloud.config import AppConfig, MqttConfig, RelayConfig, load_config
from foxess_local_cloud.mqtt import MqttPublisher, metadata_for
from foxess_local_cloud.protocol import (
    BootstrapResponder,
    extract_frames,
    is_registration,
    is_telemetry,
    make_frame,
    product_info,
    registration_serial,
)
from foxess_local_cloud.server import FOXESS_UPSTREAM_CERT_SHA256, FoxessLocalCloud, Session, check_upstream_cert
from foxess_local_cloud.telemetry import FAULT_CODE_NAMES, Telemetry, decode_telemetry, fault_code_for, fault_code_message_for, is_known_fault_code, nonzero_u16_words, u32_wordswapped


ROOT = Path(__file__).resolve().parents[1]
TEST_SERIAL = "TESTM1SERIAL001"


def registration_frame(serial: str = TEST_SERIAL, marker: bytes = b"\x31") -> bytes:
    serial_bytes = serial.encode("ascii")
    payload = b"\x01\x00\x01" + marker + bytes([len(serial_bytes)]) + serial_bytes + b"\x05v1.31\x00"
    payload = payload.ljust(28, b"\x00")
    return make_frame(b"\x7e\x7e", b"\x2a\x6a\x20\xe8", 0x8F, payload, b"\xe7\xe7")


def product_info_frame(model: str = "M1-800-E") -> bytes:
    payload = bytearray(108)
    payload[0:6] = b"M1V180"
    payload[32:34] = b"M1"
    payload[42 : 42 + len(model)] = model.encode("ascii")
    payload[76:80] = b"1.80"
    return make_frame(b"\x7e\x7e", b"\x01\x00\x00\x00", 0x00, bytes(payload), b"\xe7\xe7")


def module_info_frame(module_id: str = "M10200") -> bytes:
    payload = bytearray(38)
    payload[0 : len(module_id)] = module_id.encode("ascii")
    return make_frame(b"\x7e\x7e", b"\x06\x00\x00\x00", 0x00, bytes(payload), b"\xe7\xe7")


def telemetry_payload() -> bytes:
    payload = bytearray(238)

    def put_u16(offset: int, value: int) -> None:
        payload[offset : offset + 2] = value.to_bytes(2, "big")

    def put_s16(offset: int, value: int) -> None:
        payload[offset : offset + 2] = value.to_bytes(2, "big", signed=True)

    put_u16(0, 0xFFFF)
    put_s16(2, -123)
    put_s16(6, 123)
    put_u16(12, int(230.0 * 32))
    put_u16(14, int(0.5 * 512))
    put_u16(16, int(50.0 * 128))
    put_u16(36, int(36.0 * 256))
    put_u16(38, int(1.5 * 512))
    put_u16(40, 54)
    put_u16(42, int(37.0 * 256))
    put_u16(44, int(1.4 * 512))
    put_u16(46, 49)
    put_u16(62, int(25.0 * 32))
    put_u16(66, 7)
    put_u16(70, int(10.0 * 128))
    put_u16(96, 1000)
    put_u16(154, 4)
    put_u16(156, 2)
    return bytes(payload)


def telemetry_frame() -> bytes:
    return make_frame(b"\x7e\x7e", b"\x02\x00\x00\x00", 0x00, telemetry_payload(), b"\xe7\xe7")


def sample_telemetry(serial: str = TEST_SERIAL, model: str | None = "M1-800-E") -> Telemetry:
    return Telemetry(
        serial=serial,
        model=model,
        r_power_w=1,
        export_power_w=1,
        r_voltage_v=230.0,
        r_current_a=0.1,
        r_frequency_hz=50.0,
        pv_power_w=2,
        pv1_power_w=1,
        pv1_voltage_v=30.0,
        pv1_current_a=0.1,
        pv2_power_w=1,
        pv2_voltage_v=30.0,
        pv2_current_a=0.1,
        inverter_temperature_c=25.0,
        generation_kwh=1.0,
        export_total_kwh=1.0,
        fault_active=False,
        operating_state="running",
        operating_state_code=4,
        sequence=1,
        raw_u16_000=0,
        raw_u16_002=0,
        raw_u16_096=0,
        raw_u16_098=0,
        raw_u16_100=0,
        raw_u16_102=0,
        raw_u16_104=0,
        raw_u16_106=0,
        raw_u16_154=0,
        raw_u16_156=0,
    )


class FakeThread:
    """Stand-in for paho's network-loop thread so loop_is_running() is testable."""

    def __init__(self) -> None:
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive


class FakeMqttClient:
    def __init__(self, fail_connect: bool = False, publish_rc: int = 0) -> None:
        self.fail_connect = fail_connect
        self.publish_rc = publish_rc
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.published: list[tuple[str, str, bool]] = []
        # Mirrors paho: None until loop_start, a live thread after, None again
        # after loop_stop. simulate_loop_death() leaves it set but not alive.
        self._thread: FakeThread | None = None

    def simulate_loop_death(self) -> None:
        if self._thread is not None:
            self._thread._alive = False

    def reconnect_delay_set(self, **kwargs: object) -> None:
        self.calls.append(("reconnect_delay_set", (), kwargs))

    def username_pw_set(self, *args: object) -> None:
        self.calls.append(("username_pw_set", args, {}))

    def will_set(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("will_set", args, kwargs))

    def subscribe(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("subscribe", args, kwargs))

    def connect_async(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("connect_async", args, kwargs))
        if self.fail_connect:
            raise OSError("broker unavailable")

    def loop_start(self) -> None:
        self.calls.append(("loop_start", (), {}))
        self._thread = FakeThread()

    def loop_stop(self) -> None:
        self.calls.append(("loop_stop", (), {}))
        self._thread = None

    def disconnect(self) -> None:
        self.calls.append(("disconnect", (), {}))

    def publish(self, topic: str, payload: str, retain: bool = False) -> object:
        self.published.append((topic, payload, retain))

        class Result:
            rc = self.publish_rc

        return Result()


class FakeStreamWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None


class LocalCloudProtocolTest(unittest.TestCase):
    def test_config_loads_relay_and_mqtt(self) -> None:
        cfg = load_config(ROOT / "local-cloud.example.json")
        self.assertFalse(cfg.relay.enabled)
        self.assertEqual(cfg.relay.upstreams["8.209.116.72"], ("8.209.116.72", 14431))
        self.assertEqual(cfg.devices, {})
        self.assertTrue(cfg.mqtt.retain)
        self.assertEqual(cfg.mqtt.expire_after_seconds, 300)
        self.assertFalse(cfg.mqtt.debug)

    def test_bootstrap_responder_matches_expected_shape(self) -> None:
        raw_frames = [
            registration_frame(),
            make_frame(b"\x7e\x7e", b"\x2c\x6a\x20\xe7", 0x91, b"\xe1\x08\x05\x00", b"\xe7\xe7"),
            make_frame(b"\x7e\x7e", b"\x2b\x6a\x20\xe7", 0x91, b"\x00", b"\xe7\xe7"),
        ]
        buffer = bytearray(b"".join(raw_frames))
        frames = extract_frames(buffer)
        self.assertEqual(len(frames), 3)
        self.assertTrue(is_registration(frames[0]))
        self.assertEqual(registration_serial(frames[0]), TEST_SERIAL)

        responder = BootstrapResponder()
        responses = [responder.response_for(frame) for frame in frames]
        self.assertEqual([len(response or b"") for response in responses], [18, 13, 14])

    def test_q1_registration_variant_is_accepted(self) -> None:
        frame = extract_frames(bytearray(registration_frame(marker=b"\x30")))[0]
        self.assertTrue(is_registration(frame))
        self.assertEqual(registration_serial(frame), TEST_SERIAL)

    def test_telemetry_decode_synthetic_frame(self) -> None:
        frame = extract_frames(bytearray(telemetry_frame()))[0]
        self.assertTrue(is_telemetry(frame))
        telemetry = decode_telemetry(frame.payload, TEST_SERIAL, "M1-800-E")
        self.assertEqual(telemetry.serial, TEST_SERIAL)
        self.assertEqual(telemetry.pv_power_w, 103)
        self.assertEqual(telemetry.export_power_w, telemetry.r_power_w)
        self.assertEqual(telemetry.r_voltage_v, 230.0)
        self.assertEqual(telemetry.generation_kwh, 10.0)
        self.assertEqual(telemetry.operating_state, "running")
        self.assertEqual(telemetry.operating_state_code, 4)

    def test_generation_uses_word_swapped_u32_counter(self) -> None:
        payload = bytearray(telemetry_payload())
        payload[70:72] = (1).to_bytes(2, "big")
        payload[72:74] = (1).to_bytes(2, "big")

        telemetry = decode_telemetry(bytes(payload), TEST_SERIAL, "M1-800-E")

        self.assertEqual(u32_wordswapped(payload, 70), 65537)
        self.assertEqual(telemetry.generation_kwh, round(65537 / 128.0, 3))

    def test_nonzero_u16_words_uses_byte_offset_keys(self) -> None:
        words = nonzero_u16_words(telemetry_payload())

        self.assertEqual(words["000"], 65535)
        self.assertEqual(words["002"], 65413)
        self.assertEqual(words["154"], 4)
        self.assertNotIn("004", words)

    def test_module_info_frame_extracts_module_id(self) -> None:
        from foxess_local_cloud.protocol import is_module_info, module_info
        frame = extract_frames(bytearray(module_info_frame()))[0]
        self.assertTrue(is_module_info(frame))
        self.assertEqual(module_info(frame), "M10200")

    def test_product_info_frame_extracts_model(self) -> None:
        frame = extract_frames(bytearray(product_info_frame()))[0]
        info = product_info(frame)
        self.assertEqual(info["model"], "M1-800-E")
        self.assertEqual(info["family"], "M1")
        self.assertEqual(info["firmware"], "1.80")

    def test_product_info_strips_control_bytes_from_model(self) -> None:
        frame = extract_frames(bytearray(product_info_frame("\x01Q1-E")))[0]
        info = product_info(frame)
        self.assertEqual(info["model"], "Q1-E")

    def test_fault_code_for_known_tuple_returns_foxess_codes(self) -> None:
        self.assertEqual(fault_code_for((4, 20, 28, 24)), "4156,4157")

    def test_fault_code_for_ac_under_voltage_tuple(self) -> None:
        # One AC Under Voltage episode walks the tuple as the fault re-logs;
        # token-set matching covers every step including the full (4,4,4,4).
        for tuple_ in ((4, 0, 0, 0), (4, 4, 0, 0), (4, 4, 4, 0), (4, 4, 4, 4)):
            self.assertEqual(fault_code_for(tuple_), "4158")
            self.assertEqual(fault_code_message_for(fault_code_for(tuple_)), "AC Under Voltage")

    def test_fault_code_for_is_token_order_independent(self) -> None:
        self.assertEqual(fault_code_for((24, 28, 20, 4)), "4156,4157")

    def test_is_known_fault_code(self) -> None:
        self.assertTrue(is_known_fault_code("4158"))
        self.assertTrue(is_known_fault_code("4156,4157"))
        self.assertFalse(is_known_fault_code("raw:04-00-00-00"))
        self.assertFalse(is_known_fault_code(""))

    def test_fault_code_for_unknown_tuple_returns_raw_hex(self) -> None:
        result = fault_code_for((4, 33, 33, 0))
        self.assertTrue(result.startswith("raw:"))
        self.assertIn("21", result)  # 33 = 0x21

    def test_mesh_root_frame_detected(self) -> None:
        from foxess_local_cloud.protocol import is_mesh_root_frame
        # Payload mirrors the captured root frame: declares the AP MAC + beacon mfg sig.
        payload = bytes.fromhex("01 05 01 01 00 06 ba 27 eb 5d 8a a1 03 f3 ba 40 01 00 04 00 00 00 ee".replace(" ", ""))
        raw = make_frame(b"\x7f\x7f", b"\x3f\x6a\x25\x8e", 0xC9, payload, b"\xf7\xf7")
        frame = extract_frames(bytearray(raw))[0]
        self.assertTrue(is_mesh_root_frame(frame))

    def test_mesh_follower_frame_yields_peer_serial(self) -> None:
        from foxess_local_cloud.protocol import is_mesh_follower_frame, mesh_peer_serial
        peer = b"60TESTSERIAL00A"
        payload = b"\x01\x05\x01\x02" + bytes([len(peer)]) + peer + bytes.fromhex("00000100040000031e")
        raw = make_frame(b"\x7f\x7f", b"\x3f\x6a\x25\x8e", 0xE2, payload, b"\xf7\xf7")
        frame = extract_frames(bytearray(raw))[0]
        self.assertTrue(is_mesh_follower_frame(frame))
        self.assertEqual(mesh_peer_serial(frame), "60TESTSERIAL00A")

    def test_mesh_frame_rejects_7e7e_family(self) -> None:
        from foxess_local_cloud.protocol import is_mesh_follower_frame, is_mesh_root_frame
        # Same prefix bytes but wrong frame family -- must not match.
        payload = b"\x01\x05\x01\x01\x00\x06\x00\x00\x00\x00\x00\x00"
        raw = make_frame(b"\x7e\x7e", b"\x00\x00\x00\x00", 0x00, payload, b"\xe7\xe7")
        frame = extract_frames(bytearray(raw))[0]
        self.assertFalse(is_mesh_root_frame(frame))
        self.assertFalse(is_mesh_follower_frame(frame))

    def test_mesh_follower_rejects_truncated_serial(self) -> None:
        from foxess_local_cloud.protocol import is_mesh_follower_frame, mesh_peer_serial
        # Declares serial_len=15 but only provides 5 bytes -- must be rejected.
        payload = b"\x01\x05\x01\x02\x0f" + b"60M28"
        raw = make_frame(b"\x7f\x7f", b"\x00\x00\x00\x00", 0xE2, payload, b"\xf7\xf7")
        frame = extract_frames(bytearray(raw))[0]
        self.assertFalse(is_mesh_follower_frame(frame))
        self.assertEqual(mesh_peer_serial(frame), "")

    def test_decode_telemetry_passes_mesh_state_through(self) -> None:
        payload = telemetry_payload()
        result = decode_telemetry(
            payload,
            serial=TEST_SERIAL,
            mesh_role="follower",
            mesh_peer_serial="60TESTSERIAL00A",
        )
        self.assertEqual(result.mesh_role, "follower")
        self.assertEqual(result.mesh_peer_serial, "60TESTSERIAL00A")
        state = result.as_dict()
        self.assertEqual(state["mesh_role"], "follower")
        self.assertEqual(state["mesh_peer_serial"], "60TESTSERIAL00A")

    def test_decode_telemetry_omits_empty_mesh_state(self) -> None:
        result = decode_telemetry(telemetry_payload(), serial=TEST_SERIAL)
        state = result.as_dict()
        # Empty strings are dropped by as_dict(), so unobserved-yet mesh state stays out.
        self.assertNotIn("mesh_role", state)
        self.assertNotIn("mesh_peer_serial", state)

    def test_fault_code_for_all_zeros_is_empty(self) -> None:
        self.assertEqual(fault_code_for((0, 0, 0, 0)), "")

    def test_fault_code_message_translates_known_codes(self) -> None:
        self.assertEqual(fault_code_message_for("4156"), "AC Under Frequency")
        self.assertEqual(fault_code_message_for("4156,4157"), "AC Under Frequency, AC Over Frequency")
        self.assertEqual(fault_code_message_for("4029"), "PV1 Internal Short-Circuit")
        self.assertEqual(fault_code_message_for("raw:04-14-1C-18"), "Unknown fault (raw:04-14-1C-18)")
        self.assertEqual(fault_code_message_for(""), "")

    def test_fault_code_name_table_covers_known_categories(self) -> None:
        # Spot-check that all PV and AC fault families are present
        for code in ("4029", "4061", "4093", "4125", "4152", "4156", "4160"):
            self.assertIn(code, FAULT_CODE_NAMES, f"missing code {code}")

    def test_export_total_decodes_from_offset_74(self) -> None:
        payload = bytearray(telemetry_payload())
        payload[74:76] = (256).to_bytes(2, "big")
        payload[76:78] = (0).to_bytes(2, "big")
        telemetry = decode_telemetry(bytes(payload), TEST_SERIAL, "M1-800-E")
        self.assertEqual(telemetry.export_total_kwh, round(256 / 128.0, 3))

    def test_fault_active_flips_when_offsets_98_to_106_nonzero(self) -> None:
        baseline = decode_telemetry(telemetry_payload(), TEST_SERIAL, "M1-800-E")
        self.assertFalse(baseline.fault_active)
        payload = bytearray(telemetry_payload())
        payload[100:102] = (42).to_bytes(2, "big")
        with_fault = decode_telemetry(bytes(payload), TEST_SERIAL, "M1-800-E")
        self.assertTrue(with_fault.fault_active)

    def test_supports_four_pv_falls_back_to_offset_156(self) -> None:
        payload = bytearray(telemetry_payload())
        # Offset 156 = 4 should enable PV3/PV4 even without "Q1" in the model string
        payload[156:158] = (4).to_bytes(2, "big")
        payload[48:60] = bytes.fromhex("1e 00 01 00 00 64 20 00 02 00 00 c8")
        telemetry = decode_telemetry(bytes(payload), "UNKNOWNMODEL", "Foo-Bar")
        self.assertIsNotNone(telemetry.pv3_power_w)
        self.assertEqual(telemetry.pv3_power_w, 100)

    def test_q1_telemetry_decodes_pv3_pv4_when_present(self) -> None:
        payload = bytearray(telemetry_payload())
        payload[48:60] = bytes.fromhex("1e 00 01 00 00 64 20 00 02 00 00 c8")
        telemetry = decode_telemetry(bytes(payload), "Q1SERIAL000001", "Q1-1200-E")
        self.assertEqual(telemetry.model, "Q1-1200-E")
        self.assertEqual(telemetry.pv3_power_w, 100)
        self.assertEqual(telemetry.pv4_power_w, 200)
        self.assertEqual(telemetry.pv_power_w, telemetry.pv1_power_w + telemetry.pv2_power_w + 300)

    def test_m1_telemetry_omits_pv3_pv4(self) -> None:
        telemetry = decode_telemetry(telemetry_payload(), TEST_SERIAL, "M1-800-E")
        state = telemetry.as_dict()
        self.assertNotIn("pv3_power_w", state)
        self.assertNotIn("pv4_power_w", state)

    def test_check_upstream_cert_accepts_pinned_fingerprint(self) -> None:
        import hashlib

        der = b"fake-cert-bytes"
        expected_hex = hashlib.sha256(der).hexdigest()

        class FakeSslObject:
            def getpeercert(self, binary_form: bool = False) -> bytes:
                return der

        class FakeWriter:
            def get_extra_info(self, key: str) -> Any:
                return FakeSslObject() if key == "ssl_object" else None

        original_pin = FOXESS_UPSTREAM_CERT_SHA256
        try:
            import foxess_local_cloud.server as server_module

            server_module.FOXESS_UPSTREAM_CERT_SHA256 = expected_hex
            self.assertIsNone(check_upstream_cert(FakeWriter()))  # type: ignore[arg-type]
        finally:
            import foxess_local_cloud.server as server_module

            server_module.FOXESS_UPSTREAM_CERT_SHA256 = original_pin

    def test_check_upstream_cert_returns_actual_on_mismatch(self) -> None:
        class FakeSslObject:
            def getpeercert(self, binary_form: bool = False) -> bytes:
                return b"some-other-cert"

        class FakeWriter:
            def get_extra_info(self, key: str) -> Any:
                return FakeSslObject() if key == "ssl_object" else None

        actual = check_upstream_cert(FakeWriter())  # type: ignore[arg-type]
        self.assertIsNotNone(actual)
        self.assertNotEqual(actual, FOXESS_UPSTREAM_CERT_SHA256)

    def test_config_loads_relay_skip_cert_verify(self) -> None:
        cfg = load_config(ROOT / "local-cloud.example.json")
        self.assertFalse(cfg.relay.skip_cert_verify)

    def test_inverter_control_defaults_to_enabled(self) -> None:
        cfg = load_config(ROOT / "local-cloud.example.json")
        # Enabled by default — verified live 2026-06-11; opt-out is via
        # --disable-inverter-control on the installer.
        self.assertTrue(cfg.inverter_control.enabled)
        # Default register address matches the live-mapped ActivePowerLimit.
        self.assertEqual(cfg.inverter_control.active_power_limit_address, 0xCA5A)

    def test_inverter_control_loads_when_present(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"inverter_control": {"enabled": True}}, fh)
            path = Path(fh.name)
        try:
            cfg = load_config(path)
        finally:
            os.unlink(path)
        self.assertTrue(cfg.inverter_control.enabled)
        # Address falls back to its default so a minimal config snippet
        # doesn't have to repeat hardware-derived constants.
        self.assertEqual(cfg.inverter_control.active_power_limit_address, 0xCA5A)

    def test_relay_falls_back_to_public_original_destination(self) -> None:
        app = FoxessLocalCloud(
            AppConfig(
                relay=RelayConfig(
                    enabled=True,
                    upstreams={
                        "8.209.116.72": ("8.209.116.72", 14431),
                        "47.91.86.144": ("47.91.86.144", 14431),
                    },
                    fallback_to_original_destination=True,
                )
            )
        )
        session = Session(app, 1)

        self.assertEqual(session.choose_upstream("8.209.116.72"), ("8.209.116.72", 14431))
        self.assertEqual(session.choose_upstream("8.8.4.4"), ("8.8.4.4", 14431))
        self.assertIsNone(session.choose_upstream("192.168.50.1"))


class LocalCloudServerTest(unittest.IsolatedAsyncioTestCase):
    async def test_expected_tls_close_failure_is_logged_as_disconnect(self) -> None:
        app = FoxessLocalCloud(AppConfig())
        events: list[tuple[str, dict[str, object]]] = []
        app.logger.emit = lambda event, **fields: events.append((event, fields))  # type: ignore[method-assign]

        async def fail_session(_reader: object, _writer: object) -> None:
            raise TimeoutError("SSL shutdown timed out")

        class StubSession:
            serial = TEST_SERIAL

            def __init__(self, _app: object, _session_id: int) -> None:
                pass

            run = staticmethod(fail_session)

        class StubWriter:
            closed = False

            def get_extra_info(self, _key: str) -> tuple[str, int]:
                return ("192.168.50.2", 12345)

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                raise TimeoutError("SSL shutdown timed out")

        writer = StubWriter()
        with patch("foxess_local_cloud.server.Session", StubSession):
            await app.handle_client(object(), writer)  # type: ignore[arg-type]

        self.assertTrue(writer.closed)
        self.assertNotIn("session_error", [event for event, _fields in events])
        self.assertNotIn("disconnect_error", [event for event, _fields in events])
        disconnect = [fields for event, fields in events if event == "disconnect"]
        self.assertEqual(disconnect, [{"session": 1, "serial": TEST_SERIAL, "reason": "ssl_shutdown_timeout"}])

    async def test_unexpected_close_failure_is_contained_and_logged(self) -> None:
        app = FoxessLocalCloud(AppConfig())
        events: list[tuple[str, dict[str, object]]] = []
        app.logger.emit = lambda event, **fields: events.append((event, fields))  # type: ignore[method-assign]

        async def fail_session(_reader: object, _writer: object) -> None:
            raise ValueError("bad protocol state")

        class StubSession:
            serial = None

            def __init__(self, _app: object, _session_id: int) -> None:
                pass

            run = staticmethod(fail_session)

        class StubWriter:
            def get_extra_info(self, _key: str) -> tuple[str, int]:
                return ("192.168.50.2", 12345)

            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                raise RuntimeError("close failed")

        with patch("foxess_local_cloud.server.Session", StubSession):
            await app.handle_client(object(), StubWriter())  # type: ignore[arg-type]

        self.assertIn("session_error", [event for event, _fields in events])
        self.assertIn("disconnect_error", [event for event, _fields in events])
        disconnect = [fields for event, fields in events if event == "disconnect"]
        self.assertEqual(disconnect, [{"session": 1, "serial": "", "reason": "session_error"}])

    async def test_invalid_crc_frame_is_not_acked_or_accepted(self) -> None:
        app = FoxessLocalCloud(AppConfig())
        events: list[tuple[str, dict[str, object]]] = []
        app.logger.emit = lambda event, **fields: events.append((event, fields))  # type: ignore[method-assign]
        session = Session(app, 1)
        writer = FakeStreamWriter()
        raw = bytearray(registration_frame())
        raw[-3] ^= 0xFF
        frame = extract_frames(raw)[0]

        await session.handle_frame(frame, writer)  # type: ignore[arg-type]

        self.assertFalse(frame.valid_crc)
        self.assertIsNone(session.serial)
        self.assertEqual(writer.writes, [])
        self.assertTrue(any(event == "invalid_crc" for event, _fields in events))

    async def test_fault_observed_event_emitted_on_first_active_telemetry(self) -> None:
        app = FoxessLocalCloud(AppConfig())
        events: list[tuple[str, dict[str, object]]] = []
        app.logger.emit = lambda event, **fields: events.append((event, fields))  # type: ignore[method-assign]
        session = Session(app, 1)
        session.serial = TEST_SERIAL

        # Frame 1: clean telemetry, no fault → no fault_observed event
        clean = bytearray(telemetry_payload())
        await session.handle_frame(extract_frames(bytearray(make_frame(b"\x7e\x7e", b"\x02\x00\x00\x00", 0x00, bytes(clean), b"\xe7\xe7")))[0], FakeStreamWriter())  # type: ignore[arg-type]

        # Frame 2: fault active (offsets 100=4, 102=20, 104=28, 106=24) → fault_observed
        fault = bytearray(telemetry_payload())
        fault[100:102] = (4).to_bytes(2, "big")
        fault[102:104] = (20).to_bytes(2, "big")
        fault[104:106] = (28).to_bytes(2, "big")
        fault[106:108] = (24).to_bytes(2, "big")
        await session.handle_frame(extract_frames(bytearray(make_frame(b"\x7e\x7e", b"\x02\x00\x00\x00", 0x00, bytes(fault), b"\xe7\xe7")))[0], FakeStreamWriter())  # type: ignore[arg-type]

        fault_events = [fields for event, fields in events if event == "fault_observed"]
        self.assertEqual(len(fault_events), 1)
        self.assertEqual(fault_events[0]["code"], "4156,4157")
        self.assertTrue(fault_events[0]["known"])
        self.assertEqual(fault_events[0]["offsets"], {"100": 4, "102": 20, "104": 28, "106": 24})

        # Frame 3: fault cleared → fault_cleared event
        await session.handle_frame(extract_frames(bytearray(make_frame(b"\x7e\x7e", b"\x02\x00\x00\x00", 0x00, bytes(clean), b"\xe7\xe7")))[0], FakeStreamWriter())  # type: ignore[arg-type]
        cleared_events = [fields for event, fields in events if event == "fault_cleared"]
        self.assertEqual(len(cleared_events), 1)
        self.assertEqual(cleared_events[0]["code"], "4156,4157")

    async def test_relay_falls_back_to_local_when_upstream_connect_fails(self) -> None:
        import asyncio as _asyncio

        app = FoxessLocalCloud(
            AppConfig(
                relay=RelayConfig(
                    enabled=True,
                    upstreams={"8.209.116.72": ("8.209.116.72", 14431)},
                    connect_timeout_seconds=0.01,
                )
            )
        )
        events: list[tuple[str, dict[str, object]]] = []
        app.logger.emit = lambda event, **fields: events.append((event, fields))  # type: ignore[method-assign]
        session = Session(app, 1)

        async def _failing_open_connection(*_args: object, **_kwargs: object) -> tuple[object, object]:
            raise OSError("upstream unreachable")

        class StubReader:
            def __init__(self, frames: bytes) -> None:
                self._frames = frames
                self._sent = False

            async def read(self, _n: int) -> bytes:
                if self._sent:
                    return b""
                self._sent = True
                return self._frames

        class StubWriter(FakeStreamWriter):
            def get_extra_info(self, _key: str) -> Any:
                return None

        original = _asyncio.open_connection
        try:
            _asyncio.open_connection = _failing_open_connection  # type: ignore[assignment]
            session.choose_upstream = lambda _ip: ("8.209.116.72", 14431)  # type: ignore[method-assign]
            await session.run(StubReader(telemetry_frame()), StubWriter())  # type: ignore[arg-type]
        finally:
            _asyncio.open_connection = original  # type: ignore[assignment]

        event_names = [event for event, _fields in events]
        self.assertIn("relay_connect_failed", event_names)
        self.assertTrue(any(e == "telemetry" for e, _ in events), "telemetry not decoded after fallback")

    async def test_command_frame_decodes_modbus_write_single(self) -> None:
        from foxess_local_cloud.protocol import is_modbus_command, parse_modbus_command

        # ActivePowerLimit = 100% captured from the FoxESS installer portal:
        # 7f7f-envelope command frame containing Modbus write-single-register
        # for slave 01, addr 0xCA5A, value 0x0064.
        raw = bytes.fromhex("7f 7f 12 39 77 0b e2 00 06 01 06 ca 5a 00 64 86 60 f7 f7".replace(" ", ""))
        frame = extract_frames(bytearray(raw))[0]
        self.assertTrue(frame.valid_crc)
        self.assertTrue(is_modbus_command(frame))
        pdu = parse_modbus_command(frame)
        self.assertEqual(pdu, {"slave": 1, "function": 0x06, "address": 0xCA5A, "value": 100})

        app = FoxessLocalCloud(AppConfig())
        events: list[tuple[str, dict[str, object]]] = []
        app.logger.emit = lambda event, **fields: events.append((event, fields))  # type: ignore[method-assign]
        session = Session(app, 1)
        session.handle_upstream_frame(frame)

        command_events = [fields for event, fields in events if event == "command_frame"]
        self.assertEqual(len(command_events), 1)
        self.assertEqual(command_events[0]["function_name"], "write_single")
        self.assertEqual(command_events[0]["address"], 0xCA5A)
        self.assertEqual(command_events[0]["address_hex"], "0xca5a")
        self.assertEqual(command_events[0]["value"], 100)

    async def test_command_frame_decodes_modbus_write_multiple(self) -> None:
        from foxess_local_cloud.protocol import parse_modbus_command

        # Reactive config save from the FoxESS installer portal: write multiple
        # registers to 0xC92C, count=27. 74 bytes total.
        captured_hex = (
            "7f 7f 12 7d 85 0b e2 00 3d "
            "01 10 c9 2c 00 1b 36 "
            "00 00 00 04 00 63 00 00 00 64 00 64 00 64 00 64 00 64 00 64 00 64 00 64 "
            "20 80 1f 40 "
            "20 80 00 00 20 80 00 00 20 80 00 00 20 80 00 00 "
            "00 1e 00 14 02 26 07 08 07 d0 "
            "4e f8 f7 f7"
        )
        raw = bytes.fromhex(captured_hex.replace(" ", ""))
        frame = extract_frames(bytearray(raw))[0]
        self.assertTrue(frame.valid_crc)
        pdu = parse_modbus_command(frame)
        self.assertEqual(pdu["slave"], 1)
        self.assertEqual(pdu["function"], 0x10)
        self.assertEqual(pdu["address"], 0xC92C)
        self.assertEqual(pdu["count"], 27)
        self.assertEqual(pdu["byte_count"], 54)
        self.assertEqual(len(pdu["values"]), 27)
        self.assertEqual(pdu["values"][1], 4)  # PFmode enum
        self.assertEqual(pdu["values"][12], 0x2080)  # PflockinV (260.0 * 32)

    async def test_command_response_decodes_and_joins_to_pending_read(self) -> None:
        from foxess_local_cloud.protocol import is_modbus_read_response, parse_modbus_read_response

        # Synthesised: cloud reads 4 holding regs at 0xC419 (ConfigurationInfo),
        # inverter responds with [4, 0, 1, 120]. Both frames must share the
        # same envelope device bytes so the session can join request→response.
        device = b"\x12\x34\x56\x78"

        request_payload = bytes.fromhex("0103c4190004")
        request_raw = make_frame(b"\x7f\x7f", device, 0xE2, request_payload, b"\xf7\xf7")
        request_frame = extract_frames(bytearray(request_raw))[0]

        response_payload = bytes.fromhex("01030800040000000100 78")
        response_raw = make_frame(b"\x7f\x7f", device, 0xE2, response_payload.replace(b" ", b""), b"\xf7\xf7")
        response_frame = extract_frames(bytearray(response_raw))[0]
        self.assertTrue(is_modbus_read_response(response_frame))
        pdu = parse_modbus_read_response(response_frame)
        self.assertEqual(pdu["values"], [4, 0, 1, 120])

        app = FoxessLocalCloud(AppConfig())
        events: list[tuple[str, dict[str, object]]] = []
        app.logger.emit = lambda event, **fields: events.append((event, fields))  # type: ignore[method-assign]
        session = Session(app, 1)

        session.handle_upstream_frame(request_frame)
        await session.handle_frame(response_frame, FakeStreamWriter(), send_bootstrap=False)  # type: ignore[arg-type]

        responses = [fields for event, fields in events if event == "command_response"]
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["values"], [4, 0, 1, 120])
        self.assertEqual(responses[0]["address_hex"], "0xc419")
        self.assertEqual(responses[0]["count"], 4)
        self.assertEqual(responses[0]["function_name"], "read_holding")
        self.assertEqual(session.pending_reads, {})

    async def test_injected_frame_builders_round_trip_through_parser(self) -> None:
        from foxess_local_cloud.protocol import (
            build_modbus_read_holding,
            build_modbus_read_input,
            build_modbus_write_single,
            is_modbus_command,
            parse_modbus_command,
        )

        # Mimics cloud envelope: first byte 0x12, last byte 0x0b.
        device = b"\x12\x00\x01\x0b"
        for raw, want_address, want_payload in (
            (build_modbus_write_single(device, 0xCA5A, 100), 0xCA5A, {"value": 100}),
            (build_modbus_read_holding(device, 0xC419, 4), 0xC419, {"count": 4}),
            (build_modbus_read_input(device, 0x277E, 28), 0x277E, {"count": 28}),
        ):
            frames = extract_frames(bytearray(raw))
            self.assertEqual(len(frames), 1)
            frame = frames[0]
            self.assertEqual(frame.start, b"\x7f\x7f")
            self.assertEqual(frame.end, b"\xf7\xf7")
            self.assertEqual(frame.func, 0xE2)
            self.assertTrue(frame.valid_crc)
            self.assertTrue(is_modbus_command(frame))
            self.assertEqual(frame.device, device)
            pdu = parse_modbus_command(frame)
            self.assertEqual(pdu["address"], want_address)
            for key, value in want_payload.items():
                self.assertEqual(pdu[key], value)

    async def test_session_observes_cloud_marker_as_diagnostic(self) -> None:
        from foxess_local_cloud.config import InverterControl
        from foxess_local_cloud.local_control import INJECTED_SESSION_MARKER
        from foxess_local_cloud.protocol import extract_frames, make_frame

        app = FoxessLocalCloud(AppConfig(inverter_control=InverterControl(enabled=True)))
        events: list[tuple[str, dict[str, object]]] = []
        app.logger.emit = lambda event, **fields: events.append((event, fields))  # type: ignore[method-assign]
        session = Session(app, 1)
        # Our marker is pre-seeded; observing the cloud's marker is purely
        # diagnostic and must NOT replace it (otherwise we'd lose multi-
        # channel injection capability).
        self.assertEqual(session.local_control.session_marker, INJECTED_SESSION_MARKER)  # type: ignore[union-attr]

        cloud_request = make_frame(
            b"\x7f\x7f",
            b"\x11\x23\x18\xcf",
            0xE2,
            bytes.fromhex("0103ca5a0001"),
            b"\xf7\xf7",
        )
        frame = extract_frames(bytearray(cloud_request))[0]
        session.handle_upstream_frame(frame)

        self.assertEqual(session.local_control.session_marker, INJECTED_SESSION_MARKER)  # type: ignore[union-attr]
        seen = [fields for event, fields in events if event == "inverter_control_cloud_marker_seen"]
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["cloud_marker"], 0xCF)

    async def test_local_control_next_device_uses_self_chosen_marker(self) -> None:
        from foxess_local_cloud.config import InverterControl
        from foxess_local_cloud.local_control import DEVICE_BYTE_READ, DEVICE_BYTE_WRITE, INJECTED_SESSION_MARKER

        app = FoxessLocalCloud(AppConfig(inverter_control=InverterControl(enabled=True)))
        session = Session(app, 1)
        # Marker is pre-seeded at session start — no observation needed,
        # injection works from the very first poll tick.
        read_device = session.local_control._next_device(DEVICE_BYTE_READ)  # type: ignore[union-attr]
        write_device = session.local_control._next_device(DEVICE_BYTE_WRITE)  # type: ignore[union-attr]
        self.assertEqual(read_device[0], DEVICE_BYTE_READ)
        self.assertEqual(write_device[0], DEVICE_BYTE_WRITE)
        self.assertEqual(read_device[3], INJECTED_SESSION_MARKER)
        self.assertEqual(write_device[3], INJECTED_SESSION_MARKER)
        # Counter advances per device and starts in the high range to avoid
        # colliding with the cloud's transactions.
        ctr_read = read_device[1] | (read_device[2] << 8)
        ctr_write = write_device[1] | (write_device[2] << 8)
        self.assertEqual(ctr_write, ctr_read + 1)
        self.assertGreater(ctr_read, 0xF000)

    async def test_local_control_cloud_marker_observed_is_diagnostic_only(self) -> None:
        from foxess_local_cloud.config import InverterControl
        from foxess_local_cloud.local_control import INJECTED_SESSION_MARKER

        app = FoxessLocalCloud(AppConfig(inverter_control=InverterControl(enabled=True)))
        events: list[tuple[str, dict[str, object]]] = []
        app.logger.emit = lambda event, **fields: events.append((event, fields))  # type: ignore[method-assign]
        session = Session(app, 1)
        # Cloud uses 0xb3. We keep our self-chosen INJECTED_SESSION_MARKER.
        session.local_control.observe_session_marker(0xB3)  # type: ignore[union-attr]
        self.assertEqual(session.local_control.session_marker, INJECTED_SESSION_MARKER)  # type: ignore[union-attr]
        seen = [fields for event, fields in events if event == "inverter_control_cloud_marker_seen"]
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["cloud_marker"], 0xB3)
        # Idempotent: observing the same value again doesn't re-emit.
        session.local_control.observe_session_marker(0xB3)  # type: ignore[union-attr]
        events_again = [fields for event, fields in events if event == "inverter_control_cloud_marker_seen"]
        self.assertEqual(len(events_again), 1)

    async def test_local_control_write_register_emits_frame_to_inverter(self) -> None:
        from foxess_local_cloud.config import InverterControl
        from foxess_local_cloud.local_control import DEVICE_BYTE_WRITE
        from foxess_local_cloud.protocol import (
            extract_frames,
            is_modbus_command,
            parse_modbus_command,
        )

        app = FoxessLocalCloud(AppConfig(inverter_control=InverterControl(enabled=True)))
        events: list[tuple[str, dict[str, object]]] = []
        app.logger.emit = lambda event, **fields: events.append((event, fields))  # type: ignore[method-assign]

        session = Session(app, 1)
        self.assertIsNotNone(session.local_control)

        inverter_writer = FakeStreamWriter()
        session.local_control.attach_inverter_writer(inverter_writer)  # type: ignore[union-attr]
        await session.local_control.write_register(0xCA5A, 75)  # type: ignore[union-attr]

        self.assertEqual(len(inverter_writer.writes), 1)
        raw = inverter_writer.writes[0]
        frames = extract_frames(bytearray(raw))
        self.assertEqual(len(frames), 1)
        frame = frames[0]
        self.assertTrue(frame.valid_crc)
        self.assertEqual(frame.device[0], DEVICE_BYTE_WRITE)
        from foxess_local_cloud.local_control import INJECTED_SESSION_MARKER
        self.assertEqual(frame.device[3], INJECTED_SESSION_MARKER)
        self.assertTrue(is_modbus_command(frame))
        pdu = parse_modbus_command(frame)
        self.assertEqual(pdu, {"slave": 1, "function": 0x06, "address": 0xCA5A, "value": 75})

        write_events = [fields for event, fields in events if event == "injected_write"]
        self.assertEqual(len(write_events), 1)
        self.assertEqual(write_events[0]["address_hex"], "0xca5a")
        self.assertEqual(write_events[0]["value"], 75)

    async def test_local_control_read_holding_registers_pending_for_response_join(self) -> None:
        from foxess_local_cloud.config import InverterControl
        from foxess_local_cloud.local_control import normalize_device
        from foxess_local_cloud.protocol import build_modbus_read_holding, extract_frames, make_frame

        app = FoxessLocalCloud(AppConfig(inverter_control=InverterControl(enabled=True)))
        events: list[tuple[str, dict[str, object]]] = []
        app.logger.emit = lambda event, **fields: events.append((event, fields))  # type: ignore[method-assign]
        session = Session(app, 1)

        class ClosableWriter(FakeStreamWriter):
            def close(self) -> None:
                return None

        inverter_writer = ClosableWriter()
        session.local_control.attach_inverter_writer(inverter_writer)  # type: ignore[union-attr]

        device = await session.local_control.read_holding(0xCA5A, 1)  # type: ignore[union-attr]
        self.assertIsNotNone(device)
        # Pending entry recorded under the normalized key.
        self.assertIn(normalize_device(device), session.pending_reads)
        self.assertEqual(session.pending_reads[normalize_device(device)], (0xCA5A, 1))

        # Now simulate the inverter responding with the echoed device (bit-7 set
        # on the first byte) and value [75]. The command_response emit should
        # join the address back even though the response device bytes differ
        # from the request bytes — that was the deferred correlation bug.
        echoed_device = bytes([device[0] | 0x80]) + device[1:]
        response_payload = bytes.fromhex("0103020 04b".replace(" ", ""))
        response_raw = make_frame(b"\x7f\x7f", echoed_device, 0xE2, response_payload, b"\xf7\xf7")
        response_frame = extract_frames(bytearray(response_raw))[0]
        await session.handle_frame(response_frame, ClosableWriter(), send_bootstrap=False)  # type: ignore[arg-type]

        responses = [fields for event, fields in events if event == "command_response"]
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["address_hex"], "0xca5a")
        self.assertEqual(responses[0]["values"], [75])
        self.assertEqual(session.pending_reads, {})

    async def test_relay_strips_injected_responses_from_upstream_forward(self) -> None:
        from foxess_local_cloud.config import InverterControl
        from foxess_local_cloud.local_control import normalize_device
        from foxess_local_cloud.protocol import extract_frames, make_frame

        app = FoxessLocalCloud(AppConfig(inverter_control=InverterControl(enabled=True)))
        events: list[tuple[str, dict[str, object]]] = []
        app.logger.emit = lambda event, **fields: events.append((event, fields))  # type: ignore[method-assign]
        session = Session(app, 1)

        # Two frames arriving on the same TCP read from the inverter: a regular
        # telemetry frame (must reach FoxCloud) followed by the echoed response
        # to a previously-injected ActivePowerLimit read (must NOT reach
        # FoxCloud). Seed the outstanding set with the request's normalized
        # device key so the response is recognised as ours.
        cloud_frame = telemetry_frame()
        request_device = b"\x12\x00\x01\x0b"
        echoed_device = bytes([request_device[0] | 0x80]) + request_device[1:]
        session.local_control._outstanding.add(normalize_device(request_device))  # type: ignore[union-attr]
        injected_response_raw = make_frame(
            b"\x7f\x7f",
            echoed_device,
            0xE2,
            bytes.fromhex("01030200 4b".replace(" ", "")),
            b"\xf7\xf7",
        )

        class ScriptedReader:
            def __init__(self, chunks: list[bytes]) -> None:
                self._chunks = chunks

            async def read(self, _n: int) -> bytes:
                if not self._chunks:
                    return b""
                return self._chunks.pop(0)

        upstream_writer = FakeStreamWriter()

        class ClosableUpstream(FakeStreamWriter):
            def close(self) -> None:
                return None

        upstream = ClosableUpstream()
        reader = ScriptedReader([cloud_frame + injected_response_raw])

        await session.relay_client_to_upstream(reader, upstream)  # type: ignore[arg-type]

        forwarded = b"".join(upstream.writes)
        self.assertIn(cloud_frame, forwarded, "cloud-bound telemetry frame must be forwarded intact")
        self.assertNotIn(injected_response_raw, forwarded, "injected-response bytes must be stripped from upstream forward")

        filtered_events = [fields for event, fields in events if event == "injected_response_filtered"]
        self.assertEqual(len(filtered_events), 1)
        self.assertEqual(filtered_events[0]["bytes"], len(injected_response_raw))

    async def test_local_control_not_constructed_when_inverter_control_disabled(self) -> None:
        # When inverter_control is explicitly disabled (opt-out), the session
        # must not allocate a LocalControl at all.
        from foxess_local_cloud.config import InverterControl

        app = FoxessLocalCloud(AppConfig(inverter_control=InverterControl(enabled=False)))
        session = Session(app, 1)
        self.assertIsNone(session.local_control)

    async def test_relay_forwards_cloud_response_unchanged_when_not_ours(self) -> None:
        from foxess_local_cloud.config import InverterControl
        from foxess_local_cloud.protocol import make_frame

        app = FoxessLocalCloud(AppConfig(inverter_control=InverterControl(enabled=True)))
        events: list[tuple[str, dict[str, object]]] = []
        app.logger.emit = lambda event, **fields: events.append((event, fields))  # type: ignore[method-assign]
        session = Session(app, 1)

        # Cloud-originated read request → cloud-bound response: device byte
        # starts with 0x12 (cloud's allocation). Must pass through forward
        # unchanged so FoxCloud's transaction completes.
        cloud_response_raw = make_frame(
            b"\x7f\x7f",
            b"\x92\x39\x77\x0b",
            0xE2,
            bytes.fromhex("01030200 4b".replace(" ", "")),
            b"\xf7\xf7",
        )

        class ScriptedReader:
            def __init__(self, chunks: list[bytes]) -> None:
                self._chunks = chunks

            async def read(self, _n: int) -> bytes:
                if not self._chunks:
                    return b""
                return self._chunks.pop(0)

        class ClosableUpstream(FakeStreamWriter):
            def close(self) -> None:
                return None

        upstream = ClosableUpstream()
        await session.relay_client_to_upstream(ScriptedReader([cloud_response_raw]), upstream)  # type: ignore[arg-type]

        forwarded = b"".join(upstream.writes)
        self.assertEqual(forwarded, cloud_response_raw, "cloud's own response must pass through verbatim")
        filtered = [e for e in events if e[0] == "injected_response_filtered"]
        self.assertEqual(filtered, [], "no injection filter should fire for a cloud-originated response")

    def test_mqtt_active_power_limit_handler_dispatches_valid_payload(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        client = FakeMqttClient()
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local"),
            {},
            emit=lambda event, **fields: events.append((event, fields)),
            client_factory=lambda: client,
        )
        publisher.connect()

        received: list[int] = []
        publisher.register_active_power_limit_handler(TEST_SERIAL, lambda v: received.append(v))

        # Simulate paho delivering a message on the command topic
        class Msg:
            topic = publisher.active_power_limit_command_topic(TEST_SERIAL)
            payload = b"75"

        publisher._on_message(client, None, Msg())
        self.assertEqual(received, [75])

    def test_mqtt_active_power_limit_handler_rejects_invalid_payloads(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        client = FakeMqttClient()
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local"),
            {},
            emit=lambda event, **fields: events.append((event, fields)),
            client_factory=lambda: client,
        )
        publisher.connect()

        received: list[int] = []
        publisher.register_active_power_limit_handler(TEST_SERIAL, lambda v: received.append(v))

        topic = publisher.active_power_limit_command_topic(TEST_SERIAL)

        for payload, reason in (
            (b"not-a-number", "not_int"),
            (b"-1", "out_of_range"),
            (b"101", "out_of_range"),
        ):

            class Msg:
                pass

            Msg.topic = topic
            Msg.payload = payload
            publisher._on_message(client, None, Msg())

        self.assertEqual(received, [], "handler must not receive invalid values")
        invalid_events = [fields for event, fields in events if event == "mqtt_command_invalid"]
        self.assertEqual(len(invalid_events), 3)
        self.assertEqual([e["reason"] for e in invalid_events], ["not_int", "out_of_range", "out_of_range"])

    def test_mqtt_subscribes_command_topics_on_connect(self) -> None:
        client = FakeMqttClient()
        subscribed: list[str] = []
        client.subscribe = lambda topic: subscribed.append(topic)  # type: ignore[method-assign]
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local"),
            {},
            client_factory=lambda: client,
        )
        publisher.connect()
        publisher.register_active_power_limit_handler(TEST_SERIAL, lambda _v: None)
        # Subscribe is called immediately because client exists
        self.assertEqual(subscribed, [publisher.active_power_limit_command_topic(TEST_SERIAL)])

        # And re-subscribed on (re)connect
        subscribed.clear()
        publisher._on_connect(client, None, None, 0)
        self.assertIn(publisher.active_power_limit_command_topic(TEST_SERIAL), subscribed)

    def test_mqtt_publishes_active_power_limit_discovery_and_state(self) -> None:
        client = FakeMqttClient()
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local"),
            {TEST_SERIAL: "TestInverter"},
            client_factory=lambda: client,
        )
        publisher.connect()
        publisher.publish_active_power_limit_discovery(TEST_SERIAL)
        publisher.publish_active_power_limit_state(TEST_SERIAL, 99)

        topics = {topic: payload for topic, payload, _retain in client.published}
        discovery_topic = f"homeassistant/number/foxess_{TEST_SERIAL}/active_power_limit/config"
        state_topic = publisher.active_power_limit_state_topic(TEST_SERIAL)
        self.assertIn(discovery_topic, topics)
        self.assertIn(state_topic, topics)

        discovery_payload = json.loads(topics[discovery_topic])
        self.assertEqual(discovery_payload["min"], 0)
        self.assertEqual(discovery_payload["max"], 100)
        self.assertEqual(discovery_payload["step"], 1)
        self.assertEqual(discovery_payload["unit_of_measurement"], "%")
        self.assertEqual(discovery_payload["command_topic"], publisher.active_power_limit_command_topic(TEST_SERIAL))
        self.assertEqual(discovery_payload["state_topic"], state_topic)
        self.assertEqual(discovery_payload["device"]["serial_number"], TEST_SERIAL)
        self.assertEqual(topics[state_topic], "99")

    async def test_session_publishes_active_power_limit_state_on_read_response(self) -> None:
        from foxess_local_cloud.config import InverterControl
        from foxess_local_cloud.local_control import normalize_device
        from foxess_local_cloud.protocol import build_modbus_read_holding, extract_frames, make_frame

        client = FakeMqttClient()
        app = FoxessLocalCloud(
            AppConfig(
                mqtt=MqttConfig(host="mqtt.local"),
                inverter_control=InverterControl(enabled=True),
            )
        )
        app.mqtt = MqttPublisher(app.config.mqtt, {}, client_factory=lambda: client)
        app.mqtt.connect()

        session = Session(app, 1)
        session.serial = TEST_SERIAL

        class ClosableWriter(FakeStreamWriter):
            def close(self) -> None:
                return None

        session.local_control.attach_inverter_writer(ClosableWriter())  # type: ignore[union-attr]
        device = await session.local_control.read_holding(0xCA5A, 1)  # type: ignore[union-attr]
        echoed = bytes([device[0] | 0x80]) + device[1:]
        response = make_frame(
            b"\x7f\x7f",
            echoed,
            0xE2,
            bytes.fromhex("01030200 4b".replace(" ", "")),  # response value 0x004b = 75
            b"\xf7\xf7",
        )
        response_frame = extract_frames(bytearray(response))[0]

        await session.handle_frame(response_frame, ClosableWriter(), send_bootstrap=False)  # type: ignore[arg-type]

        state_topic = app.mqtt.active_power_limit_state_topic(TEST_SERIAL)
        state_publishes = [(topic, payload) for topic, payload, _retain in client.published if topic == state_topic]
        self.assertEqual(state_publishes, [(state_topic, "75")])

    async def test_active_power_limit_read_deferred_until_first_telemetry(self) -> None:
        """The one-shot read of the current setpoint must fire on the FIRST
        telemetry frame (a natural settled-session marker) — never at
        session-start, and never more than once per session.

        Background: injecting Modbus at T+~20ms after the inverter's first
        registration frame reliably kills the TLS session with
        APPLICATION_DATA_AFTER_CLOSE_NOTIFY (observed live 2026-06-10). The
        first telemetry frame is a known-good gate past the fragile window.
        """
        import asyncio

        from foxess_local_cloud.config import InverterControl

        client = FakeMqttClient()
        app = FoxessLocalCloud(
            AppConfig(
                mqtt=MqttConfig(host="mqtt.local"),
                inverter_control=InverterControl(enabled=True),
            )
        )
        app.mqtt = MqttPublisher(app.config.mqtt, {}, client_factory=lambda: client)
        app.mqtt.connect()
        session = Session(app, 1)
        session.serial = TEST_SERIAL
        session.model = "M1-800-E"

        class ClosableWriter(FakeStreamWriter):
            def close(self) -> None:
                return None

        writer = ClosableWriter()
        session.local_control.attach_inverter_writer(writer)  # type: ignore[union-attr]
        # Register HA discovery + handler but do NOT fire any read yet.
        session._maybe_wire_active_power_limit_mqtt()
        # No frames written to the inverter at session start.
        self.assertEqual(len(writer.writes), 0)
        self.assertEqual(len(session.local_control._outstanding), 0)  # type: ignore[union-attr]

        # First telemetry frame → exactly one read should be injected.
        frame = extract_frames(bytearray(telemetry_frame()))[0]
        await session.handle_frame(frame, writer, send_bootstrap=False)  # type: ignore[arg-type]
        await asyncio.sleep(0)  # let create_task() actually run
        self.assertEqual(
            len(session.local_control._outstanding),  # type: ignore[union-attr]
            1,
            "first telemetry must trigger the one-shot read",
        )

        # Second telemetry frame → no additional reads.
        frame2 = extract_frames(bytearray(telemetry_frame()))[0]
        await session.handle_frame(frame2, writer, send_bootstrap=False)  # type: ignore[arg-type]
        await asyncio.sleep(0)
        self.assertEqual(
            len(session.local_control._outstanding),  # type: ignore[union-attr]
            1,
            "subsequent telemetry must not re-trigger the read",
        )

    def test_mqtt_active_power_limit_discovery_idempotent_per_serial(self) -> None:
        client = FakeMqttClient()
        publisher = MqttPublisher(MqttConfig(host="mqtt.local"), {}, client_factory=lambda: client)
        publisher.connect()
        publisher.publish_active_power_limit_discovery(TEST_SERIAL)
        first = len(client.published)
        publisher.publish_active_power_limit_discovery(TEST_SERIAL)
        self.assertEqual(len(client.published), first, "second call must not re-publish discovery")

    async def test_command_frame_ignores_non_modbus_7f_frames(self) -> None:
        from foxess_local_cloud.protocol import is_modbus_command

        # Mesh-role declaration frames also use 7f7f framing but the payload
        # begins with 01 05 (a non-Modbus opcode for us); they must not be
        # misclassified as Modbus commands.
        mesh_payload = bytes.fromhex("0105010100060001020304050603aabbccddee04000000ee")
        raw = make_frame(b"\x7f\x7f", b"\x00\x00\x00\x00", 0xE2, mesh_payload, b"\xf7\xf7")
        frame = extract_frames(bytearray(raw))[0]
        self.assertFalse(is_modbus_command(frame))

    async def test_relay_decrypted_log_includes_full_payload_hex(self) -> None:
        app = FoxessLocalCloud(AppConfig())
        events: list[tuple[str, dict[str, object]]] = []
        app.logger.emit = lambda event, **fields: events.append((event, fields))  # type: ignore[method-assign]
        session = Session(app, 1)

        downlink_bytes = bytes.fromhex("01 02 03 ff aa 55 00 13")

        class StubReader:
            def __init__(self, data: bytes) -> None:
                self._data = data
                self._sent = False

            async def read(self, _n: int) -> bytes:
                if self._sent:
                    return b""
                self._sent = True
                return self._data

        class ClosableWriter(FakeStreamWriter):
            def close(self) -> None:
                return None

        await session.relay_upstream_to_client(StubReader(downlink_bytes), ClosableWriter())  # type: ignore[arg-type]

        relay_events = [fields for event, fields in events if event == "relay_decrypted"]
        self.assertEqual(len(relay_events), 1)
        self.assertEqual(relay_events[0]["direction"], "upstream_to_client")
        self.assertEqual(relay_events[0]["bytes"], len(downlink_bytes))
        self.assertEqual(relay_events[0]["payload_hex"], downlink_bytes.hex(" "))

    async def test_telemetry_log_includes_nonzero_raw_words(self) -> None:
        app = FoxessLocalCloud(AppConfig())
        events: list[tuple[str, dict[str, object]]] = []
        app.logger.emit = lambda event, **fields: events.append((event, fields))  # type: ignore[method-assign]
        session = Session(app, 1)
        session.serial = TEST_SERIAL
        frame = extract_frames(bytearray(telemetry_frame()))[0]

        await session.handle_frame(frame, FakeStreamWriter())  # type: ignore[arg-type]

        telemetry_events = [fields for event, fields in events if event == "telemetry"]
        self.assertEqual(len(telemetry_events), 1)
        self.assertEqual(telemetry_events[0]["raw_nonzero_u16"]["154"], 4)  # type: ignore[index]


class MqttPublisherTest(unittest.TestCase):
    def test_mqtt_connect_uses_async_reconnect_loop(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        client = FakeMqttClient()
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local", port=1883, username="user", password="pass"),
            {},
            emit=lambda event, **fields: events.append((event, fields)),
            client_factory=lambda: client,
        )

        publisher.connect()

        self.assertIn(("reconnect_delay_set", (), {"min_delay": 1, "max_delay": 15}), client.calls)
        self.assertIn(("username_pw_set", ("user", "pass"), {}), client.calls)
        self.assertIn(("will_set", ("foxess_m1/status", "offline"), {"retain": True}), client.calls)
        self.assertIn(("connect_async", ("mqtt.local", 1883), {"keepalive": 180}), client.calls)
        self.assertIn(("loop_start", (), {}), client.calls)
        self.assertEqual(events[0][0], "mqtt_connecting")

    def test_mqtt_connect_failure_is_logged_not_raised(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local"),
            {},
            emit=lambda event, **fields: events.append((event, fields)),
            client_factory=lambda: FakeMqttClient(fail_connect=True),
        )

        publisher.connect()

        self.assertEqual(events[0][0], "mqtt_error")
        self.assertIn("broker unavailable", str(events[0][1]["error"]))

    def test_mqtt_publish_errors_are_logged(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        client = FakeMqttClient(publish_rc=4)
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local"),
            {TEST_SERIAL: "Test Inverter"},
            emit=lambda event, **fields: events.append((event, fields)),
            client_factory=lambda: client,
        )
        publisher.connect()

        publisher.publish(sample_telemetry())

        self.assertGreater(len(client.published), 1)
        self.assertTrue(any(event == "mqtt_publish_error" for event, _fields in events))

    def test_mqtt_discovery_includes_sw_and_hw_version_on_device(self) -> None:
        client = FakeMqttClient()
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local"),
            {TEST_SERIAL: "Test Inverter"},
            client_factory=lambda: client,
        )
        publisher.connect()

        from dataclasses import replace
        tele = replace(sample_telemetry(), firmware="1.80", module="M10200")
        publisher.publish(tele)

        configs = [json.loads(p) for t, p, r in client.published if t.endswith("/config") and p]
        self.assertTrue(configs)
        # All sensor discovery payloads carry the same device dict; check any of them
        device = configs[0]["device"]
        self.assertEqual(device["sw_version"], "1.80")
        self.assertEqual(device["hw_version"], "M10200")
        # serial_number lets HA show the inverter's own serial on the
        # device card; without it the only visible serial is mesh_peer_serial.
        self.assertEqual(device["serial_number"], TEST_SERIAL)
        # firmware/module shouldn't appear as their own sensors
        unique_ids = {c["unique_id"] for c in configs}
        self.assertFalse(any(uid.endswith("_firmware") or uid.endswith("_module") for uid in unique_ids))

    def test_mqtt_discovery_uses_detected_model_and_dynamic_fields(self) -> None:
        client = FakeMqttClient()
        payload = bytearray(telemetry_payload())
        payload[48:60] = bytes.fromhex("1e 00 01 00 00 64 20 00 02 00 00 c8")
        telemetry = decode_telemetry(bytes(payload), "Q1SERIAL000001", "Q1-1200-E")
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local"),
            {"Q1SERIAL000001": "Q1 Inverter"},
            client_factory=lambda: client,
        )
        publisher.connect()

        publisher.publish(telemetry)

        discovery_payloads = [json.loads(payload) for topic, payload, retain in client.published if topic.endswith("/config") and retain and payload]
        self.assertTrue(any(payload["device"]["model"] == "Q1-1200-E" for payload in discovery_payloads))
        self.assertTrue(any(payload["unique_id"].endswith("_pv4_power_w") for payload in discovery_payloads))
        self.assertTrue(any(payload["object_id"].endswith("_pv4_power_w") for payload in discovery_payloads))
        for payload in discovery_payloads:
            self.assertEqual(payload["availability_mode"], "all")
            topics = [entry["topic"] for entry in payload["availability"]]
            self.assertEqual(topics, ["foxess_m1/status", "foxess_m1/Q1SERIAL000001/availability"])
        self.assertTrue(any(payload.get("expire_after") == 300 for payload in discovery_payloads if payload["unique_id"].endswith("_pv4_power_w")))

    def test_mqtt_republishes_discovery_when_model_is_later_detected(self) -> None:
        client = FakeMqttClient()
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local"),
            {TEST_SERIAL: "Test Inverter"},
            client_factory=lambda: client,
        )
        publisher.connect()

        first = sample_telemetry(model=None)
        second = sample_telemetry(model="M1-800-E")
        publisher.publish(first)
        first_discovery_count = len([topic for topic, _payload, _retain in client.published if topic.endswith("/config")])
        publisher.publish(second)
        discovery_payloads = [json.loads(payload) for topic, payload, retain in client.published if topic.endswith("/config") and retain and payload]

        self.assertGreater(len(discovery_payloads), first_discovery_count)
        self.assertTrue(any(payload["device"]["model"] == "M1-800-E" for payload in discovery_payloads))

    def test_mqtt_publishes_state_scalar_topics_and_availability(self) -> None:
        client = FakeMqttClient()
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local", retain=False),
            {TEST_SERIAL: "Test Inverter"},
            client_factory=lambda: client,
        )
        publisher.connect()

        publisher.publish(sample_telemetry())

        published = {topic: (payload, retain) for topic, payload, retain in client.published}
        self.assertEqual(published[f"foxess_m1/{TEST_SERIAL}/state"][1], False)
        self.assertEqual(published[f"foxess_m1/{TEST_SERIAL}/availability"], ("online", True))
        self.assertEqual(published[f"foxess_m1/{TEST_SERIAL}/0/power"], ("2", False))
        self.assertEqual(published[f"foxess_m1/{TEST_SERIAL}/0/ac/export_power"], ("1", False))
        self.assertEqual(published[f"foxess_m1/{TEST_SERIAL}/0/status"], ("running", False))
        self.assertEqual(published[f"foxess_m1/{TEST_SERIAL}/1/power"], ("1", False))
        self.assertEqual(published[f"foxess_m1/{TEST_SERIAL}/2/current"], ("0.1", False))
        state = json.loads(published[f"foxess_m1/{TEST_SERIAL}/state"][0])
        self.assertIn("export_power_w", state)
        self.assertEqual(state["operating_state"], "running")
        self.assertNotIn("sequence", state)
        self.assertNotIn("operating_state_code", state)
        self.assertNotIn("raw_u16_002", state)
        self.assertEqual(published[f"foxess_m1/{TEST_SERIAL}/0/sequence"], ("", True))
        self.assertEqual(published[f"foxess_m1/{TEST_SERIAL}/0/status_code"], ("", True))

    def test_mqtt_discovery_publishes_operating_state_and_running_binary_sensor(self) -> None:
        client = FakeMqttClient()
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local"),
            {TEST_SERIAL: "Test Inverter"},
            client_factory=lambda: client,
        )
        publisher.connect()

        publisher.publish(sample_telemetry())

        configs = {
            topic: json.loads(payload)
            for topic, payload, retain in client.published
            if topic.endswith("/config") and retain and payload
        }
        state_config = configs[f"homeassistant/sensor/foxess_{TEST_SERIAL}/operating_state/config"]
        running_config = configs[f"homeassistant/binary_sensor/foxess_{TEST_SERIAL}/running/config"]
        self.assertEqual(state_config["device_class"], "enum")
        self.assertEqual(state_config["options"], ["standby", "running", "unknown"])
        self.assertEqual(running_config["device_class"], "running")
        self.assertEqual(running_config["value_template"], "{{ 'ON' if value_json.operating_state == 'running' else 'OFF' }}")

    def test_mqtt_debug_includes_raw_and_sequence_fields(self) -> None:
        client = FakeMqttClient()
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local", debug=True),
            {TEST_SERIAL: "Test Inverter"},
            client_factory=lambda: client,
        )
        publisher.connect()

        publisher.publish(sample_telemetry())

        published = {topic: (payload, retain) for topic, payload, retain in client.published}
        state = json.loads(published[f"foxess_m1/{TEST_SERIAL}/state"][0])
        discovery_payloads = [json.loads(payload) for topic, payload, retain in client.published if topic.endswith("/config") and payload]
        discovery_ids = [payload["unique_id"] for payload in discovery_payloads]
        names_by_id = {payload["unique_id"]: payload["name"] for payload in discovery_payloads}
        self.assertIn("sequence", state)
        self.assertIn("operating_state_code", state)
        self.assertIn("raw_u16_002", state)
        self.assertEqual(published[f"foxess_m1/{TEST_SERIAL}/0/sequence"], ("1", True))
        self.assertEqual(published[f"foxess_m1/{TEST_SERIAL}/0/status_code"], ("4", True))
        self.assertIn(f"foxess_{TEST_SERIAL}_raw_u16_002", discovery_ids)
        self.assertEqual(names_by_id[f"foxess_{TEST_SERIAL}_export_power_w"], "Export Power")

    def test_mqtt_publish_clears_legacy_feedin_discovery(self) -> None:
        client = FakeMqttClient()
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local"),
            {TEST_SERIAL: "Test Inverter"},
            client_factory=lambda: client,
        )
        publisher.connect()

        publisher.publish(sample_telemetry())

        legacy_topic = f"homeassistant/sensor/foxess_{TEST_SERIAL}/feedin_power_w/config"
        cleared = [(topic, payload, retain) for topic, payload, retain in client.published if topic == legacy_topic]
        self.assertEqual(len(cleared), 1)
        self.assertEqual(cleared[0][1], "")
        self.assertTrue(cleared[0][2])

    def test_mqtt_connect_callback_distinguishes_failed_connack(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local"),
            {},
            emit=lambda event, **fields: events.append((event, fields)),
            client_factory=FakeMqttClient,
        )

        publisher._on_connect(None, None, None, "Success")
        publisher._on_connect(None, None, None, "Not authorized")

        self.assertEqual([event for event, _fields in events], ["mqtt_connected", "mqtt_connect_failed"])

    def test_generation_metadata_uses_total_increasing_for_lifetime_counter(self) -> None:
        self.assertEqual(metadata_for("generation_kwh"), ("kWh", "energy", "total_increasing"))

    def test_operating_state_metadata_uses_enum(self) -> None:
        self.assertEqual(metadata_for("operating_state"), (None, "enum", None))

    def test_loop_is_running_true_after_connect_false_after_death(self) -> None:
        client = FakeMqttClient()
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local"), {}, client_factory=lambda: client
        )
        self.assertFalse(publisher.loop_is_running())
        publisher.connect()
        self.assertTrue(publisher.loop_is_running())
        client.simulate_loop_death()
        self.assertFalse(publisher.loop_is_running())

    def test_ensure_connected_is_noop_while_loop_alive(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        client = FakeMqttClient()
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local"),
            {},
            emit=lambda event, **fields: events.append((event, fields)),
            client_factory=lambda: client,
        )
        publisher.connect()
        connect_calls_before = client.calls.count(("loop_start", (), {}))

        publisher.ensure_connected()

        self.assertEqual(client.calls.count(("loop_start", (), {})), connect_calls_before)
        self.assertFalse(any(event == "mqtt_loop_restart" for event, _ in events))

    def test_ensure_connected_rebuilds_dead_loop(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        clients = [FakeMqttClient(), FakeMqttClient()]
        factory = iter(clients)
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local"),
            {},
            emit=lambda event, **fields: events.append((event, fields)),
            client_factory=lambda: next(factory),
        )
        publisher.connect()
        clients[0].simulate_loop_death()

        publisher.ensure_connected()

        restart = [fields for event, fields in events if event == "mqtt_loop_restart"]
        self.assertEqual(len(restart), 1)
        self.assertEqual(restart[0]["reason"], "loop_dead")
        # Old client was torn down, new client took over and started its loop.
        self.assertIn(("loop_stop", (), {}), clients[0].calls)
        self.assertIs(publisher.client, clients[1])
        self.assertIn(("loop_start", (), {}), clients[1].calls)
        self.assertTrue(publisher.loop_is_running())
        # The fresh connection republishes online once its on_connect fires.
        publisher._on_connect(clients[1], None, None, "Success")
        self.assertIn(("foxess_m1/status", "online", True), clients[1].published)

    def test_ensure_connected_recovers_when_initial_connect_failed(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        clients = [FakeMqttClient(fail_connect=True), FakeMqttClient()]
        factory = iter(clients)
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local"),
            {},
            emit=lambda event, **fields: events.append((event, fields)),
            client_factory=lambda: next(factory),
        )
        publisher.connect()  # broker down: loop never starts
        self.assertFalse(publisher.loop_is_running())

        publisher.ensure_connected()

        restart = [fields for event, fields in events if event == "mqtt_loop_restart"]
        self.assertEqual(restart[0]["reason"], "loop_dead")
        self.assertIs(publisher.client, clients[1])
        self.assertTrue(publisher.loop_is_running())

    def test_teardown_clears_discovery_dedup_so_it_is_resent(self) -> None:
        client = FakeMqttClient()
        publisher = MqttPublisher(
            MqttConfig(host="mqtt.local"),
            {TEST_SERIAL: "Test Inverter"},
            client_factory=lambda: client,
        )
        publisher.connect()
        publisher.publish(sample_telemetry())
        self.assertIn(TEST_SERIAL, publisher.announced)

        publisher._teardown_client()

        self.assertNotIn(TEST_SERIAL, publisher.announced)


class InstallerTest(unittest.TestCase):
    def test_pi_installer_inverter_control_enabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(
                [
                    str(ROOT / "installer/install_pi_zero_gateway.sh"),
                    "--dry-run",
                    "--preview-dir",
                    tmpdir,
                    "--skip-app-copy",
                    "--non-interactive",
                    "--ap-passphrase",
                    "testpass123",
                    "--mqtt-host",
                    "mqtt.local",
                ],
                cwd=ROOT,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                env={**os.environ, "FOXESS_EXISTING_CONFIG": "/nonexistent"},
            )
            cfg = load_config(Path(tmpdir) / "etc/foxess-local-cloud/config.json")
            self.assertTrue(cfg.inverter_control.enabled)

    def test_pi_installer_inverter_control_enabled_via_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [
                    str(ROOT / "installer/install_pi_zero_gateway.sh"),
                    "--dry-run",
                    "--preview-dir",
                    tmpdir,
                    "--skip-app-copy",
                    "--non-interactive",
                    "--ap-passphrase",
                    "testpass123",
                    "--mqtt-host",
                    "mqtt.local",
                    "--enable-inverter-control",
                ],
                cwd=ROOT,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                env={**os.environ, "FOXESS_EXISTING_CONFIG": "/nonexistent"},
            )
            self.assertIn("Inverter control: enabled", result.stdout)
            cfg = load_config(Path(tmpdir) / "etc/foxess-local-cloud/config.json")
            self.assertTrue(cfg.inverter_control.enabled)

    def test_pi_installer_inverter_control_disabled_via_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [
                    str(ROOT / "installer/install_pi_zero_gateway.sh"),
                    "--dry-run",
                    "--preview-dir",
                    tmpdir,
                    "--skip-app-copy",
                    "--non-interactive",
                    "--ap-passphrase",
                    "testpass123",
                    "--mqtt-host",
                    "mqtt.local",
                    "--disable-inverter-control",
                ],
                cwd=ROOT,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                env={**os.environ, "FOXESS_EXISTING_CONFIG": "/nonexistent"},
            )
            self.assertIn("Inverter control: disabled", result.stdout)
            cfg = load_config(Path(tmpdir) / "etc/foxess-local-cloud/config.json")
            self.assertFalse(cfg.inverter_control.enabled)

    def test_pi_installer_dry_run_renders_valid_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [
                    str(ROOT / "installer/install_pi_zero_gateway.sh"),
                    "--dry-run",
                    "--preview-dir",
                    tmpdir,
                    "--skip-app-copy",
                    "--non-interactive",
                    "--ap-passphrase",
                    "testpass123",
                    "--ap-address",
                    "192.168.51.1",
                    "--ap-prefix",
                    "24",
                    "--ap-dhcp-start",
                    "192.168.51.20",
                    "--ap-dhcp-end",
                    "192.168.51.80",
                    "--mqtt-host",
                    "mqtt.local",
                    "--relay",
                ],
                cwd=ROOT,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertIn("Passphrase: testpass123", result.stdout)
            config_path = Path(tmpdir) / "etc/foxess-local-cloud/config.json"
            cfg = load_config(config_path)
            self.assertTrue(cfg.relay.enabled)
            self.assertFalse(cfg.relay.skip_cert_verify)
            self.assertEqual(cfg.host, "192.168.51.1")
            self.assertEqual(cfg.mqtt.host, "mqtt.local")
            self.assertIn("8.209.116.72", cfg.relay.upstreams)
            self.assertIn("47.91.86.144", cfg.relay.upstreams)

            dnsmasq = (Path(tmpdir) / "etc/dnsmasq.d/foxess-local-cloud.conf").read_text(encoding="utf-8")
            self.assertIn("dhcp-option=option:router,192.168.51.1", dnsmasq)
            self.assertIn("address=/foxesscloud.com/192.168.51.1", dnsmasq)
            self.assertNotIn("www.foxesscloud.com", dnsmasq)

            ap_helper = (Path(tmpdir) / "usr/local/sbin/foxess-pi-ap").read_text(encoding="utf-8")
            self.assertIn("find_cmd iw /usr/sbin/iw /sbin/iw", ap_helper)
            self.assertIn('phy=$("$IW" dev "$STA_IFACE" info', ap_helper)
            self.assertIn('"$NFT" add rule inet "$NFT_TABLE" prerouting', ap_helper)

            hostapd_conf = (Path(tmpdir) / "etc/hostapd/hostapd-foxess.conf").read_text(encoding="utf-8")
            self.assertIn("ap_isolate=1", hostapd_conf)

            status_script = (Path(tmpdir) / "usr/local/sbin/foxess-gateway-status").read_text(encoding="utf-8")
            self.assertIn('section "Services"', status_script)
            self.assertIn("foxess-local-cloud.service", status_script)
            self.assertIn("Passphrase=<stored in", status_script)

            local_cloud_unit = (Path(tmpdir) / "etc/systemd/system/foxess-local-cloud.service").read_text(encoding="utf-8")
            self.assertIn("After=network-online.target foxess-pi-ap.service", local_cloud_unit)
            self.assertNotIn("foxess-hostapd.service", local_cloud_unit)

            hostapd_unit = (Path(tmpdir) / "etc/systemd/system/foxess-hostapd.service").read_text(encoding="utf-8")
            self.assertIn("After=foxess-pi-ap.service\n", hostapd_unit)
            self.assertIn("Requires=foxess-pi-ap.service\n", hostapd_unit)
            self.assertNotIn("foxess-local-cloud.service", hostapd_unit)

            logrotate = (Path(tmpdir) / "etc/logrotate.d/foxess-local-cloud").read_text(encoding="utf-8")
            self.assertIn("/var/log/foxess-local-cloud/*.jsonl", logrotate)
            self.assertIn("copytruncate", logrotate)

            credentials = (Path(tmpdir) / "etc/foxess-local-cloud/wifi-credentials.txt").read_text(encoding="utf-8")
            self.assertIn("SSID=FoxESS-Local", credentials)
            self.assertIn("Passphrase=testpass123", credentials)
            self.assertIn("Generated=0", credentials)

    def test_pi_installer_generates_ap_passphrase(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(
                [
                    str(ROOT / "installer/install_pi_zero_gateway.sh"),
                    "--dry-run",
                    "--preview-dir",
                    tmpdir,
                    "--skip-app-copy",
                    "--non-interactive",
                    "--ap-channel",
                    "6",
                ],
                cwd=ROOT,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            credentials = (Path(tmpdir) / "etc/foxess-local-cloud/wifi-credentials.txt").read_text(encoding="utf-8")
            passphrase = next(line.split("=", 1)[1] for line in credentials.splitlines() if line.startswith("Passphrase="))
            self.assertEqual(len(passphrase), 32)
            self.assertIn("Generated=1", credentials)

    def test_pi_installer_refuses_nonempty_unmarked_preview_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            preview = Path(tmpdir) / "preview"
            preview.mkdir()
            sentinel = preview / "sentinel.txt"
            sentinel.write_text("keep me", encoding="utf-8")

            result = subprocess.run(
                [
                    str(ROOT / "installer/install_pi_zero_gateway.sh"),
                    "--dry-run",
                    "--preview-dir",
                    str(preview),
                    "--skip-app-copy",
                    "--non-interactive",
                    "--ap-channel",
                    "6",
                ],
                cwd=ROOT,
                text=True,
                stderr=subprocess.PIPE,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not marked as disposable", result.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me")

    def test_pi_installer_refuses_system_preview_dir(self) -> None:
        result = subprocess.run(
            [
                str(ROOT / "installer/install_pi_zero_gateway.sh"),
                "--dry-run",
                "--preview-dir",
                "/etc",
                "--skip-app-copy",
                "--non-interactive",
                "--ap-channel",
                "6",
            ],
            cwd=ROOT,
            text=True,
            stderr=subprocess.PIPE,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing unsafe preview dir", result.stderr)

    def test_pi_installer_preserves_existing_ap_passphrase(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            existing_credentials = Path(tmpdir) / "wifi-credentials.txt"
            existing_credentials.write_text(
                "FoxESS local inverter Wi-Fi\n\nSSID=FoxESS-Local\nPassphrase=existingpass123\nGateway=192.168.50.1\nSubnet=192.168.50.0/24\n\nGenerated=1\n",
                encoding="utf-8",
            )
            env = {**os.environ, "FOXESS_EXISTING_WIFI_CREDENTIALS": str(existing_credentials)}
            subprocess.run(
                [
                    str(ROOT / "installer/install_pi_zero_gateway.sh"),
                    "--dry-run",
                    "--preview-dir",
                    str(Path(tmpdir) / "preview"),
                    "--skip-app-copy",
                    "--non-interactive",
                    "--ap-channel",
                    "6",
                ],
                cwd=ROOT,
                env=env,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            credentials = (Path(tmpdir) / "preview/etc/foxess-local-cloud/wifi-credentials.txt").read_text(encoding="utf-8")
            self.assertIn("Passphrase=existingpass123", credentials)
            self.assertIn("Generated=0", credentials)

    def test_pi_installer_replaces_invalid_existing_ap_passphrase(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            existing_credentials = Path(tmpdir) / "wifi-credentials.txt"
            existing_credentials.write_text("Passphrase=short\n", encoding="utf-8")
            env = {**os.environ, "FOXESS_EXISTING_WIFI_CREDENTIALS": str(existing_credentials)}
            subprocess.run(
                [
                    str(ROOT / "installer/install_pi_zero_gateway.sh"),
                    "--dry-run",
                    "--preview-dir",
                    str(Path(tmpdir) / "preview"),
                    "--skip-app-copy",
                    "--non-interactive",
                    "--ap-channel",
                    "6",
                ],
                cwd=ROOT,
                env=env,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            credentials = (Path(tmpdir) / "preview/etc/foxess-local-cloud/wifi-credentials.txt").read_text(encoding="utf-8")
            passphrase = next(line.split("=", 1)[1] for line in credentials.splitlines() if line.startswith("Passphrase="))
            self.assertEqual(len(passphrase), 32)
            self.assertIn("Generated=1", credentials)

    def test_pi_installer_merges_extra_cloud_ips_and_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(
                [
                    str(ROOT / "installer/install_pi_zero_gateway.sh"),
                    "--dry-run",
                    "--preview-dir",
                    tmpdir,
                    "--skip-app-copy",
                    "--non-interactive",
                    "--ap-passphrase",
                    "testpass123",
                    "--ap-channel",
                    "6",
                    "--foxess-cloud-ip",
                    "203.0.113.10",
                    "--foxess-cloud-host",
                    "api.example.invalid",
                    "--relay",
                ],
                cwd=ROOT,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            cfg = load_config(Path(tmpdir) / "etc/foxess-local-cloud/config.json")
            self.assertEqual(cfg.relay.upstreams["203.0.113.10"], ("203.0.113.10", 14431))

            pi_ap = (Path(tmpdir) / "etc/foxess-local-cloud/pi-ap.conf").read_text(encoding="utf-8")
            self.assertIn("203.0.113.10", pi_ap)

            dnsmasq = (Path(tmpdir) / "etc/dnsmasq.d/foxess-local-cloud.conf").read_text(encoding="utf-8")
            self.assertIn("address=/api.example.invalid/192.168.50.1", dnsmasq)

    def test_pi_installer_preserves_existing_mqtt_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            existing_config = Path(tmpdir) / "existing-config.json"
            existing_config.write_text(
                json.dumps(
                    {
                        "mqtt": {
                            "host": "mqtt.local",
                            "port": 1884,
                            "username": "foxess",
                            "password": "secret",
                        },
                        "devices": {
                            "60TESTSERIAL00A": "FirstInverter",
                            "60TESTSERIAL00B": "SecondInverter",
                        },
                    }
                ),
                encoding="utf-8",
            )
            env = {**os.environ, "FOXESS_EXISTING_CONFIG": str(existing_config)}
            subprocess.run(
                [
                    str(ROOT / "installer/install_pi_zero_gateway.sh"),
                    "--dry-run",
                    "--preview-dir",
                    str(Path(tmpdir) / "preview"),
                    "--skip-app-copy",
                    "--non-interactive",
                    "--ap-passphrase",
                    "testpass123",
                    "--ap-channel",
                    "6",
                    "--mqtt-host",
                    "mqtt.local",
                ],
                cwd=ROOT,
                env=env,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            cfg = load_config(Path(tmpdir) / "preview/etc/foxess-local-cloud/config.json")
            self.assertEqual(cfg.mqtt.host, "mqtt.local")
            self.assertEqual(cfg.mqtt.port, 1884)
            self.assertEqual(cfg.mqtt.username, "foxess")
            self.assertEqual(cfg.mqtt.password, "secret")
            self.assertEqual(
                cfg.devices,
                {
                    "60TESTSERIAL00A": "FirstInverter",
                    "60TESTSERIAL00B": "SecondInverter",
                },
            )


if __name__ == "__main__":
    unittest.main()
