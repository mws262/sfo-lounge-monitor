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
    Returns airport-wide totals, a per-terminal breakdown, and a
    delayed/cancelled tally within the window.
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
    else:
        n = reading["next_window"]
        scope = ""
    extra = ""
    if reading.get("cancelled") or reading.get("delayed"):
        extra = f" ({reading['delayed']} delayed, {reading['cancelled']} cxl)"
    bt = reading["by_terminal"]
    bd = " ".join(f"{k}:{v}" for k, v in bt.items() if v)
    return (
        f"Departures{scope}: {n} scheduled in next {w}m{extra}"
        + (f" [{bd}]" if bd and not terminal else "")
    )
