"""The project page in a real browser (Chrome headless), for what a DOM shim cannot show."""
import os
import time
import unittest

from tests import browser

NEED = unittest.skipUnless(os.path.exists(browser.CHROME), "needs Google Chrome")


@NEED
class RealPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.home, cls.root = browser.serve()
        cls.px = browser.proxy(cls.srv.server_address[1])
        cls.ch = browser.Chrome()
        t = cls.ch.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        cls.s = cls.ch.send("Target.attachToTarget", {"targetId": t, "flatten": True})["sessionId"]
        cls.ch.send("Page.enable", session=cls.s)
        cls.ch.send("Page.navigate", {"url": f"http://127.0.0.1:{cls.px.server_address[1]}/umcares/p/demo/"}, session=cls.s)
        end = time.time() + 20
        while time.time() < end and not cls.ev("document.querySelectorAll('#montage input').length"):
            time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.ch.close()
        cls.srv.shutdown()
        cls.px.shutdown()

    @classmethod
    def ev(cls, js):
        return cls.ch.send("Runtime.evaluate", {"expression": js, "returnByValue": True, "awaitPromise": True}, session=cls.s)["result"].get("value")

    def type_into(self, selector, text):
        self.ev(f"(()=>{{const e={selector}; e.scrollIntoView(); e.focus();}})()")
        for c in text:
            self.ch.send("Input.dispatchKeyEvent", {"type": "keyDown", "text": c, "key": c, "unmodifiedText": c}, session=self.s)
            self.ch.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": c}, session=self.s)

    BPM = "document.querySelector('#montage input[placeholder^=BPM]')"
    TITLE = "document.querySelector('#montage input[type=text]')"

    def test_the_title_card_box_takes_text(self):
        self.type_into(self.TITLE, "Hari Terbuka")
        self.assertEqual(self.ev(f"{self.TITLE}.value"), "Hari Terbuka")

    def test_the_bpm_box_keeps_only_digits_and_never_gets_stuck_after_a_song_name(self):
        self.ev(f"{self.BPM}.value = ''")
        self.type_into(self.BPM, "Great Expectation")
        self.assertEqual(self.ev(f"{self.BPM}.value"), "", "letters never stay in the box")
        self.type_into(self.BPM, "75")
        self.assertEqual(self.ev(f"{self.BPM}.value"), "75", "and digits typed after letters are accepted (a number box would refuse them after an 'e')")
        self.type_into(self.BPM, ".5x")
        self.assertEqual(self.ev(f"{self.BPM}.value"), "75.5")
        text = self.ev("document.querySelector('#montage').innerText")
        self.assertIn("numbers only", text)
        self.assertIn("pick a song from this list", text)

    def test_the_music_control_is_a_pick_list_not_a_text_box_and_there_is_an_add_a_song_button(self):
        kinds = self.ev("Array.from(document.querySelectorAll('#montage select')).map(s => s.options[0].text.slice(0, 8)).join('|')")
        self.assertIn("No music", kinds)
        self.assertTrue(self.ev("Array.from(document.querySelectorAll('button')).some(b => b.textContent === 'Add a song')"))
        self.assertEqual(self.ev("document.getElementById('f_song').accept.includes('audio/*')"), True)


    def test_cut_on_the_beat_is_greyed_out_with_a_reason_until_a_song_is_picked_then_it_can_be_ticked(self):
        box = "Array.from(document.querySelectorAll('#montage input[type=checkbox]'))[1]"
        self.assertTrue(self.ev(f"{box}.disabled"), "nothing to cut to without a song")
        self.assertIn("pick a song in the list above first", self.ev("document.querySelector('#montage').innerText"))
        self.ev("""(() => { const s = Array.from(document.querySelectorAll('#montage select')).find(x => x.options[0].text.startsWith('No music'));
                  s.value = 'song.m4a'; s.dispatchEvent(new Event('change')); })()""")
        self.assertFalse(self.ev(f"{box}.disabled"), "a song is picked: the box can be ticked")
        self.assertNotIn("pick a song in the list above first", self.ev("document.querySelector('#montage').innerText"))
        self.ev(f"{box}.click()")
        self.assertTrue(self.ev(f"{box}.checked"), "a real click ticks it")
        self.ev("""(() => { const s = Array.from(document.querySelectorAll('#montage select')).find(x => x.options[0].text.startsWith('No music'));
                  s.value = ''; s.dispatchEvent(new Event('change')); })()""")
        self.assertTrue(self.ev(f"{box}.disabled"), "and it is greyed out again")
        self.assertFalse(self.ev(f"{box}.checked"), "removing the song clears the tick")

    def test_tapping_along_fills_the_bpm_box(self):
        self.ev(f"{self.BPM}.value = ''")
        tap = "Array.from(document.querySelectorAll('#montage button')).find(b => b.textContent.startsWith('Tap along'))"
        for _ in range(6):                                                  # six taps half a second apart = 120 BPM
            self.ev(f"{tap}.click()")
            time.sleep(0.5)
        bpm = float(self.ev(f"{self.BPM}.value"))
        self.assertAlmostEqual(bpm, 120, delta=8)
        self.assertIn("BPM", self.ev("document.querySelector('#montage').innerText"))


if __name__ == "__main__":
    unittest.main()
