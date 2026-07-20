"""Road congestion on the drives into SFO (TomTom Flow Segment API).

DORMANT as of 2026-07: unwired from the composite and the dashboard. The
probes and scoring below still work -- re-add "approach" to score.WEIGHTS and
restore the fetch in cli.gather() to bring it back (the key was 401-ing when
it was pulled).


Origin-independent "is the approach jammed" signal, distinct from drive.py
(which measures a personal door-to-SFO time and needs an origin). Three probes
cover the chain every arriving car uses, chosen by live segment-snapping on
2026-07-19 (the API returns the matched segment's geometry, so each probe was
verified to hit the intended road, not a frontage street):

  * US-101 mainline approaching the SFO exit from the south (67 mph free-flow)
  * the collector descending into the exit from the SF side   (57 mph)
  * the Airport Access Rd from 101 to the terminal loop        (24 mph)

Scored by travel-time ratio (current/free-flow) on the WORST probe -- the
probes are in series, so the slowest link governs the trip. Ratio 1.0 -> 0,
2.5x -> 100. A flagged road closure pins the score at 100.

Caveat: point-snapping picks the nearest carriageway; direction was chosen by
segment geometry but TomTom doesn't label it explicitly. Congestion on these
links is strongly bidirectional in practice (exit backups + ramp metering), so
the signal is honest even if a probe reads the outbound side.

Needs a (free, no-card) TomTom key: config [tomtom] api_key or
SFO_TOMTOM_API_KEY. Inert without it. 3 calls per fetch; the 20-min cron
cadence uses ~216 of the free tier's 2,500 calls/day.
"""
from __future__ import annotations

import json
from typing import Any

from . import common
from .config import Config

# (label, lat, lon, zoom) -- zoom controls snap granularity; 18 isolates the
# access road from the adjacent freeway, 10 prefers the mainline.
PROBES = [
    ("101 from S", 37.6021, -122.3763, 10),
    ("101 from SF", 37.6250, -122.4020, 10),
    ("terminal rd", 37.6150, -122.3910, 18),
]

_URL = ("https://api.tomtom.com/traffic/services/4/flowSegmentData"
        "/absolute/{zoom}/json?point={lat},{lon}&unit=MPH&key={key}")

# Score anchors: travel-time ratio 1.0 (free flow) -> 0, 2.5x -> 100.
RATIO_WORST = 2.5


def fetch(cfg: Config | None = None) -> dict[str, Any]:
    key = (cfg or Config()).tomtom_key
    if not key:
        return {"ok": False, "reason": "no_key",
                "error": "TomTom api_key not configured"}

    probes: list[dict] = []
    errors = 0
    for label, lat, lon, zoom in PROBES:
        url = _URL.format(zoom=zoom, lat=lat, lon=lon, key=key)
        try:
            status, body = common.http_get(url, timeout=15)
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            d = json.loads(body.decode("utf-8"))["flowSegmentData"]
        except Exception as e:  # noqa: BLE001 - a dead probe shouldn't kill the rest
            probes.append({"label": label, "ok": False, "error": str(e)})
            errors += 1
            continue
        cur_tt = d.get("currentTravelTime")
        free_tt = d.get("freeFlowTravelTime")
        ratio = (cur_tt / free_tt) if cur_tt and free_tt else None
        probes.append({
            "label": label,
            "ok": True,
            "current_mph": d.get("currentSpeed"),
            "freeflow_mph": d.get("freeFlowSpeed"),
            "ratio": round(ratio, 3) if ratio else None,
            "confidence": d.get("confidence"),
            "closed": bool(d.get("roadClosure")),
        })

    if errors == len(PROBES):
        return {"ok": False, "reason": "all_probes_failed", "probes": probes,
                "error": "; ".join(p.get("error", "?") for p in probes)}

    good = [p for p in probes if p.get("ok")]
    worst = max((p for p in good if p.get("ratio")),
                key=lambda p: p["ratio"], default=None)
    return {
        "ok": True,
        "probes": probes,
        "closed": any(p.get("closed") for p in good),
        "worst_label": worst["label"] if worst else None,
        "worst_ratio": worst["ratio"] if worst else None,
    }


def score(reading: dict) -> float | None:
    """0..100 congestion on the worst approach link (100 == worst/closed)."""
    if not reading.get("ok"):
        return None
    if reading.get("closed"):
        return 100.0
    ratio = reading.get("worst_ratio")
    if ratio is None:
        return None
    return common.linscale(ratio, 1.0, RATIO_WORST)


def summarize(reading: dict) -> str:
    if not reading.get("ok"):
        return f"Approach: unavailable ({reading.get('reason')})"
    if reading.get("closed"):
        closed = [p["label"] for p in reading["probes"] if p.get("closed")]
        return f"Approach: ROAD CLOSED ({', '.join(closed)})"
    bits = []
    for p in reading["probes"]:
        if not p.get("ok"):
            bits.append(f"{p['label']} n/a")
            continue
        bits.append(f"{p['label']} {p['current_mph']}/{p['freeflow_mph']}mph")
    worst = reading.get("worst_ratio")
    tag = "clear" if (worst or 1.0) < 1.15 else f"{round((worst-1)*100)}% slow"
    return f"Approach: {tag} - " + ", ".join(bits)
