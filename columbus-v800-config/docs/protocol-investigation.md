# Why the V-800 MarkIII ignores PMTK

**Answer: it is not a MediaTek receiver.** It parses u-blox UBX framing as a
compatibility stub and answers the **CASIC/Allystar binary protocol**. It has no
PMTK support at all.

An earlier conclusion recorded in this repository — that the receiver accepts no
commands, probably because the PL2303's transmit line is not wired to the
module's receive pin — **was wrong**. It was reached by testing only PMTK. The
host-to-device path works perfectly. Everything below is the evidence.

Investigated 2026-08-13 against the unit on `/dev/ttyUSB0` at 9600 baud.

## Ruling out the boring explanations first

| Check | Result |
|---|---|
| USB descriptor | PL2303 HXD (`bcdDevice 4.00`), bulk OUT endpoint `0x02` present |
| Kernel driver | `pl2303` bound, no errors in `dmesg` |
| Flow control | `rtscts`/`dsrdtr`/`xonxoff` all off — nothing can block a write |
| Modem lines | DTR and RTS asserted; CTS/DSR low but irrelevant with flow control off |
| Port contention | `fuser` clean — nothing else holds the port |
| Write errors | none; `write()` and `flush()` both return normally |

So the bytes reach the bridge. The question was whether anything downstream
listens.

## The decisive test

Probing every plausible GNSS command protocol, using queries and
deliberately-invalid IDs (safe by construction — a chip that speaks a protocol
must answer "unsupported", and one that does not cannot act on it):

| Protocol | Probe | Result |
|---|---|---|
| MediaTek PMTK | `PMTK605`, `PMTK000`, `PMTK999` | **nothing, ever** — not even a `PMTK001` unsupported flag |
| u-blox NMEA | `$PUBX,00/03/04` | nothing |
| SiRF / ST / Furuno / Unicore / SkyTraq | assorted | nothing |
| **u-blox UBX** | 318 class/id polls | **correctly-checksummed `ACK-NAK` to every one** |
| **CASIC binary** | `BA CE` framed polls | **real configuration data + `ACK`** |

The UBX result alone settles the wiring question. A `ACK-NAK` frame carries a
valid Fletcher-8 checksum and echoes back the exact class and id that was
polled. The device could not produce that without receiving our bytes.

## What it actually supports

CASIC frames (`BA CE`, little-endian length, class, id, 32-bit sum checksum):

| Message | Class/ID | Response |
|---|---|---|
| `CFG-PRT` | `0x06/0x00` | 8 bytes — port 0 at 9600, port 1 at 115200 |
| `CFG-MSG` | `0x06/0x01` | 4 bytes |
| `CFG-RATE` | `0x06/0x04` | 4 bytes — measurement interval in ms |
| `CFG-NAVX` | `0x06/0x07` | 44 bytes |
| `0x06/0x03`, `0x06/0x05`, `0x06/0x08`, `0x0B/0x01` | | ACK |

`MON-VER`, `MON-HW` and `NAV-STATUS` are NACKed. `$PCAS` ASCII commands get no
reply — this device is binary-CASIC only.

## Cross-checking the decode against reality

A decode that agrees with itself proves nothing, so both readable values were
checked against independent measurements:

- `CFG-RATE` reported **1000 ms**. The observed sentence cadence, counted off
  the wire, was **1.00 Hz**. ✓
- `CFG-PRT` port 0 reported **9600 baud**. That is the rate the link was
  actually open at. ✓

Then a write, to confirm the device is genuinely configurable:

```
BEFORE  : measured 1.00 Hz
-> CFG-RATE = 200 ms (5 Hz)
AFTER   : measured 1.67 Hz
-> CFG-RATE = 1000 ms (restored)
RESTORED: measured 1.00 Hz, device reports CFG-RATE = 1000 ms
```

The write took effect. It reached only 1.67 Hz rather than 5 Hz because at 9600
baud the full sentence set does not fit the link — the same saturation the Rate
& Output tab's load estimate warns about. The device was returned to exactly the
state it was found in.

