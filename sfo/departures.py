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
    # Both fields are the same GATE time on every row -- airlines publish no
    # takeoff schedule -- so this is the baseline for gate-vs-gate delays.
    return r.get("scheduled_in_off_block_time") or r.get("scheduled_aod_time")


def _physical(rows: list[dict], kind: str) -> list[dict]:
    """PAX rows of one kind, deduped to one per physical aircraft movement.

    Codeshares repeat a movement under every marketing code; time + other
    airport + gate identifies the aircraft. The surviving row carries the
    operating carrier's code (it lists codeshares under `code_shares`).
    """
    seen: dict[tuple, dict] = {}
    for r in rows:
        if r.get("flight_kind") != kind:
            continue
        if r.get("flight_nature") != "PAX":
            continue
        key = (_sched(r), _dest(r), (r.get("gate") or {}).get("gate_number"))
        seen.setdefault(key, r)
    return list(seen.values())


def _physical_departures(rows: list[dict]) -> list[dict]:
    """PAX departures, deduped to one row per physical aircraft movement."""
    return _physical(rows, "Departure")


def _parse(t: str | None) -> datetime | None:
    if not t:
        return None
    try:
        return datetime.fromisoformat(t)
    except ValueError:
        return None


# Delay stats come in two buckets with different evidentiary weight:
#   departed -- flights that ACTUALLY left the gate in the last 2h (selected
#               by actual off-block time; delay = actual - scheduled, gate vs
#               gate). Ground truth for "how late are flights really leaving".
#   upcoming -- flights scheduled in the next 3h (delay = current estimate -
#               scheduled). Airline estimates skew optimistic, so treat this
#               bucket as a floor.
#
# GATE (in_off_block) times, NOT aod (wheels-up): the published schedule is a
# gate schedule -- scheduled_aod == scheduled_in_off_block on every row --
# so aod-vs-schedule silently books SFO's ~24m median taxi as "delay" on
# every single flight (it once claimed 90% of departures late where
# gate-vs-gate says ~30%). DOT on-time stats are gate-departure too.
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
        # Full sorted distribution (ints, minutes) for the gradient-bar viz.
        "delays_sorted": sorted(round(d) for d in delays),
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
        act = _parse(r.get("actual_in_off_block_time"))
        if act and dep_lo <= act <= ref:
            departed.append(max(0.0, (act - sched).total_seconds() / 60))
        elif ref <= sched <= up_hi:
            est = _parse(r.get("estimated_in_off_block_time")) or act
            if est:
                upcoming.append(max(0.0, (est - sched).total_seconds() / 60))

    return {
        "departed": _bucket_stats(departed),
        "upcoming": {**_bucket_stats(upcoming), "cancelled": cancelled},
    }


# Destinations worth an explicit per-flight list on the dashboard (routes the
# user actually flies). Airport-wide, not terminal-scoped -- you take the
# flight from whichever terminal it leaves.
WATCH_DESTINATIONS = ("SEA", "PAE")
WATCH_PAST_MIN = 30  # keep just-departed rows briefly for context


def _watch_flights(deps: list[dict], ref: datetime) -> list[dict]:
    """Per-flight rows to WATCH_DESTINATIONS: upcoming + just-departed.

    Times are GATE times (see the bucket note above): sched/best are
    off-block, wheels_up is the actual takeoff once airborne. late_min is
    signed (negative = pushed back early) and None for cancellations.
    """
    out = []
    for r in deps:
        if _dest(r) not in WATCH_DESTINATIONS:
            continue
        sched = _parse(_sched(r))
        if not sched:
            continue
        cancelled = any(h in (r.get("remark") or "").lower()
                        for h in _CANCEL_HINTS)
        act = _parse(r.get("actual_in_off_block_time"))
        best = act or _parse(r.get("estimated_in_off_block_time")) or sched
        # Window on the flight's best-known gate time (schedule for cancels).
        if (sched if cancelled else best) < ref - timedelta(minutes=WATCH_PAST_MIN):
            continue
        out.append({
            "flight": f"{(r.get('airline') or {}).get('iata_code') or '?'}"
                      f"{r.get('flight_number') or ''}",
            "airline": (r.get("airline") or {}).get("airline_display_name"),
            "dest": _dest(r),
            "sched": sched.isoformat(),
            "best": best.isoformat(),
            "late_min": None if cancelled
            else round((best - sched).total_seconds() / 60),
            "status": r.get("remark") or "",
            "departed": bool(act),
            "wheels_up": r.get("actual_aod_time"),
            "gate": (r.get("gate") or {}).get("gate_number"),
            "terminal": _terminal(r),
        })
    out.sort(key=lambda f: f["sched"])
    _enrich_sea_arrivals(out)
    return out


