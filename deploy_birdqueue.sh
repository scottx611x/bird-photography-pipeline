#!/bin/sh
# Push birdqueue to the Pi and restart it.
#
# The repo is the source of truth; the Pi is a deployment. Editing
# /srv/birdqueue/birdqueue.py in place works right up until the next deploy
# silently reverts it, so don't.
#
# The Pi's .env is deliberately never touched — tokens live only there, and a
# deploy should never be able to clobber them.
#
# Usage: ./deploy_birdqueue.sh [host]        (default: birdpi)

set -e

HOST="${1:-birdpi}"
REMOTE=/srv/birdqueue
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$HERE/birdqueue.py" ]; then
    echo "birdqueue.py not found next to this script" >&2
    exit 1
fi

echo "→ $HOST:$REMOTE"
scp -q "$HERE/birdqueue.py" "$HOST:$REMOTE/birdqueue.py"

# Species autocomplete data: only seed it if the box has none, so a deploy
# never discards names learned from posts published since the last one.
if ! ssh "$HOST" "test -s $REMOTE/species_history.json"; then
    echo "→ seeding species history (none on the box yet)"
    scp -q "$HERE/docs/species_history.json" "$HOST:$REMOTE/species_history.json"
fi

# Keep the unit file in step, but only bounce systemd when it actually changed.
if ! ssh "$HOST" "sudo cmp -s /etc/systemd/system/birdqueue.service -" \
        < "$HERE/systemd/birdqueue.service" 2>/dev/null; then
    echo "→ updating systemd unit"
    ssh "$HOST" "sudo tee /etc/systemd/system/birdqueue.service >/dev/null && sudo systemctl daemon-reload" \
        < "$HERE/systemd/birdqueue.service"
fi

ssh "$HOST" "chmod 755 $REMOTE/birdqueue.py && sudo systemctl restart birdqueue && sleep 3"

STATUS=$(ssh "$HOST" "systemctl is-active birdqueue")
echo "→ service: $STATUS"
[ "$STATUS" = "active" ] || { echo "birdqueue did not come back up" >&2; exit 1; }

# A health check is the only thing that proves the new code actually runs —
# systemd calls a process that crashes a second later "active" for a moment.
ssh "$HOST" "curl -sS -m8 http://127.0.0.1:8910/api/health" || {
    echo "health check failed — check: ssh $HOST journalctl -u birdqueue -n 30" >&2
    exit 1
}
echo
echo "deployed to $HOST"
