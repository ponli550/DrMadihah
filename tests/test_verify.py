"""The delivery verifier.

A verifier nobody tests is decoration. These use synthetic thumbnails so the
decision logic can be exercised without a remote, a render, or ffmpeg.
"""
import unittest

from umcares import verify


def thumb(full: int = 100, caption: int = 100) -> bytes:
    """A fake sample: `full` grey in the frame rows, `caption` in the band."""
    return (bytes([full]) * (verify.TW * verify.FULL_ROWS) +
            bytes([caption]) * (verify.TW * verify.CAP_ROWS))


def visual(ref="a.mp4", start=0.0, duration=10.0, captioned=False):
    return {"ref": ref, "start": start, "duration": duration,
            "src": f"/src/{ref}", "captioned": captioned}


def raw(thumbs, duration=10.0, lufs=None, black=None):
    return {"duration": duration, "thumbs": thumbs,
            "lufs": lufs if lufs is not None else {}, "black": black or []}


def failed(result):
    return {f["check"] for f in result["failed"]}


class Mad(unittest.TestCase):
    def test_identical_is_zero(self):
        self.assertEqual(verify.mad([1, 2, 3], [1, 2, 3]), 0.0)

    def test_mean_absolute_difference(self):
        self.assertAlmostEqual(verify.mad([0, 0], [10, 20]), 15.0)

    def test_unusable_samples_score_high_not_zero(self):
        """Empty input must never look like a perfect match."""
        self.assertEqual(verify.mad([], [1]), 999.0)
        self.assertEqual(verify.mad([1, 2], [1]), 999.0)


class Bands(unittest.TestCase):
    def test_caption_band_is_separate_from_the_frame(self):
        t = thumb(full=0, caption=255)
        self.assertTrue(all(v == 0 for v in verify._band(t, *verify.PICTURE_FULL)))
        self.assertTrue(all(v == 255 for v in verify._band(t, *verify.CAPTION_BAND)))

    def test_picture_band_excludes_captions_only_when_needed(self):
        self.assertGreater(verify.PICTURE_FULL[1], verify.PICTURE_ABOVE_CAPTION[1])


class Check(unittest.TestCase):
    RESOLVED = {"total": 10.0, "scenes": []}

    def test_passes_when_everything_matches(self):
        v = [visual()]
        r = raw({"d0": thumb(), "s0": thumb()})
        self.assertTrue(verify.check(self.RESOLVED, r, v, burned=False)["ok"])

    def test_catches_duration_drift(self):
        v = [visual()]
        r = raw({"d0": thumb(), "s0": thumb()}, duration=12.0)
        self.assertIn("duration", failed(verify.check(self.RESOLVED, r, v, False)))

    def test_catches_a_visual_that_does_not_match_its_source(self):
        v = [visual()]
        r = raw({"d0": thumb(full=20), "s0": thumb(full=200)})
        self.assertIn("picture matches sources",
                      failed(verify.check(self.RESOLVED, r, v, False)))

    # -- the regression this whole module exists for ------------------------
    def test_catches_a_stale_card_differing_only_in_its_lower_half(self):
        """The card patch that silently failed differed only near the bottom.

        The first version of the picture check excluded that region outright to
        dodge captions, so a wrong card scored a perfect match. With no cue on
        screen the comparison must cover the full frame.
        """
        d = bytes([100]) * (verify.TW * (verify.FULL_ROWS - 4)) + \
            bytes([200]) * (verify.TW * 4) + \
            bytes([100]) * (verify.TW * verify.CAP_ROWS)
        r = raw({"d0": d, "s0": thumb(full=100, caption=100)})
        res = verify.check(self.RESOLVED, r, [visual(captioned=False)], burned=True)
        self.assertIn("picture matches sources", failed(res))

    def test_ignores_the_caption_strip_when_a_cue_is_on_screen(self):
        """A burned caption must not be mistaken for the wrong shot."""
        d = bytes([100]) * (verify.TW * (verify.FULL_ROWS - 2)) + \
            bytes([255]) * (verify.TW * 2) + \
            bytes([255]) * (verify.TW * verify.CAP_ROWS)
        r = raw({"d0": d, "s0": thumb(full=100, caption=100)})
        res = verify.check(self.RESOLVED, r, [visual(captioned=True)], burned=True)
        self.assertNotIn("picture matches sources", failed(res))

    def test_caption_detection_is_relative_to_the_picture_difference(self):
        """A caption over low-contrast footage moves the average only slightly.

        An absolute threshold called such a frame uncaptioned when the text was
        plainly on screen. What matters is how much the strip differs BEYOND the
        frame's own re-encoding noise.
        """
        d = bytes([100]) * (verify.TW * verify.FULL_ROWS) + \
            bytes([108]) * (verify.TW * verify.CAP_ROWS)      # a faint caption
        r = raw({"d0": d, "s0": thumb(full=100, caption=100)})
        res = verify.check(self.RESOLVED, r, [visual(captioned=True)], burned=True)
        self.assertTrue(res["ok"], "a faint but real caption must not be flagged")

    def test_a_noisy_frame_alone_does_not_look_like_a_caption(self):
        """If the WHOLE frame differs, the strip difference is not an overlay."""
        d = bytes([130]) * (verify.TW * verify.FULL_ROWS) + \
            bytes([130]) * (verify.TW * verify.CAP_ROWS)
        r = raw({"d0": d, "s0": thumb(full=100, caption=100)})
        res = verify.check(self.RESOLVED, r, [visual(captioned=True)], burned=True)
        self.assertIn("captions burned in", failed(res))

    def test_catches_a_burn_that_did_nothing(self):
        r = raw({"d0": thumb(), "s0": thumb()})
        res = verify.check(self.RESOLVED, r, [visual(captioned=True)], burned=True)
        self.assertIn("captions burned in", failed(res))

    def test_catches_a_caption_burned_where_no_cue_exists(self):
        r = raw({"d0": thumb(caption=255), "s0": thumb(caption=0)})
        res = verify.check(self.RESOLVED, r, [visual(captioned=False)], burned=True)
        self.assertIn("captions burned in", failed(res))

    def test_caption_checks_are_skipped_for_soft_subtitles(self):
        r = raw({"d0": thumb(), "s0": thumb()})
        res = verify.check(self.RESOLVED, r, [visual(captioned=True)], burned=False)
        self.assertTrue(res["ok"])

    def test_unsampled_frames_are_reported_not_ignored(self):
        """A missing sample must never be read as a pass."""
        res = verify.check(self.RESOLVED, raw({}), [visual()], burned=False)
        self.assertIn("frames sampled", failed(res))

    def test_catches_a_scene_outside_the_loudness_band(self):
        v = [visual()]
        r = raw({"d0": thumb(), "s0": thumb()}, lufs={"s1": "-39.0 LUFS"})
        res = verify.check(self.RESOLVED, r, v, False, target_lufs=-16.0)
        self.assertTrue(any("loudness" in c for c in failed(res)))

    def test_accepts_scenes_inside_the_loudness_band(self):
        v = [visual()]
        r = raw({"d0": thumb(), "s0": thumb()}, lufs={"s1": "-16.4 LUFS"})
        self.assertTrue(verify.check(self.RESOLVED, r, v, False)["ok"])

    def test_catches_black_gaps(self):
        v = [visual()]
        r = raw({"d0": thumb(), "s0": thumb()},
                black=["black_start:12.0 black_end:14.5"])
        self.assertIn("no black gaps", failed(verify.check(self.RESOLVED, r, v, False)))


if __name__ == "__main__":
    unittest.main()
