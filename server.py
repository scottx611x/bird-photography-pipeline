#!/usr/bin/env python3
"""
server.py — Birb workflow web UI (runs in Docker).
Open http://localhost:8765 in your browser.

Architecture:
  Docker (this server) ←→ host.docker.internal:8766 ←→ lr_host.py (Mac)
  lr_host.py runs lr_auto.py for Lightroom UI automation.
"""

import json
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import httpx
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

app = Flask(__name__)

# ── Paths (Docker volume mounts) ──────────────────────────────────────────────
DOWNLOADS    = Path("/downloads")
BIRBS_DIR    = Path("/birbs")
RAW_SUFFIXES = {".nef", ".arw", ".cr2", ".cr3", ".dng", ".raf", ".rw2", ".orf", ".pef"}
BATCH_RE     = re.compile(r"^(\d{4}-\d{1,2}-\d{1,2})(?:-(.+))?$")
HOST_BRIDGE  = "http://host.docker.internal:8766"
MAC_HOME     = os.environ.get("MAC_HOME", "/Users/scott")
STATE_FILE   = DOWNLOADS / ".birb_state.json"

# ── State ─────────────────────────────────────────────────────────────────────

state = {
    "batches":       {},
    "active":        None,
    "proc_step":     None,
    "new_birbs":     [],
    "log":           deque(maxlen=300),
    "host_ok":       None,
    "last_post":     None,
    "thread_active":   False,
    "stop_requested":  False,
    "syno_skipped":    set(),   # Synology album names hidden from the list
    "done_albums":     set(),   # posted album names — stay hidden even if local folder is deleted
    "syno_fetching":   {},      # album -> {got, total} while a download is in progress
}

# In-memory cache of the Synology album list (avoid hammering the NAS each load)
_syno_cache = {"at": 0.0, "albums": None}
SYNO_CACHE_TTL = 300  # seconds
lock    = threading.Lock()
log_seq = 0   # monotonically increases with every log() call


def save_state():
    try:
        # Remember posted albums permanently — local folders get deleted for disk
        # space, but a posted album should never resurface as a fetchable card.
        state["done_albums"].update(
            name for name, b in state["batches"].items() if b.get("status") == "done")
        data = {
            "statuses":  {name: b["status"] for name, b in state["batches"].items()},
            "birbs":     {name: b.get("birbs", []) for name, b in state["batches"].items()},
            "posted_due":{name: b.get("posted_due","") for name, b in state["batches"].items()},
            "active":    state["active"],
            "proc_step": state["proc_step"],
            "new_birbs": list(state["new_birbs"]),
            "syno_skipped": sorted(state["syno_skipped"]),
            "done_albums": sorted(state["done_albums"]),
        }
        STATE_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def load_saved_statuses() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {}


def log(msg: str):
    global log_seq
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with lock:
        state["log"].append(line)
        log_seq += 1
    print(line)


# ── Batch helpers ─────────────────────────────────────────────────────────────

def scan_batches():
    """Scan /downloads for YYYY-MM-DD-* folders and update state."""
    if not DOWNLOADS.exists():
        return

    # Do all I/O outside the lock so requests are never blocked
    new_batches = {}
    try:
        entries = list(DOWNLOADS.iterdir())
    except Exception:
        return

    for d in entries:
        if not d.is_dir():
            continue
        m = BATCH_RE.match(d.name)
        if not m:
            continue
        name = d.name
        with lock:
            already = name in state["batches"]
        if already:
            continue

        # I/O outside lock
        date_str = m.group(1)
        suffix   = m.group(2) or "best"
        try:
            raws = [f for f in d.iterdir() if f.suffix.lower() in RAW_SUFFIXES]
        except Exception:
            raws = []
        location = "Rea St." if (suffix == "best" or suffix.startswith("best")) else suffix.replace("-", " ").title()
        # Normalize date before parsing (handles both 2026-5-3 and 2026-05-03)
        y, mo, day = date_str.split("-")
        date_norm = f"{y}-{mo.zfill(2)}-{day.zfill(2)}"
        dt        = datetime.strptime(date_norm, "%Y-%m-%d")
        cap_date  = f"{dt.month}-{dt.day}-{str(dt.year)[2:]}"
        new_batches[name] = {
            "folder":    name,
            "path":      str(d),
            "date":      date_str,
            "cap_date":  cap_date,
            "location":  location,
            "raw_count": len(raws),
            "status":    "pending",
        }

    # Brief lock hold — no I/O; restore persisted status if available
    if new_batches:
        saved = load_saved_statuses()
        restored_msg = None
        with lock:
            for name, batch in new_batches.items():
                if name not in state["batches"]:
                    statuses = saved.get("statuses", saved)  # handle old format
                    batch["status"] = statuses.get(name, "pending")
                    batch["birbs"]      = saved.get("birbs", {}).get(name, [])
                    batch["posted_due"] = saved.get("posted_due", {}).get(name, "")
                    state["batches"][name] = batch
            # Restore active run — log() call moved outside lock to avoid deadlock
            if not state["active"] and saved.get("active") and saved["active"] in state["batches"]:
                state["active"]    = saved["active"]
                state["proc_step"] = saved.get("proc_step")
                state["new_birbs"] = saved.get("new_birbs", [])
                restored_msg = f"Restored active run: {state['active']} at step {state['proc_step']}"
        if restored_msg:
            log(restored_msg)
        for name, batch in new_batches.items():
            log(f"New batch: {name} ({batch['raw_count']} RAWs)")


