import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from umcares import prepare, textimg

FF = prepare.find_tool("ffmpeg")
NEED = unittest.skipUnless(textimg.available() and FF, "needs macOS osascript and ffmpeg")


def rgba(path):
    """(width, height, bytes) of the PNG as raw RGBA, decoded by ffmpeg."""
    head = Path(path).read_bytes()
    w, h = struct.unpack(">II", head[16:24])
    raw = subprocess.run([FF, "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgba", "-"], capture_output=True, check=True).stdout
    return w, h, raw


def alpha_at(img, x, y):
    w, h, raw = img
    return raw[(y * w + x) * 4 + 3]


def opaque_columns(img, minimum=200):
    w, h, raw = img
    cols = [x for x in range(w) if any(raw[(y * w + x) * 4 + 3] >= minimum for y in range(0, h, 2))]
    return (cols[0], cols[-1]) if cols else (None, None)


def opaque_rows(img, minimum=200):
    w, h, raw = img
    rows = [y for y in range(h) if any(raw[(y * w + x) * 4 + 3] >= minimum for x in range(0, w, 2))]
    return (rows[0], rows[-1]) if rows else (None, None)


@NEED
class Render(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def draw(self, text, name="t.png", **kw):
        out = self.d / name
        kw.setdefault("width", 800)
        kw.setdefault("height", 300)
        res = textimg.render(text, out, **kw)
        return res, rgba(out)

    def test_draws_real_text_on_a_transparent_background(self):
        res, img = self.draw("Hello CyberDSA", size=80)
        self.assertEqual((img[0], img[1]), (800, 300))
        self.assertEqual(alpha_at(img, 0, 0), 0, "the background is transparent")
        left, right = opaque_columns(img)
        self.assertIsNotNone(left, "something was drawn")
        self.assertGreater(right - left, 200)
        self.assertEqual(res["size"], 80, "short text keeps the requested size")

    def test_centred_text_sits_in_the_middle(self):
        _, img = self.draw("Centre me", size=70)
        left, right = opaque_columns(img)
        self.assertAlmostEqual(left, 800 - right, delta=40)
        top, bottom = opaque_rows(img)
        self.assertAlmostEqual(top, 300 - bottom, delta=40)

    def test_every_line_of_wrapped_text_is_centred_not_just_the_block(self):
        """Single lines hide alignment bugs: the box is as wide as the text. Two lines of different length do not."""
        _, img = self.draw("Short\nA much longer second line here", size=60, width=900, height=400)
        top, bottom = opaque_rows(img)
        mid = (top + bottom) // 2
        w, h, raw = img
        def centre(rows):
            cols = [x for x in range(w) if any(raw[(y * w + x) * 4 + 3] >= 200 for y in rows)]
            return (cols[0] + cols[-1]) / 2
        first, second = centre(range(top, mid - 10)), centre(range(mid + 10, bottom + 1))
        self.assertAlmostEqual(first, 450, delta=30, msg="first line")
        self.assertAlmostEqual(second, 450, delta=30, msg="second line")

    def test_left_alignment_starts_at_the_margin(self):
        _, img = self.draw("Left aligned", size=70, align="left", pad=50)
        left, _ = opaque_columns(img)
        self.assertTrue(40 <= left <= 90, left)

    def test_bottom_alignment_hugs_the_bottom(self):
        _, img = self.draw("Low", size=60, valign="bottom", pad=30)
        _, bottom = opaque_rows(img)
        self.assertGreater(bottom, 300 - 60)

    def test_long_text_shrinks_to_fit_and_is_never_clipped(self):
        res, img = self.draw("Selamat datang ke majlis penutup CyberDSA 2026 dan terima kasih kepada semua", size=120)
        self.assertLess(res["size"], 120)
        left, right = opaque_columns(img, 30)
        top, bottom = opaque_rows(img, 30)
        self.assertGreater(left, 0)
        self.assertLess(right, 799)
        self.assertGreater(top, 0)
        self.assertLess(bottom, 299)

    def test_a_plate_puts_a_translucent_dark_band_behind_the_text(self):
        _, plain = self.draw("Readable", "a.png", size=70)
        _, plated = self.draw("Readable", "b.png", size=70, plate=True)
        def translucent(img):
            w, h, raw = img
            return sum(1 for i in range(3, len(raw), 4) if 20 < raw[i] < 200)
        self.assertGreater(translucent(plated), translucent(plain) + 3000)

    def test_the_plate_can_be_any_colour_such_as_cream_paper_for_a_label(self):
        _, img = self.draw("Label", size=70, plate=True, plate_alpha=1.0, plate_color="#ece3cf", color="#16130f")
        w, h, raw = img
        solid = [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 4) if raw[i + 3] == 255]
        cream = sum(1 for r, g, b in solid if abs(r - 236) <= 2 and abs(g - 227) <= 2 and abs(b - 207) <= 2)      # exact, not colour-managed
        dark = sum(1 for r, g, b in solid if abs(r - 22) <= 3 and abs(g - 19) <= 3 and abs(b - 15) <= 3)
        self.assertGreater(cream, 2000, "a cream plate")
        self.assertGreater(dark, 200, "with dark ink on it")
        with self.assertRaises(textimg.TextError):
            textimg.render("x", self.d / "bad.png", 200, 100, plate=True, plate_color="cream")

    def test_a_caption_is_data_never_code(self):
        marker = self.d / "pwned"
        for text in ['"; touch %s #' % marker, "$(touch %s)" % marker, "`touch %s`" % marker, "it's \"quoted\" \\ back\\slash",
                     "line one\nline two", "</script><b>x</b>", "{ \"json\": true }"]:
            self.draw(text, size=50)
        self.assertFalse(marker.exists(), "text must never be executed")

    def test_other_scripts_and_emoji_draw(self):
        for text in ("Selamat Hari Raya 🎉", "项目 开幕式", "Ñandú ñ café", "العربية"):
            _, img = self.draw(text, size=60)
            self.assertIsNotNone(opaque_columns(img)[0], text)

    def test_an_unknown_font_falls_back_instead_of_failing(self):
        _, img = self.draw("Fallback", size=60, font="NoSuchFont-Bold")
        self.assertIsNotNone(opaque_columns(img)[0])

    def test_the_output_folder_is_created(self):
        out = self.d / "deep" / "er" / "x.png"
        textimg.render("x", out, 200, 100, size=40)
        self.assertTrue(out.is_file())

    def test_colour_is_applied(self):
        _, img = self.draw("Red", size=100, color="#ff0000")
        w, h, raw = img
        solid = [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 4) if raw[i + 3] == 255]
        self.assertTrue(solid)
        r, g, b = solid[len(solid) // 2]
        self.assertGreater(r, 200)
        self.assertLess(g, 60)

    def test_bad_requests_are_refused_with_a_message(self):
        for kw in ({"text": ""}, {"text": "   "}, {"text": "x" * 401}, {"text": "x", "width": 5}, {"text": "x", "align": "right"},
                   {"text": "x", "valign": "diagonal"}, {"text": "x", "color": "red"}):
            args = {"text": "x", "width": 200, "height": 100, **kw}
            with self.assertRaises(textimg.TextError, msg=kw):
                textimg.render(args.pop("text"), self.d / "bad.png", args.pop("width"), args.pop("height"), **args)
        self.assertFalse((self.d / "bad.png").exists())

    def test_without_macos_it_says_so(self):
        with mock.patch.object(textimg, "available", return_value=False):
            with self.assertRaises(textimg.TextError) as cm:
                textimg.render("x", self.d / "x.png", 200, 100)
        self.assertIn("macOS", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
