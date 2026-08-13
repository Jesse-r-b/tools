# columbus-v800-config

Configuration and diagnostics GUI for the **Columbus V-800 MarkIII** USB GNSS
receiver — a multi-constellation module (GPS + GLONASS + BeiDou + QZSS) behind a
Prolific PL2303 USB-serial bridge. Python 3.11+, PySide6, pyserial.

Live sky view and signal bars, per-sentence NMEA output control, update rate,
constellation selection, navigation dynamic model, link diagnostics, TTFF
timing, a port scanner and a raw protocol console.

The interesting part is that **this receiver is not the MediaTek part everyone
assumes it is.** It ignores PMTK entirely and answers the CASIC/Allystar binary
protocol. There is no vendor document for it, so the command set here was
established by probing the hardware and is documented as such — see
[docs/protocol-investigation.md](docs/protocol-investigation.md). The tool
detects which protocol a receiver speaks and uses that one, so it works against
a genuine MediaTek unit too.

## Running it

No install needed — PySide6 and pyserial are the only dependencies:

```bash
./v800-config
```

That launcher works from any directory. From inside the repo, `python3 -m v800`
does the same thing.

Debian's Python is [PEP 668](https://peps.python.org/pep-0668/) "externally
managed", so `pip install -e .` fails with *externally-managed-environment*.
That is expected and nothing here needs it. If you do want an isolated
environment, reuse the already-installed Qt rather than downloading it again:

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .
.venv/bin/v800-config
```

If the dependencies are ever missing, `pip install --user PySide6 pyserial`
installs them to `~/.local` without touching the system Python.

---

## Read this first: this receiver does not speak PMTK

**The Columbus V-800 MarkIII is not a MediaTek receiver.** It ignores every PMTK
command, parses u-blox UBX framing as a compatibility stub, and answers the
**CASIC/Allystar binary protocol** (`BA CE` frames).

That was established by probing every plausible GNSS command protocol — the full
evidence, including the ruled-out driver and wiring explanations, is in
[docs/protocol-investigation.md](docs/protocol-investigation.md). Summary:

| Protocol | Result |
|---|---|
| MediaTek PMTK | nothing, ever — not even a `PMTK001` "unsupported" reply |
| u-blox UBX | correctly-checksummed `ACK-NAK` to all 318 class/id polls |
| **CASIC binary** | **real config data + ACK** — `CFG-PRT`, `CFG-RATE`, `CFG-MSG`, `CFG-NAVX` |

The UBX reply proves the host-to-device path works: the device cannot produce a
valid ACK-NAK echoing our class and id without receiving our bytes. Writing
`CFG-RATE` visibly changed the fix rate, confirming it is genuinely
configurable — over CASIC, not PMTK.

> An earlier version of this README claimed the receiver accepted no commands at
> all, probably because the PL2303's transmit line was not wired to the module.
> **That was wrong** — it came from testing only PMTK.

### The tool speaks CASIC

It no longer assumes. On connect it **probes both protocols and uses whichever
answers** (`v800/protocol.py`), so the same binary works against a real MT3333
and against this receiver. Detection probes are pure queries.

What works over CASIC, each verified by writing to the device and observing the
change:

| Operation | Message | Verified by |
|---|---|---|
| Fix rate | `CFG-RATE` `0x06/0x04` | writing 200 ms and measuring the cadence, then restoring |
| NMEA sentence rates | `CFG-MSG` `0x06/0x01` | silencing each of the 8 sentences individually and restoring |
| Port baud | `CFG-PRT` `0x06/0x00` | port 0 reporting 9600 against the rate actually connected |
| Constellations | `$PCAS04` | all seven masks set, GSV talkers observed each time; read back at `CFG-NAVX[13]` |
| Navigation dynamic model | `$PCAS11` | values 0-8 written and read back at `CFG-NAVX[4]`; 9 clamps to 8 |

`$PCAS` ASCII commands are **honoured but never acknowledged** — an early probe
concluded the opposite because it only sent a query and waited for a reply.
There is still a read-back: both settings land in `CFG-NAVX`, found by sending a
`$PCAS` command and diffing every CFG payload before and after. Constellation
changes also show in the tracked list, though re-enabling takes minutes to
reappear (the receiver reacquires from scratch) while disabling shows in
seconds.

**Not available on this firmware:** host-supplied position/time aiding and
almanac upload. `AID-INI` decodes correctly but is output-only — a 20 km
position write was ignored, as were twelve validity-flag hypotheses. No almanac
message exists in the `0x0B` class.

Panes CASIC cannot serve — Datum, Power, Aiding, and the SBAS/DGPS/QZSS half of
Constellations — **disable themselves and say why**, naming the protocol and the
missing operation, rather than offering controls that silently do nothing. Those
are absent because no message for them has been identified on this device, not
because they are known impossible. `$PCAS10` restart was tried and produced no
observable restart, so it is deliberately not claimed.

`CFG` ids `0x02` and `0x09` are deliberately unreachable: in every protocol of
this family those are reset and save/clear-configuration, and without a document
confirming the payload a mistake there is not recoverable over the wire.

## Finding a device

**Find receivers…** (Ctrl+F, or the button in the connection bar) opens every
serial port, tries each baud rate until NMEA decodes, and then asks the receiver
a harmless question to establish whether it answers commands at all. Results are
ranked best-first:

| Result | Meaning |
|---|---|
| **GNSS receiver — configurable** | Streams NMEA *and* answers PMTK. This is what you want |
| **GNSS receiver (read-only)** | Streams NMEA but ignores commands — readable, not writable |
| **Data, but not NMEA** | Something is transmitting, but it is not a GNSS receiver |
| **Nothing there** | Not one byte at any rate |
| **Could not open** | Busy, or you are not in the `dialout` group |

The middle distinction is the point. A scan that stopped at "found a GNSS
receiver" would report success for a device nothing can be written to — which is
exactly what is plugged in here.

The scan is read-only: the only things it ever transmits are `PMTK605` and
`PMTK000`, both pure queries. It never changes a setting on a device it finds.

Legacy `ttyS*` ports are skipped by default (this machine exposes 32 of them,
almost all empty) — there is a checkbox to include them. A port that produces
*no bytes at all* is abandoned after two rates rather than ten: a baud mismatch
garbles bytes, it does not suppress them, so silence is decisive. That took a
real scan here from 13.4 s to 5.3 s with identical verdicts.

## Telling connected from working

The banner across the top says which of these you have, because they all look
alike from the Connect button and each needs a different fix:

| Banner | What it means |
|---|---|
| **Not connected** | Resting state, not a fault |
| **No data from the port** | Not one byte arrived — nothing is transmitting; check power and port |
| **Data is arriving, but it is not valid NMEA** | Bytes flowing, nothing decodes — almost always the wrong baud rate; press Detect |
| **Receiver has stopped sending** | Was working, went quiet — power-saving mode, standby, or the cable |
| **Corrupted data on the link** | Checksums failing — usually a saturated port |
| **Antenna fault: OPEN / SHORT** | The receiver reports this itself; no fix is possible until fixed |
| **Connected, but no satellites in view** | Decoding fine, sky empty — indoors or antenna disconnected |
| **Satellites in view, but none tracked** | Knows where they are, cannot lock — siting or interference |
| **Acquiring — no fix yet** | Normal; needs four satellites for 3D |
| **3D fix** | Working, with the satellite counts and HDOP |

Whether the receiver *answers commands* is reported on a separate line, because
a unit that streams perfectly and ignores every command is a healthy receiver
and a useless configurable device — collapsing those into one status hides
whichever half you were not looking at.

On connect the tool runs all ten queries the chipset can answer, paced so as not
to overrun its input buffer, and reports **how many were answered** — not how
many were sent. A receiver that ignores commands produces "None of 10 queries
were answered", rather than a pane full of defaults that look like they came
from the device.

The tool is built around one rule, and this investigation is why it matters:

> **Show what the receiver reports, not what it was told.**

Every write is followed by the matching query, and each pane shows a status pill
reading *not read* / *edited* / *written — awaiting read-back* / *confirmed* /
*failed*. An unanswered command raises "no acknowledgement … the setting may not
have applied" rather than silently looking successful. **Diagnostics → Check
command path** settles the question in three seconds.

That rule is the only reason the wrong-protocol problem was visible at all: the
panes reported "no acknowledgement" instead of displaying defaults that looked
like they had come from the device.

---

## What it does

| Tab | Covers |
|---|---|
| **Navigation** | Sky plot, C/N0 bars, full fix readout, satellite table |
| **Rate & Output** | Fix interval (PMTK220/300), per-sentence divisors (PMTK314/514), port baud (PMTK251), live link-budget estimate |
| **Constellations** | GNSS search mode (PMTK353), SBAS (PMTK313), DGPS (PMTK301), QZSS (PMTK351/352), interference cancellation (PMTK286), static navigation (PMTK386) |
| **Datum** | All 223 datums (PMTK330/430) with filter, plus the user-defined ellipsoid (PMTK331/431) |
| **Power** | Periodic and AlwaysLocate modes (PMTK225), DEE tuning (PMTK223), standby (PMTK161) |
| **Aiding & Restart** | Hot/warm/cold/full-cold restart, flash-aid clear, time and position aiding (PMTK335/740/741), ephemeris and almanac inventory (PMTK660/661), EPO check (PMTK607) |
| **Diagnostics** | Firmware identity (PMTK605/705), antenna status, link health, TTFF timing, RF test mode (PMTK810–815), jamming scan (PMTK837), TCXO drift (PMTK589) |
| **Console** | Raw traffic with TX/RX colouring, command sender with history, and the full PMTK reference |

Every packet type in the MT3333 specification is implemented — all 52 in
section 2.3, all reachable from the GUI.

Settings that can be verified round-trip to a JSON profile (**File → Save /
Load**). Power mode and the static-navigation threshold are deliberately
excluded: the chipset offers no query for them, and saving an unverifiable value
would present a guess as a record.

---

## Things measured on real hardware

Three findings from the unit on `/dev/ttyUSB0`, each now pinned by a test in
`tests/test_real_device.py` using verbatim captured sentences:

1. **It came up at 9600 baud**, not the 38400 the V-800 specification page
   advertises. Use **Detect** if the link is silent.

2. **GSV sentences carry an NMEA 4.10 trailing signal-ID field.** Striding over
   the fields four at a time reads that lone trailing field as a fifth
   satellite — this produced **seven phantom satellites with PRN 0** in the sky
   view and inflated every count.

3. **QZSS satellites are reported under the `GP` talker, not `GQ`.** Trusting
   the talker ID labelled PRNs 194, 195 and 199 as GPS. The receiver also emits
   the GSA system-ID field, so satellites used in the fix are matched on
   *(constellation, PRN)* — GPS 5 and BeiDou 5 are different satellites.

Neither (2) nor (3) would have failed loudly. Both quietly corrupt the display.

The device also reports antenna state unprompted via `GPTXT` (`ANTENNA OK` /
`OPEN` / `SHORT`); that is surfaced on the Diagnostics tab.

---

## The specification is wrong in seven places

`docs/spec-errata.md` lists every defect found while transcribing the *MT3333
Platform NMEA Message Specification* V1.00 — four wrong checksums on its own
printed examples, a PMTK514 example with 18 fields where the text says 19, a
datum count that disagrees with its own appendix, and a `PMTK352` parameter
table that contradicts both the examples above it and the packet's own name.

`tests/test_spec_examples.py` pins each one, so nobody "corrects" this tool into
agreement with the printed document.

The `%g` format specifier was also caught corrupting the user datum: a
semi-major axis of 6377397.155 m went out as `6.3774e+06`. `pmtk.format_number`
exists because of that, and is tested.

---

## How it is put together

The protocol layers are pure — bytes in, bytes out, no serial port, no Qt — so
the whole command surface is testable without hardware. Everything that touches
a port lives in `device.py`, and everything that touches a pixel lives in `ui/`.

```
v800/
  casic.py      CASIC/Allystar binary + $PCAS. What this receiver speaks.
  pmtk.py       MediaTek PMTK, per the MT3333 specification. A different chipset.
  protocol.py   Picks between them by probing, and declares what each can do.
  nmea.py       NMEA-0183 decoding: GSV reassembly, multi-constellation, GPTXT.
  datums.py     223 geodetic datums (PMTK only).
  health.py     Turns measured link state into a plain-English verdict.
  scan.py       Port/baud sweep that reports which protocol each device answers.
  device.py     Serial transport, reader thread, framing, protocol adoption.
  ui/           One module per tab, plus the sky view and shared widgets.
