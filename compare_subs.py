import json
from pathlib import Path
from umcares.burn import parse_srt

cues = parse_srt(Path(".umcares/subtitles.srt"))
with open(".umcares/check_v13_audio.json") as f:
    whisper = json.load(f)
segs = whisper.get("segments", [])

def norm(t):
    return t.lower().replace(",", "").replace(".", "").replace("-", " ").strip()

print(f"{'#':>3} {'start':>9} {'end':>9} | subtitle")
print("-" * 80)
for i, (a, b, text) in enumerate(cues, 1):
    # collect whisper text overlapping the cue
    overlap = []
    for s in segs:
        sa, sb = s["start"], s["end"]
        if sb <= a or sa >= b:
            continue
        overlap.append(s["text"].strip())
    wtext = " ".join(overlap)
    ok = norm(text) in norm(wtext) or norm(wtext) in norm(text)
    flag = "OK" if ok else "CHECK"
    print(f"{i:3d} {a:9.2f} {b:9.2f} | {flag}")
    print(f"    SUB: {text}")
    print(f"    WSP: {wtext}")
    print()
