#!/usr/bin/env python3
"""birdqueue — a self-hosted replacement for Buffer's Instagram queue.

Buffer was doing five jobs for this pipeline: hosting images, holding a dated
queue, telling us the next open day, remembering past captions for autocomplete,
and giving a UI to reorder and reschedule. This does all five, on hardware we
own, with no session cookies to go stale.

Runs on birdpi rather than the Mac because a post scheduled for Thursday has to
fire with nobody logged in.

Media hosting is pluggable because Meta insists on fetching images by URL:
    MEDIA_BACKEND=funnel   serve them from this box over Tailscale Funnel
    MEDIA_BACKEND=s3       presigned links from a private bucket
    MEDIA_BACKEND=none     queue works, publishing is refused (default)

The publisher only ever appends to Instagram. Nothing here edits or deletes a
published post.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from flask import Flask, g, jsonify, request, send_from_directory

ROOT = Path(os.environ.get("BIRDQUEUE_ROOT", "/srv/birdqueue"))
DB_PATH = ROOT / "queue.db"
MEDIA_ROOT = ROOT / "media"
PUBLIC_ROOT = ROOT / "public"          # only populated during a publish
HISTORY = ROOT / "species_history.json"

API_PORT = int(os.environ.get("BIRDQUEUE_PORT", "8910"))
MEDIA_BACKEND = os.environ.get("MEDIA_BACKEND", "none").lower()
FUNNEL_PORT = int(os.environ.get("FUNNEL_PORT", "8443"))
MEDIA_PORT = int(os.environ.get("MEDIA_PORT", "8911"))
PUBLIC_HOST = os.environ.get("BIRDQUEUE_PUBLIC_HOST", "")

IG_USER_ID = os.environ.get("IG_USER_ID", "")
IG_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
S3_BUCKET = os.environ.get("IG_S3_BUCKET", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

IG_API = "https://graph.instagram.com"
IG_VERSION = "v25.0"
MAX_CAROUSEL = 10
CONTAINER_TIMEOUT = 180
FUNNEL_READY_TIMEOUT = int(os.environ.get("FUNNEL_READY_TIMEOUT", "180"))
FUNNEL_READY_STREAK = int(os.environ.get("FUNNEL_READY_STREAK", "5"))
TICK_SECONDS = int(os.environ.get("BIRDQUEUE_TICK", "60"))
MAX_ATTEMPTS = 3

# Posting time of day for auto-assigned slots, local time.
DEFAULT_HOUR = int(os.environ.get("BIRDQUEUE_HOUR", "9"))

app = Flask(__name__)


# ── storage ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id           TEXT PRIMARY KEY,
    caption      TEXT NOT NULL,
    due_at       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    published_at TEXT,
    media_id     TEXT,
    permalink    TEXT,
    error        TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS media (
    id       TEXT PRIMARY KEY,
    post_id  TEXT NOT NULL,
    idx      INTEGER NOT NULL,
    filename TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS media_post ON media(post_id, idx);
CREATE INDEX IF NOT EXISTS posts_due ON posts(status, due_at);
"""


def db() -> sqlite3.Connection:
    conn = getattr(g, "_db", None)
    if conn is None:
        conn = g._db = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    return conn


@app.teardown_appcontext
def _close(_exc):
    conn = getattr(g, "_db", None)
    if conn is not None:
        conn.close()


