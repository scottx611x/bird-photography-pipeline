#!/bin/bash
# pi_image_backup.sh — make a bootable, flashable image of birdpi.
#
# Goal: if the SD card dies, flash this file onto a new card and the Pi comes
# back exactly as it was — birdnet-go, mediamtx, ynab-mcp, birdqueue, the lot.
# Not a file backup you'd have to reassemble by hand.
#
# WHY IT RESTORES CLEANLY
#   /etc/fstab and cmdline.txt both reference PARTUUIDs, which derive from the
#   disk identifier. This image is built with the SAME identifier as the live
#   card (665081e2), so the partitions it creates get the same PARTUUIDs and a
#   restored card boots with no edits. Getting this wrong is the usual reason a
#   restored Pi image drops to an initramfs prompt.
#
# WHAT IS LEFT OUT
#   Audio clips (31G, archived to S3 separately) and old core dumps. That is
#   what turns a 41G card into an ~11G image. Clips restore from S3; the
#   directory is recreated empty so birdnet-go starts cleanly.
#
# RESTORE
#   zstd -d birdpi-YYYY-MM-DD.img.zst
#   then flash with Raspberry Pi Imager ("Use custom") or:
#   sudo dd if=birdpi-YYYY-MM-DD.img of=/dev/diskN bs=4M status=progress conv=fsync
#   The image is smaller than the card; on first boot run
#   `sudo raspi-config --expand-rootfs && sudo reboot` to reclaim the rest.
#
# USAGE
#   sudo ./pi_image_backup.sh [--s3 s3://bucket/prefix] [--class CLASS]
#                             [--dest user@host:/path] [--keep N]
#
#   Default storage class is GLACIER_IR (Glacier Instant Retrieval): about
#   $0.06/month for three ~5GB images, restorable immediately. DEEP_ARCHIVE is
#   cheaper by pennies but takes 12-48h to retrieve, which is the wrong trade
#   when the card has just failed. Note the 90-day minimum billing duration —
#   monthly images suit it; weekly just pays the minimum repeatedly.

set -euo pipefail

DISK_ID="665081e2"                 # must match the live card; see above
SRC_BOOT="/boot/firmware"
BUILD_DIR="${BUILD_DIR:-/var/backups/pi-image}"
BOOT_MB=512
SLACK_MB=1500                      # headroom so the restored fs isn't 100% full
KEEP=3
DEST=""
S3_DEST=""
S3_CLASS="${S3_CLASS:-GLACIER_IR}"

while [ $# -gt 0 ]; do
    case "$1" in
        --dest) DEST="$2"; shift 2 ;;
        --s3) S3_DEST="${2%/}"; shift 2 ;;
        --class) S3_CLASS="$2"; shift 2 ;;
        --keep) KEEP="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

STAMP="$(date +%Y-%m-%d)"
IMG="$BUILD_DIR/birdpi-$STAMP.img"
SNAP="$BUILD_DIR/.dbsnap"
mkdir -p "$BUILD_DIR" "$SNAP"

# Things that must not be copied: kernel virtual filesystems, volatile dirs,
# the bulky data we deliberately shed, and the image we are writing right now.
EXCLUDES=(
    --exclude="/proc/*" --exclude="/sys/*" --exclude="/dev/*"
    --exclude="/run/*"  --exclude="/tmp/*" --exclude="/mnt/*"
    --exclude="/media/*" --exclude="/lost+found"
    --exclude="/var/swap" --exclude="/swapfile"
    --exclude="/var/cache/apt/archives/*"
    --exclude="$BUILD_DIR"
    --exclude="/home/scott/birdnet-go/data/clips/*"
    --exclude="/home/scott/birdnet-go/data/core.*"
)

cleanup() {
    set +e
    mountpoint -q /mnt/piimg/boot/firmware && umount /mnt/piimg/boot/firmware
    mountpoint -q /mnt/piimg && umount /mnt/piimg
    [ -n "${LOOP:-}" ] && losetup -d "$LOOP" 2>/dev/null
    rm -rf "$SNAP"
}
trap cleanup EXIT

# ── 1. consistent snapshots of the live databases ────────────────────────────
# rsync of an open SQLite file can capture a torn write: the main db and its
# WAL are copied at different instants. `.backup` takes a proper snapshot.
echo "→ snapshotting databases"
snap_db() {
    local src="$1" name="$2"
    [ -f "$src" ] || return 0
    if sqlite3 "$src" ".backup '$SNAP/$name'" 2>/dev/null; then
        echo "   $name ($(du -h "$SNAP/$name" | cut -f1))"
    else
        echo "   WARNING: could not snapshot $src; the copy in the image may be torn" >&2
    fi
}
command -v sqlite3 >/dev/null 2>&1 || echo "   sqlite3 missing — databases copied as-is" >&2
snap_db /home/scott/birdnet-go/data/birdnet.db birdnet.db
snap_db /srv/birdqueue/queue.db queue.db

# ── 2. size the image from what will actually be copied ──────────────────────
echo "→ measuring"
USED_MB=$(du -sxm --exclude=/home/scott/birdnet-go/data/clips \
             --exclude='/home/scott/birdnet-go/data/core.*' \
             --exclude="$BUILD_DIR" / 2>/dev/null | cut -f1)
ROOT_MB=$(( USED_MB + SLACK_MB ))
TOTAL_MB=$(( BOOT_MB + ROOT_MB + 8 ))
echo "   root data ${USED_MB}MB → image ${TOTAL_MB}MB"

