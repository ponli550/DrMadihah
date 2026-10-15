"""The in-browser resumable download, in real headless Chrome, across a link that drops connections mid-body."""
import hashlib
import json
import os
import random
import time
import unittest

from tests import browser

NEED = unittest.skipUnless(os.path.exists(browser.CHROME), "needs Google Chrome")
CHUNK = 65536                                   # the page's own downloader uses 128 KB; the tests build one with a smaller piece


def make_export(root, name, size, quality="final", seed=7):
    d = root / "exports"
    d.mkdir(exist_ok=True)
    data = random.Random(seed).randbytes(size)
    (d / name).write_bytes(data)
    (d / name).with_suffix(".json").write_text(json.dumps({"plan": {"template": "punchy", "format": "reels", "title": "t", "quality": quality},
                                                          "result": {"duration": 3.0, "audio": False}}))
    return hashlib.sha256(data).hexdigest()


@NEED
class Download(unittest.TestCase):
    def setUp(self):
        self.srv, self.home, self.root = browser.serve()
        self.px = browser.proxy(self.srv.server_address[1])
        self.ch = browser.Chrome()
        t = self.ch.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.s = self.ch.send("Target.attachToTarget", {"targetId": t, "flatten": True})["sessionId"]
        self.ch.send("Page.enable", session=self.s)
        self.ch.send("Page.setDownloadBehavior", {"behavior": "deny"}, session=self.s) if False else None
        self.load()

    def tearDown(self):
        self.ch.close()
        self.srv.shutdown()
        self.px.shutdown()

    def load(self):
        self.ch.send("Page.navigate", {"url": f"http://127.0.0.1:{self.px.server_address[1]}/umcares/p/demo/"}, session=self.s)
        end = time.time() + 20
        while time.time() < end:
            try:
                if self.ev("typeof createDownloader === 'function' && document.querySelectorAll('#montage input').length > 0"):
                    return
            except Exception:
                pass
            time.sleep(0.3)
        self.fail("the page did not load")

    def ev(self, js, timeout=60):
        return self.ch.send("Runtime.evaluate", {"expression": js, "returnByValue": True, "awaitPromise": True}, session=self.s, timeout=timeout)["result"].get("value")

    def downloader(self, **kw):
        opts = json.dumps({"base": "/umcares/p/demo", "chunk": CHUNK, "timeout": 4000, "maxWait": 200, **kw})
        self.ev(f"window.T = createDownloader({opts}); true")

    def wait(self, name, state, seconds=40):
        end = time.time() + seconds
        got = None
        while time.time() < end:
            got = self.ev(f"T.status({json.dumps(name)}).state")
            if got == state:
                return
            time.sleep(0.2)
        self.fail(f"{name} is {got!r}, wanted {state!r}: {self.ev(f'T.status({json.dumps(name)}).note')}")

    def stored_sha(self, name):
        return self.ev(f"""(async () => {{ const b = await T.blob({json.dumps(name)}); if (!b) return null;
            const d = await crypto.subtle.digest('SHA-256', await b.arrayBuffer());
            return Array.from(new Uint8Array(d)).map(x => x.toString(16).padStart(2, '0')).join(''); }})()""")

    # ---------------------------------------------------------------- the download itself
    def test_a_download_over_a_link_that_keeps_dropping_still_arrives_whole(self):
        name = "demo-reels-20261014-000001.mp4"
        want = make_export(self.root, name, 9 * CHUNK + 1234)
        self.px.opts["drop_every"] = 3
        self.downloader()
        self.ev(f"T.start({json.dumps(name)}, {9 * CHUNK + 1234})")
        self.wait(name, "done")
        self.assertEqual(self.stored_sha(name), want, "byte-for-byte the file on the server")
        pieces = 10
        self.assertGreater(len(self.px.log), pieces, "some requests were dropped and made again")
        self.assertEqual(sorted(set(self.px.log)), [i * CHUNK for i in range(pieces)], "every piece, and only real pieces, was asked for")

    def test_after_a_reload_it_carries_on_from_the_pieces_already_stored(self):
        name = "demo-reels-20261014-000002.mp4"
        size = 12 * CHUNK
        want = make_export(self.root, name, size)
        self.px.opts["delay"] = 0.1
        self.downloader()
        self.ev(f"T.start({json.dumps(name)}, {size})")
        end = time.time() + 20
        while time.time() < end and self.ev(f"T.status({json.dumps(name)}).got") < 4 * CHUNK:
            time.sleep(0.02)
        self.ev(f"T.pause({json.dumps(name)})")
        got_before = self.ev(f"T.status({json.dumps(name)}).got")
        self.assertGreaterEqual(got_before, 4 * CHUNK)
        self.assertLess(got_before, size, "paused part-way")
        self.px.log.clear()
        self.px.opts["delay"] = 0
        self.load()                                                          # the page is closed and opened again
        self.downloader()
        self.ev(f"T.watch([{{name: {json.dumps(name)}, size: {size}, quality: 'final'}}])")
        self.assertEqual(self.ev(f"T.status({json.dumps(name)}).state"), "paused", "a paused download stays paused until resumed")
        self.ev(f"T.start({json.dumps(name)}, {size})")
        self.wait(name, "done")
        self.assertEqual(self.stored_sha(name), want)
        self.assertEqual(len(set(self.px.log)), 12 - got_before // CHUNK, "exactly the pieces that were missing were fetched, none already stored")

    def test_pieces_come_in_parallel_so_a_link_that_is_slow_per_request_is_not_slow_overall(self):
        size = 12 * CHUNK
        self.px.opts["delay"] = 0.3                                          # every request costs 0.3 s, whatever its size
        took = {}
        for label, kw in (("serial", {"parallel": 1}), ("parallel", {})):
            name = f"demo-reels-20261014-0000{'1' if label == 'serial' else '2'}0.mp4"
            want = make_export(self.root, name, size)
            self.downloader(**kw)
            t = time.time()
            self.ev(f"T.start({json.dumps(name)}, {size})")
            self.wait(name, "done", seconds=60)
            took[label] = time.time() - t
            self.assertEqual(self.stored_sha(name), want, f"{label}: byte-for-byte")
        self.assertGreater(took["serial"], 12 * 0.3 * 0.9, "the control really is one request at a time")
        self.assertLess(took["parallel"], 0.5 * took["serial"], f"4 at a time is at least twice as fast: {took}")

    def test_a_resume_with_gaps_fetches_only_the_missing_pieces(self):
        """Pieces arrive out of order now, so what is stored need not be a prefix: pieces 3 and 4 are there, 0-2 and 5-9 are not."""
        name = "demo-reels-20261014-000099.mp4"
        size = 10 * CHUNK
        want = make_export(self.root, name, size)
        self.downloader()
        key = f"/umcares/p/demo|{name}"
        self.ev(f"""(async () => {{
          const db = await new Promise((res, rej) => {{ const r = indexedDB.open('umcares-dl', 1);
            r.onupgradeneeded = () => {{ r.result.createObjectStore('meta'); r.result.createObjectStore('chunks'); }}; r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); }});
          const put = (store, k, v) => new Promise(res => {{ const t = db.transaction(store, 'readwrite'); t.objectStore(store).put(v, k); t.oncomplete = res; }});
          for (const i of [3, 4]) {{
            const r = await fetch('/umcares/p/demo/exports/{name}', {{headers: {{Range: 'bytes=' + i * {CHUNK} + '-' + ((i + 1) * {CHUNK} - 1)}}}});
            await put('chunks', {json.dumps(key)} + '#' + i, await r.arrayBuffer());
          }}
          await put('meta', {json.dumps(key)}, {{key: {json.dumps(key)}, base: '/umcares/p/demo', name: {json.dumps(name)}, size: {size}, next: 0, state: 'paused', startedAt: 1}});
          return true; }})()""")
        self.px.log.clear()
        self.ev(f"T.start({json.dumps(name)}, {size})")
        self.wait(name, "done")
        self.assertEqual(self.stored_sha(name), want)
        self.assertEqual(sorted(set(self.px.log)), [i * CHUNK for i in range(10) if i not in (3, 4)], "everything missing, nothing already stored")

    def test_a_piece_that_arrives_damaged_is_caught_by_the_hash_and_the_file_is_fetched_again(self):
        name = "demo-reels-20261014-000003.mp4"
        size = 6 * CHUNK
        want = make_export(self.root, name, size)
        self.px.opts["corrupt_at"] = 2
        self.downloader()
        self.ev(f"T.start({json.dumps(name)}, {size})")
        self.wait(name, "done", seconds=60)
        self.assertEqual(self.stored_sha(name), want, "the damaged copy was thrown away and a good one fetched")
        self.assertGreaterEqual(len(self.px.log), 12, "the whole file was fetched twice")

    def test_a_video_deleted_on_the_server_is_dropped_with_a_clear_message_and_its_stored_pieces_freed(self):
        name = "demo-reels-20261014-000004.mp4"
        size = 40 * CHUNK
        make_export(self.root, name, size)
        self.px.opts["delay"] = 0.4                                         # a slow link, so the video can be deleted part-way
        self.downloader()
        self.ev(f"T.start({json.dumps(name)}, {size})")
        end = time.time() + 20
        while time.time() < end and self.ev(f"T.status({json.dumps(name)}).got") < 2 * CHUNK:
            time.sleep(0.02)
        os.unlink(self.root / "exports" / name)
        self.wait(name, "failed")
        self.assertIn("no longer on the server", self.ev(f"T.status({json.dumps(name)}).note"))
        left = self.ev("""new Promise(res => { const r = indexedDB.open('umcares-dl', 1); r.onsuccess = () => {
            const c = r.result.transaction('chunks').objectStore('chunks').count(); c.onsuccess = () => res(c.result); }; })""")
        self.assertEqual(left, 0, "no stored pieces are left behind")

    def test_a_file_that_changed_size_on_the_server_starts_over_instead_of_mixing_two_files(self):
        name = "demo-reels-20261014-000005.mp4"
        make_export(self.root, name, 6 * CHUNK, seed=1)
        self.px.opts["delay"] = 0.1
        self.downloader()
        self.ev(f"T.start({json.dumps(name)}, {6 * CHUNK})")
        end = time.time() + 20
        while time.time() < end and self.ev(f"T.status({json.dumps(name)}).got") < 2 * CHUNK:
            time.sleep(0.02)
        self.ev(f"T.pause({json.dumps(name)})")
        want = make_export(self.root, name, 7 * CHUNK + 5, seed=2)             # the video was replaced by a longer one
        self.ev(f"T.start({json.dumps(name)}, {7 * CHUNK + 5})")
        self.wait(name, "done")
        self.assertEqual(self.stored_sha(name), want, "the new file, whole: not a mix of the old pieces and the new ones")

    def test_even_if_the_page_still_believes_the_old_size_the_servers_own_answer_catches_a_replaced_file(self):
        name = "demo-reels-20261014-000007.mp4"
        make_export(self.root, name, 6 * CHUNK, seed=11)
        self.px.opts["delay"] = 0.1
        self.downloader()
        self.ev(f"T.start({json.dumps(name)}, {6 * CHUNK})")
        end = time.time() + 20
        while time.time() < end and self.ev(f"T.status({json.dumps(name)}).got") < 2 * CHUNK:
            time.sleep(0.02)
        self.ev(f"T.pause({json.dumps(name)})")
        want = make_export(self.root, name, 8 * CHUNK + 7, seed=12)
        self.px.opts["delay"] = 0
        self.ev(f"T.start({json.dumps(name)}, {6 * CHUNK})")                  # a stale list: the page still passes the OLD size
        self.wait(name, "done")
        self.assertEqual(self.stored_sha(name), want, "the Content-Range total in the server's reply exposed the change")

    # ---------------------------------------------------------------- saving, and the page
    def test_save_hands_the_browser_the_whole_file_under_its_own_name(self):
        name = "demo-reels-20261014-000006.mp4"
        size = 5 * CHUNK + 99
        want = make_export(self.root, name, size)
        self.downloader()
        self.ev("""window.__saved = []; HTMLAnchorElement.prototype.click = function () { window.__saved.push({download: this.download, href: this.href}); }; true""")
        self.ev(f"T.start({json.dumps(name)}, {size})")
        self.wait(name, "done")
        self.assertTrue(self.ev(f"T.save({json.dumps(name)})"))
        saved = self.ev("window.__saved")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["download"], name)
        self.assertTrue(saved[0]["href"].startswith("blob:"))
        self.assertTrue(self.ev(f"T.status({json.dumps(name)}).saved"))
        self.assertEqual(self.stored_sha(name), want, "the stored copy is kept until it is removed")
        self.ev(f"T.discard({json.dumps(name)})")
        self.assertIsNone(self.stored_sha(name), "Remove copy frees it")

    def test_the_page_shows_progress_then_a_save_button_and_downloads_a_new_final_video_by_itself_but_not_a_draft(self):
        # the page's own downloader (128 KB pieces) and the real card: a new final video appears while the page is open
        make_export(self.root, "old-reels-20261014-000001.mp4", 200000, seed=3)
        self.load()                                                          # opened with the old video already there
        time.sleep(1.5)
        self.assertEqual(self.ev("dl.status('old-reels-20261014-000001.mp4').state"), "idle", "videos that were there when the page opened are not fetched on their own")
        want = make_export(self.root, "new-reels-20261014-000002.mp4", 300000, seed=4)
        make_export(self.root, "new-reels-draft-20261014-000003.mp4", 100000, quality="draft", seed=5)
        self.ev("mont.refresh(); true")
        end = time.time() + 30
        while time.time() < end and self.ev("dl.status('new-reels-20261014-000002.mp4').state") != "done":
            time.sleep(0.3)
        self.assertEqual(self.ev("dl.status('new-reels-20261014-000002.mp4').state"), "done")
        self.assertEqual(self.ev("dl.status('new-reels-draft-20261014-000003.mp4').state"), "idle", "a draft is not downloaded automatically")
        text = self.ev("document.querySelector('#montage').innerText")
        self.assertIn("on this phone", text)
        self.assertIn("Save to phone", text)
        self.assertIn("Download to this phone", text, "the older video and the draft offer a manual button")
        self.ev("window.T = dl; true")                                      # the page's own downloader, for the byte check
        self.assertEqual(self.stored_sha("new-reels-20261014-000002.mp4"), want)

    def test_turning_the_automatic_download_off_stops_it(self):
        make_export(self.root, "a-reels-20261014-000001.mp4", 100000, seed=6)
        self.ev("mont.refresh(); true")
        time.sleep(1.2)
        self.ev("dl.setAuto(false); true")
        make_export(self.root, "b-reels-20261014-000002.mp4", 100000, seed=8)
        self.ev("mont.refresh(); true")
        time.sleep(2.5)
        self.assertEqual(self.ev("dl.status('b-reels-20261014-000002.mp4').state"), "idle")
        self.ev("localStorage.removeItem('umcares-auto-download'); true")

    def test_a_video_deleted_on_the_server_loses_its_phone_copy_when_the_page_next_looks(self):
        name = "gone-reels-20261014-000001.mp4"
        make_export(self.root, name, 90000, seed=9)
        self.downloader()
        self.ev(f"T.start({json.dumps(name)}, 90000)")
        self.wait(name, "done")
        os.unlink(self.root / "exports" / name)
        self.ev("T.watch([]); true")
        time.sleep(1.0)
        self.assertIsNone(self.stored_sha(name), "the copy of a deleted video is removed")


if __name__ == "__main__":
    unittest.main()
