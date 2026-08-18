"""Check that a delivered file is actually the film the recipe describes.

Every other stage reports success from an exit code. That is not the same thing
as being correct, and the gap is not theoretical: a card patch was once wired to
a dead code path, ffmpeg ran happily for four minutes, and the delivery came
back with the right duration, the right loudness, the right byte count — and the
wrong picture. Nothing failed. It was caught by a human looking at a frame.

So this compares the delivery against its sources:

  picture   sample three frames per visual (near start, middle, near end) and
            compare them with the same moments of the source asset. Catches a
            visual that is missing, stale, out of order, or black.
  captions  the same samples, lower band only. Where a cue is on screen the
            delivery must DIFFER from the source; where none is, it must match.
            Catches a burn that silently did nothing, and captions burned at the
            wrong time.
  loudness  per scene, not integrated. An integrated figure looked fine once
            while the opening card sat at -39 dB.
  duration  against the resolved timeline.
  black     ffmpeg blackdetect, for gaps nothing else noticed.

Frames are compared as coarse greyscale thumbnails. That is deliberate:
the question is "is this the right shot", not "is this bit-identical", and
re-encoding guarantees it never will be bit-identical.
"""
from __future__ import annotations

import base64
import json
import shlex

from . import log
from . import voice as voice_mod

# One sample is a 64x24 greyscale image built from two crops stacked together:
#
#   rows  0..17   the whole frame, for "is this the right shot"
#   rows 18..23   ONLY the caption band (y 955..1065), for "is a caption here"
#
# The caption band gets its own crop because at whole-frame resolution a line of
# text occupies about one row out of eighteen and averages away to nothing --
# which is exactly how the first version of this check reported four captions
# missing that were plainly on screen.
TW = 64
FULL_ROWS = 18
CAP_Y, CAP_H_PX, CAP_ROWS = 955, 110, 6
TH = FULL_ROWS + CAP_ROWS

# How much of the frame the picture check may compare. Burned captions occupy
# roughly y 960-1080 (the last two rows), so they are excluded only when a cue
# is actually on screen. Excluding them ALWAYS was a real mistake: the logo
# strip sits at y 671-905, and a wrong card slipped straight through the
# picture check because that region had been cut out of the comparison.
PICTURE_FULL = (0, FULL_ROWS)
PICTURE_ABOVE_CAPTION = (0, FULL_ROWS - 2)
CAPTION_BAND = (FULL_ROWS, FULL_ROWS + CAP_ROWS)

# Mean absolute difference on 0-255 grey. Re-encoding alone lands in the low
# single digits; a different shot is far above.
PICTURE_MATCH = 14.0

# A caption is detected RELATIVE to the same frame's picture difference, not
# against a fixed number. How much a caption moves the average depends on the
# footage under it -- text over a smooth tan surface scored 9.8 where an
# absolute threshold of 10.0 called it missing, on a frame where the caption was
# plainly there. Comparing the two bands of one frame cancels that out: the top
# shows what re-encoding alone costs, so anything the strip adds beyond it is
# the overlay.
CAPTION_MARGIN = 4.0


def thresholds(recipe: dict | None = None) -> dict:
    """Return active thresholds from env, recipe meta, or defaults."""
    t = {"picture": PICTURE_MATCH, "caption": CAPTION_MARGIN}
    env = __import__("os").environ
    if env.get("UMC_VERIFY_PICTURE_MATCH"):
        t["picture"] = float(env["UMC_VERIFY_PICTURE_MATCH"])
    if env.get("UMC_VERIFY_CAPTION_MARGIN"):
        t["caption"] = float(env["UMC_VERIFY_CAPTION_MARGIN"])
    cfg = ((recipe or {}).get("meta") or {}).get("verify") or {}
    if cfg.get("picture_match") is not None:
        t["picture"] = float(cfg["picture_match"])
    if cfg.get("caption_margin") is not None:
        t["caption"] = float(cfg["caption_margin"])
    return t


def _q(p: str) -> str:
    return shlex.quote(p)


def _band(thumb: bytes, r0: int, r1: int) -> list:
    return list(thumb[r0 * TW:r1 * TW])


