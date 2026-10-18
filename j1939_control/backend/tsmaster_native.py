from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Any

from ..config import ConnectionConfig
from .base import CanFrame


APP_CAN = 0
TX_DIRECTION = 0x01
REMOTE_FRAME = 0x02
EXTENDED_FRAME = 0x04
ERROR_FRAME = 0x80


class _TLIBCAN(ctypes.Structure):
    """Native Classic CAN record from TSMaster/TSMasterApi/TSStruct.py."""

    _pack_ = 1
    _fields_ = [
        ("FIdxChn", ctypes.c_uint8),
        ("FProperties", ctypes.c_uint8),
        ("FDLC", ctypes.c_uint8),
        ("FReserved", ctypes.c_uint8),
        ("FIdentifier", ctypes.c_int32),
        ("FTimeUs", ctypes.c_int64),
        ("FData", ctypes.c_uint8 * 8),
    ]


class _TLIBTSMapping(ctypes.Structure):
    """Native channel mapping record from TSMaster/TSMasterApi/TSStruct.py."""

    _pack_ = 1
    _fields_ = [
        ("FAppName", ctypes.c_char * 32),
        ("FAppChannelIndex", ctypes.c_int32),
        ("FAppChannelType", ctypes.c_int32),
        ("FHWDeviceType", ctypes.c_int32),
        ("FHWIndex", ctypes.c_int32),
        ("FHWChannelIndex", ctypes.c_int32),
        ("FHWDeviceSubType", ctypes.c_int32),
        ("FHWDeviceName", ctypes.c_char * 32),
        ("FMappingDisabled", ctypes.c_bool),
    ]


def _find_tsmaster_dll() -> Path:
    candidates: list[Path] = []
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\TOSUN\TSMaster") as key:
            value_name = "libTSMaster_x64" if ctypes.sizeof(ctypes.c_void_p) == 8 else "libTSMaster_x86"
            registered_path = Path(winreg.QueryValueEx(key, value_name)[0])
            candidates.append(registered_path.parent / "TSMaster.dll")
    except (ImportError, FileNotFoundError, OSError):
        pass

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        bin_dir = "bin64" if ctypes.sizeof(ctypes.c_void_p) == 8 else "bin"
        candidates.append(
            Path(local_app_data) / "Programs" / "TOSUN" / "TSMaster" / bin_dir / "TSMaster.dll"
        )
    candidates.extend(
        (
            Path(r"C:\Program Files\TOSUN\TSMaster\bin64\TSMaster.dll"),
            Path(r"C:\Program Files (x86)\TOSUN\TSMaster\bin\TSMaster.dll"),
        )
    )

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    checked = "; ".join(str(path) for path in candidates) or "no installation paths"
    raise RuntimeError(f"TSMaster.dll was not found. Checked: {checked}")


def _load_tsmaster_dll(path: Path) -> Any:
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise RuntimeError("The TSMaster native backend requires Windows")
    try:
        return loader(str(path))
    except OSError as exc:
        raise RuntimeError(f"Could not load TSMaster native API from {path}: {exc}") from exc


