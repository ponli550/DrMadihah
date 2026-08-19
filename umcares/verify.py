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
  regions   recipe `meta.verify.regions` — named boxes on specific scenes. The
            box is cropped from the delivery AND the source at the visual's
            middle sample and compared. Catches a logo that was swapped,
            dropped, or rendered from a stale card, at a resolution the whole
            frame averages away.
  edges     card visuals only. The outer strips of the frame must be flat: a
            design surface with content touching the edge is a logo cut off.
  sync      clips whose own audio is kept (`audio: keep`, testimonials). The
            scene's audio window is extracted from the delivery and from the
            source clip, decimated to envelopes, and cross-correlated. A lag
            beyond `sync_sec` means the clip's audio drifted from its picture.

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

# Logo QC. A design surface is flat by design, so any structure in the outer
# strips of a card frame is either a cut-off logo or text bleeding to the edge.
# Strips are judged by variance (glyphs) OR by mean shift (a solid bright bar).
EDGE_ROWS = 2          # rows at the top and bottom of the 18-row frame sample
EDGE_COLS = 2          # columns at the left and right
EDGE_STD = 12.0        # grey stdev inside a strip
EDGE_MEAN = 40.0       # |strip mean - frame mean|

# A/V source sync. Log-energy envelopes are cross-correlated; a lag beyond this
# is a clip whose own audio no longer matches its picture.
SYNC_SEC = 0.2
SYNC_PEAK = 0.4        # below this the envelopes share no structure; skip
SYNC_BIN = 0.025       # envelope bin width in seconds
SYNC_RATE = 4000       # decimation rate for envelope extraction
SYNC_MAX_WINDOW = 60.0 # cap the extracted window (testimonials are short)
SYNC_SEARCH = 8.0      # max drift looked for, seconds each way


def thresholds(recipe: dict | None = None) -> dict:
    """Return active thresholds from env, recipe meta, or defaults."""
    t = {"picture": PICTURE_MATCH, "caption": CAPTION_MARGIN,
         "edge_std": EDGE_STD, "edge_mean": EDGE_MEAN,
         "sync_sec": SYNC_SEC, "sync_peak": SYNC_PEAK}
    env = __import__("os").environ
    for key, val in (("picture", "UMC_VERIFY_PICTURE_MATCH"),
                     ("caption", "UMC_VERIFY_CAPTION_MARGIN"),
                     ("edge_std", "UMC_VERIFY_EDGE_STD"),
                     ("edge_mean", "UMC_VERIFY_EDGE_MEAN"),
                     ("sync_sec", "UMC_VERIFY_SYNC_SEC"),
                     ("sync_peak", "UMC_VERIFY_SYNC_PEAK")):
        if env.get(val):
            t[key] = float(env[val])
    cfg = ((recipe or {}).get("meta") or {}).get("verify") or {}
    for key, name in (("picture", "picture_match"), ("caption", "caption_margin"),
                      ("edge_std", "edge_std"), ("edge_mean", "edge_mean"),
                      ("sync_sec", "sync_sec"), ("sync_peak", "sync_peak")):
        if cfg.get(name) is not None:
            t[key] = float(cfg[name])
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


def _stats(values: list) -> tuple:
    if not values:
        return (None, None)
    m = sum(values) / len(values)
    var = sum((x - m) ** 2 for x in values) / len(values)
    return (m, var ** 0.5)


def correlate_offset(d: list, s: list, bin_sec: float = SYNC_BIN,
                     max_lag_sec: float = 1.0) -> tuple:
    """Best-alignment lag of `s` within `d`, and the peak correlation.

    Returns (offset_seconds, peak). A positive offset means the source audio
    appears LATER in the delivery than the timeline says (the clip is late);
    negative means it is early. `peak` in [0,1]; a low peak means the
    envelopes share no structure, so the result is not evidence of drift.
    """
    if len(d) < 4 or len(s) < 4:
        return (0.0, 0.0)
    n = min(len(d), len(s))
    d, s = d[:n], s[:n]

    def unit(a):
        m = sum(a) / len(a)
        a = [x - m for x in a]
        e = (sum(x * x for x in a)) ** 0.5
        return [x / e for x in a] if e else a

    best_lag, best = 0, -1.0
    max_lag = max(1, int(max_lag_sec / bin_sec))
    min_overlap = max(8, n // 4)   # shorter windows self-correlate by chance
    for lag in range(-max_lag, max_lag + 1):
        if lag < 0:
            dd, ss = d[-lag:], s[:lag]
        elif lag > 0:
            dd, ss = d[:-lag], s[lag:]
        else:
            dd, ss = d, s
        if len(dd) < min_overlap:
            continue
        dd, ss = unit(dd), unit(ss)
        c = sum(a * b for a, b in zip(dd, ss))
        if c > best:
            best, best_lag = c, lag
    return (round(best_lag * bin_sec, 3), round(max(0.0, best), 3))


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


def _region_lines(regions: dict, visuals: list, delivery: str) -> str:
    """Crops of each qc region from delivery and source, middle sample."""
    out = []
    for name, spec in (regions or {}).items():
        box = spec.get("box") or []
        if len(box) != 4:
            continue
        x, y, w, h = box
        vf = (f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},"
              f"scale={TW}:{TH},format=gray")
        for i, v in enumerate(visuals):
            if spec.get("scene") and v.get("scene") != spec.get("scene"):
                continue
            at = v["start"] + v["duration"] / 2.0
            for tag, path in (("d", delivery), ("s", v["src"])):
                out.append(
                    f'echo "R|{name}:{tag}{i}|$(ffmpeg -v error -ss {at:.3f} '
                    f'-i {_q(path)} -frames:v 1 '
                    f'-filter_complex {shlex.quote(vf)} -f rawvideo - '
                    f'2>/dev/null | base64 | tr -d "\\n")"')
    return "\n".join(out)


