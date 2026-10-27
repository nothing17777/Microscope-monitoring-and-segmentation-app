"""What LumaViewPro 4 writes, read without ever modifying it.

A protocol run creates one folder under <live_folder>/ProtocolData named for
its start time (YYYYMMDD_HHMMSS) and writes every image flat into it:

    A1_PC_TA1_Z0_0000.tiff   well _ color _ tile _ Z-slice _ scan
    unsaved_protocol.tsv     the protocol: one 'Steps' row per image to take
    protocol_record.tsv      one row per image as it lands

Everything here is best effort. A name that does not parse is not an error:
the well still comes from wells.py, and the other fields stay unknown.
"""
import csv
import re
import time
from datetime import datetime
from pathlib import Path

NAME_RE = re.compile(
    r"^(?P<well>[A-Ha-h]\d{1,2})_(?P<channel>[A-Za-z0-9]+)_(?P<tile>T[A-Z]*\d*)"
    r"_Z(?P<z>\d+)_(?P<scan>\d+)$")
FOLDER_RE = re.compile(r"^(\d{8}_\d{6})")


def parse_name(path: Path) -> dict | None:
    """{'channel', 'tile', 'z', 'scan'} from an LVP file name, or None."""
    m = NAME_RE.match(path.name.split(".")[0])
    if not m:
        return None
    return {"channel": m["channel"], "tile": m["tile"], "z": int(m["z"]),
            "scan": int(m["scan"])}


def folder_started_at(folder: Path) -> float | None:
    """The run's start time, from the folder name LVP gives it."""
    m = FOLDER_RE.match(folder.name)
    if not m:
        return None
    try:
        return time.mktime(datetime.strptime(m[1], "%Y%m%d_%H%M%S").timetuple())
    except ValueError:
        return None


def expected_images(folder: Path) -> int:
    """Images the protocol will take, from the copy LVP saves with the run.

    Counts 'Steps' rows that acquire an image. 0 when there is no protocol
    file or it cannot be read: an unknown total is reported as unknown, and
    the run then never auto-completes rather than completing early.
    """
    for tsv in sorted(folder.glob("*.tsv")):
        if tsv.name == "protocol_record.tsv":
            continue
        try:
            with open(tsv, newline="", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        if not lines or not lines[0].startswith("LumaViewPro Protocol"):
            continue
        try:
            start = lines.index("Steps") + 1
        except ValueError:
            continue
        rows = list(csv.reader(lines[start:], delimiter="\t"))
        if not rows:
            continue
        header, body = rows[0], [r for r in rows[1:] if any(c.strip() for c in r)]
        if "Acquire" in header:
            i = header.index("Acquire")
            return sum(1 for r in body if len(r) > i and r[i].strip().lower() == "image")
        return len(body)
    return 0
