#!/usr/bin/env python3
"""InkDrop Live — local server that serves fresh trending data on demand.

Run on the PC:  python scripts/live_server.py   (port 8765)

The page (https://nathanielsai19-rgb.github.io/news-post-generator/) tries
http://127.0.0.1:8765/trends first on every Generate; when this server is
running, posts come from trends fetched minutes ago instead of the 30-min
GitHub snapshot. When it is not running, the page falls back to news.json.

A background thread rebuilds the dataset every REFRESH_SECONDS so a click
always answers instantly. Note: live mode works in the browser on this PC
(localhost is exempt from mixed-content rules); phones keep the snapshot.
"""

import datetime
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fetch_news as fn  # same module the GitHub Action uses

PORT = 8765
REFRESH_SECONDS = 180  # rebuild cadence in the background

_lock = threading.Lock()
_cache = {"data": None, "at": 0.0}
_building = False


def build() -> dict | None:
    """Return the cached snapshot, or None while the first build is running.
    Only the background thread ever builds — requests never trigger work."""
    global _building
    with _lock:
        if _cache["data"] and time.time() - _cache["at"] < REFRESH_SECONDS:
            return _cache["data"]
        if _building:
            return _cache["data"]  # stale (or None) while a rebuild is in flight
        _building = True

    try:
        print("rebuilding live trend data…", flush=True)
        items = fn.fetch_x_trends() + fn.fetch_trending()
        seen: set[str] = set()
        out: list[dict] = []
        for item in items:
            key = re.sub(r"\W+", "", item["title"].lower())[:80]
            if key and key not in seen:
                seen.add(key)
                item["category"] = "trending"
                out.append(item)
        out.sort(key=lambda i: i["published"], reverse=True)

        data = {
            "updated": datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"),
            "items": out,
            "live": True,
        }
        with _lock:
            _cache["data"], _cache["at"] = data, time.time()
        print(f"live data ready: {len(out)} trending items", flush=True)
        return data
    finally:
        with _lock:
            _building = False


def _bg_loop() -> None:
    while True:
        try:
            build()
        except Exception as e:  # keep serving the last good snapshot
            print("live build failed:", e, flush=True)
        time.sleep(REFRESH_SECONDS)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server API)
        if self.path.startswith("/trends"):
            data = build()
            if not data:  # first build still running — page falls back to snapshot
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass
        elif self.path.startswith("/health"):
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, fmt, *args):  # quieter console
        pass


def main() -> None:
    threading.Thread(target=_bg_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"InkDrop Live on http://127.0.0.1:{PORT}/trends — open the site and tap Generate")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
