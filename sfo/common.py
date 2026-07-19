"""Shared helpers: HTTP, Firestore decoding, config, normalization.

Stdlib only. Works on Windows and Linux (Hetzner VPS).
"""
from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from typing import Any

USER_AGENT = (
    "sfo-lounge-monitor/0.1 (personal, read-only; "
    "https://github.com/ (private))"
)

DEFAULT_TIMEOUT = 20


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _decode_body(resp) -> bytes:
    """Read a response, transparently gunzip/inflate per Content-Encoding.

    Big JSON boards (the flysfo flight feed) compress ~20x, so we always ask
    for gzip and decompress here. Falls back to raw bytes on any mismatch.
    """
    raw = resp.read()
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    try:
        if enc == "gzip":
            return gzip.decompress(raw)
        if enc == "deflate":
            return zlib.decompress(raw)
    except (OSError, zlib.error):
        return raw  # server mislabeled encoding; hand back what we got
    return raw


def http_get(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[int, bytes]:
    """GET a URL. Returns (status_code, decompressed_body). Raises on net error.

    Requests gzip and transparently decompresses, so callers always get plain
    bytes regardless of transfer encoding.
    """
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept-Encoding", "gzip")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _decode_body(resp)
    except urllib.error.HTTPError as e:  # 4xx/5xx still carry a body
        return e.code, _decode_body(e)


def http_get_json(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Any:
    status, body = http_get(url, headers=headers, timeout=timeout)
    if status != 200:
        raise RuntimeError(f"GET {url} -> HTTP {status}: {body[:200]!r}")
    return json.loads(body.decode("utf-8"))


def http_post_form(
    url: str,
    data: dict[str, str],
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[int, bytes]:
    """POST application/x-www-form-urlencoded."""
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# --------------------------------------------------------------------------- #
# Firestore typed-value decoding
# --------------------------------------------------------------------------- #
def firestore_decode(value: Any) -> Any:
    """Recursively decode a Firestore REST typed value into plain Python.

    Handles {"integerValue": "3"}, {"booleanValue": true}, mapValue,
    arrayValue, nullValue, timestampValue, doubleValue, stringValue, etc.
    """
    if not isinstance(value, dict):
        return value
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "booleanValue" in value:
        return bool(value["booleanValue"])
    if "stringValue" in value:
        return value["stringValue"]
    if "timestampValue" in value:
        return value["timestampValue"]
    if "nullValue" in value:
        return None
    if "mapValue" in value:
        fields = value["mapValue"].get("fields", {}) or {}
        return {k: firestore_decode(v) for k, v in fields.items()}
    if "arrayValue" in value:
        vals = value["arrayValue"].get("values", []) or []
        return [firestore_decode(v) for v in vals]
    if "bytesValue" in value:
        return value["bytesValue"]
    if "referenceValue" in value:
        return value["referenceValue"]
    if "geoPointValue" in value:
        return value["geoPointValue"]
    # A bare Firestore document: {"fields": {...}}
    if "fields" in value:
        return {k: firestore_decode(v) for k, v in value["fields"].items()}
    return value


def firestore_doc_fields(doc: dict) -> dict:
    """Decode the `fields` map of a Firestore document into plain Python."""
    return {k: firestore_decode(v) for k, v in (doc.get("fields") or {}).items()}


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
def iso_local() -> str:
    """Current time as ISO-8601 with the system's local UTC offset."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pacific_now() -> datetime:
    """Best-effort America/Los_Angeles now; falls back to system local."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:
        return datetime.now().astimezone()


# --------------------------------------------------------------------------- #
# Normalization helpers (0..100 where 100 == busiest / worst)
# --------------------------------------------------------------------------- #
def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def linscale(x: float, x0: float, x1: float) -> float:
    """Map x in [x0, x1] linearly to [0, 100], clamped."""
    if x1 == x0:
        return 0.0
    return clamp((x - x0) / (x1 - x0) * 100.0)
