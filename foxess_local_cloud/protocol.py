"""FoxESS TCP/14431 frame parsing and local cloud responses."""

from __future__ import annotations

from dataclasses import dataclass


def crc16_le(data: bytes) -> bytes:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc.to_bytes(2, "little")


def ascii_text(data: bytes) -> str:
    return "".join(chr(byte) if 32 <= byte <= 126 else "." for byte in data)


@dataclass(frozen=True)
class Frame:
    start: bytes
    device: bytes
    func: int
    payload: bytes
    crc: bytes
    end: bytes
    raw: bytes
    valid_crc: bool

    @property
    def payload_len(self) -> int:
        return len(self.payload)

    @property
    def family(self) -> str:
        if self.start == b"\x7e\x7e":
            return "7e"
        if self.start == b"\x7f\x7f":
            return "7f"
        return "unknown"


def make_frame(start: bytes, device: bytes, func: int, payload: bytes, end: bytes) -> bytes:
    body = device + bytes([func]) + len(payload).to_bytes(2, "big") + payload
    return start + body + crc16_le(body) + end


def parse_frame(raw: bytes) -> Frame:
    if len(raw) < 13:
        raise ValueError("frame too short")
    start = raw[:2]
    end = raw[-2:]
    payload_len = int.from_bytes(raw[7:9], "big")
    expected_len = 2 + 4 + 1 + 2 + payload_len + 2 + 2
    if len(raw) != expected_len:
        raise ValueError(f"wrong frame length: got {len(raw)}, expected {expected_len}")
    body = raw[2 : 9 + payload_len]
    crc = raw[9 + payload_len : 11 + payload_len]
    return Frame(
        start=start,
        device=raw[2:6],
        func=raw[6],
        payload=raw[9 : 9 + payload_len],
        crc=crc,
        end=end,
        raw=raw,
        valid_crc=crc == crc16_le(body),
    )


def extract_frames(buffer: bytearray) -> list[Frame]:
    frames: list[Frame] = []
    while True:
        starts = [idx for marker in (b"\x7e\x7e", b"\x7f\x7f") if (idx := buffer.find(marker)) >= 0]
        if not starts:
            buffer.clear()
            return frames
        start_idx = min(starts)
        if start_idx:
            del buffer[:start_idx]
        if len(buffer) < 13:
            return frames
        start = bytes(buffer[:2])
        end = b"\xe7\xe7" if start == b"\x7e\x7e" else b"\xf7\xf7"
        payload_len = int.from_bytes(buffer[7:9], "big")
        total_len = 2 + 4 + 1 + 2 + payload_len + 2 + 2
        if total_len < 13 or total_len > 4096:
            del buffer[:2]
            continue
        if len(buffer) < total_len:
            return frames
        raw = bytes(buffer[:total_len])
        del buffer[:total_len]
        if raw[-2:] != end:
            continue
        try:
            frames.append(parse_frame(raw))
        except ValueError:
            continue


def bootstrap_response_device(request_device: bytes, first_byte: int) -> bytes:
    tail = (int.from_bytes(request_device[1:4], "big") - 0x71) & 0xFFFFFF
    return bytes([first_byte]) + tail.to_bytes(3, "big")


def bootstrap_response(request: Frame, first_byte: int, func_offset: int, payload: bytes) -> bytes:
    response_func = (request.func + func_offset) & 0xFF
    return make_frame(
        b"\x7e\x7e",
        bootstrap_response_device(request.device, first_byte),
        response_func,
        payload,
        b"\xe7\xe7",
    )


def is_registration(frame: Frame) -> bool:
    return (
        frame.start == b"\x7e\x7e"
        and frame.payload_len == 28
        and len(frame.payload) >= 20
        and frame.payload[:4] == b"\x01\x00\x01\x31"
    )


def registration_serial(frame: Frame) -> str | None:
    if not is_registration(frame):
        return None
    try:
        length = frame.payload[4]
        return frame.payload[5 : 5 + length].decode("ascii")
    except (IndexError, UnicodeDecodeError):
        return None


def is_telemetry(frame: Frame) -> bool:
    return frame.start == b"\x7e\x7e" and frame.device == b"\x02\x00\x00\x00" and frame.func == 0 and frame.payload_len == 238


def is_product_info(frame: Frame) -> bool:
    return frame.start == b"\x7e\x7e" and frame.device == b"\x01\x00\x00\x00" and frame.func == 0 and frame.payload_len >= 32


