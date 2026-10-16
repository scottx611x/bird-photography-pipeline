# Recovering birdpi onto a new SD card

The Pi runs birdnet-go, mediamtx, the YNAB MCP server and birdqueue off one SD
card. This is how to get all of it back when that card dies.

Written to be followed while annoyed and without rereading any of the code.

---

## What exists

| what | where | how often | retention |
|---|---|---|---|
| Bootable system image (~3 GB) | `s3://birds-scott-ouellette/birds/pi-images/` | monthly | 180 days (6 images) |
| Small state tarball (~17 MB) | `s3://birds-scott-ouellette/birds/pi-state/` | daily | 35 days |
| Audio clips | `s3://birds-scott-ouellette/birds/sounds/clips/` | continuous | kept indefinitely |

The image contains the OS, Docker and its images, all four services, their
configs and units, and `birdnet.db` **as of the image date**. It does *not*
contain the audio clips — that exclusion is what keeps a 41 GB card down to a
3 GB image. Clips are already in S3 separately and are not needed to boot.

Managed in the `infra` repo: `infra/website/pi_backup.tf`.

---

## The one thing that will catch you out

**Deep Archive objects cannot be downloaded directly.** `aws s3 cp` on one fails
with `InvalidObjectState`. You must first submit a restore job and wait
**12–48 hours**. Start this before doing anything else.

Also: **the Pi's own credentials cannot read these backups.** That is deliberate
— a backup key that can read and delete its own history is how ransomware gets
leverage. Run everything below from your Mac with your normal AWS credentials.

---

## 1. Start the restore job immediately

```sh
BUCKET=birds-scott-ouellette
# newest image first
aws s3api list-objects-v2 --bucket $BUCKET --prefix birds/pi-images/ \
  --query 'reverse(sort_by(Contents,&LastModified))[].{K:Key,When:LastModified}' --output table

KEY=birds/pi-images/birdpi-YYYY-MM-DD.img.zst   # pick one

aws s3api restore-object --bucket $BUCKET --key "$KEY" \
  --restore-request 'Days=7,GlacierJobParameters={Tier=Standard}'
```

`Tier=Standard` is 12 hours or so. `Tier=Bulk` is cheaper and up to 48. There is
no expedited tier for Deep Archive.

Poll until `ongoing-request="false"`:

```sh
aws s3api head-object --bucket $BUCKET --key "$KEY" --query Restore --output text
```

**While you wait**, grab the latest state tarball — it is in Standard and
downloads instantly. It holds a far more recent `birdnet.db` than the image:

```sh
aws s3 ls s3://$BUCKET/birds/pi-state/ --recursive | tail -3
aws s3 cp s3://$BUCKET/birds/pi-state/<newest>/state.tar.zst .
```

## 2. Download and decompress

```sh
aws s3 cp "s3://$BUCKET/$KEY" .
zstd -d birdpi-YYYY-MM-DD.img.zst          # ~3 GB -> ~12 GB
```

## 3. Flash it

Raspberry Pi Imager → "Use custom" → pick the `.img`. **Do not** apply any of
Imager's OS-customisation settings (hostname, Wi-Fi, SSH); they rewrite config
this image already has correct.

Or on the command line — check the device name twice, `dd` to the wrong one
destroys it:

```sh
diskutil list                              # find the card, e.g. /dev/disk4
diskutil unmountDisk /dev/disk4
sudo dd if=birdpi-YYYY-MM-DD.img of=/dev/rdisk4 bs=4m status=progress conv=fsync
```

Use `rdisk4`, not `disk4`, on macOS — it is roughly ten times faster.

## 4. Boot and expand

The image was built with the original card's disk identifier (`665081e2`), so
the PARTUUIDs in `/etc/fstab` and `cmdline.txt` resolve and it boots unmodified.
Nothing to edit.

The image is smaller than the card, so reclaim the rest:

```sh
ssh birdpi
sudo raspi-config --expand-rootfs && sudo reboot
```

## 5. Put the recent data back

The image's `birdnet.db` is up to a month stale. Overwrite it from the state
tarball you downloaded in step 1:

```sh
scp state.tar.zst birdpi:/tmp/
ssh birdpi
sudo systemctl stop birdqueue
docker stop birdnet-go
tar --use-compress-program=unzstd -xf /tmp/state.tar.zst -C /tmp/state
sudo cp /tmp/state/birdnet.db /home/scott/birdnet-go/data/birdnet.db
sudo cp /tmp/state/queue.db   /srv/birdqueue/queue.db
sudo rm -f /home/scott/birdnet-go/data/birdnet.db-wal \
           /home/scott/birdnet-go/data/birdnet.db-shm
docker start birdnet-go
sudo systemctl start birdqueue
```

Delete the stale `-wal`/`-shm` files or SQLite replays old frames over the
restored database.

## 6. Check it came back

```sh
ssh birdpi '
  docker ps --format "{{.Names}} {{.Status}}"
  systemctl is-active birdqueue ynab-mcp-http mediamtx mic-loopback tailscaled
  curl -sS http://127.0.0.1:8910/api/health
  tailscale status | head -3
'
```

Expect `birdnet-go` healthy, five services active, birdqueue returning JSON, and
the node on the tailnet.

**Tailscale** usually reconnects on its own because the node key is in the
image. If it does not, `sudo tailscale up --accept-dns=false --hostname=birdpi`
and approve the login. Keep `--accept-dns=false`: without it Tailscale rewrites
`/etc/resolv.conf`, which the bird stack depends on.

## 7. Afterwards

- `sudo /tmp/pi_image_backup.sh --s3 s3://birds-scott-ouellette/birds/pi-images`
  to take a fresh image of the recovered system.
- Clips are **not** restored by any of the above. The directory comes back empty
  and birdnet-go starts writing new ones. Old recordings stay in S3 under
  `birds/sounds/clips/`; pull them down only if you actually need them locally.
- If the card died from wear, consider a higher-endurance card or moving the
  build target off the card (see `pi_image_backup.sh`, `BUILD_DIR`).

---

## Untested

As of 2026-10-16 this procedure has **not** been exercised end to end. The
PARTUUID matching — the usual reason a restored Pi image fails to boot — is
verified by the build script on every run, but nobody has flashed one of these
images and watched it come up. Worth doing with a spare card before you need it.
