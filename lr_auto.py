#!/usr/bin/env python3
"""
lr_auto.py — Lightroom UI automation for bird batch editing.

Commands:
  import            Open a YYYY-MM-DD-* folder in Lightroom's Add Photos dialog
  auto-tone         Select all + apply Auto Settings in Lightroom
  copy-and-paste    Copy edits from current photo → paste to all selected
                    (run AFTER manually editing one photo with denoise + CA removal)
  export            Trigger Export with Previous settings in Lightroom

Typical workflow (driven by the web UI at localhost:8765):
  1. Web UI: Organize — scans Downloads for YYYY-MM-DD-* folders
  2. Web UI: Import  — opens the folder in Lightroom
  3. Web UI: Auto-tone — Select All + Auto Settings
  4. In Lightroom: edit ONE photo — set Denoise + Remove Chromatic Aberration
  5. Web UI: Denoise+CA — copies that photo's settings, pastes to all
  6. In Lightroom: pick your best shot
  7. Web UI: Export — Export with Previous → ~/Desktop/birds/
  8. Web UI: Post — fills in species/location/date, queues to Buffer
"""

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DOWNLOADS    = Path.home() / "Downloads"
BIRDS_DIR    = Path.home() / "Desktop" / "birbs"   # Lightroom's "Export with Previous" target
RAW_SUFFIXES = {".nef", ".arw", ".cr2", ".cr3", ".dng", ".raf", ".rw2", ".orf", ".pef"}


# ── AppleScript helpers ────────────────────────────────────────────────────────

# Keystrokes go to whichever app is in front, whatever process the script
# names — "tell process "Adobe Lightroom" to keystroke" does not aim them. On
# 2026-11-22 Lightroom's activate timed out and a search meant for it was
# typed into Scott's terminal instead. So nothing is typed unless Lightroom is
# frontmost, checked right before (ensure_lr_front) and again inside the same
# script, which closes the gap between the check and the first key.
KEYS_RE = re.compile(r"\bkeystroke\b|\bkey code\b")
FRONT_GUARD = (
    'tell application "System Events" to set _fp to name of '
    '(first process whose frontmost is true)\n'
    'if _fp is not "Adobe Lightroom" then error "Lightroom is not in front ("'
    ' & _fp & " is) - no keystrokes sent"\n')


def lr_frontmost() -> str:
    try:
        r = subprocess.run(["osascript", "-e", 'tell application "System Events" to return '
                            'name of (first process whose frontmost is true)'],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""


def user_idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input on the Mac."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"],
                             capture_output=True, text=True, timeout=10).stdout
        m = re.search(r'"HIDIdleTime" = (\d+)', out)
        return int(m.group(1)) / 1e9 if m else 1e9
    except (subprocess.TimeoutExpired, OSError):
        return 1e9


USER_IDLE_BEFORE_FOCUS = 45     # seconds of no input before Lightroom takes focus
USER_IDLE_WAIT_CAP     = 30 * 60


def wait_for_user_idle() -> None:
    """Don't pull focus from someone mid-use: wait until the Mac has been left
    alone for a bit. Taking focus while Scott types in Chrome sends his keys to
    Lightroom, where single letters rate, flag and reject photos."""
    t0, said = time.time(), False
    while (user_idle_seconds() < USER_IDLE_BEFORE_FOCUS
           and time.time() - t0 < USER_IDLE_WAIT_CAP):
        if not said:
            print(f"  You're using the Mac — waiting until it's been idle "
                  f"{USER_IDLE_BEFORE_FOCUS}s before bringing Lightroom forward.", flush=True)
            said = True
        time.sleep(5)


def ensure_lr_front(tries: int = 3) -> None:
    front = ""
    if lr_frontmost() != "Adobe Lightroom":
        wait_for_user_idle()
    for i in range(tries):
        front = lr_frontmost()
        if front == "Adobe Lightroom":
            return
        try:
            subprocess.run(["osascript", "-e", 'tell application "Adobe Lightroom" to activate'],
                           capture_output=True, timeout=15)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1.0 + i)
    if lr_frontmost() != "Adobe Lightroom":
        raise RuntimeError(f"Lightroom isn't the frontmost app ({front or 'unknown'} is) "
                           "— not sending keystrokes.")


def run_applescript(script: str) -> str:
    if KEYS_RE.search(script):
        ensure_lr_front()
        script = FRONT_GUARD + script
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


def grid_view() -> None:
    """Photo Grid by its View-menu item. The "g" shortcut needs Lightroom in
    front, which means pulling focus from whatever Scott is using; the menu
    click works with Lightroom in the background (2026-11-22: a recovery step's
    "g" stopped a 75-photo export because Chrome was in front)."""
    lr_menu_click("View", "Photo Grid")


def lr_is_running() -> bool:
    try:
        run_applescript('tell application "System Events" to get name of process "Adobe Lightroom"')
        return True
    except RuntimeError:
        return False


