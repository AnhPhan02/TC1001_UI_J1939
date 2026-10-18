"""Legacy COM implementation retained only for reference.

The active runtime backend is :mod:`tsmaster_native`; see the compatibility alias below.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from ..config import ConnectionConfig
from .base import CanFrame
from .tsmaster_native import TSMasterBackend


# Compatibility name only. Runtime traffic now uses the native backend because the
# installed out-of-process COM server corrupts FIFO records and exposes no event sink.
TSMasterComBackend = TSMasterBackend


TSMASTER_OOP_TYPELIB_ID = "{8F945AA7-2205-48EB-AF40-2F614FC188FB}"
TSMASTER_OOP_APPLICATION_INTERFACE_ID = "{8996F797-3C97-4DB4-8047-97625B79E361}"
APP_CAN = 0


def _format_com_data(data: bytes) -> str:
    """Format CAN bytes for TSMaster's verbose COM BSTR argument."""
    return ",".join(str(value) for value in data)


def _decode_com_data(data: Any) -> bytes:
    """Decode the complete byte container returned by TSMaster COM."""
    if isinstance(data, str):
        values = [part.strip() for part in data.split(",") if part.strip()]
        return bytes((int(value) & 0xFF) for value in values)
    try:
        values = list(data)
    except TypeError as exc:
        raise RuntimeError(
            f"TSMaster returned unsupported CAN data type: {type(data).__name__}"
        ) from exc
    return bytes((int(value) & 0xFF) for value in values)


def _normalize_com_data(data: Any, dlc: int) -> bytes:
    return _decode_com_data(data)[:dlc]


def _validate_mapping_result(result: Any, channel: int) -> str:
    if not isinstance(result, (tuple, list)) or len(result) != 6:
        raise RuntimeError(
            f"TSMaster returned an unexpected CAN channel {channel + 1} mapping: {result!r}"
        )
    _device_type, _hw_index, hw_channel, _device_subtype, device_name, disabled = result
    device_name = str(device_name).strip()
    if bool(disabled) or not device_name:
        raise RuntimeError(
            f"TSMaster CAN channel {channel + 1} has no enabled hardware mapping. "
            "Map it to the physical CAN adapter in TSMaster, then retry."
        )
    return f"{device_name} channel {int(hw_channel) + 1}"


def _application_is_connected(result: Any) -> bool:
    if not isinstance(result, (tuple, list)) or len(result) != 2 or not bool(result[0]):
        return False
    value = str(result[1]).strip().lower()
    if value in {"true", "yes", "on"}:
        return True
    try:
        return float(value) != 0.0
    except ValueError:
        return False


def _decode_can_event(
    channel: Any,
    is_tx: Any,
    is_remote: Any,
    is_extended: Any,
    is_error: Any,
    dlc: Any,
    can_id: Any,
    timestamp_us: Any,
    data: Any,
) -> CanFrame | None:
    """Translate a TSMaster OnRxCAN callback without accepting corrupt metadata."""
    if bool(is_remote) or bool(is_error):
        return None

    dlc = int(dlc)
    can_id = int(can_id)
    if not 0 <= dlc <= 8:
        raise RuntimeError(f"TSMaster OnRxCAN returned invalid Classic CAN DLC: {dlc}")
    if not 0 <= can_id <= 0x1FFFFFFF:
        raise RuntimeError(f"TSMaster OnRxCAN returned invalid CAN identifier: {can_id}")

    raw_payload = _decode_com_data(data)
    payload = raw_payload[:dlc]
    if len(payload) != dlc:
        raise RuntimeError(
            f"TSMaster OnRxCAN returned {len(payload)} CAN data bytes for DLC {dlc}"
        )
    return CanFrame(
        channel=int(channel),
        can_id=can_id,
        data=payload,
        timestamp_us=int(timestamp_us),
        is_extended=bool(is_extended),
        is_tx=bool(is_tx),
    )


class _TSMasterEvents:
    """pywin32 event sink; constructed by win32com.client.WithEvents."""

    def __init__(self) -> None:
        self.frames: deque[CanFrame] = deque()
        self.errors: deque[Exception] = deque()

    def OnRxCAN(
        self,
        AIdxChn: Any,
        AIsTx: Any,
        AIsRemote: Any,
        AIsExtended: Any,
        AIsError: Any,
        ADLC: Any,
        AIdentifier: Any,
        ATimeUs: Any,
        ADatas: Any,
    ) -> None:
        try:
            frame = _decode_can_event(
                AIdxChn,
                AIsTx,
                AIsRemote,
                AIsExtended,
                AIsError,
                ADLC,
                AIdentifier,
                ATimeUs,
                ADatas,
            )
        except Exception as exc:
            self.errors.append(exc)
            return
        if frame is not None:
            self.frames.append(frame)