```

Three ideas carry most of the design:

**`protocol.py` asks rather than assumes.** Assuming PMTK from the model number
is what made early versions useless against this hardware. Each protocol
declares a set of `Capability` values, and a pane that needs one it does not have
disables itself and says which protocol is active and what is missing — instead
of writing commands into the void.

**`health.py` is pure and ordered outside-in.** Port open → bytes arriving →
bytes decoding → satellites visible → satellites tracked → fix. The first
failure wins, because an inner symptom is meaningless while an outer stage is
broken. Being Qt-free means states that are awkward to stage on real hardware — a
shorted antenna, a saturated port — are still tested.

**Docstrings distinguish measured from inferred.** There is no vendor document
for this receiver, so `casic.py` records for each field whether it was verified
by *writing* it and watching the device change, or merely read back and
interpreted. Only the former are wired to controls that write.

## Tests

```bash
python -m pytest
```

255 tests, no hardware required. Three groups are worth knowing about:

- `test_spec_examples.py` pins seven defects in the MediaTek specification, so
  nobody "corrects" the code into agreement with the printed document.
- `test_real_device.py` replays verbatim sentences captured from the receiver,
  including the two that silently corrupted the sky view.
- `test_scan.py` drives the port scanner through a fake serial module that
  garbles bytes at mismatched baud rates, because an earlier fake emitted
  *nothing* — a physically impossible device that hid a real bug.

## Notes

- On Linux your user must be in the `dialout` group to open the port.
- `PMTK251` baud changes revert on a full cold start or on entering standby, so
  the port speed can change without you asking. The tool reopens and verifies
  after a change, and says so if nothing decodes.
- Static navigation (`PMTK386`) fabricates a stationary position below its
  threshold. Leave it at 0 for data you intend to analyse.
