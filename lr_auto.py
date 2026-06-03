#!/usr/bin/env python3
"""
lr_auto.py — Lightroom UI automation for birb batch editing.

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
  7. Web UI: Export — Export with Previous → ~/Desktop/birbs/
  8. Web UI: Post — fills in species/location/date, queues to Buffer
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

DOWNLOADS    = Path.home() / "Downloads"
BIRBS_DIR    = Path.home() / "Desktop" / "birbs"
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


def cmd_ai_denoise(args):
    """Check the Denoise checkbox in Lightroom's Edit panel for all photos."""
    if not lr_is_running():
        print("Lightroom is not running.")
        sys.exit(1)

    # Try accessibility first — recursive search for Denoise checkbox
    result = run_applescript('''
on findAndClick(root, targetName, depth)
  if depth > 8 then return "deep"
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
  return "not_found"
end findAndClick

tell application "System Events"
  tell process "Adobe Lightroom"
    return my findAndClick(window 1, "Denoise", 0)
  end tell
end tell
''')
    print(f"Accessibility result: {result}")

    if result in ("clicked", "already_on"):
        if result == "clicked":
            print("Denoise enabled via accessibility. Processing…")
        else:
            print("Denoise was already enabled.")
        return

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
    time.sleep(0.5)
    print("Denoise click sent. Check Lightroom to confirm.")


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

    print("Done — denoise + CA settings applied to all. Now pick your best shot.")


def cmd_export(args):
    """Trigger Export with Previous settings in Lightroom."""
    if not lr_is_running():
        print("Lightroom is not running.")
        sys.exit(1)

    run_applescript('tell application "Adobe Lightroom" to activate')
    time.sleep(0.3)

    print("Triggering Export with Previous…")
    lr_menu_click("File", "Export with Previous...")
    print(f"Export started → check {BIRBS_DIR}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _latest_batch() -> Path | None:
    """Find the oldest unprocessed YYYY-MM-DD-* folder in Downloads."""
    import re
    pat = re.compile(r'^\d{4}-\d{2}-\d{2}')
    folders = sorted([d for d in DOWNLOADS.iterdir()
                      if d.is_dir() and pat.match(d.name)])
    return folders[0] if folders else None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Lightroom UI automation for birb photos")
    p.add_argument("--folder", help="Path to batch folder (for import command)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("import",         help="Open a batch folder in Lightroom's Add Photos dialog")
    sub.add_parser("auto-tone",      help="Select all + Auto Settings in Lightroom")
    sub.add_parser("ai-denoise",     help="Trigger AI Denoise via right-click → Enhance")
    sub.add_parser("copy-and-paste", help="Copy edits from current photo → paste to all selected")
    sub.add_parser("export",         help="Export with Previous settings in Lightroom")

    args = p.parse_args()
    {
        "import":         cmd_import,
        "auto-tone":      cmd_auto_tone,
        "ai-denoise":     cmd_ai_denoise,
        "copy-and-paste": cmd_copy_and_paste,
        "export":         cmd_export,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
