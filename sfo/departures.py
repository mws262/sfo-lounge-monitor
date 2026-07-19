"""Forward-looking scheduled departures from flysfo's own flight board.

Replaces the OpenSky module. Source:

    GET https://www.flysfo.com/flysfo/api/flight-status   (keyless JSON)

This is the full day's board (arrivals + departures) that the flysfo
flight-status React widget consumes. It gives what OpenSky could not: scheduled
*future* departures, per terminal, with live status -- and no credentials.

Two data-quality steps are essential:
  * Codeshares appear as separate rows (one physical UA flight to YVR shows up
    as AC/AV/UA). We dedup by (scheduled_time, destination, gate) to count
    physical movements -- undeduped counts run ~3x high.
  * Keep flight_nature == "PAX" (drop cargo).

The API has no server-side filter -- every call is the full ~11 MB board -- so
we cache it on disk with a short TTL and recompute windows locally.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from typing import Any

from . import common
from .config import Config

URL = "https://www.flysfo.com/flysfo/api/flight-status"
CACHE_NAME = "flysfo_board.json"
# Seconds. The board is served gzipped (~500 KB), and a "next 90 min" schedule
# barely moves over this long, so a generous TTL keeps bandwidth low. Extra
# polls inside the TTL cost zero bytes (served from the on-disk cache).
# 20 min => ~500 KB x 72/day => ~36 MB/day even under a tight lounge cadence.
CACHE_TTL = 1200

TERMINALS = ("ITM", "T1", "T2", "T3")

# Anchor for the 0..100 score: airport-wide physical departures in the window.
# ~50-55/hr is SFO's realistic peak, so a 90-min window tops out near 80.
BUSY_WINDOW_DEPARTURES = 80

_CANCEL_HINTS = ("cancel",)
_DELAY_HINTS = ("delay",)


def _cache_path(cache_dir: str | None) -> str:
    return os.path.join(cache_dir or ".", CACHE_NAME)


def _load_board(cache_dir: str | None, cache_ttl: int, force: bool) -> dict:
    """Return the board JSON, using a disk cache younger than cache_ttl."""
    path = _cache_path(cache_dir)
    if not force and os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < cache_ttl:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    status, body = common.http_get(URL, timeout=45)
    if status != 200:
        raise RuntimeError(f"flight board -> HTTP {status}")
    text = body.decode("utf-8", errors="replace")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass  # cache is best-effort; still return the fresh data
    return json.loads(text)


def _dest(r: dict) -> str | None:
    return (r.get("airport") or {}).get("iata_code")


def _terminal(r: dict) -> str:
    return (r.get("terminal") or {}).get("terminal_code") or "?"


def _sched(r: dict) -> str | None:
    return r.get("scheduled_aod_time") or r.get("scheduled_in_off_block_time")


def _physical_departures(rows: list[dict]) -> list[dict]:
    """PAX departures, deduped to one row per physical aircraft movement."""
    seen: dict[tuple, dict] = {}
    for r in rows:
        if r.get("flight_kind") != "Departure":
            continue
        if r.get("flight_nature") != "PAX":
            continue
        key = (_sched(r), _dest(r), (r.get("gate") or {}).get("gate_number"))
        seen.setdefault(key, r)
    return list(seen.values())


def _parse(t: str | None) -> datetime | None:
    if not t:
        return None
    try:
        return datetime.fromisoformat(t)
    except ValueError:
        return None


# Delay stats come in two buckets with different evidentiary weight:
#   departed -- flights that ACTUALLY took off in the last 2h (selected by
#               actual departure time; delay = actual - scheduled). Ground
#               truth for "how late are flights really leaving".
#   upcoming -- flights scheduled in the next 3h (delay = current estimate -
#               scheduled). Airline estimates skew optimistic, so treat this
#               bucket as a floor.
DEPARTED_LOOKBACK_MIN = 120
UPCOMING_LOOKAHEAD_MIN = 180
DELAY_THRESHOLD_MIN = 15  # DOT's standard "delayed" cutoff


def _bucket_stats(delays: list[float]) -> dict[str, Any]:
    import statistics

    late = [d for d in delays if d >= DELAY_THRESHOLD_MIN]
    n = len(delays)
    return {
        "n": n,
        "delayed_n": len(late),
        "delayed_pct": round(len(late) / n * 100) if n else None,
        "median_delay_min": round(statistics.median(late)) if late else None,
        "max_delay_min": round(max(late)) if late else None,
    }


def _delay_stats(deps: list[dict], ref: datetime,
                 terminal: str | None) -> dict[str, Any]:
    departed: list[float] = []
    upcoming: list[float] = []
    cancelled = 0
    dep_lo = ref - timedelta(minutes=DEPARTED_LOOKBACK_MIN)
    up_hi = ref + timedelta(minutes=UPCOMING_LOOKAHEAD_MIN)
    for r in deps:
        if terminal and _terminal(r) != terminal:
            continue
        sched = _parse(_sched(r))
        if not sched:
            continue
        remark = (r.get("remark") or "").lower()
        if any(h in remark for h in _CANCEL_HINTS):
            if dep_lo <= sched <= up_hi:
                cancelled += 1
            continue
        act = _parse(r.get("actual_aod_time"))
        if act and dep_lo <= act <= ref:
            departed.append(max(0.0, (act - sched).total_seconds() / 60))
        elif ref <= sched <= up_hi:
            est = _parse(r.get("estimated_aod_time")) or act
            if est:
                upcoming.append(max(0.0, (est - sched).total_seconds() / 60))

    return {
        "departed": _bucket_stats(departed),
        "upcoming": {**_bucket_stats(upcoming), "cancelled": cancelled},
    }


def fetch(
    cfg: Config | None = None,
    window_min: int = 90,
    now: datetime | None = None,
    cache_dir: str | None = None,
    cache_ttl: int = CACHE_TTL,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Count physical PAX departures scheduled in the next `window_min`.

    `now` (tz-aware) is injectable for testing; defaults to Pacific now.
    Returns airport-wide totals, a per-terminal breakdown, remark-based
    delayed/cancelled tallies in the count window, and computed delay
    distributions (airport-wide + per terminal) over a wider sample window.
    """
    try:
        board = _load_board(cache_dir, cache_ttl, force_refresh)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    rows = board.get("data") or []
    deps = _physical_departures(rows)
    ref = now or common.pacific_now()
    end = ref + timedelta(minutes=window_min)

    per_terminal = {t: 0 for t in TERMINALS}
    total = delayed = cancelled = 0
    for r in deps:
        t = _parse(_sched(r))
        if not (t and ref <= t <= end):
            continue
        total += 1
        term = _terminal(r)
        per_terminal[term] = per_terminal.get(term, 0) + 1
        remark = (r.get("remark") or "").lower()
        if any(h in remark for h in _CANCEL_HINTS):
            cancelled += 1
        elif any(h in remark for h in _DELAY_HINTS):
            delayed += 1

    return {
        "ok": True,
        "window_min": window_min,
        "board_updated": board.get("last_update"),
        "physical_departures_total": len(deps),
        "next_window": total,
        "by_terminal": per_terminal,
        "delayed": delayed,
        "cancelled": cancelled,
        "delays": _delay_stats(deps, ref, None),
        "delays_by_terminal": {t: _delay_stats(deps, ref, t)
                               for t in TERMINALS},
    }