def mad(a: list, b: list) -> float:
    """Mean absolute difference; 999 when the samples are unusable."""
    if not a or not b or len(a) != len(b):
        return 999.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _sample_lines(items: list) -> str:
    """Bash to emit one base64 thumbnail per (file, time) sample."""
    out = []
    for key, path, at in items:
        vf = (f"split=2[a][b];"
              f"[a]scale={TW}:{FULL_ROWS},format=gray[t];"
              f"[b]crop=iw:{CAP_H_PX}:0:{CAP_Y},scale={TW}:{CAP_ROWS},format=gray[c];"
              f"[t][c]vstack=inputs=2")
        out.append(
            f'echo "T|{key}|$(ffmpeg -v error -ss {at:.3f} -i {_q(path)} '
            f'-frames:v 1 -filter_complex {shlex.quote(vf)} -f rawvideo - '
            f'2>/dev/null | base64 | tr -d "\\n")"')
    return "\n".join(out)


def _sample_offsets(duration: float) -> list:
    """Three sample points per visual: near start, middle, near end."""
    d = max(0.0, duration)
    return [min(0.5, d / 3.0), d / 2.0, max(d - 0.5, d * 2.0 / 3.0)]


def collect(t, delivery: str, visuals: list, scenes: list) -> dict:
    """Run every measurement in ONE remote script and parse the results."""
    samples = []
    for i, v in enumerate(visuals):
        for k, off in enumerate(_sample_offsets(v["duration"])):
            samples.append((f"d{i}_{k}", delivery, v["start"] + off))
            samples.append((f"s{i}_{k}", v["src"], off))

    lufs = "\n".join(
        f'echo "L|{s["scene"]}|$(ffmpeg -hide_banner -ss {s["start"]:.2f} '
        f'-to {min(s["end"], s["start"] + 30):.2f} -i {_q(delivery)} '
        f'-af ebur128=framelog=quiet -f null - 2>&1 '
        f'| grep -E "^[[:space:]]+I:" | tail -1 | sed "s/.*I: *//")"'
        for s in scenes)

    script = f"""
set -u
echo "DUR|$(ffprobe -v error -show_entries format=duration -of default=nk=1:nw=1 {_q(delivery)})"
{_sample_lines(samples)}
{lufs}
ffmpeg -hide_banner -i {_q(delivery)} -vf blackdetect=d=0.4:pic_th=0.98 -f null - 2>&1 \\
  | grep -o "black_start:[0-9.]* black_end:[0-9.]*" | sed "s/^/B|/" || true
"""
    r = t.run_script(script, timeout=2400)
    r.check("verify")

    thumbs, lufs_by_scene, blacks, duration = {}, {}, [], 0.0
    for line in r.stdout.splitlines():
        parts = line.strip().split("|")
        if parts[0] == "DUR" and len(parts) > 1:
            duration = float(parts[1] or 0)
        elif parts[0] == "T" and len(parts) > 2 and parts[2]:
            try:
                thumbs[parts[1]] = base64.b64decode(parts[2])
            except Exception:
                pass
        elif parts[0] == "L" and len(parts) > 2:
            lufs_by_scene[parts[1]] = parts[2].strip()
        elif parts[0] == "B":
            blacks.append(parts[1].strip())
    return {"duration": duration, "thumbs": thumbs,
            "lufs": lufs_by_scene, "black": blacks}


def _is_testimoni(v: dict, scenes: list) -> bool:
    """Testimonials have their own dialogue; caption checks are unreliable."""
    if v.get("audio") == "keep":
        return True
    scene = next((s for s in scenes if s.get("scene") == v.get("scene")), {})
    tags = scene.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    return "testimoni" in [str(t).lower() for t in tags]


def check_text(recipe: dict) -> list:
    """Warn when a narration term is not wrapped in the expected SSML tag."""
    voice_cfg = recipe.get("voice") or {}
    if not voice_cfg:
        return []
    checks = []
    terms = []
    for term in voice_cfg.get("acronyms") or []:
        terms.append((term, "say-as"))
    for term in voice_cfg.get("english_terms") or []:
        terms.append((term, "lang"))
    for term in voice_cfg.get("phoneme_terms") or []:
        terms.append((term, "phoneme"))
    for term in voice_cfg.get("arabic_terms") or []:
        terms.append((term, "phoneme"))
    for scene in recipe.get("scenes") or []:
        text = (scene.get("narration") or "").strip()
        if not text:
            continue
        ssml = voice_mod.build_ssml(text, voice_cfg)
        for term, tag in terms:
            if term.lower() not in text.lower():
                continue
            # crude: the SSML should contain the term inside the expected tag
            if f"<{tag}" not in ssml:
                checks.append(
                    f"{scene.get('id')}: `{term}` not wrapped in <{tag}>"
                )
    return checks


