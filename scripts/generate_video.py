#!/usr/bin/env python3
"""InkDrop video production pipeline with a QA engine.

Stage 1 (always, zero cost): the PRODUCTION TEAM writes a production pack
(logline, scene, dramatic beat, photorealistic character sheets,
environment, shot). With Groq/DeepSeek secrets the team is an ENSEMBLE:
3 candidate packs, one critic call picks the best. A ~40-point automated
QA battery validates structure, fact-faithfulness and photoreal specs on
every pack.

Stage 2 (when ALLOW_VIDEO_GEN=true): the pack becomes an LTX job -
text-to-video, or image-to-video when keyframes/latest.png exists in the
repo (Blender-rendered director's cut; served publicly via Pages).

Stage 3 (learning): after rendering, a Groq VISION model inspects real
frames of the video and writes a score + lesson for the next production.
All lessons live in video_history.json.
"""

import base64
import datetime
import json
import os
import random
import re
import subprocess
import tempfile
import time
import urllib.request

LTX = "https://api.ltx.io/v2/text-to-video"
LTX_I2V = "https://api.ltx.io/v2/image-to-video"
MODEL = os.environ.get("VIDEO_MODEL", "ltx-2-3-fast")
DURATION = int(os.environ.get("VIDEO_DURATION", "5"))
RESOLUTION = os.environ.get("VIDEO_RESOLUTION", "1280x720")
FPS = 24
POLL_SECONDS = 10
POLL_TIMEOUT = 480
KEEP_VIDEOS = 3
HISTORY_PATH = "video_history.json"
KEYFRAME_PATH = "keyframes/latest.png"
KEYFRAME_URL = ("https://nathanielsai19-rgb.github.io/news-post-generator/"
                "keyframes/latest.png")

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
MOMENT_RULES = [
    "Capture the exact second something shifts - the beat before a reveal, not after",
    "One clear dramatic beat: a decision, a discovery, a confrontation - never a static establishing shot",
    "End on an open loop: a look, a sound off-screen, a door opening - something that makes viewers rewatch",
]
CAMERA_MOVES = ["dolly_in", "jib_up", "focus_shift"]

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "been", "has", "have", "had", "it", "its",
    "this", "that", "as", "at", "by", "from", "their", "they", "he", "she", "his",
    "her", "not", "no", "who", "what", "when", "how", "why", "after", "before",
    "more", "most", "one", "two", "new", "say", "says", "said", "will", "would",
}

EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF☀-➿]")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


def http(method, url, key=None, payload=None, timeout=60):
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


def llm_chat(base, key, model, messages, temperature=0.9, max_tokens=900):
    payload = {"model": model, "temperature": temperature,
               "max_tokens": max_tokens, "messages": messages}
    data = http("POST", base + "/chat/completions", key=key, payload=payload)
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()


def parse_llm_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        text = text.lstrip("json").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        try:
            start, end = text.find("["), text.rfind("]")
        except Exception:
            return None
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


# ---------------------------------------------------------------- production

