"""NMEA decoding tests, using real multi-constellation traffic shapes."""

from __future__ import annotations

import datetime as dt

import pytest

from v800 import pmtk
from v800.nmea import (
    Constellation,
    FixQuality,
    FixType,
    GsvAssembler,
    NavState,
    constellation_for,
    parse_latlon,
    parse_nmea_date,
    parse_nmea_time,
)


def s(line: str):
    """Parse a sentence, adding a correct checksum so tests stay readable."""
    if "*" not in line:
        line = pmtk.build(line.lstrip("$")).decode().strip()
    parsed = pmtk.parse(line)
    assert parsed is not None
    return parsed


# --------------------------------------------------------------------------
# Field helpers
# --------------------------------------------------------------------------


def test_parse_latlon_handles_both_widths() -> None:
    assert parse_latlon("4807.038", "N") == pytest.approx(48.1173)
    assert parse_latlon("01131.000", "E") == pytest.approx(11.516667, abs=1e-6)


def test_parse_latlon_applies_hemisphere() -> None:
    assert parse_latlon("3352.000", "S") == pytest.approx(-33.866667, abs=1e-6)
    assert parse_latlon("15112.000", "W") == pytest.approx(-151.2, abs=1e-6)


def test_parse_latlon_tolerates_extra_precision() -> None:
    """At 10 Hz some firmware emits more than four fractional minutes digits."""
    assert parse_latlon("3352.1234567", "S") == pytest.approx(-(33 + 52.1234567 / 60))


def test_parse_latlon_rejects_junk() -> None:
    assert parse_latlon("", "N") is None
    assert parse_latlon("4807.038", "") is None
    assert parse_latlon("12", "N") is None


def test_parse_nmea_time() -> None:
    assert parse_nmea_time("123519.000") == dt.time(12, 35, 19)
    assert parse_nmea_time("") is None


def test_parse_nmea_time_clamps_a_leap_second() -> None:
    """:60 is legal in NMEA and illegal in datetime.time; the sentence must survive."""
    value = parse_nmea_time("235960.000")
    assert value is not None and value.second == 59


def test_parse_nmea_date_pivots_on_1980() -> None:
    assert parse_nmea_date("230394") == dt.date(1994, 3, 23)
    assert parse_nmea_date("010126") == dt.date(2026, 1, 1)


# --------------------------------------------------------------------------
# Constellation resolution
# --------------------------------------------------------------------------


def test_talker_id_wins_over_prn_range() -> None:
    assert constellation_for("GL", 5) is Constellation.GLONASS
    assert constellation_for("GP", 5) is Constellation.GPS
    assert constellation_for("BD", 5) is Constellation.BEIDOU


def test_gn_falls_back_to_prn_allocation() -> None:
    assert constellation_for("GN", 5) is Constellation.GPS
    assert constellation_for("GN", 70) is Constellation.GLONASS
    assert constellation_for("GN", 40) is Constellation.SBAS
    assert constellation_for("GN", 310) is Constellation.GALILEO


# --------------------------------------------------------------------------
# Sentence decoding
# --------------------------------------------------------------------------


def test_gga_populates_the_fix() -> None:
    nav = NavState()
    nav.feed(s("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"))
    fix = nav.fix
    assert fix.quality is FixQuality.GPS_SPS
    assert fix.latitude == pytest.approx(48.1173)
    assert fix.longitude == pytest.approx(11.516667, abs=1e-6)
    assert fix.altitude_m == pytest.approx(545.4)
    assert fix.geoid_separation_m == pytest.approx(46.9)
    assert fix.satellites_used == 8
    assert fix.hdop == pytest.approx(0.9)
    assert fix.has_fix


def test_gga_without_a_position_does_not_erase_the_last_one() -> None:
    """During reacquisition the receiver emits GGA with empty position fields."""
    nav = NavState()
    nav.feed(s("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"))
    nav.feed(s("GPGGA,123520,,,,,0,00,,,M,,M,,"))
    assert nav.fix.latitude == pytest.approx(48.1173)
    assert nav.fix.quality is FixQuality.INVALID  # but the loss of fix is reported


def test_rmc_populates_date_speed_and_variation() -> None:
    nav = NavState()
    nav.feed(s("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"))
    fix = nav.fix
    assert fix.utc_date == dt.date(1994, 3, 23)
    assert fix.speed_knots == pytest.approx(22.4)
    assert fix.course_true == pytest.approx(84.4)
    assert fix.magnetic_variation == pytest.approx(-3.1)  # W is negative


def test_vtg_populates_both_speed_units() -> None:
    nav = NavState()
    nav.feed(s("GPVTG,084.4,T,087.5,M,022.4,N,041.5,K,A"))
    assert nav.fix.course_true == pytest.approx(84.4)
    assert nav.fix.course_magnetic == pytest.approx(87.5)
    assert nav.fix.speed_knots == pytest.approx(22.4)
    assert nav.fix.speed_kph == pytest.approx(41.5)


def test_zda_gives_a_four_digit_year() -> None:
    nav = NavState()
    nav.feed(s("GPZDA,123519.00,23,03,2026,10,00"))
    assert nav.fix.utc_date == dt.date(2026, 3, 23)
    assert nav.fix.local_zone_hours == 10


def test_datetime_utc_combines_date_and_time() -> None:
    nav = NavState()
    nav.feed(s("GPZDA,123519.00,23,03,2026,00,00"))
    combined = nav.fix.datetime_utc
    assert combined == dt.datetime(2026, 3, 23, 12, 35, 19, tzinfo=dt.timezone.utc)


