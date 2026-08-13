"""Live navigation view: sky plot, signal bars, fix data and a satellite table.

This is the pane MiniGPS opens on, and the one that answers "is the receiver
actually working".  Nothing here writes to the device.
"""

from __future__ import annotations

import datetime as dt

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..nmea import (
    CONSTELLATION_NAMES,
    FIX_QUALITY_TEXT,
    FIX_TYPE_TEXT,
    Fix,
    NavState,
)
from .common import Pane, hint, monospace
from .skyview import ConstellationLegend, SignalBars, SkyView, constellation_colour

#: The view is repainted on a timer rather than on every sentence.  At 10 Hz
#: with GPS+GLONASS+BeiDou the receiver can emit 30+ sentences a second, and
#: repainting per sentence spends the whole frame budget on the sky plot.
REDRAW_INTERVAL_MS = 250


def format_latitude(value: float | None) -> str:
    if value is None:
        return "--"
    hemisphere = "N" if value >= 0 else "S"
    return f"{_dms(abs(value))} {hemisphere}   ({value:+.7f}°)"


def format_longitude(value: float | None) -> str:
    if value is None:
        return "--"
    hemisphere = "E" if value >= 0 else "W"
    return f"{_dms(abs(value))} {hemisphere}   ({value:+.7f}°)"


def _dms(value: float) -> str:
    degrees = int(value)
    minutes_full = (value - degrees) * 60.0
    minutes = int(minutes_full)
    seconds = (minutes_full - minutes) * 60.0
    return f"{degrees:3d}° {minutes:02d}' {seconds:06.3f}\""


