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
          'if spread == "none"' in src and "Not exporting on its own" in src)
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
    i_gate   = tail.rfind("_export_gate(")    # the early one is the still-rendering bail-out
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
          src.count('body={"expect": _batch_size()}') == 5)   # + export's two AI-update fixes
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

def _load_funcs(path, names, env=None):
    """Exec just the named top-level functions/assignments from a module, so
    their real logic runs here without importing Flask or touching the Mac."""
    import ast
    tree = ast.parse(path.read_text())
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in names)
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) in names for t in n.targets))]
    ns = dict(env or {})
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns)
    return ns


def test_wrapped_filenames_are_collected():
    """The camera counter wrapped: an August batch still claimed DSC_0475 and
    DSC_0617, so on 2026-11-20 a fresh 48-photo export collected only 46."""
    src = (ROOT / "server.py").read_text()
    ns = _load_funcs(ROOT / "server.py", {"_claimed_elsewhere"})
    f = ns["_claimed_elsewhere"]
    start = 1_000_000.0
    check("claims: an old batch's claim doesn't block a file written by this export",
          f("2026-08-01-best", "2026-09-25-best", start + 60, start) is False)
    check("claims: an old batch keeps a file this export didn't write",
          f("2026-08-01-best", "2026-09-25-best", start - 60, start) is True)
    check("claims: unclaimed and self-claimed files are never blocked",
          f(None, "b", 0, start) is False and f("b", "b", 0, start) is False)
    exp = src[src.find("def _exported():"):src.find('set_step("export_wait")')]
    check("server: export collection asks _claimed_elsewhere",
          "_claimed_elsewhere(claimed.get(f), folder, mtime, export_start)" in exp)


def test_failed_export_trigger_is_not_waited_on():
    """On 2026-11-19 File > Export with Previous failed (-1719, no menu bar
    for a second after Select All), yet the run logged "Export running" and
    waited a full day for files that were never coming."""
    src = (ROOT / "server.py").read_text()
    body = src[src.find("def _pick_export_post("):src.find('set_step("export_wait")')]
    check("server: a failed export trigger goes back to the gate",
          'if not result.get("ok"):' in body and 'set_step("export_ready")' in body)

    calls, sleeps = [], []

    def flaky(n):
        def run(script):
            calls.append(script)
            if "click menu item" in script and len(
                    [c for c in calls if "click menu item" in c]) <= n:
                raise RuntimeError("execution error: Can't get menu bar 1 of "
                                   'process "Adobe Lightroom". Invalid index. (-1719)')
            return ""
        return run

    class T:
        @staticmethod
        def sleep(s):
            sleeps.append(s)

    ns = _load_funcs(ROOT / "lr_auto.py", {"lr_menu_click", "MENU_RETRY_WAITS"},
                     {"time": T})
    ns["run_applescript"] = flaky(2)
    ns["lr_menu_click"]("File", "Export with Previous...")
    clicks = [c for c in calls if "click menu item" in c]
    check("lr_auto: a missing menu bar is retried until the click lands",
          len(clicks) == 3 and len(sleeps) == 2, f"{len(clicks)} clicks, {sleeps}")

    calls.clear()
    ns["run_applescript"] = flaky(99)
    try:
        ns["lr_menu_click"]("File", "Export with Previous...")
        raised = False
    except RuntimeError:
        raised = True
    check("lr_auto: a menu bar that never comes back still raises",
          raised and len([c for c in calls if "click menu item" in c]) == 4)

    calls.clear()

    def other_error(script):
        calls.append(script)
        raise RuntimeError("execution error: some other failure (-1728)")
    ns["run_applescript"] = other_error
    try:
        ns["lr_menu_click"]("Edit", "Select All")
    except RuntimeError:
        pass
    check("lr_auto: other AppleScript errors are not retried", len(calls) == 1)


