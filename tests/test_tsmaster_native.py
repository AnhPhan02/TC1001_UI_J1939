from __future__ import annotations

import ctypes
import unittest

from j1939_control.backend.base import CanFrame
from j1939_control.backend.tsmaster_native import (
    ERROR_FRAME,
    EXTENDED_FRAME,
    REMOTE_FRAME,
    TSMasterBackend,
    TX_DIRECTION,
    _TLIBCAN,
    _TLIBTSMapping,
    _decode_native_can,
    _mapping_description,
)
from j1939_control.config import ConnectionConfig


class FakeNativeDll:
    def __init__(self) -> None:
        self.transmitted: _TLIBCAN | None = None
        self.receive_message: _TLIBCAN | None = None

    def tsapp_transmit_can_async(self, message_pointer: object) -> int:
        source = message_pointer._obj  # type: ignore[attr-defined]
        self.transmitted = _TLIBCAN.from_buffer_copy(source)
        return 0

    def tsfifo_receive_can_msgs(
        self,
        messages: object,
        count_pointer: object,
        channel: int,
        include_tx: bool,
    ) -> int:
        if self.receive_message is None:
            count_pointer._obj.value = 0  # type: ignore[attr-defined]
            return 0
        messages[0] = self.receive_message  # type: ignore[index]
        count_pointer._obj.value = 1  # type: ignore[attr-defined]
        return 0

    def tsapp_get_error_description(self, code: int, description: object) -> int:
        return -1


class TSMasterNativeTests(unittest.TestCase):
    def test_native_struct_layout_matches_vendor_definition(self) -> None:
        self.assertEqual(24, ctypes.sizeof(_TLIBCAN))
        self.assertEqual(89, ctypes.sizeof(_TLIBTSMapping))

    def test_mapping_description_requires_an_enabled_device(self) -> None:
        mapping = _TLIBTSMapping()
        mapping.FHWDeviceName = b"TC1001"
        mapping.FHWChannelIndex = 0

        self.assertEqual("TC1001 channel 1", _mapping_description(mapping, 0))

        mapping.FMappingDisabled = True
        with self.assertRaisesRegex(RuntimeError, "no enabled hardware mapping"):
            _mapping_description(mapping, 0)

    def test_send_uses_native_tlibcan_record(self) -> None:
        dll = FakeNativeDll()
        backend = TSMasterBackend()
        backend._dll = dll

        backend.send(
            CanFrame(
                channel=0,
                can_id=0x18EF80F9,
                data=bytes.fromhex("01 2A 00 00 00 00 00 00"),
            )
        )

        self.assertIsNotNone(dll.transmitted)
        assert dll.transmitted is not None
        self.assertEqual(0x18EF80F9, dll.transmitted.FIdentifier)
        self.assertEqual(8, dll.transmitted.FDLC)
        self.assertEqual(TX_DIRECTION | EXTENDED_FRAME, dll.transmitted.FProperties)
        self.assertEqual(
            bytes.fromhex("01 2A 00 00 00 00 00 00"),
            bytes(dll.transmitted.FData),
        )

    def test_receive_uses_native_fifo_record(self) -> None:
        dll = FakeNativeDll()
        message = _TLIBCAN()
        message.FIdxChn = 0
        message.FProperties = EXTENDED_FRAME
        message.FDLC = 8
        message.FIdentifier = 0x18EFF980
        message.FTimeUs = 123456
        for index, value in enumerate(bytes.fromhex("81 01 00 00 00 00 00 00")):
            message.FData[index] = value
        dll.receive_message = message
        backend = TSMasterBackend()
        backend._dll = dll
        backend._config = ConnectionConfig()

        frame = backend.receive()

        self.assertIsNotNone(frame)
        assert frame is not None
        self.assertEqual(0x18EFF980, frame.can_id)
        self.assertEqual(bytes.fromhex("81 01 00 00 00 00 00 00"), frame.data)
        self.assertEqual(123456, frame.timestamp_us)
        self.assertTrue(frame.is_extended)
        self.assertFalse(frame.is_tx)

    def test_receive_returns_none_when_fifo_is_empty(self) -> None:
        backend = TSMasterBackend()
        backend._dll = FakeNativeDll()
        backend._config = ConnectionConfig()

        self.assertIsNone(backend.receive())

    def test_decoder_ignores_remote_and_error_frames(self) -> None:
        message = _TLIBCAN()
        message.FDLC = 8
        message.FIdentifier = 0x123
        message.FProperties = REMOTE_FRAME
        self.assertIsNone(_decode_native_can(message))

        message.FProperties = ERROR_FRAME
        self.assertIsNone(_decode_native_can(message))


if __name__ == "__main__":
    unittest.main()
