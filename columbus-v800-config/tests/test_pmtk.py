"""Protocol tests, checked against the MT3333 specification's own worked examples."""

from __future__ import annotations

import pytest

from v800 import pmtk
from v800.pmtk import AckFlag, ChecksumState, DgpsMode, Packet, PeriodicMode, StandbyType


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "example",
    [
        "PMTK000*32",
        "PMTK001,604,3*32",
        "PMTK010,001*2E",
        "PMTK011,MTKGPS*08",
        "PMTK101*32",
        "PMTK102*31",
        "PMTK103*30",
        "PMTK104*37",
        "PMTK120*31",
        "PMTK161,0*28",
        "PMTK220,1000*1F",
        "PMTK251,38400*27",
        "PMTK286,1*23",
        "PMTK301,1*2D",
        "PMTK313,1*2E",
        "PMTK314,-1*04",
        "PMTK330,0*2E",
        "PMTK331,6377397.155,299.1528128,-148.0,507.0,685.0*16",
        "PMTK335,2007,1,1,0,0,0*02",
        "PMTK351,0*29",
        "PMTK351,1*28",
        "PMTK353,0,1*36",
        "PMTK353,1,0*36",
        "PMTK353,1,1*37",
        "PMTK400*36",
        "PMTK401*37",
        "PMTK413*34",
        "PMTK414*33",
        "PMTK430*35",
        "PMTK431*34",
        "PMTK500,1000,0,0,0,0*1A",
        "PMTK501,1*2B",
        "PMTK513,1*28",
        "PMTK530,0*28",
        "PMTK589,1,052130.000,-0.4712*03",
        "PMTK605*31",
        "PMTK607*33",
        "PMTK660,1800*17",
        "PMTK661,30*1C",
        "PMTK705,AXN_0.2,1234,ABCD,*14",
        "PMTK740,2010,2,10,9,0,58*05",
        "PMTK810,0003,1D*4D",
        "PMTK811*3A",
        "PMTK812*39",
        "PMTK813,29,2*01",
        "PMTK814,29,1*05",
        "PMTK815,29,16,98,10000,30,4100,0*18",
        "PMTK837,1,50*0A",
    ],
)
def test_specification_example_checksums(example: str) -> None:
    """Every example the specification prints correctly must round-trip.

    The four it prints *in*correctly are pinned separately in
    ``test_spec_examples.py``.
    """
    payload, given = example.rsplit("*", 1)
    assert f"{pmtk.checksum(payload):02X}" == given.upper()
    assert pmtk.build(payload) == f"${example}\r\n".encode()


def test_build_strips_existing_framing() -> None:
    assert pmtk.build("$PMTK605*99") == b"$PMTK605*31\r\n"


def test_parse_reports_bad_checksum_rather_than_dropping() -> None:
    sentence = pmtk.parse("$PMTK605*00")
    assert sentence is not None
    assert sentence.checksum_state is ChecksumState.BAD
    assert sentence.packet_type == 605


def test_parse_reports_absent_checksum() -> None:
    sentence = pmtk.parse("$PMTK605")
    assert sentence is not None
    assert sentence.checksum_state is ChecksumState.ABSENT


def test_parse_rejects_non_sentences() -> None:
    assert pmtk.parse("") is None
    assert pmtk.parse("not a sentence") is None


def test_parse_splits_talker_and_formatter() -> None:
    sentence = pmtk.parse("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47")
    assert sentence is not None
    assert sentence.talker == "GP"
    assert sentence.formatter == "GGA"
    assert sentence.address == "GPGGA"
    assert not sentence.is_pmtk
    assert sentence.packet_type is None


# --------------------------------------------------------------------------
# Acknowledgements
# --------------------------------------------------------------------------


def test_parse_ack() -> None:
    ack = pmtk.parse_ack(pmtk.parse("$PMTK001,604,3*32"))
    assert ack is not None
    assert ack.command == 604
    assert ack.flag is AckFlag.SUCCEEDED
    assert ack.ok


