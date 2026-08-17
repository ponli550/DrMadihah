"""Render a recipe into a video.

Stages, in order. Each is idempotent — anything already present is reused, so
re-running after an edit only redoes what actually changed:

    voice    narration -> per-scene WAVs        (json2video -> Azure)
    cards    card specs -> per-card clips       (json2video text + ffmpeg logos)
    motion   kenburns specs -> clips            (ffmpeg, on the remote)
    resolve  measure everything, place it
    import   put the assets into the project
    build    lay the timeline, mute sync audio
    subs     write the SRT from the delivered audio
    export   master out of Premiere
    deliver  music bed + subtitles -> H.264

`--from` and `--to` run a slice. `--force` regenerates even if outputs exist.

The design rule throughout: **measure, never assume.** Durations come from the
rendered files, and `resolve` is re-run after generation so the timeline is
built from what actually exists rather than what was hoped for.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import cards as cards_mod
from . import log, media, post, recipe as recipe_mod, spinner, srt as srt_mod
from . import voice as voice_mod
from .premiere import Premiere

STAGES = ["voice", "cards", "motion", "resolve", "import", "build",
          "subs", "export", "deliver"]


class Renderer:
    def __init__(self, t, cfg, rec: dict, work: Path, force: bool = False):
        self.t = t
        self.cfg = cfg
        self.rec = rec
        self.work = work
        self.force = force
        self.remote = cfg.remote
        self.durations: dict = {}
        self._resolved: dict | None = None
        self.report: dict = {"stages": {}}

        self.vo_local = work / "vo"
        self.cards_local = work / "cards"
        self.logo_local = cfg.root / "assets" / "logos"

        # Load measurements ALWAYS, even under --force. Force means "regenerate
        # the artifacts", not "forget how long everything else is": each stage
        # overwrites its own keys after it re-measures, so keeping the rest is
        # what makes `--only cards --force` safe to run on a finished cut.
        dpath = work / "durations.json"
        if dpath.exists():
            self.durations = json.loads(dpath.read_text(encoding="utf-8"))

    # -- helpers ------------------------------------------------------------
    @property
    def resolved(self) -> dict:
        """The resolved timeline, loaded from disk if `resolve` ran earlier.

        Without this, every stage from `import` onwards only works when
        `resolve` runs in the SAME invocation — which defeats `--only` and
        `--from`, the whole point of having stages.
        """
        if self._resolved is None:
            path = self.work / "resolved.json"
            if not path.exists():
                raise SystemExit(
                    "no resolved timeline yet — run `umcares render --only resolve` "
                    "first (or include `resolve` in this run)")
            self._resolved = json.loads(path.read_text(encoding="utf-8"))
        return self._resolved

    @resolved.setter
    def resolved(self, value: dict) -> None:
        self._resolved = value

    def _save_durations(self):
        (self.work / "durations.json").write_text(
            json.dumps(self.durations, indent=2), encoding="utf-8")

    def _remote(self, *parts) -> str:
        return "/".join([self.remote.root.rstrip("/"), *parts])

    def _have_remote(self, path: str) -> bool:
        return (not self.force) and self.t.exists(path)

    # -- stages -------------------------------------------------------------
    def stage_voice(self):
        scenes = voice_mod.from_recipe(self.rec)
        if not scenes:
            log.step("no narration in this recipe")
            return {"rendered": 0}

        missing = [s for s in scenes
                   if self.force or not (self.vo_local / f"{s[0]}.wav").exists()]
        if not missing:
            log.ok(f"voice: {len(scenes)} file(s) already rendered")
        else:
            env = self.cfg.env
            if not env.get("JSON2VIDEO_API_KEY"):
                raise SystemExit("JSON2VIDEO_API_KEY is required to render narration")
            # Render only the scenes that are actually missing, in ONE call: the
            # silence between them is what the splitter keys on, and a subset
            # splits exactly like a full set.
            #
            # Re-rendering everything when one line changed would be wrong twice
            # over: it burns quota on narration nobody edited, and a neural voice
            # is not bit-identical between runs, so every untouched scene would
            # come back a few frames longer or shorter and shift the whole cut.
            log.step(f"{len(scenes) - len(missing)} narration file(s) reused, "
                     f"{len(missing)} to render: "
                     + ", ".join(s[0] for s in missing))
            with spinner.spin(f"rendering narration ({len(missing)} scenes)",
                              20 + 6 * len(missing)):
                out = voice_mod.render(env, missing, self.rec.get("voice") or {})
            with spinner.spin("splitting narration per scene", 20):
                files = voice_mod.download_and_split(
                    out["url"], [s[0] for s in missing], self.vo_local,
                    target_lufs=float((self.rec.get("output") or {}).get(
                        "target_lufs", -16)))
            for f in files:
                log.debug(f"  {f['scene']}: {f['duration']}s")

        # measure and upload
        for sid, _, _ in scenes:
            wav = self.vo_local / f"{sid}.wav"
            if not wav.exists():
                continue
            self.durations[f"vo:{sid}"] = srt_mod.duration(wav)
            dest = self._remote("assets", "vo", f"{sid}.wav")
            if not self._have_remote(dest):
                self.t.push(wav, dest)
        self._save_durations()
        return {"rendered": len(scenes)}

    def stage_cards(self):
        cards = self.rec.get("cards") or {}
        if not cards:
            return {"rendered": 0}

        need = {cid: c for cid, c in cards.items()
                if self.force or not (self.cards_local / f"card_{cid}.mp4").exists()}
        if need:
            env = self.cfg.env
            if not env.get("JSON2VIDEO_API_KEY"):
                raise SystemExit("JSON2VIDEO_API_KEY is required to render cards")
            with spinner.spin(f"rendering {len(need)} card(s)", 30 + 8 * len(need)):
                out = cards_mod.render(env, need, self.durations,
                                       self.rec.get("style"))
            with spinner.spin("cutting cards and adding logos", 30):
                cards_mod.split_and_composite(out["url"], out["order"], need,
                                              self.cards_local, self.logo_local)
        else:
            log.ok(f"cards: {len(cards)} already rendered")

        for cid in cards:
            f = self.cards_local / f"card_{cid}.mp4"
            if not f.exists():
                continue
            self.durations[f"card:{cid}"] = srt_mod.duration(f)
            dest = self._remote("assets", "cards", f"card_{cid}.mp4")
            if not self._have_remote(dest):
                self.t.push(f, dest)
        self._save_durations()
        return {"rendered": len(need)}

    def stage_motion(self):
        """Ken Burns sequences, built on the remote where the photos live."""
        jobs = []
        for scene in self.rec.get("scenes") or []:
            for v in scene.get("visuals") or []:
                if "kenburns" not in v:
                    continue
                spec = v["kenburns"] or {}
                kid = spec.get("id") or f"{scene['id']}_kb{len(jobs)}"
                jobs.append((kid, spec.get("photos") or [],
                             float(v.get("duration") or spec.get("duration") or 8.0)))
        if not jobs:
            return {"built": 0}

        photo_dir = (self.rec.get("paths") or {}).get(
            "photos") or self._remote("assets", "photos")
        built = 0
        for kid, photos, dur in jobs:
            dest = self._remote("assets", "edit_ready", f"{kid}.mp4")
            if self._have_remote(dest):
                self.durations[f"kenburns:{kid}"] = dur
                continue
            paths = [f"{photo_dir}/{p}" for p in photos]
            with spinner.spin(f"ken burns {kid} ({len(photos)} stills)",
                              20 + 25 * len(photos)):
                res = media.kenburns(self.t, dest, paths, dur)
            self.durations[f"kenburns:{kid}"] = res["duration"]
            built += 1
        self._save_durations()
        return {"built": built, "total": len(jobs)}

    def stage_resolve(self, manifest: dict | None):
        if manifest:
            for item in manifest.get("items", []):
                if not item.get("duration"):
                    continue
                kind = "clip" if item.get("kind") == "video" else "audio"
                # setdefault: a measured duration from a rendered file always
                # beats one probed from the source manifest
                self.durations.setdefault(f"{kind}:{item['file']}", item["duration"])
        self._save_durations()
        resolved = recipe_mod.resolve(self.rec, self.durations)
        (self.work / "resolved.json").write_text(
            json.dumps(resolved, indent=2, ensure_ascii=False), encoding="utf-8")
        print(recipe_mod.summary(resolved), file=__import__("sys").stderr)
        if resolved.get("short"):
            raise SystemExit(
                "cannot build: these scenes have less footage than narration, and "
                "the shortfall would render as BLACK —\n  "
                + "\n  ".join(
                    f"{sh['scene']}: {sh['shortfall']}s short "
                    f"({sh['visuals']}s of visuals, {sh['narration']}s of narration)"
                    for sh in resolved["short"])
                + "\n  add another visual to the scene, or shorten the narration.")
        if resolved["missing"]:
            raise SystemExit(
                "cannot build: no measured duration for "
                + ", ".join(resolved["missing"])
                + "\n  run the earlier stages first, or add an explicit `duration`")
        self.resolved = resolved
        return {"total": resolved["total"], "visuals": len(resolved["video"])}

    def _path_for(self, entry) -> str:
        kind, ref = entry["kind"], entry["ref"]
        if kind == "clip":
            return self._remote("assets", "edit_ready", ref)
        if kind == "card":
            return self._remote("assets", "cards", f"card_{ref}.mp4")
        if kind == "kenburns":
            return self._remote("assets", "edit_ready", f"{ref}.mp4")
        return ref

    def _audio_path_for(self, entry) -> str:
        key = entry["key"]
        if key.startswith("vo:"):
            return self._remote("assets", "vo", f"{key[3:]}.wav")
        if key.startswith("audio:"):
            return self._remote("assets", key[6:])
        return key

    def stage_import(self):
        paths = sorted({self._path_for(v) for v in self.resolved["video"]} |
                       {self._audio_path_for(a) for a in self.resolved["audio"]})
        p = Premiere(self.t, self.remote)
        with spinner.spin("healing Premiere + importing assets", 30 + 2 * len(paths)):
            p.heal()
            n = p.import_files(paths)
        return {"imported": n, "requested": len(paths)}

    def stage_build(self):
        audio_cfg = self.rec.get("audio") or {}
        db = {"keep": float(audio_cfg.get("keep_db", 0)),
              "duck": float(audio_cfg.get("duck_db", -18)),
              "mute": float(audio_cfg.get("mute_db", -60))}

        # a clip's own audio is matched by filename on the sync track
        rules = []
        for v in self.resolved["video"]:
            mode = v.get("audio", "mute")
            if mode == "mute":
                continue
            stem = str(v["ref"]).rsplit("/", 1)[-1]
            stem = stem.rsplit(".", 1)[0]
            rules.append([stem, db[mode]])

        plan = {
            "video": [[self._path_for(v), v["start"]] for v in self.resolved["video"]],
            "audio": [[self._audio_path_for(a), a["start"], a.get("track", 1)]
                      for a in self.resolved["audio"]],
            "mute_sync_db": db["mute"],
            "audio_levels": rules,
        }
        if rules:
            log.step(f"{len(rules)} clip(s) keep their own audio: "
                     + ", ".join(f"{r[0]}@{r[1]}dB" for r in rules[:4]))
        (self.work / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
        p = Premiere(self.t, self.remote)
        with spinner.spin(f"building timeline ({len(plan['video'])} visuals)", 120):
            res = p.build(plan)
        if res.get("gaps"):
            log.warn(f"gaps: {', '.join(res['gaps'])}")
        else:
            log.ok("no gaps")
        for f in res.get("failures", []):
            log.err(f)
        return res

    def stage_subs(self):
        if not (self.rec.get("subtitles") or {}).get("generate", True):
            return {"skipped": True}
        out = self.work / "subtitles.srt"
        res = srt_mod.build(self.rec, self.resolved, self.vo_local, out)
        dest = self._remote("subtitles", "subtitles.srt")
        self.t.push(out, dest)
        self.srt_remote = dest
        log.ok(f"{res['cues']} cues, last ends {res['last_end']}s")
        return res

    def stage_export(self):
        outcfg = self.rec.get("output") or {}
        master = self._remote(outcfg.get("master", "exports/master.mxf"))
        # recipe wins, then config, then the built-in default
        preset = outcfg.get("preset_path") or self.remote.preset_path
        self.t.run(f"mkdir -p {self._remote('exports')}", timeout=60)
        p = Premiere(self.t, self.remote)
        with spinner.spin("exporting master from Premiere", 480):
            res = p.export(master, preset, timeout=2400)
        self.master = master
        return res

    def _burn_pngs(self, subs: dict) -> list:
        """Render caption images locally, push them, return remote paths.

        Rendering happens on THIS machine because the remote has no imaging
        library, and the remote only ever sees finished PNGs — so burning needs
        nothing installed over there.
        """
        from . import burn as burn_mod

        local_srt = self.work / "subtitles.srt"
        if not local_srt.exists():
            raise SystemExit("no subtitles.srt yet — run the `subs` stage first")
        out_dir = self.work / "captions"
        entries = burn_mod.render_cue_pngs(
            local_srt, out_dir,
            width=int((self.rec.get("meta") or {}).get("width") or 1920),
            size=int(subs.get("font_size") or 46),
            ink=(self.rec.get("style") or {}).get("ink", "#ffffff"))

        dest_dir = self._remote("subtitles", "captions")
        with spinner.spin(f"uploading {len(entries)} caption images", 60):
            self.t.push_many([e["png"] for e in entries], dest_dir)
        return [{**e, "png": f"{dest_dir}/{Path(e['png']).name}"} for e in entries]

    def stage_deliver(self):
        outcfg = self.rec.get("output") or {}
        music = self.rec.get("music") or {}
        delivery = self._remote(outcfg.get("delivery", "exports/delivery.mp4"))
        master = getattr(self, "master", self._remote(
            outcfg.get("master", "exports/master.mxf")))
        srt_remote = getattr(self, "srt_remote", self._remote(
            "subtitles", "subtitles.srt"))

        subs = self.rec.get("subtitles") or {}
        burn = str(subs.get("mode", "soft")).lower() == "burn"
        lang = subs.get("language", "msa")
        pngs = self._burn_pngs(subs) if burn else None

        # `patch_visuals` re-composites named visuals into the delivery without
        # re-exporting the master. For a static card that changed after export
        # this turns a 12-minute round trip into a 4-minute one — at the cost of
        # the master no longer matching the delivery, which is why it is opt-in
        # and named in the recipe rather than inferred.
        patches = []
        for ref in (outcfg.get("patch_visuals") or []):
            hits = [v for v in self.resolved["video"] if v["ref"] == ref]
            if not hits:
                raise SystemExit(f"patch_visuals: no visual named `{ref}` in the cut")
            for v in hits:
                patches.append({"file": self._path_for(v), "at": v["start"],
                                "duration": v["duration"]})
        if patches:
            log.warn(f"patching {len(patches)} visual(s) into the delivery "
                     f"({', '.join(outcfg['patch_visuals'])}) — the master still "
                     f"holds the previous picture")

        if not music.get("file"):
            label = "burning subtitles in" if burn else "muxing subtitles"
            with spinner.spin(f"{label} (no music in recipe)",
                              600 if burn else 90):
                return post.mux_subtitles_only(self.t, master, srt_remote, delivery,
                                               lang=lang, burn_pngs=pngs,
                                               patches=patches or None)

        music_path = self._remote("assets", "music", music["file"])
        with spinner.spin("mixing music + subtitles", 480 if burn else 360):
            return post.mix(
                self.t, master, music_path, srt_remote, delivery,
                sections=music.get("ducking"),
                music_start=float(music.get("start", 25)),
                lang=lang, burn_pngs=pngs, patches=patches or None)


def run(t, cfg, rec: dict, work: Path, manifest: dict | None = None,
        stages: list | None = None, force: bool = False) -> dict:
    r = Renderer(t, cfg, rec, work, force=force)
    todo = stages or STAGES
    out = {}
    for name in STAGES:
        if name not in todo:
            continue
        log.info(f"stage: {name}")
        if name == "resolve":
            out[name] = r.stage_resolve(manifest)
        else:
            out[name] = getattr(r, f"stage_{name}")()
    out["work"] = str(work)
    return out
