#!/usr/bin/env python3
"""InkDrop video production pipeline (OpenAI writer + local Blender renderer).

Stage 1 (always): OpenAI writes a production pack for a news story AND a
Blender-ready scene spec - stylized 3D character builds, props from a fixed
catalog, palette, lights, camera keyframes. With no OpenAI key the pipeline
falls back to a built-in stylized template. A QA battery validates every
pack (structure, catalog types, colors, bounds, fact-faithfulness).

Stage 2 (when ALLOW_VIDEO_GEN=true, LOCAL): the scene spec goes to Blender
(scripts/build_render.py, headless), which builds the scene, animates the
camera and renders an H.264 mp4 + keyframes/latest.png. Runs on the local
PC - not in GitHub Actions.

Stage 3 (learning): after rendering, a Groq VISION model inspects real
frames of the video and writes a score + lesson for the next production.
All lessons live in video_history.json.
"""

import datetime
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_PATH = "video_history.json"
KEYFRAME_PATH = "keyframes/latest.png"
KEEP_VIDEOS = 3
OPENAI_API = "https://api.openai.com/v1"
OPENAI_MODEL = os.environ.get("VIDEO_OPENAI_MODEL", "gpt-4o-mini")
VIDEO_RES = os.environ.get("VIDEO_RES", "1280x720")
VIDEO_DURATION = int(os.environ.get("VIDEO_DURATION", "5"))

# Mirrors the catalog in scripts/build_render.py - the writer may only use
# these, so any pack that passes QA is guaranteed buildable.
PROP_CATALOG = ("table", "chair", "window", "door", "tree", "bush",
                "building", "lamppost", "bench", "rock", "cloud", "fence",
                "car", "boat", "sign", "plant", "stairs", "tv", "sofa",
                "bookshelf")
POSES = ("standing", "reaching", "pointing", "looking_up")
ACCESSORIES = ("none", "hat")
CAMERA_MOVES = ("dolly_in", "dolly_out", "pan_left", "pan_right",
                "orbit_left", "orbit_right", "crane_up", "static")
BOUNDS = 7.0

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "been", "has", "have", "had", "it", "its",
    "this", "that", "as", "at", "by", "from", "their", "they", "he", "she", "his",
    "her", "not", "no", "who", "what", "when", "how", "why", "after", "before",
    "more", "most", "one", "two", "new", "say", "says", "said", "will", "would",
}

EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF☀-➿]")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def load_env(path=".env"):
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    except OSError:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


def http(method, url, key=None, payload=None, timeout=120):
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


def llm_chat(base, key, model, messages, temperature=0.9, max_tokens=900,
             json_mode=False):
    payload = {"model": model, "temperature": temperature,
               "max_tokens": max_tokens, "messages": messages}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
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
        start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


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


# ---------------------------------------------------------------- writer

def writer_prompt(story, history):
    past = history.get("packs", [])[-2:]
    past_note = "\n".join(
        f"- pack: {json.dumps(p.get('pack'), ensure_ascii=False)[:400]}\n"
        f"  vision lesson: {p.get('vision', {}).get('lesson') or 'none'}"
        for p in past if (p.get("vision") or {}).get("lesson")
    )
    ask = (
        "You are the showrunner of a short stylized-3D news film. Story:\n"
        f"{story_text(story)}\n\n"
        "Write a production pack as STRICT JSON with these keys:\n"
        "- logline: one sentence selling the story's most dramatic angle\n"
        "- scene: 2-3 sentences setting the scene\n"
        "- beat: the single most dramatic 5-second moment to film, one sentence\n"
        "- characters: array of 1-3 display descriptions, each with keys name, role, "
        "appearance, clothing, expression, motivation - describe the STYLIZED 3D look "
        "(rounded simple shapes, matte colors, toy-like figures), faithful to the real "
        "people in the story, no real-person likeness claims\n"
        "- environment: object with keys location, time, lighting, weather, atmosphere - "
        "real-life accuracy for place and time, weather one of: clear, cloudy, rain, night, sunset\n"
        "- shot: object with keys camera, framing, lens\n"
        "- build: the Blender build spec, EXACTLY these keys:\n"
        f"  - palette: object with hex colors sky, ground, accent (e.g. \"#8fb8d8\")\n"
        "  - duration: 5 (seconds, 3 to 10)\n"
        f"  - characters: array matching the display characters above; each: name, height "
        f"(1.2-2.4), pose one of {list(POSES)}, accessory one of {list(ACCESSORIES)}, "
        f"facing (degrees 0-360, 0 = facing +Y), body (hex), head (hex), "
        f"position [x, y] within {-BOUNDS}..{BOUNDS}\n"
        f"  - props: 2-6 objects; each: type one of {list(PROP_CATALOG)}, "
        f"position [x, y] within {-BOUNDS}..{BOUNDS}, scale 0.3-3, color (hex)\n"
        f"  - camera: move one of {list(CAMERA_MOVES)}; start and end objects each with "
        f"pos [x,y,z] (x,y within -12..12, z 0.3..8), target [x,y,z] (within the scene), "
        f"fov 24-70 (vertical degrees)\n"
        "  - lights: sun_dir [x,y,z] (towards the scene, z positive), sun_color (hex), "
        "sun_strength 0.5-8, fill 0-2\n\n"
        "Rules: stay strictly faithful to the story - never invent events. All colors must "
        "be 6-digit hex. No emoji anywhere. Return ONLY the JSON object."
    )
    if past_note:
        ask += "\n\nLearn from the past - do not repeat these weaknesses:\n" + past_note
    return ask


