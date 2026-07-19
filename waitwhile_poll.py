#!/usr/bin/env python3
"""Standalone Club SFO lounge poller (the original 'keeper').

Preserves the original interface (--once / --interval / --db) while delegating
to the sfo package's lounge module + SQLite store, so there's a single source
of truth for the decode/summarize/logging logic.

    python waitwhile_poll.py --once
    python waitwhile_poll.py --interval 120 --db club_sfo.db

Read-only. Polite cadence enforced at >=60s. stdlib only.
"""
from __future__ import annotations

import argparse
import sys
import time

from sfo import common, lounge, store


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--once", action="store_true", help="poll once and exit")
    p.add_argument("--interval", type=int, default=120,
                   help="seconds between polls (min 60); ignored with --once")
    p.add_argument("--db", default=store.DEFAULT_DB, help="SQLite path")
    p.add_argument("--no-log", action="store_true", help="don't write SQLite")
    args = p.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    interval = max(60, args.interval)
    conn = None if args.no_log else store.connect(args.db)

    prev_ticket = None
    prev_state = None
    try:
        while True:
            try:
                f = lounge.fetch()
            except Exception as e:  # noqa: BLE001
                print(f"[{common.iso_local()}] fetch error: {e}", file=sys.stderr)
            else:
                note = ""
                tk, st = f.get("nextTicket"), f.get("state")
                if prev_ticket is not None and isinstance(tk, int) and tk > prev_ticket:
                    note += f"  (+{tk - prev_ticket} joined)"
                if prev_state is not None and st != prev_state:
                    note += f"  [state {prev_state} -> {st}]"
                prev_ticket = tk if isinstance(tk, int) else prev_ticket
                prev_state = st
                print(f"[{common.iso_local()}] {lounge.summarize(f)}{note}", flush=True)
                if conn is not None:
                    store.log_lounge(conn, f)

            if args.once:
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
