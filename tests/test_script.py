"""The script round trip, and the drift check that guards it.

The bug class these pin down is the one that shipped twice: `subtitles.srt`
gets hand-edited for readability, the recipe keeps the old wording, and the
next render quietly reverts the fix. So the tests care about two things —
that a round trip through markdown changes nothing on its own, and that the
check tells apart a proofread from a rewrite from a stale file.
"""
import unittest

from umcares import script


RECIPE = {
    "meta": {"title": "Amanah"},
    "scenes": [
        {"id": "open", "visuals": [{"card": "open_v9", "duration": 11}]},
        {"id": "s1", "narration": "Lindungi diri daripada scam cyber.",
         "visuals": [{"clip": "C0006.mp4"}]},
        {"id": "s2", "narration": "Program ini bukan sekadar bengkel.",
         "visuals": [{"clip": "C0011.mp4", "audio": "keep"}]},
        {"id": "testimoni",
         "captions": [{"at": 1.1, "duration": 6.3, "text": "Saya belajar."},
                      {"at": 7.6, "duration": 4.4, "text": "Semak dulu."}],
         "visuals": [{"clip": "C0017.mp4", "audio": "keep"}]},
    ],
}

RESOLVED = {
    "fps": 50, "total": 40.0,
    "video": [{"start": 0.0, "kind": "card", "ref": "open_v9",
               "duration": 11.0, "scene": "open", "audio": "mute"}],
    "audio": [],
    "scenes": [
        {"scene": "open", "start": 0.0, "end": 11.0, "duration": 11.0,
         "narration": 0.0},
        {"scene": "s1", "start": 11.0, "end": 20.0, "duration": 9.0,
         "narration": 4.0},
        {"scene": "s2", "start": 20.0, "end": 30.0, "duration": 10.0,
         "narration": 5.0},
        {"scene": "testimoni", "start": 30.0, "end": 40.0, "duration": 10.0,
         "narration": 0.0},
    ],
    "missing": [], "short": [],
}


def cues_from(recipe: dict = RECIPE) -> list:
    """The SRT a clean build would produce: one cue per scene window."""
    return [
        (12.0, 18.0, recipe["scenes"][1]["narration"]),
        (21.0, 28.0, recipe["scenes"][2]["narration"]),
        (31.1, 37.4, recipe["scenes"][3]["captions"][0]["text"]),
        (37.6, 39.0, recipe["scenes"][3]["captions"][1]["text"]),
    ]


class Compare(unittest.TestCase):
    def test_line_wrapping_is_not_a_change(self):
        self.assertEqual(script.collapse("satu\n  dua   tiga"), "satu dua tiga")

    def test_words_ignores_punctuation_and_case(self):
        self.assertEqual(script.words('apa itu "scam".'),
                         script.words("Apa itu scam"))

    def test_word_diff_names_both_sides(self):
        d = script.word_diff("daripada scam cyber", "daripada penipuan siber")
        self.assertEqual(d["removed"], ["scam", "cyber"])
        self.assertEqual(d["added"], ["penipuan", "siber"])


class Classify(unittest.TestCase):
    NARR = "Sebelum ini, majoriti peserta sudah tahu apa itu scam."

    def test_identical_is_ok(self):
        self.assertEqual(script.classify(self.NARR, self.NARR), "ok")

    def test_punctuation_only_is_a_proofread(self):
        """The client quoted a word. Same speech, so not a rewrite."""
        edited = self.NARR.replace("scam", '"scam"')
        self.assertEqual(script.classify(self.NARR, edited), "edited")

    def test_reworded_is_drift(self):
        edited = self.NARR.replace("scam", "penipuan siber")
        self.assertEqual(script.classify(self.NARR, edited), "drift")

    def test_no_cues_is_missing(self):
        self.assertEqual(script.classify(self.NARR, ""), "missing")

    def test_text_present_elsewhere_is_shifted_not_drift(self):
        """An SRT from an earlier cut has the right words in the wrong window.

        Calling that 'drift' would send someone rewriting narration that is
        already correct; the fix is to rebuild the SRT.
        """
        self.assertEqual(
            script.classify(self.NARR, "sesuatu yang lain",
                            all_subs=f"pembukaan. {self.NARR} penutup."),
            "shifted")