def test_sort_batch():
    """Claude Code sorts each export (bird_sort.py + the sort-batch skill)."""
    sys.path.insert(0, str(ROOT))
    import bird_sort as bs

    # Real detections from the 9/25 batch. The old "biggest confident box"
    # rule framed a clump of dead leaves (0525) and bird-plus-branch (0614).
    leaves = [(0.61, [0.6738, 0.4995, 0.0561, 0.1059]), (0.37, [0.8211, 0.7184, 0.1193, 0.172])]
    check("sort: the confident bird beats a bigger, weaker box",
          bs.subject(leaves) == leaves[0][1], str(bs.subject(leaves)))
    nested = [(0.51, [0.3117, 0.5238, 0.1844, 0.1062]), (0.51, [0.1744, 0.5218, 0.3324, 0.2257])]
    check("sort: the tighter of two equally confident nested boxes wins",
          bs.subject(sorted(nested, reverse=True)) == nested[0][1])
    pair = [(0.87, [0.541, 0.5191, 0.2266, 0.1421]), (0.86, [0.2762, 0.0901, 0.2018, 0.2098])]
    b = bs.subject(pair)
    check("sort: two confident birds are framed together",
          b[0] <= 0.2762 and b[0] + b[2] >= 0.541 + 0.2266 - 1e-3)

    W, H = 6000, 4000
    for name, bird, facing in [("warbler", [0.425, 0.435, 0.105, 0.105], "left"),
                               ("perched hawk", [0.4164, 0.4113, 0.2693, 0.307], None),
                               ("speck", [0.5, 0.5, 0.01, 0.01], "right"),
                               ("edge bird", [0.95, 0.9, 0.05, 0.08], "right")]:
        c = bs.crop_box(bird, W, H, facing)
        cw, ch = c["w"] * W, c["h"] * H
        inside = (c["x"] <= bird[0] + 1e-3 and c["y"] <= bird[1] + 1e-3
                  and c["x"] + c["w"] >= min(1, bird[0] + bird[2]) - 1e-3
                  and c["y"] + c["h"] >= min(1, bird[1] + bird[3]) - 1e-3)
        check(f"sort: {name} crop is 3:2, in frame, ≥1200px, contains the bird",
              abs(cw / ch - 1.5) < 0.02 and c["x"] >= 0 and c["y"] >= 0
              and c["x"] + c["w"] <= 1.0001 and c["y"] + c["h"] <= 1.0001
              and cw >= 1199 and inside, str(c))
    # Scott approved warblers at ~31-43% of the crop's height (looser than
    # 44% was "too tight") and the perched hawk at ~74%, not near-uncropped.
    wb = bs.crop_box([0.425, 0.435, 0.105, 0.105], W, H, "left")
    fill = 0.105 * H / (wb["h"] * H)
    check("sort: a small bird is framed loosely (~30-45% of the height)",
          0.30 <= fill <= 0.45, f"{fill:.2f}")
    hk = bs.crop_box([0.4164, 0.4113, 0.2693, 0.307], W, H)
    check("sort: a big bird's crop doesn't balloon toward the whole frame",
          hk["w"] <= 0.5, str(hk))
    r = bs.crop_box([0.45, 0.45, 0.1, 0.1], W, H, "right")
    lft = bs.crop_box([0.45, 0.45, 0.1, 0.1], W, H, "left")
    check("sort: facing leaves room ahead of the bird (right-facing crop sits further right)", r["x"] > lft["x"])

    src = (ROOT / "bird_sort.py").read_text()
    apply_ = src[src.find("def cmd_apply("):src.find("COMMANDS = ")]
    check("sort: apply refuses to overwrite Scott's own sorting",
          "touched and not ours and not force" in apply_)
    check("sort: apply crops from the original, never a crop of a crop",
          '"reset": True' in apply_ and 'api("/api/crop", {"file": f, "box": box, "folder": folder})' in apply_)
    check("sort: plan validation caps posts at Instagram's 10",
          "MAX_PER_POST = 10" in src and "> MAX_PER_POST" in src)

    srv = (ROOT / "server.py").read_text()
    tail = srv[srv.find("def _pick_export_post("):srv.find("def _export_clash(")]
    check("server: a fresh export is set aside and handed to the sorter",
          "_shelve(folder, None, new, dates, False" in tail and "_queue_sort(folder)" in tail
          and 'set_step("posting")' not in tail)
    check("lr_host: sort-batch is allowed and runs the launcher",
          '"sort-batch"' in (ROOT / "lr_host.py").read_text()
          and 'TOOLS / "sort_batch.sh"' in (ROOT / "lr_host.py").read_text())
    sh = (ROOT / "sort_batch.sh").read_text()
    check("sort_batch.sh: unattended run has narrow permissions",
          "dangerously" not in sh and "bypassPermissions" not in sh
          and 'Bash(~/.bird_sort_venv/bin/python bird_sort.py:*)' in sh
          and '"Bash"' not in sh)
    check("sort_batch.sh: one sort at a time, locked by the real pid",
          "sh -c 'echo $PPID'" in sh and "kill -0" in sh and "BASHPID\" >" not in sh)
    gate = srv[srv.find('set_step("denoise")'):srv.find("if not _denoise_and_export_tail():")]
    check("server: Denoise is ticked automatically before the manual gate",
          "if AUTO_DENOISE:" in gate and 'call_host("denoise-check"' in gate
          and gate.find('call_host("denoise-check"') < gate.find("while True:"))
    check("server: only a proven click skips the manual gate",
          'if r.get("ok"):' in gate and '"copy_pasting"' in gate
          and "_record_auto_denoise(" in gate)
    den = (ROOT / "lr_denoise.py").read_text()
    body = den[den.find("def run(kind"):den.find("def locate_denoise(")]
    check("lr_denoise: switches to Detail view before hunting the checkbox",
          "show_detail_view()" in body
          and body.find("show_detail_view()") < body.find("find_label("))
    import time as _t
    now = _t.mktime((2026, 11, 22, 10, 0, 0, 0, 0, -1))
    ok = bs.classify_run('{"type":"result","is_error":false,"result":"Sorted. Warning: '
                         'this sign-in ends between 2026-11-27 and 2026-11-30. Run claude '
                         'auth login before then"}', 0, now)
    check("sort: a good run is ok even with the sign-in expiry warning", ok[0] == "ok", str(ok))
    lim = bs.classify_run('{"is_error":true,"result":"You\'ve hit your limit · resets 3pm"}', 1, now)
    check("sort: a usage limit waits until it resets (3pm -> just after 3pm)",
          lim[0] == "wait" and 15 * 3600 <= (lim[1] - now + 10 * 3600) <= 15 * 3600 + 600, str(lim))
    vague = bs.classify_run("Claude usage limit reached", 1, now)
    check("sort: a limit with no reset time retries in ~half an hour",
          vague[0] == "wait" and 1500 <= vague[1] - now <= 2400, str(vague))
    auth = bs.classify_run('{"is_error":true,"result":"Invalid API key · Please run /login"}', 1, now)
    check("sort: a lapsed sign-in waits and retries instead of failing",
          auth[0] == "wait" and "auth login" in auth[2], str(auth))
    bad = bs.classify_run('{"is_error":true,"result":"Plan rejected: post 1 is empty"}', 1, now)
    check("sort: a real failure isn't retried forever", bad[0] == "fail", str(bad))
    check("sort_batch.sh: asks pending before every attempt and waits on wait",
          'bird_sort.py pending' in sh and 'sleep_until "$wake"' in sh
          and "GIVE_UP_AFTER" in sh and "--output-format json" in sh)
    check("sort_batch.sh: needs no long-lived token", "oauth_token" not in sh
          and "CLAUDE_CODE_OAUTH_TOKEN" not in sh)
    la = (ROOT / "lr_auto.py").read_text()
    ef = la[la.find("def _lr_export_folder("):la.find("def cmd_export(")]
    check("lr_auto: the export-folder check can't block on a macOS privacy prompt",
          "timeout=5" in ef and "TimeoutExpired" in ef)
    exp2 = la[la.find("def cmd_export("):la.find("def cmd_status(")]
    check("lr_auto: export cancels 'AI Update Required' by name, never Export",
          "_cancel_ai_update_dialog()" in exp2 and "AI_UPDATE_MARKER" in exp2
          and 'click button "Cancel" of window' in la
          and 'click button "Export" of window "AI Update' not in la)
    pe = srv[srv.find("def _pick_export_post("):srv.find('set_step("export_wait")')]
    check("server: an AI-update cancel re-renders, then goes one by one, retrying after each",
          '"AI UPDATE REQUIRED" not in result.get("output", "")' in pe
          and 'for fix in ("batch", "each"):' in pe
          and 'call_host("render-each"' in pe and pe.count('call_host("export"') == 2)
    # Wrapped filenames: a stale crop backup from an older photo of the same
    # name must never be restored over, or analysed in place of, a new export.
    import tempfile
    from PIL import Image as _Im
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        fresh = td / "fresh.jpg"
        ex = _Im.Exif(); ex[0x8769] = {36867: "2026:09:26 13:08:44"}
        _Im.new("RGB", (8, 8)).save(fresh, exif=ex)
        cropped = td / "cropped.jpg"
        _Im.new("RGB", (8, 8)).save(cropped)
        check("sort: a fresh export reads as uncropped (has a capture date)",
              bs._has_capture_date(fresh) and not bs._has_capture_date(cropped))
        old_b, old_o = bs.BIRDS_DIR, bs.ORIG_DIR
        try:
            bs.BIRDS_DIR, bs.ORIG_DIR = td, td / ".originals"
            bs.ORIG_DIR.mkdir()
            _Im.new("RGB", (8, 8)).save(bs.ORIG_DIR / "fresh.jpg")
            _Im.new("RGB", (8, 8)).save(bs.ORIG_DIR / "cropped.jpg")
            check("sort: a stale backup is ignored for a fresh export",
                  bs.source("fresh.jpg") == td / "fresh.jpg")
            check("sort: a real backup is used for a cropped file",
                  bs.source("cropped.jpg") == td / ".originals" / "cropped.jpg")
        finally:
            bs.BIRDS_DIR, bs.ORIG_DIR = old_b, old_o
    pe2 = srv[srv.find("new = _exported()"):srv.find("def get_next_open_scheduled_at(")]
    check("server: collecting an export sets aside same-name crop backups",
          "_retire_stale_backups(new)" in pe2)
    crop = srv[srv.find("def crop_image("):srv.find("@app.get(\"/api/log-stream\")")]
    check("server: /api/crop never restores an older photo's backup over a fresh export",
          "_has_capture_date(src)" in crop and crop.find("_has_capture_date(src)") < crop.find('if data.get("reset"):'))
    cs = srv[srv.find("def crop_status("):srv.find("def crop_image(")]
    check("server: crop-status doesn't report an older photo's backup",
          "_has_capture_date(src)" in cs and "_retire_stale_backups([name], orig)" in cs)
    skill = (ROOT / ".claude/skills/sort-batch/SKILL.md").read_text()
    check("skill: never posts and never asks",
          "never post" in skill and "Never stop to ask" in skill)


