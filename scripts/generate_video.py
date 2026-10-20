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
                "bookshelf", "skyline", "hills")
POSES = ("standing", "reaching", "pointing", "looking_up")
ACCESSORIES = ("none", "hat")
CAMERA_MOVES = ("dolly_in", "dolly_out", "pan_left", "pan_right",
                "orbit_left", "orbit_right", "crane_up", "static")
BOUNDS = 7.0
KEEP_PACKS = 3

# Visual variety: each run rotates through distinct looks. `mj` tokens feed
# the Midjourney prompts, the palette drives the Blender scene.
STYLE_POOL = [
    {"name": "sunset", "weather": "sunset", "sky": "#d98d5f", "ground": "#3f3a52",
     "accent": "#ffd166", "mj": "golden hour, warm rim light, long shadows, cinematic haze"},
    {"name": "rainy_night", "weather": "rain", "sky": "#1b2735", "ground": "#232830",
     "accent": "#6fd3c7", "mj": "moody night rain, neon reflections on wet ground, cinematic noir"},
    {"name": "neon_city", "weather": "night", "sky": "#1a1433", "ground": "#241d3d",
     "accent": "#ff4d9d", "mj": "neon-lit city night, cyberpunk palette, volumetric glow"},
    {"name": "desert_noon", "weather": "clear", "sky": "#9fd0e8", "ground": "#d9b382",
     "accent": "#e2574c", "mj": "harsh desert noon sun, heat haze, bleached warm colors"},
    {"name": "foggy_forest", "weather": "cloudy", "sky": "#b8c4c0", "ground": "#3e5a44",
     "accent": "#e8e3d0", "mj": "soft morning fog, diffused light, muted greens, quiet mood"},
    {"name": "snowy_dawn", "weather": "cloudy", "sky": "#c7d4e8", "ground": "#e8edf2",
     "accent": "#b3543f", "mj": "snowy dawn, cool blue light, soft pink horizon glow"},
    {"name": "noon_clear", "weather": "clear", "sky": "#8fb8d8", "ground": "#4a5d3a",
     "accent": "#e8c04a", "mj": "bright midday, crisp shadows, saturated natural colors"},
    {"name": "night_clear", "weather": "night", "sky": "#101a33", "ground": "#26303c",
     "accent": "#f0c060", "mj": "moonlit night, deep blues, warm glowing windows"},
]

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

def pick_style(history):
    """Rotate visual styles, avoiding the last few used."""
    used = {p.get("pack", {}).get("style") for p in history.get("packs", [])[-4:]}
    options = [s for s in STYLE_POOL if s["name"] not in used] or STYLE_POOL
    return random.choice(options)


def style_directive(style):
    return (
        f"Visual style directive (MUST follow): {style['name']} - "
        f"palette sky {style['sky']}, ground {style['ground']}, accent {style['accent']} "
        f"(use exactly these hexes in build.palette and as prop/character colors where it fits); "
        f"weather exactly \"{style['weather']}\"; mood tokens: {style['mj']}."
    )


