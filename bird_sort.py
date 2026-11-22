#!/usr/bin/env python3
"""
bird_sort.py — the mechanical half of /sort-batch.

Claude Code does the judgement (species, which frames, how to group, how to
frame); this does everything that is measurement or bookkeeping, so the
judgement is made from good evidence and lands in the pipeline exactly.

    PY=~/.bird_sort_venv/bin/python
    $PY bird_sort.py prepare              # analyse the batch on the posting step
    $PY bird_sort.py zoom DSC_0617 ...    # full-resolution close-ups for an ID
    $PY bird_sort.py grid 0617            # frame + coordinate grid, to place a missed bird
    $PY bird_sort.py grid 0617=x,y,w,h    # ...and check + re-score the box you placed
    $PY bird_sort.py preview plan.json    # render the proposed posts + crops
    $PY bird_sort.py apply plan.json      # save arrangement + crops to the app

Everything is written to ~/.bird_sort/<batch folder>/ (printed by each
command). Runs on the Mac, beside Lightroom's export folder — not in the
container — and talks to the app at localhost:8765.

Setup (once):
    ~/.pyenv/versions/3.12.11/bin/python3 -m venv ~/.bird_sort_venv
    ~/.bird_sort_venv/bin/pip install opencv-python-headless numpy Pillow
The bird detector (YOLOv5m, COCO "bird") downloads itself on first use to
~/.cache/birds/, shared with ~/Website/tools/bird_focus.py.

plan.json (written by Claude, read by preview/apply):
    {
      "summary": "one paragraph for Scott",
      "posts": [
        {"photos": [
           {"file": "DSC_0617.jpg", "species": "Black-and-white Warbler",
            "facing": "left",            # left | right | null  (room ahead)
            "crop": 1.0,                 # framing: 1 default, >1 looser, <1 tighter,
                                         #   "none" = leave uncropped, or {x,y,w,h}
            "bird": [x, y, w, h],        # optional: fix a missed/wrong detection
            "location": "Rea St.",       # optional: overrides the batch default
            "oos": false,                # optional: out-of-area ⚠️
            "flag": "why Scott should look"}   # optional: shown at review
        ]}
      ]
    }
Every exported photo not named in a post is excluded (never deleted).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = os.environ.get("BIRD_APP", "http://localhost:8765")
BIRDS_DIR = Path.home() / "Desktop" / "birbs"
ORIG_DIR = BIRDS_DIR / ".originals"          # /api/crop keeps pristine copies here
WORK_ROOT = Path.home() / ".bird_sort"

MODEL_URL = "https://github.com/ultralytics/yolov5/releases/download/v7.0/yolov5m.onnx"
MODEL = Path.home() / ".cache" / "birds" / "yolov5m.onnx"
BIRD, ANIMALS = 14, set(range(14, 24))       # COCO: bird, then any animal

BURST_GAP = 2.0          # seconds; frames closer than this are one burst
MAX_PER_POST = 10        # Instagram carousel cap

# Framing, fitted to the crops Scott approved in Nov 2026 after twice saying
# "too tight": small birds filled 31–43% of the crop's height (warblers,
# nuthatches), big ones 65–74% (perched juvenile hawk, Pileated). So aim for
# ~40% and at most half the width — but never let a big bird's crop grow past
# ~45% of the frame's height unless it needs to; that read as uncropped.
BIRD_OF_HEIGHT = 0.40
BIRD_OF_WIDTH = 0.50
BIG_BIRD_FILL = 0.72      # a big bird may fill up to this much of the crop...
BIG_CROP_OF_FRAME = 0.45  # ...rather than the crop exceeding this much of the frame
ASPECT = 1.5             # 3:2 — Instagram crops a carousel to its first photo
MIN_CROP_W = 1200        # px; stays above Instagram's 1080 after resize
LEAD = {"right": 0.44, "left": 0.56}         # bird centre across the crop
BIRD_DOWN = 0.54         # bird centre down the crop: a little headroom


# ── App + files ───────────────────────────────────────────────────────────────

def api(path, body=None, timeout=60):
    req = urllib.request.Request(
        BASE + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def batch_state():
    """(state, batch folder, exported files) for the batch on the posting step.
    BIRD_SORT_TEST=path/to.json ({"folder","location","files"}) stands in for
    the app so prepare/zoom/preview can be exercised on an old batch; apply
    refuses to run that way."""
    test = os.environ.get("BIRD_SORT_TEST")
    if test:
        t = json.loads(Path(test).read_text())
        return ({"proc_step": "posting", "active": {"folder": t["folder"],
                 "location": t.get("location", "")}, "new_birds": t["files"],
                 "photo_dates": {}, "arrangement": None},
                t["folder"], list(t["files"]))
    st = api("/api/state")
    if st.get("proc_step") != "posting":
        sys.exit(f"The app isn't on the posting step (step: {st.get('proc_step')}). "
                 "Nothing to sort yet.")
    folder = (st.get("active") or {}).get("folder")
    files = list(st.get("new_birds") or [])
    if not folder or not files:
        sys.exit("No active batch with exported photos.")
    return st, folder, files


def work_dir(folder):
    d = WORK_ROOT / folder
    d.mkdir(parents=True, exist_ok=True)
    return d


def _has_capture_date(path):
    from PIL import Image
    try:
        with Image.open(path) as im:
            return bool(im.getexif().get_ifd(0x8769).get(36867))
    except Exception:
        return False


def source(name):
    """The uncropped file: crop boxes are always relative to the original.

    Only trust .originals when the file in birbs is our crop (crops carry no
    capture date). A file with a capture date is a fresh export, and a backup
    under its name belongs to an older photo — the camera counter wraps. On
    2026-11-22 that put a June starling, already posted, into a 9/26 post."""
    o, b = ORIG_DIR / name, BIRDS_DIR / name
    if o.exists() and not (b.exists() and _has_capture_date(b)):
        return o
    return b


def norm(name):
    """'0617', 'DSC_0617', 'DSC_0617.jpg' -> 'DSC_0617.jpg'."""
    n = name if name.lower().endswith((".jpg", ".jpeg")) else name + ".jpg"
    return n if n.upper().startswith("DSC_") or "_" in n else "DSC_" + n


def short(name):
    return Path(name).stem.replace("DSC_", "")


# ── Measurement ───────────────────────────────────────────────────────────────

_NET = None


def _net():
    global _NET
    if _NET is None:
        import cv2
        if not MODEL.exists():
            MODEL.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(MODEL_URL, str(MODEL) + ".part")
            os.replace(str(MODEL) + ".part", MODEL)
        _NET = cv2.dnn.readNetFromONNX(str(MODEL))
    return _NET


def detect(img):
    """BGR image -> list of (score, [x, y, w, h]) normalized boxes, birds first;
    any other animal only when no bird is found (a hawk read as a cat still
    marks where it is). Same model and thresholds as bird_focus.py."""
    import cv2
    import numpy as np
    h, w = img.shape[:2]
    s = 640 / max(h, w)
    nh, nw = int(h * s), int(w * s)
    canvas = np.full((640, 640, 3), 114, np.uint8)
    canvas[:nh, :nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    net = _net()
    net.setInput(cv2.dnn.blobFromImage(canvas, 1 / 255, (640, 640), swapRB=True))
    out = net.forward()[0]
    cls = out[:, 5:].argmax(1)
    conf = out[:, 4] * out[np.arange(len(out)), 5 + cls]
    for pref in ({BIRD}, ANIMALS):
        m = np.isin(cls, list(pref)) & (conf > .25)
        if not m.any():
            continue
        boxes = [[float(x - bw / 2), float(y - bh / 2), float(bw), float(bh)]
                 for x, y, bw, bh in out[m, :4]]
        sc = conf[m].astype(float).tolist()
        keep = np.array(cv2.dnn.NMSBoxes(boxes, sc, .25, .45)).flatten()
        dets = []
        for i in keep:
            x, y, bw, bh = boxes[i]
            x0, y0 = max(0, x / s / w), max(0, y / s / h)
            x1, y1 = min(1, (x + bw) / s / w), min(1, (y + bh) / s / h)
            dets.append((round(sc[i], 2), [round(x0, 4), round(y0, 4),
                                           round(x1 - x0, 4), round(y1 - y0, 4)]))
        return sorted(dets, reverse=True)
    return []


def _inside(a, b):
    """Fraction of box a that lies inside box b."""
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return ix * iy / max(1e-9, a[2] * a[3])


def subject(dets):
    """The box to frame. Start from the most confident detection — not the
    biggest: on small warblers the biggest confident box was a clump of dead
    leaves (0525) or bird-plus-branch (0614). A tighter box of near-equal
    confidence sitting inside it is the bird itself. Then widen to take in any
    other confident bird of comparable size (a pair on one branch)."""
    if not dets:
        return None
    top, main = dets[0]
    for sc, b in dets[1:]:
        if sc >= top * .9 and _inside(b, main) > .85:
            main = b
    x0, y0, x1, y1 = main[0], main[1], main[0] + main[2], main[1] + main[3]
    area = main[2] * main[3]
    for sc, b in dets:
        if (sc >= max(.5, top * .75) and b[2] * b[3] >= .35 * area
                and _inside(b, main) < .5 and _inside(main, b) < .5):
            x0, y0 = min(x0, b[0]), min(y0, b[1])
            x1, y1 = max(x1, b[0] + b[2]), max(y1, b[1] + b[3])
    return [round(x0, 4), round(y0, 4), round(x1 - x0, 4), round(y1 - y0, 4)]


def sharpness(img, box):
    """Mean of the strongest 1% of edges inside the bird box, measured at a
    fixed size so a small bird and a big one score on the same scale. Scored
    on the bird, never the frame: branches otherwise dominate."""
    import cv2
    import numpy as np
    h, w = img.shape[:2]
    x, y, bw, bh = box
    px, py = bw * .1, bh * .1
    x0, y0 = int(max(0, x - px) * w), int(max(0, y - py) * h)
    x1, y1 = int(min(1, x + bw + px) * w), int(min(1, y + bh + py) * h)
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return 0.0, 0
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    k = 512 / max(g.shape)
    if k < 1:
        g = cv2.resize(g, (int(g.shape[1] * k), int(g.shape[0] * k)), interpolation=cv2.INTER_AREA)
    lap = np.abs(cv2.Laplacian(g.astype(np.float32), cv2.CV_32F))
    flat = np.sort(lap.ravel())
    top = flat[int(len(flat) * .99):]
    return round(float(top.mean()) if len(top) else 0.0, 1), int(g.mean())


def taken_at(path):
    """Capture time in seconds (with sub-seconds when the camera wrote them)."""
    from PIL import Image
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            ifd = ex.get_ifd(0x8769)
            dt = ifd.get(36867) or ex.get(306)
            sub = str(ifd.get(37521) or "0").strip() or "0"
        t = datetime.strptime(str(dt)[:19], "%Y:%m:%d %H:%M:%S").timestamp()
        return t + float("0." + "".join(c for c in sub if c.isdigit()) or 0)
    except Exception:
        return None


# ── Sheets ────────────────────────────────────────────────────────────────────

def _label(draw, xy, text, fill=(255, 230, 0)):
    x, y = xy
    tw = draw.textlength(text)
    draw.rectangle([x, y, x + tw + 8, y + 15], fill=(0, 0, 0))
    draw.text((x + 4, y + 2), text, fill=fill)


def _thumb(path, size):
    from PIL import Image, ImageOps
    im = Image.open(path)
    im.draft("RGB", (size[0] * 2, size[1] * 2))    # fast JPEG downscale
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail(size)
    return im


def _crop_of(path, box, pad, out):
    """Square-ish crop around a normalized box, padded, resized to `out`."""
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    W, H = im.size
    x, y, w, h = box
    side = max(w * W, h * H) * (1 + 2 * pad)
    cx, cy = (x + w / 2) * W, (y + h / 2) * H
    x0 = int(min(max(0, cx - side / 2), max(0, W - side)))
    y0 = int(min(max(0, cy - side / 2), max(0, H - side)))
    c = im.crop((x0, y0, int(min(W, x0 + side)), int(min(H, y0 + side))))
    c.thumbnail((out, out))
    return c


def _grid(tiles, cols, cell, path):
    from PIL import Image
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell[0], rows * cell[1]), (24, 24, 24))
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % cols) * cell[0], (i // cols) * cell[1]))
    sheet.save(path, quality=86)
    return path


# ── Crop geometry ─────────────────────────────────────────────────────────────

def crop_box(bird, W, H, facing=None, factor=1.0):
    """Loose 3:2 crop around a normalized bird box, as fractions for /api/crop."""
    bx, by, bw, bh = bird
    bwp, bhp = bw * W, bh * H
    ch = max(bhp / BIRD_OF_HEIGHT, bwp / (BIRD_OF_WIDTH * ASPECT))
    big = max(bhp / BIG_BIRD_FILL, bwp / (BIG_BIRD_FILL * ASPECT), BIG_CROP_OF_FRAME * H)
    ch = min(ch, big) * factor
    cw = ch * ASPECT
    if cw < MIN_CROP_W:
        cw, ch = MIN_CROP_W, MIN_CROP_W / ASPECT
    fit = min(1.0, W / cw, H / ch)
    cw, ch = cw * fit, ch * fit
    cx, cy = (bx + bw / 2) * W, (by + bh / 2) * H
    x0 = cx - LEAD.get(facing or "", 0.5) * cw
    y0 = cy - BIRD_DOWN * ch
    x0 = min(max(0.0, x0), W - cw)
    y0 = min(max(0.0, y0), H - ch)
    return {"x": round(x0 / W, 4), "y": round(y0 / H, 4),
            "w": round(min(1.0, cw / W), 4), "h": round(min(1.0, ch / H), 4)}


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_prepare(args):
    import cv2
    st, folder, files = batch_state()
    wd = work_dir(folder)
    print(f"Batch {folder}: {len(files)} exported photos → {wd}")

    info, t0 = {}, time.time()
    for n, f in enumerate(sorted(files), 1):
        p = source(f)
        img = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if img is None:
            info[f] = {"error": "unreadable"}
            continue
        H, W = img.shape[:2]
        dets = detect(img)
        box = subject(dets)
        sharp, light = sharpness(img, box) if box else (0.0, 0)
        info[f] = {
            "size": [W, H], "taken": taken_at(p), "date": st.get("photo_dates", {}).get(f, ""),
            "birds": len(dets), "score": dets[0][0] if dets else 0, "bird": box,
            "detections": dets[:6],
            "area": round(box[2] * box[3], 4) if box else 0,
            "sharp": sharp, "light": light, "cropped": (ORIG_DIR / f).exists(),
        }
        if n % 10 == 0:
            print(f"  …{n}/{len(files)} ({time.time() - t0:.0f}s)")

    # Bursts by capture time — never by file number (every frame is consecutive).
    timed = sorted((v["taken"] or 0, k) for k, v in info.items() if "error" not in v)
    bursts, cur, last = [], [], None
    for t, k in timed:
        if cur and (not t or not last or t - last > BURST_GAP):
            bursts.append(cur)
            cur = []
        cur.append(k)
        last = t
    if cur:
        bursts.append(cur)
    for i, b in enumerate(bursts, 1):
        best = max(b, key=lambda k: (info[k]["sharp"], info[k]["area"]))
        for k in b:
            info[k]["burst"] = i
            info[k]["burst_best"] = k == best

    # Lightroom writes "-2" copies on a re-export; both can be in the batch.
    by_stem = {}
    for k in info:
        by_stem.setdefault(Path(k).stem.split("-")[0], []).append(k)
    dupes = {s: v for s, v in by_stem.items() if len(v) > 1}

    # Sheets: the whole frame (bird boxed) and the bird itself, larger.
    from PIL import ImageDraw
    names = sorted(info, key=lambda k: (info[k].get("taken") or 0, k))
    overview, birds = [], []
    for k in names:
        v = info[k]
        t = _thumb(source(k), (400, 267))
        d = ImageDraw.Draw(t)
        for sc, (x, y, w, h) in v.get("detections", []):
            d.rectangle([x * t.width, y * t.height, (x + w) * t.width, (y + h) * t.height],
                        outline=(0, 220, 255), width=1)
            d.text((x * t.width + 2, (y + h) * t.height + 1), f"{sc:.2f}", fill=(0, 220, 255))
        if v.get("bird"):
            x, y, w, h = v["bird"]
            d.rectangle([x * t.width, y * t.height, (x + w) * t.width, (y + h) * t.height],
                        outline=(255, 230, 0), width=2)
        star = " BEST" if v.get("burst_best") else ""
        _label(d, (4, 4), f"{short(k)}  b{v.get('burst', '?')}{star}")
        overview.append(t)
        if v.get("bird"):
            c = _crop_of(source(k), v["bird"], .2, 300)
        else:
            c = _thumb(source(k), (300, 300))
        d = ImageDraw.Draw(c)
        _label(d, (4, 4), f"{short(k)} s{v['sharp']:.0f}" + ("" if v.get("bird") else " NO BIRD"),
               (255, 230, 0) if v.get("bird") else (255, 80, 200))
        birds.append(c)
    sheets = []
    for i in range(0, len(overview), 12):
        sheets.append(str(_grid(overview[i:i + 12], 3, (400, 267), wd / f"overview_{i // 12}.jpg")))
    for i in range(0, len(birds), 16):
        sheets.append(str(_grid([_pad(b, 300) for b in birds[i:i + 16]], 4, (300, 300),
                                wd / f"birds_{i // 16}.jpg")))

    out = {"folder": folder, "location": (st.get("active") or {}).get("location", ""),
           "prepared": datetime.now().isoformat(timespec="seconds"),
           "files": info, "bursts": bursts, "duplicate_exports": dupes, "sheets": sheets}
    (wd / "analysis.json").write_text(json.dumps(out, indent=1))

    nobird = [short(k) for k, v in info.items() if not v.get("bird")]
    print(f"  {len(files)} photos, {len(bursts)} bursts, "
          f"{sum(1 for v in info.values() if v.get('bird'))} with a bird found"
          + (f"; NO BIRD FOUND: {', '.join(nobird)}" if nobird else ""))
    if dupes:
        print(f"  duplicate exports (same RAW): {dupes}")
    print(f"  default location: {out['location'] or '(none)'}")
    print("  sheets:")
    for s in sheets:
        print(f"    {s}")
    print(f"  analysis: {wd / 'analysis.json'}")
    print("\n  burst  photo   sharp  bird%  light  note")
    for i, b in enumerate(bursts, 1):
        for k in b:
            v = info[k]
            print(f"  b{i:<4} {short(k):<7} {v['sharp']:>5.0f}  {v['area'] * 100:>5.1f}  "
                  f"{v['light']:>5}  {'best' if v.get('burst_best') else ''}"
                  f"{'' if v.get('bird') else ' no bird found'}")


def _pad(im, side):
    from PIL import Image
    if im.size == (side, side):
        return im
    c = Image.new("RGB", (side, side), (24, 24, 24))
    c.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
    return c


def _analysis():
    _, folder, _ = batch_state()
    p = work_dir(folder) / "analysis.json"
    if not p.exists():
        sys.exit("Run `prepare` first.")
    return work_dir(folder), json.loads(p.read_text())


def cmd_zoom(args):
    """Full-resolution close-ups of the bird, for field marks.
    zoom 0617 0333            the detected bird, tight
    zoom 0333@0.55,0.52,0.1   centred at x,y (fractions), width 0.1 of the frame"""
    wd, an = _analysis()
    from PIL import ImageDraw
    for a in args:
        name, _, at = a.partition("@")
        f = norm(name)
        if at:
            cx, cy, s = (float(v) for v in at.split(","))
            W, H = an["files"][f]["size"]
            box = [cx - s / 2, cy - s * W / H / 2, s, s * W / H]
            pad = 0
        else:
            box = an["files"].get(f, {}).get("bird")
            if not box:
                print(f"{short(f)}: no bird found — give a point: zoom {short(f)}@x,y,size")
                continue
            pad = .12
        c = _crop_of(source(f), box, pad, 1200)
        _label(ImageDraw.Draw(c), (4, 4), short(f))
        out = wd / f"zoom_{short(f)}.jpg"
        c.save(out, quality=90)
        print(out)


def cmd_grid(args):
    """The whole frame with a 10x10 coordinate grid, for placing a bird the
    detector missed: read off its box and pass it as "bird": [x, y, w, h].
    grid 0617=0.42,0.43,0.11,0.11 draws that box in green and re-scores its
    sharpness — the sheet's score belongs to the detector's (wrong) box."""
    wd, an = _analysis()
    from PIL import ImageDraw
    for a in args:
        name, _, mine = a.partition("=")
        f = norm(name)
        mybox = [float(v) for v in mine.split(",")] if mine else None
        im = _thumb(source(f), (1500, 1000))
        d = ImageDraw.Draw(im)
        W, H = im.size
        for i in range(1, 10):
            d.line([(W * i / 10, 0), (W * i / 10, H)], fill=(255, 0, 255), width=1)
            d.line([(0, H * i / 10), (W, H * i / 10)], fill=(255, 0, 255), width=1)
            d.text((W * i / 10 + 3, 3), f".{i}", fill=(255, 0, 255))
            d.text((3, H * i / 10 + 3), f".{i}", fill=(255, 0, 255))
        v = an["files"].get(f, {})
        if v.get("bird"):
            x, y, w, h = v["bird"]
            d.rectangle([x * W, y * H, (x + w) * W, (y + h) * H], outline=(255, 230, 0), width=2)
        if mybox:
            x, y, w, h = mybox
            d.rectangle([x * W, y * H, (x + w) * W, (y + h) * H], outline=(0, 255, 90), width=3)
            import cv2
            sharp, light = sharpness(cv2.imread(str(source(f))), mybox)
            print(f"{short(f)}: your box {mybox} → sharpness {sharp:.0f}, light {light} "
                  f"(detector's box scored {v.get('sharp', 0):.0f})")
        _label(d, (W - 90, H - 20), short(f))
        out = wd / f"grid_{short(f)}.jpg"
        im.save(out, quality=88)
        print(out)


def _sort_status(st, folder):
    """(touched, ours) for the batch's current arrangement. The app seeds an
    untouched batch with filename-order lanes and blank species; anything
    beyond that — a species typed, a photo excluded — is Scott's, unless it is
    exactly what this tool last saved."""
    cur = st.get("arrangement") or {}
    touched = bool(cur.get("excluded")) or any(
        (m or {}).get("species") for m in (cur.get("meta") or {}).values())
    last = WORK_ROOT / folder / "applied.json"
    ours = touched and last.exists() and json.loads(last.read_text()).get("arrangement") == {
        k: cur.get(k) for k in ("lanes", "excluded", "meta")}
    return touched, ours


def cmd_pending(args):
    """Exit 0 when a batch on the posting step still wants sorting, else 1.
    sort_batch.sh asks before every attempt, so a run that waited out a usage
    limit doesn't re-sort something already sorted (by us or by Scott)."""
    st, folder = (batch_state()[:2] if os.environ.get("BIRD_SORT_TEST")
                  else (api("/api/state"), None))
    folder = folder or (st.get("active") or {}).get("folder")
    if st.get("proc_step") != "posting" or not folder or not st.get("new_birds"):
        print("Nothing to sort: no batch with exports on the posting step.")
        sys.exit(1)
    touched, ours = _sort_status(st, folder)
    if ours:
        print(f"{folder} is already sorted.")
        sys.exit(1)
    if touched:
        print(f"Scott has started sorting {folder} himself — leaving it.")
        sys.exit(1)
    print(f"{folder} needs sorting.")


