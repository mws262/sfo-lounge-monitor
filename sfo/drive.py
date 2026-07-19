"""Optional traffic-aware drive time to SFO via the Google Routes API.

Inert unless you supply a Routes API key and an origin in config:

    [google]
    routes_api_key = "..."
    [drive]
    origin = "37.7749,-122.4194"   # lat,lng or an address string

The origin is deliberately not hardcoded -- departure origin varies. Without a
key/origin the composite simply drops the drive-time signal.
"""
from __future__ import annotations

import json
from typing import Any

from . import common
from .config import Config

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
SFO_LATLNG = (37.6213, -122.3790)

# Normalization anchors (minutes): a 25-min baseline drive, 75 min == awful.
DRIVE_BASELINE_MIN = 25
DRIVE_WORST_MIN = 75


def _waypoint(origin: str) -> dict:
    origin = origin.strip()
    if "," in origin and all(
        _isfloat(p) for p in origin.split(",", 1)
    ):
        lat, lng = (float(p) for p in origin.split(",", 1))
        return {"location": {"latLng": {"latitude": lat, "longitude": lng}}}
    return {"address": origin}


def _isfloat(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def fetch(cfg: Config) -> dict[str, Any]:
    key = cfg.google_routes_key
    origin = cfg.drive_origin
    if not key or not origin:
        return {"ok": False, "reason": "not_configured",
                "error": "google.routes_api_key + drive.origin required"}

    payload = {
        "origin": _waypoint(origin),
        "destination": {
            "location": {"latLng": {
                "latitude": SFO_LATLNG[0], "longitude": SFO_LATLNG[1]}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
    }
    body = json.dumps(payload).encode("utf-8")
    import urllib.request

    req = urllib.request.Request(ROUTES_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Goog-Api-Key", key)
    req.add_header("X-Goog-FieldMask", "routes.duration,routes.distanceMeters")
    req.add_header("User-Agent", common.USER_AGENT)
    try:
        import urllib.error

        with urllib.request.urlopen(req, timeout=common.DEFAULT_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 - report, don't crash the composite
        return {"ok": False, "reason": "fetch_failed", "error": str(e)}

    routes = data.get("routes") or []
    if not routes:
        return {"ok": False, "reason": "no_route", "error": str(data)[:200]}
    dur = routes[0].get("duration", "0s")
    seconds = int(str(dur).rstrip("s") or 0)
    return {
        "ok": True,
        "minutes": round(seconds / 60),
        "meters": routes[0].get("distanceMeters"),
    }


def score(reading: dict) -> float | None:
    if not reading.get("ok"):
        return None
    return common.linscale(reading["minutes"], DRIVE_BASELINE_MIN, DRIVE_WORST_MIN)


def summarize(reading: dict) -> str:
    if not reading.get("ok"):
        return f"Drive: unavailable ({reading.get('reason')})"
    km = (reading.get("meters") or 0) / 1000
    return f"Drive: ~{reading['minutes']}m to SFO ({km:.0f} km)"
