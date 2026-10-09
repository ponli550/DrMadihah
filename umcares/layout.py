"""The project folder tree: one definition, shared by `init` and by the web's project creation.

Stdlib only and no package imports, so it can run on a machine that has Python and nothing else.
"""
# (path, purpose) — purpose doubles as the README line written into the tree
TREE = [
    ("assets",              "all source and generated media"),
    ("assets/footage",      "camera video, split by role"),
    ("assets/footage/A",    "A-roll: speakers, interviews, primary action"),
    ("assets/footage/B",    "B-roll: cutaways, atmosphere, filler"),
    ("assets/photos",       "source stills"),
    ("assets/edit_ready",   "H.264 transcodes — the ONLY folder Premiere imports from"),
    ("assets/logos",        "brand marks (source, committed)"),
    ("assets/music",        "licensed music beds"),
    ("assets/vo",           "voiceover renders"),
    ("assets/cards",        "generated title / statistic / logo cards"),
    ("presets",             "copies of the .epr and .sqpreset actually used"),
    ("subtitles",           "SRT files"),
    ("exports",             "masters and delivery files"),
    ("project",             "the .prproj"),
]
