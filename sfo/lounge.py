"""The Club SFO lounge feed (Waitwhile -> Firestore).

CONFIRMED working 2026-07-18/19. Anonymous read with the public web API key;
no OAuth. If this ever starts returning 403, re-run the recon tooling to
recover a fresh key / doc path (see recon/ and README).
"""
from __future__ import annotations

import json
from typing import Any

from . import common

PROJECT = "waitwhile-app"
API_KEY = "AIzaSyCIyFv4AQyk0g8pFAdC26eGgV3J-IQAlJs"
LOCATION_ID = "o0Sz5GVh6nIrQet8Ifbi"
QR_PAGE = f"https://waitwhile.com/locations/{LOCATION_ID}?qr=true"
_SIGNUP_URL = (
    f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={API_KEY}"
)

# Fields we promote to columns / summaries. Drop the mask to fetch everything.
KEY_FIELDS = [
    "isOpen", "isForceClosed", "isWaitlistOpen", "isWaitlistFull",
    "isServingFull", "numWaiting", "numWaitingGuests", "numWaitingByPartySize",
    "numServing", "numServingGuests", "wait", "waitByPartySize",
    "naEstWaitReason", "nextTicket", "numServers", "maxPartySize",
    "updated", "countersUpdated", "lastActive",
]


def _endpoint(mask: list[str] | None) -> str:
    base = (
        f"https://firestore.googleapis.com/v1/projects/{PROJECT}"
        f"/databases/(default)/documents/location-status/{LOCATION_ID}"
        f"?key={API_KEY}"
    )
    if mask:
        base += "".join(f"&mask.fieldPaths={f}" for f in mask)
    return base


def _anon_token() -> str | None:
    """Mint an anonymous Firebase token (fallback if plain-key GET is refused)."""
    status, body = common.http_post_form(
        _SIGNUP_URL, {"returnSecureToken": "true"}
    )
    if status != 200:
        return None
    try:
        return json.loads(body.decode("utf-8")).get("idToken")
    except (ValueError, KeyError):
        return None


def fetch(full: bool = False) -> dict[str, Any]:
    """Fetch and decode the lounge status document.

    Returns a dict of plain-Python fields plus derived keys: `state` and
    `waitMin`. `full=True` drops the field mask to return all ~60 fields.

    Tries the anonymous plain-key GET first (confirmed working). If Firestore
    tightens rules and returns 401/403, transparently retries once with an
    anonymous Firebase token.
    """
    url = _endpoint(None if full else KEY_FIELDS)
    status, raw = common.http_get(url)
    if status in (401, 403):
        token = _anon_token()
        if token:
            status, raw = common.http_get(
                url, headers={"Authorization": f"Bearer {token}"}
            )
    if status != 200:
        raise RuntimeError(f"lounge fetch -> HTTP {status}: {raw[:200]!r}")

    doc = json.loads(raw.decode("utf-8"))
    fields = common.firestore_doc_fields(doc)
    fields["state"] = derive_state(fields)
    fields["waitMin"] = wait_minutes(fields.get("wait"))
    fields["_docUpdateTime"] = doc.get("updateTime")
    return fields


def derive_state(f: dict) -> str:
    """Human-readable lounge state from the boolean/counter fields."""
    if f.get("isForceClosed") or not f.get("isOpen", True):
        return "CLOSED"
    if f.get("isWaitlistFull"):
        return "FULL"
    if f.get("isWaitlistOpen") and (f.get("numWaiting") or 0) > 0:
        return "WAITLIST"
    if f.get("isWaitlistOpen"):
        return "OPEN/list-on"
    return "OPEN/walk-in"


def wait_minutes(wait_seconds: Any) -> int | None:
    """Quoted estimated wait in minutes; None when n/a (-1)."""
    if wait_seconds is None:
        return None
    try:
        w = int(wait_seconds)
    except (TypeError, ValueError):
        return None
    return None if w < 0 else round(w / 60)


def hint(state: str) -> str:
    """A 'what should I do' nudge derived purely from state."""
    return {
        "CLOSED": "don't bother - closed",
        "FULL": "don't bother - at capacity",
        "WAITLIST": "join the list now",
        "OPEN/list-on": "walk up (list running, no queue)",
        "OPEN/walk-in": "walk in",
    }.get(state, state)


def summarize(f: dict) -> str:
    """One-line human readout for the lounge."""
    state = f.get("state", "?")
    wm = f.get("waitMin")
    wait_txt = f"~{wm}m wait" if wm is not None else "wait n/a"
    ahead = f.get("numWaiting") or 0
    guests = f.get("numWaitingGuests") or 0
    serving = f.get("numServing") or 0
    reason = f.get("naEstWaitReason")
    extra = ""
    if state == "CLOSED" and reason:
        extra = f" [{reason}]"
    return (
        f"Club SFO: {state} - {wait_txt}, {ahead} ahead "
        f"({guests} guests), {serving} serving - {hint(state)}{extra}"
    )