def is_module_info(frame: Frame) -> bool:
    return frame.start == b"\x7e\x7e" and frame.device == b"\x06\x00\x00\x00" and frame.func == 0 and frame.payload_len == 38


def module_info(frame: Frame) -> str:
    """Extract the ASCII module identifier from the 38-byte heartbeat frame (e.g. "M10200")."""
    if not is_module_info(frame):
        return ""
    text = frame.payload.split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
    return text


def product_info(frame: Frame) -> dict[str, str]:
    if not is_product_info(frame):
        return {}
    parts = [part.decode("ascii", errors="ignore").strip() for part in frame.payload.split(b"\x00")]
    strings = [part for part in parts if part and any(char.isalnum() for char in part)]
    model = next((part for part in strings if "-" in part), "")
    family = next((part for part in strings if part.startswith(("M", "Q")) and "-" not in part and not part.endswith("V180")), "")
    firmware = next((part for part in strings if part[:1].isdigit() and "." in part), "")
    module = strings[0] if strings else ""
    return {"module": module, "family": family, "model": model, "firmware": firmware}


MODBUS_FN_READ_HOLDING = 0x03
MODBUS_FN_READ_INPUT = 0x04
MODBUS_FN_WRITE_SINGLE = 0x06
MODBUS_FN_WRITE_MULTIPLE = 0x10
MODBUS_FUNCTIONS = {MODBUS_FN_READ_HOLDING, MODBUS_FN_READ_INPUT, MODBUS_FN_WRITE_SINGLE, MODBUS_FN_WRITE_MULTIPLE}

# Envelope function byte the FoxESS cloud uses for all observed Modbus
# command frames (requests + responses, both directions).
COMMAND_ENVELOPE_FUNC = 0xE2

# First byte of the 4-byte envelope "device" field that the daemon uses for
# its OWN injected requests. The inverter echoes the device bytes back in
# the response with bit 7 set (0x7f | 0x80 = 0xff), so we can recognise
# both halves of an injected round-trip by ``(device[0] & 0x7f) == 0x7f``.
# The cloud has been observed using 0x11 / 0x12 here, so 0x7f is clearly
# outside its allocation.
INJECTED_DEVICE_MARKER = 0x7F


def is_injected_device(device: bytes) -> bool:
    """True iff this 4-byte envelope device field belongs to a request the
    daemon injected (or its echoed response)."""
    return len(device) >= 1 and (device[0] & 0x7F) == INJECTED_DEVICE_MARKER


def build_modbus_write_single(device: bytes, address: int, value: int) -> bytes:
    """Build a 7f7f-envelope frame carrying a Modbus write-single-register PDU."""
    pdu = bytes([0x01, MODBUS_FN_WRITE_SINGLE]) + address.to_bytes(2, "big") + value.to_bytes(2, "big")
    return make_frame(b"\x7f\x7f", device, COMMAND_ENVELOPE_FUNC, pdu, b"\xf7\xf7")


def build_modbus_read_holding(device: bytes, address: int, count: int) -> bytes:
    """Build a 7f7f-envelope frame carrying a Modbus read-holding-registers PDU."""
    pdu = bytes([0x01, MODBUS_FN_READ_HOLDING]) + address.to_bytes(2, "big") + count.to_bytes(2, "big")
    return make_frame(b"\x7f\x7f", device, COMMAND_ENVELOPE_FUNC, pdu, b"\xf7\xf7")


def build_modbus_read_input(device: bytes, address: int, count: int) -> bytes:
    """Build a 7f7f-envelope frame carrying a Modbus read-input-registers PDU."""
    pdu = bytes([0x01, MODBUS_FN_READ_INPUT]) + address.to_bytes(2, "big") + count.to_bytes(2, "big")
    return make_frame(b"\x7f\x7f", device, COMMAND_ENVELOPE_FUNC, pdu, b"\xf7\xf7")


def is_modbus_command(frame: Frame) -> bool:
    """A 7f7f frame whose payload is a Modbus PDU (slave + function + body).

    Disambiguated from mesh-role declaration frames (which use the same
    framing but a non-Modbus payload prefix) by checking the function byte
    against the known Modbus subset we observe on the wire.
    """
    if frame.start != b"\x7f\x7f" or len(frame.payload) < 6:
        return False
    return frame.payload[1] in MODBUS_FUNCTIONS