def test_crank_and_review():
    """Scott approves posts one by one, many batches later (2026-11-22), so a
    sorted batch goes on a shelf and the crank starts the next one. Shelving
    moves the photos out of birbs: with the camera counter wrapped, two open
    batches can both own a DSC_0947.jpg, and a shared name is how a June
    starling ended up in a September post."""
    import tempfile, threading, time as _time
    from datetime import datetime
    srv = ROOT / "server.py"
    src = srv.read_text()

    # ── which albums run next, and how they combine ──
    ns = _load_funcs(srv, {"_crank_groups", "_best_album_day", "CRANK_TARGET", "CRANK_SMALL",
                           "CRANK_CAP", "CRANK_HOLD_DAYS"}, {"re": re, "datetime": datetime})
    alb = lambda n, c: {"name": n, "item_count": c}
    albums = [alb("2026-09-26-best", 85), alb("2026-09-27-best", 24), alb("2026-09-27-wildlife", 9),
              alb("2026-09-28-best", 31), alb("2026-09-25-family", 33), alb("2026-09-29-best", 17),
              alb("2026-09-30-best", 16), alb("2026-10-03-best", 13), alb("2026-10-05-best", 7),
              alb("2026-10-06-best", 11), alb("2026-10-07-best", 29), alb("2026-10-12-best", 1),
              alb("2026-10-13-best", 14), alb("2026-10-14-best", 53), alb("2026-01-24-25-best-vids", 9),
              alb("2026-10-14-sadie", 5), alb("2026-mems-day-birds", 122), alb("2026-10-20-best", 0),
              alb("2026-07-09-best", 1)]
    left_out = []
    groups = ns["_crank_groups"](albums, {"2026-09-26-best", "2026-10-24-multi-9-19_thru_10-24"},
                                 today=datetime(2026, 11, 22), left_out=left_out)
    names = [[a["name"][5:10] for a in g] for g in groups]
    check("crank: -best only, oldest first, small days combined to ~25",
          names == [["09-27"], ["09-28"], ["09-29", "09-30"], ["10-03", "10-05", "10-06"],
                    ["10-07"], ["10-12", "10-13"], ["10-14"]], str(names))
    check("crank: never reaches back behind what already ran (a lone 7/9 padded 9/27)",
          left_out == ["2026-07-09-best"], str(left_out))
    recent = ns["_crank_groups"]([alb("2026-11-20-best", 6)], set(), today=datetime(2026, 11, 22))
    check("crank: a short newest batch waits a week for more albums", recent == [])
    old = ns["_crank_groups"]([alb("2026-11-01-best", 6)], set(), today=datetime(2026, 11, 22))
    check("crank: …and runs short once the week is up", len(old) == 1)

    # ── sort_batch.sh status lines → what they mean ──
    ns = _load_funcs(srv, {"SORT_STATES"})
    kind = lambda t: next((k for p, k in ns["SORT_STATES"] if t.startswith(p)), "running")
    check("sort status: lines map to states",
          [kind(t) for t in ("done — sorted 10:49", "waiting until Sun 09:38 — usage limit",
                             "gave up waiting (auth) — run ./sort_batch.sh", "idle — nothing to sort",
                             "failed — exit 1 (log: x)", "sorting (cwd /x)")]
          == ["done", "waiting", "failed", "idle", "failed", "running"])
    sh = (ROOT / "sort_batch.sh").read_text()
    check("sort_batch.sh reports each status to the app", "/api/sort-status" in sh
          and 'BIRD_SORT_TEST:-}" ] && return' in sh)

    # ── shelf round trip, with a same-name photo from another batch ──
    with tempfile.TemporaryDirectory() as td:
        birds = Path(td)
        excl, orig = birds / ".excluded", birds / ".originals"
        for d in (excl, orig, birds / ".thumbs", birds / ".previews"):
            d.mkdir()
        (birds / "DSC_0947.jpg").write_text("A-0947")
        (birds / "DSC_0001.jpg").write_text("A-0001 cropped")
        (orig / "DSC_0001.jpg").write_text("A-0001 original")
        (excl / "DSC_0002.jpg").write_text("A-0002")
        (birds / ".thumbs" / "DSC_0947.jpg").write_text("thumb")
        (birds / "other.jpg").write_text("not this batch's")
        arr = {"lanes": [["DSC_0947.jpg", "DSC_0001.jpg"]], "excluded": ["DSC_0002.jpg"], "meta": {}}
        st = {"active": "A", "proc_step": "posting", "arrangement": arr, "posting": False,
              "new_birds": ["DSC_0947.jpg", "DSC_0001.jpg"], "run_error": "",
              "sort": {"folder": "A", "state": "done"}, "photo_dates": {"DSC_0947.jpg": "9-26-26"},
              "batches": {"A": {"status": "processing"}}, "review": {}, "claimed": {},
              "stop_requested": False, "no_post": False}
        st.update(run=None, run_step=None, open_sorted=False)
        sorts = []
        env = {"state": st, "lock": threading.Lock(), "json": json, "datetime": datetime,
               "time": _time, "BIRDS_DIR": birds, "EXCLUDED_DIR": excl,
               "PREVIEW_DIR": birds / ".previews", "save_state": lambda: None,
               "log": lambda m: None, "_queue_sort": lambda f: sorts.append(f),
               "SORT_START_GRACE": 900}
        ns = _load_funcs(srv, {"REVIEW_DIR", "ORIGINALS_DIR", "SHELF_PARTS", "_review_summary",
                               "_shelf_files", "_drop_caches", "_write_shelf", "_shelve",
                               "shelve_active", "open_review", "_dirs"}, env)
        ok, _ = ns["shelve_active"]("sorted")
        shelf = birds / ".review" / "A"
        check("shelf: the batch's photos, exclusions and crop backups move out of birbs",
              ok and not (birds / "DSC_0947.jpg").exists() and not (orig / "DSC_0001.jpg").exists()
              and (shelf / "live" / "DSC_0947.jpg").read_text() == "A-0947"
              and (shelf / "excluded" / "DSC_0002.jpg").exists()
              and (shelf / "originals" / "DSC_0001.jpg").read_text() == "A-0001 original")
        check("shelf: other photos in birbs are left alone", (birds / "other.jpg").exists())
        check("shelf: the posting screen is freed and the batch waits for review",
              st["active"] is None and st["proc_step"] is None and st["arrangement"] is None
              and st["batches"]["A"]["status"] == "review" and st["review"]["A"]["sorted"] is True)
        check("shelf: cached thumbnails go with it", not (birds / ".thumbs" / "DSC_0947.jpg").exists())

        check("shelf: a shelved batch's photos are found in its own folder",
              ns["_dirs"]("A") == (shelf / "live", shelf / "excluded", shelf / "originals")
              and ns["_dirs"]() == (birds, excl, orig))

        # Batch B exports its own DSC_0947 while A is away, posts it, leaves it in birbs.
        (birds / "DSC_0947.jpg").write_text("B-0947")
        st["active"], st["proc_step"] = "B", "posting"
        ok, msg = ns["open_review"]("A")
        check("review: won't open while another batch is on the posting screen",
              not ok and "B" in msg and (shelf / "live" / "DSC_0947.jpg").exists())
        st["active"], st["proc_step"] = None, None
        st["run"], st["run_step"] = "C", "export_wait"
        env["_batch_raw_stems"] = lambda f: {"dsc_0947"}
        env["_export_stem"] = lambda n: re.sub(r"-\d+$", "", Path(n).stem.lower())
        ns["open_review"].__globals__.update(_batch_raw_stems=env["_batch_raw_stems"],
                                             _export_stem=env["_export_stem"], re=re, Path=Path)
        ok, msg = ns["open_review"]("A")
        check("review: waits only when a photo shares a name with the batch being exported",
              not ok and "share a name" in msg and "DSC_0947.jpg" in msg)
        ns["open_review"].__globals__["_batch_raw_stems"] = lambda f: {"dsc_9999"}
        st["run_step"] = "denoising"
        st["sort"] = {"folder": "A", "state": "running", "at": _time.time()}
        ok, msg = ns["open_review"]("A")
        check("review: not while Claude is part-way through sorting that batch",
              not ok and "sorting" in msg)
        st["sort"] = {"folder": "A", "state": "done", "at": _time.time()}
        ok, _ = ns["open_review"]("A")
        check("review: opens while Lightroom works on another batch",
              ok and st["run"] == "C" and st["run_step"] == "denoising")
        disp = list((birds / ".review" / ".displaced").glob("*/live/DSC_0947.jpg"))
        check("review: A gets its own DSC_0947 back, not B's",
              ok and (birds / "DSC_0947.jpg").read_text() == "A-0947")
        check("review: B's same-name photo is kept, out of the way",
              len(disp) == 1 and disp[0].read_text() == "B-0947")
        check("review: arrangement, crops and exclusions come back",
              st["active"] == "A" and st["proc_step"] == "posting" and st["arrangement"] == arr
              and (orig / "DSC_0001.jpg").read_text() == "A-0001 original"
              and (excl / "DSC_0002.jpg").exists() and st["photo_dates"]["DSC_0947.jpg"] == "9-26-26"
              and st["claimed"].get("DSC_0947.jpg") == "A" and not shelf.exists())
        check("review: a sorted batch isn't re-sorted", sorts == [])
        st["sort"] = {"folder": "A", "state": "waiting"}
        st["open_sorted"] = False
        ns["shelve_active"]("not sorted yet")
        ns["open_review"]("A")
        check("review: a batch shelved unsorted gets sorted when opened", sorts == ["A"])

        # The end of an export: the run's photos go straight to their own shelf.
        ns["shelve_active"]("later")
        (birds / "DSC_5000.jpg").write_text("D-5000")
        (birds / "DSC_0947.jpg").write_text("A's again, open")
        st["active"], st["proc_step"] = "OPEN", "posting"
        st["batches"]["D"] = {"status": "processing"}
        rec = ns["_shelve"]("D", None, ["DSC_5000.jpg"], {}, False, "exported")
        check("export: lands in its own folder, never on the posting screen",
              (birds / ".review" / "D" / "live" / "DSC_5000.jpg").read_text() == "D-5000"
              and not (birds / "DSC_5000.jpg").exists() and st["active"] == "OPEN"
              and (birds / "DSC_0947.jpg").read_text() == "A's again, open"
              and st["batches"]["D"]["status"] == "review" and rec["sorted"] is False)

    # ── the crank's decisions ──
    st = {"active": None, "proc_step": None, "run": None, "run_step": None, "run_hold": "",
          "thread_active": False, "posting": False, "run_error": "", "sort": {},
          "batches": {}, "review": {}, "done_albums": set(), "syno_skipped": set(),
          "multi_sources": {}, "sort_queue": [],
          "crank": {"on": True, "status": "", "batch": None}}
    calls = []
    env = {"state": st, "lock": threading.Lock(), "time": _time, "re": re, "datetime": datetime,
           "json": json, "log": lambda m: None, "save_state": lambda: None,
           "_sort_next": lambda: calls.append(("sort_next", list(st["sort_queue"]))),
           "check_host": lambda: True, "scan_batches": lambda: None,
           "shutil": type("S", (), {"disk_usage": staticmethod(lambda p: type("U", (), {"free": 90e9})())}),
           "DOWNLOADS": Path("/tmp"), "_syno_cache": {},
           "_crank_albums": lambda: [alb("2026-09-27-best", 24), alb("2026-09-28-best", 31)],
           "_fetch_album": lambda *a, **k: calls.append(("fetch", a[0])) or True,
           "process_batch": lambda f: calls.append(("process", f)),
           "_multi_name": lambda n: "multi",
           "threading": type("Th", (), {"Lock": threading.Lock, "Thread": _SyncThread})}
    ns = _load_funcs(srv, {"_crank_tick", "_crank_process", "_crank_status", "_crank_taken",
                           "_crank_groups", "_best_album_day", "_crank_left_out",
                           "CRANK_TARGET", "CRANK_SMALL", "CRANK_CAP", "SORT_TRIES",
                           "CRANK_HOLD_DAYS", "CRANK_MIN_FREE_GB", "SORT_START_GRACE",
                           "SORT_RUN_CAP", "CRANK_GATES"}, env)
    tick = ns["_crank_tick"]
    fetches = lambda: [c for c in calls if c[0] in ("fetch", "process")]

    def fetched(f):
        st["batches"][f] = {"status": "pending"}
    env["scan_batches"] = lambda: [fetched(c[1]) for c in calls if c[0] == "fetch"]
    ns["_crank_process"].__globals__["scan_batches"] = env["scan_batches"]
    tick()
    check("crank: fetches the oldest album and runs it",
          fetches() == [("fetch", "2026-09-27-best"), ("process", "2026-09-27-best")]
          and st["crank"]["batch"] == "2026-09-27-best", str(calls))

    # Its export landed and was set aside: Lightroom is free again at once.
    calls.clear()
    st["batches"]["2026-09-27-best"]["status"] = "review"
    st["review"]["2026-09-27-best"] = {"sorted": False, "shelved_at": "2026-11-22T20:00"}
    st["crank"]["phase"] = "processing"
    tick()
    check("crank: a batch set aside unsorted is queued for the sorter",
          ("sort_next", ["2026-09-27-best"]) in calls)
    check("crank: and Lightroom moves straight on, without waiting for the sort",
          ("fetch", "2026-09-28-best") in calls and st["crank"]["batch"] == "2026-09-28-best")

    calls.clear()
    st.update(active="2026-09-26-best", proc_step="posting",
              run="2026-09-28-best", run_step="denoising", thread_active=True)
    tick()
    check("crank: a batch open on the posting screen doesn't hold up Lightroom",
          fetches() == [] and st["crank"]["status"] == "2026-09-28-best: denoising")
    st["run_hold"] = "holding the export — 2026-09-26-best has the same names"
    tick()
    check("crank: says why an export is held", "holding the export" in st["crank"]["status"])
    st["run_hold"] = ""

    st.update(run_step="denoise")
    tick()
    check("crank: says when a gate needs a person", st["crank"]["status"].startswith("needs you"))
    st.update(thread_active=False, run_step="export_ready", run_error="Export didn't start")
    tick()
    check("crank: says when a run stopped", "stopped at 'export_ready'" in st["crank"]["status"])
    st.update(run="X-by-hand", run_step="toning", thread_active=True)
    tick()
    check("crank: leaves a batch run by hand alone", "by hand" in st["crank"]["status"])

    # ── the sort queue ──
    st2 = {"active": None, "proc_step": None, "review": {"A": {"sorted": False}, "B": {"sorted": True}},
           "sort": {}, "sort_queue": ["B", "A"]}
    started = []
    env2 = {"state": st2, "lock": threading.Lock(), "time": _time, "threading": threading,
            "save_state": lambda: None, "_start_sort": lambda f: started.append(f)}
    ns2 = _load_funcs(srv, {"_sort_busy", "_sortable", "_sort_next", "_sort_lock", "_queue_sort",
                            "SORT_START_GRACE", "SORT_RUN_CAP", "SORT_TRIES"}, env2)
    ns2["_sort_next"]()
    check("sort queue: skips a batch that's already sorted and starts the next",
          started == ["A"] and st2["sort_queue"] == ["A"])
    started.clear()
    st2["sort"] = {"folder": "A", "state": "running", "at": _time.time()}
    ns2["_queue_sort"]("C")
    check("sort queue: one sorter at a time — the rest wait their turn",
          started == [] and st2["sort_queue"] == ["A", "C"])

    # ── wiring ──
    tail = src[src.find("def _denoise_and_export_tail"):src.find("def _run_batch")]
    gate = src[src.find("def _export_gate"):src.find("def _denoise_and_export_tail")]
    check("auto-export: only after a clean paste and spread check",
          '_export_gate(auto=pasted and spread in ("all", "unknown"))' in tail
          and "if auto and AUTO_EXPORT:" in gate)
    check("auto-export: still rendering parks at the gate",
          "Don't export until its progress dialog is gone.\")\n        return _export_gate()" in tail)
    check("boot starts the crank thread", "threading.Thread(target=crank_loop, daemon=True).start()" in src)
    check("crank/shelf state is persisted",
          all(f'"{k}":' in src[src.find("def save_state"):src.find("def load_saved_statuses")]
              for k in ("review", "crank", "sort", "run", "run_step", "sort_queue")))
    check("boot: a save from before the split moves its run into the run slot",
          'if "run" not in saved and saved.get("proc_step") not in (None, "posting"):' in src)
    sv = src[src.find("def _sort_view("):src.find("def get_state(")]
    check("sorter: sees a shelved batch where it is, shaped like the posting step",
          '"subdir": subdir' in sv and 'f".review/{folder}/live"' in sv)
    bs = (ROOT / "bird_sort.py").read_text()
    check("sorter: told which batch, and every app call names it",
          'api("/api/state" + _q())' in bs and '"folder": folder' in bs and "_use_dirs(st)" in bs)
    sh2 = (ROOT / "sort_batch.sh").read_text()
    check("sort_batch.sh: takes --folder and reports it with each status",
          'export BIRD_SORT_FOLDER="$FOLDER"' in sh2 and '"folder=$FOLDER"' in sh2)
    check("lr_host: passes the batch to the sorter",
          '["--folder", folder]' in (ROOT / "lr_host.py").read_text())
    html = INDEX.read_text()
    check("ui: Review, Later and the crank toggle are wired",
          all(x in html for x in ("doReviewOpen(", "doShelve()", "renderCrank(d)", "/api/crank",
                                  "/api/review/open/", "/api/review/shelve", 'data-f="review"')))
    ns = _load_funcs(srv, {"_post_failure"})
    pf = ns["_post_failure"]
    m401 = pf("httpx.HTTPStatusError: Client error '401 Unauthorized' for url", 0, 4, "Buffer")
    check("post: a 401 says the login expired, what to do, and that nothing went out",
          "login has expired" in m401 and "publish.buffer.com" in m401 and "Nothing was posted." in m401)
    check("post: a partial failure says what went out and that a retry sends only the rest",
          "2 of 4 posts went out" in pf("Buffer rejected: x", 2, 4, "Buffer")
          and "only the 2 left" in pf("Buffer rejected: x", 2, 4, "Buffer"))
    post = src[src.find("def post_to_buffer("):src.find("@app.get(\"/api/next-post-date\")")]
    check("post: progress and the outcome are recorded for the button",
          '"state": "running"' in post and "_post_progress(done=i + 1, items=items)" in post
          and 'items[i]["state"] = "running"' in post and 'items[i]["state"] = "failed"' in post
          and 'state="failed"' in post and 'state="done"' in post)
    gs = src[src.find("def goto_step("):src.find("def force_advance(")]
    host = (ROOT / "lr_host.py").read_text()
    check("progress: Lightroom's toolbar paste counts as work, read off its own window",
          "def _toolbar_activity():" in host and "_toolbar_activity() or out" in host
          and '"-l", wid' in host and "if blue and min(blue) > int(60 * sx):" in host)
    la2 = (ROOT / "lr_auto.py").read_text()
    rend = la2[la2.find("def render_ai_settings("):la2.find("def render_ai_settings(") + 1500]
    check("render: never asks for a render while the toolbar paste is rendering",
          "if _toolbar_task():" in rend and "8766/progress" in la2)
    check("progress: a percentage that moves is progress, not a stall",
          'done = (prog.get("done"), prog.get("percent"))' in src
          and 'mark = (prog.get("done"), prog.get("percent"))' in src
          and 'prog.get("toolbar")' in src)
    check("restart: waits for the old run to finish before starting the new one",
          'if not state["thread_active"]:' in gs and "time.sleep(0.8)" not in gs)
    check("birdqueue: off unless BIRDQUEUE=1, and the Post screen hides it",
          'BIRDQUEUE_ON = os.environ.get("BIRDQUEUE", "0") == "1"' in src
          and 'use_bq = BIRDQUEUE_ON and' in src and "data.birdqueue_on ?" in html
          and "if (!lastState?.birdqueue_on) return 'buffer';" in html)
    check("post: a refreshed Buffer login is kept for the rest of the posts",
          'os.environ["BUFFER_COOKIES"] = cand' in post)
    check("ui: the Post button follows the server — locked while posting, Try again on failure",
          "function renderPostLive(d)" in html and 'id="post-live"' in html
          and "renderPostLive(d);" in html and "'↻ Try again'" in html
          and "if (btn?.disabled) return;" in html)
    # bird_post.py read line by line: each step reaches the Post button
    import sys as _sys
    seen = []
    ns = _load_funcs(srv, {"_post_day", "_post_stage", "_run_bird_post"},
                     {"datetime": datetime, "_post_progress": lambda **kw: seen.append(kw.get("step"))})
    fake = ("print('Resizing DSC_1.jpg …'); print('Resizing DSC_2.jpg …'); "
            "print(\"Uploading to Buffer's S3 …\"); print('  DSC_1.jpg ✓'); print('  DSC_2.jpg ✓'); "
            "print('Queuing to Buffer …'); print('  Scheduling for: 2026-11-30T20:41:00.000Z'); "
            "print('Done! Scheduled for: 2026-11-30T20:41:00.000Z')")
    items = [{"label": "Hawk", "photos": 2, "state": "running"}]
    r = ns["_run_bird_post"]([_sys.executable, "-c", fake], {}, 0, 1, items, 2)
    check("post: each step is reported as it happens — resize, upload, create, schedule",
          seen == ["resizing photo 1 of 2", "resizing photo 2 of 2", "uploading to Buffer — 0 of 2",
                   "uploading to Buffer — 1 of 2", "uploading to Buffer — 2 of 2",
                   "creating the post in Buffer", "scheduling it for Nov 30"]
          and r.returncode == 0 and "Done! Scheduled for:" in r.stdout and items[0]["frac"] > 0.9, str(seen))
    check("ui: a checklist and bar show each post — queued, sending, failed, waiting",
          "function postItemsHtml(ps)" in html and "post-seg" in html and "is-posting" in html)
    check("ui: Post and Resize wrap instead of running off the card on a phone",
          'class="post-btn-row"' in html and ".post-btn-row { display: flex; flex-wrap: wrap;" in html)
    check("ui: every running job names its batch — Lightroom, the sorter, Buffer",
          "function jobsHtml(d)" in html and "${jobsHtml(d)}" in html
          and "escHtml(d.run.folder)" in html and "escHtml(so.folder)" in html
          and "escHtml(ps.folder)" in html)
    check("ui: a failed post no longer silently resets the button",
          "btn.textContent = '▶ Post to Buffer'; }" not in html)
    check("ui: Lightroom's batch and the posting screen are drawn separately",
          "renderStepCard(data.run_step, b, data)" in html
          and "renderStepCard('posting', b, data)" in html
          and "renderOverlay(null);" in html and "renderOverlay(d.run_step)" not in html)


