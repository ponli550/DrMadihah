import unittest

from umcares import kinetic


class Words(unittest.TestCase):
    def test_words_are_upper_case_and_capped(self):
        self.assertEqual(kinetic.split_words("Dua hari. Satu cabaran!"), ["DUA", "HARI.", "SATU", "CABARAN!"])
        self.assertEqual(len(kinetic.split_words(" ".join(["kata"] * 40))), kinetic.MAX_WORDS)
        self.assertEqual(kinetic.split_words("   "), [])
        self.assertEqual(kinetic.split_words(None), [])

    def test_a_short_caption_is_one_word_per_line_and_a_long_one_is_grouped_to_five_lines_at_most(self):
        self.assertEqual(kinetic.lines_for(["A", "B", "C"]), ["A", "B", "C"])
        self.assertEqual(kinetic.lines_for(["A"] * 5), ["A"] * 5)
        grouped = kinetic.lines_for([str(i) for i in range(10)])
        self.assertEqual(len(grouped), 5)
        self.assertEqual(grouped[0], "0 1")
        for n in range(1, kinetic.MAX_WORDS + 1):
            self.assertLessEqual(len(kinetic.lines_for(["w"] * n)), kinetic.MAX_LINES, n)
        self.assertEqual(kinetic.lines_for([]), [])


class Timing(unittest.TestCase):
    def test_off_the_beat_lines_are_spread_evenly_after_a_short_lead(self):
        t = kinetic.schedule(3, 3.0)
        self.assertEqual(t[0], kinetic.LEAD)
        self.assertEqual([round(b - a, 3) for a, b in zip(t, t[1:])], [0.5, 0.5])

    def test_on_the_beat_the_first_line_lands_on_the_first_frame_and_the_rest_on_beats(self):
        t = kinetic.schedule(3, 3.2, period=0.8)
        self.assertEqual(t, [0.0, 0.8, 1.6])

    def test_a_long_caption_on_the_beat_drops_to_half_beats_and_stays_on_the_grid(self):
        period = 0.8
        t = kinetic.schedule(5, 3.2, period=period)               # five beats would run to 3.2 s: too late
        self.assertEqual(t[0], 0.0)
        for x in t:
            half = period / 2
            self.assertAlmostEqual(round(x / half) * half, x, delta=0.01, msg=f"{x} is on a half-beat")
        self.assertLessEqual(t[-1], 3.2 - kinetic.END_MARGIN + 1e-6)

    def test_a_piece_too_short_for_its_words_squeezes_them_but_never_overruns(self):
        t = kinetic.schedule(5, 1.0, period=0.8)
        self.assertLessEqual(t[-1], 1.0 - kinetic.END_MARGIN + 1e-6)
        self.assertEqual(t, sorted(t))
        self.assertEqual(len(set(t)), 5, "no two lines arrive together")

    def test_the_last_line_always_lands_before_the_end(self):
        for d in (0.8, 1.2, 2.0, 3.5):
            for n in (1, 3, 5):
                for period in (None, 0.5, 0.8, 1.2):
                    t = kinetic.schedule(n, d, period)
                    self.assertEqual(len(t), n)
                    self.assertLessEqual(t[-1], max(0.0, d - kinetic.END_MARGIN) + 1e-6, (d, n, period))

    def test_nothing_to_say(self):
        self.assertEqual(kinetic.schedule(0, 2.0), [])



class Place(unittest.TestCase):
    def test_the_lowest_line_never_goes_below_the_limit(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        seen = {}

        def fake_render(text, out, w, h, **kw):
            Path(out).write_bytes(b"png")
        with mock.patch.object(kinetic.textimg, "render", fake_render):
            for centre in (0.5, 0.68, 0.9):
                for n_lines in (1, 3, 5):
                    _, graph, lines, _, y_end = kinetic.overlay(Path(tempfile.mkdtemp()), "s", " ".join(["w"] * n_lines), 1080, 1920, 30, 3.0,
                                                               "#fff", None, 1, "[v]", "[o]", centre=centre)
                    self.assertEqual(len(lines), n_lines)
                    self.assertLessEqual(y_end, int(1920 * kinetic.LOWEST) + 1, (centre, n_lines))


if __name__ == "__main__":
    unittest.main()
