"""The "Find receivers" dialog: sweeps ports and baud rates, reports what is there."""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import serial

from ..device import list_serial_ports
from ..scan import (
    BAUD_ORDER,
    OUTCOME_TEXT,
    Outcome,
    ScanResult,
    describe_findings,
    probe_port,
    rank,
)

OUTCOME_COLOURS = {
    Outcome.ERROR: QColor("#8a8a8a"),
    Outcome.SILENT: QColor("#8a8a8a"),
    Outcome.NOT_NMEA: QColor("#c08a2f"),
    Outcome.NMEA_ONLY: QColor("#2f7fd0"),
    Outcome.CONFIGURABLE: QColor("#2f9e5f"),
}


class ScanWorker(QThread):
    """Runs the probe off the GUI thread, one port at a time."""

    progress = Signal(str, int, int, int)  # port, baud, index, total
    found = Signal(object)  # ScanResult
    done = Signal(list)  # list[ScanResult]

    def __init__(self, ports: list, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._ports = ports
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D102
        results: list[ScanResult] = []
        total = len(self._ports)
        for index, info in enumerate(self._ports):
            if self._stop:
                break
            result = probe_port(
                serial,
                info.device,
                info.description,
                bauds=BAUD_ORDER,
                should_stop=lambda: self._stop,
                on_progress=lambda port, baud, i=index: self.progress.emit(
                    port, baud, i, total
                ),
            )
            results.append(result)
            self.found.emit(result)
        self.done.emit(results)


class ScanDialog(QDialog):
    """Sweeps every serial port and baud rate, and says what each one is.

    The result column distinguishes a receiver that can be *configured* from one
    that merely *streams*, because on this hardware family that is a real and
    consequential difference and nothing else in the UI would reveal it until
    you tried to write a setting and got silence.
    """

    #: Emitted when the user picks a device to connect to.
    device_chosen = Signal(str, int)  # port, baud

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Find GNSS receivers")
        self.resize(900, 460)

        self._results: list[ScanResult] = []
        self._worker: ScanWorker | None = None

        layout = QVBoxLayout(self)

        blurb = QLabel(
            "Opens every serial port and tries each baud rate until NMEA decodes, then asks "
            "the receiver a harmless question (PMTK605) to find out whether it answers "
            "commands at all. Nothing is written to any device.",
            self,
        )
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        options = QHBoxLayout()
        self.include_legacy = QCheckBox("Also scan built-in serial ports (ttyS*)", self)
        self.include_legacy.setToolTip(
            "Off by default: this machine exposes dozens of legacy ttyS ports that almost "
            "never have anything attached, and each one costs several seconds to rule out."
        )
        options.addWidget(self.include_legacy)
        options.addStretch(1)
        layout.addLayout(options)

        self.progress = QProgressBar(self)
        self.progress.setTextVisible(True)
        self.progress.setFormat("Ready")
        layout.addWidget(self.progress)

        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["Port", "Device", "Result", "Details"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemDoubleClicked.connect(lambda _: self._use_selected())
        layout.addWidget(self.table, 1)

        self.summary = QLabel("", self)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        buttons = QDialogButtonBox(self)
        self.scan_button = QPushButton("Scan", self)
        self.scan_button.setDefault(True)
        self.scan_button.clicked.connect(self._toggle_scan)
        buttons.addButton(self.scan_button, QDialogButtonBox.ButtonRole.ActionRole)

        self.use_button = QPushButton("Connect to selected", self)
        self.use_button.setEnabled(False)
        self.use_button.clicked.connect(self._use_selected)
        buttons.addButton(self.use_button, QDialogButtonBox.ButtonRole.AcceptRole)

        close = QPushButton("Close", self)
        close.clicked.connect(self.reject)
        buttons.addButton(close, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(buttons)

    # -- scanning --------------------------------------------------------

    def _ports_to_scan(self) -> list:
        ports = list_serial_ports()
        if not self.include_legacy.isChecked():
            # Keep anything with USB identification, drop the legacy 8250 ports.
            ports = [p for p in ports if p.vid is not None or "ttyS" not in p.device]
        return ports

    def _toggle_scan(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self.scan_button.setText("Scan")
            self.progress.setFormat("Cancelled")
            return
        self.start_scan()

    def start_scan(self) -> None:
        ports = self._ports_to_scan()
        self.table.setRowCount(0)
        self._results = []
        self.summary.setText("")
        self.use_button.setEnabled(False)

        if not ports:
            self.progress.setFormat("No serial ports found")
            self.summary.setText(
                "No serial ports are present at all. Plug the receiver in, then scan again."
            )
            return

        self.progress.setRange(0, len(ports))
        self.progress.setValue(0)
        self.scan_button.setText("Stop")

        self._worker = ScanWorker(ports, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.found.connect(self._on_found)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_progress(self, port: str, baud: int, index: int, total: int) -> None:
        self.progress.setValue(index)
        self.progress.setFormat(f"Port {index + 1} of {total}: {port} at {baud} baud")

    def _on_found(self, result: ScanResult) -> None:
        self._results.append(result)
        self._repopulate()

    def _on_done(self, results: list) -> None:
        self.scan_button.setText("Scan again")
        self.progress.setValue(self.progress.maximum())
        self.progress.setFormat("Scan complete")
        self._results = list(results) or self._results
        self._repopulate()

        text = describe_findings(self._results)
        self.summary.setText(text)
        usable = [r for r in rank(self._results) if r.is_usable]
        if usable:
            # Preselect the best candidate so Enter does the obvious thing.
            self.table.selectRow(0)
            self.summary.setStyleSheet(
                "color: #2f9e5f;"
                if usable[0].outcome is Outcome.CONFIGURABLE
                else "color: #c08a2f;"
            )
        else:
            self.summary.setStyleSheet("color: #c03f3f;")

    def _repopulate(self) -> None:
        ordered = rank(self._results)
        self.table.setRowCount(len(ordered))
        for row, result in enumerate(ordered):
            colour = OUTCOME_COLOURS[result.outcome]
            cells = [
                result.port,
                result.description or "-",
                OUTCOME_TEXT[result.outcome]
                + (f" @ {result.baud}" if result.baud else ""),
                result.summary,
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column == 2:
                    item.setForeground(colour)
                # The full text on every cell: the Details column is the one
                # that matters and it is also the one most likely to be clipped.
                item.setToolTip(result.summary)
                item.setData(Qt.ItemDataRole.UserRole, row)
                self.table.setItem(row, column, item)

        header = self.table.horizontalHeader()
        self.table.resizeColumnsToContents()
        # Device descriptions can be absurdly long (the CP2102 repeats its own
        # name), which would squeeze Details down to an ellipsis. Cap it.
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(1, min(self.table.columnWidth(1), 260))
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

    # -- selection -------------------------------------------------------

    def _selected(self) -> ScanResult | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        ordered = rank(self._results)
        index = rows[0].row()
        return ordered[index] if 0 <= index < len(ordered) else None

    def _selection_changed(self) -> None:
        result = self._selected()
        self.use_button.setEnabled(bool(result and result.is_usable))

    def _use_selected(self) -> None:
        result = self._selected()
        if result is None or not result.is_usable or result.baud is None:
            return
        self.device_chosen.emit(result.port, result.baud)
        self.accept()

    # -- lifecycle -------------------------------------------------------

    def reject(self) -> None:  # noqa: D102
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(3000)
        super().reject()

    def accept(self) -> None:  # noqa: D102
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(3000)
        super().accept()
