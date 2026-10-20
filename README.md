# InkDrop — news to post

A mobile-first page that turns today's headlines into ready-to-paste text posts, with a credit line to the outlet (and journalist when the feed provides one), plus a true random fact on every tap.

**Open on your phone:** https://nathanielsai19-rgb.github.io/news-post-generator/ — then use your browser's *Add to Home Screen* to keep it one tap away.

## How it works

- **Tap Generate** → a fresh story is picked from the latest headlines and turned into one plain-English news sentence — *"[Who] [says/declares/reportedly] [what they said]"* or *"[Who/what] [verb] [what happened]"* — a reader with no background in the topic understands it in one read: headlinese is translated ("WR" → receiver, "to IR" → goes on injured reserve, "vs." → against), feed labels stripped, no commentary, no questions. A credit block is appended (`— via Outlet, by Author` + link). A true fact (Wikipedia "On this day" or a random article intro) appears below it. Categories: Mixed / World / Tech / Sports / Business / Science / **Trending** — Trending chases today's Google Trends search terms into real news stories about each term, with search noise filtered out (question headlines, live-score pages, first-person blogs, stories that don't even mention the term).
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
| `index.html` | the whole app (styles, logic, optional Groq call, video tab) |
| `scripts/fetch_news.py` | RSS collector that writes `news.json` |
| `.github/workflows/refresh-news.yml` | runs the collector every 30 min and commits |
| `news.json` | committed news snapshot the page reads |
| `scripts/generate_video.py` | video pipeline: OpenAI writes the production pack + Blender scene spec, QA battery validates it, then the local Blender renders it (needs `ALLOW_VIDEO_GEN=true`) |
| `scripts/build_render.py` | headless Blender script: builds the stylized 3D scene from `scene_spec.json`, animates the camera, renders PNG frames + `keyframes/latest.png` |
| `scene_spec.json` | the latest Blender-ready scene spec (written by the writer) |
| `production.json` | latest production pack + QA report (shown on the video tab) |
| `video.json` | latest video status (shown on the video tab) |
| `.env` | local secrets (gitignored): `OPENAI_API_KEY`, optional `GROQ_API_KEY` / `DEEPSEEK_API_KEY` |

## Video pipeline (OpenAI writer + local Blender render)

The cloud workflow (`generate-video.yml`, every 4 h) only **writes** the
production pack + scene spec with the ChatGPT API (`OPENAI_API_KEY` repo
secret). Rendering happens **on the local PC** with Blender 5.2 installed:

```bash
ALLOW_VIDEO_GEN=true python scripts/generate_video.py   # full 1280x720 render
```

Blender builds a stylized low-poly scene from the spec (characters, props
from a fixed catalog, palette, sun/fill lights), keyframes a tracked camera
move, renders a 24 fps PNG sequence, and the system ffmpeg muxes it to
`videos/inkdrop_<ts>.mp4`. A Groq vision model then critiques real frames
and writes a lesson into `video_history.json` for the next production.

## Run the refresh manually

```bash
gh workflow run refresh-news.yml
```

Note: GitHub's scheduler sometimes delays `schedule` runs by hours. If the page footer shows old news, trigger the run manually with the command above.
