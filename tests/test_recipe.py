"""Recipe validation and timeline resolution.

Every test here is a regression test for a bug that actually shipped. The
resolver is the one component that can silently ruin a cut -- a wrong duration
becomes black frames nobody notices until a client does -- so it gets the
harshest treatment.
"""
import unittest

from umcares import recipe


def rec(scenes, **extra):
    r = {"meta": {"fps": 50, "scene_pad": 0.5, "narration_lead": 0.5},
         "scenes": scenes}
    r.update(extra)
    return r


class Validate(unittest.TestCase):
    def test_accepts_a_minimal_recipe(self):
        r = rec([{"id": "s1", "visuals": [{"clip": "a.mp4"}]}])
        self.assertEqual(recipe.validate(r), [])

    def test_rejects_unknown_audio_mode(self):
        r = rec([{"id": "s1", "visuals": [{"clip": "a.mp4", "audio": "loud"}]}])
        self.assertTrue(any("audio `loud`" in p for p in recipe.validate(r)))

    def test_accepts_the_three_audio_modes(self):
        for mode in ("keep", "duck", "mute"):
            r = rec([{"id": "s1", "visuals": [{"clip": "a.mp4", "audio": mode}]}])
            self.assertEqual(recipe.validate(r), [], mode)

    def test_rejects_scene_with_no_visuals(self):
        self.assertTrue(any("no `visuals`" in p
                            for p in recipe.validate(rec([{"id": "s1"}]))))

    def test_rejects_duplicate_scene_ids(self):
        r = rec([{"id": "s1", "visuals": [{"clip": "a.mp4"}]},
                 {"id": "s1", "visuals": [{"clip": "b.mp4"}]}])
        self.assertTrue(any("duplicate id" in p for p in recipe.validate(r)))

    def test_rejects_undefined_card(self):
        r = rec([{"id": "s1", "visuals": [{"card": "nope"}]}], cards={"open": {}})
        self.assertTrue(any("card `nope`" in p for p in recipe.validate(r)))

    def test_rejects_visual_with_two_kinds(self):
        r = rec([{"id": "s1", "visuals": [{"clip": "a.mp4", "card": "x"}]}])
        self.assertTrue(any("exactly one of" in p for p in recipe.validate(r)))

    def test_flags_vp9_clip_premiere_cannot_decode(self):
        manifest = {"items": [{"file": "a.mp4", "kind": "video",
                               "premiere_usable": False}]}
        r = rec([{"id": "s1", "visuals": [{"clip": "a.mp4"}]}])
        self.assertTrue(any("not H.264" in p for p in recipe.validate(r, manifest)))

    def test_flags_clip_missing_from_media_dir(self):
        manifest = {"items": [{"file": "b.mp4", "kind": "video"}]}
        r = rec([{"id": "s1", "visuals": [{"clip": "a.mp4"}]}])
        self.assertTrue(any("not in the media dir" in p
                            for p in recipe.validate(r, manifest)))

    def test_rejects_non_increasing_ducking(self):
        r = rec([{"id": "s1", "visuals": [{"clip": "a.mp4"}]}],
                music={"file": "m.mp3", "ducking": [[10, -3], [5, -20]]})
        self.assertTrue(any("must increase" in p for p in recipe.validate(r)))

    def test_kenburns_needs_two_photos(self):
        r = rec([{"id": "s1", "visuals": [{"kenburns": {"photos": ["a.jpg"]}}]}])
        self.assertTrue(any(">= 2 photos" in p for p in recipe.validate(r)))

    def test_missing_durations_are_ignored_when_no_durations_dict_given(self):
        """First-run structural validation must not require rendered files."""
        r = rec([{"id": "s1", "narration": "hi",
                  "visuals": [{"clip": "a.mp4"}]}])
        self.assertEqual(recipe.validate(r, durations={}), [])

    def test_flags_missing_visual_duration_when_durations_exist(self):
        r = rec([{"id": "s1", "visuals": [{"clip": "a.mp4"}]}])
        problems = recipe.validate(r, durations={"clip:other.mp4": 5.0})
        self.assertTrue(any("no measured duration" in p for p in problems))

    def test_flags_zero_visual_duration(self):
        r = rec([{"id": "s1", "visuals": [{"clip": "a.mp4"}]}])
        problems = recipe.validate(r, durations={"clip:a.mp4": 0})
        self.assertTrue(any("duration for `clip:a.mp4` is zero" in p for p in problems))

    def test_flags_missing_narration_duration(self):
        r = rec([{"id": "s1", "narration": "hi",
                  "visuals": [{"clip": "a.mp4", "duration": 10}]}])
        problems = recipe.validate(r, durations={"clip:a.mp4": 10.0})
        self.assertTrue(any("narration has no measured duration" in p for p in problems))

    def test_flags_missing_scene_audio_duration(self):
        r = rec([{"id": "s1", "visuals": [{"clip": "a.mp4", "duration": 10}],
                  "audio": [{"file": "x.wav"}]}])
        problems = recipe.validate(r, durations={"clip:a.mp4": 10.0})
        self.assertTrue(any("audio[0]: no measured duration" in p for p in problems))


