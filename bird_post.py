#!/usr/bin/env python3
"""
bird_post.py — Resize, upload, and queue a bird photo to Buffer.

Usage:
  python3 bird_post.py --file DSC_5360-2.jpg \
                       --species "Sharp-shinned Hawk" \
                       --location "Rea St." \
                       --date 5-30-26

  # Out-of-area sighting (adds ⚠️ prefix):
  python3 bird_post.py --file DSC_1234.jpg \
                       --species "Common Loon" \
                       --location "Sand Pond, Litchfield ME" \
                       --date 5-23-26 \
                       --out-of-area

  # Multiple species (separate with commas):
  python3 bird_post.py --file DSC_5107.jpg \
                       --species "Gray Catbird, Downy Woodpecker" \
                       --location "Rea St." \
                       --date 5-16-26

Env vars required:
  BUFFER_TOKEN   Buffer API key (from publish.buffer.com/settings/api)

Requires Chrome with an active Buffer login (for S3 upload).
"""

import os, sys, argparse
from pathlib import Path
from io import BytesIO

import httpx
import browser_cookie3
from PIL import Image, ImageCms, ImageOps

# ── Constants ─────────────────────────────────────────────────────────────────

ORG_ID      = "6a008c6e3e4597b26fe42152"
CHANNEL_ID  = "6a008cfb090476fb99050c51"   # birdsofnorthandover Instagram
BIRDS_DIR   =  Path("/birds")
BUFFER_TOKEN = os.environ.get("BUFFER_TOKEN", "")

# ── Chrome session ─────────────────────────────────────────────────────────────

def get_buffer_cookies() -> dict:
    raw_cookie = os.environ.get("BUFFER_COOKIES")

    if raw_cookie:
        cookies = {}
        for part in raw_cookie.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                cookies[k] = v

        if "buffer_session" not in cookies:
            print("BUFFER_COOKIES is set, but buffer_session was not found.")
            print(f"Cookies found: {sorted(cookies.keys())}")
            sys.exit(1)

        return cookies

    # Local non-Docker fallback
    jar = browser_cookie3.chrome(domain_name=".buffer.com")
    cookies = {c.name: c.value for c in jar}

    if "buffer_session" not in cookies:
        print("No buffer_session in Chrome — log in to publish.buffer.com first.")
        sys.exit(1)

    return cookies

# ── Image ─────────────────────────────────────────────────────────────────────

# Goal: hand Instagram the highest-quality file we can while staying just under
# its ~8 MB upload limit, and let IG do its own single downscale to display size.
# Pre-shrinking to 1080 here only throws away detail before IG sees it. We keep
# resolution generous (long edge up to 4096px — beyond that IG keeps nothing and
# the pixels are wasted) and spend the byte budget by dialing JPEG quality to
# land as close to 8 MB as we can without going over.
MAX_EDGE = 4096
MAX_UPLOAD_MB = 8.0
MIN_QUALITY = 70
SRGB = ImageCms.createProfile("sRGB")


def resize_for_instagram(src: Path, dst: Path):
    img = Image.open(src)
    img = ImageOps.exif_transpose(img)

    # IG assumes sRGB — Adobe RGB / ProPhoto exports come out flat without this
    icc = img.info.get("icc_profile")
    if icc:
        try:
            src_profile = ImageCms.ImageCmsProfile(BytesIO(icc))
            img = ImageCms.profileToProfile(img, src_profile, SRGB, outputMode="RGB")
        except Exception as e:
            print(f"  (icc → sRGB skipped: {e})")
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Cap the long edge so we don't waste bytes on pixels IG discards anyway.
    w, h = img.size
    scale = min(MAX_EDGE / max(w, h), 1.0)
    if scale < 1.0:
        img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)

    # Walk quality down from max until we fit under the limit — this fills the
    # byte budget: a detailed photo settles near 8 MB at high quality, while an
    # already-small one just stays at q100.
    srgb_bytes = ImageCms.ImageCmsProfile(SRGB).tobytes()
    quality = 100
    while True:
        img.save(dst, "JPEG", quality=quality, optimize=True, subsampling=0,
                 icc_profile=srgb_bytes)
        mb = dst.stat().st_size / 1024 / 1024
        if mb <= MAX_UPLOAD_MB or quality <= MIN_QUALITY:
            break
        quality -= 1

    print(f"  saved → {img.width}×{img.height}  q{quality}  {mb:.1f} MB")

# ── Buffer / S3 ───────────────────────────────────────────────────────────────

def upload_to_buffer_s3(path: Path, cookies: dict) -> str:
    """Upload to Buffer's own S3 bucket. Returns permanent public URL."""
    r = httpx.post(
        "https://graph.buffer.com/?_o=s3PreSignedURL",
        headers={
            "Content-Type": "application/json",
            "x-buffer-client-id": "webapp-publishing",
            "Origin": "https://publish.buffer.com",
        },
        cookies=cookies,
        json={
            "operationName": "s3PreSignedURL",
            "variables": {"input": {
                "organizationId": ORG_ID,
                "fileName": path.name,
                "mimeType": "image/jpeg",
                "uploadType": "postAsset",
            }},
            "query": "query s3PreSignedURL($input: S3PreSignedURLInput!) { s3PreSignedURL(input: $input) { url key bucket } }",
        },
        timeout=15,
    )
    r.raise_for_status()
    s3 = r.json()["data"]["s3PreSignedURL"]

    with open(path, "rb") as f:
        put_r = httpx.put(s3["url"], content=f.read(), headers={"Content-Type": "image/jpeg"}, timeout=60)
    put_r.raise_for_status()

    return f"https://{s3['bucket']}.s3.amazonaws.com/{s3['key']}"


