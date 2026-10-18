from __future__ import annotations

import unittest

from j1939_control.backend.base import CanFrame
from j1939_control.config import ConnectionConfig
from j1939_control.backend.tsmaster_com import (
    _LegacyTSMasterComBackend as TSMasterComBackend,
    _TSMasterEvents,
    _application_is_connected,
    _decode_can_event,
    _format_com_data,
    _normalize_com_data,
    _validate_mapping_result,
)


class FakeCom:
    def __init__(self) -> None:
        self.transmit_args: tuple[object, ...] | None = None

    def transmit_can_async_verbose(self, *args: object) -> None:
        self.transmit_args = args


class FakePythonCom:
    def __init__(self) -> None:
        self.pump_count = 0

    def PumpWaitingMessages(self) -> None:
        self.pump_count += 1


class TSMasterComDataTests(unittest.TestCase):
    def test_accepts_an_enabled_hardware_mapping(self) -> None:
        self.assertEqual(
            "TC1001 channel 1",
            _validate_mapping_result((3, 0, 0, 9, "TC1001", False), 0),
        )

    def test_rejects_a_disabled_hardware_mapping(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no enabled hardware mapping"):
            _validate_mapping_result((3, 0, 0, 9, "TC1001", True), 0)

    def test_parses_application_connected_system_variable(self) -> None:
        self.assertTrue(_application_is_connected((True, "True")))
        self.assertTrue(_application_is_connected((True, "1")))
        self.assertFalse(_application_is_connected((True, "False")))
        self.assertFalse(_application_is_connected((False, "True")))

    def test_formats_bytes_for_verbose_transmit(self) -> None:
        self.assertEqual("1,171,0,255", _format_com_data(bytes.fromhex("01 AB 00 FF")))

    def test_decodes_com_csv_data(self) -> None:
        self.assertEqual(
            bytes.fromhex("35 61 F5 3E F9 7F 00 00"),
            _normalize_com_data("53,97,245,62,249,127,0,0", 8),
        )

    def test_decodes_signed_com_values(self) -> None:
        self.assertEqual(
            bytes.fromhex("FF 00 7F 80"),
            _normalize_com_data("-1,0,127,-128", 4),
        )

    def test_limits_data_to_dlc(self) -> None:
        self.assertEqual(bytes.fromhex("01 02"), _normalize_com_data("1,2,3,4", 2))

    def test_send_uses_verbose_api_without_a_com_record(self) -> None:
        backend = TSMasterComBackend()
        fake_com = FakeCom()
        backend._app = object()
        backend._com = fake_com
        backend._pythoncom = object()

        backend.send(CanFrame(channel=0, can_id=0x18EF80F9, data=bytes.fromhex("01 2A 00")))

        self.assertEqual(
            (0, False, True, 3, 0x18EF80F9, "1,42,0"),
            fake_com.transmit_args,
        )

    def test_decodes_on_rx_can_event(self) -> None:
        frame = _decode_can_event(
            0,
            False,
            False,
            True,
            False,
            8,
            0x18EFF980,
            123456,
            "129,1,0,0,0,0,0,0",
        )

        self.assertIsNotNone(frame)
        assert frame is not None
        self.assertEqual(0, frame.channel)
        self.assertEqual(0x18EFF980, frame.can_id)
        self.assertEqual(bytes.fromhex("81 01 00 00 00 00 00 00"), frame.data)
        self.assertEqual(123456, frame.timestamp_us)
        self.assertTrue(frame.is_extended)

    def test_event_rejects_captured_corrupt_fifo_metadata(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "invalid Classic CAN DLC: 63"):
            _decode_can_event(
                0,
                False,
                False,
                False,
                False,
                63,
                666,
                60473431,
                "53,97,251,184,250,127,0,0",
            )

    def test_event_sink_queues_a_valid_frame(self) -> None:
        sink = _TSMasterEvents()

        sink.OnRxCAN(
            0,
            False,
            False,
            True,
            False,
            8,
            0x18EFF980,
            123456,
            (129, 1, 0, 0, 0, 0, 0, 0),
        )

        self.assertEqual(1, len(sink.frames))
        self.assertFalse(sink.errors)
        self.assertEqual(0x18EFF980, sink.frames[0].can_id)

    def test_receive_pumps_com_events_and_returns_queued_frame(self) -> None:
        backend = TSMasterComBackend()
        pythoncom = FakePythonCom()
        sink = _TSMasterEvents()
        expected = CanFrame(
            channel=0,
            can_id=0x18EFF980,
            data=bytes.fromhex("81 01 00 00 00 00 00 00"),
            timestamp_us=123456,
            is_extended=True,
        )
        sink.frames.append(expected)
        backend._pythoncom = pythoncom
        backend._event_sink = sink
        backend._config = ConnectionConfig()

        self.assertEqual(expected, backend.receive())
        self.assertEqual(1, pythoncom.pump_count)

    def test_event_ignores_remote_and_error_frames(self) -> None:
        self.assertIsNone(
            _decode_can_event(0, False, True, True, False, 8, 0x18EFF980, 0, bytes(8))
        )
        self.assertIsNone(
            _decode_can_event(0, False, False, True, True, 8, 0x18EFF980, 0, bytes(8))
        )

    def test_event_reports_an_invalid_data_type(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unsupported CAN data type: int"):
            _decode_can_event(0, False, False, True, False, 8, 0x123, 123456, 42)


if __name__ == "__main__":
    unittest.main()
