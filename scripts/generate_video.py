#!/usr/bin/env python3
"""InkDrop video worker: news story -> cinematic LTX text-to-video -> committed mp4.

Runs in GitHub Actions (dispatchable, gated by the ALLOW_VIDEO_GEN secret).
With Groq/DeepSeek secrets present the prompt is written by the two-writer
team and self-critiqued after each generation; otherwise a rotating
cinematic template pool is used. video_history.json records prompts and
critiques so every generation learns from the previous ones.
"""

import datetime
import json
import os
import random
import time
import urllib.request

LTX = "https://api.ltx.io/v2/text-to-video"
MODEL = os.environ.get("VIDEO_MODEL", "ltx-2-3-fast")
DURATION = int(os.environ.get("VIDEO_DURATION", "5"))
RESOLUTION = os.environ.get("VIDEO_RESOLUTION", "1280x720")
FPS = 24
POLL_SECONDS = 10
POLL_TIMEOUT = 480
KEEP_VIDEOS = 3

# Cinematic building blocks; the composer rotates combinations so videos
# don't repeat each other's look.
STYLE_POOL = [
    "Photorealistic, shot on 35mm film with shallow depth of field and anamorphic lens flare",
    "Ultra-detailed cinematic realism, volumetric light through dust and haze, 8K texture",
    "Moody neo-noir: hard rim light, deep shadows, wet asphalt reflections, teal-and-amber grade",
    "Golden-hour documentary realism, natural skin texture, hand-held energy with subtle shake",
    "High-contrast thriller look, slow dolly, dense atmosphere, foreground bokeh",
    "Dreamlike realism: soft morning fog, sun shafts, silky highlights, gentle grain",
    "Gritty photoreal detail: close-up macro textures, sweat, weathered materials, harsh key light",
    "Cinematic low-key lighting, candlelight and neon mixed, long shadows, film noir framing",
]
CHARACTER_RULES = [
    "Every character shows surprising detail: visible pores and skin texture, natural micro-expressions, realistic fabric and hair movement",
    "The central figure feels lived-in: wrinkled clothing, precise eye lines, weight in every gesture",
    "Characters act with restrained tension - small tells (a jaw clench, a glance away) sell the emotion",
]
MOMENT_RULES = [
    "Capture the exact second something shifts - the beat before a reveal, not after",
    "One clear dramatic beat: a decision, a discovery, a confrontation - never a static establishing shot",
    "End on an open loop: a look, a sound off-screen, a door opening - something that makes viewers rewatch",
]
CAMERA_MOVES = ["dolly_in", "jib_up", "focus_shift"]

HISTORY_PATH = "video_history.json"


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


def http(method, url, key=None, payload=None, timeout=30):
    req = urllib.request.Request(url, method=method)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data, timeout=timeout) as resp:
        raw = resp.read()
        ctype = resp.headers.get("Content-Type", "")
        if "json" in ctype:
            return json.loads(raw.decode() or "{}")
        return raw


def pick_story():
    news = load_json("news.json", {"items": []})
    items = sorted(news.get("items", []),
                   key=lambda i: i.get("published", ""), reverse=True)
    trending = [i for i in items if i.get("category") == "trending"]
    return (trending or items or [None])[0]


def story_text(story):
    if not story:
        return "A lone lighthouse keeper spots a ship appearing out of the fog at night"
    parts = [story.get("title", "")]
    if story.get("trend"):
        parts.append("Trending: " + story["trend"])
    if story.get("summary"):
        parts.append(story["summary"])
    return ". ".join(p for p in parts if p)


def llm_chat(base, key, model, messages, temperature=0.9, max_tokens=400):
    payload = {"model": model, "temperature": temperature,
               "max_tokens": max_tokens, "messages": messages}
    data = http("POST", base + "/chat/completions", key=key, payload=payload)
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()