def test_parse_ack_failure_flags() -> None:
    for flag in (AckFlag.INVALID, AckFlag.UNSUPPORTED, AckFlag.FAILED):
        raw = pmtk.build(f"PMTK001,314,{int(flag)}").decode().strip()
        ack = pmtk.parse_ack(pmtk.parse(raw))
        assert ack is not None and not ack.ok


# --------------------------------------------------------------------------
# The PMTK660/661 satellite mask, per the specification's worked examples
# --------------------------------------------------------------------------


def test_available_sv_eph_matches_spec_example() -> None:
    """Section 2.3.41: hex 40449464 -> SVs 3, 6, 7, 11, 13, 16, 19, 23, 31."""
    query, svs = pmtk.parse_available_sv(pmtk.parse("$PMTK001,660,3,40449464*17"))
    assert query == int(Packet.Q_AVAILABLE_SV_EPH)
    assert svs == [3, 6, 7, 11, 13, 16, 19, 23, 31]


def test_available_sv_alm_matches_spec_example() -> None:
    """Section 2.3.42: hex fec0bfff -> SVs 1-14, 16, 23, 24, 26-32."""
    query, svs = pmtk.parse_available_sv(pmtk.parse("$PMTK001,661,3,fec0bfff*49"))
    assert query == int(Packet.Q_AVAILABLE_SV_ALM)
    assert svs == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16, 23, 24, 26, 27, 28, 29, 30, 31, 32]


def test_plain_ack_is_not_mistaken_for_an_sv_mask() -> None:
    assert pmtk.parse_available_sv(pmtk.parse("$PMTK001,604,3*32")) is None


def test_sv_mask_is_not_mistaken_for_a_plain_ack_result() -> None:
    """PMTK660's reply is still a valid PMTK001, and must decode as one too."""
    ack = pmtk.parse_ack(pmtk.parse("$PMTK001,660,3,40449464*17"))
    assert ack is not None and ack.command == 660


# --------------------------------------------------------------------------
# NMEA output configuration
# --------------------------------------------------------------------------


def test_set_nmea_output_matches_spec_example() -> None:
    """Section 2.3.19 prints GLL/RMC/VTG/GGA/GSA every fix and GSV every fifth."""
    payload = pmtk.set_nmea_output(
        {"GLL": 1, "RMC": 1, "VTG": 1, "GGA": 1, "GSA": 1, "GSV": 5}
    )
    assert payload == "PMTK314,1,1,1,1,1,5,0,0,0,0,0,0,0,0,0,0,0,0,0"
    assert len(payload.split(",")) == 1 + pmtk.NMEA_OUTPUT_FIELD_COUNT


def test_set_nmea_output_always_sends_19_fields() -> None:
    payload = pmtk.set_nmea_output({})
    assert payload.split(",")[1:] == ["0"] * 19


def test_set_nmea_output_places_zda_in_field_17() -> None:
    fields = pmtk.set_nmea_output({"ZDA": 1}).split(",")[1:]
    assert fields[17] == "1"
    assert sum(int(v) for v in fields) == 1


def test_set_nmea_output_rejects_unknown_sentence() -> None:
    with pytest.raises(ValueError, match="no field"):
        pmtk.set_nmea_output({"GRS": 1})


def test_set_nmea_output_rejects_out_of_range_rate() -> None:
    with pytest.raises(ValueError, match="must be one of"):
        pmtk.set_nmea_output({"GGA": 6})


def test_parse_nmea_output_round_trips() -> None:
    rates = {"GLL": 0, "RMC": 1, "VTG": 2, "GGA": 1, "GSA": 5, "GSV": 5, "ZDA": 1}
    payload = pmtk.set_nmea_output(rates)
    reply = pmtk.parse(pmtk.build(payload.replace("PMTK314", "PMTK514")).decode().strip())
    assert pmtk.parse_nmea_output(reply) == rates


# --------------------------------------------------------------------------
# Range checking
# --------------------------------------------------------------------------


def test_set_pos_fix_enforces_minimum() -> None:
    assert pmtk.set_pos_fix(1000) == "PMTK220,1000"
    with pytest.raises(ValueError):
        pmtk.set_pos_fix(50)


