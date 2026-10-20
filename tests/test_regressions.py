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
    # order within the tail: paste → wait for Lightroom → verify → export gate.
    # Measured inside the function, not by position in the file — the gate was
    # extracted above it, which made a whole-file offset comparison meaningless.
    tail = src[src.find("def _denoise_and_export_tail("):]
    tail = tail[:tail.find("\ndef ", 1)]
    i_paste  = tail.find("Denoise applied to all photos")
    i_verify = tail.find("spread = _verify_spread(")
    i_gate   = tail.rfind("_export_gate()")   # the early one is the still-rendering bail-out
    check("server: verification sits between the denoise wait and export",
          0 < i_paste < i_verify < i_gate)

    den = (ROOT / "lr_denoise.py").read_text()
    check("lr_denoise: has a spread sampler", "def cmd_spread(" in den)
    check("lr_denoise: skips the check while a progress modal covers the panel",
          'in lr_window_names().lower()' in den or '"progress" in names' in den
          or '("progress", "updating", "pasting")' in den)
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
    # The originals are ~26 MB NEFs and 1920x1280 is the largest rendition
    # Synology serves, so the button must not promise "full size".
    check("curate: the toggle does not claim to show the original",
          "full size'" not in cur and "1920" in cur)


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


def test_corrupt_exports_are_caught():
    """Lightroom has three times written an export whose lower half is magenta
    scanline garbage — a valid JPEG of plausible size, so nothing downstream
    noticed and one reached the posting screen. Detect it at collection."""
    src = (ROOT / "server.py").read_text()
    check("server: exports are scanned for render corruption",
          "_flag_corrupt_exports(" in src and "def _corrupt_fraction(" in src)
    check("server: the scan runs at collection, before dates or posting",
          src.find("_flag_corrupt_exports(new)") < src.find("_record_photo_dates(new)"))
    check("server: a corrupt export is parked, not deleted",
          "EXCLUDED_DIR / n" in src)
    # The RAW is always fine — say so, so nobody deletes a good photo.
    check("server: it says the RAW is fine", "The RAW is fine" in src)

    sys.path.insert(0, str(ROOT))
    try:
        from PIL import Image
    except ImportError:
        print("  skip  corruption detector (no Pillow)")
        return
    import importlib.util, tempfile, os
    spec = importlib.util.spec_from_file_location("_srv", ROOT / "server.py")
    # Importing server.py starts Flask machinery, so re-implement the same
    # arithmetic here and assert the constant it uses is sane.
    check("server: the neon threshold is a small fraction",
          0 < float(re.search(r"NEON_LIMIT = ([\d.]+)", src).group(1)) < 0.05)

    def frac(img):
        px = list(img.convert("RGB").resize((200, 140)).getdata())
        bad = sum(1 for r, g, b in px
                  if (r > 180 and b > 180 and g < 90) or (g > 180 and r < 90 and b < 90))
        return bad / len(px)

    sky = Image.new("RGB", (400, 300), (120, 160, 210))      # a normal photo
    check("detector: a clean photo reads as undamaged", frac(sky) < 0.005)
    # Bands, not alternating rows: real damage comes in blocks tens of pixels
    # deep, which is what survives the downscale the detector does.
    damaged = sky.copy()
    for y in range(150, 300):
        band = (255, 40, 255) if (y // 8) % 2 else (40, 255, 40)
        for x in range(400):
            damaged.putpixel((x, y), band)
    check("detector: scanline garbage is caught", frac(damaged) > 0.005)


def test_edit_panel_not_blindly_toggled():
    """The tool-rail icon toggles. The locate step used to click it whenever
    the Denoise label wasn't matched, assuming that meant the panel was shut —
    so with the panel already open but Detail collapsed or scrolled, the click
    closed the only thing it needed and every later attempt read a blank panel.
    `View > Edit` carries a checkmark when the panel is open; ask, don't guess."""
    den = (ROOT / "lr_denoise.py").read_text()
    check("lr_denoise: reads the panel's open state from the menu",
          "def edit_panel_open(" in den and '_view_flag("Edit")' in den)
    check("lr_denoise: opening an open panel is a no-op",
          "if edit_panel_open():\n        return False" in den)
    check("lr_denoise: Detail is only unhidden when hidden",
          "def show_detail_section(" in den
          and 'if menu_flag("Detail", "Edit Panels"):' in den)
    # The blind click must be gone from both the denoise paths. The dust path
    # still uses its own icon, so only EDIT_ICON is checked here.
    for fn in ("def run(", "def locate_denoise("):
        i = den.find(fn)
        body = den[i:i + 1600]
        check(f"lr_denoise: {fn.strip('def (')} no longer blind-clicks EDIT_ICON",
              "click(EDIT_ICON)" not in body)


def test_every_goto_step_is_handled():
    """"pick" was accepted by /api/goto-step and /api/run and handled by
    nothing: _run_batch matched none of its start_step blocks and returned
    instantly, so clicking Export in the progress bar logged "Restarting
    from: pick" and did nothing. Every step the API accepts must be reachable."""
    src = (ROOT / "server.py").read_text()
    m = re.search(r'VALID = \{([^}]*)\}', src)
    valid = set(re.findall(r'"([a-z_]+)"', m.group(1)))
    check("server: goto-step advertises the expected steps",
          valid == {"import", "tone", "denoise", "pick", "collect"}, str(valid))

    body = src[src.find("def _run_batch("):]
    body = body[:body.find("\ndef ", 1)]
    handled = set()
    for frag in re.findall(r'if start_step (?:==|in) ([^:]+):', body):
        handled |= set(re.findall(r'"([a-z_]+)"', frag))
    missing = valid - handled
    check("server: _run_batch handles every accepted step", not missing,
          f"unreachable: {sorted(missing)}")

    check("server: the export gate is its own function so 'pick' can reach it",
          "def _export_gate(" in src and "_export_gate()" in body)

    html = INDEX.read_text()
    backs = set(re.findall(r'back:\s*"([a-z_]+)"', html))
    check("server: every progress-bar back target is accepted", backs <= valid,
          f"UI sends {sorted(backs - valid)} which the API rejects")


def test_denoise_is_actually_rendered():
    """Pasting only ticks the Denoise checkbox. Lightroom keeps the AI edit
    *pending* — it shows a yellow badge reading "Highlighted settings need to
    be updated to properly render your photo" — and exporting in that state
    produces an un-denoised JPEG that the pipeline cheerfully called denoised.
    The paste step must explicitly ask Lightroom to render."""
    auto = (ROOT / "lr_auto.py").read_text()
    check("lr_auto: the paste step renders the AI settings",
          "def render_ai_settings(" in auto and "render_ai_settings()" in auto)
    # The label carries a photo count, so it can't be matched by exact name;
    # and clicking the loop variable doesn't open the menu, which made this
    # look like a no-op the first time round.
    check("lr_auto: the counted menu item is matched by prefix",
          'starts with "{prefix}"' in auto or "_click_menu_item_by_prefix(" in auto)
    check("lr_auto: it clicks by the resolved exact name",
          "lr_menu_click(menu_name, name)" in auto)
    check("lr_auto: nothing pending is not an error",
          "already rendered" in auto)
    # Scoped to the paste function — the module-level def also contains the
    # call's spelling, so a whole-file search matches the wrong thing.
    body = auto[auto.find("def cmd_copy_and_paste("):]
    body = body[:body.find("\ndef ", 1)]
    i_paste  = body.find("Pasting to entire selection")
    i_render = body.find("render_ai_settings()")
    check("lr_auto: the render is asked for after the paste",
          0 < i_paste < i_render)


def test_stalled_render_is_nudged():
    """The AI render wedges every couple of dozen photos: the dialog freezes
    on one count while Lightroom drops to idle. Recovery is always Stop →
    reselect → re-trigger, so the wait loop does it rather than making someone
    nurse a 94-photo batch by hand."""
    src = (ROOT / "server.py").read_text()
    check("server: a frozen progress count is detected",
          "stalled_for" in src and "STALL_AFTER" in src)
    # Denoise renders on the GPU, so Lightroom reads idle between photos while
    # working fine. Gating on CPU restarted a healthy render again and again.
    check("server: a nudge is not gated on CPU",
          "idle = float(call_host(\"lr-busy\"" not in src)
    m = re.search(r"^STALL_AFTER = (\d+)", src, re.M)
    check("server: a stall is minutes, not one slow photo (~25s)",
          bool(m) and int(m.group(1)) >= 300)
    check("server: nudging is bounded", "MAX_NUDGES" in src
          and "nudges < MAX_NUDGES" in src)
    check("server: the nudge budget resets when the count moves",
          "stalled_for, nudges = 0, 0" in src)
    check("server: no fixed 60-minute cap on a big batch",
          "while waited < 3600" not in src and "DENOISE_CAP" in src)
    tail = src[src.find("def _denoise_and_export_tail("):src.find("def _run_batch(")]
    check("server: never runs the spread check while Lightroom is rendering",
          tail.find('lr_modal_progress().get("active")') < tail.find("_verify_spread()"))

    den = (ROOT / "lr_denoise.py").read_text()
    spread = den[den.find("def cmd_spread("):]
    check("lr_denoise: spread check stands down for the render dialog too",
          '"updating"' in spread and '"pasting"' in spread)

    auto = (ROOT / "lr_auto.py").read_text()
    check("lr_auto: ai-resume exists", "def cmd_ai_resume(" in auto)
    # Dismissing the dialog collapses the selection to one photo, which drops
    # the count from the menu item — reselecting is what makes the retry work.
    check("lr_auto: ai-resume reselects before re-triggering",
          'lr_menu_click("Edit", "Select All")' in
          auto[auto.find("def cmd_ai_resume("):auto.find("def cmd_import(")])
    check("lr_host: allows ai-resume", '"ai-resume"' in (ROOT / "lr_host.py").read_text())
    # A wedged paste is unstuck with "Update AI Settings Later" — safe only
    # because ai-resume then reselects and asks for the render itself.
    body = auto[auto.find("def cmd_ai_resume("):auto.find("def cmd_import(")]
    check("lr_auto: ai-resume unsticks a paste by its button name",
          '"Pasting" in label' in body
          and 'click button "Update AI Settings Later"' in body)
    check("lr_auto: Stop is clicked by name, not a screen coordinate",
          'click button "Stop" of w' in auto and "CLICLICK, f\"c:{x + w" not in body)
    check("lr_auto: never starts a render beside a running one",
          "if _render_dialog_count():" in auto[auto.find("def render_ai_settings("):])
    check("lr_auto: ai-resume bails if a render dialog won't close",
          "sys.exit(1)" in body[body.find("_stop_render_dialogs()"):])
    rend = auto[auto.find("def render_ai_settings("):auto.find("CLICLICK = ")]
    check("lr_auto: render refuses a selection that isn't the batch",
          "got != expect" in rend)
    check("lr_auto: ai-resume refuses to run without a batch size",
          "if args.expect is None:" in body)
    check("lr_host: passes the batch size through",
          '"--expect"' in (ROOT / "lr_host.py").read_text())
    check("server: every render request carries the batch size",
          src.count('body={"expect": _batch_size()}') == 3)
    each = auto[auto.find("def cmd_render_each("):auto.find("# ── Main")]
    check("lr_auto: render-each exists and is allowed through the bridge",
          bool(each) and '"render-each"' in (ROOT / "lr_host.py").read_text())
    check("lr_auto: render-each checks view, selection and dialogs per photo",
          '_selection_header()' in each and '== 1 else None' in each
          and each.count("_render_dialog_count()") >= 4)
    check("lr_auto: render-each skips a wedged photo instead of stalling",
          "bad.append(i)" in each and "RENDER_EACH_LIMIT" in each)
    check("server: falls back to render-each once nudges stop helping",
          'call_host("render-each"' in src and "went_each = True" in src)
    exp = auto[auto.find("def cmd_export("):auto.find("def cmd_status(")]
    check("lr_auto: export refuses when Lightroom would write elsewhere",
          "_lr_export_folder()" in exp and "!= BIRDS_DIR" in exp)
    check("lr_auto: export Select All is checked against the batch size",
          "got != args.expect" in exp)
    check("server: Post can send to birdqueue instead of Buffer",
          'data.get("backend") == "birdqueue"' in src and "def _queue_to_birdqueue(" in src)
    check("server: birdqueue posts skip Buffer's calendar",
          "if use_bq:\n            slots = []" in src)
    check("server: both backends record a chunk the same way",
          src.count("_mark_chunk_posted(chunk_files)") == 2)
    check("compose: container reaches the birdqueue tunnel on the host",
          "BIRDQUEUE_URL=http://host.docker.internal:8910" in (ROOT / "docker-compose.yml").read_text())
    ui = (ROOT / "templates/index.html").read_text()
    check("ui: both Post buttons say where to send",
          ui.count("backend: postBackend()") == 2 and 'id="pf-bq"' in ui)
    check("lr_auto: ...and still renders afterwards",
          body.rfind("render_ai_settings()") > body.find("Update AI Settings Later\""))

def test_dust_section_not_ambiguous():
    """Distraction Removal grew two more collapsible sub-sections above Dust
    (Reflections, People) with an identically-labelled "Apply" checkbox each.
    A stale header template once matched "Reflections" while printing "Dust >
    Apply", silently ticking the wrong one. The header template must be a real
    crop of the word "Dust" (not reused from an older, differently-shaped
    panel), and the shape-search fallback — which can't tell three identical
    checkboxes apart — must never run for this kind."""
    tpl = ROOT / "lr_templates" / "hdr_dust.png"
    check("hdr_dust.png exists", tpl.exists())
    try:
        from PIL import Image
        im = Image.open(tpl)
        check("hdr_dust.png is a small greyscale crop, not a placeholder",
              im.mode == "L" and 20 <= im.width <= 120 and 10 <= im.height <= 40,
              str(im.size))
    except ImportError:
        print("  skip  hdr_dust.png shape check (no Pillow)")

    den = (ROOT / "lr_denoise.py").read_text()
    check("lr_denoise: the shape-search fallback is skipped for dust",
          'if not box and kind != "dust":' in den)
    check("lr_denoise: says why — three identical Apply checkboxes now exist",
          "Reflections, People, Dust" in den or "three collapsible" in den)
    # Templates are cropped from /screen (1500x974) but matched in grab()'s
    # logical space (1512x982) — 0.8% off, enough to miss a label entirely if
    # only one x column is sampled.
    check("lr_denoise: find_label scans an x-band, not one fixed column",
          "x_offsets" in den and "for dx in x_offsets:" in den)


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
    test_corrupt_exports_are_caught()
    test_edit_panel_not_blindly_toggled()
    test_every_goto_step_is_handled()
    test_denoise_is_actually_rendered()
    test_stalled_render_is_nudged()
    test_dust_section_not_ambiguous()
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