def batch_host_path(folder_name: str) -> str:
    """Convert the Docker /downloads path to the Mac ~/Downloads path."""
    return str(Path.home() / "Downloads" / folder_name)


# ── Host bridge ───────────────────────────────────────────────────────────────

def call_host(cmd: str, folder: str = None, body: dict = None, timeout: float = 90) -> dict:
    data = dict(body or {})
    if folder:
        data["folder"] = folder
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(f"{HOST_BRIDGE}/run/{cmd}", json=data)
        return r.json()
    except httpx.ConnectError:
        return {"ok": False, "output": "Cannot reach lr_host.py — is it running on your Mac?"}
    except Exception as e:
        return {"ok": False, "output": str(e)}


def check_host():
    try:
        with httpx.Client(timeout=3) as client:
            r = client.get(f"{HOST_BRIDGE}/health")
        ok = r.json().get("ok", False)
    except Exception:
        ok = False
    with lock:
        state["host_ok"] = ok
    return ok


# ── Processing sequence ───────────────────────────────────────────────────────

def set_step(step: str):
    with lock:
        state["proc_step"] = step
    save_state()


def process_batch(folder_name: str):
    """Full automation sequence for one batch. Runs in a background thread."""
    with lock:
        batch = state["batches"].get(folder_name)
        if not batch:
            return
        batch["status"] = "processing"
        state["active"] = folder_name
        state["new_birbs"] = []
        state["stop_requested"] = False
        state["thread_active"] = True
    try:
        _run_batch(folder_name)
    finally:
        with lock:
            state["thread_active"] = False


def _run_batch(folder_name: str, start_step: str = "import"):
    mac_path = str(Path(MAC_HOME) / "Downloads" / folder_name)

    if start_step == "import":
        # ── Step 1: Import ────────────────────────────────────────────────────
        set_step("importing")
        log(f"Importing {folder_name} into Lightroom…")
        result = call_host("import", folder=mac_path)
        for line in result.get("output", "").splitlines():
            log(f"  {line}")
        if not result.get("ok"):
            log("Import failed — check lr_host.py is running.")
            set_step("import_failed")
            return
        log("Import triggered. Waiting for Lightroom to load photos…")
        for _ in range(25):
            time.sleep(1)
            with lock:
                stop = state["stop_requested"]
                done = state["proc_step"] == "continue_toning"
            if stop or done:
                break
        else:
            set_step("continue_toning")
        if state.get("stop_requested"): return

    if start_step in ("import", "tone"):
        # ── Step 2: Auto-tone ─────────────────────────────────────────────────
        set_step("toning")
        log("Applying Auto Settings to all photos…")
        result = call_host("auto-tone")
        for line in result.get("output", "").splitlines():
            log(f"  {line}")
        log("Auto-tone done." if result.get("ok") else "Auto-tone may have failed — check Lightroom.")

    if start_step in ("import", "tone", "denoise"):
        # ── Step 3: Denoise ───────────────────────────────────────────────────
        set_step("denoise")
        log("Check Denoise on one photo in Lightroom, then click Continue.")
        while True:
            time.sleep(0.5)
            with lock:
                step_changed = state["proc_step"] != "denoise"
                stopped = state["stop_requested"]
            if step_changed or stopped:
                break
        if state.get("stop_requested"): return
        set_step("copy_pasting")
        log("Spreading Denoise settings to all photos…")
        result = call_host("copy-and-paste")
        for line in result.get("output", "").splitlines():
            log(f"  {line}")
        log("Denoise applied to all photos." if result.get("ok") else "Paste may have failed — check Lightroom.")

    # ── Step 4+: Pick → Export → Post (always runs) ───────────────────────────
    _pick_export_post()


