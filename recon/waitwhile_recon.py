#!/usr/bin/env python3
"""Re-discovery: capture a HAR from the public Club SFO QR page.

Diagnostic tool, LOCAL-ONLY, not part of the deployed loop. Loads the Waitwhile
QR page in Chromium and records network traffic to a HAR, from which
waitwhile_extract.py recovers the Firebase key + Firestore doc path.

Requires Playwright:
    pip install playwright
    playwright install chromium

    python recon/waitwhile_recon.py            # headless
    python recon/waitwhile_recon.py --headed    # watch it

Writes capture.har next to this script (override with --out).
"""
from __future__ import annotations

import argparse
import sys

QR_PAGE = "https://waitwhile.com/locations/o0Sz5GVh6nIrQet8Ifbi?qr=true"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--headed", action="store_true", help="show the browser")
    p.add_argument("--out", default="capture.har", help="HAR output path")
    p.add_argument("--wait", type=float, default=8.0,
                   help="seconds to let the SPA load its Firestore stream")
    args = p.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        print("Playwright not installed. Run:\n"
              "  pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 2

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context = browser.new_context(record_har_path=args.out)
        page = context.new_page()
        page.goto(QR_PAGE, wait_until="networkidle")
        page.wait_for_timeout(int(args.wait * 1000))
        context.close()  # flushes the HAR
        browser.close()
    print(f"wrote {args.out} -- now run: python recon/waitwhile_extract.py {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
