#!/usr/bin/env python3
"""
flash-sdcard.py - curses UI to pick an SD card (never the OS disk), zero exactly the
region a new image will occupy (image size + 1MB margin), then write the image with
a live progress bar.

Why zero first: dd-ing a new image onto a card that previously held a different
(often larger, since OpenWrt/Raspberry Pi OS images often auto-expand the rootfs to
fill the card on first boot) install can leave old partition-table entries or
filesystem data sitting past where the new image's own write actually lands. That
leftover data is never "wrong" at the filesystem level, so a plain sync doesn't help
- the new image's first boot can find and reuse it, appearing to "retain the old
configuration". Zeroing the image's own footprint plus a small margin up front
removes that leftover data before the new partition table/filesystems are written
over it. This does NOT require zeroing the whole card (slow) - just the part the new
image will actually touch.

Why a mandatory interactive picker, always: a raw device path is one typo away from
wiping the wrong disk. This tool enumerates real disks, hard-excludes whatever disk
holds your root filesystem (both as a menu entry and as a safety check on the final
choice), and requires you to actually look at the list and type a confirmation
phrase before anything destructive happens. There is no flag to skip this.

Linux only (uses lsblk/findmnt). Requires root. Destroys ALL data on the chosen disk.

Usage: sudo ./flash-sdcard.py <image.img.gz>
"""
import curses
import gzip
import json
import os
import platform
import subprocess
import sys

CHUNK_SIZE = 4 * 1024 * 1024  # 4MiB
MARGIN_BYTES = 1 * 1024 * 1024
CONFIRM_PHRASE = "yes"