def _pick_export_post():
    """Picking → export → posting tail. Extracted so re-picking can reuse it."""
    set_step("picking")
    log("Select your best shot(s) in Lightroom — hold ⌘ for multiple. Click Done when ready.")
    while True:
        time.sleep(0.5)
        with lock:
            exporting = state["proc_step"] == "exporting"
            stopped   = state["stop_requested"]
        if exporting or stopped:
            break
    if state.get("stop_requested"): return

    export_start = time.time() - 2  # small slack for filesystem/clock skew
    log("Triggering export…")
    result = call_host("export")
    for line in result.get("output", "").splitlines():
        log(f"  {line}")

    def _exported():
        # Files written or *overwritten* since export began. /birbs is a flat
        # dump that accumulates every shoot, so re-exporting a name that already
        # exists is invisible to a name set-diff — mtime catches those too.
        out = []
        for f in _birbs_snapshot():
            try:
                if (BIRBS_DIR / f).stat().st_mtime >= export_start:
                    out.append(f)
            except OSError:
                pass
        return sorted(out)

    set_step("export_wait")
    log("Export running in Lightroom — auto-continues when files land in ~/Desktop/birbs/ (or click Done).")
    last_sig, stable = None, 0
    while True:
        time.sleep(1)
        with lock:
            done    = state["proc_step"] != "export_wait"
            stopped = state["stop_requested"]
        if done or stopped:
            break
        # Auto-advance once exported files appear and stop growing for 8s
        new_files = _exported()
        if not new_files:
            continue
        try:
            sig = tuple((f, (BIRBS_DIR / f).stat().st_size) for f in new_files)
        except OSError:
            continue
        if sig == last_sig:
            stable += 1
            if stable >= 8:
                log("Export finished — continuing automatically.")
                break
        else:
            last_sig, stable = sig, 0
    if state.get("stop_requested"): return

    new = _exported()
    with lock:
        state["new_birbs"] = new
    save_state()
    for n in new:
        log(f"  ✓ {n}")
    if not new:
        log("No new files detected — check ~/Desktop/birbs/")

    set_step("posting")
    log("Export done! Fill in the species below and click Post.")


def get_next_open_scheduled_at() -> str:
    """Query Buffer, find first open day, return the scheduledAt ISO string for that day's slot."""
    from datetime import datetime, timezone, timedelta
    token = os.environ.get("BUFFER_TOKEN", "")
    if not token:
        return ""
    query = """
    query GetPosts($input: PostsInput!, $first: Int) {
      posts(input: $input, first: $first) { edges { node { dueAt } } }
    }
    """
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post("https://api.buffer.com",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"query": query, "variables": {
                    "input": {"organizationId": "6a008c6e3e4597b26fe42152"},
                    "first": 100
                }})
        edges = r.json().get("data", {}).get("posts", {}).get("edges", [])
        now = datetime.now(timezone.utc)
        future = [datetime.fromisoformat(e["node"]["dueAt"].replace("Z", "+00:00"))
                  for e in edges if e.get("node", {}).get("dueAt", "")
                  and datetime.fromisoformat(e["node"]["dueAt"].replace("Z", "+00:00")) > now]

        if not future:
            return ""

        # Build set of filled local dates (handles EST/EDT correctly)
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        filled = {dt.astimezone(et).date().isoformat() for dt in future}

        # Walk forward from tomorrow to find first open day
        check = (now.astimezone(et) + timedelta(days=1)).date()
        for _ in range(365):
            if check.isoformat() not in filled:
                break
            check += timedelta(days=1)

        # Reuse the ET wall-clock time of a queued post on the same day-of-week
        target_dow = check.weekday()
        ref = next((dt for dt in future if dt.astimezone(et).weekday() == target_dow), future[0])
        ref_et = ref.astimezone(et)
        sched = datetime(check.year, check.month, check.day,
                         ref_et.hour, ref_et.minute, tzinfo=et).astimezone(timezone.utc)
        scheduled_at = sched.strftime("%Y-%m-%dT%H:%M:00.000Z")
        log(f"Scheduling for {check} (ET) → {scheduled_at} UTC")
        return scheduled_at
    except Exception as e:
        log(f"get_next_open_scheduled_at error: {e}")
        return ""


