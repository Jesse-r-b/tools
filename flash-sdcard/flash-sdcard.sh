#!/bin/sh
# flash-sdcard.sh - interactively pick an SD card (never the OS disk), zero exactly
# the region a new image will occupy (image size + 1MB margin), then write the image.
#
# Why zero first: dd-ing a new image onto a card that previously held a different
# (often larger, since OpenWrt auto-expands the rootfs/overlay to fill the card on
# first boot) install can leave old partition-table entries or overlay data sitting
# past where the new image's own dd actually writes. That stale data is never "wrong"
# at the filesystem level, so `sync` doesn't help - the new image's first boot can
# find and reuse it, appearing to "retain the old configuration". Zeroing the image's
# own footprint plus a small margin up front removes that leftover data before the
# new partition table/filesystems are written over it. This does NOT require zeroing
# the whole card (slow) - just the part the new image will actually touch.
#
# Why interactive device selection: a raw device path on the command line is one
# typo away from wiping the wrong disk. This script enumerates real disks, hard-
# excludes whatever disk holds your root filesystem, and shows you what's mounted
# where before you confirm - rather than trusting a hand-typed /dev/sdX every time.
#
# Usage:   ./flash-sdcard.sh <image.img.gz> [device]
#   - device omitted: interactive picker (recommended)
#   - device given:   skips the picker, but still refuses your OS disk and still
#                      requires typing 'yes' to confirm - use for scripting/CI
# Example: ./flash-sdcard.sh openmanet-24.10-...-rpi-3-squashfs-factory.img.gz
#
# Requires root (raw block device access). Destroys ALL data on the chosen device.

set -eu

IMAGE="${1:-}"
DEVICE_ARG="${2:-}"

if [ -z "$IMAGE" ]; then
	echo "Usage: $0 <image.img.gz> [device]" >&2
	exit 1
fi

if [ ! -f "$IMAGE" ]; then
	echo "Error: image file not found: $IMAGE" >&2
	exit 1
fi

OS_NAME=$(uname -s)

# Find the whole-disk device holding root, so it can never be offered/selected.
# Best-effort: if this can't be determined (unusual root setups - LVM, overlayfs in
# a container, etc.), fall back to an empty exclusion and lean on the mounted-
# partition listing + explicit confirmation as the safety net instead.
root_disk_linux() {
	root_src=$(findmnt -no SOURCE / 2>/dev/null) || return 0
	[ -b "$root_src" ] || return 0
	pk=$(lsblk -no PKNAME "$root_src" 2>/dev/null) || return 0
	if [ -n "$pk" ]; then
		echo "$pk"
	else
		basename "$root_src"
	fi
}

root_disk_macos() {
	diskutil info / 2>/dev/null | awk -F': *' '/Part of Whole/ {print $2}'
}

case "$OS_NAME" in
Linux) ROOT_DISK=$(root_disk_linux) ;;
Darwin) ROOT_DISK=$(root_disk_macos) ;;
*) ROOT_DISK="" ;;
esac

if [ -z "$ROOT_DISK" ]; then
	echo "Warning: could not automatically identify your OS disk to exclude it." >&2
	echo "Double-check the device you pick below very carefully." >&2
fi

pick_device_linux() {
	tmp=$(mktemp)
	# TYPE=disk only (skip partitions/loop/rom); print size, transport, removable
	# flag, model, and any mounted partitions so the list is actually identifiable.
	lsblk -d -n -o NAME,TYPE | awk '$2=="disk"{print $1}' | while read -r name; do
		[ "$name" = "$ROOT_DISK" ] && continue
		size=$(lsblk -dn -o SIZE "/dev/$name" 2>/dev/null)
		tran=$(lsblk -dn -o TRAN "/dev/$name" 2>/dev/null)
		rm=$(lsblk -dn -o RM "/dev/$name" 2>/dev/null)
		model=$(lsblk -dn -o MODEL "/dev/$name" 2>/dev/null)
		mounts=$(lsblk -n -o MOUNTPOINT "/dev/$name" 2>/dev/null | grep -v '^$' | tr '\n' ',' | sed 's/,$//')
		[ "$rm" = "1" ] && removable="removable" || removable="NOT removable"
		echo "/dev/$name|${size}|${tran:-?}|${removable}|${model:-?}|${mounts:-none}" >>"$tmp"
	done

	if [ ! -s "$tmp" ]; then
		echo "No candidate disks found (besides what looks like your OS disk)." >&2
		rm -f "$tmp"
		return 1
	fi

	echo "Candidate disks (your OS disk, /dev/${ROOT_DISK:-?}, is excluded):" >&2
	i=1
	while IFS='|' read -r dev size tran removable model mounts; do
		echo "  $i) $dev  size=$size  bus=$tran  $removable  model=$model  mounted=$mounts" >&2
		i=$((i + 1))
	done <"$tmp"
	echo "  m) enter a device path manually" >&2

	line_count=$(wc -l <"$tmp")
	printf "Select a number (or 'm'): " >&2
	read -r choice
	if [ "$choice" = "m" ]; then
		rm -f "$tmp"
		printf "Device path: " >&2
		read -r manual
		echo "$manual"
		return 0
	fi
	# Must be a plain positive integer within range - anything else (empty, letters,
	# out of range) is rejected outright rather than silently falling through to
	# sed's "no address means every line" behavior on a blank/bad $choice.
	case "$choice" in
	'' | *[!0-9]*)
		echo "Invalid selection." >&2
		rm -f "$tmp"
		return 1
		;;
	esac
	if [ "$choice" -lt 1 ] || [ "$choice" -gt "$line_count" ]; then
		echo "Invalid selection." >&2
		rm -f "$tmp"
		return 1
	fi
	chosen=$(sed -n "${choice}p" "$tmp" | cut -d'|' -f1)
	rm -f "$tmp"
	if [ -z "$chosen" ]; then
		echo "Invalid selection." >&2
		return 1
	fi
	echo "$chosen"
}

