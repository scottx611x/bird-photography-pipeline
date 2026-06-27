#!/usr/bin/env python3
"""
syno_fetch.py — Pull originals from Synology Photos (personal space) into a
birb-workflow batch folder.

Runs Mac-side (where s-cubed-nas.local resolves over mDNS). Reads creds from env:

  SYNO_HOST   e.g. http://s-cubed-nas.local:5000   (default)
  SYNO_USER   DSM service account
  SYNO_PASS   its password
  SYNO_OTP    optional one-time 2FA code (better: use a no-2FA service account)

Usage:
  python3 syno_fetch.py albums                      # list albums as JSON (for the UI picker)
  python3 syno_fetch.py fetch --album "2026-06-08-best" [--dest /path] [--raw-only]

`fetch` maps the album to ~/Downloads/<album name>/ by default so the existing
batch scanner picks it up. Already-present files are skipped (resumable).
"""

import os
import sys
import json
import argparse
from pathlib import Path

import httpx


def _load_env():
    """Load .env from this script's dir so creds work whether run standalone or via lr_host."""
    envp = Path(__file__).parent / ".env"
    if envp.exists():
        for line in envp.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

HOST  = os.environ.get("SYNO_HOST", "http://s-cubed-nas.local:5000").rstrip("/")
USER  = os.environ.get("SYNO_USER", "")
PASS  = os.environ.get("SYNO_PASS", "")
OTP   = os.environ.get("SYNO_OTP", "")
ENTRY = f"{HOST}/webapi/entry.cgi"

RAW_EXTS = {".arw", ".nef", ".cr2", ".cr3", ".raf", ".rw2", ".dng", ".orf", ".pef", ".srw"}


class SynoError(Exception):
    pass


# ── API helpers ──────────────────────────────────────────────────────────────

