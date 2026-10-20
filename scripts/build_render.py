#!/usr/bin/env python3
"""InkDrop Blender build + render (stylized 3D, EEVEE).

Run headless:

    blender -b --factory-startup -P scripts/build_render.py -- <scene_spec.json> <output.mp4>

Reads the InkDrop production pack (scene_spec.json), constructs a stylized
scene from the primitive catalog - characters, props, palette, lights -
keyframes the camera (24 fps), renders an H.264 mp4, and also saves frame 1
as keyframes/latest.png (the director's cut).

Everything the model may choose is clamped here (positions, scales, colors),
so an imperfect spec can never break the render.
"""

import json
import math
import os
import random
import sys

import bpy
from mathutils import Vector

RES = os.environ.get("VIDEO_RES", "1280x720")
try:
    RES_X, RES_Y = (int(v) for v in RES.lower().split("x"))
except Exception:
    RES_X, RES_Y = 1280, 720
FPS = 24
MIN_DUR, MAX_DUR = 3, 10
BOUNDS = 8.0  # props/characters live inside [-BOUNDS, BOUNDS]
FALLBACK = "#888888"

POSES = ("standing", "reaching", "pointing", "looking_up")
ACCESSORIES = ("none", "hat")
CAMERA_MOVES = ("dolly_in", "dolly_out", "pan_left", "pan_right",
                "orbit_left", "orbit_right", "crane_up", "static")


