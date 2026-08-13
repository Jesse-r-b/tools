# tools

Personal collection of small standalone utilities, one per folder.

- [`flash-sdcard/`](flash-sdcard/) - safely write a `.img.gz` disk image to an SD card
  (curses UI, always confirms, never touches your OS disk).
- [`columbus-v800-config/`](columbus-v800-config/) - configure and diagnose a Columbus
  V-800 MarkIII USB GNSS receiver (PySide6). Sky view, NMEA output control, update
  rate, constellations; speaks the receiver's actual CASIC protocol, not the
  MediaTek one it is usually assumed to use.