def _enrich_sea_arrivals(flights: list[dict]) -> None:
    """Attach Sea-Tac's scheduled arrival info to SEA rows, in place.

    Best-effort garnish: any failure (site down, markup changed) leaves the
    rows without arr_* fields and never breaks the board. PAE has no feed.
    """
    from . import seatac

    for f in flights:
        if f["dest"] != "SEA":
            continue
        sched = _parse(f["sched"])
        if not sched:
            continue
        try:
            hit = seatac.match(seatac.rows_for_departure(sched),
                               f["flight"], sched)
        except Exception:  # noqa: BLE001 - arrivals are optional decoration
            return  # site unreachable; skip the rest too
        if hit:
            f["arr_sched"] = hit["when"].isoformat()
            f["arr_est"] = hit["est"].isoformat() if hit["est"] else None
            f["arr_status"] = hit["status"]
            f["arr_gate"] = hit["gate"]
            f["arr_claim"] = hit["claim"]


def _return_flights(rows: list[dict], ref: datetime) -> dict:
    """SEA -> SFO returns for the SEA tab: departure-centric rows.

    The flysfo board's ARRIVAL rows are the spine (physical flights under
    operating codes, with SFO in-block times, gate and baggage carousel);
    the Port of Seattle's departures page supplies the SEA-side departure
    schedule, live "Now" revision, status and gate. PAE returns are
    omitted -- Paine Field is a separate airport with no feed. Without the
    Port site there is no departure time to show, so this reports ok=False
    rather than a half-empty list.
    """
    from . import seatac

    flights: list[dict] = []
    for r in _physical(rows, "Arrival"):
        if _dest(r) != "SEA":   # on arrival rows, `airport` is the origin
            continue
        sched_arr = _parse(_sched(r))
        if not sched_arr:
            continue
        cancelled = any(h in (r.get("remark") or "").lower()
                        for h in _CANCEL_HINTS)
        act = _parse(r.get("actual_in_off_block_time"))
        est_arr = act or _parse(r.get("estimated_in_off_block_time"))
        best_arr = est_arr or sched_arr
        if ((sched_arr if cancelled else best_arr)
                < ref - timedelta(minutes=WATCH_PAST_MIN)):
            continue
        code = (f"{(r.get('airline') or {}).get('iata_code') or '?'}"
                f"{r.get('flight_number') or ''}")
        try:
            dep = seatac.match(seatac.dep_rows_for_arrival(sched_arr),
                               code, sched_arr, before=True)
        except Exception:  # noqa: BLE001 - Port site down: no dep times at all
            return {"origin": "SEA", "ok": False, "flights": []}
        if not dep:
            continue  # oddity the Port page doesn't list
        status = (dep.get("status") or "").replace("On-Time", "On Time")
        if dep["est"] and dep["est"] > dep["when"]:
            status = "Delayed"  # raw status is the "Now H:MM" string itself
        arr_remark = r.get("remark") or ""
        term = _terminal(r)
        flights.append({
            "flight": code,
            "airline": (r.get("airline") or {}).get("airline_display_name"),
            "dest": "SEA",
            "sched": dep["when"].isoformat(),
            "best": (dep["est"] or dep["when"]).isoformat(),
            "late_min": None if cancelled else round(
                ((dep["est"] or dep["when"]) - dep["when"])
                .total_seconds() / 60),
            "status": "Cancelled" if cancelled else status,
            "departed": status == "Departed"
            or arr_remark in ("Arrived", "Landed"),
            "gate": dep.get("gate"),
            "terminal": None,   # SEA is one connected terminal
            "arr_sched": sched_arr.isoformat(),
            "arr_est": est_arr.isoformat() if est_arr else None,
            "arr_status": arr_remark or None,
            "arr_gate": " ".join(
                x for x in (term if term != "?" else None,
                            (r.get("gate") or {}).get("gate_number")) if x
            ) or None,
            "arr_claim": ((r.get("baggage_carousel") or {})
                          .get("carousel_name") or "").removeprefix("CL-")
            or None,
        })
    flights.sort(key=lambda f: f["sched"])
    return {"origin": "SEA", "ok": True, "flights": flights}


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
        "watch": {"destinations": list(WATCH_DESTINATIONS),
                  "flights": _watch_flights(deps, ref)},
        "watch_return": _return_flights(rows, ref),
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
    cxl = f", {up['cancelled']} cancelled" if up.get("cancelled") else ""
    return (f"left gate last 2h: {_bucket_summary(dep)} | "
            f"next 3h est: {_bucket_summary(up)}{cxl}")


