"""Async local FoxESS cloud emulator."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import ssl
import struct
import time
from pathlib import Path
from typing import Any, TextIO

from .cert import ensure_cert
from .config import AppConfig
from .firmware import FirmwareCapture, FirmwareImage, FirmwareProtocolError, FirmwareUploader
from .local_control import LocalControl, WRITE_CONFIRMED, normalize_device
from .mqtt import MqttPublisher
from .protocol import (
    BootstrapResponder,
    Frame,
    ascii_text,
    extract_frames,
    build_modbus_write_single,
    is_mesh_follower_frame,
    is_mesh_root_frame,
    is_modbus_command,
    is_modbus_read_exception,
    is_modbus_read_response,
    is_modbus_write_success_response,
    is_module_info,
    is_product_info,
    is_registration,
    is_telemetry,
    mesh_peer_serial,
    parse_modbus_command,
    parse_modbus_read_exception,
    parse_modbus_read_response,
    module_info,
    product_info,
    registration_serial,
)
from .telemetry import decode_telemetry, fault_code_for, is_known_fault_code, nonzero_u16_words


FOXESS_CIPHERS = "ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384"
# Pinned SHA-256 fingerprint of the FoxESS Cloud TLS cert (self-signed,
# valid until 2124, identical across the known upstream IPs as of 2026).
# Used to detect MITM on the upstream relay leg, since the FoxESS PKI is
# self-signed and standard CA validation cannot apply. If FoxESS rotates,
# update this constant or set relay.skip_cert_verify=true in config.
FOXESS_UPSTREAM_CERT_SHA256 = "0ff6d2d0b548f0a03dced31ce7621a8c9497bdc074d723bc152094e4d299c1b7"
ACTIVE_POWER_LIMIT_MIN_FIRMWARE = (1, 80)
ACTIVE_POWER_LIMIT_MIN_FIRMWARE_TEXT = "1.80"

_MODBUS_NAMES = {
    0x03: "read_holding",
    0x04: "read_input",
    0x06: "write_single",
    0x10: "write_multiple",
}

_EXPECTED_TLS_DISCONNECT_ERRORS = (
    "application data after close notify",
    "eof occurred in violation of protocol",
    "ssl shutdown timed out",
)


def expected_disconnect_reason(exc: BaseException) -> str | None:
    message = str(exc).lower()
    if isinstance(exc, TimeoutError) and "ssl shutdown timed out" in message:
        return "ssl_shutdown_timeout"
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return "connection_lost"
    if isinstance(exc, ssl.SSLError) and any(
        expected in message for expected in _EXPECTED_TLS_DISCONNECT_ERRORS
    ):
        return "tls_connection_lost"
    return None


def supports_active_power_limit(firmware: str) -> bool:
    """Whether a reported FoxESS firmware version supports register 0xCA5A.

    FoxESS currently reports versions such as ``1.80`` and ``1.84``. Accept a
    leading ``v`` and a patch/suffix defensively, but fail closed when no
    numeric major/minor pair is available.
    """
    match = re.search(r"(?i)(?:^|[^0-9])v?(\d+)\.(\d+)(?:\.\d+)?", firmware.strip())
    if match is None:
        return False
    version = (int(match.group(1)), int(match.group(2)))
    return version >= ACTIVE_POWER_LIMIT_MIN_FIRMWARE


class JsonLogger:
    def __init__(self, path: Path | None = None) -> None:
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.file: TextIO | None = path.open("a", encoding="utf-8") if path else None

    def emit(self, event: str, **fields: Any) -> None:
        record = {"ts": time.time(), "event": event, **fields}
        line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        print(line, flush=True)
        if self.file:
            self.file.write(line + "\n")
            self.file.flush()

    def close(self) -> None:
        if self.file:
            self.file.close()


class FoxessLocalCloud:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = JsonLogger(config.jsonl)
        self.mqtt = MqttPublisher(config.mqtt, config.devices, emit=self.logger.emit)
        self.next_session_id = 1
        self.last_publish_by_serial: dict[str, float] = {}
        # ActivePowerLimit retry-on-reconnect state, keyed by serial. ``desired``
        # is the last setpoint Home Assistant asked for; ``applied`` is the last
        # value the inverter acknowledged (kept for observability). A serial is
        # "pending" until *that specific desired command* is confirmed — tracked
        # by ``_confirmed`` rather than by comparing values, because a later
        # command can coincide with an older acknowledged value while an
        # unconfirmed write in between left the inverter somewhere else. All
        # outlive any single Session (recreated on reconnect), so they live on
        # the app.
        self.desired_active_power_limit: dict[str, int] = {}
        self.applied_active_power_limit: dict[str, int] = {}
        # Per-serial command generation. Each new setpoint bumps ``_gen``;
        # ``_confirmed_gen`` records the highest generation whose own write was
        # acknowledged. Confirmation is tied to the generation, not the percent,
        # so that with concurrent writes (e.g. 80 -> 50 -> 80) an ack for the
        # first 80 can't mark the latest 80 confirmed — otherwise a lost final
        # write could leave the inverter stuck at the intervening value.
        self._active_power_limit_gen: dict[str, int] = {}
        self._active_power_limit_confirmed_gen: dict[str, int] = {}
        self.sessions_by_serial: dict[str, Session] = {}

    def set_desired_active_power_limit(self, serial: str, percent: int) -> int:
        """Record a new desired setpoint and return its command generation."""
        self.desired_active_power_limit[serial] = percent
        generation = self._active_power_limit_gen.get(serial, 0) + 1
        self._active_power_limit_gen[serial] = generation
        return generation

    def current_active_power_limit_generation(self, serial: str) -> int:
        return self._active_power_limit_gen.get(serial, 0)

    def mark_active_power_limit_applied(self, serial: str, percent: int, generation: int) -> None:
        """Record an acknowledged write for ``generation``. Only advances the
        confirmed generation, so a late ack for a superseded command cannot
        clear a newer pending setpoint."""
        self.applied_active_power_limit[serial] = percent
        if generation > self._active_power_limit_confirmed_gen.get(serial, 0):
            self._active_power_limit_confirmed_gen[serial] = generation

    def pending_active_power_limit(self, serial: str) -> int | None:
        """The setpoint awaiting (re)application for this serial, or None when
        there is no desired value or the latest command's write was acked."""
        desired = self.desired_active_power_limit.get(serial)
        if desired is None:
            return None
        confirmed = self._active_power_limit_confirmed_gen.get(serial, 0)
        if confirmed >= self._active_power_limit_gen.get(serial, 0):
            return None
        return desired

    def ssl_context(self) -> ssl.SSLContext:
        ensure_cert(self.config.cert, self.config.key, self.config.force_cert)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.set_ciphers(FOXESS_CIPHERS)
        context.options |= ssl.OP_CIPHER_SERVER_PREFERENCE
        try:
            context.set_ecdh_curve("X25519")
        except (ValueError, ssl.SSLError):
            pass
        context.load_cert_chain(self.config.cert, self.config.key)
        return context

    async def run(self) -> None:
        self.mqtt.connect()
        server = await asyncio.start_server(self.handle_client, self.config.host, self.config.port, ssl=self.ssl_context())
        sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        self.logger.emit("listen", sockets=sockets)
        control_server: asyncio.AbstractServer | None = None
        control_path = self.config.firmware_control_socket
        if control_path is not None:
            control_path.parent.mkdir(parents=True, exist_ok=True)
            control_path.unlink(missing_ok=True)
            control_server = await asyncio.start_unix_server(self.handle_firmware_control, path=control_path)
            control_path.chmod(0o600)
            self.logger.emit("firmware_control_listen", path=str(control_path))
        watchdog = asyncio.create_task(self._mqtt_watchdog())
        tasks = [asyncio.create_task(server.serve_forever())]
        if control_server is not None:
            tasks.append(asyncio.create_task(control_server.serve_forever()))
        try:
            await asyncio.gather(*tasks)
        finally:
            watchdog.cancel()
            for task in tasks:
                task.cancel()
            server.close()
            await server.wait_closed()
            if control_server is not None:
                control_server.close()
                await control_server.wait_closed()
            if control_path is not None:
                control_path.unlink(missing_ok=True)

    async def handle_firmware_control(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Receive one root/local firmware upload request over the Unix socket."""
        response: dict[str, Any]
        try:
            header_size = int.from_bytes(await reader.readexactly(4), "big")
            if not 0 < header_size <= 65536:
                raise FirmwareProtocolError("invalid firmware control header size")
            header = json.loads((await reader.readexactly(header_size)).decode("utf-8"))
            if header.get("command") != "upgrade":
                raise FirmwareProtocolError("unsupported firmware control command")
            size = int(header.get("size", 0))
            if not 0 < size <= 0xFFFF:
                raise FirmwareProtocolError("invalid firmware image size")
            data = await reader.readexactly(size)
            serial = str(header.get("serial", ""))
            image = FirmwareImage.from_bytes(data, str(header.get("filename", "firmware.bin")))
            expected_sha256 = str(header.get("sha256", "")).lower()
            allow_unverified = bool(header.get("allow_unverified", False))
            if not expected_sha256 and not allow_unverified:
                raise FirmwareProtocolError("an expected SHA-256 is required")
            if expected_sha256 and (
                len(expected_sha256) != 64
                or any(char not in "0123456789abcdef" for char in expected_sha256)
            ):
                raise FirmwareProtocolError("expected SHA-256 must be 64 hexadecimal characters")
            if expected_sha256 and image.sha256 != expected_sha256:
                raise FirmwareProtocolError(
                    f"SHA-256 mismatch: expected {expected_sha256}, got {image.sha256}"
                )
            session = self.sessions_by_serial.get(serial)
            if session is None:
                raise FirmwareProtocolError(f"inverter {serial!r} is not connected")
            protocol = str(header.get("protocol", "foxess-7f-func-99"))
            response = await session.upload_firmware(image, protocol=protocol)
            response["ok"] = True
        except Exception as exc:
            response = {"ok": False, "error": str(exc) or type(exc).__name__}
            self.logger.emit("firmware_control_error", error=response["error"])
        try:
            writer.write((json.dumps(response, sort_keys=True) + "\n").encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _mqtt_watchdog(self) -> None:
        """Periodically verify the MQTT network loop is alive and rebuild the
        client if it died. Without this, a dead paho loop thread silently
        drops every publish (rc=0, no on_disconnect) until the daemon is
        manually restarted."""
        interval = self.config.mqtt.health_check_interval_seconds
        if interval <= 0 or not self.mqtt.enabled:
            return
        while True:
            await asyncio.sleep(interval)
            try:
                self.mqtt.ensure_connected()
            except Exception as exc:
                self.logger.emit("mqtt_watchdog_error", error=str(exc))

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session_id = self.next_session_id
        self.next_session_id += 1
        peer = writer.get_extra_info("peername")
        self.logger.emit("connect", session=session_id, peer=str(peer))
        apply_inverter_keepalive(writer, self.config.inverter_tcp_keepalive_seconds)
        session = Session(self, session_id)
        disconnect_reason = "eof"
        try:
            await session.run(reader, writer)
        except Exception as exc:
            expected_reason = expected_disconnect_reason(exc)
            if expected_reason:
                disconnect_reason = expected_reason
            else:
                disconnect_reason = "session_error"
                self.logger.emit("session_error", session=session_id, error=str(exc))
        finally:
            if session.serial and self.sessions_by_serial.get(session.serial) is session:
                self.sessions_by_serial.pop(session.serial, None)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception as exc:
                expected_reason = expected_disconnect_reason(exc)
                if expected_reason:
                    if disconnect_reason == "eof":
                        disconnect_reason = expected_reason
                else:
                    self.logger.emit("disconnect_error", session=session_id, error=str(exc))
            self.logger.emit(
                "disconnect",
                session=session_id,
                serial=session.serial or "",
                reason=disconnect_reason,
            )

    def publish_telemetry(self, session_id: int, telemetry: Any, raw_nonzero_u16: dict[str, int] | None = None) -> None:
        serial = telemetry.serial
        now = time.time()
        min_interval = self.config.publish_min_interval_seconds
        if serial and min_interval > 0 and now - self.last_publish_by_serial.get(serial, 0) < min_interval:
            self.logger.emit("publish_skipped", session=session_id, serial=serial, reason="min_interval")
            return
        if serial:
            self.last_publish_by_serial[serial] = now
        fields = telemetry.as_dict()
        if raw_nonzero_u16 is not None:
            fields["raw_nonzero_u16"] = raw_nonzero_u16
        self.logger.emit("telemetry", session=session_id, **fields)
        self.mqtt.publish(telemetry)


class Session:
    def __init__(self, app: FoxessLocalCloud, session_id: int) -> None:
        self.app = app
        self.session_id = session_id
        self.serial: str | None = None
        self.model: str = ""
        self.firmware: str = ""
        self.module: str = ""
        self.last_fault_code: str = ""
        self.last_fault_timestamp: str = ""
        self.mesh_role: str = ""
        self.mesh_peer_serial: str = ""
        self._previous_fault_active: bool = False
        self.bootstrap = BootstrapResponder()
        self.buffer = bytearray()
        self.upstream_buffer = bytearray()
        self.last_telemetry_at: float | None = None
        # Outstanding read requests keyed by NORMALIZED envelope device bytes
        # (first-byte bit 7 cleared, so request and echoed response use the
        # same key). Used to annotate the read response with the address it
        # was asking about.
        self.pending_reads: dict[bytes, tuple[int, int]] = {}
        # Diagnostic readbacks issued immediately after an acknowledged local
        # ActivePowerLimit write. Kept separately so ordinary startup and
        # cloud-originated reads retain their existing correlation shape.
        self.pending_active_power_limit_readbacks: dict[bytes, tuple[int, str]] = {}
        # Cloud-originated write requests keyed the same way. Used to publish
        # externally changed writable settings only after the inverter echoes a
        # successful write response.
        self.pending_writes: dict[bytes, tuple[int, int]] = {}
        self.local_control: LocalControl | None = None
        self._inverter_control_command_registered = False
        self._inverter_control_settle_scheduled = False
        self.inverter_writer: asyncio.StreamWriter | None = None
        self.client_device_tail = b""
        self.last_client_func = 0
        self.firmware_uploader: FirmwareUploader | None = None
        self._firmware_upload_lock = asyncio.Lock()
        capture_config = app.config.firmware_capture
        self.firmware_capture: FirmwareCapture | None = None
        if capture_config.enabled:
            self.firmware_capture = FirmwareCapture(
                capture_config.directory,
                self.app.logger.emit,
                self.session_id,
                simulate_progress=capture_config.simulate_progress,
                progress_interval_seconds=capture_config.progress_interval_seconds,
            )
        if app.config.inverter_control.enabled:
            self.local_control = LocalControl(
                emit=self.app.logger.emit,
                session_id=self.session_id,
                register_pending_read=self._register_pending_read,
                unregister_pending_read=self._unregister_pending_read,
            )

    def _register_pending_read(
        self,
        key: bytes,
        address: int,
        count: int,
        expected_value: int | None,
        source: str,
    ) -> None:
        self.pending_reads[key] = (address, count)
        if expected_value is not None:
            self.pending_active_power_limit_readbacks[key] = (expected_value, source)

    def _unregister_pending_read(self, key: bytes) -> None:
        self.pending_reads.pop(key, None)
        self.pending_active_power_limit_readbacks.pop(key, None)

    def _maybe_wire_active_power_limit_mqtt(self) -> None:
        """Once we know the serial, register the HA Number entity and the
        command-topic handler that translates incoming setpoints into a
        Modbus write. Idempotent."""
        if self._inverter_control_command_registered:
            return
        if (
            self.local_control is None
            or not self.serial
            or not supports_active_power_limit(self.firmware)
        ):
            return
        publisher = self.app.mqtt
        loop = asyncio.get_running_loop()

        def _on_setpoint(percent: int) -> None:
            # Runs on the MQTT thread; bridge to the asyncio loop.
            asyncio.run_coroutine_threadsafe(
                self._handle_active_power_limit_setpoint(percent),
                loop,
            )

        publisher.register_active_power_limit_handler(self.serial, _on_setpoint)
        publisher.publish_active_power_limit_discovery(self.serial)
        self._inverter_control_command_registered = True
        # The one-shot read of the current setpoint is deferred until the
        # first telemetry frame arrives — see handle_frame. Injecting Modbus
        # at session start (T+~20ms after the inverter's first registration
        # frame) reliably kills the TLS session with
        # APPLICATION_DATA_AFTER_CLOSE_NOTIFY; waiting for telemetry is a
        # natural "session is settled" gate that avoids the fragile window.

    def _sync_active_power_limit_mqtt(self) -> None:
        """Expose local control only after a compatible version is known.

        Product-info frames repeat during a session, so both branches are
        idempotent. The unsupported branch also removes retained discovery
        left by an earlier/newer firmware session.
        """
        if not self.serial or self.local_control is None:
            return
        if supports_active_power_limit(self.firmware):
            self._maybe_wire_active_power_limit_mqtt()
            self._maybe_settle_inverter_control()
            return
        self.app.mqtt.unregister_active_power_limit_handler(self.serial)
        self.app.mqtt.clear_active_power_limit_discovery(self.serial)
        self._inverter_control_command_registered = False

    def _maybe_settle_inverter_control(self) -> None:
        """Schedule the startup read once both compatibility and a settled
        session are known, regardless of which signal arrived first."""
        if (
            self._inverter_control_settle_scheduled
            or self.last_telemetry_at is None
            or self.local_control is None
            or not self._inverter_control_command_registered
        ):
            return
        self._inverter_control_settle_scheduled = True
        asyncio.create_task(self._settle_inverter_control())

    async def _handle_active_power_limit_setpoint(self, percent: int) -> None:
        if self.local_control is None or not self.serial:
            return
        # Record the desired value first so it survives an unconfirmed write and
        # supersedes any older pending value, then attempt to apply it now. The
        # returned generation ties this command's confirmation to this exact
        # write, so a stale ack can't mark a newer setpoint applied.
        generation = self.app.set_desired_active_power_limit(self.serial, percent)
        await self._apply_active_power_limit(percent, generation=generation, source="setpoint")

    async def _apply_active_power_limit(self, percent: int, *, generation: int, source: str) -> None:
        """Write a setpoint and report the outcome. On a confirmed write, mark
        that generation applied; the optimistic state is published only when the
        confirmed write is still the latest command. An unconfirmed write leaves
        the value pending for re-application on the next reconnect."""
        if self.local_control is None or not self.serial:
            return
        address = self.app.config.inverter_control.active_power_limit_address
        timeout = self.app.config.inverter_control.write_timeout_seconds
        try:
            result = await self.local_control.write_register(address, percent, timeout=timeout)
        except Exception as exc:
            self.app.logger.emit(
                "active_power_limit_write_error",
                session=self.session_id,
                serial=self.serial,
                value=percent,
                error=str(exc),
            )
            self.app.mqtt.publish_active_power_limit_result(self.serial, "error")
            return
        self.app.logger.emit(
            "active_power_limit_write_result",
            session=self.session_id,
            serial=self.serial,
            value=percent,
            result=result,
            source=source,
        )
        self.app.mqtt.publish_active_power_limit_result(self.serial, result)
        # On an acknowledged write, record the generation. Publish the optimistic
        # state only when this confirm is for the latest command, so a late ack
        # for a superseded value can't make HA show a setpoint that isn't current.
        if result == WRITE_CONFIRMED:
            self.app.mark_active_power_limit_applied(self.serial, percent, generation)
            if generation == self.app.current_active_power_limit_generation(self.serial):
                self.app.mqtt.publish_active_power_limit_state(self.serial, percent)
            await self._read_active_power_limit_once(expected_value=percent, source=source)

    async def _settle_inverter_control(self) -> None:
        """Run once the session has settled (first telemetry frame). Either
        re-apply a setpoint an earlier session couldn't confirm (so curtailment
        is self-healing across reconnects) OR read the current setpoint for HA —
        never issue the startup read before a pending re-apply. An acknowledged
        re-apply now performs its own correlated diagnostic readback."""
        pending = self.app.pending_active_power_limit(self.serial) if self.serial else None
        if pending is None:
            await self._read_active_power_limit_once()
            return
        generation = self.app.current_active_power_limit_generation(self.serial)
        self.app.logger.emit(
            "active_power_limit_reapply",
            session=self.session_id,
            serial=self.serial,
            value=pending,
        )
        await self._apply_active_power_limit(pending, generation=generation, source="reapply")

    async def _read_active_power_limit_once(
        self,
        *,
        expected_value: int | None = None,
        source: str = "startup",
    ) -> None:
        """Inject a one-shot read of the ActivePowerLimit register so HA can
        show the current setpoint as soon as the inverter is reachable. The
        response flows through the normal command_response handler, which
        publishes state via ``publish_active_power_limit_state``."""
        if self.local_control is None or not self.serial:
            return
        address = self.app.config.inverter_control.active_power_limit_address
        try:
            device = await self.local_control.read_holding(
                address,
                1,
                expected_value=expected_value,
                source=source,
            )
        except Exception as exc:
            self.app.logger.emit(
                "active_power_limit_read_error",
                session=self.session_id,
                serial=self.serial,
                error=str(exc),
            )
            if expected_value is not None:
                self._emit_active_power_limit_readback_result(
                    requested_value=expected_value,
                    source=source,
                    result="error",
                    error=str(exc),
                )
            return
        if expected_value is None:
            return
        if device is None:
            self._emit_active_power_limit_readback_result(
                requested_value=expected_value,
                source=source,
                result="no_connection",
            )
            return
        key = normalize_device(device)
        timeout = self.app.config.inverter_control.write_timeout_seconds
        asyncio.create_task(
            self._active_power_limit_readback_timeout(
                key,
                requested_value=expected_value,
                source=source,
                timeout=timeout if timeout > 0 else 3.0,
            )
        )

    async def _active_power_limit_readback_timeout(
        self,
        key: bytes,
        *,
        requested_value: int,
        source: str,
        timeout: float,
    ) -> None:
        await asyncio.sleep(timeout)
        if self.pending_active_power_limit_readbacks.pop(key, None) is None:
            return
        self.pending_reads.pop(key, None)
        self._emit_active_power_limit_readback_result(
            requested_value=requested_value,
            source=source,
            result="timeout",
        )

    def _emit_active_power_limit_readback_result(
        self,
        *,
        requested_value: int,
        source: str,
        result: str,
        readback_value: int | None = None,
        error: str | None = None,
        modbus_exception_code: int | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "session": self.session_id,
            "serial": self.serial or "",
            "model": self.model,
            "firmware": self.firmware,
            "mesh_role": self.mesh_role,
            "address": self.app.config.inverter_control.active_power_limit_address,
            "address_hex": f"0x{self.app.config.inverter_control.active_power_limit_address:04x}",
            "requested_value": requested_value,
            "result": result,
            "source": source,
        }
        if readback_value is not None:
            fields["readback_value"] = readback_value
        if error is not None:
            fields["error"] = error
        if modbus_exception_code is not None:
            fields["modbus_exception_code"] = modbus_exception_code
            fields["modbus_exception_code_hex"] = f"0x{modbus_exception_code:02x}"
        self.app.logger.emit("active_power_limit_readback_result", **fields)

    async def run(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.app.config.relay.enabled:
            await self.run_relay(reader, writer)
            return
        await self.run_local(reader, writer)

    async def run_local(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.inverter_writer = writer
        if self.local_control is not None:
            self.local_control.attach_inverter_writer(writer)
        while True:
            data = await reader.read(4096)
            if not data:
                return
            self.buffer.extend(data)
            for frame in extract_frames(self.buffer):
                self._handle_firmware_upload_frame(frame)
                await self.handle_frame(frame, writer)

    async def run_relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        original_ip = original_destination_ip(writer)
        upstream = self.choose_upstream(original_ip)
        if not upstream:
            self.app.logger.emit("relay_no_upstream", session=self.session_id, original_ip=original_ip or "")
            await self.run_local(reader, writer)
            return
        upstream_host, upstream_port = upstream
        self.app.logger.emit(
            "relay_connecting",
            session=self.session_id,
            original_ip=original_ip or "",
            upstream_host=upstream_host,
            upstream_port=upstream_port,
        )
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    upstream_host,
                    upstream_port,
                    ssl=ssl._create_unverified_context(),
                    server_hostname=original_ip or upstream_host,
                ),
                timeout=self.app.config.relay.connect_timeout_seconds,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            self.app.logger.emit(
                "relay_connect_failed",
                session=self.session_id,
                upstream_host=upstream_host,
                upstream_port=upstream_port,
                error=str(exc) or type(exc).__name__,
            )
            await self.run_local(reader, writer)
            return
        if not self.app.config.relay.skip_cert_verify:
            mismatch = check_upstream_cert(upstream_writer)
            if mismatch:
                self.app.logger.emit(
                    "relay_cert_mismatch",
                    session=self.session_id,
                    upstream_host=upstream_host,
                    expected=FOXESS_UPSTREAM_CERT_SHA256,
                    actual=mismatch,
                )
                upstream_writer.close()
                try:
                    await upstream_writer.wait_closed()
                except Exception:
                    pass
                await self.run_local(reader, writer)
                return
        self.app.logger.emit("relay_connected", session=self.session_id, upstream_host=upstream_host, upstream_port=upstream_port)
        self.inverter_writer = writer
        if self.local_control is not None:
            self.local_control.attach_inverter_writer(writer)
        await asyncio.gather(
            self.relay_client_to_upstream(reader, upstream_writer),
            self.relay_upstream_to_client(upstream_reader, writer, upstream_writer),
        )

    def choose_upstream(self, original_ip: str | None) -> tuple[str, int] | None:
        upstreams = self.app.config.relay.upstreams
        if original_ip and original_ip in upstreams:
            return upstreams[original_ip]
        if original_ip and self.app.config.relay.fallback_to_original_destination and is_public_ipv4(original_ip):
            return (original_ip, 14431)
        if len(upstreams) == 1:
            return next(iter(upstreams.values()))
        return None

    async def relay_client_to_upstream(self, reader: asyncio.StreamReader, upstream_writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    return
                self.app.logger.emit("relay_decrypted", session=self.session_id, direction="client_to_upstream", bytes=len(data), payload_hex=data.hex(" "))
                self.buffer.extend(data)
                # Snapshot what's about to be classified, so we can rebuild a
                # cloud-bound byte stream that omits responses to our own
                # injected Modbus requests (the inverter still answers them,
                # but the cloud must never see those answers).
                snapshot = bytes(self.buffer)
                frames = extract_frames(self.buffer)
                leftover = bytes(self.buffer)
                consumed = snapshot[: len(snapshot) - len(leftover)]
                forward = bytearray(consumed)
                for frame in frames:
                    firmware_owned = self._handle_firmware_upload_frame(frame)
                    await self.handle_frame(frame, upstream_writer, send_bootstrap=False)
                    local_control_owned = self.local_control is not None and self.local_control.is_our_frame(frame)
                    if local_control_owned or firmware_owned:
                        idx = forward.find(frame.raw)
                        if idx >= 0:
                            del forward[idx : idx + len(frame.raw)]
                        if local_control_owned:
                            self.app.logger.emit(
                                "injected_response_filtered",
                                session=self.session_id,
                                device=frame.device.hex(),
                                bytes=len(frame.raw),
                            )
                        if firmware_owned:
                            self.app.logger.emit(
                                "firmware_upload_response_filtered",
                                session=self.session_id,
                                device=frame.device.hex(),
                                bytes=len(frame.raw),
                            )
                if forward:
                    upstream_writer.write(bytes(forward))
                    await upstream_writer.drain()
        finally:
            upstream_writer.close()

    async def relay_upstream_to_client(
        self,
        upstream_reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        upstream_writer: asyncio.StreamWriter | None = None,
    ) -> None:
        try:
            while True:
                data = await upstream_reader.read(4096)
                if not data:
                    return
                self.app.logger.emit("relay_decrypted", session=self.session_id, direction="upstream_to_client", bytes=len(data), payload_hex=data.hex(" "))
                self.upstream_buffer.extend(data)
                snapshot = bytes(self.upstream_buffer)
                frames = extract_frames(self.upstream_buffer)
                leftover = bytes(self.upstream_buffer)
                consumed = snapshot[: len(snapshot) - len(leftover)]
                forward = bytearray(consumed)
                for frame in frames:
                    self.handle_upstream_frame(frame)
                    captured = False
                    if self.firmware_capture is not None and upstream_writer is not None:
                        captured = await self.firmware_capture.handle_upstream_frame(
                            frame,
                            serial=self.serial or "",
                            upstream_writer=upstream_writer,
                            client_device_tail=self.client_device_tail,
                            last_client_func=self.last_client_func,
                        )
                    if captured:
                        idx = forward.find(frame.raw)
                        if idx >= 0:
                            del forward[idx:idx + len(frame.raw)]
                if forward:
                    writer.write(bytes(forward))
                    await writer.drain()
        finally:
            writer.close()

    def handle_upstream_frame(self, frame: Frame) -> None:
        now = time.time()
        fields: dict[str, Any] = {
            "session": self.session_id,
            "serial": self.serial or "",
            "family": frame.family,
            "device": frame.device.hex(),
            "func": f"0x{frame.func:02x}",
            "bytes": len(frame.raw),
            "payload_len": frame.payload_len,
            "valid_crc": frame.valid_crc,
            "payload_hex": frame.payload.hex(" "),
            "ascii": ascii_text(frame.payload),
        }
        if self.last_telemetry_at is not None:
            fields["seconds_since_last_telemetry"] = round(now - self.last_telemetry_at, 3)
        self.app.logger.emit("upstream_frame", **fields)
        if is_modbus_command(frame):
            pdu = parse_modbus_command(frame)
            if pdu["function"] in (0x03, 0x04):
                self.pending_reads[normalize_device(bytes(frame.device))] = (pdu["address"], pdu["count"])
            if (
                pdu["function"] == 0x06
                and self.local_control is not None
                and not self.local_control.is_our_frame(frame)
            ):
                self.pending_writes[normalize_device(bytes(frame.device))] = (pdu["address"], pdu["value"])
            # Cloud-originated command frames carry the session marker in
            # device[3]. Capture it so our injections can mimic it.
            if (
                self.local_control is not None
                and not self.local_control.is_our_frame(frame)
                and len(frame.device) >= 4
            ):
                self.local_control.observe_session_marker(frame.device[3])
            self.app.logger.emit(
                "command_frame",
                session=self.session_id,
                serial=self.serial or "",
                device=frame.device.hex(),
                slave=pdu["slave"],
                function=pdu["function"],
                function_name=_MODBUS_NAMES.get(pdu["function"], "unknown"),
                address=pdu["address"],
                address_hex=f"0x{pdu['address']:04x}",
                **{k: v for k, v in pdu.items() if k in ("value", "count", "values", "byte_count")},
            )

    async def handle_frame(self, frame: Frame, writer: asyncio.StreamWriter, send_bootstrap: bool = True) -> None:
        self.app.logger.emit(
            "frame",
            session=self.session_id,
            serial=self.serial or "",
            family=frame.family,
            device=frame.device.hex(),
            func=f"0x{frame.func:02x}",
            bytes=len(frame.raw),
            payload_len=frame.payload_len,
            valid_crc=frame.valid_crc,
            ascii=ascii_text(frame.payload),
            payload_hex=frame.payload.hex(" "),
        )
        if not frame.valid_crc:
            self.app.logger.emit("invalid_crc", session=self.session_id, serial=self.serial or "", bytes=len(frame.raw))
            return
        # Confirm any ActivePowerLimit write awaiting this echoed response. Safe
        # for every is-ours frame; a no-op for read responses. Runs for both
        # local and relay paths (run_relay separately strips it from upstream).
        if self.local_control is not None and self.local_control.is_our_frame(frame):
            # Only a genuine write acknowledgement counts as confirmed. Some
            # firmware replies with the full Modbus write echo, others with a
            # short ``01 06`` ACK. Exceptions/NAKs still do not confirm.
            confirmed = is_modbus_write_success_response(frame)
            self.local_control.resolve_response(bytes(frame.device), confirmed=confirmed)
        if is_modbus_write_success_response(frame):
            pending_write = self.pending_writes.pop(normalize_device(bytes(frame.device)), None)
            if (
                pending_write
                and self.serial
                and pending_write[0] == self.app.config.inverter_control.active_power_limit_address
            ):
                self.app.mqtt.publish_active_power_limit_state(self.serial, int(pending_write[1]))
        if is_registration(frame):
            self.serial = registration_serial(frame)
            self.client_device_tail = frame.device[1:]
            self.last_client_func = frame.func
            if self.serial:
                self.app.sessions_by_serial[self.serial] = self
                # A handler may still reference the previous Session after a
                # reconnect. Until product info proves compatibility, remove
                # both the command path and retained discovery so normal
                # telemetry cannot make a stale, non-functional slider online.
                self.app.mqtt.unregister_active_power_limit_handler(self.serial)
                self.app.mqtt.clear_active_power_limit_discovery(self.serial)
                self.firmware_uploader = FirmwareUploader(
                    self.app.logger.emit,
                    self.session_id,
                    self.serial,
                )
            self.app.logger.emit("registration", session=self.session_id, serial=self.serial or "")
        elif self.client_device_tail and frame.device[1:] == self.client_device_tail:
            self.last_client_func = frame.func
        if is_product_info(frame):
            info = product_info(frame)
            self.model = info.get("model", "") or self.model
            self.firmware = info.get("firmware", "") or self.firmware
            self.app.logger.emit("product_info", session=self.session_id, serial=self.serial or "", **info)
            self._sync_active_power_limit_mqtt()
        if is_module_info(frame):
            module = module_info(frame)
            if module:
                self.module = module
                self.app.logger.emit("module_info", session=self.session_id, serial=self.serial or "", module=module)
        if is_mesh_root_frame(frame):
            if self.mesh_role != "root" or self.mesh_peer_serial:
                self.mesh_role = "root"
                self.mesh_peer_serial = ""
                self.app.logger.emit("mesh_role", session=self.session_id, serial=self.serial or "", role="root")
        elif is_mesh_follower_frame(frame):
            peer = mesh_peer_serial(frame)
            if self.mesh_role != "follower" or self.mesh_peer_serial != peer:
                self.mesh_role = "follower"
                self.mesh_peer_serial = peer
                self.app.logger.emit("mesh_role", session=self.session_id, serial=self.serial or "", role="follower", peer_serial=peer)
        response = self.bootstrap.response_for(frame) if send_bootstrap else None
        if response:
            writer.write(response)
            await writer.drain()
            self.app.logger.emit("bootstrap_ack", session=self.session_id, serial=self.serial or "", bytes=len(response), hex=response.hex(" "))
        if is_telemetry(frame):
            self._update_fault_state(frame.payload)
            telemetry = decode_telemetry(
                frame.payload,
                self.serial or "",
                self.model,
                firmware=self.firmware,
                module=self.module,
                last_fault_code=self.last_fault_code,
                last_fault_timestamp=self.last_fault_timestamp,
                mesh_role=self.mesh_role,
                mesh_peer_serial=self.mesh_peer_serial,
            )
            self.app.publish_telemetry(self.session_id, telemetry, raw_nonzero_u16=nonzero_u16_words(frame.payload))
            self.last_telemetry_at = time.time()
            self._maybe_settle_inverter_control()
        if is_modbus_read_response(frame):
            pdu = parse_modbus_read_response(frame)
            key = normalize_device(bytes(frame.device))
            pending = self.pending_reads.pop(key, None)
            readback = self.pending_active_power_limit_readbacks.pop(key, None)
            fields: dict[str, Any] = {
                "session": self.session_id,
                "serial": self.serial or "",
                "device": frame.device.hex(),
                "slave": pdu["slave"],
                "function": pdu["function"],
                "function_name": _MODBUS_NAMES.get(pdu["function"], "unknown"),
                "byte_count": pdu["byte_count"],
                "values": pdu["values"],
            }
            if pending:
                address, count = pending
                fields["address"] = address
                fields["address_hex"] = f"0x{address:04x}"
                fields["count"] = count
            self.app.logger.emit("command_response", **fields)
            if (
                pending
                and self.local_control is not None
                and self.serial
                and pending[0] == self.app.config.inverter_control.active_power_limit_address
                and pdu["values"]
            ):
                actual_value = int(pdu["values"][0])
                self.app.mqtt.publish_active_power_limit_state(self.serial, actual_value)
                if readback:
                    requested_value, source = readback
                    self._emit_active_power_limit_readback_result(
                        requested_value=requested_value,
                        source=source,
                        result="matched" if actual_value == requested_value else "mismatch",
                        readback_value=actual_value,
                    )
        if is_modbus_read_exception(frame):
            pdu = parse_modbus_read_exception(frame)
            key = normalize_device(bytes(frame.device))
            pending = self.pending_reads.pop(key, None)
            readback = self.pending_active_power_limit_readbacks.pop(key, None)
            fields: dict[str, Any] = {
                "session": self.session_id,
                "serial": self.serial or "",
                "device": frame.device.hex(),
                "slave": pdu["slave"],
                "function": pdu["function"],
                "function_name": _MODBUS_NAMES.get(pdu["function"], "unknown"),
                "exception_code": pdu["exception_code"],
                "exception_code_hex": f"0x{pdu['exception_code']:02x}",
            }
            if pending:
                address, count = pending
                fields["address"] = address
                fields["address_hex"] = f"0x{address:04x}"
                fields["count"] = count
            self.app.logger.emit("command_error", **fields)
            if readback:
                requested_value, source = readback
                self._emit_active_power_limit_readback_result(
                    requested_value=requested_value,
                    source=source,
                    result="error",
                    error="modbus_exception",
                    modbus_exception_code=pdu["exception_code"],
                )

    def _handle_firmware_upload_frame(self, frame: Frame) -> bool:
        uploader = self.firmware_uploader
        return uploader.handle_client_frame(frame) if uploader is not None else False

    async def upload_firmware(
        self,
        image: FirmwareImage,
        *,
        protocol: str = "foxess-7f-func-99",
    ) -> dict[str, Any]:
        writer = self.inverter_writer
        uploader = self.firmware_uploader
        if writer is None or uploader is None:
            raise FirmwareProtocolError("inverter session is not ready for firmware upload")
        is_closing = getattr(writer, "is_closing", None)
        if callable(is_closing) and is_closing():
            raise FirmwareProtocolError("inverter connection is closing")
        async with self._firmware_upload_lock:
            return await uploader.upload(writer, image, protocol=protocol)

    def _update_fault_state(self, payload: bytes) -> None:
        from .telemetry import u16
        offset_98 = u16(payload, 98)
        offsets = (u16(payload, 100), u16(payload, 102), u16(payload, 104), u16(payload, 106))
        fault_active = any(offsets) or bool(offset_98)
        code = fault_code_for(offsets, offset_98) if fault_active else ""
        if code and (not self._previous_fault_active or code != self.last_fault_code):
            now = time.time()
            timestamp = _iso8601(now)
            self.last_fault_code = code
            self.last_fault_timestamp = timestamp
            self.app.logger.emit(
                "fault_observed",
                session=self.session_id,
                serial=self.serial or "",
                code=code,
                known=is_known_fault_code(code),
                offsets={"98": offset_98, "100": offsets[0], "102": offsets[1], "104": offsets[2], "106": offsets[3]},
                at=timestamp,
            )
        elif not fault_active and self._previous_fault_active:
            self.app.logger.emit(
                "fault_cleared",
                session=self.session_id,
                serial=self.serial or "",
                code=self.last_fault_code,
                at=_iso8601(time.time()),
            )
        self._previous_fault_active = fault_active


def _iso8601(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def apply_inverter_keepalive(writer: asyncio.StreamWriter, idle_seconds: int) -> None:
    """Enable TCP keepalive on the inverter connection so a marginal Wi-Fi link
    is kept warm and a dropped session is detected within ~a minute (feeding the
    ActivePowerLimit retry-on-reconnect). ``idle_seconds <= 0`` disables it. The
    per-idle/-interval/-count knobs are Linux-only; missing ones are skipped, so
    this is a safe no-op on platforms (or sockets) that don't support them."""
    if idle_seconds <= 0:
        return
    sock = writer.get_extra_info("socket")
    if sock is None or not hasattr(sock, "setsockopt"):
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        tunables = (
            ("TCP_KEEPIDLE", idle_seconds),
            ("TCP_KEEPINTVL", min(idle_seconds, 10)),
            ("TCP_KEEPCNT", 3),
        )
        for name, value in tunables:
            opt = getattr(socket, name, None)
            if opt is not None:
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)
    except OSError:
        pass


def original_destination_ip(writer: asyncio.StreamWriter) -> str | None:
    """Best-effort Linux SO_ORIGINAL_DST lookup for redirected IPv4 sockets."""

    sock = writer.get_extra_info("socket")
    if sock is None:
        return None
    try:
        data = sock.getsockopt(socket.SOL_IP, 80, 16)
    except OSError:
        return None
    if len(data) < 8:
        return None
    _family, _port, raw_addr = struct.unpack_from("!HH4s", data)
    return socket.inet_ntoa(raw_addr)


def check_upstream_cert(writer: asyncio.StreamWriter) -> str | None:
    """Return the peer cert's SHA-256 if it does not match the pinned FoxESS fingerprint."""

    ssl_object = writer.get_extra_info("ssl_object")
    if ssl_object is None:
        return "no_ssl_object"
    der = ssl_object.getpeercert(binary_form=True)
    if not der:
        return "no_peer_cert"
    actual = hashlib.sha256(der).hexdigest()
    if actual.lower() == FOXESS_UPSTREAM_CERT_SHA256.lower():
        return None
    return actual


def is_public_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.version == 4 and address.is_global