def test_set_fix_ctl_matches_spec_example_and_range() -> None:
    assert pmtk.set_fix_ctl(1000) == "PMTK300,1000,0,0,0,0"
    with pytest.raises(ValueError):
        pmtk.set_fix_ctl(99)
    with pytest.raises(ValueError):
        pmtk.set_fix_ctl(10001)


def test_set_nmea_baudrate_rejects_undocumented_rates() -> None:
    assert pmtk.set_nmea_baudrate(38400) == "PMTK251,38400"
    with pytest.raises(ValueError):
        pmtk.set_nmea_baudrate(31250)


def test_static_nav_threshold_range() -> None:
    assert pmtk.set_static_nav_threshold(0.4) == "PMTK386,0.4"
    assert pmtk.set_static_nav_threshold(0) == "PMTK386,0"
    with pytest.raises(ValueError):
        pmtk.set_static_nav_threshold(2.5)


def test_al_dee_config_matches_spec_example() -> None:
    assert pmtk.al_dee_config(1, 25, 180000, 60000) == "PMTK223,1,25,180000,60000"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sv": 5},
        {"snr": 24},
        {"extension_threshold": 39999},
        {"extension_gap": 3600001},
    ],
)
def test_al_dee_config_enforces_ranges(kwargs) -> None:
    with pytest.raises(ValueError):
        pmtk.al_dee_config(**kwargs)


def test_query_intervals_are_range_checked() -> None:
    assert pmtk.query_available_sv_eph(1800) == "PMTK660,1800"
    assert pmtk.query_available_sv_alm(30) == "PMTK661,30"
    with pytest.raises(ValueError):
        pmtk.query_available_sv_eph(7201)
    with pytest.raises(ValueError):
        pmtk.query_available_sv_alm(366)


def test_position_aiding_matches_spec_example() -> None:
    """Section 2.3.45's example, with its explicit two-digit time fields."""
    payload = pmtk.set_position_aiding(24.772816, 121.022636, 160, 2011, 8, 1, 8, 0, 0)
    assert payload == "PMTK741,24.772816,121.022636,160,2011,8,1,08,00,00"


def test_position_aiding_range_checks() -> None:
    with pytest.raises(ValueError, match="latitude"):
        pmtk.set_position_aiding(91, 0, 0, 2011, 8, 1, 8, 0, 0)
    with pytest.raises(ValueError, match="longitude"):
        pmtk.set_position_aiding(0, 181, 0, 2011, 8, 1, 8, 0, 0)


def test_utc_aiding_matches_spec_example() -> None:
    assert pmtk.set_utc_aiding(2010, 2, 10, 9, 0, 58) == "PMTK740,2010,2,10,9,0,58"


def test_rtc_time_matches_spec_example() -> None:
    assert pmtk.set_rtc_time(2007, 1, 1, 0, 0, 0) == "PMTK335,2007,1,1,0,0,0"


def test_datum_advance_matches_spec_example() -> None:
    """Section 2.3.21's Bessel 1841 example, to full precision.

    The spec prints trailing ``.0`` on the three offsets; those are dropped as
    numerically identical. Every significant digit of the axis and eccentricity
    must survive, which is what caught the ``%g`` bug.
    """
    payload = pmtk.set_datum_advance(6377397.155, 299.1528128, -148.0, 507.0, 685.0)
    assert payload == "PMTK331,6377397.155,299.1528128,-148,507,685"


def test_number_formatting_never_uses_scientific_notation() -> None:
    """A field the receiver cannot parse is worse than a rejected input."""
    for value in (6377397.155, 6378137.0, 1e-7, 123456789.0, 0.0000001, -6377397.155):
        text = pmtk.format_number(value)
        assert "e" not in text.lower(), f"{value!r} formatted as {text!r}"


def test_number_formatting_is_lossless_for_datum_ranges() -> None:
    for value in (6377397.155, 299.1528128, 6378137.0, 298.257223563):
        assert float(pmtk.format_number(value)) == pytest.approx(value, rel=1e-11)


def test_number_formatting_trims_but_keeps_the_value() -> None:
    assert pmtk.format_number(-148.0) == "-148"
    assert pmtk.format_number(0.0) == "0"
    assert pmtk.format_number(0.4) == "0.4"