class _SyncThread:
    """threading.Thread stand-in that runs its target on start()."""
    def __init__(self, target=None, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)

    def is_alive(self):
        return False


def test_import_needs_no_keystrokes():
    """2026-11-22: the Add Photos panel ignored Cmd-Shift-G, the import's keys
    went nowhere useful, and the run said "Imported" anyway, then auto-toned
    whatever Lightroom was showing."""
    src = (ROOT / "lr_auto.py").read_text()
    imp = src[src.find("def cmd_import("):src.find("\ndef ", src.find("def cmd_import(") + 5)]
    check("import: hands the folder to Lightroom with open -a",
          '["open", "-a", "Adobe Lightroom", str(folder)]' in imp)
    check("import: presses Add through accessibility, not the keyboard",
          "_add_button(click=True)" in imp and not re.search(r'keystroke "|key code \d', imp))
    check("import: only says Imported once the review has closed",
          imp.find('print(f"Imported: {folder}")') > imp.find("_wait(lambda: not _add_button()"))
    check("import: fails loudly (exit 1) when the review never appears",
          "never showed" in imp and "sys.exit(1)" in imp)


def test_denoise_survives_a_slow_first_photo():
    """2026-11-22: the window had drifted half off screen, so the Denoise box
    was never found; then the first photo after an import took over 12s to
    show "Applying Denoise", and the click that worked was undone."""
    den = (ROOT / "lr_denoise.py").read_text()
    m = re.search(r"DENOISE_WINDOW_WAIT = (\d+)", den)
    check("denoise: waits long enough for a fresh import's first render",
          bool(m) and int(m.group(1)) >= 30)
    act = den[den.find("def activate():"):den.find("def grab(")]
    check("denoise: puts Lightroom's window back to full screen before reading it",
          "place_window()" in act and 'set position of window' in act)
    und = den[den.find("tick and show a progress window"):den.find("sys.exit(4)", den.find("tick and show a progress window"))]
    check("denoise: a late progress window stops the undo click",
          und.find('wait_window("applying denoise"') < und.find("click(box)"))
    check("denoise: finds the progress dialog by its text, not only its window name",
          "def lr_dialog_text" in den and "every static text of w" in den
          and 'wait_window("denoise"' not in den)
    srv = (ROOT / "server.py").read_text()
    step = srv[srv.find("if AUTO_DENOISE:"):srv.find("_record_auto_denoise(folder_name")]
    check("denoise: auto-Denoise retries once Lightroom has loaded the import",
          "for attempt in range(AUTO_DENOISE_TRIES)" in step and "time.sleep(60)" in step)
    tick = srv[srv.find("def _crank_tick"):srv.find("def _crank_process")]
    check("crank: no 'needs you' while auto-Denoise is still trying",
          'trying = state.get("auto_denoising")' in tick)


