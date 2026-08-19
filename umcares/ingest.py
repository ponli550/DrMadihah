"""Turn pre-production material into a brief an AI author can work from.

The recipe is the author's medium, but the raw material is a form, a
spreadsheet, and a notebook of notes. This makes the boring parts (CSV
fetching, notebook references) one command and writes:

  .umcares/brief.md   — a storyboard-shaped summary, for the author to read
  .umcares/brief.json — the same data, for the author's tools to consume

Nothing here reaches the remote machine; this is the one command that is
purely local.
"""
from __future__ import annotations

import csv
import io
import json
import urllib.request
from pathlib import Path


def fetch_csv(source: str, timeout: int = 30) -> str:
    """Read CSV text from a local file or an http(s) URL."""
    if source.startswith("http://") or source.startswith("https://"):
        with urllib.request.urlopen(source, timeout=timeout) as r:
            return r.read().decode("utf-8-sig")
    p = Path(source).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"no such file: {p}")
    with open(p, encoding="utf-8-sig", newline="") as f:
        return f.read()


def parse_responses(text: str) -> dict:
    """Parse a Google Forms responses CSV into columns + responses."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return {"columns": [], "responses": []}
    columns = rows[0]
    responses = [dict(zip(columns, r)) for r in rows[1:]
                  if any(c.strip() for c in r)]
    return {"columns": columns, "responses": responses}


def _cell(value, width: int = 60) -> str:
    value = str(value).replace("\n", " ").replace("|", "/").strip()
    return value if len(value) <= width else value[:width - 1] + "…"


def build_brief(parsed: dict, csv_source: str = "", notebook: str = "",
                max_rows: int = 200) -> str:
    """Render the brief markdown the author starts from."""
    columns = parsed["columns"]
    responses = parsed["responses"]
    lines = [
        "# Brief",
        "",
        f"- responses: {len(responses)}",
        f"- columns: {len(columns)}",
    ]
    if csv_source:
        lines.append(f"- source: `{csv_source}`")
    if notebook:
        lines.append(f"- notebook: <{notebook}>")
    lines.append("")
    lines.append("## Columns")
    lines.append("")
    for c in columns:
        lines.append(f"- {c}")
    lines.append("")
    lines.append("## Responses")
    lines.append("")
    if not responses:
        lines.append("_no responses yet_")
        return "\n".join(lines) + "\n"

    header = " | ".join(_cell(c, 24) for c in columns)
    lines.append(f"| {header} |")
    lines.append(f"|{'---|' * len(columns)}")
    for r in responses[:max_rows]:
        lines.append("| " + " | ".join(_cell(r.get(c, ""), 60)
                                       for c in columns) + " |")
    if len(responses) > max_rows:
        lines.append("")
        lines.append(f"_… {len(responses) - max_rows} more responses in "
                     f"brief.json_")
    return "\n".join(lines) + "\n"


def write(parsed: dict, out: Path, csv_source: str = "", notebook: str = "",
          max_rows: int = 200) -> Path:
    """Write brief.md (and brief.json beside it) for the author."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_brief(parsed, csv_source, notebook, max_rows),
                   encoding="utf-8")
    json_out = out.with_name(out.stem + ".json")
    json_out.write_text(
        json.dumps({"source": csv_source, "notebook": notebook,
                    "columns": parsed["columns"],
                    "responses": parsed["responses"]},
                   indent=2, ensure_ascii=False),
        encoding="utf-8")
    return out