#!/usr/bin/env python3
"""Re-discovery: recover the Waitwhile Firebase key + Firestore doc path.

Diagnostic tool. Only needed if the live lounge endpoint starts 403-ing (key
rotation / tightened rules). Parses a HAR captured from the public QR page
(waitwhile_recon.py) to recover:
  * the Firebase web API key   (AIza[0-9A-Za-z_-]{35})
  * the Firestore document path (projects/waitwhile-app/.../location-status/<id>)
Then performs a live REST read to confirm, with an anonymous-token fallback.

    python recon/waitwhile_extract.py path/to/capture.har

stdlib only.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request

KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{35}")
DOCPATH_RE = re.compile(
    r"projects/[a-z0-9-]+/databases/\(default\)/documents/location-status/[A-Za-z0-9]+"
)


def _har_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _first(pattern: re.Pattern, text: str) -> str | None:
    m = pattern.search(text)
    return m.group(0) if m else None


def _rest_read(key: str, docpath: str) -> tuple[int, bytes]:
    url = f"https://firestore.googleapis.com/v1/{docpath}?key={key}"
    req = urllib.request.Request(url, headers={"User-Agent": "sfo-recon/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return e.code, e.read()


def _anon_token(key: str) -> str | None:
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={key}"
    body = json.dumps({"returnSecureToken": True}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()).get("idToken")
    except Exception:
        return None


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    text = _har_text(argv[1])
    key = _first(KEY_RE, text)
    docpath = _first(DOCPATH_RE, text)
    print(f"api key : {key}")
    print(f"doc path: {docpath}")
    if not (key and docpath):
        print("!! could not recover both -- inspect the HAR manually.")
        return 1

    status, raw = _rest_read(key, docpath)
    print(f"live REST read: HTTP {status} ({len(raw)} bytes)")
    if status in (401, 403):
        tok = _anon_token(key)
        if tok:
            url = f"https://firestore.googleapis.com/v1/{docpath}?key={key}"
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {tok}"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                print(f"anon-token read: HTTP {r.status} ({len(raw)} bytes)")
    if raw[:1] == b"{":
        doc = json.loads(raw)
        fields = list((doc.get("fields") or {}).keys())
        print(f"fields ({len(fields)}): {', '.join(fields[:20])}"
              + (" ..." if len(fields) > 20 else ""))
    return 0


if __name__ == "__main__":
    import urllib.error  # noqa: E402 (used lazily above)
    raise SystemExit(main(sys.argv))