def get_next_open_scheduled_ats(n: int) -> list:
    """ISO scheduledAt strings for the next `n` open days — one post per day."""
    from datetime import datetime, timezone, timedelta
    from zoneinfo import ZoneInfo
    token = os.environ.get("BUFFER_TOKEN", "")
    if not token or n < 1:
        return []
    query = """
    query GetPosts($input: PostsInput!, $first: Int) {
      posts(input: $input, first: $first) { edges { node { dueAt } } }
    }
    """
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post("https://api.buffer.com",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"query": query, "variables": {
                    "input": {"organizationId": "6a008c6e3e4597b26fe42152"},
                    "first": 100
                }})
        edges = r.json().get("data", {}).get("posts", {}).get("edges", [])
        now = datetime.now(timezone.utc)
        future = [datetime.fromisoformat(e["node"]["dueAt"].replace("Z", "+00:00"))
                  for e in edges if e.get("node", {}).get("dueAt", "")
                  and datetime.fromisoformat(e["node"]["dueAt"].replace("Z", "+00:00")) > now]
        et = ZoneInfo("America/New_York")
        filled = {dt.astimezone(et).date().isoformat() for dt in future}

        def slot_for(day):
            # Reuse the wall-clock time of an existing post on the same weekday.
            ref = next((dt for dt in future if dt.astimezone(et).weekday() == day.weekday()),
                       future[0] if future else None)
            h, m = (ref.astimezone(et).hour, ref.astimezone(et).minute) if ref else (17, 37)
            sched = datetime(day.year, day.month, day.day, h, m, tzinfo=et).astimezone(timezone.utc)
            return sched.strftime("%Y-%m-%dT%H:%M:00.000Z")

        out, check = [], (now.astimezone(et) + timedelta(days=1)).date()
        for _ in range(n):
            for _ in range(365):
                if check.isoformat() not in filled:
                    break
                check += timedelta(days=1)
            out.append(slot_for(check))
            filled.add(check.isoformat())   # so the next post lands on a later day
            check += timedelta(days=1)
        log(f"Scheduling {n} post(s) across open slots: {out}")
        return out
    except Exception as e:
        log(f"get_next_open_scheduled_ats error: {e}")
        return []


def _caption_for(chunk: list, cap_date: str) -> str:
    """Build an IG caption for one post's photos (dedupes consecutive repeats)."""
    locs = [p.get("location", "") for p in chunk]
    same_loc = len(set(locs)) == 1 and locs[0]
    seen, lines = None, []
    for p in chunk:
        item = p.get("species", "") if same_loc else f"{p.get('species','')} - {p.get('location','')}"
        if item != seen:
            lines.append(item)
            seen = item
    tail = f"\n\n{locs[0]}\n\n{cap_date}" if same_loc else f"\n\n{cap_date}"
    return "\n".join(lines) + tail


def _birbs_snapshot():
    if not BIRBS_DIR.exists():
        return []
    return [f.name for f in BIRBS_DIR.iterdir()
            if f.suffix.lower() in {".jpg", ".jpeg"}]


# ── Background scanner ────────────────────────────────────────────────────────

def scanner_loop():
    time.sleep(3)   # let Flask start accepting requests first
    while True:
        scan_batches()
        check_host()
        time.sleep(30)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/birb-img/<path:filename>")
def serve_birb_image(filename):
    from flask import send_from_directory
    return send_from_directory(str(BIRBS_DIR), filename)


@app.get("/birb-thumb/<path:filename>")
def serve_birb_thumb(filename):
    from flask import send_file
    from PIL import Image as PILImage
    src = BIRBS_DIR / filename
    if not src.exists():
        return "", 404
    thumb_dir = BIRBS_DIR / ".thumbs"
    thumb_dir.mkdir(exist_ok=True)
    thumb = thumb_dir / filename
    if not thumb.exists() or src.stat().st_mtime > thumb.stat().st_mtime:
        img = PILImage.open(src)
        img.thumbnail((400, 400), PILImage.LANCZOS)
        img.save(thumb, "JPEG", quality=82)
    return send_file(str(thumb), mimetype="image/jpeg")




