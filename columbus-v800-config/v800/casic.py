"""CASIC/Allystar binary protocol -- what the Columbus V-800 MarkIII actually speaks.

The V-800 MarkIII is not a MediaTek receiver.  It ignores PMTK entirely, parses
u-blox UBX framing as a stub that ACK-NAKs everything, and answers this
protocol.  See ``docs/protocol-investigation.md`` for the evidence.

There is no vendor document for this unit, so **everything here was established
by measurement against the hardware**, and the docstrings record which. The
distinction matters: a field verified by writing it and watching the device
change is a fact, while a field read out of a returned payload and interpreted
is an inference. Only the former should be wired to a control that writes.

Frame layout::

    BA CE | len(2, LE) | class | id | payload(len) | checksum(4, LE)

The checksum is a 32-bit sum seeded with the header words, not a CRC::

    ck = (id << 24) + (class << 16) + len
    for each little-endian 32-bit word of the payload:
        ck += word

Nothing here touches a serial port; it is pure bytes in, bytes out, so the whole
protocol is testable without hardware.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

SYNC = b"\xba\xce"

#: Minimum bytes for a frame carrying no payload: sync + len + class/id + checksum.
MIN_FRAME = 10


class Class(IntEnum):
    """Message classes seen on this receiver."""

    NAV = 0x01
    TIM = 0x02
    RXM = 0x03
    ACK = 0x05
    CFG = 0x06
    MSG = 0x08
    MON = 0x0A
    AID = 0x0B


class Cfg(IntEnum):
    """CFG message ids.

    ``PRT``, ``MSG``, ``RATE`` and ``NAVX`` are confirmed: each returns a
    well-formed payload to a zero-length poll.  The others answer ACK but their
    payload layout is unknown, so nothing here writes them.
    """

    PRT = 0x00
    MSG = 0x01
    #: 0x02 and 0x09 are deliberately absent. In every protocol of this family
    #: those slots are reset and save/clear-configuration; without a document to
    #: confirm the payload, a mistake there is not recoverable over the wire.
    TP = 0x03
    RATE = 0x04
    NAVMODE = 0x05
    NAVX = 0x07
    GROUP = 0x08


class Ack(IntEnum):
    """ACK class message ids."""

    NACK = 0x00
    ACK = 0x01


#: NMEA sentences are addressed as class 0x4E ('N') with one id per sentence.
NMEA_CLASS = 0x4E

#: id -> sentence name.  **Every entry verified individually against the
#: hardware**: each was set to rate 0, the named sentence was observed to stop
#: while the others continued, and it was restored.  Nothing here is inferred
#: from the ordering of a table.
NMEA_MESSAGES = {
    0x00: "GGA",
    0x01: "GLL",
    0x02: "GSA",
    0x03: "GSV",
    0x04: "RMC",
    0x05: "VTG",
    0x08: "ZDA",
    0x11: "TXT",
}

NMEA_IDS = {name: mid for mid, name in NMEA_MESSAGES.items()}

#: What each sentence carries, for the UI.
NMEA_DESCRIPTIONS = {
    "GGA": "Fix data: time, position, satellites used, HDOP, altitude",
    "GLL": "Geographic position - latitude/longitude",
    "GSA": "DOP and the satellites used in the solution",
    "GSV": "Satellites in view, one group per constellation",
    "RMC": "Recommended minimum: time, date, position, speed, course",
    "VTG": "Course over ground and ground speed",
    "ZDA": "UTC time and date with a four-digit year",
    "TXT": "Text messages, including antenna status",
}

#: Rate is a divisor of the fix rate: 0 disables, N means every Nth fix.
#: Verified: rate 2 on GLL produced GLL at half the GGA cadence.
MAX_RATE = 255

#: Baud rates the port config field can express.  The receiver was found at 9600
#: on port 0, with port 1 (not exposed over USB) configured for 115200.
BAUD_RATES = (4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)

#: The USB-facing port. Port 1 exists in the receiver's configuration but is not
#: brought out to the USB bridge, so writing it would change nothing reachable.
USB_PORT_ID = 0


def checksum(cls: int, mid: int, payload: bytes) -> int:
    """The 32-bit sum checksum described in the module docstring."""
    value = ((mid << 24) + (cls << 16) + len(payload)) & 0xFFFFFFFF
    for offset in range(0, len(payload), 4):
        word = payload[offset : offset + 4].ljust(4, b"\x00")
        value = (value + int.from_bytes(word, "little")) & 0xFFFFFFFF
    return value


def build(cls: int, mid: int, payload: bytes = b"") -> bytes:
    """Frame a message ready to write to the port."""
    header = struct.pack("<HBB", len(payload), cls, mid)
    return SYNC + header + payload + struct.pack("<I", checksum(cls, mid, payload))


@dataclass(frozen=True)
class Frame:
    """One decoded CASIC frame."""

    cls: int
    mid: int
    payload: bytes
    checksum_ok: bool

    @property
    def key(self) -> tuple[int, int]:
        return (self.cls, self.mid)

    @property
    def is_ack(self) -> bool:
        return self.key == (int(Class.ACK), int(Ack.ACK))

    @property
    def is_nack(self) -> bool:
        return self.key == (int(Class.ACK), int(Ack.NACK))

    def __str__(self) -> str:
        if self.is_ack:
            return "ACK"
        if self.is_nack:
            return "NACK"
        return f"0x{self.cls:02X}/0x{self.mid:02X} ({len(self.payload)} bytes)"


def parse(buffer: bytes) -> tuple[list[Frame], int]:
    """Extract every complete frame from ``buffer``.

    Returns the frames and the number of bytes consumed, so a caller reading a
    stream can keep the trailing partial frame and hand it back next time.  A
    frame with a bad checksum is still returned, flagged -- corrupt traffic and
    absent traffic are different faults and the console should show both.
    """
    frames: list[Frame] = []
    index = 0
    consumed = 0
    while True:
        start = buffer.find(SYNC, index)
        if start < 0 or start + MIN_FRAME > len(buffer):
            break
        length = int.from_bytes(buffer[start + 2 : start + 4], "little")
        cls, mid = buffer[start + 4], buffer[start + 5]
        end = start + 6 + length + 4
        if end > len(buffer):
            break
        payload = bytes(buffer[start + 6 : start + 6 + length])
        given = int.from_bytes(buffer[start + 6 + length : end], "little")
        frames.append(Frame(cls, mid, payload, given == checksum(cls, mid, payload)))
        index = end
        consumed = end
    return frames, consumed


# --------------------------------------------------------------------------
# Polls
# --------------------------------------------------------------------------
#
# A zero-length CFG message is a poll.  Confirmed against the hardware: each of
# the four below answers with its current configuration followed by an ACK.


def poll(mid: int) -> bytes:
    """Build a zero-length CFG poll."""
    return build(int(Class.CFG), int(mid))


def poll_port() -> bytes:
    """CFG-PRT -- returns one payload per port."""
    return poll(Cfg.PRT)


def poll_message_rates() -> bytes:
    """CFG-MSG -- returns the whole rate table, one frame per message.

    Note this receiver rejects a *targeted* CFG-MSG poll (a two-byte
    class/id payload gets NACKed); only the full dump works.
    """
    return poll(Cfg.MSG)


def poll_rate() -> bytes:
    """CFG-RATE -- returns the measurement interval."""
    return poll(Cfg.RATE)


def poll_navx() -> bytes:
    """CFG-NAVX -- returns 44 bytes whose layout is not established.

    Polled so the raw payload can be shown, never written.
    """
    return poll(Cfg.NAVX)


# --------------------------------------------------------------------------
# CFG-RATE  (0x06/0x04)  -- verified by writing and measuring
# --------------------------------------------------------------------------

MIN_INTERVAL_MS = 50
MAX_INTERVAL_MS = 65535


def set_fix_interval(interval_ms: int) -> bytes:
    """Set the measurement interval in milliseconds.

    Verified: writing 200 ms changed the observed cadence, and writing 1000 ms
    restored it, with the device reporting the new value back both times.

    The receiver will not necessarily *achieve* the requested rate -- at 9600
    baud with the default sentence set a 5 Hz request measured 1.67 Hz, because
    the sentences do not fit the link.  That is a link budget problem, not a
    rejected command, so this does not treat it as an error.
    """
    interval_ms = int(interval_ms)
    if not (MIN_INTERVAL_MS <= interval_ms <= MAX_INTERVAL_MS):
        raise ValueError(
            f"fix interval must be {MIN_INTERVAL_MS}..{MAX_INTERVAL_MS} ms, got {interval_ms}"
        )
    return build(int(Class.CFG), int(Cfg.RATE), struct.pack("<HH", interval_ms, 0))


def parse_fix_interval(frame: Frame) -> int | None:
    """Read the measurement interval from a CFG-RATE reply."""
    if frame.key != (int(Class.CFG), int(Cfg.RATE)) or len(frame.payload) < 2:
        return None
    return struct.unpack_from("<H", frame.payload, 0)[0]


# --------------------------------------------------------------------------
# CFG-MSG  (0x06/0x01)  -- verified per sentence
# --------------------------------------------------------------------------


def set_message_rate(msg_class: int, msg_id: int, rate: int) -> bytes:
    """Set how often one message is emitted, as a divisor of the fix rate."""
    rate = int(rate)
    if not (0 <= rate <= MAX_RATE):
        raise ValueError(f"rate must be 0..{MAX_RATE}, got {rate}")
    return build(
        int(Class.CFG), int(Cfg.MSG), bytes([msg_class & 0xFF, msg_id & 0xFF, rate, 0])
    )


def set_sentence_rate(name: str, rate: int) -> bytes:
    """Set the rate for a named NMEA sentence, e.g. ``set_sentence_rate("GSV", 5)``."""
    key = name.upper()
    if key not in NMEA_IDS:
        raise ValueError(f"unknown NMEA sentence {name!r}; known: {sorted(NMEA_IDS)}")
    return set_message_rate(NMEA_CLASS, NMEA_IDS[key], rate)


def parse_message_rate(frame: Frame) -> tuple[int, int, int] | None:
    """Decode one CFG-MSG reply into ``(class, id, rate)``."""
    if frame.key != (int(Class.CFG), int(Cfg.MSG)) or len(frame.payload) < 3:
        return None
    return frame.payload[0], frame.payload[1], frame.payload[2]


def collect_sentence_rates(frames: list[Frame]) -> dict[str, int]:
    """Pick the NMEA rates out of a full CFG-MSG dump.

    Messages outside the NMEA class, and NMEA ids this module has not verified,
    are ignored rather than guessed at.
    """
    rates: dict[str, int] = {}
    for frame in frames:
        decoded = parse_message_rate(frame)
        if decoded is None:
            continue
        msg_class, msg_id, rate = decoded
        if msg_class != NMEA_CLASS:
            continue
        name = NMEA_MESSAGES.get(msg_id)
        if name is not None:
            rates[name] = rate
    return rates


# --------------------------------------------------------------------------
# CFG-PRT  (0x06/0x00)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PortConfig:
    """One port's configuration.

    ``port_id`` and ``baud`` are confirmed -- port 0 reported 9600 while the link
    was demonstrably open at 9600.  ``protocol_mask`` and ``mode`` are carried
    through verbatim because their bit layout is not established; writing a port
    reuses whatever the device reported rather than composing a value.
    """

    port_id: int
    protocol_mask: int
    mode: int
    baud: int
    raw: bytes = b""


def parse_port_config(frame: Frame) -> PortConfig | None:
    """Decode a CFG-PRT reply."""
    if frame.key != (int(Class.CFG), int(Cfg.PRT)) or len(frame.payload) < 8:
        return None
    port_id, protocol_mask = frame.payload[0], frame.payload[1]
    mode = struct.unpack_from("<H", frame.payload, 2)[0]
    baud = struct.unpack_from("<I", frame.payload, 4)[0]
    return PortConfig(port_id, protocol_mask, mode, baud, bytes(frame.payload))


def set_port_baud(current: PortConfig, baud: int) -> bytes:
    """Change a port's baud rate, preserving every other field.

    Only the baud word is altered.  The protocol mask and mode bits are echoed
    back exactly as the receiver reported them, because guessing at undocumented
    bits in the message that controls the port you are talking over is the one
    mistake you cannot then undo over that port.
    """
    baud = int(baud)
    if baud not in BAUD_RATES:
        raise ValueError(f"baud must be one of {BAUD_RATES}, got {baud}")
    payload = bytearray(current.raw if len(current.raw) >= 8 else bytes(8))
    payload[0] = current.port_id
    payload[1] = current.protocol_mask
    struct.pack_into("<H", payload, 2, current.mode)
    struct.pack_into("<I", payload, 4, baud)
    return build(int(Class.CFG), int(Cfg.PRT), bytes(payload))


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

#: A poll that is cheap, harmless and answered by any CASIC receiver.  CFG-RATE
#: is used rather than a MON message because this device NACKs MON entirely.
DETECT_POLL = build(int(Class.CFG), int(Cfg.RATE))


def looks_like_casic(buffer: bytes) -> bool:
    """True if ``buffer`` holds at least one checksum-valid CASIC frame.

    The checksum matters: ``BA CE`` can occur inside binary noise, but a frame
    whose 32-bit sum also checks out effectively cannot.
    """
    frames, _ = parse(buffer)
    return any(frame.checksum_ok for frame in frames)


# --------------------------------------------------------------------------
# $PCAS -- the ASCII half of the protocol
# --------------------------------------------------------------------------
#
# This receiver never *replies* to a $PCAS sentence, which is why an earlier
# probe concluded it did not support them.  It does: it acts on them silently.
# Established by sending $PCAS02 and measuring the fix cadence change.
#
# Because there is no reply, a $PCAS write cannot be confirmed by an
# acknowledgement.  It can only be confirmed by observing the effect, so
# anything wired to $PCAS must be read back from the data stream rather than
# from a query.

#: $PCAS04 constellation mask. Bit 0 = GPS, bit 1 = BeiDou, bit 2 = GLONASS.
#: All five reachable combinations were verified individually against the
#: hardware by setting the mask and observing which GSV talkers appeared.
CONSTELLATION_BITS = {"GPS": 0x01, "BEIDOU": 0x02, "GLONASS": 0x04}


def set_constellations(gps: bool, glonass: bool, beidou: bool) -> bytes:
    """Build $PCAS04 selecting which constellations to track.

    At least one must be enabled; a mask of 0 would leave the receiver unable
    to fix and there is no reply to tell you that is what happened.
    """
    mask = 0
    if gps:
        mask |= CONSTELLATION_BITS["GPS"]
    if beidou:
        mask |= CONSTELLATION_BITS["BEIDOU"]
    if glonass:
        mask |= CONSTELLATION_BITS["GLONASS"]
    if mask == 0:
        raise ValueError("at least one constellation must be enabled")
    from . import pmtk  # local import: pmtk only supplies NMEA framing here

    return pmtk.build(f"PCAS04,{mask}")


def constellations_from_mask(mask: int) -> dict[str, bool]:
    """Decode a $PCAS04 mask back into named constellations."""
    return {
        "GPS": bool(mask & CONSTELLATION_BITS["GPS"]),
        "GLONASS": bool(mask & CONSTELLATION_BITS["GLONASS"]),
        "BeiDou": bool(mask & CONSTELLATION_BITS["BEIDOU"]),
    }


#: Offsets within the 44-byte CFG-NAVX payload that were identified by
#: *differential probing*: a $PCAS command was sent and the payload diffed
#: before and after, so the mapping is measured rather than guessed.
#:
#: Byte 9 deliberately has no name -- it changes on its own between polls, so it
#: is live state of some kind, not configuration.
NAVX_NAV_MODE = 4
NAVX_CONSTELLATIONS = 13
NAVX_LENGTH = 44

#: $PCAS11 navigation (dynamic) mode. 0-8 are accepted by this receiver; 9 is
#: clamped to 8, which is how the range was established.
#:
#: The *names* are the conventional CASIC dynamic-model labels and are NOT
#: verified on this unit -- confirming them would need controlled motion.  The
#: numeric value is what gets written, and the read-back proves which value took.
NAV_MODES = {
    0: "Portable / normal",
    1: "Stationary",
    2: "Pedestrian",
    3: "Automotive",
    4: "Sea",
    5: "Airborne < 1 g",
    6: "Airborne < 2 g",
    7: "Airborne < 4 g",
    8: "Mode 8",
}

MAX_NAV_MODE = 8


def set_navigation_mode(mode: int) -> bytes:
    """$PCAS11 -- the dynamic model the navigation filter assumes.

    Verified by read-back: every value 0..8 written here appears at
    ``CFG-NAVX[4]``.  Like all $PCAS commands this is not acknowledged, so the
    read-back is the only confirmation.
    """
    mode = int(mode)
    if not (0 <= mode <= MAX_NAV_MODE):
        raise ValueError(f"navigation mode must be 0..{MAX_NAV_MODE}, got {mode}")
    from . import pmtk

    return pmtk.build(f"PCAS11,{mode}")


def parse_navx(frame: Frame) -> dict | None:
    """Read the two fields of CFG-NAVX whose meaning has been established.

    Everything else in the 44 bytes is returned raw rather than interpreted.
    This is the read-back for both the navigation mode and the constellation
    mask -- the latter matters because ``$PCAS04`` itself is never acknowledged.
    """
    if frame.key != (int(Class.CFG), int(Cfg.NAVX)) or len(frame.payload) < NAVX_LENGTH:
        return None
    mask = frame.payload[NAVX_CONSTELLATIONS]
    return {
        "nav_mode": frame.payload[NAVX_NAV_MODE],
        "constellation_mask": mask,
        "constellations": constellations_from_mask(mask),
        "raw": bytes(frame.payload),
    }


def set_fix_interval_ascii(interval_ms: int) -> bytes:
    """$PCAS02 -- the ASCII equivalent of CFG-RATE.

    Kept because it is what proved $PCAS is honoured at all.  The binary
    CFG-RATE is preferred in normal use because it can be read back.
    """
    interval_ms = int(interval_ms)
    if not (MIN_INTERVAL_MS <= interval_ms <= MAX_INTERVAL_MS):
        raise ValueError(f"fix interval must be {MIN_INTERVAL_MS}..{MAX_INTERVAL_MS} ms")
    from . import pmtk

    return pmtk.build(f"PCAS02,{interval_ms}")


def describe(frame: Frame) -> str:
    """One line for the console."""
    if frame.is_ack:
        return "CASIC ACK"
    if frame.is_nack:
        return "CASIC NACK (message not supported)"
    names = {
        (0x06, 0x00): "CFG-PRT port configuration",
        (0x06, 0x01): "CFG-MSG message rate",
        (0x06, 0x04): "CFG-RATE measurement interval",
        (0x06, 0x07): "CFG-NAVX navigation configuration",
    }
    label = names.get(frame.key, f"class 0x{frame.cls:02X} id 0x{frame.mid:02X}")
    return f"CASIC {label} ({len(frame.payload)} bytes)"
