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
