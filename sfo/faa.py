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


def fetch(airport: str = AIRPORT) -> dict[str, Any]:
    return fetch_multi((airport,))[airport]


def fetch_multi(airports: tuple[str, ...] = (AIRPORT,)) -> dict[str, dict]:
    """One national XML pull, filtered into a per-airport reading each.

    The document covers the whole NAS, so watching a second airport (SEA)
    costs zero extra bandwidth.
    """
    status, body = common.http_get(URL)
    root = None
    err = None
    if status != 200:
        err = f"HTTP {status}"
    else:
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            err = f"XML parse: {e}"
    if err:
        return {a: {"ok": False, "error": err, "events": []} for a in airports}

    updated = _text(root.find("Update_Time"))
    by_arpt: dict[str, list[dict]] = {a: [] for a in airports}

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
            code = _text(arpt_el) if arpt_el is not None else None
            if code not in by_arpt:
                continue
            ev = {"category": category, "arpt": code}
            for tag in ("Reason", "Avg", "Max", "Min", "Start", "Reopen",
                        "Trend", "Comment"):
                v = _text(el.find(tag))
                if v:
                    ev[tag.lower()] = v
            # Delays nest under <Arrival_Departure Type="Arrival|Departure">.
            # Type is an ATTRIBUTE (not a child), and Trend/Min/Max are children.
            ad = el.find("Arrival_Departure")
            if ad is not None:
                if ad.get("Type"):
                    ev["ad_type"] = ad.get("Type")  # "Arrival" | "Departure"
                for tag in ("Avg", "Max", "Min", "Trend"):
                    v = _text(ad.find(tag))
                    if v:
                        ev[f"ad_{tag.lower()}"] = v
            by_arpt[code].append(ev)

    out: dict[str, dict] = {}
    for a in airports:
        events = by_arpt[a]
        out[a] = {
            "ok": True,
            "updated": updated,
            "events": events,
            "ground_stop": any("Ground Stop" in e["category"] for e in events),
            "ground_delay": any(
                "Ground Delay" in e["category"]
                or "Delay Program" in e["category"] for e in events
            ),
            "closure": any("Closure" in e["category"] for e in events),
        }
    return out


def score(reading: dict) -> float | None:
    """0..100. Ground stop dominates; GDP scales by average delay minutes."""
    if not reading.get("ok"):
        return None
    if reading.get("closure") or reading.get("ground_stop"):
        return 100.0  # nothing is departing / arriving
    # Score by the worst delay minutes the FAA is advertising -- across ALL
    # categories, not just formal Ground Delay Programs. A "General Delay Info"
    # miles-in-trail initiative still reports real minutes. 60 min -> ~100.
    mins = _max_delay_minutes(reading["events"])
    if mins is not None:
        base = common.linscale(mins, 0, 60)
        # A declared GDP is significant even at modest minutes -> floor it.
        return common.clamp(max(base, 55) if reading.get("ground_delay") else base)
    if reading.get("ground_delay"):
        return 55.0  # GDP declared but no minutes parsed
    return 0.0  # events with no delay info, or none at all


def _to_minutes(v: str | None) -> int | None:
    """Parse an ASWS delay value to total minutes.

    Handles '45 minutes', '1 hour', '1 hour and 30 minutes', and bare numbers.
    """
    import re
    if not v:
        return None
    s = str(v)
    h = re.search(r"(\d+)\s*hour", s)
    m = re.search(r"(\d+)\s*min", s)
    if h or m:
        return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)
    n = re.search(r"(\d+)", s)
    return int(n.group(1)) if n else None


def _max_delay_minutes(events: list[dict]) -> int | None:
    mins = [
        t for e in events
        for key in ("ad_max", "max", "ad_avg", "avg", "ad_min", "min")
        if (t := _to_minutes(e.get(key))) is not None
    ]
    return max(mins) if mins else None


# Decode ASWS reason codes (colon-delimited segments) into plain words.
_REASON_WORDS = {
    "MIT": "miles-in-trail", "VOL": "volume", "WX": "weather",
    "RWY": "runway", "Construction": "construction", "EQUIP": "equipment",
    "STAFF": "staffing", "DEICE": "de-icing",
}


def _friendly_reason(raw: str | None) -> str:
    if not raw:
        return ""
    out = []
    for seg in (s.strip() for s in raw.split(":")):
        if not seg or seg.lower().startswith("tm initiative"):
            continue
        out.append(_REASON_WORDS.get(seg, seg.lower()))
    return ", ".join(dict.fromkeys(out))  # dedupe, keep order


def _mins(v: str | None) -> str | None:
    m = _to_minutes(v)
    return f"{m}m" if m is not None else None


# --------------------------------------------------------------------------- #
# Per-direction rows (inbound / outbound)
# --------------------------------------------------------------------------- #
_TREND_DISPLAY = {"Increasing": ("Rising", "up"), "Decreasing": ("Falling", "down")}


