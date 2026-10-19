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
TRENDS_GEO = ["US", "GB"]  # two regions for topic variety
TRENDS_URL = "https://trends.google.com/trending/rss?geo={}"
TREND_NEWS_URL = "https://news.google.com/rss/search?q={}&hl=en-US&gl=US&ceid=US:en"
MAX_TRENDS_PER_GEO = 8
PER_TREND = 3       # stories to keep per term
MAX_TREND_AGE_HOURS = 24
TRENDING_CAP = 45

# Google News search results include pages that are not news stories. Titles
# matching any of these would produce nonsense posts, so they are dropped.
GN_TITLE_JUNK = [
    r"live scores?",
    r"how to watch",
    r"what (channel|time|day|week)",
    r"odds, picks",
    r"\?",                 # question headlines ("What channel is … tonight?")
    r"^(i|we|my|our)\b",   # first-person blog/opinion pieces
    r"^(what|why|where|when|who|which|is|are|will|can|should|could|does|did)\b",  # question/explainer forms
    r"alert:",             # feed-alert labels ("Fantasy Football Injury Alert: …")
    r"fantasy football",   # niche alert-jargon headlines
    r"waiver wire",
    r"tells us everything",  # interview-teaser headlines
    r"prediction, picks",  # betting-preview pages
    r"stats, injuries",    # game stats pages
    r"\b&\b",              # multi-subject list titles ("A & B Become …")
]
TREND_TITLE_PREFIX = re.compile(
    r"^(look|watch|just in|today in sports|update)\b[:,\- ]*", re.I
)
TREND_STOPWORDS = {"the", "and", "for", "with", "vs", "news",
                   "this", "that", "week", "today", "tonight"}
TREND_MIN_TITLE_WORDS = 5


def gn_title_is_junk(title: str) -> bool:
    return any(re.search(p, title, re.I) for p in GN_TITLE_JUNK)


def trend_overlaps(trend: str, title: str) -> bool:
    """True if the title mentions at least one significant word of the trend term."""
    words = [w for w in re.findall(r"\w+", trend.lower())
             if w not in TREND_STOPWORDS and len(w) >= 3]
    if not words:
        words = [trend.lower().strip()]
    title_l = title.lower()
    return any(
        re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", title_l)
        for w in words
    )

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
    """Google News titles carry the publisher suffix (' - Publisher', '| Publisher'); strip it."""
    for sep in (" - ", " | ", " – ", " — "):
        if sep in title:
            head, tail = title.rsplit(sep, 1)
            tail_words = tail.strip().split()
            short_clean_tail = len(tail_words) <= 3 and not re.search(r"\d", tail)
            if (outlet == "Google News" and short_clean_tail) \
                    or tail.strip().lower() == outlet.lower():
                return head.rstrip()[:220]
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
        if outlet == "Google News" and gn_title_is_junk(title):
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
    """Today's trending Google searches (US + GB) -> news stories about each term."""
    terms: list[str] = []
    for geo in TRENDS_GEO:
        try:
            req = urllib.request.Request(TRENDS_URL.format(geo), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as resp:
                parsed = feedparser.parse(resp.read())
        except Exception:
            continue
        for entry in parsed.entries:
            term = clean_text(entry.get("title"))
            if not term or term.lower() in {t.lower() for t in terms}:
                continue
            terms.append(term)
            if len(terms) >= MAX_TRENDS_PER_GEO * len(TRENDS_GEO):
                break
        if len(terms) >= MAX_TRENDS_PER_GEO * len(TRENDS_GEO):
            break
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
            title = TREND_TITLE_PREFIX.sub("", title).strip()
            link = (entry.get("link") or "").strip()
            if not title or not link:
                continue
            if len(title.split()) < TREND_MIN_TITLE_WORDS or gn_title_is_junk(title):
                continue
            if not trend_overlaps(term, title):
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
