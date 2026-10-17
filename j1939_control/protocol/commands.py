from __future__ import annotations

import struct
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import IntEnum


class Opcode(IntEnum):
    TC1001 = 0x01
    STOP = 0x02
    START = 0x03
    GET_STATUS = 0x04
    KEEPALIVE = 0x05
    SET_CONST = 0x10
    SET_PGN_PERIOD = 0x11
    TRIGGER_PGN = 0x12


ENGINE_SPEED_SIGNAL_KEY = 103
ENGINE_SPEED_RESOLUTION = 0.125
ENGINE_SPEED_MAX_RAW = 0xFAFF


@dataclass(frozen=True, slots=True)
class CommandRequest:
    opcode: Opcode
    sequence: int
    argument16: int = 0
    argument32: int = 0

    def encode(self) -> bytes:
        if not 0 <= self.sequence <= 0xFF:
            raise ValueError("Sequence must fit in one byte")
        if not 0 <= self.argument16 <= 0xFFFF:
            raise ValueError("argument16 must fit in two bytes")
        if not 0 <= self.argument32 <= 0xFFFFFFFF:
            raise ValueError("argument32 must fit in four bytes")
        return struct.pack(
            "<BBHI",
            int(self.opcode),
            self.sequence,
            self.argument16,
            self.argument32,
        )


@dataclass(frozen=True, slots=True)
class CommandResponse:
    request_opcode: Opcode
    sequence: int
    status: int
    detail: int
    value: int

    @property
    def succeeded(self) -> bool:
        return self.status == 0


def physical_engine_speed_to_raw(rpm: float) -> int:
    if rpm < 0:
        raise ValueError("Engine Speed cannot be negative")
    raw = int(
        (Decimal(str(rpm)) / Decimal(str(ENGINE_SPEED_RESOLUTION))).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    if raw > ENGINE_SPEED_MAX_RAW:
        raise ValueError("Engine Speed is outside the valid J1939 raw range")
    return raw


def make_set_const_engine_speed(sequence: int, rpm: float) -> CommandRequest:
    return CommandRequest(
        opcode=Opcode.SET_CONST,
        sequence=sequence,
        argument16=ENGINE_SPEED_SIGNAL_KEY,
        argument32=physical_engine_speed_to_raw(rpm),
    )


def decode_response(payload: bytes) -> CommandResponse:
    if len(payload) != 8:
        raise ValueError("Application response must contain exactly 8 bytes")
    encoded_opcode, sequence, status, detail, value = struct.unpack("<BBBBI", payload)
    if not encoded_opcode & 0x80:
        raise ValueError("Response opcode does not have the response bit set")
    try:
        request_opcode = Opcode(encoded_opcode & 0x7F)
    except ValueError as exc:
        raise ValueError(f"Unknown response opcode 0x{encoded_opcode:02X}") from exc
    return CommandResponse(request_opcode, sequence, status, detail, value)
