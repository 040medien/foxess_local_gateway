"""Async local FoxESS cloud emulator."""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import struct
import time
from pathlib import Path
from typing import Any, TextIO

from .cert import ensure_cert
from .config import AppConfig
from .mqtt import MqttPublisher
from .protocol import (
    BootstrapResponder,
    Frame,
    ascii_text,
    extract_frames,
    is_product_info,
    is_registration,
    is_telemetry,
    product_info,
    registration_serial,
)
from .telemetry import decode_telemetry


FOXESS_CIPHERS = "ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384"


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

    def publish_telemetry(self, session_id: int, telemetry: Any) -> None:
        serial = telemetry.serial
        now = time.time()
        min_interval = self.config.publish_min_interval_seconds
        if serial and min_interval > 0 and now - self.last_publish_by_serial.get(serial, 0) < min_interval:
            self.logger.emit("publish_skipped", session=session_id, serial=serial, reason="min_interval")
            return
        if serial:
            self.last_publish_by_serial[serial] = now
        self.logger.emit("telemetry", session=session_id, **telemetry.as_dict())
        self.mqtt.publish(telemetry)


class Session:
    def __init__(self, app: FoxessLocalCloud, session_id: int) -> None:
        self.app = app
        self.session_id = session_id
        self.serial: str | None = None
        self.model: str = ""
        self.bootstrap = BootstrapResponder()
        self.buffer = bytearray()
        self.upstream_buffer = bytearray()
        self.last_telemetry_at: float | None = None

    async def run(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.app.config.relay.enabled:
            await self.run_relay(reader, writer)
            return
        while True:
            data = await reader.read(4096)
            if not data:
                return
            self.buffer.extend(data)
            for frame in extract_frames(self.buffer):
                await self.handle_frame(frame, writer)

    async def run_relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        original_ip = original_destination_ip(writer)
        upstream = self.choose_upstream(original_ip)
        if not upstream:
            self.app.logger.emit("relay_no_upstream", session=self.session_id, original_ip=original_ip or "")
            return
        upstream_host, upstream_port = upstream
        self.app.logger.emit(
            "relay_connecting",
            session=self.session_id,
            original_ip=original_ip or "",
            upstream_host=upstream_host,
            upstream_port=upstream_port,
        )
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(
                upstream_host,
                upstream_port,
                ssl=ssl._create_unverified_context(),
                server_hostname=original_ip or upstream_host,
            ),
            timeout=self.app.config.relay.connect_timeout_seconds,
        )
        self.app.logger.emit("relay_connected", session=self.session_id, upstream_host=upstream_host, upstream_port=upstream_port)
        await asyncio.gather(
            self.relay_client_to_upstream(reader, upstream_writer),
            self.relay_upstream_to_client(upstream_reader, writer),
        )

    def choose_upstream(self, original_ip: str | None) -> tuple[str, int] | None:
        upstreams = self.app.config.relay.upstreams
        if original_ip and original_ip in upstreams:
            return upstreams[original_ip]
        if len(upstreams) == 1:
            return next(iter(upstreams.values()))
        return None

    async def relay_client_to_upstream(self, reader: asyncio.StreamReader, upstream_writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    return
                self.app.logger.emit("relay_decrypted", session=self.session_id, direction="client_to_upstream", bytes=len(data))
                self.buffer.extend(data)
                for frame in extract_frames(self.buffer):
                    await self.handle_frame(frame, upstream_writer, send_bootstrap=False)
                upstream_writer.write(data)
                await upstream_writer.drain()
        finally:
            upstream_writer.close()

    async def relay_upstream_to_client(self, upstream_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await upstream_reader.read(4096)
                if not data:
                    return
                self.app.logger.emit("relay_decrypted", session=self.session_id, direction="upstream_to_client", bytes=len(data))
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
        if is_registration(frame):
            self.serial = registration_serial(frame)
            self.app.logger.emit("registration", session=self.session_id, serial=self.serial or "")
        if is_product_info(frame):
            info = product_info(frame)
            self.model = info.get("model", "") or self.model
            self.app.logger.emit("product_info", session=self.session_id, serial=self.serial or "", **info)
        response = self.bootstrap.response_for(frame) if send_bootstrap else None
        if response:
            writer.write(response)
            await writer.drain()
            self.app.logger.emit("bootstrap_ack", session=self.session_id, serial=self.serial or "", bytes=len(response), hex=response.hex(" "))
        if is_telemetry(frame):
            telemetry = decode_telemetry(frame.payload, self.serial or "", self.model)
            self.app.publish_telemetry(self.session_id, telemetry)
            self.last_telemetry_at = time.time()


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