class Check(unittest.TestCase):
    def test_clean_build_passes(self):
        res = script.check(RECIPE, RESOLVED, cues_from())
        self.assertTrue(res["ok"], res["failed"])
        self.assertEqual(res["counts"]["ok"], 3)      # 2 narrated + captions
        self.assertEqual(res["orphans"], [])

    def test_silent_scene_is_not_checked(self):
        scenes = [f["scene"] for f in script.check(RECIPE, RESOLVED,
                                                  cues_from())["scenes"]]
        self.assertNotIn("open", scenes)

    def test_caption_only_scene_is_checked(self):
        res = script.check(RECIPE, RESOLVED, cues_from())
        f = next(f for f in res["scenes"] if f["scene"] == "testimoni")
        self.assertEqual(f["field"], "captions")
        self.assertEqual(f["status"], "ok")

    def test_hand_edited_srt_is_reported_as_drift(self):
        cues = cues_from()
        cues[0] = (12.0, 18.0, "Lindungi diri daripada penipuan siber.")
        res = script.check(RECIPE, RESOLVED, cues)
        self.assertFalse(res["ok"])
        f = next(f for f in res["scenes"] if f["scene"] == "s1")
        self.assertEqual(f["status"], "drift")
        self.assertEqual(f["diff"]["added"], ["penipuan", "siber"])

    def test_missing_cues_fail_rather_than_pass_quietly(self):
        cues = [c for c in cues_from() if c[0] > 20]
        res = script.check(RECIPE, RESOLVED, cues)
        self.assertEqual(
            next(f["status"] for f in res["scenes"] if f["scene"] == "s1"),
            "missing")

    def test_cue_outside_every_scene_is_an_orphan(self):
        res = script.check(RECIPE, RESOLVED,
                           cues_from() + [(100.0, 103.0, "hantu")])
        self.assertEqual(len(res["orphans"]), 1)
        self.assertEqual(res["orphans"][0]["text"], "hantu")

    def test_scene_with_no_window_is_flagged_not_skipped(self):
        resolved = dict(RESOLVED,
                        scenes=[s for s in RESOLVED["scenes"]
                                if s["scene"] != "s2"])
        res = script.check(RECIPE, resolved, cues_from())
        self.assertEqual(
            next(f["status"] for f in res["scenes"] if f["scene"] == "s2"),
            "unresolved")
        self.assertFalse(res["ok"])

    def test_report_lines_name_every_scene(self):
        lines = "\n".join(script.report(script.check(RECIPE, RESOLVED,
                                                    cues_from())))
        for sid in ("s1", "s2", "testimoni"):
            self.assertIn(sid, lines)


class Attribution(unittest.TestCase):
    """A cue belongs to one scene, not to every window it touches."""

    SPANS = [("s1", 11.0, 20.0), ("s2", 20.0, 30.0)]

    def test_a_cue_straddling_a_cut_goes_to_its_larger_side(self):
        mine, orphans = script.attribute([(19.5, 24.0, "sentence")], self.SPANS)
        self.assertEqual(mine["s1"], [])
        self.assertEqual(len(mine["s2"]), 1)
        self.assertEqual(orphans, [])

    def test_a_straddling_cue_is_not_counted_twice(self):
        """Counting it in both windows read as a whole extra sentence of drift."""
        mine, _ = script.attribute([(19.9, 26.0, "x")], self.SPANS)
        self.assertEqual(sum(len(v) for v in mine.values()), 1)

    def test_a_cue_in_no_window_is_an_orphan(self):
        _, orphans = script.attribute([(50.0, 52.0, "hantu")], self.SPANS)
        self.assertEqual(len(orphans), 1)

    def test_boundary_bleed_no_longer_reads_as_drift(self):
        """A cue that runs past its scene used to be read as drift in both.

        In the real export, s8's opening sentence overhung the cut and turned
        up as words added to s7 — a rewrite that had not happened.
        """
        cues = cues_from()
        cues[1] = (21.0, 31.0, RECIPE["scenes"][2]["narration"])  # 1s overhang
        res = script.check(RECIPE, RESOLVED, cues)
        status = {f["scene"]: f["status"] for f in res["scenes"]}
        self.assertEqual(status["s2"], "ok")
        self.assertEqual(status["testimoni"], "ok")


