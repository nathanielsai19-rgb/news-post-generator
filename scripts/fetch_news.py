#!/usr/bin/env python3
"""Fetch fresh news from public RSS feeds and write news.json for InkDrop.

Runs on a GitHub Actions schedule (every 30 min) and commits news.json,
which the static page reads. No API keys, no CORS, no proxies.
"""

import datetime
import html
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser

UA = "InkDrop/1.0 (news collector; +https://github.com/nathanielsai19-rgb/news-post-generator)"

FEEDS = {
    "world": [
        ("BBC News", "http://feeds.bbci.co.uk/news/world/rss.xml"),
        ("The Guardian", "https://www.theguardian.com/world/rss"),
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        ("France 24", "https://www.france24.com/en/rss"),
        ("DW", "https://rss.dw.com/rdf/rss-en-world"),
        ("NPR", "https://feeds.npr.org/1004/rss.xml"),
    ],
    "tech": [
        ("TechCrunch", "https://techcrunch.com/feed/"),
        ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
        ("Engadget", "https://www.engadget.com/rss.xml"),
    ],
    "sports": [
        ("BBC Sport", "http://feeds.bbci.co.uk/sport/rss.xml"),
        ("Sky Sports", "https://www.skysports.com/rss/12040"),
    ],
    "business": [
        ("BBC Business", "http://feeds.bbci.co.uk/news/business/rss.xml"),
        ("The Guardian", "https://www.theguardian.com/uk/business/rss"),
        ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ],
    "science": [
        ("BBC Science", "http://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
        ("NASA", "https://www.nasa.gov/rss/dyn/breaking_news.rss"),
        ("ScienceDaily", "https://www.sciencedaily.com/rss/top/science.xml"),
    ],
}

MAX_AGE_HOURS = 48
PER_CATEGORY = 40
SUMMARY_LEN = 210
MIN_ITEMS = 15  # abort (and keep the previous news.json) if we can't do better

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_text(raw: str, limit: int | None = None) -> str:
    text = html.unescape(TAG_RE.sub(" ", raw or ""))
    text = WS_RE.sub(" ", text).strip()
    if limit and len(text) > limit:
        cut = text[:limit].rsplit(" ", 1)[0]
        text = cut.rstrip(" ,.;:-") + "…"
    return text


def clean_author(raw: str) -> str:
    """Keep the first byline and drop live-blog markers like '(now)'."""
    text = clean_text(raw)
    text = re.sub(r"\([^)]*\)", " ", text)  # "(now)", "(earlier)", "(later)"…
    text = WS_RE.sub(" ", text).strip(" ,;")
    if ";" in text:
        text = text.split(";", 1)[0].strip(" ,")
        text += " et al."
    return text[:60]


def fetch_one(outlet: str, url: str) -> list[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            parsed = feedparser.parse(resp.read())
    except Exception:
        return []

    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for entry in parsed.entries:
        title = clean_text(entry.get("title"))
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue

        ts = entry.get("published_parsed") or entry.get("updated_parsed")
        if not ts:
            continue
        try:
            published = datetime.datetime(*ts[:6], tzinfo=datetime.timezone.utc)
        except (TypeError, ValueError):
            continue
        if published < now - datetime.timedelta(hours=MAX_AGE_HOURS):
            continue
        if published > now + datetime.timedelta(hours=2):
            continue

        out.append(
            {
                "title": title[:220],
                "link": link[:500],
                "summary": clean_text(
                    entry.get("summary") or entry.get("description"), SUMMARY_LEN
                ),
                "author": clean_author(entry.get("author") or parsed.feed.get("author")),
                "outlet": outlet,
                "published": published.isoformat(timespec="seconds").replace("+00:00", "Z"),
            }
        )
    return out


def main() -> None:
    tasks = [
        (category, outlet, url)
        for category, feeds in FEEDS.items()
        for outlet, url in feeds
    ]

    per_category: dict[str, list[dict]] = {}
    seen: set[str] = set()

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(fetch_one, outlet, url): (category, outlet)
            for category, outlet, url in tasks
        }
        for future in as_completed(futures):
            category, outlet = futures[future]
            for item in future.result():
                item["category"] = category
                key = re.sub(r"\W+", "", item["title"].lower())[:80]
                if key and key not in seen:
                    seen.add(key)
                    per_category.setdefault(category, []).append(item)

    items: list[dict] = []
    for category in per_category:
        per_category[category].sort(key=lambda i: i["published"], reverse=True)
        items.extend(per_category[category][:PER_CATEGORY])
    items.sort(key=lambda i: i["published"], reverse=True)

    if len(items) < MIN_ITEMS:
        raise SystemExit(f"only {len(items)} items fetched, keeping previous data")

    payload = {
        "updated": datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "items": items,
    }
    with open("news.json", "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote news.json with {len(items)} items")


if __name__ == "__main__":
    main()
