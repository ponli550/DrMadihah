"""The recipe: what an AI writes, and what umcares renders.

Division of labour:

  * The **AI** looks at the media (see `umcares inspect`) and declares intent —
    scenes, narration text, which clip or stills go where, what each card says.
    It does NOT compute timings.
  * **umcares** resolves that intent into exact frame positions, renders the
    missing pieces, and builds the timeline. Deterministic and repeatable: the
    same recipe always produces the same cut.

Why the split matters: every timing bug this pipeline has had came from a human
or model doing arithmetic by hand — narration that outran its scene, music
lifted before a sentence finished, 23 seconds of black nobody noticed. Durations
are *measured* here (from the rendered voiceover and the probed clips), never
guessed.

A scene declares `narration` and a list of `visuals`. Visual durations may be
given explicitly; anything left open is stretched so the scene covers its
narration plus `pad`.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import log

# ---------------------------------------------------------------- loading --
def load(path: Path) -> dict:
    """Load a recipe. YAML when PyYAML is present, otherwise JSON."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yml", ".yaml"):
        try:
            import yaml  # optional
        except ImportError:
            raise SystemExit(
                f"{path.name} is YAML but PyYAML is not installed.\n"
                "  pip install pyyaml     (or write the recipe as .json)")
        return yaml.safe_load(text) or {}
    return json.loads(text or "{}")


