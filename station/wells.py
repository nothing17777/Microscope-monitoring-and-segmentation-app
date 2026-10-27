"""Work out which plate well an image came from.

LumaViewPro's layout is not fixed, so nothing here is hardcoded to one naming
scheme: any path component or filename prefix that looks like a well label
(row letter + 1..12) is taken, preferring the deepest match, which is the one
closest to the file. Returns None when the path says nothing — an unknown
well is reported as unknown rather than guessed into A01.
"""
import re
from pathlib import Path

WELL_RE = re.compile(r"^([A-Ha-h])(0?[1-9]|1[0-2])$")


def normalize(label: str) -> str | None:
    m = WELL_RE.match(label)
    if not m:
        return None
    return f"{m.group(1).upper()}{int(m.group(2)):02d}"


def well_of(path: str, input_dir: Path) -> str | None:
    try:
        rel = Path(path).relative_to(input_dir)
    except ValueError:
        rel = Path(path)
    parts = list(rel.parts)
    # Deepest-first: a file named A01_... inside a folder named B02 belongs to
    # whatever the filename says, since that is what the writer labelled it.
    candidates = [p.split("_")[0] for p in [parts[-1]]] + list(reversed(parts[:-1]))
    for c in candidates:
        w = normalize(c)
        if w:
            return w
    return None


def grid(rows: int, cols: int) -> list[str]:
    return [f"{chr(ord('A') + r)}{c + 1:02d}" for r in range(rows) for c in range(cols)]