# What a failed headless run means. Usage limits and a lapsed sign-in both
# clear up on their own schedule — the limit resets, Scott logs in again — so
# the run waits and retries rather than giving up; anything else is a real
# failure and is reported once.
LIMIT_RE = (r"(usage|rate|session|weekly|5-hour) limit|limit (reached|exceeded|hit)"
            r"|hit your (usage )?limit|out of (extra )?usage|too many requests"
            r"|\b429\b|overloaded|\b529\b")
AUTH_RE = (r"not logged in|please (run )?(/login|claude (auth )?login)|log ?in again"
           r"|authenticat|unauthori[sz]ed|\b401\b|token (has )?expired|expired token"
           r"|invalid (api key|token|credentials)|sign-in (has )?(ended|expired)")
LIMIT_WAIT, AUTH_WAIT, BUFFER = 30 * 60, 2 * 3600, 120


def reset_time(text, now):
    """When a limit message says the limit resets, as epoch seconds, or None.
    Understands "|1732300000", "resets 3pm", "resets at 3:30 PM", "in 2 hours"."""
    import re
    m = re.search(r"\|(\d{10})\b", text)
    if m:
        return int(m.group(1))
    m = re.search(r"in (\d+)\s*(minute|min|hour|hr)s?\b", text, re.I)
    if m:
        n = int(m.group(1))
        return now + n * (3600 if m.group(2).lower().startswith("h") else 60)
    m = re.search(r"reset\w*\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)", text, re.I)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower().startswith("p") else 0)
        t = datetime.fromtimestamp(now).replace(hour=h, minute=int(m.group(2) or 0),
                                                second=0, microsecond=0).timestamp()
        return int(t if t > now else t + 86400)
    return None