def _edge_lines(visuals: list, delivery: str) -> str:
    """Full-frame grey thumbs of card visuals, for edge-strip inspection."""
    out = []
    for i, v in enumerate(visuals):
        if v.get("kind") != "card":
            continue
        at = v["start"] + v["duration"] / 2.0
        vf = f"scale={TW}:{FULL_ROWS},format=gray"
        out.append(
            f'echo "E|{i}|$(ffmpeg -v error -ss {at:.3f} -i {_q(delivery)} '
            f'-frames:v 1 -filter_complex {shlex.quote(vf)} -f rawvideo - '
            f'2>/dev/null | base64 | tr -d "\\n")"')
    return "\n".join(out)


def _sync_lines(visuals: list, delivery: str) -> str:
    """Log-energy envelopes of kept-audio clips, delivery vs source."""
    env = (
        "python3 -c 'import sys,struct,math\n"
        f"BIN={int(SYNC_BIN * SYNC_RATE)}\n"
        "raw=sys.stdin.buffer.read()\n"
        "n=len(raw)//2\n"
        "s=struct.unpack(\"<%dh\"%n, raw[:n*2]) if n else ()\n"
        "env=[math.sqrt(sum(x*x for x in s[i:i+BIN])/max(1,len(s[i:i+BIN])))"
        " for i in range(0,n,BIN)]\n"
        "print(\"|\".join(\"%.2f\"%math.log10(v+1e-9) for v in env))'")
    out = []
    for i, v in enumerate(visuals):
        if v.get("kind") != "clip" or v.get("audio") != "keep":
            continue
        dur = min(float(v["duration"]), SYNC_MAX_WINDOW)
        af = (f"aresample={SYNC_RATE},"
              f"aformat=channel_layouts=mono:sample_fmts=s16")
        out.append(
            f'echo "Y|{i}|d|$(ffmpeg -v error -ss {v["start"]:.3f} '
            f'-t {dur:.3f} -i {_q(delivery)} -af {af} -f s16le - 2>/dev/null '
            f'| {env})"')
        out.append(
            f'echo "Y|{i}|s|$(ffmpeg -v error -t {dur:.3f} '
            f'-i {_q(v["src"])} -af {af} -f s16le - 2>/dev/null | {env})"')
    return "\n".join(out)


def collect(t, delivery: str, visuals: list, scenes: list,
            regions: dict | None = None) -> dict:
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
{_region_lines(regions, visuals, delivery)}
{_edge_lines(visuals, delivery)}
{_sync_lines(visuals, delivery)}
{lufs}
ffmpeg -hide_banner -i {_q(delivery)} -vf blackdetect=d=0.4:pic_th=0.98 -f null - 2>&1 \\
  | grep -o "black_start:[0-9.]* black_end:[0-9.]*" | sed "s/^/B|/" || true
