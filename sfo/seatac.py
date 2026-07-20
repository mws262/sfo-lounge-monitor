"""Scheduled arrivals at Sea-Tac for the watched SFO->SEA flights.

Source: the Port of Seattle's own flight-status widget backend --

    GET https://www.portseattle.org/pos/flights
        ?arr_or_depart=A&arrive_city=SFO&flight_date=YYYY-MM-DD

Keyless server-side-rendered HTML (same pattern as flysfo's checkpoint
table, discovered 2026-07-19). One request per arrival date (~130 KB raw,
gzips well) lists every SFO->SEA flight that day, codeshares included,
under the operating flight number too. The time column is the SCHEDULED
arrival (it never moves -- a flight that left SFO 101 min late kept its
slot there); live revisions surface in the STATUS column as "Now 12:46
AM", which we parse into an estimate. Otherwise status is On-Time /
Landed / etc., plus arrival gate and baggage claim.

SEA shares SFO's timezone, so arrival stamps borrow the departure's
Pacific tzinfo. PAE (Paine Field) has no comparable feed; those flights
simply get no arrival info.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from . import common

URL = ("https://www.portseattle.org/pos/flights?arr_or_depart=A"
       "&arrive_city=SFO&flight_date={date}")

# In-memory per-date cache so a polling CLI loop doesn't hammer the Port
# of Seattle; each cron run is a fresh process, so it fetches once anyway.
CACHE_TTL = 600
_cache: dict[str, tuple[float, list[dict]]] = {}

# SFO->SEA block time is ~2h10m gate to gate; the window tolerates padded
# schedules and same-number flights earlier/later in the day.
BLOCK_GUESS = timedelta(hours=2, minutes=10)
MATCH_LO = timedelta(minutes=45)
MATCH_HI = timedelta(hours=5)

_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _cell(raw: str) -> str:
    return _TAG_RE.sub("", raw).replace("&nbsp;", " ").strip()


def _parse_rows(html: str, tz) -> list[dict]:
    """Arrival rows -> [{code, arr, status, gate, claim}] (aware datetimes)."""
    out = []
    for tr in html.split("<tr")[1:]:
        cells = [_cell(c) for c in _TD_RE.findall(tr)]
        # origin, airline, code, MM-DD-YYYY, time, status, gate, claim, ...
        if len(cells) < 6 or not re.fullmatch(r"[A-Z0-9]{2}\d{1,4}", cells[2]):
            continue
        try:
            arr = datetime.strptime(f"{cells[3]} {cells[4]}",
                                    "%m-%d-%Y %I:%M%p").replace(tzinfo=tz)
        except ValueError:
            continue
        status = cells[5] or None
        # Live revisions come through the status column as "Now 12:46 AM".
        # The row's date belongs to the *scheduled* arrival; a revision can
        # roll past midnight, so pick the day that lands nearest the plan.
        est = None
        m = re.match(r"Now\s+(\d{1,2}:\d{2})\s*([AP]M)", status or "", re.I)
        if m:
            try:
                t = datetime.strptime(
                    f"{cells[3]} {m.group(1)}{m.group(2).upper()}",
                    "%m-%d-%Y %I:%M%p").replace(tzinfo=tz)
                est = min((t + timedelta(days=k) for k in (-1, 0, 1)),
                          key=lambda x: abs(x - arr))
            except ValueError:
                pass
        out.append({
            "code": cells[2],
            "arr": arr,
            "est": est,
            "status": status,
            "gate": (cells[6] or None) if len(cells) > 6 else None,
            "claim": (cells[7] or None) if len(cells) > 7 else None,
        })
    return out


def _fetch_date(date_iso: str, tz) -> list[dict]:
    hit = _cache.get(date_iso)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    status, body = common.http_get(URL.format(date=date_iso), timeout=15)
    if status != 200:
        raise RuntimeError(f"seatac arrivals -> HTTP {status}")
    rows = _parse_rows(body.decode("utf-8", errors="replace"), tz)
    _cache[date_iso] = (time.time(), rows)
    return rows


def match(rows: list[dict], code: str, sched_dep: datetime) -> dict | None:
    """Pick the row for `code` whose arrival fits this departure's window."""
    best, best_off = None, None
    for r in rows:
        if r["code"] != code:
            continue
        off = r["arr"] - sched_dep
        if not (MATCH_LO <= off <= MATCH_HI):
            continue
        score = abs(off - BLOCK_GUESS)
        if best is None or score < best_off:
            best, best_off = r, score
    return best


def rows_for_departure(sched_dep: datetime) -> list[dict]:
    """Fetch the arrival page(s) covering a departure at `sched_dep`.

    A ~2h block that straddles midnight lands on the next calendar day, so
    fetch both candidate dates when the guess sits near the boundary.
    """
    tz = sched_dep.tzinfo
    guess = sched_dep + BLOCK_GUESS
    dates = {guess.date()}
    dates.add((sched_dep + MATCH_LO).date())
    dates.add((sched_dep + MATCH_HI).date())
    rows: list[dict] = []
    for d in sorted(dates):
        rows.extend(_fetch_date(d.isoformat(), tz))
    return rows