def classify_run(output, code, now):
    """('ok'|'wait'|'fail', wake epoch or 0, reason) for one `claude -p` run."""
    import re
    result, is_error = output, code != 0
    try:                                   # --output-format json
        j = json.loads(output[output.index("{"):])
        result = str(j.get("result") or "")
        is_error = is_error or bool(j.get("is_error"))
    except (ValueError, AttributeError):
        pass
    if not is_error:
        return "ok", 0, "sorted"
    if re.search(LIMIT_RE, output, re.I):
        wake = reset_time(output, now) or now + LIMIT_WAIT
        return "wait", int(max(wake, now + 60) + BUFFER), "usage limit"
    if re.search(AUTH_RE, output, re.I):
        return "wait", int(now + AUTH_WAIT), "Claude sign-in lapsed — run `claude auth login`"
    return "fail", 0, (result or output).strip().splitlines()[-1][:200] if (result or output).strip() else f"exit {code}"


def cmd_classify(args):
    """stdin = a run's output; argv = its exit code. Prints the run's own
    summary, then a last line: `ok 0 …`, `wait <epoch> <reason>`, `fail 0 <reason>`."""
    out = sys.stdin.read()
    code = int(args[0]) if args else 0
    try:
        print(str(json.loads(out[out.index("{"):]).get("result") or "").strip())
    except (ValueError, AttributeError):
        print(out.strip()[-4000:])
    verdict, wake, reason = classify_run(out, code, time.time())
    print(f"{verdict} {wake} {reason}")


