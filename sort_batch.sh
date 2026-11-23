#!/bin/bash
# sort_batch.sh — run the /sort-batch skill headless, in the background.
#
# Claude Code on Scott's own plan (no API key, no per-call billing) sorts the
# batch on the posting step: species, frames, posts, crops, flags. lr_host's
# "sort-batch" command calls this when an export lands; run it by hand to
# re-sort. Returns at once; the run logs to ~/.bird_sort/logs/.
#
# Waits out usage limits and a lapsed sign-in instead of failing (see run()).
#
# Permissions are narrow on purpose — it runs unattended. It may only run
# bird_sort.py and read/write its own work dir; anything else is denied
# (print mode never prompts, it refuses).
#
#   ./sort_batch.sh            # background, returns immediately
#   ./sort_batch.sh --wait     # foreground (for testing)

set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"   # launchd's PATH is bare
DIR="$(cd "$(dirname "$0")" && pwd)"
WORK="$HOME/.bird_sort"
LOGDIR="$WORK/logs"
LOCK="$WORK/.running"
CLAUDE="${CLAUDE_BIN:-/opt/homebrew/bin/claude}"
mkdir -p "$LOGDIR"

PY="$HOME/.bird_sort_venv/bin/python"
STATUS="$WORK/status.txt"           # one line: what the sorter is doing now
GIVE_UP_AFTER=$((36 * 3600))        # stop waiting on a batch after a day and a half

# --folder <batch>: sort that batch where it waits for review (the app passes
# it). Without it, the batch on the posting screen, as before.
FOLDER=""
if [ "${1:-}" = "--folder" ]; then FOLDER="${2:-}"; shift 2; fi
case "$FOLDER" in *[!A-Za-z0-9._+-]*) echo "bad folder: $FOLDER"; exit 1 ;; esac
export BIRD_SORT_FOLDER="$FOLDER"

if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK")" 2>/dev/null; then
  echo "sort-batch is already running (pid $(cat "$LOCK"))."
  exit 0
fi
LOG="$LOGDIR/$(date +%Y%m%d-%H%M%S).log"

WHICH="the batch that is on the posting step"
[ -n "$FOLDER" ] && WHICH="the batch $FOLDER (bird_sort.py already knows which one — just run its commands)"
PROMPT="Use the sort-batch skill to sort $WHICH.
You are running unattended: never ask questions — flag anything uncertain.
Run the sorter exactly as ~/.bird_sort_venv/bin/python bird_sort.py <command> from
the current directory (no cd, no shell variables), and keep plan.json in the work
directory that prepare prints."

# Each status line also goes to the app, which is how the crank knows when a
# batch is sorted (or won't be soon) and can set it aside and start the next.
APP_URL="${BIRD_APP:-http://localhost:8765}"
status() {
  echo "$(date '+%a %H:%M') $*" > "$STATUS"; echo "== $*"
  [ -n "${BIRD_SORT_TEST:-}" ] && return 0      # a rehearsal: leave the app alone
  curl -s -m 5 -o /dev/null --data-urlencode "text=$*" --data-urlencode "folder=$FOLDER" \
    "$APP_URL/api/sort-status" || true
}

# Sleep until an epoch time in short naps, so a Mac that slept through the
# wake-up time (or a clock change) doesn't strand the wait.
sleep_until() {
  while [ "$(date +%s)" -lt "$1" ]; do
    left=$(( $1 - $(date +%s) )); [ "$left" -gt 900 ] && left=900
    sleep "$left"
  done
}

run() {
  # This subshell's own pid. Not $$ (the parent, gone in a moment) and not
  # $BASHPID — macOS ships bash 3.2, which doesn't have it.
  sh -c 'echo $PPID' > "$LOCK"
  echo "$FOLDER" > "$WORK/target"     # for bird_sort.py, should the env not reach it
  trap 'rm -f "$LOCK" "$WORK/target"' EXIT
  cd "$DIR" || exit 1
  started=$(date +%s)
  # Runs on Scott's own Claude plan. If it hits the plan's usage limit, or
  # the sign-in has lapsed, it waits — until the limit resets, or in 2-hour
  # steps until `claude auth login` — and tries again. No API key, no token.
  while :; do
    if ! "$PY" bird_sort.py pending; then status "idle — nothing to sort"; break; fi
    status "sorting (cwd $DIR)"
    out="$("$CLAUDE" -p "$PROMPT" --output-format json \
      --add-dir "$WORK" \
      --permission-mode acceptEdits \
      --allowedTools \
        "Bash(~/.bird_sort_venv/bin/python bird_sort.py:*)" \
        "Bash($HOME/.bird_sort_venv/bin/python bird_sort.py:*)" \
        "Read" "Glob" "Grep" "Write" "Edit" "Skill" 2>&1)"
    code=$?
    res="$(printf '%s\n' "$out" | "$PY" bird_sort.py classify "$code")"
    printf '%s\n' "$res" | sed '$d'
    set -- $(printf '%s\n' "$res" | tail -1)
    verdict="${1:-fail}"; wake="${2:-0}"; shift 2 2>/dev/null; reason="$*"
    case "$verdict" in
      ok)   status "done — sorted $(date '+%H:%M')"; break ;;
      wait)
        if [ "$wake" -gt $(( started + GIVE_UP_AFTER )) ]; then
          status "gave up waiting ($reason) — run ./sort_batch.sh to retry"; break
        fi
        status "waiting until $(date -r "$wake" '+%a %H:%M') — $reason"
        sleep_until "$wake" ;;
      *)    status "failed — $reason (log: $LOG)"; break ;;
    esac
  done
}

if [ "${1:-}" = "--wait" ]; then
  run 2>&1 | tee "$LOG"
else
  ( run > "$LOG" 2>&1 & )
  echo "sort-batch started — log: $LOG"
fi