class Aliases(unittest.TestCase):
    """The voice says "seratus", the caption shows "100". Not drift."""

    RECIPE = {"subtitles": {"aliases": [["seratus", "100"]]},
              "scenes": [{"id": "s1", "narration": "Seramai seratus peserta."}]}
    RESOLVED = {"total": 20.0, "scenes": [
        {"scene": "s1", "start": 0.0, "end": 10.0, "duration": 10.0,
         "narration": 5.0}]}

    def test_declared_pair_reads_as_ok(self):
        res = script.check(self.RECIPE, self.RESOLVED,
                           [(1.0, 6.0, "Seramai 100 peserta.")])
        self.assertTrue(res["ok"], res["failed"])

    def test_without_the_pair_it_is_drift(self):
        recipe = dict(self.RECIPE, subtitles={})
        res = script.check(recipe, self.RESOLVED,
                           [(1.0, 6.0, "Seramai 100 peserta.")])
        self.assertFalse(res["ok"])

    def test_an_alias_does_not_hide_a_real_rewrite(self):
        res = script.check(self.RECIPE, self.RESOLVED,
                           [(1.0, 6.0, "Seramai 100 pelajar.")])
        self.assertEqual(res["scenes"][0]["status"], "drift")
        self.assertEqual(res["scenes"][0]["diff"]["added"], ["pelajar"])

    def test_the_reported_text_is_what_the_srt_actually_says(self):
        """Canonicalising is for comparing; a human must still see reality."""
        res = script.check(self.RECIPE, self.RESOLVED,
                           [(1.0, 6.0, "Seramai 100 peserta.")])
        self.assertIn("100", res["scenes"][0]["subs"])

    def test_alias_matching_ignores_case_but_not_word_boundaries(self):
        recipe = {"subtitles": {"aliases": [["lima", "5"]]},
                  "scenes": [{"id": "s1", "narration": "Rating lima."}]}
        res = script.check(recipe, self.RESOLVED, [(1.0, 6.0, "Rating 5.")])
        self.assertTrue(res["ok"], res["failed"])
        res = script.check(recipe, self.RESOLVED, [(1.0, 6.0, "Rating 55.")])
        self.assertFalse(res["ok"])