MENU_RETRY_WAITS = (1.0, 2.0, 4.0)


def lr_menu_click(menu_name: str, item_name: str) -> None:
    """Click a Lightroom menu item, riding out a menu bar that isn't there yet.

    Right after a heavy action Lightroom can briefly expose no menu bar at all:
    on 2026-11-19 Edit > Select All on 48 photos succeeded and File > Export
    with Previous one second later died with "Can't get menu bar 1 ... Invalid
    index (-1719)" — and the run sat a day waiting for an export that was
    never started. -1719 means the element was not found, so nothing was
    clicked and retrying cannot double-click anything."""
    script = f'''
tell application "System Events"
  tell process "Adobe Lightroom"
    click menu item "{item_name}" of menu "{menu_name}" of menu bar item "{menu_name}" of menu bar 1
  end tell
end tell
'''
    for wait in MENU_RETRY_WAITS + (None,):
        try:
            run_applescript(script)
            return
        except RuntimeError as e:
            if wait is None or "-1719" not in str(e):
                raise
            print(f"  (menu bar not ready for {menu_name} > {item_name}; retrying in {wait:.0f}s)")
            time.sleep(wait)
            try:
                run_applescript('tell application "Adobe Lightroom" to activate')
            except RuntimeError:
                pass


# ── Commands ──────────────────────────────────────────────────────────────────

def _click_menu_item_by_prefix(menu_name: str, prefix: str):
    """Click a menu item whose label carries a count ("Update AI Settings in
    94 Photos"). Two steps on purpose: read the exact name, then click it by
    that name. Clicking the loop variable directly does not open the menu and
    silently does nothing — which is why this looked like a no-op the first
    time it was tried."""
    name = run_applescript(f'''
tell application "System Events"
  tell process "Adobe Lightroom"
    repeat with mi in menu items of menu 1 of menu bar item "{menu_name}" of menu bar 1
      set n to name of mi
      if n is not missing value then
        if n starts with "{prefix}" then
          if enabled of mi then return n
          return "disabled"
        end if
      end if
    end repeat
    return ""
  end tell
end tell
''').strip()
    if not name or name == "disabled":
        return None
    lr_menu_click(menu_name, name)
    return name


def _menu_item_count(menu_name: str, prefix: str, pattern: str = r"in ([\d,]+)"):
    """The number in a menu item like "Update AI Settings in 5,762 Items",
    read without clicking. None when the item is absent or carries no count."""
    name = run_applescript(f'''
tell application "System Events"
  tell process "Adobe Lightroom"
    repeat with mi in menu items of menu 1 of menu bar item "{menu_name}" of menu bar 1
      set n to name of mi
      if n is not missing value then
        if n starts with "{prefix}" then return n
      end if
    end repeat
    return ""
  end tell
end tell
''').strip()
    m = re.search(pattern, name)
    return int(m.group(1).replace(",", "")) if m else None


def _toolbar_task() -> bool:
    """Is Lightroom's toolbar background task (a paste that renders Denoise as
    it goes) running? lr_host reads it off a capture of Lightroom's window."""
    import json as _json, urllib.request as _ur
    try:
        with _ur.urlopen("http://127.0.0.1:8766/progress", timeout=25) as r:
            return bool(_json.loads(r.read()).get("toolbar"))
    except Exception:
        return False


def render_ai_settings(expect: int = None):
    """Actually render the pasted Denoise.

    Pasting only ticks the checkbox. Lightroom holds the AI edit as *pending*
    and shows a yellow badge saying "Highlighted settings need to be updated
    to properly render your photo" — export it in that state and you get an
    un-denoised JPEG. Lightroom sometimes kicks the render off by itself after
    a paste, but when the paste dialog wedges it never does, so ask explicitly.
    Harmless when nothing is pending: the menu item is disabled and we skip.
    Never starts a second render beside a running one — two at once fight and
    look exactly like a wedge.
    """
    if _render_dialog_count():
        print("  A render is already running — not starting another.")
        return None
    # A paste running in the toolbar renders as it goes. Asking for a render
    # on top of it put two Denoise jobs in a fight, and the paste hung at
    # photo 10 of 36 for good (2026-11-22, 10/24).
    if _toolbar_task():
        print("  Lightroom's paste is rendering in the toolbar — not starting another render.")
        return None
    # Select All selects whatever view Lightroom is on. After a relaunch that
    # was All Photos, and a render began on 5,762 library photos (2026-10-18).
    # When the batch size is known, render only if the selection matches it.
    if expect is not None:
        got = _menu_item_count("Photo", "Update AI Settings")
        if got != expect:
            print(f"  Refusing to render: selection is {got} photos, the batch "
                  f"is {expect}. Is Lightroom showing the batch's album?")
            return None
    name = _click_menu_item_by_prefix("Photo", "Update AI Settings")
    if name:
        print(f"  Rendering denoise: {name}")
    else:
        print("  AI settings already rendered — nothing pending.")
    return name