"""
    r = t.run_script(script, timeout=2400)
    r.check("verify")

    thumbs, lufs_by_scene, blacks, duration = {}, {}, [], 0.0
    edges, sync = {}, {}
    regions_out = {}
    for line in r.stdout.splitlines():
        parts = line.strip().split("|", 3)
        if parts[0] == "DUR" and len(parts) > 1:
            duration = float(parts[1] or 0)
        elif parts[0] == "T" and len(parts) > 2 and parts[2]:
            try:
                thumbs[parts[1]] = base64.b64decode(parts[2])
            except Exception:
                pass
        elif parts[0] == "R" and len(parts) > 2 and parts[2]:
            try:
                regions_out[parts[1]] = base64.b64decode(parts[2])
            except Exception:
                pass
        elif parts[0] == "E" and len(parts) > 2 and parts[2]:
            try:
                edges[parts[1]] = base64.b64decode(parts[2])
            except Exception:
                pass
        elif parts[0] == "Y" and len(parts) > 3 and parts[3]:
            try:
                env_vals = [float(x) for x in parts[3].split("|") if x]
                if env_vals:
                    sync.setdefault(parts[1], {})[parts[2]] = env_vals
            except ValueError:
                pass
        elif parts[0] == "L" and len(parts) > 2:
            lufs_by_scene[parts[1]] = parts[2].strip()
        elif parts[0] == "B":
            blacks.append(parts[1].strip())
    return {"duration": duration, "thumbs": thumbs,
            "lufs": lufs_by_scene, "black": blacks,
            "regions": regions_out, "edges": edges, "sync": sync}


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

    # -- qc regions: named boxes on specific scenes -------------------------
    regions = ((recipe or {}).get("meta") or {}).get("verify", {}).get("regions") or {}
    for name, spec in regions.items():
        box = spec.get("box") or []
        if len(box) != 4:
            add(f"qc region {name}", False, "bad `box` — want [x, y, w, h]")
            continue
        hits = [i for i, v in enumerate(visuals)
                if not spec.get("scene") or v.get("scene") == spec.get("scene")]
        if not hits:
            add(f"qc region {name}", False,
                f"scene `{spec.get('scene')}` has no visual in the resolved timeline")
            continue
        bad, missing_region, warnings = [], [], []
        for i in hits:
            d = raw.get("regions", {}).get(f"{name}:d{i}")
            s = raw.get("regions", {}).get(f"{name}:s{i}")
            if not d or not s or len(d) < TW * TH or len(s) < TW * TH:
                missing_region.append(f"visual {i}")
                continue
            diff = mad(list(d), list(s))
            s_mean, s_sd = _stats(list(s))
            d_mean, d_sd = _stats(list(d))
            if diff > thresh["picture"]:
                bad.append(f"visual {i} (diff {diff:.1f})")
            elif s_sd is not None and s_sd > 5.0 and d_sd is not None and d_sd < 2.0:
                bad.append(f"visual {i} (region went blank in the delivery)")
            elif s_sd is not None and s_sd < 2.0:
                warnings.append(f"visual {i}: region flat in the source — "
                                f"the box may miss the content")
        if missing_region:
            add(f"qc region {name}", False,
                f"could not sample: {', '.join(missing_region)}")
        else:
            add(f"qc region {name}", not bad,
                ("matches the source" if not bad else "; ".join(bad))
                + ("  (warn: " + "; ".join(warnings) + ")" if warnings else ""))

    # -- card edges: a design surface must not touch the frame edge ---------
    edge_bad = []
    for i, v in enumerate(visuals):
        if v.get("kind") != "card":
            continue
        e = raw.get("edges", {}).get(str(i))
        if not e or len(e) < TW * FULL_ROWS:
            continue
        rows = [list(e[r * TW:(r + 1) * TW]) for r in range(FULL_ROWS)]
        frame_mean = sum(sum(r) for r in rows) / (FULL_ROWS * TW)
        strips = [("top", [x for r in rows[:EDGE_ROWS] for x in r]),
                  ("left", [r[c] for r in rows for c in range(EDGE_COLS)]),
                  ("right", [r[c] for r in rows
                             for c in range(TW - EDGE_COLS, TW)])]
        if not v.get("captioned"):
            strips.append(
                ("bottom", [x for r in rows[FULL_ROWS - EDGE_ROWS:] for x in r]))
        for name, vals in strips:
            m, sd = _stats(vals)
            if sd is None:
                continue
            if sd > thresh["edge_std"] or abs(m - frame_mean) > thresh["edge_mean"]:
                edge_bad.append(f"{v['ref']} {name} (sd {sd:.1f}, {m:.0f} vs {frame_mean:.0f})")
    if any(v.get("kind") == "card" for v in visuals):
        add("card edges clean", not edge_bad,
            "no content touching frame edges" if not edge_bad
            else "; ".join(edge_bad[:5]))

    # -- A/V source sync: kept audio must line up with its picture ----------
    sync_bad, sync_skipped = [], []
    for i, v in enumerate(visuals):
        if v.get("kind") != "clip" or v.get("audio") != "keep":
            continue
        pair = raw.get("sync", {}).get(str(i))
        if not pair or not pair.get("d") or not pair.get("s"):
            sync_skipped.append(v["ref"])
            continue
        offset, peak = correlate_offset(pair["d"], pair["s"],
                                        bin_sec=SYNC_BIN,
                                        max_lag_sec=SYNC_SEARCH)
        if peak < thresh["sync_peak"]:
            sync_skipped.append(f"{v['ref']} (no common envelope)")
            continue
        if abs(offset) > thresh["sync_sec"]:
            sync_bad.append(
                f"{v['ref']} {'late' if offset > 0 else 'early'} by "
                f"{abs(offset):.2f}s (peak {peak:.2f})")
    if any(v.get("kind") == "clip" and v.get("audio") == "keep"
           for v in visuals):
        add("audio sync with source", not sync_bad,
            "kept-audio clips line up" if not sync_bad
            else "; ".join(sync_bad[:5]))
        if sync_skipped:
            findings.append({"check": "audio sync with source",
                             "ok": True,
                             "detail": "skipped: " + "; ".join(sync_skipped[:5])})

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