def writer_prompt(story, history, style):
    past = history.get("packs", [])[-2:]
    past_note = "\n".join(
        f"- pack: {json.dumps(p.get('pack'), ensure_ascii=False)[:400]}\n"
        f"  vision lesson: {p.get('vision', {}).get('lesson') or 'none'}"
        for p in past if (p.get("vision") or {}).get("lesson")
    )
    ask = (
        "You are the showrunner of a short stylized-3D news film. Story:\n"
        f"{story_text(story)}\n\n"
        f"{style_directive(style)}\n\n"
        "Write a production pack as STRICT JSON with these keys:\n"
        "- logline: one sentence selling the story's most dramatic angle\n"
        "- scene: 2-3 sentences setting the scene\n"
        "- beat: the single most dramatic 5-second moment to film, one sentence\n"
        "- characters: array of 1-3 display descriptions, each with keys name, role, "
        "appearance, clothing, expression, motivation - describe the STYLIZED 3D look "
        "(rounded simple shapes, matte colors, toy-like figures), faithful to the real "
        "people in the story, no real-person likeness claims\n"
        "- environment: object with keys location, time, lighting, weather, atmosphere - "
        "real-life accuracy for place and time, weather as in the style directive\n"
        "- shot: object with keys camera, framing, lens - written like a film shot "
        "(vivid framing and lens language a video AI can follow)\n"
        "- build: the Blender build spec, EXACTLY these keys:\n"
        f"  - palette: object with the style directive's hex colors sky, ground, accent\n"
        "  - duration: 5 (seconds, 3 to 10)\n"
        f"  - characters: array matching the display characters above; each: name, height "
        f"(1.2-2.4), pose one of {list(POSES)}, accessory one of {list(ACCESSORIES)}, "
        f"facing (degrees 0-360, 0 = facing +Y), body (hex), head (hex), "
        f"position [x, y] within {-BOUNDS}..{BOUNDS}\n"
        f"  - props: 3-7 objects for a DENSE scene; each: type one of {list(PROP_CATALOG)}, "
        f"position [x, y] within {-BOUNDS}..{BOUNDS}, scale 0.3-3, color (hex). Include "
        f"background depth (skyline or hills near the far edge) when outdoors\n"
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


def openai_production(story, history, style):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    ask = writer_prompt(story, history, style)
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


def template_production(story, history, style):
    """Zero-cost fallback: a stylized scene driven by the style pool."""
    text = story_text(story)
    first = text.split(".")[0]
    moves = [m for m in ("dolly_in", "orbit_left", "pan_right", "crane_up")
             if m != history.get("last_move")]
    move = random.choice(moves)
    env = {
        "location": "a quiet place implied by the story",
        "time": "the time of day the story suggests",
        "lighting": "key light from the sun with gentle fill",
        "weather": style["weather"],
        "atmosphere": f"{style['name'].replace('_', ' ')} mood - calm and unhurried",
    }
    return {
        "style": style["name"],
        "logline": f"{first}. One quiet moment that captures why it matters.",
        "scene": f"{text}. The scene opens on a figure mid-thought.",
        "beat": "The figure turns toward the light as the moment settles - a decision made.",
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
        "environment": env,
        "shot": {
            "camera": move,
            "framing": "medium shot on the figure, window behind",
            "lens": "50mm equivalent",
        },
        "build": {
            "palette": {"sky": style["sky"], "ground": style["ground"],
                        "accent": style["accent"]},
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
                {"type": "skyline", "position": [0, -6.8], "scale": 1.3, "color": "#2a3442"},
                {"type": "hills", "position": [5.0, -6.5], "scale": 1.5, "color": "#5a6b58"},
            ],
            "camera": {
                "move": move,
                "start": {"pos": [4.8, -6.2, 2.6], "target": [0.4, -0.4, 1.3], "fov": 50},
                "end": {"pos": [2.6, -3.4, 1.9], "target": [0.4, -0.4, 1.3], "fov": 44},
            },
            "lights": {"sun_dir": [-0.6, -0.8, 1.2], "sun_color": "#fff2d9",
                       "sun_strength": 3.0, "fill": 0.35},
        },
    }


def build_production(story, history, style):
    pack = openai_production(story, history, style)
    writer = "openai" if pack else "template"
    pack = pack or template_production(story, history, style)
    pack["style"] = style["name"]
    return pack, writer


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


# ---------------------------------------------------------------- scene pack

def pack_to_mj_prompt(pack, style):
    """Midjourney text-to-video prompt: beat, subjects, scene, style, shot."""
    shot = pack.get("shot") or {}
    env = pack.get("environment") or {}
    subjects = "; ".join(
        f"{c.get('name')} - {c.get('appearance', '')}, {c.get('clothing', '')}, "
        f"{c.get('expression', '')}"
        for c in pack.get("characters", [])[:3]
    )
    prompt = (
        f"{pack.get('beat', '')} {subjects}. "
        f"Scene: {pack.get('scene', '')} "
        f"{env.get('location', '')}, {env.get('time', '')}, {env.get('weather', '')}. "
        f"{style.get('mj', '')}, {env.get('lighting', '')}, {env.get('atmosphere', '')}. "
        f"{shot.get('framing', '')}, camera {shot.get('camera', 'slow dolly')}, "
        f"{shot.get('lens', '')}. --ar 16:9"
    )
    return re.sub(r"\s+", " ", prompt).strip()


