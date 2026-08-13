"""Tests for the connection/health assessment.

The point of this module is that a user can tell *what* is wrong, so these tests
assert on the distinction between states rather than on exact wording. Several
of these states are awkward to produce on real hardware -- a shorted antenna, a
saturated port -- which is exactly why the logic is pure and tested here.
"""

from __future__ import annotations

from v800.health import Level, Snapshot, assess, command_path_note


def connected(**kwargs) -> Snapshot:
    """A healthy baseline: open, decoding, tracking, fixed."""
    base = dict(
        is_open=True,
        port="/dev/ttyUSB0",
        baud=9600,
        seconds_since_open=30.0,
        bytes_received=40000,
        lines_received=500,
        sentences_decoded=480,
        checksum_errors=0,
        seconds_since_last_sentence=0.2,
        satellites_in_view=12,
        satellites_tracked=8,
        satellites_used=7,
        has_fix=True,
        fix_description="3D fix",
        hdop=1.1,
        antenna_status="ANTENNA OK",
    )
    base.update(kwargs)
    return Snapshot(**base)


# --------------------------------------------------------------------------
# The healthy case
# --------------------------------------------------------------------------


def test_healthy_link_is_ok() -> None:
    health = assess(connected())
    assert health.level is Level.OK
    assert not health.is_problem


def test_healthy_detail_reports_the_satellite_counts() -> None:
    health = assess(connected(satellites_in_view=12, satellites_tracked=8, satellites_used=7))
    assert "8 of 12" in health.detail
    assert "7 used" in health.detail


# --------------------------------------------------------------------------
# Not connected
# --------------------------------------------------------------------------


def test_not_connected_is_idle_not_an_error() -> None:
    """Disconnected is a resting state, not a fault to shout about."""
    health = assess(Snapshot(is_open=False))
    assert health.level is Level.IDLE
    assert not health.is_problem
    assert "Not connected" in health.headline


# --------------------------------------------------------------------------
# Link faults, outermost first
# --------------------------------------------------------------------------


def test_no_data_during_startup_grace_is_only_informational() -> None:
    health = assess(connected(bytes_received=0, lines_received=0, sentences_decoded=0,
                              seconds_since_open=1.0, seconds_since_last_sentence=None))
    assert health.level is Level.INFO
    assert not health.is_problem


def test_no_data_after_the_grace_period_is_an_error() -> None:
    health = assess(connected(bytes_received=0, lines_received=0, sentences_decoded=0,
                              seconds_since_open=10.0, seconds_since_last_sentence=None))
    assert health.level is Level.ERROR
    assert "No data" in health.headline


def test_data_that_does_not_decode_points_at_the_baud_rate() -> None:
    """The signature of a wrong baud rate: bytes arrive, nothing parses."""
    health = assess(connected(bytes_received=3493, lines_received=300, sentences_decoded=0,
                              checksum_errors=0, seconds_since_open=10.0,
                              seconds_since_last_sentence=None))
    assert health.level is Level.ERROR
    assert "baud" in health.detail.lower()


def test_bytes_arriving_with_no_lines_is_wrong_baud_not_a_dead_port() -> None:
    """Regression: measured on real hardware at 38400 against a 9600 receiver.

    Reading 9600-baud data at 38400 produced 3493 bytes and not a single
    recognised line terminator. Counting lines rather than bytes reported "No
    data from the port" and sent the reader to check the power supply, when the
    fix was to press Detect. The byte counter exists for exactly this case.
    """
    health = assess(connected(bytes_received=3493, lines_received=0, sentences_decoded=0,
                              seconds_since_open=8.0, seconds_since_last_sentence=None))
    assert health.level is Level.ERROR
    assert "not valid NMEA" in health.headline
    assert "baud" in health.detail.lower()
    assert "3,493 bytes" in health.detail


def test_truly_dead_port_and_wrong_baud_give_different_headlines() -> None:
    dead = assess(connected(bytes_received=0, lines_received=0, sentences_decoded=0,
                            seconds_since_open=8.0, seconds_since_last_sentence=None))
    wrong_baud = assess(connected(bytes_received=3493, lines_received=0, sentences_decoded=0,
                                  seconds_since_open=8.0, seconds_since_last_sentence=None))
    assert dead.headline != wrong_baud.headline
    # The two must give different *advice*, not just different wording: sweeping
    # baud rates is the fix for one and a waste of time for the other.
    assert "Detect" in wrong_baud.detail
    assert "Detect" not in dead.detail
    assert "power" in dead.detail.lower()


def test_a_stalled_link_is_distinguished_from_one_that_never_started() -> None:
    stalled = assess(connected(seconds_since_last_sentence=15.0))
    never = assess(connected(bytes_received=0, lines_received=0, sentences_decoded=0,
                             seconds_since_open=10.0, seconds_since_last_sentence=None))
    assert stalled.level is Level.ERROR
    assert never.level is Level.ERROR
    assert stalled.headline != never.headline
    assert "stopped" in stalled.headline.lower()


def test_high_checksum_error_rate_is_reported_as_corruption() -> None:
    health = assess(connected(sentences_decoded=100, checksum_errors=20))
    assert health.level is Level.ERROR
    assert "checksum" in health.detail.lower()


