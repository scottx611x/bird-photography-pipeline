#!/usr/bin/env python3
"""
lr_host.py — Mac-side HTTP bridge for Lightroom automation.

Runs on your Mac (not in Docker). The Docker web UI calls this via
host.docker.internal:8766 to trigger AppleScript commands in Lightroom.

Run once before using the workflow:
  python3 ~/bird-photography-pipeline/lr_host.py

Keep this running in a Terminal tab while you work.
"""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOOLS  = Path(__file__).parent
PYTHON = str(Path.home() / ".pyenv" / "versions" / "3.12.11" / "bin" / "python3")
ALLOWED = {"import", "auto-tone", "ai-denoise", "copy-and-paste", "export",
           "syno-albums", "syno-fetch", "lr-busy", "lr-status"}

# One automation command at a time — concurrent Lightroom AppleScript runs (or
# two fetches of the same album) would collide. Health checks skip the lock,
# so a long Synology fetch no longer makes the host look offline.
_run_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass  # silence default request logs

    def do_GET(self):
        if self.path == "/health":
            self._json({"ok": True, "host": "lr_host.py"})
        elif self.path.startswith("/curate/"):
            self._curate_get()
        else:
            self._json({"error": "not found"}, 404)

    def _curate_get(self):
        """Album-curator bridge: Synology runs Mac-side (mDNS), Docker proxies here."""
        import syno_curate
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        parts = u.path.strip("/").split("/")
        try:
            if parts[1] == "days":
                since = parse_qs(u.query).get("since", ["2026-07-01"])[0]
                self._json({"ok": True, "days": syno_curate.days(since)})
            elif parts[1] == "thumb":          # /curate/thumb/<size>/<id>
                data = syno_curate.thumb(int(parts[3]), parts[2])
                if not data:
                    self._json({"error": "no thumbnail"}, 404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "max-age=86400")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif parts[1] == "exif":           # /curate/exif/<id>
                self._json(syno_curate.exif(int(parts[2])))
            else:
                self._json({"error": "unknown"}, 404)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    def do_POST(self):
        if self.path == "/curate/sync":
            import syno_curate
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            try:
                log = syno_curate.sync(body.get("albums", {}))
                for line in log:
                    print(f"  curate sync: {line}")
                self._json({"ok": True, "log": log})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return
        parts = self.path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "run" or parts[1] not in ALLOWED:
            self._json({"error": f"unknown: {self.path}"}, 400)
            return

        cmd = parts[1]

        # Optional JSON body (e.g. {"folder": "/path/to/batch"})
        body = {}
        length = int(self.headers.get("Content-Length", 0))
        if length:
            body = json.loads(self.rfile.read(length))

        # Total CPU% of all Lightroom processes — the server polls this to
        # detect when the background AI-denoise queue has drained
        if cmd == "lr-busy":
            args = ["bash", "-c",
                    "ps -A -o %cpu= -o comm= | grep -i 'lightroom' | "
                    "awk '{s+=$1} END {printf \"%.0f\", s}'"]
        elif cmd == "lr-status":
            args = [PYTHON, str(TOOLS / "lr_auto.py"), "status"]
        # Synology fetch runs Mac-side so s-cubed-nas.local resolves over mDNS
        elif cmd == "syno-albums":
            print("→ syno_fetch.py albums")
            args = [PYTHON, str(TOOLS / "syno_fetch.py"), "albums"]
        elif cmd == "syno-fetch":
            album = body.get("album", "")
            print(f"→ syno_fetch.py fetch --album {album}")
            args = [PYTHON, str(TOOLS / "syno_fetch.py"), "fetch", "--album", album]
            if body.get("dest"):
                args += ["--dest", body["dest"]]   # combined multi-album batches
            if body.get("raw_only"):
                args.append("--raw-only")
        else:
            print(f"→ lr_auto.py {cmd}" + (f"  folder={body.get('folder','')}" if body.get("folder") else ""))
            # --folder must come before the subcommand in argparse
            args = [PYTHON, str(TOOLS / "lr_auto.py")]
            if body.get("folder"):
                args += ["--folder", body["folder"]]
            if body.get("all"):
                args.append("--all")
            args.append(cmd)

        with _run_lock:
            result = subprocess.run(args, capture_output=True, text=True)
        output = (result.stdout + result.stderr).strip()
        for line in output.splitlines():
            print(f"  {line}")
        if cmd in {"auto-tone", "export"}:
            subprocess.Popen(["open", "http://localhost:8765"])
        self._json({"ok": result.returncode == 0, "output": output})

    def _json(self, data: dict, code: int = 200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = 8766
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"lr_host.py listening on :{port}")
    print(f"Docker will reach this via host.docker.internal:{port}")
    print("Keep this running while using the workflow UI.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
