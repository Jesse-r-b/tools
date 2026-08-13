"""Tests for protocol detection and the capability abstraction.

The point of this layer is that the tool asks the receiver which language it
speaks instead of assuming. These tests cover the assumption that was wrong
(everything is PMTK) and the honesty requirement that replaced it (a pane must
be able to find out it cannot do something).
"""

from __future__ import annotations

import pytest

from v800 import casic, pmtk, protocol
from v800.protocol import Capability, CasicProtocol, Kind, PmtkProtocol, UnknownProtocol


# --------------------------------------------------------------------------
# Identification
# --------------------------------------------------------------------------


def test_identifies_casic_from_a_valid_frame() -> None:
    assert protocol.identify(casic.build(0x06, 0x04, b"\xe8\x03\x00\x00")) is Kind.CASIC


def test_identifies_pmtk_from_a_sentence() -> None:
    assert protocol.identify(pmtk.build("PMTK705,AXN_0.2,1234,ABCD,")) is Kind.PMTK


def test_identifies_nothing_from_plain_nmea() -> None:
    """Navigation output is not evidence of any command protocol."""
    assert protocol.identify(pmtk.build("GNGGA,1,2,3")) is Kind.UNKNOWN
    assert protocol.identify(b"") is Kind.UNKNOWN


def test_casic_wins_over_stray_text_because_its_framing_is_checksummed() -> None:
    buffer = b"$GNGGA,1*00\r\n" + casic.build(0x06, 0x04) + b"$GNRMC,1*00\r\n"
    assert protocol.identify(buffer) is Kind.CASIC


def test_a_corrupt_casic_frame_does_not_count_as_identification() -> None:
    bad = bytearray(casic.build(0x06, 0x04))
    bad[-1] ^= 0xFF
    assert protocol.identify(bytes(bad)) is Kind.UNKNOWN


def test_detection_probes_are_all_pure_queries() -> None:
    """Detection must never change a setting on an unidentified device."""
    for kind, payload, _ in protocol.DETECTION_PROBES:
        if payload.startswith(b"$"):
            sentence = pmtk.parse(payload.decode().strip())
            assert sentence.packet_type in (0, 605), payload
        else:
            frames, _ = casic.parse(payload)
            # A zero-length CFG message is a poll, not a write.
            assert frames and frames[0].payload == b"", payload


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------


def test_casic_declares_only_what_was_verified_on_hardware() -> None:
    """Each of these was proven by writing to the device and observing the change.

    Fix rate via CFG-RATE, sentence rates via CFG-MSG (all eight ids checked
    individually), port baud via CFG-PRT, constellations via $PCAS04 (all seven
    masks checked), navigation mode via $PCAS11 (all nine values read back from
    CFG-NAVX).
    """
    caps = CasicProtocol().capabilities
    assert caps == {
        Capability.FIX_RATE,
        Capability.SENTENCE_RATES,
        Capability.PORT_BAUD,
        Capability.CONSTELLATIONS,
        Capability.NAV_MODE,
    }


def test_casic_does_not_claim_operations_with_no_identified_message() -> None:
    """Claiming these would put controls in front of the user that do nothing.

    RESTART is the pointed one: $PCAS10 was tried and produced no observable
    restart, so it stays unclaimed rather than being assumed to work because
    the command exists in the protocol family.
    """
    casic_proto = CasicProtocol()
    for capability in (
        Capability.DATUM,
        Capability.SBAS,
        Capability.POWER_MODES,
        Capability.RESTART,
        Capability.SELF_TEST,
        Capability.FIRMWARE_VERSION,
    ):
        assert not casic_proto.supports(capability)


def test_pmtk_declares_the_full_mt3333_surface() -> None:
    assert PmtkProtocol().supports(Capability.DATUM)
    assert PmtkProtocol().supports(Capability.POWER_MODES)


def test_missing_reports_exactly_what_cannot_be_done() -> None:
    missing = CasicProtocol().missing(Capability.FIX_RATE, Capability.DATUM)
    assert missing == [Capability.DATUM]


def test_unknown_protocol_can_do_nothing() -> None:
    assert UnknownProtocol().capabilities == frozenset()


def test_unknown_protocol_refuses_loudly_rather_than_sending_nonsense() -> None:
    """A pane that ignores supports() must fail here, not transmit into the void."""
    unknown = UnknownProtocol()
    with pytest.raises(ValueError, match="no command protocol"):
        unknown.set_fix_interval(1000)
    with pytest.raises(ValueError):
        unknown.poll_sentence_rates()


# --------------------------------------------------------------------------
# The operations themselves
# --------------------------------------------------------------------------


def test_casic_fix_interval_produces_a_casic_frame() -> None:
    payload = CasicProtocol().set_fix_interval(200)
    frames, _ = casic.parse(payload)
    assert frames[0].checksum_ok
    assert casic.parse_fix_interval(frames[0]) == 200


def test_pmtk_fix_interval_produces_a_sentence() -> None:
    payload = PmtkProtocol().set_fix_interval(200)
    assert payload.startswith(b"$PMTK220,200*")


