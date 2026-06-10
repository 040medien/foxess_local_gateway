"""Optional MQTT publishing for decoded FoxESS telemetry."""

from __future__ import annotations

import json
from typing import Any, Callable

from .config import MqttConfig
from .telemetry import Telemetry


DEBUG_SCALAR_TOPICS = ("0/sequence", "0/status_code")
OPERATING_STATE_OPTIONS = ("standby", "running", "unknown")
LEGACY_DISCOVERY_FIELDS = ("feedin_power_w",)


class MqttPublisher:
    def __init__(
        self,
        config: MqttConfig,
        device_names: dict[str, str],
        emit: Callable[..., None] | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self.device_names = device_names
        self.emit = emit or (lambda _event, **_fields: None)
        self.client_factory = client_factory
        self.client: Any = None
        self.announced: set[str] = set()
        self.model_by_serial: dict[str, str] = {}
        self.device_signature_by_serial: dict[str, tuple[str, str, str]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.config.host)

    def connect(self) -> None:
        if not self.enabled:
            return

        self.client = self.client_factory() if self.client_factory else self._default_client()
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        if hasattr(self.client, "reconnect_delay_set"):
            self.client.reconnect_delay_set(min_delay=1, max_delay=self.config.reconnect_max_delay_seconds)
        if self.config.username:
            self.client.username_pw_set(self.config.username, self.config.password)
        if hasattr(self.client, "will_set"):
            self.client.will_set(f"{self.config.topic_prefix}/status", "offline", retain=True)
        try:
            if hasattr(self.client, "connect_async"):
                self.client.connect_async(self.config.host, self.config.port, keepalive=self.config.keepalive_seconds)
            else:
                self.client.connect(self.config.host, self.config.port, keepalive=self.config.keepalive_seconds)
            self.client.loop_start()
            self.emit("mqtt_connecting", host=self.config.host, port=self.config.port)
        except Exception as exc:
            self.emit("mqtt_error", host=self.config.host, port=self.config.port, error=str(exc))

    def _default_client(self) -> Any:
        import paho.mqtt.client as mqtt

        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def _on_connect(self, _client: Any, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None) -> None:
        event = "mqtt_connected" if reason_is_success(reason_code) else "mqtt_connect_failed"
        self.emit(event, host=self.config.host, port=self.config.port, reason=str(reason_code))
        if event == "mqtt_connected" and self.client is not None:
            self._publish(f"{self.config.topic_prefix}/status", "online", retain=True)

    def _on_disconnect(self, _client: Any, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None) -> None:
        self.announced.clear()
        self.emit("mqtt_disconnected", host=self.config.host, port=self.config.port, reason=str(reason_code))

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception as exc:
                self.emit("mqtt_close_error", error=str(exc))

    def publish(self, telemetry: Telemetry) -> None:
        if not self.enabled or self.client is None or not telemetry.serial:
            return
        known_model = self.model_by_serial.get(telemetry.serial)
        signature = (telemetry.model or "", telemetry.firmware or "", telemetry.module or "")
        known_signature = self.device_signature_by_serial.get(telemetry.serial)
        should_announce = telemetry.serial not in self.announced
        if telemetry.model and telemetry.model != known_model:
            should_announce = True
        if known_signature is not None and signature != known_signature:
            should_announce = True
        if telemetry.model:
            self.model_by_serial[telemetry.serial] = telemetry.model
        self.device_signature_by_serial[telemetry.serial] = signature
        if should_announce:
            self._publish_discovery(telemetry)
            self.announced.add(telemetry.serial)
        state_topic = f"{self.config.topic_prefix}/{telemetry.serial}/state"
        self._publish(state_topic, json.dumps(self._state_dict(telemetry), separators=(",", ":")), retain=self.config.retain)
        self._publish(f"{self.config.topic_prefix}/{telemetry.serial}/availability", "online", retain=True)
        self._publish_scalar_topics(telemetry)

    def _publish_discovery(self, telemetry: Telemetry) -> None:
        assert self.client is not None
        serial = telemetry.serial
        name = self.device_names.get(serial, serial)
        model = telemetry.model or self.model_by_serial.get(serial) or "FoxESS inverter"
        device: dict[str, Any] = {"identifiers": [f"foxess_{serial}"], "name": name, "manufacturer": "FoxESS", "model": model}
        if telemetry.firmware:
            device["sw_version"] = telemetry.firmware
        if telemetry.module:
            device["hw_version"] = telemetry.module
        state_topic = f"{self.config.topic_prefix}/{serial}/state"
        availability = self._availability_block(serial)
        state = self._state_dict(telemetry)
        for field_name in state:
            if field_name in {"serial", "model", "firmware", "module", "fault_active", "last_fault_code", "last_fault_message", "last_fault_timestamp", "mesh_role", "mesh_peer_serial"}:
                continue
            config_topic = f"{self.config.discovery_prefix}/sensor/foxess_{serial}/{field_name}/config"
            payload: dict[str, Any] = {
                "name": friendly_field_name(field_name),
                "unique_id": f"foxess_{serial}_{field_name}",
                "object_id": f"foxess_{serial}_{field_name}",
                "state_topic": state_topic,
                "value_template": "{{ value_json." + field_name + " }}",
                "availability": availability,
                "availability_mode": "all",
                "device": device,
            }
            if self.config.expire_after_seconds is not None:
                payload["expire_after"] = self.config.expire_after_seconds
            unit, device_class, state_class = metadata_for(field_name)
            if unit:
                payload["unit_of_measurement"] = unit
            if device_class:
                payload["device_class"] = device_class
            if field_name == "operating_state":
                payload["options"] = list(OPERATING_STATE_OPTIONS)
            if state_class:
                payload["state_class"] = state_class
            self._publish(config_topic, json.dumps(payload, separators=(",", ":")), retain=True)
        self._publish_running_discovery(telemetry, device, state_topic)
        self._publish_fault_discovery(telemetry, device, state_topic)
        self._publish_last_fault_discovery(telemetry, device, state_topic)
        self._publish_mesh_discovery(telemetry, device, state_topic)
        self._clear_legacy_discovery(telemetry)
        if not self.config.debug:
            self._clear_debug_discovery(telemetry)

    def _publish_running_discovery(self, telemetry: Telemetry, device: dict[str, Any], state_topic: str) -> None:
        serial = telemetry.serial
        config_topic = f"{self.config.discovery_prefix}/binary_sensor/foxess_{serial}/running/config"
        payload: dict[str, Any] = {
            "name": "Running",
            "unique_id": f"foxess_{serial}_running",
            "object_id": f"foxess_{serial}_running",
            "state_topic": state_topic,
            "value_template": "{{ 'ON' if value_json.operating_state == 'running' else 'OFF' }}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability": self._availability_block(serial),
            "availability_mode": "all",
            "device_class": "running",
            "device": device,
        }
        if self.config.expire_after_seconds is not None:
            payload["expire_after"] = self.config.expire_after_seconds
        self._publish(config_topic, json.dumps(payload, separators=(",", ":")), retain=True)

    def _publish_fault_discovery(self, telemetry: Telemetry, device: dict[str, Any], state_topic: str) -> None:
        serial = telemetry.serial
        config_topic = f"{self.config.discovery_prefix}/binary_sensor/foxess_{serial}/fault/config"
        payload: dict[str, Any] = {
            "name": "Fault",
            "unique_id": f"foxess_{serial}_fault",
            "object_id": f"foxess_{serial}_fault",
            "state_topic": state_topic,
            "value_template": "{{ 'ON' if value_json.fault_active else 'OFF' }}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability": self._availability_block(serial),
            "availability_mode": "all",
            "device_class": "problem",
            "entity_category": "diagnostic",
            "device": device,
        }
        if self.config.expire_after_seconds is not None:
            payload["expire_after"] = self.config.expire_after_seconds
        self._publish(config_topic, json.dumps(payload, separators=(",", ":")), retain=True)

    def _publish_last_fault_discovery(self, telemetry: Telemetry, device: dict[str, Any], state_topic: str) -> None:
        serial = telemetry.serial
        availability = self._availability_block(serial)
        common = {
            "state_topic": state_topic,
            "availability": availability,
            "availability_mode": "all",
            "entity_category": "diagnostic",
            "device": device,
        }
        code_config_topic = f"{self.config.discovery_prefix}/sensor/foxess_{serial}/last_fault_code/config"
        code_payload: dict[str, Any] = {
            "name": "Last Fault Code",
            "unique_id": f"foxess_{serial}_last_fault_code",
            "object_id": f"foxess_{serial}_last_fault_code",
            "value_template": "{{ value_json.last_fault_code }}",
            **common,
        }
        self._publish(code_config_topic, json.dumps(code_payload, separators=(",", ":")), retain=True)

        msg_config_topic = f"{self.config.discovery_prefix}/sensor/foxess_{serial}/last_fault_message/config"
        msg_payload: dict[str, Any] = {
            "name": "Last Fault Message",
            "unique_id": f"foxess_{serial}_last_fault_message",
            "object_id": f"foxess_{serial}_last_fault_message",
            "value_template": "{{ value_json.last_fault_message }}",
            **common,
        }
        self._publish(msg_config_topic, json.dumps(msg_payload, separators=(",", ":")), retain=True)

        ts_config_topic = f"{self.config.discovery_prefix}/sensor/foxess_{serial}/last_fault_timestamp/config"
        ts_payload: dict[str, Any] = {
            "name": "Last Fault Time",
            "unique_id": f"foxess_{serial}_last_fault_timestamp",
            "object_id": f"foxess_{serial}_last_fault_timestamp",
            "value_template": "{{ value_json.last_fault_timestamp }}",
            "device_class": "timestamp",
            **common,
        }
        self._publish(ts_config_topic, json.dumps(ts_payload, separators=(",", ":")), retain=True)

    def _publish_mesh_discovery(self, telemetry: Telemetry, device: dict[str, Any], state_topic: str) -> None:
        serial = telemetry.serial
        availability = self._availability_block(serial)
        common = {
            "state_topic": state_topic,
            "availability": availability,
            "availability_mode": "all",
            "entity_category": "diagnostic",
            "device": device,
        }
        role_topic = f"{self.config.discovery_prefix}/sensor/foxess_{serial}/mesh_role/config"
        role_payload: dict[str, Any] = {
            "name": "Mesh Role",
            "unique_id": f"foxess_{serial}_mesh_role",
            "object_id": f"foxess_{serial}_mesh_role",
            "value_template": "{{ value_json.mesh_role | default('') }}",
            **common,
        }
        self._publish(role_topic, json.dumps(role_payload, separators=(",", ":")), retain=True)

        peer_topic = f"{self.config.discovery_prefix}/sensor/foxess_{serial}/mesh_peer_serial/config"
        peer_payload: dict[str, Any] = {
            "name": "Mesh Peer Serial",
            "unique_id": f"foxess_{serial}_mesh_peer_serial",
            "object_id": f"foxess_{serial}_mesh_peer_serial",
            "value_template": "{{ value_json.mesh_peer_serial | default('') }}",
            **common,
        }
        self._publish(peer_topic, json.dumps(peer_payload, separators=(",", ":")), retain=True)

    def _availability_block(self, serial: str) -> list[dict[str, str]]:
        return [
            {
                "topic": f"{self.config.topic_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
            },
            {
                "topic": f"{self.config.topic_prefix}/{serial}/availability",
                "payload_available": "online",
                "payload_not_available": "offline",
            },
        ]

    def _state_dict(self, telemetry: Telemetry) -> dict[str, Any]:
        state = telemetry.as_dict()
        if self.config.debug:
            return state
        return {key: value for key, value in state.items() if not is_debug_field(key)}

    def _clear_legacy_discovery(self, telemetry: Telemetry) -> None:
        for field_name in LEGACY_DISCOVERY_FIELDS:
            config_topic = f"{self.config.discovery_prefix}/sensor/foxess_{telemetry.serial}/{field_name}/config"
            self._publish(config_topic, "", retain=True)

    def _clear_debug_discovery(self, telemetry: Telemetry) -> None:
        for field_name in telemetry.as_dict():
            if not is_debug_field(field_name):
                continue
            config_topic = f"{self.config.discovery_prefix}/sensor/foxess_{telemetry.serial}/{field_name}/config"
            self._publish(config_topic, "", retain=True)

    def _publish_scalar_topics(self, telemetry: Telemetry) -> None:
        serial = telemetry.serial
        base = f"{self.config.topic_prefix}/{serial}"
        scalar_values: dict[str, int | float | str] = {
            "0/power": telemetry.pv_power_w,
            "0/ac/power": telemetry.r_power_w,
            "0/ac/export_power": telemetry.export_power_w,
            "0/ac/voltage": telemetry.r_voltage_v,
            "0/ac/current": telemetry.r_current_a,
            "0/ac/frequency": telemetry.r_frequency_hz,
            "0/temperature": telemetry.inverter_temperature_c,
            "0/yieldtotal": telemetry.generation_kwh,
            "0/status": telemetry.operating_state,
            "1/power": telemetry.pv1_power_w,
            "1/voltage": telemetry.pv1_voltage_v,
            "1/current": telemetry.pv1_current_a,
            "2/power": telemetry.pv2_power_w,
            "2/voltage": telemetry.pv2_voltage_v,
            "2/current": telemetry.pv2_current_a,
        }
        optional_values = {
            "3/power": telemetry.pv3_power_w,
            "3/voltage": telemetry.pv3_voltage_v,
            "3/current": telemetry.pv3_current_a,
            "4/power": telemetry.pv4_power_w,
            "4/voltage": telemetry.pv4_voltage_v,
            "4/current": telemetry.pv4_current_a,
        }
        for topic_suffix, value in optional_values.items():
            if value is not None:
                scalar_values[topic_suffix] = value
        if self.config.debug:
            scalar_values["0/sequence"] = telemetry.sequence
            scalar_values["0/status_code"] = telemetry.operating_state_code
        for topic_suffix, value in scalar_values.items():
            self._publish(f"{base}/{topic_suffix}", str(value), retain=self.config.retain)
        if not self.config.debug:
            for topic_suffix in DEBUG_SCALAR_TOPICS:
                self._publish(f"{base}/{topic_suffix}", "", retain=True)

    def _publish(self, topic: str, payload: str, retain: bool) -> None:
        assert self.client is not None
        try:
            result = self.client.publish(topic, payload, retain=retain)
        except Exception as exc:
            self.emit("mqtt_publish_error", topic=topic, error=str(exc))
            return
        rc = getattr(result, "rc", 0)
        if rc:
            self.emit("mqtt_publish_error", topic=topic, rc=rc)


def metadata_for(name: str) -> tuple[str | None, str | None, str | None]:
    if name.endswith("_w"):
        return "W", "power", "measurement"
    if name.endswith("_v"):
        return "V", "voltage", "measurement"
    if name.endswith("_a"):
        return "A", "current", "measurement"
    if name.endswith("_hz"):
        return "Hz", "frequency", "measurement"
    if name.endswith("_c"):
        return "°C", "temperature", "measurement"
    if name.endswith("_kwh"):
        return "kWh", "energy", "total_increasing"
    if name == "operating_state":
        return None, "enum", None
    return None, None, None


def is_debug_field(name: str) -> bool:
    return name in {"sequence", "operating_state_code"} or name.startswith("raw_")


def friendly_field_name(name: str) -> str:
    explicit = {
        "r_power_w": "AC Power",
        "export_power_w": "Export Power",
        "r_voltage_v": "AC Voltage",
        "r_current_a": "AC Current",
        "r_frequency_hz": "AC Frequency",
        "pv_power_w": "PV Power",
        "pv1_power_w": "PV1 Power",
        "pv1_voltage_v": "PV1 Voltage",
        "pv1_current_a": "PV1 Current",
        "pv2_power_w": "PV2 Power",
        "pv2_voltage_v": "PV2 Voltage",
        "pv2_current_a": "PV2 Current",
        "pv3_power_w": "PV3 Power",
        "pv3_voltage_v": "PV3 Voltage",
        "pv3_current_a": "PV3 Current",
        "pv4_power_w": "PV4 Power",
        "pv4_voltage_v": "PV4 Voltage",
        "pv4_current_a": "PV4 Current",
        "inverter_temperature_c": "Inverter Temperature",
        "generation_kwh": "Total Generation",
        "export_total_kwh": "Total Grid Export",
        "operating_state": "Operating State",
        "operating_state_code": "Operating State Code",
        "sequence": "Sequence",
    }
    if name in explicit:
        return explicit[name]
    return name.replace("_", " ").title()


def reason_is_success(reason_code: Any) -> bool:
    if reason_code is None:
        return True
    value = getattr(reason_code, "value", None)
    if value is not None:
        return value == 0
    try:
        return int(reason_code) == 0
    except (TypeError, ValueError):
        pass
    return str(reason_code).lower() in {"success", "normal disconnection"}
