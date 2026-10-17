from __future__ import annotations

from typing import Any

from ..config import ConnectionConfig
from .base import CanFrame


def _signed_bytes(data: bytes) -> list[int]:
    """Convert bytes for a COM SAFEARRAY(VT_I1), whose elements are signed."""
    return [value if value < 0x80 else value - 0x100 for value in data]


def _normalize_com_data(data: Any, dlc: int) -> bytes:
    if isinstance(data, str):
        return data.encode("latin-1")[:dlc]
    return bytes((int(value) & 0xFF) for value in data[:dlc])


class TSMasterComBackend:
    """Out-of-process TSMaster COM backend owned by one Qt worker thread."""

    def __init__(self) -> None:
        self._pythoncom: Any | None = None
        self._variant: Any | None = None
        self._app: Any | None = None
        self._com: Any | None = None
        self._config: ConnectionConfig | None = None

    def connect(self, config: ConnectionConfig) -> None:
        if self._app is not None:
            return
        try:
            import pythoncom
            import win32com.client
            from win32com.client import VARIANT
        except ImportError as exc:
            raise RuntimeError("pywin32 is required for the TSMaster backend") from exc

        pythoncom.CoInitialize()
        try:
            try:
                app = win32com.client.Dispatch("TSMaster.TSApplication")
            except pythoncom.com_error as exc:
                raise RuntimeError(
                    "TSMaster COM class is not registered for this 64-bit Python. "
                    "Run TSMaster64.exe once as Administrator, then retry."
                ) from exc
            com = app.TSCOM()
            app.set_current_application(config.app_name)
            app.set_can_channel_count(max(config.channel + 1, 1))
            app.set_lin_channel_count(0)
            app.configure_baudrate_can(config.channel, config.bitrate_kbps, False, False)
            app.connect()
            com.fifo_enable_receive_fifo()
        except Exception:
            pythoncom.CoUninitialize()
            raise

        self._pythoncom = pythoncom
        self._variant = VARIANT
        self._app = app
        self._com = com
        self._config = config

    def disconnect(self) -> None:
        if self._app is None:
            return
        try:
            if self._com is not None:
                self._com.fifo_disable_receive_fifo()
            self._app.disconnect()
        finally:
            self._com = None
            self._app = None
            self._config = None
            if self._pythoncom is not None:
                self._pythoncom.CoUninitialize()
            self._pythoncom = None
            self._variant = None

    def send(self, frame: CanFrame) -> None:
        if self._app is None or self._com is None or self._variant is None or self._pythoncom is None:
            raise RuntimeError("TSMaster COM backend is not connected")
        if len(frame.data) > 8:
            raise ValueError("Classic CAN frame cannot contain more than 8 bytes")
        can_record = __import__("win32com.client", fromlist=["Record"]).Record("TCAN", self._app)
        can_record.FIdxChn = frame.channel
        can_record.FIsExtendedId = 1 if frame.is_extended else 0
        can_record.FIsRemote = False
        can_record.FIdentifier = frame.can_id
        can_record.FDLC = len(frame.data)
        can_record.FDatas = self._variant(
            self._pythoncom.VT_ARRAY | self._pythoncom.VT_I1,
            _signed_bytes(frame.data),
        )
        self._com.transmit_can_async(can_record)

    def receive(self) -> CanFrame | None:
        if self._com is None or self._config is None:
            return None
        result = self._com.fifo_receive_can_msg(self._config.channel, False)
        if not result or not result[0]:
            return None
        _, channel, _is_remote, is_extended, dlc, can_id, timestamp_us, data = result
        return CanFrame(
            channel=int(channel),
            can_id=int(can_id),
            data=_normalize_com_data(data, int(dlc)),
            timestamp_us=int(timestamp_us),
            is_extended=bool(is_extended),
            is_tx=False,
        )