class NavigationPane(Pane):
    """Read-only live view of the current solution."""

    def __init__(self, nav: NavState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._nav = nav
        self._dirty = True

        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # --- left: sky plot + legend ---
        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.sky = SkyView(left)
        left_layout.addWidget(self.sky, 1)
        self.legend = ConstellationLegend(left)
        left_layout.addWidget(self.legend)
        splitter.addWidget(left)

        # --- right: fix data ---
        right = QWidget(splitter)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self._build_fix_group(right))
        right_layout.addStretch(1)
        splitter.addWidget(right)
        splitter.setSizes([420, 420])

        self.body.addWidget(splitter, 3)

        bars_group = QGroupBox("Carrier-to-noise density (C/N0)", self)
        bars_layout = QVBoxLayout(bars_group)
        self.bars = SignalBars(bars_group)
        bars_layout.addWidget(self.bars)
        self.body.addWidget(bars_group, 2)

        self.body.addWidget(self._build_table_group())

        self._timer = QTimer(self)
        self._timer.setInterval(REDRAW_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    # -- construction ----------------------------------------------------

    def _build_fix_group(self, parent: QWidget) -> QGroupBox:
        group = QGroupBox("Fix", parent)
        grid = QGridLayout(group)
        grid.setColumnStretch(1, 1)

        self._values: dict[str, QLabel] = {}
        rows = [
            ("Status", "status"),
            ("Fix type", "fix_type"),
            ("UTC", "utc"),
            ("Local", "local"),
            ("Latitude", "latitude"),
            ("Longitude", "longitude"),
            ("Altitude (MSL)", "altitude"),
            ("Geoid separation", "geoid"),
            ("Speed", "speed"),
            ("Course", "course"),
            ("Magnetic variation", "variation"),
            ("Satellites used", "used"),
            ("PDOP / HDOP / VDOP", "dop"),
            ("DGPS", "dgps"),
        ]
        for row, (label_text, key) in enumerate(rows):
            label = QLabel(label_text + ":", group)
            label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(label, row, 0)
            value = QLabel("--", group)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if key in ("latitude", "longitude", "utc", "local"):
                monospace(value)
            grid.addWidget(value, row, 1)
            self._values[key] = value

        # The note goes in its own full-width row with the row stretched, so a
        # wrapped two-line label is not clipped by the group box border.
        note = hint(
            "Local time is this machine's timezone, converted from the receiver's UTC. "
            "The receiver itself has no timezone."
        )
        note.setMinimumHeight(note.fontMetrics().height() * 2 + 6)
        grid.addWidget(note, len(rows), 0, 1, 2)
        grid.setRowStretch(len(rows), 1)
        return group

    def _build_table_group(self) -> QGroupBox:
        group = QGroupBox("Satellites", self)
        layout = QVBoxLayout(group)

        self.table = QTableWidget(0, 6, group)
        self.table.setHorizontalHeaderLabels(
            ["PRN", "System", "Elevation", "Azimuth", "C/N0 (dB-Hz)", "In fix"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setMinimumHeight(150)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)

        counts = QHBoxLayout()
        self.count_label = QLabel("--", group)
        counts.addWidget(self.count_label)
        counts.addStretch(1)
        self.traffic_label = QLabel("--", group)
        counts.addWidget(self.traffic_label)
        layout.addLayout(counts)
        return group

    # -- updates ---------------------------------------------------------

    def mark_dirty(self) -> None:
        """Called when new navigation data has arrived."""
        self._dirty = True

    def _refresh(self) -> None:
        if not self._dirty:
            return
        self._dirty = False

        satellites = self._nav.satellites()
        self.sky.set_satellites(satellites)
        self.bars.set_satellites(satellites)
        self.legend.set_counts(self._nav.constellation_summary())
        self._refresh_fix(self._nav.fix)
        self._refresh_table(satellites)

    def _refresh_fix(self, fix: Fix) -> None:
        set_value = lambda key, text: self._values[key].setText(text)  # noqa: E731

        set_value("status", FIX_QUALITY_TEXT.get(fix.quality, "Unknown"))
        set_value("fix_type", FIX_TYPE_TEXT.get(fix.fix_type, "Unknown"))

        utc = fix.datetime_utc
        if utc is not None:
            set_value("utc", utc.strftime("%Y-%m-%d %H:%M:%S UTC"))
            local = utc.astimezone()
            set_value("local", local.strftime("%Y-%m-%d %H:%M:%S %Z"))
        elif fix.utc_time is not None:
            set_value("utc", fix.utc_time.strftime("%H:%M:%S (no date yet)"))
            set_value("local", "--")
        else:
            set_value("utc", "--")
            set_value("local", "--")

        set_value("latitude", format_latitude(fix.latitude))
        set_value("longitude", format_longitude(fix.longitude))
        set_value(
            "altitude",
            "--" if fix.altitude_m is None else f"{fix.altitude_m:.1f} m",
        )
        set_value(
            "geoid",
            "--" if fix.geoid_separation_m is None else f"{fix.geoid_separation_m:.1f} m",
        )

        if fix.speed_knots is None and fix.speed_kph is None:
            set_value("speed", "--")
        else:
            knots = fix.speed_knots
            kph = fix.speed_kph if fix.speed_kph is not None else (knots or 0) * 1.852
            knots = knots if knots is not None else kph / 1.852
            set_value("speed", f"{kph:.2f} km/h   ({knots:.2f} kn, {kph / 3.6:.2f} m/s)")

        course_bits = []
        if fix.course_true is not None:
            course_bits.append(f"{fix.course_true:.1f}° true")
        if fix.course_magnetic is not None:
            course_bits.append(f"{fix.course_magnetic:.1f}° magnetic")
        set_value("course", "   ".join(course_bits) if course_bits else "--")

        if fix.magnetic_variation is None:
            set_value("variation", "--")
        else:
            direction = "E" if fix.magnetic_variation >= 0 else "W"
            set_value("variation", f"{abs(fix.magnetic_variation):.1f}° {direction}")

        set_value("used", str(fix.satellites_used))
        set_value(
            "dop",
            "  /  ".join(
                "--" if value is None else f"{value:.2f}"
                for value in (fix.pdop, fix.hdop, fix.vdop)
            ),
        )

        if fix.dgps_age_s is None and not fix.dgps_station:
            set_value("dgps", "--")
        else:
            age = "--" if fix.dgps_age_s is None else f"{fix.dgps_age_s:.0f} s old"
            station = fix.dgps_station or "no station id"
            set_value("dgps", f"correction {age}, station {station}")

    def _refresh_table(self, satellites) -> None:
        self.table.setRowCount(len(satellites))
        for row, sat in enumerate(satellites):
            colour = constellation_colour(sat.constellation)
            cells = [
                str(sat.prn),
                CONSTELLATION_NAMES.get(sat.constellation, "?"),
                "--" if sat.elevation is None else f"{sat.elevation}°",
                "--" if sat.azimuth is None else f"{sat.azimuth}°",
                "--" if sat.snr is None else str(sat.snr),
                "yes" if sat.used else ("tracked" if sat.tracked else "no"),
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if column == 1:
                    item.setForeground(colour)
                self.table.setItem(row, column, item)

        tracked = sum(1 for sat in satellites if sat.tracked)
        used = sum(1 for sat in satellites if sat.used)
        self.count_label.setText(
            f"{len(satellites)} in view, {tracked} tracked, {used} used in the fix"
        )
        errors = self._nav.checksum_errors
        total = self._nav.sentence_count
        error_text = f"{errors} checksum error{'s' if errors != 1 else ''}"
        if errors and total:
            error_text += f" ({errors / (total + errors) * 100:.2f}% of traffic)"
        self.traffic_label.setText(f"{total} sentences decoded, {error_text}")

    def on_connected(self, is_connected: bool) -> None:
        if not is_connected:
            self._dirty = True
