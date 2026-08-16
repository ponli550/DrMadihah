"""A worked example recipe — the shape an AI should produce.

Modelled on the UM Cares video that this pipeline actually shipped, trimmed to
three scenes. The point is the SHAPE: declare intent, never arithmetic.
Durations left out are measured and filled in by `recipe.resolve`.
"""

EXAMPLE_RECIPE = {
    "meta": {
        "title": "Amanah di Dunia Digital",
        "subtitle": "Lindungi Diri dan Keluarga daripada Scam Siber",
        "grant": "RU2025-T323A",
        "language": "ms-MY",
        "fps": 50,
        "resolution": "1920x1080",
        # every scene gets this much air after its narration ends
        "scene_pad": 0.6,
        # narration starts this long after the scene does
        "narration_lead": 0.5,
    },

    "voice": {
        "provider": "azure",
        "name": "ms-MY-OsmanNeural",
        "pitch": "0%",
        "rate": "0%",
        "fillers": False,
        # spoken with English phonetics inside Malay narration
        "english_terms": ["scam cyber", "online", "link", "internet"],
        # spelled out letter by letter rather than read as a word
        "acronyms": ["UM Cares", "PPR", "ICYM"],
    },

    "style": {
        "surface": "#0d2847",
        "ink": "#ffffff",
        "muted": "#a9b6c9",
        "accent": "#ffd166",
        "font": "Inter",
    },

    "cards": {
        "open": {
            "type": "logo",
            "eyebrow": "PROGRAM KOMUNITI UM CARES",
            "title": "Amanah di Dunia Digital",
            "subtitle": "Lindungi Diri dan Keluarga daripada Scam Siber",
            "footnote": "No. Geran RU2025-T323A",
            "logos": ["logo_um_1.png", "logo_mohe.png", "logo_ayg.png"],
            "duration": 11,
        },
        "impak": {
            "type": "stats",
            "eyebrow": "IMPAK PROGRAM",
            "tiles": [
                {"value": "84%", "label": "akan lebih berhati-hati menggunakan internet"},
                {"value": "88%", "label": "tahu tidak boleh tekan pautan pelik"},
            ],
            "footnote": "Perbandingan sebelum dan selepas bengkel",
        },
    },

    "scenes": [
        {
            "id": "s1_pembukaan",
            "narration": "Amanah di Dunia Digital. "
                         "Lindungi diri dan keluarga daripada scam siber.",
            "visuals": [
                {"card": "open"},
                # no duration: resolve() stretches this to cover the narration
                {"clip": "C0006.mp4"},
            ],
        },
        {
            "id": "s2_konteks",
            "narration": "Perkembangan teknologi membawa banyak manfaat. "
                         "Tetapi, ia turut membawa risiko. Scam siber semakin "
                         "meruncing, menjadikan golongan muda sasaran utama.",
            "emphasis": "menjadikan golongan muda sasaran utama",
            "visuals": [
                {"clip": "C0011.mp4"},
                {"kenburns": {
                    "id": "kb_risiko",
                    "photos": ["DSC01218.JPG", "DSC01220.JPG", "DSC01296.JPG"],
                }, "duration": 12.8},
            ],
        },
        {
            "id": "s5_impak",
            "narration": "Hasil tinjauan menunjukkan impak yang positif. "
                         "Sebanyak 84 peratus berkata mereka akan lebih "
                         "berhati-hati menggunakan internet.",
            "visuals": [
                {"card": "impak", "duration": 21},
            ],
        },
    ],

    "music": {
        "file": "Steps_Toward_Common_Ground.mp3",
        # skip a soft intro so music-only stretches are not inaudible
        "start": 25,
        # [until_seconds, dB] — MUST extend past the end of the last narration
        # line, not just to the scene boundary
        "ducking": [
            [11, -3],
            [161, -20],
            [185, -25],
            [228, -20],
            [9999, -3],
        ],
    },

    "subtitles": {
        "generate": True,
        "language": "msa",
        "soft": True,
    },

    "output": {
        "master": "exports/master.mxf",
        "delivery": "exports/UMCares_RU2025-T323A.mp4",
        "preset": "AVC-Intra Class100 1080 50p",
        "target_lufs": -16,
    },
}
