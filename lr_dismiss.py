#!/usr/bin/env python3
"""
lr_dismiss.py — close a modal Lightroom dialog without a mouse.

A failed import ("No images that can be imported were found...") leaves a sheet
that blocks every other Lightroom command, and its buttons are absent from the
accessibility tree — `entire contents` of the window returns nothing — so
AppleScript cannot click them and Escape is ignored. That made it a dead end
from a phone: the pipeline could not proceed until someone reached the Mac.

So: try the polite routes first, then fall back to clicking where the sheet's
dismiss button actually is, verifying by screenshot that the dialog went away.
"""

import subprocess
import sys
import time

APP = "Adobe Lightroom"
CLICLICK = "/opt/homebrew/bin/cliclick"

# Dismiss targets in logical points, tried in order. The import sheet puts
# Cancel top-right; centred alerts put their button mid-screen-bottom.
TARGETS = [(1357, 61), (1420, 61), (756, 600), (756, 660)]


def osa(script, timeout=20):
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return ""


def dialog_text():
    """Text of any non-main Lightroom window — empty when nothing is blocking."""
    return osa(f'''
    tell application "System Events"
      tell process "{APP}"
        set acc to ""
        repeat with w in every window
          set nm to ""
          try
            set nm to name of w
          end try
          if nm is not "{APP}" then
            try
              repeat with t in every static text of w
                try
                  set acc to acc & (value of t) & " "
                end try
              end repeat
            end try
          end if
        end repeat
        return acc
      end tell
    end tell''')


def blocking() -> str:
    """The blocking dialog's text, ignoring Lightroom's own notification panel."""
    txt = dialog_text().strip()
    if not txt or txt.strip() == "Notifications":
        return ""
    return txt


def main():
    if osa(f'tell application "System Events" to return (exists process "{APP}")') != "true":
        print("Lightroom is not running.")
        sys.exit(1)

    before = blocking()
    if not before:
        print("No blocking dialog — nothing to dismiss.")
        return

    print(f"Dialog: {before[:120]}")
    osa(f'tell application "{APP}" to activate', timeout=15)
    time.sleep(1.0)

    # 1. a named button, when Lightroom exposes one
    hit = osa(f'''
    tell application "System Events"
      tell process "{APP}"
        repeat with w in every window
          try
            repeat with b in every button of w
              if name of b is in {{"Cancel", "Done", "OK", "Close", "Stop", "Skip"}} then
                click b
                return "clicked " & (name of b)
              end if
            end repeat
          end try
        end repeat
        return ""
      end tell
    end tell''')
    if hit:
        time.sleep(1.5)
        if not blocking():
            print(f"{hit} — dialog closed.")
            return

    # 2. Escape
    osa(f'tell application "System Events" to tell process "{APP}" to key code 53')
    time.sleep(1.5)
    if not blocking():
        print("Escape — dialog closed.")
        return

    # 3. click where the dismiss button actually is; verify after each
    for x, y in TARGETS:
        subprocess.run([CLICLICK, f"c:{x},{y}"], capture_output=True, timeout=20)
        time.sleep(1.5)
        if not blocking():
            print(f"Clicked ({x},{y}) — dialog closed.")
            return

    print(f"Couldn't dismiss it. Still showing: {blocking()[:160]}")
    sys.exit(2)


if __name__ == "__main__":
    main()
