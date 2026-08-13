"""Which command language a receiver speaks, and a common interface to both.

This tool was originally written on the assumption that a Columbus V-800 MarkIII
is a MediaTek part and speaks PMTK.  It is not, and it does not.  Rather than
swap one hard-coded assumption for another, the receiver is *asked*: both
protocols are probed on connect and whichever answers is used.

The abstraction is deliberately narrow.  It covers only the operations that both
protocols can actually perform on the hardware in front of us, and every
capability is declared rather than assumed, so a pane can grey itself out
honestly instead of writing commands into the void.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

from . import casic, pmtk


class Kind(Enum):
    """The command language."""

    UNKNOWN = "unknown"
    PMTK = "pmtk"
    CASIC = "casic"


class Capability(Enum):
    """An operation a pane may want to perform.

    Declared per protocol so the UI can disable what genuinely cannot be done,
    instead of offering a control that silently achieves nothing.
    """

    FIX_RATE = "fix rate"
    SENTENCE_RATES = "NMEA sentence rates"
    PORT_BAUD = "port baud rate"
    DATUM = "geodetic datum"
    CONSTELLATIONS = "constellation selection"
    SBAS = "SBAS"
    POWER_MODES = "power saving modes"
    RESTART = "restart and aiding"
    FIRMWARE_VERSION = "firmware version"
    SELF_TEST = "RF test mode"
    NAV_MODE = "navigation dynamic model"


class Protocol(ABC):
    """What the rest of the application talks to."""

    kind: Kind = Kind.UNKNOWN
    name: str = "unknown"

    #: Operations this protocol can perform on this hardware.
    capabilities: frozenset[Capability] = frozenset()

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def missing(self, *capabilities: Capability) -> list[Capability]:
        """Which of ``capabilities`` this protocol cannot do."""
        return [c for c in capabilities if c not in self.capabilities]

    # Each of these returns the bytes to transmit, or raises ValueError for an
    # out-of-range argument. Returning None means "this protocol cannot".

    @abstractmethod
    def set_fix_interval(self, interval_ms: int) -> bytes: ...

    @abstractmethod
    def poll_fix_interval(self) -> bytes: ...

    @abstractmethod
    def set_sentence_rates(self, rates: dict[str, int]) -> list[bytes]: ...

    @abstractmethod
    def poll_sentence_rates(self) -> bytes: ...

    @abstractmethod
    def poll_version(self) -> bytes | None: ...

    @abstractmethod
    def sentence_names(self) -> tuple[str, ...]:
        """The sentences this protocol can control, in display order."""

    @abstractmethod
    def rate_choices(self) -> tuple[int, ...]:
        """Legal per-sentence rate divisors."""


class PmtkProtocol(Protocol):
    """MediaTek PMTK, per the MT3333 specification in ``docs/``.

    Correct for an actual MT3333-class receiver. Not what the V-800 MarkIII
    speaks -- see ``docs/protocol-investigation.md``.
    """

    kind = Kind.PMTK
    name = "MediaTek PMTK"
    capabilities = frozenset(
        {
            Capability.FIX_RATE,
            Capability.SENTENCE_RATES,
            Capability.PORT_BAUD,
            Capability.DATUM,
            Capability.CONSTELLATIONS,
            Capability.SBAS,
            Capability.POWER_MODES,
            Capability.RESTART,
            Capability.FIRMWARE_VERSION,
            Capability.SELF_TEST,
        }
    )

    def set_fix_interval(self, interval_ms: int) -> bytes:
        return pmtk.build(pmtk.set_pos_fix(interval_ms))

    def poll_fix_interval(self) -> bytes:
        return pmtk.build(pmtk.query_fix_ctl())

    def set_sentence_rates(self, rates: dict[str, int]) -> list[bytes]:
        # PMTK314 carries every sentence in one packet.
        return [pmtk.build(pmtk.set_nmea_output(rates))]

    def poll_sentence_rates(self) -> bytes:
        return pmtk.build("PMTK414")

    def poll_version(self) -> bytes:
        return pmtk.build(pmtk.query_release())

    def sentence_names(self) -> tuple[str, ...]:
        return tuple(pmtk.NMEA_OUTPUT_FIELDS.values())

    def rate_choices(self) -> tuple[int, ...]:
        return pmtk.NMEA_RATE_CHOICES


class CasicProtocol(Protocol):
    """CASIC/Allystar binary -- what the V-800 MarkIII actually answers.

    The capability set is deliberately short.  Fix rate, sentence rates and port
    baud were each verified by writing to the receiver and observing the change.
    Constellation selection was added later, over ``$PCAS04``: all five
    reachable masks were set and the resulting GSV talkers observed.

    Datum, SBAS, power modes, restart and the test mode are *absent* because no
    message for them has been identified on this device -- not because they are
    known to be impossible.  ``$PCAS10`` restart was tried and produced no
    observable restart, so it is deliberately not claimed.  Claiming any of
    these would put controls in front of the user that quietly do nothing,
    which is the exact failure this rewrite exists to remove.
    """

    kind = Kind.CASIC
    name = "CASIC binary"
    capabilities = frozenset(
        {
            Capability.FIX_RATE,
            Capability.SENTENCE_RATES,
            Capability.PORT_BAUD,
            Capability.CONSTELLATIONS,
            Capability.NAV_MODE,
        }
    )

    def __init__(self) -> None:
        #: Last CFG-PRT seen for the USB port, needed to change baud without
        #: disturbing the undocumented protocol/mode bits.
        self.port_config: casic.PortConfig | None = None

    def set_fix_interval(self, interval_ms: int) -> bytes:
        return casic.set_fix_interval(interval_ms)

    def poll_fix_interval(self) -> bytes:
        return casic.poll_rate()

    def set_sentence_rates(self, rates: dict[str, int]) -> list[bytes]:
        # CASIC sets one message per frame, so this is a batch.
        return [casic.set_sentence_rate(name, rate) for name, rate in sorted(rates.items())]

    def poll_sentence_rates(self) -> bytes:
        return casic.poll_message_rates()

    def poll_version(self) -> None:
        # MON-VER is NACKed by this receiver; there is no version query.
        return None

    def poll_port(self) -> bytes:
        return casic.poll_port()

    def set_constellations(self, gps: bool, glonass: bool, beidou: bool) -> bytes:
        """$PCAS04. Note the receiver does not acknowledge this.

        The only confirmation available is watching which GSV talkers appear,
        which the Constellations pane already shows.
        """
        return casic.set_constellations(gps, glonass, beidou)

    def set_navigation_mode(self, mode: int) -> bytes:
        """$PCAS11. Read back from CFG-NAVX[4]."""
        return casic.set_navigation_mode(mode)

    def poll_navx(self) -> bytes:
        """CFG-NAVX -- carries the navigation mode and constellation mask."""
        return casic.poll_navx()

    def set_port_baud(self, baud: int) -> bytes:
        if self.port_config is None:
            raise ValueError(
                "the port configuration has not been read yet; "
                "read from the device before changing the baud rate"
            )
        return casic.set_port_baud(self.port_config, baud)

    def sentence_names(self) -> tuple[str, ...]:
        return tuple(casic.NMEA_MESSAGES[i] for i in sorted(casic.NMEA_MESSAGES))

    def rate_choices(self) -> tuple[int, ...]:
        return (0, 1, 2, 3, 4, 5)


class UnknownProtocol(Protocol):
    """No command protocol identified: read-only operation.

    Every method raises, so a pane that has ignored ``supports()`` fails loudly
    here rather than transmitting something meaningless.
    """

    kind = Kind.UNKNOWN
    name = "none identified"
    capabilities = frozenset()

    def _refuse(self):
        raise ValueError(
            "no command protocol has been identified for this receiver; "
            "it can be read but not configured"
        )

    def set_fix_interval(self, interval_ms: int) -> bytes:
        self._refuse()

    def poll_fix_interval(self) -> bytes:
        self._refuse()

    def set_sentence_rates(self, rates: dict[str, int]) -> list[bytes]:
        self._refuse()

    def poll_sentence_rates(self) -> bytes:
        self._refuse()

    def poll_version(self) -> None:
        return None

    def sentence_names(self) -> tuple[str, ...]:
        return ()

    def rate_choices(self) -> tuple[int, ...]:
        return (0, 1)


#: Probes used to identify the protocol, in the order they are sent.  Each is a
#: pure query: nothing here changes a setting on an unidentified device.
DETECTION_PROBES: tuple[tuple[Kind, bytes, str], ...] = (
    (Kind.CASIC, casic.DETECT_POLL, "CASIC CFG-RATE poll"),
    (Kind.PMTK, pmtk.build(pmtk.query_release()), "PMTK605 firmware query"),
    (Kind.PMTK, pmtk.build("PMTK000"), "PMTK000 test packet"),
)


def identify(buffer: bytes) -> Kind:
    """Work out which protocol a reply buffer belongs to.

    CASIC is tested first and on a checksum-valid frame, because its binary
    framing is unambiguous.  PMTK is recognised by its sentence prefix.
    """
    if casic.looks_like_casic(buffer):
        return Kind.CASIC
    text = buffer.decode("ascii", errors="replace")
    if any(line.startswith("$PMTK") for line in text.splitlines()):
        return Kind.PMTK
    return Kind.UNKNOWN


def create(kind: Kind) -> Protocol:
    """Instantiate the protocol object for ``kind``."""
    if kind is Kind.CASIC:
        return CasicProtocol()
    if kind is Kind.PMTK:
        return PmtkProtocol()
    return UnknownProtocol()
