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


if __name__ == "__main__":
    unittest.main()
