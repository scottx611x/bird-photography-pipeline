#!/usr/bin/env python3
"""
ig_post.py — post to Instagram directly, without Buffer.

Buffer kept losing channel access ("We've lost channel access — Refresh to
continue posting") and silently failing scheduled posts. This talks to Meta's
own publishing API instead, so the only thing between a photo and Instagram is
a token we control.

Why this is allowed to be simple: publishing to *your own* account needs no App
Review. A Meta app in development mode, with your account added as an Instagram
Tester, can publish immediately. The limit is 100 posts per 24 hours and a
carousel counts as one — far past anything a bird account will do.

Meta fetches the images itself, by URL, so they have to be on the public
internet for a moment. That's what S3 is for here: upload, hand Meta a
presigned link, publish, and the link expires on its own. Nothing is left
world-readable.

Setup (once) — see `python ig_post.py setup` for the current checklist:
  IG_USER_ID        your Instagram professional account's ID
  IG_ACCESS_TOKEN   long-lived token (60 days; `refresh` renews it)
  IG_S3_BUCKET      a private bucket you own
  AWS_REGION        e.g. us-east-1
  plus normal AWS credentials (env vars, profile, or instance role)

Deliberately free of Flask and of any pipeline imports beyond the two pure
helpers it reuses, so the same file can run unchanged inside a Lambda later.
"""

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

# The Instagram Login path lives on graph.instagram.com — not graph.facebook.com,
# which is the older Page-linked flow and rejects these tokens.
API = "https://graph.instagram.com"
VERSION = "v25.0"