def fresh_conn() -> sqlite3.Connection:
    """A connection for the publisher thread, which has no Flask context."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_storage() -> None:
    for d in (ROOT, MEDIA_ROOT, PUBLIC_ROOT):
        d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def row_to_post(conn: sqlite3.Connection, r: sqlite3.Row) -> dict:
    imgs = conn.execute(
        "SELECT filename FROM media WHERE post_id=? ORDER BY idx", (r["id"],)
    ).fetchall()
    return {
        "id": r["id"],
        "caption": r["caption"],
        "due_at": r["due_at"],
        "status": r["status"],
        "images": [m["filename"] for m in imgs],
        "image_count": len(imgs),
        "created_at": r["created_at"],
        "published_at": r["published_at"],
        "media_id": r["media_id"],
        "permalink": r["permalink"],
        "error": r["error"],
        "attempts": r["attempts"],
    }


# ── scheduling helpers ────────────────────────────────────────────────────────

def next_open_slot(conn: sqlite3.Connection) -> str:
    """Day after the last queued post, matching the old Buffer behaviour.

    Buffer's queue was one post per day; this keeps that, so the UI's notion of
    'the next open day' does not change out from under anyone.
    """
    row = conn.execute(
        "SELECT due_at FROM posts WHERE status IN ('queued','paused') "
        "ORDER BY due_at DESC LIMIT 1"
    ).fetchone()
    base = datetime.now().astimezone()
    if row:
        try:
            last = datetime.fromisoformat(row["due_at"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            last = last.astimezone()
            if last.date() >= base.date():
                base = last + timedelta(days=1)
            else:
                base = base + timedelta(days=1)
        except ValueError:
            base = base + timedelta(days=1)
    else:
        base = base + timedelta(days=1)
    slot = base.replace(hour=DEFAULT_HOUR, minute=0, second=0, microsecond=0)
    return slot.astimezone(timezone.utc).isoformat(timespec="seconds")


# ── media hosting ─────────────────────────────────────────────────────────────

class PublishError(RuntimeError):
    """Something went wrong publishing this particular post."""


class ConfigError(PublishError):
    """The server is not set up to publish at all.

    Kept distinct because it says nothing about the post: counting it as a
    failed attempt would let a few minutes of bad config permanently exhaust
    the retry budget of every post in the queue.
    """


def _host_via_s3(paths: list[Path]) -> tuple[list[str], callable]:
    import boto3

    if not S3_BUCKET:
        raise ConfigError("IG_S3_BUCKET is not set")
    s3 = boto3.client("s3", region_name=AWS_REGION)
    keys, urls = [], []
    for p in paths:
        key = f"ig-upload/{uuid.uuid4().hex}/{p.name}"
        s3.upload_file(str(p), S3_BUCKET, key, ExtraArgs={"ContentType": "image/jpeg"})
        keys.append(key)
        urls.append(
            s3.generate_presigned_url(
                "get_object", Params={"Bucket": S3_BUCKET, "Key": key}, ExpiresIn=3600
            )
        )

    def cleanup():
        for k in keys:
            try:
                s3.delete_object(Bucket=S3_BUCKET, Key=k)
            except Exception:
                pass

    return urls, cleanup


def _host_via_funnel(paths: list[Path]) -> tuple[list[str], callable]:
    """Expose the images publicly for the length of one publish, then stop.

    A random directory name is the only thing standing between the files and the
    internet, so the window is kept as short as possible and the directory is
    removed in a finally-block by the caller.

    The tailscale CLI is called without sudo on purpose: this service runs with
    NoNewPrivileges, which makes setuid binaries unusable, so scott is an
    operator on tailscaled instead (`tailscale set --operator=scott`). The
    hardening stays on and sudo is never needed.
    """
    if not PUBLIC_HOST:
        raise ConfigError("BIRDQUEUE_PUBLIC_HOST is not set")
    token = uuid.uuid4().hex
    staging = PUBLIC_ROOT / token
    staging.mkdir(parents=True)
    urls = []
    for p in paths:
        shutil.copy2(p, staging / p.name)
        urls.append(f"https://{PUBLIC_HOST}:{FUNNEL_PORT}/{token}/{p.name}")
    subprocess.run(
        ["tailscale", "funnel", "--bg", f"--https={FUNNEL_PORT}",
         f"http://127.0.0.1:{MEDIA_PORT}"],
        capture_output=True, timeout=30,
    )

    def cleanup_staging():
        subprocess.run(
            ["tailscale", "funnel", f"--https={FUNNEL_PORT}", "off"],
            capture_output=True, timeout=30,
        )
        shutil.rmtree(staging, ignore_errors=True)

    # Tailscale's edge takes ~20-30s to start serving after a funnel is enabled,
    # and the hostname resolves to several ingress addresses that each propagate
    # on their own schedule. One successful probe therefore proves very little:
    # Meta opens its own fresh connection and can land on an ingress node that
    # is not ready yet, which fails the whole post. So require several
    # consecutive successes, each on a brand-new connection (no pooling), and
    # only then hand the URLs over.
    probe = urls[0]
    deadline = time.time() + FUNNEL_READY_TIMEOUT
    streak, last = 0, ""
    while time.time() < deadline:
        try:
            # A fresh Client per attempt: connection reuse would keep hitting the
            # one ingress node that already works and hide the others.
            with httpx.Client(timeout=10) as c:
                ok = c.get(probe).status_code == 200
            streak = streak + 1 if ok else 0
            if not ok:
                last = "non-200"
        except Exception as exc:
            streak, last = 0, f"{type(exc).__name__}: {exc}"[:120]
        if streak >= FUNNEL_READY_STREAK:
            break
        time.sleep(4)
    else:
        cleanup_staging()
        raise PublishError(
            f"Funnel did not serve {FUNNEL_READY_STREAK} consecutive probes within "
            f"{FUNNEL_READY_TIMEOUT}s (last: {last or 'non-200'})"
        )

    return urls, cleanup_staging


def host_images(paths: list[Path]) -> tuple[list[str], callable]:
    if MEDIA_BACKEND == "s3":
        return _host_via_s3(paths)
    if MEDIA_BACKEND == "funnel":
        return _host_via_funnel(paths)
    raise ConfigError(
        f"MEDIA_BACKEND is {MEDIA_BACKEND!r}; set it to 's3' or 'funnel' to publish."
    )


# ── Instagram ─────────────────────────────────────────────────────────────────

def _ig(method: str, path: str, **params) -> dict:
    params.setdefault("access_token", IG_TOKEN)
    url = f"{IG_API}/{IG_VERSION}/{path.lstrip('/')}"
    with httpx.Client(timeout=60) as c:
        r = c.request(method, url,
                      data=params if method == "POST" else None,
                      params=None if method == "POST" else params)
    try:
        body = r.json()
    except Exception:
        raise PublishError(f"{r.status_code}: {r.text[:200]}")
    if "error" in body:
        e = body["error"]
        raise PublishError(f"{e.get('message', body)} (code {e.get('code')})")
    if r.status_code >= 400:
        raise PublishError(f"{r.status_code}: {body}")
    return body


def _await_container(cid: str) -> None:
    deadline = time.time() + CONTAINER_TIMEOUT
    code = None
    while time.time() < deadline:
        s = _ig("GET", cid, fields="status_code,status")
        code = s.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise PublishError(f"Meta could not process the image: {s.get('status')}")
        time.sleep(3)
    raise PublishError(f"container {cid} still {code} after {CONTAINER_TIMEOUT}s")


def publish_to_instagram(paths: list[Path], caption: str) -> dict:
    if not IG_USER_ID or not IG_TOKEN:
        raise ConfigError("IG_USER_ID / IG_ACCESS_TOKEN are not set")
    if not paths:
        raise PublishError("no images")
    if len(paths) > MAX_CAROUSEL:
        raise PublishError(f"{len(paths)} images — Instagram allows {MAX_CAROUSEL}")

    urls, cleanup = host_images(paths)
    try:
        if len(urls) == 1:
            cid = _ig("POST", f"{IG_USER_ID}/media",
                      image_url=urls[0], caption=caption)["id"]
            _await_container(cid)
        else:
            children = []
            for u in urls:
                children.append(
                    _ig("POST", f"{IG_USER_ID}/media",
                        image_url=u, is_carousel_item="true")["id"]
                )
            for c in children:
                _await_container(c)
            cid = _ig("POST", f"{IG_USER_ID}/media", media_type="CAROUSEL",
                      caption=caption, children=",".join(children))["id"]
            _await_container(cid)
        media_id = _ig("POST", f"{IG_USER_ID}/media_publish", creation_id=cid)["id"]
    finally:
        cleanup()

    permalink = ""
    try:
        permalink = _ig("GET", media_id, fields="permalink").get("permalink", "")
    except PublishError:
        pass
    return {"media_id": media_id, "permalink": permalink}


# ── publisher loop ────────────────────────────────────────────────────────────

def due_posts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM posts WHERE status='queued' AND due_at <= ? "
        "AND attempts < ? ORDER BY due_at",
        (now_iso(), MAX_ATTEMPTS),
    ).fetchall()


def publish_one(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    pid = row["id"]
    imgs = conn.execute(
        "SELECT filename FROM media WHERE post_id=? ORDER BY idx", (pid,)
    ).fetchall()
    paths = [MEDIA_ROOT / pid / m["filename"] for m in imgs]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise PublishError(f"missing image files: {missing[:2]}")

    conn.execute("UPDATE posts SET status='publishing', updated_at=? WHERE id=?",
                 (now_iso(), pid))
    conn.commit()
    try:
        result = publish_to_instagram(paths, row["caption"])
    except ConfigError as exc:
        conn.execute(
            "UPDATE posts SET status='queued', error=?, updated_at=? WHERE id=?",
            (f"not published (server config): {exc}"[:500], now_iso(), pid),
        )
        conn.commit()
        raise
    except Exception as exc:
        conn.execute(
            "UPDATE posts SET status='queued', attempts=attempts+1, error=?, "
            "updated_at=? WHERE id=?",
            (str(exc)[:500], now_iso(), pid),
        )
        conn.commit()
        raise
    conn.execute(
        "UPDATE posts SET status='published', published_at=?, media_id=?, "
        "permalink=?, error=NULL, updated_at=? WHERE id=?",
        (now_iso(), result["media_id"], result.get("permalink", ""), now_iso(), pid),
    )
    conn.commit()
    remember_species(row["caption"])
    return result


def publisher_loop() -> None:
    while True:
        try:
            conn = fresh_conn()
            try:
                for row in due_posts(conn):
                    try:
                        r = publish_one(conn, row)
                        app.logger.info("published %s -> %s", row["id"], r["media_id"])
                    except Exception as exc:
                        app.logger.warning("publish failed %s: %s", row["id"], exc)
            finally:
                conn.close()
        except Exception as exc:  # never let the loop die
            app.logger.error("publisher tick failed: %s", exc)
        time.sleep(TICK_SECONDS)


# ── species history (replaces mining Buffer for autocomplete) ─────────────────

def load_species() -> list[str]:
    if HISTORY.exists():
        try:
            return json.loads(HISTORY.read_text())
        except ValueError:
            return []
    return []


def remember_species(caption: str) -> None:
    first = (caption or "").strip().split("\n")[0].strip()
    if not first or len(first) > 60:
        return
    names = load_species()
    if first in names:
        names.remove(first)
    names.insert(0, first)
    HISTORY.write_text(json.dumps(names[:400], indent=2))


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    conn = db()
    counts = {
        r["status"]: r["n"]
        for r in conn.execute("SELECT status, COUNT(*) n FROM posts GROUP BY status")
    }
    return jsonify({
        "ok": True,
        "media_backend": MEDIA_BACKEND,
        "can_publish": MEDIA_BACKEND in {"s3", "funnel"} and bool(IG_USER_ID and IG_TOKEN),
        "counts": counts,
        "species_known": len(load_species()),
        "next_slot": next_open_slot(conn),
    })


@app.get("/api/queue")
def list_queue():
    conn = db()
    rows = conn.execute(
        "SELECT * FROM posts ORDER BY CASE status WHEN 'published' THEN 1 ELSE 0 END, due_at"
    ).fetchall()
    return jsonify({"posts": [row_to_post(conn, r) for r in rows],
                    "next_slot": next_open_slot(conn)})


@app.post("/api/posts")
def create_post():
    """Accept a post. Images arrive as multipart files, caption/due_at as form fields."""
    conn = db()
    caption = (request.form.get("caption") or "").strip()
    if not caption:
        return jsonify({"error": "caption is required"}), 400
    files = request.files.getlist("images")
    if not files:
        return jsonify({"error": "at least one image is required"}), 400
    if len(files) > MAX_CAROUSEL:
        return jsonify({"error": f"{len(files)} images — Instagram allows {MAX_CAROUSEL}"}), 400

    due_at = (request.form.get("due_at") or "").strip() or next_open_slot(conn)
    pid = uuid.uuid4().hex[:12]
    dest = MEDIA_ROOT / pid
    dest.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(files):
        name = f"{i:02d}_{Path(f.filename or 'image.jpg').name}"
        f.save(dest / name)
        conn.execute("INSERT INTO media (id, post_id, idx, filename) VALUES (?,?,?,?)",
                     (uuid.uuid4().hex[:12], pid, i, name))
    ts = now_iso()
    conn.execute(
        "INSERT INTO posts (id, caption, due_at, status, created_at, updated_at) "
        "VALUES (?,?,?,'queued',?,?)", (pid, caption, due_at, ts, ts))
    conn.commit()
    row = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    return jsonify(row_to_post(conn, row)), 201


@app.patch("/api/posts/<pid>")
def update_post(pid):
    conn = db()
    row = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if not row:
        return jsonify({"error": "no such post"}), 404
    if row["status"] == "published":
        return jsonify({"error": "already published; cannot modify"}), 409

    body = request.get_json(silent=True) or {}
    sets, vals = [], []
    if "caption" in body:
        sets.append("caption=?"); vals.append(str(body["caption"]))
    if "due_at" in body:
        sets.append("due_at=?"); vals.append(str(body["due_at"]))
    if "status" in body:
        if body["status"] not in {"queued", "paused"}:
            return jsonify({"error": "status must be queued or paused"}), 400
        sets.append("status=?"); vals.append(body["status"])
        sets.append("attempts=0")
    if not sets:
        return jsonify({"error": "nothing to update"}), 400
    sets.append("updated_at=?"); vals.append(now_iso()); vals.append(pid)
    conn.execute(f"UPDATE posts SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()
    row = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    return jsonify(row_to_post(conn, row))


@app.post("/api/queue/reorder")
def reorder():
    """Permute posts across the existing slots — Buffer's reorder semantics.

    The set of dates stays put; which post sits in which date changes. That is
    what dragging a post up the queue means to someone used to Buffer.
    """
    conn = db()
    order = (request.get_json(silent=True) or {}).get("order") or []
    if not isinstance(order, list) or not order:
        return jsonify({"error": "order must be a non-empty list of post ids"}), 400
    rows = conn.execute(
        "SELECT id, due_at FROM posts WHERE status IN ('queued','paused') ORDER BY due_at"
    ).fetchall()
    known = {r["id"] for r in rows}
    if set(order) != known:
        return jsonify({
            "error": "order must list exactly the unpublished posts",
            "expected": sorted(known), "got": sorted(set(order)),
        }), 400
    slots = [r["due_at"] for r in rows]
    for pid, slot in zip(order, slots):
        conn.execute("UPDATE posts SET due_at=?, updated_at=? WHERE id=?",
                     (slot, now_iso(), pid))
    conn.commit()
    return list_queue()


@app.delete("/api/posts/<pid>")
def delete_post(pid):
    conn = db()
    row = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if not row:
        return jsonify({"error": "no such post"}), 404
    conn.execute("DELETE FROM media WHERE post_id=?", (pid,))
    conn.execute("DELETE FROM posts WHERE id=?", (pid,))
    conn.commit()
    shutil.rmtree(MEDIA_ROOT / pid, ignore_errors=True)
    return jsonify({"deleted": pid, "was_status": row["status"]})


@app.post("/api/posts/<pid>/publish-now")
def publish_now(pid):
    conn = db()
    row = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if not row:
        return jsonify({"error": "no such post"}), 404
    if row["status"] == "published":
        return jsonify({"error": "already published"}), 409
    try:
        result = publish_one(conn, row)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"published": pid, **result})


@app.get("/api/next-slot")
def next_slot():
    return jsonify({"next_slot": next_open_slot(db())})


@app.get("/api/species")
def species():
    return jsonify({"species": load_species()})


@app.get("/media/<pid>/<path:filename>")
def media(pid, filename):
    """Serve a queued image to the UI. Tailnet-only; not the publish path."""
    return send_from_directory(MEDIA_ROOT / pid, filename)


def public_media_server() -> None:
    """Serve PUBLIC_ROOT on loopback, for Tailscale Funnel to proxy during a publish.

    This is the only thing in the system that is ever reachable from the open
    internet, so it does exactly one thing: hand back a file whose random
    directory name the caller already knew. Directory listings are refused, so
    the random token cannot be discovered by browsing.
    """
    import functools
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    class NoListing(SimpleHTTPRequestHandler):
        def list_directory(self, path):          # noqa: D102 - stdlib override
            self.send_error(404, "Not Found")
            return None

        def log_message(self, fmt, *args):       # keep journald readable
            app.logger.info("media: " + fmt, *args)

    handler = functools.partial(NoListing, directory=str(PUBLIC_ROOT))
    ThreadingHTTPServer(("127.0.0.1", MEDIA_PORT), handler).serve_forever()


def main() -> None:
    init_storage()
    threading.Thread(target=publisher_loop, daemon=True).start()
    threading.Thread(target=public_media_server, daemon=True).start()
    app.run(host="127.0.0.1", port=API_PORT, threaded=True)


if __name__ == "__main__":
    main()