def test_export_runs_unattended():
    """2026-11-22, first crank batch: the export refused to start ("selection
    is None photos") because its count came from a menu item that loses its
    number once every render is done; and reading Lightroom's export folder
    raised macOS's "access data from other apps" prompt yet again."""
    lra = (ROOT / "lr_auto.py").read_text()
    exp = lra[lra.find("def cmd_export("):lra.find("def _cancel_ai_update_dialog")]
    check("export: counts the selection even after every render is done",
          '"Create Album with"' in exp and "if got is None:" in exp)
    check("export: doesn't read Lightroom's container unless asked",
          'os.environ.get("LR_CHECK_EXPORT_FOLDER") == "1"' in exp)
    src = (ROOT / "server.py").read_text()
    wait = src[src.find('set_step("export_wait")'):src.find("new = _exported()\n")]
    check("export: an export that lands nothing gives up and says so",
          "EXPORT_NOTHING_AFTER" in wait and 'set_step("export_ready")' in wait
          and "quiet_since = time.time()" in wait)


def test_keys_only_reach_lightroom():
    """2026-11-22: Lightroom's activate timed out and a filename meant for its
    search box was typed into Scott's terminal. Keystrokes go to whatever app
    is in front, whatever process the script names."""
    lra = ROOT / "lr_auto.py"
    ran = []

    class R:
        def __init__(self, out=""):
            self.returncode, self.stdout, self.stderr = 0, out, ""

    idle = [999.0]                  # seconds since Scott last touched the Mac
    clock = [0.0]

    def fake_run(args, **kw):
        if args[0] == "ioreg":
            return R(f'"HIDIdleTime" = {int(idle[0] * 1e9)}')
        script = args[-1]
        ran.append(script)
        if "frontmost is true" in script and "keystroke" not in script:
            return R(front[0])
        if "activate" in script:
            ran.append(f"activate@idle={idle[0]}")
        return R()

    def fake_sleep(sec):
        clock[0] += sec
        idle[0] += sec

    front = ["Adobe Lightroom"]
    fake_sp = type("SP", (), {"run": staticmethod(fake_run), "TimeoutExpired": TimeoutError})
    fake_time = type("T", (), {"sleep": staticmethod(fake_sleep),
                               "time": staticmethod(lambda: clock[0])})
    ns = _load_funcs(lra, {"KEYS_RE", "FRONT_GUARD", "lr_frontmost", "ensure_lr_front",
                           "run_applescript", "user_idle_seconds", "wait_for_user_idle",
                           "USER_IDLE_BEFORE_FOCUS", "USER_IDLE_WAIT_CAP"},
                     {"re": re, "subprocess": fake_sp, "time": fake_time, "OSError": OSError})
    ns["run_applescript"]('tell application "System Events" to keystroke "g"')
    check("keys: a keystroke script carries the in-script frontmost check",
          ran[-1].startswith(ns["FRONT_GUARD"]))
    ran.clear()
    ns["run_applescript"]('tell application "System Events" to click menu item "X"')
    check("keys: menu clicks aren't held up by the check", len(ran) == 1)
    ran.clear()
    front[0] = "iTerm2"
    try:
        ns["run_applescript"]('tell application "System Events" to keystroke "v" using command down')
        raised = False
    except RuntimeError as e:
        raised = "iTerm2" in str(e)
    check("keys: nothing is typed while another app is in front",
          raised and not any("keystroke" in r for r in ran))
    ran.clear()
    idle[0] = 3.0                   # typing in Chrome a moment ago
    try:
        ns["run_applescript"]('tell application "System Events" to keystroke "a"')
    except RuntimeError:
        pass
    acts = [r for r in ran if r.startswith("activate@")]
    check("keys: Lightroom isn't pulled forward while someone is using the Mac",
          acts and float(acts[0].split("=")[1]) >= ns["USER_IDLE_BEFORE_FOCUS"], str(acts))
    src = lra.read_text()
    exp = src[src.find("def cmd_export("):src.find("AI_UPDATE_MARKER =")]
    check("keys: an export is menu clicks only — it never takes focus",
          "keystroke" not in exp and "key code" not in exp and "to activate" not in exp)
    check("keys: grid view comes from the View menu, not the G key",
          'keystroke "g"' not in src and 'lr_menu_click("View", "Photo Grid")' in src)
    srv = (ROOT / "server.py").read_text()
    tail = srv[srv.find("def _pick_export_post("):]
    cont = srv[srv.find("def continue_step("):srv.find("def done_picking(")]
    check("crank: Continue wakes the crank, so 'needs you' clears with the click",
          'state["crank"]["kick"] = True' in cont)
    check("export: an unrendered batch is re-rendered by menu before going one by one",
          0 < tail.find('"ai-resume"') < tail.find('"render-each"'))
    den = (ROOT / "lr_denoise.py").read_text()
    check("keys: lr_denoise checks before a screen click and before arrow keys",
          "if not lr_in_front():" in den[den.find("def click("):den.find("def lr_window_names")]
          and "if not lr_in_front():" in den[den.find("def arrow(code):"):])
    dis = (ROOT / "lr_dismiss.py").read_text()
    check("keys: lr_dismiss checks before Escape and its clicks",
          dis.find("first process whose frontmost is true") < dis.find("key code 53"))


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
    for key in ("batches", "proc_step", "photo_versions", "buffer_ready",
                "run", "run_step", "sort_queue"):
        check(f"live: state exposes {key}", key in state)
    shelved = sorted((state.get("review") or {}))
    if shelved:
        v = httpx.get(f"{BASE}/api/state", params={"folder": shelved[0]}, timeout=20).json()
        check("live: the sorter sees a shelved batch where it waits",
              v.get("proc_step") == "posting" and v.get("subdir") == f".review/{shelved[0]}/live"
              and bool(v.get("new_birds")), str(v)[:200])

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


