import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from umcares import prepare, speech
from tests.test_beats import song

FF = prepare.find_tool("ffmpeg")
SAY = shutil.which("say")
NEED = unittest.skipUnless(FF, "needs ffmpeg")
NEED_SAY = unittest.skipUnless(FF and SAY, "needs ffmpeg and macOS `say` for real speech")


def ff(*a):
    subprocess.run([FF, "-v", "error", "-y", *a], check=True, capture_output=True)


class Fixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Path(tempfile.mkdtemp())
        ff("-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "2", str(cls.d / "sil2.wav"))
        ff("-f", "lavfi", "-i", "anoisesrc=d=4:c=pink:a=0.08:r=16000", "-ac", "1", str(cls.d / "noise4.wav"))
        ff("-f", "lavfi", "-i", "anoisesrc=d=14:c=pink:a=0.05:r=16000", "-ac", "1", str(cls.d / "bed14.wav"))
        if SAY:
            subprocess.run([SAY, "-o", str(cls.d / "a.aiff"), "Selamat datang ke hari terbuka CyberDSA dua ribu dua puluh enam. Terima kasih kerana hadir."], check=True)
            subprocess.run([SAY, "-o", str(cls.d / "b.aiff"), "Hello everyone and welcome to the opening of the event today."], check=True)
            for n in "ab":
                ff("-i", str(cls.d / f"{n}.aiff"), "-ar", "16000", "-ac", "1", str(cls.d / f"{n}.wav"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    def join(self, name, *parts):
        args = []
        for p in parts:
            args += ["-i", str(self.d / f"{p}.wav")]
        ff(*args, "-filter_complex", f"concat=n={len(parts)}:v=0:a=1", "-ar", "16000", "-ac", "1", str(self.d / f"{name}.wav"))
        return self.d / f"{name}.wav"


@NEED_SAY
class RealSpeech(Fixtures):
    def test_finds_each_phrase_where_it_is_and_not_the_silence_or_noise_around_it(self):
        mix = self.join("mix", "sil2", "a", "sil2", "noise4", "b")
        r = speech.analyse(mix)
        a_len = 6.29
        starts = [s for s, _, _ in r["spans"]]
        ends = [e for _, e, _ in r["spans"]]
        self.assertAlmostEqual(starts[0], 2.0, delta=0.3)
        self.assertAlmostEqual(max(e for e in ends if e < 10), 2.0 + a_len, delta=0.4)
        self.assertAlmostEqual(starts[-1], 2.0 + a_len + 2 + 4, delta=0.4, msg="the noise before the second phrase is not speech")
        self.assertTrue(all(s < 8.5 or s > 14 for s in starts), r["spans"])

    def test_speech_over_a_continuous_noise_bed(self):
        ff("-i", str(self.d / "bed14.wav"), "-i", str(self.d / "a.wav"), "-i", str(self.d / "b.wav"), "-filter_complex",
           "[1]adelay=1500[a1];[2]adelay=8500[b1];[0][a1][b1]amix=inputs=3:normalize=0,atrim=0:14", "-ar", "16000", "-ac", "1",
           str(self.d / "real.wav"))
        r = speech.analyse(self.d / "real.wav")
        self.assertAlmostEqual(r["spans"][0][0], 1.5, delta=0.3)
        self.assertAlmostEqual(r["spans"][-1][1], 8.5 + 3.0, delta=0.4)
        self.assertGreater(r["speech_seconds"], 7.0)
        self.assertFalse(any(5.9 < s < 6.0 for s, _, _ in r["spans"]))

    def test_best_start_lands_on_the_words_with_a_lead_in(self):
        spans = speech.analyse(self.join("late", "sil2", "sil2", "sil2", "a"))["spans"]       # speech starts at 6 s
        got = speech.best_start(spans, 12.3, 3.0)
        self.assertAlmostEqual(got[0], 6.0 - 0.3, delta=0.3)
        self.assertGreater(got[1], 2.5)


@NEED
class NotSpeech(Fixtures):
    def test_silence_steady_noise_and_a_noise_bed_have_none(self):
        for n in ("sil2", "noise4", "bed14"):
            with self.subTest(n):
                r = speech.analyse(self.d / f"{n}.wav")
                self.assertEqual(r["spans"], [])
                self.assertTrue(r["has_audio"])

    def test_drums_and_hats_are_percussion_not_speech(self):
        for kw in ({}, {"hats": False}):
            with self.subTest(kw):
                self.assertEqual(speech.analyse(song(self.d / f"d{len(kw)}.wav", 120, seconds=20, **kw))["spans"], [])

    def test_a_clip_with_no_sound_track_says_so(self):
        ff("-f", "lavfi", "-i", "testsrc2=size=160x90:d=2", "-pix_fmt", "yuv420p", str(self.d / "mute.mp4"))
        r = speech.analyse(self.d / "mute.mp4")
        self.assertFalse(r["has_audio"])
        self.assertEqual(r["spans"], [])

    def test_an_unreadable_file_is_refused_with_a_message(self):
        p = self.d / "x.mp4"
        p.write_bytes(b"not a video")
        with self.assertRaises(speech.SpeechError):
            speech.analyse(p)

    def test_cache_is_used(self):
        cache = self.d / "cache"
        first = speech.analyse(self.d / "noise4.wav", cache)
        self.assertEqual(len(list(cache.glob("*.json"))), 1)
        orig = speech._samples
        speech._samples = lambda *a, **k: (_ for _ in ()).throw(AssertionError("decoded again"))
        try:
            self.assertEqual(speech.analyse(self.d / "noise4.wav", cache), first)
        finally:
            speech._samples = orig


class Choosing(unittest.TestCase):
    SPANS = [[3.0, 4.0, 0.7], [8.0, 12.0, 0.7]]

    def test_picks_the_window_with_the_most_speech(self):
        start, got = speech.best_start(self.SPANS, 20.0, 4.0)
        self.assertEqual((start, got), (7.7, 3.7))   # starts 0.3 s early so the first word is not clipped

    def test_ties_go_to_the_earliest(self):
        self.assertEqual(speech.best_start([[2.0, 3.0, 0.7], [9.0, 10.0, 0.7]], 20.0, 2.0)[0], 1.7)

    def test_clamps_to_the_clip(self):
        start, _ = speech.best_start([[18.5, 19.5, 0.7]], 20.0, 4.0)
        self.assertEqual(start, 16.0)

    def test_nothing_to_follow(self):
        self.assertIsNone(speech.best_start([], 20.0, 4.0))
        self.assertIsNone(speech.best_start(self.SPANS, 4.0, 4.0), "the whole clip is used anyway")

    def test_covered(self):
        self.assertAlmostEqual(speech.covered(self.SPANS, 3.5, 9.0), 1.5)


class Tightening(unittest.TestCase):
    SPANS = [[1.0, 3.0, 0.7], [3.4, 5.0, 0.7], [9.0, 11.0, 0.7], [15.0, 16.0, 0.7]]

    def test_each_phrase_is_kept_with_a_little_either_side_and_phrases_a_short_pause_apart_are_joined(self):
        r = speech.tighten(self.SPANS, 20.0)
        self.assertEqual(r[0], [round(1.0 - speech.LEAD, 3), round(5.0 + speech.TAIL, 3)], "1-3 s and 3.4-5 s are one stretch: a 0.4 s pause is natural")
        self.assertEqual(r[1], [round(9.0 - speech.LEAD, 3), round(11.0 + speech.TAIL, 3)])
        self.assertEqual(len(r), 3, "the long silences between 5 and 9 s, and 11 and 15 s are cut")

    def test_the_kept_stretches_are_in_order_inside_the_clip_and_never_overlap(self):
        r = speech.tighten(self.SPANS, 16.1)
        self.assertEqual(r, sorted(r))
        for (a, b), (c, d) in zip(r, r[1:]):
            self.assertLess(b, c)
        self.assertGreaterEqual(r[0][0], 0.0)
        self.assertLessEqual(r[-1][1], 16.1)

    def test_a_phrase_at_the_very_start_or_end_is_clamped_to_the_clip(self):
        r = speech.tighten([[0.02, 1.0, 0.7], [19.9, 20.0, 0.7]], 20.0)
        self.assertEqual(r[0][0], 0.0)
        self.assertLessEqual(max(b for a, b in r), 20.0)

    def test_start_skips_what_comes_before_it(self):
        r = speech.tighten(self.SPANS, 20.0, start=8.0)
        self.assertGreaterEqual(r[0][0], 8.0)
        self.assertEqual(len(r), 2)

    def test_a_budget_caps_the_total_and_shortens_the_last_stretch(self):
        r = speech.tighten(self.SPANS, 20.0, budget=5.0)
        self.assertAlmostEqual(sum(b - a for a, b in r), 5.0, delta=0.01)
        r = speech.tighten(self.SPANS, 20.0, budget=100.0)
        self.assertEqual(len(r), 3, "a big budget keeps everything")

    def test_nothing_to_keep(self):
        self.assertEqual(speech.tighten([], 10.0), [])
        self.assertEqual(speech.tighten([[1.0, 1.1, 0.7]], 10.0, lead=0.0, tail=0.0), [], "a blip shorter than the minimum is dropped")

    def test_to_tight_maps_a_source_interval_onto_the_joined_clip(self):
        ranges = [[1.0, 3.0], [9.0, 11.0]]
        self.assertEqual(speech.to_tight(ranges, 1.5, 2.5), [[0.5, 1.5]])
        self.assertEqual(speech.to_tight(ranges, 9.5, 10.0), [[2.5, 3.0]], "the second stretch starts where the first ended: 2 s")
        self.assertEqual(speech.to_tight(ranges, 4.0, 8.0), [], "silence that was cut has no place on the new timeline")
        self.assertEqual(speech.to_tight(ranges, 2.0, 10.0), [[1.0, 2.0], [2.0, 3.0]], "an interval across the cut lands as two pieces")


if __name__ == "__main__":
    unittest.main()
