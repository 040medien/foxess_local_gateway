"""Local Modbus control of the inverter.

When enabled in config, a ``LocalControl`` instance attached to a session can
inject its own Modbus read/write requests into the inverter-bound TCP stream
without going through the FoxESS cloud. Each injected request uses a 4-byte
envelope device field whose first byte carries ``INJECTED_DEVICE_MARKER``
(0x7f) so the response — which the inverter echoes back with bit 7 of that
byte set — is easy to recognise as ours and strip out of the upstream-bound
byte stream.

The Session is responsible for:

  * Capturing the inverter writer when relay/local mode starts, so the
    control object can write to it.
  * Calling ``consume_response`` from its uplink frame handler so the
    response can be classified and (in the relay loop) stripped from the
    bytes forwarded to FoxCloud.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from .protocol import (
    INJECTED_DEVICE_MARKER,
    Frame,
    build_modbus_read_holding,
    build_modbus_read_input,
    build_modbus_write_single,
    is_injected_device,
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


class LocalControl:
    """Per-session helper that injects Modbus requests into the inverter
    stream and tracks them so responses can be matched and filtered.

    Callers are expected to serialize concurrent writes externally (the
    inverter handles one Modbus transaction at a time).
    """

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

    def attach_inverter_writer(self, writer: asyncio.StreamWriter) -> None:
        self.inverter_writer = writer

    def _next_device(self) -> bytes:
        self._counter = (self._counter + 1) & 0xFFFFFF
        return bytes([INJECTED_DEVICE_MARKER]) + self._counter.to_bytes(3, "big")

    def is_our_frame(self, frame: Frame) -> bool:
        """True iff the frame's envelope marks it as one of ours (request or
        echoed response)."""
        return frame.start == b"\x7f\x7f" and is_injected_device(frame.device)

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