def openai_production(story, history):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    ask = writer_prompt(story, history)
    try:
        raw = llm_chat(OPENAI_API, key, OPENAI_MODEL,
                       [{"role": "system",
                         "content": "You are a film showrunner who designs stylized "
                                    "low-poly 3D scenes. Reply with JSON only."},
                        {"role": "user", "content": ask}],
                       temperature=0.9, max_tokens=2500, json_mode=True)
        pack = parse_llm_json(raw)
        if isinstance(pack, dict) and pack.get("build"):
            return pack
    except Exception as e:
        print("openai production failed:", e)
    return None


def template_production(story, history):
    """Zero-cost fallback: a fixed stylized scene with the story woven in."""
    text = story_text(story)
    first = text.split(".")[0]
    return {
        "logline": f"{first}. One quiet moment that captures why it matters.",
        "scene": f"{text}. The scene opens on a figure at a window, mid-thought.",
        "beat": "The figure turns from the window as the light shifts - a decision made.",
        "characters": [
            {
                "name": "The figure",
                "role": "the person at the center of the story",
                "appearance": "rounded low-poly figure: capsule body, sphere head, simple dark eyes",
                "clothing": "matte single-color body with a bright accent hat",
                "expression": "still, head slightly raised - mid-thought",
                "motivation": "to understand what the news means before acting",
            },
        ],
        "environment": {
            "location": "a quiet room implied by the story",
            "time": "the time of day the story suggests",
            "lighting": "soft key light from the window, gentle fill",
            "weather": "clear",
            "atmosphere": "calm and unhurried",
        },
        "shot": {
            "camera": "dolly_in",
            "framing": "medium shot on the figure, window behind",
            "lens": "50mm equivalent",
        },
        "build": {
            "palette": {"sky": "#8fb8d8", "ground": "#4a5d3a", "accent": "#e8c04a"},
            "duration": VIDEO_DURATION,
            "characters": [
                {"name": "The figure", "height": 1.7, "pose": "looking_up",
                 "accessory": "hat", "facing": 180,
                 "body": "#2456a5", "head": "#d8a06a", "position": [0.6, -0.6]},
            ],
            "props": [
                {"type": "window", "position": [0, -4.5], "scale": 1.6, "color": "#6b4a2f"},
                {"type": "table", "position": [0.8, 0.9], "scale": 1.0, "color": "#6b4a2f"},
                {"type": "plant", "position": [-1.6, 0.4], "scale": 1.1, "color": "#3f7d3f"},
                {"type": "lamppost", "position": [2.8, 1.6], "scale": 1.0, "color": "#3a3a3a"},
            ],
            "camera": {
                "move": "dolly_in",
                "start": {"pos": [4.8, -6.2, 2.6], "target": [0.4, -0.4, 1.3], "fov": 50},
                "end": {"pos": [2.6, -3.4, 1.9], "target": [0.4, -0.4, 1.3], "fov": 44},
            },
            "lights": {"sun_dir": [-0.6, -0.8, 1.2], "sun_color": "#fff2d9",
                       "sun_strength": 3.0, "fill": 0.35},
        },
    }


def build_production(story, history):
    pack = openai_production(story, history)
    writer = "openai" if pack else "template"
    return (pack or template_production(story, history)), writer


# ---------------------------------------------------------------- QA engine

