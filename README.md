# SFO busyness estimator + Club SFO lounge monitor

A personal, read-only tool that reports, at any moment:

- **How busy SFO is** — a composite 0–100 score from security waits, fog/ceiling
  risk, FAA ground programs, scheduled departures, and (optionally) drive time.
- **The Club SFO lounge** — live state, estimated wait, and queue depth, from the
  same Waitwhile feed a walk-in's phone loads.

Runs as a one-shot (`python -m sfo`) or a logging poller (`--interval`). Stdlib
only at runtime — no pip installs needed for the confirmed-working signals.

Illustrative busy-morning output (the lounge counters are only nonzero when it's
open and running its waitlist):

```
$ python -m sfo --terminal T1
[2026-07-19T08:12:00-07:00]
SFO right now: 61.4/100 (busy)
  Security [T1]: avg ~6m, worst ~12m general, busiest Checkpoint B 12m
  Weather: IFR, 900ft ceiling, 10SM vis (wind 280@8kt)
  FAA: no SFO ground program (normal ops)
  Departures from T1: 16 scheduled in next 90m (2 delayed, 3 cancelled)
  (missing: drive)
Club SFO: WAITLIST - ~15m wait, 8 ahead (19 guests), 42 serving - join the list now
```

## Quick start

```bash
python -m sfo                    # one-shot combined readout
python -m sfo --lounge-only      # just the lounge (no airport signals)
python -m sfo --terminal T1      # scope the security line to your terminal
python -m sfo --interval 180     # poll every 180s, log to club_sfo.db
python -m sfo --json             # machine-readable bundle
python waitwhile_poll.py --once  # standalone lounge-only poller
```

Requires Python 3.11+ (for `tomllib`; everything else works on 3.9+). No
third-party packages for normal operation.

## Signals & sources (verified 2026-07-18/19)

| Signal | Source | Status | Notes |
|---|---|---|---|
| **Lounge** | Waitwhile → Firestore (anonymous) | ✅ confirmed | `sfo/lounge.py`. Self-heals via anon token if the plain-key GET is ever refused. |
| **Security waits** | flysfo.com SSR checkpoint table | ✅ confirmed | `sfo/security.py`. Replaces the **dead** TSA MyTSA feed (now 302s to tsa.gov). Per-checkpoint General + PreCheck. |
| **Fog / ceiling** | aviationweather.gov METAR (KSFO) | ✅ confirmed | `sfo/metar.py`. Ceiling + flight category — SFO's leading delay indicator. |
| **FAA ground programs** | nasstatus.faa.gov ASWS XML | ✅ confirmed | `sfo/faa.py`. National feed filtered to SFO. Ground-stop/GDP parsing is validated structurally; confirm field mapping during an *active* SFO event. |
| **Scheduled departures** | flysfo flight board JSON | ✅ confirmed | `sfo/departures.py`. Keyless `/flysfo/api/flight-status`. Forward, per-terminal, with live status. Replaces OpenSky (which needed OAuth and only gave *past* movements). |
| **SEA flights** | Port of Seattle flight widget | ✅ confirmed | `sfo/seatac.py`. Keyless SSR `/pos/flights`, arrivals *and* departures. Scheduled + live-revised times, gate, baggage claim. Feeds the To Seattle card and the SEA tab's To SFO returns. PAE has no feed. |
| **SEA security** | Port of Seattle checkpoint API | ✅ confirmed | `sfo/seatac.py`. Keyless JSON `/api/cwt/wait-times`: per-checkpoint wait, queue length, lane types (PreCheck etc.), with the API's own staleness flags honored. Drives the SEA tab's line picker. |
| **Drive time** | Google Routes API | ⚙️ optional | `sfo/drive.py`. Needs a key + origin — inert until configured. |

Missing signals are dropped and the composite weights renormalize over whatever
was actually readable, so the headline is always honest about its inputs.

### Composite weights (`sfo/score.py`)

`security 35% · fog 20% · departures 20% · GDP 15% · drive 10%` — starting
points; tune against logged history. The **lounge is tracked separately** — it's
a different question from "how hard is it to get through the airport."

The departures feed has no server-side filter — it's the whole day board — but
it's served **gzipped: ~500 KB over the wire** (11 MB decompressed), and
`http_get` requests + decompresses gzip transparently for every source.
`sfo/departures.py` then caches the board to `flysfo_board.json` and computes
windows locally, so extra polls inside the TTL cost **zero bytes**. It dedups
codeshares by (time, destination, gate) — one physical flight is listed under
every marketing code, so raw counts run ~3× high — and keeps
`flight_nature == "PAX"`.