def _configure_api(dll: Any) -> None:
    dll.initialize_lib_tsmaster.restype = ctypes.c_int32
    dll.initialize_lib_tsmaster.argtypes = [ctypes.c_char_p]
    dll.finalize_lib_tsmaster.restype = None
    dll.finalize_lib_tsmaster.argtypes = []
    dll.tsapp_set_current_application.restype = ctypes.c_int32
    dll.tsapp_set_current_application.argtypes = [ctypes.c_char_p]
    dll.tsapp_get_can_channel_count.restype = ctypes.c_int32
    dll.tsapp_get_can_channel_count.argtypes = [ctypes.POINTER(ctypes.c_int32)]
    dll.tsapp_set_can_channel_count.restype = ctypes.c_int32
    dll.tsapp_set_can_channel_count.argtypes = [ctypes.c_int32]
    dll.tsapp_get_mapping.restype = ctypes.c_int32
    dll.tsapp_get_mapping.argtypes = [ctypes.POINTER(_TLIBTSMapping)]
    dll.tsapp_configure_baudrate_can.restype = ctypes.c_int32
    dll.tsapp_configure_baudrate_can.argtypes = [
        ctypes.c_int32,
        ctypes.c_float,
        ctypes.c_bool,
        ctypes.c_bool,
    ]
    dll.tsapp_connect.restype = ctypes.c_int32
    dll.tsapp_connect.argtypes = []
    dll.tsapp_disconnect.restype = ctypes.c_int32
    dll.tsapp_disconnect.argtypes = []
    dll.tsfifo_enable_receive_fifo.restype = None
    dll.tsfifo_enable_receive_fifo.argtypes = []
    dll.tsfifo_disable_receive_fifo.restype = None
    dll.tsfifo_disable_receive_fifo.argtypes = []
    dll.tsfifo_receive_can_msgs.restype = ctypes.c_int32
    dll.tsfifo_receive_can_msgs.argtypes = [
        ctypes.POINTER(_TLIBCAN),
        ctypes.POINTER(ctypes.c_int32),
        ctypes.c_int32,
        ctypes.c_bool,
    ]
    dll.tsapp_transmit_can_async.restype = ctypes.c_int32
    dll.tsapp_transmit_can_async.argtypes = [ctypes.POINTER(_TLIBCAN)]
    dll.tsapp_get_error_description.restype = ctypes.c_int32
    dll.tsapp_get_error_description.argtypes = [
        ctypes.c_int32,
        ctypes.POINTER(ctypes.c_char_p),
    ]


def _error_description(dll: Any, code: int) -> str:
    description = ctypes.c_char_p()
    try:
        result = int(dll.tsapp_get_error_description(code, ctypes.byref(description)))
        if result == 0 and description.value:
            return description.value.decode("utf-8", errors="replace")
    except Exception:
        pass
    return f"error code {code}"


def _check_result(dll: Any, code: Any, operation: str) -> None:
    numeric_code = int(code)
    if numeric_code != 0:
        detail = _error_description(dll, numeric_code)
        if numeric_code == 82:
            detail += (
                " (close other TSMaster/Python sessions, then verify the 64-bit driver "
                "and hardware installation)"
            )
        raise RuntimeError(f"TSMaster {operation} failed: {detail}")


def _mapping_description(mapping: _TLIBTSMapping, channel: int) -> str:
    device_name = bytes(mapping.FHWDeviceName).split(b"\0", 1)[0].decode(
        "utf-8", errors="replace"
    )
    if mapping.FMappingDisabled or not device_name:
        raise RuntimeError(
            f"TSMaster CAN channel {channel + 1} has no enabled hardware mapping. "
            "Map it to the physical CAN adapter in TSMaster, then retry."
        )
    return f"{device_name} channel {int(mapping.FHWChannelIndex) + 1}"


def _decode_native_can(message: _TLIBCAN) -> CanFrame | None:
    properties = int(message.FProperties)
    if properties & (REMOTE_FRAME | ERROR_FRAME):
        return None
    dlc = int(message.FDLC)
    can_id = int(message.FIdentifier)
    if not 0 <= dlc <= 8:
        raise RuntimeError(f"TSMaster returned invalid Classic CAN DLC: {dlc}")
    if not 0 <= can_id <= 0x1FFFFFFF:
        raise RuntimeError(f"TSMaster returned invalid CAN identifier: {can_id}")
    return CanFrame(
        channel=int(message.FIdxChn),
        can_id=can_id,
        data=bytes(message.FData[:dlc]),
        timestamp_us=int(message.FTimeUs),
        is_extended=bool(properties & EXTENDED_FRAME),
        is_tx=bool(properties & TX_DIRECTION),
    )