CLICLICK = "/opt/homebrew/bin/cliclick"


def _progress_window():
    """(x, y, w, h) of Lightroom's progress dialog, or None."""
    out = run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    repeat with w in windows
      set n to ""
      try
        set n to name of w
      end try
      if n contains "progress" or n contains "Updating" then
        set p to position of w
        set s to size of w
        return ((item 1 of p) as string) & " " & ((item 2 of p) as string) & " " & ¬
               ((item 1 of s) as string) & " " & ((item 2 of s) as string)
      end if
    end repeat
    return ""
  end tell
end tell
''').strip()
    parts = out.split()
    return tuple(int(v) for v in parts) if len(parts) == 4 else None


def _render_dialog_count() -> int:
    """How many dialogs are rendering, or about to: "Updating AI Settings",
    plus the paste dialog, whose own text promises "AI Settings will be
    updated next" — asking for a render during a paste stacks a second one on
    the render Lightroom starts itself. Should be 0 or 1."""
    out = run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    return count of (every window whose name is "Updating AI Settings" or name is "batch_progress_dialog")
  end tell
end tell
''').strip()
    return int(out) if out.isdigit() else 0


def _stop_render_dialogs() -> int:
    """Click Stop on every render dialog; returns how many it clicked."""
    out = run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    set n to 0
    repeat with w in (every window whose name is "Updating AI Settings")
      try
        click button "Stop" of w
        set n to n + 1
      end try
    end repeat
    return n
  end tell
end tell
''').strip()
    return int(out) if out.isdigit() else 0


def cmd_ai_resume(args):
    """Unstick a stalled "Updating AI Settings" pass and restart it.

    The render wedges every couple of dozen photos: the dialog freezes on one
    count while Lightroom drops to idle, and nothing behind it proceeds. The
    recovery is always the same three steps, so do them rather than making
    someone sit and nurse a 94-photo batch:

      1. click Stop, which dismisses the stuck dialog (already-rendered photos
         keep their render — it resumes from where it stopped, not from one);
      2. re-select everything, because dismissing it collapses the selection
         to a single photo and the menu item then loses its count;
      3. ask for the render again.
    """
    if not lr_is_running():
        print("Lightroom is not running.")
        sys.exit(1)
    if args.expect is None:
        print("ai-resume needs --expect <batch size>: it selects all in "
              "whatever view is open, so it must know what it should see.")
        sys.exit(1)

    # The paste wedges too ("Pasting 70 of 228", Lightroom idle, forever), and
    # its only button is "Update AI Settings Later". That is safe to click
    # *here*: it lets the paste finish on every photo and defers only the
    # render, which the Select All + render_ai_settings() below asks for right
    # away (seen 2026-10-18: Later, then "Update AI Settings in 228 Photos").
    # Click it by name — it is not a Stop, so the coordinate click is wrong.
    label = run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    repeat with w in windows
      set acc to ""
      try
        repeat with t in every static text of w
          try
            set acc to acc & (value of t) & " "
          end try
        end repeat
      end try
      if acc is not "" then return acc
    end repeat
    return ""
  end tell
end tell
''').strip()
    box = None
    if "Pasting" in label:
        run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    click button "Update AI Settings Later" of window "batch_progress_dialog"
  end tell
end tell
''')
        print("  Paste was stuck — let it finish with the render deferred.")
        time.sleep(3)
    else:
        box = _progress_window()
    if box:
        # Stop EVERY render dialog by its button name, then make sure they are
        # gone. The old coordinate click could miss, and the render requested
        # below then ran alongside the wedged one: on 2026-10-18 two renders
        # (31 and 51 of 228) fought each other all evening, which looked like
        # stalls and counts running backwards.
        for _ in range(3):
            n = _stop_render_dialogs()
            if n == 0:
                break
            print(f"  Stopped {n} render dialog(s).")
            time.sleep(3)
        if _render_dialog_count():
            print("  A render dialog would not close — not starting another "
                  "on top of it. Needs a hand in Lightroom.")
            sys.exit(1)
    elif "Pasting" not in label:
        print("  No progress dialog up — nothing to dismiss.")

    grid_view()
    time.sleep(1.5)
    lr_menu_click("Edit", "Select All")
    time.sleep(1.0)
    render_ai_settings(expect=args.expect)


ADD_BUTTON_SCRIPT = '''
tell application "System Events" to tell process "Adobe Lightroom"
  repeat with w in every window
    repeat with b in (every button of w)
      try
        set t to (value of attribute "AXTitle" of b) as text
        if t starts with "Add " and t contains "Photo" then
          %s
        end if
      end try
    end repeat
  end repeat
  return ""