def direction_delays(reading: dict) -> dict[str, dict | None]:
    """Split SFO's active delays into inbound (Arrival) / outbound (Departure).

    Ground stops and Ground Delay Programs meter traffic *bound for* SFO --
    flights are held at their origin airports -- so a program that arrives
    without an explicit direction is filed as inbound.
    """
    out: dict[str, dict | None] = {"Arrival": None, "Departure": None}
    if not reading.get("ok"):
        return out
    for e in reading.get("events", []):
        cat = e.get("category", "")
        # Keep min / avg / max distinct: a Ground Delay Program publishes Avg
        # and Max (no Min), and the average is the representative number --
        # folding Max into it would advertise the worst case as typical.
        info = {
            "min": _to_minutes(e.get("ad_min") or e.get("min")),
            "avg": _to_minutes(e.get("ad_avg") or e.get("avg")),
            "max": _to_minutes(e.get("ad_max") or e.get("max")),
            "trend": e.get("ad_trend") or e.get("trend"),
            "reason": _friendly_reason(e.get("reason")),
            "stop": "Ground Stop" in cat,
            "closed": "Closure" in cat,
            "program": "Ground Delay" in cat or "Delay Program" in cat,
        }
        d = e.get("ad_type")
        if d not in ("Arrival", "Departure"):
            d = "Arrival"  # direction-less programs meter inbound traffic
        out[d] = info
    return out


def direction_value(info: dict | None) -> str:
    """The number shown on a direction row -- typical delay, not worst case."""
    if not info:
        return "none"
    if info.get("closed"):
        return "CLOSED"
    if info.get("stop"):
        return "STOP"
    lo, avg, hi = info.get("min"), info.get("avg"), info.get("max")
    if lo and hi and lo != hi:
        return f"{lo}-{hi}m"      # a genuine observed range
    if avg:
        return f"~{avg}m"         # average: what a typical flight gets
    if hi:
        return f"~{hi}m"
    return "active" if info.get("program") else "none"


def direction_score(info: dict | None) -> float:
    """0..100 severity for coloring a direction row."""
    if not info:
        return 0.0
    if info.get("stop") or info.get("closed"):
        return 100.0
    mins = info.get("avg") or info.get("max") or info.get("min")
    base = common.linscale(mins, 0, 60) if mins else 0.0
    return common.clamp(max(base, 55.0) if info.get("program") else base)


def direction_trend(info: dict | None) -> dict | None:
    """{'word': 'Rising', 'dir': 'up'} -- the FAA's own trend assessment."""
    if not info or not info.get("trend"):
        return None
    raw = info["trend"]
    word, arrow = _TREND_DISPLAY.get(raw, (raw.capitalize(), ""))
    return {"word": word, "dir": arrow}


def direction_rows(reading: dict, airport: str = AIRPORT) -> list[dict]:
    """Ready-to-render inbound/outbound rows for an Airport status card."""
    dd = direction_delays(reading)
    rows = []
    for key, label, d, who in (
        ("faa_in", "FAA Inbound Delay", "Arrival", "into"),
        ("faa_out", "FAA Outbound Delay", "Departure", "out of"),
    ):
        info = dd.get(d)
        note = f"FAA-declared delay for flights {who} {airport}."
        if info:
            if info.get("stop"):
                note += (f" GROUND STOP: flights bound for {airport} are "
                         "being held on the ground at their origin airports.")
            elif info.get("program"):
                note += (" Ground Delay Program: flights are held at their "
                         "origin and given metered departure times.")
            bits = []
            if info.get("avg"):
                bits.append(f"average {info['avg']}m")
            if info.get("max"):
                bits.append(f"worst {info['max']}m")
            if info.get("min") and not info.get("avg"):
                bits.append(f"from {info['min']}m")
            if bits:
                note += " Delay: " + ", ".join(bits) + "."
            if info.get("reason"):
                note += f" Cause: {info['reason']}."
            if info.get("trend"):
                note += (" Rising/Falling is the FAA's own read on whether "
                         "that delay is growing or winding down.")
        if not reading.get("ok"):
            note = f"FAA feed unavailable ({reading.get('error')})."
        rows.append({
            "key": key,
            "label": label,
            "value": direction_value(info) if reading.get("ok") else "n/a",
            "score": direction_score(info) if reading.get("ok") else None,
            "trend": direction_trend(info),
            "note": note,
        })
    return rows


def summarize(reading: dict) -> str:
    if not reading.get("ok"):
        return f"FAA: unavailable ({reading.get('error')})"
    events = reading["events"]
    if not events:
        return "FAA: no delays or programs reported (normal ops)"
    parts = []
    for e in events:
        cat = e["category"]
        direction = (e.get("ad_type") or "").lower()  # "arrival"|"departure"|""
        if "Ground Stop" in cat:
            what = "ground stop"
        elif "Ground Delay" in cat or "Delay Program" in cat:
            what = "ground delay program"
        elif "Closure" in cat:
            what = "airport closure"
        else:  # a general delay advisory -- name the side it hits
            what = f"{direction} delays".strip() if direction else "delays"
        lo = _mins(e.get("ad_min") or e.get("min"))
        avg = _mins(e.get("ad_avg") or e.get("avg"))
        hi = _mins(e.get("ad_max") or e.get("max"))
        if lo and hi and lo != hi:
            rng = f" {lo}-{hi}"
        elif avg and hi:
            rng = f" avg {avg}, worst {hi}"
        elif avg or hi:
            rng = f" ~{avg or hi}"
        else:
            rng = ""
        # The FAA's own <Trend> on the delay record: is the delay they're
        # imposing growing or shrinking? Unknown values are surfaced verbatim
        # rather than silently dropped.
        raw_trend = e.get("ad_trend") or e.get("trend")
        trend = {"Increasing": ", rising", "Decreasing": ", easing"}.get(
            raw_trend, f", {raw_trend.lower()}" if raw_trend else "")
        reason = _friendly_reason(e.get("reason"))
        parts.append(f"{what}{rng}{trend}" + (f" ({reason})" if reason else ""))
    return "FAA: " + "; ".join(parts)
