from __future__ import annotations

import time
import unittest

from PySide6.QtCore import QCoreApplication

from j1939_control.backend.base import CanFrame
from j1939_control.backend.mock import MockBackend
from j1939_control.config import ConnectionConfig
from j1939_control.protocol.commands import (
    Capability,
    CommandRequest,
    Opcode,
    ProtocolInfo,
    RuntimeFlag,
)
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
        protocol_infos: list[ProtocolInfo] = []
        runtime_states: list[RuntimeFlag] = []
        worker.log_message.connect(logs.append)
        worker.command_result.connect(lambda message, ok: results.append((message, ok)))
        worker.readback_changed.connect(
            lambda value, state: readbacks.append((value, state))
        )
        worker.protocol_info_changed.connect(protocol_infos.append)
        worker.runtime_status_changed.connect(runtime_states.append)

        worker.connect_backend("mock", ConnectionConfig())
        worker._poll()
        worker.apply_engine_speed(1500.0)
        worker._poll()

        status_index = next(index for index, line in enumerate(logs) if "GET_STATUS seq=0x02" in line)
        set_index = next(index for index, line in enumerate(logs) if "SET_CONST seq=0x03" in line)
        start_index = next(index for index, line in enumerate(logs) if "START seq=0x04" in line)
        self.assertLess(status_index, set_index)
        self.assertLess(set_index, start_index)
        self.assertEqual("1.0", protocol_infos[-1].version)
        self.assertIn(RuntimeFlag.RUNNING, runtime_states[-1])
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

    def test_stop_ack_changes_runtime_and_readback_state(self) -> None:
        worker = CanWorker()
        runtime_states: list[RuntimeFlag] = []
        readbacks: list[tuple[object, str]] = []
        worker.runtime_status_changed.connect(runtime_states.append)
        worker.readback_changed.connect(lambda value, state: readbacks.append((value, state)))

        worker.connect_backend("mock", ConnectionConfig())
        worker._poll()
        worker.apply_engine_speed(1500.0)
        worker._poll()
        worker.stop_transmission()
        worker._poll()

        self.assertNotIn(RuntimeFlag.RUNNING, runtime_states[-1])
        self.assertIn((None, "Stopped"), readbacks)
        worker.shutdown()

    def test_incompatible_capabilities_do_not_mark_stm32_ready(self) -> None:
        worker = CanWorker()
        ready: list[bool] = []
        results: list[tuple[str, bool]] = []
        worker.stm32_ready.connect(lambda: ready.append(True))
        worker.command_result.connect(lambda message, ok: results.append((message, ok)))
        incomplete = ProtocolInfo(1, 0, 1, Capability.GET_STATUS)

        worker._handle_status_response(0, incomplete.encode())

        self.assertFalse(ready)
        self.assertTrue(any(message.startswith("Incompatible STM32") for message, _ in results))

    def test_timeout_retry_reuses_exact_sequence_and_payload(self) -> None:
        class SilentBackend:
            def __init__(self) -> None:
                self.frames: list[CanFrame] = []

            def send(self, frame: CanFrame) -> None:
                self.frames.append(frame)

            def receive(self) -> None:
                return None

            def disconnect(self) -> None:
                pass

        backend = SilentBackend()
        worker = CanWorker()
        worker._backend = backend
        worker._config = ConnectionConfig()
        request = CommandRequest(Opcode.GET_STATUS, 0x55)
        worker._send_request(request)
        worker._pending[0x55].deadline = 0.0

        worker._poll()

        self.assertEqual(2, len(backend.frames))
        self.assertEqual(backend.frames[0].data, backend.frames[1].data)
        self.assertEqual(0, worker._pending[0x55].retries_remaining)
        worker.shutdown()


class MockDedupTests(unittest.TestCase):
    def test_duplicate_request_replays_ack_without_reapplying(self) -> None:
        backend = MockBackend()
        config = ConnectionConfig()
        backend.connect(config)
        payload = CommandRequest(
            Opcode.SET_CONST,
            sequence=0x44,
            argument16=103,
            argument32=12000,
        ).encode()
        frame = CanFrame(config.channel, config.request_can_id, payload)

        backend.send(frame)
        first_ack = backend.receive()
        backend.send(frame)
        replayed_ack = backend.receive()

        self.assertIsNotNone(first_ack)
        self.assertIsNotNone(replayed_ack)
        assert first_ack is not None and replayed_ack is not None
        self.assertEqual(first_ack.data, replayed_ack.data)
        self.assertEqual(1, backend.applied_command_count[Opcode.SET_CONST])


if __name__ == "__main__":
    unittest.main()
