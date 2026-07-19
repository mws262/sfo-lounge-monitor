"""FAA national airspace status (ASWS) -> SFO ground delays / stops / closures.

nasstatus.faa.gov returns one XML document covering the whole NAS. We filter
for SFO entries across the delay categories (Ground Delay Programs, Ground
Stops, arrival/departure delays, closures) and turn them into a 0..100 signal.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from . import common

URL = "https://nasstatus.faa.gov/api/airport-status-information"
AIRPORT = "SFO"


def _text(el: ET.Element | None) -> str | None:
    return el.text.strip() if el is not None and el.text else None


def fetch() -> dict[str, Any]:
    status, body = common.http_get(URL)
    if status != 200:
        return {"ok": False, "error": f"HTTP {status}", "events": []}
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        return {"ok": False, "error": f"XML parse: {e}", "events": []}

    updated = _text(root.find("Update_Time"))
    events: list[dict] = []

    # ASWS uses a different wrapper element per delay category:
    #   Airport Closures      -> <Airport>
    #   Ground Delay Programs  -> <Ground_Delay>
    #   Ground Stop Programs   -> <Program> / <Ground_Stop>
    #   Arrival/Dep delays     -> <Delay>
    # They only share an <ARPT> child, so match on that rather than tag name.
    for dtype in root.findall("Delay_type"):
        category = _text(dtype.find("Name")) or "Unknown"
        for el in dtype.iter():
            arpt_el = el.find("ARPT")
            if arpt_el is None or _text(arpt_el) != AIRPORT:
                continue
            ev = {"category": category, "arpt": AIRPORT}
            for tag in ("Reason", "Avg", "Max", "Min", "Start", "Reopen",
                        "Trend", "Comment"):
                v = _text(el.find(tag))
                if v:
                    ev[tag.lower()] = v
            # Ground Delay Programs nest their delay under Arrival_Departure.
            ad = el.find("Arrival_Departure")
            if ad is not None:
                for tag in ("Type", "Avg", "Max", "Min"):
                    v = _text(ad.find(tag))
                    if v:
                        ev[f"ad_{tag.lower()}"] = v
            events.append(ev)

    return {
        "ok": True,
        "updated": updated,
        "events": events,
        "ground_stop": any("Ground Stop" in e["category"] for e in events),
        "ground_delay": any(
            "Ground Delay" in e["category"] or "Delay Program" in e["category"]
            for e in events
        ),
        "closure": any("Closure" in e["category"] for e in events),
    }


def score(reading: dict) -> float | None:
    """0..100. Ground stop dominates; GDP scales by average delay minutes."""
    if not reading.get("ok"):
        return None
    if reading.get("closure"):
        return 100.0
    if reading.get("ground_stop"):
        return 100.0
    if reading.get("ground_delay"):
        # Scale by the largest avg delay we can find (30 min -> ~100).
        avg = _max_avg_minutes(reading["events"])
        return common.clamp(50 + common.linscale(avg, 0, 60) / 2) if avg else 60.0
    return 0.0


def _max_avg_minutes(events: list[dict]) -> float | None:
    mins: list[float] = []
    for e in events:
        for key in ("avg", "ad_avg", "max", "ad_max"):
            v = e.get(key)
            if not v:
                continue
            import re

            m = re.search(r"(\d+)", str(v))
            if m:
                mins.append(float(m.group(1)))
    return max(mins) if mins else None


def summarize(reading: dict) -> str:
    if not reading.get("ok"):
        return f"FAA: unavailable ({reading.get('error')})"
    events = reading["events"]
    if not events:
        return "FAA: no SFO ground program (normal ops)"
    parts = []
    for e in events:
        bit = e["category"]
        detail = e.get("avg") or e.get("ad_avg") or e.get("reason")
        if detail:
            bit += f" ({detail})"
        parts.append(bit)
    return "FAA: SFO " + "; ".join(parts)