def die(msg):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def get_root_disk():
    """Whole-disk device name (e.g. 'sda') holding '/'. None if it can't be determined -
    callers must treat that as 'exclude nothing automatically, rely on the explicit
    per-choice check plus the confirmation screen' rather than silently trusting it."""
    try:
        src = subprocess.run(["findmnt", "-no", "SOURCE", "/"], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return None
    if not src.startswith("/dev/"):
        return None
    try:
        pk = subprocess.run(["lsblk", "-no", "PKNAME", src], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        pk = ""
    return pk or os.path.basename(src)


def collect_mounts(node, out):
    for mp in node.get("mountpoints") or []:
        if mp:
            out.append(mp)
    for child in node.get("children") or []:
        collect_mounts(child, out)


def list_candidate_disks(root_disk):
    out = subprocess.run(
        ["lsblk", "-J", "-o", "NAME,TYPE,SIZE,TRAN,RM,MODEL,MOUNTPOINTS"],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    candidates = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        name = dev.get("name")
        if root_disk and name == root_disk:
            continue
        mounts = []
        collect_mounts(dev, mounts)
        candidates.append({
            "name": name,
            "path": f"/dev/{name}",
            "size": dev.get("size") or "?",
            "tran": dev.get("tran") or "?",
            "rm": bool(dev.get("rm")),
            "model": dev.get("model") or "unknown",
            "mounts": mounts,
        })
    return candidates


def is_part_of_root_disk(device_path, root_disk):
    if not root_disk:
        return False
    name = os.path.basename(device_path)
    if name == root_disk:
        return True
    # partition naming: sda1, sda2, ... or nvme0n1p1 style
    rest = name[len(root_disk):] if name.startswith(root_disk) else ""
    rest = rest.lstrip("p")
    return rest.isdigit()


def gzip_uncompressed_size(path):
    """Streams through once to get an exact byte count - avoids gzip's own stored-size
    trailer, which is a 32-bit field that wraps for files >4GiB (not a concern at
    today's image sizes, but this is no slower and has no such limit)."""
    size = 0
    with gzip.open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            size += len(chunk)
    return size


def unmount_all_partitions(device_path):
    out = subprocess.run(
        ["lsblk", "-J", "-o", "NAME,MOUNTPOINTS"], capture_output=True, text=True, check=True
    ).stdout
    data = json.loads(out)
    target_name = os.path.basename(device_path)
    for dev in data.get("blockdevices", []):
        if dev.get("name") != target_name:
            continue
        for child in dev.get("children") or []:
            mounts = []
            collect_mounts(child, mounts)
            if mounts:
                part_path = f"/dev/{child['name']}"
                subprocess.run(["umount", part_path], check=True)


# ---------------------------------------------------------------------------
# curses UI
# ---------------------------------------------------------------------------

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title
    curses.init_pair(2, curses.COLOR_YELLOW, -1)   # warnings / non-removable
    curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_CYAN)  # selected row
    curses.init_pair(4, curses.COLOR_RED, -1)      # danger text
    curses.init_pair(5, curses.COLOR_GREEN, -1)    # progress fill


def draw_title(stdscr, text):
    stdscr.addstr(0, 0, text, curses.color_pair(1) | curses.A_BOLD)
    stdscr.addstr(1, 0, "-" * min(len(text), curses.COLS - 1), curses.color_pair(1))


def select_device(stdscr, candidates, root_disk):
    curses.curs_set(0)
    idx = 0
    while True:
        stdscr.erase()
        draw_title(stdscr, "flash-sdcard: choose the SD card to write")
        row = 3
        stdscr.addstr(row, 0, f"Your OS disk (/dev/{root_disk or '?'}) is excluded from this list.")
        row += 2
        for i, c in enumerate(candidates):
            removable = "removable" if c["rm"] else "NOT removable"
            mounts = ", ".join(c["mounts"]) if c["mounts"] else "none"
            line = f'{c["path"]:<12} {c["size"]:>8}  bus={c["tran"]:<5} {removable:<14} {c["model"]:<20} mounted={mounts}'
            attr = curses.color_pair(3) if i == idx else curses.A_NORMAL
            if not c["rm"] and i != idx:
                attr = curses.color_pair(2)
            stdscr.addstr(row, 0, line[: curses.COLS - 1], attr)
            row += 1
        row += 1
        stdscr.addstr(row, 0, "Up/Down: move   Enter: select   q: quit without doing anything")
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")) and idx > 0:
            idx -= 1
        elif key in (curses.KEY_DOWN, ord("j")) and idx < len(candidates) - 1:
            idx += 1
        elif key in (curses.KEY_ENTER, 10, 13):
            return candidates[idx]
        elif key in (ord("q"), 27):
            return None


def confirm_screen(stdscr, image, device, image_size, zero_mb):
    curses.curs_set(1)
    stdscr.erase()
    draw_title(stdscr, "flash-sdcard: confirm")
    row = 3
    stdscr.addstr(row, 0, f"Image:  {image}"); row += 1
    stdscr.addstr(row, 0, f"Size:   {human(image_size)}"); row += 1
    stdscr.addstr(row, 0, f"Device: {device}", curses.A_BOLD); row += 1
    stdscr.addstr(row, 0, f"Will zero the first {zero_mb}MB, then write the image."); row += 2
    stdscr.addstr(row, 0, f"*** THIS IS IRREVERSIBLE. ALL DATA ON {device} WILL BE DESTROYED. ***",
                  curses.color_pair(4) | curses.A_BOLD)
    row += 2
    prompt = f"Type '{CONFIRM_PHRASE}' to continue: "
    stdscr.addstr(row, 0, prompt)
    stdscr.refresh()
    curses.echo()
    try:
        typed = stdscr.getstr(row, len(prompt), 20).decode(errors="replace")
    finally:
        curses.noecho()
        curses.curs_set(0)
    return typed.strip() == CONFIRM_PHRASE


def progress_bar(stdscr, title, done, total, extra=""):
    stdscr.erase()
    draw_title(stdscr, f"flash-sdcard: {title}")
    pct = 0 if total <= 0 else min(100, int(done * 100 / total))
    bar_width = max(10, min(60, curses.COLS - 10))
    filled = int(bar_width * pct / 100)
    bar = "#" * filled + "-" * (bar_width - filled)
    stdscr.addstr(3, 0, f"[{bar}] {pct:3d}%")
    stdscr.addstr(4, 0, f"{human(done)} / {human(total)}")
    if extra:
        stdscr.addstr(5, 0, extra)
    stdscr.refresh()


def do_zero(stdscr, device_path, zero_bytes):
    chunk = b"\x00" * CHUNK_SIZE
    written = 0
    with open(device_path, "wb", buffering=0) as f:
        while written < zero_bytes:
            to_write = min(CHUNK_SIZE, zero_bytes - written)
            f.write(chunk[:to_write])
            written += to_write
            progress_bar(stdscr, "zeroing target region", written, zero_bytes)
        os.fsync(f.fileno())


def do_write_image(stdscr, image_path, device_path, total_size):
    written = 0
    with gzip.open(image_path, "rb") as src, open(device_path, "wb", buffering=0) as dst:
        while True:
            data = src.read(CHUNK_SIZE)
            if not data:
                break
            dst.write(data)
            written += len(data)
            progress_bar(stdscr, "writing image", written, total_size)
        os.fsync(dst.fileno())


def run_app(stdscr, image, image_size, candidates, root_disk):
    init_colors()
    chosen = select_device(stdscr, candidates, root_disk)
    if chosen is None:
        return None
    if is_part_of_root_disk(chosen["path"], root_disk):
        # Shouldn't be reachable (root disk is excluded from candidates), but this is
        # the actual safety backstop, not the menu filtering - keep it regardless.
        stdscr.erase()
        stdscr.addstr(0, 0, f"Refusing: {chosen['path']} looks like your OS disk.", curses.color_pair(4))
        stdscr.addstr(2, 0, "Press any key to exit.")
        stdscr.refresh()
        stdscr.getch()
        return None

    zero_bytes = image_size + MARGIN_BYTES
    zero_mb = -(-zero_bytes // (1024 * 1024))

    if not confirm_screen(stdscr, image, chosen["path"], image_size, zero_mb):
        return None

    stdscr.erase()
    stdscr.addstr(0, 0, f"Unmounting any mounted partitions of {chosen['path']}...")
    stdscr.refresh()
    unmount_all_partitions(chosen["path"])

    do_zero(stdscr, chosen["path"], zero_bytes)
    do_write_image(stdscr, image, chosen["path"], image_size)

    stdscr.erase()
    stdscr.addstr(0, 0, "Done.", curses.color_pair(5) | curses.A_BOLD)
    stdscr.addstr(2, 0, "Press any key to exit.")
    stdscr.refresh()
    stdscr.getch()
    return chosen["path"]


def main():
    if platform.system() != "Linux":
        die("this tool is Linux-only (uses lsblk/findmnt). Use flash-sdcard.sh on other platforms.")
    if len(sys.argv) != 2:
        die(f"Usage: {sys.argv[0]} <image.img.gz>")
    image = sys.argv[1]
    if not os.path.isfile(image):
        die(f"image file not found: {image}")
    if os.geteuid() != 0:
        die("must be run as root (raw block device access)")
    for tool in ("lsblk", "findmnt", "umount"):
        if subprocess.run(["which", tool], capture_output=True).returncode != 0:
            die(f"required tool not found: {tool}")

    print("Checking image integrity and size...")
    try:
        image_size = gzip_uncompressed_size(image)
    except OSError as e:
        die(f"corrupt or invalid gzip file: {e}")
    if image_size <= 0:
        die(f"could not determine uncompressed size of {image}")

    root_disk = get_root_disk()
    candidates = list_candidate_disks(root_disk)
    if not candidates:
        die("no candidate disks found (besides what looks like your OS disk)")

    device = curses.wrapper(run_app, image, image_size, candidates, root_disk)

    if device is None:
        print("Aborted.")
        sys.exit(1)

    print(f"Wrote {image} to {device}.")
    print("Safely eject before removing the card, e.g.:")
    print(f"  eject {device}")
    print(f"  udisksctl power-off -b {device}")


if __name__ == "__main__":
    main()
