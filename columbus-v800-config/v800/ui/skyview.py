"""Sky plot and C/N0 bar graph, the two views MiniGPS leads with.

Both widgets are passive: they are handed a satellite list and repaint.  They
hold no device state, so they can be exercised from a test harness with
synthetic satellites.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..nmea import CONSTELLATION_NAMES, Constellation, Satellite

#: One colour per constellation.  Chosen to stay distinguishable in both light
#: and dark palettes and to survive the most common form of colour blindness --
#: the hues are spread in lightness as well as hue, so the bars remain readable
#: as a greyscale ramp if the colours cannot be told apart.
CONSTELLATION_COLOURS = {
    Constellation.GPS: QColor("#2f7fd0"),
    Constellation.GLONASS: QColor("#d06b2f"),
    Constellation.BEIDOU: QColor("#2fa87a"),
    Constellation.GALILEO: QColor("#8a5fc0"),
    Constellation.QZSS: QColor("#c0a52f"),
    Constellation.SBAS: QColor("#b0405f"),
    Constellation.UNKNOWN: QColor("#808080"),
}

#: C/N0 above which a signal is considered strong enough for a reliable fix.
GOOD_SNR_DB = 35
USABLE_SNR_DB = 25


def constellation_colour(constellation: Constellation) -> QColor:
    return CONSTELLATION_COLOURS.get(constellation, CONSTELLATION_COLOURS[Constellation.UNKNOWN])


class SkyView(QWidget):
    """Polar plot of satellite azimuth and elevation.

    North is up, elevation runs from 90 degrees at the centre to 0 at the rim --
    the convention every GNSS tool uses, and the one MiniGPS uses.  Satellites
    contributing to the current solution are drawn filled; those merely in view
    are drawn hollow, which is the distinction that actually matters when you
    are diagnosing a poor fix.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._satellites: list[Satellite] = []
        self.setMinimumSize(260, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setToolTip(
            "Sky plot: north up, centre = directly overhead, rim = horizon.\n"
            "Filled markers are satellites used in the current fix."
        )

    def set_satellites(self, satellites: list[Satellite]) -> None:
        self._satellites = satellites
        self.update()

    def paintEvent(self, event) -> None:  # noqa: D102, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        side = min(self.width(), self.height())
        margin = 18.0
        radius = side / 2.0 - margin
        centre = QPointF(self.width() / 2.0, self.height() / 2.0)

        text_colour = self.palette().windowText().color()
        grid_colour = QColor(text_colour)
        grid_colour.setAlpha(70)
        faint_colour = QColor(text_colour)
        faint_colour.setAlpha(110)

        # Elevation rings at 0, 30 and 60 degrees.
        painter.setPen(QPen(grid_colour, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for elevation in (0, 30, 60):
            ring = radius * (90 - elevation) / 90.0
            painter.drawEllipse(centre, ring, ring)

        # Azimuth spokes every 30 degrees.
        for azimuth in range(0, 360, 30):
            angle = math.radians(azimuth - 90)
            painter.drawLine(
                centre,
                QPointF(
                    centre.x() + radius * math.cos(angle),
                    centre.y() + radius * math.sin(angle),
                ),
            )

        # Cardinal labels.
        painter.setPen(QPen(faint_colour))
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1))
        painter.setFont(font)
        metrics = QFontMetrics(font)
        for label, azimuth in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
            angle = math.radians(azimuth - 90)
            x = centre.x() + (radius + 10) * math.cos(angle)
            y = centre.y() + (radius + 10) * math.sin(angle)
            rect = QRectF(
                x - metrics.horizontalAdvance(label),
                y - metrics.height() / 2.0,
                metrics.horizontalAdvance(label) * 2,
                metrics.height(),
            )
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        if not self._satellites:
            painter.setPen(QPen(faint_colour))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "No satellites in view"
            )
            painter.end()
            return

        marker = max(9.0, radius * 0.075)
        for sat in self._satellites:
            if sat.elevation is None or sat.azimuth is None:
                continue
            elevation = max(0, min(90, sat.elevation))
            distance = radius * (90 - elevation) / 90.0
            angle = math.radians(sat.azimuth - 90)
            point = QPointF(
                centre.x() + distance * math.cos(angle),
                centre.y() + distance * math.sin(angle),
            )

            colour = constellation_colour(sat.constellation)
            if sat.used:
                painter.setBrush(QBrush(colour))
                painter.setPen(QPen(colour.darker(150), 1.5))
            elif sat.tracked:
                pale = QColor(colour)
                pale.setAlpha(70)
                painter.setBrush(QBrush(pale))
                painter.setPen(QPen(colour, 1.5))
            else:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(colour, 1.0, Qt.PenStyle.DashLine))

            _draw_marker(painter, point, marker, sat.constellation)

            painter.setPen(QPen(text_colour))
            painter.drawText(
                QRectF(point.x() - marker * 2, point.y() + marker * 0.6, marker * 4, marker * 1.8),
                Qt.AlignmentFlag.AlignCenter,
                str(sat.prn),
            )

        painter.end()


