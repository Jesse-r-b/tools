"""The connection/health banner shown above the tabs.

Deliberately large and always visible.  The states it distinguishes -- port not
open, nothing arriving, arriving but not decoding, decoding but no satellites,
satellites but no lock, lock but no fix -- are the ones that otherwise all look
like "it isn't working", and each has a different fix.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..health import Health, Level

#: Colour per level.  Chosen to read on both light and dark palettes, and paired
#: with a distinct glyph so the state is not carried by colour alone.
LEVEL_COLOURS = {
    Level.IDLE: QColor("#8a8a8a"),
    Level.OK: QColor("#2f9e5f"),
    Level.INFO: QColor("#2f7fd0"),
    Level.WARN: QColor("#c08a2f"),
    Level.ERROR: QColor("#c03f3f"),
}

LEVEL_GLYPHS = {
    Level.IDLE: "○",      # hollow circle
    Level.OK: "✓",        # check
    Level.INFO: "…",      # ellipsis - in progress
    Level.WARN: "⚠",      # warning triangle
    Level.ERROR: "✕",     # cross
}


class HealthBanner(QWidget):
    """A coloured strip: glyph, headline, and the reason underneath."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._level = Level.IDLE
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setMinimumHeight(52)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(12)

        self.glyph = QLabel(LEVEL_GLYPHS[Level.IDLE], self)
        glyph_font = QFont(self.font())
        glyph_font.setPointSizeF(glyph_font.pointSizeF() + 9)
        glyph_font.setBold(True)
        self.glyph.setFont(glyph_font)
        self.glyph.setFixedWidth(28)
        self.glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.glyph)

        text_column = QVBoxLayout()
        text_column.setSpacing(1)

        self.headline = QLabel("Not connected", self)
        headline_font = QFont(self.font())
        headline_font.setBold(True)
        headline_font.setPointSizeF(headline_font.pointSizeF() + 1)
        self.headline.setFont(headline_font)
        text_column.addWidget(self.headline)

        self.detail = QLabel("Choose a port and press Connect.", self)
        self.detail.setWordWrap(True)
        detail_font = QFont(self.font())
        detail_font.setPointSizeF(max(7.5, detail_font.pointSizeF() - 0.5))
        self.detail.setFont(detail_font)
        text_column.addWidget(self.detail)

        self.extra = QLabel("", self)
        self.extra.setWordWrap(True)
        self.extra.setFont(detail_font)
        self.extra.setVisible(False)
        text_column.addWidget(self.extra)

        outer.addLayout(text_column, 1)

    def set_health(self, health: Health, extra: Health | None = None) -> None:
        """Show ``health``, with an optional secondary line (the command path)."""
        self._level = health.level
        colour = LEVEL_COLOURS[health.level]

        self.glyph.setText(LEVEL_GLYPHS[health.level])
        self.glyph.setStyleSheet(f"color: {colour.name()};")
        self.headline.setText(health.headline)
        self.headline.setStyleSheet(f"color: {colour.name()};")
        self.detail.setText(health.detail)
        self.detail.setStyleSheet("color: palette(windowText);")

        if extra is None:
            self.extra.setVisible(False)
        else:
            extra_colour = LEVEL_COLOURS[extra.level]
            self.extra.setText(f"{LEVEL_GLYPHS[extra.level]}  {extra.headline} - {extra.detail}")
            self.extra.setStyleSheet(f"color: {extra_colour.name()};")
            self.extra.setVisible(True)

        self.setToolTip(f"{health.headline}\n\n{health.detail}")
        self.update()

    def paintEvent(self, event) -> None:  # noqa: D102, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colour = LEVEL_COLOURS[self._level]

        # A tinted background rather than a saturated one, so the banner reads as
        # status and not as an error dialog demanding to be dismissed.
        fill = QColor(colour)
        fill.setAlpha(28)
        painter.setBrush(fill)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 5, 5)

        # A solid bar down the left edge carries the state at a glance.
        painter.setBrush(colour)
        painter.drawRoundedRect(0, 0, 4, self.height() - 1, 2, 2)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        edge = QColor(colour)
        edge.setAlpha(90)
        painter.setPen(QPen(edge, 1.0))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 5, 5)
        painter.end()