def queue_to_buffer(text: str, image_urls: list[str], scheduled_at: str = "", schedule_date: str = "") -> dict:
    from datetime import datetime, timezone
    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess { post { id dueAt } }
        ... on MutationError { message }
      }
    }
    """
    inp = {
        "channelId": CHANNEL_ID,
        "text":      text,
        "assets":    [{"image": {"url": url}} for url in image_urls],
        "metadata":  {"instagram": {"type": "post", "shouldShareToFeed": True}},
    }
    if scheduled_at:
        inp["schedulingType"] = "automatic"
        inp["mode"]           = "customScheduled"
        inp["dueAt"]          = scheduled_at
        print(f"  Scheduling for: {scheduled_at}")
    else:
        inp["schedulingType"] = "automatic"
        inp["mode"]           = "addToQueue"
    r = httpx.post(
        "https://api.buffer.com",
        headers={"Authorization": f"Bearer {BUFFER_TOKEN}", "Content-Type": "application/json"},
        json={"query": mutation, "variables": {"input": inp}},
        timeout=30,
    )
    if not r.is_success:
        print(f"Buffer API error {r.status_code}: {r.text[:500]}")
        r.raise_for_status()
    return r.json()

# ── Caption builder ────────────────────────────────────────────────────────────

def build_caption(species: str, location: str, date: str, out_of_area: bool) -> str:
    lines = [s.strip() for s in species.split(",")]
    if out_of_area:
        lines = [f"⚠️ {l}" for l in lines]
    return "\n".join(lines) + f"\n\n{location}\n\n{date}"

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Queue a bird photo to Buffer Instagram")
    parser.add_argument("--file",        required=True, nargs="+", help="One or more filenames in ~/Desktop/birds/ (carousel if >1)")
    parser.add_argument("--species",     help="Bird species, comma-separated for multiple")
    parser.add_argument("--location",    help="Shoot location, e.g. 'Rea St.'")
    parser.add_argument("--date",        help="Shoot date, e.g. '5-30-26'")
    parser.add_argument("--out-of-area", action="store_true", help="Add ⚠️ prefix (outside North Andover)")
    parser.add_argument("--text",        help="Override full caption text (skips --species/--location/--date)")
    parser.add_argument("--schedule-date", help="Post date YYYY-MM-DD (informational)")
    parser.add_argument("--scheduled-at",  help="Exact ISO scheduledAt timestamp (overrides --schedule-date)")
    parser.add_argument("--dry-run",     action="store_true", help="Show caption and stop — don't post")
    parser.add_argument("--resize-only", action="store_true", help="Resize images into .ready and stop — no upload, no post")
    args = parser.parse_args()

    if not BUFFER_TOKEN and not args.resize_only:
        print("Missing BUFFER_TOKEN env var.")
        sys.exit(1)

    # Resolve file paths
    srcs = []
    for f in args.file:
        p = Path(f)
        if not p.is_absolute():
            p = BIRDS_DIR / p
        if not p.exists():
            print(f"File not found: {p}")
            sys.exit(1)
        srcs.append(p)

    # Resize-only: produce instagram-ready files and stop
    if args.resize_only:
        ready_dir = BIRDS_DIR / ".ready"
        ready_dir.mkdir(exist_ok=True)
        for src in srcs:
            ready = ready_dir / src.name
            print(f"Resizing {src.name} …")
            resize_for_instagram(src, ready)
        print(f"\n✓ Resized {len(srcs)} image(s) → {ready_dir}")
        return

    # Build and preview caption
    if args.text:
        caption = args.text
    elif args.species and args.location and args.date:
        caption = build_caption(args.species, args.location, args.date, args.out_of_area)
    else:
        print("Provide either --text or all of --species, --location, --date")
        sys.exit(1)
    print(f"\n── caption ──\n{caption}\n─────────────")
    if len(srcs) > 1:
        print(f"── {len(srcs)}-photo carousel ──")

    if args.dry_run:
        print("\n(dry run — not posting)")
        return

    # Resize all
    ready_dir = BIRDS_DIR / ".ready"
    ready_dir.mkdir(exist_ok=True)
    readies = []
    for src in srcs:
        ready = ready_dir / src.name
        print(f"\nResizing {src.name} …")
        resize_for_instagram(src, ready)
        readies.append(ready)

    # Upload all
    print("\nUploading to Buffer's S3 …")
    cookies = get_buffer_cookies()
    image_urls = []
    for ready in readies:
        url = upload_to_buffer_s3(ready, cookies)
        image_urls.append(url)
        print(f"  {ready.name} ✓")

    # Queue
    print("Queuing to Buffer …")
    result = queue_to_buffer(caption, image_urls,
                             scheduled_at=getattr(args, 'scheduled_at', None) or "",
                             schedule_date=args.schedule_date or "")

    if "errors" in result:
        print(f"Buffer API error: {result['errors']}")
        sys.exit(1)

    inner = result.get("data", {}).get("createPost", {})
    if "message" in inner:
        print(f"Buffer rejected: {inner['message']}")
        sys.exit(1)

    due = inner.get("post", {}).get("dueAt", "next available slot")
    print(f"\nDone! Scheduled for: {due}")


if __name__ == "__main__":
    main()
