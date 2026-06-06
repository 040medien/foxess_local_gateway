from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

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
from foxess_local_cloud.telemetry import Telemetry, decode_telemetry, nonzero_u16_words, u32_wordswapped


ROOT = Path(__file__).resolve().parents[1]
TEST_SERIAL = "TESTM1SERIAL001"


def registration_frame(serial: str = TEST_SERIAL) -> bytes:
    serial_bytes = serial.encode("ascii")
    payload = b"\x01\x00\x01\x31" + bytes([len(serial_bytes)]) + serial_bytes + b"\x05v1.31\x00"
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


class FakeMqttClient:
    def __init__(self, fail_connect: bool = False, publish_rc: int = 0) -> None:
        self.fail_connect = fail_connect
        self.publish_rc = publish_rc
        self.on_connect = None
        self.on_disconnect = None
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.published: list[tuple[str, str, bool]] = []

    def reconnect_delay_set(self, **kwargs: object) -> None:
        self.calls.append(("reconnect_delay_set", (), kwargs))

    def username_pw_set(self, *args: object) -> None:
        self.calls.append(("username_pw_set", args, {}))

    def will_set(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("will_set", args, kwargs))

    def connect_async(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("connect_async", args, kwargs))
        if self.fail_connect:
            raise OSError("broker unavailable")

    def loop_start(self) -> None:
        self.calls.append(("loop_start", (), {}))

    def loop_stop(self) -> None:
        self.calls.append(("loop_stop", (), {}))

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
        self.assertFalse(cfg.mqtt.retain)
        self.assertEqual(cfg.mqtt.expire_after_seconds, 180)
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
        self.assertTrue(any(payload.get("expire_after") == 180 for payload in discovery_payloads if payload["unique_id"].endswith("_pv4_power_w")))

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
        self.assertEqual(published[f"foxess_m1/{TEST_SERIAL}/0/sequence"], ("1", False))
        self.assertEqual(published[f"foxess_m1/{TEST_SERIAL}/0/status_code"], ("4", False))
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


class InstallerTest(unittest.TestCase):
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
                            "60M28010563R686": "Schuppen",
                            "60M2801056BR589": "Garage",
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
                    "60M28010563R686": "Schuppen",
                    "60M2801056BR589": "Garage",
                },
            )


if __name__ == "__main__":
    unittest.main()
