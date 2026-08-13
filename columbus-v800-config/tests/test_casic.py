"""Tests for the CASIC binary protocol.

Every constant and layout in ``v800/casic.py`` was established by measurement
against the hardware rather than from a vendor document. These tests pin the
frames actually captured from the device, so a later "tidy-up" cannot quietly
change what goes on the wire.
"""

from __future__ import annotations

import pytest

from v800 import casic
from v800.casic import Cfg, Class, Frame

# Captured verbatim from /dev/ttyUSB0. Payloads as the receiver sent them.
CFG_RATE_1HZ = bytes.fromhex("e8030000")
CFG_PRT_USB = bytes.fromhex("00ffc008802500 00".replace(" ", ""))
CFG_PRT_PORT1 = bytes.fromhex("0107c00800c20100")


# --------------------------------------------------------------------------
# Framing and checksum
# --------------------------------------------------------------------------


def test_build_matches_the_frame_the_device_accepted() -> None:
    """The exact CFG-RATE poll that produced a reply on the bench."""
    assert casic.build(0x06, 0x04) == bytes.fromhex("bace0000060400000604")


def test_build_round_trips_through_parse() -> None:
    frame = casic.build(0x06, 0x04, CFG_RATE_1HZ)
    frames, consumed = casic.parse(frame)
    assert consumed == len(frame)
    assert len(frames) == 1
    assert frames[0].cls == 0x06 and frames[0].mid == 0x04
    assert frames[0].payload == CFG_RATE_1HZ
    assert frames[0].checksum_ok


def test_checksum_is_seeded_with_the_header_words() -> None:
    """Not a plain payload sum: the class, id and length seed it."""
    assert casic.checksum(0x06, 0x04, b"") == (0x04 << 24) + (0x06 << 16)
    assert casic.checksum(0x06, 0x04, b"") != 0


def test_checksum_pads_a_partial_final_word() -> None:
    """A payload that is not a multiple of four is zero-padded for the sum.

    Hand-computed: seed = (id << 24) + (class << 16) + len
                        = 0x01000000 + 0x00060000 + 3 = 0x01060003
                   word = b"\\x4e\\x01\\x01\\x00" little-endian = 0x0001014E
                   total = 0x01070151

    Note the padded 3-byte payload does *not* equal the 4-byte one: the length
    goes into the seed, so they legitimately differ by one.
    """
    assert casic.checksum(0x06, 0x01, b"\x4e\x01\x01") == 0x01070151
    assert casic.checksum(0x06, 0x01, b"\x4e\x01\x01") != casic.checksum(
        0x06, 0x01, b"\x4e\x01\x01\x00"
    )


def test_corrupt_frame_is_returned_flagged_not_dropped() -> None:
    """Corrupt traffic and absent traffic are different faults."""
    frame = bytearray(casic.build(0x06, 0x04, CFG_RATE_1HZ))
    frame[-1] ^= 0xFF
    frames, _ = casic.parse(bytes(frame))
    assert len(frames) == 1
    assert not frames[0].checksum_ok


def test_parse_reports_bytes_consumed_so_a_partial_tail_survives() -> None:
    """A frame split across two reads must not be lost."""
    whole = casic.build(0x06, 0x04, CFG_RATE_1HZ)
    stream = whole + whole[:6]
    frames, consumed = casic.parse(stream)
    assert len(frames) == 1
    assert stream[consumed:] == whole[:6]


def test_parse_finds_frames_embedded_in_nmea() -> None:
    """Binary frames arrive interleaved with the NMEA stream."""
    stream = b"$GNGGA,1,2,3*00\r\n" + casic.build(0x06, 0x04, CFG_RATE_1HZ) + b"$GNRMC,x*00\r\n"
    frames, _ = casic.parse(stream)
    assert len(frames) == 1 and frames[0].checksum_ok


def test_parse_ignores_a_sync_pattern_without_a_valid_frame() -> None:
    frames, _ = casic.parse(b"\xba\xce")
    assert frames == []


def test_looks_like_casic_requires_a_valid_checksum() -> None:
    """Sync bytes alone can occur in noise; a valid checksum effectively cannot."""
    assert casic.looks_like_casic(casic.build(0x06, 0x04, CFG_RATE_1HZ))
    assert not casic.looks_like_casic(b"\xba\xce\x00\x00\x06\x04\xde\xad\xbe\xef")
    assert not casic.looks_like_casic(b"$GNGGA,1,2,3*00\r\n")


# --------------------------------------------------------------------------
# ACK / NACK
# --------------------------------------------------------------------------


def test_ack_and_nack_are_distinguished() -> None:
    ack = Frame(int(Class.ACK), 0x01, b"\x06\x01\x00\x00", True)
    nack = Frame(int(Class.ACK), 0x00, b"\x0a\x04\x00\x00", True)
    assert ack.is_ack and not ack.is_nack
    assert nack.is_nack and not nack.is_ack
    assert "NACK" in casic.describe(nack)


# --------------------------------------------------------------------------
# CFG-RATE -- verified by writing and measuring the cadence
# --------------------------------------------------------------------------