def _draw_marker(painter: QPainter, centre: QPointF, size: float, constellation: Constellation) -> None:
    """Draw a per-constellation marker shape.

    Shape carries the same information as colour, so the plot stays readable
    when colours cannot be distinguished, and in a screenshot printed in mono.
    """
    half = size / 2.0
    if constellation is Constellation.GPS:
        painter.drawEllipse(centre, half, half)
    elif constellation is Constellation.GLONASS:
        painter.drawRect(QRectF(centre.x() - half, centre.y() - half, size, size))
    elif constellation is Constellation.BEIDOU:
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(centre.x(), centre.y() - half),
                    QPointF(centre.x() + half, centre.y() + half),
                    QPointF(centre.x() - half, centre.y() + half),
                ]
            )
        )
    elif constellation is Constellation.GALILEO:
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(centre.x(), centre.y() - half),
                    QPointF(centre.x() + half, centre.y()),
                    QPointF(centre.x(), centre.y() + half),
                    QPointF(centre.x() - half, centre.y()),
                ]
            )
        )
    else:
        painter.drawRoundedRect(
            QRectF(centre.x() - half, centre.y() - half, size, size), half / 2, half / 2
        )


class SignalBars(QWidget):
    """C/N0 bar graph, one bar per satellite in view.

    Bars are grouped by constellation and labelled with the PRN.  Satellites in
    view but not tracked get a hollow bar at zero height so they are visibly
    *present but not contributing* rather than simply missing -- the difference
    between "the antenna cannot see it" and "the receiver has not locked it".
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._satellites: list[Satellite] = []
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setToolTip(
            "Carrier-to-noise density per satellite.\n"
            "Solid = used in fix, pale = tracked, outline = in view but not tracked."
        )

    def set_satellites(self, satellites: list[Satellite]) -> None:
        self._satellites = satellites
        self.update()

    def paintEvent(self, event) -> None:  # noqa: D102, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        text_colour = self.palette().windowText().color()
        faint = QColor(text_colour)
        faint.setAlpha(60)

        font = QFont(self.font())
        font.setPointSizeF(max(6.5, font.pointSizeF() - 2))
        painter.setFont(font)
        metrics = QFontMetrics(font)

        label_height = metrics.height() + 2
        top = 4.0
        bottom = self.height() - label_height - 2
        plot_height = max(1.0, bottom - top)
        full_scale = 55.0  # dB-Hz; above this the bar is simply full

        # Reference lines at the usable and good thresholds.
        painter.setPen(QPen(faint, 1.0, Qt.PenStyle.DashLine))
        for threshold in (USABLE_SNR_DB, GOOD_SNR_DB):
            y = bottom - plot_height * (threshold / full_scale)
            painter.drawLine(QPointF(0, y), QPointF(self.width(), y))
            painter.setPen(QPen(faint))
            painter.drawText(QPointF(2, y - 2), f"{threshold} dB")
            painter.setPen(QPen(faint, 1.0, Qt.PenStyle.DashLine))

        if not self._satellites:
            painter.setPen(QPen(faint))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No satellites in view")
            painter.end()
            return

        count = len(self._satellites)
        slot = self.width() / count
        bar_width = max(3.0, min(24.0, slot * 0.72))

        for index, sat in enumerate(self._satellites):
            x = slot * index + (slot - bar_width) / 2.0
            colour = constellation_colour(sat.constellation)
            snr = sat.snr or 0
            height = plot_height * min(snr, full_scale) / full_scale

            if sat.used:
                painter.setBrush(QBrush(colour))
                painter.setPen(QPen(colour.darker(140), 1.0))
            elif sat.tracked:
                pale = QColor(colour)
                pale.setAlpha(90)
                painter.setBrush(QBrush(pale))
                painter.setPen(QPen(colour, 1.0))
            else:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(colour, 1.0, Qt.PenStyle.DotLine))
                height = 0.0

            rect = QRectF(x, bottom - height, bar_width, max(height, 2.0))
            painter.drawRect(rect)

            painter.setPen(QPen(text_colour))
            painter.drawText(
                QRectF(slot * index, bottom + 1, slot, label_height),
                Qt.AlignmentFlag.AlignCenter,
                str(sat.prn),
            )
            if snr:
                painter.drawText(
                    QRectF(slot * index, bottom - height - label_height, slot, label_height),
                    Qt.AlignmentFlag.AlignCenter,
                    str(snr),
                )

        painter.end()


class ConstellationLegend(QWidget):
    """Small colour/shape key for the sky view and bar graph."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._counts: dict[Constellation, tuple[int, int]] = {}
        self.setMinimumHeight(24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_counts(self, counts: dict[Constellation, tuple[int, int]]) -> None:
        self._counts = counts
        self.update()

    def paintEvent(self, event) -> None:  # noqa: D102, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1))
        painter.setFont(font)
        metrics = QFontMetrics(font)

        x = 2.0
        y = self.height() / 2.0
        for constellation in (
            Constellation.GPS,
            Constellation.GLONASS,
            Constellation.BEIDOU,
            Constellation.GALILEO,
            Constellation.QZSS,
            Constellation.SBAS,
        ):
            if constellation not in self._counts:
                continue
            tracked, in_view = self._counts[constellation]
            colour = constellation_colour(constellation)
            painter.setBrush(QBrush(colour))
            painter.setPen(QPen(colour.darker(140), 1.0))
            _draw_marker(painter, QPointF(x + 6, y), 10, constellation)

            label = f"{CONSTELLATION_NAMES[constellation]} {tracked}/{in_view}"
            painter.setPen(QPen(self.palette().windowText().color()))
            painter.drawText(QPointF(x + 16, y + metrics.ascent() / 2.0 - 1), label)
            x += 16 + metrics.horizontalAdvance(label) + 14

        painter.end()