def pack_to_mj_motion(pack):
    """Short motion prompt for image-to-video with opening_frame.png uploaded."""
    shot = pack.get("shot") or {}
    return re.sub(r"\s+", " ", (
        f"Camera {shot.get('camera', 'slow dolly')}, {pack.get('beat', '')} "
        "Consistent stylized 3D scene and characters, smooth natural motion, "
        "no morphing, no text overlays.")).strip()


def write_pack_doc(path, production, style, mj_prompt, mj_motion):
    """Scene narration document - everything another AI needs for the scene."""
    pack = production["pack"]
    story = production["story"]
    env = pack.get("environment") or {}
    shot = pack.get("shot") or {}
    chars = "\n\n".join(
        f"### {c.get('name', 'Character')} - {c.get('role', '')}\n"
        f"- Appearance: {c.get('appearance', '')}\n"
        f"- Clothing: {c.get('clothing', '')}\n"
        f"- Expression: {c.get('expression', '')}\n"
        f"- Motivation: {c.get('motivation', '')}"
        for c in pack.get("characters", [])
    )
    lines = [
        f"# InkDrop Scene Pack - {story.get('title') or 'News scene'}",
        "",
        f"Story: {story.get('title', '')}",
        f"Outlet: {story.get('outlet', '')}",
        f"Link: {story.get('link', '')}",
        f"Generated: {production.get('generated_at', '')} · "
        f"writer: {production.get('writer', '')} · style: {style['name']}",
        "",
        "## Scene script",
        "",
        f"LOGLINE: {pack.get('logline', '')}",
        "",
        f"SCENE: {pack.get('scene', '')}",
        "",
        f"BEAT: {pack.get('beat', '')}",
        "",
        "## Characters",
        "",
        chars or "(none)",
        "",
        "## Environment & shot",
        "",
        f"- Location: {env.get('location', '')}",
        f"- Time: {env.get('time', '')}",
        f"- Lighting: {env.get('lighting', '')}",
        f"- Weather: {env.get('weather', '')}",
        f"- Atmosphere: {env.get('atmosphere', '')}",
        f"- Camera: {shot.get('camera', '')}",
        f"- Framing: {shot.get('framing', '')}",
        f"- Lens: {shot.get('lens', '')}",
        "",
        "## Midjourney text-to-video prompt",
        "",
        mj_prompt,
        "",
        "## Midjourney image-to-video (upload opening_frame.png as the image)",
        "",
        mj_motion,
        "",
        "## Reference images",
        "",
        "- opening_frame.png - the video's first frame (camera view)",
        "- refs/ref_front.png - three-quarter view of the scene",
        "- refs/ref_high.png - high angle overview",
        "- refs/ref_close.png - close-up on the main character",
        "",
        "## Blender scene",
        "",
        "scene.blend - open in Blender 5.2 to inspect or re-render the scene.",
        "scene_spec.json - the machine-readable build spec.",
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


# ---------------------------------------------------------------- Blender render

def find_blender():
    candidates = [os.environ.get("BLENDER_EXE", ""),
                  r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"]
    for cand in candidates:
        if cand and os.path.exists(cand):
            return cand
    return shutil.which("blender")


def render_with_blender(ts, pack_dir):
    """Headless local render: Blender writes frames + scene pack, ffmpeg muxes."""
    blender = find_blender()
    if not blender:
        print("Blender not found - set BLENDER_EXE or install Blender")
        return None, None
    os.makedirs("videos", exist_ok=True)
    out = f"videos/inkdrop_{ts}.mp4"
    if os.path.isdir("frames"):
        shutil.rmtree("frames")
    cmd = [blender, "-b", "--factory-startup",
           "-P", "scripts/build_render.py", "--", "scene_spec.json", out, pack_dir]
    try:
        result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True,
                                text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        print("Blender render timed out")
        return None, None
    tail = "\n".join((result.stdout or "").strip().splitlines()[-6:])
    print(tail or (result.stderr or "")[-2000:])
    if result.returncode != 0:
        return None, None

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not os.path.isdir("frames"):
        print("ffmpeg or frames missing - no mp4 produced")
        return None, None
    mux = subprocess.run(
        [ffmpeg, "-y", "-framerate", "24", "-i", "frames/f%04d.png",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", out],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=600)
    shutil.rmtree("frames", ignore_errors=True)
    if mux.returncode != 0:
        print("ffmpeg mux failed:", (mux.stderr or "")[-400:])
        return None, None

    # Zip the scene pack: narration doc + opening frame + refs + .blend + spec.
    zip_path = pack_dir + ".zip"
    try:
        shutil.copyfile("keyframes/latest.png",
                        os.path.join(pack_dir, "opening_frame.png"))
        shutil.make_archive(pack_dir, "zip", pack_dir)
        shutil.rmtree(pack_dir, ignore_errors=True)
        zip_path = zip_path.replace("\\", "/")
    except OSError as e:
        print("scene pack zip failed:", e)
        zip_path = None
    return out, zip_path


def prune_packs(keep=KEEP_PACKS):
    import glob
    files = sorted(glob.glob("scene-packs/*.zip"))
    for old in files[:-keep]:
        os.remove(old)
        print("pruned", old)


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
    style = pick_style(history)
    pack, writer = build_production(story, history, style)
    qa = qa_battery(pack, story)
    print(f"style: {style['name']} | writer: {writer} | "
          f"QA: {qa['passed']}/{qa['checks']} passed"
          + (f" - failed: {qa['failed'][:5]}" if qa["failed"] else ""))

    generated_at = datetime.datetime.now(datetime.timezone.utc) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    mj_prompt = pack_to_mj_prompt(pack, style)
    mj_motion = pack_to_mj_motion(pack)
    production = {
        "generated_at": generated_at,
        "writer": writer,
        "style": style["name"],
        "story": {
            "title": story.get("title") if story else "",
            "outlet": story.get("outlet") if story else "",
            "link": story.get("link") if story else "",
            "trend": story.get("trend") if story else "",
        },
        "pack": pack,
        "prompt": mj_prompt,
        "mj_motion": mj_motion,
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
                "prompt": mj_prompt,
                "model": OPENAI_MODEL + " + Blender EEVEE",
                "duration": pack.get("build", {}).get("duration", VIDEO_DURATION),
                "resolution": VIDEO_RES,
                "generated_at": generated_at,
            })
        print("pipeline standby - local render not requested (ALLOW_VIDEO_GEN=true to render)")
        return

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    pack_dir = os.path.join("scene-packs", f"inkdrop_{ts}")
    os.makedirs(pack_dir, exist_ok=True)
    write_pack_doc(os.path.join(pack_dir, "pack.md"), production, style,
                   mj_prompt, mj_motion)
    shutil.copyfile("scene_spec.json", os.path.join(pack_dir, "scene_spec.json"))
    video, zip_path = render_with_blender(ts, pack_dir)
    if video:
        production["status"] = "ready"
        production["video"] = video
        if zip_path:
            production["pack_zip"] = zip_path
        production["vision"] = vision_critique(video)
        prune_videos()
        prune_packs()
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
    history["last_move"] = (pack.get("shot") or {}).get("camera")
    save_json(HISTORY_PATH, history)

    save_json("video.json", {
        "status": production["status"],
        "title": production["story"]["title"],
        "outlet": production["story"]["outlet"],
        "story_link": production["story"]["link"],
        "trend": production["story"]["trend"],
        "prompt": mj_prompt,
        "model": OPENAI_MODEL + " + Blender EEVEE",
        "duration": pack.get("build", {}).get("duration", VIDEO_DURATION),
        "resolution": VIDEO_RES,
        "generated_at": generated_at,
        "video": production.get("video"),
        "pack": production.get("pack_zip"),
        "vision": production.get("vision"),
    })
    save_json("production.json", production)


if __name__ == "__main__":
    main()
