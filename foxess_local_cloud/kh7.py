"""Experimental KH7 cloud upload decoding for relay-mode validation."""

from dataclasses import dataclass
import re
from typing import ClassVar

from .protocol import Frame


# Zero-based offsets in the 425-byte payload. Scales and channel assignments
# are hypotheses from issue #63 and should be compared against FoxCloud.
FIELDS = {
    "pv1_voltage_v": (33, 2, 10),
    "pv1_current_a": (35, 2, 10),
    "pv1_power_w": (37, 2, 1),
    "pv2_voltage_v": (39, 2, 10),
    "pv2_current_a": (41, 2, 10),
    "pv2_power_w": (43, 2, 1),
    "pv_power_w": (57, 2, 1),
    "ac_51_voltage_v": (51, 2, 10),
    "ac_61_voltage_v": (61, 2, 10),
    "ac_59_frequency_hz": (59, 2, 100),
    "ac_69_frequency_hz": (69, 2, 100),
    "ac_71_power_w": (71, 4, 1),
    "ac_75_power_w": (75, 4, 1),
}


def kh7_product_serial(frame: Frame) -> str | None:
    if (
        frame.start == b"\x7f\x7f"
        and frame.device[0] == 1
        and frame.payload_len == 141
        and frame.payload[93:109].rstrip(b" \x00") == b"KH7"
        and re.fullmatch(rb"[A-Z0-9]{15}", frame.payload[:15])
    ):
        return frame.payload[:15].decode("ascii")
    return None


@dataclass(frozen=True)
class Kh7Telemetry:
    serial: str
    payload: bytes
    model: ClassVar[str] = "KH7"
    firmware: ClassVar[str] = ""
    module: ClassVar[str] = ""

    def as_dict(self) -> dict[str, str | int | float]:
        fields: dict[str, str | int | float] = {"serial": self.serial, "model": self.model}
        for name, (offset, size, divisor) in FIELDS.items():
            raw = int.from_bytes(self.payload[offset : offset + size], "big", signed=size == 4)
            fields[name] = raw if divisor == 1 else raw / divisor
        return fields


def kh7_telemetry(frame: Frame, serial: str) -> Kh7Telemetry | None:
    if (
        frame.start == b"\x7f\x7f"
        and frame.device[0] == 2
        and frame.payload_len == 425
        and frame.payload[:15] == serial.encode("ascii")
    ):
        return Kh7Telemetry(serial, frame.payload)
    return None