end tell'''


def _add_button(click: bool = False) -> str:
    """The title of Lightroom's "Add N Photos" button, clicking it if asked.
    A real accessibility button — pressing it needs no keystroke and no
    particular app in front."""
    action = "click b\n          return t" if click else "return t"
    try:
        return run_applescript(ADD_BUTTON_SCRIPT % action)
    except RuntimeError:
        return ""


def _wait(pred, timeout: float, step: float = 0.5):
    end = time.time() + timeout
    while time.time() < end:
        got = pred()
        if got:
            return got
        time.sleep(step)
    return pred()


def cmd_import(args):
    """Add a batch folder to Lightroom, with no keystrokes.

    Handing the folder to Lightroom with `open -a` (what dropping it on the
    Dock icon does) opens its import review with an "Add N Photos" button,
    which accessibility can press. The old route drove the Add Photos file
    panel with Cmd-Shift-G, Cmd-V and three Returns. On 2026-11-22 the panel
    ignored Cmd-Shift-G, the keys went nowhere useful, the panel sat open, and
    the run still printed "Imported" and went on to auto-tone whatever
    Lightroom happened to be showing. Every step here is checked instead."""
    folder = Path(args.folder) if args.folder else _latest_batch()
    if not folder or not folder.exists():
        print(f"Folder not found: {folder}")
        sys.exit(1)
    raws = [f for f in folder.iterdir() if f.suffix.lower() in RAW_SUFFIXES]

    print(f"Importing {folder.name} into Lightroom…")

    if not lr_is_running():
        print("Opening Lightroom…")
        subprocess.Popen(["open", "-a", "Adobe Lightroom"])
        time.sleep(8)

    if _add_button():
        print("  An import review is already open — not stacking another on it. "
              "Cancel it in Lightroom, then Retry.")
        sys.exit(1)

    subprocess.run(["open", "-a", "Adobe Lightroom", str(folder)], check=False)
    title = _wait(_add_button, 60)
    if not title:
        print("✗ Lightroom never showed its \"Add Photos\" review for this folder.")
        sys.exit(1)
    m = re.search(r"(\d+)", title)
    offered = int(m.group(1)) if m else None
    print(f"  Lightroom offers: {title} ({len(raws)} RAWs in the folder)")
    if offered is not None and raws and offered < len(raws):
        print(f"  ⚠ {len(raws) - offered} RAW(s) not offered — likely already in the catalogue.")

    if _add_button(click=True) != title:
        print("✗ Couldn't press the Add button.")
        sys.exit(1)
    if _wait(lambda: not _add_button(), 30):
        print(f"Imported: {folder}")
    else:
        print("✗ The import review is still open after pressing Add.")
        sys.exit(1)
    print("Lightroom is loading the photos — wait a moment, then run Auto-tone.")


def cmd_auto_tone(args):
    """Select all + apply Auto Settings in Lightroom."""
    if not lr_is_running():
        print("Lightroom is not running. Import photos first.")
        sys.exit(1)

    run_applescript('tell application "Adobe Lightroom" to activate')
    time.sleep(0.3)

    # Briefly switch to Grid so Apply Auto Settings hits all photos, not just active
    print("Switching to Grid view…")
    grid_view()
    time.sleep(0.8)

    print("Selecting all photos…")
    lr_menu_click("Edit", "Select All")
    time.sleep(0.5)

    print("Applying Auto Settings to all selected…")
    run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    set photoMenu to menu "Photo" of menu bar item "Photo" of menu bar 1
    repeat with mi in every menu item of photoMenu
      try
        if name of mi starts with "Apply Auto Settings" then
          click mi
          exit repeat
        end if
      end try
    end repeat
  end tell
end tell
''')

    # Switch back to detail/carousel view
    print("Switching back to detail view…")
    run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    keystroke "e"
    delay 0.5
  end tell
end tell
''')
    print("Auto-tone done.")


FIND_AND_CLICK = '''
on findAndClick(root, targetName, depth)
  if depth > 12 then return "deep"
  -- System Events terminology (click, UI element, …) only compiles inside
  -- a tell block, so the handler body lives in one
  tell application "System Events"
    try
      set n to name of root
      if n is targetName then
        set v to value of root
        if v is 0 then
          click root
          return "clicked"
        else
          return "already_on"
        end if
      end if
    end try
    try
      repeat with child in every UI element of root
        set r to my findAndClick(child, targetName, depth + 1)
        if r is "clicked" or r is "already_on" then return r
      end repeat
    end try
  end tell
  return "not_found"
end findAndClick

tell application "System Events"
  tell process "Adobe Lightroom"
    repeat with w in every window
      set r to my findAndClick(w, "%s", 0)
      if r is "clicked" or r is "already_on" then return r
    end repeat
    return "not_found"
  end tell
