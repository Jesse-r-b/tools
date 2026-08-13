"""Power saving: periodic modes, AlwaysLocate, DEE tuning and standby.

Every mode here can make the receiver stop answering for a while, which is
indistinguishable from a broken link if you are not expecting it.  The pane says
so next to each control rather than in a manual nobody reads.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QWidget,
)

from .. import pmtk
from ..protocol import Capability
from ..pmtk import (
    PERIODIC_MODE_TEXT,
    PERIODIC_MODES_WITH_TIMING,
    PeriodicMode,
    StandbyType,
)
from .common import Pane, Section, State, hint


class PowerPane(Pane):
    """PMTK225, PMTK223 and PMTK161."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.body.addWidget(self._build_periodic())
        self.body.addWidget(self._build_dee())
        self.body.addWidget(self._build_standby())
        self.body.addStretch(1)

        self._mode_changed()

    # -- construction ----------------------------------------------------

    def _build_periodic(self) -> Section:
        section = Section(
            "Periodic power saving (PMTK225)",
            "In periodic modes the receiver alternates between fixing and sleeping. "
            "AlwaysLocate modes let the firmware decide the duty cycle from motion and signal "
            "conditions instead of fixed timers.",
        )

        self.mode_combo = QComboBox()
        for mode in PeriodicMode:
            self.mode_combo.addItem(PERIODIC_MODE_TEXT[mode], int(mode))
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        section.add_row("Mode", self.mode_combo)

        self.run_spin = self._time_spin(3000)
        section.add_row("Run time", self.run_spin)
        self.sleep_spin = self._time_spin(12000)
        section.add_row("Sleep time", self.sleep_spin)
        self.second_run_spin = self._time_spin(18000)
        section.add_row("Second run time", self.second_run_spin)
        self.second_sleep_spin = self._time_spin(72000)
        section.add_row("Second sleep time", self.second_sleep_spin)

        self.timing_note = QLabel()
        self.timing_note.setWordWrap(True)
        section.add_widget(self.timing_note)

        apply_button = QPushButton("Apply power mode")
        apply_button.clicked.connect(self._apply_periodic)
        section.add_row("", apply_button)

        normal_button = QPushButton("Return to normal mode")
        normal_button.setToolTip("Sends $PMTK225,0 - the way out of any power saving mode")
        normal_button.clicked.connect(self._return_to_normal)
        section.add_row("", normal_button)

        section.add_widget(
            hint(
                "The specification's own worked examples always send $PMTK225,0 first, then "
                "the tuning, then the mode. This tool follows that sequence. "
                "PMTK225 has no query command, so there is no way to read the current mode "
                "back - the only evidence is whether sentences keep arriving."
            )
        )
        return section

    def _time_spin(self, default: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(0, 518_400_000)
        spin.setSingleStep(1000)
        spin.setSuffix(" ms")
        spin.setValue(default)
        spin.setSpecialValueText("0 ms (disabled)")
        spin.setToolTip("0 disables this slot; otherwise the valid range is 1000-518400000 ms")
        return spin

    def _build_dee(self) -> Section:
        section = Section(
            "AlwaysLocate / DEE tuning (PMTK223)",
            "The conditions the receiver must meet before it is willing to sleep: how many "
            "satellites at what C/N0. Looser settings save more power at the cost of a worse "
            "position when it wakes.",
        )

        self.sv_spin = QSpinBox()
        self.sv_spin.setRange(1, 4)
        self.sv_spin.setValue(1)
        section.add_row("Satellites required", self.sv_spin)

        self.snr_spin = QSpinBox()
        self.snr_spin.setRange(25, 30)
        self.snr_spin.setValue(30)
        self.snr_spin.setSuffix(" dB-Hz")
        section.add_row("C/N0 required", self.snr_spin)

        self.threshold_spin = QSpinBox()
        self.threshold_spin.setRange(40_000, 180_000)
        self.threshold_spin.setSingleStep(10_000)
        self.threshold_spin.setValue(180_000)
        self.threshold_spin.setSuffix(" ms")
        section.add_row("Extension threshold", self.threshold_spin)

        self.gap_spin = QSpinBox()
        self.gap_spin.setRange(0, 3_600_000)
        self.gap_spin.setSingleStep(10_000)
        self.gap_spin.setValue(60_000)
        self.gap_spin.setSuffix(" ms")
        section.add_row("Extension gap", self.gap_spin)

        apply_button = QPushButton("Apply DEE tuning")
        apply_button.clicked.connect(self._apply_dee)
        section.add_row("", apply_button)

        section.add_widget(
            hint(
                "Ranges are exactly as the specification gives them (table 2-24) and are "
                "enforced before sending. The defaults shown are the specification's defaults."
            )
        )
        return section

    def _build_standby(self) -> Section:
        section = Section(
            "Standby (PMTK161)",
            "Puts the receiver to sleep immediately. It stops emitting NMEA until it is woken, "
            "and on this hardware the only reliable wake is unplugging and replugging the USB "
            "connector.",
        )

        self.standby_combo = QComboBox()
        self.standby_combo.addItem("Stop mode", int(StandbyType.STOP))
        self.standby_combo.addItem("Sleep mode", int(StandbyType.SLEEP))
        section.add_row("Standby type", self.standby_combo)

        standby_button = QPushButton("Enter standby")
        standby_button.clicked.connect(self._enter_standby)
        section.add_row("", standby_button)

        section.add_widget(
            hint(
                "Two consequences worth knowing before you press this: entering standby "
                "reverts a PMTK251 baud rate change back to the default, and this tool will "
                "report the link as silent because it is."
            )
        )
        return section

    # -- interaction -----------------------------------------------------

    def _mode_changed(self) -> None:
        mode = PeriodicMode(self.mode_combo.currentData())
        needs_timing = mode in PERIODIC_MODES_WITH_TIMING
        for spin in (
            self.run_spin,
            self.sleep_spin,
            self.second_run_spin,
            self.second_sleep_spin,
        ):
            spin.setEnabled(needs_timing)

        if needs_timing:
            self.timing_note.setText(
                "Timing applies. The second run time must be larger than the first when it is "
                "non-zero, and the hardware sleep is capped at 2047 s - longer intervals are "
                "extended in firmware, which powers the GNSS back on for the extension."
            )
        elif mode is PeriodicMode.NORMAL:
            self.timing_note.setText("Normal mode: continuous fixing, no power saving.")
        else:
            self.timing_note.setText(
                f"{PERIODIC_MODE_TEXT[mode]} takes no timing arguments - the firmware chooses "
                "the duty cycle itself."
            )

    def _apply_periodic(self) -> None:
        mode = PeriodicMode(self.mode_combo.currentData())

        if mode is not PeriodicMode.NORMAL:
            answer = QMessageBox.question(
                self,
                "Apply power saving mode",
                f"Put the receiver into {PERIODIC_MODE_TEXT[mode]}?\n\n"
                "It will stop emitting NMEA for part or all of each cycle, so the live view "
                "will go intermittent. Use 'Return to normal mode' to undo this.",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Ok:
                return

        try:
            # The specification's examples always drop to normal mode first.
            self.send(pmtk.periodic_mode(PeriodicMode.NORMAL), "return to normal mode")
            if mode in PERIODIC_MODES_WITH_TIMING:
                self.send(self._dee_payload(), "set AlwaysLocate / DEE tuning")
                payload = pmtk.periodic_mode(
                    mode,
                    self.run_spin.value(),
                    self.sleep_spin.value(),
                    self.second_run_spin.value(),
                    self.second_sleep_spin.value(),
                )
            else:
                payload = pmtk.periodic_mode(mode)
            self.send(payload, f"set {PERIODIC_MODE_TEXT[mode]}")
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid power mode", str(exc))
            return

    def _return_to_normal(self) -> None:
        self.send(pmtk.periodic_mode(PeriodicMode.NORMAL), "return to normal mode")
        index = self.mode_combo.findData(int(PeriodicMode.NORMAL))
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)

    def _dee_payload(self) -> str:
        return pmtk.al_dee_config(
            self.sv_spin.value(),
            self.snr_spin.value(),
            self.threshold_spin.value(),
            self.gap_spin.value(),
        )

    def _apply_dee(self) -> None:
        try:
            self.send(self._dee_payload(), "set AlwaysLocate / DEE tuning")
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid DEE tuning", str(exc))

    def _enter_standby(self) -> None:
        kind = StandbyType(self.standby_combo.currentData())
        answer = QMessageBox.warning(
            self,
            "Enter standby",
            f"Put the receiver into {kind.name.lower()} mode?\n\n"
            "It will stop emitting NMEA entirely. On USB hardware the reliable way back is to "
            "unplug and reconnect. Any baud rate set with PMTK251 also reverts to the default.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return
        self.send(pmtk.standby_mode(kind), f"enter {kind.name.lower()} standby")

    def on_connected(self, is_connected: bool) -> None:
        self.setEnabled(is_connected)

    def on_protocol(self, protocol) -> None:
        """Disable this pane when the active protocol cannot perform it."""
        self.require(protocol, Capability.POWER_MODES)
