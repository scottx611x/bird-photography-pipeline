#!/bin/bash
# pi_state_backup.sh — daily snapshot of the small, irreplaceable state on birdpi.
#
# The monthly disk image (pi_image_backup.sh) restores the whole machine but is
# up to 30 days stale. This covers that gap: the data that actually changes is
# about 17 MB, so there is no reason to let it age a month.
#
#   birdnet.db          3+ months of detections — the source of truth
#   birdnet-go config   painful to reconstruct by hand
#   queue.db            birdqueue's scheduled posts
#   species_history.json  autocomplete names migrated off Buffer
#   .env files          tokens; the disk image already carries these, so putting
#                       them here adds no new exposure and avoids a half-restore
#
# Lands in S3 Standard (not Glacier) deliberately — it is tiny, and the point is
# that it is instantly available while the image takes 12-48h to thaw.
#
# Usage: sudo ./pi_state_backup.sh [--s3 s3://bucket/prefix]

set -euo pipefail

S3_DEST=""
WORK="${WORK:-/var/backups/pi-state}"

while [ $# -gt 0 ]; do
    case "$1" in
        --s3) S3_DEST="${2%/}"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

STAMP="$(date +%Y-%m-%d)"
STAGE="$WORK/$STAMP"
rm -rf "$STAGE"; mkdir -p "$STAGE"
trap 'rm -rf "$STAGE"' EXIT

# SQLite needs .backup rather than cp: with WAL enabled, copying the file and
# its sidecars at different instants can capture a torn database.
snap_db() {
    local src="$1" name="$2"
    [ -f "$src" ] || { echo "   skip $name (absent)"; return 0; }
    if sqlite3 "$src" ".backup '$STAGE/$name'" 2>/dev/null; then
        echo "   $name ($(du -h "$STAGE/$name" | cut -f1))"
    else
        echo "   WARNING: .backup failed for $src, falling back to cp" >&2
        cp -f "$src" "$STAGE/$name"
    fi
}

echo "→ snapshotting"
snap_db /home/scott/birdnet-go/data/birdnet.db birdnet.db
snap_db /srv/birdqueue/queue.db queue.db

copy_if() { [ -e "$1" ] && cp -a "$1" "$STAGE/$2" && echo "   $2"; }
copy_if /home/scott/birdnet-go/config birdnet-go-config
copy_if /srv/birdqueue/species_history.json species_history.json
copy_if /srv/ynab-mcp/.env ynab-mcp.env
copy_if /srv/birdqueue/.env birdqueue.env

# Unit files and the tailscale state too — cheap, and they are what make the
# restored box *this* box rather than a fresh Pi.
mkdir -p "$STAGE/systemd"
for u in birdqueue ynab-mcp-http mediamtx mic-loopback; do
    [ -f "/etc/systemd/system/$u.service" ] && cp -a "/etc/systemd/system/$u.service" "$STAGE/systemd/"
done
echo "   systemd units ($(ls "$STAGE/systemd" | wc -l))"

TARBALL="$WORK/state-$STAMP.tar.zst"
tar -C "$STAGE" -cf - . | zstd -q -T0 -6 -o "$TARBALL" -f
SIZE=$(du -h "$TARBALL" | cut -f1)
echo "→ $TARBALL ($SIZE)"

if [ -n "$S3_DEST" ]; then
    aws s3 cp "$TARBALL" "$S3_DEST/$STAMP/state.tar.zst" --only-show-errors
    echo "→ uploaded to $S3_DEST/$STAMP/state.tar.zst"
    rm -f "$TARBALL"
else
    echo "   NOTE: no --s3 given; this copy is on the card it protects."
fi

# Local copies are a convenience, not the backup. Keep a few days for quick
# rollbacks and let S3 lifecycle handle real retention.
#
# `ls` on an empty glob exits 2, which under `set -o pipefail` fails the whole
# script *after* a successful upload — reporting failure for a run that worked.
# find handles the empty case without complaining.
find "$WORK" -maxdepth 1 -name 'state-*.tar.zst' -type f -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | tail -n +4 | cut -d' ' -f2- | while read -r old; do rm -f "$old"; done

echo "done"
