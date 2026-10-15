import json
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from umcares import kinetic, prepare, sfx

FF = prepare.find_tool("ffmpeg")


def seg(kind, start, duration, **kw):
    return {"kind": kind, "start": start, "duration": duration, **kw}


SEGS = [seg("title", 0.0, 2.5, text="CyberDSA 2026"), seg("photo", 2.5, 2.0, text="Dua hari satu cabaran"), seg("video", 4.5, 3.0),
        seg("countdown", 7.5, 2.4, count=3, text="3-2-1"), seg("end", 9.9, 2.5, text="Jumpa lagi")]


class Events(unittest.TestCase):
    def test_a_cut_effect_lands_on_every_cut_but_not_the_start(self):
        ev = sfx.events(SEGS, 12.4, 0.0, {"cuts": "whoosh"}, False)
        self.assertEqual([e["t"] for e in ev], [2.5, 4.5, 7.5, 9.9])
        self.assertTrue(all(e["kind"] == "cut" and e["sound"] == "whoosh" for e in ev))

    def test_on_a_crossfading_look_the_cut_is_the_middle_of_the_overlap(self):
        ev = sfx.events([seg("photo", 0.0, 2.0), seg("photo", 1.6, 2.0)], 3.6, 0.4, {"cuts": "pop"}, False)
        self.assertEqual([e["t"] for e in ev], [1.8])

    def test_word_effects_follow_the_kinetic_schedule_and_only_on_that_look(self):
        words = sfx.events(SEGS, 12.4, 0.0, {"words": "tick"}, True)
        want = [round(2.5 + t, 3) for t in kinetic.schedule(len(kinetic.lines_for(kinetic.split_words("Dua hari satu cabaran"))), 2.0, None)]
        got = [e["t"] for e in words if 2.5 <= e["t"] < 4.5]
        self.assertEqual(got, want, "the same times the words pop in on the picture")
        none = sfx.events(SEGS, 12.4, 0.0, {"words": "tick"}, False)
        self.assertFalse([e for e in none if e["t"] < 7.5], "no word pops, no word ticks, on another look")
        self.assertEqual(len([e for e in none if 7.5 <= e["t"] < 9.9]), 3, "but a countdown ticks on every number in every look")

    def test_a_countdown_ticks_once_per_number_on_equal_shares(self):
        ev = [e for e in sfx.events(SEGS, 12.4, 0.0, {"words": "pop"}, False)]
        self.assertEqual([e["t"] for e in ev], [7.5, 8.3, 9.1])

    def test_on_a_tempo_the_word_effects_are_on_the_beat_grid(self):
        ev = sfx.events([seg("photo", 0.0, 1.6, text="Satu dua tiga")], 1.6, 0.0, {"words": "tick"}, True, period=0.8)
        half = 0.4
        for e in ev:
            self.assertAlmostEqual(round(e["t"] / half) * half, e["t"], delta=0.01)

    def test_nothing_is_placed_past_the_end_and_nothing_with_no_spec(self):
        self.assertEqual(sfx.events(SEGS, 5.0, 0.0, {"cuts": "hit"}, False), [{"t": 2.5, "kind": "cut", "sound": "hit"}, {"t": 4.5, "kind": "cut", "sound": "hit"}])
        self.assertEqual(sfx.events(SEGS, 12.4, 0.0, {}, True), [])
        self.assertEqual(sfx.events([seg("photo", 0.0, 3.0)], 3.0, 0.0, {"cuts": "hit"}, False), [], "one piece has no cut")

    def test_cuts_and_words_together_are_in_time_order(self):
        ev = sfx.events(SEGS, 12.4, 0.0, {"cuts": "whoosh", "words": "tick"}, True)
        self.assertEqual([e["t"] for e in ev], sorted(e["t"] for e in ev))
        self.assertEqual({e["kind"] for e in ev}, {"cut", "word"})


@unittest.skipUnless(FF, "needs ffmpeg")
class Sounds(unittest.TestCase):
    def test_every_effect_is_made_as_a_short_stereo_48k_wav_that_is_audible_and_not_clipped(self):
        d = Path(tempfile.mkdtemp())
        for name in sfx.SOUNDS:
            with self.subTest(name=name):
                out = sfx.render_wav(name, d / f"{name}.wav")
                with wave.open(str(out)) as w:
                    self.assertEqual((w.getnchannels(), w.getframerate()), (2, 48000))
                    seconds = w.getnframes() / w.getframerate()
                    raw = w.readframes(w.getnframes())
                self.assertTrue(0.04 < seconds < 0.6, seconds)
                samples = struct.unpack(f"<{len(raw) // 2}h", raw)
                peak = max(abs(x) for x in samples) / 32768
                self.assertTrue(0.25 < peak <= 0.91, f"peak {peak:.2f}: audible but limited")

    def test_an_unknown_effect_is_refused_with_its_name(self):
        with self.assertRaises(sfx.SfxError) as ctx:
            sfx.render_wav("laser", Path(tempfile.mkdtemp()) / "x.wav")
        self.assertIn("laser", str(ctx.exception))

    def test_the_whoosh_starts_before_its_event_so_its_body_lands_on_it(self):
        self.assertGreater(sfx.lead("whoosh"), 0.15)
        self.assertEqual(sfx.lead("hit"), 0.0)
        self.assertEqual(sfx.lead("tick"), 0.0)


if __name__ == "__main__":
    unittest.main()