def test_gsa_records_used_prns_and_dop() -> None:
    nav = NavState()
    nav.feed(s("GPGSA,A,3,04,05,,09,12,,,24,,,,,2.5,1.3,2.1"))
    assert nav.fix.fix_type is FixType.FIX_3D
    assert nav.fix.used_prns == {4, 5, 9, 12, 24}
    assert nav.fix.pdop == pytest.approx(2.5)
    assert nav.fix.hdop == pytest.approx(1.3)
    assert nav.fix.vdop == pytest.approx(2.1)


def test_consecutive_gsa_sentences_accumulate_across_constellations() -> None:
    """GPS and GLONASS GSA arrive back to back and must not overwrite each other."""
    nav = NavState()
    nav.feed(s("GPGSA,A,3,04,05,09,,,,,,,,,,2.5,1.3,2.1"))
    nav.feed(s("GLGSA,A,3,68,69,,,,,,,,,,,2.5,1.3,2.1"))
    assert nav.fix.used_prns == {4, 5, 9, 68, 69}


def test_a_new_gsa_group_resets_the_used_list() -> None:
    """The next epoch's GSA must replace the previous one, not add to it."""
    nav = NavState()
    nav.feed(s("GPGSA,A,3,04,05,,,,,,,,,,,2.5,1.3,2.1"))
    nav.feed(s("GLGSA,A,3,68,,,,,,,,,,,,2.5,1.3,2.1"))
    nav.feed(s("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"))
    nav.feed(s("GPGSA,A,3,11,12,,,,,,,,,,,2.5,1.3,2.1"))
    assert nav.fix.used_prns == {11, 12}


# --------------------------------------------------------------------------
# GSV reassembly
# --------------------------------------------------------------------------


def test_gsv_group_completes_only_on_the_last_sentence() -> None:
    a = GsvAssembler()
    assert a.feed(s("GPGSV,2,1,05,01,40,083,46,02,17,308,41,12,07,344,39,14,22,228,45")) is False
    assert a.groups == {}
    assert a.feed(s("GPGSV,2,2,05,25,15,050,38")) is True
    assert len(a.groups["GP"]) == 5


def test_gsv_out_of_order_group_is_discarded() -> None:
    """A dropped middle sentence must not produce a half-populated group."""
    a = GsvAssembler()
    a.feed(s("GPGSV,3,1,09,01,40,083,46"))
    assert a.feed(s("GPGSV,3,3,09,25,15,050,38")) is False
    assert a.groups == {}


def test_gsv_groups_are_tracked_per_talker() -> None:
    a = GsvAssembler()
    a.feed(s("GPGSV,1,1,01,01,40,083,46"))
    a.feed(s("GLGSV,1,1,01,68,30,120,40"))
    assert set(a.groups) == {"GP", "GL"}
    prns = {sat.prn for sat in a.satellites()}
    assert prns == {1, 68}


def test_gsv_interleaved_talkers_do_not_corrupt_each_other() -> None:
    """The receiver can interleave constellations; state is per talker."""
    a = GsvAssembler()
    a.feed(s("GPGSV,2,1,05,01,40,083,46"))
    a.feed(s("GLGSV,1,1,01,68,30,120,40"))
    assert a.feed(s("GPGSV,2,2,05,25,15,050,38")) is True
    assert len(a.groups["GP"]) == 2
    assert len(a.groups["GL"]) == 1


def test_gsv_blank_snr_means_in_view_but_not_tracked() -> None:
    a = GsvAssembler()
    a.feed(s("GPGSV,1,1,02,01,40,083,46,02,17,308,"))
    tracked, untracked = a.satellites()
    assert tracked.snr == 46 and tracked.tracked
    assert untracked.snr is None and not untracked.tracked


def test_satellites_are_marked_used_from_gsa() -> None:
    nav = NavState()
    nav.feed(s("GPGSV,1,1,02,01,40,083,46,02,17,308,41"))
    nav.feed(s("GPGSA,A,3,01,,,,,,,,,,,,2.5,1.3,2.1"))
    used = {sat.prn: sat.used for sat in nav.satellites()}
    assert used == {1: True, 2: False}


# --------------------------------------------------------------------------
# NavState bookkeeping
# --------------------------------------------------------------------------


def test_nav_state_counts_sentences_by_address() -> None:
    nav = NavState()
    nav.feed(s("GPGSV,1,1,01,01,40,083,46"))
    nav.feed(s("GLGSV,1,1,01,68,30,120,40"))
    nav.feed(s("GPGSV,1,1,01,02,40,083,46"))
    assert nav.seen == {"GPGSV": 2, "GLGSV": 1}
    assert nav.sentence_count == 3


def test_nav_state_ignores_pmtk() -> None:
    nav = NavState()
    nav.feed(s("PMTK001,314,3"))
    assert nav.sentence_count == 0


def test_constellation_summary_counts_tracked_and_in_view() -> None:
    nav = NavState()
    nav.feed(s("GPGSV,1,1,03,01,40,083,46,02,17,308,41,03,10,200,"))
    nav.feed(s("GLGSV,1,1,01,68,30,120,40"))
    summary = nav.constellation_summary()
    assert summary[Constellation.GPS] == (2, 3)
    assert summary[Constellation.GLONASS] == (1, 1)


def test_reset_clears_everything() -> None:
    nav = NavState()
    nav.feed(s("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"))
    nav.feed(s("GPGSV,1,1,01,01,40,083,46"))
    nav.reset()
    assert nav.sentence_count == 0
    assert nav.seen == {}
    assert nav.satellites() == []
    assert nav.fix.latitude is None
