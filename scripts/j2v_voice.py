#!/usr/bin/env python3
"""Render Bahasa Malaysia voiceover through json2video -> Azure ms-MY neural voices.

Azure has no age control for ms-MY voices and no mstts:express-as styles, so a
younger read is approximated with prosody (higher pitch, slightly quicker).

  python3 scripts/j2v_voice.py --pitch-test
  python3 scripts/j2v_voice.py --scenes --pitch "+15%" --rate "-4%"
  python3 scripts/j2v_voice.py --status <project_id>
"""
import argparse
import json
import re
import sys

import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"

VOICE = "ms-MY-OsmanNeural"

# Terms to speak with English pronunciation inside Malay narration.
ENGLISH_TERMS = ["scam cyber", "scam siber", "online", "link", "internet"]

# Acronyms that must be spelled out, not read as a word. Applied before
# ENGLISH_TERMS so the <lang> wrapper does not nest inside itself.
ACRONYMS = {
    "UM Cares": '<lang xml:lang="en-US"><say-as interpret-as="characters">UM</say-as> Cares</lang>',
    "PPR": '<say-as interpret-as="characters">PPR</say-as>',
    "ICYM": '<say-as interpret-as="characters">ICYM</say-as>',
    "International College of Yayasan Melaka":
        '<lang xml:lang="en-US">International College of Yayasan Melaka</lang>',
}
# Canonical English spelling used inside the <lang> tag.
ENGLISH_SPELLING = {"scam siber": "scam cyber"}

