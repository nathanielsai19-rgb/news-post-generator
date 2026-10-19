# InkDrop — news to post

A mobile-first page that turns today's headlines into ready-to-paste text posts, with a credit line to the outlet (and journalist when the feed provides one), plus a true random fact on every tap.

**Open on your phone:** https://nathanielsai19-rgb.github.io/news-post-generator/ — then use your browser's *Add to Home Screen* to keep it one tap away.

## How it works

- **Tap Generate** → a fresh story is picked from the latest headlines and turned into one declarative news sentence — *"[Who] [says/declares/reportedly] [what they said]"* or *"[Who/what] [verb] [what happened]"* — no commentary, no questions. A credit block is appended (`— via Outlet, by Author` + link). A true fact (Wikipedia "On this day" or a random article intro) appears below it. Categories: Mixed / World / Tech / Sports / Business / Science / **Trending** — Trending chases today's Google Trends search terms into real news stories about each term, with search noise filtered out (question headlines, live-score pages, first-person blogs, stories that don't even mention the term).
- **News freshness** — a GitHub Action (`refresh-news.yml`) fetches 23 public RSS feeds (BBC, Guardian, Al Jazeera, TechCrunch, NASA, Google News per category, …) every 30 minutes and commits `news.json`, which this static page reads. Google News feeds update every few minutes, so fresh runs almost always contain stories under an hour old. No API keys, no CORS proxies.
- **Optional AI writing** — open ⚙ Settings and paste [Groq](https://console.groq.com/keys) (free, llama-3.3-70b) and/or [DeepSeek](https://platform.deepseek.com/api_keys) keys. Five writing modes:
  - **Auto** — uses whatever is saved (both keys → Team-up)
  - **Groq only** / **DeepSeek only** — single-model drafts
  - **Team-up** — Groq drafts, DeepSeek critiques and rewrites with the original facts in hand, two writers checking each other
  - **Consensus** — both models act as editors and must agree the post is one clean factual news sentence (each scores it 1-10, ≥7 = approve). On disagreement they exchange notes, rewrite, and re-review — capped at 3 review rounds / 2 rewrites, then the best-scored version is shown with a notice. Editor scores appear under the post.
  Without keys the page uses its own writing style. Keys are stored only in your browser's local storage and never leave your device.
- **Facts are real** — sourced from Wikipedia (on-this-day events / random article intros), with a small built-in fallback list of widely documented facts if offline.

## Files

| Path | Purpose |
| --- | --- |
| `index.html` | the whole app (styles, logic, optional Groq call) |
| `scripts/fetch_news.py` | RSS collector that writes `news.json` |
| `.github/workflows/refresh-news.yml` | runs the collector every 30 min and commits |
| `news.json` | committed news snapshot the page reads |

## Run the refresh manually

```bash
gh workflow run refresh-news.yml
```

Note: GitHub's scheduler sometimes delays `schedule` runs by hours. If the page footer shows old news, trigger the run manually with the command above.
