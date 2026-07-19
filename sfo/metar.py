"""KSFO weather / fog risk from the Aviation Weather Center METAR API.

DORMANT as of 2026-07: the fog signal was removed from the composite after the
FAA permanently banned SFO's side-by-side landings (see below), which made the
weather-capacity model obsolete. Measured delays (departures.delay_score)
replaced it. This module is retained, unwired, in case it's ever useful again.

Historically this was SFO's dominant delay driver: the marine layer dropping
the ceiling below ~3,500 ft cost the airport its side-by-side parallel
landings, halving arrival capacity. As of spring 2026 the FAA PERMANENTLY
BANNED those side-by-side landings on safety grounds, so fair-weather arrivals
are now ~36/hr in ALL conditions -- the big weather-driven swing is gone.

A low ceiling still adds restriction (single-runway ops, go-arounds), so this
stays as a soft leading hint, but it's de-weighted in the composite and the
measured flight-delay bars are the reliable read now.
"""
from __future__ import annotations

from typing import Any

from . import common

URL = "https://aviationweather.gov/api/data/metar?ids=KSFO&format=json"

# Rough marker for "ceiling low enough to add restriction." This used to be the
# hard side-by-side floor (~3,500 ft); post-ban it's just a soft IMC-onset hint.
LOW_CEILING_FT = 3500

# Flight-category baseline risk. LIFR/IFR at SFO = marine layer choking arrivals.
_FLTCAT_BASE = {"VFR": 5, "MVFR": 40, "IFR": 80, "LIFR": 100}

# Shown as the fog signal's tooltip so the number has a sense of scale.
SCALE_NOTE = (
    "Heads up: the FAA permanently banned SFO's side-by-side parallel landings "
    "(spring 2026), cutting fair-weather arrivals from ~54 to ~36/hr in ALL "
    "conditions -- so SFO now runs delay-prone even on clear days. A low marine "
    "ceiling still adds restriction (single-runway, go-arounds) on top, but the "
    "weather swing is smaller than before. The flight-delay bars measure the "
    "real outcome and are the better guide now."
)


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

    # Ceiling is THE SFO variable: the dual-approach capacity cut happens at
    # ~3,500 ft, not at IFR minimums. Model the step -- ~0 above 4,000 ft,
    # rising to ~50 (the halving) as it crosses the 3,500 ft threshold near
    # 3,000 ft, then on toward 100 as it deepens to ~500 ft.
    ceil = reading.get("ceiling_ft")
    ceil_pen = 0.0
    if ceil is not None:
        if ceil >= 4000:
            ceil_pen = 0.0
        elif ceil >= 3000:  # 4,000 -> 3,000 ft ramps 0 -> 50
            ceil_pen = common.linscale(4000 - ceil, 0, 1000) * 0.5
        else:               # 3,000 -> 500 ft ramps 50 -> 100
            ceil_pen = 50 + common.linscale(3000 - ceil, 0, 2500) * 0.5

    # Marine-layer confirmation: temp near dewpoint => saturated air / fog.
    spread_pen = 0.0
    t, d = reading.get("temp_c"), reading.get("dewp_c")
    if isinstance(t, (int, float)) and isinstance(d, (int, float)):
        spread_pen = common.linscale(3 - (t - d), 0, 3)  # spread<=0 -> 100

    # Ceiling-led weighting: at SFO the ceiling drives delay risk more than the
    # coarse flight category does.
    return common.clamp(0.35 * base + 0.5 * ceil_pen + 0.15 * spread_pen)


def summarize(reading: dict) -> str:
    if not reading.get("ok"):
        return f"Weather: unavailable ({reading.get('error')})"
    ceil = reading.get("ceiling_ft")
    vis = reading.get("visib_sm")
    vis_txt = f"{vis:g}SM vis" if vis is not None else "vis n/a"

    # Always frame the ceiling against the 3,500 ft dual-approach floor so the
    # number carries scale: below it, SFO's arrival rate roughly halves.
    if ceil is None:
        ceil_txt = "no ceiling"
    elif ceil < LOW_CEILING_FT:
        ceil_txt = f"{ceil}ft ceiling - low, adds restriction on the ~36/hr rate"
    else:
        ceil_txt = f"{ceil}ft ceiling"
    return (
        f"Weather: {reading.get('fltCat','?')}, {ceil_txt}, {vis_txt} "
        f"(wind {reading.get('wind_dir','?')}@{reading.get('wind_kt','?')}kt)"
    )
