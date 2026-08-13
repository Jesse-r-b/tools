"""Shared widgets and conventions for the configuration panes.

Every pane follows the same rule: **show what the receiver reports, not what we
asked it to be.**  A write is followed by the matching query, and the pane's
fields are only updated from the query's answer.  :class:`ReadWriteBar` gives
each pane the two buttons that make that explicit, and :class:`StatusPill` shows
whether the displayed values came from the device or are unconfirmed edits.
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class State(Enum):
    """Where the values currently shown in a pane came from."""

    UNKNOWN = "unknown"
    """Nothing has been read from the device yet."""

    CONFIRMED = "confirmed"
    """The device was queried and reported these values."""

    EDITED = "edited"
    """The user has changed a field but not written it."""

    WRITTEN = "written"
    """Written and acknowledged, but not yet confirmed by a read-back."""

    FAILED = "failed"
    """The device rejected the write or did not answer."""


_STATE_TEXT = {
    State.UNKNOWN: "not read from device",
    State.CONFIRMED: "confirmed by device",
    State.EDITED: "edited - not written",
    State.WRITTEN: "written - awaiting read-back",
    State.FAILED: "write failed",
}

_STATE_COLOUR = {
    State.UNKNOWN: QColor("#8a8a8a"),
    State.CONFIRMED: QColor("#2f9e5f"),
    State.EDITED: QColor("#c08a2f"),
    State.WRITTEN: QColor("#2f7fd0"),
    State.FAILED: QColor("#c03f3f"),
}


class StatusPill(QWidget):
    """A small coloured dot plus text saying where the shown values came from."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = State.UNKNOWN
        self._detail = ""
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(20)

    def set_state(self, state: State, detail: str = "") -> None:
        self._state = state
        self._detail = detail
        self.setToolTip(detail)
        self.update()

    @property
    def state(self) -> State:
        return self._state

    def paintEvent(self, event) -> None:  # noqa: D102, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colour = _STATE_COLOUR[self._state]
        radius = 5.0
        cy = self.height() / 2.0
        painter.setBrush(colour)
        painter.setPen(QPen(colour.darker(140), 1.0))
        painter.drawEllipse(int(4), int(cy - radius), int(radius * 2), int(radius * 2))

        text = _STATE_TEXT[self._state]
        if self._detail:
            text = f"{text} - {self._detail}"
        painter.setPen(QPen(self.palette().windowText().color()))
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1))
        painter.setFont(font)
        painter.drawText(
            int(4 + radius * 2 + 6),
            0,
            self.width() - int(4 + radius * 2 + 6),
            self.height(),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            text,
        )
        painter.end()