def test_casic_sets_sentence_rates_one_frame_each() -> None:
    """CASIC has no combined packet, so a batch is genuinely several frames."""
    frames = CasicProtocol().set_sentence_rates({"GGA": 1, "GLL": 0})
    assert len(frames) == 2
    for frame in frames:
        parsed, _ = casic.parse(frame)
        assert parsed[0].checksum_ok


def test_pmtk_sets_all_sentence_rates_in_one_packet() -> None:
    frames = PmtkProtocol().set_sentence_rates({"GGA": 1, "GLL": 0})
    assert len(frames) == 1
    assert frames[0].startswith(b"$PMTK314,")


def test_the_two_protocols_expose_different_sentence_sets() -> None:
    """CASIC controls TXT; PMTK's 19-field packet has no slot for it."""
    assert "TXT" in CasicProtocol().sentence_names()
    assert "TXT" not in PmtkProtocol().sentence_names()


def test_casic_has_no_version_query() -> None:
    """MON-VER is NACKed by this receiver, so there is nothing honest to send."""
    assert CasicProtocol().poll_version() is None
    assert PmtkProtocol().poll_version() is not None


def test_casic_baud_change_requires_reading_the_port_first() -> None:
    """The undocumented mode bits must be echoed back, so they must be known."""
    proto = CasicProtocol()
    with pytest.raises(ValueError, match="has not been read yet"):
        proto.set_port_baud(115200)

    proto.port_config = casic.parse_port_config(
        casic.Frame(0x06, 0x00, bytes.fromhex("00ffc00880250000"), True)
    )
    frames, _ = casic.parse(proto.set_port_baud(115200))
    assert casic.parse_port_config(frames[0]).baud == 115200


def test_create_returns_the_right_implementation() -> None:
    assert isinstance(protocol.create(Kind.CASIC), CasicProtocol)
    assert isinstance(protocol.create(Kind.PMTK), PmtkProtocol)
    assert isinstance(protocol.create(Kind.UNKNOWN), UnknownProtocol)


# --------------------------------------------------------------------------
# Constellation selection over $PCAS04
# --------------------------------------------------------------------------


def test_constellation_mask_matches_the_bits_verified_on_hardware() -> None:
    """bit 0 = GPS, bit 1 = BeiDou, bit 2 = GLONASS. All five masks were checked."""
    proto = CasicProtocol()
    assert proto.set_constellations(True, False, False) == b"$PCAS04,1*18\r\n"
    assert proto.set_constellations(False, False, True) == b"$PCAS04,2*1B\r\n"
    assert proto.set_constellations(True, False, True) == b"$PCAS04,3*1A\r\n"
    assert proto.set_constellations(False, True, False) == b"$PCAS04,4*1D\r\n"
    assert proto.set_constellations(True, True, False) == b"$PCAS04,5*1C\r\n"
    assert proto.set_constellations(False, True, True) == b"$PCAS04,6*1F\r\n"
    assert proto.set_constellations(True, True, True) == b"$PCAS04,7*1E\r\n"


def test_constellation_mask_refuses_to_disable_everything() -> None:
    """A zero mask leaves the receiver unable to fix, and it will not say so."""
    with pytest.raises(ValueError, match="at least one"):
        CasicProtocol().set_constellations(False, False, False)


def test_constellations_from_mask_round_trips() -> None:
    assert casic.constellations_from_mask(0x05) == {
        "GPS": True, "GLONASS": True, "BeiDou": False
    }


# --------------------------------------------------------------------------
# Navigation mode and the CFG-NAVX read-back
# --------------------------------------------------------------------------


def test_navigation_mode_range_matches_what_the_receiver_accepted() -> None:
    """0-8 were each written and read back at CFG-NAVX[4]; 9 clamped to 8."""
    proto = CasicProtocol()
    for mode in range(0, 9):
        assert proto.set_navigation_mode(mode).startswith(b"$PCAS11,")
    with pytest.raises(ValueError, match="0\\.\\.8"):
        proto.set_navigation_mode(9)


def test_navx_read_back_recovers_both_settings() -> None:
    """CFG-NAVX is the read-back for two commands that are never acknowledged.

    Byte offsets were found by differential probing: send a $PCAS command, diff
    the payload before and after, and see which byte moved.
    """
    payload = bytearray(44)
    payload[casic.NAVX_NAV_MODE] = 3
    payload[casic.NAVX_CONSTELLATIONS] = 0x05
    decoded = casic.parse_navx(casic.Frame(0x06, 0x07, bytes(payload), True))
    assert decoded["nav_mode"] == 3
    assert decoded["constellation_mask"] == 0x05
    assert decoded["constellations"] == {"GPS": True, "GLONASS": True, "BeiDou": False}


def test_navx_ignores_a_short_or_wrong_frame() -> None:
    assert casic.parse_navx(casic.Frame(0x06, 0x07, b"\x00" * 10, True)) is None
    assert casic.parse_navx(casic.Frame(0x06, 0x04, b"\x00" * 44, True)) is None


def test_navx_offsets_are_the_measured_ones() -> None:
    """Pinned: these came from diffing the payload, not from a datasheet."""
    assert casic.NAVX_NAV_MODE == 4
    assert casic.NAVX_CONSTELLATIONS == 13
