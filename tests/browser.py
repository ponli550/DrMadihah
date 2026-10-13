"""A real browser for tests: Chrome, headless, driven over its debugging pipe. Standard library only.

The node harnesses run the page's scripts against a DOM shim; they cannot tell what a browser does with a
number box or a select. This can. Tests using it skip when Chrome is not installed.
"""
import json, os, select, subprocess, tempfile, threading, time
from pathlib import Path
from umcares import projects, web, prepare
from tests.test_prepare import put
FF = prepare.find_tool("ffmpeg")

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class Chrome:
    def __init__(self):
        r1, w1 = os.pipe(); r2, w2 = os.pipe()              # we write w1 -> chrome fd3; chrome writes fd4 -> we read r2
        def setup():
            os.dup2(r1, 3, inheritable=True); os.dup2(w2, 4, inheritable=True)
        exe = CHROME
        self.p = subprocess.Popen([exe, "--headless=new", "--remote-debugging-pipe", "--no-sandbox", "--disable-gpu", "--window-size=420,900",
                                   f"--user-data-dir={tempfile.mkdtemp()}", "about:blank"], pass_fds=(3, 4), preexec_fn=setup,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(r1); os.close(w2)
        self.w, self.r, self.n, self.buf, self.events = w1, r2, 0, b"", []
    def send(self, method, params=None, session=None, timeout=30):
        self.n += 1; msg = {"id": self.n, "method": method, "params": params or {}}
        if session: msg["sessionId"] = session
        os.write(self.w, json.dumps(msg).encode() + b"\0")
        end = time.time() + timeout
        while time.time() < end:
            if select.select([self.r], [], [], 0.2)[0]:
                self.buf += os.read(self.r, 1 << 20)
            while b"\0" in self.buf:
                raw, self.buf = self.buf.split(b"\0", 1)
                d = json.loads(raw)
                if d.get("id") == self.n:
                    if "error" in d: raise RuntimeError(d["error"])
                    return d.get("result", {})
                self.events.append(d)
        raise TimeoutError(method)
    def close(self):
        try: self.p.terminate()
        except Exception: pass

def serve():
    home = Path(tempfile.mkdtemp()); root = Path(projects.create_project(home, "Demo", init_git=False)["path"])
    fx = Path(tempfile.mkdtemp())
    for i, n in enumerate(("a.jpg", "b.jpg")):
        subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360", "-frames:v", "1", str(fx / n)], check=True)
        put(root, fx / n, mtime=1_700_000_000_000 + i * 60000)
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=8", "-c:a", "aac", str(fx / "song.m4a")], check=True)
    put(root, fx / "song.m4a", mtime=1_700_000_500_000)
    srv = web.make_server(home, "me@x.com", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, home, root

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def proxy(upstream_port, login="me@x.com"):
    """Strips /umcares like `tailscale serve` does and adds the owner's login header."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _go(self):
            path = self.path[len("/umcares"):] if self.path.startswith("/umcares") else self.path
            n = int(self.headers.get("Content-Length") or 0); body = self.rfile.read(n) if n else None
            h = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")}
            h["Tailscale-User-Login"] = login
            c = http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=60)
            c.request(self.command, path, body=body, headers=h); r = c.getresponse(); data = r.read()
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() not in ("transfer-encoding", "connection", "content-length"): self.send_header(k, v)
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        do_GET = do_POST = do_PUT = do_HEAD = _go
    p = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=p.serve_forever, daemon=True).start()
    return p
