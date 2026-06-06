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
    retain: bool = False
    expire_after_seconds: int | None = 180
    debug: bool = False


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
    relay: RelayConfig = field(default_factory=lambda: RelayConfig())


@dataclass(frozen=True)
class RelayConfig:
    enabled: bool = False
    upstreams: dict[str, tuple[str, int]] = field(default_factory=dict)
    fallback_to_original_destination: bool = True
    connect_timeout_seconds: float = 10.0


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
            retain=bool(mqtt_raw.get("retain", False)),
            expire_after_seconds=optional_int(mqtt_raw.get("expire_after_seconds", 180)),
            debug=bool(mqtt_raw.get("debug", False)),
        ),
        publish_min_interval_seconds=float(raw.get("publish_min_interval_seconds", 0.0)),
        relay=RelayConfig(
            enabled=bool(relay_raw.get("enabled", False)),
            upstreams=upstreams,
            fallback_to_original_destination=bool(relay_raw.get("fallback_to_original_destination", True)),
            connect_timeout_seconds=float(relay_raw.get("connect_timeout_seconds", 10.0)),
        ),
    )


def optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
