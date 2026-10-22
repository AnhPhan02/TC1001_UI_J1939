from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TextIO

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ..backend.base import CanBackend, CanFrame
from ..backend.mock import MockBackend
from ..backend.tsmaster_native import TSMasterBackend
from ..config import ConnectionConfig
from ..profiles import PROFILE_40_NAME, PROFILE_40_SIGNALS, ProfileSignal
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
    make_set_const,
    make_set_const_engine_speed,
)
from ..protocol.j1939 import parse_j1939_id


class BatchPhase(str, Enum):
    IDLE = "Idle"
    STOPPING = "Stopping"
    CONFIGURING = "Configuring"
    STARTING = "Starting"
    RUNNING = "Running"
    FAILED = "Failed"


@dataclass(slots=True)
class PendingRequest:
    opcode: Opcode
    deadline: float
    request: CommandRequest | None = None
    retries_remaining: int = 1


class ProfileCanWorker(QObject):
    connected = Signal()
    disconnected = Signal()
    connection_failed = Signal(str)
    stm32_ready = Signal()
    command_result = Signal(str, bool)
    readback_changed = Signal(object, str)
    protocol_info_changed = Signal(object)
    runtime_status_changed = Signal(object)
    batch_status_changed = Signal(str, int, int)
    profile_signal_status_changed = Signal(int, str, str)
    rx_rate_snapshot = Signal(object)
    recording_status_changed = Signal(bool, str)
    log_message = Signal(str)

    ACK_TIMEOUT_SECONDS = 1.0
    RX_RATE_WINDOW_SECONDS = 1.0

    def __init__(self) -> None:
        super().__init__()
        self._backend: CanBackend | None = None
        self._config: ConnectionConfig | None = None
        self._sequence = 0
        self._pending: dict[int, PendingRequest] = {}
        self._compatible = False
        self._runtime_flags = RuntimeFlag(0)
        self._timer: QTimer | None = None

        self._batch_phase = BatchPhase.IDLE
        self._batch_signals: tuple[ProfileSignal, ...] = ()
        self._batch_index = 0
        self._failed_request: CommandRequest | None = None
        self._failed_phase: BatchPhase | None = None
        self._failed_index: int | None = None

        self._rx_window_started = time.monotonic()
        self._rx_window_counts: dict[int, int] = {}
        self._record_file: TextIO | None = None
        self._record_path = ""
        self._record_started = 0.0

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
        self._compatible = False
        self._runtime_flags = RuntimeFlag(0)
        self._reset_batch()
        self._reset_rx_window()
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
        self._close_recording()
        if self._backend is not None:
            try:
                self._backend.disconnect()
            except Exception as exc:
                self.log_message.emit(f"DISCONNECT ERROR: {exc}")
        self._backend = None
        self._config = None
        self._pending.clear()
        self._compatible = False
        self._runtime_flags = RuntimeFlag(0)
        self._reset_batch()
        self._reset_rx_window()
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
        if self._control_command_pending():
            self.command_result.emit("A control command is already in progress", False)
            return
        self._reset_batch()
        self._batch_phase = BatchPhase.STOPPING
        self._emit_batch_status()
        request = CommandRequest(Opcode.STOP, self._next_sequence())
        if not self._send_request(request):
            self._fail_batch(request, BatchPhase.STOPPING, None, "transport error")

    @Slot(float)
    def apply_engine_speed(self, rpm: float) -> None:
        """Compatibility helper: send one SET_CONST and never auto-START."""
        if not self._compatible:
            self.command_result.emit("STM32 protocol is not verified", False)
            return
        if self._control_command_pending():
            self.command_result.emit("A control command is already in progress", False)
            return
        try:
            request = make_set_const_engine_speed(self._next_sequence(), rpm)
        except ValueError as exc:
            self.command_result.emit(str(exc), False)
            return
        self._send_request(request)

    @Slot()
    def start_profile_40(self) -> None:
        if not self._compatible:
            self.command_result.emit("STM32 protocol is not verified", False)
            return
        if self._control_command_pending() or self._batch_phase in {
            BatchPhase.STOPPING,
            BatchPhase.CONFIGURING,
            BatchPhase.STARTING,
        }:
            self.command_result.emit("A profile command is already in progress", False)
            return

        self._batch_signals = PROFILE_40_SIGNALS
        self._batch_index = 0
        self._failed_request = None
        self._failed_phase = None
        self._failed_index = None
        for index in range(len(self._batch_signals)):
            self.profile_signal_status_changed.emit(index, "Pending", "")
        self._batch_phase = BatchPhase.STOPPING
        self._emit_batch_status()
        self.log_message.emit(f"Starting {PROFILE_40_NAME}: STOP before configuration")
        request = CommandRequest(Opcode.STOP, self._next_sequence())
        if not self._send_request(request):
            self._fail_batch(request, BatchPhase.STOPPING, None, "transport error")

    @Slot()
    def retry_failed_profile_command(self) -> None:
        if self._batch_phase is not BatchPhase.FAILED or self._failed_request is None:
            self.command_result.emit("There is no failed profile command to retry", False)
            return
        if self._control_command_pending():
            self.command_result.emit("A control command is already in progress", False)
            return
        request = self._failed_request
        phase = self._failed_phase or BatchPhase.FAILED
        index = self._failed_index
        self._batch_phase = phase
        if phase is BatchPhase.CONFIGURING and index is not None:
            self.profile_signal_status_changed.emit(
                index, "Sending", "Retrying same sequence/payload"
            )
        self._emit_batch_status()
        self.log_message.emit(
            f"Retrying exact {request.opcode.name} command: seq=0x{request.sequence:02X}"
        )
        if not self._send_request(request):
            self._fail_batch(request, phase, index, "transport error")

    @Slot(str)
    def start_asc_recording(self, path: str) -> None:
        self._close_recording()
        try:
            record_path = Path(path)
            record_path.parent.mkdir(parents=True, exist_ok=True)
            self._record_file = record_path.open("w", encoding="ascii", newline="\n")
            self._record_path = str(record_path)
            self._record_started = time.monotonic()
            now = datetime.now().astimezone()
            stamp = (
                now.strftime("%a %b %d %H:%M:%S.")
                + f"{now.microsecond // 1000:03d} "
                + now.strftime("%Y")
            )
            self._record_file.write(f"date {stamp}\n")
            self._record_file.write("base hex  timestamps absolute\n")
            self._record_file.write("no internal events logged\n")
            self._record_file.flush()
        except Exception as exc:
            self._record_file = None
            self._record_path = ""
            self.command_result.emit(f"Cannot start ASC recording: {exc}", False)
            self.recording_status_changed.emit(False, "")
            return
        self.recording_status_changed.emit(True, self._record_path)
        self.log_message.emit(f"ASC recording started: {self._record_path}")

    @Slot()
    def stop_asc_recording(self) -> None:
        path = self._record_path
        self._close_recording()
        if path:
            self.log_message.emit(f"ASC recording saved: {path}")

    def _close_recording(self) -> None:
        if self._record_file is not None:
            try:
                self._record_file.flush()
                self._record_file.close()
            except OSError:
                pass
        was_recording = self._record_file is not None
        self._record_file = None
        self._record_path = ""
        if was_recording:
            self.recording_status_changed.emit(False, "")

    def _record_frame(self, frame: CanFrame, direction: str) -> None:
        if self._record_file is None:
            return
        elapsed = time.monotonic() - self._record_started
        channel = frame.channel + 1
        data = " ".join(f"{byte:02X}" for byte in frame.data)
        self._record_file.write(
            f"{elapsed:12.6f} {channel} {frame.can_id:08X}x {direction} d "
            f"{len(frame.data)} {data}\n"
        )

    def _control_command_pending(self) -> bool:
        return any(
            item.opcode in (Opcode.SET_CONST, Opcode.START, Opcode.STOP)
            for item in self._pending.values()
        )

    def _next_sequence(self) -> int:
        self._sequence = (self._sequence + 1) & 0xFF
        return self._sequence

    def _send_request(self, request: CommandRequest) -> bool:
        if self._backend is None or self._config is None:
            self.command_result.emit("Not connected", False)
            return False
        payload = request.encode()
        frame = CanFrame(
            channel=self._config.channel,
            can_id=self._config.request_can_id,
            data=payload,
        )
        try:
            self._backend.send(frame)
            self._record_frame(frame, "Tx")
        except Exception as exc:
            self.command_result.emit(str(exc), False)
            self.log_message.emit(f"TX ERROR: {exc}")
            return False
        self._pending[request.sequence] = PendingRequest(
            opcode=request.opcode,
            deadline=time.monotonic() + self.ACK_TIMEOUT_SECONDS,
            request=request,
        )
        self.log_message.emit(
            f"TX 0x{frame.can_id:08X} [{len(payload)}] {payload.hex(' ').upper()} "
            f"{request.opcode.name} seq=0x{request.sequence:02X}"
        )
        return True

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
        expired = [
            sequence for sequence, item in self._pending.items() if item.deadline <= now
        ]
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
                    self._fail_pending_batch_request(item, "transport error")
                continue
            self._pending.pop(sequence, None)
            if item.opcode in (Opcode.TC1001, Opcode.GET_STATUS):
                self._compatible = False
            message = f"{item.opcode.name} timeout (seq=0x{sequence:02X})"
            if not self._fail_pending_batch_request(item, "timeout"):
                self.command_result.emit(message, False)
        self._emit_rx_snapshot_if_due(now)

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
        self._record_frame(frame, "Tx")
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
        self._record_frame(frame, "Rx")
        identifier = parse_j1939_id(frame.can_id)
        if (
            identifier.pgn == 0xEF00
            and identifier.source_address == self._config.stm32_address
            and identifier.destination_address == self._config.pc_address
        ):
            self.log_message.emit(
                f"RX 0x{frame.can_id:08X} [{len(frame.data)}] "
                f"{frame.data.hex(' ').upper()}"
            )
            self._handle_response(frame.data)
            return
        self._rx_window_counts[frame.can_id] = self._rx_window_counts.get(frame.can_id, 0) + 1

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
        if not response.succeeded:
            if response.request_opcode in (Opcode.TC1001, Opcode.GET_STATUS):
                self._compatible = False
            if self._batch_phase in {
                BatchPhase.STOPPING,
                BatchPhase.CONFIGURING,
                BatchPhase.STARTING,
            } and response.request_opcode in (Opcode.STOP, Opcode.SET_CONST, Opcode.START):
                self._fail_batch(
                    pending.request,
                    self._batch_phase,
                    self._batch_index
                    if self._batch_phase is BatchPhase.CONFIGURING
                    else None,
                    f"status={response.status}, detail={response.detail}",
                )
            else:
                self.command_result.emit(
                    f"{response.request_opcode.name} rejected: "
                    f"status={response.status}, detail={response.detail}",
                    False,
                )
            return

        self.command_result.emit(f"{response.request_opcode.name} applied", True)
        if response.request_opcode is Opcode.TC1001:
            self.log_message.emit("TC1001 accepted; querying protocol/status...")
            self.get_status()
        elif response.request_opcode is Opcode.GET_STATUS:
            self._handle_status_response(response.detail, response.value)
        elif response.request_opcode is Opcode.STOP:
            self._handle_stop_ack()
        elif response.request_opcode is Opcode.SET_CONST:
            self._handle_set_const_ack()
        elif response.request_opcode is Opcode.START:
            self._handle_start_ack()

    def _handle_stop_ack(self) -> None:
        self._runtime_flags &= ~RuntimeFlag.RUNNING
        self.runtime_status_changed.emit(self._runtime_flags)
        self.readback_changed.emit(None, "Stopped")
        if self._batch_phase is BatchPhase.STOPPING and self._batch_signals:
            self._batch_phase = BatchPhase.CONFIGURING
            self._emit_batch_status()
            self._send_next_profile_signal()
        else:
            self._batch_phase = BatchPhase.IDLE
            self._emit_batch_status()

    def _send_next_profile_signal(self) -> None:
        if self._batch_index >= len(self._batch_signals):
            self._batch_phase = BatchPhase.STARTING
            self._emit_batch_status()
            request = CommandRequest(Opcode.START, self._next_sequence())
            if not self._send_request(request):
                self._fail_batch(request, BatchPhase.STARTING, None, "transport error")
            return

        signal = self._batch_signals[self._batch_index]
        try:
            request = make_set_const(
                self._next_sequence(), signal.signal_key, signal.raw_value
            )
        except ValueError as exc:
            self._fail_batch(None, BatchPhase.CONFIGURING, self._batch_index, str(exc))
            return
        self.profile_signal_status_changed.emit(self._batch_index, "Sending", "")
        if not self._send_request(request):
            self._fail_batch(
                request, BatchPhase.CONFIGURING, self._batch_index, "transport error"
            )

    def _handle_set_const_ack(self) -> None:
        # A SET_CONST ACK never starts transmission by itself. Only this batch
        # state machine reaches START, after all 40 ACKs have succeeded.
        if self._batch_phase is not BatchPhase.CONFIGURING or not self._batch_signals:
            return
        self.profile_signal_status_changed.emit(self._batch_index, "Configured", "")
        self._batch_index += 1
        self._emit_batch_status()
        self._send_next_profile_signal()

    def _handle_start_ack(self) -> None:
        self._runtime_flags |= RuntimeFlag.RUNNING
        self.runtime_status_changed.emit(self._runtime_flags)
        self.readback_changed.emit(None, "Waiting for periodic PGNs")
        if self._batch_phase is BatchPhase.STARTING:
            self._batch_phase = BatchPhase.RUNNING
            self._emit_batch_status()
            self.log_message.emit(
                f"{PROFILE_40_NAME} is running; monitoring 11 PGNs in 1-second windows"
            )

    def _fail_pending_batch_request(self, pending: PendingRequest, reason: str) -> bool:
        if (
            pending.request is None
            or self._batch_phase
            not in {BatchPhase.STOPPING, BatchPhase.CONFIGURING, BatchPhase.STARTING}
            or pending.opcode not in (Opcode.STOP, Opcode.SET_CONST, Opcode.START)
        ):
            return False
        self._fail_batch(
            pending.request,
            self._batch_phase,
            self._batch_index if self._batch_phase is BatchPhase.CONFIGURING else None,
            reason,
        )
        return True

    def _fail_batch(
        self,
        request: CommandRequest | None,
        phase: BatchPhase,
        signal_index: int | None,
        reason: str,
    ) -> None:
        self._failed_request = request
        self._failed_phase = phase
        self._failed_index = signal_index
        self._batch_phase = BatchPhase.FAILED
        if signal_index is not None and signal_index < len(self._batch_signals):
            signal = self._batch_signals[signal_index]
            signal_state = "Timeout" if "timeout" in reason.lower() else "Error"
            self.profile_signal_status_changed.emit(signal_index, signal_state, reason)
            message = (
                f"{PROFILE_40_NAME} failed at {signal_index + 1}/40: "
                f"key={signal.signal_key}, SPN={signal.spn}, {reason}"
            )
        else:
            opcode = request.opcode.name if request is not None else phase.value
            message = f"{PROFILE_40_NAME} failed during {opcode}: {reason}"
        self._emit_batch_status()
        self.command_result.emit(message, False)
        self.log_message.emit(message + "; START was not sent")

    def _reset_batch(self) -> None:
        self._batch_phase = BatchPhase.IDLE
        self._batch_signals = ()
        self._batch_index = 0
        self._failed_request = None
        self._failed_phase = None
        self._failed_index = None
        self._emit_batch_status()

    def _emit_batch_status(self) -> None:
        total = len(self._batch_signals) or len(PROFILE_40_SIGNALS)
        current = min(self._batch_index, total)
        self.batch_status_changed.emit(self._batch_phase.value, current, total)

    def _reset_rx_window(self) -> None:
        self._rx_window_started = time.monotonic()
        self._rx_window_counts.clear()

    def _emit_rx_snapshot_if_due(self, now: float) -> None:
        elapsed = now - self._rx_window_started
        if elapsed < self.RX_RATE_WINDOW_SECONDS:
            return
        frames = []
        for can_id, count in sorted(self._rx_window_counts.items()):
            identifier = parse_j1939_id(can_id)
            frames.append(
                {
                    "can_id": can_id,
                    "pgn": identifier.pgn,
                    "destination_address": identifier.destination_address,
                    "source_address": identifier.source_address,
                    "count": count,
                    "fps": count / elapsed,
                }
            )
        total_count = sum(self._rx_window_counts.values())
        snapshot = {
            "window_seconds": elapsed,
            "frames": frames,
            "total_count": total_count,
            "total_fps": total_count / elapsed,
        }
        self.rx_rate_snapshot.emit(snapshot)
        if frames:
            self.log_message.emit(
                f"RX 1 s summary: {snapshot['total_fps']:.1f} frame/s "
                f"across {len(frames)} CAN IDs"
            )
        if self._record_file is not None:
            self._record_file.flush()
        self._rx_window_started = now
        self._rx_window_counts.clear()

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
                "Incompatible STM32: " + "; ".join(problems), False
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