pick_device_macos() {
	echo "Candidate disks (your OS disk, ${ROOT_DISK:-?}, is excluded):" >&2
	diskutil list 2>/dev/null | grep -E '^/dev/disk' | while read -r line; do
		dev=$(echo "$line" | awk '{print $1}')
		diskname=$(basename "$dev")
		[ "$diskname" = "$ROOT_DISK" ] && continue
		echo "  $line" >&2
	done
	echo "  (macOS: pick the whole disk, e.g. /dev/disk4, not a slice like /dev/disk4s1)" >&2
	printf "Device path: " >&2
	read -r manual
	echo "$manual"
}

if [ -n "$DEVICE_ARG" ]; then
	DEVICE="$DEVICE_ARG"
else
	case "$OS_NAME" in
	Linux) DEVICE=$(pick_device_linux) ;;
	Darwin) DEVICE=$(pick_device_macos) ;;
	*)
		printf "Device path: "
		read -r DEVICE
		;;
	esac
fi

if [ -z "$DEVICE" ]; then
	echo "No device selected. Aborted." >&2
	exit 1
fi

if [ ! -b "$DEVICE" ]; then
	echo "Error: not a block device: $DEVICE" >&2
	exit 1
fi

if [ -n "$ROOT_DISK" ]; then
	chosen_name=$(basename "$DEVICE")
	if [ "$chosen_name" = "$ROOT_DISK" ] || echo "$chosen_name" | grep -q "^${ROOT_DISK}[0-9p]"; then
		echo "Error: $DEVICE looks like it's part of your OS disk (/dev/$ROOT_DISK). Refusing." >&2
		exit 1
	fi
fi

echo "Checking gzip integrity of $IMAGE..."
gzip -t "$IMAGE"

# Actual decompressed byte count (what lands on the card) - more reliable than
# parsing gzip -l's stored-size field, which wraps for files >4GiB (not a concern at
# today's image sizes, but wc -c is just as easy and doesn't have that limit).
echo "Determining uncompressed image size..."
IMAGE_SIZE=$(gzip -dc "$IMAGE" | wc -c)
if [ -z "$IMAGE_SIZE" ] || [ "$IMAGE_SIZE" -le 0 ]; then
	echo "Error: could not determine uncompressed size of $IMAGE" >&2
	exit 1
fi

MARGIN_BYTES=$((1024 * 1024))
ZERO_BYTES=$((IMAGE_SIZE + MARGIN_BYTES))
ZERO_MB=$(((ZERO_BYTES + 1048575) / 1048576)) # round up to whole MB for bs=1M

echo
echo "Image:              $IMAGE"
echo "Uncompressed size:  $IMAGE_SIZE bytes"
echo "Target device:      $DEVICE"
echo "Will zero:           ${ZERO_MB}MB (image size + 1MB margin) on $DEVICE"
echo "Then write:          $IMAGE to $DEVICE"
echo
echo "*** This is IRREVERSIBLE and destroys ALL data on $DEVICE. ***"
command -v lsblk >/dev/null 2>&1 && lsblk "$DEVICE" 2>/dev/null
printf "Type 'yes' to continue: "
read -r CONFIRM
if [ "$CONFIRM" != "yes" ]; then
	echo "Aborted."
	exit 1
fi

# Unmount anything currently mounted from this device - dd against a mounted
# filesystem risks corruption, and the whole point of this script is a clean write.
if [ "$OS_NAME" = "Linux" ]; then
	for part in $(lsblk -ln -o NAME "$DEVICE" 2>/dev/null | tail -n +2); do
		mp=$(lsblk -n -o MOUNTPOINT "/dev/$part" 2>/dev/null)
		if [ -n "$mp" ]; then
			echo "Unmounting /dev/$part from $mp..."
			umount "/dev/$part" || {
				echo "Error: could not unmount /dev/$part - aborting." >&2
				exit 1
			}
		fi
	done
elif [ "$OS_NAME" = "Darwin" ]; then
	diskutil unmountDisk "$DEVICE" || {
		echo "Error: could not unmount $DEVICE - aborting." >&2
		exit 1
	}
fi

echo "Zeroing first ${ZERO_MB}MB of $DEVICE..."
dd if=/dev/zero of="$DEVICE" bs=1M count="$ZERO_MB" status=progress conv=fsync

echo "Writing $IMAGE to $DEVICE..."
gzip -dc "$IMAGE" | dd of="$DEVICE" bs=4M status=progress conv=fsync

sync

echo
echo "Done. Safely eject $DEVICE before removing the card, e.g.:"
echo "  eject $DEVICE                     (Linux)"
echo "  diskutil eject $DEVICE            (macOS)"
echo "  udisksctl power-off -b $DEVICE    (Linux, most thorough)"
