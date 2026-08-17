"""Caption text, caption timing, and the filter strings that burn them in.

Timing is where captions go wrong in ways nobody notices until they watch the
whole thing: a line that flashes past, a line that sits there after the speaker
has moved on. These tests pin the properties that make captions readable
rather than checking exact numbers, which would just re-state the code.
"""
import unittest

from umcares import burn, srt


class SplitSentences(unittest.TestCase):
    LONG = ("Majlis diakhiri dengan penyampaian cenderahati kepada para "
            "penceramah, serta hadiah kepada peserta yang cemerlang dalam kuiz.")

    def test_short_text_is_one_line(self):
        self.assertEqual(srt.split_sentences("Sekejap sahaja."),
                         ["Sekejap sahaja."])

    def test_splits_on_sentence_boundaries(self):
        self.assertEqual(srt.split_sentences("Satu. Dua. Tiga."),
                         ["Satu.", "Dua.", "Tiga."])

    def test_no_line_exceeds_the_limit(self):
        for line in srt.split_sentences(self.LONG, 62):
            self.assertLessEqual(len(line), 62, line)

    def test_never_strands_a_short_orphan_line(self):
        """Greedy wrapping left a final line of just 'kuiz.'.

        An orphan reads badly, and once cue length follows text length it also
        gets almost no time on screen.
        """
        lines = srt.split_sentences(self.LONG, 62)
        self.assertGreater(len(lines[-1]), 15, f"orphan line: {lines[-1]!r}")

    def test_lines_are_reasonably_balanced(self):
        lines = srt.split_sentences(self.LONG, 62)
        self.assertLess(max(map(len, lines)) - min(map(len, lines)), 30)

    def test_does_not_invent_punctuation(self):
        """Continuation commas put words on screen the speaker never said."""
        text = "Program dibahagikan kepada beberapa sesi yang panjang sekali dan seterusnya"
        joined = " ".join(srt.split_sentences(text, 40))
        self.assertEqual(joined.count(","), text.count(","))

    def test_preserves_every_word(self):
        got = " ".join(srt.split_sentences(self.LONG, 40)).split()
        self.assertEqual(got, self.LONG.split())


class Allocate(unittest.TestCase):
    def test_falls_back_to_even_split_without_spans(self):
        out = srt.allocate([], ["a", "b"], 10.0)
        self.assertEqual(out, [(0.0, 5.0), (5.0, 10.0)])

    def test_longer_lines_get_more_time(self):
        spans = [(0.0, 10.0)]
        short, long_ = "hi", "a considerably longer caption line than the other"
        out = srt.allocate(spans, [short, long_], 10.0)
        self.assertLess(out[0][1] - out[0][0], out[1][1] - out[1][0])

    def test_no_cue_is_unreadably_brief(self):
        """A five-character remnant must still get time on screen."""
        spans = [(0.0, 20.0)]
        lines = ["a much longer line of caption text here", "kuiz."]
        out = srt.allocate(spans, lines, 20.0)
        self.assertGreater(out[1][1] - out[1][0], 1.0)

    def test_cues_stay_inside_detected_speech(self):
        spans = [(0.0, 3.0), (7.0, 10.0)]          # 4s of silence in the middle
        out = srt.allocate(spans, ["one", "two"], 10.0)
        for s, e in out:
            self.assertTrue(any(a - 1e-6 <= s <= b + 1e-6 for a, b in spans), s)
            self.assertTrue(any(a - 1e-6 <= e <= b + 1e-6 for a, b in spans), e)

    def test_a_cue_never_starts_during_a_pause(self):
        spans = [(0.0, 3.0), (7.0, 10.0)]
        out = srt.allocate(spans, ["equal", "equal"], 10.0)
        self.assertAlmostEqual(out[1][0], 7.0, places=2)

    def test_cues_are_ordered_and_non_overlapping(self):
        spans = [(0.0, 5.0), (6.0, 12.0)]
        out = srt.allocate(spans, ["a a a", "b b b", "c c c"], 12.0)
        for i in range(1, len(out)):
            self.assertGreaterEqual(out[i][0], out[i - 1][1] - 1e-6)

    def test_every_cue_has_positive_duration(self):
        out = srt.allocate([(0.0, 2.0)], ["x", "y", "z"], 2.0)
        for s, e in out:
            self.assertGreater(e, s)