def qa_battery(pack, story):
    """~35 automated checks: structure, catalog types, colors, bounds, facts."""
    checks = []

    def check(name, ok):
        checks.append((name, bool(ok)))

    env = pack.get("environment") or {}
    shot = pack.get("shot") or {}
    build = pack.get("build") or {}
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
        check(f"char{i} stylized words",
              bool(re.search(r"low.?poly|rounded|sphere|matte|capsule|stylized|3d|shape", joined)))

    story_words = set(re.sub(r"[^\w\s]", " ", story_text(story).lower()).split()) - STOPWORDS
    pack_words = set(re.sub(
        r"[^\w\s]", " ",
        (pack.get("logline", "") + " " + pack.get("beat", "")).lower()).split()) - STOPWORDS
    overlap = story_words & pack_words
    check("story overlap >= 1", len(overlap) >= 1)
    check("story overlap >= 3", len(overlap) >= 3)

    pal = build.get("palette") or {}
    check("build.palette present", bool(pal))
    for k in ("sky", "ground"):
        check(f"palette.{k} valid hex", bool(HEX_RE.match(str(pal.get(k, "")))))
    dur = build.get("duration", VIDEO_DURATION)
    check("duration in range", isinstance(dur, (int, float)) and 3 <= dur <= 10)

    bchars = build.get("characters") or []
    check("build.characters present", len(bchars) >= 1)
    for i, c in enumerate(bchars):
        check(f"bchar{i}.height in range",
              isinstance(c.get("height"), (int, float)) and 1.2 <= c.get("height") <= 2.4)
        check(f"bchar{i}.pose valid", c.get("pose") in POSES)
        check(f"bchar{i}.accessory valid", c.get("accessory") in ACCESSORIES)
        check(f"bchar{i}.body valid hex", bool(HEX_RE.match(str(c.get("body", "")))))
        pos = c.get("position") or [0, 0]
        check(f"bchar{i}.position in bounds",
              len(pos) >= 2 and all(isinstance(v, (int, float)) and
                                    -BOUNDS <= v <= BOUNDS for v in pos[:2]))

    props = build.get("props") or []
    check("props 1-8", 1 <= len(props) <= 8)
    for i, p in enumerate(props):
        check(f"prop{i}.type valid", p.get("type") in PROP_CATALOG)
        check(f"prop{i}.color valid hex", bool(HEX_RE.match(str(p.get("color", "")))))
        check(f"prop{i}.scale in range",
              isinstance(p.get("scale"), (int, float)) and 0.3 <= p.get("scale") <= 3)
        pos = p.get("position") or [0, 0]
        check(f"prop{i}.position in bounds",
              len(pos) >= 2 and all(isinstance(v, (int, float)) and
                                    -BOUNDS <= v <= BOUNDS for v in pos[:2]))

    cam = build.get("camera") or {}
    check("camera.move valid", cam.get("move") in CAMERA_MOVES)
    for k in ("start", "end"):
        node = cam.get(k) or {}
        pos = node.get("pos") or []
        tgt = node.get("target") or []
        check(f"camera.{k}.pos in bounds",
              len(pos) >= 3 and all(isinstance(v, (int, float)) for v in pos[:3])
              and abs(pos[0]) <= 12 and abs(pos[1]) <= 12 and 0.3 <= pos[2] <= 8)
        check(f"camera.{k}.target present",
              len(tgt) >= 3 and all(isinstance(v, (int, float)) for v in tgt[:3]))
        check(f"camera.{k}.fov in range",
              isinstance(node.get("fov"), (int, float)) and 24 <= node.get("fov") <= 70)

    lights = build.get("lights") or {}
    check("lights.sun_strength in range",
          isinstance(lights.get("sun_strength"), (int, float)) and
          0.5 <= lights.get("sun_strength") <= 8)
    check("lights.sun_color valid hex", bool(HEX_RE.match(str(lights.get("sun_color", "")))))

    total = len(checks)
    passed = sum(1 for _, ok in checks if ok)
    return {"checks": total, "passed": passed,
            "failed": [name for name, ok in checks if not ok]}


# ---------------------------------------------------------------- Blender render