end tell
'''


def _toggle_checkbox(name: str) -> str:
    """Find a named checkbox in LR's window and turn it on. Returns the result."""
    return run_applescript(FIND_AND_CLICK % name)


def cmd_ai_denoise(args):
    """Enable Denoise (and Remove Chromatic Aberration) on the current photo.
    Exits nonzero unless Denoise is verifiably ON, so the server can fall back
    to the manual step instead of silently exporting noisy photos."""
    if not lr_is_running():
        print("Lightroom is not running.")
        sys.exit(1)

    run_applescript('tell application "Adobe Lightroom" to activate')
    time.sleep(0.3)
    # Detail view — the Edit panel (and its Denoise checkbox) lives there
    run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    keystroke "e"
    delay 0.8
  end tell
end tell
''')

    result = _toggle_checkbox("Denoise")
    print(f"Denoise accessibility result: {result}")

    if result == "not_found":
        # Fallback: cliclick on the Denoise checkbox position in the right panel
        print("Accessibility search failed — trying coordinate-based click…")
        CLICLICK = "/opt/homebrew/bin/cliclick"
        if not Path(CLICLICK).exists():
            print("cliclick not found — run: brew install cliclick")
            sys.exit(1)

        bounds = run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    set p to position of window 1
    set s to size of window 1
    return ((item 1 of p) as string) & "," & ((item 2 of p) as string) & "," & ((item 1 of s) as string) & "," & ((item 2 of s) as string)
  end tell
end tell
''')
        wx, wy, ww, wh = (int(v.strip()) for v in bounds.split(","))

        # Denoise checkbox is in the right Edit panel, Detail section
        # Right panel is ~265px wide; checkbox is roughly 60% down the panel height
        cx = wx + ww - 250   # near left edge of right panel
        cy = wy + int(wh * 0.55)   # rough vertical position of Detail section
        print(f"Clicking at ({cx}, {cy}) for Denoise checkbox…")
        subprocess.run([CLICLICK, f"c:{cx},{cy}"])
        time.sleep(1.0)
        # Trust but verify — a missed click must not read as success
        result = _toggle_checkbox("Denoise")
        print(f"Post-click verification: {result}")

    if result not in ("clicked", "already_on"):
        print("Could not confirm Denoise is enabled.")
        sys.exit(1)

    # Best-effort: CA removal checkbox too (Lens Corrections section). Failure
    # is fine — it may be collapsed; denoise is the part that matters.
    try:
        ca = _toggle_checkbox("Remove Chromatic Aberration")
        print(f"Chromatic Aberration result: {ca}")
    except RuntimeError as e:
        print(f"Chromatic Aberration skipped: {e}")

    print("Denoise enabled. Processing…")


def cmd_copy_and_paste(args):
    """Copy edit settings from current photo and paste to all selected.

    Before running: in Lightroom, edit ONE photo with your desired
    Denoise and Remove Chromatic Aberration settings, then run this.
    """
    if not lr_is_running():
        print("Lightroom is not running.")
        sys.exit(1)

    run_applescript('tell application "Adobe Lightroom" to activate')
    time.sleep(0.5)

    # Ensure we're in detail/develop view so edit settings are available
    run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    keystroke "e"
    delay 0.5
  end tell
end tell
''')

    print("Copying edit settings from current photo…")
    copied = False
    for name in ("Copy Edit Settings", "Copy Settings", "Copy Develop Settings"):
        try:
            lr_menu_click("Photo", name)
            copied = True
            # Accept any copy-settings dialog that may appear
            time.sleep(0.6)
            run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    key code 36
    delay 0.3
  end tell
end tell
''')
            print(f"  Copied via menu: {name}")
            break
        except RuntimeError:
            continue

    if not copied:
        print("  Menu item not found — using Cmd+Shift+C keyboard shortcut…")
        run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    keystroke "c" using {command down, shift down}
    delay 0.8
    key code 36
    delay 0.3
  end tell
end tell
''')

    time.sleep(0.3)
    # Select All in Detail view left one photo selected on 2026-11-22 (10/24,
    # 36 photos): the paste reached only the photo it was copied from, and
    # the batch would have exported noisy. The grid selects the whole album —
    # and the count is checked before anything is pasted.
    print("Selecting all photos…")
    got = None
    for _ in range(2):
        grid_view()
        time.sleep(1.0)
        lr_menu_click("Edit", "Select All")
        time.sleep(0.6)
        got = _menu_item_count("Photo", "Create Album with", r"with ([\d,]+) Photo")
        if args.expect is None or got == args.expect:
            break
    if args.expect is not None and got != args.expect:
        print(f"Refusing to paste: the selection is {got} photos, the batch is {args.expect}. "
              "Is Lightroom showing the batch?")
        sys.exit(1)
    print(f"  {got} photos selected.")

    print("Pasting to entire selection…")
    pasted = False
    for name in ("Paste to Entire Selection", "Paste Settings to Entire Selection", "Paste Edit Settings"):
        try:
            lr_menu_click("Photo", name)
            pasted = True
            print(f"  Pasted via menu: {name}")
            break
        except RuntimeError:
            continue

    if not pasted:
        print("  Menu item not found — using Cmd+Shift+V keyboard shortcut…")
        run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    keystroke "v" using {command down, shift down}
    delay 0.3
  end tell
