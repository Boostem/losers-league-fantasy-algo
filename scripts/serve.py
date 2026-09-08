#!/usr/bin/env python3
"""Serve the Losers League page and persist picks to a JSON file.

Opening index.html directly works fine, but then picks live only in that one
browser's localStorage. Running this instead keeps them in a real file under
state/, so they survive clearing site data, can be committed to git, and can be
read by anything else.

    python3 scripts/serve.py            # http://localhost:8000
    python3 scripts/serve.py --port 9000

The page detects the API automatically; with no server it falls back to
browser storage. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib.parse import urlparse, parse_qs

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(REPO_ROOT, "state")

# One writer at a time: two tabs saving at once must not interleave.
_write_lock = threading.Lock()

MAX_BODY = 1 << 20  # 1 MB is far more than a season of picks needs


def state_path(season: str) -> str:
    """Path for a season's state file, rejecting anything that isn't a year."""
    if not re.fullmatch(r"\d{4}", season):
        raise ValueError(f"bad season {season!r}")
    return os.path.join(STATE_DIR, f"losers_{season}.json")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=REPO_ROOT, **kwargs)

    # -- helpers ---------------------------------------------------------
    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _season(self) -> str:
        qs = parse_qs(urlparse(self.path).query)
        return (qs.get("season") or ["0000"])[0]

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if urlparse(self.path).path == "/api/state":
            try:
                path = state_path(self._season())
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            if not os.path.exists(path):
                return self._json({"state": None})
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return self._json({"state": json.load(fh)})
            except (OSError, ValueError) as exc:
                return self._json({"error": f"could not read state: {exc}"}, 500)
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/state":
            return self._json({"error": "not found"}, 404)
        try:
            path = state_path(self._season())
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "bad Content-Length"}, 400)
        if length <= 0 or length > MAX_BODY:
            return self._json({"error": "bad body size"}, 400)

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError as exc:
            return self._json({"error": f"bad JSON: {exc}"}, 400)
        if not isinstance(payload, dict):
            return self._json({"error": "expected a JSON object"}, 400)

        with _write_lock:
            try:
                os.makedirs(STATE_DIR, exist_ok=True)
                # Keep one generation back: a bad write should never be the
                # only copy of a season's picks.
                if os.path.exists(path):
                    shutil.copyfile(path, path + ".bak")
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2, sort_keys=True)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)  # atomic; no torn file if we die here
            except OSError as exc:
                return self._json({"error": f"could not write state: {exc}"}, 500)
        return self._json({"ok": True, "path": os.path.relpath(path, REPO_ROOT)})

    def log_message(self, fmt: str, *args: Any) -> None:
        # Quiet the per-request noise; keep API writes visible.
        if "/api/state" in (self.path or "") and self.command == "POST":
            sys.stderr.write(f"saved picks -> {self.path}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address; default is localhost only")
    args = parser.parse_args()

    os.makedirs(STATE_DIR, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}/"
    print(f"Losers League running at {url}")
    print(f"Picks are saved to {os.path.relpath(STATE_DIR, os.getcwd())}/losers_<season>.json")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
