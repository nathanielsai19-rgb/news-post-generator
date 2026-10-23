# InkDrop — news to post

A mobile-first page that turns today's hottest headlines into ready-to-paste social posts — hook, story, closer — with a credit line to the outlet (and journalist when the feed provides one), plus a true random fact on every tap.

**Open on your phone:** https://nathanielsai19-rgb.github.io/news-post-generator/ — then use your browser's *Add to Home Screen* to keep it one tap away.

## How it works

- **Tap Generate** → a fresh story is picked from the latest headlines and turned into a three-beat post — **hook** (one short line with the story's most striking real detail), **story** (1-3 plain-English sentences anyone understands in one read; headlinese is translated: "WR" → receiver, "to IR" → goes on injured reserve, "vs." → against), and **closer** (a take or question tied to that specific story that invites replies — never generic). Facts only, nothing invented; a credit block is appended (`— via Outlet, by Author` + link). A true fact (Wikipedia "On this day" or a random article intro) appears below it. Categories: Mixed / World / Tech / Business / Science / **Trending** (the default) — politics and sports stories are never picked (user rule). Trending mixes two sources, both chased into real news stories about each term: **X (Twitter) trends** from the public trends24.in mirror (worldwide + UK — Ghana dropped at the user's request, US dropped because its list is ~70% in-season sports; no account, no login) — these show an "X:" chip on the post card — and **Google Trends** (US + GB). Search noise is filtered out (question headlines, live-score pages, first-person blogs, stories that don't even mention the term, sports-only outlets, sports keywords and fixture patterns in titles, non-English terms and titles). Story picking weights trending topics, big names, and shock value (wild numbers, firsts, bans, leaks) over routine filler, and each AI post gets a random lead angle so no two open the same way.
- **News freshness** — a GitHub Action (`refresh-news.yml`) fetches 23 public RSS feeds (BBC, Guardian, Al Jazeera, TechCrunch, NASA, Google News per category, …) every 30 minutes and commits `news.json`, which this static page reads. Google News feeds update every few minutes, so fresh runs almost always contain stories under an hour old. No API keys, no CORS proxies.
- **Optional AI writing** — open ⚙ Settings and paste [Groq](https://console.groq.com/keys) (free, llama-3.3-70b) and/or [DeepSeek](https://platform.deepseek.com/api_keys) keys. Five writing modes:
  - **Auto** — uses whatever is saved (both keys → Team-up)
  - **Groq only** / **DeepSeek only** — single-model drafts
  - **Team-up** — Groq drafts, DeepSeek critiques and rewrites with the original facts in hand, two writers checking each other
  - **Consensus** — both models act as editors and must agree the post will actually get read: a striking factual hook, a story anyone understands, a closer that earns replies (each scores it 1-10, ≥7 = approve). On disagreement they exchange notes, rewrite, and re-review — capped at 3 review rounds / 2 rewrites, then the best-scored version is shown with a notice. Editor scores appear under the post. A built-in banned-phrases check also regenerates any draft that trips an AI tell ("not just X but Y", "honestly", "thoughts?", bare "This." closers, …).
  Without keys the page uses its own writing style. Keys are stored only in your browser's local storage and never leave your device.
- **Matching GIFs (Giphy)** — add a free [Giphy beta key](https://developers.giphy.com/) in ⚙ and every post also gets a matching GIF. The search query is the trending term when there is one, otherwise the headline's key words, progressively shortened until results come back (a wrong GIF is worse than none). Serious stories (death, violence, crime, disaster) skip GIFs entirely. Buttons: **Copy GIF link** (paste the URL into the X composer and the GIF attaches) and **Another GIF** (re-roll from the most relevant results).
- **Facts are real** — sourced from Wikipedia (on-this-day events / random article intros), with a small built-in fallback list of widely documented facts if offline.

## Files

| Path | Purpose |
| --- | --- |
| `index.html` | the whole app (styles, logic, optional Groq call, video tab) |
| `scripts/fetch_news.py` | RSS collector that writes `news.json` |
| `.github/workflows/refresh-news.yml` | runs the collector every 30 min and commits |
| `news.json` | committed news snapshot the page reads |
| `scripts/generate_video.py` | video pipeline: OpenAI writes the production pack + Blender scene spec (rotating through 8 visual styles), QA battery validates it, local Blender renders it (needs `ALLOW_VIDEO_GEN=true`), then zips a **scene pack** |
| `scripts/build_render.py` | headless Blender script: builds the stylized 3D scene from `scene_spec.json`, animates the camera, saves `scene.blend` + reference stills, renders PNG frames + `keyframes/latest.png` |
| `scene_spec.json` | the latest Blender-ready scene spec (written by the writer) |
| `production.json` | latest production pack + QA report (shown on the video tab) |
| `video.json` | latest video status + scene pack link (shown on the video tab) |
| `scene-packs/*.zip` | scene packs: narration doc with Midjourney prompts, reference images, `scene.blend`, spec |
| `.env` | local secrets (gitignored): `OPENAI_API_KEY`, optional `GROQ_API_KEY` / `DEEPSEEK_API_KEY` |

## Video pipeline (OpenAI writer + local Blender render)

The cloud workflow (`generate-video.yml`, every 4 h) only **writes** the
production pack + scene spec with the ChatGPT API (`OPENAI_API_KEY` repo
secret). Rendering happens **on the local PC** with Blender 5.2 installed:

```bash
ALLOW_VIDEO_GEN=true python scripts/generate_video.py   # full 1280x720 render
```

Blender builds a stylized low-poly scene from the spec (characters, props
from a 22-type catalog incl. skyline/hills, palette, sun/fill lights),
keyframes a tracked camera move, renders a 24 fps PNG sequence, and the
system ffmpeg muxes it to `videos/inkdrop_<ts>.mp4`. A Groq vision model
then critiques real frames and writes a lesson into `video_history.json`.

**Scene packs** — every local render also zips a pack
(`scene-packs/inkdrop_<ts>.zip`, downloadable via the **Scene pack** button
on the site's video tab) containing:

- `pack.md` — scene narration: script, characters, environment & shot, plus
  a ready-to-paste **Midjourney text-to-video prompt** and an
  image-to-video motion prompt
- `opening_frame.png` + `refs/` — reference renders of the scene (front,
  high angle, character close-up) to upload to image-based video AIs
- `scene.blend` — the actual Blender scene, open in Blender 5.2
- `scene_spec.json` — the machine-readable build spec

## Run the refresh manually

```bash
gh workflow run refresh-news.yml
```

Note: GitHub's scheduler sometimes delays `schedule` runs by hours. If the page footer shows old news, trigger the run manually with the command above.
