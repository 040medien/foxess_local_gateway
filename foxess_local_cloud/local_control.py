"""Local Modbus control of the inverter.

When enabled in config, a ``LocalControl`` instance attached to a session can
inject its own Modbus read/write requests into the inverter-bound TCP stream
without going through the FoxESS cloud. Each injected request uses a 4-byte
envelope device field that mimics the cloud's observed pattern
(``12 <ctr_hi> <ctr_lo> 0b``) so the inverter accepts it; ours are
distinguished from the cloud's at filter-time by membership in an
``_outstanding`` set tracked per session. The inverter echoes the device
field back in responses with bit 7 of the first byte set, so the set is
keyed by the normalized (bit-7 cleared) form.

The Session is responsible for:

  * Capturing the inverter writer when relay/local mode starts, so the
    control object can write to it.
  * Querying ``is_our_frame`` on uplink frames so responses to our
    injections can be stripped from the bytes forwarded to FoxCloud.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from .protocol import (
    Frame,
    build_modbus_read_holding,
    build_modbus_read_input,
    build_modbus_write_single,
)


def normalize_device(device: bytes) -> bytes:
    """Return the 4-byte envelope device field with bit 7 of the first byte
    cleared, so request and echoed-response forms compare equal.

    The Modbus-over-FoxESS-envelope protocol echoes the device field back in
    responses with ``device[0] | 0x80`` set — this normalization gives a
    single key usable for request → response correlation regardless of
    direction.
    """
    if not device:
        return device
    return bytes([device[0] & 0x7F]) + device[1:4]


# Byte layout of the envelope device field for injected requests. Mimics the
# cloud's observed pattern (first byte 0x12, last byte 0x0b) so the inverter
# accepts the frame on the same basis it accepts cloud-originated ones. The
# 16-bit middle is a per-session counter — ours are distinguished from the
# cloud's at filter-time by ``LocalControl._outstanding`` membership, not by
# any byte-level marker.
INJECTED_DEVICE_FIRST = 0x12
INJECTED_DEVICE_LAST = 0x0B


class LocalControl:
    """Per-session helper that injects Modbus requests into the inverter
    stream and tracks them so responses can be matched and filtered."""

    def __init__(
        self,
        *,
        emit: Callable[..., None],
        session_id: int,
        register_pending_read: Callable[[bytes, int, int], None],
    ) -> None:
        self._emit = emit
        self._session_id = session_id
        self._register_pending_read = register_pending_read
        self._counter = 0
        self.inverter_writer: asyncio.StreamWriter | None = None
        # Normalized device keys of every request we've issued this session.
        # Looked up (without removal) when classifying uplink frames as ours
        # for upstream-stripping purposes.
        self._outstanding: set[bytes] = set()

    def attach_inverter_writer(self, writer: asyncio.StreamWriter) -> None:
        self.inverter_writer = writer

    def _next_device(self) -> bytes:
        self._counter = (self._counter + 1) & 0xFFFF
        return bytes(
            [
                INJECTED_DEVICE_FIRST,
                (self._counter >> 8) & 0xFF,
                self._counter & 0xFF,
                INJECTED_DEVICE_LAST,
            ]
        )

    def is_our_frame(self, frame: Frame) -> bool:
        """True iff the frame's envelope device matches a request we've
        issued (or its echoed-response form)."""
        if frame.start != b"\x7f\x7f":
            return False
        return normalize_device(bytes(frame.device)) in self._outstanding

    async def write_register(self, address: int, value: int) -> bytes | None:
        """Inject a Modbus write-single-register request. Returns the device
        bytes used, or None if no inverter writer is attached."""
        device = self._next_device()
        frame = build_modbus_write_single(device, address, value)
        self._emit(
            "injected_write",
            session=self._session_id,
            device=device.hex(),
            address=address,
            address_hex=f"0x{address:04x}",
            value=value,
        )
        self._outstanding.add(normalize_device(device))
        return await self._send(device, frame)

    async def read_holding(self, address: int, count: int) -> bytes | None:
        device = self._next_device()
        frame = build_modbus_read_holding(device, address, count)
        self._register_pending_read(normalize_device(device), address, count)
        self._emit(
            "injected_read",
            session=self._session_id,
            device=device.hex(),
            function="read_holding",
            address=address,
            address_hex=f"0x{address:04x}",
            count=count,
        )
        self._outstanding.add(normalize_device(device))
        return await self._send(device, frame)

    async def read_input(self, address: int, count: int) -> bytes | None:
        device = self._next_device()
        frame = build_modbus_read_input(device, address, count)
        self._register_pending_read(normalize_device(device), address, count)
        self._emit(
            "injected_read",
            session=self._session_id,
            device=device.hex(),
            function="read_input",
            address=address,
            address_hex=f"0x{address:04x}",
            count=count,
        )
        self._outstanding.add(normalize_device(device))
        return await self._send(device, frame)

    async def _send(self, device: bytes, frame: bytes) -> bytes | None:
        writer = self.inverter_writer
        if writer is None:
            self._emit(
                "injected_drop",
                session=self._session_id,
                reason="no_inverter_writer",
                device=device.hex(),
            )
            return None
        writer.write(frame)
        await writer.drain()
        return device