end tell
''')

    _confirm_affected(args.expect or got)

    # The paste only sets the checkbox; this makes Lightroom actually
    # render it, which is the difference between a denoised export and
    # a noisy one that claimed to be denoised. On a big batch Lightroom shows
    # its paste dialog and renders by itself, and render_ai_settings() stands
    # down rather than stack a second render — so give the dialog time to show.
    time.sleep(4)
    render_ai_settings(expect=args.expect)

    print("Done — denoise + CA settings applied to all. Now pick your best shot.")


def _confirm_affected(expect, wait=8.0):
    """Lightroom asks "<N> Photos Will Be Affected — Are you sure you want to
    continue?" before a big paste. Unanswered, nothing is pasted: on
    2026-11-27 it sat there while the run reported Denoise done in 80s for
    109 photos and the spot check found it off on every one. Click OK by name
    (a click, not a keystroke, so it is safe with Lightroom in the
    background) — but only when N is this batch's own count."""
    end = time.time() + wait
    while time.time() < end:
        try:
            out = run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    set ns to name of every window
    repeat with n in ns
      if (n as text) ends with "Photos Will Be Affected" then return n as text
    end repeat
  end tell
end tell
return ""
''').strip()
        except RuntimeError:
            out = ""
        if out:
            m = re.match(r"(\d+) Photos Will Be Affected$", out)
            if not m or int(m.group(1)) != expect:
                print(f"  Lightroom asks: {out!r} — not this batch's {expect}, leaving it for you.")
                return False
            run_applescript(f'''
tell application "System Events" to tell process "Adobe Lightroom" to click button "OK" of window "{out}"
''')
            print(f"  Confirmed Lightroom's \"{out}\" prompt (OK).")
            return True
        time.sleep(0.5)
    return False


LR_PREFS = (Path.home() / "Library/Containers/com.adobe.mas.lightroomCC/Data"
            "/Library/Preferences/com.adobe.mas.lightroomCC")