def test_parse_fix_interval_from_the_captured_payload() -> None:
    """The device reported 1000 ms while emitting a measured 1.00 Hz."""
    frame = Frame(int(Class.CFG), int(Cfg.RATE), CFG_RATE_1HZ, True)
    assert casic.parse_fix_interval(frame) == 1000


def test_set_fix_interval_encodes_little_endian_uint16() -> None:
    frames, _ = casic.parse(casic.set_fix_interval(200))
    assert frames[0].payload[:2] == b"\xc8\x00"
    assert casic.parse_fix_interval(frames[0]) == 200


def test_set_fix_interval_range_checked() -> None:
    with pytest.raises(ValueError):
        casic.set_fix_interval(0)
    with pytest.raises(ValueError):
        casic.set_fix_interval(70000)


# --------------------------------------------------------------------------
# CFG-MSG -- every sentence id verified individually against the hardware
# --------------------------------------------------------------------------


def test_every_named_sentence_id_is_one_that_was_verified() -> None:
    """These eight were each silenced and restored on the device, one at a time."""
    assert casic.NMEA_MESSAGES == {
        0x00: "GGA", 0x01: "GLL", 0x02: "GSA", 0x03: "GSV",
        0x04: "RMC", 0x05: "VTG", 0x08: "ZDA", 0x11: "TXT",
    }


def test_set_sentence_rate_builds_the_frame_that_worked() -> None:
    """Turning GLL off is the exact frame that suppressed GLL on the bench."""
    assert casic.set_sentence_rate("GLL", 0) == casic.build(0x06, 0x01, b"\x4e\x01\x00\x00")
    assert casic.set_sentence_rate("VTG", 1) == casic.build(0x06, 0x01, b"\x4e\x05\x01\x00")


def test_set_sentence_rate_is_case_insensitive() -> None:
    assert casic.set_sentence_rate("gsv", 5) == casic.set_sentence_rate("GSV", 5)


def test_set_sentence_rate_rejects_an_unknown_sentence() -> None:
    """Better to fail than to address an id whose meaning was never verified."""
    with pytest.raises(ValueError, match="unknown NMEA sentence"):
        casic.set_sentence_rate("GRS", 1)


def test_collect_sentence_rates_from_a_real_dump() -> None:
    """The zero-length poll returns one frame per message; only NMEA is kept."""
    dump = [
        Frame(0x06, 0x01, bytes([0x4E, 0x00, 1, 0]), True),   # GGA every fix
        Frame(0x06, 0x01, bytes([0x4E, 0x03, 5, 0]), True),   # GSV every 5th
        Frame(0x06, 0x01, bytes([0x4E, 0x01, 0, 0]), True),   # GLL off
        Frame(0x06, 0x01, bytes([0x01, 0x00, 1, 0]), True),   # binary NAV - not NMEA
        Frame(0x06, 0x01, bytes([0x4E, 0x99, 1, 0]), True),   # unverified id
    ]
    assert casic.collect_sentence_rates(dump) == {"GGA": 1, "GSV": 5, "GLL": 0}


def test_message_rate_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        casic.set_message_rate(casic.NMEA_CLASS, 0x00, 999)


# --------------------------------------------------------------------------
# CFG-PRT
# --------------------------------------------------------------------------


def test_parse_port_config_from_the_captured_payloads() -> None:
    """Port 0 reported 9600 - the rate the link was demonstrably open at."""
    usb = casic.parse_port_config(Frame(0x06, 0x00, CFG_PRT_USB, True))
    assert usb.port_id == 0 and usb.baud == 9600
    other = casic.parse_port_config(Frame(0x06, 0x00, CFG_PRT_PORT1, True))
    assert other.port_id == 1 and other.baud == 115200


def test_set_port_baud_preserves_every_undocumented_field() -> None:
    """Only the baud word changes; the protocol and mode bits are echoed back.

    Guessing at undocumented bits in the message controlling the port you are
    talking over is the one mistake that cannot be undone over that port.
    """
    current = casic.parse_port_config(Frame(0x06, 0x00, CFG_PRT_USB, True))
    frames, _ = casic.parse(casic.set_port_baud(current, 115200))
    new = casic.parse_port_config(frames[0])
    assert new.baud == 115200
    assert new.port_id == current.port_id
    assert new.protocol_mask == current.protocol_mask
    assert new.mode == current.mode


def test_set_port_baud_rejects_an_unsupported_rate() -> None:
    current = casic.parse_port_config(Frame(0x06, 0x00, CFG_PRT_USB, True))
    with pytest.raises(ValueError):
        casic.set_port_baud(current, 31250)


# --------------------------------------------------------------------------
# Safety
# --------------------------------------------------------------------------


def test_reset_and_save_ids_are_not_exposed() -> None:
    """0x02 and 0x09 are reset and save/clear in every protocol of this family.

    Without a document confirming the payload, a mistake there is not
    recoverable over the wire, so this module offers no way to send them.
    """
    values = {int(c) for c in Cfg}
    assert 0x02 not in values
    assert 0x09 not in values
