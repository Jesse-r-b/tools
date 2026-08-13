"""Regressions captured from a real Columbus V-800 MarkIII on /dev/ttyUSB0.

Every sentence below is a verbatim capture, checksums included, taken at 9600
baud on 2026-08-13. They are here because the device disagreed with two
assumptions that looked entirely reasonable on paper:

1. GSV sentences carry an NMEA 4.10 trailing signal-ID field. Striding over the
   fields four at a time reads that lone field as a fifth satellite with PRN 0.
   The capture produced seven phantom satellites before this was fixed.

2. QZSS satellites are reported under the ``GP`` talker, not ``GQ``. Trusting
   the talker ID labelled PRNs 194, 195 and 199 as GPS.

Neither would have failed loudly. Both would have quietly corrupted the sky
view and the satellite counts.
"""

from __future__ import annotations

from v800 import pmtk
from v800.nmea import Constellation, NavState

#: Verbatim capture: one full epoch of GSV, GSA and position sentences.
CAPTURE = [
    "$GNGGA,042148.000,,,,,0,00,2.8,,,,,,*49",
    "$GNGLL,,,,,042148.000,V,N*6F",
    "$GNGSA,A,1,,,,,,,,,,,,,7.8,2.8,7.2,1*33",
    "$GNGSA,A,1,,,,,,,,,,,,,7.8,2.8,7.2,4*36",
    "$GNGSA,A,1,,,,,,,,,,,,,7.8,2.8,7.2,2*30",
    "$GPGSV,4,1,13,05,24,017,,06,27,138,,11,65,137,,12,79,206,,0*61",
    "$GPGSV,4,2,13,19,16,107,21,21,36,056,26,24,21,332,,25,40,225,,0*6B",
    "$GPGSV,4,3,13,28,06,218,,29,20,258,,194,10,351,,195,12,348,,0*6A",
    "$GPGSV,4,4,13,199,43,321,,0*61",
    "$BDGSV,1,1,02,01,49,340,,04,51,016,,0*7A",
    "$GLGSV,2,1,07,74,06,026,,70,,,34,86,65,249,32,77,14,210,,0*49",
    "$GLGSV,2,2,07,76,62,220,,87,32,294,,85,29,148,,0*41",
    "$GNRMC,042148.000,V,,,,,,,130826,,,N,V*2C",
    "$GNVTG,,,,,,,,,N*2E",
    "$GNZDA,042148.000,13,08,2026,00,00*4F",
    "$GPTXT,01,01,01,ANTENNA OK*35",
]


def feed(lines: list[str]) -> NavState:
    nav = NavState()
    for line in lines:
        sentence = pmtk.parse(line)
        assert sentence is not None, line
        assert sentence.checksum_state is pmtk.ChecksumState.OK, (
            f"capture has a bad checksum, so it is not a faithful capture: {line}"
        )
        nav.feed(sentence)
    return nav


def test_capture_checksums_are_all_valid() -> None:
    """If this fails the capture was transcribed wrongly, not the parser."""
    for line in CAPTURE:
        sentence = pmtk.parse(line)
        assert sentence is not None and sentence.checksum_state is pmtk.ChecksumState.OK


def test_trailing_signal_id_does_not_create_phantom_satellites() -> None:
    """The GSV signal-ID field must not be read as a satellite.

    Observed: seven satellites with PRN 0, elevation/azimuth/SNR all None.
    """
    nav = feed(CAPTURE)
    assert [sat for sat in nav.satellites() if sat.prn == 0] == []


def test_gsv_satellite_count_matches_the_sentences() -> None:
    """The three GSV groups declare 13 + 2 + 7 satellites and must yield exactly that.

    Before the trailing signal-ID fix this returned 29: the real 22 plus seven
    phantoms, one per GSV sentence.
    """
    nav = feed(CAPTURE)
    assert len(nav.satellites()) == 13 + 2 + 7


def test_qzss_reported_under_the_gp_talker_is_identified_as_qzss() -> None:
    """PRNs 194/195 arrive in GPGSV; they are QZSS, not GPS."""
    nav = feed(CAPTURE)
    systems = {sat.prn: sat.constellation for sat in nav.satellites()}
    assert systems[194] is Constellation.QZSS
    assert systems[195] is Constellation.QZSS
    assert systems[199] is Constellation.QZSS
    assert systems[5] is Constellation.GPS
    assert systems[29] is Constellation.GPS


def test_constellation_summary_from_the_capture() -> None:
    nav = feed(CAPTURE)
    summary = nav.constellation_summary()
    # GPGSV declares 13, of which 194/195/199 are QZSS -- so 10 GPS, 3 QZSS.
    # Two GPS carry a C/N0 (PRN 19 and 21); no QZSS or BeiDou is tracked.
    assert summary[Constellation.GPS] == (2, 10)
    assert summary[Constellation.QZSS] == (0, 3)
    assert summary[Constellation.GLONASS] == (2, 7)
    assert summary[Constellation.BEIDOU] == (0, 2)


def test_gsa_system_id_records_contributing_constellations() -> None:
    """The three GSA sentences carry system IDs 1, 4 and 2 - GPS, BeiDou, GLONASS."""
    nav = feed(CAPTURE)
    assert nav.fix.used_systems == {
        Constellation.GPS,
        Constellation.BEIDOU,
        Constellation.GLONASS,
    }


def test_no_fix_is_reported_as_no_fix() -> None:
    """The capture is from indoors: GGA quality 0, GSA mode 1, empty position."""
    nav = feed(CAPTURE)
    assert not nav.fix.has_fix
    assert nav.fix.latitude is None
    assert nav.fix.satellites_used == 0
    # DOP is still reported and must be decoded from the correct fields.
    assert nav.fix.pdop == 7.8
    assert nav.fix.hdop == 2.8
    assert nav.fix.vdop == 7.2


def test_date_decodes_from_both_rmc_and_zda() -> None:
    """RMC carries 130826 and ZDA carries 13/08/2026 - both mean 13 August 2026."""
    import datetime as dt

    nav = feed(CAPTURE)
    assert nav.fix.utc_date == dt.date(2026, 8, 13)
    assert nav.fix.utc_time is not None
    assert (nav.fix.utc_time.hour, nav.fix.utc_time.minute) == (4, 21)


def test_glonass_satellite_with_snr_but_no_elevation_is_kept() -> None:
    """GLGSV reports PRN 70 as ",,,34" - tracked, but with no position in the sky."""
    nav = feed(CAPTURE)
    sat = next(s for s in nav.satellites() if s.prn == 70)
    assert sat.elevation is None and sat.azimuth is None
    assert sat.snr == 34 and sat.tracked


def test_system_id_disambiguates_prns_shared_between_constellations() -> None:
    """GPS 5 and BeiDou 5 are different satellites and must not be conflated."""
    lines = [
        "GPGSV,1,1,01,05,22,017,40,0",
        "BDGSV,1,1,01,05,30,120,42,0",
        # Only GPS 5 is used in the solution (system ID 1).
        "GNGSA,A,3,05,,,,,,,,,,,,2.5,1.3,2.1,1",
    ]
    framed = [pmtk.build(line).decode().strip() for line in lines]
    nav = feed(framed)
    used = {(sat.constellation, sat.prn): sat.used for sat in nav.satellites()}
    assert used[(Constellation.GPS, 5)] is True
    assert used[(Constellation.BEIDOU, 5)] is False