def test_number_formatting_rejects_non_finite() -> None:
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            pmtk.format_number(value)


# --------------------------------------------------------------------------
# Periodic modes
# --------------------------------------------------------------------------


def test_periodic_mode_matches_spec_examples() -> None:
    """The two sequences printed in section 2.3.13."""
    assert pmtk.periodic_mode(PeriodicMode.NORMAL) == "PMTK225,0"
    assert (
        pmtk.periodic_mode(PeriodicMode.PERIODIC_BACKUP, 3000, 12000, 18000, 72000)
        == "PMTK225,1,3000,12000,18000,72000"
    )
    assert (
        pmtk.periodic_mode(PeriodicMode.PERIODIC_STANDBY, 3000, 12000, 18000, 72000)
        == "PMTK225,2,3000,12000,18000,72000"
    )


def test_alwayslocate_modes_take_no_timing() -> None:
    assert pmtk.periodic_mode(PeriodicMode.ALWAYSLOCATE_STANDBY) == "PMTK225,8"
    assert pmtk.periodic_mode(PeriodicMode.ALWAYSLOCATE_BACKUP) == "PMTK225,9"
    assert pmtk.periodic_mode(PeriodicMode.PERPETUAL_BACKUP) == "PMTK225,4"


def test_periodic_mode_requires_timing_when_the_mode_needs_it() -> None:
    with pytest.raises(ValueError, match="run time"):
        pmtk.periodic_mode(PeriodicMode.PERIODIC_BACKUP)


def test_periodic_mode_enforces_second_run_time_ordering() -> None:
    """Section 2.3.13: "The Second run time should larger than First run time"."""
    with pytest.raises(ValueError, match="second run time"):
        pmtk.periodic_mode(PeriodicMode.PERIODIC_BACKUP, 12000, 12000, 3000, 72000)


def test_periodic_mode_allows_zero_to_disable_a_slot() -> None:
    assert (
        pmtk.periodic_mode(PeriodicMode.PERIODIC_STANDBY, 3000, 12000, 0, 0)
        == "PMTK225,2,3000,12000,0,0"
    )


def test_periodic_mode_rejects_out_of_range_times() -> None:
    with pytest.raises(ValueError):
        pmtk.periodic_mode(PeriodicMode.PERIODIC_BACKUP, 999, 12000, 0, 0)
    with pytest.raises(ValueError):
        pmtk.periodic_mode(PeriodicMode.PERIODIC_BACKUP, 3000, 518_400_001, 0, 0)


def test_standby_mode() -> None:
    assert pmtk.standby_mode(StandbyType.STOP) == "PMTK161,0"
    assert pmtk.standby_mode(StandbyType.SLEEP) == "PMTK161,1"


# --------------------------------------------------------------------------
# Simple setters
# --------------------------------------------------------------------------


def test_dgps_and_sbas() -> None:
    assert pmtk.set_dgps_mode(DgpsMode.WAAS) == "PMTK301,2"
    assert pmtk.set_sbas_enabled(True) == "PMTK313,1"
    assert pmtk.set_sbas_enabled(False) == "PMTK313,0"


def test_gnss_search_mode_matches_spec_examples() -> None:
    assert pmtk.set_gnss_search_mode(False, True) == "PMTK353,0,1"
    assert pmtk.set_gnss_search_mode(True, False) == "PMTK353,1,0"
    assert pmtk.set_gnss_search_mode(True, True) == "PMTK353,1,1"


def test_parse_gnss_search_mode_reads_the_echo() -> None:
    reply = pmtk.build("PMTK001,353,3,1,1,1").decode().strip()
    assert pmtk.parse_gnss_search_mode(pmtk.parse(reply)) == (1, 1, 1)


def test_restart_commands() -> None:
    assert pmtk.hot_start() == "PMTK101"
    assert pmtk.warm_start() == "PMTK102"
    assert pmtk.cold_start() == "PMTK103"
    assert pmtk.full_cold_start() == "PMTK104"
    assert pmtk.clear_flash_aid() == "PMTK120"