def _lr_export_folder():
    """Where Lightroom's Export with Previous will write, or None if unknown.

    Lightroom's prefs live in its sandbox container, and macOS guards other
    apps' containers: reading them can raise a "python3.12 would like to
    access data from other apps" prompt, and `defaults` blocks until someone
    answers it. On 2026-11-22 that held the export (and with it every bridge
    command) until the server timed out. Never wait on it — an unanswered or
    denied prompt just means the folder check is skipped."""
    try:
        r = subprocess.run(["defaults", "read", str(LR_PREFS),
                            "AgExport_File_destinationFolder"],
                           capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        print("  (couldn't read Lightroom's export folder — macOS is asking "
              "whether python may read other apps' data; skipping that check)")
        return None
    return r.stdout.strip() or None


def cmd_export(args):
    """Trigger Export with Previous settings in Lightroom."""
    if not lr_is_running():
        print("Lightroom is not running.")
        sys.exit(1)

    # "Export with Previous" goes wherever the last manual export went. On
    # 2026-10-19 that had become ~/Documents/birbs: the sandboxed (App Store)
    # Lightroom holds no folder grant there, so all 228 failed with "The file
    # could not be created" — and the pipeline watches ~/Desktop/birbs anyway.
    # Reading it pops macOS's "access data from other apps" prompt on every
    # export (2026-11-22, twice), so it's opt-in. The app catches a wrong
    # folder anyway: an export that lands nothing in birbs times out.
    dest = _lr_export_folder() if os.environ.get("LR_CHECK_EXPORT_FOLDER") == "1" else None
    if dest and Path(dest) != BIRDS_DIR:
        print(f"Refusing to export: Lightroom's Export with Previous goes to "
              f"{dest}, not {BIRDS_DIR}. Export once by hand to {BIRDS_DIR} "
              f"to reset it.")
        sys.exit(1)

    # No activate: every step below is a menu or button click, which works with
    # Lightroom in the background, so an export never takes focus from Scott.
    if args.all:
        # Hands-off mode: export the whole batch — culling happens later on
        # the posting screen. Grid view first so Select All grabs every photo.
        print("Selecting all photos…")
        grid_view()
        time.sleep(0.8)
        lr_menu_click("Edit", "Select All")
        time.sleep(0.5)
        # Same trap as the render: Select All grabs whatever view is open.
        if args.expect is not None:
            got = _menu_item_count("Photo", "Update AI Settings")
            if got is None:
                # That item loses its count once every render is done — the
                # normal state before an export. "Create Album with N Photos"
                # always counts the selection.
                got = _menu_item_count("Photo", "Create Album with", r"with ([\d,]+) Photo")
            if got != args.expect:
                print(f"Refusing to export: selection is {got} photos, the "
                      f"batch is {args.expect}. Is Lightroom showing the batch?")
                sys.exit(1)

    print("Triggering Export with Previous…")
    lr_menu_click("File", "Export with Previous...")
    # Photos whose AI settings (Denoise) never finished rendering make
    # Lightroom ask "AI Update Required" first. Its Export button sends them
    # out un-denoised (the Nov 10 owls scored 5-10 on noise vs ~2), so never
    # take it: cancel, and let the caller render them one by one and retry.
    if _cancel_ai_update_dialog():
        print(f"{AI_UPDATE_MARKER}: some photos' Denoise hadn't rendered. "
              "Cancelled the export rather than send them out noisy.")
        sys.exit(3)
    print(f"Export started → check {BIRDS_DIR}")


AI_UPDATE_MARKER = "AI UPDATE REQUIRED"


def _cancel_ai_update_dialog(wait: float = 8.0) -> bool:
    """Click Cancel — by name, never the default Export — on Lightroom's
    "AI Update Required" dialog if it shows up within `wait` seconds."""
    t0 = time.time()
    while time.time() - t0 < wait:
        try:
            names = run_applescript('tell application "System Events" to tell '
                                    'process "Adobe Lightroom" to get name of every window')
        except RuntimeError:
            names = ""
        if "AI Update Required" in names:
            run_applescript('tell application "System Events" to tell process '
                            '"Adobe Lightroom" to click button "Cancel" of window '
                            '"AI Update Required"')
            time.sleep(1.5)
            return True
        time.sleep(1)
    return False


def cmd_status(args):
    """Dump Lightroom's visible state via accessibility: windows, progress
    dialogs (with their text and %), and any dialog buttons. Lets the web UI
    show what Lightroom is doing when you can't see the screen."""
    if not lr_is_running():
        print("Lightroom is not running.")
        return
    out = run_applescript('''
tell application "System Events"
  set frontApp to name of first process whose frontmost is true
  tell process "Adobe Lightroom"
    set out to "frontmost app: " & frontApp & linefeed
    repeat with w in every window
      set wn to name of w
      if wn is missing value or wn is "" then set wn to "(unnamed)"
      set out to out & "window: " & wn & linefeed
      try
        repeat with t in every static text of w
          set tv to value of t
          if tv is not missing value and tv is not "" then set out to out & "    " & tv & linefeed
        end repeat
      end try
      try
        repeat with b in every button of w
          set bn to name of b
          if bn is not missing value then set out to out & "    [button: " & bn & "]" & linefeed
        end repeat
      end try
      try
        repeat with sh in every sheet of w
          set out to out & "  dialog open:" & linefeed
          try
            repeat with t in every static text of sh
              set out to out & "    " & (value of t) & linefeed
            end repeat
          end try
          try
            repeat with b in every button of sh
              set out to out & "    [button: " & (name of b) & "]" & linefeed
            end repeat
          end try
        end repeat
      end try
    end repeat
    return out
  end tell
end tell''')
    print(out)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _latest_batch() -> Path | None:
    """Find the oldest unprocessed YYYY-MM-DD-* folder in Downloads."""
    import re
    pat = re.compile(r'^\d{4}-\d{2}-\d{2}')
    folders = sorted([d for d in DOWNLOADS.iterdir()
                      if d.is_dir() and pat.match(d.name)])
    return folders[0] if folders else None


RENDER_EACH_LIMIT = 240   # one photo renders in ~30s; 100s+ happens; 4 min is a wedge


def _selection_header() -> str:
    """The grid header's "1 of 228 photos" — proof of view and selection."""
    return run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    repeat with b in (every button of group 2 of window "Adobe Lightroom")
      try
        set n to name of b
        if n contains " photos" then return n
      end try
    end repeat
    return ""
  end tell
end tell''').strip()


def _arrow(code: int, pause: float = 0.9):
    run_applescript('tell application "Adobe Lightroom" to activate')
    time.sleep(0.3)
    run_applescript(f'tell application "System Events" to key code {code}')
    time.sleep(pause)


def cmd_render_each(args):
    """Render pending AI settings one photo at a time — the fallback when the
    batch render keeps wedging.

    On 2026-10-18 a 228-photo batch render froze at ~29 on every restart, while
    the same photos rendered fine one by one (the slow one took 103s alone).
    Guards before each photo — any failing stops the run, never guesses:
    the header reads "1 of N photos", the menu reads "in 1 Photo", and no
    render dialog is up. A photo still rendering after RENDER_EACH_LIMIT is
    stopped, reported, and skipped, so one bad photo can't hold up the rest.
    """
    n = args.expect
    if not n:
        print("render-each needs --expect <batch size>.")
        sys.exit(1)
    for _ in range(3):                  # the wedged batch render, if any
        if not _render_dialog_count():
            break
        _stop_render_dialogs()
        time.sleep(3)
    if _render_dialog_count():
        print("A render dialog won't close — not rendering on top of it.")
        sys.exit(1)
    grid_view()
    time.sleep(1.5)
    _arrow(124); _arrow(123)            # collapse any multi-selection to one
    for _ in range(n + 2):              # left arrow clamps at the first photo
        run_applescript('tell application "System Events" to key code 123')
        time.sleep(0.12)
    time.sleep(1)

    bad, rendered = [], 0
    for i in range(1, n + 1):
        h = _selection_header()
        if h != f"1 of {n} photos":
            print(f"Stopped at photo {i}: header reads {h!r}, not '1 of {n} photos'.")
            sys.exit(1)
        if _render_dialog_count():
            print(f"Stopped at photo {i}: a render dialog is already up.")
            sys.exit(1)
        name = _click_menu_item_by_prefix("Photo", "Update AI Settings") \
            if _menu_item_count("Photo", "Update AI Settings") == 1 else None
        if name:
            t0 = time.time()
            while time.time() - t0 < 10 and not _render_dialog_count():
                time.sleep(1)
            # Only a render we actually saw counts. On 2026-11-23 no dialog ever
            # appeared (the photos had no Denoise to render), the 10s wait ran
            # out, and the elapsed-time test reported "Rendered 27".
            seen = bool(_render_dialog_count())
            while _render_dialog_count() and time.time() - t0 < RENDER_EACH_LIMIT:
                time.sleep(3)
            if _render_dialog_count():
                print(f"  photo {i}: still rendering after {RENDER_EACH_LIMIT}s — skipped")
                for _ in range(3):
                    _stop_render_dialogs()
                    time.sleep(3)
                    if not _render_dialog_count():
                        break
                if _render_dialog_count():
                    print(f"Stopped at photo {i}: its render dialog won't close.")
                    sys.exit(1)
                bad.append(i)
            elif seen:
                rendered += 1
        if i < n:
            _arrow(124)
    print(f"Rendered {rendered} photo(s) one by one.")
    if rendered == 0:
        print("Nothing rendered — if these photos should be denoised, Denoise "
              "isn't ticked on them: tick it on one, copy-and-paste to all.")
    if bad:
        print(f"Could not render photo(s) {bad} (grid order) — check them by eye.")


# ── Main ──────────────────────────────────────────────────────────────────────

def cmd_click_at(args):
    """One click inside Lightroom's main window, at (fx, fy) as fractions of
    its size — for the sidebar, which isn't in the accessibility tree (e.g.
    "Recently Added > Yesterday 7:38 PM" to get back to an older batch). Waits
    until the Mac is idle and refuses unless Lightroom is frontmost, so the
    click can never land in another app."""
    if not (0 < args.fx < 1 and 0 < args.fy < 1):
        print("fx/fy must be fractions strictly between 0 and 1")
        sys.exit(2)
    ensure_lr_front()
    out = run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    set w to window "Adobe Lightroom"
    set p to position of w
    set s to size of w
    return ((item 1 of p) as string) & " " & ((item 2 of p) as string) & " " & ¬
           ((item 1 of s) as string) & " " & ((item 2 of s) as string)
  end tell
end tell
''').split()
    if len(out) != 4:
        print("Couldn't read Lightroom's window bounds")
        sys.exit(1)
    x, y, w, h = (int(float(v)) for v in out)
    cx, cy = x + round(w * args.fx), y + round(h * args.fy)
    if lr_frontmost() != "Adobe Lightroom":
        print("Lightroom lost focus — not clicking.")
        sys.exit(1)
    subprocess.run([CLICLICK, f"c:{cx},{cy}"], check=True, timeout=10)
    print(f"  Clicked {cx},{cy} ({args.fx:.3f}, {args.fy:.3f} of the window)")


