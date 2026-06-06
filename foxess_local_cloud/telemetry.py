"""Decode FoxESS M1 238-byte telemetry payloads."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


def u16(payload: bytes, offset: int) -> int:
    return int.from_bytes(payload[offset : offset + 2], "big")


def s16(payload: bytes, offset: int) -> int:
    return int.from_bytes(payload[offset : offset + 2], "big", signed=True)


def u32_wordswapped(payload: bytes, offset: int) -> int:
    low = u16(payload, offset)
    high = u16(payload, offset + 2)
    return (high << 16) | low


def nonzero_u16_words(payload: bytes) -> dict[str, int]:
    return {f"{offset:03d}": u16(payload, offset) for offset in range(0, len(payload) - 1, 2) if u16(payload, offset)}


@dataclass(frozen=True)
class Telemetry:
    serial: str
    r_power_w: int
    export_power_w: int
    r_voltage_v: float
    r_current_a: float
    r_frequency_hz: float
    pv_power_w: int
    pv1_power_w: int
    pv1_voltage_v: float
    pv1_current_a: float
    pv2_power_w: int
    pv2_voltage_v: float
    pv2_current_a: float
    inverter_temperature_c: float
    generation_kwh: float
    operating_state: str
    operating_state_code: int
    sequence: int
    raw_u16_000: int
    raw_u16_002: int
    raw_u16_096: int
    raw_u16_098: int
    raw_u16_100: int
    raw_u16_102: int
    raw_u16_104: int
    raw_u16_106: int
    raw_u16_154: int
    raw_u16_156: int
    model: str = ""
    pv3_power_w: int | None = None
    pv3_voltage_v: float | None = None
    pv3_current_a: float | None = None
    pv4_power_w: int | None = None
    pv4_voltage_v: float | None = None
    pv4_current_a: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None and value != ""}


def decode_telemetry(payload: bytes, serial: str = "", model: str = "") -> Telemetry:
    if len(payload) != 238:
        raise ValueError(f"expected 238-byte payload, got {len(payload)}")
    pv1_power_w = u16(payload, 40)
    pv2_power_w = u16(payload, 46)
    pv3_voltage_v = round(u16(payload, 48) / 256.0, 3)
    pv3_current_a = round(u16(payload, 50) / 512.0, 3)
    pv3_power_w = u16(payload, 52)
    pv4_voltage_v = round(u16(payload, 54) / 256.0, 3)
    pv4_current_a = round(u16(payload, 56) / 512.0, 3)
    pv4_power_w = u16(payload, 58)
    include_pv34 = supports_four_pv(model) or any(
        value
        for value in (
            pv3_power_w,
            pv3_voltage_v,
            pv3_current_a,
            pv4_power_w,
            pv4_voltage_v,
            pv4_current_a,
        )
    )
    r_power_w = s16(payload, 6)
    export_power_w = -s16(payload, 2)
    operating_state_code = u16(payload, 154)
    return Telemetry(
        serial=serial,
        model=model,
        r_power_w=r_power_w,
        export_power_w=export_power_w,
        r_voltage_v=round(u16(payload, 12) / 32.0, 3),
        r_current_a=round(u16(payload, 14) / 512.0, 3),
        r_frequency_hz=round(u16(payload, 16) / 128.0, 3),
        pv_power_w=pv1_power_w + pv2_power_w + (pv3_power_w if include_pv34 else 0) + (pv4_power_w if include_pv34 else 0),
        pv1_power_w=pv1_power_w,
        pv1_voltage_v=round(u16(payload, 36) / 256.0, 3),
        pv1_current_a=round(u16(payload, 38) / 512.0, 3),
        pv2_power_w=pv2_power_w,
        pv2_voltage_v=round(u16(payload, 42) / 256.0, 3),
        pv2_current_a=round(u16(payload, 44) / 512.0, 3),
        pv3_power_w=pv3_power_w if include_pv34 else None,
        pv3_voltage_v=pv3_voltage_v if include_pv34 else None,
        pv3_current_a=pv3_current_a if include_pv34 else None,
        pv4_power_w=pv4_power_w if include_pv34 else None,
        pv4_voltage_v=pv4_voltage_v if include_pv34 else None,
        pv4_current_a=pv4_current_a if include_pv34 else None,
        inverter_temperature_c=round(u16(payload, 62) / 32.0, 2),
        generation_kwh=round(u32_wordswapped(payload, 70) / 128.0, 3),
        operating_state=operating_state(operating_state_code),
        operating_state_code=operating_state_code,
        sequence=u16(payload, 66),
        raw_u16_000=u16(payload, 0),
        raw_u16_002=u16(payload, 2),
        raw_u16_096=u16(payload, 96),
        raw_u16_098=u16(payload, 98),
        raw_u16_100=u16(payload, 100),
        raw_u16_102=u16(payload, 102),
        raw_u16_104=u16(payload, 104),
        raw_u16_106=u16(payload, 106),
        raw_u16_154=u16(payload, 154),
        raw_u16_156=u16(payload, 156),
    )


def supports_four_pv(model: str) -> bool:
    normalized = model.upper().replace("_", "-")
    return normalized.startswith("Q1") or "-Q1" in normalized or "Q1-" in normalized


def operating_state(code: int) -> str:
    return {
        2: "standby",
        4: "running",
    }.get(code, "unknown")