class _LegacyTSMasterComBackend:
    """Out-of-process TSMaster COM backend owned by one Qt worker thread."""

    def __init__(self) -> None:
        self._pythoncom: Any | None = None
        self._app: Any | None = None
        self._com: Any | None = None
        self._event_sink: Any | None = None
        self._config: ConnectionConfig | None = None
        self.mapping_description: str | None = None

    def connect(self, config: ConnectionConfig) -> None:
        if self._app is not None:
            return
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise RuntimeError("pywin32 is required for the TSMaster backend") from exc

        pythoncom.CoInitialize()
        app: Any | None = None
        com: Any | None = None
        event_sink: Any | None = None
        try:
            try:
                win32com.client.gencache.GetModuleForTypelib(
                    TSMASTER_OOP_TYPELIB_ID,
                    0,
                    1,
                    0,
                )
                app = win32com.client.Dispatch(
                    "TSMaster.TSApplication",
                    resultCLSID=TSMASTER_OOP_APPLICATION_INTERFACE_ID,
                )
            except pythoncom.com_error as exc:
                raise RuntimeError(
                    "TSMaster COM class is not registered for this 64-bit Python. "
                    "Run TSMaster64.exe once as Administrator, then retry."
                ) from exc
            except (ImportError, ModuleNotFoundError) as exc:
                raise RuntimeError(
                    "TSMaster out-of-process COM wrapper is missing. Run makepy against "
                    "TSMaster64.exe with the same Python interpreter, then restart the UI."
                ) from exc

            def setup_step(name: str, action: Any) -> Any:
                try:
                    return action()
                except Exception as exc:
                    raise RuntimeError(f"TSMaster {name} failed: {exc}") from exc

            com = setup_step("TSCOM", app.TSCOM)
            setup_step(
                "set_current_application",
                lambda: app.set_current_application(config.app_name),
            )
            channel_count = int(
                setup_step("get_can_channel_count", app.get_can_channel_count)
            )
            if channel_count <= config.channel:
                setup_step(
                    "set_can_channel_count",
                    lambda: app.set_can_channel_count(config.channel + 1),
                )
            mapping_result = setup_step(
                "get_mapping_verbose",
                lambda: app.get_mapping_verbose(APP_CAN, config.channel),
            )
            mapping_description = _validate_mapping_result(mapping_result, config.channel)
            setup_step(
                "configure_baudrate_can",
                lambda: app.configure_baudrate_can(
                    config.channel,
                    config.bitrate_kbps,
                    False,
                    False,
                ),
            )
            setup_step("connect", app.connect)
            setup_step("wait for connection", lambda: app.wait(100))
            connected_result = setup_step(
                "read Application.Connected",
                lambda: app.get_system_var_generic("Application.Connected"),
            )
            if not _application_is_connected(connected_result):
                raise RuntimeError(
                    "TSMaster did not enter the connected state. Check the physical adapter, "
                    "channel mapping, driver, and bitrate in TSMaster."
                )
            event_sink = setup_step(
                "OnRxCAN event subscription",
                lambda: win32com.client.WithEvents(com, _TSMasterEvents),
            )
            setup_step("enable_event_canfd", lambda: com.enable_event_canfd(False))
            setup_step("enable_event_can", lambda: com.enable_event_can(True))
        except Exception:
            if com is not None:
                try:
                    com.enable_event_can(False)
                except Exception:
                    pass
            if event_sink is not None:
                try:
                    event_sink.close()
                except Exception:
                    pass
            if app is not None:
                try:
                    app.disconnect()
                except Exception:
                    pass
            pythoncom.CoUninitialize()
            raise

        self._pythoncom = pythoncom
        self._app = app
        self._com = com
        self._event_sink = event_sink
        self._config = config
        self.mapping_description = mapping_description

    def disconnect(self) -> None:
        if self._app is None:
            return
        first_error: Exception | None = None
        try:
            if self._com is not None:
                try:
                    self._com.enable_event_can(False)
                except Exception as exc:
                    first_error = exc
            if self._event_sink is not None:
                try:
                    self._event_sink.close()
                except Exception as exc:
                    first_error = first_error or exc
            try:
                self._app.disconnect()
            except Exception as exc:
                first_error = first_error or exc
        finally:
            self._event_sink = None
            self._com = None
            self._app = None
            self._config = None
            self.mapping_description = None
            if self._pythoncom is not None:
                self._pythoncom.CoUninitialize()
            self._pythoncom = None
        if first_error is not None:
            raise RuntimeError(f"TSMaster disconnect failed: {first_error}") from first_error

    def send(self, frame: CanFrame) -> None:
        if self._app is None or self._com is None or self._pythoncom is None:
            raise RuntimeError("TSMaster COM backend is not connected")
        if len(frame.data) > 8:
            raise ValueError("Classic CAN frame cannot contain more than 8 bytes")
        self._com.transmit_can_async_verbose(
            frame.channel,
            False,
            frame.is_extended,
            len(frame.data),
            frame.can_id,
            _format_com_data(frame.data),
        )

    def receive(self) -> CanFrame | None:
        if self._pythoncom is None or self._event_sink is None or self._config is None:
            return None
        self._pythoncom.PumpWaitingMessages()
        if self._event_sink.errors:
            raise self._event_sink.errors.popleft()
        while self._event_sink.frames:
            frame = self._event_sink.frames.popleft()
            if frame.channel == self._config.channel:
                return frame
        return None
