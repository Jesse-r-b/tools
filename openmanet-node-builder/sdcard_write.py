"""sdcard_write.py - thin wrapper around the existing flash-sdcard.py
tool's actual disk-selection and write logic, so this project has
exactly one place that ever writes raw bytes to a block device.

Per standing preference, this always uses flash-sdcard.py's own
curses flow (device picker, zero-then-write, gzip integrity check via
gzip_uncompressed_size) rather than re-implementing or shortcutting
any of it - that tool's safety checks (root-disk exclusion, mandatory
typed confirmation, pre-zeroing the target region) are deliberate and
not something to bypass for convenience here.
"""
import curses
import importlib.util
import os

FLASH_SDCARD_PATH = "/home/jesse/Src/tools/flash-sdcard/flash-sdcard.py"


def _load_flash_sdcard():
    spec = importlib.util.spec_from_file_location("flash_sdcard", FLASH_SDCARD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_image_to_sdcard(image_path):
    """Runs flash-sdcard.py's full interactive curses flow (device
    picker -> confirm -> zero -> write) against image_path. Returns the
    device path written, or None if the user aborted. Must be called
    from inside an already-running curses session's caller context
    (i.e. after curses.endwin() if you were in your own curses screen -
    flash-sdcard.py opens its own curses.wrapper) or from a plain
    terminal.
    """
    if not os.path.isfile(image_path):
        raise FileNotFoundError(image_path)
    if os.geteuid() != 0:
        raise PermissionError("writing to an SD card requires root")

    fsc = _load_flash_sdcard()

    print("Checking image integrity and size...")
    image_size = fsc.gzip_uncompressed_size(image_path)
    if image_size <= 0:
        raise ValueError(f"could not determine uncompressed size of {image_path}")

    root_disk = fsc.get_root_disk()
    candidates = fsc.list_candidate_disks(root_disk)
    if not candidates:
        raise RuntimeError("no candidate disks found (besides what looks like your OS disk)")

    device = curses.wrapper(fsc.run_app, image_path, image_size, candidates, root_disk)
    return device
