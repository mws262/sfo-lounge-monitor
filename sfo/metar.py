"""KSFO weather / fog risk from the Aviation Weather Center METAR API.

SFO's parallel-runway approach degrades in the marine layer: a low, dropping
ceiling cascades into arrival delays *before* delay feeds show them. So ceiling
+ flight category is a leading indicator, weighted independently of GDP.
"""
from __future__ import annotations

from typing import Any

from . import common

URL = "https://aviationweather.gov/api/data/metar?ids=KSFO&format=json"

# The operational cliff. SFO's two arrival runways are 750 ft apart, so
# side-by-side ("dual") parallel landings need visual conditions: a ceiling of
# >=3,500 ft and >5 mi visibility. Above that, ~55 arrivals/hr. The moment the
# marine layer drops the ceiling below ~3,500 ft, SFO reverts to a single
# arrival stream and capacity roughly HALVES to ~30/hr -- the cause of roughly
# half of all SFO delay. (theclubairportlounges/MIT-LL marine stratus studies.)
DUAL_APPROACH_CEILING_FT = 3500
DUAL_APPROACH_VIS_SM = 5

# Flight-category baseline risk. LIFR/IFR at SFO = marine layer choking arrivals.
_FLTCAT_BASE = {"VFR": 5, "MVFR": 40, "IFR": 80, "LIFR": 100}

# Shown as the fog signal's tooltip so the number has a sense of scale.
SCALE_NOTE = (
    "SFO lands side-by-side on both runways only in visual conditions "
    "(ceiling >=3,500 ft & vis >5 mi): ~55 arrivals/hr. Below ~3,500 ft the "
    "marine layer forces a single stream -> ~30/hr, and delays build during "
    "the arrival banks. The score jumps as the ceiling falls past 3,500 ft."
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
    elif ceil < DUAL_APPROACH_CEILING_FT:
        ceil_txt = (f"{ceil}ft ceiling - below the {DUAL_APPROACH_CEILING_FT}ft "
                    f"dual-approach floor, arrivals ~halved")
    else:
        ceil_txt = f"{ceil}ft ceiling - above the {DUAL_APPROACH_CEILING_FT}ft floor"
    return (
        f"Weather: {reading.get('fltCat','?')}, {ceil_txt}, {vis_txt} "
        f"(wind {reading.get('wind_dir','?')}@{reading.get('wind_kt','?')}kt)"
    )
