#!/usr/bin/env python3
"""Generate Bahasa Malaysia voiceover with Alibaba Qwen3-TTS.

Reads credentials from .env (gitignored). Never hardcode the key.

  python3 scripts/tts_generate.py --audition          # one line, several voices
  python3 scripts/tts_generate.py --scenes            # full VO, one file per scene
  python3 scripts/tts_generate.py --text "..." --voice Cherry --out out.wav
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
OUTDIR = ROOT / "video" / "voiceover"

# Qwen3-TTS voice roster. f/m noted for picking a narrator.
VOICES = {
    "Cherry": "f", "Jennifer": "f", "Katerina": "f", "Jada": "f",
    "Kiki": "f", "Sunny": "f", "Elias": "m", "Ethan": "m",
    "Ryan": "m", "Marcus": "m", "Roy": "m", "Peter": "m",
    "Rocky": "m", "Eric": "m", "Dylan": "m", "Nofish": "m",
}

# Narration per scene, lifted from video/video_script.md.
SCENES = [
    ("s1_pembukaan",
     "Amanah di Dunia Digital: Lindungi Diri dan Keluarga daripada Scam Siber."),
    ("s2_konteks",
     "Perkembangan teknologi membawa banyak manfaat. Tetapi, ia turut membawa risiko. "
     "Scam siber, penipuan dalam talian, dan kecurian identiti semakin meruncing, "
     "menjadikan golongan muda sebagai sasaran utama. Program ini dilahirkan untuk "
     "memberi mereka perisai digital yang kukuh."),
    ("s3_pengenalan",
     "Dengan geran daripada UM Cares, Universiti Malaya menganjurkan satu program komuniti "
     "khas untuk anak-anak penghuni PPR sekitar Lembah Klang. Seramai 150 orang peserta, "
     "berumur 12 hingga 17 tahun, menyertai program sehari ini."),
    ("s4_aktiviti",
     "Program dibahagikan kepada beberapa sesi. Ia bermula dengan perkongsian khas daripada "
     "Encik Mohd Firdaus Khairi dari Kementerian Digital. Kemudian, peserta menyertai Latihan "
     "Dalam Kumpulan, mengenal pasti ciri-ciri scam, dan menyertai kuiz interaktif yang menyeronokkan."),
    ("s5_impak",
     "Hasil tinjauan menunjukkan impak yang positif. Sebelum ini, majoriti peserta sudah tahu "
     "apa itu scam. Tetapi selepas bengkel, keyakinan mereka meningkat. Sebanyak 84 peratus "
     "berkata mereka akan lebih berhati-hati menggunakan internet, dan 88 peratus tahu tidak "
     "boleh tekan pautan pelik sewenang-wenangnya."),
    ("s7_statistik",
     "Program ini mendapat sambutan cemerlang. Kepuasan keseluruhan mencapai rating 4.77 daripada 5. "
     "Sembilan puluh dua peratus peserta bersetuju bengkel ini mudah difahami, dan 89.3 peratus "
     "mendapat ilmu yang berguna. Lebih bermakna, 72 peratus peserta berkata mereka akan berkongsi "
     "apa yang dipelajari dengan keluarga."),
    ("s8_penutup",
     "Program ini bukan sekadar bengkel. Ia adalah langkah pertama untuk membina komuniti yang "
     "celik digital, amanah, dan berdaya tahan terhadap ancaman scam siber. Terima kasih kepada "
     "UM Cares, para penaja, dan semua pihak yang menjayakan program ini. Bersama, kita lindungi "
     "generasi muda di dunia digital."),
]
# Scene 6 is testimonials -- real participant audio from C0018/C0019, not TTS.


def load_env():
    if not ENV.exists():
        sys.exit("Missing .env -- copy .env.example and fill in QWEN_API_KEY.")
    env = {}
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    if not env.get("QWEN_API_KEY"):
        sys.exit("QWEN_API_KEY is empty in .env")
    return env


def synth(env, text, voice, out_path):
    """One TTS call -> a .wav on disk. Returns duration-ish info."""
    url = env["QWEN_DASHSCOPE_ENDPOINT"].rstrip("/") + \
        "/services/aigc/multimodal-generation/generation"
    body = json.dumps({
        "model": env.get("QWEN_TTS_MODEL") or "qwen3-tts-flash",
        "input": {"text": text, "voice": voice},
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": "Bearer " + env["QWEN_API_KEY"],
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=120) as r:
        payload = json.loads(r.read())

    audio = payload.get("output", {}).get("audio", {})
    link = audio.get("url")
    if not link:
        raise RuntimeError("no audio url in response: " + json.dumps(payload)[:400])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(link, timeout=120) as a, open(out_path, "wb") as f:
        f.write(a.read())
    return out_path.stat().st_size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audition", action="store_true", help="one line across several voices")
    ap.add_argument("--scenes", action="store_true", help="full VO, one file per scene")
    ap.add_argument("--text")
    ap.add_argument("--voice", default="Cherry")
    ap.add_argument("--out")
    args = ap.parse_args()
    env = load_env()

    if args.audition:
        line = SCENES[0][1]
        picks = ["Cherry", "Jennifer", "Katerina", "Ethan", "Ryan", "Elias"]
        for v in picks:
            dest = OUTDIR / "audition" / ("audition_%s_%s.wav" % (v, VOICES.get(v, "?")))
            try:
                n = synth(env, line, v, dest)
                print("OK   %-10s %7d bytes  %s" % (v, n, dest))
            except Exception as e:
                print("FAIL %-10s %s" % (v, str(e)[:160]))
        return

    if args.scenes:
        voice = args.voice
        for name, text in SCENES:
            dest = OUTDIR / ("%s_%s.wav" % (name, voice))
            try:
                n = synth(env, text, voice, dest)
                print("OK   %-16s %8d bytes  %d chars" % (name, n, len(text)))
            except Exception as e:
                print("FAIL %-16s %s" % (name, str(e)[:160]))
        return

    if not args.text:
        ap.error("give --audition, --scenes, or --text")
    dest = Path(args.out) if args.out else (OUTDIR / ("tts_%s.wav" % args.voice))
    print(synth(env, args.text, args.voice, dest), "bytes ->", dest)


if __name__ == "__main__":
    main()
