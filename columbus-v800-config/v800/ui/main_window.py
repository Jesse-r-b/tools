"""Main window: connection bar, tabbed panes, status bar and profiles."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import time

from .. import casic, pmtk
from ..protocol import Kind as ProtocolKind
from ..device import BAUD_SWEEP, BaudDetector, Device, list_serial_ports
from ..health import Level, Snapshot, assess, command_path_note
from ..nmea import CONSTELLATION_NAMES
from ..pmtk import BAUD_RATES
from .aiding_pane import AidingPane
from .common import State
from .console_pane import ConsolePane
from .datum_pane import DatumPane
from .diagnostics_pane import DiagnosticsPane
from .gnss_pane import GnssPane
from .health_banner import HealthBanner
from .nav_pane import NavigationPane
from .scan_dialog import ScanDialog
from .power_pane import PowerPane
from .rate_pane import RatePane

APP_NAME = "Columbus V-800 MarkIII Configuration"

PROFILE_VERSION = 1


class MainWindow(QMainWindow):
    """The application window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1120, 860)

        self.device = Device(self)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self._build_connection_bar())

        self.banner = HealthBanner(central)
        layout.addWidget(self.banner)

        self.tabs = QTabWidget(central)
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self._build_panes()
        self._build_menu()
        self._wire_device()

        self.status = self.statusBar()
        self._status_label = QLabel("Not connected")
        self.status.addPermanentWidget(self._status_label)

        self._seen_timer = QTimer(self)
        self._seen_timer.setInterval(1000)
        self._seen_timer.timeout.connect(self._refresh_derived)
        self._seen_timer.start()

        #: Queries sent by the last "read all", and whether each was answered.
        #: Kept so the summary can say what the receiver actually returned rather
        #: than assuming a sent query is an answered one.
        self._interrogation: dict[int, list] = {}
        self._interrogation_started: float | None = None
        self._command_path = "unknown"

        self.refresh_ports()
        self._set_connected(False)
        self._update_health()

    # -- construction ----------------------------------------------------

    def _build_connection_bar(self) -> QWidget:
        bar = QWidget(self)
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)

        row.addWidget(QLabel("Port:"))
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(340)
        row.addWidget(self.port_combo, 1)

        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_ports)
        row.addWidget(refresh)

        find = QPushButton("Find receivers...")
        find.setToolTip(
            "Scan every serial port and baud rate for a GNSS receiver, and report whether "
            "each one will accept configuration commands"
        )
        find.clicked.connect(self.find_receivers)
        row.addWidget(find)

        row.addWidget(QLabel("Baud:"))
        self.baud_combo = QComboBox()
        for baud in BAUD_SWEEP:
            self.baud_combo.addItem(str(baud), baud)
        self.baud_combo.setCurrentIndex(0)
        self.baud_combo.setEditable(False)
        row.addWidget(self.baud_combo)

        self.detect_button = QPushButton("Detect")
        self.detect_button.setToolTip(
            "Try each documented baud rate until decodable NMEA appears. "
            "Takes up to about 12 seconds."
        )
        self.detect_button.clicked.connect(self.detect_baud)
        row.addWidget(self.detect_button)

        self.connect_button = QPushButton("Connect")
        self.connect_button.setDefault(True)
        self.connect_button.clicked.connect(self.toggle_connection)
        row.addWidget(self.connect_button)

        return bar

    def _build_panes(self) -> None:
        self.nav_pane = NavigationPane(self.device.nav, self)
        self.rate_pane = RatePane(self)
        self.gnss_pane = GnssPane(self)
        self.datum_pane = DatumPane(self)
        self.power_pane = PowerPane(self)
        self.aiding_pane = AidingPane(self)
        self.diagnostics_pane = DiagnosticsPane(self.device.nav, self)
        self.console_pane = ConsolePane(self)

        self.panes = [
            self.nav_pane,
            self.rate_pane,
            self.gnss_pane,
            self.datum_pane,
            self.power_pane,
            self.aiding_pane,
            self.diagnostics_pane,
            self.console_pane,
        ]

        self.tabs.addTab(self.nav_pane, "Navigation")
        self.tabs.addTab(self.rate_pane, "Rate && Output")
        self.tabs.addTab(self.gnss_pane, "Constellations")
        self.tabs.addTab(self.datum_pane, "Datum")
        self.tabs.addTab(self.power_pane, "Power")
        self.tabs.addTab(self.aiding_pane, "Aiding && Restart")
        self.tabs.addTab(self.diagnostics_pane, "Diagnostics")
        self.tabs.addTab(self.console_pane, "Console")

        for pane in self.panes:
            pane.command.connect(self._send_command)
            pane.raw_command.connect(self._send_bytes)

        self.rate_pane.baud_change_requested.connect(self._reopen_at_baud)
        self.aiding_pane.set_fix_source(lambda: self.device.nav.fix)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        save_action = QAction("&Save profile...", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self.save_profile)
        file_menu.addAction(save_action)

        load_action = QAction("&Load profile...", self)
        load_action.setShortcut(QKeySequence.StandardKey.Open)
        load_action.triggered.connect(self.load_profile)
        file_menu.addAction(load_action)

        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        device_menu = self.menuBar().addMenu("&Device")

        read_all = QAction("&Read all settings from device", self)
        read_all.setShortcut("Ctrl+R")
        read_all.triggered.connect(self.read_all)
        device_menu.addAction(read_all)

        device_menu.addSeparator()
        find = QAction("&Find receivers...", self)
        find.setShortcut("Ctrl+F")
        find.triggered.connect(self.find_receivers)
        device_menu.addAction(find)

        detect = QAction("&Detect baud rate", self)
        detect.triggered.connect(self.detect_baud)
        device_menu.addAction(detect)

        help_menu = self.menuBar().addMenu("&Help")
        about = QAction("&About", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    def _wire_device(self) -> None:
        self.device.connected.connect(self._on_connected)
        self.device.disconnected.connect(self._on_disconnected)
        self.device.error.connect(self._on_error)
        self.device.line_in.connect(self._on_line_in)
        self.device.line_out.connect(self.console_pane.append_tx)
        self.device.sentence.connect(self._on_sentence)
        self.device.ack.connect(self._on_ack)
        self.device.nav_updated.connect(self.nav_pane.mark_dirty)
        self.device.casic_frame.connect(self._on_casic_frame)
        self.device.protocol_identified.connect(self._on_protocol)

    # -- connection ------------------------------------------------------

    def refresh_ports(self) -> None:
        current = self.port_combo.currentData()
        self.port_combo.clear()
        ports = list_serial_ports()
        for port in ports:
            self.port_combo.addItem(port.label, port.device)
        if not ports:
            self.port_combo.addItem("No serial ports found", None)
        if current:
            index = self.port_combo.findData(current)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)

    def toggle_connection(self) -> None:
        if self.device.is_open:
            self.device.close()
            return
        port = self.port_combo.currentData()
        if not port:
            QMessageBox.information(
                self,
                "No port selected",
                "No serial port is selected. Plug in the receiver and press Refresh.\n\n"
                "On Linux your user must be in the 'dialout' group to open the port.",
            )
            return
        self.device.open(port, self.baud_combo.currentData())

    def find_receivers(self) -> None:
        """Scan every port and baud rate, then offer to connect to what was found.

        The link is closed first: a port we already hold open cannot be probed,
        and a half-open port would be reported as "could not open" -- which
        would be true, and completely misleading.
        """
        if self.device.is_open:
            self.console_pane.append_note("Disconnecting to scan for receivers")
            self.device.close()

        dialog = ScanDialog(self)
        dialog.device_chosen.connect(self._connect_to_found)
        dialog.start_scan()
        dialog.exec()

    def _connect_to_found(self, port: str, baud: int) -> None:
        self.refresh_ports()
        index = self.port_combo.findData(port)
        if index >= 0:
            self.port_combo.setCurrentIndex(index)
        index = self.baud_combo.findData(baud)
        if index >= 0:
            self.baud_combo.setCurrentIndex(index)
        else:
            self.baud_combo.addItem(str(baud), baud)
            self.baud_combo.setCurrentIndex(self.baud_combo.count() - 1)
        self.console_pane.append_note(f"Scan selected {port} at {baud} baud")
        if not self.device.is_open:
            self.device.open(port, baud)

    def detect_baud(self) -> None:
        port = self.port_combo.currentData()
        if not port:
            QMessageBox.information(self, "No port selected", "Select a serial port first.")
            return
        if self.device.is_open:
            self.device.close()

        self.detect_button.setEnabled(False)
        self.connect_button.setEnabled(False)
        self._status_label.setText(f"Sweeping baud rates on {port}...")

        self._detector = BaudDetector(port, self)
        self._detector.progress.connect(
            lambda baud: self._status_label.setText(f"Trying {baud} baud...")
        )
        self._detector.finished_with.connect(self._detection_finished)
        self._detector.start()

    def _detection_finished(self, baud) -> None:
        self.detect_button.setEnabled(True)
        self.connect_button.setEnabled(True)
        if baud is None:
            self._status_label.setText("No decodable NMEA at any documented baud rate")
            QMessageBox.warning(
                self,
                "Nothing found",
                "No checksum-valid NMEA appeared at any of the documented baud rates.\n\n"
                "Check that the receiver is the device on this port, that it has power, and "
                "that nothing else has the port open.",
            )
            return
        index = self.baud_combo.findData(baud)
        if index >= 0:
            self.baud_combo.setCurrentIndex(index)
        self._status_label.setText(f"Found NMEA at {baud} baud")
        self.console_pane.append_note(f"Baud detection found decodable NMEA at {baud} baud")

    def _reopen_at_baud(self, baud: int) -> None:
        """Reopen the port after a PMTK251 baud change has been sent."""
        port = self.device.port_name or self.port_combo.currentData()
        if not port:
            return
        self.console_pane.append_note(
            f"Reopening {port} at {baud} baud after PMTK251. "
            "If nothing decodes, use Device > Detect baud rate."
        )
        QTimer.singleShot(400, lambda: self._do_reopen(port, baud))

    def _do_reopen(self, port: str, baud: int) -> None:
        self.device.close()
        index = self.baud_combo.findData(baud)
        if index >= 0:
            self.baud_combo.setCurrentIndex(index)
        if self.device.open(port, baud):
            QTimer.singleShot(600, self._verify_after_baud_change)

    def _verify_after_baud_change(self) -> None:
        """Confirm the receiver is actually talking at the new rate.

        A baud change that half-works produces a stream of checksum failures
        rather than silence, so both conditions are checked.
        """
        if self.device.nav.sentence_count > 0:
            self.console_pane.append_note("Traffic decoding correctly at the new baud rate.")
        else:
            self.console_pane.append_note(
                "No sentences decoded since the baud change - the receiver may not have "
                "accepted it. Use Device > Detect baud rate."
            )

    def _set_connected(self, is_connected: bool) -> None:
        self.connect_button.setText("Disconnect" if is_connected else "Connect")
        self.port_combo.setEnabled(not is_connected)
        self.baud_combo.setEnabled(not is_connected)
        self.detect_button.setEnabled(not is_connected)
        for pane in self.panes:
            pane.on_connected(is_connected)

    def _on_connected(self, port: str, baud: int) -> None:
        self._set_connected(True)
        self.rate_pane.set_actual_baud(baud)
        self._command_path = "unknown"
        self.console_pane.append_note(f"Opened {port} at {baud} baud")
        self._update_health()
        # Identify the command language before issuing any settings queries.
        # Reading with the wrong protocol is what made this tool useless against
        # the V-800 MarkIII for its first several versions.
        QTimer.singleShot(800, self.device.detect_protocol)

    def _on_disconnected(self, reason: str) -> None:
        self._set_connected(False)
        self.rate_pane.set_actual_baud(None)
        self._command_path = "unknown"
        self._interrogation = {}
        if reason:
            self.console_pane.append_note(f"Disconnected: {reason}")
        self._update_health()

    def _on_error(self, message: str) -> None:
        self.console_pane.append_note(message)

    # -- traffic ---------------------------------------------------------

    def _send_bytes(self, payload: bytes, description: str) -> None:
        if not self.device.is_open:
            QMessageBox.information(
                self, "Not connected", "Connect to the receiver before sending commands."
            )
            return
        self.device.send_bytes(payload, description)

    def _on_casic_frame(self, frame) -> None:
        for pane in self.panes:
            handler = getattr(pane, "on_casic_frame", None)
            if handler is not None:
                handler(frame)
        if frame.checksum_ok:
            self._command_path = "working"
        self.console_pane.append_note(
            ("<- " if frame.checksum_ok else "<- BAD CHECKSUM ") + casic.describe(frame)
        )

    def _on_protocol(self, active) -> None:
        """Tell every pane which command language is in use."""
        for pane in self.panes:
            pane.on_protocol(active)
        self.console_pane.append_note(f"Command protocol identified: {active.name}")
        self._command_path = "working" if active.kind is not ProtocolKind.UNKNOWN else "silent"
        self._update_health()
        # Now that the protocol is known, read the settings it can actually read.
        QTimer.singleShot(300, self.read_all)

    def _send_command(self, payload: str, description: str) -> None:
        if not self.device.is_open:
            QMessageBox.information(
                self, "Not connected", "Connect to the receiver before sending commands."
            )
            return
        self.device.send_command(payload, description)

    def _on_line_in(self, line: str) -> None:
        self.console_pane.append_rx(line)
        self.diagnostics_pane.note_line(line)

    def _on_sentence(self, sentence) -> None:
        self._note_interrogation_reply(sentence)
        for pane in self.panes:
            pane.on_sentence(sentence)

    def _on_ack(self, ack) -> None:
        self._command_path = "working"
        for pane in self.panes:
            pane.on_ack(ack)
        if not ack.ok:
            text = f"{ack}"
            self._status_label.setText(text)
            self.console_pane.append_note(text)

    def _refresh_derived(self) -> None:
        self.rate_pane.update_seen_counts(self.device.nav.seen)
        self.gnss_pane.update_observed(self.device.nav.constellation_summary())
        self._update_health()

    def _snapshot(self) -> Snapshot:
        """Gather measured state for the health assessment."""
        nav = self.device.nav
        satellites = nav.satellites()
        fix = nav.fix
        now = time.monotonic()

        from ..nmea import FIX_QUALITY_TEXT, FIX_TYPE_TEXT

        if fix.has_fix:
            description = FIX_TYPE_TEXT.get(fix.fix_type) or FIX_QUALITY_TEXT.get(fix.quality, "Fix")
            if fix.quality.name == "DGPS":
                description += " (differential)"
        else:
            description = ""

        return Snapshot(
            is_open=self.device.is_open,
            port=self.device.port_name,
            baud=self.device.baud,
            seconds_since_open=(
                now - self.device.opened_at if self.device.opened_at else 0.0
            ),
            bytes_received=self.device.bytes_received,
            lines_received=self.device.lines_received,
            sentences_decoded=nav.sentence_count,
            checksum_errors=nav.checksum_errors,
            seconds_since_last_sentence=(
                now - self.device.last_sentence_at if self.device.last_sentence_at else None
            ),
            satellites_in_view=len(satellites),
            satellites_tracked=sum(1 for s in satellites if s.tracked),
            satellites_used=sum(1 for s in satellites if s.used),
            has_fix=fix.has_fix,
            fix_description=description,
            hdop=fix.hdop,
            antenna_status=nav.antenna_status,
            command_path=self._command_path,
            constellations=tuple(
                CONSTELLATION_NAMES[c] for c in sorted(nav.constellation_summary())
            ),
        )

    def _update_health(self) -> None:
        health = assess(self._snapshot())
        self.banner.set_health(health, command_path_note(self._command_path))
        self._status_label.setText(health.headline)

    #: Every query the receiver can answer, with the packet type of the reply to
    #: watch for. This is the whole readable configuration surface -- if a query
    #: exists for a setting, it is here, so "read all" genuinely means all.
    INTERROGATION = (
        ("PMTK605", 705, "firmware release"),
        ("PMTK400", 500, "fix interval"),
        ("PMTK414", 514, "NMEA sentence output"),
        ("PMTK401", 501, "DGPS mode"),
        ("PMTK413", 513, "SBAS setting"),
        ("PMTK430", 530, "datum"),
        ("PMTK431", 530, "user datum"),
        ("PMTK607", 1, "EPO validity"),
        ("PMTK660,1800", 1, "ephemeris inventory"),
        ("PMTK661,30", 1, "almanac inventory"),
    )

    def read_all(self) -> None:
        """Query every readable setting, paced, and report what came back.

        Sending is not reading. The summary at the end counts *answers*, so a
        receiver that ignores commands produces "0 of 10" rather than a silent
        pane full of defaults that look like they came from the device.
        """
        if not self.device.is_open:
            return

        active = self.device.protocol
        if active.kind is ProtocolKind.CASIC:
            # CASIC replies are binary frames, not PMTK acknowledgements, so the
            # PMTK interrogation bookkeeping does not apply. The panes update
            # themselves from the frames as they arrive.
            self.console_pane.append_note("Reading all settings over CASIC")
            for pane in self.panes:
                pane.read_from_device()
            return
        if active.kind is ProtocolKind.UNKNOWN:
            self.console_pane.append_note(
                "No command protocol identified - skipping the settings read"
            )
            return

        self._interrogation = {}
        for payload, reply, description in self.INTERROGATION:
            self._interrogation[id(payload)] = [payload, reply, description, False]
        self._interrogation_started = time.monotonic()

        self.console_pane.append_note(
            f"Reading all settings from the device ({len(self.INTERROGATION)} queries)"
        )
        self.device.suppress_ack_warnings = True
        self.device.queue_commands(
            [(payload, f"query {description}") for payload, _, description in self.INTERROGATION],
            interval_ms=180,
        )
        budget_ms = len(self.INTERROGATION) * 180 + 2500
        QTimer.singleShot(budget_ms, self._report_interrogation)

    def _note_interrogation_reply(self, sentence) -> None:
        """Mark any outstanding query answered by this sentence."""
        if not self._interrogation:
            return
        packet = sentence.packet_type
        if packet is None:
            return
        for entry in self._interrogation.values():
            if entry[3]:
                continue
            expected = entry[1]
            if expected is None:
                continue
            if packet == expected:
                # PMTK001 answers several different queries; match on the command
                # field so an unrelated acknowledgement does not tick one off.
                if expected == 1 and sentence.fields:
                    wanted = entry[0].split(",")[0].removeprefix("PMTK")
                    if sentence.fields[0] != wanted:
                        continue
                entry[3] = True
                return

    def _report_interrogation(self) -> None:
        if not self._interrogation or not self.device.is_open:
            return
        entries = list(self._interrogation.values())
        answered = [e for e in entries if e[3]]
        unanswered = [e for e in entries if not e[3] and e[1] is not None]
        total = len([e for e in entries if e[1] is not None])

        if answered:
            self._command_path = "working"
            self.console_pane.append_note(
                f"Read {len(answered)} of {total} settings from the device"
                + (
                    "; no answer for: " + ", ".join(e[2] for e in unanswered)
                    if unanswered
                    else " - all answered"
                )
            )
        else:
            self._command_path = "silent"
            self.console_pane.append_note(
                f"None of {total} queries were answered. This receiver streams NMEA but does "
                "not accept commands, so the settings shown are defaults, not the device's "
                "actual configuration."
            )
        self._interrogation = {}
        self.device.suppress_ack_warnings = False
        self._update_health()

    # -- profiles --------------------------------------------------------

    def _profile(self) -> dict:
        """Collect the settings this tool can meaningfully round-trip.

        Deliberately excludes anything that cannot be read back from the device
        (power mode, static navigation threshold), because saving a value the
        tool cannot verify would present a guess as a record.
        """
        return {
            "version": PROFILE_VERSION,
            "tool": "columbus-v800-config",
            "note": (
                "Settings for a Columbus V-800 MarkIII. Power modes and the static "
                "navigation threshold are not included: the chipset provides no query "
                "for them, so they cannot be verified after writing."
            ),
            "fix_interval_ms": self.rate_pane.interval_spin.value(),
            "nmea_rates": self.rate_pane.current_rates(),
            "gps": self.gnss_pane.gps_check.isChecked(),
            "glonass": self.gnss_pane.glonass_check.isChecked(),
            "sbas": self.gnss_pane.sbas_check.isChecked(),
            "dgps_mode": self.gnss_pane.dgps_combo.currentData(),
            "qzss": self.gnss_pane.qzss_check.isChecked(),
            "qzss_nmea": self.gnss_pane.qzss_nmea_check.isChecked(),
            "aic": self.gnss_pane.aic_check.isChecked(),
            "datum": self.datum_pane.datum_combo.currentData(),
        }

    def save_profile(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save profile", "v800-profile.json", "JSON files (*.json);;All files (*)"
        )
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self._profile(), indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Could not save profile", str(exc))
            return
        self._status_label.setText(f"Profile saved to {path}")

    def load_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load profile", "", "JSON files (*.json);;All files (*)"
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Could not load profile", str(exc))
            return
        if data.get("version") != PROFILE_VERSION:
            QMessageBox.warning(
                self,
                "Unsupported profile",
                f"This profile is version {data.get('version')!r}; "
                f"this tool writes version {PROFILE_VERSION}.",
            )
            return

        self.rate_pane.interval_spin.setValue(int(data.get("fix_interval_ms", 1000)))
        for name, rate in (data.get("nmea_rates") or {}).items():
            combo = self.rate_pane.rate_combos.get(name)
            if combo is not None:
                index = combo.findData(rate)
                if index >= 0:
                    combo.setCurrentIndex(index)

        self.gnss_pane.gps_check.setChecked(bool(data.get("gps", True)))
        self.gnss_pane.glonass_check.setChecked(bool(data.get("glonass", True)))
        self.gnss_pane.sbas_check.setChecked(bool(data.get("sbas", False)))
        index = self.gnss_pane.dgps_combo.findData(data.get("dgps_mode", 0))
        if index >= 0:
            self.gnss_pane.dgps_combo.setCurrentIndex(index)
        self.gnss_pane.qzss_check.setChecked(bool(data.get("qzss", True)))
        self.gnss_pane.qzss_nmea_check.setChecked(bool(data.get("qzss_nmea", False)))
        self.gnss_pane.aic_check.setChecked(bool(data.get("aic", False)))

        datum = data.get("datum")
        if datum is not None:
            found = self.datum_pane.datum_combo.findData(datum)
            if found >= 0:
                self.datum_pane.datum_combo.setCurrentIndex(found)

        for pane in (self.rate_pane, self.gnss_pane, self.datum_pane):
            pane.bar.set_state(State.EDITED, "loaded from profile - not written yet")
        self._status_label.setText(
            f"Profile loaded from {path}. Nothing has been written to the device yet."
        )

    # -- misc ------------------------------------------------------------

    def _about(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<h3>{APP_NAME}</h3>"
            "<p>Configuration and diagnostics for the Columbus V-800 MarkIII USB GNSS "
            "receiver, an MT3333-class multi-constellation module behind a Prolific PL2303 "
            "USB-serial bridge.</p>"
            "<p>Every command is implemented from the <i>MT3333 Platform NMEA Message "
            "Specification for GPS+GLONASS</i> V1.00, a copy of which is in the "
            "<code>docs/</code> directory alongside a list of the errors found in it.</p>"
            "<p>Settings are always read back from the receiver after writing. Where the "
            "chipset provides no query, the pane says so rather than showing you what it "
            "assumes.</p>",
        )

    def closeEvent(self, event) -> None:  # noqa: D102, N802
        self.device.close()
        self.console_pane._close_log()
        super().closeEvent(event)
