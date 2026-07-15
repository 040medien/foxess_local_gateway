from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from foxess_local_cloud.config import FirmwareCaptureConfig
from foxess_local_cloud.firmware import (
    FIRMWARE_ENVELOPE_FUNC,
    FIRMWARE_MAGIC,
    FirmwareCapture,
    FirmwareImage,
    FirmwareProtocolError,
    FirmwareUploader,
    FIRMWARE_VARIANT_7F_A2,
    FIRMWARE_VARIANT_7F_99,
    build_firmware_metadata,
    parse_firmware_metadata,
    safe_firmware_filename,
)
from foxess_local_cloud.protocol import extract_frames, make_frame


TEST_SERIAL = "TESTM1SERIAL001"


class FakeWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None


def frame(raw: bytes):
    return extract_frames(bytearray(raw))[0]


class FirmwareMetadataTest(unittest.TestCase):
    def test_184_metadata_matches_observed_cloud_bytes(self) -> None:
        image = FirmwareImage.from_bytes(
            bytes.fromhex("00") * 0xCAE5,
            "10-300-11100-31_M1_1200_v1.84_BA.bin",
        )
        # Substitute the captured image CRC without embedding firmware bytes.
        from dataclasses import replace
        image = replace(image, crc=bytes.fromhex("c2 86"))
        expected = bytes.fromhex(
            "fa 6b 7c 7a 4d 00 00 ca e5 86 c2 24 "
            "31 30 2d 33 30 30 2d 31 31 31 30 30 2d 33 31 5f 4d 31 5f "
            "31 32 30 30 5f 76 31 2e 38 34 5f 42 41 2e 62 69 6e 00 0f "
            "36 30 4d 32 38 30 31 30 35 36 33 52 36 38 36 01 2c"
        )
        self.assertEqual(
            build_firmware_metadata(image, "60M28010563R686", FIRMWARE_VARIANT_7F_A2),
            expected,
        )

    def test_metadata_round_trip(self) -> None:
        image = FirmwareImage.from_bytes(bytes(range(256)) * 5, "M1_test_v1.84.bin")
        metadata = parse_firmware_metadata(build_firmware_metadata(image, TEST_SERIAL))
        self.assertEqual(metadata.size, image.size)
        self.assertEqual(metadata.crc, image.crc)
        self.assertEqual(metadata.filename, image.filename)
        self.assertEqual(metadata.serial, TEST_SERIAL)
        self.assertEqual(metadata.timeout_seconds, 300)

    def test_image_rejects_oversize_protocol_payload(self) -> None:
        with self.assertRaises(FirmwareProtocolError):
            FirmwareImage.from_bytes(b"x" * 65536, "too-large.bin")

    def test_capture_config_is_opt_in(self) -> None:
        self.assertFalse(FirmwareCaptureConfig().enabled)

    def test_official_regional_filename_is_preserved(self) -> None:
        name = "110-300-11100-14_M1_1200_v1.66_BA(Only Europe).bin"
        self.assertEqual(safe_firmware_filename(name), name)

    def test_upgrade_cli_dry_run_verifies_renamed_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image = Path(tmpdir) / "renamed.bin"
            image.write_bytes(b"test firmware bytes")
            expected = hashlib.sha256(image.read_bytes()).hexdigest()
            result = subprocess.run(
                [
                    str(Path(__file__).resolve().parents[1] / "tools/foxess-firmware-upgrade"),
                    str(image),
                    "--serial",
                    TEST_SERIAL,
                    "--sha256",
                    expected,
                    "--dry-run",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["verified"])


class FirmwareTransferTest(unittest.IsolatedAsyncioTestCase):
    async def test_capture_save_failure_is_fail_closed_without_raising(self) -> None:
        data = b"firmware"
        image = FirmwareImage.from_bytes(data, "test.bin")
        events: list[tuple[str, dict]] = []
        writer = FakeWriter()
        with tempfile.TemporaryDirectory() as tmpdir:
            blocked = Path(tmpdir) / "not-a-directory"
            blocked.write_text("blocked", encoding="utf-8")
            capture = FirmwareCapture(
                blocked,
                lambda event, **fields: events.append((event, fields)),
                1,
                simulate_progress=False,
            )
            metadata = make_frame(
                b"\x7e\x7e", b"\x11\x00\x00\x01", FIRMWARE_ENVELOPE_FUNC,
                build_firmware_metadata(image, TEST_SERIAL), b"\xe7\xe7",
            )
            await capture.handle_upstream_frame(
                frame(metadata), serial=TEST_SERIAL, upstream_writer=writer,  # type: ignore[arg-type]
                client_device_tail=b"\x00\x00\x01", last_client_func=1,
            )
            chunk = make_frame(
                b"\x7e\x7e", b"\x12\x00\x00\x01", FIRMWARE_ENVELOPE_FUNC,
                FIRMWARE_MAGIC + b"\x00\x00" + data, b"\xe7\xe7",
            )
            self.assertTrue(await capture.handle_upstream_frame(
                frame(chunk), serial=TEST_SERIAL, upstream_writer=writer,  # type: ignore[arg-type]
                client_device_tail=b"\x00\x00\x01", last_client_func=1,
            ))
        self.assertTrue(any(
            event == "firmware_capture_failed" and fields.get("reason") == "save_failed"
            for event, fields in events
        ))

    async def test_capture_saves_verified_image_and_manifest(self) -> None:
        data = bytes(range(251)) * 10
        image = FirmwareImage.from_bytes(data, "M1_test_v1.84.bin")
        events: list[tuple[str, dict]] = []
        writer = FakeWriter()
        with tempfile.TemporaryDirectory() as tmpdir:
            capture = FirmwareCapture(
                Path(tmpdir),
                lambda event, **fields: events.append((event, fields)),
                7,
                simulate_progress=False,
            )
            metadata_raw = make_frame(
                b"\x7e\x7e",
                b"\x11\x12\x34\x56",
                FIRMWARE_ENVELOPE_FUNC,
                build_firmware_metadata(image, TEST_SERIAL),
                b"\xe7\xe7",
            )
            self.assertTrue(
                await capture.handle_upstream_frame(
                    frame(metadata_raw),
                    serial=TEST_SERIAL,
                    upstream_writer=writer,  # type: ignore[arg-type]
                    client_device_tail=b"\x12\x34\x56",
                    last_client_func=0x8F,
                )
            )
            chunks = [data[pos:pos + 1024] for pos in range(0, len(data), 1024)]
            for index, chunk in enumerate(chunks):
                raw = make_frame(
                    b"\x7e\x7e",
                    b"\x12\x12\x34\x56",
                    FIRMWARE_ENVELOPE_FUNC,
                    FIRMWARE_MAGIC + index.to_bytes(2, "big") + chunk,
                    b"\xe7\xe7",
                )
                self.assertTrue(
                    await capture.handle_upstream_frame(
                        frame(raw),
                        serial=TEST_SERIAL,
                        upstream_writer=writer,  # type: ignore[arg-type]
                        client_device_tail=b"\x12\x34\x56",
                        last_client_func=0x8F,
                    )
                )

            saved = Path(tmpdir) / image.filename
            self.assertEqual(saved.read_bytes(), data)
            manifest = json.loads(saved.with_suffix(".bin.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["sha256"], image.sha256)
            self.assertEqual(manifest["crc16_modbus_le"], image.crc.hex())
            self.assertEqual(manifest["chunks"], len(chunks))
            self.assertEqual(frame(writer.writes[-1]).payload, FIRMWARE_MAGIC)
            self.assertTrue(any(event == "firmware_capture_complete" for event, _ in events))

    async def test_capture_rejects_wrong_target_fail_closed(self) -> None:
        image = FirmwareImage.from_bytes(b"firmware", "test.bin")
        capture = FirmwareCapture(Path("unused"), lambda *_args, **_fields: None, 1)
        raw = make_frame(
            b"\x7e\x7e",
            b"\x11\x00\x00\x01",
            FIRMWARE_ENVELOPE_FUNC,
            build_firmware_metadata(image, TEST_SERIAL),
            b"\xe7\xe7",
        )
        writer = FakeWriter()
        intercepted = await capture.handle_upstream_frame(
            frame(raw),
            serial="A_DIFFERENT_TEST_SERIAL",
            upstream_writer=writer,  # type: ignore[arg-type]
            client_device_tail=b"\x00\x00\x01",
            last_client_func=1,
        )
        self.assertTrue(intercepted)
        self.assertEqual(writer.writes, [])

    async def test_capture_supports_observed_7f_99_variant(self) -> None:
        data = b"older firmware" * 93
        image = FirmwareImage.from_bytes(data, "M1_test_v1.66.bin")
        # The first two bytes are image-specific (1.64 and 1.66 differed in
        # live captures); the interceptor must learn rather than hard-code it.
        from dataclasses import replace

        variant = replace(
            FIRMWARE_VARIANT_7F_99,
            func=0x5D,
            magic=bytes.fromhex("ac 76 7c 7a 4d"),
        )
        name = image.filename.encode("ascii")
        serial = TEST_SERIAL.encode("ascii")
        metadata = b"".join(
            (
                variant.magic,
                b"\x00\x00",
                image.size.to_bytes(2, "big"),
                image.crc[::-1],
                bytes([len(name)]),
                name,
                b"\x00",
                bytes([len(serial)]),
                serial,
                (300).to_bytes(2, "big"),
            )
        )
        writer = FakeWriter()
        with tempfile.TemporaryDirectory() as tmpdir:
            capture = FirmwareCapture(
                Path(tmpdir),
                lambda *_args, **_fields: None,
                9,
                simulate_progress=False,
            )
            start = frame(make_frame(variant.start, b"\x21\x01\x02\x03", variant.func, metadata, variant.end))
            self.assertTrue(
                await capture.handle_upstream_frame(
                    start,
                    serial=TEST_SERIAL,
                    upstream_writer=writer,  # type: ignore[arg-type]
                    client_device_tail=b"\x01\x02\x03",
                    last_client_func=1,
                )
            )
            chunks = [data[pos:pos + 1024] for pos in range(0, len(data), 1024)]
            for index, chunk in enumerate(chunks):
                request = frame(
                    make_frame(
                        variant.start,
                        b"\x22\x01\x02\x03",
                        variant.func,
                        variant.magic + index.to_bytes(2, "big") + chunk,
                        variant.end,
                    )
                )
                self.assertTrue(
                    await capture.handle_upstream_frame(
                        request,
                        serial=TEST_SERIAL,
                        upstream_writer=writer,  # type: ignore[arg-type]
                        client_device_tail=b"\x01\x02\x03",
                        last_client_func=1,
                    )
                )
            final_ack = frame(writer.writes[-1])
            self.assertEqual(final_ack.family, "7f")
            self.assertEqual(final_ack.func, 0x5D)
            self.assertEqual(final_ack.payload, variant.magic + b"\x00")
            manifest = json.loads(
                (Path(tmpdir) / "M1_test_v1.66.bin.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["protocol"], "foxess-7f-dynamic")
            self.assertEqual(manifest["crc16_modbus_le"], image.crc.hex())
            self.assertEqual(manifest["protocol_crc_field"], image.crc[::-1].hex())

    async def test_uploader_waits_for_acknowledgements_and_progress(self) -> None:
        image = FirmwareImage.from_bytes(b"f" * 1500, "M1_test.bin")
        writer = FakeWriter()
        uploader = FirmwareUploader(lambda *_args, **_fields: None, 3, TEST_SERIAL)
        task = asyncio.create_task(
            uploader.upload(
                writer,  # type: ignore[arg-type]
                image,
                protocol="foxess-7f-func-a2",
                ack_timeout_seconds=1,
                progress_timeout_seconds=1,
            )
        )

        for write_index in range(3):
            for _ in range(20):
                if len(writer.writes) > write_index:
                    break
                await asyncio.sleep(0)
            request = frame(writer.writes[write_index])
            magic = request.payload[:5]
            payload = (
                magic + b"\x00\x00\x05"
                if write_index == 0
                else magic + b"\x00\x01\x05"
                if write_index == 1
                else magic + (b"" if request.family == "7e" else b"\x00")
            )
            response = frame(
                make_frame(
                    b"\x7f\x7f" if request.family == "7e" else request.start,
                    b"\xa1" + request.device[1:],
                    request.func,
                    payload,
                    b"\xf7\xf7" if request.family == "7e" else request.end,
                )
            )
            self.assertTrue(uploader.handle_client_frame(response))
            await asyncio.sleep(0)

        for percent in (25, 75, 100):
            progress = frame(
                make_frame(
                    b"\x7f\x7f",
                    b"\xaf\x00\x00\x01",
                    1,
                    magic + b"\x00" + bytes([percent]),
                    b"\xf7\xf7",
                )
            )
            self.assertTrue(uploader.handle_client_frame(progress))
        result = await task
        self.assertEqual(result["status"], "flashed")
        self.assertEqual(result["progress"], [25, 75, 100])
        self.assertEqual(result["protocol"], "foxess-7f-func-a2")
        self.assertEqual(len(writer.writes), 3)
