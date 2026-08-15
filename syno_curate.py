#!/usr/bin/env python3
"""
syno_curate.py — Mac-side Synology helpers for the album curator (/curate).

Used by lr_host.py. Keeps a persistent DSM session, an on-disk thumbnail/EXIF
cache (~/.bird_curate_cache), and does the album create/delete calls for sync.
Runs Mac-side because s-cubed-nas.local only resolves over mDNS.
"""

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

import syno_fetch as sf

CACHE = Path.home() / ".bird_curate_cache"
for sub in ("m", "xl", "exif"):
    (CACHE / sub).mkdir(parents=True, exist_ok=True)

RAW_EXTS = sf.RAW_EXTS
KINDS = ("best", "wildlife", "sadie", "family")

_lock = threading.RLock()
_sess = {"client": None, "sid": None}
_days_cache = {"at": 0.0, "since": None, "data": None}


def _session():
    with _lock:
        if _sess["client"] is None:
            c = httpx.Client()
            _sess["sid"] = sf.login(c)
            _sess["client"] = c
        return _sess["client"], _sess["sid"]


def _drop_session():
    with _lock:
        _sess["client"] = None


def _call(params: dict, timeout: float = 60) -> dict:
    last = None
    for _ in (1, 2):  # one retry with a fresh login — DSM sessions go stale
        try:
            client, sid = _session()
            r = client.get(sf.ENTRY, params=dict(params, _sid=sid), timeout=timeout)
            r.raise_for_status()
            data = r.json()
            if not data.get("success"):
                raise sf.SynoError(f"{params.get('api')} failed: {data.get('error')}")
            return data.get("data", {})
        except Exception as e:
            last = e
            _drop_session()
    raise last


def days(since: str) -> list:
    """Shooting days (RAW files only) since `since`, with current album membership."""
    with _lock:
        if _days_cache["data"] and _days_cache["since"] == since \
           and time.time() - _days_cache["at"] < 300:
            return _days_cache["data"]

    since_ts = datetime.strptime(since, "%Y-%m-%d").timestamp()
    by_day, offset = {}, 0
    while True:
        data = _call({"api": "SYNO.Foto.Browse.Item", "version": "1", "method": "list",
                      "offset": offset, "limit": 1000,
                      "sort_by": "takentime", "sort_direction": "desc"}, timeout=90)
        page = data.get("list", [])
        for it in page:
            t = it.get("time", 0)
            if t and t < since_ts:
                page = None
                break
            if Path(it.get("filename", "")).suffix.lower() in RAW_EXTS:
                d = datetime.fromtimestamp(t).strftime("%Y-%m-%d")
                by_day.setdefault(d, []).append(
                    {"id": it["id"], "fn": it["filename"].rsplit(".", 1)[0], "t": t})
        if page is None or len(data.get("list", [])) < 1000:
            break
        offset += 1000

    # current album contents for those days (source of truth for initial selection)
    client, sid = _session()
    albums = sf.list_albums(client, sid)
    wanted = {}
    pat = re.compile(r"^(\d{4}-\d{2}-\d{2})-(best|wildlife|sadie|family)$")
    for a in albums:
        m = pat.match(a["name"])
        if m and m.group(1) in by_day:
            try:
                ids = [i["id"] for i in sf.list_items(client, sid, a)]
            except Exception:
                ids = []
            wanted.setdefault(m.group(1), {})[m.group(2)] = ids

    out = []
    for d in sorted(by_day):
        items = sorted(by_day[d], key=lambda i: i["t"])
        out.append({"day": d,
                    "items": [{"id": i["id"], "fn": i["fn"]} for i in items],
                    "albums": wanted.get(d, {})})
    with _lock:
        _days_cache.update({"at": time.time(), "since": since, "data": out})
    return out


def thumb(iid: int, size: str) -> bytes:
    size = "xl" if size == "xl" else "m"
    p = CACHE / size / f"{iid}.jpg"
    if p.exists():
        return p.read_bytes()
    d = _call({"api": "SYNO.Foto.Browse.Item", "version": "1", "method": "get",
               "id": json.dumps([iid]), "additional": json.dumps(["thumbnail"])})
    ck = d["list"][0]["additional"]["thumbnail"].get("cache_key")
    client, sid = _session()
    r = client.get(sf.ENTRY, params={"api": "SYNO.Foto.Thumbnail", "version": "2",
                                     "method": "get", "id": iid, "cache_key": ck,
                                     "type": "unit", "size": size, "_sid": sid}, timeout=60)
    if not r.headers.get("content-type", "").startswith("image"):
        return b""
    p.write_bytes(r.content)
    return r.content


def exif(iid: int) -> dict:
    p = CACHE / "exif" / f"{iid}.json"
    if p.exists():
        return json.loads(p.read_text())
    d = _call({"api": "SYNO.Foto.Browse.Item", "version": "1", "method": "get",
               "id": json.dumps([iid]), "additional": json.dumps(["exif", "resolution"])})
    rec = d["list"][0]
    a = rec.get("additional", {})
    ex, res = a.get("exif") or {}, a.get("resolution") or {}
    t = rec.get("time")
    out = {"shutter": ex.get("exposure_time", ""), "aperture": ex.get("aperture", ""),
           "iso": ex.get("iso", ""), "focal": ex.get("focal_length", ""),
           "camera": ex.get("camera", ""), "lens": ex.get("lens", ""),
           "res": f"{res.get('width', '?')}×{res.get('height', '?')}",
           "time": datetime.fromtimestamp(t).strftime("%H:%M:%S") if t else ""}
    p.write_text(json.dumps(out))
    return out


def sync(albums_map: dict) -> list:
    """Replace album contents: {album_name: [item ids]}. Empty list deletes."""
    log = []
    client, sid = _session()
    existing = {a["name"]: a for a in sf.list_albums(client, sid)}
    for name, ids in sorted(albums_map.items()):
        try:
            old = existing.get(name)
            if old:
                r = client.get(sf.ENTRY, params={"api": "SYNO.Foto.Browse.Album",
                                                 "version": "1", "method": "delete",
                                                 "id": json.dumps([old["id"]]),
                                                 "_sid": sid}, timeout=30)
                if not r.json().get("success"):
                    log.append(f"{name}: FAILED to delete old ({r.json().get('error')})")
                    continue
            if not ids:
                log.append(f"{name}: removed" if old else f"{name}: nothing to do")
                continue
            d = _call({"api": "SYNO.Foto.Browse.NormalAlbum", "version": "3",
                       "method": "create", "name": json.dumps(name),
                       "item": json.dumps(ids)})
            log.append(f"{name}: {d.get('album', {}).get('item_count')} photos")
        except Exception as e:
            log.append(f"{name}: ERROR {e}")
    with _lock:
        _days_cache["data"] = None   # membership changed — refresh next load
    return log
