"""Geodetic datum selection (PMTK330/430) and the user datum (PMTK331/431)."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QWidget,
)

from .. import pmtk
from ..protocol import Capability
from ..datums import DATUMS, USER_DATUM_INDEX, WGS84_INDEX, datum_label
from ..pmtk import Packet
from .common import Pane, ReadWriteBar, Section, State, WrapLabel, hint


class DatumPane(Pane):
    """Pick one of the 223 built-in datums, or define your own."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.bar = ReadWriteBar(parent=self)
        self.bar.read_requested.connect(self.read_from_device)
        self.bar.write_requested.connect(self._write)
        self.body.addWidget(self.bar)

        self.body.addWidget(self._build_selection())
        self.body.addWidget(self._build_user_datum())
        self.body.addStretch(1)

    def _build_selection(self) -> Section:
        section = Section(
            "Datum",
            "The datum the receiver reports positions against. Almost everything modern - "
            "GPS itself, OpenStreetMap, Google Maps, GDA2020 for practical purposes at this "
            "accuracy - expects WGS84. Change this only if you are feeding software that "
            "specifically wants a local datum, and record which one you used: a position in "
            "an unstated datum is not a position.",
        )

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Type to filter, e.g. 'Australia' or 'Tokyo'")
        self.filter_edit.textChanged.connect(self._apply_filter)
        section.add_row("Filter", self.filter_edit)

        self.datum_combo = QComboBox()
        self.datum_combo.setMaxVisibleItems(20)
        self._populate("")
        self.datum_combo.currentIndexChanged.connect(self._datum_chosen)
        section.add_row("Datum", self.datum_combo)

        wgs84 = QPushButton("Reset to WGS84")
        wgs84.clicked.connect(self._select_wgs84)
        section.add_row("", wgs84)

        self.current_label = WrapLabel("Receiver has not reported a datum yet.")
        self.current_label.setWordWrap(True)
        section.add_row("Reported by receiver", self.current_label)

        section.add_widget(
            hint(
                "223 datums, numbered 0-222, transcribed from Appendix A of the MT3333 "
                "specification. The specification's prose says 219; the appendix is what the "
                "chipset indexes against, so the appendix is what is listed here."
            )
        )
        return section

    def _build_user_datum(self) -> Section:
        section = Section(
            "User-defined datum (index 3)",
            "PMTK331 defines the ellipsoid that datum index 3 selects. Values are the "
            "semi-major axis in metres, the eccentricity term, and the three-axis offset to "
            "WGS84 in metres.",
        )

        self.maj_spin = QDoubleSpinBox()
        self.maj_spin.setRange(0.0, 7_000_000.0)
        self.maj_spin.setDecimals(3)
        self.maj_spin.setValue(6_378_137.0)
        self.maj_spin.setSuffix(" m")
        section.add_row("Semi-major axis", self.maj_spin)

        self.ecc_spin = QDoubleSpinBox()
        self.ecc_spin.setRange(0.0, 330.0)
        self.ecc_spin.setDecimals(7)
        self.ecc_spin.setValue(298.2572236)
        section.add_row("Eccentricity term", self.ecc_spin)

        self.dx_spin = self._offset_spin()
        section.add_row("dX to WGS84", self.dx_spin)
        self.dy_spin = self._offset_spin()
        section.add_row("dY to WGS84", self.dy_spin)
        self.dz_spin = self._offset_spin()
        section.add_row("dZ to WGS84", self.dz_spin)

        write_user = QPushButton("Write user datum")
        write_user.clicked.connect(self._write_user_datum)
        section.add_row("", write_user)

        section.add_widget(
            hint(
                "The defaults shown are the WGS84 ellipsoid with zero offset, which makes "
                "index 3 behave like index 0 until you change them. PMTK431 (query) is "
                "documented as firmware-dependent, so this may not read back."
            )
        )
        return section

    def _offset_spin(self) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(-10_000.0, 10_000.0)
        spin.setDecimals(3)
        spin.setSuffix(" m")
        return spin

    # -- interaction -----------------------------------------------------

    def _populate(self, needle: str) -> None:
        needle = needle.strip().lower()
        current = self.datum_combo.currentData()
        self.datum_combo.blockSignals(True)
        self.datum_combo.clear()
        for index in sorted(DATUMS):
            label = datum_label(index)
            if needle and needle not in label.lower():
                continue
            self.datum_combo.addItem(label, index)
        if current is not None:
            found = self.datum_combo.findData(current)
            if found >= 0:
                self.datum_combo.setCurrentIndex(found)
        self.datum_combo.blockSignals(False)

    def _apply_filter(self, text: str) -> None:
        self._populate(text)

    def _datum_chosen(self) -> None:
        self.bar.set_state(State.EDITED)

    def _select_wgs84(self) -> None:
        self.filter_edit.clear()
        index = self.datum_combo.findData(WGS84_INDEX)
        if index >= 0:
            self.datum_combo.setCurrentIndex(index)

    def _write(self) -> None:
        index = self.datum_combo.currentData()
        if index is None:
            return
        if index == USER_DATUM_INDEX:
            QMessageBox.information(
                self,
                "User datum selected",
                "Datum index 3 uses the ellipsoid defined by PMTK331. Write the user datum "
                "below first if you have not already, or the receiver will use whatever was "
                "last programmed.",
            )
        self.send(pmtk.set_datum(index), f"set datum to {datum_label(index)}")
        self.bar.set_state(State.WRITTEN)
        self.read_from_device()

    def _write_user_datum(self) -> None:
        try:
            payload = pmtk.set_datum_advance(
                self.maj_spin.value(),
                self.ecc_spin.value(),
                self.dx_spin.value(),
                self.dy_spin.value(),
                self.dz_spin.value(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid user datum", str(exc))
            return
        self.send(payload, "set user datum")
        self.send(pmtk.query_datum_advance(), "query user datum")

    def read_from_device(self) -> None:
        self.send(pmtk.query_datum(), "query datum")
        self.send(pmtk.query_datum_advance(), "query user datum")

    # -- device feedback -------------------------------------------------

    def on_sentence(self, sentence) -> None:
        if sentence.packet_type != Packet.DT_DATUM:
            return
        index = pmtk.parse_datum(sentence)
        if index is None:
            return
        self.current_label.setText(datum_label(index))
        found = self.datum_combo.findData(index)
        if found < 0:
            # The reported datum is filtered out of the list; clear the filter
            # so the user can see what the receiver is actually set to.
            self.filter_edit.blockSignals(True)
            self.filter_edit.clear()
            self.filter_edit.blockSignals(False)
            self._populate("")
            found = self.datum_combo.findData(index)
        if found >= 0:
            self.datum_combo.blockSignals(True)
            self.datum_combo.setCurrentIndex(found)
            self.datum_combo.blockSignals(False)
        self.bar.set_state(State.CONFIRMED)

    def on_connected(self, is_connected: bool) -> None:
        self.bar.set_enabled(is_connected)
        if not is_connected:
            self.bar.set_state(State.UNKNOWN)
            self.current_label.setText("Receiver has not reported a datum yet.")

    def on_protocol(self, protocol) -> None:
        """Disable this pane when the active protocol cannot perform it."""
        self.require(protocol, Capability.DATUM)