def test_post_never_opens_buffer():
    """Posting must not open Buffer in Scott's Chrome (2026-11-23): images go to
    our own bucket, and the login refresh only re-reads cookies."""
    post = (ROOT / "bird_post.py").read_text()
    host = (ROOT / "lr_host.py").read_text()
    main = post[post.index("def main():"):]
    check("post: uploads go through upload_image (own bucket first)",
          "upload_image(ready)" in main and "upload_to_buffer_s3(ready" not in main)
    check("post: own-bucket upload targets the public images prefix",
          'IMAGE_PREFIX = "birds/images/buffer/"' in post)
    check("post: no Chrome login needed unless the own-bucket upload fails",
          "get_buffer_cookies()" not in main)
    check("lr_host: buffer-refresh opens no browser",
          "publish.buffer.com" not in host and "open -g -a 'Google Chrome'" not in host)


def test_lr_click_stays_in_lightroom():
    """2026-11-23: driving the sidebar (not in the a11y tree) back to an older
    batch. The click must be guarded like keystrokes: idle wait, Lightroom in
    front, and a look at LR's window must never raise it or capture other apps."""
    auto = (ROOT / "lr_auto.py").read_text()
    host = (ROOT / "lr_host.py").read_text()
    body = auto[auto.index("def cmd_click_at"):auto.index("def main():")]
    check("click-at: waits for idle with Lightroom in front", "ensure_lr_front()" in body)
    check("click-at: re-checks Lightroom is frontmost right before clicking",
          body.index('lr_frontmost() != "Adobe Lightroom"') < body.index("CLICLICK"))
    check("click-at: only fractions inside the window", "0 < args.fx < 1" in body)
    check("lr_host: lr-click is an allowed command", '"lr-click"' in host)
    i = host.index("lr_only =")
    scr = host[i:host.index("from PIL import Image", i)]
    check("lr_host: ?window=lr captures by window id without activating",
          '"-l", wid' in scr and "if app and not lr_only" in scr)


