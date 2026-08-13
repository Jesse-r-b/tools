"""Raw NMEA console, command sender and the built-in PMTK reference.

Everything the other panes do goes through here as well, so the console is the
record of what was actually sent and received.  Transmitted lines are marked and
coloured differently from received ones -- a log where you cannot tell your own
traffic from the device's is worthless for diagnosis.
"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import pmtk
from ..pmtk import COMMAND_CATALOGUE
from .common import Pane, Section, monospace

#: Lines kept in the console before the oldest are dropped.  At 10 Hz with the
#: full sentence set this is roughly ten minutes of traffic.
MAX_LINES = 20000

TX_COLOUR = QColor("#2f7fd0")
ERROR_COLOUR = QColor("#c03f3f")
PMTK_COLOUR = QColor("#7a5fc0")


class ConsolePane(Pane):
    """Raw traffic view plus a command entry box."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._paused = False
        self._history: list[str] = []
        self._history_index = 0
        self._log_file = None

        self.body.addWidget(self._build_console())
        self.body.addWidget(self._build_sender())
        self.body.addWidget(self._build_reference())

    # -- construction ----------------------------------------------------

    def _build_console(self) -> Section:
        section = Section(
            "Traffic",
            "Everything on the wire. Transmitted lines are prefixed with > and shown in blue; "
            "sentences that failed their checksum are shown in red and are not decoded by any "
            "other pane.",
        )

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(MAX_LINES)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.view.setMinimumHeight(260)
        monospace(self.view)
        section.add_widget(self.view)

        controls = QWidget()
        row = QHBoxLayout(controls)
        row.setContentsMargins(0, 0, 0, 0)

        self.pause_button = QPushButton("Pause")
        self.pause_button.setCheckable(True)
        self.pause_button.toggled.connect(self._toggle_pause)
        row.addWidget(self.pause_button)

        clear = QPushButton("Clear")
        clear.clicked.connect(self.view.clear)
        row.addWidget(clear)

        self.timestamps_check = QCheckBox("Timestamps")
        self.timestamps_check.setChecked(True)
        row.addWidget(self.timestamps_check)

        self.hide_nmea_check = QCheckBox("PMTK only")
        self.hide_nmea_check.setToolTip("Hide the navigation sentences and show only PMTK traffic")
        row.addWidget(self.hide_nmea_check)

        row.addStretch(1)

        self.log_button = QPushButton("Log to file...")
        self.log_button.setCheckable(True)
        self.log_button.toggled.connect(self._toggle_logging)
        row.addWidget(self.log_button)

        save = QPushButton("Save view...")
        save.clicked.connect(self._save_view)
        row.addWidget(save)

        section.add_widget(controls)

        self.log_label = QLabel("Not logging")
        self.log_label.setStyleSheet("color: palette(mid);")
        section.add_widget(self.log_label)
        return section

    def _build_sender(self) -> Section:
        section = Section(
            "Send a command",
            "Type a payload without the leading $ and without the checksum - both are added "
            "for you. Up and down arrows step through what you have sent.",
        )

        entry = QWidget()
        row = QHBoxLayout(entry)
        row.setContentsMargins(0, 0, 0, 0)

        self.command_combo = QComboBox()
        self.command_combo.setMinimumWidth(320)
        self.command_combo.addItem("-- pick a documented command --", "")
        for info in COMMAND_CATALOGUE:
            self.command_combo.addItem(
                f"PMTK{info.packet:03d}  {info.name}", info.example.lstrip("$")
            )
        self.command_combo.currentIndexChanged.connect(self._command_chosen)
        row.addWidget(self.command_combo)

        self.input = _HistoryLineEdit(self)
        self.input.setPlaceholderText("PMTK605")
        monospace(self.input)
        self.input.returnPressed.connect(self._send_typed)
        row.addWidget(self.input, 1)

        send = QPushButton("Send")
        send.clicked.connect(self._send_typed)
        row.addWidget(send)

        section.add_widget(entry)

        self.preview_label = QLabel("--")
        monospace(self.preview_label)
        self.preview_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        section.add_row("Will send", self.preview_label)
        self.input.textChanged.connect(self._update_preview)
        return section

    def _build_reference(self) -> Section:
        section = Section(
            "PMTK reference",
            "Every packet type in the MT3333 NMEA Message Specification V1.00. "
            "Double-click a row to load its example into the send box.",
        )

        self.reference = QTableWidget(len(COMMAND_CATALOGUE), 5, self)
        self.reference.setHorizontalHeaderLabels(
            ["Packet", "Name", "Purpose", "Example", "Section"]
        )
        self.reference.verticalHeader().setVisible(False)
        self.reference.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.reference.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.reference.setAlternatingRowColors(True)
        self.reference.setMinimumHeight(240)
        for row, info in enumerate(COMMAND_CATALOGUE):
            cells = [
                f"PMTK{info.packet:03d}",
                info.name,
                info.summary,
                info.example,
                info.section,
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column == 3:
                    item.setToolTip(
                        "Fields: " + ", ".join(info.fields) if info.fields else "No parameters"
                    )
                self.reference.setItem(row, column, item)
        header = self.reference.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.reference.itemDoubleClicked.connect(self._reference_chosen)
        section.add_widget(self.reference)

        note = QLabel(
            "Four of the specification's own printed examples carry wrong checksums, and one "
            "has the wrong number of fields. The examples above are reproduced as printed; "
            "this tool always recomputes the checksum before sending. "
            "See docs/spec-errata.md."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        section.add_widget(note)
        return section

    # -- console ---------------------------------------------------------

    def _toggle_pause(self, paused: bool) -> None:
        self._paused = paused
        self.pause_button.setText("Resume" if paused else "Pause")

    def append_rx(self, line: str) -> None:
        parsed = pmtk.parse(line)
        bad = parsed is not None and parsed.checksum_state is pmtk.ChecksumState.BAD
        is_pmtk = parsed is not None and parsed.is_pmtk
        if self.hide_nmea_check.isChecked() and not is_pmtk and not bad:
            self._write_log(line, transmitted=False)
            return
        colour = ERROR_COLOUR if bad else (PMTK_COLOUR if is_pmtk else None)
        suffix = "   <- CHECKSUM FAILED" if bad else ""
        self._append(f"  {line}{suffix}", colour)
        self._write_log(line, transmitted=False)

    def append_tx(self, line: str) -> None:
        self._append(f"> {line}", TX_COLOUR)
        self._write_log(line, transmitted=True)

    def append_note(self, text: str) -> None:
        """Add a tool-generated annotation, clearly marked as not being wire traffic."""
        self._append(f"# {text}", QColor("#8a8a8a"))
        self._write_log(f"# {text}", transmitted=None)

    def _append(self, text: str, colour: QColor | None) -> None:
        if self._paused:
            return
        if self.timestamps_check.isChecked():
            text = f"{time.strftime('%H:%M:%S')}.{int(time.time() % 1 * 1000):03d} {text}"

        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        if colour is not None:
            fmt.setForeground(colour)
        cursor.insertText(text + "\n", fmt)

        scrollbar = self.view.verticalScrollBar()
        # Only auto-scroll if the user is already at the bottom, so scrolling
        # back to read something is not yanked away by incoming traffic.
        if scrollbar.value() >= scrollbar.maximum() - 4:
            scrollbar.setValue(scrollbar.maximum())

    # -- logging ---------------------------------------------------------

    def _toggle_logging(self, enabled: bool) -> None:
        if not enabled:
            self._close_log()
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Log raw traffic to", "v800-traffic.log", "Log files (*.log *.txt);;All files (*)"
        )
        if not path:
            self.log_button.setChecked(False)
            return
        try:
            self._log_file = open(path, "a", encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Could not open log", str(exc))
            self.log_button.setChecked(False)
            return
        # Stamp the file so a log found later can be told apart from an older
        # one, and so it is obvious which tool produced it.
        self._log_file.write(
            f"# columbus-v800-config raw traffic log, opened "
            f"{time.strftime('%Y-%m-%d %H:%M:%S %z')}\n"
        )
        self._log_file.flush()
        self.log_label.setText(f"Logging to {path}")
        self.log_button.setText("Stop logging")

    def _close_log(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.write(
                    f"# closed {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n"
                )
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None
        self.log_label.setText("Not logging")
        self.log_button.setText("Log to file...")

    def _write_log(self, line: str, transmitted: bool | None) -> None:
        if self._log_file is None:
            return
        marker = "#" if transmitted is None else (">" if transmitted else "<")
        try:
            self._log_file.write(f"{time.time():.3f} {marker} {line}\n")
        except OSError:
            self._close_log()

    def _save_view(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save console contents", "v800-console.txt", "Text files (*.txt);;All files (*)"
        )
        if not path:
            return
        try:
            Path(path).write_text(self.view.toPlainText(), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))

    # -- sending ---------------------------------------------------------

    def _command_chosen(self) -> None:
        payload = self.command_combo.currentData()
        if payload:
            self.input.setText(payload.split("*")[0])

    def _reference_chosen(self, item) -> None:
        info = COMMAND_CATALOGUE[item.row()]
        self.input.setText(info.example.lstrip("$").split("*")[0])
        self.input.setFocus()

    def _update_preview(self, text: str) -> None:
        text = text.strip()
        if not text:
            self.preview_label.setText("--")
            return
        framed = pmtk.build(text).decode("ascii").rstrip("\r\n")
        packet = None
        parsed = pmtk.parse(framed)
        if parsed is not None:
            packet = parsed.packet_type
        description = f"    ({pmtk.describe(packet)})" if packet is not None else ""
        self.preview_label.setText(framed + description)

    def _send_typed(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self._history.append(text)
        self._history_index = len(self._history)
        self.send(text, "manual command")
        self.input.clear()

    def history(self) -> list[str]:
        return self._history

    def closeEvent(self, event) -> None:  # noqa: D102, N802
        self._close_log()
        super().closeEvent(event)


class _HistoryLineEdit(QLineEdit):
    """A line edit whose up/down arrows step through the console's history."""

    def __init__(self, pane: ConsolePane) -> None:
        super().__init__(pane)
        self._pane = pane

    def keyPressEvent(self, event) -> None:  # noqa: D102, N802
        history = self._pane.history()
        if event.key() == Qt.Key.Key_Up and history:
            self._pane._history_index = max(0, self._pane._history_index - 1)
            self.setText(history[self._pane._history_index])
            return
        if event.key() == Qt.Key.Key_Down and history:
            self._pane._history_index = min(len(history), self._pane._history_index + 1)
            self.setText(
                history[self._pane._history_index]
                if self._pane._history_index < len(history)
                else ""
            )
            return
        super().keyPressEvent(event)