def composer_prompt(story, history):
    """LLM two-writer prompt composer when keys exist, else template pool."""
    groq_key = os.environ.get("GROQ_API_KEY")
    ds_key = os.environ.get("DEEPSEEK_API_KEY")
    past = history.get("prompts", [])[-3:]
    past_note = "\n".join(
        f"- prompt: {p['prompt']}\n  lesson: {p.get('critique') or 'none'}"
        for p in past if p.get("critique")
    )
    base_ask = (
        "Write ONE text-to-video prompt for an LTX video generator about this story:\n"
        f"{story_text(story)}\n\n"
        "Requirements: photorealistic, not boring - a single interesting scene with a moment "
        "of tension or surprise; surprisingly detailed characters (skin texture, natural "
        "expressions, lived-in clothing); explicit camera movement and lighting; 40-90 words; "
        "no text overlays, no watermarks, no logos."
    )
    if past_note:
        base_ask += "\n\nLearn from the past - do not repeat these weaknesses:\n" + past_note
    system = ("You are a film director writing prompts for a photorealistic AI video model. "
              "Reply with the prompt text only.")
    draft = None
    if groq_key:
        for model in ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"):
            try:
                draft = llm_chat("https://api.groq.com/openai/v1", groq_key, model,
                                 [{"role": "system", "content": system},
                                  {"role": "user", "content": base_ask}])
                if draft:
                    break
            except Exception:
                continue
    if not draft and ds_key:
        try:
            draft = llm_chat("https://api.deepseek.com", ds_key,
                             os.environ.get("VIDEO_DS_MODEL", "deepseek-flash"),
                             [{"role": "system", "content": system},
                              {"role": "user", "content": base_ask}])
        except Exception:
            draft = None
    if draft and ds_key:
        try:
            improved = llm_chat(
                "https://api.deepseek.com", ds_key,
                os.environ.get("VIDEO_DS_MODEL", "deepseek-flash"),
                [{"role": "system", "content":
                    "You are the final writer on a two-director team. Improve the video prompt "
                    "your teammate drafted: keep it photorealistic with a moment of tension, "
                    "detailed characters, explicit camera and lighting, 40-90 words. "
                    "Reply with the prompt text only."},
                 {"role": "user", "content": draft}])
            if improved:
                draft = improved
        except Exception:
            pass
    if draft:
        return draft

    # Template pool with rotation: avoid the style/moment combos used recently.
    used = {(p.get("style"), p.get("moment")) for p in past if p.get("style")}
    options = [(s, m) for s in STYLE_POOL for m in MOMENT_RULES if (s, m) not in used]
    if options:
        style, moment = random.choice(options)
    else:
        style, moment = random.choice([(s, MOMENT_RULES[0]) for s in STYLE_POOL])
    prompt = (
        f"{style}. {moment} {random.choice(CHARACTER_RULES)}. "
        f"The scene: {story_text(story)}. "
        f"Camera: {random.choice(CAMERA_MOVES)}. "
        "No text overlays, no watermarks, no logos."
    )
    return prompt, style, moment


def self_critique(prompt):
    ds_key = os.environ.get("DEEPSEEK_API_KEY")
    if not ds_key:
        return None
    try:
        return llm_chat(
            "https://api.deepseek.com", ds_key,
            os.environ.get("VIDEO_DS_MODEL", "deepseek-flash"),
            [{"role": "system", "content":
                "You review AI video prompts. After seeing the result in your mind, write ONE "
                "sentence lesson for the next prompt: what to keep, what to change. Be specific."},
             {"role": "user", "content": prompt}],
            temperature=0.4, max_tokens=120)
    except Exception:
        return None


def submit(payload):
    key = os.environ["LTX_API_KEY"]
    return http("POST", LTX, key=key, payload=payload, timeout=60)


def poll(job_id):
    key = os.environ["LTX_API_KEY"]
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        try:
            job = http("GET", f"{LTX}/{job_id}", key=key)
        except Exception as e:
            print("poll error:", e)
            time.sleep(POLL_SECONDS)
            continue
        status = job.get("status")
        print("job status:", status)
        if status in ("completed", "failed", "error", "cancelled"):
            return job
        time.sleep(POLL_SECONDS)
    return {"status": "timeout"}


def download(url, path):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=120) as resp, open(path, "wb") as fh:
        fh.write(resp.read())


def prune_videos(keep=KEEP_VIDEOS):
    import glob
    files = sorted(glob.glob("videos/*.mp4"))
    for old in files[:-keep]:
        os.remove(old)
        print("pruned", old)


def main():
    allow = os.environ.get("ALLOW_VIDEO_GEN", "false") == "true"
    story = pick_story()
    history = load_json(HISTORY_PATH, {"prompts": []})
    composed = composer_prompt(story, history)
    if isinstance(composed, tuple):
        prompt, style, moment = composed
    else:
        prompt, style, moment = composed, None, None

    base = {
        "title": story.get("title") if story else "Original scene",
        "outlet": story.get("outlet") if story else "",
        "story_link": story.get("link") if story else "",
        "trend": story.get("trend") if story else "",
        "prompt": prompt,
        "model": MODEL,
        "duration": DURATION,
        "resolution": RESOLUTION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds").replace("+00:00", "Z"),
    }

    if not allow:
        base["status"] = "standby"
        save_json("video.json", base)
        print("dry-run: pipeline standby, prompt composed")
        return

    try:
        job = submit({"prompt": prompt, "model": MODEL, "duration": DURATION,
                      "resolution": RESOLUTION, "fps": FPS, "generate_audio": True})
    except urllib.error.HTTPError as e:
        base["status"] = "insufficient_funds" if e.code == 402 else f"submit_error_{e.code}"
        save_json("video.json", base)
        print("submit failed:", e.code)
        return

    job = poll(job["id"])
    status = job.get("status")
    base["job_id"] = job.get("id")

    if status == "completed":
        result = job.get("result") or {}
        url = result.get("video_url")
        if not url:
            base["status"] = "no_video_url"
        else:
            os.makedirs("videos", exist_ok=True)
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
            path = f"videos/inkdrop_{ts}.mp4"
            download(url, path)
            prune_videos()
            base["status"] = "ready"
            base["video"] = path
            base["critique"] = self_critique(prompt)
            print("video ready:", path)
    else:
        base["status"] = status or "failed"

    history.setdefault("prompts", []).append(
        {"ts": base["generated_at"], "prompt": prompt, "status": base["status"],
         "style": style, "moment": moment,
         "critique": base.get("critique")})
    history["prompts"] = history["prompts"][-10:]
    save_json(HISTORY_PATH, history)
    save_json("video.json", base)


if __name__ == "__main__":
    main()
