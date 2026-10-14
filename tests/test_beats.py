import math
import random
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from umcares import beats, prepare

NEED = unittest.skipUnless(prepare.find_tool("ffmpeg"), "needs ffmpeg")
SR = 22050


def song(path, bpm, seconds=40, offset=0.0, noise=0.0, hats=True, seed=1, drift=0.0, silent=False):
    """A synthetic track: kick on every beat, a hi-hat between, optional noise bed. `drift` stretches the tempo."""
    rnd = random.Random(seed)
    n = int(seconds * SR)
    buf = [0.0] * n
    period = 60.0 / bpm
    if not silent:
        k = 0
        while True:
            t = offset + k * period * (1 + drift * k / 100)
            if t >= seconds:
                break
            i0 = int(t * SR)
            for j in range(int(0.12 * SR)):
                if i0 + j < n:
                    buf[i0 + j] += 0.8 * math.sin(2 * math.pi * 60 * j / SR) * math.exp(-j / (0.03 * SR))
            if hats:
                h0 = int((t + period / 2) * SR)
                for j in range(int(0.02 * SR)):
                    if h0 + j < n:
                        buf[h0 + j] += 0.25 * (rnd.random() * 2 - 1) * math.exp(-j / (0.005 * SR))
            k += 1
    if noise:
        for i in range(n):
            buf[i] += noise * (rnd.random() * 2 - 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(struct.pack("<h", max(-32767, min(32767, int(v * 32767 * 0.9)))) for v in buf))
    return path


def phase_error(res, offset):
    """Distance in seconds between the found grid and a true beat at `offset`."""
    period = res["period"]
    d = (res["phase"] - offset) % period
    return min(d, period - d)


@NEED
class Analyse(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def check(self, bpm, offset, **kw):
        res = beats.analyse(song(self.tmp / f"s{bpm}-{offset}.wav", bpm, offset=offset, **kw))
        folded = beats._fold(bpm)
        self.assertAlmostEqual(res["bpm"], folded, delta=folded * 0.01, msg=res)
        self.assertLess(phase_error(res, offset), 0.012, res)
        self.assertTrue(beats.steady(res), res)
        return res

    def test_common_tempos(self):
        for bpm in (90, 120, 140):
            with self.subTest(bpm=bpm):
                self.check(bpm, 0.0)

    def test_offset_is_found(self):
        for off in (0.13, 0.31):
            with self.subTest(offset=off):
                self.check(120, off)

    def test_fractional_tempo(self):
        self.check(97, 0.2)

    def test_noisy_track_still_steady(self):
        self.check(110, 0.1, noise=0.15)

    def test_tempo_outside_range_is_folded(self):
        res = beats.analyse(song(self.tmp / "slow.wav", 60, offset=0.0))   # 60 BPM kicks -> 120 grid still on the beat
        self.assertTrue(70 <= res["bpm"] <= 160, res)
        self.assertLess(phase_error(res, 0.0), 0.012, res)

    def test_silence_is_refused(self):
        with self.assertRaises(beats.BeatError):
            beats.analyse(song(self.tmp / "quiet.wav", 120, silent=True))

    def test_noise_only_is_not_steady(self):
        rnd = random.Random(5)
        p = self.tmp / "hiss.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(b"".join(struct.pack("<h", int(rnd.uniform(-9000, 9000))) for _ in range(SR * 30)))
        try:
            res = beats.analyse(p)
        except beats.BeatError:
            return
        self.assertFalse(beats.steady(res), res)

    def test_too_short_is_refused(self):
        with self.assertRaises(beats.BeatError):
            beats.analyse(song(self.tmp / "short.wav", 120, seconds=3))

    def test_cache_is_used(self):
        p = song(self.tmp / "c.wav", 120)
        cache = self.tmp / "cache"
        first = beats.analyse(p, cache)
        self.assertEqual(len(list(cache.glob("*.json"))), 1)
        orig = beats._samples
        beats._samples = lambda *a, **k: (_ for _ in ()).throw(AssertionError("decoded again"))
        try:
            self.assertEqual(beats.analyse(p, cache), first)
        finally:
            beats._samples = orig

    def test_unreadable_file(self):
        p = self.tmp / "x.mp3"
        p.write_bytes(b"not audio")
        with self.assertRaises(beats.BeatError):
            beats.analyse(p)


class Helpers(unittest.TestCase):
    R = {"bpm": 120.0, "period": 0.5, "phase": 0.2, "confidence": 3.0, "duration": 30}

    def test_first_beat(self):
        self.assertEqual(beats.first_beat(self.R, 0), 0.2)
        self.assertEqual(beats.first_beat(self.R, 0.2), 0.2)
        self.assertEqual(beats.first_beat(self.R, 0.21), 0.7)
        self.assertEqual(beats.first_beat(self.R, 10.0), 10.2)

    def test_cut_unit_is_whole_beats_at_least_a_second(self):
        self.assertEqual(beats.cut_unit(self.R), (1.0, 2))
        self.assertEqual(beats.cut_unit({**self.R, "period": 0.75}), (1.5, 2))
        self.assertEqual(beats.cut_unit({**self.R, "period": 1.0}), (1.0, 1))
        self.assertEqual(beats.cut_unit({**self.R, "period": 0.375}), (1.5, 4))

    def test_steady_needs_confidence_and_a_sane_tempo(self):
        self.assertTrue(beats.steady(self.R))
        self.assertFalse(beats.steady({**self.R, "confidence": 1.1}))
        self.assertFalse(beats.steady({**self.R, "bpm": 50.0}))

    def test_fold(self):
        self.assertEqual(beats._fold(60), 120)
        self.assertEqual(beats._fold(240), 120)
        self.assertEqual(beats._fold(100), 100)


if __name__ == "__main__":
    unittest.main()