def check(resolved: dict, raw: dict, visuals: list, burned: bool,
          target_lufs: float = -16.0, tolerance: float = 4.0,
          recipe: dict | None = None) -> dict:
    """Turn raw measurements into pass/fail findings."""
    findings, ok = [], True
    thresh = thresholds(recipe)

    def add(name, passed, detail):
        nonlocal ok
        findings.append({"check": name, "ok": bool(passed), "detail": detail})
        if not passed:
            ok = False

    want = float(resolved.get("total") or 0)
    got = raw["duration"]
    add("duration", abs(got - want) <= 0.3,
        f"{got:.2f}s (timeline says {want:.2f}s)")

    bad_pic, missing, cap_bad = [], [], []
    scenes = resolved.get("scenes") or []
    for i, v in enumerate(visuals):
        testimoni = _is_testimoni(v, scenes)
        diffs = []
        for k in range(len(_sample_offsets(v["duration"]))):
            d, s = raw["thumbs"].get(f"d{i}_{k}"), raw["thumbs"].get(f"s{i}_{k}")
            if not d or not s or len(d) < TW * TH or len(s) < TW * TH:
                missing.append(f"{v['ref']}@{k}")
                continue
            band = PICTURE_ABOVE_CAPTION if (burned and v["captioned"]) else PICTURE_FULL
            top = mad(_band(d, *band), _band(s, *band))
            diffs.append(top)
            if top > thresh["picture"]:
                bad_pic.append(f"{v['ref']} sample{k} (diff {top:.1f})")

        if burned and not testimoni and diffs:
            # caption check uses the middle sample; the edge samples are for
            # picture robustness only
            d = raw["thumbs"].get(f"d{i}_1")
            s = raw["thumbs"].get(f"s{i}_1")
            if d and s:
                low = mad(_band(d, *CAPTION_BAND), _band(s, *CAPTION_BAND))
                delta = low - mad(_band(d, *PICTURE_ABOVE_CAPTION),
                                  _band(s, *PICTURE_ABOVE_CAPTION))
                if v["captioned"] and delta < thresh["caption"]:
                    cap_bad.append(
                        f"{v['ref']} cue due but strip unchanged (+{delta:.1f})")
                elif not v["captioned"] and delta > thresh["picture"]:
                    cap_bad.append(
                        f"{v['ref']} no cue but strip differs (+{delta:.1f})")

    add("picture matches sources", not bad_pic,
        "every visual matches its source" if not bad_pic
        else f"{len(bad_pic)} wrong: " + "; ".join(bad_pic[:5]))
    if missing:
        add("frames sampled", False, f"could not sample: {', '.join(missing[:5])}")
    if burned:
        add("captions burned in", not cap_bad,
            "captions present where cues are, absent where they are not"
            if not cap_bad else "; ".join(cap_bad[:5]))

    loud = []
    for scene, val in raw["lufs"].items():
        try:
            v = float(str(val).split()[0])
        except (ValueError, IndexError):
            continue
        if abs(v - target_lufs) > tolerance:
            loud.append(f"{scene} {v} LUFS")
    add(f"per-scene loudness within {tolerance:g} LU of {target_lufs:g}", not loud,
        "all scenes in band" if not loud else "; ".join(loud))

    add("no black gaps", not raw["black"],
        "none detected" if not raw["black"] else "; ".join(raw["black"][:5]))

    if recipe:
        text_warnings = check_text(recipe)
        if text_warnings:
            findings.append({"check": "narration terms tagged",
                             "ok": True,
                             "detail": "warnings: " + "; ".join(text_warnings[:5])})

    return {"ok": ok, "checks": findings,
            "failed": [f for f in findings if not f["ok"]]}


def report(result: dict) -> None:
    for f in result["checks"]:
        (log.ok if f["ok"] else log.err)(f"{f['check']}: {f['detail']}")
    if result["ok"]:
        log.ok("delivery matches the recipe")
    else:
        log.err(f"{len(result['failed'])} check(s) failed")
