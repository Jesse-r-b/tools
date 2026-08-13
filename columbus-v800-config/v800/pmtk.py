"""PMTK proprietary NMEA protocol for MediaTek MT3333-class GNSS receivers.

Every command builder and constant here is transcribed from the *MT3333 Platform
NMEA Message Specification For GPS+GLONASS*, V1.00, 2013-09-26 (a copy lives in
``docs/``).  Section numbers in the docstrings refer to that document.

The Columbus V-800 MarkIII is an MT3333-class multi-GNSS receiver presented over
a Prolific PL2303 USB-serial bridge, so the whole PMTK surface applies.  Where a
command's behaviour is firmware-dependent the spec says so, and that note is
repeated here -- the GUI must never assume a setting took effect just because
the command was sent.  Verify with the matching query.

Nothing in this module talks to a serial port; it is pure string handling so it
can be unit-tested without hardware.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum

# --------------------------------------------------------------------------
# Sentence framing
# --------------------------------------------------------------------------

#: A well-formed NMEA-0183 sentence, with the checksum optional so that we can
#: report "missing checksum" separately from "wrong checksum".
_SENTENCE_RE = re.compile(r"^[$!](?P<payload>[^*$!\r\n]*)(?:\*(?P<cksum>[0-9A-Fa-f]{2}))?\s*$")


def checksum(payload: str) -> int:
    """XOR of every character between ``$`` and ``*``, per NMEA-0183."""
    value = 0
    for ch in payload:
        value ^= ord(ch)
    return value


def format_number(value: float, significant: int = 12) -> str:
    """Format a float for an NMEA field without losing it to scientific notation.

    ``%g`` is the obvious choice and the wrong one: it switches to an exponent
    above six significant digits, so a datum semi-major axis of 6377397.155 m
    goes out as ``6.3774e+06`` -- a field the receiver cannot parse, carrying a
    value 8 mm to 400 m adrift depending on how it is read.  Nothing type-checks
    that; floats are floats either way.

    So: fixed notation, enough significant digits to carry every value the
    protocol accepts, and trailing zeros trimmed so the field stays short.
    """
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"cannot format {value!r} into an NMEA field")
    text = f"{value:.{significant}g}"
    if "e" in text or "E" in text:
        # Fall back to fixed notation with enough decimals to be lossless for
        # the ranges this protocol uses.
        text = f"{value:.6f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def build(payload: str) -> bytes:
    """Frame a bare payload (e.g. ``PMTK101``) into a complete sentence.

    Returns bytes terminated with CR LF, ready to write to the port.
    """
    payload = payload.lstrip("$")
    if "*" in payload:
        payload = payload.split("*", 1)[0]
    return f"${payload}*{checksum(payload):02X}\r\n".encode("ascii")


class ChecksumState(IntEnum):
    """Why we do or do not trust a received sentence."""

    OK = 0
    BAD = 1
    ABSENT = 2


@dataclass(frozen=True)
class Sentence:
    """One parsed NMEA/PMTK sentence."""

    raw: str
    talker: str
    """Talker ID: ``GP``, ``GL``, ``GN``, ``GB``/``BD``, ``GA``, ``GQ``, or ``PMTK``-style ``P``."""
    formatter: str
    """Sentence formatter: ``GGA``, ``RMC``, ... or the PMTK packet type as text, e.g. ``001``."""
    fields: tuple[str, ...]
    """Comma-separated fields *after* the address field."""
    checksum_state: ChecksumState

    @property
    def address(self) -> str:
        """The full address field, e.g. ``GPGGA`` or ``PMTK001``."""
        return self.talker + self.formatter

    @property
    def is_pmtk(self) -> bool:
        return self.talker == "PMTK"

    @property
    def packet_type(self) -> int | None:
        """PMTK packet type as an integer, or ``None`` for standard NMEA."""
        if not self.is_pmtk:
            return None
        try:
            return int(self.formatter)
        except ValueError:
            return None


def parse(line: str) -> Sentence | None:
    """Parse one line into a :class:`Sentence`, or ``None`` if it is not a sentence.

    A bad checksum is reported in ``checksum_state`` rather than rejected, so the
    console can show corrupt traffic instead of silently dropping it -- a line
    disappearing and a line arriving mangled are very different faults.
    """
    line = line.strip()
    match = _SENTENCE_RE.match(line)
    if not match:
        return None
    payload = match.group("payload")
    given = match.group("cksum")
    if given is None:
        state = ChecksumState.ABSENT
    elif int(given, 16) == checksum(payload):
        state = ChecksumState.OK
    else:
        state = ChecksumState.BAD

    parts = payload.split(",")
    address = parts[0]
    fields = tuple(parts[1:])

    if address.startswith("PMTK"):
        talker, formatter = "PMTK", address[4:]
    elif len(address) >= 5:
        talker, formatter = address[:2], address[2:]
    else:
        talker, formatter = address, ""

    return Sentence(line, talker, formatter, fields, state)


# --------------------------------------------------------------------------
# Packet types (section 2.3)
# --------------------------------------------------------------------------


class Packet(IntEnum):
    """PMTK packet type numbers, named as in the specification."""

    TEST = 0
    ACK = 1
    SYS_MSG = 10
    TXT_MSG = 11
    CMD_HOT_START = 101
    CMD_WARM_START = 102
    CMD_COLD_START = 103
    CMD_FULL_COLD_START = 104
    CMD_CLEAR_FLASH_AID = 120
    CMD_STANDBY_MODE = 161
    SET_POS_FIX = 220
    SET_AL_DEE_CFG = 223
    SET_PERIODIC_MODE = 225
    SET_NMEA_BAUDRATE = 251
    SET_AIC_CMD = 286
    API_SET_FIX_CTL = 300
    API_SET_DGPS_MODE = 301
    API_SET_SBAS_ENABLED = 313
    API_SET_NMEA_OUTPUT = 314
    API_SET_DATUM = 330
    API_SET_DATUM_ADVANCE = 331
    API_SET_RTC_TIME = 335
    API_SET_SUPPORT_QZSS_NMEA = 351
    API_SET_STOP_QZSS = 352
    API_SET_GNSS_SEARCH_MODE = 353
    API_SET_STATIC_NAV_THD = 386
    API_Q_FIX_CTL = 400
    API_Q_DGPS_MODE = 401
    API_Q_SBAS_ENABLED = 413
    API_Q_NMEA_OUTPUT = 414
    API_Q_DATUM = 430
    API_Q_DATUM_ADVANCE = 431
    DT_FIX_CTL = 500
    DT_DGPS_MODE = 501
    DT_SBAS_ENABLED = 513
    DT_NMEA_OUTPUT = 514
    DT_DATUM = 530
    DT_SET_TCXO_DEBUG = 589
    Q_RELEASE = 605
    Q_EPO_INFO = 607
    Q_AVAILABLE_SV_EPH = 660
    Q_AVAILABLE_SV_ALM = 661
    DT_RELEASE = 705
    DT_UTC = 740
    DT_POS = 741
    TEST_ALL = 810
    TEST_STOP = 811
    TEST_FINISH = 812
    TEST_ALL_ACQ = 813
    TEST_ALL_BITSYNC = 814
    TEST_ALL_SIGNAL = 815
    TEST_JAMMING = 837


#: Query packet type -> the packet type the receiver answers with (section 2.3).
#: PMTK605 is the odd one out: it is answered by PMTK705, not by an ACK.
QUERY_REPLY = {
    Packet.API_Q_FIX_CTL: Packet.DT_FIX_CTL,
    Packet.API_Q_DGPS_MODE: Packet.DT_DGPS_MODE,
    Packet.API_Q_SBAS_ENABLED: Packet.DT_SBAS_ENABLED,
    Packet.API_Q_NMEA_OUTPUT: Packet.DT_NMEA_OUTPUT,
    Packet.API_Q_DATUM: Packet.DT_DATUM,
    Packet.API_Q_DATUM_ADVANCE: Packet.DT_DATUM,
    Packet.Q_RELEASE: Packet.DT_RELEASE,
}


#: Packets that ask the receiver something rather than change it.  Used to word
#: a timeout correctly: a query that goes unanswered has not "failed to apply",
#: it simply was not answered, and saying otherwise sends the reader looking for
#: a setting that was never being set.
QUERY_PACKETS = frozenset(
    {int(p) for p in QUERY_REPLY}
    | {int(Packet.Q_EPO_INFO), int(Packet.Q_AVAILABLE_SV_EPH), int(Packet.Q_AVAILABLE_SV_ALM)}
)


def is_query(packet_type: int) -> bool:
    """True if this packet type asks a question rather than changes a setting."""
    return int(packet_type) in QUERY_PACKETS


class AckFlag(IntEnum):
    """PMTK001 flag field (table 2-14)."""

    INVALID = 0
    UNSUPPORTED = 1
    FAILED = 2
    SUCCEEDED = 3


ACK_TEXT = {
    AckFlag.INVALID: "invalid command / packet",
    AckFlag.UNSUPPORTED: "unsupported command / packet type",
    AckFlag.FAILED: "valid packet, but action failed",
    AckFlag.SUCCEEDED: "valid packet, action succeeded",
}


class SysMsg(IntEnum):
    """PMTK010 message field (table 2-15)."""

    UNKNOWN = 0
    STARTUP = 1
    EPO_AIDING_NOTIFICATION = 2
    NORMAL_MODE_TRANSITION_DONE = 3


SYS_MSG_TEXT = {
    SysMsg.UNKNOWN: "unknown",
    SysMsg.STARTUP: "startup",
    SysMsg.EPO_AIDING_NOTIFICATION: "notification for the host aiding EPO",
    SysMsg.NORMAL_MODE_TRANSITION_DONE: "transition to normal mode done",
}


@dataclass(frozen=True)
class Ack:
    """A decoded PMTK001 acknowledgement."""

    command: int
    flag: AckFlag

    @property
    def ok(self) -> bool:
        return self.flag is AckFlag.SUCCEEDED

    def __str__(self) -> str:
        return f"PMTK{self.command:03d}: {ACK_TEXT.get(self.flag, 'unknown flag')}"


def parse_ack(sentence: Sentence) -> Ack | None:
    """Decode a PMTK001 sentence, or return ``None`` if it is not one.

    PMTK660/661 reuse PMTK001 as a *data* carrier (fields ``660,3,<hex flags>``),
    so those are deliberately not treated as plain acknowledgements here -- see
    :func:`parse_available_sv`.
    """
    if sentence.packet_type != Packet.ACK or len(sentence.fields) < 2:
        return None
    try:
        command = int(sentence.fields[0])
        flag = AckFlag(int(sentence.fields[1]))
    except ValueError:
        return None
    return Ack(command, flag)


# --------------------------------------------------------------------------
# NMEA output configuration (PMTK314 / PMTK514, section 2.3.19)
# --------------------------------------------------------------------------

#: Field index -> sentence name, for the 19-field PMTK314 payload.  The spec
#: names only these seven; the remaining twelve fields are reserved and must
#: still be transmitted (as 0) to keep the field count at 19.
NMEA_OUTPUT_FIELDS: dict[int, str] = {
    0: "GLL",
    1: "RMC",
    2: "VTG",
    3: "GGA",
    4: "GSA",
    5: "GSV",
    17: "ZDA",
}

NMEA_OUTPUT_FIELD_COUNT = 19

#: Descriptions taken from section 2.3.19.
NMEA_OUTPUT_DESCRIPTIONS = {
    "GLL": "Geographic position - latitude/longitude",
    "RMC": "Recommended minimum specific GNSS data",
    "VTG": "Course over ground and ground speed",
    "GGA": "GNSS fix data",
    "GSA": "GNSS DOP and active satellites",
    "GSV": "GNSS satellites in view",
    "ZDA": "Time and date",
}

#: Supported per-sentence rate divisors.  0 disables; N means "once every N fixes".
NMEA_RATE_CHOICES = (0, 1, 2, 3, 4, 5)

#: The receiver's own default, as printed in the PMTK514 example (section 2.3.36).
NMEA_OUTPUT_RESTORE_DEFAULT = "PMTK314,-1"


def set_nmea_output(rates: dict[str, int]) -> str:
    """Build PMTK314 from a ``{"GGA": 1, "GSV": 5, ...}`` mapping.

    Unnamed sentences default to 0.  Raises ``ValueError`` on an unknown name or
    an out-of-range divisor rather than silently clamping -- a rate the chipset
    rejects should surface as an error here, not as a mysterious PMTK001 flag 0.
    """
    slots = [0] * NMEA_OUTPUT_FIELD_COUNT
    index_of = {name: idx for idx, name in NMEA_OUTPUT_FIELDS.items()}
    for name, rate in rates.items():
        key = name.upper()
        if key not in index_of:
            raise ValueError(f"PMTK314 has no field for sentence {name!r}")
        if rate not in NMEA_RATE_CHOICES:
            raise ValueError(f"rate for {key} must be one of {NMEA_RATE_CHOICES}, got {rate!r}")
        slots[index_of[key]] = rate
    return "PMTK314," + ",".join(str(v) for v in slots)


def restore_nmea_output_defaults() -> str:
    """PMTK314,-1 -- restore the system default output set (section 2.3.19)."""
    return NMEA_OUTPUT_RESTORE_DEFAULT


def parse_nmea_output(sentence: Sentence) -> dict[str, int] | None:
    """Decode a PMTK514 reply into ``{"GGA": 1, ...}``."""
    if sentence.packet_type != Packet.DT_NMEA_OUTPUT:
        return None
    out: dict[str, int] = {}
    for index, name in NMEA_OUTPUT_FIELDS.items():
        if index < len(sentence.fields):
            try:
                out[name] = int(sentence.fields[index])
            except ValueError:
                out[name] = 0
        else:
            out[name] = 0
    return out


# --------------------------------------------------------------------------
# Restart / power (sections 2.3.5 - 2.3.13)
# --------------------------------------------------------------------------


def hot_start() -> str:
    """PMTK101 -- restart using all available data in the NV store."""
    return "PMTK101"


def warm_start() -> str:
    """PMTK102 -- restart without using ephemeris."""
    return "PMTK102"


def cold_start() -> str:
    """PMTK103 -- restart without time, position, almanac or ephemeris."""
    return "PMTK103"


def full_cold_start() -> str:
    """PMTK104 -- cold start *and* reset system/user configuration to factory state.

    Note this also reverts a PMTK251 baud rate change (section 2.3.14).
    """
    return "PMTK104"


def clear_flash_aid() -> str:
    """PMTK120 -- erase aiding data held in flash."""
    return "PMTK120"


class StandbyType(IntEnum):
    """PMTK161 standby type (table 2-21)."""

    STOP = 0
    SLEEP = 1


def standby_mode(kind: StandbyType | int = StandbyType.STOP) -> str:
    """PMTK161 -- enter standby for power saving."""
    return f"PMTK161,{int(kind)}"


class PeriodicMode(IntEnum):
    """PMTK225 operation mode (table 2-25)."""

    NORMAL = 0
    PERIODIC_BACKUP = 1
    PERIODIC_STANDBY = 2
    PERPETUAL_BACKUP = 4
    ALWAYSLOCATE_STANDBY = 8
    ALWAYSLOCATE_BACKUP = 9


PERIODIC_MODE_TEXT = {
    PeriodicMode.NORMAL: "Normal (no power saving)",
    PeriodicMode.PERIODIC_BACKUP: "Periodic backup",
    PeriodicMode.PERIODIC_STANDBY: "Periodic standby",
    PeriodicMode.PERPETUAL_BACKUP: "Perpetual backup",
    PeriodicMode.ALWAYSLOCATE_STANDBY: "AlwaysLocate standby",
    PeriodicMode.ALWAYSLOCATE_BACKUP: "AlwaysLocate backup",
}

#: Modes that take the four timing arguments.  The AlwaysLocate and perpetual
#: modes are sent bare (section 2.3.13 examples).
PERIODIC_MODES_WITH_TIMING = (PeriodicMode.PERIODIC_BACKUP, PeriodicMode.PERIODIC_STANDBY)

PERIODIC_TIME_MIN = 1000
PERIODIC_TIME_MAX = 518400000


def periodic_mode(
    mode: PeriodicMode | int,
    run_time: int | None = None,
    sleep_time: int | None = None,
    second_run_time: int | None = None,
    second_sleep_time: int | None = None,
) -> str:
    """PMTK225 -- periodic power-saving mode.

    Times are in milliseconds.  ``0`` disables a slot; otherwise the spec's range
    is 1000..518400000, and the second run time must exceed the first when
    non-zero.  Those constraints are enforced here.
    """
    mode = PeriodicMode(int(mode))
    if mode not in PERIODIC_MODES_WITH_TIMING:
        return f"PMTK225,{int(mode)}"

    values = [run_time, sleep_time, second_run_time, second_sleep_time]
    names = ["run time", "sleep time", "second run time", "second sleep time"]
    resolved: list[int] = []
    for value, name in zip(values, names):
        if value is None:
            raise ValueError(f"{mode.name} requires {name}")
        value = int(value)
        if value != 0 and not (PERIODIC_TIME_MIN <= value <= PERIODIC_TIME_MAX):
            raise ValueError(
                f"{name} must be 0 or {PERIODIC_TIME_MIN}..{PERIODIC_TIME_MAX} ms, got {value}"
            )
        resolved.append(value)

    if resolved[2] and resolved[2] <= resolved[0]:
        raise ValueError("second run time must be larger than the first run time when non-zero")

    return "PMTK225," + ",".join(str(v) for v in [int(mode), *resolved])


AL_DEE_SV_RANGE = (1, 4)
AL_DEE_SNR_RANGE = (25, 30)
AL_DEE_EXT_THRESHOLD_RANGE = (40000, 180000)
AL_DEE_EXT_GAP_RANGE = (0, 3600000)


def al_dee_config(
    sv: int = 1,
    snr: int = 30,
    extension_threshold: int = 180000,
    extension_gap: int = 60000,
) -> str:
    """PMTK223 -- AlwaysLocate / DEE tuning (table 2-24).

    ``sv`` is the number of satellites and ``snr`` the C/N0 the receiver must
    reach before it is happy to sleep; the two extension figures are in ms.
    """
    for value, (low, high), name in (
        (sv, AL_DEE_SV_RANGE, "SV"),
        (snr, AL_DEE_SNR_RANGE, "SNR"),
        (extension_threshold, AL_DEE_EXT_THRESHOLD_RANGE, "extension threshold"),
        (extension_gap, AL_DEE_EXT_GAP_RANGE, "extension gap"),
    ):
        if not (low <= int(value) <= high):
            raise ValueError(f"{name} must be {low}..{high}, got {value}")
    return f"PMTK223,{int(sv)},{int(snr)},{int(extension_threshold)},{int(extension_gap)}"


# --------------------------------------------------------------------------
# Fix rate and port (sections 2.3.11, 2.3.14, 2.3.16)
# --------------------------------------------------------------------------

POS_FIX_MIN_MS = 100
FIX_CTL_RANGE_MS = (100, 10000)


def set_pos_fix(interval_ms: int) -> str:
    """PMTK220 -- position fix interval in ms (must be > 100).

    The rate the receiver can actually sustain depends on how many sentences are
    enabled and on the port baud rate; 10 Hz with the full sentence set will not
    fit in 9600 baud.  :func:`nmea_budget_bps` estimates that.
    """
    interval_ms = int(interval_ms)
    if interval_ms < POS_FIX_MIN_MS:
        raise ValueError(f"position fix interval must be >= {POS_FIX_MIN_MS} ms")
    return f"PMTK220,{interval_ms}"


def set_fix_ctl(interval_ms: int) -> str:
    """PMTK300 -- fix interval in ms, range 100..10000 (table 2-28).

    The trailing four fields are reserved and are sent as 0, as in the spec's
    own example ``$PMTK300,1000,0,0,0,0``.
    """
    interval_ms = int(interval_ms)
    low, high = FIX_CTL_RANGE_MS
    if not (low <= interval_ms <= high):
        raise ValueError(f"fix interval must be {low}..{high} ms, got {interval_ms}")
    return f"PMTK300,{interval_ms},0,0,0,0"


def query_fix_ctl() -> str:
    """PMTK400 -- query fix control; answered by PMTK500."""
    return "PMTK400"


def parse_fix_ctl(sentence: Sentence) -> int | None:
    """Decode the fix interval from a PMTK500 reply."""
    if sentence.packet_type != Packet.DT_FIX_CTL or not sentence.fields:
        return None
    try:
        return int(sentence.fields[0])
    except ValueError:
        return None


#: Baud rates PMTK251 accepts (table 2-26).  0 means "back to default".
BAUD_RATES = (0, 4800, 9600, 14400, 19200, 38400, 57600, 115200, 230400, 460800, 921600)

#: The V-800 family ships at 38400 baud, NMEA 0183 v3.01 -- see the Columbus
#: V-800 specification page.  Used as the connect-dialog default.
DEFAULT_BAUD = 38400


def set_nmea_baudrate(baud: int) -> str:
    """PMTK251 -- set the NMEA port baud rate.

    The spec warns this reverts to default on a full cold start (PMTK104) or on
    entering standby, so the GUI must be prepared to re-scan for the port speed.
    """
    baud = int(baud)
    if baud not in BAUD_RATES:
        raise ValueError(f"baud rate must be one of {BAUD_RATES}, got {baud}")
    return f"PMTK251,{baud}"


# --------------------------------------------------------------------------
# Constellations, augmentation, navigation behaviour
# --------------------------------------------------------------------------


class DgpsMode(IntEnum):
    """PMTK301/PMTK501 DGPS data source (table 2-29)."""

    NONE = 0
    RTCM = 1
    WAAS = 2


DGPS_MODE_TEXT = {
    DgpsMode.NONE: "No DGPS source",
    DgpsMode.RTCM: "RTCM",
    DgpsMode.WAAS: "WAAS",
}


def set_dgps_mode(mode: DgpsMode | int) -> str:
    """PMTK301 -- DGPS correction data source."""
    return f"PMTK301,{int(DgpsMode(int(mode)))}"


def query_dgps_mode() -> str:
    """PMTK401 -- query DGPS mode; answered by PMTK501."""
    return "PMTK401"


def set_sbas_enabled(enabled: bool) -> str:
    """PMTK313 -- search for SBAS satellites or not."""
    return f"PMTK313,{1 if enabled else 0}"


def query_sbas_enabled() -> str:
    """PMTK413 -- query SBAS; answered by PMTK513."""
    return "PMTK413"


def set_gnss_search_mode(gps: bool, glonass: bool) -> str:
    """PMTK353 -- which constellations to search (table 2-36).

    The spec documents only the GPS and GLONASS fields for this platform.  A
    firmware build that also carries BeiDou accepts extra trailing fields, but
    since that is undocumented here we send the documented two-field form and
    let the GUI read back what the receiver actually reports in its ACK.
    """
    return f"PMTK353,{1 if gps else 0},{1 if glonass else 0}"


def parse_gnss_search_mode(sentence: Sentence) -> tuple[int, ...] | None:
    """Decode the echo of a PMTK353 from its PMTK001 acknowledgement.

    Firmware answers ``$PMTK001,353,3,<gps>,<glonass>[,...]`` -- the fields after
    the flag mirror the constellations actually enabled, which is the only
    reliable read-back for this setting.
    """
    if sentence.packet_type != Packet.ACK or len(sentence.fields) < 3:
        return None
    if sentence.fields[0] != str(int(Packet.API_SET_GNSS_SEARCH_MODE)):
        return None
    values: list[int] = []
    for raw in sentence.fields[2:]:
        try:
            values.append(int(raw))
        except ValueError:
            break
    return tuple(values) or None


def set_qzss_nmea_format(enabled: bool) -> str:
    """PMTK351 -- use the QZSS NMEA format (default: disabled, NMEA 0183 v3.01)."""
    return f"PMTK351,{1 if enabled else 0}"


def set_qzss_enabled(enabled: bool) -> str:
    """PMTK352 -- enable QZSS ranging.

    Careful: this command's polarity is inverted relative to every other
    enable/disable in the protocol.  Section 2.3.24 spells it out --
    ``$PMTK352,0`` *enables* QZSS and ``$PMTK352,1`` *disables* it.  The
    argument here is the user-facing meaning, and the inversion is applied once,
    here, so no caller has to remember it.
    """
    return f"PMTK352,{0 if enabled else 1}"


def set_aic(enabled: bool) -> str:
    """PMTK286 -- active interference cancellation."""
    return f"PMTK286,{1 if enabled else 0}"


STATIC_NAV_THRESHOLD_RANGE = (0.0, 2.0)


def set_static_nav_threshold(speed_mps: float) -> str:
    """PMTK386 -- static navigation speed threshold in m/s.

    Below this speed the receiver freezes the reported position and reports zero
    speed.  0 disables it; the usable range is 0.1..2.0 m/s.
    """
    speed = float(speed_mps)
    low, high = STATIC_NAV_THRESHOLD_RANGE
    if not (low <= speed <= high):
        raise ValueError(f"static navigation threshold must be {low}..{high} m/s, got {speed}")
    return f"PMTK386,{format_number(speed)}"


# --------------------------------------------------------------------------
# Datum (sections 2.3.20 - 2.3.21)
# --------------------------------------------------------------------------

USER_DATUM_MAJOR_AXIS_RANGE = (0.0, 7000000.0)
USER_DATUM_ECCENTRICITY_RANGE = (0.0, 330.0)


def set_datum(index: int) -> str:
    """PMTK330 -- select one of the datums in :mod:`v800.datums`."""
    return f"PMTK330,{int(index)}"


def query_datum() -> str:
    """PMTK430 -- query the datum; answered by PMTK530."""
    return "PMTK430"


def set_datum_advance(maj_a: float, ecc: float, dx: float, dy: float, dz: float) -> str:
    """PMTK331 -- define the user datum selected by index 3.

    ``maj_a`` is the semi-major axis in metres and ``ecc`` the eccentricity term;
    ``dx``/``dy``/``dz`` are the offsets to WGS84 in metres.
    """
    low, high = USER_DATUM_MAJOR_AXIS_RANGE
    if not (low <= float(maj_a) <= high):
        raise ValueError(f"semi-major axis must be {low}..{high} m, got {maj_a}")
    low, high = USER_DATUM_ECCENTRICITY_RANGE
    if not (low <= float(ecc) <= high):
        raise ValueError(f"eccentricity must be {low}..{high}, got {ecc}")
    return (
        f"PMTK331,{format_number(float(maj_a))},{format_number(float(ecc))},"
        f"{format_number(float(dx))},{format_number(float(dy))},{format_number(float(dz))}"
    )


def query_datum_advance() -> str:
    """PMTK431 -- query the user datum.  Firmware-dependent (section 2.3.32)."""
    return "PMTK431"


def parse_datum(sentence: Sentence) -> int | None:
    """Decode the datum index from a PMTK530 reply."""
    if sentence.packet_type != Packet.DT_DATUM or not sentence.fields:
        return None
    try:
        return int(sentence.fields[0])
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Aiding: time, position, EPO (sections 2.3.22, 2.3.40 - 2.3.45)
# --------------------------------------------------------------------------


def set_rtc_time(year: int, month: int, day: int, hour: int, minute: int, second: int) -> str:
    """PMTK335 -- set the RTC UTC time.

    This does *not* set GPS time; the receiver overwrites it with a better figure
    within about 60 seconds of getting a fix (section 2.3.22).
    """
    _check_utc(year, month, day, hour, minute, second)
    return f"PMTK335,{year},{month},{day},{hour},{minute},{second}"


def set_utc_aiding(year: int, month: int, day: int, hour: int, minute: int, second: int) -> str:
    """PMTK740 -- supply reference UTC for a faster TTFF.

    Must be UTC, not local time, and should be accurate to better than 3 seconds
    to be useful (section 2.3.44).
    """
    _check_utc(year, month, day, hour, minute, second)
    return f"PMTK740,{year},{month},{day},{hour},{minute},{second}"


def set_position_aiding(
    lat: float,
    lon: float,
    alt: float,
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    second: int,
) -> str:
    """PMTK741 -- supply a reference position for a faster TTFF.

    The position should be within 30 km to help (section 2.3.45).  The chipset
    range-checks latitude and longitude itself; we check here too so a typo is
    caught before it reaches the port.
    """
    if not (-90.0 <= float(lat) <= 90.0):
        raise ValueError(f"latitude must be -90..90, got {lat}")
    if not (-180.0 <= float(lon) <= 180.0):
        raise ValueError(f"longitude must be -180..180, got {lon}")
    _check_utc(year, month, day, hour, minute, second)
    return (
        f"PMTK741,{lat:.6f},{lon:.6f},{format_number(float(alt))},"
        f"{year},{month},{day},{hour:02d},{minute:02d},{second:02d}"
    )


def _check_utc(year: int, month: int, day: int, hour: int, minute: int, second: int) -> None:
    for value, (low, high), name in (
        (year, (1981, 9999), "year"),
        (month, (1, 12), "month"),
        (day, (1, 31), "day"),
        (hour, (0, 23), "hour"),
        (minute, (0, 59), "minute"),
        (second, (0, 59), "second"),
    ):
        if not (low <= int(value) <= high):
            raise ValueError(f"{name} must be {low}..{high}, got {value}")


def query_epo_info() -> str:
    """PMTK607 -- EPO data valid-day check."""
    return "PMTK607"


EPH_INTERVAL_MAX_S = 7200
ALM_INTERVAL_MAX_DAYS = 365


def query_available_sv_eph(interval_s: int) -> str:
    """PMTK660 -- which ephemerides will still be valid after ``interval_s`` seconds."""
    interval_s = int(interval_s)
    if not (0 < interval_s <= EPH_INTERVAL_MAX_S):
        raise ValueError(f"ephemeris interval must be 1..{EPH_INTERVAL_MAX_S} s, got {interval_s}")
    return f"PMTK660,{interval_s}"


def query_available_sv_alm(interval_days: int) -> str:
    """PMTK661 -- which almanacs will still be valid after ``interval_days`` days."""
    interval_days = int(interval_days)
    if not (0 < interval_days <= ALM_INTERVAL_MAX_DAYS):
        raise ValueError(
            f"almanac interval must be 1..{ALM_INTERVAL_MAX_DAYS} days, got {interval_days}"
        )
    return f"PMTK661,{interval_days}"


def parse_available_sv(sentence: Sentence) -> tuple[int, list[int]] | None:
    """Decode the PMTK660/661 reply, which arrives dressed as a PMTK001.

    The reply is ``$PMTK001,<660|661>,3,<hex>`` where ``<hex>`` is a 32-bit mask,
    bit *n* meaning SV *n+1*.  Returns ``(query packet type, sorted SV numbers)``.

    Worked example from section 2.3.41: ``40449464`` is
    ``0100 0000 0100 0100 1001 0100 0110 0100`` and the spec reads off SVs
    3, 6, 7, 11, 13, 16, 19, 23, 31 -- so the mask is little-endian by bit
    position, i.e. bit 0 of the *whole 32-bit value* is SV 1.
    """
    if sentence.packet_type != Packet.ACK or len(sentence.fields) < 3:
        return None
    try:
        query = int(sentence.fields[0])
    except ValueError:
        return None
    if query not in (int(Packet.Q_AVAILABLE_SV_EPH), int(Packet.Q_AVAILABLE_SV_ALM)):
        return None
    try:
        mask = int(sentence.fields[2], 16)
    except ValueError:
        return None
    return query, [bit + 1 for bit in range(32) if mask & (1 << bit)]


@dataclass(frozen=True)
class Release:
    """Decoded PMTK705 firmware release information (table 2-53)."""

    release: str
    build_id: str
    product_model: str
    sdk_version: str = ""

    def __str__(self) -> str:
        parts = [self.release, self.build_id, self.product_model]
        if self.sdk_version:
            parts.append(self.sdk_version)
        return " / ".join(p for p in parts if p)


def query_release() -> str:
    """PMTK605 -- query firmware release; answered by PMTK705."""
    return "PMTK605"


def parse_release(sentence: Sentence) -> Release | None:
    """Decode a PMTK705 reply."""
    if sentence.packet_type != Packet.DT_RELEASE or not sentence.fields:
        return None
    fields = list(sentence.fields) + [""] * 4
    return Release(fields[0], fields[1], fields[2], fields[3])


def parse_tcxo_debug(sentence: Sentence) -> tuple[bool, str, float] | None:
    """Decode PMTK589 -- ``(valid, utc, drift_ppm)`` (table 2-48)."""
    if sentence.packet_type != Packet.DT_SET_TCXO_DEBUG or len(sentence.fields) < 3:
        return None
    try:
        return sentence.fields[0] == "1", sentence.fields[1], float(sentence.fields[2])
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Manufacturing / RF test mode (sections 2.3.46 - 2.3.52)
# --------------------------------------------------------------------------


class TestItem(IntEnum):
    """Bits of the PMTK810 test-item bitmap (table 2-56)."""

    INFO = 1 << 0
    ACQ = 1 << 1
    BITSYNC = 1 << 2
    SIGNAL = 1 << 3


TEST_ITEM_TEXT = {
    TestItem.INFO: "Firmware version, NMEA type and output rate",
    TestItem.ACQ: "Time to acquire the specified SV",
    TestItem.BITSYNC: "Time to bit sync",
    TestItem.SIGNAL: "Phase error, TCXO clock/drift, CNR mean/sigma",
}

TEST_SVID_RANGE = (1, 20)


def test_all(items: int, svid: int) -> str:
    """PMTK810 -- enter MP test mode for the given items and SV.

    Both fields are hexadecimal in the spec's example (``$PMTK810,0003,1D``:
    bitmap 0x0003 = INFO|ACQ, SV 0x1D = PRN 29).  ``svid`` here is the decimal
    PRN and is range-checked to the documented 1..20 window before being
    formatted as hex.
    """
    items = int(items)
    if not (0 < items <= 0x0F):
        raise ValueError(f"test item bitmap must select at least one of bits 0-3, got {items:#x}")
    low, high = TEST_SVID_RANGE
    if not (low <= int(svid) <= high):
        raise ValueError(f"test SV id must be {low}..{high}, got {svid}")
    return f"PMTK810,{items:04X},{int(svid):02X}"


def test_stop() -> str:
    """PMTK811 -- leave MP test mode."""
    return "PMTK811"


def test_jamming(enabled: bool, scans: int = 50) -> str:
    """PMTK837 -- run a jamming scan ``scans`` times."""
    scans = int(scans)
    if scans < 1:
        raise ValueError(f"jamming scan count must be >= 1, got {scans}")
    return f"PMTK837,{1 if enabled else 0},{scans}"


@dataclass(frozen=True)
class AcqResult:
    """PMTK813 -- time to acquire an SV."""

    svid: int
    seconds: float


@dataclass(frozen=True)
class BitsyncResult:
    """PMTK814 -- time to reach bit sync."""

    svid: int
    seconds: float


@dataclass(frozen=True)
class SignalResult:
    """PMTK815 -- signal quality for one SV (table 2-61).

    The wire format carries fixed-point integers; the scale factors in the
    spec's "Unit" column (0.01 for phase/TCXO, 0.001 for CNR) are applied here,
    so these attributes are in engineering units.  The spec's own worked example
    ``$PMTK815,29,16,98,10000,30,4100,0`` reads back as phase error 0.98,
    TCXO offset 100.0 Hz / drift 0.3, CNR mean 4.1 -- note the spec's prose
    rounds those to "10/0.03" and "41/0", so treat the absolute values as
    indicative and use them for comparison between runs, not as calibrated
    figures.
    """

    svid: int
    test_seconds: float
    phase_error: float
    tcxo_offset: float
    tcxo_drift: float
    cnr_mean: float
    cnr_sigma: float


def parse_test_result(
    sentence: Sentence,
) -> AcqResult | BitsyncResult | SignalResult | None:
    """Decode PMTK813 / PMTK814 / PMTK815 into the matching result record."""
    fields = sentence.fields
    try:
        if sentence.packet_type == Packet.TEST_ALL_ACQ and len(fields) >= 2:
            return AcqResult(int(fields[0]), float(fields[1]))
        if sentence.packet_type == Packet.TEST_ALL_BITSYNC and len(fields) >= 2:
            return BitsyncResult(int(fields[0]), float(fields[1]))
        if sentence.packet_type == Packet.TEST_ALL_SIGNAL and len(fields) >= 7:
            return SignalResult(
                svid=int(fields[0]),
                test_seconds=float(fields[1]),
                phase_error=int(fields[2]) * 0.01,
                tcxo_offset=int(fields[3]) * 0.01,
                tcxo_drift=int(fields[4]) * 0.01,
                cnr_mean=int(fields[5]) * 0.001,
                cnr_sigma=int(fields[6]) * 0.001,
            )
    except ValueError:
        return None
    return None


# --------------------------------------------------------------------------
# Link budget
# --------------------------------------------------------------------------

#: Rough bytes-per-sentence at a typical satellite count, measured from captured
#: MT3333 output.  Used only to warn that a requested rate cannot fit the port,
#: so approximate figures are fine -- but they are approximate, and the warning
#: is phrased as an estimate in the UI for that reason.
TYPICAL_SENTENCE_BYTES = {
    "GLL": 50,
    "RMC": 80,
    "VTG": 45,
    "GGA": 82,
    "GSA": 68,
    "GSV": 210,  # several sentences per fix once GPS+GLONASS are both tracked
    "ZDA": 38,
}


def nmea_budget_bps(rates: dict[str, int], fix_interval_ms: int) -> float:
    """Estimate the bits per second the enabled sentence set will need.

    Assumes 10 bits on the wire per character (8N1 plus start and stop).  A
    divisor of N means the sentence appears once every N fixes.
    """
    if fix_interval_ms <= 0:
        return 0.0
    fixes_per_second = 1000.0 / fix_interval_ms
    total_bytes = 0.0
    for name, divisor in rates.items():
        if divisor <= 0:
            continue
        total_bytes += TYPICAL_SENTENCE_BYTES.get(name.upper(), 0) / divisor
    return total_bytes * fixes_per_second * 10.0


# --------------------------------------------------------------------------
# Command catalogue for the raw console's completer / reference pane
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandInfo:
    """One row of the built-in PMTK reference."""

    packet: int
    name: str
    summary: str
    example: str
    section: str
    fields: tuple[str, ...] = field(default_factory=tuple)


COMMAND_CATALOGUE: tuple[CommandInfo, ...] = (
    CommandInfo(0, "PMTK_TEST", "Test packet", "$PMTK000", "2.3.1"),
    CommandInfo(1, "PMTK_ACK", "Acknowledgement of a PMTK command", "$PMTK001,604,3", "2.3.2",
                ("Cmd", "Flag")),
    CommandInfo(10, "PMTK_SYS_MSG", "System message output by the receiver", "$PMTK010,001",
                "2.3.3", ("Msg",)),
    CommandInfo(11, "PMTK_TXT_MSG", "Text message output by the receiver", "$PMTK011,MTKGPS",
                "2.3.4", ("txt",)),
    CommandInfo(101, "PMTK_CMD_HOT_START", "Restart using all NV store data", "$PMTK101", "2.3.5"),
    CommandInfo(102, "PMTK_CMD_WARM_START", "Restart without ephemeris", "$PMTK102", "2.3.6"),
    CommandInfo(103, "PMTK_CMD_COLD_START", "Restart without time/position/almanac/ephemeris",
                "$PMTK103", "2.3.7"),
    CommandInfo(104, "PMTK_CMD_FULL_COLD_START",
                "Cold start and reset configuration to factory state", "$PMTK104", "2.3.8"),
    CommandInfo(120, "PMTK_CMD_CLEAR_FLASH_AID", "Erase aiding data held in flash", "$PMTK120",
                "2.3.10"),
    CommandInfo(161, "PMTK_CMD_STANDBY_MODE", "Enter standby (0 = stop, 1 = sleep)", "$PMTK161,0",
                "2.3.9", ("Type",)),
    CommandInfo(220, "PMTK_SET_POS_FIX", "Position fix interval, ms (> 100)", "$PMTK220,1000",
                "2.3.11", ("Interval",)),
    CommandInfo(223, "PMTK_SET_AL_DEE_CFG", "AlwaysLocate / DEE tuning",
                "$PMTK223,1,25,180000,60000", "2.3.12",
                ("SV", "SNR", "Extension threshold", "Extension gap")),
    CommandInfo(225, "PMTK_SET_PERIODIC_MODE", "Periodic power-saving mode",
                "$PMTK225,2,3000,12000,18000,72000", "2.3.13",
                ("Type", "Run time", "Sleep time", "Second run time", "Second sleep time")),
    CommandInfo(251, "PMTK_SET_NMEA_BAUDRATE", "NMEA port baud rate", "$PMTK251,38400", "2.3.14",
                ("Baudrate",)),
    CommandInfo(286, "PMTK_SET_AIC_CMD", "Active interference cancellation", "$PMTK286,1",
                "2.3.15", ("Enabled",)),
    CommandInfo(300, "PMTK_API_SET_FIX_CTL", "Fix interval, ms (100..10000)",
                "$PMTK300,1000,0,0,0,0", "2.3.16", ("Fixinterval",)),
    CommandInfo(301, "PMTK_API_SET_DGPS_MODE", "DGPS source (0 none, 1 RTCM, 2 WAAS)",
                "$PMTK301,2", "2.3.17", ("Mode",)),
    CommandInfo(313, "PMTK_API_SET_SBAS_ENABLED", "Search SBAS satellites", "$PMTK313,1", "2.3.18",
                ("Enabled",)),
    CommandInfo(314, "PMTK_API_SET_NMEA_OUTPUT", "Per-sentence output divisors (19 fields)",
                "$PMTK314,1,1,1,1,1,5,0,0,0,0,0,0,0,0,0,0,0,0,0", "2.3.19"),
    CommandInfo(330, "PMTK_API_SET_DATUM", "Select datum by index", "$PMTK330,0", "2.3.20",
                ("Datum",)),
    CommandInfo(331, "PMTK_API_SET_DATUM_ADVANCE", "Define the user datum",
                "$PMTK331,6377397.155,299.1528128,-148.0,507.0,685.0", "2.3.21",
                ("majA", "ecc", "dX", "dY", "dZ")),
    CommandInfo(335, "PMTK_API_SET_RTC_TIME", "Set RTC UTC time", "$PMTK335,2007,1,1,0,0,0",
                "2.3.22", ("Year", "Month", "Day", "Hour", "Min", "Sec")),
    CommandInfo(351, "PMTK_API_SET_SUPPORT_QZSS_NMEA", "Use the QZSS NMEA format", "$PMTK351,1",
                "2.3.23", ("Enabled",)),
    CommandInfo(352, "PMTK_API_SET_STOP_QZSS",
                "QZSS ranging -- NOTE 0 enables, 1 disables", "$PMTK352,0", "2.3.24",
                ("Enabled",)),
    CommandInfo(353, "PMTK_API_SET_GNSS_SEARCH_MODE", "Constellations to search",
                "$PMTK353,1,1", "2.3.25", ("GPS_Enabled", "GLONASS_Enabled")),
    CommandInfo(386, "PMTK_API_SET_STATIC_NAV_THD", "Static navigation speed threshold, m/s",
                "$PMTK386,0.4", "2.3.26", ("speed_threshold",)),
    CommandInfo(400, "PMTK_API_Q_FIX_CTL", "Query fix control (-> PMTK500)", "$PMTK400", "2.3.27"),
    CommandInfo(401, "PMTK_API_Q_DGPS_MODE", "Query DGPS mode (-> PMTK501)", "$PMTK401", "2.3.28"),
    CommandInfo(413, "PMTK_API_Q_SBAS_ENABLED", "Query SBAS (-> PMTK513)", "$PMTK413", "2.3.29"),
    CommandInfo(414, "PMTK_API_Q_NMEA_OUTPUT", "Query NMEA output (-> PMTK514)", "$PMTK414",
                "2.3.30"),
    CommandInfo(430, "PMTK_API_Q_DATUM", "Query datum (-> PMTK530)", "$PMTK430", "2.3.31"),
    CommandInfo(431, "PMTK_API_Q_DATUM_ADVANCE", "Query user datum (firmware-dependent)",
                "$PMTK431", "2.3.32"),
    CommandInfo(500, "PMTK_DT_FIX_CTL", "Reply: fix interval", "$PMTK500,1000,0,0,0,0", "2.3.33"),
    CommandInfo(501, "PMTK_DT_DGPS_MODE", "Reply: DGPS mode", "$PMTK501,1", "2.3.34"),
    CommandInfo(513, "PMTK_DT_SBAS_ENABLED", "Reply: SBAS enabled", "$PMTK513,1", "2.3.35"),
    CommandInfo(514, "PMTK_DT_NMEA_OUTPUT", "Reply: NMEA output divisors", "$PMTK514,...",
                "2.3.36"),
    CommandInfo(530, "PMTK_DT_DATUM", "Reply: datum in use", "$PMTK530,0", "2.3.37"),
    CommandInfo(589, "PMTK_DT_SET_TCXO_DEBUG", "Reply: TCXO clock drift",
                "$PMTK589,1,052130.000,-0.4712", "2.3.38", ("valid", "UTC", "TCXO_drift_ppm")),
    CommandInfo(605, "PMTK_Q_RELEASE", "Query firmware release (-> PMTK705)", "$PMTK605",
                "2.3.39"),
    CommandInfo(607, "PMTK_Q_EPO_INFO", "EPO data valid-day check", "$PMTK607", "2.3.40"),
    CommandInfo(660, "PMTK_Q_AVAILABLE_SV_EPH", "Which ephemerides survive N seconds",
                "$PMTK660,1800", "2.3.41", ("Time interval",)),
    CommandInfo(661, "PMTK_Q_AVAILABLE_SV_ALM", "Which almanacs survive N days", "$PMTK661,30",
                "2.3.42", ("Time interval",)),
    CommandInfo(705, "PMTK_DT_RELEASE", "Reply: firmware release", "$PMTK705,AXN_0.2,1234,ABCD,",
                "2.3.43", ("ReleaseStr", "Build_ID", "Product_Model", "SDK_Version")),
    CommandInfo(740, "PMTK_DT_UTC", "Reference UTC time aiding", "$PMTK740,2010,2,10,9,0,58",
                "2.3.44", ("YYYY", "MM", "DD", "hh", "mm", "ss")),
    CommandInfo(741, "PMTK_DT_POS", "Reference position aiding",
                "$PMTK741,24.772816,121.022636,160,2011,8,1,08,00,00", "2.3.45",
                ("Lat", "Long", "Alt", "YYYY", "MM", "DD", "hh", "mm", "ss")),
    CommandInfo(810, "PMTK_TEST_ALL", "Enter MP test mode (hex bitmap, hex SV id)",
                "$PMTK810,0003,1D", "2.3.46", ("Bitmap", "SVID")),
    CommandInfo(811, "PMTK_TEST_STOP", "Leave MP test mode", "$PMTK811", "2.3.47"),
    CommandInfo(812, "PMTK_TEST_FINISH", "Reply: MP testing finished", "$PMTK812", "2.3.48"),
    CommandInfo(813, "PMTK_TEST_ALL_ACQ", "Reply: acquisition time", "$PMTK813,29,2", "2.3.49",
                ("SVid", "Acq Time")),
    CommandInfo(814, "PMTK_TEST_ALL_BITSYNC", "Reply: bit sync time", "$PMTK814,29,1", "2.3.50",
                ("SVid", "BitSync Time")),
    CommandInfo(815, "PMTK_TEST_ALL_SIGNAL", "Reply: phase error, TCXO, CNR",
                "$PMTK815,29,16,98,10000,30,4100,0", "2.3.51",
                ("SVid", "Testing Time", "Phase", "TCXO Offset", "TCXO Drift", "CNR mean",
                 "CNR sigma")),
    CommandInfo(837, "PMTK_TEST_JAMMING", "Jamming scan", "$PMTK837,1,50", "2.3.52",
                ("JamScanType", "JamScanNum")),
)

COMMANDS_BY_PACKET = {info.packet: info for info in COMMAND_CATALOGUE}


def describe(packet_type: int) -> str:
    """One-line description of a packet type, for the console and log."""
    info = COMMANDS_BY_PACKET.get(packet_type)
    if info is None:
        return f"PMTK{packet_type:03d} (not in the MT3333 specification)"
    return f"PMTK{packet_type:03d} {info.name} -- {info.summary}"