# --------------------------------------------------------------------------- #
# Delay as a scored composite signal (measured outcome, not a model)
# --------------------------------------------------------------------------- #
MIN_DELAY_SAMPLE = 3   # need a few flights before the stats mean anything

DELAY_SIGNAL_NOTE = (
    "Measured straight off the flight board: the share of recent (last 2h) "
    "departures that ACTUALLY left the gate >=15 min late (pushback vs "
    "schedule, DOT-style), plus their median delay. "
    "It's the real outcome -- robust to the cause (weather, staffing, the "
    "side-by-side landing ban) in a way a weather model isn't. Falls back to "
    "the next-3h estimates overnight when nothing has departed recently. See "
    "the Flight delays card for the full distribution."
)


def _scoring_bucket(reading: dict, terminal: str | None) -> tuple[dict, str]:
    """Pick the bucket to score: recent actuals if available, else estimates."""
    stats = ((reading.get("delays_by_terminal") or {}).get(terminal)
             if terminal else reading.get("delays")) or {}
    for key in ("departed", "upcoming"):
        b = stats.get(key) or {}
        if b.get("n", 0) >= MIN_DELAY_SAMPLE:
            return b, key
    return {}, ""


def delay_score(reading: dict, terminal: str | None = None) -> float | None:
    """0..100 from measured delays: blends % of flights late with the median.

    100 == every flight late by a long way. Uses actual last-2h departures when
    there are enough of them, else the next-3h estimates.
    """
    if not reading.get("ok"):
        return None
    b, _ = _scoring_bucket(reading, terminal)
    if not b:
        return None
    pct = b.get("delayed_pct") or 0
    med = b.get("median_delay_min") or 0
    # % late is the breadth; median (15m -> 0, 75m -> 100) is the depth.
    return common.clamp(0.55 * pct + 0.45 * common.linscale(med, 15, 75))


def median_departed_delay(reading: dict, terminal: str | None = None) -> int | None:
    """Median delay (min) across ALL recently-departed flights (on-time = 0).

    Rearward-looking: the last-2h actuals. Unlike the bars' median (late
    flights only), this is over the whole departed population, so it tracks
    overall delay severity over time -- 0 when things are clean, rising as more
    flights slip. None when nothing has departed recently.
    """
    if not reading.get("ok"):
        return None
    stats = ((reading.get("delays_by_terminal") or {}).get(terminal)
             if terminal else reading.get("delays")) or {}
    arr = (stats.get("departed") or {}).get("delays_sorted") or []
    if not arr:
        return None
    n = len(arr)
    return arr[n // 2] if n % 2 else round((arr[n // 2 - 1] + arr[n // 2]) / 2)


def delay_signal_summary(reading: dict, terminal: str | None = None) -> str:
    """One-line summary for the Delays signal row (the scoring bucket)."""
    if not reading.get("ok"):
        return f"Delays: unavailable ({reading.get('error')})"
    b, key = _scoring_bucket(reading, terminal)
    if not b:
        return "Delays: too few flights to gauge"
    when = "last 2h actual" if key == "departed" else "next 3h est"
    if not b.get("delayed_n"):
        return f"Delays: {when} - all {b['n']} ~on time"
    return (f"Delays: {when} - {b['delayed_pct']}% of {b['n']} left "
            f">={DELAY_THRESHOLD_MIN}m late, median {b['median_delay_min']}m")