def test_render_each_counts_only_seen_renders():
    """2026-11-23: render-each said "Rendered 27" when nothing rendered — it
    counted any photo that took over 10s, and the wait for a dialog is 10s."""
    auto = (ROOT / "lr_auto.py").read_text()
    body = auto[auto.index("def cmd_render_each"):auto.index("def cmd_click_at")]
    check("render-each: counts a photo only if its render dialog appeared",
          "seen = bool(_render_dialog_count())" in body and "elif seen:" in body
          and "time.time() - t0 > 10" not in body)
    check("render-each: says so when nothing rendered", "rendered == 0" in body)


def test_arrangement_keeps_new_birds_in_step():
    """2026-11-24: photos swapped into the arrangement under new names were
    missing from new_birds, so the page rebuilt posts from the old, moved-away
    files (all broken images). Saving an arrangement now syncs the list."""
    import tempfile
    from pathlib import Path as P
    srv = ROOT / "server.py"
    with tempfile.TemporaryDirectory() as d:
        d = P(d)
        for n in ("new.jpg", "kept.jpg", "x.jpg"):
            (d / n).write_bytes(b"")
        st = {"new_birds": ["old.jpg", "kept.jpg"]}
        ns = _load_funcs(srv, {"_sync_new_birds"},
                         env={"state": st, "bird_file": lambda n: d / n})
        ns["_sync_new_birds"]({"lanes": [["new.jpg", "kept.jpg"]], "excluded": ["x.jpg", "gone.jpg"]})
        check("arrangement: new names join new_birds, missing ones leave",
              st["new_birds"] == ["kept.jpg", "new.jpg", "x.jpg"], str(st["new_birds"]))
    check("arrangement: save syncs new_birds for the open batch",
          "_sync_new_birds(arrangement)" in srv.read_text())


