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
import re
import subprocess
import sys
import time
from pathlib import Path

DOWNLOADS    = Path.home() / "Downloads"
BIRDS_DIR    = Path.home() / "Desktop" / "birbs"   # Lightroom's "Export with Previous" target
RAW_SUFFIXES = {".nef", ".arw", ".cr2", ".cr3", ".dng", ".raf", ".rw2", ".orf", ".pef"}


# ── AppleScript helpers ────────────────────────────────────────────────────────

def run_applescript(script: str) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


def lr_is_running() -> bool:
    try:
        run_applescript('tell application "System Events" to get name of process "Adobe Lightroom"')
        return True
    except RuntimeError:
        return False


def lr_menu_click(menu_name: str, item_name: str) -> None:
    run_applescript(f'''
tell application "System Events"
  tell process "Adobe Lightroom"
    click menu item "{item_name}" of menu "{menu_name}" of menu bar item "{menu_name}" of menu bar 1
  end tell
end tell
''')


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


def _menu_item_count(menu_name: str, prefix: str):
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
    m = re.search(r"in ([\d,]+)", name)
    return int(m.group(1).replace(",", "")) if m else None


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

    run_applescript('tell application "Adobe Lightroom" to activate')
    time.sleep(0.5)
    run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    keystroke "g"
    delay 1.5
  end tell
end tell
''')
    lr_menu_click("Edit", "Select All")
    time.sleep(1.0)
    render_ai_settings(expect=args.expect)


def cmd_import(args):
    """Open a batch folder in Lightroom's Add Photos dialog."""
    folder = Path(args.folder) if args.folder else _latest_batch()
    if not folder or not folder.exists():
        print(f"Folder not found: {folder}")
        sys.exit(1)

    print(f"Importing {folder.name} into Lightroom…")

    if not lr_is_running():
        print("Opening Lightroom…")
        subprocess.Popen(["open", "-a", "Adobe Lightroom"])
        time.sleep(4)

    # Bring Lightroom to front
    run_applescript('tell application "Adobe Lightroom" to activate')
    time.sleep(0.5)

    # File > Add Photos...
    lr_menu_click("File", "Add Photos...")
    time.sleep(1.5)

    # Cmd+Shift+G → paste path → Return (navigate) → Return (click default button)
    # Default button label varies: "Review for Import" or "Add N Photos"
    subprocess.run(["pbcopy"], input=str(folder), text=True)
    run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    keystroke "g" using {command down, shift down}
    delay 1.0
    keystroke "v" using command down
    delay 0.5
    key code 36
    delay 2.5
    key code 36
    delay 2.5
    key code 36
    delay 1.0
  end tell
end tell
''')
    print(f"Imported: {folder}")
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
    run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    keystroke "g"
    delay 0.8
  end tell
end tell
''')

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
    print("Selecting all photos…")
    lr_menu_click("Edit", "Select All")
    time.sleep(0.3)

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

    # The paste only sets the checkbox; this makes Lightroom actually
    # render it, which is the difference between a denoised export and
    # a noisy one that claimed to be denoised. On a big batch Lightroom shows
    # its paste dialog and renders by itself, and render_ai_settings() stands
    # down rather than stack a second render — so give the dialog time to show.
    time.sleep(4)
    render_ai_settings(expect=args.expect)

    print("Done — denoise + CA settings applied to all. Now pick your best shot.")


LR_PREFS = (Path.home() / "Library/Containers/com.adobe.mas.lightroomCC/Data"
            "/Library/Preferences/com.adobe.mas.lightroomCC")


def _lr_export_folder():
    """Where Lightroom's Export with Previous will write, or None if unknown."""
    r = subprocess.run(["defaults", "read", str(LR_PREFS),
                        "AgExport_File_destinationFolder"],
                       capture_output=True, text=True)
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
    dest = _lr_export_folder()
    if dest and Path(dest) != BIRDS_DIR:
        print(f"Refusing to export: Lightroom's Export with Previous goes to "
              f"{dest}, not {BIRDS_DIR}. Export once by hand to {BIRDS_DIR} "
              f"to reset it.")
        sys.exit(1)

    run_applescript('tell application "Adobe Lightroom" to activate')
    time.sleep(0.3)

    if args.all:
        # Hands-off mode: export the whole batch — culling happens later on
        # the posting screen. Grid view first so Select All grabs every photo.
        print("Selecting all photos…")
        run_applescript('''
tell application "System Events"
  tell process "Adobe Lightroom"
    keystroke "g"
    delay 0.8
  end tell
end tell
''')
        lr_menu_click("Edit", "Select All")
        time.sleep(0.5)
        # Same trap as the render: Select All grabs whatever view is open.
        if args.expect is not None:
            got = _menu_item_count("Photo", "Update AI Settings")
            if got != args.expect:
                print(f"Refusing to export: selection is {got} photos, the "
                      f"batch is {args.expect}. Is Lightroom showing the batch?")
                sys.exit(1)

    print("Triggering Export with Previous…")
    lr_menu_click("File", "Export with Previous...")
    print(f"Export started → check {BIRDS_DIR}")


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
    run_applescript('tell application "Adobe Lightroom" to activate')
    run_applescript('tell application "System Events" to tell process "Adobe Lightroom" to keystroke "g"')
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
            elif time.time() - t0 > 10:
                rendered += 1
        if i < n:
            _arrow(124)
    print(f"Rendered {rendered} photo(s) one by one.")
    if bad:
        print(f"Could not render photo(s) {bad} (grid order) — check them by eye.")


# ── Main ──────────────────────────────────────────────────────────────────────

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
    }[args.cmd](args)


if __name__ == "__main__":
    main()
