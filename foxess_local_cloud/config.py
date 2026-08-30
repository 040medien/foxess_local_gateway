"""Configuration loading for the local FoxESS cloud emulator."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MqttConfig:
    host: str = ""
    port: int = 1883
    username: str | None = None
    password: str | None = None
    discovery_prefix: str = "homeassistant"
    topic_prefix: str = "foxess_m1"
    retain: bool = True
    expire_after_seconds: int | None = 300
    # Mark an inverter's MQTT availability offline when no telemetry has been
    # received for this long. 0 disables the explicit stale-data watchdog.
    telemetry_stale_after_seconds: int = 300
    keepalive_seconds: int = 180
    reconnect_max_delay_seconds: int = 15
    # How often the daemon checks that paho's network loop is still alive and
    # rebuilds the client if it died. 0 disables the watchdog.
    health_check_interval_seconds: int = 30
    debug: bool = False


@dataclass(frozen=True)
class InverterControl:
    """Opt-in local Modbus write channel to the inverter.

    When enabled, the daemon may write to the inverter's ActivePowerLimit
    Modbus holding register on behalf of Home Assistant — without the
    FoxESS cloud round-trip. Responses to our injected writes are decoded
    locally and stripped from the upstream-bound bytes so they never reach
    FoxCloud.

    Scope is intentionally narrow. The cloud's installer portal exposes
    several other Modbus-writable groups (grid voltage / frequency
    protection, reactive power modes, start parameters, safety country
    code, etc.); we do NOT expose those because they are grid-code
    regulatory settings that should only be changed by a certified
    installer in coordination with the DSO. See README for the full
    rationale.
    """

    enabled: bool = True
    # Holding-register address of the ActivePowerLimit setting (slave 1,
    # value in whole percent, mapped 2026-06-09).
    active_power_limit_address: int = 0xCA5A
    # How long to wait for the inverter's Modbus write-response before
    # reporting the write as unconfirmed (TIMEOUT). Measured round-trip on a
    # healthy link is ~0.3 s; 3 s leaves generous headroom. 0 disables the wait
    # (fire-and-forget).
    write_timeout_seconds: float = 3.0


@dataclass(frozen=True)
class FirmwareCaptureConfig:
    """Opt-in interception of FoxCloud firmware pushes for research."""

    enabled: bool = False
    directory: Path = Path("runtime/firmware-captures")
    simulate_progress: bool = True
    progress_interval_seconds: float = 0.25


@dataclass(frozen=True)
class AppConfig:
    host: str = "0.0.0.0"
    port: int = 14431
    cert: Path = Path("runtime/foxess-local-cloud-cert.pem")
    key: Path = Path("runtime/foxess-local-cloud-key.pem")
    force_cert: bool = False
    jsonl: Path | None = None
    devices: dict[str, str] = field(default_factory=dict)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    publish_min_interval_seconds: float = 0.0
    # TCP keepalive (seconds idle before probing) on the inverter connection, to
    # keep a marginal Wi-Fi link warm and detect a dropped session quickly so the
    # ActivePowerLimit retry-on-reconnect kicks in. 0 disables. Linux-only knobs;
    # silently skipped where unsupported.
    inverter_tcp_keepalive_seconds: int = 30
    relay: RelayConfig = field(default_factory=lambda: RelayConfig())
    inverter_control: InverterControl = field(default_factory=InverterControl)
    firmware_capture: FirmwareCaptureConfig = field(default_factory=FirmwareCaptureConfig)
    # Root/local maintenance interface used only by the separate firmware CLI.
    # None disables it (the default for library/test configurations).
    firmware_control_socket: Path | None = None


@dataclass(frozen=True)
class RelayConfig:
    enabled: bool = False
    upstreams: dict[str, tuple[str, int]] = field(default_factory=dict)
    fallback_to_original_destination: bool = True
    connect_timeout_seconds: float = 10.0
    skip_cert_verify: bool = False


def _load_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml  # type: ignore[import-not-found]

        loaded = yaml.safe_load(text)
        return loaded or {}
    except ModuleNotFoundError as exc:
        raise RuntimeError("YAML config requires PyYAML; use JSON config or install pyyaml") from exc


def load_config(path: Path | None) -> AppConfig:
    if path is None:
        return AppConfig()
    raw = _load_raw(path)
    mqtt_raw = raw.get("mqtt", {}) or {}
    relay_raw = raw.get("relay", {}) or {}
    control_raw = raw.get("inverter_control", {}) or {}
    capture_raw = raw.get("firmware_capture", {}) or {}
    upstreams: dict[str, tuple[str, int]] = {}
    for original, target in (relay_raw.get("upstreams", {}) or {}).items():
        host, port = str(target).rsplit(":", 1)
        upstreams[str(original)] = (host, int(port))
    return AppConfig(
        host=raw.get("host", "0.0.0.0"),
        port=int(raw.get("port", 14431)),
        cert=Path(raw.get("cert", "runtime/foxess-local-cloud-cert.pem")),
        key=Path(raw.get("key", "runtime/foxess-local-cloud-key.pem")),
        force_cert=bool(raw.get("force_cert", False)),
        jsonl=Path(raw["jsonl"]) if raw.get("jsonl") else None,
        devices={str(k): str(v) for k, v in (raw.get("devices", {}) or {}).items()},
        mqtt=MqttConfig(
            host=str(mqtt_raw.get("host", "")),
            port=int(mqtt_raw.get("port", 1883)),
            username=mqtt_raw.get("username"),
            password=mqtt_raw.get("password"),
            discovery_prefix=str(mqtt_raw.get("discovery_prefix", "homeassistant")),
            topic_prefix=str(mqtt_raw.get("topic_prefix", "foxess_m1")),
            retain=bool(mqtt_raw.get("retain", True)),
            expire_after_seconds=optional_int(mqtt_raw.get("expire_after_seconds", 300)),
            telemetry_stale_after_seconds=int(mqtt_raw.get("telemetry_stale_after_seconds", 300)),
            keepalive_seconds=int(mqtt_raw.get("keepalive_seconds", 180)),
            reconnect_max_delay_seconds=int(mqtt_raw.get("reconnect_max_delay_seconds", 15)),
            health_check_interval_seconds=int(mqtt_raw.get("health_check_interval_seconds", 30)),
            debug=bool(mqtt_raw.get("debug", False)),
        ),
        publish_min_interval_seconds=float(raw.get("publish_min_interval_seconds", 0.0)),
        inverter_tcp_keepalive_seconds=int(raw.get("inverter_tcp_keepalive_seconds", 30)),
        relay=RelayConfig(
            enabled=bool(relay_raw.get("enabled", False)),
            upstreams=upstreams,
            fallback_to_original_destination=bool(relay_raw.get("fallback_to_original_destination", True)),
            connect_timeout_seconds=float(relay_raw.get("connect_timeout_seconds", 10.0)),
            skip_cert_verify=bool(relay_raw.get("skip_cert_verify", False)),
        ),
        inverter_control=InverterControl(
            enabled=bool(control_raw.get("enabled", True)),
            active_power_limit_address=int(control_raw.get("active_power_limit_address", 0xCA5A)),
            write_timeout_seconds=float(control_raw.get("write_timeout_seconds", 3.0)),
        ),
        firmware_capture=FirmwareCaptureConfig(
            enabled=bool(capture_raw.get("enabled", False)),
            directory=Path(capture_raw.get("directory", "runtime/firmware-captures")),
            simulate_progress=bool(capture_raw.get("simulate_progress", True)),
            progress_interval_seconds=float(capture_raw.get("progress_interval_seconds", 0.25)),
        ),
        firmware_control_socket=(
            Path(raw["firmware_control_socket"])
            if raw.get("firmware_control_socket")
            else None
        ),
    )


def optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
