"""Constellations, augmentation and navigation behaviour.

Covers PMTK353 (which systems to search), PMTK313/301 (SBAS and DGPS),
PMTK351/352 (QZSS), PMTK286 (interference cancellation) and PMTK386 (static
navigation).
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QLabel,
    QMessageBox,
    QWidget,
)

from .. import casic, pmtk
from ..protocol import Capability, CasicProtocol, Kind
from ..nmea import CONSTELLATION_NAMES, Constellation
from ..pmtk import DGPS_MODE_TEXT, DgpsMode, Packet
from .common import Pane, ReadWriteBar, Section, State, WrapLabel, hint


class GnssPane(Pane):
    """Which satellites to use, and how to treat them."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.bar = ReadWriteBar(parent=self)
        self.bar.read_requested.connect(self.read_from_device)
        self.bar.write_requested.connect(self._write)
        self.body.addWidget(self.bar)

        self.body.addWidget(self._build_constellations())
        self.body.addWidget(self._build_nav_mode())
        self.body.addWidget(self._build_augmentation())
        self.body.addWidget(self._build_behaviour())
        self.body.addStretch(1)

    # -- construction ----------------------------------------------------

    def _build_constellations(self) -> Section:
        self.constellation_section = section = Section(
            "Constellations",
            "PMTK353 selects which systems the receiver searches. The MT3333 specification "
            "documents only the GPS and GLONASS fields, so that documented two-field form is "
            "what gets sent. The V-800 MarkIII is advertised as GPS + GLONASS + BeiDou + QZSS; "
            "if this firmware accepts more fields it will say so in its acknowledgement, and "
            "the line below shows exactly what it reported back.",
        )

        self.gps_check = QCheckBox("GPS")
        self.gps_check.setChecked(True)
        self.gps_check.toggled.connect(self._mark_edited)
        section.add_widget(self.gps_check)

        self.glonass_check = QCheckBox("GLONASS")
        self.glonass_check.setChecked(True)
        self.glonass_check.toggled.connect(self._mark_edited)
        section.add_widget(self.glonass_check)

        self.beidou_check = QCheckBox("BeiDou")
        self.beidou_check.setChecked(True)
        self.beidou_check.toggled.connect(self._mark_edited)
        section.add_widget(self.beidou_check)

        self.reported_label = WrapLabel("Receiver has not reported a constellation set yet.")
        self.reported_label.setWordWrap(True)
        section.add_widget(self.reported_label)

        self.observed_label = WrapLabel("--")
        self.observed_label.setWordWrap(True)
        section.add_row("Actually being tracked", self.observed_label)

        section.add_widget(
            hint(
                "Disabling every constellation leaves the receiver unable to fix. "
                "The Write button refuses that combination."
            )
        )
        return section

    def _build_nav_mode(self) -> Section:
        self.nav_mode_section = section = Section(
            "Navigation dynamic model ($PCAS11)",
            "Tells the navigation filter what kind of motion to expect. A model that matches "
            "the platform lets the filter smooth harder without lagging, which helps both "
            "acquisition and position stability; a wrong one does the opposite.",
        )

        self.nav_mode_combo = QComboBox()
        for value, label in sorted(casic.NAV_MODES.items()):
            self.nav_mode_combo.addItem(f"{value} - {label}", value)
        self.nav_mode_combo.currentIndexChanged.connect(self._mark_edited)
        section.add_row("Model", self.nav_mode_combo)

        self.nav_mode_reported = WrapLabel("not read yet")
        section.add_row("Reported by receiver", self.nav_mode_reported)

        section.add_widget(
            hint(
                "Values 0-8 are accepted by this receiver; 9 is clamped to 8, which is how "
                "the range was established. The model names are the conventional CASIC "
                "labels and are NOT verified on this unit - confirming what each does would "
                "need controlled motion. The number is what gets written, and the read-back "
                "above proves which value took."
            )
        )
        return section

    def _build_augmentation(self) -> Section:
        self.augmentation_section = section = Section(
            "Augmentation",
            "SBAS (WAAS/EGNOS/MSAS) and DGPS improve accuracy where corrections are available. "
            "In Australia there is no geostationary SBAS service on the WAAS/EGNOS model, so "
            "enabling SBAS here typically costs a tracking channel for no benefit.",
        )

        self.sbas_check = QCheckBox("Search for SBAS satellites (PMTK313)")
        self.sbas_check.toggled.connect(self._mark_edited)
        section.add_widget(self.sbas_check)

        self.dgps_combo = QComboBox()
        for mode in DgpsMode:
            self.dgps_combo.addItem(DGPS_MODE_TEXT[mode], int(mode))
        self.dgps_combo.currentIndexChanged.connect(self._mark_edited)
        section.add_row("DGPS source (PMTK301)", self.dgps_combo)

        section.add_widget(
            hint(
                "RTCM corrections arrive on the chipset's second UART, which the V-800's USB "
                "bridge does not expose - selecting RTCM on this hardware has no path to "
                "supply the data."
            )
        )

        self.qzss_check = QCheckBox("Use QZSS satellites for ranging (PMTK352)")
        self.qzss_check.setChecked(True)
        self.qzss_check.toggled.connect(self._mark_edited)
        self.qzss_check.setToolTip(
            "Regional service over Japan and Australia. Note PMTK352 is inverted in the "
            "protocol: this tool sends 0 to enable and 1 to disable, per the specification's "
            "own examples and the packet name SET_STOP_QZSS. See docs/spec-errata.md."
        )
        section.add_widget(self.qzss_check)

        self.qzss_nmea_check = QCheckBox("Emit QZSS-format NMEA (PMTK351)")
        self.qzss_nmea_check.toggled.connect(self._mark_edited)
        self.qzss_nmea_check.setToolTip(
            "Off by default, which keeps output at NMEA 0183 v3.01. Turning this on changes "
            "the sentence format and may confuse software expecting v3.01."
        )
        section.add_widget(self.qzss_nmea_check)
        return section

    def _build_behaviour(self) -> Section:
        self.behaviour_section = section = Section(
            "Navigation behaviour",
            "Both of these change what the receiver reports rather than how well it tracks. "
            "Static navigation in particular fabricates a stationary position below the "
            "threshold - useful for a car, wrong for anything that needs honest data.",
        )

        self.aic_check = QCheckBox("Active interference cancellation (PMTK286)")
        self.aic_check.toggled.connect(self._mark_edited)
        self.aic_check.setToolTip(
            "Notches out narrowband interference. Worth enabling near switching supplies, "
            "USB 3 ports and display cables."
        )
        section.add_widget(self.aic_check)

        self.static_spin = QDoubleSpinBox()
        self.static_spin.setRange(0.0, 2.0)
        self.static_spin.setSingleStep(0.1)
        self.static_spin.setDecimals(1)
        self.static_spin.setSuffix(" m/s")
        self.static_spin.setSpecialValueText("0.0 m/s (disabled)")
        self.static_spin.valueChanged.connect(self._mark_edited)
        section.add_row("Static navigation threshold (PMTK386)", self.static_spin)

        section.add_widget(
            hint(
                "Below this speed the receiver freezes the reported position and reports zero "
                "speed. It suppresses the wander you see when stationary, but it also means "
                "genuine slow movement - walking, drifting at anchor - is reported as no "
                "movement at all. Leave at 0 when logging data you intend to analyse. "
                "There is no query command for this setting, so it cannot be read back."
            )
        )
        return section

    # -- interaction -----------------------------------------------------

    def _mark_edited(self) -> None:
        self.bar.set_state(State.EDITED)

    def _write(self) -> None:
        protocol = getattr(self, "_protocol", None)
        casic_mode = isinstance(protocol, CasicProtocol)

        chosen = [self.gps_check.isChecked(), self.glonass_check.isChecked()]
        if casic_mode:
            chosen.append(self.beidou_check.isChecked())
        if not any(chosen):
            QMessageBox.warning(
                self,
                "No constellations selected",
                "At least one constellation must stay enabled or the receiver cannot fix.",
            )
            return

        if casic_mode:
            try:
                self.send_bytes(
                    protocol.set_constellations(
                        self.gps_check.isChecked(),
                        self.glonass_check.isChecked(),
                        self.beidou_check.isChecked(),
                    ),
                    "set constellations",
                )
            except ValueError as exc:
                QMessageBox.warning(self, "Invalid selection", str(exc))
                self.bar.set_state(State.FAILED, str(exc))
                return
            try:
                self.send_bytes(
                    protocol.set_navigation_mode(self.nav_mode_combo.currentData()),
                    "set navigation mode",
                )
            except ValueError as exc:
                QMessageBox.warning(self, "Invalid navigation mode", str(exc))
                return
            # $PCAS is never acknowledged, but both settings land in CFG-NAVX,
            # so polling it is a genuine read-back rather than a hopeful guess.
            self.bar.set_state(State.WRITTEN, "confirming via CFG-NAVX")
            self.send_bytes(protocol.poll_navx(), "poll CFG-NAVX")
            return

        try:
            self.send(
                pmtk.set_gnss_search_mode(
                    self.gps_check.isChecked(), self.glonass_check.isChecked()
                ),
                "set constellation search mode",
            )
            self.send(
                pmtk.set_sbas_enabled(self.sbas_check.isChecked()),
                "set SBAS search",
            )
            self.send(
                pmtk.set_dgps_mode(self.dgps_combo.currentData()),
                "set DGPS source",
            )
            self.send(
                pmtk.set_qzss_enabled(self.qzss_check.isChecked()),
                "set QZSS ranging",
            )
            self.send(
                pmtk.set_qzss_nmea_format(self.qzss_nmea_check.isChecked()),
                "set QZSS NMEA format",
            )
            self.send(pmtk.set_aic(self.aic_check.isChecked()), "set interference cancellation")
            self.send(
                pmtk.set_static_nav_threshold(self.static_spin.value()),
                "set static navigation threshold",
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid setting", str(exc))
            self.bar.set_state(State.FAILED, str(exc))
            return

        self.bar.set_state(State.WRITTEN)
        self.read_from_device()

    def read_from_device(self) -> None:
        protocol = getattr(self, "_protocol", None)
        if isinstance(protocol, CasicProtocol):
            # CFG-NAVX carries both the constellation mask and the navigation
            # mode, so there *is* a read-back after all.
            self.send_bytes(protocol.poll_navx(), "poll CFG-NAVX")
            return
        self.send(pmtk.query_sbas_enabled(), "query SBAS")
        self.send(pmtk.query_dgps_mode(), "query DGPS mode")

    # -- device feedback -------------------------------------------------

    def on_sentence(self, sentence) -> None:
        packet = sentence.packet_type
        if packet == Packet.DT_SBAS_ENABLED and sentence.fields:
            self.sbas_check.blockSignals(True)
            self.sbas_check.setChecked(sentence.fields[0] == "1")
            self.sbas_check.blockSignals(False)
            self.bar.set_state(State.CONFIRMED)
        elif packet == Packet.DT_DGPS_MODE and sentence.fields:
            try:
                mode = int(sentence.fields[0])
            except ValueError:
                return
            index = self.dgps_combo.findData(mode)
            if index >= 0:
                self.dgps_combo.blockSignals(True)
                self.dgps_combo.setCurrentIndex(index)
                self.dgps_combo.blockSignals(False)
            self.bar.set_state(State.CONFIRMED)

        reported = pmtk.parse_gnss_search_mode(sentence)
        if reported:
            names = ["GPS", "GLONASS", "BeiDou", "Galileo"]
            parts = [
                f"{names[index] if index < len(names) else f'field {index}'}: "
                f"{'on' if value else 'off'}"
                for index, value in enumerate(reported)
            ]
            self.reported_label.setText(
                "Receiver reported after PMTK353: " + ", ".join(parts)
            )

    def on_ack(self, ack) -> None:
        if ack.command in (
            int(Packet.API_SET_GNSS_SEARCH_MODE),
            int(Packet.API_SET_SBAS_ENABLED),
            int(Packet.API_SET_DGPS_MODE),
            int(Packet.API_SET_STOP_QZSS),
            int(Packet.API_SET_SUPPORT_QZSS_NMEA),
            int(Packet.SET_AIC_CMD),
            int(Packet.API_SET_STATIC_NAV_THD),
        ) and not ack.ok:
            self.bar.set_state(State.FAILED, str(ack))

    def update_observed(self, counts: dict[Constellation, tuple[int, int]]) -> None:
        """Show which constellations are actually producing satellites.

        This is the honest answer to "did PMTK353 take effect" -- more reliable
        than the acknowledgement, because it is measured from the data stream.
        """
        if not counts:
            self.observed_label.setText("--")
            return
        parts = [
            f"{CONSTELLATION_NAMES[constellation]}: {tracked} tracked of {in_view} in view"
            for constellation, (tracked, in_view) in sorted(counts.items())
        ]
        self.observed_label.setText("; ".join(parts))

    def on_connected(self, is_connected: bool) -> None:
        self.bar.set_enabled(is_connected)
        if not is_connected:
            self.bar.set_state(State.UNKNOWN)
            self.reported_label.setText("Receiver has not reported a constellation set yet.")
            self.observed_label.setText("--")

    def on_protocol(self, protocol) -> None:
        """Adapt to the active protocol.

        CASIC can select constellations but offers nothing for SBAS, DGPS, QZSS
        or the navigation-behaviour settings, so those sections are disabled
        individually rather than blanking the whole pane -- the part that works
        should stay usable.
        """
        self._protocol = protocol
        if not protocol.supports(Capability.CONSTELLATIONS):
            self.require(protocol, Capability.CONSTELLATIONS)
            return
        self._set_unsupported(None)

        casic_mode = protocol.kind is Kind.CASIC
        self.beidou_check.setVisible(casic_mode)
        self.augmentation_section.setEnabled(not casic_mode)
        self.behaviour_section.setEnabled(not casic_mode)
        if casic_mode:
            self.constellation_section.set_note(
                "$PCAS04 selects which systems the receiver tracks. Verified against this "
                "receiver: all five reachable combinations were set and the resulting GSV "
                "talkers observed. The receiver does not acknowledge this command, so the "
                "only confirmation is the tracked list below. Disabling shows within seconds; "
                "re-enabling can take a minute or more, because the receiver has to reacquire "
                "the constellation from scratch - longer again indoors."
            )
            self.nav_mode_section.setEnabled(True)
            self.augmentation_section.setToolTip(
                "No CASIC message for SBAS, DGPS or QZSS has been identified on this receiver."
            )
            self.behaviour_section.setToolTip(
                "No CASIC message for interference cancellation or static navigation has "
                "been identified on this receiver."
            )

    def on_casic_frame(self, frame) -> None:
        """Update from CFG-NAVX, the read-back for both $PCAS settings."""
        decoded = casic.parse_navx(frame)
        if decoded is None:
            return

        mode = decoded["nav_mode"]
        index = self.nav_mode_combo.findData(mode)
        if index >= 0:
            self.nav_mode_combo.blockSignals(True)
            self.nav_mode_combo.setCurrentIndex(index)
            self.nav_mode_combo.blockSignals(False)
        self.nav_mode_reported.setText(
            f"{mode} - {casic.NAV_MODES.get(mode, 'unknown')}   (CFG-NAVX byte 4)"
        )

        flags = decoded["constellations"]
        for check, key in (
            (self.gps_check, "GPS"),
            (self.glonass_check, "GLONASS"),
            (self.beidou_check, "BeiDou"),
        ):
            check.blockSignals(True)
            check.setChecked(flags[key])
            check.blockSignals(False)
        self.reported_label.setText(
            f"Receiver reports mask 0x{decoded['constellation_mask']:02X}: "
            + ", ".join(f"{k} {'on' if v else 'off'}" for k, v in flags.items())
            + "   (CFG-NAVX byte 13)"
        )
        self.bar.set_state(State.CONFIRMED)
