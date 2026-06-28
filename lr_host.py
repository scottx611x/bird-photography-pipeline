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
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TOOLS  = Path(__file__).parent
PYTHON = str(Path.home() / ".pyenv" / "versions" / "3.12.11" / "bin" / "python3")
ALLOWED = {"import", "auto-tone", "ai-denoise", "copy-and-paste", "export",
           "syno-albums", "syno-fetch"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass  # silence default request logs

    def do_GET(self):
        if self.path == "/health":
            self._json({"ok": True, "host": "lr_host.py"})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
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

        # Synology fetch runs Mac-side so s-cubed-nas.local resolves over mDNS
        if cmd == "syno-albums":
            print("→ syno_fetch.py albums")
            args = [PYTHON, str(TOOLS / "syno_fetch.py"), "albums"]
        elif cmd == "syno-fetch":
            album = body.get("album", "")
            print(f"→ syno_fetch.py fetch --album {album}")
            args = [PYTHON, str(TOOLS / "syno_fetch.py"), "fetch", "--album", album]
            if body.get("raw_only"):
                args.append("--raw-only")
        else:
            print(f"→ lr_auto.py {cmd}" + (f"  folder={body.get('folder','')}" if body.get("folder") else ""))
            # --folder must come before the subcommand in argparse
            args = [PYTHON, str(TOOLS / "lr_auto.py")]
            if body.get("folder"):
                args += ["--folder", body["folder"]]
            args.append(cmd)

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
    HTTPServer.allow_reuse_address = True
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"lr_host.py listening on :{port}")
    print(f"Docker will reach this via host.docker.internal:{port}")
    print("Keep this running while using the workflow UI.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
