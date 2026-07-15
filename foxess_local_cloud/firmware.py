"""FoxESS M-series firmware transfer parsing, capture, and local upload.

Two protocol variants were observed on FoxCloud relay sessions on 2026-07-15.
Firmware is carried inline in framed TCP/14431 messages; there is no
inverter-side HTTP download. This module deliberately keeps firmware handling
separate from Home Assistant and ordinary Modbus control.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .protocol import Frame, crc16_le, make_frame


FIRMWARE_MAGIC = bytes.fromhex("fa6b7c7a4d")
FIRMWARE_ENVELOPE_FUNC = 0xA2
FIRMWARE_CHUNK_SIZE = 1024
FIRMWARE_TIMEOUT_SECONDS = 300
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._() -]+")


class FirmwareProtocolError(ValueError):
    """The firmware image or transfer did not match the observed protocol."""


@dataclass(frozen=True)
class FirmwareVariant:
    name: str
    start: bytes
    end: bytes
    family: str
    func: int
    magic: bytes
    filename_has_length: bool
    crc_field_big_endian: bool
    final_ack_suffix: bytes

    def crc_field(self, crc_le: bytes) -> bytes:
        return crc_le[::-1] if self.crc_field_big_endian else crc_le


FIRMWARE_VARIANT_7E_A2 = FirmwareVariant(
    "foxess-7e-func-a2",
    b"\x7e\x7e",
    b"\xe7\xe7",
    "7e",
    FIRMWARE_ENVELOPE_FUNC,
    FIRMWARE_MAGIC,
    True,
    False,
    b"",
)
FIRMWARE_VARIANT_7F_99 = FirmwareVariant(
    "foxess-7f-dynamic",
    b"\x7f\x7f",
    b"\xf7\xf7",
    "7f",
    0x99,
    bytes.fromhex("01 74 7c 7a 4d"),
    True,
    True,
    b"\x00",
)
FIRMWARE_VARIANT_7F_A2 = replace(
    FIRMWARE_VARIANT_7F_99,
    name="foxess-7f-func-a2",
    func=FIRMWARE_ENVELOPE_FUNC,
    magic=FIRMWARE_MAGIC,
)
FIRMWARE_VARIANTS = (
    FIRMWARE_VARIANT_7E_A2,
    FIRMWARE_VARIANT_7F_99,
    FIRMWARE_VARIANT_7F_A2,
)


@dataclass(frozen=True)
class FirmwareMetadata:
    size: int
    crc: bytes
    filename: str
    serial: str
    timeout_seconds: int
    variant: FirmwareVariant = FIRMWARE_VARIANT_7E_A2


@dataclass(frozen=True)
class FirmwareImage:
    data: bytes
    filename: str
    size: int
    crc: bytes
    sha256: str

    @classmethod
    def from_bytes(cls, data: bytes, filename: str) -> "FirmwareImage":
        if not data:
            raise FirmwareProtocolError("firmware image is empty")
        if len(data) > 0xFFFF:
            raise FirmwareProtocolError("firmware image exceeds the protocol's 65535-byte size field")
        safe_name = safe_firmware_filename(filename)
        return cls(
            data=data,
            filename=safe_name,
            size=len(data),
            crc=crc16_le(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )


def safe_firmware_filename(filename: str) -> str:
    name = Path(filename).name.strip()
    name = _SAFE_FILENAME.sub("_", name)
    if not name or name in (".", ".."):
        raise FirmwareProtocolError("invalid firmware filename")
    encoded = name.encode("ascii", errors="strict")
    if len(encoded) > 255:
        raise FirmwareProtocolError("firmware filename is too long")
    return name


def variant_for_payload(payload: bytes) -> FirmwareVariant | None:
    if payload.startswith(FIRMWARE_VARIANT_7E_A2.magic):
        return FIRMWARE_VARIANT_7F_A2
    # In 7f/0x99 transfers the first two bytes identify the particular
    # firmware image; only the trailing 7c 7a 4d signature is stable. Learn
    # the full five-byte transfer id from metadata and require chunks to keep
    # using it.
    if len(payload) >= 5 and payload[2:5] == bytes.fromhex("7c 7a 4d"):
        return replace(FIRMWARE_VARIANT_7F_99, magic=payload[:5])
    return None


def parse_firmware_metadata(
    payload: bytes,
    variant: FirmwareVariant | None = None,
) -> FirmwareMetadata:
    variant = variant or variant_for_payload(payload)
    if variant is None or len(payload) < 15 or not payload.startswith(variant.magic + b"\x00\x00"):
        raise FirmwareProtocolError("not a firmware start payload")
    size = int.from_bytes(payload[7:9], "big")
    crc = payload[9:11]
    if variant.filename_has_length:
        name_len = payload[11]
        name_start = 12
        name_end = name_start + name_len
    else:
        name_start = 11
        try:
            name_end = payload.index(0, name_start)
        except ValueError as exc:
            raise FirmwareProtocolError("firmware filename is not NUL-terminated") from exc
    if name_end + 4 > len(payload):
        raise FirmwareProtocolError("truncated firmware metadata filename")
    try:
        filename = payload[name_start:name_end].decode("ascii")
    except UnicodeDecodeError as exc:
        raise FirmwareProtocolError("firmware filename is not ASCII") from exc
    safe_firmware_filename(filename)
    if payload[name_end] != 0:
        raise FirmwareProtocolError("firmware filename is not NUL-terminated")
    serial_len = payload[name_end + 1]
    serial_start = name_end + 2
    serial_end = serial_start + serial_len
    if serial_end + 2 != len(payload):
        raise FirmwareProtocolError("truncated firmware metadata serial or timeout")
    try:
        serial = payload[serial_start:serial_end].decode("ascii")
    except UnicodeDecodeError as exc:
        raise FirmwareProtocolError("firmware target serial is not ASCII") from exc
    timeout_seconds = int.from_bytes(payload[serial_end:serial_end + 2], "big")
    return FirmwareMetadata(size, crc, filename, serial, timeout_seconds, variant)


def build_firmware_metadata(
    image: FirmwareImage,
    serial: str,
    variant: FirmwareVariant = FIRMWARE_VARIANT_7E_A2,
) -> bytes:
    serial_bytes = serial.encode("ascii", errors="strict")
    if not serial_bytes or len(serial_bytes) > 255:
        raise FirmwareProtocolError("invalid target serial length")
    name = image.filename.encode("ascii")
    filename_field = bytes([len(name)]) + name + b"\x00" if variant.filename_has_length else name + b"\x00"
    return b"".join(
        (
            variant.magic,
            b"\x00\x00",
            image.size.to_bytes(2, "big"),
            variant.crc_field(image.crc),
            filename_field,
            bytes([len(serial_bytes)]),
            serial_bytes,
            FIRMWARE_TIMEOUT_SECONDS.to_bytes(2, "big"),
        )
    )


def is_firmware_transfer_frame(frame: Frame) -> bool:
    return firmware_variant_for_frame(frame) is not None


def firmware_variant_for_frame(frame: Frame) -> FirmwareVariant | None:
    if (
        frame.family == FIRMWARE_VARIANT_7E_A2.family
        and frame.func == FIRMWARE_VARIANT_7E_A2.func
        and frame.payload.startswith(FIRMWARE_VARIANT_7E_A2.magic)
    ):
        return FIRMWARE_VARIANT_7E_A2
    if (
        frame.family == FIRMWARE_VARIANT_7F_A2.family
        and frame.func == FIRMWARE_VARIANT_7F_A2.func
        and frame.payload.startswith(FIRMWARE_VARIANT_7F_A2.magic)
    ):
        return FIRMWARE_VARIANT_7F_A2
    if (
        frame.family == FIRMWARE_VARIANT_7F_99.family
        and frame.device[:1] in (b"\x21", b"\x22")
        and len(frame.payload) >= 5
        and frame.payload[2:5] == bytes.fromhex("7c 7a 4d")
    ):
        return replace(
            FIRMWARE_VARIANT_7F_99,
            func=frame.func,
            magic=frame.payload[:5],
        )
    return None


class FirmwareCapture:
    """Capture a FoxCloud firmware push while impersonating its target.

    The caller removes every frame for which ``handle_upstream_frame`` returns
    true from the inverter-bound stream. Acknowledgements are written directly
    to the cloud connection, so the physical inverter never enters its updater.
    """

    def __init__(
        self,
        directory: Path,
        emit: Callable[..., None],
        session_id: int,
        *,
        simulate_progress: bool = True,
        progress_interval_seconds: float = 0.25,
    ) -> None:
        self.directory = directory
        self.emit = emit
        self.session_id = session_id
        self.simulate_progress = simulate_progress
        self.progress_interval_seconds = progress_interval_seconds
        self.metadata: FirmwareMetadata | None = None
        self.variant: FirmwareVariant | None = None
        self.data = bytearray()
        self.expected_chunk = 0
        self._progress_task: asyncio.Task[None] | None = None

    async def handle_upstream_frame(
        self,
        frame: Frame,
        *,
        serial: str,
        upstream_writer: asyncio.StreamWriter,
        client_device_tail: bytes,
        last_client_func: int,
    ) -> bool:
        variant = firmware_variant_for_frame(frame)
        if variant is None:
            return False

        if not frame.valid_crc:
            self.emit(
                "firmware_capture_failed",
                session=self.session_id,
                serial=serial,
                reason="invalid_frame_crc",
                protocol=variant.name,
            )
            return True

        try:
            metadata = parse_firmware_metadata(frame.payload, variant)
        except FirmwareProtocolError:
            metadata = None

        if metadata is not None:
            if not serial or metadata.serial != serial:
                self.emit(
                    "firmware_capture_rejected",
                    session=self.session_id,
                    serial=serial,
                    reason="serial_mismatch",
                    target_serial=metadata.serial,
                )
                # Capture mode's primary safety promise is that no firmware
                # transfer reaches physical hardware. Do not acknowledge a
                # mismatched target, but always remove the frame.
                return True
            self.metadata = metadata
            self.variant = variant
            self.data.clear()
            self.expected_chunk = 0
            self.emit(
                "firmware_capture_started",
                session=self.session_id,
                serial=serial,
                filename=metadata.filename,
                size=metadata.size,
                crc16=metadata.crc.hex(),
                timeout_seconds=metadata.timeout_seconds,
                protocol=variant.name,
            )
            await self._send_ack(
                upstream_writer,
                frame,
                variant.magic + b"\x00\x00\x05",
                0xA1,
                variant,
            )
            return True

        if self.metadata is None or self.variant is None or len(frame.payload) < 7:
            self.emit(
                "firmware_capture_rejected",
                session=self.session_id,
                serial=serial,
                reason="chunk_without_metadata",
            )
            # A transfer may have started before this relay session was fully
            # observed. Keep capture mode fail-closed: never leak a firmware
            # chunk to the inverter merely because we cannot save it.
            return True

        if variant != self.variant:
            self.emit(
                "firmware_capture_rejected",
                session=self.session_id,
                serial=serial,
                reason="protocol_changed_mid_transfer",
                expected_protocol=self.variant.name,
                received_protocol=variant.name,
            )
            return True

        chunk_index = int.from_bytes(frame.payload[5:7], "big")
        if chunk_index != self.expected_chunk:
            self.emit(
                "firmware_capture_chunk_error",
                session=self.session_id,
                serial=serial,
                expected_chunk=self.expected_chunk,
                received_chunk=chunk_index,
            )
            # Tell FoxCloud which chunk is still required; do not expose a
            # malformed/duplicate firmware transfer to the real inverter.
            ack = variant.magic + self.expected_chunk.to_bytes(2, "big") + b"\x05"
            await self._send_ack(upstream_writer, frame, ack, 0xA1, variant)
            return True

        chunk = frame.payload[7:]
        self.data.extend(chunk)
        self.expected_chunk += 1
        if len(self.data) > self.metadata.size:
            self.emit(
                "firmware_capture_failed",
                session=self.session_id,
                serial=serial,
                reason="image_too_large",
                expected_size=self.metadata.size,
                received_size=len(self.data),
            )
            return True

        if len(self.data) < self.metadata.size:
            ack = variant.magic + self.expected_chunk.to_bytes(2, "big") + b"\x05"
            await self._send_ack(upstream_writer, frame, ack, 0xA1, variant)
            return True

        image = bytes(self.data)
        actual_crc = crc16_le(image)
        if variant.crc_field(actual_crc) != self.metadata.crc:
            self.emit(
                "firmware_capture_failed",
                session=self.session_id,
                serial=serial,
                reason="crc_mismatch",
                expected_crc16=self.metadata.crc.hex(),
                actual_crc16=actual_crc.hex(),
                protocol_crc_field=variant.crc_field(actual_crc).hex(),
            )
            return True

        try:
            image_path, manifest_path, sha256 = self._save_capture(image, serial)
        except OSError as exc:
            self.emit(
                "firmware_capture_failed",
                session=self.session_id,
                serial=serial,
                reason="save_failed",
                error=str(exc),
                directory=str(self.directory),
            )
            # Keep the cloud connection alive but withhold the final ACK. The
            # cloud can retry; the physical inverter remains protected.
            return True
        await self._send_ack(
            upstream_writer,
            frame,
            variant.magic + variant.final_ack_suffix,
            0xA2,
            variant,
        )
        self.emit(
            "firmware_capture_complete",
            session=self.session_id,
            serial=serial,
            filename=self.metadata.filename,
            path=str(image_path),
            manifest_path=str(manifest_path),
            size=len(image),
            crc16=actual_crc.hex(),
            sha256=sha256,
            chunks=self.expected_chunk,
        )
        if self.simulate_progress and (self._progress_task is None or self._progress_task.done()):
            self._progress_task = asyncio.create_task(
                self._simulate_flash_progress(
                    upstream_writer,
                    serial=serial,
                    device_tail=client_device_tail,
                    starting_func=last_client_func,
                    variant=variant,
                )
            )
        self.metadata = None
        self.variant = None
        self.data.clear()
        self.expected_chunk = 0
        return True

    async def _send_ack(
        self,
        writer: asyncio.StreamWriter,
        request: Frame,
        payload: bytes,
        first_device_byte: int,
        variant: FirmwareVariant,
    ) -> None:
        device = bytes([first_device_byte]) + request.device[1:]
        raw = make_frame(variant.start, device, request.func, payload, variant.end)
        writer.write(raw)
        await writer.drain()
        self.emit(
            "firmware_capture_ack",
            session=self.session_id,
            device=device.hex(),
            payload_hex=payload.hex(" "),
        )

    def _save_capture(self, image: bytes, serial: str) -> tuple[Path, Path, str]:
        assert self.metadata is not None
        variant = self.metadata.variant
        self.directory.mkdir(parents=True, exist_ok=True)
        filename = safe_firmware_filename(self.metadata.filename)
        image_path = self.directory / filename
        sha256 = hashlib.sha256(image).hexdigest()
        temporary = image_path.with_name(image_path.name + ".tmp")
        temporary.write_bytes(image)
        os.replace(temporary, image_path)
        manifest_path = image_path.with_suffix(image_path.suffix + ".json")
        manifest = {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "foxcloud-relay",
            "filename": filename,
            "serial": serial,
            "size": len(image),
            "crc16_modbus_le": crc16_le(image).hex(),
            "protocol_crc_field": self.metadata.crc.hex(),
            "sha256": sha256,
            "chunks": self.expected_chunk,
            "protocol": variant.name,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return image_path, manifest_path, sha256

    async def _simulate_flash_progress(
        self,
        writer: asyncio.StreamWriter,
        *,
        serial: str,
        device_tail: bytes,
        starting_func: int,
        variant: FirmwareVariant,
    ) -> None:
        if len(device_tail) != 3:
            device_tail = b"\x00\x00\x00"
        func = starting_func
        try:
            for percent in (18, 38, 58, 77, 97, 100):
                await asyncio.sleep(self.progress_interval_seconds)
                func = (func + 1) & 0xFF
                payload = variant.magic + b"\x00" + bytes([percent])
                raw = make_frame(b"\x7f\x7f", b"\xaf" + device_tail, func, payload, b"\xf7\xf7")
                writer.write(raw)
                await writer.drain()
                self.emit(
                    "firmware_capture_progress_simulated",
                    session=self.session_id,
                    serial=serial,
                    percent=percent,
                )
        except (ConnectionError, OSError, RuntimeError) as exc:
            self.emit(
                "firmware_capture_progress_error",
                session=self.session_id,
                serial=serial,
                error=str(exc),
            )


class FirmwareUploader:
    """Upload one validated image through an already-connected Session."""

    def __init__(self, emit: Callable[..., None], session_id: int, serial: str) -> None:
        self.emit = emit
        self.session_id = session_id
        self.serial = serial
        self.active = False
        self._expected_tail: bytes | None = None
        self._ack_future: asyncio.Future[bytes] | None = None
        self._progress_queue: asyncio.Queue[int] = asyncio.Queue()
        self._counter = int(time.monotonic_ns()) & 0xFFFFFF
        self._variant: FirmwareVariant | None = None

    def handle_client_frame(self, frame: Frame) -> bool:
        variant = self._variant
        if not self.active or variant is None or not frame.payload.startswith(variant.magic):
            return False
        # Progress frames use a separate device tail, and their function byte
        # can collide with the dynamically selected request function. Classify
        # their unambiguous seven-byte payload before applying ack-tail checks.
        if frame.family == "7f" and len(frame.payload) == 7 and frame.payload[5] == 0:
            self._progress_queue.put_nowait(frame.payload[6])
            return True
        # The original 1.84 cloud transfer sends requests in 7e/A2 frames,
        # but the inverter acknowledges them in 7f frames with the same
        # function and request tail. The newer variant uses 7f both ways.
        if frame.family in (variant.family, "7f") and frame.func == variant.func:
            if self._expected_tail is None or frame.device[1:] != self._expected_tail:
                return False
            future = self._ack_future
            if future is not None and not future.done():
                future.set_result(frame.payload)
            return True
        return False

    async def upload(
        self,
        writer: asyncio.StreamWriter,
        image: FirmwareImage,
        *,
        protocol: str = "foxess-7f-dynamic",
        ack_timeout_seconds: float = 10.0,
        progress_timeout_seconds: float = 180.0,
    ) -> dict[str, Any]:
        if self.active:
            raise FirmwareProtocolError("a firmware upload is already active")
        self.active = True
        serial = self.serial
        try:
            while not self._progress_queue.empty():
                self._progress_queue.get_nowait()
            if protocol == "foxess-7f-func-99":
                # Compatibility with manifests created before the function
                # byte was also observed to vary between cloud transfers.
                protocol = FIRMWARE_VARIANT_7F_99.name
            if protocol == FIRMWARE_VARIANT_7E_A2.name:
                variant = FIRMWARE_VARIANT_7E_A2
            elif protocol == FIRMWARE_VARIANT_7F_A2.name:
                variant = FIRMWARE_VARIANT_7F_A2
            elif protocol == FIRMWARE_VARIANT_7F_99.name:
                variant = replace(
                    FIRMWARE_VARIANT_7F_99,
                    func=secrets.randbelow(256),
                    magic=secrets.token_bytes(2) + bytes.fromhex("7c 7a 4d"),
                )
            else:
                raise FirmwareProtocolError(f"unsupported firmware upload protocol {protocol!r}")
            self._variant = variant
            metadata = build_firmware_metadata(image, serial, variant)
            await self._send_and_expect(
                writer,
                first_device_byte=0x21,
                payload=metadata,
                expected=variant.magic + b"\x00\x00\x05",
                timeout=ack_timeout_seconds,
            )
            chunks = [image.data[i:i + FIRMWARE_CHUNK_SIZE] for i in range(0, image.size, FIRMWARE_CHUNK_SIZE)]
            for index, chunk in enumerate(chunks):
                payload = variant.magic + index.to_bytes(2, "big") + chunk
                expected = (
                    variant.magic + variant.final_ack_suffix
                    if index == len(chunks) - 1
                    else variant.magic + (index + 1).to_bytes(2, "big") + b"\x05"
                )
                await self._send_and_expect(
                    writer,
                    first_device_byte=0x22,
                    payload=payload,
                    expected=expected,
                    timeout=ack_timeout_seconds,
                )
                self.emit(
                    "firmware_upload_chunk_acknowledged",
                    session=self.session_id,
                    serial=serial,
                    chunk=index,
                    chunks=len(chunks),
                )
            self.emit(
                "firmware_upload_image_accepted",
                session=self.session_id,
                serial=serial,
                filename=image.filename,
                size=image.size,
                crc16=image.crc.hex(),
                sha256=image.sha256,
                chunks=len(chunks),
            )
            progress: list[int] = []
            deadline = asyncio.get_running_loop().time() + progress_timeout_seconds
            while not progress or progress[-1] < 100:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("timed out waiting for firmware flash progress")
                percent = await asyncio.wait_for(self._progress_queue.get(), timeout=remaining)
                progress.append(percent)
                self.emit(
                    "firmware_upload_progress",
                    session=self.session_id,
                    serial=serial,
                    percent=percent,
                )
            return {
                "status": "flashed",
                "serial": serial,
                "filename": image.filename,
                "size": image.size,
                "crc16": image.crc.hex(),
                "sha256": image.sha256,
                "chunks": len(chunks),
                "progress": progress,
                "protocol": variant.name,
            }
        finally:
            self.active = False
            self._expected_tail = None
            self._ack_future = None
            self._variant = None

    async def _send_and_expect(
        self,
        writer: asyncio.StreamWriter,
        *,
        first_device_byte: int,
        payload: bytes,
        expected: bytes,
        timeout: float,
    ) -> None:
        self._counter = (self._counter + 1) & 0xFFFFFF
        tail = self._counter.to_bytes(3, "big")
        device = bytes([first_device_byte]) + tail
        loop = asyncio.get_running_loop()
        self._expected_tail = tail
        self._ack_future = loop.create_future()
        variant = self._variant
        if variant is None:
            raise FirmwareProtocolError("firmware upload protocol is not initialized")
        raw = make_frame(variant.start, device, variant.func, payload, variant.end)
        writer.write(raw)
        await writer.drain()
        received = await asyncio.wait_for(self._ack_future, timeout=timeout)
        if received != expected:
            raise FirmwareProtocolError(
                f"unexpected firmware acknowledgement: expected {expected.hex()}, got {received.hex()}"
            )
