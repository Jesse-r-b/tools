# docs

| File | What it is |
|---|---|
| [`protocol-investigation.md`](protocol-investigation.md) | How the receiver's actual command protocol was identified, and every negative result along the way. Start here. |
| [`spec-errata.md`](spec-errata.md) | Defects found in the MediaTek MT3333 NMEA specification while implementing `v800/pmtk.py`. |

## The MT3333 specification is not included

`v800/pmtk.py` was written from the *MT3333 Platform NMEA Message Specification
for GPS+GLONASS*, V1.00 (2013-09-26). That document is **SIMCom proprietary** and
states plainly that copying it or giving it to others is forbidden, so it is not
committed here and never will be — see `.gitignore`.

It is findable from SIMCom's own module documentation (it ships as the NMEA
message specification for the SIM33ELA). Drop it in this directory as
`MT3333_NMEA_Message_Specification_V1.00.pdf` if you want to follow along with
the section numbers quoted throughout `v800/pmtk.py` and `spec-errata.md`.

Nothing in this repository depends on the file being present. The errata
document quotes only the short fragments needed to identify each defect.

**It describes a different chipset from the one in the V-800 MarkIII.** That is
the whole point of `protocol-investigation.md`.
