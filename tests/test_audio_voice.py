"""Music ducking, narration SSML, card geometry, config precedence."""
import unittest

from umcares import cards, post, recipe, voice
from umcares.config import Config


def level_at(expr: str, t: float) -> float:
    """Evaluate an ffmpeg `volume` envelope at time t.

    The expression is nested if(lt(t,X),A,B), which is close enough to Python
    that a two-function shim evaluates it — far more useful than asserting on
    the string, because it tests the LEVEL the viewer actually hears.
    """
    return eval(expr.replace("if(", "if_("),                    # noqa: S307
                {"if_": lambda c, a, b: a if c else b,
                 "lt": lambda a, b: a < b, "t": t})


class Envelope(unittest.TestCase):
    SECTIONS = [[11, -3], [161, -20], [186, -25], [231, -20], [9999, -3]]

    def test_db_to_linear(self):
        self.assertAlmostEqual(post.db_to_lin(0), 1.0, places=4)
        self.assertAlmostEqual(post.db_to_lin(-20), 0.1, places=4)
        self.assertAlmostEqual(post.db_to_lin(-6), 0.5012, places=3)

    def test_flat_when_no_sections(self):
        self.assertEqual(post.build_envelope([]), "1.0")

    def test_each_section_gets_its_level(self):
        e = post.build_envelope(self.SECTIONS)
        for t, db in ((5, -3), (100, -20), (170, -25), (200, -20), (235, -3)):
            self.assertAlmostEqual(level_at(e, t), post.db_to_lin(db), places=4,
                                   msg=f"t={t}s should be {db} dB")

    def test_boundaries_are_exclusive_upper(self):
        e = post.build_envelope(self.SECTIONS)
        self.assertAlmostEqual(level_at(e, 10.99), post.db_to_lin(-3), places=4)
        self.assertAlmostEqual(level_at(e, 11.0), post.db_to_lin(-20), places=4)

    def test_music_stays_ducked_until_after_the_last_word(self):
        """The v8 defect: music lifted at a scene boundary while a sentence
        was still playing, and buried the closing line."""
        last_narration_ends = 230.44
        e = post.build_envelope(self.SECTIONS)
        self.assertAlmostEqual(level_at(e, last_narration_ends),
                               post.db_to_lin(-20), places=4)

    def test_testimonial_section_is_quieter_than_narration_section(self):
        e = post.build_envelope(self.SECTIONS)
        self.assertLess(level_at(e, 170), level_at(e, 100))


class MarkText(unittest.TestCase):
    def test_plain_text_untouched(self):
        self.assertEqual(voice.mark_text("Selamat pagi", [], []), "Selamat pagi")

    def test_escapes_xml(self):
        self.assertIn("&amp;", voice.mark_text("A & B", [], []))

    def test_bare_acronym_is_spelled_out(self):
        out = voice.mark_text("dari ICYM", [], ["ICYM"])
        self.assertIn('<say-as interpret-as="characters">ICYM</say-as>', out)

    def test_um_cares_spells_the_initialism_and_says_the_word(self):
        out = voice.mark_text("dengan UM Cares", [], ["UM Cares"])
        self.assertIn('<say-as interpret-as="characters">UM</say-as> Cares', out)

    def test_um_press_is_not_spelled_letter_by_letter(self):
        """'UM Press' as one acronym reads 'U-M-P-R-E-S-S'."""
        out = voice.mark_text("dan UM Press", [], ["UM Press"])
        self.assertIn('<say-as interpret-as="characters">UM</say-as> Press', out)
        self.assertNotIn("UM Press</say-as>", out)

    def test_english_loanword_gets_english_phonetics(self):
        out = voice.mark_text("elak scam siber", ["scam siber"], [])
        self.assertIn('<lang xml:lang="en-US">scam cyber</lang>', out)

    def test_acronyms_applied_before_loanwords(self):
        out = voice.mark_text("UM Cares dan scam siber", ["scam siber"], ["UM Cares"])
        self.assertIn("Cares", out)
        self.assertIn("scam cyber", out)


class Flow(unittest.TestCase):
    def test_single_sentence_unchanged(self):
        self.assertEqual(voice.flow("Satu sahaja."), "Satu sahaja.")

    def test_non_final_full_stops_become_commas(self):
        out = voice.flow("Satu. Dua. Tiga.")
        self.assertTrue(out.startswith("Satu,"))
        self.assertTrue(out.rstrip().endswith("Tiga."))

    def test_joins_without_fillers_by_default(self):
        self.assertNotIn("errr", voice.flow("Satu. Dua."))

    def test_fillers_when_asked(self):
        self.assertIn("errr", voice.flow("Satu. Dua.", fillers=True))


class Ssml(unittest.TestCase):
    def test_wraps_in_speak_and_voice(self):
        out = voice.build_ssml("Hai.", {"name": "ms-MY-OsmanNeural"})
        self.assertTrue(out.startswith("<speak"))
        self.assertIn('<voice name="ms-MY-OsmanNeural">', out)
        self.assertTrue(out.endswith("</speak>"))

    def test_applies_prosody_settings(self):
        out = voice.build_ssml("Hai.", {"pitch": "+6%", "rate": "-10%"})
        self.assertIn('pitch="+6%"', out)
        self.assertIn('rate="-10%"', out)

    def test_emphasis_wraps_the_named_phrase(self):
        out = voice.build_ssml("Ini sangat penting.", {}, emphasis="sangat penting")
        self.assertIn("<emphasis", out)


class CardGeometry(unittest.TestCase):
    def test_screen_centre_maps_to_zero(self):
        """json2video measures y from the vertical centre, not the top."""
        self.assertEqual(cards.cy(540), 0)
        self.assertEqual(cards.cy(215), -325)

    def test_logo_strip_clears_the_burned_caption_band(self):
        """A two-line caption reaches about y=918; the strip must end above it."""
        self.assertLessEqual(cards.LOGO_BOTTOM, 915)


class ConfigDefaults(unittest.TestCase):
    def test_defaults_expose_every_recipe_block(self):
        d = Config().defaults()
        for block in ("meta", "subtitles", "audio", "music", "voice", "style",
                      "output"):
            self.assertIn(block, d)

    def test_recipe_beats_config_beats_builtin(self):
        cfg = Config()
        cfg.subtitles.language = "eng"
        merged = recipe.apply_defaults({"subtitles": {"max_chars": 40}},
                                       cfg.defaults())
        self.assertEqual(merged["subtitles"]["max_chars"], 40)     # recipe
        self.assertEqual(merged["subtitles"]["language"], "eng")   # config

    def test_audio_modes_map_to_decibels(self):
        d = Config().defaults()["audio"]
        self.assertGreater(d["keep_db"], d["duck_db"])
        self.assertGreater(d["duck_db"], d["mute_db"])


if __name__ == "__main__":
    unittest.main()
