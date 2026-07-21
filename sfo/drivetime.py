"""Traffic-aware drive times to the airports (TomTom Routing API).

The live-traffic sibling of the dormant Google module (drive.py): same
question, answered with the TomTom account the project already holds.
One call per configured route per fetch; the free tier (2,500/day)
dwarfs the cron cadence. The key needs only the Routing API.

Config (each route is inert -- row omitted -- until its origin exists):

    [tomtom]
    api_key = "..."              # or SFO_TOMTOM_API_KEY
    [drive]
    origin = "37.65,-122.39"     # or SFO_DRIVE_ORIGIN     -> SFO T1 row
    sea_origin = "47.68,-122.34" # or SFO_DRIVE_SEA_ORIGIN -> SEA row

Origins are deliberately coordinates-only and live in a gitignored
config / Actions secrets, never in this public repo; the published
data.json carries just the minutes. Destinations are the terminal curbs,
each resolved once (OSM) and hardcoded -- no runtime geocoding.
"""
from __future__ import annotations

import json
from typing import Any

from . import common
from .config import Config

# Terminal curbs, one-off OSM geocodes (2026-07-20/21). Small offsets don't
# matter: routing snaps to the nearest drivable point on the terminal loop.
T1_LATLNG = (37.61308, -122.38459)       # SFO Harvey Milk Terminal 1
SEA_DEP_LATLNG = (47.44310, -122.30050)  # SEA main-terminal departures drive

URL = ("https://api.tomtom.com/routing/1/calculateRoute/"
       "{olat},{olon}:{dlat},{dlon}/json"
       "?key={key}&traffic=true&computeTravelTimeFor=all&routeType=fastest")

# Score anchors: live/free-flow travel-time ratio, same scale as approach.py
# used -- 1.0x (no traffic) -> 0, 2.5x -> 100.
RATIO_WORST = 2.5


def _origin_latlng(origin: str | None) -> tuple[float, float] | None:
    if not origin or "," not in origin:
        return None
    try:
        lat, lon = (float(p) for p in origin.split(",", 1))
        return lat, lon
    except ValueError:
        return None


def fetch(cfg: Config | None = None, origin: str | None = None,
          dest: tuple[float, float] = T1_LATLNG) -> dict[str, Any]:
    cfg = cfg or Config()
    key = cfg.tomtom_key
    o = _origin_latlng(origin if origin is not None else cfg.drive_origin)
    if not key or not o:
        return {"ok": False, "reason": "not_configured",
                "error": "tomtom.api_key + an origin (lat,lng) required"}

    url = URL.format(olat=o[0], olon=o[1],
                     dlat=dest[0], dlon=dest[1], key=key)
    try:
        status, body = common.http_get(url, timeout=15)
        if status != 200:
            raise RuntimeError(f"HTTP {status}: {body[:120]!r}")
        summary = json.loads(body)["routes"][0]["summary"]
    except Exception as e:  # noqa: BLE001 - report, don't crash the bundle
        return {"ok": False, "reason": "fetch_failed", "error": str(e)}

    live_s = summary.get("travelTimeInSeconds")
    free_s = summary.get("noTrafficTravelTimeInSeconds")
    if not live_s:
        return {"ok": False, "reason": "no_route", "error": str(summary)[:200]}
    return {
        "ok": True,
        "minutes": round(live_s / 60),
        "freeflow_min": round(free_s / 60) if free_s else None,
        "delay_min": round((live_s - free_s) / 60) if free_s else None,
        "km": round((summary.get("lengthInMeters") or 0) / 1000, 1),
    }


def score(reading: dict) -> float | None:
    """0..100 by how much traffic inflates the trip (free-flow == 0)."""
    if not reading.get("ok"):
        return None
    free = reading.get("freeflow_min")
    if not free:
        return None
    return common.linscale(reading["minutes"] / free, 1.0, RATIO_WORST)


def signal_row(reading: dict, label: str = "Drive to T1",
               dest_desc: str = "the Terminal 1 curb") -> dict:
    """A ready-to-render stat row for an Airport status card."""
    ok = reading.get("ok")
    note = ("TomTom live-traffic drive time from the configured origin to "
            f"{dest_desc}.")
    if ok:
        if reading.get("freeflow_min"):
            note += (f" Free-flow is ~{reading['freeflow_min']}m; the "
                     f"difference is traffic.")
        summary = (f"~{reading['km']} km"
                   + (f" · +{reading['delay_min']}m traffic"
                      if reading.get("delay_min") else " · no traffic delay"))
    else:
        note += f" Unavailable: {reading.get('error')}"
        summary = None
    return {
        "key": "drive",
        "label": label,
        "value": f"~{reading['minutes']}m" if ok else "n/a",
        "score": score(reading),
        "note": note,
        **({"summary": summary} if summary else {}),
    }


def summarize(reading: dict) -> str:
    if not reading.get("ok"):
        return f"Drive: unavailable ({reading.get('reason')})"
    extra = (f", +{reading['delay_min']}m traffic"
             if reading.get("delay_min") else "")
    return f"Drive to T1: ~{reading['minutes']}m ({reading['km']} km{extra})"
