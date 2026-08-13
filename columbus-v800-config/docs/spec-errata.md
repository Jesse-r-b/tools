# Errata in the MT3333 NMEA Message Specification V1.00 (2013-09-26)

Found while transcribing the specification into `v800/pmtk.py`. Each item was
checked by computing the NMEA-0183 checksum (XOR of everything between `$` and
`*`) over the spec's own example string. `tests/test_spec_examples.py` re-runs
every one of these checks, so a future edit that "fixes" the code to agree with
the printed document will fail the suite.

Nothing here is a bug in this tool. It is a list of places where the document
cannot be copied verbatim.

## 1. PMTK352 example checksums are swapped (section 2.3.24)

The document prints:

    $PMTK352,0*2B : Enable QZSS function
    $PMTK352,1*2A : Disable QZSS function

The correct checksums are `*2A` for `PMTK352,0` and `*2B` for `PMTK352,1` — the
two are transposed. Changing the last character from `0` to `1` flips exactly
the low bit of the XOR, so the pair must differ by one, and they do; they are
just attached to the wrong lines.

## 2. PMTK352 semantics contradict themselves (section 2.3.24)

Same section. The parameter table says:

| Name | Description |
|------|-------------|
| Enabled | '0': Disable, '1': Enable |

but the examples directly above say `0` **enables** QZSS and `1` **disables**
it, and the packet is named `PMTK_API_SET_STOP_QZSS` — a "stop" flag, where 1
means stop.

Two of the three agree, and they are the two that are specific to this command;
the table row is the generic enable/disable boilerplate reused from PMTK313 and
PMTK351 without editing. **This tool follows the examples and the packet name:
`set_qzss_enabled(True)` emits `$PMTK352,0`.**

The inversion is applied in exactly one place, `pmtk.set_qzss_enabled()`, so no
caller has to remember it. If a particular firmware build turns out to honour
the table instead, that one function is the only thing to change — and the QZSS
row in the Constellations pane will show it, because the pane reads state back
rather than assuming the write took.

## 3. PMTK386 example checksum is wrong (section 2.3.26)

The document prints `$PMTK386,0.4*19`. The correct checksum is `*39`.

## 4. PMTK514 example has 18 fields, not 19 (section 2.3.36)

The document prints:

    $PMTK514,1,1,1,1,1,5,1,1,1,1,1,0,1,1,1,1,1,1*2A

That is 18 data fields, but both section 2.3.19 and section 2.3.36 state the
packet carries 19. The printed checksum `*2A` does not match the printed string
either (it computes to `*37`), so the example is corrupt rather than merely
short. This tool always emits and expects 19 fields.

## 5. The datum count in the prose disagrees with Appendix A

Section 2.3.20 says the receiver supports "219 different datums". Appendix A
enumerates 223, numbered 0–222 with no gaps and no duplicates. `v800/datums.py`
carries the appendix, since that is the table the chipset indexes against.

## 6. PMTK220 and PMTK300 specify different minimum fix intervals

- PMTK220 (section 2.3.11): interval "Must be larger than 100" ms.
- PMTK300 (section 2.3.16): range 100–10000 ms.
- PMTK500 (section 2.3.33), the *reply* to a PMTK400 query: "[ >= 200 ]".

So the reply packet documents a floor twice that of the command that sets it.
This tool enforces the documented range of whichever command it is building and
does not try to reconcile them. Practically: if you set 100 ms and the receiver
reports back 200 ms, that is the chipset disagreeing with its own datasheet, not
a fault in the tool — which is why the Rate pane always re-queries after a write
instead of showing what it just sent.

## 7. PMTK815 scale factors do not reproduce the worked example exactly

Section 2.3.51 gives `$PMTK815,29,16,98,10000,30,4100,0` and reads it back as
phase error 0.98, "TCXO offset/drift(Hz): 10/0.03", CNR mean/sigma 41/0.

Applying the scale factors from the table's own Unit column (0.01 for phase and
both TCXO fields, 0.001 for both CNR fields) gives phase 0.98 ✓, TCXO offset
100.0, TCXO drift 0.30, CNR mean 4.1, CNR sigma 0.0. The prose's "10" and "41"
are consistent with different divisors again (1000 and 100 respectively).

This tool applies the Unit column, and the Diagnostics pane labels these figures
as uncalibrated. Treat them as comparable between runs on the same unit, not as
absolute measurements.