class RoundTrip(unittest.TestCase):
    def md(self, recipe=RECIPE):
        return script.export_markdown(recipe, recipe_path="recipes/v10.json",
                                      resolved=RESOLVED)

    def test_export_carries_every_scene_marker(self):
        md = self.md()
        for sid in ("open", "s1", "s2", "testimoni"):
            self.assertIn(f"<!-- umcares:scene id={sid} -->", md)

    def test_export_shows_resolved_timings(self):
        self.assertIn("11.0–20.0s", self.md())

    def test_import_of_an_untouched_export_changes_nothing(self):
        """The round trip must be identity, or every export is a diff."""
        parsed = script.parse_markdown(self.md())
        _, changes, unknown = script.apply_markdown(RECIPE, parsed)
        self.assertEqual(changes, [])
        self.assertEqual(unknown, [])

    def test_narration_edit_comes_back(self):
        md = self.md().replace("scam cyber", "penipuan siber")
        updated, changes, _ = script.apply_markdown(
            RECIPE, script.parse_markdown(md))
        self.assertEqual(len(changes), 1)
        self.assertIn("penipuan siber", updated["scenes"][1]["narration"])
        self.assertIn("scam cyber", RECIPE["scenes"][1]["narration"])   # no mutation

    def test_reflowing_a_paragraph_is_not_an_edit(self):
        md = self.md().replace("Lindungi diri daripada",
                               "Lindungi diri\ndaripada")
        _, changes, _ = script.apply_markdown(RECIPE,
                                             script.parse_markdown(md))
        self.assertEqual(changes, [])

    def test_caption_timing_round_trips(self):
        md = self.md().replace("| 1.1 | 6.3 |", "| 1.3 | 6 |")
        updated, changes, _ = script.apply_markdown(
            RECIPE, script.parse_markdown(md))
        caps = updated["scenes"][3]["captions"]
        self.assertEqual(caps[0]["at"], 1.3)
        self.assertEqual(caps[0]["duration"], 6.0)
        self.assertEqual(caps[0]["text"], "Saya belajar.")
        self.assertEqual(changes[0]["field"], "captions")

    def test_visuals_table_is_not_imported(self):
        """Durations are resolved from measured media, never typed in."""
        md = self.md().replace("| card | open_v9 | 11 | mute |",
                               "| card | open_v9 | 99 | mute |")
        updated, changes, _ = script.apply_markdown(
            RECIPE, script.parse_markdown(md))
        self.assertEqual(changes, [])
        self.assertEqual(updated["scenes"][0]["visuals"][0]["duration"], 11)

    def test_deleting_a_narration_clears_it(self):
        md = self.md().replace("Lindungi diri daripada scam cyber.",
                               "_(silent)_")
        updated, changes, _ = script.apply_markdown(
            RECIPE, script.parse_markdown(md))
        self.assertNotIn("narration", updated["scenes"][1])
        self.assertEqual(changes[0]["after"], "")

    def test_scene_absent_from_the_markdown_is_left_alone(self):
        """An author may paste back one section, not the whole file."""
        md = self.md()
        md = md[:md.index("<!-- umcares:scene id=s2 -->")]
        _, changes, unknown = script.apply_markdown(
            RECIPE, script.parse_markdown(md))
        self.assertEqual(changes, [])
        self.assertEqual(unknown, [])

    def test_unknown_scene_id_is_reported_not_added(self):
        md = self.md() + "\n## ghost\n<!-- umcares:scene id=ghost -->\n\n" \
                         "**Narration**\n\nhantu\n"
        updated, changes, unknown = script.apply_markdown(
            RECIPE, script.parse_markdown(md))
        self.assertEqual(unknown, ["ghost"])
        self.assertEqual(changes, [])
        self.assertEqual(len(updated["scenes"]), len(RECIPE["scenes"]))

    def test_a_pipe_in_caption_text_becomes_a_slash(self):
        """A literal pipe would split the row into the wrong cells.

        Substituting it is lossy but visible; a broken table would silently
        drop the caption on the way back in.
        """
        recipe = {"scenes": [{"id": "t", "captions":
                              [{"at": 0, "duration": 3, "text": "a | b"}]}]}
        parsed = script.parse_markdown(script.export_markdown(recipe))
        self.assertEqual(parsed["t"]["captions"][0]["text"], "a / b")


class TimelineMarkdown(unittest.TestCase):
    def test_lists_scenes_and_visuals(self):
        md = script.timeline_markdown(RESOLVED)
        self.assertIn("| open | 0.0 | 11.0 | 11.0 | 0.0 |", md)
        self.assertIn("| 0.0 | card | open_v9 | 11.0 | open | mute |", md)

    def test_missing_durations_are_called_out(self):
        md = script.timeline_markdown(dict(RESOLVED, missing=["card:s7"]))
        self.assertIn("Missing durations", md)
        self.assertIn("`card:s7`", md)

    def test_short_scenes_are_called_out(self):
        md = script.timeline_markdown(dict(RESOLVED, short=[
            {"scene": "s2", "narration": 20.0, "visuals": 12.0,
             "shortfall": 8.0}]))
        self.assertIn("shortfall 8.0s", md)


if __name__ == "__main__":
    unittest.main()
