"""Pins the known defects in the MT3333 specification, documented in docs/spec-errata.md.

These tests exist so that nobody "corrects" this tool to agree with the printed
document. Each one asserts that the specification's printed example is wrong and
that the computed value is right.
"""

from __future__ import annotations

import pytest

from v800 import pmtk


def computed(payload: str) -> str:
    return f"{pmtk.checksum(payload):02X}"


def test_pmtk352_example_checksums_are_transposed() -> None:
    """Errata 1: section 2.3.24 prints *2B for ,0 and *2A for ,1 -- swapped."""
    assert computed("PMTK352,0") == "2A"  # spec prints 2B
    assert computed("PMTK352,1") == "2B"  # spec prints 2A


def test_pmtk352_polarity_follows_the_examples_not_the_table() -> None:
    """Errata 2: the packet is SET_STOP_QZSS, so 1 stops QZSS.

    The parameter table's generic "0: Disable, 1: Enable" row contradicts both
    the worked examples and the packet name. This tool follows the examples.
    """
    assert pmtk.set_qzss_enabled(True) == "PMTK352,0"
    assert pmtk.set_qzss_enabled(False) == "PMTK352,1"


def test_pmtk386_example_checksum_is_wrong() -> None:
    """Errata 3: section 2.3.26 prints $PMTK386,0.4*19; the correct value is *39."""
    assert computed("PMTK386,0.4") == "39"
    assert pmtk.build("PMTK386,0.4") == b"$PMTK386,0.4*39\r\n"


def test_pmtk514_example_is_short_one_field_and_miscomputed() -> None:
    """Errata 4: the printed PMTK514 example has 18 fields, not 19, and a bad checksum."""
    printed = "PMTK514,1,1,1,1,1,5,1,1,1,1,1,0,1,1,1,1,1,1"
    assert len(printed.split(",")) - 1 == 18
    assert computed(printed) != "2A"  # the checksum the spec prints

    # What this tool emits always carries the documented 19.
    ours = pmtk.set_nmea_output({"GLL": 1, "RMC": 1, "VTG": 1, "GGA": 1, "GSA": 1, "GSV": 5})
    assert len(ours.split(",")) - 1 == pmtk.NMEA_OUTPUT_FIELD_COUNT == 19


def test_datum_table_has_223_entries_not_the_219_claimed_in_prose() -> None:
    """Errata 5: section 2.3.20 says 219; Appendix A enumerates 0-222."""
    from v800.datums import DATUMS

    assert len(DATUMS) == 223
    assert min(DATUMS) == 0 and max(DATUMS) == 222
    assert sorted(DATUMS) == list(range(223))  # no gaps, no duplicates
    assert DATUMS[0][0] == "WGS1984"
    assert DATUMS[3][0] == "User Setting"


def test_fix_interval_minimum_disagrees_between_command_and_reply() -> None:
    """Errata 6: PMTK220/300 allow 100 ms, but the PMTK500 reply documents >= 200.

    Both are enforced as documented rather than reconciled, so 100 ms builds
    fine and the Rate pane's warning about read-back is the user's protection.
    """
    assert pmtk.set_pos_fix(100) == "PMTK220,100"
    assert pmtk.set_fix_ctl(100) == "PMTK300,100,0,0,0,0"
    assert pmtk.FIX_CTL_RANGE_MS == (100, 10000)
    assert pmtk.POS_FIX_MIN_MS == 100


def test_pmtk815_unit_column_does_not_reproduce_the_prose_figures() -> None:
    """Errata 7: the prose reads the example back with inconsistent divisors.

    We apply the Unit column. This test records the discrepancy so the numbers
    are never mistaken for calibrated values.
    """
    signal = pmtk.parse_test_result(pmtk.parse("$PMTK815,29,16,98,10000,30,4100,0*18"))

    # Unit column (0.01 / 0.001) -- what this tool reports.
    assert signal.tcxo_offset == pytest.approx(100.0)
    assert signal.cnr_mean == pytest.approx(4.1)

    # The prose says "10/0.03" and "41/0", which need divisors of 1000 and 100.
    assert 10000 / 1000 == 10.0
    assert 4100 / 100 == 41.0
    # i.e. the two readings differ by a factor of 10 either way.
    assert signal.tcxo_offset != pytest.approx(10.0)
    assert signal.cnr_mean != pytest.approx(41.0)
