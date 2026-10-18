from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QMetaObject, QThread, Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config import ConnectionConfig
from ..workers.can_worker import CanWorker


class MainWindow(QMainWindow):
    connect_requested = Signal(str, object)
    disconnect_requested = Signal()
    tc1001_requested = Signal()
    apply_requested = Signal(float)

    def __init__(self, *, initial_backend: str = "mock") -> None:
        super().__init__()
        self.setWindowTitle("J1939 Pattern Generator - Engine Speed")
        self.resize(860, 620)
        self._connected = False
        self._ready = False

        self._build_ui(initial_backend)
        self._thread = QThread(self)
        self._worker = CanWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.start)
        self.connect_requested.connect(self._worker.connect_backend)
        self.disconnect_requested.connect(self._worker.disconnect_backend)
        self.tc1001_requested.connect(self._worker.send_tc1001)
        self.apply_requested.connect(self._worker.apply_engine_speed)
        self._worker.connected.connect(self._on_connected)
        self._worker.disconnected.connect(self._on_disconnected)
        self._worker.connection_failed.connect(self._on_connection_failed)
        self._worker.stm32_ready.connect(self._on_stm32_ready)
        self._worker.command_result.connect(self._on_command_result)
        self._worker.readback_changed.connect(self._on_readback_changed)
        self._worker.log_message.connect(self._append_log)
        self._thread.start()
        self._set_state("Disconnected", "#9b1c1c")

    def _build_ui(self, initial_backend: str) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)

        connection_group = QGroupBox("Connection")
        connection_layout = QGridLayout(connection_group)
        self.backend_combo = QComboBox()
        self.backend_combo.addItem("Mock (offline)", "mock")
        self.backend_combo.addItem("TSMaster Native API", "tsmaster")
        index = self.backend_combo.findData(initial_backend)
        self.backend_combo.setCurrentIndex(max(index, 0))
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
        self.check_button.setEnabled(False)
        self.check_button.clicked.connect(self.tc1001_requested.emit)
        self.state_label = QLabel()

        connection_layout.addWidget(QLabel("Backend"), 0, 0)
        connection_layout.addWidget(self.backend_combo, 0, 1)
        connection_layout.addWidget(QLabel("CAN channel (zero-based)"), 0, 2)
        connection_layout.addWidget(self.channel_spin, 0, 3)
        connection_layout.addWidget(QLabel("Bitrate"), 1, 0)
        connection_layout.addWidget(self.bitrate_combo, 1, 1)
        connection_layout.addWidget(QLabel("PC source address (hex)"), 1, 2)
        connection_layout.addWidget(self.pc_address_edit, 1, 3)
        connection_layout.addWidget(QLabel("STM32 address (hex)"), 2, 0)
        connection_layout.addWidget(self.stm32_address_edit, 2, 1)
        connection_layout.addWidget(self.connect_button, 2, 2)
        connection_layout.addWidget(self.check_button, 2, 3)
        connection_layout.addWidget(QLabel("State"), 3, 0)
        connection_layout.addWidget(self.state_label, 3, 1, 1, 3)

        signal_group = QGroupBox("Engine Speed - EEC1 / PGN 61444 / SPN 190 / key 103")
        signal_layout = QFormLayout(signal_group)
        value_row = QHBoxLayout()
        self.engine_speed_spin = QDoubleSpinBox()
        self.engine_speed_spin.setRange(0.0, 8031.875)
        self.engine_speed_spin.setDecimals(3)
        self.engine_speed_spin.setSingleStep(0.125)
        self.engine_speed_spin.setValue(1500.0)
        self.apply_button = QPushButton("Apply")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(
            lambda: self.apply_requested.emit(self.engine_speed_spin.value())
        )
        value_row.addWidget(self.engine_speed_spin)
        value_row.addWidget(QLabel("rpm"))
        value_row.addWidget(self.apply_button)
        self.readback_label = QLabel("-")
        self.readback_status_label = QLabel("No data")
        self.last_received_label = QLabel("-")
        signal_layout.addRow("Set value", value_row)
        signal_layout.addRow("Readback", self.readback_label)
        signal_layout.addRow("Readback state", self.readback_status_label)
        signal_layout.addRow("Last received", self.last_received_label)

        log_group = QGroupBox("Log / Status")
        log_layout = QVBoxLayout(log_group)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.document().setMaximumBlockCount(1000)
        log_layout.addWidget(self.log_edit)

        layout.addWidget(connection_group)
        layout.addWidget(signal_group)
        layout.addWidget(log_group, 1)
        self.setCentralWidget(central)

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
    def _on_connected(self) -> None:
        self._connected = True
        self._ready = False
        self.connect_button.setText("Disconnect")
        self.connect_button.setEnabled(True)
        self.check_button.setEnabled(True)
        self.backend_combo.setEnabled(False)
        self.apply_button.setEnabled(False)
        self._set_state("CAN Connected - verifying STM32...", "#0969a2")

    @Slot()
    def _on_disconnected(self) -> None:
        self._connected = False
        self._ready = False
        self.connect_button.setText("Connect")
        self.connect_button.setEnabled(True)
        self.check_button.setEnabled(False)
        self.backend_combo.setEnabled(True)
        self.apply_button.setEnabled(False)
        self._set_state("Disconnected", "#9b1c1c")

    @Slot(str)
    def _on_connection_failed(self, message: str) -> None:
        self._on_disconnected()
        self._set_state("Connection error", "#9b1c1c")
        QMessageBox.critical(self, "Connection failed", message)

    @Slot()
    def _on_stm32_ready(self) -> None:
        self._ready = True
        self.apply_button.setEnabled(True)
        self._set_state("STM32 Ready", "#1a7f37")

    @Slot(str, bool)
    def _on_command_result(self, message: str, succeeded: bool) -> None:
        self.statusBar().showMessage(message, 5000)
        self._append_log(("OK: " if succeeded else "ERROR: ") + message)
        if not succeeded and message.startswith("TC1001"):
            self._ready = False
            self.apply_button.setEnabled(False)
            self._set_state("CAN Connected - STM32 not verified", "#9a6700")

    @Slot(object, str)
    def _on_readback_changed(self, value: object, state: str) -> None:
        if value is None:
            self.readback_label.setText("-")
        else:
            self.readback_label.setText(f"{float(value):.3f} rpm")
            self.last_received_label.setText(datetime.now().astimezone().isoformat(timespec="seconds"))
        self.readback_status_label.setText(state)

    @Slot(str)
    def _append_log(self, message: str) -> None:
        now = datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3]
        self.log_edit.appendPlainText(f"{now}  {message}")

    def _set_state(self, text: str, color: str) -> None:
        self.state_label.setText(text)
        self.state_label.setStyleSheet(f"font-weight: 600; color: {QColor(color).name()};")

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._thread.isRunning():
            QMetaObject.invokeMethod(self._worker, "shutdown", Qt.BlockingQueuedConnection)
            self._thread.quit()
            self._thread.wait(3000)
        super().closeEvent(event)
