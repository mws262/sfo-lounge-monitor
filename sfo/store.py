"""SQLite time-series logging for the lounge and the airport composite.

Two tables:
  status   -- the lounge feed (schema compatible with the original poller)
  airport  -- the composite score + per-component sub-scores

The full decoded reading is stored as JSON in a `raw` column on both, so no
data is lost even though only decision-relevant fields are promoted to columns.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import common

DEFAULT_DB = "club_sfo.db"

_STATUS_DDL = """
CREATE TABLE IF NOT EXISTS status (
    ts TEXT PRIMARY KEY,
    state TEXT,
    numWaiting INTEGER,
    numWaitingGuests INTEGER,
    numServing INTEGER,
    numServingGuests INTEGER,
    waitMin INTEGER,
    nextTicket INTEGER,
    isFull INTEGER,
    isForceClosed INTEGER,
    docUpdated TEXT,
    raw TEXT
)
"""

_AIRPORT_DDL = """
CREATE TABLE IF NOT EXISTS airport (
    ts TEXT PRIMARY KEY,
    score REAL,
    band TEXT,
    security REAL,
    fog REAL,
    departures REAL,
    gdp REAL,
    drive REAL,
    raw TEXT
)
"""


def connect(path: str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(_STATUS_DDL)
    conn.execute(_AIRPORT_DDL)
    conn.commit()
    return conn


def log_lounge(conn: sqlite3.Connection, fields: dict, ts: str | None = None) -> str:
    ts = ts or common.iso_local()
    conn.execute(
        """INSERT OR REPLACE INTO status
           (ts, state, numWaiting, numWaitingGuests, numServing,
            numServingGuests, waitMin, nextTicket, isFull, isForceClosed,
            docUpdated, raw)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ts,
            fields.get("state"),
            fields.get("numWaiting"),
            fields.get("numWaitingGuests"),
            fields.get("numServing"),
            fields.get("numServingGuests"),
            fields.get("waitMin"),
            fields.get("nextTicket"),
            1 if fields.get("isWaitlistFull") else 0,
            1 if fields.get("isForceClosed") else 0,
            fields.get("updated"),
            json.dumps(_jsonable(fields)),
        ),
    )
    conn.commit()
    return ts


def log_airport(
    conn: sqlite3.Connection,
    comp: dict,
    subscores: dict,
    detail: dict[str, Any],
    ts: str | None = None,
) -> str:
    from .score import band

    ts = ts or common.iso_local()
    conn.execute(
        """INSERT OR REPLACE INTO airport
           (ts, score, band, security, fog, departures, gdp, drive, raw)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            ts,
            comp.get("score"),
            band(comp.get("score")),
            subscores.get("security"),
            subscores.get("fog"),
            subscores.get("departures"),
            subscores.get("gdp"),
            subscores.get("drive"),
            json.dumps(_jsonable(detail)),
        ),
    )
    conn.commit()
    return ts


def last_lounge(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT ts, state, nextTicket, raw FROM status ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return {"ts": row[0], "state": row[1], "nextTicket": row[2],
            "raw": json.loads(row[3]) if row[3] else {}}


def recent_airport(conn: sqlite3.Connection, limit: int = 240) -> list[dict]:
    """Most recent airport rows, oldest-first (for trend charts)."""
    rows = conn.execute(
        "SELECT ts, score, band, security, fog, departures, gdp, drive "
        "FROM airport ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    cols = ("ts", "score", "band", "security", "fog", "departures", "gdp", "drive")
    return [dict(zip(cols, r)) for r in reversed(rows)]


def recent_lounge(conn: sqlite3.Connection, limit: int = 240) -> list[dict]:
    """Most recent lounge rows, oldest-first."""
    rows = conn.execute(
        "SELECT ts, state, numWaiting, numWaitingGuests, numServing, waitMin "
        "FROM status ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    cols = ("ts", "state", "numWaiting", "numWaitingGuests", "numServing", "waitMin")
    return [dict(zip(cols, r)) for r in reversed(rows)]


def _jsonable(obj: Any) -> Any:
    """Best-effort make a dict JSON-serializable (drop unknowns to str)."""
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        return json.loads(json.dumps(obj, default=str))
