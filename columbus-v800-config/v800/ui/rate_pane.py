"""Update rate, NMEA sentence selection and port baud rate.

The three settings on this pane interact, which is why they share a pane: the
sentence set and the fix rate together decide how many bits per second the port
must carry, and asking for more than the baud rate allows produces truncated
sentences rather than an error.  The link budget readout makes that visible
before you write it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QWidget,
)

from .. import casic, pmtk
from ..protocol import Capability, CasicProtocol, Kind
from ..pmtk import (
    BAUD_RATES,
    NMEA_OUTPUT_DESCRIPTIONS,
    NMEA_OUTPUT_FIELDS,
    NMEA_RATE_CHOICES,
    Packet,
)
from .common import Pane, ReadWriteBar, Section, State, WrapLabel, hint

#: Presets offered next to the raw millisecond box, matching the rates the
#: Columbus V-800 MarkIII product page advertises (1, 2, 5 and 10 Hz).
RATE_PRESETS = (
    ("1 Hz (default)", 1000),
    ("2 Hz", 500),
    ("5 Hz", 200),
    ("10 Hz", 100),
)


class RatePane(Pane):
    """Fix interval, per-sentence output divisors, and port speed."""

    #: Emitted when the user asks to change the port baud rate, so the main
    #: window can reopen the link at the new speed.
    baud_change_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.bar = ReadWriteBar(parent=self)
        self.bar.read_requested.connect(self.read_from_device)
        self.bar.write_requested.connect(self._write)
        self.body.addWidget(self.bar)

        self.body.addWidget(self._build_rate_section())
        self.body.addWidget(self._build_sentence_section())
        self.body.addWidget(self._build_baud_section())
        self.body.addStretch(1)

        self._actual_baud: int | None = None
        self._update_budget()

    def set_actual_baud(self, baud: int | None) -> None:
        """Tell the pane what the port is *actually* running at.

        The budget must be measured against the live port speed, not against
        whatever is selected in the 'change baud rate' box. Those differ by
        default -- the box offers 38400 because that is the documented family
        default, while the unit on the bench came up at 9600 -- and using the
        wrong denominator turns a 60%-loaded port into a reassuring 15%.
        """
        self._actual_baud = baud
        self._update_budget()

    # -- construction ----------------------------------------------------

    def _build_rate_section(self) -> Section:
        self.rate_section = section = Section(
            "Position fix rate",
            "PMTK220 sets the interval between fixes. The specification gives a minimum of "
            "100 ms for PMTK220 and 100-10000 ms for PMTK300, but the PMTK500 reply documents "
            "a floor of 200 ms - so the receiver may report back a slower rate than you asked "
            "for. That is the chipset disagreeing with its own datasheet, not an error here.",
        )

        self.preset_combo = QComboBox()
        for label, interval in RATE_PRESETS:
            self.preset_combo.addItem(label, interval)
        self.preset_combo.addItem("Custom", None)
        self.preset_combo.currentIndexChanged.connect(self._preset_chosen)
        section.add_row("Preset", self.preset_combo)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(100, 10000)
        self.interval_spin.setSingleStep(100)
        self.interval_spin.setValue(1000)
        self.interval_spin.setSuffix(" ms")
        self.interval_spin.valueChanged.connect(self._interval_changed)
        section.add_row("Interval", self.interval_spin)

        self.rate_label = QLabel("1.00 Hz")
        section.add_row("Equivalent rate", self.rate_label)

        self.use_fix_ctl = QComboBox()
        self.use_fix_ctl.addItem("PMTK220 (SET_POS_FIX)", "220")
        self.use_fix_ctl.addItem("PMTK300 (API_SET_FIX_CTL)", "300")
        self.use_fix_ctl_row = self.use_fix_ctl
        self.use_fix_ctl.setToolTip(
            "Two commands set the same thing. PMTK220 is the one MiniGPS uses; "
            "PMTK300 is the API form and is the one PMTK400 queries."
        )
        section.add_row("Command to use", self.use_fix_ctl)
        return section

    def _build_sentence_section(self) -> Section:
        self.sentence_section = section = Section(
            "NMEA sentence output",
            "PMTK314 sets how often each sentence is emitted, as a divisor of the fix rate: "
            "1 = every fix, 5 = every fifth fix, 0 = off. The receiver always transmits all "
            "19 fields; the twelve the specification does not name are reserved and sent as 0.",
        )

        self._sentence_grid = grid = QGridLayout()
        grid.addWidget(QLabel("<b>Sentence</b>"), 0, 0)
        grid.addWidget(QLabel("<b>Rate</b>"), 0, 1)
        grid.addWidget(QLabel("<b>Meaning</b>"), 0, 2)
        grid.addWidget(QLabel("<b>Seen</b>"), 0, 3)
        grid.setColumnStretch(2, 1)

        self.rate_combos: dict[str, QComboBox] = {}
        self.seen_labels: dict[str, QLabel] = {}
        self._descriptions = dict(NMEA_OUTPUT_DESCRIPTIONS)
        for row, name in enumerate(NMEA_OUTPUT_FIELDS.values(), start=1):
            grid.addWidget(QLabel(name), row, 0)

            combo = QComboBox()
            for choice in NMEA_RATE_CHOICES:
                combo.addItem("off" if choice == 0 else f"every {choice}", choice)
            combo.setCurrentIndex(1)  # every fix
            combo.currentIndexChanged.connect(self._mark_edited)
            grid.addWidget(combo, row, 1)
            self.rate_combos[name] = combo

            description = QLabel(self._descriptions.get(name, ""))
            description.setStyleSheet("color: palette(mid);")
            grid.addWidget(description, row, 2)

            seen = QLabel("--")
            seen.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            seen.setToolTip("How many of this sentence have actually arrived since connecting")
            grid.addWidget(seen, row, 3)
            self.seen_labels[name] = seen

        holder = QWidget()
        holder.setLayout(grid)
        section.add_widget(holder)

        self.budget_label = WrapLabel()
        section.add_widget(self.budget_label)

        buttons = QWidget()
        button_row = QGridLayout(buttons)
        button_row.setContentsMargins(0, 0, 0, 0)

        all_on = QPushButton("All on (every fix)")
        all_on.clicked.connect(lambda: self._set_all(1))
        button_row.addWidget(all_on, 0, 0)

        minimal = QPushButton("Minimal (RMC + GGA)")
        minimal.setToolTip("Position, time, speed and fix quality only - the smallest useful set")
        minimal.clicked.connect(self._set_minimal)
        button_row.addWidget(minimal, 0, 1)

        all_off = QPushButton("All off")
        all_off.clicked.connect(lambda: self._set_all(0))
        button_row.addWidget(all_off, 0, 2)

        self.restore_button = restore = QPushButton("Restore receiver default")
        restore.setToolTip("Sends $PMTK314,-1, which restores the firmware's own default set")
        restore.clicked.connect(self._restore_defaults)
        button_row.addWidget(restore, 0, 3)

        section.add_widget(buttons)
        return section

    def _build_baud_section(self) -> Section:
        self.baud_section = section = Section(
            "Port baud rate",
            "PMTK251 changes the speed of the serial port itself. The link is reopened at the "
            "new rate immediately after the command is acknowledged. Note the specification's "
            "warning: a full cold start (PMTK104) or entering standby reverts this to the "
            "default, so the port speed can change without you asking.",
        )

        self.baud_combo = QComboBox()
        for baud in BAUD_RATES:
            self.baud_combo.addItem("Receiver default" if baud == 0 else f"{baud} baud", baud)
        self.baud_combo.setCurrentIndex(BAUD_RATES.index(pmtk.DEFAULT_BAUD))
        section.add_row("New baud rate", self.baud_combo)

        apply_button = QPushButton("Change port speed")
        apply_button.clicked.connect(self._change_baud)
        section.add_row("", apply_button)

        section.add_widget(
            hint(
                "The V-800 family ships at 38400 baud. If the link goes quiet after a change, "
                "use Connection > Detect baud rate to sweep the documented rates."
            )
        )
        return section

    # -- interaction -----------------------------------------------------

    def on_protocol(self, protocol) -> None:
        """Rebuild the sentence list for whichever protocol is in use.

        PMTK and CASIC control overlapping but different sentence sets (CASIC
        exposes TXT, PMTK does not; PMTK's set is fixed at 19 fields), and the
        legal rate divisors differ too. Rebuilding from the protocol keeps the
        pane honest instead of showing PMTK's list to a CASIC receiver.
        """
        self._protocol = protocol
        if not self.require(protocol, Capability.FIX_RATE, Capability.SENTENCE_RATES):
            return

        names = protocol.sentence_names()
        choices = protocol.rate_choices()
        if isinstance(protocol, CasicProtocol):
            self._descriptions = dict(casic.NMEA_DESCRIPTIONS)
        else:
            self._descriptions = dict(NMEA_OUTPUT_DESCRIPTIONS)
        self._rebuild_sentences(names, choices)
        self.baud_section.setEnabled(protocol.supports(Capability.PORT_BAUD))
        self._retext(protocol)

    def _retext(self, protocol) -> None:
        """Rewrite the explanatory text for the protocol actually in use.

        Leaving PMTK command numbers on screen while talking CASIC is exactly
        the incoherence this rewrite removes: the tool would be documenting one
        protocol and speaking another.
        """
        casic_mode = protocol.kind is Kind.CASIC
        # The PMTK/PMTK300 choice is meaningless outside PMTK.
        self.use_fix_ctl.setVisible(not casic_mode)
        label = self.rate_section.form.labelForField(self.use_fix_ctl)
        if label is not None:
            label.setVisible(not casic_mode)

        if casic_mode:
            self.rate_section.set_note(
                "CFG-RATE (0x06/0x04) sets the measurement interval. Verified against this "
                "receiver by writing and measuring the cadence. Note the receiver will not "
                "reach a rate the serial link cannot carry: at 9600 baud with the full "
                "sentence set, a 5 Hz request measured 1.67 Hz."
            )
            self.sentence_section.set_note(
                "CFG-MSG (0x06/0x01) sets how often each sentence is emitted, as a divisor "
                "of the fix rate: 1 = every fix, 5 = every fifth fix, 0 = off. Each sentence "
                "is a separate frame, and every id below was verified individually against "
                "this receiver."
            )
            self.restore_button.setVisible(False)
        else:
            self.rate_section.set_note(
                "PMTK220 sets the interval between fixes. The specification gives a minimum "
                "of 100 ms for PMTK220 and 100-10000 ms for PMTK300, but the PMTK500 reply "
                "documents a floor of 200 ms - so the receiver may report back a slower rate "
                "than you asked for."
            )
            self.sentence_section.set_note(
                "PMTK314 sets how often each sentence is emitted, as a divisor of the fix "
                "rate: 1 = every fix, 5 = every fifth fix, 0 = off. The receiver always "
                "transmits all 19 fields; the twelve the specification does not name are "
                "reserved and sent as 0."
            )
            self.restore_button.setVisible(True)

    def _rebuild_sentences(self, names, choices) -> None:
        grid = self._sentence_grid
        # Drop everything below the header row.
        while grid.count() > 4:
            item = grid.takeAt(grid.count() - 1)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.rate_combos.clear()
        self.seen_labels.clear()

        for row, name in enumerate(names, start=1):
            grid.addWidget(QLabel(name), row, 0)
            combo = QComboBox()
            for choice in choices:
                combo.addItem("off" if choice == 0 else f"every {choice}", choice)
            combo.setCurrentIndex(min(1, combo.count() - 1))
            combo.currentIndexChanged.connect(self._mark_edited)
            grid.addWidget(combo, row, 1)
            self.rate_combos[name] = combo

            description = QLabel(self._descriptions.get(name, ""))
            description.setStyleSheet("color: palette(mid);")
            grid.addWidget(description, row, 2)

            seen = QLabel("--")
            seen.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(seen, row, 3)
            self.seen_labels[name] = seen
        self._update_budget()

    def _mark_edited(self) -> None:
        self.bar.set_state(State.EDITED)
        self._update_budget()

    def _preset_chosen(self) -> None:
        interval = self.preset_combo.currentData()
        if interval is not None and interval != self.interval_spin.value():
            self.interval_spin.setValue(interval)

    def _interval_changed(self, value: int) -> None:
        self.rate_label.setText(f"{1000.0 / value:.2f} Hz")
        matching = next(
            (index for index, (_, interval) in enumerate(RATE_PRESETS) if interval == value),
            self.preset_combo.count() - 1,
        )
        if self.preset_combo.currentIndex() != matching:
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(matching)
            self.preset_combo.blockSignals(False)
        self._mark_edited()

    def _set_all(self, rate: int) -> None:
        for combo in self.rate_combos.values():
            combo.setCurrentIndex(NMEA_RATE_CHOICES.index(rate))

    def _set_minimal(self) -> None:
        for name, combo in self.rate_combos.items():
            wanted = 1 if name in ("RMC", "GGA") else 0
            combo.setCurrentIndex(NMEA_RATE_CHOICES.index(wanted))

    def _restore_defaults(self) -> None:
        self.send(pmtk.restore_nmea_output_defaults(), "restore default NMEA output set")
        self.bar.set_state(State.WRITTEN, "default set requested")
        self.send("PMTK414", "query NMEA output")

    def current_rates(self) -> dict[str, int]:
        return {name: combo.currentData() for name, combo in self.rate_combos.items()}

    def _update_budget(self) -> None:
        rates = self.current_rates()
        interval = self.interval_spin.value()
        needed = pmtk.nmea_budget_bps(rates, interval)

        baud = getattr(self, "_actual_baud", None)
        if baud:
            source = "the connected port"
        else:
            baud = (self.baud_combo.currentData() if hasattr(self, "baud_combo") else None) \
                or pmtk.DEFAULT_BAUD
            source = "the selected rate - not connected"

        headroom = baud - needed
        text = (
            f"Estimated load: <b>{needed:,.0f} bit/s</b> of {baud:,} available "
            f"({needed / baud * 100:.0f}% of {source})."
        )
        if headroom < 0:
            text += (
                " <b>This will not fit.</b> The receiver truncates sentences when the port "
                "cannot keep up, which shows as checksum errors rather than as an error "
                "message. Raise the baud rate, lower the fix rate, or turn off GSV."
            )
            self.budget_label.setStyleSheet("color: #c03f3f;")
        elif headroom < baud * 0.2:
            text += " Little headroom - expect occasional truncation."
            self.budget_label.setStyleSheet("color: #c08a2f;")
        else:
            self.budget_label.setStyleSheet("")
        text += (
            "<br><span style='color: palette(mid);'>Estimate only: sentence lengths vary with "
            "the number of satellites, and GSV grows the most.</span>"
        )
        self.budget_label.setText(text)

    def _write(self) -> None:
        interval = self.interval_spin.value()
        protocol = getattr(self, "_protocol", None)
        try:
            if protocol is None or protocol.kind is Kind.PMTK:
                if self.use_fix_ctl.currentData() == "300":
                    self.send(pmtk.set_fix_ctl(interval), f"set fix interval to {interval} ms")
                else:
                    self.send(pmtk.set_pos_fix(interval), f"set fix interval to {interval} ms")
                self.send(pmtk.set_nmea_output(self.current_rates()), "set NMEA sentence output")
            else:
                self.send_bytes(
                    protocol.set_fix_interval(interval), f"set fix interval to {interval} ms"
                )
                for frame in protocol.set_sentence_rates(self.current_rates()):
                    self.send_bytes(frame, "set sentence rate")
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid setting", str(exc))
            self.bar.set_state(State.FAILED, str(exc))
            return

        self.bar.set_state(State.WRITTEN)
        self.read_from_device()

    def _change_baud(self) -> None:
        baud = self.baud_combo.currentData()
        target = baud or pmtk.DEFAULT_BAUD
        answer = QMessageBox.question(
            self,
            "Change port speed",
            f"Send $PMTK251,{baud} and reopen the port at {target} baud?\n\n"
            "If the receiver does not accept the change the link will go quiet; "
            "use Connection > Detect baud rate to recover.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return
        protocol = getattr(self, "_protocol", None)
        try:
            if isinstance(protocol, CasicProtocol):
                self.send_bytes(protocol.set_port_baud(target), f"set port baud to {target}")
            else:
                self.send(pmtk.set_nmea_baudrate(baud), f"set port baud rate to {target}")
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot change baud rate", str(exc))
            return
        self.baud_change_requested.emit(target)

    # -- device feedback -------------------------------------------------

    def read_from_device(self) -> None:
        protocol = getattr(self, "_protocol", None)
        if protocol is None or protocol.kind is Kind.PMTK:
            self.send(pmtk.query_fix_ctl(), "query fix interval")
            self.send("PMTK414", "query NMEA output")
            return
        self.send_bytes(protocol.poll_fix_interval(), "poll fix interval")
        self.send_bytes(protocol.poll_sentence_rates(), "poll sentence rates")
        if hasattr(protocol, "poll_port"):
            self.send_bytes(protocol.poll_port(), "poll port configuration")

    def on_casic_frame(self, frame) -> None:
        """Update the fields from a CASIC reply."""
        if not frame.checksum_ok:
            return
        interval = casic.parse_fix_interval(frame)
        if interval is not None and interval > 0:
            self.interval_spin.blockSignals(True)
            self.interval_spin.setValue(max(100, min(10000, interval)))
            self.rate_label.setText(f"{1000.0 / interval:.2f} Hz")
            self.interval_spin.blockSignals(False)
            self._confirm()

        rates = casic.collect_sentence_rates([frame])
        for name, rate in rates.items():
            combo = self.rate_combos.get(name)
            if combo is None:
                continue
            index = combo.findData(rate)
            if index < 0:
                combo.addItem(f"every {rate}", rate)
                index = combo.count() - 1
            combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)
            self._confirm()

        port = casic.parse_port_config(frame)
        if port is not None:
            protocol = getattr(self, "_protocol", None)
            if isinstance(protocol, CasicProtocol) and port.port_id == casic.USB_PORT_ID:
                protocol.port_config = port
                index = self.baud_combo.findData(port.baud)
                if index >= 0:
                    self.baud_combo.blockSignals(True)
                    self.baud_combo.setCurrentIndex(index)
                    self.baud_combo.blockSignals(False)

    def on_sentence(self, sentence) -> None:
        packet = sentence.packet_type
        if packet == Packet.DT_FIX_CTL:
            interval = pmtk.parse_fix_ctl(sentence)
            if interval is not None:
                self.interval_spin.blockSignals(True)
                self.interval_spin.setValue(max(100, min(10000, interval)))
                self.rate_label.setText(f"{1000.0 / max(1, interval):.2f} Hz")
                self.interval_spin.blockSignals(False)
                self._confirm()
        elif packet == Packet.DT_NMEA_OUTPUT:
            rates = pmtk.parse_nmea_output(sentence)
            if rates:
                for name, value in rates.items():
                    combo = self.rate_combos.get(name)
                    if combo is None or value not in NMEA_RATE_CHOICES:
                        continue
                    combo.blockSignals(True)
                    combo.setCurrentIndex(NMEA_RATE_CHOICES.index(value))
                    combo.blockSignals(False)
                self._confirm()

    def _confirm(self) -> None:
        self.bar.set_state(State.CONFIRMED)
        self._update_budget()

    def update_seen_counts(self, seen: dict[str, int]) -> None:
        """Refresh the "Seen" column from what has actually arrived."""
        for name, label in self.seen_labels.items():
            total = sum(count for address, count in seen.items() if address.endswith(name))
            label.setText(str(total) if total else "--")
            wanted = self.rate_combos[name].currentData()
            # A sentence configured on but never seen is the clearest possible
            # sign the write did not take -- flag it rather than leaving the
            # user to compare two columns by eye.
            if wanted and not total:
                label.setStyleSheet("color: #c03f3f;")
                label.setToolTip("Configured on, but none have arrived")
            elif not wanted and total:
                label.setStyleSheet("color: #c08a2f;")
                label.setToolTip("Configured off, but still arriving")
            else:
                label.setStyleSheet("")
                label.setToolTip("")

    def on_connected(self, is_connected: bool) -> None:
        self.bar.set_enabled(is_connected)
        if not is_connected:
            self.bar.set_state(State.UNKNOWN)