AVAIL_MB=$(df -Pm "$BUILD_DIR" | awk 'NR==2{print $4}')
[ "$AVAIL_MB" -gt "$TOTAL_MB" ] || {
    echo "not enough free space to build (${AVAIL_MB}MB free, need ${TOTAL_MB}MB)" >&2
    exit 1; }

# ── 3. create and partition, preserving the disk identifier ──────────────────
echo "→ creating image"
rm -f "$IMG"
truncate -s "${TOTAL_MB}M" "$IMG"          # sparse: costs nothing until written

sfdisk --quiet "$IMG" <<EOF
label: dos
label-id: 0x$DISK_ID
unit: sectors
start=8192, size=$(( BOOT_MB * 2048 )), type=c, bootable
start=$(( 8192 + BOOT_MB * 2048 )), type=83
EOF

LOOP=$(losetup --show -fP "$IMG")
mkfs.vfat -F32 -n bootfs "${LOOP}p1" >/dev/null
mkfs.ext4 -q -L rootfs -m 0 "${LOOP}p2"

# Confirm the PARTUUIDs match what fstab expects, before copying 11GB into it.
for part in 01 02; do
    want="$DISK_ID-$part"
    got=$(blkid -s PARTUUID -o value "${LOOP}p${part#0}")
    [ "$got" = "$want" ] || { echo "PARTUUID mismatch: got $got, expected $want" >&2; exit 1; }
done
echo "   PARTUUIDs $DISK_ID-01/-02 confirmed — restored card will boot unmodified"

# ── 4. copy ──────────────────────────────────────────────────────────────────
mkdir -p /mnt/piimg
mount "${LOOP}p2" /mnt/piimg
mkdir -p /mnt/piimg/boot/firmware
mount "${LOOP}p1" /mnt/piimg/boot/firmware

echo "→ copying root filesystem"
# Live progress is useful by hand and useless from a timer, where the
# carriage-return updates turn the journal into hundreds of KB of noise.
if [ -t 1 ]; then RSYNC_PROGRESS=(--info=progress2); else RSYNC_PROGRESS=(); fi
rsync -aHAXx "${RSYNC_PROGRESS[@]}" "${EXCLUDES[@]}" / /mnt/piimg/

echo "→ copying boot partition"
rsync -rtx "$SRC_BOOT/" /mnt/piimg/boot/firmware/

# Overlay the clean database snapshots over whatever rsync grabbed live.
[ -f "$SNAP/birdnet.db" ] && cp -f "$SNAP/birdnet.db" /mnt/piimg/home/scott/birdnet-go/data/birdnet.db
[ -f "$SNAP/queue.db" ]   && cp -f "$SNAP/queue.db"   /mnt/piimg/srv/birdqueue/queue.db
# The WAL/SHM belong to the pre-snapshot database; leaving them would confuse
# SQLite into replaying stale frames over a consistent file.
rm -f /mnt/piimg/home/scott/birdnet-go/data/birdnet.db-wal \
      /mnt/piimg/home/scott/birdnet-go/data/birdnet.db-shm \
      /mnt/piimg/srv/birdqueue/queue.db-wal \
      /mnt/piimg/srv/birdqueue/queue.db-shm

# Recreate the shed directories so services find what they expect.
mkdir -p /mnt/piimg/home/scott/birdnet-go/data/clips \
         /mnt/piimg/proc /mnt/piimg/sys /mnt/piimg/dev \
         /mnt/piimg/run /mnt/piimg/tmp /mnt/piimg/mnt
chmod 1777 /mnt/piimg/tmp
chown --reference=/home/scott/birdnet-go/data /mnt/piimg/home/scott/birdnet-go/data/clips

sync
umount /mnt/piimg/boot/firmware
umount /mnt/piimg
e2fsck -fp "${LOOP}p2" >/dev/null || true
losetup -d "$LOOP"; LOOP=""

# ── 5. compress and ship ─────────────────────────────────────────────────────
echo "→ compressing"
# -6, not -19: on a Pi the higher levels cost hours for a few percent, and the
# image is mostly zeroed free space that compresses well at any level.
zstd -q -T0 -6 --rm "$IMG" -o "$IMG.zst"
SIZE=$(du -h "$IMG.zst" | cut -f1)
echo "   $IMG.zst ($SIZE)"

if [ -n "$S3_DEST" ]; then
    echo "→ uploading to $S3_DEST ($S3_CLASS)"
    # GLACIER_IR, not DEEP_ARCHIVE: the price gap at this size is pennies a
    # month, while Deep Archive takes 12-48h to restore — in the one scenario
    # this exists for, the card has just died and you want it back tonight.
    aws s3 cp "$IMG.zst" "$S3_DEST/$(basename "$IMG.zst")" \
        --storage-class "$S3_CLASS" --only-show-errors
    echo "   uploaded; removing local copy"
    rm -f "$IMG.zst"
elif [ -n "$DEST" ]; then
    echo "→ shipping to $DEST"
    rsync -a --partial "$IMG.zst" "$DEST/" && rm -f "$IMG.zst"
    echo "   sent; local copy removed"
else
    echo "   NOTE: no destination given, so this image is still on the SD card"
    echo "   it is meant to protect. Pass --s3 or --dest to make it a backup."
fi

# ── 6. retention ─────────────────────────────────────────────────────────────
ls -1t "$BUILD_DIR"/birdpi-*.img.zst 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    echo "→ pruning $(basename "$old")"; rm -f "$old"
done

echo "done"
