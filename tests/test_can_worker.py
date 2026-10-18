from __future__ import annotations

import time
import unittest

from PySide6.QtCore import QCoreApplication

from j1939_control.config import ConnectionConfig
from j1939_control.protocol.commands import Opcode
from j1939_control.workers.can_worker import CanWorker, PendingRequest


class CanWorkerSequenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_successful_set_const_automatically_sends_start_then_reads_eec1(self) -> None:
        worker = CanWorker()
        logs: list[str] = []
        results: list[tuple[str, bool]] = []
        readbacks: list[tuple[object, str]] = []
        worker.log_message.connect(logs.append)
        worker.command_result.connect(lambda message, ok: results.append((message, ok)))
        worker.readback_changed.connect(
            lambda value, state: readbacks.append((value, state))
        )

        worker.connect_backend("mock", ConnectionConfig())
        worker._poll()
        worker.apply_engine_speed(1500.0)
        worker._poll()

        set_index = next(index for index, line in enumerate(logs) if "SET_CONST seq=0x02" in line)
        start_index = next(index for index, line in enumerate(logs) if "START seq=0x03" in line)
        self.assertLess(set_index, start_index)
        self.assertIn(("SET_CONST applied", True), results)
        self.assertIn(("START applied", True), results)
        self.assertIn((1500.0, "Fresh"), readbacks)
        worker.shutdown()

    def test_rejected_set_const_does_not_send_start(self) -> None:
        worker = CanWorker()
        results: list[tuple[str, bool]] = []
        logs: list[str] = []
        worker.command_result.connect(lambda message, ok: results.append((message, ok)))
        worker.log_message.connect(logs.append)
        worker._pending[0x22] = PendingRequest(
            opcode=Opcode.SET_CONST,
            deadline=time.monotonic() + 1.0,
        )

        worker._handle_response(bytes.fromhex("90 22 01 00 00 00 00 00"))

        self.assertIn(("SET_CONST rejected: status=1, detail=0", False), results)
        self.assertFalse(any("START seq=" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
