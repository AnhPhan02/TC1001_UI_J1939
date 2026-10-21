from __future__ import annotations

import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ..backend.base import CanBackend, CanFrame
from ..backend.mock import MockBackend
from ..backend.tsmaster_native import TSMasterBackend
from ..config import ConnectionConfig
from ..protocol.commands import (
    PROTOCOL_SCHEMA_VERSION,
    PROTOCOL_VERSION_MAJOR,
    REQUIRED_CAPABILITIES,
    Capability,
    CommandRequest,
    Opcode,
    ProtocolInfo,
    RuntimeFlag,
    capability_names,
    decode_protocol_info,
    decode_response,
    make_set_const_engine_speed,
)
from ..protocol.j1939 import EEC1_PGN, decode_eec1_engine_speed, parse_j1939_id


@dataclass(slots=True)
class PendingRequest:
    opcode: Opcode
    deadline: float
    request: CommandRequest | None = None
    retries_remaining: int = 1


class CanWorker(QObject):
    connected = Signal()
    disconnected = Signal()
    connection_failed = Signal(str)
    stm32_ready = Signal()
    command_result = Signal(str, bool)
    readback_changed = Signal(object, str)
    protocol_info_changed = Signal(object)
    runtime_status_changed = Signal(object)
    log_message = Signal(str)

    ACK_TIMEOUT_SECONDS = 1.0
    READBACK_STALE_SECONDS = 1.0

    def __init__(self) -> None:
        super().__init__()
        self._backend: CanBackend | None = None
        self._config: ConnectionConfig | None = None
        self._sequence = 0
        self._pending: dict[int, PendingRequest] = {}
        self._last_readback_at: float | None = None
        self._stale_emitted = False
        self._compatible = False
        self._runtime_flags = RuntimeFlag(0)
        self._timer: QTimer | None = None

    @Slot()
    def start(self) -> None:
        self._timer = QTimer(self)
        self._timer.setInterval(10)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    @Slot(str, object)
    def connect_backend(self, backend_kind: str, config: ConnectionConfig) -> None:
        self._disconnect_impl(emit_signal=False)
        try:
            backend: CanBackend
            if backend_kind == "mock":
                backend = MockBackend()
            elif backend_kind == "tsmaster":
                backend = TSMasterBackend()
            else:
                raise ValueError(f"Unknown backend: {backend_kind}")
            backend.connect(config)
        except Exception as exc:
            self.connection_failed.emit(str(exc))
            self.log_message.emit(f"CONNECT ERROR: {exc}")
            return
        self._backend = backend
        self._config = config
        self._pending.clear()
        self._last_readback_at = None
        self._stale_emitted = False
        self._compatible = False
        self._runtime_flags = RuntimeFlag(0)
        self.connected.emit()
        mapping = getattr(backend, "mapping_description", None)
        self.log_message.emit(
            f"CAN connected: backend={backend_kind}, channel={config.channel}, "
            f"bitrate={config.bitrate_kbps} kbit/s"
            + (f", mapping={mapping}" if mapping else "")
        )
        self.log_message.emit("Verifying STM32 automatically with TC1001...")
        self.send_tc1001()

    @Slot()
    def disconnect_backend(self) -> None:
        self._disconnect_impl(emit_signal=True)

    def _disconnect_impl(self, *, emit_signal: bool) -> None:
        if self._backend is not None:
            try:
                self._backend.disconnect()
            except Exception as exc:
                self.log_message.emit(f"DISCONNECT ERROR: {exc}")
        self._backend = None
        self._config = None
        self._pending.clear()
        self._last_readback_at = None
        self._stale_emitted = False
        self._compatible = False
        self._runtime_flags = RuntimeFlag(0)
        if emit_signal:
            self.disconnected.emit()

    @Slot()
    def send_tc1001(self) -> None:
        if any(item.opcode is Opcode.TC1001 for item in self._pending.values()):
            self.command_result.emit("TC1001 verification is already in progress", False)
            return
        self._compatible = False
        self._send_request(CommandRequest(Opcode.TC1001, self._next_sequence()))

    @Slot()
    def get_status(self) -> None:
        if any(item.opcode is Opcode.GET_STATUS for item in self._pending.values()):
            self.command_result.emit("GET_STATUS is already in progress", False)
            return
        self._send_request(CommandRequest(Opcode.GET_STATUS, self._next_sequence()))

    @Slot()
    def stop_transmission(self) -> None:
        if not self._compatible:
            self.command_result.emit("STM32 protocol is not verified", False)
            return
        if any(
            item.opcode in (Opcode.SET_CONST, Opcode.START, Opcode.STOP)
            for item in self._pending.values()
        ):
            self.command_result.emit("A control command is already in progress", False)
            return
        self._send_request(CommandRequest(Opcode.STOP, self._next_sequence()))

    @Slot(float)
    def apply_engine_speed(self, rpm: float) -> None:
        if not self._compatible:
            self.command_result.emit("STM32 protocol is not verified", False)
            return
        if any(
            item.opcode in (Opcode.SET_CONST, Opcode.START, Opcode.STOP)
            for item in self._pending.values()
        ):
            self.command_result.emit("SET_CONST/START sequence is already in progress", False)
            return
        try:
            request = make_set_const_engine_speed(self._next_sequence(), rpm)
        except ValueError as exc:
            self.command_result.emit(str(exc), False)
            return
        self._send_request(request)

    def _next_sequence(self) -> int:
        self._sequence = (self._sequence + 1) & 0xFF
        return self._sequence

    def _send_request(self, request: CommandRequest) -> None:
        if self._backend is None or self._config is None:
            self.command_result.emit("Not connected", False)
            return
        payload = request.encode()
        frame = CanFrame(
            channel=self._config.channel,
            can_id=self._config.request_can_id,
            data=payload,
        )
        try:
            self._backend.send(frame)
        except Exception as exc:
            self.command_result.emit(str(exc), False)
            self.log_message.emit(f"TX ERROR: {exc}")
            return
        self._pending[request.sequence] = PendingRequest(
            opcode=request.opcode,
            deadline=time.monotonic() + self.ACK_TIMEOUT_SECONDS,
            request=request,
        )
        self.log_message.emit(
            f"TX 0x{frame.can_id:08X} [{len(payload)}] {payload.hex(' ').upper()} "
            f"{request.opcode.name} seq=0x{request.sequence:02X}"
        )

    @Slot()
    def _poll(self) -> None:
        if self._backend is None:
            return
        try:
            for _ in range(100):
                frame = self._backend.receive()
                if frame is None:
                    break
                self._handle_frame(frame)
        except Exception as exc:
            self.log_message.emit(f"RX ERROR: {exc}")

        now = time.monotonic()
        expired = [sequence for sequence, item in self._pending.items() if item.deadline <= now]
        for sequence in expired:
            item = self._pending.get(sequence)
            if item is None:
                continue
            if item.retries_remaining > 0 and item.request is not None:
                try:
                    self._retry_request(item)
                except Exception as exc:
                    self._pending.pop(sequence, None)
                    if item.opcode in (Opcode.TC1001, Opcode.GET_STATUS):
                        self._compatible = False
                    self.command_result.emit(str(exc), False)
                    self.log_message.emit(f"TX RETRY ERROR: {exc}")
                continue
            self._pending.pop(sequence, None)
            if item.opcode in (Opcode.TC1001, Opcode.GET_STATUS):
                self._compatible = False
            message = f"{item.opcode.name} timeout (seq=0x{sequence:02X})"
            self.command_result.emit(message, False)

        if (
            self._last_readback_at is not None
            and not self._stale_emitted
            and now - self._last_readback_at >= self.READBACK_STALE_SECONDS
        ):
            self._stale_emitted = True
            self.readback_changed.emit(None, "Stale")

    def _retry_request(self, pending: PendingRequest) -> None:
        if self._backend is None or self._config is None or pending.request is None:
            raise RuntimeError("Cannot retry while disconnected")
        payload = pending.request.encode()
        frame = CanFrame(
            channel=self._config.channel,
            can_id=self._config.request_can_id,
            data=payload,
        )
        self._backend.send(frame)
        pending.retries_remaining -= 1
        pending.deadline = time.monotonic() + self.ACK_TIMEOUT_SECONDS
        self.log_message.emit(
            f"TX RETRY 0x{frame.can_id:08X} [{len(payload)}] "
            f"{payload.hex(' ').upper()} {pending.opcode.name} "
            f"seq=0x{pending.request.sequence:02X}"
        )

    def _handle_frame(self, frame: CanFrame) -> None:
        if self._config is None or frame.is_tx or not frame.is_extended:
            return
        self.log_message.emit(
            f"RX 0x{frame.can_id:08X} [{len(frame.data)}] {frame.data.hex(' ').upper()}"
        )
        identifier = parse_j1939_id(frame.can_id)

        if (
            identifier.pgn == 0xEF00
            and identifier.source_address == self._config.stm32_address
            and identifier.destination_address == self._config.pc_address
        ):
            self._handle_response(frame.data)
            return

        if identifier.pgn == EEC1_PGN and identifier.source_address == self._config.stm32_address:
            try:
                rpm = decode_eec1_engine_speed(frame.data)
            except ValueError as exc:
                self.log_message.emit(f"EEC1 DECODE ERROR: {exc}")
                return
            self._last_readback_at = time.monotonic()
            self._stale_emitted = False
            if rpm is None:
                self.readback_changed.emit(None, "NA/error")
            else:
                self.readback_changed.emit(rpm, "Fresh")

    def _handle_response(self, payload: bytes) -> None:
        try:
            response = decode_response(payload)
        except ValueError as exc:
            self.log_message.emit(f"ACK DECODE ERROR: {exc}")
            return
        pending = self._pending.get(response.sequence)
        if pending is None or pending.opcode is not response.request_opcode:
            self.log_message.emit(
                f"Ignoring unmatched ACK {response.request_opcode.name} "
                f"seq=0x{response.sequence:02X}"
            )
            return
        self._pending.pop(response.sequence)
        if response.succeeded:
            message = f"{response.request_opcode.name} applied"
            self.command_result.emit(message, True)
            if response.request_opcode is Opcode.TC1001:
                self.log_message.emit("TC1001 accepted; querying protocol/status...")
                self.get_status()
            elif response.request_opcode is Opcode.GET_STATUS:
                self._handle_status_response(response.detail, response.value)
            elif response.request_opcode is Opcode.SET_CONST:
                self.log_message.emit("SET_CONST accepted; sending START...")
                self._send_request(CommandRequest(Opcode.START, self._next_sequence()))
            elif response.request_opcode is Opcode.START:
                self._runtime_flags |= (
                    RuntimeFlag.RUNNING | RuntimeFlag.ENGINE_SPEED_CONFIGURED
                )
                self.runtime_status_changed.emit(self._runtime_flags)
                self.readback_changed.emit(None, "Waiting for EEC1")
            elif response.request_opcode is Opcode.STOP:
                self._last_readback_at = None
                self._stale_emitted = False
                self._runtime_flags &= ~RuntimeFlag.RUNNING
                self.runtime_status_changed.emit(self._runtime_flags)
                self.readback_changed.emit(None, "Stopped")
        else:
            if response.request_opcode in (Opcode.TC1001, Opcode.GET_STATUS):
                self._compatible = False
            message = (
                f"{response.request_opcode.name} rejected: "
                f"status={response.status}, detail={response.detail}"
            )
            self.command_result.emit(message, False)

    def _handle_status_response(self, detail: int, value: int) -> None:
        info = decode_protocol_info(value)
        runtime = RuntimeFlag(detail)
        self._runtime_flags = runtime
        self.protocol_info_changed.emit(info)
        self.runtime_status_changed.emit(runtime)

        problems: list[str] = []
        if info.major != PROTOCOL_VERSION_MAJOR:
            problems.append(
                f"protocol major {info.major} (expected {PROTOCOL_VERSION_MAJOR})"
            )
        if info.schema != PROTOCOL_SCHEMA_VERSION:
            problems.append(
                f"schema {info.schema} (expected {PROTOCOL_SCHEMA_VERSION})"
            )
        missing = Capability(int(REQUIRED_CAPABILITIES) & ~int(info.capabilities))
        if missing:
            problems.append("missing " + ", ".join(capability_names(missing)))
        if problems:
            self._compatible = False
            self.command_result.emit(
                "Incompatible STM32: " + "; ".join(problems),
                False,
            )
            return

        self._compatible = True
        self.stm32_ready.emit()
        self.log_message.emit(
            f"STM32 protocol v{info.version}, schema {info.schema}, "
            f"capabilities={','.join(capability_names(info.capabilities))}"
        )

    @Slot()
    def shutdown(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self._disconnect_impl(emit_signal=False)
