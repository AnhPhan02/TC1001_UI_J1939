from __future__ import annotations

import struct
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

from ..config import ConnectionConfig
from ..profiles import PROFILE_40_PGNS, PROFILE_40_SIGNALS
from ..protocol.commands import (
    ENGINE_SPEED_SIGNAL_KEY,
    PROTOCOL_SCHEMA_VERSION,
    PROTOCOL_VERSION_MAJOR,
    PROTOCOL_VERSION_MINOR,
    REQUIRED_CAPABILITIES,
    Opcode,
    ProtocolInfo,
    RuntimeFlag,
)
from ..protocol.j1939 import EEC1_PGN, build_j1939_id
from .base import CanFrame


@dataclass(frozen=True, slots=True)
class _CachedResponse:
    request: bytes
    response: bytes
    expires_at: float


class MockBackend:
    """Deterministic STM32 simulator for development without CAN hardware."""

    DEDUP_TTL_SECONDS = 5.0
    DEDUP_MAX_ENTRIES = 64

    def __init__(self) -> None:
        self._config: ConnectionConfig | None = None
        self._rx: deque[CanFrame] = deque()
        self._signal_values: dict[int, int] = {}
        self._running = False
        self._next_due: dict[str, float] = {}
        self._dedup: OrderedDict[tuple[int, int, int], _CachedResponse] = OrderedDict()
        self.applied_command_count: dict[Opcode, int] = {}

    def connect(self, config: ConnectionConfig) -> None:
        self._config = config

    def disconnect(self) -> None:
        self._config = None
        self._rx.clear()
        self._signal_values.clear()
        self._running = False
        self._next_due.clear()
        self._dedup.clear()

    def send(self, frame: CanFrame) -> None:
        if self._config is None:
            raise RuntimeError("Mock backend is not connected")
        if len(frame.data) != 8:
            raise ValueError("Command frame must have DLC 8")
        opcode_value, sequence, argument16, argument32 = struct.unpack("<BBHI", frame.data)
        now = time.monotonic()
        self._expire_dedup(now)
        source_address = frame.can_id & 0xFF
        cache_key = (source_address, opcode_value, sequence)
        cached = self._dedup.get(cache_key)
        if cached is not None:
            if cached.request == frame.data:
                self._dedup.move_to_end(cache_key)
                self._queue_payload(cached.response)
            else:
                self._queue_ack(opcode_value, sequence, status=4, detail=1, value=0)
            return

        try:
            opcode = Opcode(opcode_value)
        except ValueError:
            response = self._make_ack(opcode_value, sequence, status=1, detail=1, value=0)
            self._remember_response(cache_key, frame.data, response, now)
            self._queue_payload(response)
            return

        status = 0
        detail = 0
        value = argument32
        supported_keys = {signal.signal_key for signal in PROFILE_40_SIGNALS}
        supported_keys.add(ENGINE_SPEED_SIGNAL_KEY)
        if opcode is Opcode.SET_CONST and argument16 not in supported_keys:
            status = 2
            detail = 1
        elif opcode is Opcode.SET_CONST:
            if argument16 not in self._signal_values and len(self._signal_values) >= 40:
                status = 5
                detail = 40
            else:
                self._signal_values[argument16] = argument32
        elif opcode is Opcode.START and not self._signal_values:
            status = 3
            detail = 1
        elif opcode is Opcode.START:
            self._running = True
            profile_keys = {signal.signal_key for signal in PROFILE_40_SIGNALS}
            if profile_keys.issubset(self._signal_values):
                started = time.monotonic()
                self._next_due = {
                    pgn.name: started + (pgn.period_ms / 1000.0)
                    for pgn in PROFILE_40_PGNS
                }
        elif opcode is Opcode.STOP:
            self._running = False
            self._next_due.clear()
        elif opcode is Opcode.GET_STATUS:
            runtime = RuntimeFlag(0)
            if self._running:
                runtime |= RuntimeFlag.RUNNING
            if self._signal_values:
                runtime |= RuntimeFlag.ENGINE_SPEED_CONFIGURED
            detail = int(runtime)
            value = ProtocolInfo(
                major=PROTOCOL_VERSION_MAJOR,
                minor=PROTOCOL_VERSION_MINOR,
                schema=PROTOCOL_SCHEMA_VERSION,
                capabilities=REQUIRED_CAPABILITIES,
            ).encode()

        response = self._make_ack(opcode_value, sequence, status, detail, value)
        self._remember_response(cache_key, frame.data, response, now)
        self._queue_payload(response)
        if status == 0:
            self.applied_command_count[opcode] = self.applied_command_count.get(opcode, 0) + 1

        if (
            opcode is Opcode.START
            and status == 0
            and ENGINE_SPEED_SIGNAL_KEY in self._signal_values
        ):
            payload = bytearray(b"\xFF" * 8)
            payload[3:5] = self._signal_values[ENGINE_SPEED_SIGNAL_KEY].to_bytes(
                2, "little", signed=False
            )
            eec1_id = build_j1939_id(
                priority=3,
                pgn=EEC1_PGN,
                source_address=self._config.stm32_address,
            )
            self._rx.append(
                CanFrame(
                    channel=self._config.channel,
                    can_id=eec1_id,
                    data=bytes(payload),
                    timestamp_us=time.monotonic_ns() // 1_000,
                )
            )

    @staticmethod
    def _make_ack(
        opcode: int,
        sequence: int,
        status: int,
        detail: int,
        value: int,
    ) -> bytes:
        return struct.pack("<BBBBI", opcode | 0x80, sequence, status, detail, value)

    def _queue_ack(
        self,
        opcode: int,
        sequence: int,
        status: int,
        detail: int,
        value: int,
    ) -> None:
        self._queue_payload(self._make_ack(opcode, sequence, status, detail, value))

    def _queue_payload(self, payload: bytes) -> None:
        assert self._config is not None
        self._rx.append(
            CanFrame(
                channel=self._config.channel,
                can_id=self._config.response_can_id,
                data=payload,
                timestamp_us=time.monotonic_ns() // 1_000,
            )
        )

    def _remember_response(
        self,
        key: tuple[int, int, int],
        request: bytes,
        response: bytes,
        now: float,
    ) -> None:
        self._dedup[key] = _CachedResponse(
            request=request,
            response=response,
            expires_at=now + self.DEDUP_TTL_SECONDS,
        )
        self._dedup.move_to_end(key)
        while len(self._dedup) > self.DEDUP_MAX_ENTRIES:
            self._dedup.popitem(last=False)

    def _expire_dedup(self, now: float) -> None:
        expired = [key for key, item in self._dedup.items() if item.expires_at <= now]
        for key in expired:
            self._dedup.pop(key, None)

    def receive(self) -> CanFrame | None:
        self._queue_due_profile_frames()
        return self._rx.popleft() if self._rx else None

    def _queue_due_profile_frames(self) -> None:
        if not self._running or self._config is None or not self._next_due:
            return
        now = time.monotonic()
        for pgn in PROFILE_40_PGNS:
            due = self._next_due.get(pgn.name)
            if due is None or now < due:
                continue
            can_id = build_j1939_id(
                priority=pgn.priority,
                pgn=pgn.receive_pgn,
                source_address=self._config.stm32_address,
                destination_address=pgn.destination_address,
            )
            while due <= now:
                self._rx.append(
                    CanFrame(
                        channel=self._config.channel,
                        can_id=can_id,
                        data=b"\x00" * 8,
                        timestamp_us=time.monotonic_ns() // 1_000,
                    )
                )
                due += pgn.period_ms / 1000.0
            self._next_due[pgn.name] = due