def _load_plan(path, an, files):
    plan = json.loads(Path(path).read_text())
    posts = plan.get("posts") or []
    seen, errs = set(), []
    for i, post in enumerate(posts, 1):
        photos = post.get("photos") or []
        if not photos:
            errs.append(f"post {i} is empty")
        if len(photos) > MAX_PER_POST:
            errs.append(f"post {i} has {len(photos)} photos (max {MAX_PER_POST})")
        for ph in photos:
            ph["file"] = f = norm(ph.get("file", ""))
            if f not in files:
                errs.append(f"post {i}: {f} is not in this batch")
            if f in seen:
                errs.append(f"{f} is in more than one post")
            seen.add(f)
            if not (ph.get("species") or "").strip():
                errs.append(f"post {i}: {f} has no species")
            if ph.get("facing") not in (None, "left", "right"):
                errs.append(f"{f}: facing must be left, right or null")
    if not posts:
        errs.append("plan has no posts")
    if errs:
        sys.exit("Plan rejected:\n  " + "\n  ".join(errs))
    return plan


def _box_for(ph, an):
    """The /api/crop box for one planned photo, or None to leave it uncropped."""
    v = an["files"][ph["file"]]
    crop = ph.get("crop", 1.0)
    if crop == "none":
        return None
    if isinstance(crop, dict):
        return {k: float(crop[k]) for k in ("x", "y", "w", "h")}
    bird = ph.get("bird") or v.get("bird")
    if not bird:
        return None
    W, H = v["size"]
    return crop_box(bird, W, H, ph.get("facing"), float(crop))