@app.get("/api/log-stream")
def log_stream():
    def generate():
        # Send last 40 lines on connect for context
        with lock:
            seen_seq = log_seq
            backfill = list(state["log"])[-40:]
        for line in backfill:
            yield f"data: {json.dumps(line)}\n\n"
        while True:
            time.sleep(0.15)
            with lock:
                cur_seq = log_seq
                cur_log = list(state["log"])
            if cur_seq > seen_seq:
                new_count = cur_seq - seen_seq
                for line in cur_log[-new_count:]:
                    yield f"data: {json.dumps(line)}\n\n"
                seen_seq = cur_seq
    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/state")
def get_state():
    with lock:
        active_batch = state["batches"].get(state["active"]) if state["active"] else None
        payload = {
            "batches":   sorted(state["batches"].values(), key=lambda b: b["date"], reverse=True),
            "active":    active_batch,
            "proc_step": state["proc_step"],
            "new_birbs":  list(state["new_birbs"]),
            "log":        list(state["log"])[-80:],
            "host_ok":    state["host_ok"],
            "last_post":  state["last_post"],
            "post_error":     state.get("post_error", False),
            "thread_active":  state["thread_active"],
            "syno_skipped":   sorted(state["syno_skipped"]),
            "done_albums":    sorted(state["done_albums"]),
            "syno_fetching":  dict(state["syno_fetching"]),
        }
    return jsonify(payload)


