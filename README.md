# InkDrop — news to post

A mobile-first page that turns today's headlines into ready-to-paste text posts, with a credit line to the outlet (and journalist when the feed provides one), plus a true random fact on every tap.

**Open on your phone:** https://nathanielsai19-rgb.github.io/news-post-generator/ — then use your browser's *Add to Home Screen* to keep it one tap away.

## How it works

- **Tap Generate** → a fresh story is picked from the latest headlines, turned into a short human-sounding post, and a credit block is appended (`— via Outlet, by Author` + link). A true fact (Wikipedia "On this day" or a random article intro) appears below it.
- **News freshness** — a GitHub Action (`refresh-news.yml`) fetches 18 public RSS feeds (BBC, Guardian, Al Jazeera, TechCrunch, NASA, …) every 30 minutes and commits `news.json`, which this static page reads. No API keys, no CORS proxies.
- **Optional AI writing** — open ⚙ Settings and paste a free [Groq API key](https://console.groq.com/keys) (llama-3.3-70b). Without it the page uses its own varied writing styles. The key is stored only in your browser's local storage and never leaves your device.
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
