"""Choosing which videos get judged, on the real page in headless Chrome."""
import os
import subprocess
import time
import unittest

from tests import browser

NEED = unittest.skipUnless(os.path.exists(browser.CHROME), "needs Google Chrome")


def real_video(root, name):
    d = root / "exports"
    d.mkdir(exist_ok=True)
    subprocess.run([browser.FF, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=216x384:rate=30:d=2", "-f", "lavfi", "-i",
                    "sine=frequency=330:duration=2,loudnorm=I=-16:LRA=11:TP=-1.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000",
                    "-t", "2", str(d / name)], check=True)


@NEED
class ChooseAndCheck(unittest.TestCase):
    def setUp(self):
        self.srv, self.home, self.root = browser.serve()
        self.names = [f"v{i}-reels-20261014-00000{i}.mp4" for i in (1, 2, 3)]
        for n in self.names:
            real_video(self.root, n)
        self.px = browser.proxy(self.srv.server_address[1])
        self.ch = browser.Chrome()
        t = self.ch.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.s = self.ch.send("Target.attachToTarget", {"targetId": t, "flatten": True})["sessionId"]
        self.ch.send("Page.enable", session=self.s)
        self.ch.send("Page.navigate", {"url": f"http://127.0.0.1:{self.px.server_address[1]}/umcares/p/demo/"}, session=self.s)
        end = time.time() + 20
        while time.time() < end and "Finished videos" not in (self.ev("document.querySelector('#montage').innerText") or ""):
            time.sleep(0.3)

    def tearDown(self):
        self.ch.close()
        self.srv.shutdown()
        self.px.shutdown()

    def ev(self, js):
        return self.ch.send("Runtime.evaluate", {"expression": js, "returnByValue": True, "awaitPromise": True}, session=self.s)["result"].get("value")

    def text(self):
        return self.ev("document.querySelector('#montage').innerText")

    def button(self, label):
        return f"Array.from(document.querySelectorAll('#montage button')).filter(b => b.textContent.startsWith({label!r}))"

    def tick(self, name, on=True):
        self.ev(f"""(() => {{ const row = Array.from(document.querySelectorAll('#montage input[type=checkbox][title^="Choose"]')).find(i => i.parentElement.textContent.includes({name!r}));
                  if (row.checked !== {str(on).lower()}) row.click(); }})()""")

    # ---------------------------------------------------------------- the finished videos as cards
    def test_each_finished_video_is_one_card_with_its_picture_its_checks_its_download_and_a_quiet_delete(self):
        cards = self.ev("Array.from(document.querySelectorAll('#montage .vcard')).map(c => ({name: c.querySelector('.vtitle a').textContent, "
                        "poster: !!c.querySelector('img.vposter'), checkbox: !!c.querySelector('input[type=checkbox]'), dl: !!c.querySelector('.dl'), "
                        "delete: Array.from(c.querySelectorAll('button')).filter(b => b.textContent === 'Delete').map(b => b.className), "
                        "state: c.innerText.includes('not checked')}))")
        self.assertEqual(sorted(c["name"] for c in cards), sorted(self.names), "one card per video")
        for c in cards:
            self.assertTrue(c["poster"] and c["checkbox"] and c["dl"] and c["state"], c)
            self.assertEqual(c["delete"], ["danger"], "Delete is one quiet red-text button, not one of a row of equal ones")
        self.assertEqual(self.ev("document.querySelectorAll('#montage .vcard .vtitle input[type=checkbox]').length"), 3)

    def test_text_buttons_are_as_wide_as_their_words(self):
        """Phase 1 gave every .mv button a 44 px width, which squeezed 'Select all' and 'Check again' onto two lines."""
        for label in ("Select all", "Check"):
            wrapped = self.ev(f"{self.button(label)}.filter(b => b.className.includes('mv')).map(b => b.scrollWidth > b.clientWidth + 1 || b.getBoundingClientRect().height > 60)")
            self.assertTrue(wrapped and not any(wrapped), f"{label}: {wrapped}")

    def wait_for(self, needle, seconds=40):
        end = time.time() + seconds
        while time.time() < end:
            if needle in self.text():
                return
            time.sleep(0.3)
        self.fail(f"never saw {needle!r}: {self.text()[:600]}")

    def test_nothing_is_checked_until_chosen_and_only_the_chosen_videos_are(self):
        self.assertEqual(self.text().count("not checked"), 3, "every video starts unchecked")
        self.assertFalse(list((self.root / "exports").glob("*.judge.json")), "opening the page judged nothing")
        self.assertTrue(self.ev(f"{self.button('Check selected')}[0].disabled"), "nothing ticked: nothing to check")
        self.tick(self.names[0])
        self.tick(self.names[2])
        self.assertIn("Check selected (2)", self.text())
        self.ev(f"{self.button('Check selected')}[0].click(); true")
        self.wait_for("Checked 2 videos")
        text = self.text()
        self.assertEqual(text.count("not checked"), 1, "the video that was not ticked is still unchecked")
        self.assertEqual(sorted(p.name for p in (self.root / "exports").glob("*.judge.json")), sorted(n.replace(".mp4", ".judge.json") for n in (self.names[0], self.names[2])))
        self.assertTrue("checks passed" in text or "warning" in text or "problem" in text, "a verdict badge is shown")

    def test_tapping_a_result_shows_why_and_one_video_can_be_checked_on_its_own(self):
        self.ev(f"{self.button('Check')}.find(b => b.textContent === 'Check').click(); true")             # the first row's own button
        self.wait_for("Checked 1 video")
        badge = f"Array.from(document.querySelectorAll('#montage button')).find(b => /checks passed|warning|problem/.test(b.textContent))"
        self.assertTrue(self.ev(f"!!{badge}"))
        self.ev(f"{badge}.click(); true")
        self.wait_for("checks fine")
        self.assertIn("check", self.text())
        self.ev(f"{badge}.click(); true")                                                     # tapping again hides it
        time.sleep(0.5)
        self.assertNotIn("checks fine", self.text())
        self.assertIn("Check again", self.text(), "a checked video offers to be checked again")

    def test_select_all_and_none(self):
        self.ev(f"{self.button('Select all')}[0].click(); true")
        self.assertIn("Check selected (3)", self.text())
        self.ev(f"{self.button('Select none')}[0].click(); true")
        self.assertNotIn("Check selected (", self.text())


if __name__ == "__main__":
    unittest.main()
