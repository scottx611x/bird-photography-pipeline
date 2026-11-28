#!/bin/bash
# site_batch.sh — put a -wildlife/-sadie export on the website's /photography page.
#
# The app moves the Lightroom export to ~/Desktop/birbs/.site/<folder>/ and
# lr_host's "site-batch" command calls this. Claude Code (headless, on Scott's
# own plan — no API key) looks at each photo and names it; site_batch.py lays
# the photos out the way the website wants; the website's own
# `make wildlife-publish` uploads them and merges them into the live store.
# Never commits, deploys or pushes the website (website-30, 2026-11-27).
#
#   ./site_batch.sh --folder <batch>          # background, returns at once
#   ./site_batch.sh --folder <batch> --wait   # foreground

set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"   # launchd's PATH is bare
DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE="${CLAUDE_BIN:-/opt/homebrew/bin/claude}"
PY="$HOME/.bird_sort_venv/bin/python"
WEBSITE="$HOME/Website"
GIVE_UP_AFTER=$((36 * 3600))

FOLDER=""
if [ "${1:-}" = "--folder" ]; then FOLDER="${2:-}"; shift 2; fi
case "$FOLDER" in ""|*[!A-Za-z0-9._+-]*) echo "usage: site_batch.sh --folder <batch>"; exit 1 ;; esac
JOB="$HOME/Desktop/birbs/.site/$FOLDER"
[ -f "$JOB/job.json" ] || { echo "no website job at $JOB"; exit 1; }
LOCK="$JOB/.running"
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK")" 2>/dev/null; then
  echo "site-batch is already running for $FOLDER (pid $(cat "$LOCK"))."; exit 0
fi
LOG="$JOB/run-$(date +%Y%m%d-%H%M%S).log"

APP_URL="${BIRD_APP:-http://localhost:8765}"
status() {
  echo "== $*"
  curl -s -m 5 -o /dev/null --data-urlencode "text=$*" --data-urlencode "folder=$FOLDER" \
    "$APP_URL/api/site-status" || true
}
sleep_until() {
  while [ "$(date +%s)" -lt "$1" ]; do
    left=$(( $1 - $(date +%s) )); [ "$left" -gt 900 ] && left=900; sleep "$left"
  done
}

KIND="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['kind'])" "$JOB/job.json")"
if [ "$KIND" = "sadie" ]; then
  WHAT="These are photos of Sadie, Scott's chocolate Labrador. Every photo's category is \"sadie\".
Name each for the moment, starting with sadie- (e.g. sadie-ball-stare, sadie-after-the-swim, sadie-sunlit-nap);
give no species. If a photo doesn't show Sadie at all, or is unusable (badly blurred, a mis-fire), mark it skip."
else
  WHAT="These are wildlife photos that are not birds (mammals, insects, reptiles, plants, fungi, landscapes).
category is one of: mammal, insect, reptile, flora, fungi, landscape, macro.
name is the subject's common name, lowercase-hyphenated (e.g. white-tailed-deer, eastern-chipmunk, common-whitetail);
photos of the same subject share the name (the website adds -2, -3 itself).
species is \"Common Name (Genus species)\", or \"(Genus sp.)\" when only the genus is sure — leave it empty if unsure.
tags: one kind word, e.g. [\"deer\"], [\"dragonfly\"], [\"squirrel\"].
Mark a photo skip only if it is unusable or turns out to be a bird or a person."
fi
PROMPT="You are naming photos for the photography page of Scott's website. Work unattended: never ask questions.
Look at every photo in $JOB/preview/ with the Read tool (they are small copies of the files listed in $JOB/job.json).
$WHAT
Write $JOB/names.json: an object keyed by file name, e.g.
{\"DSC_1234.jpg\": {\"category\": \"mammal\", \"name\": \"white-tailed-deer\", \"species\": \"White-tailed Deer (Odocoileus virginianus)\", \"tags\": [\"deer\"]},
 \"DSC_1240.jpg\": {\"skip\": true, \"why\": \"blurred\"}}
Every file in job.json must appear. Then run: $PY $DIR/site_batch.py check $FOLDER
and fix names.json until it prints ok."

run() {
  sh -c 'echo $PPID' > "$LOCK"
  trap 'rm -f "$LOCK"' EXIT
  cd "$JOB" || exit 1
  started=$(date +%s)
  "$PY" "$DIR/site_batch.py" previews "$FOLDER" || { status "failed — couldn't make previews"; return; }
  while ! "$PY" "$DIR/site_batch.py" check "$FOLDER" >/dev/null 2>&1; do
    status "naming $(ls "$JOB/preview" | wc -l | tr -d ' ') photos"
    out="$("$CLAUDE" -p "$PROMPT" --output-format json \
      --add-dir "$JOB" \
      --permission-mode acceptEdits \
      --allowedTools "Read" "Glob" "Write" "Edit" \
        "Bash($PY $DIR/site_batch.py check:*)" 2>&1)"
    code=$?
    res="$(printf '%s\n' "$out" | "$PY" "$DIR/bird_sort.py" classify "$code")"
    printf '%s\n' "$res" | sed '$d'
    set -- $(printf '%s\n' "$res" | tail -1)
    verdict="${1:-fail}"; wake="${2:-0}"; shift 2 2>/dev/null; reason="$*"
    case "$verdict" in
      ok)
        "$PY" "$DIR/site_batch.py" check "$FOLDER" || { status "failed — names.json incomplete after Claude's run"; return; } ;;
      wait)
        if [ "$wake" -gt $(( started + GIVE_UP_AFTER )) ]; then
          status "failed — gave up waiting ($reason)"; return
        fi
        status "waiting until $(date -r "$wake" '+%a %H:%M') — $reason"
        sleep_until "$wake" ;;
      *) status "failed — naming: $reason"; return ;;
    esac
  done
  OUT="$("$PY" "$DIR/site_batch.py" stage "$FOLDER")" || { status "failed — couldn't stage the photos"; return; }
  n=$(find "$OUT" -name '*.jpg' | wc -l | tr -d ' ')
  status "publishing $n photos"
  if (cd "$WEBSITE" && make wildlife-publish DIR="$OUT") > "$JOB/publish.log" 2>&1; then
    cat "$JOB/publish.log"
    status "done — $n photos on the website ($(tail -1 "$JOB/publish.log" | cut -c1-120))"
  else
    cat "$JOB/publish.log"
    status "failed — publish: $(tail -1 "$JOB/publish.log" | cut -c1-160)"
  fi
}

if [ "${1:-}" = "--wait" ]; then
  run 2>&1 | tee "$LOG"
else
  ( run > "$LOG" 2>&1 & )
  echo "site-batch started — log: $LOG"
fi