def cmd_preview(args):
    """Render each post as it would go out: order, species, crop."""
    if not args:
        sys.exit("usage: preview plan.json")
    st, folder, files = batch_state()
    wd, an = _analysis()
    plan = _load_plan(args[0], an, files)
    from PIL import Image, ImageDraw, ImageOps
    for i, post in enumerate(plan["posts"], 1):
        tiles = []
        for j, ph in enumerate(post["photos"], 1):
            im = Image.open(source(ph["file"]))
            im.draft("RGB", (im.width // 4, im.height // 4))   # fast JPEG downscale
            im = ImageOps.exif_transpose(im).convert("RGB")
            W, H = im.size
            b = _box_for(ph, an)
            if b:
                im = im.crop((int(b["x"] * W), int(b["y"] * H),
                              int((b["x"] + b["w"]) * W), int((b["y"] + b["h"]) * H)))
            k = min(420 / im.width, 280 / im.height)
            im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))))
            t = _pad_to(im, (420, 280))
            d = ImageDraw.Draw(t)
            real_w = an["files"][ph["file"]]["size"][0]
            px = f" {int(b['w'] * real_w)}px" if b else " uncropped"
            _label(d, (4, 4), f"{j}. {short(ph['file'])}{px}")
            _label(d, (4, 262), ph["species"][:48], (255, 255, 255))
            if ph.get("flag"):
                _label(d, (4, 22), "FLAG: " + ph["flag"][:44], (255, 120, 120))
            tiles.append(t)
        out = _grid(tiles, 3, (420, 280), wd / f"preview_post{i}.jpg")
        print(out)


