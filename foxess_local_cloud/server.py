"""Async local FoxESS cloud emulator."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import socket
import ssl
import struct
import time
from pathlib import Path
from typing import Any, TextIO

from .cert import ensure_cert
from .config import AppConfig
from .local_control import LocalControl, normalize_device
from .mqtt import MqttPublisher
from .protocol import (
    BootstrapResponder,
    Frame,
    ascii_text,
    extract_frames,
    build_modbus_read_holding,
    build_modbus_read_input,
    build_modbus_write_single,
    is_injected_device,
    is_mesh_follower_frame,
    is_mesh_root_frame,
    is_modbus_command,
    is_modbus_read_response,
    is_module_info,
    is_product_info,
    is_registration,
    is_telemetry,
    mesh_peer_serial,
    parse_modbus_command,
    parse_modbus_read_response,
    INJECTED_DEVICE_MARKER,
    module_info,
    product_info,
    registration_serial,
)
from .telemetry import decode_telemetry, fault_code_for, nonzero_u16_words


FOXESS_CIPHERS = "ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384"
# Pinned SHA-256 fingerprint of the FoxESS Cloud TLS cert (self-signed,
# valid until 2124, identical across the known upstream IPs as of 2026).
# Used to detect MITM on the upstream relay leg, since the FoxESS PKI is
# self-signed and standard CA validation cannot apply. If FoxESS rotates,
# update this constant or set relay.skip_cert_verify=true in config.
FOXESS_UPSTREAM_CERT_SHA256 = "0ff6d2d0b548f0a03dced31ce7621a8c9497bdc074d723bc152094e4d299c1b7"

_MODBUS_NAMES = {
    0x03: "read_holding",
    0x04: "read_input",
    0x06: "write_single",
    0x10: "write_multiple",
}


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

    def ssl_context(self) -> ssl.SSLContext:
        ensure_cert(self.config.cert, self.config.key, self.config.force_cert)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.set_ciphers(FOXESS_CIPHERS)
        context.options |= ssl.OP_CIPHER_SERVER_PREFERENCE
        try:
            context.set_ecdh_curve("X25519")
        except ValueError:
            pass
        context.load_cert_chain(self.config.cert, self.config.key)
        return context

    async def run(self) -> None:
        self.mqtt.connect()
        server = await asyncio.start_server(self.handle_client, self.config.host, self.config.port, ssl=self.ssl_context())
        sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        self.logger.emit("listen", sockets=sockets)
        async with server:
            await server.serve_forever()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session_id = self.next_session_id
        self.next_session_id += 1
        peer = writer.get_extra_info("peername")
        self.logger.emit("connect", session=session_id, peer=str(peer))
        session = Session(self, session_id)
        try:
            await session.run(reader, writer)
        except Exception as exc:
            self.logger.emit("session_error", session=session_id, error=str(exc))
        finally:
            writer.close()
            await writer.wait_closed()
            self.logger.emit("disconnect", session=session_id, serial=session.serial or "")

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
        self.local_control: LocalControl | None = None
        self._inverter_control_command_registered = False
        if app.config.inverter_control.enabled:
            self.local_control = LocalControl(
                emit=self.app.logger.emit,
                session_id=self.session_id,
                register_pending_read=lambda key, addr, count: self.pending_reads.__setitem__(key, (addr, count)),
            )

    def _maybe_wire_active_power_limit_mqtt(self) -> None:
        """Once we know the serial, register the HA Number entity and the
        command-topic handler that translates incoming setpoints into a
        Modbus write. Idempotent."""
        if self._inverter_control_command_registered:
            return
        if self.local_control is None or not self.serial:
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

    async def _handle_active_power_limit_setpoint(self, percent: int) -> None:
        if self.local_control is None or not self.serial:
            return
        address = self.app.config.inverter_control.active_power_limit_address
        try:
            await self.local_control.write_register(address, percent)
        except Exception as exc:
            self.app.logger.emit(
                "active_power_limit_write_error",
                session=self.session_id,
                serial=self.serial,
                value=percent,
                error=str(exc),
            )
            return
        # Optimistic state publication so HA updates immediately. The next
        # read of this register (either by us or by the cloud) will correct
        # it if the inverter clamped/rejected the value.
        self.app.mqtt.publish_active_power_limit_state(self.serial, percent)

    async def run(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.app.config.relay.enabled:
            await self.run_relay(reader, writer)
            return
        await self.run_local(reader, writer)

    async def run_local(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.local_control is not None:
            self.local_control.attach_inverter_writer(writer)
        poll_task = self._start_poll_task()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    return
                self.buffer.extend(data)
                for frame in extract_frames(self.buffer):
                    await self.handle_frame(frame, writer)
        finally:
            await self._cancel_poll_task(poll_task)

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
        if self.local_control is not None:
            self.local_control.attach_inverter_writer(writer)
        poll_task = self._start_poll_task()
        try:
            await asyncio.gather(
                self.relay_client_to_upstream(reader, upstream_writer),
                self.relay_upstream_to_client(upstream_reader, writer),
            )
        finally:
            await self._cancel_poll_task(poll_task)

    def _start_poll_task(self) -> asyncio.Task[None] | None:
        if self.local_control is None or self.app.config.inverter_control.poll_interval_seconds <= 0:
            return None
        return asyncio.create_task(self._poll_loop())

    async def _cancel_poll_task(self, task: asyncio.Task[None] | None) -> None:
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _poll_loop(self) -> None:
        cfg = self.app.config.inverter_control
        interval = cfg.poll_interval_seconds
        # Initial delay so we don't fire a Modbus read before the inverter
        # has finished its registration handshake on a fresh session.
        await asyncio.sleep(interval)
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.app.logger.emit("inverter_control_poll_error", session=self.session_id, error=str(exc))
            await asyncio.sleep(interval)

    async def _poll_once(self) -> None:
        """One iteration of the poll loop. Separate method so tests can
        exercise the read injection without driving the timed loop."""
        if self.local_control is None:
            return
        cfg = self.app.config.inverter_control
        await self.local_control.read_input(cfg.telemetry_input_address, cfg.telemetry_input_count)

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
                    await self.handle_frame(frame, upstream_writer, send_bootstrap=False)
                    if self.local_control is not None and self.local_control.is_our_frame(frame):
                        idx = forward.find(frame.raw)
                        if idx >= 0:
                            del forward[idx : idx + len(frame.raw)]
                        self.app.logger.emit(
                            "injected_response_filtered",
                            session=self.session_id,
                            device=frame.device.hex(),
                            bytes=len(frame.raw),
                        )
                if forward:
                    upstream_writer.write(bytes(forward))
                    await upstream_writer.drain()
        finally:
            upstream_writer.close()

    async def relay_upstream_to_client(self, upstream_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await upstream_reader.read(4096)
                if not data:
                    return
                self.app.logger.emit("relay_decrypted", session=self.session_id, direction="upstream_to_client", bytes=len(data), payload_hex=data.hex(" "))
                self.upstream_buffer.extend(data)
                for frame in extract_frames(self.upstream_buffer):
                    self.handle_upstream_frame(frame)
                writer.write(data)
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
        if is_registration(frame):
            self.serial = registration_serial(frame)
            self.app.logger.emit("registration", session=self.session_id, serial=self.serial or "")
            self._maybe_wire_active_power_limit_mqtt()
        if is_product_info(frame):
            info = product_info(frame)
            self.model = info.get("model", "") or self.model
            self.firmware = info.get("firmware", "") or self.firmware
            self.app.logger.emit("product_info", session=self.session_id, serial=self.serial or "", **info)
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
        if is_modbus_read_response(frame):
            pdu = parse_modbus_read_response(frame)
            pending = self.pending_reads.pop(normalize_device(bytes(frame.device)), None)
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
                self.app.mqtt.publish_active_power_limit_state(self.serial, int(pdu["values"][0]))

    def _update_fault_state(self, payload: bytes) -> None:
        from .telemetry import u16
        offsets = (u16(payload, 100), u16(payload, 102), u16(payload, 104), u16(payload, 106))
        fault_active = any(offsets) or bool(u16(payload, 98))
        if fault_active and not self._previous_fault_active:
            code = fault_code_for(offsets)
            now = time.time()
            timestamp = _iso8601(now)
            self.last_fault_code = code
            self.last_fault_timestamp = timestamp
            self.app.logger.emit(
                "fault_observed",
                session=self.session_id,
                serial=self.serial or "",
                code=code,
                offsets={"100": offsets[0], "102": offsets[1], "104": offsets[2], "106": offsets[3]},
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