class ReadWriteBar(QWidget):
    """The "Read from device" / "Write to device" pair every pane carries."""

    read_requested = Signal()
    write_requested = Signal()

    def __init__(self, write_text: str = "Write to device", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.pill = StatusPill(self)
        layout.addWidget(self.pill, 1)

        self.read_button = QPushButton("Read from device", self)
        self.read_button.setToolTip("Query the receiver and replace the fields below with its answer")
        self.read_button.clicked.connect(self.read_requested)
        layout.addWidget(self.read_button)

        self.write_button = QPushButton(write_text, self)
        self.write_button.setToolTip("Send the settings below, then read them back to confirm")
        self.write_button.clicked.connect(self.write_requested)
        layout.addWidget(self.write_button)

    def set_state(self, state: State, detail: str = "") -> None:
        self.pill.set_state(state, detail)

    def set_enabled(self, enabled: bool) -> None:
        self.read_button.setEnabled(enabled)
        self.write_button.setEnabled(enabled)


class Section(QGroupBox):
    """A titled group with a form layout and an optional explanatory note."""

    def __init__(self, title: str, note: str = "", parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        outer = QVBoxLayout(self)
        self.note_label = None
        if note:
            self.note_label = label = QLabel(note, self)
            label.setWordWrap(True)
            font = QFont(label.font())
            font.setPointSizeF(max(7.5, font.pointSizeF() - 1))
            label.setFont(font)
            label.setStyleSheet("color: palette(mid);")
            outer.addWidget(label)
        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        outer.addLayout(self.form)

    def set_note(self, text: str) -> None:
        """Replace the explanatory note, e.g. when the active protocol changes."""
        if self.note_label is not None:
            self.note_label.setText(text)

    def add_row(self, label: str, widget: QWidget) -> QWidget:
        self.form.addRow(label, widget)
        return widget

    def add_widget(self, widget: QWidget) -> QWidget:
        self.form.addRow(widget)
        return widget


class Pane(QScrollArea):
    """Base class for a configuration tab.

    Scrollable so panes stay usable on a small window, and with a vertical box
    layout available as ``self.body`` for subclasses to fill.
    """

    #: Emitted with a payload the device should send.
    command = Signal(str, str)  # payload, description

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget(self)
        self.body = QVBoxLayout(container)
        self.body.setContentsMargins(12, 12, 12, 12)
        self.body.setSpacing(12)
        self.setWidget(container)

    #: Emitted with pre-framed bytes for protocols that are not NMEA sentences.
    raw_command = Signal(bytes, str)

    def send(self, payload: str, description: str = "") -> None:
        self.command.emit(payload, description)

    def send_bytes(self, payload: bytes, description: str = "") -> None:
        self.raw_command.emit(payload, description)

    def require(self, protocol, *capabilities) -> bool:
        """Show or hide the unsupported banner for this pane.

        Returns True if the pane is usable. The banner names the protocol and
        the missing operation rather than saying "unsupported", because the
        useful question is always *why* -- a receiver that speaks a different
        language is a different problem from one that is broken.
        """
        missing = protocol.missing(*capabilities)
        if not missing:
            self._set_unsupported(None)
            return True
        names = ", ".join(c.value for c in missing)
        self._set_unsupported(
            f"This receiver speaks {protocol.name}, which this tool cannot use for "
            f"{names}. The controls below are disabled because sending them would "
            f"change nothing. Reading and diagnostics are unaffected."
        )
        return False

    def _set_unsupported(self, message: str | None) -> None:
        banner = getattr(self, "_unsupported_banner", None)
        if message is None:
            if banner is not None:
                banner.setVisible(False)
            self._set_content_enabled(True)
            return
        if banner is None:
            banner = WrapLabel("", self.widget())
            banner.setStyleSheet(
                "QLabel { background: palette(alternate-base); color: #c08a2f;"
                " border: 1px solid #c08a2f; border-radius: 4px; padding: 8px; }"
            )
            self.body.insertWidget(0, banner)
            self._unsupported_banner = banner
        banner.setText(message)
        banner.setVisible(True)
        self._set_content_enabled(False)

    def _set_content_enabled(self, enabled: bool) -> None:
        """Enable/disable everything except the unsupported banner itself."""
        banner = getattr(self, "_unsupported_banner", None)
        for index in range(self.body.count()):
            item = self.body.itemAt(index)
            widget = item.widget() if item is not None else None
            if widget is not None and widget is not banner:
                widget.setEnabled(enabled)

    def on_protocol(self, protocol) -> None:
        """Called when the active command protocol is identified or changes."""

    # Subclasses override these.

    def on_sentence(self, sentence) -> None:
        """Called for every checksum-valid sentence received."""

    def on_ack(self, ack) -> None:
        """Called for every PMTK001 acknowledgement."""

    def on_connected(self, is_connected: bool) -> None:
        """Called when the link opens or closes."""

    def read_from_device(self) -> None:
        """Query everything this pane displays."""


class WrapLabel(QLabel):
    """A word-wrapping label that actually reserves the height it needs.

    ``QLabel`` with ``wordWrap`` set reports a ``sizeHint`` for a single line,
    so inside a ``QFormLayout`` or ``QGridLayout`` any text that wraps gets
    clipped by the row height.  For status text that is exactly backwards: the
    longest messages are the ones explaining that something went wrong, and
    those are the ones that end up half-visible.

    Implementing ``heightForWidth`` and reporting it from ``sizeHint`` makes the
    layout allocate the real height.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        policy.setVerticalPolicy(QSizePolicy.Policy.MinimumExpanding)
        self.setSizePolicy(policy)

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        metrics = self.fontMetrics()
        rect = metrics.boundingRect(
            0, 0, max(width, 1), 0,
            int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignLeft),
            self.text(),
        )
        return rect.height() + 2

    def _apply_height(self) -> None:
        """Pin the minimum height to what the current text actually needs.

        ``QFormLayout`` does not reliably consult ``heightForWidth``, so relying
        on it alone still clips long text.  Setting an explicit minimum height
        whenever the text or the width changes is deterministic, and the whole
        point of these labels is that the *long* messages are the important
        ones.
        """
        if self.width() > 0:
            self.setMinimumHeight(self.heightForWidth(self.width()))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_height()

    def setText(self, text: str) -> None:  # noqa: N802
        super().setText(text)
        self._apply_height()
        self.updateGeometry()


def hint(text: str) -> QLabel:
    """A small, dimmed explanatory label."""
    label = QLabel(text)
    label.setWordWrap(True)
    font = QFont(label.font())
    font.setPointSizeF(max(7.5, font.pointSizeF() - 1))
    label.setFont(font)
    label.setStyleSheet("color: palette(mid);")
    return label


def monospace(widget: QWidget) -> QWidget:
    """Give a widget a fixed-pitch font, for anything showing raw sentences."""
    font = QFont("Monospace")
    font.setStyleHint(QFont.StyleHint.TypeWriter)
    font.setPointSizeF(max(8.0, widget.font().pointSizeF() - 0.5))
    widget.setFont(font)
    return widget