def save(recipe: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in (".yml", ".yaml"):
        try:
            import yaml
            path.write_text(yaml.safe_dump(recipe, sort_keys=False,
                                           allow_unicode=True), encoding="utf-8")
            return path
        except ImportError:
            path = path.with_suffix(".json")
    path.write_text(json.dumps(recipe, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def apply_defaults(recipe: dict, defaults: dict) -> dict:
    """Fill a recipe's blanks from config. The recipe always wins.

    Precedence is recipe > config > built-in default, so house style lives in
    config and a one-off override lives in the recipe that needs it — neither
    has to be restated in the other. Merging is one level deep into each block,
    which is as deep as these settings nest.
    """
    out = dict(recipe)
    for block, values in defaults.items():
        if not isinstance(values, dict):
            continue
        merged = dict(values)
        merged.update(out.get(block) or {})
        out[block] = merged
    return out


# ------------------------------------------------------------- validation --
VISUAL_KINDS = ("clip", "card", "kenburns", "still", "black")


def validate(recipe: dict, manifest: dict | None = None,
             durations: dict | None = None) -> list:
    """Return a list of problems. Empty means the recipe is renderable.

    When a manifest is supplied, referenced media is checked for existence and
    for the VP9 trap, so a bad reference is caught before any rendering starts.
    When durations are supplied, every referenced asset is checked for a
    measured duration, so missing or zero-length entries are caught early.
    """
    problems = []
    durations = durations or {}
    check_durations = bool(durations)
    AUDIO_MODES = ("keep", "duck", "mute")
    known, unusable = set(), set()
    if manifest:
        for item in manifest.get("items", []):
            known.add(item["file"])
            if item.get("kind") == "video" and not item.get("premiere_usable", True):
                unusable.add(item["file"])

    if not recipe.get("scenes"):
        problems.append("recipe has no `scenes`")

    cards = recipe.get("cards") or {}
    seen_ids = set()

    for i, scene in enumerate(recipe.get("scenes") or []):
        sid = scene.get("id") or f"scene[{i}]"
        if not scene.get("id"):
            problems.append(f"{sid}: missing `id`")
        if sid in seen_ids:
            problems.append(f"{sid}: duplicate id")
        seen_ids.add(sid)

        visuals = scene.get("visuals") or []
        if not visuals:
            problems.append(f"{sid}: no `visuals` — the scene would be black")

        for j, v in enumerate(visuals):
            kinds = [k for k in VISUAL_KINDS if k in v]
            if len(kinds) != 1:
                problems.append(
                    f"{sid}.visuals[{j}]: expected exactly one of {VISUAL_KINDS}, got {kinds}")
                continue
            kind = kinds[0]

            if kind == "clip" and manifest:
                name = v["clip"]
                if name not in known:
                    problems.append(f"{sid}.visuals[{j}]: clip `{name}` not in the media dir")
                elif name in unusable:
                    problems.append(
                        f"{sid}.visuals[{j}]: `{name}` is not H.264 — Premiere would "
                        f"import it as audio only. Run `umcares media prepare` first.")

            mode = v.get("audio", "mute")
            if mode not in AUDIO_MODES:
                problems.append(
                    f"{sid}.visuals[{j}]: audio `{mode}` must be one of {AUDIO_MODES}")

            if kind == "card" and v["card"] not in cards:
                problems.append(f"{sid}.visuals[{j}]: card `{v['card']}` is not defined")

            # compute the same key the resolver uses
            if kind == "clip":
                key = f"clip:{v['clip']}"
            elif kind == "card":
                key = f"card:{v['card']}"
            elif kind == "kenburns":
                ref = (v["kenburns"] or {}).get("id") or f"{sid}_kb{j}"
                key = f"kenburns:{ref}"
            else:
                key = f"{kind}:{v.get(kind)}"

            # duration checks: missing durations produce black frames
            if check_durations and kind != "black":
                if key not in durations:
                    problems.append(
                        f"{sid}.visuals[{j}]: no measured duration for `{key}` — "
                        f"run the relevant render stage first")
                elif float(durations.get(key) or 0) <= 0:
                    problems.append(
                        f"{sid}.visuals[{j}]: duration for `{key}` is zero — "
                        f"re-render or check the source")

            if kind == "kenburns":
                photos = (v["kenburns"] or {}).get("photos") or []
                if len(photos) < 2:
                    problems.append(f"{sid}.visuals[{j}]: kenburns needs >= 2 photos")
                if manifest:
                    for ph in photos:
                        if ph not in known:
                            problems.append(
                                f"{sid}.visuals[{j}]: photo `{ph}` not in the media dir")

        # narration and extra audio also need real durations
        if check_durations and scene.get("narration"):
            vo_key = f"vo:{sid}"
            if vo_key not in durations:
                problems.append(
                    f"{sid}: narration has no measured duration for `{vo_key}` — "
                    f"run the `voice` stage first")
            elif float(durations.get(vo_key) or 0) <= 0:
                problems.append(f"{sid}: narration duration for `{vo_key}` is zero")

        if check_durations:
            for k, extra in enumerate(scene.get("audio") or []):
                akey = f"audio:{extra['file']}"
                if akey not in durations:
                    problems.append(
                        f"{sid}.audio[{k}]: no measured duration for `{akey}`")
                elif float(durations.get(akey) or 0) <= 0:
                    problems.append(
                        f"{sid}.audio[{k}]: duration for `{akey}` is zero")

    music = recipe.get("music") or {}
    if music.get("file") and music.get("ducking"):
        last = 0
        for k, entry in enumerate(music["ducking"]):
            if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
                problems.append(f"music.ducking[{k}]: expected [until_seconds, dB]")
                continue
            if entry[0] <= last:
                problems.append(
                    f"music.ducking[{k}]: boundary {entry[0]} must increase (previous {last})")
            last = entry[0]

    return problems


# --------------------------------------------------------------- resolving --
def resolve(recipe: dict, durations: dict) -> dict:
    """Turn declared intent into an exact timeline.

    `durations` maps an asset key to measured seconds:
        {"vo:s1_pembukaan": 4.68, "clip:C0006.mp4": 7.2, "card:open": 11.0, ...}

    Anything missing is treated as 0 and reported, rather than silently
    producing a gap — black frames are the failure mode we keep hitting.
    """
    fps = float((recipe.get("meta") or {}).get("fps") or 50)
    pad = float((recipe.get("meta") or {}).get("scene_pad") or 0.5)
    vo_lead = float((recipe.get("meta") or {}).get("narration_lead") or 0.5)

    video, audio, missing, short = [], [], [], []
    t = 0.0
    timeline = []

    for scene in recipe.get("scenes") or []:
        sid = scene["id"]
        scene_start = t

        vo_key = f"vo:{sid}"
        vo_len = float(durations.get(vo_key) or 0)
        if scene.get("narration") and not vo_len:
            missing.append(vo_key)

        # place visuals back to back
        fixed, open_slots = [], []
        for v in scene.get("visuals") or []:
            kind = next(k for k in VISUAL_KINDS if k in v)
            if kind == "clip":
                key, ref = f"clip:{v['clip']}", v["clip"]
            elif kind == "card":
                key, ref = f"card:{v['card']}", v["card"]
            elif kind == "kenburns":
                ref = v["kenburns"].get("id") or f"{sid}_kb{len(fixed)}"
                key = f"kenburns:{ref}"
            else:
                key, ref = f"{kind}:{v.get(kind)}", v.get(kind)

            dur = v.get("duration")
            if dur is None:
                dur = durations.get(key)
            if dur is None:
                open_slots.append(len(fixed))
                dur = 0.0
                missing.append(key)
            fixed.append({"kind": kind, "key": key, "ref": ref,
                          "duration": float(dur), "spec": v,
                          # keep = its own audio leads (a testimonial),
                          # duck = present but under narration, mute = silent
                          "audio": v.get("audio", "mute")})

        # A scene must last at least as long as its narration plus the pad.
        #
        # Stretching is capped at each asset's REAL length. A clip asked to run
        # longer than it is does not stretch, it ends -- and the rest of the
        # slot is black. That is the single failure mode this resolver exists to
        # make impossible, so an uncoverable scene is reported, never rendered.
        def cap(f):
            real = durations.get(f["key"])
            return float(real) if real else None

        need = (vo_lead + vo_len + pad) if vo_len else 0.0
        have = sum(f["duration"] for f in fixed)
        targets = open_slots or ([len(fixed) - 1] if fixed else [])
        while need > have + 1e-6 and targets:
            room = []
            for idx in targets:
                c = cap(fixed[idx])
                if c is None or c > fixed[idx]["duration"] + 1e-6:
                    room.append(idx)
            if not room:
                break
            share = (need - have) / len(room)
            grew = 0.0
            for idx in room:
                c = cap(fixed[idx])
                add = share if c is None else min(share, c - fixed[idx]["duration"])
                fixed[idx]["duration"] += add
                grew += add
            have += grew
            if grew < 1e-6:
                break

        if need > have + 0.05:
            short.append({
                "scene": sid,
                "narration": round(vo_len, 2),
                "visuals": round(have, 2),
                "shortfall": round(need - have, 2),
            })

        for f in fixed:
            if f["duration"] <= 0:
                continue
            video.append({"key": f["key"], "ref": f["ref"], "kind": f["kind"],
                          "start": round(t, 3), "duration": round(f["duration"], 3),
                          "scene": sid, "spec": f["spec"], "audio": f["audio"]})
            t += f["duration"]

        if vo_len:
            audio.append({"key": vo_key, "scene": sid,
                          "start": round(scene_start + vo_lead, 3),
                          "duration": round(vo_len, 3), "track": 1})

        for extra_a in scene.get("audio") or []:
            key = f"audio:{extra_a['file']}"
            audio.append({"key": key, "scene": sid,
                          "start": round(scene_start + float(extra_a.get("at", 0)), 3),
                          "duration": float(durations.get(key) or 0),
                          "track": int(extra_a.get("track", 2))})

        timeline.append({"scene": sid, "start": round(scene_start, 3),
                         "end": round(t, 3), "duration": round(t - scene_start, 3),
                         "narration": round(vo_len, 2)})

    return {
        "fps": fps,
        "total": round(t, 3),
        "video": video,
        "audio": audio,
        "scenes": timeline,
        "missing": sorted(set(missing)),
        "short": short,
    }


def to_build_plan(resolved: dict, path_for) -> dict:
    """Convert a resolved timeline into the plan `premiere build` consumes.

    `path_for(entry)` maps a resolved entry to an absolute remote path.
    """
    return {
        "video": [[path_for(v), v["start"]] for v in resolved["video"]],
        "audio": [[path_for(a), a["start"], a.get("track", 1)]
                  for a in resolved["audio"]],
        "mute_sync_db": -60,
    }


def summary(resolved: dict) -> str:
    lines = [f"total {resolved['total']}s ({resolved['total'] / 60:.1f} min), "
             f"{len(resolved['video'])} visuals, {len(resolved['audio'])} audio"]
    for s in resolved["scenes"]:
        lines.append(f"  {s['start']:>7.1f} - {s['end']:>7.1f}  {s['scene']:<18} "
                     f"({s['duration']:.1f}s, narration {s['narration']:.1f}s)")
    if resolved["missing"]:
        lines.append(f"  MISSING durations: {', '.join(resolved['missing'])}")
    for sh in resolved.get("short") or []:
        lines.append(f"  SHORT {sh['scene']}: {sh['visuals']}s of visuals for "
                     f"{sh['narration']}s of narration — {sh['shortfall']}s would be BLACK")
    return "\n".join(lines)
