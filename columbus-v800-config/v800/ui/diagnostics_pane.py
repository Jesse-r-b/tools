"""Diagnostics: firmware identity, link health, TTFF timing and the RF test mode.

Three different kinds of measurement live here, and they are worth telling apart:

* **Link health** is measured from the data stream and is always trustworthy.
* **TTFF** is measured by this tool with a monotonic clock, from the moment the
  restart command is written to the moment a valid fix arrives.  It is honest
  but it includes USB and scheduling latency, so treat sub-second differences
  as noise.
* **The PMTK81x test mode** figures come from the chipset and are reported in
  units the specification is internally inconsistent about -- see
  docs/spec-errata.md.  They are useful for comparing one run against another on
  the same unit, not as calibrated absolute values.  The pane says so.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from .. import pmtk
from ..nmea import NavState
from ..pmtk import (
    SYS_MSG_TEXT,
    AcqResult,
    BitsyncResult,
    Packet,
    SignalResult,
    SysMsg,
    TEST_ITEM_TEXT,
    TestItem,
)
from .common import Pane, Section, WrapLabel, hint, monospace


class DiagnosticsPane(Pane):
    """Everything that measures rather than configures."""

    def __init__(self, nav: NavState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._nav = nav

        self._ttff_started_at: float | None = None
        self._ttff_label_text = "--"
        self._last_sentence_at: float | None = None
        self._byte_window: list[tuple[float, int]] = []
        self._probe_replies = 0
        self._probe_running = False

        self.body.addWidget(self._build_identity())
        self.body.addWidget(self._build_link())
        self.body.addWidget(self._build_ttff())
        self.body.addWidget(self._build_test_mode())
        self.body.addStretch(1)

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    # -- construction ----------------------------------------------------

    def _build_identity(self) -> Section:
        section = Section(
            "Receiver identity",
            "PMTK605 asks the firmware to identify itself. The release string tells you which "
            "core the unit is built on - AXN_x.x is the MT3329/MT3333 family - which decides "
            "which of the firmware-dependent commands are likely to work.",
        )

        self.release_label = WrapLabel("--")
        self.release_label.setWordWrap(True)
        monospace(self.release_label)
        section.add_row("Firmware release", self.release_label)

        self.sysmsg_label = WrapLabel("--")
        self.sysmsg_label.setWordWrap(True)
        section.add_row("Last system message", self.sysmsg_label)

        self.txtmsg_label = WrapLabel("--")
        self.txtmsg_label.setWordWrap(True)
        monospace(self.txtmsg_label)
        section.add_row("Last text message", self.txtmsg_label)

        self.protocol_label = WrapLabel("not identified yet")
        self.protocol_label.setToolTip(
            "Which command language this receiver answers. Determined by probing, "
            "not assumed from the model number."
        )
        section.add_row("Command protocol", self.protocol_label)

        self.antenna_label = WrapLabel("--")
        self.antenna_label.setWordWrap(True)
        self.antenna_label.setToolTip(
            "Reported unprompted by the receiver in GPTXT sentences. "
            "ANTENNA OPEN means nothing is connected or the feed is broken; "
            "ANTENNA SHORT means the bias line is shorted."
        )
        section.add_row("Antenna", self.antenna_label)

        self.tcxo_label = WrapLabel("--")
        self.tcxo_label.setWordWrap(True)
        section.add_row("TCXO drift (PMTK589)", self.tcxo_label)

        query = QPushButton("Query firmware release")
        query.clicked.connect(lambda: self.send(pmtk.query_release(), "query firmware release"))
        section.add_row("", query)

        ping = QPushButton("Send test packet (PMTK000)")
        ping.setToolTip("The cheapest possible liveness check: expect $PMTK001,0,3 back")
        ping.clicked.connect(lambda: self.send("PMTK000", "test packet"))
        section.add_row("", ping)

        check = QPushButton("Check command path")
        check.setToolTip(
            "Sends three harmless queries and reports whether the receiver answers any of "
            "them. Run this first if settings appear not to apply."
        )
        check.clicked.connect(self._check_command_path)
        section.add_row("", check)

        self.command_path_label = WrapLabel("Not checked")
        self.command_path_label.setWordWrap(True)
        section.add_row("Command path", self.command_path_label)

        section.add_widget(
            hint(
                "Receiving NMEA proves only that the device-to-host direction works. The "
                "V-800 MarkIII answers nothing here because it is not a MediaTek part: it "
                "parses u-blox UBX framing (replying ACK-NAK) and answers the CASIC binary "
                "protocol, but has no PMTK support at all. The host-to-device path is fine; "
                "the protocol is wrong. See docs/protocol-investigation.md."
            )
        )
        return section

    def _check_command_path(self) -> None:
        """Probe whether the receiver answers commands at all.

        Deliberately uses three different query packets: if the firmware happens
        not to implement one, the other two still settle the question.
        """
        self._probe_replies = 0
        self._probe_running = True
        self.command_path_label.setText("Probing...")
        self.command_path_label.setStyleSheet("")
        for payload, description in (
            ("PMTK000", "test packet"),
            (pmtk.query_release(), "query firmware release"),
            ("PMTK414", "query NMEA output"),
        ):
            self.send(payload, description)
        QTimer.singleShot(3000, self._report_command_path)

    def _report_command_path(self) -> None:
        self._probe_running = False
        if self._probe_replies:
            self.command_path_label.setText(
                f"Working - the receiver answered {self._probe_replies} of 3 probes. "
                "Settings written from this tool will take effect."
            )
            self.command_path_label.setStyleSheet("color: #2f9e5f;")
        else:
            self.command_path_label.setText(
                "No reply to any of 3 PMTK probes. This receiver streams NMEA but does not "
                "implement PMTK, so nothing this tool writes will take effect. On the V-800 "
                "MarkIII the cause is the protocol, not the wiring: it answers CASIC binary "
                "commands instead. Reading and diagnostics work normally."
            )
            self.command_path_label.setStyleSheet("color: #c03f3f;")

    def _build_link_placeholder(self) -> None:
        return

    def _build_link(self) -> Section:
        section = Section(
            "Link health",
            "Measured from the data stream itself, not from anything the receiver claims. "
            "A rising checksum error count with a healthy sentence rate usually means the "
            "port is saturated - check the load estimate on the Rate & Output tab.",
        )

        self.stats: dict[str, QLabel] = {}
        for key, label in (
            ("sentences", "Sentences decoded"),
            ("errors", "Checksum errors"),
            ("rate", "Sentence rate"),
            ("throughput", "Throughput"),
            ("silence", "Time since last sentence"),
        ):
            widget = QLabel("--")
            section.add_row(label, widget)
            self.stats[key] = widget

        self.sentence_table = QTableWidget(0, 2, self)
        self.sentence_table.setHorizontalHeaderLabels(["Sentence", "Count"])
        self.sentence_table.verticalHeader().setVisible(False)
        self.sentence_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.sentence_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.sentence_table.setMaximumHeight(180)
        section.add_widget(self.sentence_table)

        reset = QPushButton("Reset counters")
        reset.clicked.connect(self._reset_counters)
        section.add_row("", reset)
        return section

    def _build_ttff(self) -> Section:
        section = Section(
            "Time to first fix",
            "Timed by this tool from the moment the restart command is written to the moment "
            "the first valid fix arrives. It includes USB and host scheduling latency, so it "
            "reads slightly high - treat differences under a second as noise.",
        )

        self.ttff_label = QLabel("--")
        monospace(self.ttff_label)
        section.add_row("Result", self.ttff_label)

        for label, payload in (
            ("Time a hot start", pmtk.hot_start()),
            ("Time a warm start", pmtk.warm_start()),
            ("Time a cold start", pmtk.cold_start()),
        ):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, p=payload, l=label: self._start_ttff(p, l))
            section.add_row("", button)

        section.add_widget(
            hint(
                "A hot start that is fast and a cold start that never completes points at the "
                "antenna or the sky view rather than the receiver. Run these outdoors with a "
                "clear view; a cold start indoors proves nothing."
            )
        )
        return section

    def _build_test_mode(self) -> Section:
        section = Section(
            "RF test mode (PMTK810)",
            "Puts the receiver into manufacturing test mode against a single satellite and "
            "reports acquisition time, bit sync time and signal quality. The receiver stops "
            "normal navigation while this runs.",
        )

        self.test_checks: dict[TestItem, QCheckBox] = {}
        for item in (TestItem.INFO, TestItem.ACQ, TestItem.BITSYNC, TestItem.SIGNAL):
            check = QCheckBox(f"{item.name} - {TEST_ITEM_TEXT[item]}")
            check.setChecked(item in (TestItem.INFO, TestItem.ACQ))
            section.add_widget(check)
            self.test_checks[item] = check

        self.test_svid = QSpinBox()
        self.test_svid.setRange(1, 20)
        self.test_svid.setValue(1)
        self.test_svid.setToolTip(
            "The specification restricts the test SV id to 1-20, so satellites above PRN 20 "
            "cannot be selected even if they are the strongest in view."
        )
        section.add_row("Satellite (PRN)", self.test_svid)

        start = QPushButton("Enter test mode")
        start.clicked.connect(self._start_test)
        section.add_row("", start)

        stop = QPushButton("Leave test mode (PMTK811)")
        stop.clicked.connect(lambda: self.send(pmtk.test_stop(), "leave test mode"))
        section.add_row("", stop)

        self.test_results = WrapLabel("--")
        self.test_results.setWordWrap(True)
        monospace(self.test_results)
        section.add_row("Results", self.test_results)

        self.jam_spin = QSpinBox()
        self.jam_spin.setRange(1, 1000)
        self.jam_spin.setValue(50)
        section.add_row("Jamming scan count", self.jam_spin)

        jam = QPushButton("Run jamming scan (PMTK837)")
        jam.setToolTip("Sweeps for in-band interference. Results appear in the raw console.")
        jam.clicked.connect(self._run_jamming)
        section.add_row("", jam)

        section.add_widget(
            hint(
                "The signal figures use the scale factors from the specification's Unit column. "
                "The specification's own worked example does not reproduce with those factors "
                "(see docs/spec-errata.md), so compare runs against each other on the same "
                "unit rather than treating the absolute numbers as calibrated."
            )
        )
        return section

    # -- interaction -----------------------------------------------------

    def _reset_counters(self) -> None:
        self._nav.sentence_count = 0
        self._nav.checksum_errors = 0
        self._nav.seen.clear()
        self._byte_window.clear()

    def _start_ttff(self, payload: str, label: str) -> None:
        self._ttff_started_at = time.monotonic()
        self._ttff_label_text = f"{label}: timing..."
        self.ttff_label.setText(self._ttff_label_text)
        # Clear the fix so a stale one does not stop the clock instantly.  This
        # is the whole measurement: without it, the previous fix satisfies the
        # "have we got a fix" test on the very next sentence and TTFF reads 0.
        self._nav.fix.quality = self._nav.fix.quality.__class__.INVALID
        self._nav.fix.latitude = None
        self._nav.fix.longitude = None
        self.send(payload, label.lower())

    def _start_test(self) -> None:
        bitmap = 0
        for item, check in self.test_checks.items():
            if check.isChecked():
                bitmap |= int(item)
        if not bitmap:
            QMessageBox.warning(self, "No test items", "Select at least one test item.")
            return

        answer = QMessageBox.question(
            self,
            "Enter test mode",
            "Put the receiver into manufacturing test mode?\n\n"
            "Normal navigation stops until you leave test mode with PMTK811.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return

        try:
            payload = pmtk.test_all(bitmap, self.test_svid.value())
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid test parameters", str(exc))
            return
        self.test_results.setText("Test running...")
        self.send(payload, "enter test mode")

    def _run_jamming(self) -> None:
        try:
            self.send(
                pmtk.test_jamming(True, self.jam_spin.value()),
                f"jamming scan x{self.jam_spin.value()}",
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid scan count", str(exc))

    # -- device feedback -------------------------------------------------

    def note_line(self, line: str) -> None:
        """Called for every received line, to measure throughput."""
        now = time.monotonic()
        self._last_sentence_at = now
        self._byte_window.append((now, len(line) + 2))  # + CR LF
        cutoff = now - 5.0
        while self._byte_window and self._byte_window[0][0] < cutoff:
            self._byte_window.pop(0)

    def on_sentence(self, sentence) -> None:
        packet = sentence.packet_type

        if self._probe_running and sentence.is_pmtk:
            self._probe_replies += 1

        if packet == Packet.DT_RELEASE:
            release = pmtk.parse_release(sentence)
            if release is not None:
                self.release_label.setText(str(release))
        elif packet == Packet.SYS_MSG and sentence.fields:
            try:
                message = SysMsg(int(sentence.fields[0]))
                self.sysmsg_label.setText(f"{int(message)} - {SYS_MSG_TEXT[message]}")
            except ValueError:
                self.sysmsg_label.setText(sentence.raw)
        elif packet == Packet.TXT_MSG and sentence.fields:
            self.txtmsg_label.setText(sentence.fields[0])

        # GPTXT (standard NMEA, not PMTK) carries the antenna state.
        if not sentence.is_pmtk and sentence.formatter.upper() == "TXT":
            status = self._nav.antenna_status
            if status:
                self.antenna_label.setText(status)
                bad = "OK" not in status.upper()
                self.antenna_label.setStyleSheet("color: #c03f3f;" if bad else "")
            if self._nav.last_text:
                severity, text = self._nav.last_text
                self.txtmsg_label.setText(f"[{severity}] {text}")
        elif packet == Packet.DT_SET_TCXO_DEBUG:
            decoded = pmtk.parse_tcxo_debug(sentence)
            if decoded is not None:
                valid, utc, drift = decoded
                self.tcxo_label.setText(
                    f"{drift:+.4f} ppm at {utc} "
                    f"({'reliable' if valid else 'NOT reliable - data not ready'})"
                )
        elif packet == Packet.TEST_FINISH:
            existing = self.test_results.text()
            self.test_results.setText(
                (existing if existing not in ("--", "Test running...") else "")
                + "\nTest finished (PMTK812)."
            )

        result = pmtk.parse_test_result(sentence)
        if result is not None:
            self._append_test_result(result)

        # Stop the TTFF clock on the first genuinely valid fix.
        if self._ttff_started_at is not None and self._nav.fix.has_fix:
            elapsed = time.monotonic() - self._ttff_started_at
            self._ttff_started_at = None
            base = self._ttff_label_text.split(":")[0]
            self.ttff_label.setText(
                f"{base}: {elapsed:.1f} s "
                f"({self._nav.fix.satellites_used} satellites, "
                f"HDOP {self._nav.fix.hdop if self._nav.fix.hdop is not None else '--'})"
            )

    def _append_test_result(self, result) -> None:
        if isinstance(result, AcqResult):
            text = f"Acquisition (PMTK813): SV {result.svid} in {result.seconds:g} s"
        elif isinstance(result, BitsyncResult):
            text = f"Bit sync (PMTK814): SV {result.svid} in {result.seconds:g} s"
        elif isinstance(result, SignalResult):
            text = (
                f"Signal (PMTK815): SV {result.svid} over {result.test_seconds:g} s - "
                f"phase error {result.phase_error:.2f}, "
                f"TCXO offset {result.tcxo_offset:.2f} / drift {result.tcxo_drift:.2f}, "
                f"C/N0 mean {result.cnr_mean:.3f} sigma {result.cnr_sigma:.3f} [uncalibrated]"
            )
        else:
            return
        existing = self.test_results.text()
        if existing in ("--", "Test running..."):
            existing = ""
        self.test_results.setText((existing + "\n" + text).strip())

    def _refresh(self) -> None:
        self.stats["sentences"].setText(str(self._nav.sentence_count))

        errors = self._nav.checksum_errors
        total = self._nav.sentence_count + errors
        error_text = str(errors)
        if errors and total:
            error_text += f"  ({errors / total * 100:.2f}% of traffic)"
        self.stats["errors"].setText(error_text)

        if self._byte_window:
            span = max(0.5, self._byte_window[-1][0] - self._byte_window[0][0])
            byte_count = sum(size for _, size in self._byte_window)
            self.stats["throughput"].setText(
                f"{byte_count / span:,.0f} B/s  ({byte_count / span * 10:,.0f} bit/s on the wire)"
            )
            self.stats["rate"].setText(f"{len(self._byte_window) / span:.1f} sentences/s")
        else:
            self.stats["throughput"].setText("--")
            self.stats["rate"].setText("--")

        if self._last_sentence_at is None:
            self.stats["silence"].setText("--")
        else:
            silence = time.monotonic() - self._last_sentence_at
            self.stats["silence"].setText(f"{silence:.1f} s")
            self.stats["silence"].setStyleSheet("color: #c03f3f;" if silence > 3 else "")

        rows = sorted(self._nav.seen.items())
        self.sentence_table.setRowCount(len(rows))
        for row, (address, count) in enumerate(rows):
            self.sentence_table.setItem(row, 0, QTableWidgetItem(address))
            self.sentence_table.setItem(row, 1, QTableWidgetItem(str(count)))

        if self._ttff_started_at is not None:
            elapsed = time.monotonic() - self._ttff_started_at
            base = self._ttff_label_text.split(":")[0]
            self.ttff_label.setText(f"{base}: {elapsed:.1f} s elapsed, no fix yet")

    def on_connected(self, is_connected: bool) -> None:
        if not is_connected:
            self._byte_window.clear()
            self._last_sentence_at = None
            self._ttff_started_at = None

    def on_protocol(self, protocol) -> None:
        """Show the identified protocol and what it can do."""
        from ..protocol import Kind

        if protocol.kind is Kind.UNKNOWN:
            self.protocol_label.setText(
                "No command protocol identified - the receiver can be read but not configured."
            )
            self.protocol_label.setStyleSheet("color: #c08a2f;")
            return
        able = ", ".join(sorted(c.value for c in protocol.capabilities)) or "nothing"
        self.protocol_label.setText(f"{protocol.name} - can set: {able}")
        self.protocol_label.setStyleSheet("color: #2f9e5f;")