def llm_production(story, history):
    groq_key = os.environ.get("GROQ_API_KEY")
    ds_key = os.environ.get("DEEPSEEK_API_KEY")
    if not (groq_key or ds_key):
        return None
    past = history.get("packs", [])[-2:]
    past_note = "\n".join(
        f"- pack: {json.dumps(p.get('pack'), ensure_ascii=False)[:600]}\n  lesson: {p.get('critique') or 'none'}"
        for p in past if p.get("critique")
    )
    ask = (
        "You are the showrunner of a one-shot film based on this news story:\n"
        f"{story_text(story)}\n\n"
        "Write a production pack as STRICT JSON with exactly these keys:\n"
        "- logline: one sentence selling the story's most dramatic angle\n"
        "- scene: 2-3 sentences setting the scene\n"
        "- beat: the single most dramatic 5-second moment to film, one sentence\n"
        "- characters: array of 1-3 objects with keys name, role, appearance, clothing, "
        "expression, motivation - photorealistic and specific (visible skin texture, real "
        "fabric, believable body language), faithful to the real people in the story\n"
        "- environment: object with keys location, time, lighting, weather, atmosphere - "
        "real-life accuracy: real places, era-correct details, plausible physics\n"
        "- shot: object with keys camera, framing, lens\n\n"
        "Stay strictly faithful to the story - never invent events. Return ONLY the JSON."
    )
    if past_note:
        ask += "\n\nLearn from the past - do not repeat these weaknesses:\n" + past_note
    system = "You are a film showrunner. Reply with JSON only."
    draft = None
    if groq_key:
        for model in ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"):
            try:
                draft = parse_llm_json(llm_chat(
                    "https://api.groq.com/openai/v1", groq_key, model,
                    [{"role": "system", "content": system},
                     {"role": "user", "content": ask}], max_tokens=900))
                if draft:
                    break
            except Exception:
                continue
    if not draft and ds_key:
        try:
            draft = parse_llm_json(llm_chat(
                "https://api.deepseek.com", ds_key,
                os.environ.get("VIDEO_DS_MODEL", "deepseek-flash"),
                [{"role": "system", "content": system},
                 {"role": "user", "content": ask}], max_tokens=1200))
        except Exception:
            draft = None
    if draft and ds_key:
        try:
            improved = parse_llm_json(llm_chat(
                "https://api.deepseek.com", ds_key,
                os.environ.get("VIDEO_DS_MODEL", "deepseek-flash"),
                [{"role": "system", "content":
                    "You are the final showrunner on a two-director team. Improve the production "
                    "pack JSON your teammate drafted: stronger beat, more photorealistic character "
                    "detail, real-life-accurate environment. Return ONLY the improved JSON with the "
                    "same keys."},
                 {"role": "user", "content": json.dumps(draft, ensure_ascii=False)}],
                max_tokens=1200))
            if improved:
                draft = improved
        except Exception:
            pass
    return draft


def critic_pick_best(drafts):
    """One critic call scores all candidate packs; the best wins."""
    ds_key = os.environ.get("DEEPSEEK_API_KEY")
    if not ds_key:
        return drafts[0]
    try:
        raw = llm_chat(
            "https://api.deepseek.com", ds_key,
            os.environ.get("VIDEO_DS_MODEL", "deepseek-flash"),
            [{"role": "system", "content":
                "You are a film critic. Score each production pack 1-10 on dramatic strength, "
                "photorealistic specificity and real-life accuracy. Reply ONLY with a JSON array "
                "of integers, one score per pack, in order."},
             {"role": "user", "content":
                "\n\n---PACK---\n\n".join(json.dumps(d, ensure_ascii=False) for d in drafts)}],
            temperature=0.3, max_tokens=80)
        scores = parse_llm_json(raw)
        if isinstance(scores, list) and len(scores) == len(drafts):
            scores = [int(s) if isinstance(s, (int, float)) else 0 for s in scores]
            return drafts[scores.index(max(scores))]
    except Exception:
        pass
    return drafts[0]


def ensemble_production(story, history):
    if not (os.environ.get("GROQ_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")):
        return None, False
    drafts = []
    for _ in range(3):
        p = llm_production(story, history)
        if p:
            drafts.append(p)
    if not drafts:
        return None, False
    return critic_pick_best(drafts), True