def _pad_to(im, size):
    from PIL import Image
    c = Image.new("RGB", size, (24, 24, 24))
    c.paste(im, ((size[0] - im.width) // 2, (size[1] - im.height) // 2))
    return c


def cmd_apply(args):
    """Write the plan into the app: crops first, then the arrangement."""
    if os.environ.get("BIRD_SORT_TEST"):
        sys.exit("apply is disabled under BIRD_SORT_TEST.")
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    if not args:
        sys.exit("usage: apply plan.json [--force]")
    st, folder, files = batch_state()
    wd, an = _analysis()
    plan = _load_plan(args[0], an, files)

    touched, ours = _sort_status(st, folder)
    last = wd / "applied.json"
    if touched and not ours and not force:
        sys.exit("The batch already has a sorting that this tool didn't write "
                 "(species or exclusions set). Not overwriting it — rerun with "
                 "--force only if Scott asked for a re-sort.")

    default_loc = an.get("location") or (st.get("active") or {}).get("location", "")
    lanes, meta, cropped = [], {}, 0
    for post in plan["posts"]:
        lane = []
        for ph in post["photos"]:
            f = ph["file"]
            lane.append(f)
            m = {"species": ph["species"].strip(),
                 "location": ph.get("location", default_loc),
                 "oos": bool(ph.get("oos"))}
            if ph.get("flag"):
                m["flag"] = ph["flag"]
            meta[f] = m
            box = _box_for(ph, an)
            status = api(f"/api/crop-status?file={urllib.request.quote(f)}")
            if status.get("hasOriginal"):
                api("/api/crop", {"file": f, "reset": True})
            if box:
                r = api("/api/crop", {"file": f, "box": box})
                if not r.get("ok"):
                    sys.exit(f"Crop failed for {f}: {r}")
                cropped += 1
        lanes.append(lane)
    used = {f for lane in lanes for f in lane}
    excluded = [f for f in files if f not in used]
    for f in excluded:
        meta[f] = {"species": "", "location": default_loc, "oos": False}
    body = {"lanes": lanes, "excluded": excluded, "meta": meta,
            "note": "sort-batch: " + (plan.get("summary") or "")[:300]}
    r = api("/api/arrangement", body)
    if not r.get("ok"):
        sys.exit(f"Saving the arrangement failed: {r}")
    saved = api("/api/state").get("arrangement") or {}
    last.write_text(json.dumps({
        "at": datetime.now().isoformat(timespec="seconds"), "plan": plan,
        "arrangement": {k: saved.get(k) for k in ("lanes", "excluded", "meta")}}, indent=1))
    report = [f"# {folder}", "", plan.get("summary", ""), ""]
    for i, post in enumerate(plan["posts"], 1):
        sp = []
        for ph in post["photos"]:
            if ph["species"] not in sp:
                sp.append(ph["species"])
        report.append(f"- Post {i} ({len(post['photos'])}): {', '.join(sp)} — "
                      + ", ".join(short(ph['file']) for ph in post["photos"]))
        for ph in post["photos"]:
            if ph.get("flag"):
                report.append(f"  - FLAG {short(ph['file'])}: {ph['flag']}")
    report.append(f"- Excluded ({len(excluded)}): " + ", ".join(short(f) for f in excluded))
    (wd / "report.md").write_text("\n".join(report) + "\n")
    print(f"Saved {len(lanes)} posts ({len(used)} photos, {cropped} cropped), "
          f"{len(excluded)} excluded. Report: {wd / 'report.md'}")


COMMANDS = {"prepare": cmd_prepare, "zoom": cmd_zoom, "grid": cmd_grid,
            "pending": cmd_pending, "classify": cmd_classify,
            "preview": cmd_preview, "apply": cmd_apply}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    try:
        COMMANDS[sys.argv[1]](sys.argv[2:])
    except urllib.error.URLError as e:
        sys.exit(f"Can't reach the app at {BASE}: {e}")


if __name__ == "__main__":
    main()
