"""Restarts, aiding data and the ephemeris/almanac inventory.

Restart type is the single most useful diagnostic on the device: if a hot start
fixes in seconds but a cold start takes minutes and then fails, the problem is
the antenna or the sky view, not the receiver.
"""

from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDateTimeEdit,
    QDoubleSpinBox,
    QGridLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QWidget,
)

from .. import pmtk
from ..protocol import Capability
from ..pmtk import Packet
from .common import Pane, Section, WrapLabel, hint, monospace


class AidingPane(Pane):
    """PMTK101/102/103/104/120, PMTK335/740/741, PMTK607/660/661."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._nav_fix_getter = None

        self.body.addWidget(self._build_restart())
        self.body.addWidget(self._build_aiding())
        self.body.addWidget(self._build_inventory())
        self.body.addStretch(1)

    def set_fix_source(self, getter) -> None:
        """Provide a callable returning the current :class:`~v800.nmea.Fix`.

        Used by "Use current fix" so the aiding position comes from the receiver
        itself rather than from something typed in.
        """
        self._nav_fix_getter = getter

    # -- construction ----------------------------------------------------

    def _build_restart(self) -> Section:
        section = Section(
            "Restart",
            "Each restart discards more of the receiver's stored state than the last. "
            "Working down the list is the standard way to separate a stale-almanac problem "
            "from an antenna problem.",
        )

        grid = QGridLayout()
        restarts = [
            ("Hot start", pmtk.hot_start(),
             "Keeps time, position, almanac and ephemeris. Fastest; use after a brief outage.",
             False),
            ("Warm start", pmtk.warm_start(),
             "Discards ephemeris, keeps time, position and almanac.", False),
            ("Cold start", pmtk.cold_start(),
             "Discards time, position, almanac and ephemeris. Expect 35 s or more to first fix.",
             True),
            ("Full cold start", pmtk.full_cold_start(),
             "Cold start AND resets every setting to factory state - including the baud rate "
             "and everything configured on the other tabs.", True),
            ("Clear flash aiding", pmtk.clear_flash_aid(),
             "Erases the aiding data held in flash without otherwise restarting.", True),
        ]
        for row, (label, payload, description, confirm) in enumerate(restarts):
            button = QPushButton(label)
            button.clicked.connect(
                lambda _=False, p=payload, l=label, d=description, c=confirm: self._restart(p, l, d, c)
            )
            grid.addWidget(button, row, 0)

            note = QLabel(description)
            note.setWordWrap(True)
            note.setStyleSheet("color: palette(mid);")
            grid.addWidget(note, row, 1)
        grid.setColumnStretch(1, 1)

        holder = QWidget()
        holder.setLayout(grid)
        section.add_widget(holder)
        return section

    def _build_aiding(self) -> Section:
        section = Section(
            "Aiding data",
            "Supplying rough time and position shortens the time to first fix after a cold "
            "start. The receiver checks these itself: time should be within 3 seconds and "
            "position within 30 km to be useful.",
        )

        self.time_edit = QDateTimeEdit()
        self.time_edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.time_edit.setTimeSpec(Qt.TimeSpec.UTC)
        self.time_edit.setDateTime(QDateTime.currentDateTimeUtc())
        section.add_row("UTC time", self.time_edit)

        now_button = QPushButton("Set to this computer's clock (UTC now)")
        now_button.clicked.connect(
            lambda: self.time_edit.setDateTime(QDateTime.currentDateTimeUtc())
        )
        section.add_row("", now_button)

        send_utc = QPushButton("Send UTC aiding (PMTK740)")
        send_utc.clicked.connect(self._send_utc)
        section.add_row("", send_utc)

        send_rtc = QPushButton("Set receiver RTC (PMTK335)")
        send_rtc.setToolTip(
            "Sets the real-time clock only. The receiver overwrites this with GPS time "
            "within about 60 seconds of getting a fix."
        )
        send_rtc.clicked.connect(self._send_rtc)
        section.add_row("", send_rtc)

        self.lat_spin = QDoubleSpinBox()
        self.lat_spin.setRange(-90.0, 90.0)
        self.lat_spin.setDecimals(6)
        self.lat_spin.setSuffix("°")
        section.add_row("Latitude", self.lat_spin)

        self.lon_spin = QDoubleSpinBox()
        self.lon_spin.setRange(-180.0, 180.0)
        self.lon_spin.setDecimals(6)
        self.lon_spin.setSuffix("°")
        section.add_row("Longitude", self.lon_spin)

        self.alt_spin = QDoubleSpinBox()
        self.alt_spin.setRange(-1000.0, 20000.0)
        self.alt_spin.setDecimals(1)
        self.alt_spin.setSuffix(" m")
        section.add_row("Altitude", self.alt_spin)

        use_fix = QPushButton("Use the receiver's current fix")
        use_fix.clicked.connect(self._use_current_fix)
        section.add_row("", use_fix)

        send_pos = QPushButton("Send position aiding (PMTK741)")
        send_pos.clicked.connect(self._send_position)
        section.add_row("", send_pos)

        section.add_widget(
            hint(
                "This computer's clock is only useful as aiding if it is itself synchronised. "
                "An unsynchronised host clock supplied as aiding makes the first fix slower, "
                "not faster."
            )
        )
        return section

    def _build_inventory(self) -> Section:
        section = Section(
            "Stored aiding inventory",
            "Which satellites the receiver still holds usable ephemeris and almanac for. "
            "Empty lists after a cold start are expected; empty lists after hours of clear "
            "sky point at a receiver that is not decoding the navigation message.",
        )

        self.eph_spin = QSpinBox()
        self.eph_spin.setRange(1, 7200)
        self.eph_spin.setValue(1800)
        self.eph_spin.setSuffix(" s")
        section.add_row("Ephemeris valid after", self.eph_spin)

        eph_button = QPushButton("Query ephemeris (PMTK660)")
        eph_button.clicked.connect(self._query_eph)
        section.add_row("", eph_button)

        self.eph_label = WrapLabel("--")
        self.eph_label.setWordWrap(True)
        monospace(self.eph_label)
        section.add_row("Ephemeris available", self.eph_label)

        self.alm_spin = QSpinBox()
        self.alm_spin.setRange(1, 365)
        self.alm_spin.setValue(30)
        self.alm_spin.setSuffix(" days")
        section.add_row("Almanac valid after", self.alm_spin)

        alm_button = QPushButton("Query almanac (PMTK661)")
        alm_button.clicked.connect(self._query_alm)
        section.add_row("", alm_button)

        self.alm_label = WrapLabel("--")
        self.alm_label.setWordWrap(True)
        monospace(self.alm_label)
        section.add_row("Almanac available", self.alm_label)

        epo_button = QPushButton("Check EPO validity (PMTK607)")
        epo_button.clicked.connect(lambda: self.send(pmtk.query_epo_info(), "query EPO info"))
        section.add_row("", epo_button)

        self.epo_label = WrapLabel("--")
        self.epo_label.setWordWrap(True)
        monospace(self.epo_label)
        section.add_row("EPO", self.epo_label)

        section.add_widget(
            hint(
                "The reply masks cover 32 satellites, so these lists are GPS PRNs only - "
                "there is no equivalent query for the GLONASS or BeiDou constellations."
            )
        )
        return section

    # -- interaction -----------------------------------------------------

    def _restart(self, payload: str, label: str, description: str, confirm: bool) -> None:
        if confirm:
            answer = QMessageBox.warning(
                self,
                label,
                f"{label}?\n\n{description}",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Ok:
                return
        self.send(payload, label.lower())

    def _utc_parts(self) -> tuple[int, int, int, int, int, int]:
        value = self.time_edit.dateTime().toPython()
        if isinstance(value, dt.datetime):
            return (value.year, value.month, value.day, value.hour, value.minute, value.second)
        raise ValueError("could not read the time field")

    def _send_utc(self) -> None:
        try:
            self.send(pmtk.set_utc_aiding(*self._utc_parts()), "send UTC aiding")
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid time", str(exc))

    def _send_rtc(self) -> None:
        try:
            self.send(pmtk.set_rtc_time(*self._utc_parts()), "set RTC time")
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid time", str(exc))

    def _use_current_fix(self) -> None:
        if self._nav_fix_getter is None:
            return
        fix = self._nav_fix_getter()
        if fix is None or fix.latitude is None or fix.longitude is None:
            QMessageBox.information(
                self,
                "No fix available",
                "The receiver has not reported a position yet, so there is nothing to copy.",
            )
            return
        self.lat_spin.setValue(fix.latitude)
        self.lon_spin.setValue(fix.longitude)
        if fix.altitude_m is not None:
            self.alt_spin.setValue(fix.altitude_m)
        if fix.datetime_utc is not None:
            self.time_edit.setDateTime(QDateTime(fix.datetime_utc).toUTC())

    def _send_position(self) -> None:
        try:
            payload = pmtk.set_position_aiding(
                self.lat_spin.value(),
                self.lon_spin.value(),
                self.alt_spin.value(),
                *self._utc_parts(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid position", str(exc))
            return
        self.send(payload, "send position aiding")

    def _query_eph(self) -> None:
        try:
            self.send(
                pmtk.query_available_sv_eph(self.eph_spin.value()), "query available ephemeris"
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid interval", str(exc))

    def _query_alm(self) -> None:
        try:
            self.send(
                pmtk.query_available_sv_alm(self.alm_spin.value()), "query available almanac"
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid interval", str(exc))

    # -- device feedback -------------------------------------------------

    def on_sentence(self, sentence) -> None:
        available = pmtk.parse_available_sv(sentence)
        if available is not None:
            query, svs = available
            text = (
                f"{len(svs)} satellite{'s' if len(svs) != 1 else ''}: "
                + ", ".join(str(sv) for sv in svs)
                if svs
                else "none"
            )
            if query == int(Packet.Q_AVAILABLE_SV_EPH):
                self.eph_label.setText(text)
            else:
                self.alm_label.setText(text)
            return

        # PMTK607's reply format is not given in this specification revision, so
        # the raw fields are shown rather than guessed at.
        if sentence.packet_type == Packet.Q_EPO_INFO or (
            sentence.is_pmtk and sentence.formatter == "607"
        ):
            self.epo_label.setText(sentence.raw)

    def on_ack(self, ack) -> None:
        if ack.command == int(Packet.Q_EPO_INFO) and not ack.ok:
            self.epo_label.setText(f"{ack} (this firmware may not carry EPO data)")

    def on_connected(self, is_connected: bool) -> None:
        self.setEnabled(is_connected)

    def on_protocol(self, protocol) -> None:
        """Disable this pane when the active protocol cannot perform it."""
        self.require(protocol, Capability.RESTART)