def template_production(story, history):
    used = {(p.get("style"), p.get("moment")) for p in history.get("packs", []) if p.get("style")}
    options = [(s, m) for s in STYLE_POOL for m in MOMENT_RULES if (s, m) not in used]
    if options:
        style, moment = random.choice(options)
    else:
        style, moment = random.choice([(s, MOMENT_RULES[0]) for s in STYLE_POOL])
    text = story_text(story)
    return {
        "logline": f"{text.split('.')[0]}. — one moment that captures why it matters.",
        "scene": f"{text}. The scene opens in the middle of the action.",
        "beat": f"{moment} {text}",
        "characters": [
            {
                "name": "The protagonist",
                "role": "the person at the center of the story",
                "appearance": "photorealistic: visible pores and skin texture, natural hair movement, "
                              "believable proportions, real eye reflections",
                "clothing": "everyday clothes with natural fabric folds, subtle wear and correct drape",
                "expression": "restrained tension - a tight jaw, a glance away, small tells",
                "motivation": "to get through the next five minutes without losing control",
            },
            {
                "name": "The witness",
                "role": "someone watching it unfold",
                "appearance": "photorealistic secondary character, distinct silhouette, age-appropriate detail",
                "clothing": "weather-appropriate outfit, believable textures",
                "expression": "quiet alarm, trying not to react",
                "motivation": "to stay out of it - and failing",
            },
        ],
        "environment": {
            "location": "the real-world location implied by the story",
            "time": "the time of day the story suggests",
            "lighting": "natural cinematic light with physically plausible shadows and reflections",
            "weather": "consistent with the story's setting and mood",
            "atmosphere": "dense and tactile - air, dust, reflections all physically plausible",
        },
        "shot": {
            "camera": random.choice(CAMERA_MOVES),
            "framing": "medium close-up on the protagonist, environment in deep focus",
            "lens": "35mm anamorphic, shallow depth of field",
        },
    }, style, moment


def build_production(story, history):
    pack, used_ensemble = ensemble_production(story, history)
    if pack:
        return pack, None, None, used_ensemble
    pack, style, moment = template_production(story, history)
    return pack, style, moment, False


def pack_to_prompt(pack):
    chars = " ".join(
        f"{c.get('name')}: {c.get('appearance', '')} {c.get('clothing', '')} {c.get('expression', '')}"
        for c in pack.get("characters", [])[:2]
    )
    env = pack.get("environment") or {}
    shot = pack.get("shot") or {}
    return (
        f"Photorealistic. Scene: {pack.get('scene', '')} "
        f"Characters: {chars} "
        f"Environment: {env.get('location', '')}, {env.get('time', '')}, {env.get('lighting', '')}, "
        f"{env.get('weather', '')}, {env.get('atmosphere', '')}. "
        f"Shot: {shot.get('camera', '')}, {shot.get('framing', '')}, {shot.get('lens', '')}. "
        f"Beat: {pack.get('beat', '')} "
        "No text overlays, no watermarks, no logos."
    )


def pack_to_motion_prompt(pack):
    shot = pack.get("shot") or {}
    return (f"{shot.get('camera', 'slow dolly')} camera movement. "
            f"{pack.get('beat', '')} Photorealistic natural motion, no morphing, "
            "no text overlays, no watermarks.")


# ---------------------------------------------------------------- QA engine