class Resolve(unittest.TestCase):
    def test_places_visuals_back_to_back(self):
        r = rec([{"id": "s1", "visuals": [{"clip": "a.mp4", "duration": 3},
                                          {"clip": "b.mp4", "duration": 4}]}])
        out = recipe.resolve(r, {})
        self.assertEqual([v["start"] for v in out["video"]], [0.0, 3.0])
        self.assertEqual(out["total"], 7.0)

    def test_reports_missing_duration_instead_of_guessing(self):
        r = rec([{"id": "s1", "visuals": [{"clip": "a.mp4"}]}])
        self.assertIn("clip:a.mp4", recipe.resolve(r, {})["missing"])

    def test_narration_duration_comes_from_measurement(self):
        r = rec([{"id": "s1", "narration": "hi",
                  "visuals": [{"clip": "a.mp4", "duration": 10}]}])
        out = recipe.resolve(r, {"vo:s1": 4.0})
        self.assertEqual(out["audio"][0]["start"], 0.5)     # narration_lead
        self.assertEqual(out["audio"][0]["duration"], 4.0)

    def test_missing_narration_is_reported(self):
        r = rec([{"id": "s1", "narration": "hi",
                  "visuals": [{"clip": "a.mp4", "duration": 10}]}])
        self.assertIn("vo:s1", recipe.resolve(r, {})["missing"])

    # -- the black-frame bug ------------------------------------------------
    def test_will_not_stretch_a_clip_past_its_own_length(self):
        """A 5s clip asked to cover 20s of narration does not stretch, it ENDS.

        The resolver used to extend the last visual to whatever the narration
        needed, which put the difference on screen as black. It must report the
        shortfall instead.
        """
        r = rec([{"id": "s1", "narration": "long",
                  "visuals": [{"clip": "short.mp4"}]}])
        out = recipe.resolve(r, {"vo:s1": 20.0, "clip:short.mp4": 5.0})
        self.assertTrue(out["short"], "shortfall must be reported")
        self.assertEqual(out["short"][0]["scene"], "s1")
        self.assertAlmostEqual(out["short"][0]["visuals"], 5.0, places=2)
        self.assertGreater(out["short"][0]["shortfall"], 15.0)
        self.assertAlmostEqual(out["video"][0]["duration"], 5.0, places=2)

    def test_measured_clips_play_full_length_and_are_not_squeezed(self):
        """More footage than narration is fine and must not be trimmed.

        A clip whose length is known plays in full unless the recipe gives it an
        explicit shorter duration. The resolver only ever grows a scene to cover
        its narration; it never shrinks one to match.
        """
        r = rec([{"id": "s1", "narration": "x",
                  "visuals": [{"clip": "a.mp4"}, {"clip": "b.mp4"}]}])
        out = recipe.resolve(r, {"vo:s1": 9.0, "clip:a.mp4": 4.0, "clip:b.mp4": 20.0})
        self.assertFalse(out["short"])
        self.assertEqual(out["total"], 24.0)

    def test_last_visual_may_grow_past_its_recipe_duration_up_to_the_file(self):
        """A trimmed clip can give its trim back to cover narration.

        The recipe asks for 4s of a 20s file. When the narration needs 10s the
        last visual is allowed to grow into material that genuinely exists --
        but no further, which is what the cap enforces.
        """
        r = rec([{"id": "s1", "narration": "x",
                  "visuals": [{"clip": "a.mp4", "duration": 4.0}]}])
        out = recipe.resolve(r, {"vo:s1": 9.0, "clip:a.mp4": 20.0})
        self.assertFalse(out["short"])
        self.assertAlmostEqual(out["total"], 10.0, places=1)

    def test_no_shortfall_when_footage_is_ample(self):
        r = rec([{"id": "s1", "narration": "x",
                  "visuals": [{"clip": "a.mp4", "duration": 30}]}])
        self.assertFalse(recipe.resolve(r, {"vo:s1": 5.0})["short"])

    def test_explicit_duration_overrides_measurement(self):
        r = rec([{"id": "s1", "visuals": [{"clip": "a.mp4", "duration": 4.0}]}])
        out = recipe.resolve(r, {"clip:a.mp4": 13.0})
        self.assertEqual(out["video"][0]["duration"], 4.0)

    def test_audio_mode_is_carried_through(self):
        r = rec([{"id": "s1", "visuals": [
            {"clip": "a.mp4", "duration": 2, "audio": "keep"},
            {"clip": "b.mp4", "duration": 2}]}])
        modes = [v["audio"] for v in recipe.resolve(r, {})["video"]]
        self.assertEqual(modes, ["keep", "mute"])       # mute is the default

    def test_scene_audio_entries_land_on_their_track(self):
        r = rec([{"id": "s1", "visuals": [{"clip": "a.mp4", "duration": 10}],
                  "audio": [{"file": "vo/x.wav", "at": 3.0, "track": 2}]}])
        entry = recipe.resolve(r, {})["audio"][0]
        self.assertEqual((entry["start"], entry["track"]), (3.0, 2))

    def test_zero_length_visuals_are_dropped_not_placed(self):
        r = rec([{"id": "s1", "visuals": [{"clip": "a.mp4", "duration": 0},
                                          {"clip": "b.mp4", "duration": 5}]}])
        out = recipe.resolve(r, {})
        self.assertEqual(len(out["video"]), 1)
        self.assertEqual(out["video"][0]["ref"], "b.mp4")


class ApplyDefaults(unittest.TestCase):
    DEFAULTS = {"meta": {"fps": 50, "scene_pad": 0.5},
                "subtitles": {"language": "msa", "max_chars": 62},
                "voice": {"name": "ms-MY-OsmanNeural", "rate": "0%"}}

    def test_recipe_beats_config(self):
        r = {"subtitles": {"language": "eng"}}
        self.assertEqual(
            recipe.apply_defaults(r, self.DEFAULTS)["subtitles"]["language"], "eng")

    def test_config_fills_what_the_recipe_omits(self):
        merged = recipe.apply_defaults({"subtitles": {"language": "eng"}},
                                       self.DEFAULTS)
        self.assertEqual(merged["subtitles"]["max_chars"], 62)

    def test_untouched_blocks_survive(self):
        r = {"scenes": [{"id": "s1"}]}
        self.assertEqual(recipe.apply_defaults(r, self.DEFAULTS)["scenes"],
                         [{"id": "s1"}])

    def test_does_not_mutate_the_input(self):
        r = {"subtitles": {"language": "eng"}}
        recipe.apply_defaults(r, self.DEFAULTS)
        self.assertEqual(r, {"subtitles": {"language": "eng"}})


if __name__ == "__main__":
    unittest.main()