def test_location_typos_not_suggested():
    """2026-11-24: "Weir Hil" kept being offered beside "Weir Hill" and spread."""
    ns = _load_funcs(ROOT / "server.py", {"_drop_truncated"})
    got = ns["_drop_truncated"](["Weir Hill", "Rea St.", "Weir Hil", "Rea St", "Mill Pond"])
    check("locations: a truncated duplicate is dropped", "Weir Hil" not in got, str(got))
    check("locations: distinct places stay", got[:2] == ["Weir Hill", "Rea St."] and "Mill Pond" in got,
          str(got))


def test_grain_guard():
    """2026-11-22: a batch exported un-denoised after the checkbox read-back saw
    Denoise on 0 of 4 photos and the run "continued anyway". The export's own
    pixels are now measured, and Post refuses grainy photos."""
    import tempfile
    from pathlib import Path as P
    from PIL import Image, ImageChops, ImageFilter
    srv = ROOT / "server.py"
    ns = _load_funcs(srv, {"_grain", "_grain_of", "_grainy", "_grain_note", "GRAINY_AT",
                           "_grain_cache", "_grain_target", "_has_capture_date"},
                     env={"Path": P})
    with tempfile.TemporaryDirectory() as d:
        d = P(d)
        smooth = Image.linear_gradient("L").resize((1200, 800)).filter(ImageFilter.GaussianBlur(20))
        noise = Image.effect_noise((1200, 800), 18)
        grainy = ImageChops.add(smooth, noise, 1, -128)
        smooth.convert("RGB").save(d / "clean.jpg", quality=92)
        grainy.convert("RGB").save(d / "noisy.jpg", quality=92)
        gc, gn = ns["_grain"](d / "clean.jpg"), ns["_grain"](d / "noisy.jpg")
        check("grain: a smooth (denoised-looking) frame reads under 1", gc < 1, str(gc))
        check("grain: a noisy frame reads over the threshold", gn >= ns["GRAINY_AT"], str(gn))
        bad = ns["_grainy"](["clean.jpg", "noisy.jpg", "missing.jpg"], d)
        check("grain: only the noisy frame is flagged", list(bad) == ["noisy.jpg"], str(bad))
        check("grain: the note names the photo", "noisy.jpg" in ns["_grain_note"](bad, 2))
        # 2026-11-24: a tight crop of a denoised frame reads grainy at 1:1. An app
        # crop (no capture date) is judged by its uncropped original instead.
        (d / ".originals").mkdir()
        grainy.convert("RGB").save(d / "crop_ok.jpg", quality=92)
        smooth.convert("RGB").save(d / ".originals" / "crop_ok.jpg", quality=92)
        grainy.convert("RGB").save(d / "crop_bad.jpg", quality=92)
        grainy.convert("RGB").save(d / ".originals" / "crop_bad.jpg", quality=92)
        bad = ns["_grainy"](["crop_ok.jpg", "crop_bad.jpg"], d)
        check("grain: a crop is judged by its saved original", list(bad) == ["crop_bad.jpg"],
              str(bad))
    real = P.home() / "Desktop/birbs/.originals/DSC_4449.jpg"
    if real.exists():
        g = ns["_grain"](real)
        check("grain: the real un-denoised hawk (DSC_4449) is flagged", g >= ns["GRAINY_AT"], str(g))
    src = srv.read_text()
    check("grain: Post refuses grainy photos unless allow_grainy",
          'if not data.get("allow_grainy"):' in src and '"grainy": bad' in src)
    check("grain: the export flags grainy photos on the shelf", 'rec["grainy"] = bad' in src)
    check("grain: no more 'Continuing anyway' after a failed Denoise read-back",
          "Continuing anyway" not in src)
    html = INDEX.read_text()
    check("grain: UI badges grainy photos and offers Post anyway",
          "function grainyBadge" in html and "doPost(true)" in html and "allow_grainy" in html)


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
    test_wrapped_filenames_are_collected()
    test_failed_export_trigger_is_not_waited_on()
    test_sort_batch()
    test_crank_and_review()
    test_keys_only_reach_lightroom()
    test_import_needs_no_keystrokes()
    test_denoise_survives_a_slow_first_photo()
    test_export_runs_unattended()
    test_dust_section_not_ambiguous()
    test_lr_verify_logic()
    test_post_never_opens_buffer()
    test_grain_guard()
    test_lr_click_stays_in_lightroom()
    test_render_each_counts_only_seen_renders()
    test_arrangement_keeps_new_birds_in_step()
    test_location_typos_not_suggested()
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