Bandwidth knobs:

- `--board-ttl <sec>` — how long to reuse the cached board (default 1200 = 20
  min → ~500 KB × 72 = **~36 MB/day**). `--board-ttl 1800` (30 min) → ~25 MB/day.
  A schedule 90 min out barely moves, so long TTLs are safe.
- `--cache-dir <path>` — put the cache on a persistent path (e.g. `/var/lib/sfo`).
- `--lounge-only` — the lounge feed is a few KB; poll *it* often and refresh the
  airport board lazily. The two cadences are independent.

## Configuration

Nothing is required for the five confirmed signals (lounge, security, fog,
FAA, departures — all keyless). Only the optional drive-time signal needs a
credential. Copy `config.example.toml` → `config.toml` and fill it in, or use
`SFO_*` env vars (which win over the file — handy for systemd secrets):

```
SFO_GOOGLE_ROUTES_API_KEY
SFO_DRIVE_ORIGIN                 # "lat,lng" or an address
```

## Data logging (`sfo/store.py`)

SQLite, default `club_sfo.db`, two tables:

- `status` — the lounge time series (schema-compatible with the original
  poller: promoted columns + a `raw` JSON blob of the full decoded doc).
- `airport` — the composite score, band, and per-component sub-scores + `raw`.

Enough to reconstruct each day's curve and later learn a per-signal baseline so
"busy" becomes relative to *this* airport's own rhythm.

## Online dashboard (GitHub Pages — no server)

The hosted setup lives in `docs/` + `.github/workflows/update.yml`:

- **`docs/index.html`** — a static page. The browser itself polls the Waitwhile
  Firestore doc every 60s (the endpoint sends open CORS headers and the key is
  Waitwhile's own public web key), so the **lounge readout is live** with no
  backend. Background tabs pause polling after the first fetch.
- **`docs/data.json`** — regenerated every ~10 min by a GitHub Actions cron
  running `python -m sfo.publish --dir docs --terminal T1` (stdlib only, no pip
  step). Carries the composite, per-signal summaries, and a rolling 7-day
  history for the sparklines. History lives inside the JSON — no database in
  the repo.

  The published dashboard is **scoped to T1** (Harvey Milk — where Club SFO
  is): security scores the *best* General line among T1's checkpoints (you'll
  join the shorter queue; airport-wide mode scores the busiest line as a
  congestion indicator), and departures count T1 only. Fog and ground-delay
  stay airport-wide — they're runway-level. Drop the `--terminal` flag in the
  workflow for an airport-wide page.

Setup, one time:

1. Create a **public** repo on GitHub (public matters: Actions minutes are
   free/unlimited there, whereas ~72 short runs/day would eat most of a private
   repo's 2,000-minute monthly quota). Then:
   ```bash
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
2. **Settings → Pages** → Source: *Deploy from a branch* → `main` / `docs`.
3. **Settings → Actions → General** → Workflow permissions → *Read and write*
   (the cron commits the refreshed `data.json` back to the repo).
4. **Actions** tab → *update-data* → **Run workflow** to seed it immediately,
   rather than waiting for the first cron tick.

No secrets or API keys are required — every wired source is keyless.
The page lands at `https://<you>.github.io/<repo>/`.

Caveats: scheduled workflows are best-effort (runs can lag a few minutes) and
GitHub disables schedules after ~60 days without repo activity — the cron's own
commits normally keep it alive. The page flags airport data older than 45 min
as stale.

## Local / VPS deployment (alternative)

The pollers are pure stdlib HTTP — no browser — so they also run anywhere as a
loop or under systemd: see `deploy/` for a unit + timer that regenerate a
static `--html` dashboard on a box you own. On Windows everything runs as-is.

## Re-discovery (only if the lounge feed breaks)

The runtime already falls back to an anonymous Firebase token on 401/403. If the
key/path is rotated entirely, recover them:

```bash
pip install playwright && playwright install chromium   # local only
python recon/waitwhile_recon.py --headed                # capture.har
python recon/waitwhile_extract.py capture.har           # recover key + path
```

## Ethics

Everything here is read-only against public endpoints serving public data. No
auth bypass, no writes, no waitlist joins. Lounge polling stays ≥60s (the same
document any walk-in loads). Keep it that way.
