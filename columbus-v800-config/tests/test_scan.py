"""Tests for the port/baud scan.

The probe does real I/O, so a fake serial module stands in for the hardware.
That lets the awkward cases be tested directly: a port that answers at only one
baud rate, a receiver that streams but ignores commands, a port full of noise,
and one that cannot be opened at all.
"""

from __future__ import annotations

import pytest

from v800 import casic, pmtk, protocol
from v800.scan import (
    BAUD_ORDER,
    Outcome,
    ScanResult,
    best,
    classify,
    describe_findings,
    probe_port,
    rank,
)

# One epoch of real V-800 MarkIII traffic, reused as the "it is a GNSS receiver"
# payload. Verbatim from the device, checksums included.
#: Built with computed checksums rather than hand-written ones. Two hand-typed
#: GSV lines here originally carried wrong checksums, and the scanner correctly
#: rejected them -- which looked like a scanner bug and was a fixture bug.
NMEA = b"".join(
    pmtk.build(payload)
    for payload in (
        "GNGGA,042148.000,,,,,0,00,2.8,,,,,,",
        "GNRMC,042148.000,V,,,,,,,130826,,,N,V",
        "GPGSV,1,1,01,05,24,017,,0",
        "GLGSV,1,1,01,74,06,026,,0",
    )
)

NOISE = bytes(range(0x80, 0xFF)) * 8


class FakeSerial:
    """Stand-in for ``serial.Serial`` that models a UART honestly.

    ``devices`` maps a port to ``(baud, payload)``. Listening at the matching
    rate yields the payload; listening at any *other* rate yields garbage of a
    comparable size, because a baud mismatch garbles bytes -- it does not
    suppress them. Getting this wrong matters: an earlier version of this fake
    emitted nothing at mismatched rates, which made a physically impossible
    device (silent at some rates, talkative at others) look testable and would
    have hidden a real bug in the silent-port shortcut.

    A port absent from ``devices`` has nothing attached and is silent at every
    rate.
    """

    def __init__(self, devices=None, answers_pmtk=(), answers_casic=(), fail=()):
        self.devices = dict(devices or {})
        self.answers_pmtk = set(answers_pmtk)
        self.answers_casic = set(answers_casic)
        self.fail = set(fail)
        self.written: list[bytes] = []

    def Serial(self, port, baud, timeout=0.2):  # noqa: N802 - mimics the real API
        if port in self.fail:
            raise OSError(f"could not open {port}")
        return _FakeHandle(self, port, baud)


def _garbled(payload: bytes, seed: int) -> bytes:
    """What a mismatched baud rate looks like: bytes, but not the right ones."""
    return bytes(((b * 7 + seed) % 0x100) | 0x80 for b in payload)


class _FakeHandle:
    def __init__(self, owner: FakeSerial, port: str, baud: int) -> None:
        self._owner = owner
        self._port = port
        self._baud = baud
        entry = owner.devices.get(port)
        if entry is None:
            stream = b""                       # nothing attached
        else:
            device_baud, payload = entry
            stream = payload if baud == device_baud else _garbled(payload, baud & 0xFF)
        self._stream = bytearray(stream)
        self._replies = bytearray()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=1):
        if self._replies:
            out, self._replies = bytes(self._replies[:size]), bytearray(self._replies[size:])
            return out
        out, self._stream = bytes(self._stream[:size]), bytearray(self._stream[size:])
        return out

    def write(self, data):
        self._owner.written.append(data)
        if self._port in self._owner.answers_pmtk:
            if b"PMTK605" in data:
                self._replies.extend(pmtk.build("PMTK705,AXN_0.2,1234,ABCD,"))
            elif b"PMTK000" in data:
                self._replies.extend(pmtk.build("PMTK001,0,3"))
        if self._port in self._owner.answers_casic and data.startswith(casic.SYNC):
            self._replies.extend(casic.build(0x06, 0x04, b"\xe8\x03\x00\x00"))
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self._stream = bytearray()


FAST = dict(listen_s=0.05, command_wait_s=0.05)


# --------------------------------------------------------------------------
# classify() -- the rule, stated once
# --------------------------------------------------------------------------


def test_classify_nothing() -> None:
    assert classify(bytes_seen=0, sentences=0, answered=False) is Outcome.SILENT


def test_classify_bytes_without_nmea() -> None:
    assert classify(bytes_seen=5000, sentences=0, answered=False) is Outcome.NOT_NMEA


def test_classify_nmea_without_command_support() -> None:
    """Streaming NMEA is not evidence that commands will work."""
    assert classify(bytes_seen=5000, sentences=40, answered=False) is Outcome.NMEA_ONLY


