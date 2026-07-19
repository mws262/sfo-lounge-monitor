"""Publish a data.json snapshot for the static GitHub Pages dashboard.

    python -m sfo.publish --dir docs [--terminal T1]

Runs one gather() over all signals, then writes <dir>/data.json containing:
  * the composite score + per-signal sub-scores and summary strings
  * the latest server-side lounge reading (fallback for the browser's live poll)
  * a rolling history (score + lounge queue), appended each run and trimmed

History lives inside data.json itself -- no database file to commit -- which is
what makes this runnable from a fresh GitHub Actions checkout every 20 minutes.
The static page (docs/index.html) fetches this file and separately polls the
Waitwhile Firestore doc directly from the browser for a live lounge readout.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

from . import approach, departures, drive, faa, metar, security
from .cli import gather
from .config import Config
from .score import WEIGHTS, band

HISTORY_KEEP = 504  # 7 days at a 20-min cadence


def build_payload(bundle: dict, prev_history: list[dict],
                  terminal: str | None, keep: int) -> dict:
    subs = bundle.get("subscores") or {}
    comp = bundle.get("composite") or {}
    lng = bundle.get("lounge") or {}

    signals = [
        {"key": "security", "label": "Security", "weight": WEIGHTS["security"][0],
         "score": subs.get("security"),
         "summary": security.summarize(bundle.get("security") or {}, terminal)},
        {"key": "fog", "label": "Fog", "weight": WEIGHTS["fog"][0],
         "score": subs.get("fog"),
         "summary": metar.summarize(bundle.get("weather") or {})},
        {"key": "departures", "label": "Departures",
         "weight": WEIGHTS["departures"][0], "score": subs.get("departures"),
         "summary": departures.summarize(bundle.get("departures") or {}, terminal)},
        {"key": "gdp", "label": "Ground delay", "weight": WEIGHTS["gdp"][0],
         "score": subs.get("gdp"),
         "summary": faa.summarize(bundle.get("faa") or {})},
        {"key": "approach", "label": "Approach", "weight": WEIGHTS["approach"][0],
         "score": subs.get("approach"),
         "summary": approach.summarize(bundle.get("approach") or {})},
        {"key": "drive", "label": "Drive", "weight": WEIGHTS["drive"][0],
         "score": subs.get("drive"),
         "summary": drive.summarize(bundle.get("drive") or {})},
    ]

    dep = bundle.get("departures") or {}
    delays = ((dep.get("delays_by_terminal") or {}).get(terminal)
              if terminal else dep.get("delays")) or None

    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry = {
        "ts": now_utc,
        "score": comp.get("score"),
        "lounge_state": lng.get("state") if lng.get("ok") is not False else None,
        "lounge_waiting": lng.get("numWaiting") if lng.get("ok") is not False else None,
    }
    history = (prev_history + [entry])[-keep:]

    return {
        "generated": now_utc,
        "terminal": terminal,
        "score": comp.get("score"),
        "band": band(comp.get("score")),
        "missing": comp.get("missing") or [],
        "signals": signals,
        "delays": delays,  # scoped delay stats (n/pct/median/max/cancelled)
        "board_updated": (bundle.get("departures") or {}).get("board_updated"),
        "lounge": lng if lng.get("ok") is not False else {"error": lng.get("error")},
        "history": history,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dir", default="docs", help="output directory (Pages root)")
    p.add_argument("--terminal", default=None, help="scope, e.g. T1")
    p.add_argument("--config", default=None, help="TOML config path")
    p.add_argument("--keep", type=int, default=HISTORY_KEEP,
                   help="history entries to retain")
    args = p.parse_args(argv)

    out_path = os.path.join(args.dir, "data.json")
    prev_history: list[dict] = []
    if os.path.exists(out_path):
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                prev_history = (json.load(f).get("history") or [])
        except (OSError, ValueError):
            pass  # corrupt/absent history is not fatal; start fresh

    cfg = Config.load(args.config)
    bundle = gather(cfg, args.terminal, lounge_only=False,
                    cache_dir=args.dir)  # board cache sits next to data.json
    payload = build_payload(bundle, prev_history, args.terminal, args.keep)

    os.makedirs(args.dir, exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, default=str, separators=(",", ":"))
    os.replace(tmp, out_path)

    sc = payload["score"]
    print(f"wrote {out_path}: score={sc} band={payload['band']} "
          f"history={len(payload['history'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
