"""Assessment of what the link and the receiver are actually doing.

"Connected" is a weak claim: opening a serial port proves only that the kernel
had a device node, not that a GNSS receiver is on the other end, nor that it is
talking, nor that it can see the sky.  Every one of those can fail
independently, and they fail in ways that look identical from the connect
button -- so this module names them apart.

The assessment is deliberately pure: it takes a snapshot of measured values and
returns a verdict.  No Qt, no serial port, no clock of its own.  That means the
banner the user reads can be tested directly, including the states that are
awkward to produce on real hardware (a shorted antenna, a saturated port).

Order matters.  The checks run from the outside in -- is the port open, is
anything arriving, does it decode, are there satellites, is there a fix -- and
the first failure wins, because the innermost symptom is meaningless while an
outer stage is broken.  Reporting "no fix" when the real problem is that nothing
is arriving on the port sends you looking in the wrong place.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Level(IntEnum):
    """How worried to be.  Ordered, so the worst finding can be taken as a max."""

    IDLE = 0
    """Not connected; nothing to say."""

    OK = 1
    """Working as intended."""

    INFO = 2
    """Normal but transient -- still starting up, still acquiring."""

    WARN = 3
    """Working, but degraded or not doing the thing you probably wanted."""

    ERROR = 4
    """Not working."""


@dataclass(frozen=True)
class Snapshot:
    """Everything the assessment needs, measured rather than assumed."""

    is_open: bool = False
    port: str = ""
    baud: int = 0
    seconds_since_open: float = 0.0

    bytes_received: int = 0
    """Raw bytes off the port, counted before any line framing.

    This is the only honest test for "is anything arriving": at the wrong baud
    rate the data never resolves into terminated lines, so a line count stays at
    zero while bytes pour in.
    """

    lines_received: int = 0
    """Raw lines off the port, whether or not they parsed."""

    sentences_decoded: int = 0
    """Lines that parsed as NMEA with a valid checksum."""

    checksum_errors: int = 0
    seconds_since_last_sentence: float | None = None

    satellites_in_view: int = 0
    satellites_tracked: int = 0
    satellites_used: int = 0

    has_fix: bool = False
    fix_description: str = ""
    hdop: float | None = None

    antenna_status: str = ""
    command_path: str = "unknown"
    """One of ``unknown``, ``working``, ``silent``."""

    constellations: tuple[str, ...] = ()


@dataclass(frozen=True)
class Health:
    """The verdict: a level, a short headline, and the reason behind it."""

    level: Level
    headline: str
    detail: str = ""

    @property
    def is_problem(self) -> bool:
        return self.level >= Level.WARN


#: How long to wait after opening before silence counts as a fault rather than
#: as start-up.  The receiver emits at 1 Hz by default, so three seconds is
#: several missed epochs, not an unlucky gap.
STARTUP_GRACE_S = 3.0

#: Silence longer than this on an established link means something stopped.
STALL_S = 3.0

#: Above this share of traffic, checksum errors indicate a real transport
#: problem rather than the occasional corrupted line.
CHECKSUM_ERROR_FRACTION = 0.02


def assess(s: Snapshot) -> Health:
    """Work out what, if anything, is wrong.  See the module docstring on order."""

    # --- is there a link at all? ---
    if not s.is_open:
        return Health(Level.IDLE, "Not connected", "Choose a port and press Connect.")

    where = f"{s.port} at {s.baud} baud"

    # --- is anything arriving at all? ---
    if s.bytes_received == 0:
        if s.seconds_since_open < STARTUP_GRACE_S:
            return Health(Level.INFO, "Waiting for data...", f"Port {where} is open.")
        return Health(
            Level.ERROR,
            "No data from the port",
            f"{where} opened, but not one byte has arrived in "
            f"{s.seconds_since_open:.0f} s. Nothing is transmitting: check the receiver has "
            f"power and that this is the right port.",
        )

    # --- does what arrives decode? ---
    if s.sentences_decoded == 0:
        if s.seconds_since_open < STARTUP_GRACE_S:
            return Health(Level.INFO, "Waiting for a valid sentence...", f"Port {where} is open.")
        arriving = (
            f"{s.bytes_received:,} bytes"
            if s.lines_received == 0
            else f"{s.lines_received} lines ({s.bytes_received:,} bytes)"
        )
        return Health(
            Level.ERROR,
            "Data is arriving, but it is not valid NMEA",
            f"{arriving} received on {where} and nothing decoded. "
            f"This is what a wrong baud rate looks like - press Detect to sweep for the "
            f"right one. If that finds nothing, this port is not a GNSS receiver.",
        )

    # --- has an established link gone quiet? ---
    if s.seconds_since_last_sentence is not None and s.seconds_since_last_sentence > STALL_S:
        return Health(
            Level.ERROR,
            "Receiver has stopped sending",
            f"Nothing for {s.seconds_since_last_sentence:.0f} s after "
            f"{s.sentences_decoded} good sentences. If you just applied a power-saving mode "
            f"or entered standby, that is the cause; otherwise check the cable.",
        )

    # --- transport integrity ---
    total = s.sentences_decoded + s.checksum_errors
    if total and s.checksum_errors / total > CHECKSUM_ERROR_FRACTION:
        share = s.checksum_errors / total * 100
        return Health(
            Level.ERROR,
            "Corrupted data on the link",
            f"{s.checksum_errors} of {total} sentences ({share:.1f}%) failed their checksum. "
            f"Usually the port is saturated - lower the fix rate, turn off GSV, or raise the "
            f"baud rate. Check the load estimate on Rate & Output.",
        )

    # --- the antenna reports on itself ---
    if s.antenna_status and "OK" not in s.antenna_status.upper():
        return Health(
            Level.ERROR,
            f"Antenna fault: {s.antenna_status}",
            "The receiver reports this itself. OPEN means nothing is connected or the feed "
            "is broken; SHORT means the bias line is shorted. No fix is possible until this "
            "is resolved.",
        )

    # --- can it see anything? ---
    good = f"Receiving on {where}"
    if s.satellites_in_view == 0:
        return Health(
            Level.WARN,
            "Connected, but no satellites in view",
            f"{good}, and the data decodes correctly, but the receiver reports nothing in "
            f"view. Expected indoors or with the antenna disconnected; give it a clear view "
            f"of the sky and up to a minute.",
        )

    seen = _describe_sky(s)

    if s.satellites_tracked == 0:
        return Health(
            Level.WARN,
            "Satellites in view, but none tracked",
            f"{seen}. The receiver knows where they should be from its almanac but cannot "
            f"lock any signal. Typical of being indoors, a poorly sited antenna, or "
            f"interference.",
        )

    if not s.has_fix:
        return Health(
            Level.INFO,
            "Acquiring - no fix yet",
            f"{seen}. Four satellites are needed for a 3D fix and three for 2D. "
            f"After a cold start this can take 35 s or more.",
        )

    detail = f"{seen}."
    if s.hdop is not None:
        detail += f" HDOP {s.hdop:.1f}."

    # A fix on very few satellites is real but fragile; say so rather than
    # showing an unqualified green.
    if s.satellites_used < 4:
        return Health(
            Level.WARN,
            f"{s.fix_description} - marginal",
            detail + " Fewer than four satellites in the solution, so altitude is unreliable "
            "and the fix will drop out easily.",
        )

    if s.hdop is not None and s.hdop > 5.0:
        return Health(
            Level.WARN,
            f"{s.fix_description} - poor geometry",
            detail + " High HDOP means the satellites are clustered; the position is much "
            "less accurate than the satellite count suggests.",
        )

    return Health(Level.OK, s.fix_description or "Fix acquired", detail)


def _describe_sky(s: Snapshot) -> str:
    text = (
        f"{s.satellites_tracked} of {s.satellites_in_view} satellites tracked, "
        f"{s.satellites_used} used in the fix"
    )
    if s.constellations:
        text += f" ({', '.join(s.constellations)})"
    return text


def command_path_note(command_path: str) -> Health | None:
    """A separate line about whether the receiver answers commands.

    Kept apart from :func:`assess` on purpose.  A receiver that streams perfectly
    but ignores every command is completely healthy as a *receiver* and useless
    as a *configurable* device, and collapsing those into one status would hide
    whichever half you were not looking at.
    """
    if command_path == "working":
        return Health(Level.OK, "Receiver answers commands", "Settings written here will apply.")
    if command_path == "silent":
        return Health(
            Level.WARN,
            "Receiver does not answer PMTK commands",
            "It streams position data but ignores MediaTek PMTK. On the V-800 MarkIII this "
            "is because the receiver is not a MediaTek part at all - it speaks the CASIC "
            "binary protocol. Reading and diagnostics work; PMTK writes will not.",
        )
    return None