IG_USER_ID = os.environ.get("IG_USER_ID", "")
IG_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
S3_BUCKET = os.environ.get("IG_S3_BUCKET", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

MAX_CAROUSEL = 10          # Instagram's hard limit
LINK_TTL = 3600            # presigned URL lifetime; Meta fetches within seconds
CONTAINER_TIMEOUT = 180    # a container usually goes FINISHED in a few seconds


class IGError(RuntimeError):
    """Anything Meta refused. Carries the API's own message where there is one."""


def _call(method: str, path: str, **params) -> dict:
    params.setdefault("access_token", IG_TOKEN)
    url = f"{API}/{VERSION}/{path.lstrip('/')}"
    with httpx.Client(timeout=60) as client:
        r = client.request(method, url, data=params if method == "POST" else None,
                           params=None if method == "POST" else params)
    try:
        body = r.json()
    except Exception:
        raise IGError(f"{r.status_code}: {r.text[:200]}")
    if "error" in body:
        e = body["error"]
        raise IGError(f"{e.get('message', body)} (code {e.get('code')})")
    if r.status_code >= 400:
        raise IGError(f"{r.status_code}: {body}")
    return body


# ── S3: a temporary public home for each image ────────────────────────────────

def s3_upload(path: Path) -> str:
    """Put one image in S3 and return a presigned URL Meta can fetch.

    The object key is random, the bucket stays private, and the link dies after
    LINK_TTL — the photo is reachable only for as long as it takes Meta to
    pull it.
    """
    import boto3
    if not S3_BUCKET:
        raise IGError("IG_S3_BUCKET is not set")
    s3 = boto3.client("s3", region_name=AWS_REGION)
    key = f"ig-upload/{uuid.uuid4().hex}/{path.name}"
    s3.upload_file(str(path), S3_BUCKET, key,
                   ExtraArgs={"ContentType": "image/jpeg"})
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": S3_BUCKET, "Key": key}, ExpiresIn=LINK_TTL)


# ── Publishing ────────────────────────────────────────────────────────────────

def _container(**params) -> str:
    return _call("POST", f"{IG_USER_ID}/media", **params)["id"]


def _await_container(cid: str):
    """Meta downloads the image asynchronously; publishing before it is FINISHED
    fails with a misleading error, so wait for the real status."""
    deadline = time.time() + CONTAINER_TIMEOUT
    while time.time() < deadline:
        s = _call("GET", cid, fields="status_code,status")
        code = s.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise IGError(f"Meta could not process the image: {s.get('status')}")
        time.sleep(3)
    raise IGError(f"Container {cid} was still {code} after {CONTAINER_TIMEOUT}s")


def publish(image_paths: list[Path], caption: str) -> str:
    """Upload, build the container(s), publish. Returns the new media ID."""
    if not IG_USER_ID or not IG_TOKEN:
        raise IGError("IG_USER_ID / IG_ACCESS_TOKEN are not set")
    if not image_paths:
        raise IGError("nothing to post")
    if len(image_paths) > MAX_CAROUSEL:
        raise IGError(f"{len(image_paths)} images — Instagram allows {MAX_CAROUSEL}")

    urls = []
    for p in image_paths:
        print(f"  uploading {p.name}…")
        urls.append(s3_upload(p))

    if len(urls) == 1:
        print("  creating container…")
        cid = _container(image_url=urls[0], caption=caption)
        _await_container(cid)
    else:
        children = []
        for i, u in enumerate(urls, 1):
            print(f"  creating carousel item {i}/{len(urls)}…")
            child = _container(image_url=u, is_carousel_item="true")
            children.append(child)
        for c in children:
            _await_container(c)
        print("  creating carousel…")
        cid = _container(media_type="CAROUSEL", caption=caption,
                         children=",".join(children))
        _await_container(cid)

    print("  publishing…")
    media_id = _call("POST", f"{IG_USER_ID}/media_publish", creation_id=cid)["id"]
    print(f"✓ published — media {media_id}")
    return media_id


def refresh_token() -> dict:
    """Roll the 60-day token forward. Safe to run often; Meta only reissues
    once the token is at least 24 hours old."""
    with httpx.Client(timeout=30) as client:
        r = client.get(f"{API}/refresh_access_token",
                       params={"grant_type": "ig_refresh_token",
                               "access_token": IG_TOKEN})
    body = r.json()
    if "access_token" not in body:
        raise IGError(f"refresh failed: {body}")
    days = int(body.get("expires_in", 0)) // 86400
    print(f"✓ token refreshed — valid another {days} days")
    print("  put this in your .env as IG_ACCESS_TOKEN:")
    print(f"  {body['access_token']}")
    return body


def check() -> dict:
    """Prove the credentials work without posting anything."""
    problems = []
    if not IG_USER_ID:  problems.append("IG_USER_ID is not set")
    if not IG_TOKEN:    problems.append("IG_ACCESS_TOKEN is not set")
    if not S3_BUCKET:   problems.append("IG_S3_BUCKET is not set")
    if problems:
        for p in problems:
            print(f"  ✕ {p}")
        raise IGError("missing configuration")

    me = _call("GET", IG_USER_ID, fields="id,username,account_type")
    print(f"  ✓ Instagram: @{me.get('username')} ({me.get('account_type')})")

    import boto3
    boto3.client("s3", region_name=AWS_REGION).head_bucket(Bucket=S3_BUCKET)
    print(f"  ✓ S3 bucket reachable: {S3_BUCKET}")

    quota = _call("GET", f"{IG_USER_ID}/content_publishing_limit",
                  fields="config,quota_usage")
    used = (quota.get("data") or [{}])[0].get("quota_usage", 0)
    print(f"  ✓ publishing quota used in the last 24h: {used}/100")
    return me


SETUP = """
One-time setup — nothing here needs Meta App Review, because you are only
posting to your own account.

1. Instagram app → Settings → Account type → switch to a Professional
   account (Business or Creator) if it isn't already.

2. developers.facebook.com → My Apps → Create App → "Other" → "Business".
   Leave it in Development mode. Do NOT submit for review.

3. In the app: Add product → "Instagram" → "API setup with Instagram login".
   Under "Generate access tokens", add your Instagram account, then click
   Generate token. Log in and approve. Copy the token.
   That page also shows your Instagram user ID.

4. Make a private S3 bucket in your own account. Block Public Access can stay
   fully ON — this posts using presigned links, not public objects.

5. Put these in the pipeline's .env:
     IG_USER_ID=...
     IG_ACCESS_TOKEN=...
     IG_S3_BUCKET=...
     AWS_REGION=us-east-1
     AWS_ACCESS_KEY_ID=...        (an IAM user limited to PutObject on that bucket)
     AWS_SECRET_ACCESS_KEY=...

6. Verify:   python ig_post.py check
   Then:     python ig_post.py post --file DSC_0001.jpg --text "test" --dry-run

The token lasts 60 days. `python ig_post.py refresh` rolls it forward; the
pipeline will do this on its own once this is wired in.
"""


def main():
    p = argparse.ArgumentParser(description="Post to Instagram without Buffer")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup",   help="Print the one-time setup checklist")
    sub.add_parser("check",   help="Verify credentials and S3 without posting")
    sub.add_parser("refresh", help="Roll the 60-day access token forward")

    po = sub.add_parser("post", help="Publish a photo or carousel now")
    po.add_argument("--file", required=True, nargs="+",
                    help="Filenames in ~/Desktop/birbs (carousel if more than one)")
    po.add_argument("--species")
    po.add_argument("--location")
    po.add_argument("--date")
    po.add_argument("--out-of-area", action="store_true")
    po.add_argument("--text", help="Full caption, instead of species/location/date")
    po.add_argument("--dry-run", action="store_true",
                    help="Resize and show the caption, but publish nothing")

    args = p.parse_args()
    if args.cmd == "setup":
        print(SETUP)
        return
    try:
        if args.cmd == "check":
            check()
            return
        if args.cmd == "refresh":
            refresh_token()
            return

        # Reuse the pipeline's own resize and caption rules so a post made here
        # is byte-for-byte what the Buffer path would have produced.
        from bird_post import BIRDS_DIR, build_caption, resize_for_instagram

        srcs = []
        for f in args.file:
            q = Path(f)
            if not q.is_absolute():
                q = BIRDS_DIR / q
            if not q.exists():
                print(f"File not found: {q}")
                sys.exit(1)
            srcs.append(q)

        if args.text:
            caption = args.text
        elif args.species and args.location and args.date:
            caption = build_caption(args.species, args.location, args.date,
                                    args.out_of_area)
        else:
            print("Need --text, or all of --species --location --date.")
            sys.exit(1)

        ready_dir = BIRDS_DIR / ".ready"
        ready_dir.mkdir(exist_ok=True)
        ready = []
        for s in srcs:
            out = ready_dir / s.name
            print(f"Resizing {s.name} …")
            resize_for_instagram(s, out)
            ready.append(out)

        print("\n--- caption ---")
        print(caption)
        print("---------------\n")
        if args.dry_run:
            print("Dry run — nothing was published.")
            return
        publish(ready, caption)
    except IGError as e:
        print(f"✕ {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