SCENES = [
    ("s1_pembukaan",
     "Amanah di Dunia Digital. Lindungi diri dan keluarga daripada scam siber."),
    ("s2_konteks",
     "Perkembangan teknologi membawa banyak manfaat. Tetapi, ia turut membawa risiko. "
     "scam siber, penipuan dalam talian, dan kecurian identiti semakin meruncing, "
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
    ("s4b_icym",
     "Sesi diteruskan dengan taklimat peluang pengajian oleh Encik Fikri Mohd Khay "
     "dari International College of Yayasan Melaka. Peserta didedahkan kepada laluan "
     "pendidikan selepas sekolah menengah, dan menyertai sesi interaktif bersama "
     "fasilitator. Majlis diakhiri dengan penyampaian cenderahati kepada para "
     "penceramah, serta hadiah kepada peserta yang cemerlang dalam kuiz."),
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

# Closing phrase per scene that carries the emotional lift.
EMPHASIS = {
    "s1_pembukaan": None,
    "s2_konteks": "perisai digital yang kukuh",
    "s3_pengenalan": None,
    "s4_aktiviti": "menyeronokkan",
    "s4b_icym": "hadiah kepada peserta yang cemerlang dalam kuiz",
    "s4b_icym": "hadiah kepada peserta yang cemerlang dalam kuiz",
    "s5_impak": None,
    "s7_statistik": "berkongsi apa yang dipelajari dengan keluarga",
    "s8_penutup": "Bersama, kita lindungi generasi muda di dunia digital",
}


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def mark_english(text):
    """Wrap English loanwords so Azure says them with English phonetics."""
    out = esc(text)
    for acro, repl in ACRONYMS.items():
        out = out.replace(acro, repl)
    for term in sorted(ENGLISH_TERMS, key=len, reverse=True):
        spelled = ENGLISH_SPELLING.get(term, term)
        out = re.sub(
            re.escape(term),
            '<lang xml:lang="en-US">%s</lang>' % spelled,
            out,
            flags=re.IGNORECASE,
        )
    return out


# Natural Malay hesitation sounds, cycled so the same one never repeats
# back-to-back. Spelled to match how Azure's ms-MY voice renders them.
FILLERS = ["errr", "aaa", "ermm"]


def flow(marked_text, fillers=True):
    """Join sentences so the read carries forward instead of stopping dead.

    Two mechanisms:
      1. A sentence-final '.' becomes ',' -- Azure then uses a continuation
         contour (pitch stays up) rather than a falling terminal contour.
      2. A hesitation sound is dropped at some boundaries, spoken lower and
         slower than the narration so it reads as thinking, not as a word.
    """
    sents = [s.strip() for s in re.split(r'(?<=[.!?])\s+', marked_text) if s.strip()]
    if len(sents) < 2:
        return marked_text

    out = []
    last_i = len(sents) - 1
    fi = 0
    for i, s in enumerate(sents):
        if i == last_i:
            out.append(s)  # only the final sentence gets a real full stop
            break
        # keep the line open
        s = re.sub(r'\.$', ',', s)
        out.append(s)
        # A filler every other boundary; more than that grates.
        if fillers and i % 2 == 0:
            f = FILLERS[fi % len(FILLERS)]
            fi += 1
            # No volume attribute here: Azure rejected the whole SSML document
            # when volume="-3dB" was present. pitch+rate alone is accepted.
            out.append(
                '<break time="90ms"/>'
                '<prosody pitch="-6%%" rate="-25%%">%s</prosody>'
                '<break time="60ms"/>' % f
            )
        else:
            out.append('<break strength="none"/>')
    return " ".join(out)


def build_ssml(text, pitch, rate, emphasis_phrase=None, fillers=True):
    body = flow(mark_english(text), fillers=fillers)
    if emphasis_phrase:
        marked = mark_english(emphasis_phrase)
        if marked in body:
            body = body.replace(
                marked,
                '<emphasis level="strong"><prosody pitch="+6%%" rate="-12%%">%s</prosody></emphasis>' % marked,
            )
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="ms-MY">'
        '<voice name="%s"><prosody pitch="%s" rate="%s">%s</prosody></voice></speak>'
        % (VOICE, pitch, rate, body)
    )


def load_env():
    env = {}
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    if not env.get("JSON2VIDEO_API_KEY"):
        sys.exit("JSON2VIDEO_API_KEY missing in .env")
    return env


def submit(env, movie):
    req = urllib.request.Request(
        env.get("JSON2VIDEO_ENDPOINT") or "https://api.json2video.com/v2/movies",
        data=json.dumps(movie).encode(), method="POST",
        headers={"x-api-key": env["JSON2VIDEO_API_KEY"], "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def status(env, pid):
    url = (env.get("JSON2VIDEO_ENDPOINT") or "https://api.json2video.com/v2/movies") + "?project=" + pid
    req = urllib.request.Request(url, headers={"x-api-key": env["JSON2VIDEO_API_KEY"]})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def voice_scene(ssml, caption):
    return {
        "background-color": "#0d2847",
        "elements": [
            {"type": "voice", "model": "azure", "voice": VOICE, "text": ssml},
            {"type": "text", "text": caption, "position": "custom",
             "x": 160, "y": -40, "width": 1600,
             "settings": {"font-family": "Inter", "font-size": "56px", "color": "#ffffff"}},
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pitch-test", action="store_true")
    ap.add_argument("--flow-test", action="store_true")
    ap.add_argument("--scenes", action="store_true")
    ap.add_argument("--status")
    ap.add_argument("--pitch", default="0%")
    ap.add_argument("--rate", default="0%")
    ap.add_argument("--show-ssml", action="store_true")
    ap.add_argument("--no-fillers", action="store_true")
    ap.add_argument("--one")
    args = ap.parse_args()
    env = load_env()

    if args.status:
        d = status(env, args.status)
        m = d.get("movie", {})
        print("status:", m.get("status"), "duration:", m.get("duration"))
        print("url:", m.get("url"))
        print("quota:", d.get("remaining_quota"))
        return

    if args.pitch_test:
        line = "Lindungi diri dan keluarga daripada scam siber."
        scenes = []
        for p, r in [("+8%", "-2%"), ("+16%", "0%"), ("+24%", "+3%")]:
            scenes.append(voice_scene(build_ssml(line, p, r), "pitch %s  rate %s" % (p, r)))
        if args.show_ssml:
            print(json.dumps(scenes[0], indent=2)[:1200]); return
        out = submit(env, {"resolution": "full-hd", "scenes": scenes})
        print(json.dumps(out))
        return

    if args.flow_test:
        # Multi-sentence sample: A = hard stops, B = flowing with fillers.
        sample = ("Perkembangan teknologi membawa banyak manfaat. Tetapi, ia turut membawa risiko. "
                  "scam siber semakin meruncing, menjadikan golongan muda sebagai sasaran utama.")
        scenes = [
            voice_scene(build_ssml(sample, args.pitch, args.rate, fillers=False), "A: no fillers"),
            voice_scene(build_ssml(sample, args.pitch, args.rate, fillers=True), "B: flow + fillers"),
        ]
        if args.show_ssml:
            print("A:", build_ssml(sample, args.pitch, args.rate, fillers=False)[:700])
            print()
            print("B:", build_ssml(sample, args.pitch, args.rate, fillers=True)[:900])
            return
        out = submit(env, {"resolution": "full-hd", "scenes": scenes})
        print(json.dumps(out))
        return

    if args.scenes:
        scenes = []
        for name, text in SCENES:
            if args.one and name != args.one:
                continue
            ssml = build_ssml(text, args.pitch, args.rate, EMPHASIS.get(name),
                              fillers=not args.no_fillers)
            if args.show_ssml:
                print("---", name); print(ssml[:600]); continue
            scenes.append(voice_scene(ssml, name))
        if args.show_ssml:
            return
        out = submit(env, {"resolution": "full-hd", "scenes": scenes})
        print(json.dumps(out))
        return

    ap.error("give --pitch-test, --flow-test, --scenes or --status")


if __name__ == "__main__":
    main()
