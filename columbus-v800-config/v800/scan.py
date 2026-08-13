"""Search serial ports and baud rates for a receiver worth talking to.

The scan answers three different questions, and keeps them apart because the
answers have different consequences:

1. **Is anything there?**  Bytes arriving at all.
2. **Is it a GNSS receiver?**  Those bytes decode as NMEA with valid checksums.
3. **Can we configure it, and in which language?**  It answers a command probe.

Question 3 is the one that matters for this tool, and it is *not* implied by
question 2.  It also has more than one right answer: the V-800 MarkIII streams
flawless multi-constellation NMEA, ignores PMTK entirely, and answers CASIC.  A
scan that only ever asked in PMTK would report that device as unconfigurable,
which is exactly what this scanner did until the CASIC backend existed.

So every protocol the tool can speak is probed, and the answer names the
language.  The probes are pure queries -- a PMTK firmware query, a PMTK test
packet, and a zero-length CASIC poll -- none of which changes a setting.  A scan
never alters a device it finds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum

from . import casic, pmtk, protocol
from .pmtk import ChecksumState

#: How long to listen for NMEA before giving up on a (port, baud) pair.  One
#: second covers the 1 Hz default cadence with margin; anything less risks
#: landing between epochs and calling a working receiver dead.
LISTEN_S = 1.0

#: How long to wait for a PMTK reply after asking.  The receiver has to fit the
#: answer between its own scheduled sentences, so this is deliberately generous.
COMMAND_WAIT_S = 2.0

#: How many baud rates to try before concluding a wholly silent port has
#: nothing attached.
#:
#: A UART mismatch garbles bytes, it does not suppress them: a device that is
#: transmitting produces *some* bytes at any rate we listen at.  So zero bytes
#: across a couple of rates is strong evidence nothing is there, and grinding
#: through the remaining eight rates only adds ten seconds per empty port.  Any
#: byte at all disables this shortcut and the full sweep runs.
SILENT_BAUDS_BEFORE_GIVING_UP = 2

#: Baud rates to try, most likely first.  9600 leads because that is what the
#: V-800 MarkIII actually came up at (measured), and it is the MediaTek default;
#: 38400 follows because that is what the Columbus specification page claims.
BAUD_ORDER = (9600, 38400, 115200, 57600, 19200, 4800, 14400, 230400, 460800, 921600)


class Outcome(IntEnum):
    """What a port turned out to be.  Ordered worst to best, so ``max`` ranks."""

    ERROR = 0
    """Could not be opened -- busy, or no permission."""

    SILENT = 1
    """Opened, but not one byte arrived at any baud rate."""

    NOT_NMEA = 2
    """Bytes arrived, but nothing decoded as NMEA at any baud rate tried."""

    NMEA_ONLY = 3
    """A GNSS receiver, but it answers none of the command protocols we speak."""

    CONFIGURABLE = 4
    """A GNSS receiver that answers a command protocol.  What we are looking for."""


OUTCOME_TEXT = {
    Outcome.ERROR: "Could not open",
    Outcome.SILENT: "Nothing there",
    Outcome.NOT_NMEA: "Data, but not NMEA",
    Outcome.NMEA_ONLY: "GNSS receiver (read-only)",
    Outcome.CONFIGURABLE: "GNSS receiver - configurable",
}


@dataclass
class ScanResult:
    """What was found on one port."""

    port: str
    description: str = ""
    outcome: Outcome = Outcome.SILENT
    baud: int | None = None
    """The baud rate that decoded, if any."""

    sentences: int = 0
    checksum_errors: int = 0
    bytes_seen: int = 0
    talkers: tuple[str, ...] = ()
    firmware: str = ""
    pmtk_replies: tuple[str, ...] = ()
    protocol_kind: protocol.Kind = protocol.Kind.UNKNOWN
    """Which command language answered, if any."""
    error: str = ""
    bauds_tried: tuple[int, ...] = ()
    gave_up_early: bool = False
    """True when the sweep stopped short because the port was completely silent."""

    @property
    def is_usable(self) -> bool:
        """True if this is a GNSS receiver, whether or not it takes commands."""
        return self.outcome >= Outcome.NMEA_ONLY

    @property
    def summary(self) -> str:
        """One line describing the find, in the terms that matter."""
        if self.outcome is Outcome.ERROR:
            return self.error or "could not be opened"
        if self.outcome is Outcome.SILENT:
            text = f"not one byte at {len(self.bauds_tried)} baud rate"
            text += "s" if len(self.bauds_tried) != 1 else ""
            if self.gave_up_early:
                text += " - nothing is transmitting on this port"
            return text
        if self.outcome is Outcome.NOT_NMEA:
            return (
                f"{self.bytes_seen:,} bytes seen but nothing decoded at any of "
                f"{len(self.bauds_tried)} baud rates - not a GNSS receiver, "
                f"or an unusual baud rate"
            )

        bits = [f"{self.sentences} sentences at {self.baud} baud"]
        if self.talkers:
            bits.append("talkers " + "/".join(self.talkers))
        if self.outcome is Outcome.CONFIGURABLE:
            name = protocol.create(self.protocol_kind).name
            bits.append(f"answers {name}")
            if self.pmtk_replies:
                bits.append(", ".join(self.pmtk_replies))
            if self.firmware:
                bits.append(f"firmware {self.firmware}")
        else:
            bits.append("answers no command protocol this tool speaks")
        return "; ".join(bits)


def classify(bytes_seen: int, sentences: int, answered: bool) -> Outcome:
    """Turn raw counts into a verdict.

    Split out from the I/O so the decision itself is testable without hardware,
    and so the rule is stated once: answering *some* command protocol is a
    strictly stronger claim than emitting NMEA, which is stronger than emitting
    bytes.  Which protocol answered is recorded separately -- it changes what
    the tool can do, not whether the device is usable.
    """
    if sentences > 0:
        return Outcome.CONFIGURABLE if answered else Outcome.NMEA_ONLY
    if bytes_seen > 0:
        return Outcome.NOT_NMEA
    return Outcome.SILENT


def rank(results: list[ScanResult]) -> list[ScanResult]:
    """Best candidates first: configurable, then readable, then the rest."""
    return sorted(results, key=lambda r: (-int(r.outcome), r.port))


def best(results: list[ScanResult]) -> ScanResult | None:
    """The one to recommend, or None if nothing usable was found."""
    ordered = rank(results)
    return ordered[0] if ordered and ordered[0].is_usable else None


def describe_findings(results: list[ScanResult]) -> str:
    """A sentence summarising the whole scan, for the status line and log."""
    configurable = [r for r in results if r.outcome is Outcome.CONFIGURABLE]
    readable = [r for r in results if r.outcome is Outcome.NMEA_ONLY]

    if configurable:
        found = configurable[0]
        name = protocol.create(found.protocol_kind).name
        text = (
            f"Found a configurable receiver on {found.port} at {found.baud} baud, "
            f"speaking {name}"
        )
        if len(configurable) > 1:
            text += f" (and {len(configurable) - 1} more)"
        return text + "."
    if readable:
        found = readable[0]
        return (
            f"Found a GNSS receiver on {found.port} at {found.baud} baud, but it answers "
            f"none of the command protocols this tool speaks. You can read from it and run "
            f"diagnostics; settings cannot be written."
        )
    scanned = len(results)
    return (
        f"No GNSS receiver found on {scanned} port{'s' if scanned != 1 else ''}. "
        f"Check the device is plugged in, and that you are in the 'dialout' group."
    )


# --------------------------------------------------------------------------
# The probe itself
# --------------------------------------------------------------------------


@dataclass
class _Listen:
    """Counters accumulated while listening to one (port, baud) pair."""

    bytes_seen: int = 0
    sentences: int = 0
    checksum_errors: int = 0
    talkers: set = field(default_factory=set)
    pmtk: list = field(default_factory=list)
    firmware: str = ""
    protocol_kind: protocol.Kind = protocol.Kind.UNKNOWN


def _consume(text: str, state: _Listen) -> None:
    """Decode whatever has arrived so far into ``state``."""
    for line in text.splitlines():
        sentence = pmtk.parse(line)
        if sentence is None:
            continue
        if sentence.checksum_state is ChecksumState.BAD:
            state.checksum_errors += 1
            continue
        if sentence.checksum_state is ChecksumState.ABSENT:
            continue
        if sentence.is_pmtk:
            name = f"PMTK{sentence.formatter}"
            if name not in state.pmtk:
                state.pmtk.append(name)
            release = pmtk.parse_release(sentence)
            if release is not None:
                state.firmware = str(release)
        else:
            state.sentences += 1
            if sentence.talker:
                state.talkers.add(sentence.talker)


def probe_port(
    serial_module,
    port: str,
    description: str = "",
    bauds: tuple[int, ...] = BAUD_ORDER,
    listen_s: float = LISTEN_S,
    command_wait_s: float = COMMAND_WAIT_S,
    should_stop=lambda: False,
    on_progress=lambda port, baud: None,
) -> ScanResult:
    """Try each baud rate on one port until NMEA decodes, then test for PMTK.

    ``serial_module`` is injected so this can be driven by a fake in tests.
    Stops at the first baud rate that decodes -- a receiver speaks one rate, and
    continuing would only waste seconds per port.
    """
    result = ScanResult(port=port, description=description)
    tried: list[int] = []
    total_bytes = 0
    gave_up_early = False

    for baud in bauds:
        if should_stop():
            break
        tried.append(baud)
        on_progress(port, baud)

        state = _Listen()
        try:
            with serial_module.Serial(port, baud, timeout=0.2) as handle:
                buffer = bytearray()
                deadline = time.monotonic() + listen_s
                while time.monotonic() < deadline and not should_stop():
                    buffer.extend(handle.read(1024) or b"")
                state.bytes_seen = len(buffer)
                total_bytes += len(buffer)
                _consume(buffer.decode("ascii", errors="replace"), state)

                if state.sentences == 0:
                    if (
                        total_bytes == 0
                        and len(tried) >= SILENT_BAUDS_BEFORE_GIVING_UP
                    ):
                        gave_up_early = True
                        break
                    continue

                # NMEA decodes here.  Now the question that actually matters:
                # will it answer, and in which language?  Every probe is a pure
                # query.
                handle.reset_input_buffer()
                for _, probe, _description in protocol.DETECTION_PROBES:
                    handle.write(probe)
                handle.flush()

                reply = bytearray()
                deadline = time.monotonic() + command_wait_s
                while time.monotonic() < deadline and not should_stop():
                    reply.extend(handle.read(1024) or b"")
                    if b"$PMTK" in reply or casic.looks_like_casic(bytes(reply)):
                        # Something answered; read on briefly to catch a second
                        # frame rather than burning the whole budget.
                        deadline = min(deadline, time.monotonic() + 0.4)
                _consume(reply.decode("ascii", errors="replace"), state)
                state.protocol_kind = protocol.identify(bytes(reply))

        except Exception as exc:
            result.outcome = Outcome.ERROR
            result.error = str(exc)
            result.bauds_tried = tuple(tried)
            return result

        result.baud = baud
        result.sentences = state.sentences
        result.checksum_errors = state.checksum_errors
        result.bytes_seen = state.bytes_seen
        result.talkers = tuple(sorted(state.talkers))
        result.pmtk_replies = tuple(state.pmtk)
        result.firmware = state.firmware
        result.protocol_kind = state.protocol_kind
        result.outcome = classify(
            state.bytes_seen,
            state.sentences,
            state.protocol_kind is not protocol.Kind.UNKNOWN,
        )
        result.bauds_tried = tuple(tried)
        return result

    result.bytes_seen = total_bytes
    result.bauds_tried = tuple(tried)
    result.gave_up_early = gave_up_early
    result.outcome = classify(total_bytes, 0, False)
    return result
