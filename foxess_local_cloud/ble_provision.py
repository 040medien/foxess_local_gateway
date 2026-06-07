"""BLE-based WiFi provisioning for FoxESS M1 inverters.

Drives the same bytes a FoxCloud-compatible mobile app sends over Bluetooth
LE, so the Pi can re-point an M1 at the local AP without needing the mobile
app. Verified byte-for-byte against an HCI snoop of the FoxCloud-compatible app.

Protocol summary:

1. Connect to the M1 advertised as ``MI_<serial>``, subscribe to notify on the
   ``0xFF01`` characteristic.
2. Run the standard ``7e7e`` bootstrap exchange via the existing
   :class:`foxess_local_cloud.protocol.BootstrapResponder`.
3. Switch to the ``7f7f`` framing family for the commissioning commands:

   - ``func=0xa1`` -- scan request; the inverter replies with a list of
     nearby SSIDs in the same family.
   - ``func=0xad`` -- ``select`` (a one-byte opaque token; the inverter
     echoes it back). Observed verbatim and replayed as-is.
   - ``func=0xae`` -- set credentials. Payload: ``03 f3 ba 40 <ssid_len:1>
     <ssid> <pw_len:1> <passphrase>``.
   - ``func=0xb1`` -- commit (payload ``04``). The inverter typically
     disconnects with HCI reason ``0x13`` shortly after acknowledging this,
     to associate to the newly configured AP.

The frame-encoding helpers in this module are pure and can be exercised
without any BLE hardware. The async ``scan_for_inverters`` / ``list_networks``
/ ``provision`` functions require ``bleak`` (loaded lazily so unit tests can
import this module without it installed).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from foxess_local_cloud import protocol as p


# GATT characteristic that carries the proprietary FoxESS protocol over BLE.
CHAR_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"

# Inverter BLE advertisements use this name prefix followed by the serial.
NAME_PREFIX = "MI_"

# Fixed manufacturer/model signature seen at the head of WiFi-config payloads.
MFG_SIG = b"\x03\xf3\xba\x40"

# Constants observed in the FoxCloud-compatible BLE capture. We mimic exactly.
SELECT_TOKEN = b"\x09"
COMMIT_PAYLOAD = b"\x04"

# High byte of the device field for outgoing 7f7f frames. The reference
# capture uses 0x3a for control frames and 0x3b for the set-credentials frame.
ALT_HIGH_BYTE = 0x3A
SET_CREDS_HIGH_BYTE = 0x3B

# Function bytes (7f7f family).
FN_SCAN = 0xA1
FN_SELECT = 0xAD
FN_SET_CREDS = 0xAE
FN_COMMIT = 0xB1


@dataclass(frozen=True)
class DiscoveredInverter:
    """An M1 inverter found by BLE advertising scan."""

    address: str
    name: str
    rssi: int

    @property
    def serial(self) -> str:
        return self.name[len(NAME_PREFIX):] if self.name.startswith(NAME_PREFIX) else ""


@dataclass(frozen=True)
class ScannedNetwork:
    """A WiFi network visible to the inverter, as reported in the scan response."""

    ssid: str
    rssi: int


class ProvisioningError(RuntimeError):
    """Raised when BLE provisioning fails (timeout, bad response, etc.)."""


# --- Frame helpers (pure; no BLE) ---------------------------------------------

def derive_device_tail(registration: p.Frame) -> bytes:
    """Return the 3-byte device tail shared between bootstrap and 7f7f frames.

    The M1 sends its registration with a device field ``XX YY ZZ WW``; the
    tail is ``(YY ZZ WW) - 0x71`` interpreted as a 24-bit big-endian number,
    which matches the value the existing :func:`bootstrap_response_device`
    in ``protocol.py`` derives.
    """
    return p.bootstrap_response_device(registration.device, 0)[1:]


def _alt_frame(device: bytes, func: int, payload: bytes) -> bytes:
    return p.make_frame(b"\x7f\x7f", device, func, payload, b"\xf7\xf7")


def _alt_device(tail: bytes, high_byte: int) -> bytes:
    return bytes([high_byte]) + tail


def make_scan_request(tail: bytes) -> bytes:
    # The reference capture sends payload 0x06 here; treated as an opaque constant.
    return _alt_frame(_alt_device(tail, ALT_HIGH_BYTE), FN_SCAN, b"\x06")


def make_select_request(tail: bytes) -> bytes:
    return _alt_frame(_alt_device(tail, ALT_HIGH_BYTE), FN_SELECT, SELECT_TOKEN)


def make_set_credentials(tail: bytes, ssid: str, passphrase: str) -> bytes:
    ssid_bytes = ssid.encode("utf-8")
    pass_bytes = passphrase.encode("utf-8")
    if not 1 <= len(ssid_bytes) <= 32:
        raise ValueError(f"SSID must be 1..32 bytes, got {len(ssid_bytes)}")
    if not 8 <= len(pass_bytes) <= 63:
        raise ValueError(
            f"WPA passphrase must be 8..63 bytes (WPA2 PSK standard), got {len(pass_bytes)}"
        )
    payload = (
        MFG_SIG
        + bytes([len(ssid_bytes)]) + ssid_bytes
        + bytes([len(pass_bytes)]) + pass_bytes
    )
    return _alt_frame(_alt_device(tail, SET_CREDS_HIGH_BYTE), FN_SET_CREDS, payload)


def make_commit(tail: bytes) -> bytes:
    return _alt_frame(_alt_device(tail, ALT_HIGH_BYTE), FN_COMMIT, COMMIT_PAYLOAD)


def parse_scan_response(frame: p.Frame) -> list[ScannedNetwork]:
    """Decode a 7f7f scan-response frame into a list of (ssid, rssi)."""
    if frame.start != b"\x7f\x7f" or frame.func != FN_SCAN:
        raise ValueError(
            f"not a scan response: start={frame.start.hex()} func={frame.func:#04x}"
        )
    body = frame.payload
    if len(body) < 2:
        return []
    count = body[1]
    networks: list[ScannedNetwork] = []
    off = 2
    for _ in range(count):
        if off + 2 > len(body):
            break
        rec_len = body[off]
        if rec_len < 1 or off + 1 + rec_len > len(body):
            break
        rssi_byte = body[off + 1]
        ssid_bytes = body[off + 2 : off + 1 + rec_len]
        rssi = rssi_byte - 256 if rssi_byte > 127 else rssi_byte
        ssid = ssid_bytes.decode("utf-8", errors="replace")
        networks.append(ScannedNetwork(ssid=ssid, rssi=rssi))
        off += 1 + rec_len
    return networks


# --- BLE session (requires bleak) ---------------------------------------------

class _BleSession:
    """One BLE connection lifecycle: bootstrap + commissioning commands."""

    def __init__(self, client) -> None:
        self.client = client
        self._buf = bytearray()
        self._queue: asyncio.Queue = asyncio.Queue()

    def _on_notify(self, _handle, data: bytearray) -> None:
        self._buf.extend(data)
        for frame in p.extract_frames(self._buf):
            if frame.valid_crc:
                self._queue.put_nowait(frame)

    async def start(self) -> None:
        await self.client.start_notify(CHAR_UUID, self._on_notify)

    async def stop(self) -> None:
        try:
            await self.client.stop_notify(CHAR_UUID)
        except Exception:
            pass

    async def _write(self, data: bytes) -> None:
        await self.client.write_gatt_char(CHAR_UUID, data, response=False)

    async def _await_frame(
        self,
        *,
        start: bytes | None = None,
        func: int | None = None,
        timeout: float = 10.0,
    ) -> p.Frame:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ProvisioningError(
                    f"timeout waiting for frame start={start!r} func={func!r}"
                )
            frame = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            if start is not None and frame.start != start:
                continue
            if func is not None and frame.func != func:
                continue
            return frame

    async def bootstrap(self) -> tuple[bytes, str]:
        """Run the 7e7e bootstrap exchange; return ``(device_tail, serial)``."""
        responder = p.BootstrapResponder()
        registration = await self._await_frame(start=b"\x7e\x7e", timeout=15.0)
        if not p.is_registration(registration):
            raise ProvisioningError(
                f"first frame not a registration (func={registration.func:#04x})"
            )
        serial = p.registration_serial(registration) or ""
        tail = derive_device_tail(registration)

        current = registration
        while responder.step < 3:
            response = responder.response_for(current)
            if response is None:
                raise ProvisioningError(
                    f"bootstrap step {responder.step} produced no response"
                )
            await self._write(response)
            if responder.step >= 3:
                break
            current = await self._await_frame(start=b"\x7e\x7e", timeout=10.0)
        return tail, serial

    async def list_networks(self, tail: bytes) -> list[ScannedNetwork]:
        await self._write(make_scan_request(tail))
        # The inverter scans the radio environment before replying; in the
        # reference capture this took ~5 seconds, so allow plenty.
        scan_frame = await self._await_frame(
            start=b"\x7f\x7f", func=FN_SCAN, timeout=20.0
        )
        return parse_scan_response(scan_frame)

    async def set_credentials(self, tail: bytes, ssid: str, passphrase: str) -> None:
        await self._write(make_select_request(tail))
        await self._await_frame(start=b"\x7f\x7f", func=FN_SELECT, timeout=5.0)
        await self._write(make_set_credentials(tail, ssid, passphrase))
        await self._await_frame(start=b"\x7f\x7f", func=FN_SET_CREDS, timeout=5.0)
        await self._write(make_commit(tail))
        # The inverter often disconnects before acking commit; treat that as success.
        try:
            await self._await_frame(start=b"\x7f\x7f", func=FN_COMMIT, timeout=5.0)
        except (ProvisioningError, Exception):
            pass


# --- Public async entry points -------------------------------------------------

async def scan_for_inverters(timeout: float = 8.0) -> list[DiscoveredInverter]:
    """Active-scan for BLE advertisements; return ``MI_<serial>`` peers, strongest first."""
    from bleak import BleakScanner

    seen: dict[str, DiscoveredInverter] = {}

    def callback(device, adv):
        name = (adv.local_name or device.name or "")
        if not name.startswith(NAME_PREFIX):
            return
        rssi = adv.rssi if adv.rssi is not None else -100
        seen[device.address] = DiscoveredInverter(address=device.address, name=name, rssi=rssi)

    scanner = BleakScanner(detection_callback=callback)
    await scanner.start()
    try:
        await asyncio.sleep(timeout)
    finally:
        await scanner.stop()

    return sorted(seen.values(), key=lambda d: d.rssi, reverse=True)


async def _retry_connect(address: str, connect_timeout: float, attempts: int):
    """Yield a connected BleakClient, retrying on flaky BLE links."""
    from bleak import BleakClient
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            client = BleakClient(address, timeout=connect_timeout)
            await client.connect()
            return client
        except Exception as exc:
            last_exc = exc
            if attempt < attempts:
                await asyncio.sleep(1.0)
                continue
    raise ProvisioningError(
        f"could not connect to {address} after {attempts} attempts: {last_exc}"
    )


async def list_networks(
    address: str,
    *,
    connect_timeout: float = 20.0,
    connect_attempts: int = 4,
) -> tuple[str, list[ScannedNetwork]]:
    """Connect, bootstrap, scan. Does not write any credentials.

    Returns ``(serial, networks)``. Use this as a smoke test before
    :func:`provision` -- if this works, the device is reachable and the
    credentials flow will too.
    """
    client = await _retry_connect(address, connect_timeout, connect_attempts)
    session = _BleSession(client)
    try:
        await session.start()
        tail, serial = await session.bootstrap()
        networks = await session.list_networks(tail)
        return serial, networks
    finally:
        await session.stop()
        try:
            await client.disconnect()
        except Exception:
            pass


async def provision(
    address: str,
    ssid: str,
    passphrase: str,
    *,
    connect_timeout: float = 20.0,
    connect_attempts: int = 4,
) -> str:
    """Connect, bootstrap, write credentials, commit. Returns the inverter serial.

    The inverter will associate to ``ssid`` once it accepts the credentials.
    BLE typically drops within ~1 second of the commit ack.
    """
    client = await _retry_connect(address, connect_timeout, connect_attempts)
    session = _BleSession(client)
    try:
        await session.start()
        tail, serial = await session.bootstrap()
        await session.set_credentials(tail, ssid, passphrase)
        return serial
    finally:
        await session.stop()
        try:
            await client.disconnect()
        except Exception:
            pass
