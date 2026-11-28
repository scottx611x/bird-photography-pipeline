#!/usr/bin/env python3
"""Helper for site_batch.sh: a -wildlife/-sadie export on its way to the
website's /photography page.

The app moves the Lightroom export to ~/Desktop/birbs/.site/<folder>/ with a
job.json. Claude (headless, on Scott's plan) looks at each photo and writes
names.json there; this turns that into the layout the website's
`make wildlife-publish DIR=…` takes:

    ~/Website/wildlife_incoming/<folder>-auto/<category>/<name>.jpg
    ~/Website/wildlife_incoming/<folder>-auto/_meta.json

Commands:
    previews <folder>   small copies Claude can read quickly (sips, 1400px)
    check <folder>      exit 0 if names.json covers every photo, else say what's missing
    stage <folder>      build the publish folder; prints its path
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

SITE = Path.home() / "Desktop/birbs/.site"
INBOX = Path.home() / "Website/wildlife_incoming"
# The website's tag vocabulary (website-30, 2026-11-27): the folder name is the first tag.
CATEGORIES = ("sadie", "mammal", "insect", "reptile", "flora", "fungi", "landscape", "macro")
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def job(folder):
    d = SITE / folder
    return d, json.loads((d / "job.json").read_text())


def cmd_previews(folder):
    d, j = job(folder)
    out = d / "preview"
    out.mkdir(exist_ok=True)
    for f in j["files"]:
        dst = out / f
        if not dst.exists():
            subprocess.run(["sips", "-Z", "1400", str(d / f), "--out", str(dst)],
                           check=True, capture_output=True)
    print(out)


def problems(folder):
    d, j = job(folder)
    try:
        names = json.loads((d / "names.json").read_text())
    except (OSError, ValueError) as e:
        return [f"names.json unreadable: {e}"]
    errs = []
    for f in j["files"]:
        n = names.get(f)
        if not isinstance(n, dict):
            errs.append(f"{f}: missing")
            continue
        if n.get("skip"):
            continue
        if n.get("category") not in CATEGORIES:
            errs.append(f"{f}: category must be one of {', '.join(CATEGORIES)}")
        if j["kind"] == "sadie" and n.get("category") != "sadie":
            errs.append(f"{f}: this is a -sadie album, category must be sadie")
        if not NAME_RE.match(n.get("name") or ""):
            errs.append(f"{f}: name must be lowercase-hyphenated, e.g. red-fox")
    return errs


def cmd_check(folder):
    errs = problems(folder)
    if errs:
        sys.exit("names.json isn't complete:\n  " + "\n  ".join(errs))
    print("ok")


def cmd_stage(folder):
    errs = problems(folder)
    if errs:
        sys.exit("names.json isn't complete:\n  " + "\n  ".join(errs))
    d, j = job(folder)
    names = json.loads((d / "names.json").read_text())
    out = INBOX / f"{folder}-auto"
    if out.exists():
        shutil.rmtree(out)
    meta, used = {}, set()
    for f in j["files"]:
        n = names[f]
        if n.get("skip"):
            continue
        base, k = n["name"], 1
        fn = f"{base}.jpg"
        while fn in used:
            k += 1
            fn = f"{base}-{k}.jpg"
        used.add(fn)
        (out / n["category"]).mkdir(parents=True, exist_ok=True)
        shutil.copy2(d / f, out / n["category"] / fn)   # keeps EXIF (date); stripped on upload
        m = {k2: n[k2] for k2 in ("species", "location") if (n.get(k2) or "").strip()}
        tags = [t for t in (n.get("tags") or []) if isinstance(t, str) and t.strip()]
        if tags:
            m["tags"] = tags
        if m:
            meta[fn] = m
    (out / "_meta.json").write_text(json.dumps(meta, indent=1))
    print(out)


if __name__ == "__main__":
    cmds = {"previews": cmd_previews, "check": cmd_check, "stage": cmd_stage}
    if len(sys.argv) != 3 or sys.argv[1] not in cmds or not re.fullmatch(r"[\w.+-]+", sys.argv[2]):
        sys.exit(__doc__)
    cmds[sys.argv[1]](sys.argv[2])
