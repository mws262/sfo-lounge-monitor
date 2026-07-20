"""Command-line entry point.

    python -m sfo                     # one-shot combined readout
    python -m sfo --lounge-only       # just the lounge
    python -m sfo --terminal T1       # scope security to your terminal
    python -m sfo --interval 180      # poll every 180s, log to SQLite
    python -m sfo --json              # machine-readable

Every network source is fetched defensively: one dead endpoint degrades the
readout, it never crashes the run.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import common, departures, faa, lounge, seatac, security, store
from .config import Config
from .score import band, composite


def _safe(fn, *args, **kwargs):
    """Run a fetch, converting any exception into an unavailable reading."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 - defensive: keep the run alive
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def gather(
    cfg: Config,
    terminal: str | None,
    lounge_only: bool,
    board_ttl: int = departures.CACHE_TTL,
    cache_dir: str | None = None,
) -> dict:
    """Fetch every signal once. Returns a bundle of readings + scores."""
    lng = _safe(lounge.fetch)
    bundle: dict = {"lounge": lng}
    if lounge_only:
        return bundle

    sec = _safe(security.fetch)
    # One national pull covers both airports (the SEA tab rides along free).
    try:
        fa_all = faa.fetch_multi(("SFO", "SEA"))
    except Exception as e:  # noqa: BLE001 - defensive: keep the run alive
        fa_all = {a: {"ok": False, "error": f"{type(e).__name__}: {e}",
                      "events": []} for a in ("SFO", "SEA")}
    fa = fa_all["SFO"]
    dep = _safe(departures.fetch, cfg, cache_dir=cache_dir, cache_ttl=board_ttl)
    sea_sec = _safe(seatac.fetch_checkpoints)

    subscores = {
        "security": security.score(sec, terminal),
        "delays": departures.delay_score(dep, terminal),
        "departures": departures.score(dep, terminal),
        "gdp": faa.score(fa),
    }
    comp = composite(subscores)
    bundle.update({
        "security": sec, "faa": fa, "departures": dep,
        "sea_faa": fa_all["SEA"], "sea_security": sea_sec,
        "subscores": subscores, "composite": comp, "terminal": terminal,
    })
    return bundle


def render(bundle: dict, terminal: str | None) -> str:
    lines = []
    if "composite" in bundle:
        comp = bundle["composite"]
        sc = comp.get("score")
        head = f"SFO right now: {sc if sc is not None else '?'}/100 ({band(sc)})"
        lines.append(head)
        lines.append("  " + security.summarize(bundle["security"], terminal))
        lines.append("  " + departures.delay_signal_summary(bundle["departures"], terminal))
        lines.append("  " + faa.summarize(bundle["faa"]))
        dep = bundle["departures"]
        if dep.get("ok"):
            lines.append("  " + departures.summarize(dep, terminal))
        if comp.get("missing"):
            lines.append("  (missing: " + ", ".join(comp["missing"]) + ")")
    lng = bundle["lounge"]
    if lng.get("ok") is False:
        lines.append(f"Club SFO: unavailable ({lng.get('error')})")
    else:
        lines.append(lounge.summarize(lng))
    return "\n".join(lines)


def _write_dashboard(conn, bundle: dict, args) -> None:
    """Render the HTML dashboard atomically (temp file then replace)."""
    from . import dashboard, store as _store
    import os

    html = dashboard.render_html(
        bundle,
        airport_hist=_store.recent_airport(conn),
        lounge_hist=_store.recent_lounge(conn),
        refresh_sec=args.refresh,
        terminal=args.terminal,
        generated=common.iso_local(),
    )
    tmp = args.html + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, args.html)  # atomic: a served file never reads half-written


def _log(conn, bundle: dict) -> None:
    lng = bundle["lounge"]
    if lng.get("ok") is not False:
        store.log_lounge(conn, lng)
    if "composite" in bundle:
        store.log_airport(
            conn, bundle["composite"], bundle["subscores"],
            {k: bundle.get(k) for k in ("security", "faa", "departures")},
            delay_median=departures.median_departed_delay(
                bundle.get("departures") or {}, bundle.get("terminal")),
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sfo", description=__doc__)
    p.add_argument("--interval", type=int, default=0,
                   help="poll every N seconds (min 60); 0 = one-shot")
    p.add_argument("--db", default=store.DEFAULT_DB, help="SQLite path")
    p.add_argument("--config", default=None, help="TOML config path")
    p.add_argument("--terminal", default=None,
                   help="scope security + departures to a terminal, e.g. T1")
    p.add_argument("--board-ttl", type=int, default=departures.CACHE_TTL,
                   help="seconds to reuse the cached flight board (default "
                        f"{departures.CACHE_TTL}); higher = less bandwidth")
    p.add_argument("--cache-dir", default=None,
                   help="where to keep the cached flight board "
                        "(default: current dir)")
    p.add_argument("--lounge-only", action="store_true",
                   help="only the lounge feed (a few KB; no airport board)")
    p.add_argument("--html", default=None, metavar="PATH",
                   help="also render a self-contained HTML dashboard to PATH")
    p.add_argument("--refresh", type=int, default=0, metavar="SEC",
                   help="dashboard auto-refresh interval (adds a meta refresh)")
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument("--no-log", action="store_true",
                   help="don't write to SQLite (one-shot only)")
    args = p.parse_args(argv)

    # Windows consoles are cp1252; force UTF-8 so output never mojibakes.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    cfg = Config.load(args.config)
    interval = args.interval
    if 0 < interval < 60:
        interval = 60  # courtesy floor: same doc a walk-in's phone loads

    conn = None
    if interval or not args.no_log or args.html:
        conn = store.connect(args.db)  # html needs history reads too

    prev_ticket = None
    prev_state = None
    try:
        while True:
            bundle = gather(cfg, args.terminal, args.lounge_only,
                            board_ttl=args.board_ttl, cache_dir=args.cache_dir)
            lng = bundle["lounge"]

            # Join-rate + state-change annotations from nextTicket deltas.
            note = ""
            if lng.get("ok") is not False:
                tk = lng.get("nextTicket")
                st = lng.get("state")
                if prev_ticket is not None and isinstance(tk, int):
                    d = tk - prev_ticket
                    if d > 0:
                        note += f"  (+{d} joined)"
                if prev_state is not None and st != prev_state:
                    note += f"  [state {prev_state} -> {st}]"
                prev_ticket = tk if isinstance(tk, int) else prev_ticket
                prev_state = st

            if args.json:
                out = json.dumps(bundle, default=str)
            else:
                out = f"[{common.iso_local()}]\n" + render(bundle, args.terminal) + note
            print(out, flush=True)

            if conn is not None:
                _log(conn, bundle)

            if args.html and conn is not None and not args.lounge_only:
                _write_dashboard(conn, bundle, args)

            if not interval:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
    finally:
        if conn is not None:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
