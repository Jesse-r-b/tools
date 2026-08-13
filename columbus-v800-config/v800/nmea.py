"""Standard NMEA-0183 decoding for the sentences the MT3333 emits.

Covers the seven sentences PMTK314 can switch on: GGA, GLL, GSA, GSV, RMC, VTG
and ZDA (section 2.2 of the MT3333 specification).

Multi-constellation handling is the fiddly part.  With GPS+GLONASS+BeiDou all
enabled the receiver emits a *separate* GSV group per constellation, each with
its own talker ID, and one or more GSA sentences that between them list the
satellites used in the solution.  A GSV group arrives as N sentences that must
be reassembled before the sky view is redrawn -- redrawing on each sentence
makes satellites flicker in and out, which looks like a tracking fault and
isn't.  :class:`GsvAssembler` handles that per talker.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import IntEnum

from .pmtk import Sentence

# --------------------------------------------------------------------------
# Constellations
# --------------------------------------------------------------------------


class Constellation(IntEnum):
    """Which system a satellite belongs to."""

    GPS = 0
    GLONASS = 1
    GALILEO = 2
    BEIDOU = 3
    QZSS = 4
    SBAS = 5
    UNKNOWN = 6


CONSTELLATION_NAMES = {
    Constellation.GPS: "GPS",
    Constellation.GLONASS: "GLONASS",
    Constellation.GALILEO: "Galileo",
    Constellation.BEIDOU: "BeiDou",
    Constellation.QZSS: "QZSS",
    Constellation.SBAS: "SBAS",
    Constellation.UNKNOWN: "Unknown",
}

#: Talker ID -> constellation.  ``GN`` means "combined solution" and carries no
#: single constellation, so it is resolved per satellite by PRN instead.
TALKER_CONSTELLATION = {
    "GP": Constellation.GPS,
    "GL": Constellation.GLONASS,
    "GA": Constellation.GALILEO,
    "GB": Constellation.BEIDOU,
    "BD": Constellation.BEIDOU,  # older MTK firmware uses BD rather than GB
    "GQ": Constellation.QZSS,
    "QZ": Constellation.QZSS,
}


#: GSA/GSV system ID field, NMEA 4.10 onward.  The V-800 MarkIII emits it as the
#: last field of every GSA, which is the only unambiguous way to tell which
#: constellation a combined ``GN`` solution drew each satellite from.
SYSTEM_ID_CONSTELLATION = {
    1: Constellation.GPS,
    2: Constellation.GLONASS,
    3: Constellation.GALILEO,
    4: Constellation.BEIDOU,
    5: Constellation.QZSS,
    6: Constellation.GALILEO,
}


def constellation_for(talker: str, prn: int) -> Constellation:
    """Work out which system a satellite belongs to.

    The talker ID is the primary signal, but it is not always sufficient: a
    V-800 MarkIII reports QZSS satellites (PRN 193-202) inside ``GPGSV``
    sentences, because QZSS is GPS-interoperable and shares the talker.  Taking
    the talker at face value there labels three visible QZSS satellites as GPS,
    so a PRN that cannot belong to the talker's constellation is resolved by
    the NMEA PRN allocation instead.

    For ``GN`` (combined) and unrecognised talkers the PRN allocation is all
    there is.
    """
    known = TALKER_CONSTELLATION.get(talker.upper())
    if known is Constellation.GPS and not (1 <= prn <= 32):
        # GPS occupies PRN 1-32 only; anything else under a GP talker is really
        # SBAS or QZSS being reported on the GPS talker.
        known = None
    if known is not None:
        return known
    if 1 <= prn <= 32:
        return Constellation.GPS
    if 33 <= prn <= 64:
        return Constellation.SBAS
    if 65 <= prn <= 96:
        return Constellation.GLONASS
    if 193 <= prn <= 202:
        return Constellation.QZSS
    if 201 <= prn <= 237:
        return Constellation.BEIDOU
    if 301 <= prn <= 336:
        return Constellation.GALILEO
    return Constellation.UNKNOWN


# --------------------------------------------------------------------------
# Field helpers
# --------------------------------------------------------------------------


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _at(fields: tuple[str, ...], index: int) -> str:
    return fields[index] if index < len(fields) else ""


def parse_latlon(value: str, hemisphere: str) -> float | None:
    """Convert NMEA ddmm.mmmm / dddmm.mmmm plus a hemisphere letter to degrees.

    The degree field is variable width (2 for latitude, 3 for longitude), so the
    split is done from the decimal point rather than by a fixed offset -- some
    firmware emits more than four fractional digits at high update rates.
    """
    if not value or not hemisphere:
        return None
    dot = value.find(".")
    if dot < 0:
        dot = len(value)
    if dot < 3:
        return None
    degrees = _float(value[: dot - 2])
    minutes = _float(value[dot - 2 :])
    if degrees is None or minutes is None:
        return None
    result = degrees + minutes / 60.0
    if hemisphere.upper() in ("S", "W"):
        result = -result
    return result


def parse_nmea_time(value: str) -> dt.time | None:
    """Parse an ``hhmmss.sss`` time field."""
    if not value or len(value) < 6:
        return None
    try:
        hour, minute = int(value[0:2]), int(value[2:4])
        seconds = float(value[4:])
    except ValueError:
        return None
    whole = int(seconds)
    micro = int(round((seconds - whole) * 1_000_000))
    # A leap second reports :60, which datetime.time cannot represent.  Clamp so
    # the display keeps working rather than dropping the whole sentence.
    if whole > 59:
        whole, micro = 59, 999_999
    try:
        return dt.time(hour, minute, whole, micro)
    except ValueError:
        return None


def parse_nmea_date(value: str) -> dt.date | None:
    """Parse a ``ddmmyy`` date field, pivoting the two-digit year on 1980."""
    if not value or len(value) != 6:
        return None
    try:
        day, month, year = int(value[0:2]), int(value[2:4]), int(value[4:6])
    except ValueError:
        return None
    year += 2000 if year < 80 else 1900
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Decoded records
# --------------------------------------------------------------------------


class FixQuality(IntEnum):
    """GGA position fix indicator (table 2-4)."""

    INVALID = 0
    GPS_SPS = 1
    DGPS = 2
    PPS = 3
    RTK_FIXED = 4
    RTK_FLOAT = 5
    ESTIMATED = 6
    MANUAL = 7
    SIMULATION = 8


FIX_QUALITY_TEXT = {
    FixQuality.INVALID: "No fix",
    FixQuality.GPS_SPS: "GNSS fix (SPS)",
    FixQuality.DGPS: "Differential fix",
    FixQuality.PPS: "PPS fix",
    FixQuality.RTK_FIXED: "RTK fixed",
    FixQuality.RTK_FLOAT: "RTK float",
    FixQuality.ESTIMATED: "Dead reckoning",
    FixQuality.MANUAL: "Manual input",
    FixQuality.SIMULATION: "Simulation",
}


class FixType(IntEnum):
    """GSA mode 2 (table 2-8)."""

    NO_FIX = 1
    FIX_2D = 2
    FIX_3D = 3


FIX_TYPE_TEXT = {
    FixType.NO_FIX: "No fix",
    FixType.FIX_2D: "2D fix",
    FixType.FIX_3D: "3D fix",
}


@dataclass
class Satellite:
    """One satellite from a GSV sentence."""

    prn: int
    constellation: Constellation
    elevation: int | None = None
    azimuth: int | None = None
    snr: int | None = None
    """C/N0 in dB-Hz; ``None`` when the field is blank, meaning "not tracked"."""
    used: bool = False
    """True when this PRN appears in a GSA sentence's list of satellites in use."""

    @property
    def tracked(self) -> bool:
        return self.snr is not None and self.snr > 0