@app.post("/api/process/<folder_name>")
def start_process(folder_name: str):
    with lock:
        if state["active"]:
            return jsonify({"error": "Already processing another batch"}), 400
        if folder_name not in state["batches"]:
            return jsonify({"error": "Unknown batch"}), 404

    threading.Thread(target=process_batch, args=(folder_name,), daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/continue")
def continue_step():
    """Advance from a waiting step to the next automated step."""
    with lock:
        if state["proc_step"] == "importing":
            state["proc_step"] = "continue_toning"
        elif state["proc_step"] == "denoise":
            state["proc_step"] = "copy_pasting"
    return jsonify({"ok": True})


@app.post("/api/done")
def done_picking():
    with lock:
        step = state["proc_step"]
        if step == "picking":
            state["proc_step"] = "exporting"
        elif step == "export_wait":
            state["proc_step"] = "export_done"
    return jsonify({"ok": True})


@app.post("/api/resize-only")
def resize_only():
    """Resize selected photos into .ready — no upload, no Buffer post."""
    data  = request.json or {}
    files = data.get("files", [])
    if not files:
        return jsonify({"error": "no files"}), 400

    def _resize():
        import subprocess
        cmd = ["/usr/local/bin/python", "/app/birb_post.py",
               "--resize-only", "--file"] + files
        log(f"Resize-only: {len(files)} image(s)")
        r = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ})
        for line in (r.stdout + r.stderr).splitlines():
            log(line)
        log("✓ Resize complete." if r.returncode == 0 else "Resize failed — check log.")

    threading.Thread(target=_resize, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/post")
def post_to_buffer():
    """Post selected photo to Buffer."""
    data          = request.json or {}
    files         = data.get("files", [])
    species       = data.get("species", "")
    location      = data.get("location", "")
    cap_date      = data.get("date", "")
    oos           = bool(data.get("out_of_area", False))
    schedule_date = data.get("schedule_date", "")
    scheduled_at  = data.get("scheduled_at", "")

    # photos is list of {file, species, location} for per-photo assignment
    photos = data.get("photos")
    if photos:
        files    = [p["file"] for p in photos]
        species  = ", ".join(p.get("species", "") for p in photos)
        location = photos[0].get("location", "") if photos else location

    # groups is the user's explicit per-post grouping (list of lists of photos).
    groups = data.get("groups")

    if not cap_date or not (groups or photos or files):
        return jsonify({"error": "missing fields"}), 400

    def _post():
        import subprocess
        with lock:
            state["post_error"] = False

        # Use the explicit lane grouping if given; else split into chunks of `per`.
        if groups:
            chunks = [g for g in groups if g]
        else:
            per = max(1, min(int(data.get("per_post") or 10), 10))
            chunk_photos = photos or [{"file": f, "species": species, "location": location} for f in files]
            chunks = [chunk_photos[i:i+per] for i in range(0, len(chunk_photos), per)]
        # Safety net: never let a single post exceed Instagram's 10-image limit.
        capped = []
        for g in chunks:
            for i in range(0, len(g), 10):
                capped.append(g[i:i+10])
        chunks = capped or chunks
        n = len(chunks)
        # One open day per split post; a single post keeps the old behaviour
        # (honour a manual slot, else next open slot).
        slots = (get_next_open_scheduled_ats(n) if n > 1
                 else [scheduled_at or get_next_open_scheduled_at()])

        all_ok, last_due = True, ""
        for i, chunk in enumerate(chunks):
            caption_text = _caption_for(chunk, cap_date)
            chunk_files  = [p["file"] for p in chunk]
            cmd = ["/usr/local/bin/python", "/app/birb_post.py",
                   "--file"] + chunk_files + ["--text", caption_text]
            slot = slots[i] if i < len(slots) else ""
            if slot:
                cmd += ["--scheduled-at", slot]
            if n > 1:
                log(f"── Post {i+1}/{n} · {len(chunk_files)} photo(s) ──")
            log(f"Posting: {' '.join(cmd)}")
            r = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ})
            for line in (r.stdout + r.stderr).splitlines():
                log(line)
            if r.returncode == 0:
                log("🐦 Posted to Buffer!" if n == 1 else f"🐦 Post {i+1}/{n} queued!")
                due = next((l.split("Scheduled for:")[-1].strip()
                            for l in (r.stdout + r.stderr).splitlines()
                            if "Scheduled for:" in l), "")
                last_due = due or last_due
            else:
                all_ok = False
                log(f"Post {i+1}/{n} failed — check log.")
                break

        if all_ok:
            if n > 1:
                log(f"✓ All {n} posts queued to Buffer.")
            with lock:
                if state["active"]:
                    state["batches"][state["active"]]["status"]     = "done"
                    state["batches"][state["active"]]["birbs"]      = list(state["new_birbs"])
                    state["batches"][state["active"]]["posted_due"] = last_due
                    state["active"]    = None
                    state["proc_step"] = None
                state["last_post"] = {"due": last_due, "url": "https://publish.buffer.com"}
            save_state()
        else:
            with lock:
                state["post_error"] = True

    threading.Thread(target=_post, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/next-post-date")
def next_post_date():
    """Return the day after the last queued Buffer post — the next open slot."""
    token = os.environ.get("BUFFER_TOKEN", "")
    if not token:
        return jsonify({"date": ""})
    query = """
    query GetPosts($input: PostsInput!, $first: Int) {
      posts(input: $input, first: $first) {
        edges { node { dueAt } }
      }
    }
    """
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        with httpx.Client(timeout=10) as client:
            r = client.post("https://api.buffer.com",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"query": query, "variables": {
                    "input": {"organizationId": "6a008c6e3e4597b26fe42152"},
                    "first": 100
                }})
        body  = r.json()
        edges = body.get("data", {}).get("posts", {}).get("edges", [])
        errs  = body.get("errors", [])
        log(f"next-post-date: {len(edges)} posts, errors={[e.get('message','')[:60] for e in errs][:1]}")
        dues = []
        for e in edges:
            node = e.get("node", {})
            due  = node.get("dueAt", "")
            if due:
                try:
                    dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                    if dt > now:
                        dues.append(due)
                except Exception:
                    pass
        return jsonify({"dues": dues})
    except Exception as e:
        return jsonify({"dues": [], "error": str(e)})


@app.get("/api/recent-species")
def recent_species():
    """Pull species names from last 50 Buffer posts for autocomplete suggestions."""
    token = os.environ.get("BUFFER_TOKEN", "")
    if not token:
        return jsonify({"species": []})
    query = """
    query GetPosts($input: PostsInput!, $first: Int) {
      posts(input: $input, first: $first) {
        edges { node { text } }
      }
    }
    """
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post("https://api.buffer.com",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"query": query, "variables": {
                    "input": {"organizationId": "6a008c6e3e4597b26fe42152"},
                    "first": 50
                }})
        edges = r.json().get("data", {}).get("posts", {}).get("edges", [])
        seen_sp, species   = set(), []
        seen_loc, locations = set(), []
        for edge in edges:
            text   = edge.get("node", {}).get("text", "")
            blocks = text.strip().split("\n\n") if text.strip() else []
            # First block = species lines
            for line in (blocks[0].split("\n") if blocks else []):
                s = line.strip().lstrip("⚠️").strip()
                if s and s not in seen_sp:
                    seen_sp.add(s); species.append(s)
            # Second block = location
            if len(blocks) >= 2:
                loc = blocks[1].strip()
                if loc and loc not in seen_loc:
                    seen_loc.add(loc); locations.append(loc)
        return jsonify({"species": species, "locations": locations})
    except Exception as e:
        return jsonify({"species": [], "locations": [], "error": str(e)})


