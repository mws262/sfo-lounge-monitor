"""SFO security checkpoint wait times.

Replaces the decommissioned TSA MyTSA feed (that endpoint now 302-redirects to
tsa.gov). flysfo.com server-side-renders a checkpoint wait-times table into the
page HTML, so we can parse it with stdlib only -- no JS execution, no key.

Table columns: Checkpoint | General | TSA PreCheck, plus a
"Checkpoint data last updated: ..." line.
"""
from __future__ import annotations

import html
import re
from typing import Any

from . import common

URL = "https://www.flysfo.com/passengers/flight-info/check-in-security"

# Checkpoint -> terminal, so a departing traveler can read their own terminal.
# Harvey Milk Terminal 1 = B, C(-ish); Terminal 2 = D; Terminal 3 = E/F;
# International = A, G. (SFO relabels periodically -- verify if it drifts.)
CHECKPOINT_TERMINAL = {
    "A": "Intl A",
    "B": "T1",
    "C": "T1",
    "D": "T2",
    "E": "T3",
    "F": "T3",
    "G": "Intl G",
}

_TABLE_RE = re.compile(
    r'<table[^>]*class="[^"]*flysfo-checkpoints-table[^"]*"[^>]*>(.*?)</table>',
    re.IGNORECASE | re.DOTALL,
)
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_UPDATED_RE = re.compile(
    r'flysfo-checkpoints-updated[^>]*>(.*?)</', re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")


def _text(cell: str) -> str:
    return html.unescape(_TAG_RE.sub("", cell)).strip()


def _minutes(cell_text: str) -> int | None:
    """'1 mins' -> 1 ; 'Not Available' -> None."""
    m = re.search(r"(\d+)\s*min", cell_text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _terminal_for(name: str) -> str | None:
    m = re.search(r"Checkpoint\s+([A-G])", name, re.IGNORECASE)
    if m:
        return CHECKPOINT_TERMINAL.get(m.group(1).upper())
    return None


def fetch() -> dict[str, Any]:
    """Fetch and parse the checkpoint wait-times table."""
    status, body = common.http_get(URL)
    if status != 200:
        return {"ok": False, "error": f"HTTP {status}", "checkpoints": []}
    text = body.decode("utf-8", errors="replace")

    tbl = _TABLE_RE.search(text)
    if not tbl:
        return {"ok": False, "error": "table not found", "checkpoints": []}

    checkpoints: list[dict] = []
    for row in _ROW_RE.finditer(tbl.group(1)):
        cells = [_text(c) for c in _CELL_RE.findall(row.group(1))]
        if len(cells) < 3 or cells[0].lower() == "checkpoint":
            continue  # header / malformed
        checkpoints.append(
            {
                "name": cells[0],
                "terminal": _terminal_for(cells[0]),
                "general_min": _minutes(cells[1]),
                "precheck_min": _minutes(cells[2]),
            }
        )

    updated_m = _UPDATED_RE.search(text)
    updated = _text(updated_m.group(1)) if updated_m else None

    gen = [c["general_min"] for c in checkpoints if c["general_min"] is not None]
    return {
        "ok": True,
        "checkpoints": checkpoints,
        "updated": updated,
        "max_general_min": max(gen) if gen else None,
        "avg_general_min": round(sum(gen) / len(gen), 1) if gen else None,
    }


def score(reading: dict) -> float | None:
    """0..100 where 100 == worst line. Based on the busiest General wait.

    Anchors: 0 min -> 0, 30 min -> 100. SFO General waits above ~30 min are
    rare and genuinely bad; PreCheck is not scored (it stays short).
    """
    if not reading.get("ok"):
        return None
    worst = reading.get("max_general_min")
    if worst is None:
        return None
    return common.linscale(worst, 0, 30)


def summarize(reading: dict, terminal: str | None = None) -> str:
    if not reading.get("ok"):
        return f"Security: unavailable ({reading.get('error')})"
    cps = reading["checkpoints"]
    if terminal:
        cps = [c for c in cps if c.get("terminal") == terminal] or cps
    worst = reading.get("max_general_min")
    avg = reading.get("avg_general_min")
    busiest = max(
        (c for c in cps if c["general_min"] is not None),
        key=lambda c: c["general_min"],
        default=None,
    )
    busiest_txt = (
        f", busiest {busiest['name']} {busiest['general_min']}m"
        if busiest else ""
    )
    scope = f" [{terminal}]" if terminal else ""
    return (
        f"Security{scope}: avg ~{avg}m, worst ~{worst}m general{busiest_txt}"
    )
