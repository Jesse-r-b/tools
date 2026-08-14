# openmanet-node-builder

Console (curses) tool to configure an OpenMANET node - hostname, mesh
gate/point role, HaLow mesh settings, AP radios, battery monitor - and
produce a ready-to-flash factory image named after the node, without
plugging each device into the network to run the setup wizard one at
a time.

## Why this exists

Driving the real setup wizard (`ApplySetup`) live over the network
works, but doesn't scale to configuring several nodes for a mesh test:
each run needs the device already booted and reachable, and the
network-reload step it triggers can interact badly with device
instability that's still being chased down elsewhere. This tool bakes
the *known-good, hardware-verified* result of that wizard directly
into an image instead - see `uci_config.py`'s module docstring for
exactly which scenarios are hardware-verified (`HIGH` confidence) vs
derived from the wizard's own source without an independent hardware
check (`MEDIUM`). WiFi-STA gate uplinks aren't implemented at all -
use the real wizard for that case.

## What it does NOT do

- **Does not run the OpenWrt build system.** It patches an
  *already-built* `.img.gz` (the normal firmware build output) by
  loop-mounting it, writing config files, and re-gzipping - seconds,
  not a 10+ minute rebuild. Build the base firmware image the normal
  way first (`make -j$(nproc)` in the firmware repo).
- **Does not replace the real wizard.** For scenarios this tool
  doesn't support (WiFi-STA uplink) or when you need to be certain
  you're getting the actual current wizard logic, drive `ApplySetup`
  over the network instead.

## Usage

```bash
cd /home/jesse/Src/tools/openmanet-node-builder
sudo ./node_builder.py
```

Root is required for building images (loop mounts) and writing SD
cards (raw block device access). Profile editing alone works without
it.

Flow: **New node profile** → configure hostname / role / mesh /
AP radios / battery → **Save profile** → **Build node image** (pick an
already-built base `.img.gz`) → **Write last built image to SD card**
(hands off to the existing `flash-sdcard.py` tool's own device picker,
confirmation, and write flow - this tool never writes to a block
device itself).

Saved profiles live in `nodes/<hostname>.json` and can be reloaded
from the main menu to rebuild or tweak later.

## Files

- `node_builder.py` - curses TUI + entrypoint.
- `uci_config.py` - generates network/wireless/dhcp/firewall/mesh11sd/
  system + openmanetd's config.yml from a profile. Read the module
  docstring before trusting a scenario you haven't tested.
- `regdb.py` - parses `channels.csv` (a copy of openmanetd's own
  regulatory test fixture) so only valid country/bandwidth/channel
  combinations are offered.
- `image_patcher.py` - loop-mount/inject/re-gzip. Requires root.
- `sdcard_write.py` - thin wrapper that imports and calls
  `flash-sdcard.py`'s existing device-picker/write flow directly,
  rather than duplicating its safety checks.
- `channels.csv` - regulatory data. Re-copy from
  `openmanetd/testfixtures/setup-wizard/channels.csv` if it needs
  updating; never hand-edit it.
- `nodes/` - saved node profiles (JSON), gitignored contents are up to
  you depending on whether you want to version node configs.

## Known limitations

- Only tested against the Raspberry Pi 3 + Wio-WM6108 (Morse Micro
  MM6108) SPI HaLow HAT board - radio hardware paths are hardcoded in
  `node_builder.py` (`RADIO1_PATH`, `RADIO2_PATH`) for that board.
- WiFi-STA gate uplink is not implemented.
- The mesh wifi-iface's own `ssid` option (distinct from `mesh_id`,
  which is what actually matters for 802.11s peer matching) defaults
  to the hostname here; on real hardware it's normally a MAC-derived
  factory default instead. Cosmetic only - doesn't affect mesh
  formation.