def qa_battery(pack, story):
    """~40 automated checks: structure, fact-faithfulness, photoreal specs."""
    checks = []

    def check(name, ok):
        checks.append((name, bool(ok)))

    env = pack.get("environment") or {}
    shot = pack.get("shot") or {}
    chars = pack.get("characters") or []

    for k in ("logline", "scene", "beat"):
        check(f"pack.{k} present", bool(pack.get(k)))
    for k in ("location", "time", "lighting", "weather", "atmosphere"):
        check(f"environment.{k} present", bool(env.get(k)))
    for k in ("camera", "framing", "lens"):
        check(f"shot.{k} present", bool(shot.get(k)))

    check("characters present", len(chars) >= 1)
    check("characters <= 3", len(chars) <= 3)

    check("logline length ok", 20 <= len(pack.get("logline", "")) <= 300)
    check("beat length ok", 20 <= len(pack.get("beat", "")) <= 300)
    check("scene length ok", 40 <= len(pack.get("scene", "")) <= 700)

    for f in ("logline", "scene", "beat"):
        check(f"{f} no emoji", not EMOJI_RE.search(pack.get(f, "")))

    for i, c in enumerate(chars):
        joined = " ".join(str(v) for v in c.values()).lower()
        for k in ("name", "role", "appearance", "clothing", "expression", "motivation"):
            check(f"char{i}.{k} present", bool(c.get(k)) and len(str(c.get(k))) >= 3)
        check(f"char{i} photoreal spec",
              bool(re.search(r"skin|texture|real|fabric|light|eye", joined)))

    story_words = set(re.sub(r"[^\w\s]", " ", story_text(story).lower()).split()) - STOPWORDS
    pack_words = set(re.sub(
        r"[^\w\s]", " ",
        (pack.get("logline", "") + " " + pack.get("beat", "")).lower()).split()) - STOPWORDS
    overlap = story_words & pack_words
    check("story overlap >= 1", len(overlap) >= 1)
    check("story overlap >= 3", len(overlap) >= 3)

    env_text = " ".join(env.values()).lower()
    check("environment has lighting spec",
          bool(re.search(r"light|shadow|sun|neon|golden|lamp", env_text)))
    check("environment has real-place words",
          bool(re.search(r"room|street|hall|office|stadium|city|town|building|stage|home|field|desk", env_text)))

    total = len(checks)
    passed = sum(1 for _, ok in checks if ok)
    return {"checks": total, "passed": passed,
            "failed": [name for name, ok in checks if not ok]}


# ---------------------------------------------------------------- vision QA

def vision_critique(video_path):
    """Groq vision model inspects real frames of the finished video."""
    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        return None
    try:
        frames = []
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(
                ["ffmpeg", "-i", video_path, "-vf", "fps=1/2,scale=640:-1",
                 "-frames:v", "3", os.path.join(td, "f%02d.jpg")],
                check=True, capture_output=True, timeout=120)
            for f in sorted(os.listdir(td)):
                with open(os.path.join(td, f), "rb") as fh:
                    frames.append(base64.b64encode(fh.read()).decode())
        content = [{"type": "text", "text":
                    "You are a cinematographer reviewing 3 frames of an AI-generated news video. "
                    "Score 1-10 on photorealism, physical accuracy and character realism. "
                    "Reply ONLY with JSON: {\"score\": <int>, \"lesson\": \"one sentence\"}."}]
        for fr in frames:
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64," + fr}})
        data = http("POST", "https://api.groq.com/openai/v1/chat/completions", key=groq_key,
                    payload={"model": os.environ.get("VIDEO_VISION_MODEL", "llama-3.2-90b-vision"),
                             "messages": [{"role": "user", "content": content}],
                             "temperature": 0.3, "max_tokens": 200})
        txt = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        parsed = parse_llm_json(txt) or {}
        return {"score": parsed.get("score"), "lesson": parsed.get("lesson", txt[:200])}
    except Exception as e:
        print("vision critique skipped:", e)
        return None


# ---------------------------------------------------------------- LTX

def submit(payload, use_keyframe=False):
    key = os.environ["LTX_API_KEY"]
    return http("POST", LTX_I2V if use_keyframe else LTX, key=key, payload=payload)


def poll(job_id, use_keyframe=False):
    key = os.environ["LTX_API_KEY"]
    endpoint = (LTX_I2V if use_keyframe else LTX) + "/" + job_id
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        try:
            job = http("GET", endpoint, key=key)
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


# ---------------------------------------------------------------- main