def score(reading: dict, terminal: str | None = None) -> float | None:
    """0..100 from departures in the window (airport-wide, or one terminal)."""
    if not reading.get("ok"):
        return None
    if terminal:
        count = reading["by_terminal"].get(terminal, 0)
        # A single terminal tops out around a quarter of the airport rate.
        return common.linscale(count, 0, BUSY_WINDOW_DEPARTURES / 3)
    return common.linscale(reading["next_window"], 0, BUSY_WINDOW_DEPARTURES)


def summarize(reading: dict, terminal: str | None = None) -> str:
    if not reading.get("ok"):
        return f"Departures: unavailable ({reading.get('error')})"
    w = reading["window_min"]
    if terminal:
        n = reading["by_terminal"].get(terminal, 0)
        scope = f" from {terminal}"
        st = (reading.get("delays_by_terminal") or {}).get(terminal) or {}
    else:
        n = reading["next_window"]
        scope = ""
        st = reading.get("delays") or {}
    bt = reading["by_terminal"]
    bd = " ".join(f"{k}:{v}" for k, v in bt.items() if v)
    return (
        f"Departures{scope}: {n} scheduled in next {w}m"
        + (f" [{bd}]" if bd and not terminal else "")
        + " | " + delay_summary(st)
    )


def _bucket_summary(st: dict) -> str:
    """'8/20 >=15m, median 26m, worst 102m' or 'none of 20 late'."""
    if not st or not st.get("n"):
        return "no data"
    if not st.get("delayed_n"):
        return f"none of {st['n']} late"
    return (f"{st['delayed_n']}/{st['n']} >={DELAY_THRESHOLD_MIN}m, "
            f"median {st['median_delay_min']}m, worst {st['max_delay_min']}m")


def delay_summary(st: dict) -> str:
    """Human line for a _delay_stats dict (departed/upcoming buckets)."""
    if not st:
        return "delays: no data"
    dep = st.get("departed") or {}
    up = st.get("upcoming") or {}
    cxl = f", {up['cancelled']} cxl" if up.get("cancelled") else ""
    return (f"took off last 2h: {_bucket_summary(dep)} | "
            f"next 3h est: {_bucket_summary(up)}{cxl}")
