#!/bin/bash
# sort_batch.sh — run the /sort-batch skill headless, in the background.
#
# Claude Code on Scott's own plan (no API key, no per-call billing) sorts the
# batch on the posting step: species, frames, posts, crops, flags. lr_host's
# "sort-batch" command calls this when an export lands; run it by hand to
# re-sort. Returns at once; the run logs to ~/.bird_sort/logs/.
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

# Unattended runs outlive an interactive sign-in (it lapses every few weeks).
# `claude setup-token` makes a long-lived one: save it, chmod 600, to
#   ~/.bird_sort/.oauth_token
TOKEN_FILE="$WORK/.oauth_token"
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -s "$TOKEN_FILE" ]; then
  export CLAUDE_CODE_OAUTH_TOKEN="$(cat "$TOKEN_FILE")"
fi

if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK")" 2>/dev/null; then
  echo "sort-batch is already running (pid $(cat "$LOCK"))."
  exit 0
fi
LOG="$LOGDIR/$(date +%Y%m%d-%H%M%S).log"

PROMPT="Use the sort-batch skill to sort the batch that is on the posting step.
You are running unattended: never ask questions — flag anything uncertain.
Run the sorter exactly as ~/.bird_sort_venv/bin/python bird_sort.py <command> from
the current directory (no cd, no shell variables), and keep plan.json in the work
directory that prepare prints."

run() {
  # This subshell's own pid. Not $$ (the parent, gone in a moment) and not
  # $BASHPID — macOS ships bash 3.2, which doesn't have it.
  sh -c 'echo $PPID' > "$LOCK"
  trap 'rm -f "$LOCK"' EXIT
  cd "$DIR" || exit 1
  echo "== sort-batch $(date)  (cwd $DIR)"
  "$CLAUDE" -p "$PROMPT" \
    --add-dir "$WORK" \
    --permission-mode acceptEdits \
    --allowedTools \
      "Bash(~/.bird_sort_venv/bin/python bird_sort.py:*)" \
      "Bash($HOME/.bird_sort_venv/bin/python bird_sort.py:*)" \
      "Read" "Glob" "Grep" "Write" "Edit" "Skill"
  echo "== exit $? $(date)"
}

if [ "${1:-}" = "--wait" ]; then
  run 2>&1 | tee "$LOG"
else
  ( run > "$LOG" 2>&1 & )
  echo "sort-batch started — log: $LOG"
fi