def test_classify_configurable_requires_an_answer() -> None:
    assert classify(bytes_seen=5000, sentences=40, answered=True) is Outcome.CONFIGURABLE


def test_outcomes_are_ordered_worst_to_best() -> None:
    assert (
        Outcome.ERROR < Outcome.SILENT < Outcome.NOT_NMEA
        < Outcome.NMEA_ONLY < Outcome.CONFIGURABLE
    )


# --------------------------------------------------------------------------
# probe_port()
# --------------------------------------------------------------------------


def test_finds_a_receiver_at_the_right_baud_and_ignores_the_others() -> None:
    fake = FakeSerial({"/dev/ttyUSB0": (9600, NMEA)})
    result = probe_port(fake, "/dev/ttyUSB0", bauds=BAUD_ORDER, **FAST)
    assert result.outcome is Outcome.NMEA_ONLY
    assert result.baud == 9600
    assert result.sentences == 4


def test_stops_at_the_first_baud_that_decodes() -> None:
    """A receiver speaks one rate; continuing would waste seconds per port."""
    fake = FakeSerial({"/dev/ttyUSB0": (9600, NMEA)})
    result = probe_port(fake, "/dev/ttyUSB0", bauds=(9600, 38400), **FAST)
    assert result.bauds_tried == (9600,)


def test_a_receiver_that_answers_pmtk_is_reported_configurable() -> None:
    fake = FakeSerial({"/dev/ttyUSB0": (9600, NMEA)}, answers_pmtk={"/dev/ttyUSB0"})
    result = probe_port(fake, "/dev/ttyUSB0", bauds=(9600,), **FAST)
    assert result.outcome is Outcome.CONFIGURABLE
    assert result.protocol_kind is protocol.Kind.PMTK
    assert "PMTK705" in result.pmtk_replies
    assert result.firmware.startswith("AXN_0.2")


def test_a_receiver_that_answers_casic_is_reported_configurable() -> None:
    """The V-800 MarkIII. Probing only PMTK reported it unconfigurable, wrongly."""
    fake = FakeSerial({"/dev/ttyUSB0": (9600, NMEA)}, answers_casic={"/dev/ttyUSB0"})
    result = probe_port(fake, "/dev/ttyUSB0", bauds=(9600,), **FAST)
    assert result.outcome is Outcome.CONFIGURABLE
    assert result.protocol_kind is protocol.Kind.CASIC
    assert "CASIC" in result.summary
    assert "cannot be written" not in result.summary


def test_findings_name_the_protocol_that_answered() -> None:
    fake = FakeSerial({"/dev/ttyUSB0": (9600, NMEA)}, answers_casic={"/dev/ttyUSB0"})
    result = probe_port(fake, "/dev/ttyUSB0", bauds=(9600,), **FAST)
    assert "CASIC" in describe_findings([result])


def test_a_receiver_that_answers_nothing_is_reported_read_only() -> None:
    """Only when *no* protocol answers is a receiver genuinely unconfigurable."""
    fake = FakeSerial({"/dev/ttyUSB0": (9600, NMEA)})
    result = probe_port(fake, "/dev/ttyUSB0", bauds=(9600,), **FAST)
    assert result.outcome is Outcome.NMEA_ONLY
    assert result.protocol_kind is protocol.Kind.UNKNOWN
    assert "answers no command protocol" in result.summary


def test_probe_only_ever_sends_pure_queries() -> None:
    """A scan must never change a setting on a device it happens to find."""
    fake = FakeSerial({"/dev/ttyUSB0": (9600, NMEA)}, answers_pmtk={"/dev/ttyUSB0"})
    probe_port(fake, "/dev/ttyUSB0", bauds=(9600,), **FAST)
    for payload in fake.written:
        if payload.startswith(casic.SYNC):
            # A zero-length CASIC CFG message is a poll, not a write.
            frames, _ = casic.parse(payload)
            assert frames and frames[0].payload == b"", payload
            continue
        packet = pmtk.parse(payload.decode().strip())
        assert packet is not None
        # PMTK000 is the test packet; 605 queries the firmware release.
        assert packet.packet_type in (0, 605), payload


def test_a_wholly_silent_port_gives_up_early() -> None:
    """A transmitting device emits bytes at any baud rate, so silence is decisive.

    Sweeping all ten rates on an empty port cost 13 s of a 13.4 s scan when
    measured on real hardware. Zero bytes across two rates ends it.
    """
    fake = FakeSerial()
    result = probe_port(fake, "/dev/ttyUSB9", bauds=BAUD_ORDER, **FAST)
    assert result.outcome is Outcome.SILENT
    assert result.bauds_tried == BAUD_ORDER[:2]
    assert result.gave_up_early
    assert result.baud is None
    assert "nothing is transmitting" in result.summary


