#!/usr/bin/env python3
"""Smoke-test the json2video API using the key in .env.

  python3 scripts/j2v_test.py --check          # auth only, renders nothing
  python3 scripts/j2v_test.py --render         # submit a tiny 1-scene movie
  python3 scripts/j2v_test.py --status <id>    # poll a submitted job
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"


def load_env():
    if not ENV.exists():
        sys.exit("Missing .env")
    env = {}
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    if not env.get("JSON2VIDEO_API_KEY"):
        sys.exit("JSON2VIDEO_API_KEY is empty in .env -- add it and re-run.")
    return env


def call(env, method, payload=None, params=""):
    url = env.get("JSON2VIDEO_ENDPOINT") or "https://api.json2video.com/v2/movies"
    if params:
        url += params
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "x-api-key": env["JSON2VIDEO_API_KEY"],
        "content-type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:500]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--status")
    args = ap.parse_args()
    env = load_env()

    if args.status:
        code, body = call(env, "GET", None, "?project=" + args.status)
        print(code, json.dumps(body, indent=2)[:1500])
        return

    if args.render:
        # Minimal 1-scene movie: a solid background + a text line. No external
        # assets, so it isolates auth/render from asset-hosting problems.
        movie = {
            "resolution": "full-hd",
            "scenes": [{
                "background-color": "#1a3d6e",
                "duration": 3,
                "elements": [{
                    "type": "text",
                    "text": "Amanah di Dunia Digital",
                    "start": 0,
                    "duration": 3,
                }],
            }],
        }
        code, body = call(env, "POST", movie)
        print(code, json.dumps(body, indent=2)[:1500])
        return

    # default: --check. Empty scenes list; we only care whether auth passes.
    code, body = call(env, "POST", {"resolution": "full-hd", "scenes": []})
    print("HTTP", code)
    print(json.dumps(body, indent=2)[:800])
    msg = json.dumps(body).lower()
    if "invalid api key" in msg:
        print("\n=> KEY REJECTED")
    else:
        print("\n=> key accepted (any error above is about the payload, not auth)")


if __name__ == "__main__":
    main()
