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
import os
import random
import re
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fetch_news as fn  # same module the GitHub Action uses

PORT = 8765
REFRESH_SECONDS = 180  # rebuild cadence in the background

# AI writing (optional): a Groq or DeepSeek key, read at runtime from a
# gitignored local file or the env — never printed, never committed.
GROQ_KEY_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_secrets.json"),
    r"C:\Users\natha\media-replier\groq_key.txt",
    r"C:/Users/natha/media-replier/groq_key.txt",
]


def _load_groq_key() -> str:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        return key
    for path in GROQ_KEY_PATHS:
        try:
            if path.endswith(".json"):
                with open(path, encoding="utf-8") as fh:
                    key = json.load(fh).get("groq_key", "").strip()
            else:
                with open(path, encoding="utf-8") as fh:
                    key = fh.read().strip()
            if key:
                return key
        except (OSError, ValueError):
            continue
    return ""


GROQ_KEY = _load_groq_key()

# Same three-beat post contract the page uses for its AI writers.
SYSTEM_PROMPT = (
    "You write short news posts for a social feed — posts people actually stop to read. You get one news item. "
    "Your job: someone who knows nothing about the topic should want to read the whole post, understand the story "
    "in one read, and come away with something worth repeating to a friend.\n\n"
    "Write three parts separated by empty lines — do not label them, just write them:\n\n"
    "1. HOOK: one short line, 3 to 9 words. The single most striking thing in the item — the wildest number, the "
    "sharpest contrast, or the detail that makes a scroller stop. Factual, from the item only. Never clickbait "
    "phrasing and never a label like BREAKING.\n\n"
    "2. BODY: one to three short sentences in plain spoken English. Say WHO with a plain-word description of what "
    "they are (\"Cavs star Donovan Mitchell\", never just \"Mitchell\"), WHAT happened, and the context or detail "
    "that makes it land (\"nearly two years after a devastating knee injury\"). A reader with zero background must "
    "understand it in one read: every name, team, company and term is introduced in plain words. Translate "
    "all headlinese into normal speech (\"WR\" becomes \"receiver\", \"to IR\" becomes \"goes on injured reserve\", "
    "\"vs.\" becomes \"against\"), expand abbreviations the item makes clear, strip feed labels. If the item names "
    "a trending topic, the post must still be self-explanatory to someone who has never heard of that topic.\n\n"
    "3. CLOSER: one line, specific to this story — a real point of view or a real question aimed at the reader that "
    "invites replies. It must only make sense for THIS story, so it names a detail from the item. Never generic "
    "(\"what do you think\", \"thoughts?\", \"let that sink in\").\n\n"
    "Hard rules: facts from the item only — never invent numbers, quotes or names; the closer may have attitude "
    "but never adds new facts, and on serious stories (death, violence, crime, disaster) it is respectful with "
    "zero jokes or flippancy; no emojis; no hashtags; no em-dashes or semicolons; no clichés or AI-sounding "
    "formulas (\"not just X but Y\", \"in a world where\", \"here's the thing\", \"game-changer\", \"unhinged\", "
    "\"honestly\"); uneven sentence lengths; a text-a-friend register, not a headline register. If a voice "
    "reference is given, borrow its wording, energy and rhythm — never its opinions or facts. "
    "Return only those three parts, nothing else."
)

HOOK_ANGLES = [
    "Lead with the wildest number or stat in the item.",
    "Lead with the sharpest contrast in the story.",
    "Lead with the most surprising detail in the item.",
    "Lead with the specific moment from the story that is hardest to believe but true.",
]

BANNED_TELLS = [
    re.compile(r"\bnot just\b[^.!?\n]{0,60}\bbut\b", re.I),
    re.compile(r"let that sink in", re.I),
    re.compile(r"\bin a world where\b", re.I),
    re.compile(r"here'?s the thing", re.I),
    re.compile(r"\bgame-?changer\b", re.I),
    re.compile(r"\bunhinged\b", re.I),
    re.compile(r"\bhonestly\b", re.I),
    re.compile(r"\bsay what you want\b", re.I),
    re.compile(r"\byou won'?t believe\b", re.I),
    re.compile(r"\bcouldn'?t make this up\b", re.I),
    re.compile(r"\bwait for it\b", re.I),
    re.compile(r"\bbuckle up\b", re.I),
    re.compile(r"\bhold my beer\b", re.I),
    re.compile(r"\bmic drop\b", re.I),
    re.compile(r"\bcrazy, right\?", re.I),
    re.compile(r"\bthoughts\?\s*$", re.I),
    re.compile(r"\bwhat do you think\?", re.I),
    re.compile(r"^(this|wild)\.\s*$", re.I | re.M),
]


def _groq_chat(system: str, user: str) -> str | None:
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps({
            "model": "llama-3.3-70b-versatile",
            "temperature": 0.9,
            "max_tokens": 380,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + GROQ_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        text = (data["choices"][0]["message"]["content"] or "").strip()
        return text or None
    except Exception as e:
        print("groq:", e, flush=True)
        return None


def _build_user(story: dict, angle: str) -> str:
    parts = []
    if story.get("outlet"):
        parts.append("Outlet: " + str(story["outlet"]))
    parts.append("Headline: " + str(story.get("title", "")))
    if story.get("trend"):
        parts.append('Trending topic: "%s" — people are searching this right now' % story["trend"])
    if story.get("summary"):
        parts.append("Summary: " + str(story["summary"]))
    if story.get("ctx"):
        parts.append("Background: " + str(story["ctx"]))
    if story.get("voice"):
        lines = [l for l in str(story["voice"]).split("\n") if l.strip()][:4]
        if lines:
            parts.append("The account's voice — imitate this energy, rhythm and personality:\n"
                         + "\n".join("- " + l for l in lines))
    return "\n".join(parts) + "\n\nWrite the post:\n" + angle


def ai_write(story: dict) -> str | None:
    """Three-beat post for a story, with banned-tell regeneration (max 2)."""
    if not GROQ_KEY:
        return None
    angle = str(story.get("angle") or random.choice(HOOK_ANGLES))
    base = _build_user(story, angle)
    note = ""
    for _ in range(3):
        text = _groq_chat(SYSTEM_PROMPT, base + (("\n" + note) if note else ""))
        if not text:
            return None
        bad = next((r.search(text).group(0) for r in BANNED_TELLS if r.search(text)), None)
        if not bad:
            return text
        note = 'Don\'t use the formulaic phrase "%s".' % bad
    return None

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
        elif self.path.startswith("/aiwrite") and self.command == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                story = json.loads(self.rfile.read(length) or b"{}")
                text = ai_write(story)
                if not text:
                    body = b'{"text":null}'
                    self.send_response(204)
                else:
                    body = json.dumps({"text": text}).encode("utf-8")
                    self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                    pass
            except Exception as e:
                print("aiwrite error:", e, flush=True)
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):  # noqa: N802
        self.do_GET()

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
