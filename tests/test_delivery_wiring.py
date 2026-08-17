"""That the delivery actually does what it was wired to do.

Written because of a specific failure: a card patch was passed to a branch that
never executes, ffmpeg ran for four minutes, and the result was returned with no
error and the wrong picture. The parameter existed, the call succeeded, and the
feature did nothing.

So these assert on the ffmpeg invocation itself — that the encode targets a
staged path, that patches and captions actually reach the filter graph, and that
the destination is only replaced through `commit_remote`.
"""
import unittest

from umcares import post, safe


class FakeResult:
    def __init__(self, stdout=""):
        self.stdout = stdout
        self.stderr = ""
        self.rc = 0

    def check(self, _label):
        return self


class FakeTransport:
    """Answers probes plausibly and records everything it is asked to run."""

    def __init__(self):
        self.scripts = []
        self.commands = []

    def run(self, cmd, timeout=None):
        self.commands.append(cmd)
        if "COMMITTED" in cmd or "mv -f" in cmd:
            return FakeResult("COMMITTED\n")
        return FakeResult("120.0\n")           # any duration probe

    def run_script(self, script, timeout=None, shell="bash"):
        self.scripts.append(script)
        return FakeResult('---RESULT---\n{"streams":[],"format":{}}\n')

    def exists(self, _p):
        return True

    def size(self, _p):
        return 10_000_000


def mix(t, **kw):
    return post.mix(t, "/r/exports/master.mxf", "/r/assets/music/m.mp3",
                    "/r/subtitles/subtitles.srt", "/r/exports/delivery.mp4", **kw)


class Staging(unittest.TestCase):
    def test_encode_targets_the_staged_path_not_the_destination(self):
        t = FakeTransport()
        mix(t)
        script = t.scripts[0]
        self.assertIn(safe.part_path("/r/exports/delivery.mp4"), script)

    def test_destination_is_never_an_ffmpeg_output(self):
        """`ffmpeg -y` on the real path would truncate a good delivery."""
        t = FakeTransport()
        mix(t)
        for line in t.scripts[0].splitlines():
            if line.strip().startswith("ffmpeg"):
                self.assertNotIn("faststart '/r/exports/delivery.mp4'", line)

    def test_destination_is_replaced_only_by_a_commit(self):
        t = FakeTransport()
        mix(t)
        self.assertTrue(any("mv -f" in c and "delivery.part.mp4" in c
                            for c in t.commands),
                        "no commit step ran")


class FeaturesReachFfmpeg(unittest.TestCase):
    CUES = [{"png": "/r/subtitles/captions/cue_0000.png", "start": 1.0, "end": 2.0}]
    PATCHES = [{"file": "/r/assets/cards/card_open.mp4", "at": 0.0, "duration": 11.0}]

    def test_soft_subtitles_are_muxed_and_flagged_default(self):
        t = FakeTransport()
        mix(t)
        s = t.scripts[0]
        self.assertIn("-c:s mov_text", s)
        self.assertIn("-disposition:s:0 default", s)

    def test_burned_captions_add_their_inputs_and_filters(self):
        t = FakeTransport()
        mix(t, burn_pngs=self.CUES)
        s = t.scripts[0]
        self.assertIn("cue_0000.png", s)
        self.assertIn("[vout]", s)
        self.assertNotIn("-c:s mov_text", s)

    # -- the bug this file exists for --------------------------------------
    def test_patches_reach_the_filter_graph(self):
        """A patch that never enters the graph is the four-minute no-op."""
        t = FakeTransport()
        mix(t, patches=self.PATCHES)
        s = t.scripts[0]
        self.assertIn("card_open.mp4", s)
        self.assertIn("setpts=PTS-STARTPTS+0.0/TB", s)
        self.assertIn('-map "[vout]"', s)

    def test_patches_and_captions_compose(self):
        t = FakeTransport()
        mix(t, burn_pngs=self.CUES, patches=self.PATCHES)
        s = t.scripts[0]
        self.assertIn("card_open.mp4", s)
        self.assertIn("cue_0000.png", s)
        self.assertIn("[patched]", s)

    def test_patch_inputs_are_numbered_before_caption_inputs(self):
        """Input order and filter indices must agree or the graph is wrong."""
        t = FakeTransport()
        mix(t, burn_pngs=self.CUES, patches=self.PATCHES)
        s = t.scripts[0]
        self.assertLess(s.index("card_open.mp4"), s.index("cue_0000.png"))
        self.assertIn("[3:v]setpts", s)      # master0 music1 music2 -> patch at 3
        self.assertIn("[4:v]", s)            # caption png next

    def test_video_is_passed_through_when_only_patching(self):
        t = FakeTransport()
        mix(t, patches=self.PATCHES)
        self.assertIn("[vout]", t.scripts[0])


class Failure(unittest.TestCase):
    def test_a_failed_encode_discards_the_staged_file_and_raises(self):
        class Failing(FakeTransport):
            def run_script(self, script, timeout=None, shell="bash"):
                self.scripts.append(script)
                r = FakeResult()
                r.check = lambda _l: (_ for _ in ()).throw(RuntimeError("ffmpeg died"))
                return r

        t = Failing()
        with self.assertRaises(RuntimeError):
            mix(t)
        self.assertTrue(any("rm -f" in c and "delivery.part.mp4" in c
                            for c in t.commands),
                        "staged file left behind after a failure")
        self.assertFalse(any("mv -f" in c for c in t.commands),
                         "destination replaced despite a failed encode")


if __name__ == "__main__":
    unittest.main()
