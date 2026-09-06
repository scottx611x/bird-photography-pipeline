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
              "syno_curate.py", "lr_denoise.py", "lr_dismiss.py"):
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

    if "--live" in sys.argv:
        print("live checks")
        test_live()

    print(f"\n{checks - len(failures)}/{checks} passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
        sys.exit(1)


if __name__ == "__main__":
    main()