## $PCAS: honoured, but never acknowledged

An early probe sent `$PCAS06` (a query), got no reply, and concluded the device
had no `$PCAS` support. It has: it **acts on `$PCAS` silently**. Established by
sending `$PCAS02,500` and measuring the fix cadence change.

That matters for how the UI must behave. A `$PCAS` write cannot be confirmed by
an acknowledgement, only by observing the effect:

| Command | Verified how |
|---|---|
| `$PCAS02` fix interval | cadence changed, restored via binary `CFG-RATE` |
| `$PCAS04` constellations | all five reachable masks set, GSV talkers observed each time |
| `$PCAS10` restart | **no observable restart** — not claimed by the tool |

The constellation mask is bit 0 = GPS, bit 1 = BeiDou, bit 2 = GLONASS, with all
of 1/2/3/4/5/6/7 checked individually against the GSV talkers that appeared.

One timing note worth knowing: **disabling a constellation shows within seconds,
re-enabling can take minutes.** The receiver has to reacquire from scratch, and
indoors that is slow. A read-back that looks like a failed write is often just
an impatient one.

## Mapping the rest by differential probing

`$PCAS` is never acknowledged and most of the CFG payloads are undocumented, so
the two were played off against each other: send a `$PCAS` command, poll every
CFG message before and after, and diff. Whatever byte moved *is* that setting.

That produced two mappings inside the 44-byte `CFG-NAVX` (`0x06/0x07`):

| Offset | Meaning | Established by |
|---|---|---|
| byte 4 | navigation dynamic model | `$PCAS11,2` → 2, `$PCAS11,4` → 4; values 0-8 accepted, 9 clamps to 8 |
| byte 13 | constellation mask | `$PCAS04,1` → 0x01, `$PCAS04,7` → 0x07 |

Byte 9 is deliberately unnamed: it changes between polls on its own, so it is
live state rather than configuration.

This matters beyond the settings themselves. `$PCAS04` and `$PCAS11` are not
acknowledged, so before this there was no way to confirm a write had landed —
now `CFG-NAVX` is a genuine read-back for both.

## Aiding: what is and is not there

`AID-INI` (`0x0B/0x01`) emits 56 bytes and decodes cleanly: three ECEF doubles,
GPS time of week, and the week number at byte 52 (`0x097F` = 2431, correct for
August 2026). The position decoded to lat −33.908, lon 151.197 — the right city,
which is what confirms the layout.

**It is output-only.** Writing a position 20 km away was ignored, as were twelve
attempts with a validity-flag word at each plausible offset (0x01/0x03/0x07/0xFF
at bytes 40, 44 and 48). The receiver reports its aiding state but will not
accept one, so host-supplied position/time aiding is not available on this
firmware.

No almanac message was found either. Class `0x0B` has exactly two live ids:
`0x01` (above) and `0x06`, which acknowledges a poll but never emits and cannot
be verified without a read-back.

## Consequences for this tool

Everything in `v800/pmtk.py` is a faithful implementation of the MT3333 PMTK
specification, and it is the wrong protocol for this hardware. It remains
correct for an actual MT3333-based receiver.

The parts that are **unaffected**, because they read the NMEA stream rather than
issue commands: the Navigation tab, the sky view and signal bars, link health,
antenna status, TTFF timing, the scan, and the whole of `nmea.py`.

**This has since been built.** `v800/casic.py` implements the protocol and
`v800/protocol.py` probes for it on connect, so the tool now uses whichever
language the receiver actually speaks. The Rate & Output tab configures this
device; the panes CASIC cannot serve disable themselves and name the reason.

Everything in `casic.py` was established by measurement, and the docstrings say
which parts were verified by *writing* (fix rate, all eight sentence ids, port
baud) versus merely read back and interpreted (`CFG-NAVX`, the protocol and mode
bits of `CFG-PRT`). Only the former are wired to controls that write.
