"""Sea-Tac data from the Port of Seattle's own site (keyless).

Two backends, both discovered 2026-07-19:

* ``/pos/flights`` -- the flight-status widget's SSR HTML (same pattern as
  flysfo's checkpoint table). One request per date (~130-190 KB raw, gzips
  well). ``arr_or_depart=A&arrive_city=SFO`` lists every arrival FROM SFO;
  ``arr_or_depart=D&arrive_city=SFO`` every departure TO SFO -- codeshares
  included, under the operating flight number too. The time column is the
  SCHEDULE (it never moves -- a flight that left SFO 101 min late kept its
  slot there); live revisions surface in the STATUS column as "Now 12:46
  AM", which we parse into an estimate. Otherwise status is On-Time /
  Landed / Departed / etc., plus gate and (arrivals) baggage claim.

* ``/api/cwt/wait-times`` -- live security checkpoints, plain JSON, ~3 KB.
  Per checkpoint: open/closed, wait minutes, QueueLength (people in line),
  which lanes are running (General / Pre / Spot Saver / Premium / Clear),
  and its own freshness flags. Respect IsDataAvailable: the API happily
  serves hours-stale numbers and *tells you so* instead of hiding it.

SEA shares SFO's timezone, so timestamps borrow the flight's Pacific
tzinfo. PAE (Paine Field) is a different airport with no comparable feed.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any

from . import common

ARR_URL = ("https://www.portseattle.org/pos/flights?arr_or_depart=A"
           "&arrive_city={city}&flight_date={date}")
DEP_URL = ("https://www.portseattle.org/pos/flights?arr_or_depart=D"
           "&arrive_city={city}&flight_date={date}")
CWT_URL = "https://www.portseattle.org/api/cwt/wait-times"

# In-memory per-URL cache so a polling CLI loop doesn't hammer the Port
# of Seattle; each cron run is a fresh process, so it fetches once anyway.
CACHE_TTL = 600
_cache: dict[str, tuple[float, list[dict]]] = {}

# SFO<->SEA block time is ~2h10m gate to gate; the window tolerates padded
# schedules and same-number flights on other rotations that day.
BLOCK_GUESS = timedelta(hours=2, minutes=10)
MATCH_LO = timedelta(minutes=45)
MATCH_HI = timedelta(hours=5)

_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _cell(raw: str) -> str:
    return _TAG_RE.sub("", raw).replace("&nbsp;", " ").strip()


def _parse_rows(html: str, tz) -> list[dict]:
    """Widget rows -> [{code, when, est, status, gate, claim}].

    ``when`` is the scheduled time at SEA (arrival or departure depending on
    the page queried); ``est`` is the live "Now H:MM" revision when present.
    """
    out = []
    for tr in html.split("<tr")[1:]:
        cells = [_cell(c) for c in _TD_RE.findall(tr)]
        # city, airline, code, MM-DD-YYYY, time, status, gate, claim, ...
        if len(cells) < 6 or not re.fullmatch(r"[A-Z0-9]{2}\d{1,4}", cells[2]):
            continue
        try:
            when = datetime.strptime(f"{cells[3]} {cells[4]}",
                                     "%m-%d-%Y %I:%M%p").replace(tzinfo=tz)
        except ValueError:
            continue
        status = cells[5] or None
        # Live revisions come through the status column as "Now 12:46 AM".
        # The row's date belongs to the *scheduled* time; a revision can
        # roll past midnight, so pick the day that lands nearest the plan.
        est = None
        m = re.match(r"Now\s+(\d{1,2}:\d{2})\s*([AP]M)", status or "", re.I)
        if m:
            try:
                t = datetime.strptime(
                    f"{cells[3]} {m.group(1)}{m.group(2).upper()}",
                    "%m-%d-%Y %I:%M%p").replace(tzinfo=tz)
                est = min((t + timedelta(days=k) for k in (-1, 0, 1)),
                          key=lambda x: abs(x - when))
            except ValueError:
                pass
        out.append({
            "code": cells[2],
            "when": when,
            "est": est,
            "status": status,
            "gate": (cells[6] or None) if len(cells) > 6 else None,
            "claim": (cells[7] or None) if len(cells) > 7 else None,
        })
    return out


def _fetch_page(url: str, tz) -> list[dict]:
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    status, body = common.http_get(url, timeout=15)
    if status != 200:
        raise RuntimeError(f"portseattle flights -> HTTP {status}")
    rows = _parse_rows(body.decode("utf-8", errors="replace"), tz)
    _cache[url] = (time.time(), rows)
    return rows


def _pages(url_fmt: str, city: str, dates, tz) -> list[dict]:
    rows: list[dict] = []
    for d in sorted(dates):
        rows.extend(_fetch_page(
            url_fmt.format(city=city, date=d.isoformat()), tz))
    return rows


def rows_for_departure(sched_dep: datetime, city: str = "SFO") -> list[dict]:
    """SEA-arrival pages covering an SFO departure at `sched_dep`.

    A ~2h block that straddles midnight lands on the next calendar day, so
    fetch every candidate date the match window could touch.
    """
    dates = {(sched_dep + off).date()
             for off in (BLOCK_GUESS, MATCH_LO, MATCH_HI)}
    return _pages(ARR_URL, city, dates, sched_dep.tzinfo)


def dep_rows_for_arrival(sched_arr: datetime, city: str = "SFO") -> list[dict]:
    """SEA-departure pages covering an SFO arrival at `sched_arr`."""
    dates = {(sched_arr - off).date()
             for off in (BLOCK_GUESS, MATCH_LO, MATCH_HI)}
    return _pages(DEP_URL, city, dates, sched_arr.tzinfo)


def match(rows: list[dict], code: str, anchor: datetime,
          before: bool = False) -> dict | None:
    """Pick the row for `code` that fits one block away from `anchor`.

    ``before=False``: rows are arrivals, expected BLOCK_GUESS *after* the
    anchor (an SFO departure). ``before=True``: rows are SEA departures,
    expected BLOCK_GUESS *before* the anchor (an SFO arrival).
    """
    best, best_off = None, None
    for r in rows:
        if r["code"] != code:
            continue
        off = (anchor - r["when"]) if before else (r["when"] - anchor)
        if not (MATCH_LO <= off <= MATCH_HI):
            continue
        score = abs(off - BLOCK_GUESS)
        if best is None or score < best_off:
            best, best_off = r, score
    return best


# --------------------------------------------------------------------------- #
# Security checkpoints (live waits + queue length + lane availability)
# --------------------------------------------------------------------------- #
STALE_MIN = 15  # beyond this, treat a reading as decoration, not data


def fetch_checkpoints() -> dict[str, Any]:
    """Live SEA security checkpoints, in the airport's numbered order.

    All checkpoints feed the same secure side (SEA is one connected
    terminal); the display keeps the airport's own numbering (which tracks
    the terminal's physical layout) so rows don't shuffle between
    refreshes, and the wait colors flag the best line.
    """
    try:
        data = common.http_get_json(CWT_URL, timeout=15)
    except Exception as e:  # noqa: BLE001 - one dead source, honest degrade
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if not isinstance(data, list):
        return {"ok": False, "error": f"unexpected payload: {str(data)[:120]}"}

    cps = []
    for c in data:
        lanes = [o.get("Name") for o in (c.get("Options") or [])
                 if o.get("Availability") in ("Available", "Only")]
        age = c.get("MinutesSinceLastUpdate")
        cps.append({
            "name": str(c.get("Name") or c.get("CheckpointID") or "?"),
            "open": bool(c.get("IsOpen")),
            "wait_min": c.get("WaitTimeMinutes"),
            "queue": c.get("QueueLength"),
            "lanes": lanes,
            "pre": "Pre" in lanes,
            # Raw `PreCheck` field, semantics UNRESOLVED (2026-07-19): either
            # a has-PreCheck boolean or a separate Pre-lane wait time. The
            # Port's own widget ignores it. Passed through for observation;
            # do not display until a busy-morning reading disambiguates.
            "pre_raw": c.get("PreCheck"),
            # The API serves hours-stale numbers and says so -- honor it.
            "fresh": bool(c.get("IsDataAvailable"))
                     and (age is None or age <= STALE_MIN),
            "age_min": age,
        })
    # The airport's own checkpoint numbering tracks the terminal's physical
    # layout. Non-numeric names (never seen so far) sort after the numbers.
    cps.sort(key=lambda c: (0, int(c["name"])) if c["name"].isdigit()
             else (1, c["name"]))
    return {"ok": True, "checkpoints": cps}


def best_wait(reading: dict, pre: bool = False) -> int | None:
    """Shortest trustworthy open line (minutes); PreCheck lines only if set."""
    if not reading.get("ok"):
        return None
    waits = [c["wait_min"] for c in reading["checkpoints"]
             if c["open"] and c["fresh"] and c["wait_min"] is not None
             and (c["pre"] or not pre)]
    return min(waits) if waits else None


def summarize_checkpoints(reading: dict) -> str:
    if not reading.get("ok"):
        return f"SEA security: unavailable ({reading.get('error')})"
    open_cps = [c for c in reading["checkpoints"] if c["open"] and c["fresh"]]
    if not open_cps:
        return "SEA security: no open checkpoints reporting"
    bits = [f"CP{c['name']} ~{c['wait_min']}m"
            + (f" ({c['queue']} queued)" if c.get("queue") else "")
            + (" Pre" if c["pre"] else "")
            for c in open_cps]
    return "SEA security: " + ", ".join(bits)
