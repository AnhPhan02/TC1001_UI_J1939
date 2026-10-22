from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QMetaObject, QThread, Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import ConnectionConfig
from ..profiles import PROFILE_40_NAME, PROFILE_40_PGNS, PROFILE_40_SIGNALS
from ..protocol.commands import ProtocolInfo, RuntimeFlag, capability_names
from ..workers.can_worker import CanWorker


class MainWindow(QMainWindow):
    connect_requested = Signal(str, object)
    disconnect_requested = Signal()
    tc1001_requested = Signal()
    status_requested = Signal()
    stop_requested = Signal()
    profile_requested = Signal()
    retry_requested = Signal()
    start_recording_requested = Signal(str)
    stop_recording_requested = Signal()

    def __init__(self, *, initial_backend: str = "mock") -> None:
        super().__init__()
        self.setWindowTitle("J1939 Pattern Generator — P3-A 40 SPN")
        self.resize(1180, 820)
        self._connected = False
        self._ready = False
        self._batch_busy = False
        self._recording = False

        self._build_ui(initial_backend)
        self._thread = QThread(self)
        self._worker = CanWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.start)
        self.connect_requested.connect(self._worker.connect_backend)
        self.disconnect_requested.connect(self._worker.disconnect_backend)
        self.tc1001_requested.connect(self._worker.send_tc1001)
        self.status_requested.connect(self._worker.get_status)
        self.stop_requested.connect(self._worker.stop_transmission)
        self.profile_requested.connect(self._worker.start_profile_40)
        self.retry_requested.connect(self._worker.retry_failed_profile_command)
        self.start_recording_requested.connect(self._worker.start_asc_recording)
        self.stop_recording_requested.connect(self._worker.stop_asc_recording)
        self._worker.connected.connect(self._on_connected)
        self._worker.disconnected.connect(self._on_disconnected)
        self._worker.connection_failed.connect(self._on_connection_failed)
        self._worker.stm32_ready.connect(self._on_stm32_ready)
        self._worker.command_result.connect(self._on_command_result)
        self._worker.protocol_info_changed.connect(self._on_protocol_info_changed)
        self._worker.runtime_status_changed.connect(self._on_runtime_status_changed)
        self._worker.batch_status_changed.connect(self._on_batch_status_changed)
        self._worker.profile_signal_status_changed.connect(
            self._on_profile_signal_status_changed
        )
        self._worker.rx_rate_snapshot.connect(self._on_rx_rate_snapshot)
        self._worker.recording_status_changed.connect(self._on_recording_status_changed)
        self._worker.log_message.connect(self._append_log)
        self._thread.start()
        self._set_state("Disconnected", "#9b1c1c")
        self._update_action_buttons()

    def _build_ui(self, initial_backend: str) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)

        top_row = QHBoxLayout()
        connection_group = self._build_connection_group(initial_backend)
        protocol_group = self._build_protocol_group()
        top_row.addWidget(connection_group, 3)
        top_row.addWidget(protocol_group, 2)
        layout.addLayout(top_row)

        layout.addWidget(self._build_profile_group(), 3)

        tabs = QTabWidget()
        tabs.addTab(self._build_rx_monitor(), "RX rate monitor")
        tabs.addTab(self._build_log_panel(), "Command log")
        layout.addWidget(tabs, 2)
        self.setCentralWidget(central)

    def _build_connection_group(self, initial_backend: str) -> QGroupBox:
        group = QGroupBox("Connection")
        grid = QGridLayout(group)
        self.backend_combo = QComboBox()
        self.backend_combo.addItem("Mock (offline)", "mock")
        self.backend_combo.addItem("TSMaster Native API", "tsmaster")
        index = self.backend_combo.findData(initial_backend)
        self.backend_combo.setCurrentIndex(max(index, 0))
        self.backend_combo.currentIndexChanged.connect(self._update_action_buttons)
        self.channel_spin = QSpinBox()
        self.channel_spin.setRange(0, 31)
        self.bitrate_combo = QComboBox()
        for bitrate in (125, 250, 500, 1000):
            self.bitrate_combo.addItem(f"{bitrate} kbit/s", bitrate)
        self.bitrate_combo.setCurrentIndex(1)
        self.pc_address_edit = QLineEdit("F9")
        self.stm32_address_edit = QLineEdit("80")
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self._toggle_connection)
        self.check_button = QPushButton("Retry STM32 Check")
        self.check_button.clicked.connect(self.tc1001_requested.emit)
        self.status_button = QPushButton("Get Status")
        self.status_button.clicked.connect(self.status_requested.emit)
        self.state_label = QLabel()

        grid.addWidget(QLabel("Backend"), 0, 0)
        grid.addWidget(self.backend_combo, 0, 1)
        grid.addWidget(QLabel("CAN channel"), 0, 2)
        grid.addWidget(self.channel_spin, 0, 3)
        grid.addWidget(QLabel("Bitrate"), 1, 0)
        grid.addWidget(self.bitrate_combo, 1, 1)
        grid.addWidget(QLabel("PC address (hex)"), 1, 2)
        grid.addWidget(self.pc_address_edit, 1, 3)
        grid.addWidget(QLabel("STM32 address (hex)"), 2, 0)
        grid.addWidget(self.stm32_address_edit, 2, 1)
        grid.addWidget(self.connect_button, 2, 2)
        grid.addWidget(self.check_button, 2, 3)
        grid.addWidget(QLabel("State"), 3, 0)
        grid.addWidget(self.state_label, 3, 1, 1, 2)
        grid.addWidget(self.status_button, 3, 3)
        return group

    def _build_protocol_group(self) -> QGroupBox:
        group = QGroupBox("STM32 Protocol")
        form = QFormLayout(group)
        self.protocol_version_label = QLabel("-")
        self.schema_label = QLabel("-")
        self.capabilities_label = QLabel("-")
        self.runtime_label = QLabel("Unknown")
        self.capabilities_label.setWordWrap(True)
        form.addRow("Version", self.protocol_version_label)
        form.addRow("Schema", self.schema_label)
        form.addRow("Capabilities", self.capabilities_label)
        form.addRow("Runtime", self.runtime_label)
        return group

    def _build_profile_group(self) -> QGroupBox:
        group = QGroupBox(PROFILE_40_NAME)
        layout = QVBoxLayout(group)

        controls = QHBoxLayout()
        self.batch_label = QLabel("Idle")
        self.batch_label.setMinimumWidth(155)
        self.progress = QProgressBar()
        self.progress.setRange(0, 40)
        self.progress.setFormat("%v / %m configured")
        self.start_profile_button = QPushButton("Configure 40 & Start")
        self.start_profile_button.clicked.connect(self._start_profile)
        self.retry_button = QPushButton("Retry failed command")
        self.retry_button.clicked.connect(self.retry_requested.emit)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_requested.emit)
        self.record_button = QPushButton("Start .asc recording")
        self.record_button.clicked.connect(self._toggle_recording)
        controls.addWidget(QLabel("Batch:"))
        controls.addWidget(self.batch_label)
        controls.addWidget(self.progress, 1)
        controls.addWidget(self.start_profile_button)
        controls.addWidget(self.retry_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.record_button)
        layout.addLayout(controls)

        self.reset_confirm = QCheckBox(
            "STM32 was reset/power-cycled and re-handshaken (configuration is empty)"
        )
        self.reset_confirm.toggled.connect(self._update_action_buttons)
        layout.addWidget(self.reset_confirm)
        warning = QLabel(
            "FW has no CLEAR_CONFIG: STOP does not remove key 103. Hardware mode is "
            "blocked until the reset confirmation above is checked."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #9a6700;")
        layout.addWidget(warning)

        self.signal_table = QTableWidget(len(PROFILE_40_SIGNALS), 8)
        self.signal_table.setHorizontalHeaderLabels(
            ["#", "Key", "PGN", "SPN", "Signal", "Value", "Raw", "Status"]
        )
        self.signal_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.signal_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.signal_table.verticalHeader().setVisible(False)
        header = self.signal_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        for row, signal in enumerate(PROFILE_40_SIGNALS):
            values = (
                str(row + 1),
                str(signal.signal_key),
                signal.pgn_name,
                str(signal.spn),
                signal.name,
                f"{signal.value} {signal.unit}",
                str(signal.raw_value),
                "Pending",
            )
            for column, value in enumerate(values):
                self.signal_table.setItem(row, column, QTableWidgetItem(value))
        layout.addWidget(self.signal_table)
        return group

    def _build_rx_monitor(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        summary = QHBoxLayout()
        self.rx_total_label = QLabel("Expected: 480 frame/s · Actual: 0.0 frame/s")
        self.rx_window_label = QLabel("Window: -")
        summary.addWidget(self.rx_total_label)
        summary.addStretch(1)
        summary.addWidget(self.rx_window_label)
        layout.addLayout(summary)

        self.rx_table = QTableWidget(len(PROFILE_40_PGNS), 7)
        self.rx_table.setHorizontalHeaderLabels(
            ["PGN name", "PGN", "Cycle", "Expected", "CAN ID(s)", "Actual", "State"]
        )
        self.rx_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.rx_table.verticalHeader().setVisible(False)
        self.rx_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for row, pgn in enumerate(PROFILE_40_PGNS):
            values = (
                pgn.name,
                f"0x{pgn.pgn:04X}",
                f"{pgn.period_ms} ms",
                f"{pgn.expected_fps} fps",
                "-",
                "0.0 fps",
                "Waiting",
            )
            for column, value in enumerate(values):
                self.rx_table.setItem(row, column, QTableWidgetItem(value))
        layout.addWidget(self.rx_table)
        return panel

    def _build_log_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.document().setMaximumBlockCount(1500)
        layout.addWidget(self.log_edit)
        return panel

    def _connection_config(self) -> ConnectionConfig:
        return ConnectionConfig(
            channel=self.channel_spin.value(),
            bitrate_kbps=int(self.bitrate_combo.currentData()),
            pc_address=int(self.pc_address_edit.text().strip(), 16),
            stm32_address=int(self.stm32_address_edit.text().strip(), 16),
        )

    @Slot()
    def _toggle_connection(self) -> None:
        if self._connected:
            self.disconnect_requested.emit()
            return
        try:
            config = self._connection_config()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid connection settings", str(exc))
            return
        self.connect_button.setEnabled(False)
        self._set_state("Connecting...", "#9a6700")
        self.connect_requested.emit(str(self.backend_combo.currentData()), config)

    @Slot()
    def _start_profile(self) -> None:
        if self.backend_combo.currentData() == "tsmaster" and not self.reset_confirm.isChecked():
            QMessageBox.warning(
                self,
                "Reset required",
                "STOP does not clear the existing configuration. Reset/power-cycle the "
                "STM32, reconnect/handshake, then check the confirmation box.",
            )
            return
        self.profile_requested.emit()

    @Slot()
    def _toggle_recording(self) -> None:
        if self._recording:
            self.stop_recording_requested.emit()
            return
        filename = datetime.now().astimezone().strftime("J1939_P3A_%Y%m%d_%H%M%S.asc")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save CAN trace", filename, "Vector ASCII trace (*.asc)"
        )
        if path:
            if not path.lower().endswith(".asc"):
                path += ".asc"
            self.start_recording_requested.emit(path)

    @Slot()
    def _on_connected(self) -> None:
        self._connected = True
        self._ready = False
        self.connect_button.setText("Disconnect")
        self.connect_button.setEnabled(True)
        self.backend_combo.setEnabled(False)
        self.reset_confirm.setChecked(self.backend_combo.currentData() == "mock")
        self._reset_device_display()
        self._set_state("CAN Connected — verifying STM32...", "#0969a2")
        self._update_action_buttons()

    @Slot()
    def _on_disconnected(self) -> None:
        self._connected = False
        self._ready = False
        self._batch_busy = False
        self.connect_button.setText("Connect")
        self.connect_button.setEnabled(True)
        self.backend_combo.setEnabled(True)
        self.reset_confirm.setChecked(False)
        self._reset_device_display()
        self._set_state("Disconnected", "#9b1c1c")
        self._update_action_buttons()

    @Slot(str)
    def _on_connection_failed(self, message: str) -> None:
        self._on_disconnected()
        self._set_state("Connection error", "#9b1c1c")
        QMessageBox.critical(self, "Connection failed", message)

    @Slot()
    def _on_stm32_ready(self) -> None:
        self._ready = True
        self._set_state("STM32 Ready", "#1a7f37")
        self._update_action_buttons()

    @Slot(str, bool)
    def _on_command_result(self, message: str, succeeded: bool) -> None:
        self.statusBar().showMessage(message, 5000)
        self._append_log(("OK: " if succeeded else "ERROR: ") + message)
        if not succeeded and (
            message.startswith("TC1001")
            or message.startswith("GET_STATUS")
            or message.startswith("Incompatible STM32")
        ):
            self._ready = False
            self._set_state("CAN Connected — STM32 not verified", "#9a6700")
            self._update_action_buttons()

    @Slot(object)
    def _on_protocol_info_changed(self, info: object) -> None:
        if not isinstance(info, ProtocolInfo):
            return
        self.protocol_version_label.setText(info.version)
        self.schema_label.setText(str(info.schema))
        names = capability_names(info.capabilities)
        self.capabilities_label.setText(", ".join(names) if names else "None")

    @Slot(object)
    def _on_runtime_status_changed(self, flags: object) -> None:
        runtime = RuntimeFlag(int(flags))
        state = "Running" if RuntimeFlag.RUNNING in runtime else "Stopped"
        configured = (
            "configuration present"
            if RuntimeFlag.ENGINE_SPEED_CONFIGURED in runtime
            else "no configuration reported"
        )
        self.runtime_label.setText(f"{state} — {configured}")

    @Slot(str, int, int)
    def _on_batch_status_changed(self, state: str, current: int, total: int) -> None:
        if state == "Configuring":
            self.batch_label.setText(f"Configuring {current}/{total}")
        else:
            self.batch_label.setText(state)
        self.progress.setMaximum(total)
        self.progress.setValue(current)
        self._batch_busy = state in {"Stopping", "Configuring", "Starting"}
        self.retry_button.setEnabled(state == "Failed")
        if state == "Failed" and self.backend_combo.currentData() == "tsmaster":
            # A full rerun after a partial batch is unsafe without CLEAR_CONFIG.
            # Exact-command retry remains available independently.
            self.reset_confirm.setChecked(False)
        colors = {
            "Running": "#1a7f37",
            "Failed": "#cf222e",
            "Stopping": "#9a6700",
            "Configuring": "#0969a2",
            "Starting": "#8250df",
        }
        self.batch_label.setStyleSheet(
            f"font-weight: 600; color: {colors.get(state, '#57606a')};"
        )
        self._update_action_buttons()

    @Slot(int, str, str)
    def _on_profile_signal_status_changed(
        self, index: int, status: str, detail: str
    ) -> None:
        item = self.signal_table.item(index, 7)
        if item is None:
            return
        item.setText(status)
        item.setToolTip(detail)
        colors = {
            "Pending": "#57606a",
            "Sending": "#0969a2",
            "Configured": "#1a7f37",
            "Error": "#cf222e",
            "Timeout": "#cf222e",
        }
        item.setForeground(QColor(colors.get(status, "#24292f")))

    @Slot(object)
    def _on_rx_rate_snapshot(self, snapshot: object) -> None:
        if not isinstance(snapshot, dict):
            return
        frames = snapshot.get("frames", [])
        matched_total = 0.0
        for row, expected in enumerate(PROFILE_40_PGNS):
            matches = [
                frame
                for frame in frames
                if frame.get("pgn") == expected.receive_pgn
                and (
                    expected.destination_address is None
                    or frame.get("destination_address") == expected.destination_address
                )
            ]
            actual = sum(float(frame.get("fps", 0.0)) for frame in matches)
            matched_total += actual
            can_ids = ", ".join(
                f"0x{int(frame['can_id']):08X}" for frame in matches
            ) or "-"
            tolerance = max(2.0, expected.expected_fps * 0.15)
            if actual == 0:
                state = "Missing"
                color = "#cf222e"
            elif abs(actual - expected.expected_fps) <= tolerance:
                state = "OK"
                color = "#1a7f37"
            elif actual < expected.expected_fps:
                state = "Low"
                color = "#9a6700"
            else:
                state = "High"
                color = "#9a6700"
            self.rx_table.item(row, 4).setText(can_ids)
            self.rx_table.item(row, 5).setText(f"{actual:.1f} fps")
            state_item = self.rx_table.item(row, 6)
            state_item.setText(state)
            state_item.setForeground(QColor(color))
        self.rx_total_label.setText(
            f"Expected: 480 frame/s · Matched actual: {matched_total:.1f} frame/s"
        )
        self.rx_window_label.setText(
            f"Window: {float(snapshot.get('window_seconds', 0.0)):.3f} s"
        )

    @Slot(bool, str)
    def _on_recording_status_changed(self, recording: bool, path: str) -> None:
        self._recording = recording
        self.record_button.setText(
            "Stop & save .asc" if recording else "Start .asc recording"
        )
        if path:
            self.statusBar().showMessage(f"Recording {path}", 5000)
        self._update_action_buttons()

    def _update_action_buttons(self) -> None:
        is_mock = self.backend_combo.currentData() == "mock"
        reset_ok = is_mock or self.reset_confirm.isChecked()
        self.check_button.setEnabled(self._connected and not self._batch_busy)
        self.status_button.setEnabled(self._connected and not self._batch_busy)
        self.start_profile_button.setEnabled(
            self._ready and not self._batch_busy and reset_ok
        )
        self.stop_button.setEnabled(self._ready and not self._batch_busy)
        self.record_button.setEnabled(self._connected or self._recording)
        if not self._batch_busy and self.batch_label.text() != "Failed":
            self.retry_button.setEnabled(False)

    def _reset_device_display(self) -> None:
        self.protocol_version_label.setText("-")
        self.schema_label.setText("-")
        self.capabilities_label.setText("-")
        self.runtime_label.setText("Unknown")
        self.batch_label.setText("Idle")
        self.progress.setValue(0)
        for row in range(self.signal_table.rowCount()):
            self._on_profile_signal_status_changed(row, "Pending", "")

    @Slot(str)
    def _append_log(self, message: str) -> None:
        now = datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3]
        self.log_edit.appendPlainText(f"{now}  {message}")

    def _set_state(self, text: str, color: str) -> None:
        self.state_label.setText(text)
        self.state_label.setStyleSheet(
            f"font-weight: 600; color: {QColor(color).name()};"
        )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._thread.isRunning():
            QMetaObject.invokeMethod(self._worker, "shutdown", Qt.BlockingQueuedConnection)
            self._thread.quit()
            self._thread.wait(3000)
        super().closeEvent(event)
