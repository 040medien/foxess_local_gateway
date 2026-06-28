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


# The 4-byte envelope device field used by the cloud for Modbus commands
# decodes as:
#
#   byte[0]   operation type: 0x11 for reads (Modbus fn 3/4), 0x12 for writes
#             (Modbus fn 6/16). Inverter responds with bit 7 of this byte set.
#   bytes[1-2] little-endian transaction-id counter, advances per request.
#   byte[3]   session marker (any byte, but must be consistent per stream).
#             We pick one ourselves and the inverter happily handles our
#             stream in parallel with the cloud's.
#
# We only emit writes from this module (reads/polling were removed when the
# PR was scoped down to ActivePowerLimit set), so only DEVICE_BYTE_WRITE is
# used in practice; DEVICE_BYTE_READ is kept for documentation / future use.
DEVICE_BYTE_READ = 0x11
DEVICE_BYTE_WRITE = 0x12
# Counter prefix for our injections — kept far from the cloud's observed
# low-thousands range so request transaction-ids don't collide with cloud
# transactions in flight on the same session.
INJECTED_COUNTER_START = 0xF000
# Our chosen byte[3] of the envelope device field. See ``LocalControl.__init__``
# for the full rationale; in short, the inverter accepts any value but every
# request on a given stream must reuse the same one.
INJECTED_SESSION_MARKER = 0xAA

# Outcome of an ActivePowerLimit write, surfaced so Home Assistant / the
# operator can tell whether a setpoint actually reached the inverter.
WRITE_CONFIRMED = "confirmed"      # inverter echoed a successful write-response in time
WRITE_REJECTED = "rejected"        # inverter answered, but with a Modbus exception/NAK
WRITE_TIMEOUT = "timeout"          # write sent, no response within the timeout
WRITE_NO_CONNECTION = "no_connection"  # no (usable) inverter session to write to


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
        self._counter = INJECTED_COUNTER_START
        self.inverter_writer: asyncio.StreamWriter | None = None
        # Normalized device keys of every request we've issued this session.
        # Looked up (without removal) when classifying uplink frames as ours
        # for upstream-stripping purposes.
        self._outstanding: set[bytes] = set()
        # Normalized device key -> Future resolved when the inverter's echoed
        # write-response arrives, so write_register can confirm a setpoint
        # actually landed (or time out).
        self._pending_writes: dict[bytes, "asyncio.Future[bool]"] = {}
        # Last cloud marker we logged via observe_session_marker, kept for
        # dedup so the diagnostic event only fires when it changes.
        self._cloud_marker_seen: int | None = None
        # Self-chosen byte[3] of the envelope device field.
        #
        # The inverter validates byte[3] as a "transaction stream marker" —
        # any value is acceptable, but every request on a given stream must
        # use the same one (mismatches get echoed back as NAK). The cloud
        # picks its own marker per session; we pick ours independently and
        # the inverter happily handles both streams in parallel (verified
        # live 2026-06-10: cloud=0xb3 and ours=0xaa receiving real Modbus
        # responses in the same TCP session).
        #
        # 0xAA is just a constant; could be any byte not currently in use
        # by the cloud on the same TCP session. Collisions are unlikely
        # because we pair this with a counter starting at 0xF000 while the
        # cloud's counter advances from low values.
        self._session_marker: int = INJECTED_SESSION_MARKER

    def attach_inverter_writer(self, writer: asyncio.StreamWriter) -> None:
        self.inverter_writer = writer

    def observe_session_marker(self, marker: int) -> None:
        """Diagnostic. Records the cloud's marker for the session — useful
        for confirming the cloud is in fact using a different value from
        ours and that the two streams coexist. Does not change behaviour."""
        if marker == self._cloud_marker_seen:
            return
        self._cloud_marker_seen = marker
        self._emit(
            "inverter_control_cloud_marker_seen",
            session=self._session_id,
            cloud_marker=marker,
            cloud_marker_hex=f"0x{marker:02x}",
            our_marker_hex=f"0x{self._session_marker:02x}",
        )

    @property
    def session_marker(self) -> int:
        return self._session_marker

    def _next_device(self, op_type: int) -> bytes:
        self._counter = (self._counter + 1) & 0xFFFF
        return bytes(
            [
                op_type,
                self._counter & 0xFF,
                (self._counter >> 8) & 0xFF,
                self._session_marker,
            ]
        )

    def is_our_frame(self, frame: Frame) -> bool:
        """True iff the frame's envelope device matches a request we've
        issued (or its echoed-response form)."""
        if frame.start != b"\x7f\x7f":
            return False
        return normalize_device(bytes(frame.device)) in self._outstanding

    async def write_register(self, address: int, value: int, *, timeout: float = 0.0) -> str:
        """Inject a Modbus write-single-register request and report the outcome.

        Returns ``WRITE_CONFIRMED`` if the inverter's echoed write-response
        arrives within ``timeout`` seconds, ``WRITE_TIMEOUT`` if it does not,
        or ``WRITE_NO_CONNECTION`` if there is no inverter writer attached.
        ``timeout <= 0`` skips the wait (fire-and-forget) and returns
        ``WRITE_CONFIRMED`` once the bytes are sent."""
        device = self._next_device(DEVICE_BYTE_WRITE)
        frame = build_modbus_write_single(device, address, value)
        self._emit(
            "injected_write",
            session=self._session_id,
            device=device.hex(),
            address=address,
            address_hex=f"0x{address:04x}",
            value=value,
        )
        key = normalize_device(device)
        self._outstanding.add(key)
        # Register the awaiter BEFORE sending, so a fast response can't slip in
        # between _send() and the wait.
        future: "asyncio.Future[bool]" | None = None
        if timeout > 0:
            future = asyncio.get_running_loop().create_future()
            self._pending_writes[key] = future
        try:
            sent = await self._send(device, frame)
            if sent is None:
                return WRITE_NO_CONNECTION
            if future is None:
                return WRITE_CONFIRMED
            try:
                confirmed = await asyncio.wait_for(future, timeout)
                return WRITE_CONFIRMED if confirmed else WRITE_REJECTED
            except asyncio.TimeoutError:
                return WRITE_TIMEOUT
        finally:
            self._pending_writes.pop(key, None)

    def resolve_response(self, device: bytes, *, confirmed: bool = True) -> None:
        """Settle the write awaiting this device's echoed response.

        ``confirmed`` is True for a successful write echo and False for a
        Modbus exception/NAK, so a rejected write is not reported as applied.
        Called when an uplink frame is recognized as ours; a no-op for frames
        that aren't an outstanding write (e.g. read responses), so it is safe
        to call for every ``is_our_frame`` frame."""
        future = self._pending_writes.get(normalize_device(bytes(device)))
        if future is not None and not future.done():
            future.set_result(confirmed)

    async def read_holding(self, address: int, count: int) -> bytes | None:
        """Inject a Modbus read-holding-registers request. Used to read back
        the current ActivePowerLimit setting so HA can show its state."""
        device = self._next_device(DEVICE_BYTE_READ)
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

    async def _send(self, device: bytes, frame: bytes) -> bytes | None:
        writer = self.inverter_writer
        # A writer that exists but is closing belongs to a session the inverter
        # has already dropped (the handler still points at the old Session until
        # the inverter reconnects). Treat it as no connection rather than
        # writing into a closing transport and reporting a false confirm/timeout.
        is_closing = getattr(writer, "is_closing", None)
        if writer is None or (callable(is_closing) and is_closing()):
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
