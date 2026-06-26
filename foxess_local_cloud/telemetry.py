"""Decode FoxESS M1/Q1 238-byte telemetry payloads."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


# Maps the SET of distinct nonzero fault-register tokens (the values seen at
# offsets 100, 102, 104, 106) to the user-friendly 4-digit code(s) that
# FoxCloud reports for the same fault. Each entry was built by correlating the
# live values our daemon captured against the cloud's fault log timestamps for
# the same inverter.
#
# The registers behave like a small FIFO/accumulator: across one fault episode
# the inverter re-logs the same fault every ~90 s, so the same token repeats
# across slots and the tuple "walks" (e.g. (4,0,0,0) -> (4,4,0,0) -> (4,4,4,0)
# -> (4,4,4,4) for AC Under Voltage). Keying on the distinct-token set rather
# than the ordered tuple collapses the whole episode to one rule, and is robust
# to which frame happens to be captured first after a reconnect.
#
# Add new entries here as new fault types are observed.
FAULT_CODE_MAP: dict[frozenset[int], str] = {
    # Confirmed against FoxCloud on 2026-06-06: AC Under Freq + AC Over Freq
    # fired simultaneously during a PV string disconnect.
    frozenset({4, 20, 28, 24}): "4156,4157",
    # Confirmed against FoxCloud on 2026-06-13: AC Under Voltage fired when the
    # PV input cables were unplugged and replugged (15:07:45 CEST, matched to
    # the cloud fault log to the second).
    frozenset({4}): "4158",
}


# Map of FoxESS 4-digit fault code → human-readable name.
# Source: FoxESS Q-Series microinverter user manual V1.0.0, Section 6.1
# Troubleshooting List. The Q and M families share a fault numbering
# scheme; codes apply to both.
FAULT_CODE_NAMES: dict[str, str] = {
    # PV1 faults
    "4029": "PV1 Internal Short-Circuit",
    "4030": "PV1 Low Input Voltage",
    "4031": "PV1 Over Voltage",
    "4032": "PV1 Over Current",
    # PV2 faults
    "4061": "PV2 Internal Short-Circuit",
    "4062": "PV2 Low Input Voltage",
    "4063": "PV2 Over Voltage",
    "4064": "PV2 Over Current",
    # PV3 faults
    "4093": "PV3 Internal Short-Circuit",
    "4094": "PV3 Low Input Voltage",
    "4095": "PV3 Over Voltage",
    "4096": "PV3 Over Current",
    # PV4 faults
    "4125": "PV4 Internal Short-Circuit",
    "4126": "PV4 Low Input Voltage",
    "4127": "PV4 Over Voltage",
    "4128": "PV4 Over Current",
    # AC failures
    "4147": "Inverter bridge is asymmetrical",
    "4148": "Voltage at both ends of relay is not equal",
    "4149": "High or Low Voltage Ride Through",
    "4150": "Remote Switch",
    "4151": "Lost AC",
    "4152": "BUS Over Voltage",
    "4153": "GFDI",
    "4154": "AC Under Temperature",
    "4155": "AC Over Temperature",
    "4156": "AC Under Frequency",
    "4157": "AC Over Frequency",
    "4158": "AC Under Voltage",
    "4159": "AC Over Voltage",
    "4160": "AC Over Current",
}


def fault_code_for(offsets: tuple[int, int, int, int]) -> str:
    """Return the FoxCloud 4-digit code(s) for a fault tuple, or raw hex for unknowns."""
    tokens = frozenset(v for v in offsets if v)
    if not tokens:
        return ""
    if tokens in FAULT_CODE_MAP:
        return FAULT_CODE_MAP[tokens]
    return "raw:" + "-".join(f"{v:02X}" for v in offsets)


def is_known_fault_code(code: str) -> bool:
    """True when a fault code was recognised — not empty and not raw hex."""
    return bool(code) and not code.startswith("raw:")


def fault_code_message_for(code_string: str) -> str:
    """Translate a code string like "4156,4157" into "AC Under Frequency, AC Over Frequency"."""
    if not code_string:
        return ""
    if code_string.startswith("raw:"):
        return "Unknown fault (" + code_string + ")"
    parts = [c.strip() for c in code_string.split(",")]
    names = [FAULT_CODE_NAMES.get(c, f"Unknown {c}") for c in parts if c]
    return ", ".join(names)


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
    export_total_kwh: float
    fault_active: bool
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
    firmware: str = ""
    module: str = ""
    last_fault_code: str = ""
    last_fault_message: str = ""
    last_fault_timestamp: str = ""
    mesh_role: str = ""
    mesh_peer_serial: str = ""
    pv3_power_w: int | None = None
    pv3_voltage_v: float | None = None
    pv3_current_a: float | None = None
    pv4_power_w: int | None = None
    pv4_voltage_v: float | None = None
    pv4_current_a: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None and value != ""}


def decode_telemetry(
    payload: bytes,
    serial: str = "",
    model: str = "",
    firmware: str = "",
    module: str = "",
    last_fault_code: str = "",
    last_fault_timestamp: str = "",
    mesh_role: str = "",
    mesh_peer_serial: str = "",
) -> Telemetry:
    last_fault_message = fault_code_message_for(last_fault_code)
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
    string_count = u16(payload, 156)
    include_pv34 = (
        supports_four_pv(model)
        or string_count >= 4
        or any(
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
    )
    r_power_w = s16(payload, 6)
    export_power_w = -s16(payload, 2)
    operating_state_code = u16(payload, 154)
    fault_active = any(u16(payload, off) for off in (98, 100, 102, 104, 106))
    return Telemetry(
        serial=serial,
        model=model,
        firmware=firmware,
        module=module,
        last_fault_code=last_fault_code,
        last_fault_message=last_fault_message,
        last_fault_timestamp=last_fault_timestamp,
        mesh_role=mesh_role,
        mesh_peer_serial=mesh_peer_serial,
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
        export_total_kwh=round(u32_wordswapped(payload, 74) / 128.0, 3),
        fault_active=fault_active,
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