# --------------------------------------------------------------------------
# Test mode
# --------------------------------------------------------------------------


def test_test_all_matches_spec_example() -> None:
    """Section 2.3.46: bitmap 0x0003 (INFO|ACQ), SV id 0x1D = PRN 29.

    PRN 29 is outside the documented 1..20 window, so the spec's own example is
    not constructible through the range check -- that is deliberate.
    """
    assert pmtk.test_all(0x0003, 20) == "PMTK810,0003,14"
    with pytest.raises(ValueError, match="SV id"):
        pmtk.test_all(0x0003, 29)


def test_test_all_rejects_empty_bitmap() -> None:
    with pytest.raises(ValueError, match="bitmap"):
        pmtk.test_all(0, 1)


def test_parse_test_results() -> None:
    acq = pmtk.parse_test_result(pmtk.parse("$PMTK813,29,2*01"))
    assert acq.svid == 29 and acq.seconds == 2

    bitsync = pmtk.parse_test_result(pmtk.parse("$PMTK814,29,1*05"))
    assert bitsync.svid == 29 and bitsync.seconds == 1

    signal = pmtk.parse_test_result(pmtk.parse("$PMTK815,29,16,98,10000,30,4100,0*18"))
    assert signal.svid == 29
    assert signal.test_seconds == 16
    # Scale factors from the Unit column; see docs/spec-errata.md item 7 for why
    # this does not reproduce the prose's rounded figures.
    assert signal.phase_error == pytest.approx(0.98)
    assert signal.tcxo_offset == pytest.approx(100.0)
    assert signal.tcxo_drift == pytest.approx(0.30)
    assert signal.cnr_mean == pytest.approx(4.1)
    assert signal.cnr_sigma == pytest.approx(0.0)


def test_jamming_scan() -> None:
    assert pmtk.test_jamming(True, 50) == "PMTK837,1,50"
    with pytest.raises(ValueError):
        pmtk.test_jamming(True, 0)


# --------------------------------------------------------------------------
# Release / TCXO
# --------------------------------------------------------------------------


def test_parse_release_matches_spec_example() -> None:
    release = pmtk.parse_release(pmtk.parse("$PMTK705,AXN_0.2,1234,ABCD,*14"))
    assert release.release == "AXN_0.2"
    assert release.build_id == "1234"
    assert release.product_model == "ABCD"
    assert release.sdk_version == ""


def test_parse_tcxo_debug_matches_spec_example() -> None:
    valid, utc, drift = pmtk.parse_tcxo_debug(pmtk.parse("$PMTK589,1,052130.000,-0.4712*03"))
    assert valid is True
    assert utc == "052130.000"
    assert drift == pytest.approx(-0.4712)


# --------------------------------------------------------------------------
# Link budget
# --------------------------------------------------------------------------


def test_budget_scales_with_rate() -> None:
    rates = {"RMC": 1, "GGA": 1}
    at_1hz = pmtk.nmea_budget_bps(rates, 1000)
    at_10hz = pmtk.nmea_budget_bps(rates, 100)
    assert at_10hz == pytest.approx(at_1hz * 10)


def test_budget_honours_divisors() -> None:
    every_fix = pmtk.nmea_budget_bps({"GSV": 1}, 1000)
    every_fifth = pmtk.nmea_budget_bps({"GSV": 5}, 1000)
    assert every_fifth == pytest.approx(every_fix / 5)


def test_budget_ignores_disabled_sentences() -> None:
    assert pmtk.nmea_budget_bps({"GSV": 0, "GGA": 0}, 1000) == 0.0


def test_full_sentence_set_at_10hz_does_not_fit_9600_baud() -> None:
    """The warning the Rate pane shows has to actually be true."""
    rates = dict.fromkeys(pmtk.NMEA_OUTPUT_DESCRIPTIONS, 1)
    assert pmtk.nmea_budget_bps(rates, 100) > 9600


def test_catalogue_covers_every_packet_enum() -> None:
    """Every packet type the protocol module names must appear in the reference."""
    missing = {p.name for p in Packet if int(p) not in pmtk.COMMANDS_BY_PACKET}
    assert not missing
