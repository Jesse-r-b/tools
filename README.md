# tools

Personal collection of small standalone utilities, one per folder.

- [`flash-sdcard/`](flash-sdcard/) - safely write a `.img.gz` disk image to an SD card
  (curses UI, always confirms, never touches your OS disk).
- [`columbus-v800-config/`](columbus-v800-config/) - configure and diagnose a Columbus
  V-800 MarkIII USB GNSS receiver (PySide6). Sky view, NMEA output control, update
  rate, constellations; speaks the receiver's actual CASIC protocol, not the
  MediaTek one it is usually assumed to use.
- [`hackrf-tv-scanner/`](hackrf-tv-scanner/) - scan RF spectrum with a HackRF for
  analogue TV carriers (NTSC/PAL/SECAM) and demodulate video + audio in real time.

## License

[MIT](LICENSE). Use them, change them, ship them; just keep the copyright notice.

Chosen for compatibility rather than ideology: these tools link libraries under
LGPL-3.0 (Qt, via PySide6) and GPL-2-or-later (libhackrf), and MIT combines
cleanly with all of them. Apache-2.0 would not — it is incompatible with
GPL-2.0-only, which both PySide6 and a lot of SDR code offer.

The warranty disclaimer is not boilerplate here. These utilities write disk
images and reconfigure hardware. Read what a tool does before pointing it at
something you care about.

Note that MIT covers *this* source. A binary you build and then redistribute
still has to honour the licences of whatever it links — notably libhackrf, which
is GPL-2-or-later.
