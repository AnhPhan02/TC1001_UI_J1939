from __future__ import annotations

from dataclasses import dataclass

EEC1_PGN = 0xF004
ENGINE_SPEED_MAX_RAW = 0xFAFF


@dataclass(frozen=True, slots=True)
class J1939Identifier:
    priority: int
    pgn: int
    source_address: int
    destination_address: int | None


def build_j1939_id(
    *,
    priority: int,
    pgn: int,
    source_address: int,
    destination_address: int | None = None,
) -> int:
    if not 0 <= priority <= 7:
        raise ValueError("Priority must be in range 0..7")
    if not 0 <= pgn <= 0x3FFFF:
        raise ValueError("PGN must fit in 18 bits")
    if not 0 <= source_address <= 0xFF:
        raise ValueError("Source address must fit in one byte")

    data_page = (pgn >> 16) & 0x01
    pdu_format = (pgn >> 8) & 0xFF
    if pdu_format < 0xF0:
        if destination_address is None:
            raise ValueError("A destination address is required for a PDU1 PGN")
        pdu_specific = destination_address
    else:
        pdu_specific = pgn & 0xFF

    return (
        (priority << 26)
        | (data_page << 24)
        | (pdu_format << 16)
        | (pdu_specific << 8)
        | source_address
    )


def parse_j1939_id(can_id: int) -> J1939Identifier:
    if not 0 <= can_id <= 0x1FFFFFFF:
        raise ValueError("J1939 CAN ID must be a 29-bit identifier")
    priority = (can_id >> 26) & 0x07
    data_page = (can_id >> 24) & 0x01
    pdu_format = (can_id >> 16) & 0xFF
    pdu_specific = (can_id >> 8) & 0xFF
    source_address = can_id & 0xFF
    if pdu_format < 0xF0:
        pgn = (data_page << 16) | (pdu_format << 8)
        destination = pdu_specific
    else:
        pgn = (data_page << 16) | (pdu_format << 8) | pdu_specific
        destination = None
    return J1939Identifier(priority, pgn, source_address, destination)


def decode_eec1_engine_speed(payload: bytes) -> float | None:
    if len(payload) < 5:
        raise ValueError("EEC1 payload must contain at least 5 bytes")
    raw = int.from_bytes(payload[3:5], byteorder="little", signed=False)
    if raw > ENGINE_SPEED_MAX_RAW:
        return None
    return raw * 0.125