def find_blender():
    candidates = [os.environ.get("BLENDER_EXE", ""),
                  r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"]
    for cand in candidates:
        if cand and os.path.exists(cand):
            return cand
    return shutil.which("blender")


def render_with_blender(ts):
    """Headless local render: build_render.py writes PNG frames, ffmpeg muxes the mp4."""
    blender = find_blender()
    if not blender:
        print("Blender not found - set BLENDER_EXE or install Blender")
        return None
    os.makedirs("videos", exist_ok=True)
    out = f"videos/inkdrop_{ts}.mp4"
    if os.path.isdir("frames"):
        shutil.rmtree("frames")
    cmd = [blender, "-b", "--factory-startup",
           "-P", "scripts/build_render.py", "--", "scene_spec.json", out]
    try:
        result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True,
                                text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        print("Blender render timed out")
        return None
    tail = "\n".join((result.stdout or "").strip().splitlines()[-6:])
    print(tail or (result.stderr or "")[-2000:])
    if result.returncode != 0:
        return None

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not os.path.isdir("frames"):
        print("ffmpeg or frames missing - no mp4 produced")
        return None
    mux = subprocess.run(
        [ffmpeg, "-y", "-framerate", "24", "-i", "frames/f%04d.png",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", out],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=600)
    shutil.rmtree("frames", ignore_errors=True)
    if mux.returncode != 0:
        print("ffmpeg mux failed:", (mux.stderr or "")[-400:])
        return None
    return out


def prune_videos(keep=KEEP_VIDEOS):
    import glob
    files = sorted(glob.glob("videos/*.mp4"))
    for old in files[:-keep]:
        os.remove(old)
        print("pruned", old)


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
                import base64
                with open(os.path.join(td, f), "rb") as fh:
                    frames.append(base64.b64encode(fh.read()).decode())
        content = [{"type": "text", "text":
                    "You are a cinematographer reviewing 3 frames of a stylized-3D news video. "
                    "Score 1-10 on scene composition, character readability and physical "
                    "plausibility. Reply ONLY with JSON: {\"score\": <int>, \"lesson\": "
                    "\"one sentence\"}."}]
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


def self_critique(pack):
    ds_key = os.environ.get("DEEPSEEK_API_KEY")
    if not ds_key:
        return None
    try:
        return llm_chat(
            "https://api.deepseek.com", ds_key,
            os.environ.get("VIDEO_DS_MODEL", "deepseek-flash"),
            [{"role": "system", "content":
                "You review stylized-3D video production packs. After seeing the result in "
                "your mind, write ONE sentence lesson for the next production: what to keep, "
                "what to change. Be specific."},
             {"role": "user", "content": json.dumps(pack, ensure_ascii=False)}],
            temperature=0.4, max_tokens=120)
    except Exception:
        return None


# ---------------------------------------------------------------- main

def main():
    os.chdir(REPO_ROOT)
    load_env(".env")
    allow = os.environ.get("ALLOW_VIDEO_GEN", "false") == "true"
    story = pick_story()
    history = load_json(HISTORY_PATH, {"packs": []})
    pack, writer = build_production(story, history)
    qa = qa_battery(pack, story)
    print(f"writer: {writer} | QA: {qa['passed']}/{qa['checks']} passed"
          + (f" - failed: {qa['failed'][:5]}" if qa["failed"] else ""))

    generated_at = datetime.datetime.now(datetime.timezone.utc) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    prompt = f"{pack.get('logline', '')} {pack.get('beat', '')}".strip()
    production = {
        "generated_at": generated_at,
        "writer": writer,
        "story": {
            "title": story.get("title") if story else "",
            "outlet": story.get("outlet") if story else "",
            "link": story.get("link") if story else "",
            "trend": story.get("trend") if story else "",
        },
        "pack": pack,
        "prompt": prompt,
        "qa": qa,
        "status": "standby",
    }
    save_json("production.json", production)
    save_json("scene_spec.json", {"pack": pack,
                                  "story": production["story"],
                                  "generated_at": generated_at})
    print("production pack + scene spec written")

    if not allow:
        if not os.path.exists("video.json"):
            save_json("video.json", {
                "status": "standby",
                "title": production["story"]["title"],
                "outlet": production["story"]["outlet"],
                "story_link": production["story"]["link"],
                "prompt": prompt,
                "model": OPENAI_MODEL + " + Blender EEVEE",
                "duration": pack.get("build", {}).get("duration", VIDEO_DURATION),
                "resolution": VIDEO_RES,
                "generated_at": generated_at,
            })
        print("pipeline standby - local render not requested (ALLOW_VIDEO_GEN=true to render)")
        return

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    video = render_with_blender(ts)
    if video:
        production["status"] = "ready"
        production["video"] = video
        production["vision"] = vision_critique(video)
        prune_videos()
        print("video ready:", video)
    else:
        production["status"] = "render_failed"

    production["critique"] = self_critique(pack)
    history.setdefault("packs", []).append(
        {"ts": generated_at, "pack": pack, "writer": writer,
         "status": production["status"], "qa": qa,
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
        "model": OPENAI_MODEL + " + Blender EEVEE",
        "duration": pack.get("build", {}).get("duration", VIDEO_DURATION),
        "resolution": VIDEO_RES,
        "generated_at": generated_at,
        "video": production.get("video"),
        "vision": production.get("vision"),
    })
    save_json("production.json", production)


if __name__ == "__main__":
    main()
