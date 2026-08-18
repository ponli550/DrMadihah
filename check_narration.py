import json
import subprocess
from pathlib import Path
from umcares.burn import parse_srt
import whisper

model = whisper.load_model("small")
vo_dir = Path(".umcares/vo")
srt = parse_srt(Path(".umcares/subtitles.srt"))

recipe = json.loads(Path("recipes/v10.json").read_text(encoding="utf-8"))
scenes = {s["id"]: s for s in recipe.get("scenes", [])}

def norm(t):
    return t.lower().replace(",", "").replace(".", "").replace("-", " ").strip()

print("Narration vs subtitle text check")
print("-" * 80)
for wav in sorted(vo_dir.glob("s*.wav")):
    sid = wav.stem
    scene = scenes.get(sid, {})
    narr = scene.get("narration", "")
    res = model.transcribe(str(wav), language="ms", fp16=False)
    transcript = res["text"].strip()
    # find srt cues for this scene by matching text
    cue_texts = [text for a, b, text in srt if norm(narr)[:20] in norm(text) or norm(text) in norm(narr)]
    combined = " ".join(cue_texts)
    print(f"\n{sid}")
    print(f"RECIPE: {narr}")
    print(f"AUDIO : {transcript}")
    print(f"SUBS  : {combined}")
    if norm(narr) and norm(transcript) and (norm(narr) in norm(transcript) or norm(transcript) in norm(narr)):
        print("STATUS: OK")
    else:
        print("STATUS: REVIEW")
