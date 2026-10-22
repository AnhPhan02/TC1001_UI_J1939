from __future__ import annotations

import struct
import time
import unittest
from collections import deque

from PySide6.QtCore import QCoreApplication

from j1939_control.backend.base import CanFrame
from j1939_control.backend.mock import MockBackend
from j1939_control.config import ConnectionConfig
from j1939_control.profiles import PROFILE_40_PGNS, PROFILE_40_SIGNALS
from j1939_control.protocol.commands import Opcode
from j1939_control.workers.profile_worker import ProfileCanWorker


class ProfileDefinitionTests(unittest.TestCase):
    def test_profile_has_exact_requested_keys_and_rate(self) -> None:
        self.assertEqual(
            [
                113, 114, 74, 75, 12, 13, 14, 15, 83, 59, 60, 61, 62,
                94, 95, 96, 97, 98, 99, 24, 25, 26, 27, 28, 29, 30,
                31, 32, 84, 85, 86, 87, 122, 123, 124, 125, 126, 67,
                68, 38,
            ],
            [signal.signal_key for signal in PROFILE_40_SIGNALS],
        )
        self.assertNotIn(103, [signal.signal_key for signal in PROFILE_40_SIGNALS])
        self.assertNotIn(144, [signal.signal_key for signal in PROFILE_40_SIGNALS])
        self.assertEqual(11, len(PROFILE_40_PGNS))
        self.assertEqual(480, sum(pgn.expected_fps for pgn in PROFILE_40_PGNS))

    def test_zero_physical_defaults_scale_to_valid_raw_values(self) -> None:
        for signal in PROFILE_40_SIGNALS:
            self.assertGreaterEqual(signal.raw_value, 0)
            self.assertLess(signal.raw_value, 1 << signal.bit_length)


class RejectNthSetBackend:
    def __init__(self, reject_number: int) -> None:
        self.reject_number = reject_number
        self.frames: list[CanFrame] = []
        self.rx: deque[CanFrame] = deque()
        self.config = ConnectionConfig()
        self.set_count = 0

    def send(self, frame: CanFrame) -> None:
        self.frames.append(frame)
        opcode, sequence, _argument16, argument32 = struct.unpack("<BBHI", frame.data)
        status = 0
        detail = 0
        if opcode == Opcode.SET_CONST:
            self.set_count += 1
            if self.set_count >= self.reject_number:
                status = 7
                detail = 9
        response = struct.pack("<BBBBI", opcode | 0x80, sequence, status, detail, argument32)
        self.rx.append(
            CanFrame(self.config.channel, self.config.response_can_id, response)
        )

    def receive(self) -> CanFrame | None:
        return self.rx.popleft() if self.rx else None

    def disconnect(self) -> None:
        pass


class ProfileWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_batch_sends_one_start_only_after_40_set_const_acks(self) -> None:
        worker = ProfileCanWorker()
        logs: list[str] = []
        batch_states: list[tuple[str, int, int]] = []
        signal_states: list[tuple[int, str, str]] = []
        worker.log_message.connect(logs.append)
        worker.batch_status_changed.connect(
            lambda state, current, total: batch_states.append((state, current, total))
        )
        worker.profile_signal_status_changed.connect(
            lambda index, state, detail: signal_states.append((index, state, detail))
        )

        worker.connect_backend("mock", ConnectionConfig())
        worker._poll()
        worker.start_profile_40()
        worker._poll()

        backend = worker._backend
        self.assertIsInstance(backend, MockBackend)
        assert isinstance(backend, MockBackend)
        self.assertEqual(1, backend.applied_command_count[Opcode.STOP])
        self.assertEqual(40, backend.applied_command_count[Opcode.SET_CONST])
        self.assertEqual(1, backend.applied_command_count[Opcode.START])
        tx_commands = [line.rsplit(" ", 2)[-2] for line in logs if line.startswith("TX ")]
        self.assertEqual("STOP", tx_commands[2])
        self.assertEqual(["SET_CONST"] * 40, tx_commands[3:43])
        self.assertEqual("START", tx_commands[43])
        self.assertEqual(("Running", 40, 40), batch_states[-1])
        self.assertEqual(
            40, sum(1 for _, state, _ in signal_states if state == "Configured")
        )
        worker.shutdown()

    def test_signal_17_failure_stops_batch_and_retry_reuses_payload(self) -> None:
        backend = RejectNthSetBackend(reject_number=17)
        worker = ProfileCanWorker()
        worker._backend = backend
        worker._config = backend.config
        worker._compatible = True
        results: list[tuple[str, bool]] = []
        batch_states: list[tuple[str, int, int]] = []
        worker.command_result.connect(lambda message, ok: results.append((message, ok)))
        worker.batch_status_changed.connect(
            lambda state, current, total: batch_states.append((state, current, total))
        )

        worker.start_profile_40()
        worker._poll()

        opcodes = [Opcode(frame.data[0]) for frame in backend.frames]
        self.assertEqual(17, opcodes.count(Opcode.SET_CONST))
        self.assertNotIn(Opcode.START, opcodes)
        self.assertEqual("Failed", batch_states[-1][0])
        self.assertTrue(any("key=97, SPN=91" in message for message, _ in results))

        failed_payload = backend.frames[-1].data
        worker.retry_failed_profile_command()
        worker._poll()
        self.assertEqual(failed_payload, backend.frames[-1].data)
        self.assertNotIn(Opcode.START, [Opcode(frame.data[0]) for frame in backend.frames])
        worker.shutdown()

    def test_rx_data_is_summarized_instead_of_logged_per_frame(self) -> None:
        worker = ProfileCanWorker()
        worker._config = ConnectionConfig()
        logs: list[str] = []
        snapshots: list[dict[str, object]] = []
        worker.log_message.connect(logs.append)
        worker.rx_rate_snapshot.connect(snapshots.append)
        frame = CanFrame(0, 0x0CF00280, b"\x00" * 8)

        for _ in range(100):
            worker._handle_frame(frame)
        worker._rx_window_started = time.monotonic() - 1.0
        worker._emit_rx_snapshot_if_due(time.monotonic())

        self.assertFalse(any(line.startswith("RX 0x") for line in logs))
        self.assertEqual(100, snapshots[-1]["total_count"])
        self.assertEqual(1, len(snapshots[-1]["frames"]))


if __name__ == "__main__":
    unittest.main()