def test_a_port_with_any_traffic_gets_the_full_sweep() -> None:
    """A device present but at an unusual rate still emits (garbled) bytes.

    Because those bytes arrive at every rate we listen at, the silent-port
    shortcut never fires and the whole sweep runs - which is what lets the
    shortcut be safe.
    """
    fake = FakeSerial({"/dev/ttyUSB1": (115200, NOISE)})
    result = probe_port(fake, "/dev/ttyUSB1", bauds=BAUD_ORDER, **FAST)
    assert result.bauds_tried == BAUD_ORDER
    assert not result.gave_up_early
    assert result.outcome is Outcome.NOT_NMEA


def test_noise_is_reported_as_not_nmea_rather_than_silent() -> None:
    """Bytes arriving but not decoding is a different problem from silence."""
    fake = FakeSerial({"/dev/ttyUSB1": (9600, NOISE)})
    result = probe_port(fake, "/dev/ttyUSB1", bauds=(9600, 38400), **FAST)
    assert result.outcome is Outcome.NOT_NMEA
    assert result.bytes_seen > 0


def test_unopenable_port_is_an_error_carrying_the_reason() -> None:
    fake = FakeSerial(fail={"/dev/ttyUSB2"})
    result = probe_port(fake, "/dev/ttyUSB2", bauds=(9600,), **FAST)
    assert result.outcome is Outcome.ERROR
    assert "could not open" in result.error


def test_talkers_are_recorded_so_the_device_can_be_recognised() -> None:
    fake = FakeSerial({"/dev/ttyUSB0": (9600, NMEA)})
    result = probe_port(fake, "/dev/ttyUSB0", bauds=(9600,), **FAST)
    assert set(result.talkers) == {"GN", "GP", "GL"}


def test_should_stop_aborts_the_sweep() -> None:
    fake = FakeSerial()
    result = probe_port(
        fake, "/dev/ttyUSB0", bauds=BAUD_ORDER, should_stop=lambda: True, **FAST
    )
    assert result.bauds_tried == ()


# --------------------------------------------------------------------------
# Ranking and reporting
# --------------------------------------------------------------------------


def make(port: str, outcome: Outcome, baud: int | None = 9600) -> ScanResult:
    return ScanResult(port=port, outcome=outcome, baud=baud, sentences=10)


def test_rank_puts_configurable_first() -> None:
    results = [
        make("/dev/ttyS0", Outcome.SILENT, None),
        make("/dev/ttyUSB0", Outcome.NMEA_ONLY),
        make("/dev/ttyUSB1", Outcome.CONFIGURABLE),
    ]
    assert [r.port for r in rank(results)] == ["/dev/ttyUSB1", "/dev/ttyUSB0", "/dev/ttyS0"]


def test_best_prefers_configurable_over_merely_readable() -> None:
    results = [make("/dev/ttyUSB0", Outcome.NMEA_ONLY), make("/dev/ttyUSB1", Outcome.CONFIGURABLE)]
    assert best(results).port == "/dev/ttyUSB1"


def test_best_falls_back_to_a_readable_receiver() -> None:
    assert best([make("/dev/ttyUSB0", Outcome.NMEA_ONLY)]).port == "/dev/ttyUSB0"


def test_best_returns_nothing_when_no_receiver_was_found() -> None:
    assert best([make("/dev/ttyS0", Outcome.SILENT, None)]) is None
    assert best([]) is None


def test_findings_name_the_configurable_device() -> None:
    text = describe_findings([make("/dev/ttyUSB1", Outcome.CONFIGURABLE)])
    assert "configurable" in text and "/dev/ttyUSB1" in text and "9600" in text


def test_findings_are_explicit_that_a_read_only_receiver_cannot_be_written() -> None:
    text = describe_findings([make("/dev/ttyUSB0", Outcome.NMEA_ONLY)])
    assert "answers none of the command protocols" in text
    assert "cannot" in text


def test_findings_when_nothing_was_found_mention_permissions() -> None:
    text = describe_findings([make("/dev/ttyS0", Outcome.SILENT, None)])
    assert "No GNSS receiver found" in text
    assert "dialout" in text


def test_a_read_only_find_is_not_described_as_a_success() -> None:
    """Regression guard: the summary must not claim a configurable device."""
    text = describe_findings([make("/dev/ttyUSB0", Outcome.NMEA_ONLY)])
    assert "configurable" not in text.lower()
