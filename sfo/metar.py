"""KSFO weather / fog risk from the Aviation Weather Center METAR API.

SFO's parallel-runway approach degrades in the marine layer: a low, dropping
ceiling cascades into arrival delays *before* delay feeds show them. So ceiling
+ flight category is a leading indicator, weighted independently of GDP.
"""
from __future__ import annotations

from typing import Any

from . import common

URL = "https://aviationweather.gov/api/data/metar?ids=KSFO&format=json"

# Flight-category baseline risk. LIFR/IFR at SFO = marine layer choking arrivals.
_FLTCAT_BASE = {"VFR": 5, "MVFR": 40, "IFR": 80, "LIFR": 100}


def _ceiling_ft(clouds: list[dict]) -> int | None:
    """Lowest broken/overcast layer = the ceiling, in feet AGL."""
    bases = [
        c["base"]
        for c in clouds or []
        if c.get("cover") in ("BKN", "OVC") and isinstance(c.get("base"), (int, float))
    ]
    return int(min(bases)) if bases else None


def _visib_sm(visib: Any) -> float | None:
    """Visibility in statute miles. '10+' -> 10.0 ; '1/2' -> 0.5."""
    if visib is None:
        return None
    if isinstance(visib, (int, float)):
        return float(visib)
    s = str(visib).strip().rstrip("+")
    if "/" in s:
        try:
            num, den = s.split("/")
            return float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch() -> dict[str, Any]:
    data = common.http_get_json(URL)
    if not data:
        return {"ok": False, "error": "empty METAR response"}
    ob = data[0]
    clouds = ob.get("clouds") or []
    return {
        "ok": True,
        "obsTime": ob.get("reportTime"),
        "fltCat": ob.get("fltCat"),
        "ceiling_ft": _ceiling_ft(clouds),
        "visib_sm": _visib_sm(ob.get("visib")),
        "wind_dir": ob.get("wdir"),
        "wind_kt": ob.get("wspd"),
        "temp_c": ob.get("temp"),
        "dewp_c": ob.get("dewp"),
        "cover": ob.get("cover"),
        "raw": ob.get("rawOb"),
    }


def score(reading: dict) -> float | None:
    """0..100 fog/approach-degradation risk (100 == worst)."""
    if not reading.get("ok"):
        return None
    base = _FLTCAT_BASE.get(reading.get("fltCat"), 20)

    # Extra penalty for a low ceiling even if category hasn't flipped yet.
    ceil = reading.get("ceiling_ft")
    ceil_pen = 0.0
    if ceil is not None:
        # 3000 ft -> 0, 500 ft -> 100 of the ceiling component.
        ceil_pen = common.linscale(3000 - ceil, 0, 2500)

    # Small marine-layer signal: temp near dewpoint => saturated air / fog.
    spread_pen = 0.0
    t, d = reading.get("temp_c"), reading.get("dewp_c")
    if isinstance(t, (int, float)) and isinstance(d, (int, float)):
        spread_pen = common.linscale(3 - (t - d), 0, 3)  # spread<=0 -> 100

    return common.clamp(0.6 * base + 0.3 * ceil_pen + 0.1 * spread_pen)


def summarize(reading: dict) -> str:
    if not reading.get("ok"):
        return f"Weather: unavailable ({reading.get('error')})"
    ceil = reading.get("ceiling_ft")
    ceil_txt = f"{ceil}ft ceiling" if ceil is not None else "no ceiling"
    vis = reading.get("visib_sm")
    vis_txt = f"{vis:g}SM vis" if vis is not None else "vis n/a"
    return (
        f"Weather: {reading.get('fltCat','?')}, {ceil_txt}, {vis_txt} "
        f"(wind {reading.get('wind_dir','?')}@{reading.get('wind_kt','?')}kt)"
    )