def _call(client: httpx.Client, params: dict, timeout: float = 30) -> dict:
    r = client.get(ENTRY, params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise SynoError(f"{params.get('api')} failed: {data.get('error')}")
    return data["data"]


DEVICE_FILE = Path(__file__).parent / ".syno_device"   # gitignored device token cache


def _load_device_id() -> str:
    if os.environ.get("SYNO_DEVICE_ID"):
        return os.environ["SYNO_DEVICE_ID"].strip()
    if DEVICE_FILE.exists():
        return DEVICE_FILE.read_text().strip()
    return ""


def _save_device_id(did: str) -> None:
    try:
        DEVICE_FILE.write_text(did)
    except Exception:
        pass


def login(client: httpx.Client) -> str:
    """Log in, transparently using a saved device token to skip 2FA.

    First time on a 2FA account: set SYNO_OTP to a current code — DSM returns a
    device token we cache in .syno_device, and subsequent logins skip OTP.
    """
    if not (USER and PASS):
        raise SynoError("SYNO_USER / SYNO_PASS not set — add them to .env")
    did = _load_device_id()
    params = {
        "api": "SYNO.API.Auth", "version": "7", "method": "login",
        "account": USER, "passwd": PASS, "format": "sid",
        "enable_device_token": "yes", "device_name": "birb-workflow",
    }
    if did:
        params["device_id"] = did
    elif OTP:
        params["otp_code"] = OTP
    r = client.get(ENTRY, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        code = (data.get("error") or {}).get("code")
        if code in (403, 404, 406) and not OTP:
            raise SynoError("2FA required — set SYNO_OTP=<current 6-digit code> in .env once "
                            "to enroll a device token (then it's not needed again)")
        msg = {
            400: "incorrect account or password",
            403: "2FA required — set SYNO_OTP to enroll a device token",
            404: "2FA code is wrong or expired — try a fresh SYNO_OTP",
            406: "2FA enforced — set SYNO_OTP to enroll a device token",
        }.get(code, f"DSM error {code}")
        raise SynoError(f"Login failed: {msg}")
    dd = data["data"]
    new_did = dd.get("did") or dd.get("device_id")
    if new_did and new_did != did:
        _save_device_id(new_did)
        print("  (saved device token — future logins won't need a 2FA code)")
    return dd["sid"]


def logout(client: httpx.Client, sid: str) -> None:
    try:
        client.get(ENTRY, params={"api": "SYNO.API.Auth", "version": "7",
                                  "method": "logout", "_sid": sid}, timeout=10)
    except Exception:
        pass


def list_albums(client: httpx.Client, sid: str) -> list:
    out, offset = [], 0
    while True:
        data = _call(client, {
            "api": "SYNO.Foto.Browse.Album", "version": "2", "method": "list",
            "offset": offset, "limit": 100, "_sid": sid,
        })
        items = data.get("list", [])
        out += items
        if len(items) < 100:
            break
        offset += 100
    return [{
        "id": a["id"],
        "name": a.get("name", ""),
        "item_count": a.get("item_count", 0),
        "passphrase": a.get("passphrase", ""),
    } for a in out]


def find_album(client: httpx.Client, sid: str, name: str) -> dict:
    albums = list_albums(client, sid)
    exact = [a for a in albums if a["name"] == name]
    if exact:
        return exact[0]
    ci = [a for a in albums if a["name"].lower() == name.lower()]
    if ci:
        return ci[0]
    avail = ", ".join(a["name"] for a in albums) or "(none)"
    raise SynoError(f"album '{name}' not found. Available: {avail}")


def list_items(client: httpx.Client, sid: str, album: dict) -> list:
    out, offset = [], 0
    base = {"api": "SYNO.Foto.Browse.Item", "version": "1", "method": "list", "_sid": sid}
    if album.get("passphrase"):
        base["passphrase"] = album["passphrase"]
    else:
        base["album_id"] = album["id"]
    while True:
        data = _call(client, dict(base, offset=offset, limit=200))
        items = data.get("list", [])
        out += items
        if len(items) < 200:
            break
        offset += 200
    return out


def download_item(client: httpx.Client, sid: str, album: dict, item: dict, dest: Path) -> bool:
    """Download one original into dest/<filename>. Returns True if written, False if skipped."""
    name = item.get("filename") or f"{item['id']}.bin"
    target = dest / name
    if target.exists() and target.stat().st_size > 0:
        return False
    params = {
        "api": "SYNO.Foto.Download", "version": "1", "method": "download",
        "unit_id": json.dumps([item["id"]]), "_sid": sid,
    }
    if album.get("passphrase"):
        params["passphrase"] = album["passphrase"]
    tmp = target.with_suffix(target.suffix + ".part")
    with client.stream("GET", ENTRY, params=params, timeout=300) as r:
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "application/json" in ctype:  # an error came back as JSON, not bytes
            raise SynoError(f"download of {name} failed: {r.read()[:200]!r}")
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                f.write(chunk)
    tmp.rename(target)
    return True


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_albums(args):
    with httpx.Client() as client:
        sid = login(client)
        try:
            print(json.dumps(list_albums(client, sid)))
        finally:
            logout(client, sid)


def cmd_fetch(args):
    dest = Path(args.dest).expanduser() if args.dest else (Path.home() / "Downloads" / args.album)
    dest.mkdir(parents=True, exist_ok=True)
    with httpx.Client() as client:
        sid = login(client)
        try:
            album = find_album(client, sid, args.album)
            items = list_items(client, sid, album)
            if args.raw_only:
                items = [i for i in items if Path(i.get("filename", "")).suffix.lower() in RAW_EXTS]
            print(f"Album '{album['name']}': {len(items)} file(s) → {dest}")
            got = skipped = 0
            for i, item in enumerate(items, 1):
                name = item.get("filename", item["id"])
                try:
                    if download_item(client, sid, album, item, dest):
                        got += 1
                        print(f"  [{i}/{len(items)}] ✓ {name}")
                    else:
                        skipped += 1
                        print(f"  [{i}/{len(items)}] · {name} (already present)")
                except Exception as e:
                    print(f"  [{i}/{len(items)}] ✗ {name}: {e}")
            print(f"Done — {got} downloaded, {skipped} skipped → {dest}")
        finally:
            logout(client, sid)


def main():
    p = argparse.ArgumentParser(description="Fetch originals from Synology Photos")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("albums", help="List albums as JSON")
    f = sub.add_parser("fetch", help="Download an album's originals into a batch folder")
    f.add_argument("--album", required=True, help="Album name (e.g. 2026-06-08-best)")
    f.add_argument("--dest", help="Destination dir (default ~/Downloads/<album>)")
    f.add_argument("--raw-only", action="store_true", help="Download only RAW files")
    args = p.parse_args()

    try:
        (cmd_albums if args.cmd == "albums" else cmd_fetch)(args)
    except SynoError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
