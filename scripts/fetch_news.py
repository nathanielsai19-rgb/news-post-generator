#!/usr/bin/env python3
"""Fetch fresh news from public RSS feeds and write news.json for InkDrop.

Runs on a GitHub Actions schedule (every 30 min) and commits news.json,
which the static page reads. No API keys, no CORS, no proxies.
"""

import datetime
import html
import json
import re
import urllib.parse
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
        ("Google News", "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en"),
    ],
    "tech": [
        ("TechCrunch", "https://techcrunch.com/feed/"),
        ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
        ("Engadget", "https://www.engadget.com/rss.xml"),
        ("Google News", "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=en-US&gl=US&ceid=US:en"),
    ],
    "sports": [
        ("BBC Sport", "http://feeds.bbci.co.uk/sport/rss.xml"),
        ("Sky Sports", "https://www.skysports.com/rss/12040"),
        ("Google News", "https://news.google.com/rss/headlines/section/topic/SPORTS?hl=en-US&gl=US&ceid=US:en"),
    ],
    "business": [
        ("BBC Business", "http://feeds.bbci.co.uk/news/business/rss.xml"),
        ("The Guardian", "https://www.theguardian.com/uk/business/rss"),
        ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
        ("Google News", "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en"),
    ],
    "science": [
        ("BBC Science", "http://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
        ("NASA", "https://www.nasa.gov/rss/dyn/breaking_news.rss"),
        ("ScienceDaily", "https://www.sciencedaily.com/rss/top/science.xml"),
        ("Google News", "https://news.google.com/rss/headlines/section/topic/SCIENCE?hl=en-US&gl=US&ceid=US:en"),
    ],
}

MAX_AGE_HOURS = 48
PER_CATEGORY = 40
SUMMARY_LEN = 210
MIN_ITEMS = 15  # abort (and keep the previous news.json) if we can't do better

# "Trending" category: chase today's trending Google searches into news stories.
TRENDS_URL = "https://trends.google.com/trending/rss?geo=US"
TREND_NEWS_URL = "https://news.google.com/rss/search?q={}&hl=en-US&gl=US&ceid=US:en"
MAX_TRENDS = 8      # how many trending terms to chase
PER_TREND = 4       # stories to keep per term
MAX_TREND_AGE_HOURS = 24
TRENDING_CAP = 30

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


def gn_item_outlet(entry, fallback: str) -> str:
    """Google News carries the real publisher in <source> — prefer it for credit."""
    src = entry.get("source") or {}
    src_title = clean_text(src.get("title", ""), 60)
    return src_title or fallback


def gn_item_title(title: str, outlet: str) -> str:
    """Google News titles end with ' - Publisher'; strip the suffix."""
    if outlet == "Google News" and " - " in title:
        return title.rsplit(" - ", 1)[0].rstrip()[:220]
    return title[:220]


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
                "title": gn_item_title(title, outlet),
                "link": link[:500],
                "summary": "" if outlet == "Google News" else clean_text(
                    entry.get("summary") or entry.get("description"), SUMMARY_LEN
                ),
                "author": clean_author(entry.get("author") or parsed.feed.get("author")),
                "outlet": gn_item_outlet(entry, outlet),
                "published": published.isoformat(timespec="seconds").replace("+00:00", "Z"),
            }
        )
    return out


def fetch_trending() -> list[dict]:
    """Today's trending Google searches -> news stories about each term."""
    try:
        req = urllib.request.Request(TRENDS_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            parsed = feedparser.parse(resp.read())
    except Exception:
        return []
    terms = [clean_text(e.get("title")) for e in parsed.entries]
    terms = [t for t in terms if t][:MAX_TRENDS]
    if not terms:
        return []

    def one_term(term: str) -> list[dict]:
        try:
            req = urllib.request.Request(
                TREND_NEWS_URL.format(urllib.parse.quote(term)), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as resp:
                fd = feedparser.parse(resp.read())
        except Exception:
            return []
        now = datetime.datetime.now(datetime.timezone.utc)
        out = []
        for entry in fd.entries[:PER_TREND]:
            title = gn_item_title(clean_text(entry.get("title")), "Google News")
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
            if published < now - datetime.timedelta(hours=MAX_TREND_AGE_HOURS):
                continue
            if published > now + datetime.timedelta(hours=2):
                continue
            out.append({
                "title": title,
                "link": link[:500],
                "summary": "",
                "author": "",
                "outlet": gn_item_outlet(entry, "Google News"),
                "trend": term,
                "published": published.isoformat(timespec="seconds").replace("+00:00", "Z"),
            })
        return out

    items: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for future in as_completed([pool.submit(one_term, t) for t in terms]):
            items.extend(future.result())
    return items


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

    # Trending: chase today's Google Trends terms into news stories.
    for item in fetch_trending():
        key = re.sub(r"\W+", "", item["title"].lower())[:80]
        if key and key not in seen:
            seen.add(key)
            item["category"] = "trending"
            per_category.setdefault("trending", []).append(item)
    if "trending" in per_category:
        per_category["trending"].sort(key=lambda i: i["published"], reverse=True)
        items.extend(per_category["trending"][:TRENDING_CAP])

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
