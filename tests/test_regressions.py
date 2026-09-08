#!/usr/bin/env python3
"""
Regression tests for the bird pipeline.

Every check here exists because something actually broke in use. Run before
committing anything that touches server.py or templates/index.html:

    python3 tests/test_regressions.py            # static checks only
    python3 tests/test_regressions.py --live     # also hit a running app

Static checks need no server and catch the class of failure that has hurt
most: a stylesheet or script silently losing chunks during an edit.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "templates" / "index.html"
CURATE = ROOT / "templates" / "curate.html"
BASE = "http://localhost:8765"

failures, checks = [], 0


def check(name, ok, detail=""):
    global checks
    checks += 1
    if ok:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
        failures.append(name)


# ── The page's own integrity ────────────────────────────────────────────────
# A bad edit once deleted 229 lines of CSS, taking the crop overlay, buttons
# and log column with it. Size floors catch that instantly.

def page_parts(path):
    html = path.read_text()
    css = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))
    js = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    return html, css, js


def test_index_structure():
    html, css, js = page_parts(INDEX)
    check("index: stylesheet is substantial", len(css.splitlines()) > 400,
          f"only {len(css.splitlines())} lines")
    check("index: css braces balanced", css.count("{") == css.count("}"),
          f"{css.count('{')} open vs {css.count('}')} close")
    check("index: script is substantial", len(js.splitlines()) > 800,
          f"only {len(js.splitlines())} lines")

    # Selectors whose loss silently breaks a feature rather than erroring.
    for sel in ("#crop-overlay", "#crop-box", ".crop-handle", "#crop-dims",
                ".img-modal", ".img-modal-tools", ".img-modal-nav",
                ".photo-card", ".lane-cards", ".log-col", ".btn-primary",
                ".shared-fields", ".chiprow", ".vchip", ".batch-card"):
        check(f"index: css keeps {sel}", sel in css)

    for fn in ("openModal", "modalStep", "updateModalTools", "startCrop",
               "applyCrop", "resetCrop", "excludeCard", "restoreCard",
               "moveCard", "renderPostForm", "renderBatches", "doPost",
               "syncArrangement", "setFilter", "autoDenoise", "rescanExports"):
        check(f"index: js keeps {fn}()", f"function {fn}(" in js or f"{fn} = " in js)


def test_curate_structure():
    html, css, js = page_parts(CURATE)
    check("curate: css braces balanced", css.count("{") == css.count("}"))
    for fn in ("boot", "render", "setPick", "doSync", "toggleDone"):
        check(f"curate: js keeps {fn}()", f"function {fn}(" in js)


def test_js_syntax():
    for path in (INDEX, CURATE):
        _, _, js = page_parts(path)
        tmp = Path(f"/tmp/_syntax_{path.stem}.js")
        tmp.write_text(js)
        r = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
        check(f"{path.name}: js parses", r.returncode == 0, r.stderr.strip()[:160])
        tmp.unlink(missing_ok=True)


def test_server_syntax():
    for f in ("server.py", "lr_host.py", "bird_post.py", "syno_fetch.py",
              "syno_curate.py", "lr_denoise.py", "lr_dismiss.py", "lr_verify.py",
              "ig_post.py"):
        p = ROOT / f
        if not p.exists():
            continue
        r = subprocess.run([sys.executable, "-c", f"import ast;ast.parse(open({str(p)!r}).read())"],
                           capture_output=True, text=True)
        check(f"{f}: parses", r.returncode == 0, r.stderr.strip()[:160])


def test_no_duplicate_defs():
    """A route and a helper sharing a name silently shadowed each other once,
    which made the denoise/export gate call a Flask view from a thread."""
    import ast
    tree = ast.parse((ROOT / "server.py").read_text())
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    dupes = {n for n in names if names.count(n) > 1}
    check("server: no duplicate top-level functions", not dupes, str(dupes))


def test_import_verification_wired():
    """The import step must not trust lr_auto's exit code alone — it has twice
    reported success while Lightroom ingested nothing, leaving auto-tone to
    run against the previous batch."""
    src = (ROOT / "server.py").read_text()
    check("server: import compares Lightroom's screen before/after",
          "_await_import(" in src and "lr_verify.fingerprint(" in src)
    check("server: a failed import stops the run",
          'set_step("import_failed")' in src)
    # auto-tone must come after that guard, never before it
    i_guard = src.find('set_step("import_failed")')
    i_tone = src.find('log("Applying Auto Settings to all photos…")')
    check("server: auto-tone sits after the import guard",
          0 < i_guard < i_tone)

    html = INDEX.read_text()
    check("index: import_failed has its own step card", "import_failed:" in html)
    check("index: import_failed offers a retry", "retryImport" in html)
    check("server: gotoStep accepts 'import' for that retry",
          '"import", "tone", "denoise", "pick", "collect"' in src)


def test_denoise_spread_verified():
    """"Denoise applied to all photos" was printed on the strength of a menu
    click alone. The paste must be read back off real photos, and only once
    Lightroom's own "Updating AI Settings" modal has cleared — that window
    covers the Edit panel, so checking too early reads a good batch as broken."""
    src = (ROOT / "server.py").read_text()
    check("server: the spread is verified, not assumed", "_verify_spread(" in src)
    # It must never block: reading a checkbox off a screenshot is not reliable
    # enough to stop a run, and a false negative once trapped a good batch at
    # the denoise gate with no way forward.
    check("server: a failed spread check warns but does not block",
          'if spread == "none"' in src and "Continuing anyway" in src)
    i_sp = src.find("spread = _verify_spread(")
    tail = src[i_sp:i_sp + 700]
    check("server: verification never sends the run back a step",
          'set_step("denoise")' not in tail)
    # order: paste → wait for Lightroom → verify → export gate
    i_paste  = src.find("Denoise applied to all photos")
    i_verify = src.find("spread = _verify_spread(")
    i_export = src.find('set_step("export_ready")')
    check("server: verification sits between the denoise wait and export",
          0 < i_paste < i_verify < i_export)

    den = (ROOT / "lr_denoise.py").read_text()
    check("lr_denoise: has a spread sampler", "def cmd_spread(" in den)
    check("lr_denoise: skips the check while a progress modal covers the panel",
          '"progress" in lr_window_names().lower()' in den)
    check("lr_denoise: an unreadable panel reports unknown, never a verdict",
          den.count("spread: unknown") >= 3)
    check("lr_denoise: re-finds the label on every frame",
          'find_label(shot, "denoise", DETAIL_SCAN)' in den
          and "panel not readable" in den)
    check("lr_denoise: collapses the multi-selection before looking",
          "arrow(124)" in den and "arrow(123)" in den)
    check("lr_host: allows denoise-spread", '"denoise-spread"' in
          (ROOT / "lr_host.py").read_text())


def test_post_busy_is_per_lane():
    """Posting one post used to light up every post's button as "⏳ Posting…",
    because each lane read one shared busy flag. Only the lane in flight may
    say it is posting — though all buttons still disable, since the server
    permits one post at a time."""
    _, _, js = page_parts(INDEX)
    check("index: postState remembers which lane is in flight", "lane: null" in js
          and "lane: i }" in js)
    check("index: the label is per-lane", "thisBusy ? '⏳ Posting…'" in js)
    check("index: every button still disables while one is posting",
          "anyBusy ? 'disabled' : ''" in js)
    check("index: busy lane is matched by index", "postState.lane === i" in js)
    # every terminal transition must clear the lane, or a stale index would
    # keep one button stuck reading "Posting…"
    starts = js.count("postState = { busy: false")
    clears = js.count("lane: null")
    check("index: every finished post clears the lane", clears >= starts,
          f"{starts} terminal states vs {clears} clears")


def test_curate_serves_previews():
    """The curator swipes through hundreds of frames, and Synology's "xl" is
    1920x1280 at ~500 KB — fine on the LAN, slow over Tailscale on cellular.
    Serve a downscaled copy cached app-side, same as /bird-preview."""
    src = (ROOT / "server.py").read_text()
    check("server: curate previews are downscaled", "CURATE_PREVIEW_EDGE" in src
          and "thumbnail((CURATE_PREVIEW_EDGE" in src)
    check("server: curate previews are cached on disk", "CURATE_PREVIEW_DIR" in src)
    check("server: a failed fetch is not cached as a good preview",
          'return "", 502' in src)

    cur = CURATE.read_text()
    check("curate: the big image goes through the source picker",
          "big.src=imgSrc(c.id)" in cur and "'/curate-preview/'+id" in cur)
    check("curate: still falls back to the raw thumbnail",
          "big.src='/curate-thumb/m/'+c.id" in cur)
    check("curate: preloads both directions", "idx+ahead" in cur and "idx-2" in cur)
    # Full size is opt-in: it is ~3.5x the bytes, which is the whole point of
    # the preview. It must never be the default a phone on cellular gets.
    check("curate: full size is off unless explicitly turned on",
          "localStorage.getItem('curate_full_size')==='1'" in cur)
    check("curate: the toggle switches the source", "function imgSrc(" in cur
          and "fullSize ? '/curate-thumb/xl/'" in cur)
    check("curate: the choice persists", "setItem('curate_full_size'" in cur)
    check("curate: preloads less far ahead at full size",
          "fullSize ? 4 : 10" in cur)


def test_ig_post_contract():
    """The Buffer replacement. These are the details that fail silently or
    embarrassingly if they drift, so they are pinned."""
    src = (ROOT / "ig_post.py").read_text()
    # The Instagram Login token is rejected by graph.facebook.com, which is the
    # older Page-linked flow — an easy and confusing mix-up.
    check("ig_post: talks to graph.instagram.com",
          'API = "https://graph.instagram.com"' in src
          and 'API = "https://graph.facebook.com"' not in src)
    check("ig_post: waits for the container before publishing",
          "_await_container(" in src and '"FINISHED"' in src)
    check("ig_post: honours the 10-image carousel limit", "MAX_CAROUSEL = 10" in src)
    check("ig_post: uploads stay private, shared by presigned link",
          "generate_presigned_url" in src and "public-read" not in src)
    check("ig_post: reuses the pipeline's resize and caption",
          "from bird_post import" in src and "resize_for_instagram" in src)
    check("ig_post: can verify credentials without posting", "def check(" in src)
    check("ig_post: can roll the 60-day token", "ig_refresh_token" in src)
    check("Dockerfile copies ig_post.py", "COPY ig_post.py" in
          (ROOT / "Dockerfile").read_text())


def test_lr_verify_logic():
    """The screen comparison itself: it must spot a changed filmstrip, ignore
    an unchanged one, and report 'unknown' rather than 'unchanged' when there
    is no screenshot — a missing capture is a permissions problem, not a
    failed import."""
    sys.path.insert(0, str(ROOT))
    try:
        import lr_verify
        from PIL import Image
    except ImportError as e:
        print(f"  skip  lr_verify logic ({e})")
        return
    import io

    def shot(fill, band):
        im = Image.new("L", (400, 300), fill)
        for x in range(0, 320):                 # the filmstrip band
            for y in range(240, 300):
                im.putpixel((x, y), band)
        buf = io.BytesIO(); im.save(buf, "PNG")
        return buf.getvalue()

    same_a = lr_verify.fingerprint(shot(40, 90))
    same_b = lr_verify.fingerprint(shot(40, 90))
    other  = lr_verify.fingerprint(shot(40, 220))

    check("lr_verify: builds a fingerprint", bool(same_a))
    check("lr_verify: identical screens read as unchanged",
          lr_verify.changed(same_a, same_b) is False)
    check("lr_verify: a different filmstrip reads as changed",
          lr_verify.changed(same_a, other) is True)
    check("lr_verify: no screenshot reads as unknown, not unchanged",
          lr_verify.changed(same_a, None) is None
          and lr_verify.fingerprint(b"") is None)
    check("lr_verify: ignores changes outside the filmstrip band",
          lr_verify.changed(lr_verify.fingerprint(shot(40, 90)),
                            lr_verify.fingerprint(shot(200, 90))) is False)


def test_container_has_server_imports():
    """Every module server.py imports must be copied into the image. lr_verify
    was added and not COPYed, so the import step would have raised
    ModuleNotFoundError inside the container while passing every local check."""
    import ast
    src = (ROOT / "server.py").read_text()
    tree = ast.parse(src)
    local = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                if (ROOT / f"{n.name}.py").exists():
                    local.add(n.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if (ROOT / f"{node.module}.py").exists():
                local.add(node.module)
    dockerfile = (ROOT / "Dockerfile").read_text()
    for mod in sorted(local):
        check(f"Dockerfile copies {mod}.py", f"COPY {mod}.py" in dockerfile)


def test_route_decorators_attached():
    """A constant inserted between @app.route and its function orphans the
    decorator — the route silently disappears."""
    src = (ROOT / "server.py").read_text().splitlines()
    bad = []
    for i, line in enumerate(src):
        if line.startswith("@app.") and "route" not in line and "(" in line:
            nxt = next((s for s in src[i + 1:] if s.strip()), "")
            if not (nxt.startswith("def ") or nxt.startswith("@")):
                bad.append(f"line {i+1}: {line.strip()}")
    check("server: every @app decorator precedes a def", not bad, "; ".join(bad[:3]))


# ── Live behaviour ──────────────────────────────────────────────────────────

def test_live():
    import httpx
    try:
        state = httpx.get(f"{BASE}/api/state", timeout=20).json()
    except Exception as e:
        check("live: /api/state responds", False, str(e))
        return
    check("live: /api/state responds", True)
    for key in ("batches", "proc_step", "photo_versions", "buffer_ready"):
        check(f"live: state exposes {key}", key in state)

    check("live: index serves", httpx.get(BASE, timeout=20).status_code == 200)
    check("live: curate serves", httpx.get(f"{BASE}/curate", timeout=20).status_code == 200)

    files = state.get("new_birds") or []
    if not files:
        print("  skip  live image checks (no staged photos)")
        return
    f = files[0]

    # Serving paths that have each broken at least once.
    full = httpx.get(f"{BASE}/bird-img/{f}", timeout=60)
    prev = httpx.get(f"{BASE}/bird-preview/{f}", timeout=60)
    thumb = httpx.get(f"{BASE}/bird-thumb/{f}", timeout=60)
    check("live: full image serves", full.status_code == 200)
    check("live: preview serves", prev.status_code == 200)
    check("live: thumbnail serves", thumb.status_code == 200)
    check("live: preview is much smaller than the original",
          len(prev.content) < len(full.content) / 5,
          f"{len(prev.content)} vs {len(full.content)}")
    check("live: preview revalidates rather than caching blind",
          "no-cache" in prev.headers.get("cache-control", ""),
          prev.headers.get("cache-control", ""))
    check("live: photo_versions covers staged photos",
          f in (state.get("photo_versions") or {}))


def main():
    print("static checks")
    test_index_structure()
    test_curate_structure()
    test_js_syntax()
    test_server_syntax()
    test_no_duplicate_defs()
    test_route_decorators_attached()
    test_import_verification_wired()
    test_denoise_spread_verified()
    test_post_busy_is_per_lane()
    test_curate_serves_previews()
    test_ig_post_contract()
    test_lr_verify_logic()
    test_container_has_server_imports()

    if "--live" in sys.argv:
        print("live checks")
        test_live()

    print(f"\n{checks - len(failures)}/{checks} passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
        sys.exit(1)


if __name__ == "__main__":
    main()
