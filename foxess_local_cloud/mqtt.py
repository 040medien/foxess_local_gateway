"""Optional MQTT publishing for decoded FoxESS telemetry."""

from __future__ import annotations

import json
from typing import Any, Callable

from .config import MqttConfig
from .telemetry import Telemetry


DEBUG_SCALAR_TOPICS = ("0/sequence", "0/status_code")
OPERATING_STATE_OPTIONS = ("standby", "running", "unknown")


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
            self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        if self.config.username:
            self.client.username_pw_set(self.config.username, self.config.password)
        if hasattr(self.client, "will_set"):
            self.client.will_set(f"{self.config.topic_prefix}/status", "offline", retain=True)
        try:
            if hasattr(self.client, "connect_async"):
                self.client.connect_async(self.config.host, self.config.port)
            else:
                self.client.connect(self.config.host, self.config.port)
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
        should_announce = telemetry.serial not in self.announced
        if telemetry.model and telemetry.model != known_model:
            should_announce = True
        if telemetry.model:
            self.model_by_serial[telemetry.serial] = telemetry.model
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
        device = {"identifiers": [f"foxess_{serial}"], "name": name, "manufacturer": "FoxESS", "model": model}
        state_topic = f"{self.config.topic_prefix}/{serial}/state"
        availability = self._availability_block(serial)
        state = self._state_dict(telemetry)
        for field_name in state:
            if field_name in {"serial", "model"}:
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