def test_a_few_checksum_errors_do_not_trigger_the_warning() -> None:
    """The occasional corrupted line is normal and must not cry wolf."""
    health = assess(connected(sentences_decoded=1000, checksum_errors=2))
    assert health.level is Level.OK


# --------------------------------------------------------------------------
# Antenna
# --------------------------------------------------------------------------


def test_antenna_open_is_an_error_naming_the_antenna() -> None:
    health = assess(connected(antenna_status="ANTENNA OPEN"))
    assert health.level is Level.ERROR
    assert "Antenna" in health.headline


def test_antenna_short_is_an_error() -> None:
    assert assess(connected(antenna_status="ANTENNA SHORT")).level is Level.ERROR


def test_antenna_ok_does_not_trigger() -> None:
    assert assess(connected(antenna_status="ANTENNA OK")).level is Level.OK


def test_antenna_fault_outranks_having_no_satellites() -> None:
    """A disconnected antenna explains the empty sky; report the cause."""
    health = assess(connected(antenna_status="ANTENNA OPEN", satellites_in_view=0,
                              satellites_tracked=0, has_fix=False))
    assert "Antenna" in health.headline


# --------------------------------------------------------------------------
# Sky states -- the ones the user specifically asked to be able to tell apart
# --------------------------------------------------------------------------


def test_connected_but_no_satellites_in_view() -> None:
    health = assess(connected(satellites_in_view=0, satellites_tracked=0, satellites_used=0,
                              has_fix=False))
    assert health.level is Level.WARN
    assert "no satellites" in health.headline.lower()
    assert health.is_problem


def test_satellites_in_view_but_none_tracked() -> None:
    health = assess(connected(satellites_in_view=14, satellites_tracked=0, satellites_used=0,
                              has_fix=False))
    assert health.level is Level.WARN
    assert "none tracked" in health.headline.lower()


def test_the_three_sky_states_are_all_distinguishable() -> None:
    """Nothing in view / nothing tracked / tracking but no fix must differ."""
    none_visible = assess(connected(satellites_in_view=0, satellites_tracked=0,
                                    satellites_used=0, has_fix=False))
    none_tracked = assess(connected(satellites_in_view=14, satellites_tracked=0,
                                    satellites_used=0, has_fix=False))
    acquiring = assess(connected(satellites_in_view=14, satellites_tracked=5,
                                 satellites_used=0, has_fix=False))
    headlines = {none_visible.headline, none_tracked.headline, acquiring.headline}
    assert len(headlines) == 3


def test_acquiring_is_informational_not_a_fault() -> None:
    """Waiting for a fix is normal; it must not look like something broke."""
    health = assess(connected(satellites_tracked=5, satellites_used=0, has_fix=False))
    assert health.level is Level.INFO
    assert not health.is_problem


# --------------------------------------------------------------------------
# Degraded fixes
# --------------------------------------------------------------------------


def test_fix_on_fewer_than_four_satellites_is_flagged_marginal() -> None:
    health = assess(connected(satellites_used=3))
    assert health.level is Level.WARN
    assert "marginal" in health.headline.lower()


def test_poor_geometry_is_flagged_even_with_many_satellites() -> None:
    health = assess(connected(satellites_used=9, hdop=9.0))
    assert health.level is Level.WARN
    assert "geometry" in health.headline.lower()


def test_good_hdop_with_enough_satellites_is_ok() -> None:
    assert assess(connected(satellites_used=9, hdop=0.9)).level is Level.OK


# --------------------------------------------------------------------------
# Ordering: the outermost failure wins
# --------------------------------------------------------------------------


def test_no_data_outranks_every_inner_symptom() -> None:
    """With nothing arriving, "no satellites" would send you the wrong way."""
    health = assess(connected(bytes_received=0, lines_received=0, sentences_decoded=0,
                              seconds_since_open=10.0, satellites_in_view=0,
                              satellites_tracked=0, has_fix=False, antenna_status="",
                              seconds_since_last_sentence=None))
    assert "No data" in health.headline


def test_disconnected_outranks_everything() -> None:
    health = assess(connected(is_open=False, bytes_received=0, lines_received=0,
                              sentences_decoded=0))
    assert health.level is Level.IDLE


# --------------------------------------------------------------------------
# Command path, reported separately
# --------------------------------------------------------------------------


def test_command_path_unknown_produces_no_note() -> None:
    assert command_path_note("unknown") is None


def test_silent_command_path_is_a_warning() -> None:
    note = command_path_note("silent")
    assert note is not None and note.level is Level.WARN
    # Must say plainly that PMTK writes are futile, and that reading still works.
    assert "PMTK writes will not" in note.detail
    assert "Reading and diagnostics work" in note.detail
    # And must not resurrect the disproved wiring explanation: the host-to-device
    # path was measured working, so blaming the TX line would be a false lead.
    assert "transmit line" not in note.detail
    assert "receive pin" not in note.detail


def test_working_command_path_is_ok() -> None:
    note = command_path_note("working")
    assert note is not None and note.level is Level.OK


def test_command_path_is_independent_of_link_health() -> None:
    """A receiver can stream perfectly and still ignore every command.

    That is exactly the unit on the bench, so the two must not be collapsed
    into a single status.
    """
    health = assess(connected(command_path="silent"))
    assert health.level is Level.OK  # as a receiver, it is fine
    note = command_path_note("silent")
    assert note.is_problem  # as a configurable device, it is not