@app.post("/api/scan")
def manual_scan():
    scan_batches()
    return jsonify({"ok": True, "count": len(state["batches"])})


@app.post("/api/skip/<folder_name>")
def skip_batch(folder_name: str):
    with lock:
        if folder_name in state["batches"]:
            state["batches"][folder_name]["status"] = "done"
    save_state()
    return jsonify({"ok": True})


@app.post("/api/skip-before/<date>")
def skip_before(date: str):
    """Mark all batches with date < given date (YYYY-MM-DD) as done."""
    count = 0
    with lock:
        for b in state["batches"].values():
            if b["date"] < date and b["status"] == "pending":
                b["status"] = "done"
                count += 1
    save_state()
    log(f"Marked {count} batches before {date} as done.")
    return jsonify({"ok": True, "count": count})


@app.get("/api/birbs")
def list_birbs():
    if not BIRBS_DIR.exists():
        return jsonify({"files": [], "assigned": {}})
    with lock:
        assigned = {f: name for name, b in state["batches"].items() for f in b.get("birbs", [])}
    files = sorted(
        f.name for f in BIRBS_DIR.iterdir()
        if f.suffix.lower() in {".jpg", ".jpeg"} and not f.name.startswith(".")
    )
    return jsonify({"files": files, "assigned": assigned})


@app.post("/api/assign-birbs/<folder_name>")
def assign_birbs(folder_name: str):
    birbs = (request.json or {}).get("birbs", [])
    with lock:
        if folder_name not in state["batches"]:
            return jsonify({"error": "unknown batch"}), 404
        state["batches"][folder_name]["birbs"] = birbs
    save_state()
    return jsonify({"ok": True})