@dataclass
class Fix:
    """Everything the sentence set tells us about the current solution.

    One instance is kept and updated in place as sentences arrive, because the
    fields come from different sentences at different rates.
    """

    utc_time: dt.time | None = None
    utc_date: dt.date | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude_m: float | None = None
    geoid_separation_m: float | None = None
    quality: FixQuality = FixQuality.INVALID
    fix_type: FixType = FixType.NO_FIX
    satellites_used: int = 0
    hdop: float | None = None
    vdop: float | None = None
    pdop: float | None = None
    speed_knots: float | None = None
    speed_kph: float | None = None
    course_true: float | None = None
    course_magnetic: float | None = None
    magnetic_variation: float | None = None
    dgps_age_s: float | None = None
    dgps_station: str = ""
    local_zone_hours: int | None = None
    local_zone_minutes: int | None = None
    used_prns: set[int] = field(default_factory=set)
    used_keys: set[tuple[Constellation, int]] = field(default_factory=set)
    """(constellation, PRN) pairs, populated when the GSA carries a system ID."""
    used_systems: set[Constellation] = field(default_factory=set)
    """Constellations that contributed to the current solution."""

    @property
    def has_fix(self) -> bool:
        return self.quality is not FixQuality.INVALID and self.latitude is not None

    @property
    def datetime_utc(self) -> dt.datetime | None:
        if self.utc_date is None or self.utc_time is None:
            return None
        return dt.datetime.combine(self.utc_date, self.utc_time, tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------
# Per-sentence decoding
# --------------------------------------------------------------------------


def apply_gga(fix: Fix, s: Sentence) -> None:
    """GGA -- time, position, fix quality, satellites used, HDOP, altitude."""
    f = s.fields
    fix.utc_time = parse_nmea_time(_at(f, 0)) or fix.utc_time
    lat = parse_latlon(_at(f, 1), _at(f, 2))
    lon = parse_latlon(_at(f, 3), _at(f, 4))
    quality = _int(_at(f, 5))
    if quality is not None:
        try:
            fix.quality = FixQuality(quality)
        except ValueError:
            fix.quality = FixQuality.INVALID
    # Only overwrite position when the sentence actually carries one; a GGA with
    # empty position fields during reacquisition should not blank the last fix.
    if lat is not None and lon is not None:
        fix.latitude, fix.longitude = lat, lon
    used = _int(_at(f, 6))
    if used is not None:
        fix.satellites_used = used
    hdop = _float(_at(f, 7))
    if hdop is not None:
        fix.hdop = hdop
    alt = _float(_at(f, 8))
    if alt is not None:
        fix.altitude_m = alt
    sep = _float(_at(f, 10))
    if sep is not None:
        fix.geoid_separation_m = sep
    fix.dgps_age_s = _float(_at(f, 12))
    fix.dgps_station = _at(f, 13)


def apply_gll(fix: Fix, s: Sentence) -> None:
    """GLL -- position, time and a validity flag."""
    f = s.fields
    lat = parse_latlon(_at(f, 0), _at(f, 1))
    lon = parse_latlon(_at(f, 2), _at(f, 3))
    if lat is not None and lon is not None:
        fix.latitude, fix.longitude = lat, lon
    fix.utc_time = parse_nmea_time(_at(f, 4)) or fix.utc_time


def apply_gsa(fix: Fix, s: Sentence, reset_used: bool) -> None:
    """GSA -- fix type, the PRNs used in the solution, and the DOP triple.

    With several constellations enabled the receiver sends one GSA per system,
    so the caller signals with ``reset_used`` when a new group starts; otherwise
    each sentence would wipe the previous constellation's contribution.
    """
    f = s.fields
    if reset_used:
        fix.used_prns = set()
        fix.used_keys = set()
        fix.used_systems = set()
    mode2 = _int(_at(f, 1))
    if mode2 is not None:
        try:
            fix.fix_type = FixType(mode2)
        except ValueError:
            fix.fix_type = FixType.NO_FIX
    # NMEA 4.10 appends a system ID identifying which constellation this GSA
    # describes.  With it, a satellite used in the fix can be matched on
    # (constellation, PRN) rather than PRN alone -- GPS 5 and BeiDou 5 are
    # different satellites, and the V-800 MarkIII tracks both.
    system = SYSTEM_ID_CONSTELLATION.get(_int(_at(f, 17)) or 0)
    if system is not None:
        fix.used_systems.add(system)

    for index in range(2, 14):
        prn = _int(_at(f, index))
        if prn:
            fix.used_prns.add(prn)
            if system is not None:
                fix.used_keys.add((system, prn))
    pdop, hdop, vdop = (_float(_at(f, i)) for i in (14, 15, 16))
    if pdop is not None:
        fix.pdop = pdop
    if hdop is not None:
        fix.hdop = hdop
    if vdop is not None:
        fix.vdop = vdop


def apply_rmc(fix: Fix, s: Sentence) -> None:
    """RMC -- time, date, position, speed over ground and course."""
    f = s.fields
    fix.utc_time = parse_nmea_time(_at(f, 0)) or fix.utc_time
    lat = parse_latlon(_at(f, 2), _at(f, 3))
    lon = parse_latlon(_at(f, 4), _at(f, 5))
    if lat is not None and lon is not None:
        fix.latitude, fix.longitude = lat, lon
    speed = _float(_at(f, 6))
    if speed is not None:
        fix.speed_knots = speed
    course = _float(_at(f, 7))
    if course is not None:
        fix.course_true = course
    fix.utc_date = parse_nmea_date(_at(f, 8)) or fix.utc_date
    variation = _float(_at(f, 9))
    if variation is not None:
        if _at(f, 10).upper() == "W":
            variation = -variation
        fix.magnetic_variation = variation


def apply_vtg(fix: Fix, s: Sentence) -> None:
    """VTG -- course over ground and ground speed in knots and km/h."""
    f = s.fields
    course = _float(_at(f, 0))
    if course is not None:
        fix.course_true = course
    magnetic = _float(_at(f, 2))
    if magnetic is not None:
        fix.course_magnetic = magnetic
    knots = _float(_at(f, 4))
    if knots is not None:
        fix.speed_knots = knots
    kph = _float(_at(f, 6))
    if kph is not None:
        fix.speed_kph = kph


#: GPTXT text-identifier field (NMEA-0183). The V-800 MarkIII uses this to
#: report antenna status unprompted -- "ANTENNA OK", "ANTENNA OPEN" (nothing
#: connected or a broken feed) or "ANTENNA SHORT" (a short on the bias line).
#: It is the only antenna diagnostic the receiver offers, and it arrives without
#: being asked for, so it is worth surfacing prominently.
TXT_SEVERITY = {
    0: "error",
    1: "warning",
    2: "notice",
    7: "user",
}


def apply_txt(state: "NavState", s: Sentence) -> None:
    """GPTXT -- free-text status messages, including antenna state."""
    f = s.fields
    if len(f) < 4:
        return
    severity = TXT_SEVERITY.get(_int(_at(f, 2)) or -1, "unknown")
    # The text itself may contain commas, so rejoin everything after field 2.
    text = ",".join(f[3:]).strip()
    state.last_text = (severity, text)
    upper = text.upper()
    if "ANTENNA" in upper:
        state.antenna_status = text


def apply_zda(fix: Fix, s: Sentence) -> None:
    """ZDA -- UTC time and date with an explicit four-digit year."""
    f = s.fields
    fix.utc_time = parse_nmea_time(_at(f, 0)) or fix.utc_time
    day, month, year = _int(_at(f, 1)), _int(_at(f, 2)), _int(_at(f, 3))
    if day and month and year:
        try:
            fix.utc_date = dt.date(year, month, day)
        except ValueError:
            pass
    fix.local_zone_hours = _int(_at(f, 4))
    fix.local_zone_minutes = _int(_at(f, 5))


# --------------------------------------------------------------------------
# GSV reassembly
# --------------------------------------------------------------------------


class GsvAssembler:
    """Reassembles multi-sentence GSV groups, one group per talker ID.

    A group is complete when the sentence numbered ``total`` arrives.  Out-of-
    order or interrupted groups are discarded rather than merged, so a dropped
    sentence shows up as a momentarily smaller satellite count instead of a
    stale satellite that never disappears.
    """

    def __init__(self) -> None:
        self._partial: dict[str, list[Satellite]] = {}
        self._expected: dict[str, int] = {}
        self._next_index: dict[str, int] = {}
        self.groups: dict[str, list[Satellite]] = {}
        """Last *complete* group per talker."""

    def reset(self) -> None:
        self._partial.clear()
        self._expected.clear()
        self._next_index.clear()
        self.groups.clear()

    def feed(self, s: Sentence) -> bool:
        """Add one GSV sentence.  Returns True when a group has just completed."""
        f = s.fields
        total = _int(_at(f, 0))
        index = _int(_at(f, 1))
        if total is None or index is None or index < 1 or total < 1:
            return False

        talker = s.talker
        if index == 1:
            self._partial[talker] = []
            self._expected[talker] = total
            self._next_index[talker] = 1
        elif self._next_index.get(talker) != index or self._expected.get(talker) != total:
            # Sentence out of sequence: drop the whole group.
            self._partial.pop(talker, None)
            self._expected.pop(talker, None)
            self._next_index.pop(talker, None)
            return False

        satellites = self._partial.setdefault(talker, [])
        # Four satellites per sentence, each four fields, starting at field 3.
        #
        # NMEA 4.10 appends a signal-ID field after the last satellite block,
        # and the V-800 MarkIII does emit it.  A naive stride over the fields
        # reads that lone trailing field as a fifth satellite with PRN 0 --
        # observed on real hardware as seven phantom satellites.  Requiring all
        # four fields of a block to be present skips it, and skipping PRN 0
        # guards the same failure in any other shape.
        for base in range(3, len(f), 4):
            if base + 3 >= len(f):
                break
            prn = _int(_at(f, base))
            if not prn:
                continue
            satellites.append(
                Satellite(
                    prn=prn,
                    constellation=constellation_for(talker, prn),
                    elevation=_int(_at(f, base + 1)),
                    azimuth=_int(_at(f, base + 2)),
                    snr=_int(_at(f, base + 3)),
                )
            )

        self._next_index[talker] = index + 1
        if index == total:
            self.groups[talker] = satellites
            self._partial.pop(talker, None)
            self._expected.pop(talker, None)
            self._next_index.pop(talker, None)
            return True
        return False

    def satellites(self) -> list[Satellite]:
        """All satellites from the most recent complete group of every talker."""
        out: list[Satellite] = []
        for group in self.groups.values():
            out.extend(group)
        out.sort(key=lambda sat: (int(sat.constellation), sat.prn))
        return out


# --------------------------------------------------------------------------
# Top-level state
# --------------------------------------------------------------------------


class NavState:
    """Accumulates NMEA traffic into a current :class:`Fix` and satellite list.

    Also tracks which sentences have been seen and how often, which the Sentence
    Rates pane uses to show what the receiver is *actually* emitting rather than
    what it was last told to emit.  Those two disagreeing is the single most
    common reason a configuration appears not to have applied.
    """

    def __init__(self) -> None:
        self.fix = Fix()
        self.gsv = GsvAssembler()
        self.seen: dict[str, int] = {}
        """Address field (e.g. ``GPGSV``) -> count since the last reset."""
        self.sentence_count = 0
        self.checksum_errors = 0
        self.antenna_status: str = ""
        """Latest antenna state from GPTXT, e.g. "ANTENNA OK"."""
        self.last_text: tuple[str, str] | None = None
        """(severity, text) of the most recent GPTXT."""
        self._gsa_group_open = False

    def reset(self) -> None:
        self.fix = Fix()
        self.gsv.reset()
        self.seen.clear()
        self.sentence_count = 0
        self.checksum_errors = 0
        self.antenna_status = ""
        self.last_text = None
        self._gsa_group_open = False

    def feed(self, s: Sentence) -> None:
        """Apply one standard NMEA sentence.  PMTK sentences are ignored here."""
        if s.is_pmtk or not s.formatter:
            return
        self.seen[s.address] = self.seen.get(s.address, 0) + 1
        self.sentence_count += 1

        formatter = s.formatter.upper()
        # A GSA group runs until something that is not a GSA turns up.  The
        # receiver emits one GSA per constellation back to back, so this is how
        # we tell "next constellation" from "next fix".
        if formatter != "GSA":
            self._gsa_group_open = False

        if formatter == "GGA":
            apply_gga(self.fix, s)
        elif formatter == "GLL":
            apply_gll(self.fix, s)
        elif formatter == "GSA":
            apply_gsa(self.fix, s, reset_used=not self._gsa_group_open)
            self._gsa_group_open = True
        elif formatter == "GSV":
            self.gsv.feed(s)
        elif formatter == "RMC":
            apply_rmc(self.fix, s)
        elif formatter == "VTG":
            apply_vtg(self.fix, s)
        elif formatter == "ZDA":
            apply_zda(self.fix, s)
        elif formatter == "TXT":
            apply_txt(self, s)

    def satellites(self) -> list[Satellite]:
        """Satellites in view, with ``used`` set from the current GSA solution."""
        sats = self.gsv.satellites()
        keys = self.fix.used_keys
        # PRNs that came from a GSA carrying no system ID.  Those are the only
        # ones that still have to be matched on PRN alone; everything else is
        # matched on (constellation, PRN), so GPS 5 and BeiDou 5 stay distinct.
        unattributed = self.fix.used_prns - {prn for _, prn in keys}
        for sat in sats:
            sat.used = (sat.constellation, sat.prn) in keys or sat.prn in unattributed
        return sats

    def constellation_summary(self) -> dict[Constellation, tuple[int, int]]:
        """``{constellation: (tracked, in view)}`` for the status bar."""
        summary: dict[Constellation, list[int]] = {}
        for sat in self.satellites():
            entry = summary.setdefault(sat.constellation, [0, 0])
            entry[1] += 1
            if sat.tracked:
                entry[0] += 1
        return {key: (value[0], value[1]) for key, value in summary.items()}
