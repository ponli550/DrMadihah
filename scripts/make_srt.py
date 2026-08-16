#!/usr/bin/env python3
"""Regenerate video_subtitles.srt from the ACTUAL edit.

The original SRT was written against the abandoned 3:45 storyboard. This builds
one from the real narration files and their real positions on the timeline, so
the captions actually line up.

Sentence timings come from silence detection inside each scene's WAV, which
tracks the delivered read far better than dividing by character count.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VO = ROOT / "video" / "voiceover" / "final"
OUT = ROOT / "video" / "video_subtitles.srt"

# Scene narration placement on the timeline (seconds), matching expr_build.js.
PLACEMENT = [
    ("s1_pembukaan", 12.0),
    ("s2_konteks", 23.5),
    ("s3_pengenalan", 49.5),
    ("s4_aktiviti", 71.5),
    ("s4b_icym", 93.0),
    ("s5_impak", 141.0),
    ("s7_statistik", 185.5),
    ("s8_penutup", 208.0),
]

# Spoken text per scene, as delivered.
TEXT = {
    "s1_pembukaan": [
        "Amanah di Dunia Digital.",
        "Lindungi diri dan keluarga daripada scam cyber.",
    ],
    "s2_konteks": [
        "Perkembangan teknologi membawa banyak manfaat.",
        "Tetapi, ia turut membawa risiko.",
        "Scam cyber, penipuan dalam talian, dan kecurian identiti semakin meruncing,",
        "menjadikan golongan muda sebagai sasaran utama.",
        "Program ini dilahirkan untuk memberi mereka perisai digital yang kukuh.",
    ],
    "s3_pengenalan": [
        "Dengan geran daripada UM Cares, Universiti Malaya menganjurkan",
        "satu program komuniti khas untuk anak-anak penghuni PPR sekitar Lembah Klang.",
        "Seramai 150 orang peserta, berumur 12 hingga 17 tahun,",
        "menyertai program sehari ini.",
    ],
    "s4_aktiviti": [
        "Program dibahagikan kepada beberapa sesi.",
        "Ia bermula dengan perkongsian khas daripada Encik Mohd Firdaus Khairi",
        "dari Kementerian Digital.",
        "Kemudian, peserta menyertai Latihan Dalam Kumpulan,",
        "mengenal pasti ciri-ciri scam, dan menyertai kuiz interaktif yang menyeronokkan.",
    ],
    "s4b_icym": [
        "Sesi diteruskan dengan taklimat peluang pengajian",
        "oleh Encik Fikri Mohd Khay dari International College of Yayasan Melaka.",
        "Peserta didedahkan kepada laluan pendidikan selepas sekolah menengah,",
        "dan menyertai sesi interaktif bersama fasilitator.",
        "Majlis diakhiri dengan penyampaian cenderahati kepada para penceramah,",
        "serta hadiah kepada peserta yang cemerlang dalam kuiz.",
    ],
    "s5_impak": [
        "Hasil tinjauan menunjukkan impak yang positif.",
        "Sebelum ini, majoriti peserta sudah tahu apa itu scam.",
        "Tetapi selepas bengkel, keyakinan mereka meningkat.",
        "Sebanyak 84 peratus berkata mereka akan lebih berhati-hati menggunakan internet,",
        "dan 88 peratus tahu tidak boleh tekan pautan pelik sewenang-wenangnya.",
    ],
    "s7_statistik": [
        "Program ini mendapat sambutan cemerlang.",
        "Kepuasan keseluruhan mencapai rating 4.77 daripada 5.",
        "Sembilan puluh dua peratus peserta bersetuju bengkel ini mudah difahami,",
        "dan 89.3 peratus mendapat ilmu yang berguna.",
        "Lebih bermakna, 72 peratus peserta berkata",
        "mereka akan berkongsi apa yang dipelajari dengan keluarga.",
    ],
    "s8_penutup": [
        "Program ini bukan sekadar bengkel.",
        "Ia adalah langkah pertama untuk membina komuniti yang celik digital,",
        "amanah, dan berdaya tahan terhadap ancaman scam cyber.",
        "Terima kasih kepada UM Cares, para penaja,",
        "dan semua pihak yang menjayakan program ini.",
        "Bersama, kita lindungi generasi muda di dunia digital.",
    ],
}

# Scene 6 uses real participant audio, not narration. Captioned from the script.
TESTIMONI = [
    (162.1, 168.4, "\"Saya belajar banyak tentang scam."),
    (168.6, 174.9, "Sekarang, saya tahu mesti semak dulu sebelum tekan mana-mana link.\""),
    (176.0, 180.4, "\"Anak saya balik rumah dan beritahu saya"),
    (180.6, 184.7, "yang dia kena berhati-hati dengan orang yang minta maklumat peribadi.\""),
]


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(path)],
        capture_output=True, text=True).stdout.strip()
    return float(out)


def speech_spans(path, min_sil=0.16):
    """Return [(start,end)] of speech, derived from silence gaps."""
    proc = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af",
         "silencedetect=noise=-40dB:d=%s" % min_sil, "-f", "null", "-"],
        capture_output=True, text=True)
    log = proc.stderr
    starts = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", log)]
    ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", log)]
    total = duration(path)

    spans = []
    cur = 0.0
    for i, s in enumerate(starts):
        if s > cur + 0.05:
            spans.append((cur, s))
        cur = ends[i] if i < len(ends) else total
    if cur < total - 0.05:
        spans.append((cur, total))
    return spans, total


def merge_to_n(spans, n, total):
    """Collapse speech spans down to n caption slots, longest-gap-first."""
    if not spans:
        step = total / n
        return [(i * step, (i + 1) * step) for i in range(n)]
    while len(spans) > n:
        # merge the pair separated by the smallest gap
        gaps = [(spans[i + 1][0] - spans[i][1], i) for i in range(len(spans) - 1)]
        _, idx = min(gaps)
        spans[idx] = (spans[idx][0], spans[idx + 1][1])
        del spans[idx + 1]
    while len(spans) < n:
        # split the longest span
        lengths = [(e - s, i) for i, (s, e) in enumerate(spans)]
        _, idx = max(lengths)
        s, e = spans[idx]
        mid = (s + e) / 2
        spans[idx] = (s, mid)
        spans.insert(idx + 1, (mid, e))
    return spans


def ts(t):
    if t < 0:
        t = 0
    h = int(t // 3600); t -= h * 3600
    m = int(t // 60); t -= m * 60
    s = int(t)
    ms = int(round((t - s) * 1000))
    if ms == 1000:
        s += 1; ms = 0
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def main():
    cues = []
    for name, offset in PLACEMENT:
        wav = VO / (name + ".wav")
        if not wav.exists():
            print("missing", wav, file=sys.stderr)
            continue
        lines = TEXT[name]
        spans, total = speech_spans(wav)
        spans = merge_to_n(spans, len(lines), total)
        for (s, e), line in zip(spans, lines):
            cues.append((offset + s, offset + e, line))

    cues.extend(TESTIMONI)
    cues.sort(key=lambda c: c[0])

    # nudge apart any overlaps
    for i in range(1, len(cues)):
        if cues[i][0] < cues[i - 1][1]:
            cues[i] = (cues[i - 1][1] + 0.02, max(cues[i][1], cues[i - 1][1] + 1.0), cues[i][2])

    out = []
    for i, (s, e, txt) in enumerate(cues, 1):
        out.append("%d\n%s --> %s\n%s\n" % (i, ts(s), ts(e), txt))
    OUT.write_text("\n".join(out), encoding="utf-8")
    print("wrote %s  (%d cues, last ends %.2fs)" % (OUT, len(cues), cues[-1][1]))


if __name__ == "__main__":
    main()
