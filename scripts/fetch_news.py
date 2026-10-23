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
TRENDING_CAP = 60

# X (Twitter) trending topics from the public trends24.in mirror — no account,
# no API keys. English-speaking regions only: Ghana (the user's country), US,
# and UK. (Worldwide mixes in too many non-English trends.)
X_TREND_REGIONS = [
    ("ghana", "https://trends24.in/ghana/"),
    ("united-states", "https://trends24.in/united-states/"),
    ("united-kingdom", "https://trends24.in/united-kingdom/"),
]
MAX_X_TRENDS_PER_REGION = 8
X_TREND_LINK_RE = re.compile(r"class=trend-link>([^<]+)</a>")

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
    r"first alert",        # local-TV weather filler ("First Alert forecast here")
    r"fantasy football",   # niche alert-jargon headlines
    r"waiver wire",
    r"tells us everything",  # interview-teaser headlines
    r"prediction, picks",  # betting-preview pages
    r"predictions, teams",
    r"stats, injuries",    # game stats pages
    r"live updates",       # liveblog labels
    r"match (wta|atp)",    # tennis match-listing pages
    r"&",                  # multi-subject list titles ("A & B Become …")
]
TREND_TITLE_PREFIX = re.compile(
    r"^(look|watch|just in|today in sports|update)\b[:,\- ]*", re.I
)
# Generic content words in a trend force exact-phrase matching: the trend
# "Black Challenge" must appear as the phrase, not just the word "challenge".
TREND_STOPWORDS = {"the", "and", "for", "with", "vs", "news",
                   "this", "that", "week", "today", "tonight",
                   "black", "white", "challenge", "open", "day", "days", "sale",
                   "sales", "festival", "show", "final", "live", "update",
                   "updates", "new", "watch", "full", "latest", "break",
                   "street", "house", "home", "star", "stars", "top", "best",
                   "big", "small", "high", "free"}
TREND_MIN_TITLE_WORDS = 5

# Stories from sports-only outlets are sports no matter what the trend term is.
SPORTS_OUTLET_RE = re.compile(
    r"espn|bleacher|sportskeeda|sky sports|talksport|\bgoal\.com\b|nba\.com|nfl\.com|"
    r"mlb\.com|nhl\.com|essentiallysports|the athletic|sporting news|soccernet|"
    r"courtside|slamonline|hoopshype|90min|football london|caughtoffside|teamtalk|"
    r"fourfourtwo|transfermarkt|goalball|sports mole|marca|diario as\b|kicker\b",
    re.I,
)

# Posts must be English. Non-Latin scripts are dropped outright; Latin-script
# non-English text is caught by its function words (two or more hits).
NON_LATIN_RE = re.compile(
    r"[Ͱ-ϿЀ-ӿԀ-ԯ֐-׿؀-ۿ܀-ݏ"
    r"ऀ-ॿ฀-๿ᄀ-ᇿ぀-ヿ㐀-鿿가-힯]"
)
NON_ENGLISH_WORD_RE = re.compile(
    r"\b(?:el|los|las|del|dos|das|una|para|como|não|nao|voce|você|hoy|mañana|les|des|"
    r"une|pour|avec|dans|sont|mais|que|qui|der|das|und|für|fur|mit|von|sich|nicht|"
    r"ist|sind|wird|werden|nach|über|uber|gli|della|delle|questa|questo|quando)\b",
    re.I,
)


def is_non_english(text: str) -> bool:
    if NON_LATIN_RE.search(text):
        return True
    hits = set(m.group(0).lower() for m in NON_ENGLISH_WORD_RE.finditer(text))
    return len(hits) >= 2

# Trending terms that are sports or politics are never chased (user rule:
# no politics, no sports posts).
TREND_BLOCK_RE = re.compile(
    r"mlb|nfl|nba|nhl|soccer|football|rugby|basketball|baseball|hockey|playoff|"
    r"league|derby|tournament|tennis|nascar|\bf1\b|formula|golf|boxing|ufc|wwe|"
    r"fifa|uefa|olympics|manager|\bfc\b|cavs|cavaliers|knicks|lakers|celtics|"
    r"mavericks|pelicans|grizzlies|hornets|pacers|pistons|nuggets|clippers|"
    r"timberwolves|\bbucks\b|raptors|76ers|sixers|bournemouth|everton|fulham|"
    r"brentford|newcastle|west ham|aston villa|burnley|southampton|brighton|"
    r"manchester|arsenal|chelsea|liverpool|madrid|barcelona|juventus|bayern|"
    r"dodgers|yankees|brewers|warriors|maxey|harden|hearts of oak|kotoko|"
    r"man utd|man united|red devils|bwfc|bolton|wanderers|"
    r"trump|biden|president|senate|congress|"
    r"election|government|minister|parliament|policy|white house|\bwar\b|"
    r"missile|military|troops|ukraine|russia|iran|israel|gaza|hamas|putin|"
    r"zelensky|nato|sanctions|tariff|ceasefire|invasion|regime",
    re.I,
)


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


def trend_stories(term: str, src: str) -> list[dict]:
    """News stories about one trending term (src = 'google' or 'x')."""
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
        if is_non_english(title):
            continue
        if not trend_overlaps(term, title):
            continue
        outlet = gn_item_outlet(entry, "Google News")
        if SPORTS_OUTLET_RE.search(outlet):
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
            "outlet": outlet,
            "trend": term,
            "trend_src": src,
            "published": published.isoformat(timespec="seconds").replace("+00:00", "Z"),
        })
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
            if not term or TREND_BLOCK_RE.search(term) or is_non_english(term):
                continue
            if term.lower() in {t.lower() for t in terms}:
                continue
            terms.append(term)
            if len(terms) >= MAX_TRENDS_PER_GEO * len(TRENDS_GEO):
                break
        if len(terms) >= MAX_TRENDS_PER_GEO * len(TRENDS_GEO):
            break
    if not terms:
        return []

    items: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for future in as_completed([pool.submit(trend_stories, t, "google") for t in terms]):
            items.extend(future.result())
    return items


def fetch_x_trends() -> list[dict]:
    """Today's X (Twitter) trending topics -> news stories about each term."""
    terms: list[str] = []
    for _geo, url in X_TREND_REGIONS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as resp:
                page = resp.read().decode("utf-8", "ignore")
        except Exception:
            continue
        for m in X_TREND_LINK_RE.findall(page):
            term = clean_text(m).lstrip("#").strip()
            if len(term) < 3 or TREND_BLOCK_RE.search(term) or is_non_english(term):
                continue
            if term.lower() in {t.lower() for t in terms}:
                continue
            terms.append(term)
            if len(terms) >= MAX_X_TRENDS_PER_REGION * len(X_TREND_REGIONS):
                break
        if len(terms) >= MAX_X_TRENDS_PER_REGION * len(X_TREND_REGIONS):
            break
    if not terms:
        return []

    items: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for future in as_completed([pool.submit(trend_stories, t, "x") for t in terms]):
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

    # Trending: chase today's Google Trends + X trending topics into news stories.
    for src_fn in (fetch_trending, fetch_x_trends):
        for item in src_fn():
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