class Timestamps(unittest.TestCase):
    def test_formats_srt_timestamp(self):
        self.assertEqual(srt.ts(0), "00:00:00,000")
        self.assertEqual(srt.ts(3661.5), "01:01:01,500")

    def test_clamps_negative_to_zero(self):
        self.assertEqual(srt.ts(-1), "00:00:00,000")

    def test_rounds_millisecond_carry_correctly(self):
        self.assertEqual(srt.ts(1.9999), "00:00:02,000")


class FilterStrings(unittest.TestCase):
    """The burn filters are strings handed to ffmpeg through a shell.

    They broke twice in exactly this layer -- a comma that ended the filter
    early, a backslash that became an invalid escape -- so their SHAPE is
    worth pinning even though the tests look pedantic.
    """
    CUES = [{"png": "/tmp/a.png", "start": 1.0, "end": 2.0},
            {"png": "/tmp/b.png", "start": 3.0, "end": 4.5}]

    def test_overlay_chain_is_empty_without_cues(self):
        self.assertEqual(burn.overlay_chain([], 3, "0:v", "vout"), "")

    def test_overlay_chain_ends_on_the_requested_label(self):
        self.assertTrue(burn.overlay_chain(self.CUES, 3, "0:v", "vout")
                        .endswith("[vout]"))

    def test_overlay_chain_numbers_inputs_from_the_offset(self):
        chain = burn.overlay_chain(self.CUES, 7, "0:v", "vout")
        self.assertIn("[7:v]", chain)
        self.assertIn("[8:v]", chain)

    def test_overlay_chain_escapes_commas_inside_enable(self):
        """An unescaped comma ends the filter instead of separating arguments."""
        chain = burn.overlay_chain(self.CUES, 3, "0:v", "vout")
        self.assertIn("between(t\\,1.000\\,2.000)", chain)

    def test_patch_chain_shifts_each_patch_to_its_position(self):
        chain = burn.patch_chain(
            [{"file": "/x/a.mp4", "at": 225.06, "duration": 12.0}], 3, "0:v", "vout")
        self.assertIn("setpts=PTS-STARTPTS+225.06/TB", chain)
        self.assertIn("between(t\\,225.06\\,237.06)", chain)

    def test_patch_chain_is_empty_without_patches(self):
        self.assertEqual(burn.patch_chain([], 3, "0:v", "vout"), "")

    def test_chains_compose_without_dangling_labels(self):
        """Every intermediate label must be produced before it is consumed."""
        import re
        patches = [{"file": "/x/a.mp4", "at": 0.0, "duration": 11.0}]
        chain = (burn.patch_chain(patches, 3, "0:v", "patched") + ";" +
                 burn.overlay_chain(self.CUES, 4, "patched", "vout"))
        produced = set(re.findall(r"\[(\w+)\](?=;|$)", chain)) | {"0:v"}
        for step in chain.split(";"):
            for label in re.findall(r"^\[(\w+)\]", step):
                if not label[0].isdigit():
                    self.assertIn(label, produced, f"consumed before produced: {label}")
            produced |= set(re.findall(r"\[(\w+)\]$", step))


class ParseSrt(unittest.TestCase):
    def test_reads_cues_back(self):
        import tempfile
        from pathlib import Path
        body = ("1\n00:00:01,000 --> 00:00:02,500\nSatu\n\n"
                "2\n00:00:03,000 --> 00:00:04,000\nDua\nbaris\n")
        with tempfile.NamedTemporaryFile("w", suffix=".srt", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(body)
        cues = burn.parse_srt(Path(fh.name))
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0], (1.0, 2.5, "Satu"))
        self.assertEqual(cues[1][2], "Dua baris")


if __name__ == "__main__":
    unittest.main()
