# flash-sdcard

Writes a `.img.gz` disk image (Raspberry Pi OS, OpenWrt, etc.) to an SD card safely:

- Zeros exactly the region the new image will occupy (image size + 1MB margin) before
  writing, so stale partition-table entries or filesystem data left over from a
  previous, differently-sized install can't survive and get picked up again on first
  boot.
- Never lets you target your OS disk - it's excluded from the picker and checked
  again even if selected some other way.
- Always requires you to look at a list of real disks and type a confirmation phrase
  before anything destructive happens. No flag to skip this.

## `flash-sdcard.py` (recommended)

Curses UI: arrow-key device picker, live progress bars for the zero and write
phases. Linux only (uses `lsblk`/`findmnt`).

```bash
sudo python3 flash-sdcard.py path/to/image.img.gz
```

## `flash-sdcard.sh`

POSIX `/bin/sh` fallback for anywhere Python/curses isn't available - same safety
behavior (device picker, OS-disk exclusion, confirmation prompt), plain text instead
of curses. Has basic macOS support via `diskutil` in addition to Linux.

```bash
sudo ./flash-sdcard.sh path/to/image.img.gz
```
