from __future__ import annotations

import unittest

from j1939_control.config import ConnectionConfig
from j1939_control.protocol.commands import (
    CommandRequest,
    Opcode,
    decode_response,
    make_set_const_engine_speed,
    physical_engine_speed_to_raw,
)
from j1939_control.protocol.j1939 import (
    EEC1_PGN,
    build_j1939_id,
    decode_eec1_engine_speed,
    parse_j1939_id,
)


class CommandTests(unittest.TestCase):
    def test_tc1001_vector(self) -> None:
        self.assertEqual(
            bytes.fromhex("01 2A 00 00 00 00 00 00"),
            CommandRequest(Opcode.TC1001, 0x2A).encode(),
        )

    def test_1500_rpm_vector(self) -> None:
        request = make_set_const_engine_speed(0x2A, 1500.0)
        self.assertEqual(12000, request.argument32)
        self.assertEqual(
            bytes.fromhex("10 2A 67 00 E0 2E 00 00"),
            request.encode(),
        )

    def test_start_vector(self) -> None:
        self.assertEqual(
            bytes.fromhex("03 2B 00 00 00 00 00 00"),
            CommandRequest(Opcode.START, 0x2B).encode(),
        )

    def test_resolution_rounding(self) -> None:
        self.assertEqual(1, physical_engine_speed_to_raw(0.125))
        self.assertEqual(1, physical_engine_speed_to_raw(0.0625))

    def test_ack_vector(self) -> None:
        response = decode_response(bytes.fromhex("90 2A 00 00 E0 2E 00 00"))
        self.assertEqual(Opcode.SET_CONST, response.request_opcode)
        self.assertEqual(0x2A, response.sequence)
        self.assertTrue(response.succeeded)
        self.assertEqual(12000, response.value)


class J1939Tests(unittest.TestCase):
    def test_expected_control_ids(self) -> None:
        config = ConnectionConfig()
        self.assertEqual(0x18EF80F9, config.request_can_id)
        self.assertEqual(0x18EFF980, config.response_can_id)

    def test_parse_eec1_id(self) -> None:
        identifier = parse_j1939_id(0x0CF00480)
        self.assertEqual(EEC1_PGN, identifier.pgn)
        self.assertEqual(0x80, identifier.source_address)
        self.assertIsNone(identifier.destination_address)

    def test_build_eec1_id(self) -> None:
        can_id = build_j1939_id(priority=3, pgn=EEC1_PGN, source_address=0x80)
        self.assertEqual(0x0CF00480, can_id)

    def test_decode_eec1_engine_speed(self) -> None:
        payload = bytes.fromhex("FF FF FF E0 2E FF FF FF")
        self.assertEqual(1500.0, decode_eec1_engine_speed(payload))

    def test_special_raw_is_not_a_measurement(self) -> None:
        payload = bytes.fromhex("FF FF FF FF FF FF FF FF")
        self.assertIsNone(decode_eec1_engine_speed(payload))


if __name__ == "__main__":
    unittest.main()
