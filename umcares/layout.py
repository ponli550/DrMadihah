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

# What the web app treats as media. One definition, so the library view and the upload check agree.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac"}


def kind_of(name: str) -> str:
    ext = name[name.rfind("."):].lower() if "." in name else ""
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    return "file"