class TSMasterBackend:
    """Native TSMaster backend using fixed ctypes records instead of COM Automation."""

    def __init__(self) -> None:
        self._dll: Any | None = None
        self._config: ConnectionConfig | None = None
        self.mapping_description: str | None = None

    def connect(self, config: ConnectionConfig) -> None:
        if self._dll is not None:
            return
        dll = _load_tsmaster_dll(_find_tsmaster_dll())
        _configure_api(dll)
        initialized = False
        connected = False
        fifo_enabled = False
        app_name = config.app_name.encode("utf-8")
        if len(app_name) > 31:
            raise ValueError("TSMaster application name must be at most 31 UTF-8 bytes")

        try:
            _check_result(dll, dll.initialize_lib_tsmaster(app_name), "initialize")
            initialized = True

            _check_result(
                dll,
                dll.tsapp_set_can_channel_count(config.channel + 1),
                "set_can_channel_count",
            )

            mapping = _TLIBTSMapping()
            mapping.FAppName = app_name
            mapping.FAppChannelIndex = config.channel
            mapping.FAppChannelType = APP_CAN
            _check_result(
                dll,
                dll.tsapp_get_mapping(ctypes.byref(mapping)),
                "get_mapping",
            )
            mapping_description = _mapping_description(mapping, config.channel)

            _check_result(
                dll,
                dll.tsapp_configure_baudrate_can(
                    config.channel,
                    ctypes.c_float(config.bitrate_kbps),
                    False,
                    False,
                ),
                "configure_baudrate_can",
            )
            _check_result(dll, dll.tsapp_connect(), "connect")
            connected = True
            dll.tsfifo_enable_receive_fifo()
            fifo_enabled = True
        except Exception:
            if fifo_enabled:
                dll.tsfifo_disable_receive_fifo()
            if connected:
                try:
                    dll.tsapp_disconnect()
                except Exception:
                    pass
            if initialized:
                dll.finalize_lib_tsmaster()
            raise

        self._dll = dll
        self._config = config
        self.mapping_description = mapping_description

    def disconnect(self) -> None:
        dll = self._dll
        if dll is None:
            return
        first_error: Exception | None = None
        try:
            try:
                dll.tsfifo_disable_receive_fifo()
            except Exception as exc:
                first_error = exc
            try:
                _check_result(dll, dll.tsapp_disconnect(), "disconnect")
            except Exception as exc:
                first_error = first_error or exc
        finally:
            try:
                dll.finalize_lib_tsmaster()
            finally:
                self._dll = None
                self._config = None
                self.mapping_description = None
        if first_error is not None:
            raise first_error

    def send(self, frame: CanFrame) -> None:
        if self._dll is None:
            raise RuntimeError("TSMaster native backend is not connected")
        if len(frame.data) > 8:
            raise ValueError("Classic CAN frame cannot contain more than 8 bytes")
        message = _TLIBCAN()
        message.FIdxChn = frame.channel
        message.FProperties = TX_DIRECTION | (EXTENDED_FRAME if frame.is_extended else 0)
        message.FDLC = len(frame.data)
        message.FIdentifier = frame.can_id
        for index, value in enumerate(frame.data):
            message.FData[index] = value
        _check_result(
            self._dll,
            self._dll.tsapp_transmit_can_async(ctypes.byref(message)),
            "transmit_can_async",
        )

    def receive(self) -> CanFrame | None:
        if self._dll is None or self._config is None:
            return None
        messages = (_TLIBCAN * 1)()
        count = ctypes.c_int32(1)
        _check_result(
            self._dll,
            self._dll.tsfifo_receive_can_msgs(
                messages,
                ctypes.byref(count),
                self._config.channel,
                False,
            ),
            "receive_can_msgs",
        )
        if count.value == 0:
            return None
        if count.value != 1:
            raise RuntimeError(f"TSMaster returned an invalid receive count: {count.value}")
        return _decode_native_can(messages[0])
