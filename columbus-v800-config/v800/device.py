"""Serial transport and command sequencing for the Columbus V-800 MarkIII.

The receiver presents as a Prolific PL2303 USB-serial bridge (see the Columbus
V-800 product page and the PL2303 drivers on their download page), streaming
NMEA continuously and accepting PMTK commands on the same port.

Two things here are worth knowing before changing anything:

* **Writes are not confirmed by the write succeeding.**  A PMTK command is only
  applied if the receiver answers with PMTK001 flag 3.  :class:`Device` tracks
  that, and every setting the GUI writes is followed by the matching query so
  the pane shows what the *receiver* reports, never what we asked for.  Several
  commands are documented as "the execution result depend on firmware version",
  so this is not paranoia.

* **The port speed can change underneath us.**  PMTK251 alters the baud rate
  mid-session, and PMTK104 or entering standby silently reverts it.  After a
  baud change the link is reopened at the new speed and verified; if nothing
  decodes, :meth:`Device.autodetect_baud` sweeps the documented rates.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from . import casic, pmtk, protocol
from .nmea import NavState
from .pmtk import Ack, AckFlag, Packet, Sentence

try:
    import serial
    from serial.tools import list_ports
except ImportError as exc:  # pragma: no cover - dependency is declared in pyproject
    raise SystemExit(
        "pyserial is required: install it with 'pip install pyserial'"
    ) from exc


#: Prolific PL2303 USB-serial bridge, as used by the V-800 family.
PL2303_VID = 0x067B
PL2303_PIDS = (0x2303, 0x23A3, 0x23B3, 0x23C3, 0x23D3, 0x23E3, 0x23F3, 0x2304)

#: Baud rates to sweep during autodetection, most likely first.
#:
#: 9600 leads on measurement rather than documentation: the V-800 MarkIII on the
#: bench came up at 9600, and 9600 is the MediaTek default.  38400 follows
#: because that is what the Columbus V-800 specification page states.  The
#: connection bar takes its default from this order, so the first entry is also
#: the rate the tool offers before you have connected to anything.
BAUD_SWEEP = (9600, 38400, 115200, 57600, 19200, 4800, 14400, 230400, 460800, 921600)


@dataclass(frozen=True)
class PortInfo:
    """A candidate serial port."""

    device: str
    description: str
    vid: int | None
    pid: int | None
    serial_number: str

    @property
    def is_pl2303(self) -> bool:
        return self.vid == PL2303_VID and (self.pid in PL2303_PIDS or self.pid is not None)

    @property
    def label(self) -> str:
        bits = [self.device]
        if self.description and self.description != "n/a":
            bits.append(self.description)
        if self.vid is not None and self.pid is not None:
            bits.append(f"{self.vid:04x}:{self.pid:04x}")
        if self.is_pl2303:
            bits.append("[PL2303 - likely V-800]")
        return "  -  ".join(bits)


def list_serial_ports() -> list[PortInfo]:
    """Enumerate serial ports, PL2303 bridges first."""
    ports = [
        PortInfo(
            device=p.device,
            description=p.description or "",
            vid=p.vid,
            pid=p.pid,
            serial_number=p.serial_number or "",
        )
        for p in list_ports.comports()
    ]
    ports.sort(key=lambda p: (not p.is_pl2303, p.device))
    return ports


class _Reader(QThread):
    """Reads the port and emits one signal per line.

    Runs in its own thread because a blocking read with a short timeout is the
    only way to get low-latency line delivery without spinning the GUI thread.
    Lines are split on the transport's own CR/LF; a partial line is carried over
    to the next read so a sentence split across two USB packets is not corrupted
    into two bad-checksum fragments.
    """

    line_received = Signal(str)
    raw_received = Signal(bytes)
    data_received = Signal(int)
    """Raw byte count per read.

    Emitted separately from :attr:`line_received` because at the wrong baud rate
    the receiver's output arrives as bytes that never resolve into terminated
    lines.  Counting only lines makes that case indistinguishable from a dead
    port -- which is the single most common real failure, and the one where the
    right advice ("try Detect") differs completely from the wrong advice
    ("check it has power").
    """
    failed = Signal(str)

    def __init__(self, port: "serial.Serial", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._port = port
        self._running = True

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:  # noqa: D102 - QThread entry point
        buffer = bytearray()
        while self._running:
            try:
                chunk = self._port.read(4096) or b""
                if not chunk:
                    # Timeout with nothing pending; loop so we notice ._running.
                    continue
                buffer.extend(chunk)
                self.data_received.emit(len(chunk))
                self.raw_received.emit(bytes(chunk))
            except Exception as exc:  # pragma: no cover - hardware failure path
                if self._running:
                    self.failed.emit(str(exc))
                return

            while True:
                cut = -1
                for terminator in (b"\r\n", b"\n", b"\r"):
                    found = buffer.find(terminator)
                    if found >= 0 and (cut < 0 or found < cut):
                        cut = found
                if cut < 0:
                    break
                line = bytes(buffer[:cut])
                # Drop the terminator, which may be one or two bytes.
                skip = 2 if buffer[cut : cut + 2] == b"\r\n" else 1
                del buffer[: cut + skip]
                if line:
                    self.line_received.emit(line.decode("ascii", errors="replace"))

            # A runaway line means we are reading something that is not NMEA --
            # wrong baud rate, most likely.  Drop it rather than growing forever.
            if len(buffer) > 8192:
                del buffer[:-1024]


@dataclass
class PendingCommand:
    """A command awaiting its PMTK001 acknowledgement."""

    packet_type: int
    payload: str
    sent_at: float
    description: str


class Device(QObject):
    """A connected V-800, or the absence of one.

    All Qt signals are emitted on the GUI thread.
    """

    connected = Signal(str, int)  # port, baud
    disconnected = Signal(str)  # reason ("" for a clean close)
    error = Signal(str)

    line_in = Signal(str)  # every raw line received, verbatim
    line_out = Signal(str)  # every raw line transmitted, verbatim
    sentence = Signal(object)  # Sentence
    ack = Signal(object)  # Ack
    nav_updated = Signal()
    checksum_error = Signal(str)
    casic_frame = Signal(object)      # casic.Frame
    protocol_identified = Signal(object)  # protocol.Protocol

    #: PMTK001 is expected within this long, else the command is reported unanswered.
    ACK_TIMEOUT_S = 2.0

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._port: serial.Serial | None = None
        self._reader: _Reader | None = None
        self.nav = NavState()
        self.port_name = ""
        self.baud = 0
        self._pending: dict[int, PendingCommand] = {}

        self.lines_received = 0
        """Raw lines off the port, whether or not they parsed as NMEA.

        Counted separately from decoded sentences so that "nothing is arriving"
        can be told apart from "something is arriving but it is not NMEA" --
        the first means a dead link, the second almost always means the wrong
        baud rate.
        """
        self.bytes_received = 0
        self.opened_at: float | None = None
        self.last_sentence_at: float | None = None

        #: The command language this receiver actually speaks. Starts unknown
        #: and is replaced once a probe is answered -- the tool asks rather than
        #: assuming, because assuming PMTK is what made it useless here.
        self.protocol: protocol.Protocol = protocol.UnknownProtocol()
        self._detecting = False
        self._detect_buffer = bytearray()

        #: Bytes that did not parse as NMEA, kept so binary frames spanning
        #: reads can be reassembled.
        self._binary = bytearray()

        self.suppress_ack_warnings = False
        """Set while a batch read is running.

        During an interrogation the caller reports one summary covering every
        query, so per-command timeouts would say the same thing three more
        times and bury the summary.
        """

        self._queue: list[tuple[str, str]] = []
        self._queue_timer = QTimer(self)
        self._queue_timer.timeout.connect(self._drain_queue)

        self._timeout_timer = QTimer(self)
        self._timeout_timer.setInterval(500)
        self._timeout_timer.timeout.connect(self._expire_pending)

    # -- connection ------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._port is not None and self._port.is_open

    def open(self, port: str, baud: int) -> bool:
        """Open ``port`` at ``baud``.  Returns True on success."""
        self.close()
        try:
            self._port = serial.Serial(
                port=port,
                baudrate=baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
                write_timeout=2.0,
            )
        except Exception as exc:
            self._port = None
            self.error.emit(f"Could not open {port} at {baud} baud: {exc}")
            return False

        self.port_name, self.baud = port, baud
        self.nav.reset()
        self._pending.clear()
        self._queue.clear()
        self.lines_received = 0
        self.bytes_received = 0
        self.last_sentence_at = None
        self.opened_at = time.monotonic()
        self.protocol = protocol.UnknownProtocol()
        self._detect_buffer.clear()
        self._binary.clear()

        self._reader = _Reader(self._port, self)
        self._reader.line_received.connect(self._on_line)
        self._reader.data_received.connect(self._on_data)
        self._reader.raw_received.connect(self._on_raw)
        self._reader.failed.connect(self._on_reader_failed)
        self._reader.start()
        self._timeout_timer.start()

        self.connected.emit(port, baud)
        return True

    def close(self, reason: str = "") -> None:
        """Close the port and stop the reader."""
        self._timeout_timer.stop()
        self._queue_timer.stop()
        self._queue.clear()
        if self._reader is not None:
            self._reader.stop()
            self._reader.wait(1500)
            self._reader = None
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                pass
            self._port = None
            self.opened_at = None
            self.disconnected.emit(reason)
        self._pending.clear()

    def _on_reader_failed(self, message: str) -> None:
        self.close(f"Read failed: {message}")

    # -- transmit --------------------------------------------------------

    def send_raw(self, payload: str) -> bool:
        """Frame and transmit ``payload``.  Returns True if it reached the port."""
        if not self.is_open:
            self.error.emit("Not connected")
            return False
        frame = pmtk.build(payload)
        try:
            self._port.write(frame)
            self._port.flush()
        except Exception as exc:
            self.error.emit(f"Write failed: {exc}")
            self.close(f"Write failed: {exc}")
            return False
        self.line_out.emit(frame.decode("ascii", errors="replace").rstrip("\r\n"))
        return True

    def send_command(self, payload: str, description: str = "") -> bool:
        """Transmit a PMTK command and start waiting for its acknowledgement."""
        packet_type = _packet_type_of(payload)
        if not self.send_raw(payload):
            return False
        if packet_type is not None:
            self._pending[packet_type] = PendingCommand(
                packet_type=packet_type,
                payload=payload,
                sent_at=time.monotonic(),
                description=description or pmtk.describe(packet_type),
            )
        return True

    def _expire_pending(self) -> None:
        now = time.monotonic()
        for packet_type, pending in list(self._pending.items()):
            if now - pending.sent_at < self.ACK_TIMEOUT_S:
                continue
            del self._pending[packet_type]
            # Query packets are answered by a data packet, not by PMTK001, so a
            # missing ACK for those is normal and must not be reported as a fault.
            if packet_type in pmtk.QUERY_REPLY:
                continue
            if self.suppress_ack_warnings:
                continue
            if pmtk.is_query(packet_type):
                self.error.emit(
                    f"No answer to {pending.description} within {self.ACK_TIMEOUT_S:g} s."
                )
            else:
                self.error.emit(
                    f"No acknowledgement for {pending.description} within "
                    f"{self.ACK_TIMEOUT_S:g} s -- the setting may not have applied."
                )

    # -- receive ---------------------------------------------------------

    def queue_commands(self, items: list[tuple[str, str]], interval_ms: int = 150) -> None:
        """Send a batch of commands spaced out in time.

        Firing a dozen commands back to back at 9600 baud is enough to overrun a
        receiver's input buffer, and a command lost that way looks exactly like a
        command the receiver chose to ignore.  Spacing them removes that
        ambiguity, so an unanswered query means something.
        """
        self._queue.extend(items)
        if not self._queue_timer.isActive():
            self._queue_timer.start(max(20, interval_ms))
            self._drain_queue()

    def _drain_queue(self) -> None:
        if not self._queue or not self.is_open:
            self._queue_timer.stop()
            self._queue.clear()
            return
        payload, description = self._queue.pop(0)
        self.send_command(payload, description)

    def _on_data(self, count: int) -> None:
        self.bytes_received += count

    def _on_raw(self, chunk: bytes) -> None:
        """Handle bytes that may contain binary frames.

        NMEA is line-oriented and binary frames are not, so the two are decoded
        from separate buffers over the same stream rather than trying to make
        one parser serve both.
        """
        self._binary.extend(chunk)
        if self._detecting:
            self._detect_buffer.extend(chunk)
        frames, consumed = casic.parse(bytes(self._binary))
        if consumed:
            del self._binary[:consumed]
        # Keep the tail bounded: without a frame in sight this is just NMEA.
        if len(self._binary) > 4096:
            del self._binary[:-512]
        for frame in frames:
            self.casic_frame.emit(frame)
            if frame.checksum_ok and not isinstance(self.protocol, protocol.CasicProtocol):
                self._adopt(protocol.Kind.CASIC)

    def _on_line(self, line: str) -> None:
        self.lines_received += 1
        self.line_in.emit(line)
        parsed = pmtk.parse(line)
        if parsed is None:
            return
        if parsed.checksum_state is pmtk.ChecksumState.BAD:
            self.nav.checksum_errors += 1
            self.checksum_error.emit(line)
            # A corrupt sentence is not decoded further: acting on half-valid
            # fields is exactly how bad data gets laundered into good.
            return

        self.last_sentence_at = time.monotonic()
        self.sentence.emit(parsed)

        if parsed.is_pmtk:
            self._on_pmtk(parsed)
        else:
            self.nav.feed(parsed)
            self.nav_updated.emit()

    def _on_pmtk(self, s: Sentence) -> None:
        packet_type = s.packet_type
        if packet_type is None:
            return
        # Clear the pending entry for a query as soon as its data reply lands.
        for query, reply in pmtk.QUERY_REPLY.items():
            if packet_type == reply:
                self._pending.pop(int(query), None)

        decoded = pmtk.parse_ack(s)
        if decoded is not None:
            self._pending.pop(decoded.command, None)
            self.ack.emit(decoded)

    # -- helpers ---------------------------------------------------------

    def _adopt(self, kind) -> None:
        """Switch to the protocol the receiver answered on."""
        if self.protocol.kind is kind:
            return
        self.protocol = protocol.create(kind)
        self.protocol_identified.emit(self.protocol)

    def detect_protocol(self, on_done=None) -> None:
        """Ask the receiver which command language it speaks.

        Sends each probe in turn and watches what comes back. Every probe is a
        pure query, so this is safe against an unidentified device.
        """
        if not self.is_open:
            return
        self._detecting = True
        self._detect_buffer.clear()

        for _, payload, description in protocol.DETECTION_PROBES:
            self.send_bytes(payload, description)

        def finish() -> None:
            self._detecting = False
            kind = protocol.identify(bytes(self._detect_buffer))
            self._adopt(kind)
            if on_done is not None:
                on_done(self.protocol)

        QTimer.singleShot(2500, finish)

    def send_bytes(self, payload: bytes, description: str = "") -> bool:
        """Transmit raw bytes already framed by a protocol module."""
        if not self.is_open:
            self.error.emit("Not connected")
            return False
        try:
            self._port.write(payload)
            self._port.flush()
        except Exception as exc:
            self.error.emit(f"Write failed: {exc}")
            self.close(f"Write failed: {exc}")
            return False
        if payload.startswith(b"$"):
            self.line_out.emit(payload.decode("ascii", errors="replace").rstrip("\r\n"))
        else:
            self.line_out.emit(
                (description or "binary") + ": " + " ".join(f"{b:02x}" for b in payload)
            )
        return True

    def query_all(self) -> None:
        """Read back every queryable setting, for the "Read from device" button."""
        for payload in (
            pmtk.query_release(),
            pmtk.query_fix_ctl(),
            pmtk.query_dgps_mode(),
            pmtk.query_sbas_enabled(),
            "PMTK414",
            pmtk.query_datum(),
            pmtk.query_datum_advance(),
            pmtk.query_epo_info(),
        ):
            self.send_command(payload)

    def autodetect_baud(self, port: str, per_rate_s: float = 1.2) -> int | None:
        """Sweep :data:`BAUD_SWEEP` looking for decodable NMEA on ``port``.

        Returns the first rate that yields a checksum-valid sentence, else None.
        This blocks, so callers run it from a worker -- see
        :class:`BaudDetector`.
        """
        for baud in BAUD_SWEEP:
            try:
                with serial.Serial(port, baud, timeout=0.2) as probe:
                    deadline = time.monotonic() + per_rate_s
                    buffer = bytearray()
                    while time.monotonic() < deadline:
                        buffer.extend(probe.read(1024) or b"")
                        text = buffer.decode("ascii", errors="replace")
                        for line in text.splitlines():
                            parsed = pmtk.parse(line)
                            if parsed and parsed.checksum_state is pmtk.ChecksumState.OK:
                                return baud
            except Exception:
                continue
        return None


class BaudDetector(QThread):
    """Runs :meth:`Device.autodetect_baud` off the GUI thread."""

    finished_with = Signal(object)  # int baud, or None
    progress = Signal(int)  # baud currently being probed

    def __init__(self, port: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._port = port

    def run(self) -> None:  # noqa: D102
        for baud in BAUD_SWEEP:
            self.progress.emit(baud)
            try:
                with serial.Serial(self._port, baud, timeout=0.2) as probe:
                    deadline = time.monotonic() + 1.2
                    buffer = bytearray()
                    while time.monotonic() < deadline:
                        buffer.extend(probe.read(1024) or b"")
                        text = buffer.decode("ascii", errors="replace")
                        for line in text.splitlines():
                            parsed = pmtk.parse(line)
                            if parsed and parsed.checksum_state is pmtk.ChecksumState.OK:
                                self.finished_with.emit(baud)
                                return
            except Exception:
                continue
        self.finished_with.emit(None)


def _packet_type_of(payload: str) -> int | None:
    """Extract the PMTK packet type from a payload, or None if it is not PMTK."""
    payload = payload.lstrip("$")
    address = payload.split(",", 1)[0]
    if not address.startswith("PMTK"):
        return None
    try:
        return int(address[4:])
    except ValueError:
        return None