def main():
    p = argparse.ArgumentParser(description="Lightroom UI automation for bird photos")
    p.add_argument("--folder", help="Path to batch folder (for import command)")
    p.add_argument("--all", action="store_true",
                   help="export: Select All before Export with Previous")
    p.add_argument("--expect", type=int,
                   help="batch size: only render AI settings on a selection of exactly this many")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("import",         help="Open a batch folder in Lightroom's Add Photos dialog")
    sub.add_parser("auto-tone",      help="Select all + Auto Settings in Lightroom")
    sub.add_parser("ai-denoise",     help="Trigger AI Denoise via right-click → Enhance")
    sub.add_parser("copy-and-paste", help="Copy edits from current photo → paste to all selected")
    sub.add_parser("export",         help="Export with Previous settings in Lightroom")
    sub.add_parser("status",         help="Dump Lightroom windows/dialogs via accessibility")
    sub.add_parser("ai-resume",      help="Unstick and restart a stalled AI-settings render")
    sub.add_parser("render-each",    help="Render pending AI settings one photo at a time")
    ck = sub.add_parser("click-at",  help="Click a spot in Lightroom's main window (fractions of its size)")
    ck.add_argument("--fx", type=float, required=True)
    ck.add_argument("--fy", type=float, required=True)

    args = p.parse_args()
    {
        "import":         cmd_import,
        "auto-tone":      cmd_auto_tone,
        "ai-denoise":     cmd_ai_denoise,
        "copy-and-paste": cmd_copy_and_paste,
        "export":         cmd_export,
        "status":         cmd_status,
        "ai-resume":      cmd_ai_resume,
        "render-each":    cmd_render_each,
        "click-at":       cmd_click_at,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