def hex_rgb(text):
    text = (text or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        text = FALLBACK[1:]
    try:
        return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return (0.53, 0.53, 0.53)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------- materials

_mat_counter = [0]


def material(name_hint, color_hex, rough=0.6, emit=None, emit_strength=1.0):
    _mat_counter[0] += 1
    mat = bpy.data.materials.new(f"{name_hint}_{_mat_counter[0]}")
    mat.use_nodes = True
    tree = mat.node_tree
    bsdf = next(n for n in tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (*hex_rgb(color_hex), 1.0)
    bsdf.inputs["Roughness"].default_value = rough
    if emit is not None:
        bsdf.inputs["Emission Color"].default_value = (*hex_rgb(emit), 1.0)
        bsdf.inputs["Emission Strength"].default_value = emit_strength
    return mat


# ---------------------------------------------------------------- primitives

def prim(kind, name, location=(0, 0, 0), scale=(1, 1, 1), mat=None):
    fn = {
        "cube": bpy.ops.mesh.primitive_cube_add,
        "cylinder": bpy.ops.mesh.primitive_cylinder_add,
        "cone": bpy.ops.mesh.primitive_cone_add,
        "sphere": bpy.ops.mesh.primitive_uv_sphere_add,
        "ico": bpy.ops.mesh.primitive_ico_sphere_add,
        "plane": bpy.ops.mesh.primitive_plane_add,
        "torus": bpy.ops.mesh.primitive_torus_add,
    }[kind]
    fn()
    obj = bpy.context.object
    obj.name = name
    obj.location = location
    obj.scale = scale
    if mat is not None:
        obj.data.materials.append(mat)
    return obj


# ---------------------------------------------------------------- characters

def build_character(spec, pal):
    """Stylized figure: capsule torso, sphere head, eyes, arms, legs, hat."""
    name = spec.get("name", "figure")
    height = clamp(float(spec.get("height", 1.7) or 1.7), 1.2, 2.4)
    body_col = spec.get("body", pal.get("accent", FALLBACK))
    head_col = spec.get("head", "#d8a06a")
    pose = spec.get("pose", "standing")
    if pose not in POSES:
        pose = "standing"
    accessory = spec.get("accessory", "none")
    if accessory not in ACCESSORIES:
        accessory = "none"

    # Character rig: an empty at the ground position; facing = rotation around Z.
    pos = spec.get("position", [0, 0, 0]) or [0, 0, 0]
    facing = float(spec.get("facing", 0) or 0)
    root = bpy.data.objects.new(f"{name}_root", None)
    bpy.context.scene.collection.objects.link(root)
    root.location = (clamp(float(pos[0]), -BOUNDS, BOUNDS),
                     clamp(float(pos[1]), -BOUNDS, BOUNDS), 0.0)
    root.rotation_euler = (0, 0, math.radians(facing))

    body_m = material("char_body", body_col, 0.55)
    skin_m = material("char_skin", head_col, 0.7)
    dark_m = material("char_dark", "#222222", 0.4)
    acc_m = material("char_acc", pal.get("accent", "#c9a227"), 0.5)

    parts = []
    torso = prim("cylinder", f"{name}_torso", (0, 0, height * 0.42),
                 (height * 0.14, height * 0.14, height * 0.24), body_m)
    head = prim("sphere", f"{name}_head", (0, 0, height * 0.86),
                (height * 0.115,) * 3, skin_m)
    # Eyes face +Y (local) so `facing` aims them correctly.
    eye = (height * 0.115,)
    e1 = prim("sphere", f"{name}_eye_l", (height * 0.045, height * 0.10, height * 0.90),
              (height * 0.02,) * 3, dark_m)
    e2 = prim("sphere", f"{name}_eye_r", (-height * 0.045, height * 0.10, height * 0.90),
              (height * 0.02,) * 3, dark_m)
    arm_l = prim("cylinder", f"{name}_arm_l", (height * 0.16, 0, height * 0.56),
                 (height * 0.035, height * 0.035, height * 0.20), body_m)
    arm_r = prim("cylinder", f"{name}_arm_r", (-height * 0.16, 0, height * 0.56),
                 (height * 0.035, height * 0.035, height * 0.20), body_m)
    leg_l = prim("cylinder", f"{name}_leg_l", (height * 0.07, 0, height * 0.13),
                 (height * 0.05, height * 0.05, height * 0.13), dark_m)
    leg_r = prim("cylinder", f"{name}_leg_r", (-height * 0.07, 0, height * 0.13),
                 (height * 0.05, height * 0.05, height * 0.13), dark_m)
    parts += [torso, head, e1, e2, arm_l, arm_r, leg_l, leg_r]

    # Pose: rotate arms/head around local X.
    if pose == "standing":
        arm_l.rotation_euler = (math.radians(150), 0, 0)
        arm_r.rotation_euler = (math.radians(150), 0, 0)
    elif pose == "reaching":
        arm_l.rotation_euler = (math.radians(150), 0, 0)
        arm_r.rotation_euler = (math.radians(60), 0, 0)
    elif pose == "pointing":
        arm_l.rotation_euler = (math.radians(150), 0, 0)
        arm_r.rotation_euler = (math.radians(95), 0, math.radians(90))
    elif pose == "looking_up":
        arm_l.rotation_euler = (math.radians(150), 0, 0)
        arm_r.rotation_euler = (math.radians(150), 0, 0)
        head.rotation_euler = (math.radians(-25), 0, 0)
        e1.rotation_euler = (math.radians(-25), 0, 0)
        e2.rotation_euler = (math.radians(-25), 0, 0)

    if accessory == "hat":
        brim = prim("cylinder", f"{name}_hat_brim", (0, 0, height * 0.995),
                    (height * 0.16, height * 0.16, height * 0.012), acc_m)
        top = prim("cylinder", f"{name}_hat_top", (0, 0, height * 1.03),
                   (height * 0.09, height * 0.09, height * 0.05), acc_m)
        parts += [brim, top]

    for obj in parts:
        obj.parent = root
    return root


# ---------------------------------------------------------------- props catalog

def _post(name, loc, h, r, mat):
    return prim("cylinder", name, loc, (r, r, h / 2), mat)


def prop_table(name, pos, s, color, pal):
    top = prim("cube", name, (0, 0, 0.9 * s), (0.9 * s, 0.5 * s, 0.06 * s),
               material("prop", color, 0.7))
    objs = [top]
    for dx in (-0.75, 0.75):
        for dy in (-0.4, 0.4):
            objs.append(prim("cylinder", f"{name}_leg", (dx * s, dy * s, 0.45 * s),
                             (0.03 * s, 0.03 * s, 0.45 * s), top.data.materials[0]))
    return objs


def prop_chair(name, pos, s, color, pal):
    m = material("prop", color, 0.7)
    seat = prim("cube", name, (0, 0, 0.45 * s), (0.32 * s, 0.32 * s, 0.04 * s), m)
    back = prim("cube", f"{name}_back", (-0.28 * s, 0, 0.75 * s),
                (0.04 * s, 0.32 * s, 0.36 * s), m)
    legs = [prim("cylinder", f"{name}_leg{i}", (dx * 0.26 * s, dy * 0.26 * s, 0.2 * s),
                 (0.025 * s, 0.025 * s, 0.2 * s), m)
            for i, (dx, dy) in enumerate(((1, 1), (1, -1), (-1, 1), (-1, -1)))]
    return [seat, back] + legs


def prop_window(name, pos, s, color, pal):
    frame = material("prop", color, 0.7)
    glass = material("prop_emit", "#aee2ff", 0.2, emit="#dfefff", emit_strength=2.5)
    objs = []
    for i, (dx, dy, sx, sy) in enumerate(((0, 0.82, 1.1, 0.08), (0, -0.82, 1.1, 0.08),
                                          (0.82, 0, 0.08, 1.1), (-0.82, 0, 0.08, 1.1),
                                          (0, 0, 0.06, 0.88), (0, 0, 0.88, 0.06))):
        objs.append(prim("cube", f"{name}_f{i}", (dx * s, dy * s, s),
                         (sx * s / 2, sy * s / 2, s), frame))
    objs.append(prim("cube", f"{name}_glass", (0, 0, s), (0.8 * s, 0.8 * s, 0.02 * s), glass))
    return objs


def prop_tree(name, pos, s, color, pal):
    trunk = prim("cylinder", name, (0, 0, 0.55 * s), (0.07 * s, 0.07 * s, 0.55 * s),
                 material("prop", "#5b4326", 0.8))
    leaf = material("prop_leaf", color, 0.7)
    return [trunk] + [
        prim("cone", f"{name}_leaf{i}", (0, 0, (0.85 + 0.28 * i) * s),
             (0.45 * s * (1 - 0.18 * i), 0.45 * s * (1 - 0.18 * i), 0.3 * s), leaf)
        for i in range(3)]


def prop_building(name, pos, s, color, pal):
    body = prim("cube", name, (0, 0, 0.5 * s), (0.5 * s, 0.5 * s, 0.5 * s),
                material("prop", color, 0.75))
    win = material("prop_win", "#ffe9a8", 0.2, emit="#fff2c0", emit_strength=2.0)
    rnd = random.Random(f"{name}{pos}")
    objs = [body]
    for _ in range(6):
        wx, wy = rnd.uniform(-0.3, 0.3) * s, rnd.uniform(-0.3, 0.3) * s
        wz = rnd.uniform(0.35, 0.9) * s
        side = rnd.choice((0, 1))
        objs.append(prim("cube", f"{name}_w",
                         ((wx, 0.5 * s + 0.01, wz) if side else (0.5 * s + 0.01, wy, wz)),
                         (0.1 * s, 0.02 * s, 0.12 * s) if side
                         else (0.02 * s, 0.1 * s, 0.12 * s), win))
    return objs


def prop_lamppost(name, pos, s, color, pal):
    pole = prim("cylinder", name, (0, 0, 0.6 * s), (0.02 * s, 0.02 * s, 0.6 * s),
                material("prop", "#3a3a3a", 0.6))
    lamp = prim("sphere", f"{name}_lamp", (0, 0, 1.25 * s), (0.09 * s,) * 3,
                material("prop_emit", "#ffe9a8", 0.2, emit="#fff2c0", emit_strength=4.0))
    return [pole, lamp]


def prop_bench(name, pos, s, color, pal):
    m = material("prop", color, 0.7)
    seat = prim("cube", name, (0, 0, 0.45 * s), (0.5 * s, 0.16 * s, 0.04 * s), m)
    back = prim("cube", f"{name}_back", (-0.13 * s, 0, 0.75 * s),
                (0.04 * s, 0.16 * s, 0.35 * s), m)
    legs = [prim("cube", f"{name}_leg{i}", (dx * 0.38 * s, dy * 0.08 * s, 0.2 * s),
                 (0.05 * s, 0.05 * s, 0.2 * s), m)
            for i, (dx, dy) in enumerate(((1, 1), (1, -1), (-1, 1), (-1, -1)))]
    return [seat, back] + legs


def prop_rock(name, pos, s, color, pal):
    return [prim("ico", name, (0, 0, 0.28 * s), (0.4 * s, 0.32 * s, 0.25 * s),
                 material("prop", color, 0.9))]


def prop_cloud(name, pos, s, color, pal):
    m = material("prop_cloud", "#f2f5f7", 0.9)
    rnd = random.Random(f"cloud{pos}")
    objs = []
    for i in range(4):
        objs.append(prim("sphere", f"{name}_{i}",
                         (rnd.uniform(-0.25, 0.25) * s, rnd.uniform(-0.15, 0.15) * s,
                          rnd.uniform(-0.08, 0.08) * s),
                         (rnd.uniform(0.22, 0.4) * s,) * 3, m))
    return objs


def prop_fence(name, pos, s, color, pal):
    m = material("prop", color, 0.7)
    objs = []
    for i in range(4):
        objs.append(prim("cylinder", f"{name}_p{i}", (i * 0.3 * s - 0.45 * s, 0, 0.5 * s),
                         (0.025 * s, 0.025 * s, 0.5 * s), m))
    objs += [prim("cube", f"{name}_rail{j}", (0, 0, (0.35 + 0.3 * j) * s),
                  (0.95 * s / 2, 0.04 * s, 0.04 * s), m) for j in range(2)]
    return objs


def prop_car(name, pos, s, color, pal):
    body = prim("cube", name, (0, 0, 0.2 * s), (0.5 * s, 0.22 * s, 0.12 * s),
                material("prop", color, 0.35))
    cabin = prim("cube", f"{name}_cabin", (0, 0, 0.36 * s), (0.26 * s, 0.2 * s, 0.1 * s),
                 material("prop_glass", "#bcd8e8", 0.2))
    wheels = [prim("cylinder", f"{name}_w{i}",
                   (dx * 0.2 * s, dy * 0.2 * s, 0.06 * s),
                   (0.06 * s, 0.06 * s, 0.02 * s), material("prop", "#222222", 0.6))
              for i, (dx, dy) in enumerate(((1, 1), (1, -1), (-1, 1), (-1, -1)))]
    return [body, cabin] + wheels


def prop_boat(name, pos, s, color, pal):
    hull = prim("cube", name, (0, 0, 0.12 * s), (0.5 * s, 0.2 * s, 0.1 * s),
                material("prop", color, 0.7))
    mast = prim("cylinder", f"{name}_mast", (0, 0, 0.5 * s), (0.015 * s, 0.015 * s, 0.4 * s),
                material("prop", "#5b4326", 0.8))
    sail = prim("cube", f"{name}_sail", (0.06 * s, 0, 0.5 * s), (0.01 * s, 0.14 * s, 0.3 * s),
                material("prop_sail", "#f4f1ea", 0.9))
    return [hull, mast, sail]


def prop_sign(name, pos, s, color, pal):
    pole = prim("cylinder", name, (0, 0, 0.45 * s), (0.02 * s, 0.02 * s, 0.45 * s),
                material("prop", "#3a3a3a", 0.6))
    board = prim("cube", f"{name}_board", (0, 0, 0.75 * s), (0.35 * s, 0.03 * s, 0.2 * s),
                 material("prop", color, 0.6))
    return [pole, board]


def prop_plant(name, pos, s, color, pal):
    pot = prim("cylinder", name, (0, 0, 0.12 * s), (0.1 * s, 0.1 * s, 0.12 * s),
               material("prop", "#8a5a3b", 0.8))
    leaf = material("prop_leaf", color, 0.7)
    return [pot] + [
        prim("sphere", f"{name}_leaf{i}",
             ((0.12 * (i % 2) - 0.06) * s, 0, (0.28 + 0.12 * (i // 2)) * s),
             (0.09 * s,) * 3, leaf) for i in range(4)]


def prop_stairs(name, pos, s, color, pal):
    m = material("prop", color, 0.75)
    return [prim("cube", f"{name}_step{i}", (i * 0.22 * s, 0, (0.12 + 0.09 * i) * s),
                 (0.22 * s, 0.5 * s, 0.09 * s), m) for i in range(4)]


def prop_tv(name, pos, s, color, pal):
    stand = prim("cube", name, (0, 0, 0.12 * s), (0.3 * s, 0.08 * s, 0.12 * s),
                 material("prop", color, 0.7))
    screen = prim("cube", f"{name}_screen", (0, 0, 0.35 * s), (0.34 * s, 0.02 * s, 0.2 * s),
                  material("prop_emit", "#9fd8ff", 0.2, emit="#bfe6ff", emit_strength=2.0))
    return [stand, screen]


def prop_sofa(name, pos, s, color, pal):
    m = material("prop", color, 0.8)
    base = prim("cube", name, (0, 0, 0.2 * s), (0.5 * s, 0.22 * s, 0.16 * s), m)
    back = prim("cube", f"{name}_back", (-0.15 * s, 0, 0.42 * s), (0.06 * s, 0.2 * s, 0.22 * s), m)
    arms = [prim("cube", f"{name}_arm{i}", (0, dy * 0.22 * s, 0.28 * s),
                 (0.5 * s, 0.06 * s, 0.12 * s), m) for i, dy in enumerate((1, -1))]
    return [base, back] + arms


def prop_bookshelf(name, pos, s, color, pal):
    m = material("prop", color, 0.7)
    objs = [prim("cube", name, (0, 0, 0.55 * s), (0.3 * s, 0.1 * s, 0.55 * s), m)]
    for j in range(3):
        objs.append(prim("cube", f"{name}_shelf{j}", (0, 0, (0.22 + 0.22 * j) * s),
                         (0.28 * s, 0.09 * s, 0.02 * s), m))
    rnd = random.Random(f"books{name}")
    for j in range(3):
        bx = rnd.uniform(-0.12, 0.12) * s
        for k in range(3):
            objs.append(prim("cube", f"{name}_book{j}_{k}",
                             (bx, 0, (0.26 + 0.22 * j) * s),
                             (0.02 * s, 0.04 * s, 0.09 * s),
                             material("prop_book", rnd.choice((pal.get("accent", "#c9a227"),
                                                              "#a33b3b", "#3b6ea3")), 0.7)))
    return objs


def prop_bush(name, pos, s, color, pal):
    m = material("prop_leaf", color, 0.7)
    return [prim("ico", f"{name}_{i}", ((0.14 * (i % 2) - 0.07) * s, 0, 0.14 * s),
                 (0.18 * s, 0.14 * s, 0.18 * s), m) for i in range(3)]


def prop_door(name, pos, s, color, pal):
    door = prim("cube", name, (0, 0, 0.5 * s), (0.24 * s, 0.04 * s, 0.5 * s),
                material("prop", color, 0.6))
    knob = prim("sphere", f"{name}_knob", (0, 0.06 * s, 0.45 * s), (0.02 * s,) * 3,
                material("prop", "#e8c04a", 0.3))
    return [door, knob]


def prop_skyline(name, pos, s, color, pal):
    """Row of dark buildings with lit windows - background depth."""
    rnd = random.Random(f"skyline{pos}")
    m = material("prop_sky", "#2a3442", 0.85)
    win = material("prop_win", "#ffd98a", 0.2, emit="#ffd98a", emit_strength=1.6)
    objs = []
    for i in range(7):
        w = rnd.uniform(0.5, 1.1) * s
        h = rnd.uniform(1.2, 2.8) * s
        x = (i - 3) * 0.95 * s
        objs.append(prim("cube", f"{name}_b{i}", (x, 0, h / 2), (w / 2, 0.5 * s, h / 2), m))
        for k in range(max(2, int(h // (0.35 * s)))):
            objs.append(prim("cube", f"{name}_w{i}_{k}",
                             (x + rnd.uniform(-w * 0.3, w * 0.3), 0.5 * s + 0.01,
                              rnd.uniform(0.25, 0.9) * h),
                             (0.06 * s, 0.02 * s, 0.08 * s), win))
    return objs


def prop_hills(name, pos, s, color, pal):
    """Soft blobby hills at the horizon - muted color from palette."""
    rnd = random.Random(f"hills{pos}")
    m = material("prop_hill", color, 0.95)
    return [prim("ico", f"{name}_{i}",
                 (rnd.uniform(-3.0, 3.0) * s, rnd.uniform(-0.5, 0.5) * s, 0.25 * s),
                 (rnd.uniform(1.6, 2.6) * s, rnd.uniform(1.4, 2.2) * s,
                  rnd.uniform(0.5, 0.9) * s), m)
            for i in range(4)]


CATALOG = {
    "table": prop_table, "chair": prop_chair, "window": prop_window,
    "door": prop_door, "tree": prop_tree, "bush": prop_bush,
    "building": prop_building, "lamppost": prop_lamppost, "bench": prop_bench,
    "rock": prop_rock, "cloud": prop_cloud, "fence": prop_fence,
    "car": prop_car, "boat": prop_boat, "sign": prop_sign,
    "plant": prop_plant, "stairs": prop_stairs, "tv": prop_tv,
    "sofa": prop_sofa, "bookshelf": prop_bookshelf,
    "skyline": prop_skyline, "hills": prop_hills,
}


def fov_to_lens(fov_deg):
    return 24.0 / (2.0 * math.tan(math.radians(fov_deg) / 2.0))


# ---------------------------------------------------------------- scene

def build_scene(spec):
    build = spec.get("build", {})
    env = spec.get("environment", {}) or {}
    pal = build.get("palette", {}) or {}

    # Fresh scene: drop factory-startup objects.
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    # World from palette sky (flat first, gradient added when nodes allow).
    world = bpy.data.worlds.new("InkDropWorld")
    world.use_nodes = True
    tree = world.node_tree
    bg = next(n for n in tree.nodes if n.type == "BACKGROUND")
    sky_col = (*hex_rgb(pal.get("sky", "#8fb8d8")), 1.0)
    bg.inputs["Color"].default_value = sky_col
    try:  # vertical sky gradient: lighter near the horizon
        coord = tree.nodes.new(type="ShaderNodeTexCoord")
        mapping = tree.nodes.new(type="ShaderNodeMapping")
        mapping.inputs["Rotation"].default_value = (0.0, 0.0, math.radians(90))
        grad = tree.nodes.new(type="ShaderNodeTexGradient")
        grad.gradient_type = "LINEAR"
        ramp = tree.nodes.new(type="ShaderNodeValToRGB")
        ramp.color_ramp.elements[0].color = (*sky_col[:3], 1.0)
        horizon = tuple(min(1.0, c * 1.35) for c in sky_col[:3])
        ramp.color_ramp.elements[1].color = (*horizon, 1.0)
        tree.links.new(coord.outputs["Generated"], mapping.inputs["Vector"])
        tree.links.new(mapping.outputs["Vector"], grad.inputs["Vector"])
        tree.links.new(grad.outputs["Fac"], ramp.inputs["Fac"])
        tree.links.new(ramp.outputs["Color"], bg.inputs["Color"])
    except Exception:
        pass  # flat sky is fine
    bpy.context.scene.world = world

    # Ground.
    prim("plane", "ground", (0, 0, 0), (BOUNDS * 3, BOUNDS * 3, 1),
         material("ground", pal.get("ground", "#4a5d3a"), 0.95))

    # Lights.
    lights = build.get("lights", {}) or {}
    sun_dir = Vector(lights.get("sun_dir", [-0.6, -0.8, 1.2]) or [-0.6, -0.8, 1.2])
    try:
        sun_dir.normalize()
    except Exception:
        sun_dir = Vector((-0.6, -0.8, 1.2)).normalized()
    sun_data = bpy.data.lights.new("InkDropSun", type="SUN")
    sun_data.energy = clamp(float(lights.get("sun_strength", 3.0) or 3.0), 0.5, 8.0)
    sun_data.color = hex_rgb(lights.get("sun_color", "#fff2d9"))
    sun_obj = bpy.data.objects.new("sun", sun_data)
    bpy.context.scene.collection.objects.link(sun_obj)
    sun_obj.rotation_euler = sun_dir.to_track_quat("-Z", "Y").to_euler()
    try:  # softer, grounded shadows (EEVEE contact shadows + sun disk angle)
        sun_data.angle = math.radians(8.0)
        sun_data.use_contact_shadow = True
        sun_data.contact_shadow_distance = 12.0
        sun_data.contact_shadow_thickness = 0.35
    except Exception:
        pass

    fill_strength = clamp(float(lights.get("fill", 0.3) or 0.3), 0.0, 2.0)
    if fill_strength > 0:
        fill_data = bpy.data.lights.new("InkDropFill", type="AREA")
        fill_data.energy = fill_strength * 40
        fill_data.size = 30
        fill_obj = bpy.data.objects.new("fill", fill_data)
        bpy.context.scene.collection.objects.link(fill_obj)
        fill_obj.location = (0, 0, 14)
        fill_obj.rotation_euler = (math.radians(180), 0, 0)

    # Characters.
    for c in build.get("characters", []) or []:
        build_character(c, pal)

    # Props (clamped into bounds, zeroed at z=0).
    for p in build.get("props", []) or []:
        ptype = p.get("type", "")
        if ptype not in CATALOG:
            continue
        pos = p.get("position", [0, 0, 0]) or [0, 0, 0]
        s = clamp(float(p.get("scale", 1.0) or 1.0), 0.3, 3.0)
        color = p.get("color", pal.get("accent", FALLBACK))
        loc = (clamp(float(pos[0]), -BOUNDS, BOUNDS),
               clamp(float(pos[1]), -BOUNDS, BOUNDS), 0.0)
        for obj in CATALOG[ptype](f"prop_{ptype}", loc, s, color, pal):
            obj.location = (obj.location[0] + loc[0],
                            obj.location[1] + loc[1],
                            obj.location[2])

    # Cloudy weather: scatter a few clouds; rain just darkens (palette does it).
    weather = (env.get("weather", "") or "").lower()
    if "cloud" in weather or "overcast" in weather:
        rnd = random.Random(7)
        for i in range(4):
            obj_list = CATALOG["cloud"](f"cloud_{i}",
                                        (rnd.uniform(-5, 5), rnd.uniform(-5, 5), 0),
                                        1.6, "#ffffff", pal)
            for obj in obj_list:
                obj.location = (obj.location[0] + rnd.uniform(-5, 5),
                                obj.location[1] + rnd.uniform(-5, 5),
                                obj.location[2] + rnd.uniform(3.2, 4.5))

    # Camera + tracked target.
    cam = build.get("camera", {}) or {}
    start = cam.get("start", {}) or {}
    end = cam.get("end", {}) or {}

    def v3(d, fallback):
        vals = d or fallback
        try:
            return [float(x) for x in vals[:3]]
        except Exception:
            return list(fallback)

    start_pos = v3(start.get("pos"), [5.5, -6.5, 2.6])
    start_tgt = v3(start.get("target"), [0, 0, 1.2])
    end_pos = v3(end.get("pos"), start_pos)
    end_tgt = v3(end.get("target"), start_tgt)
    for i, (p, t) in enumerate(zip((start_pos, end_pos, start_tgt, end_tgt),
                                   ((12, 12, 8), (12, 12, 8), (BOUNDS, BOUNDS, 5), (BOUNDS, BOUNDS, 5)))):
        if i < 2:
            p[0], p[1], p[2] = clamp(p[0], -12, 12), clamp(p[1], -12, 12), clamp(p[2], 0.3, 8)
        else:
            p[0], p[1], p[2] = clamp(p[0], -BOUNDS, BOUNDS), clamp(p[1], -BOUNDS, BOUNDS), clamp(p[2], 0.1, 5)

    cam_data = bpy.data.cameras.new("InkDropCam")
    cam_data.sensor_fit = "VERTICAL"
    cam_data.lens_unit = "FOV"
    fov0 = clamp(float(start.get("fov", 50) or 50), 24, 70)
    fov1 = clamp(float(end.get("fov", fov0) or fov0), 24, 70)
    cam_data.lens = fov_to_lens(fov0)
    cam_obj = bpy.data.objects.new("camera", cam_data)
    bpy.context.scene.collection.objects.link(cam_obj)
    cam_obj.location = start_pos
    bpy.context.scene.camera = cam_obj

    tgt = bpy.data.objects.new("cam_target", None)
    bpy.context.scene.collection.objects.link(tgt)
    tgt.location = start_tgt
    track = cam_obj.constraints.new(type="TRACK_TO")
    track.target = tgt
    track.track_axis = "TRACK_NEGATIVE_Z"
    track.up_axis = "UP_Y"

    duration = clamp(int(float(spec.get("build", {}).get("duration", 5) or 5)),
                     MIN_DUR, MAX_DUR)
    return cam_obj, tgt, cam_data, fov1, duration


# ---------------------------------------------------------------- render

def set_easing(owner, data_path, mode):
    """Sine/linear easing on keyframes; skipped on slotted actions (5.x)."""
    if not owner.animation_data or not owner.animation_data.action:
        return
    action = owner.animation_data.action
    if not hasattr(action, "fcurves"):
        return  # slotted action: default bezier easing is fine
    for fc in action.fcurves:
        if fc.data_path == data_path:
            for kf in fc.keyframe_points:
                kf.interpolation = mode


def main():
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    spec_path = args[0] if args else "scene_spec.json"
    out_path = args[1] if len(args) > 1 else "videos/inkdrop_out.mp4"
    pack_dir = args[2] if len(args) > 2 else "scene-packs/latest"
    # Absolute: after saving the .blend inside pack_dir, Blender would resolve
    # relative render paths against the blend file's folder.
    pack_dir = os.path.abspath(pack_dir)
    refs_dir = os.path.join(pack_dir, "refs")
    os.makedirs(refs_dir, exist_ok=True)
    with open(spec_path, encoding="utf-8") as fh:
        spec = json.load(fh)
    pack = spec.get("pack", spec)

    cam_obj, tgt, cam_data, fov_end, duration = build_scene(pack)
    scene = bpy.context.scene

    try:
        scene.render.engine = "BLENDER_EEVEE"
    except TypeError:
        engines = [i.identifier for i in
                   bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
        scene.render.engine = next(e for e in engines if "EEVEE" in e.upper())
    scene.render.resolution_x = RES_X
    scene.render.resolution_y = RES_Y
    scene.render.fps = FPS
    scene.frame_start = 1
    scene.frame_end = duration * FPS
    scene.render.film_transparent = False

    # Prettier picture: AgX color, punchier look, gentle bloom on emissives.
    try:
        scene.view_settings.view_transform = "AgX"
    except Exception:
        pass
    try:
        scene.view_settings.look = "Medium High Contrast"
    except Exception:
        pass
    try:
        scene.eevee.use_bloom = True
        scene.eevee.bloom_intensity = 0.05
        scene.eevee.bloom_threshold = 0.8
        scene.eevee.bloom_radius = 4.0
    except Exception:
        pass

    # Camera animation: position/aim/fov keyframed with sine easing.
    build = pack.get("build", {})
    cam = build.get("camera", {}) or {}
    end = cam.get("end", {}) or {}
    start = cam.get("start", {}) or {}
    end_pos = [float(x) for x in (end.get("pos") or start.get("pos") or [5.5, -6.5, 2.6])[:3]]
    end_tgt = [float(x) for x in (end.get("target") or start.get("target") or [0, 0, 1.2])[:3]]
    for i in range(3):
        end_pos[i] = clamp(end_pos[i], -12, 12)
        end_tgt[i] = clamp(end_tgt[i], -BOUNDS if i < 2 else 0.1, BOUNDS if i < 2 else 5)
    end_pos[2] = clamp(end_pos[2], 0.3, 8)

    cam_obj.keyframe_insert(data_path="location", frame=1)
    tgt.keyframe_insert(data_path="location", frame=1)
    cam_data.keyframe_insert(data_path="lens", frame=1)
    cam_obj.location = end_pos
    tgt.location = end_tgt
    cam_data.lens = fov_to_lens(fov_end)
    cam_obj.keyframe_insert(data_path="location", frame=scene.frame_end)
    tgt.keyframe_insert(data_path="location", frame=scene.frame_end)
    cam_data.keyframe_insert(data_path="lens", frame=scene.frame_end)
    set_easing(cam_obj, "location", "SINE")
    set_easing(tgt, "location", "SINE")
    set_easing(cam_data, "lens", "LINEAR")

    # Scene pack: keep the .blend and render reference stills for other AIs.
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(pack_dir, "scene.blend"))

    def ref_camera(name, pos, target, fov):
        cdata = bpy.data.cameras.new(name)
        cdata.sensor_fit = "VERTICAL"
        cdata.lens_unit = "FOV"
        cdata.lens = fov_to_lens(fov)
        cam = bpy.data.objects.new(name, cdata)
        bpy.context.scene.collection.objects.link(cam)
        cam.location = pos
        direction = Vector(target) - Vector(pos)
        cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
        return cam

    bchars = pack.get("build", {}).get("characters", []) or []
    close_pos = (bchars[0].get("position") or [0, 0])[:2] if bchars else [0, 0]
    refs = [
        ("ref_front", (4.2, -6.8, 2.2), (0, 0, 1.1), 52),
        ("ref_high", (0.2, -9.8, 7.6), (0, 0, 0.3), 44),
        ("ref_close", (close_pos[0] + 1.3, close_pos[1] - 2.4, 1.5),
         (close_pos[0], close_pos[1], 1.2), 45),
    ]
    scene.frame_set(1)
    scene.render.image_settings.file_format = "PNG"
    for name, pos, target, fov in refs:
        rcam = ref_camera(name, pos, target, fov)
        scene.camera = rcam
        scene.render.filepath = os.path.join(refs_dir, f"{name}.png")
        bpy.ops.render.render(write_still=True)
    scene.camera = cam_obj

    # Director's cut: frame 1 as keyframes/latest.png.
    scene.frame_set(1)
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = os.path.abspath("keyframes/latest.png")
    bpy.ops.render.render(write_still=True)

    # Full render: PNG sequence (this Blender build has no FFMPEG format);
    # the pipeline muxes frames/ to mp4 with the system ffmpeg.
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = os.path.abspath(os.path.join("frames", "f"))
    bpy.ops.render.render(animation=True)

    print(json.dumps({"status": "frames", "frames_dir": os.path.abspath("frames"),
                      "frames": scene.frame_end, "fps": FPS,
                      "resolution": [RES_X, RES_Y]}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # report cleanly so the pipeline can fail fast
        print(json.dumps({"status": "error", "error": repr(exc)}))
        sys.exit(1)