@app.post("/api/goto-step")
def goto_step():
    VALID = {"tone", "denoise", "pick"}
    data  = request.json or {}
    step  = data.get("step", "")
    if step not in VALID:
        return jsonify({"error": "invalid step"}), 400
    with lock:
        folder = state["active"]
    if not folder:
        return jsonify({"error": "no active batch"}), 400

    # Signal the running thread to exit its wait loop
    with lock:
        state["stop_requested"] = True

    def _restart():
        time.sleep(0.8)  # let the thread notice and exit
        with lock:
            state["stop_requested"] = False
            state["thread_active"]  = True
        log(f"↩ Restarting from: {step}")
        try:
            _run_batch(folder, start_step=step)
        finally:
            with lock:
                state["thread_active"] = False

    threading.Thread(target=_restart, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/force-advance")
def force_advance():
    """Emergency escape from a stuck automation step."""
    with lock:
        step = state["proc_step"]
        next_step = {
            "importing":       "continue_toning",
            "continue_toning": "toning",
            "toning":          "denoise",
            "copy_pasting":    "picking",
            "exporting":       "export_wait",
            "export_done":     "posting",
        }.get(step)
        if next_step:
            state["proc_step"] = next_step
            if step == "export_done":
                import time as _time
                cutoff = _time.time() - 7200  # files exported in the last 2 hours
                recent = [f for f in _birbs_snapshot()
                          if (BIRBS_DIR / f).stat().st_mtime >= cutoff]
                state["new_birbs"] = recent if recent else _birbs_snapshot()
    if next_step:
        log(f"Force-advanced: {step} → {next_step}")
    save_state()
    return jsonify({"ok": bool(next_step)})


@app.post("/api/reset/<folder_name>")
def reset_batch(folder_name: str):
    was_active = False
    with lock:
        if folder_name in state["batches"]:
            state["batches"][folder_name]["status"] = "pending"
        if state["active"] == folder_name:
            was_active = True
            # Tell the running thread to actually bail — otherwise clearing
            # proc_step just trips its wait loop and it marches on to the next step.
            state["stop_requested"] = True
            state["active"]    = None
            state["proc_step"] = None
            state["new_birbs"] = []
    save_state()
    if was_active:
        # Clear the stop flag once the thread has had time to exit, so the next
        # run isn't immediately aborted by a leftover flag.
        def _clear():
            time.sleep(1.0)
            with lock:
                state["stop_requested"] = False
        threading.Thread(target=_clear, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/syno/albums")
def syno_albums():
    """List Synology Photos albums (cached; ?refresh=1 bypasses)."""
    import time as _t
    if not request.args.get("refresh") and _syno_cache["albums"] is not None \
            and _t.time() - _syno_cache["at"] < SYNO_CACHE_TTL:
        return jsonify({"albums": _syno_cache["albums"], "cached": True})
    r = call_host("syno-albums", timeout=30)
    if not r.get("ok"):
        return jsonify({"error": r.get("output", "lr_host error")}), 502
    try:
        albums = json.loads(r.get("output", "[]"))
    except Exception:
        return jsonify({"error": f"bad album response: {r.get('output','')[:200]}"}), 502
    _syno_cache["albums"] = albums
    _syno_cache["at"] = _t.time()
    return jsonify({"albums": albums})


@app.post("/api/syno/fetch")
def syno_fetch():
    """Download a Synology album's originals into ~/Downloads/<album> (background)."""
    data  = request.json or {}
    album = (data.get("album") or "").strip()
    if not album:
        return jsonify({"error": "no album"}), 400

    then_process = bool(data.get("then_process"))
    total = int(data.get("total") or 0)

    def _watch_progress(stop):
        """Report download progress by counting files landing in the dest folder."""
        dest = DOWNLOADS / album
        last = -1
        while not stop["done"]:
            try:
                got = sum(1 for f in dest.iterdir()
                          if f.is_file() and not f.name.endswith(".part") and not f.name.startswith("."))
            except OSError:
                got = 0
            with lock:
                if album in state["syno_fetching"]:
                    state["syno_fetching"][album]["got"] = got
            # Log at ~10% milestones so the log isn't spammed per file
            step = max(1, total // 10) if total else 10
            if got != last and (got % step == 0 or got == total):
                log(f"  …{album}: {got}/{total or '?'} fetched")
                last = got
            time.sleep(1.5)

    def _fetch():
        log(f"📥 Fetching from Synology: '{album}' ({total or '?'} photos) …")
        with lock:
            state["syno_fetching"][album] = {"got": 0, "total": total}
        stop = {"done": False}
        threading.Thread(target=_watch_progress, args=(stop,), daemon=True).start()
        try:
            r = call_host("syno-fetch", body={"album": album, "raw_only": bool(data.get("raw_only"))},
                          timeout=3600)
        finally:
            stop["done"] = True
            with lock:
                state["syno_fetching"].pop(album, None)
        for line in (r.get("output", "") or "").splitlines():
            log(line)
        if not r.get("ok"):
            log("Synology fetch failed — check log.")
            return
        log("✓ Synology fetch complete.")
        if not then_process:
            log("Batch will appear shortly.")
            return
        # Register the new folder, then kick off the pipeline automatically
        scan_batches()
        with lock:
            exists = album in state["batches"]
            busy   = state["active"]
        if not exists:
            log(f"Fetched, but no batch matched '{album}' (check the date-prefixed name).")
        elif busy:
            log(f"Another batch is active ({busy}) — start '{album}' manually when free.")
        else:
            log(f"▶ Starting pipeline for {album} …")
            threading.Thread(target=process_batch, args=(album,), daemon=True).start()

    threading.Thread(target=_fetch, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/syno/skip")
def syno_skip():
    """Hide a Synology album from the list (persisted)."""
    album = ((request.json or {}).get("album") or "").strip()
    if not album:
        return jsonify({"error": "no album"}), 400
    with lock:
        state["syno_skipped"].add(album)
    save_state()
    return jsonify({"ok": True})


@app.post("/api/syno/unskip")
def syno_unskip():
    """Un-hide a previously skipped Synology album."""
    album = ((request.json or {}).get("album") or "").strip()
    with lock:
        state["syno_skipped"].discard(album)
    save_state()
    return jsonify({"ok": True})


# ── Boot ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Restore persisted Synology skip list + posted-album memory
    _saved = load_saved_statuses()
    state["syno_skipped"] = set(_saved.get("syno_skipped", []))
    state["done_albums"]  = set(_saved.get("done_albums", [])) | {
        n for n, s in _saved.get("statuses", {}).items() if s == "done"}
    # Defer scanning to background so Flask starts immediately
    threading.Thread(target=scanner_loop, daemon=True).start()
    log("Birb workflow server ready — scanning for batches…")
    app.run(host="0.0.0.0", port=8765, debug=False)
