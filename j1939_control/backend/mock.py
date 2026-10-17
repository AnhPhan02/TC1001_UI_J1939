from __future__ import annotations

import struct
import time
from collections import deque

from ..config import ConnectionConfig
from ..protocol.commands import ENGINE_SPEED_SIGNAL_KEY, Opcode
from ..protocol.j1939 import EEC1_PGN, build_j1939_id
from .base import CanFrame


class MockBackend:
    """Deterministic STM32 simulator for development without CAN hardware."""

    def __init__(self) -> None:
        self._config: ConnectionConfig | None = None
        self._rx: deque[CanFrame] = deque()

    def connect(self, config: ConnectionConfig) -> None:
        self._config = config

    def disconnect(self) -> None:
        self._config = None
        self._rx.clear()

    def send(self, frame: CanFrame) -> None:
        if self._config is None:
            raise RuntimeError("Mock backend is not connected")
        if len(frame.data) != 8:
            raise ValueError("Command frame must have DLC 8")
        opcode_value, sequence, argument16, argument32 = struct.unpack("<BBHI", frame.data)
        try:
            opcode = Opcode(opcode_value)
        except ValueError:
            self._queue_ack(opcode_value, sequence, status=1, detail=1, value=0)
            return

        status = 0
        detail = 0
        if opcode is Opcode.SET_CONST and argument16 != ENGINE_SPEED_SIGNAL_KEY:
            status = 2
            detail = 1
        self._queue_ack(opcode_value, sequence, status, detail, argument32)

        if opcode is Opcode.SET_CONST and status == 0:
            payload = bytearray(b"\xFF" * 8)
            payload[3:5] = int(argument32).to_bytes(2, "little", signed=False)
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

    def _queue_ack(
        self,
        opcode: int,
        sequence: int,
        status: int,
        detail: int,
        value: int,
    ) -> None:
        assert self._config is not None
        payload = struct.pack("<BBBBI", opcode | 0x80, sequence, status, detail, value)
        self._rx.append(
            CanFrame(
                channel=self._config.channel,
                can_id=self._config.response_can_id,
                data=payload,
                timestamp_us=time.monotonic_ns() // 1_000,
            )
        )

    def receive(self) -> CanFrame | None:
        return self._rx.popleft() if self._rx else None