def main():
    allow = os.environ.get("ALLOW_VIDEO_GEN", "false") == "true"
    story = pick_story()
    history = load_json(HISTORY_PATH, {"packs": []})
    pack, style, moment, used_ensemble = build_production(story, history)
    qa = qa_battery(pack, story)
    print(f"QA: {qa['passed']}/{qa['checks']} passed"
          + (f" - failed: {qa['failed'][:5]}" if qa["failed"] else ""))
    prompt = pack_to_prompt(pack)

    generated_at = datetime.datetime.now(datetime.timezone.utc) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    production = {
        "generated_at": generated_at,
        "story": {
            "title": story.get("title") if story else "",
            "outlet": story.get("outlet") if story else "",
            "link": story.get("link") if story else "",
            "trend": story.get("trend") if story else "",
        },
        "pack": pack,
        "prompt": prompt,
        "qa": qa,
        "ensemble": used_ensemble,
        "status": "standby",
    }
    save_json("production.json", production)
    print("production pack written")

    if not allow:
        if not os.path.exists("video.json"):
            save_json("video.json", {
                "status": "standby",
                "title": production["story"]["title"],
                "outlet": production["story"]["outlet"],
                "story_link": production["story"]["link"],
                "prompt": prompt,
                "model": MODEL, "duration": DURATION, "resolution": RESOLUTION,
                "generated_at": generated_at,
            })
        print("pipeline standby - no LTX call")
        return

    use_keyframe = os.path.exists(KEYFRAME_PATH)
    production["used_keyframe"] = use_keyframe
    if use_keyframe:
        payload = {"image_uri": KEYFRAME_URL,
                   "prompt": pack_to_motion_prompt(pack),
                   "model": MODEL, "duration": DURATION,
                   "resolution": RESOLUTION, "fps": FPS, "generate_audio": True}
        print("using Blender/director keyframe as first frame")
    else:
        payload = {"prompt": prompt, "model": MODEL, "duration": DURATION,
                   "resolution": RESOLUTION, "fps": FPS, "generate_audio": True}

    try:
        job = submit(payload, use_keyframe)
    except urllib.error.HTTPError as e:
        production["status"] = "insufficient_funds" if e.code == 402 else f"submit_error_{e.code}"
        save_json("production.json", production)
        print("submit failed:", e.code)
        return

    job = poll(job["id"], use_keyframe)
    status = job.get("status")
    production["job_id"] = job.get("id")

    if status == "completed":
        result = job.get("result") or {}
        url = result.get("video_url")
        if not url:
            production["status"] = "no_video_url"
        else:
            os.makedirs("videos", exist_ok=True)
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
            path = f"videos/inkdrop_{ts}.mp4"
            download(url, path)
            prune_videos()
            production["status"] = "ready"
            production["video"] = path
            production["vision"] = vision_critique(path)
            print("video ready:", path)
    else:
        production["status"] = status or "failed"

    production["critique"] = production.get("critique") or self_critique(pack)
    history.setdefault("packs", []).append(
        {"ts": generated_at, "pack": pack, "prompt": prompt,
         "style": style, "moment": moment,
         "status": production["status"],
         "qa": qa,
         "vision": production.get("vision"),
         "critique": production.get("critique")})
    history["packs"] = history["packs"][-10:]
    save_json(HISTORY_PATH, history)

    save_json("video.json", {
        "status": production["status"],
        "title": production["story"]["title"],
        "outlet": production["story"]["outlet"],
        "story_link": production["story"]["link"],
        "trend": production["story"]["trend"],
        "prompt": prompt,
        "model": MODEL, "duration": DURATION, "resolution": RESOLUTION,
        "generated_at": generated_at,
        "video": production.get("video"),
        "job_id": production.get("job_id"),
        "vision": production.get("vision"),
    })
    save_json("production.json", production)


def self_critique(pack):
    ds_key = os.environ.get("DEEPSEEK_API_KEY")
    if not ds_key:
        return None
    try:
        return llm_chat(
            "https://api.deepseek.com", ds_key,
            os.environ.get("VIDEO_DS_MODEL", "deepseek-flash"),
            [{"role": "system", "content":
                "You review AI video production packs. After seeing the result in your mind, write ONE "
                "sentence lesson for the next production: what to keep, what to change. Be specific."},
             {"role": "user", "content": json.dumps(pack, ensure_ascii=False)}],
            temperature=0.4, max_tokens=120)
    except Exception:
        return None


if __name__ == "__main__":
    main()