def parse_modbus_command(frame: Frame) -> dict:
    """Decode the Modbus PDU inside a 7f7f command frame, or {} if not one."""
    if not is_modbus_command(frame):
        return {}
    payload = frame.payload
    slave = payload[0]
    function = payload[1]
    address = int.from_bytes(payload[2:4], "big")
    word = int.from_bytes(payload[4:6], "big")
    if function in (MODBUS_FN_READ_HOLDING, MODBUS_FN_READ_INPUT):
        return {"slave": slave, "function": function, "address": address, "count": word}
    if function == MODBUS_FN_WRITE_SINGLE:
        return {"slave": slave, "function": function, "address": address, "value": word}
    if function == MODBUS_FN_WRITE_MULTIPLE and len(payload) >= 7:
        count = word
        byte_count = payload[6]
        values: list[int] = []
        for i in range(count):
            base = 7 + i * 2
            if base + 2 > len(payload):
                break
            values.append(int.from_bytes(payload[base : base + 2], "big"))
        return {
            "slave": slave,
            "function": function,
            "address": address,
            "count": count,
            "byte_count": byte_count,
            "values": values,
        }
    return {}


def is_modbus_read_response(frame: Frame) -> bool:
    """A 7f7f frame carrying a Modbus read response (fn 0x03/0x04 with data).

    Distinguished from same-function requests by payload structure: a request
    is always exactly 6 bytes (slave+fn+addr+count) while a read response is
    ``3 + byte_count`` bytes (slave+fn+bc+data) with ``bc`` an even number
    matching the data length. Requests never satisfy that shape, so the check
    is unambiguous.
    """
    if frame.start != b"\x7f\x7f" or len(frame.payload) < 5:
        return False
    fn = frame.payload[1]
    if fn not in (MODBUS_FN_READ_HOLDING, MODBUS_FN_READ_INPUT):
        return False
    bc = frame.payload[2]
    return bc > 0 and bc % 2 == 0 and len(frame.payload) == 3 + bc


def parse_modbus_read_response(frame: Frame) -> dict:
    """Decode a Modbus read response PDU into slave/function/byte_count/values."""
    if not is_modbus_read_response(frame):
        return {}
    payload = frame.payload
    bc = payload[2]
    values = [int.from_bytes(payload[3 + i * 2 : 5 + i * 2], "big") for i in range(bc // 2)]
    return {"slave": payload[0], "function": payload[1], "byte_count": bc, "values": values}


def is_mesh_root_frame(frame: Frame) -> bool:
    """A 7f7f frame in which the inverter declares it is the mesh root, i.e.
    directly associated to the Pi's AP.

    Payload layout: ``01 05 01 01 00 06 <ap_mac:6> 03 <beacon_mfg:5> 04 00 00 00 ee``.
    """
    return (
        frame.start == b"\x7f\x7f"
        and len(frame.payload) >= 12
        and frame.payload[:4] == b"\x01\x05\x01\x01"
    )


def is_mesh_follower_frame(frame: Frame) -> bool:
    """A 7f7f frame in which the inverter declares it is a mesh follower,
    tunnelling its TCP session through another inverter (the root).

    Payload layout: ``01 05 01 02 <serial_len:1> <root_serial:serial_len> ...``.
    """
    if frame.start != b"\x7f\x7f" or len(frame.payload) < 5:
        return False
    if frame.payload[:4] != b"\x01\x05\x01\x02":
        return False
    serial_len = frame.payload[4]
    return 1 <= serial_len <= 32 and len(frame.payload) >= 5 + serial_len


def mesh_peer_serial(frame: Frame) -> str:
    """Return the root inverter's serial declared in a mesh-follower frame, or '' if not one."""
    if not is_mesh_follower_frame(frame):
        return ""
    serial_len = frame.payload[4]
    try:
        return frame.payload[5 : 5 + serial_len].decode("ascii")
    except UnicodeDecodeError:
        return ""


class BootstrapResponder:
    """Minimal local FoxESS cloud bootstrap ACK state machine."""

    def __init__(self) -> None:
        self.step = 0

    def response_for(self, frame: Frame) -> bytes | None:
        if self.step == 0 and is_registration(frame):
            self.step = 1
            return bootstrap_response(frame, frame.device[0] | 0x80, 0x82, b"\x01\x01\x01\x00\x00")
        if self.step == 1 and frame.start == b"\x7e\x7e" and len(frame.raw) == 17:
            self.step = 2
            return bootstrap_response(frame, frame.device[0] | 0x80, 0x80, b"")
        if self.step == 2 and frame.start == b"\x7e\x7e" and len(frame.raw) == 14:
            self.step = 3
            return bootstrap_response(frame, frame.device[0] | 0x80, 0x81, b"\x01")
        return None
