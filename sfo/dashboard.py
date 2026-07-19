"""Render a self-contained HTML dashboard from a live bundle + SQLite history.

Design: a split-flap departure-board vernacular -- monospace data, amber accent,
cool night-tarmac slate -- but pitched as a scan-and-operate ops panel, not a
document. Summary first (composite score + verdict), then the signal breakdown,
the lounge, and trend sparklines.

The output is one static HTML string with everything inlined (no external CSS,
JS, fonts, or images), so it can be written to a file and served from anywhere.
"""
from __future__ import annotations

import html
from typing import Any

from .score import band

# Semantic band scale (0..100 busyness). Hues legible on both grounds.
_BANDS = [
    (20, "quiet", "#2fa76a"),
    (40, "light", "#7aa63c"),
    (60, "moderate", "#d99a2b"),
    (80, "busy", "#e2792e"),
    (101, "rough", "#d64545"),
]
_MUTED = "#8b93a1"

_LOUNGE_COLORS = {
    "CLOSED": _MUTED,
    "FULL": "#d64545",
    "WAITLIST": "#d99a2b",
    "OPEN/list-on": "#2fa76a",
    "OPEN/walk-in": "#2fa76a",
}


def _band_color(v: float | None) -> str:
    if v is None:
        return _MUTED
    for hi, _name, color in _BANDS:
        if v < hi:
            return color
    return "#d64545"


def _esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


# --------------------------------------------------------------------------- #
# Fragments
# --------------------------------------------------------------------------- #
_WT_TIP = ("Weight: this signal's share of the headline score. Unavailable "
           "signals are dropped and the remaining weights renormalize.")
_VAL_TIP = ("This signal now, on a 0 (clear) to 100 (rough) scale. The grey "
            "line below shows the raw reading it was computed from.")


def _signal_bar(label: str, value: float | None, weight: float | None,
                summary: str) -> str:
    color = _band_color(value)
    pct = 0 if value is None else max(2, min(100, value))
    val_txt = "n/a" if value is None else f"{round(value)}"
    val_tip = ("No data right now - not counted in the headline score"
               if value is None else _VAL_TIP)
    wt_txt = f"wt {round(weight * 100)}%" if weight else "&mdash;"
    dim = ' data-dim="1"' if value is None else ""
    return (
        f'<div class="sig"{dim}>'
        f'<div class="sig-head">'
        f'<span class="sig-label">{_esc(label)}</span>'
        f'<span class="sig-weight" title="{_esc(_WT_TIP)}">{wt_txt}</span>'
        f'<span class="sig-val" style="color:{color}" title="{_esc(val_tip)}">'
        f'{val_txt}</span>'
        f'</div>'
        f'<div class="track"><div class="fill" style="width:{pct}%;'
        f'background:{color}"></div></div>'
        f'<div class="sig-sum">{_esc(summary)}</div>'
        f'</div>'
    )


def _sparkline(values: list[float], w: int = 260, h: int = 44,
               color: str = "#f5a524") -> str:
    pts = [v for v in values if v is not None]
    if len(pts) < 2:
        return '<div class="spark-empty">not enough history yet</div>'
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1
    n = len(pts)
    def x(i): return round(i / (n - 1) * (w - 4) + 2, 1)
    def y(v): return round(h - 4 - (v - lo) / span * (h - 12) + 2, 1)
    line = " ".join(f"{x(i)},{y(v)}" for i, v in enumerate(pts))
    area = f"2,{h-2} " + line + f" {w-2},{h-2}"
    ex, ey = x(n - 1), y(pts[-1])
    return (
        f'<svg viewBox="0 0 {w} {h}" class="spark" preserveAspectRatio="none" '
        f'role="img" aria-label="trend">'
        f'<polyline points="{area}" fill="{color}" fill-opacity="0.10" '
        f'stroke="none"/>'
        f'<polyline points="{line}" fill="none" stroke="{color}" '
        f'stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{ex}" cy="{ey}" r="2.6" fill="{color}"/>'
        f'</svg>'
    )


def _delay_bar_row(label: str, st: dict, gmax: int, extra: str) -> str:
    """One gradient-bar row: flights sorted by delay, colored green->red."""
    arr = (st or {}).get("delays_sorted") or []
    meta = (f'{st["n"]} flights' if st.get("n") else "") + \
        (f' &middot; {extra}' if extra else "")
    if not arr:
        return (f'<div class="dbar-row"><div class="dbar-label">{_esc(label)}'
                f'</div><div class="dbar-empty">no data</div>'
                f'<div class="dbar-meta">{meta}</div></div>')

    n = len(arr)

    # Green is RESERVED for zero delay; any lateness ramps yellow -> red on
    # the shared 0..max scale. A duplicate-offset stop makes a hard
    # green/yellow cut, so the green span reads directly as the on-time share.
    def _color(v: float) -> str:
        if v <= 0:
            return "hsl(120,60%,40%)"
        t = min(1.0, v / gmax) if gmax else 0.0
        return f"hsl({round(60 * (1 - t))},78%,45%)"

    stops = []
    for i, v in enumerate(arr):
        off = 0.0 if n == 1 else i / (n - 1) * 100
        if i > 0 and arr[i - 1] <= 0 and v > 0:
            stops.append(f"{_color(0)} {off:.1f}%")
        stops.append(f"{_color(v)} {off:.1f}%")
    if n == 1:
        stops.append(stops[0].replace(" 0.0%", " 100%"))
    # Median tick marks the median delay AMONG LATE flights (>=15m), placed at
    # that flight's true rank in the sorted bar -- not the bar's center.
    thresh = 15  # keep in sync with departures.DELAY_THRESHOLD_MIN
    late_count = sum(1 for v in arr if v >= thresh)
    med_late = (st or {}).get("median_delay_min")
    med_html = ""
    if late_count and med_late is not None:
        first_late = n - late_count
        med_rank = first_late + (late_count - 1) / 2
        med_pos = med_rank / (n - 1) * 100 if n > 1 else 50
        lab_pos = max(9.0, min(91.0, med_pos))
        med_html = (
            f'<div class="dtick" style="left:{med_pos:.1f}%"></div>'
            f'<div class="dmed" style="left:{lab_pos:.1f}%">med {med_late}m</div>')
    labels = "".join(f"<span>{k} {v}m</span>"
                     for k, v in (("min", arr[0]), ("max", arr[-1])))
    return (
        f'<div class="dbar-row"><div class="dbar-label">{_esc(label)}</div>'
        f'<div class="dbar-wrap"><div class="dbar" style="background:'
        f'linear-gradient(to right,{",".join(stops)})"></div>{med_html}'
        f'<div class="dlabels">{labels}</div></div>'
        f'<div class="dbar-meta">{meta}</div></div>')


def _lounge_card(lng: dict) -> str:
    if lng.get("ok") is False:
        return ('<div class="card lounge"><div class="card-title">Club SFO</div>'
                f'<div class="unavail">unavailable &mdash; {_esc(lng.get("error"))}'
                '</div></div>')
    state = lng.get("state", "?")
    color = _LOUNGE_COLORS.get(state, _MUTED)
    wm = lng.get("waitMin")
    wait_txt = f"{wm} min" if wm is not None else "&mdash;"
    ahead = lng.get("numWaiting") or 0
    guests = lng.get("numWaitingGuests") or 0
    serving = lng.get("numServing") or 0
    from .lounge import hint
    return (
        '<div class="card lounge">'
        '<div class="card-title">Club SFO <span class="dim">&middot; T1 '
        'Priority Pass</span></div>'
        f'<div class="lounge-state"><span class="pill" style="--pill:{color}">'
        f'{_esc(state)}</span><span class="lounge-hint">{_esc(hint(state))}'
        '</span></div>'
        '<div class="metrics">'
        f'<div class="metric"><div class="m-val">{wait_txt}</div>'
        '<div class="m-lab">est wait</div></div>'
        f'<div class="metric"><div class="m-val">{ahead}</div>'
        '<div class="m-lab">parties ahead</div></div>'
        f'<div class="metric"><div class="m-val">{guests}</div>'
        '<div class="m-lab">guests waiting</div></div>'
        f'<div class="metric"><div class="m-val">{serving}</div>'
        '<div class="m-lab">now serving</div></div>'
        '</div></div>'
    )


def _verdict(score: float | None, lng: dict) -> str:
    b = band(score)
    airport = {
        "quiet": "Smooth sailing at SFO right now.",
        "light": "SFO is moving well.",
        "moderate": "SFO is filling up &mdash; leave a little early.",
        "busy": "SFO is busy &mdash; give yourself extra time.",
        "rough": "SFO is rough right now &mdash; pad your schedule.",
        "unknown": "SFO status is partial right now.",
    }.get(b, "")
    state = lng.get("state")
    tail = ""
    if state and lng.get("ok") is not False:
        tail = {
            "FULL": " The lounge is at capacity.",
            "WAITLIST": " The lounge has a waitlist running.",
            "CLOSED": " The lounge is closed.",
            "OPEN/walk-in": " The lounge is open for walk-ins.",
            "OPEN/list-on": " The lounge is open (list running, no queue).",
        }.get(state, "")
    return airport + tail


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
_CSS = """
:root{
  --bg:#f6f7f9; --surface:#ffffff; --ink:#1a1f2b; --muted:#5b6474;
  --hair:#e4e8ee; --accent:#e0920c; --shadow:0 1px 2px rgba(20,28,45,.06),
   0 8px 24px rgba(20,28,45,.05);
}
@media (prefers-color-scheme:dark){
  :root{ --bg:#0e1116; --surface:#171b22; --ink:#e8ecf2; --muted:#9aa4b2;
    --hair:#262c36; --accent:#f5a524; --shadow:0 1px 2px rgba(0,0,0,.4); }
}
:root[data-theme="light"]{ --bg:#f6f7f9; --surface:#ffffff; --ink:#1a1f2b;
  --muted:#5b6474; --hair:#e4e8ee; --accent:#e0920c;
  --shadow:0 1px 2px rgba(20,28,45,.06),0 8px 24px rgba(20,28,45,.05); }
:root[data-theme="dark"]{ --bg:#0e1116; --surface:#171b22; --ink:#e8ecf2;
  --muted:#9aa4b2; --hair:#262c36; --accent:#f5a524;
  --shadow:0 1px 2px rgba(0,0,0,.4); }

*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--ink);
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  line-height:1.5;-webkit-font-smoothing:antialiased;}
.mono{font-family:ui-monospace,"SFMono-Regular","Cascadia Code",Menlo,Consolas,
  monospace;font-variant-numeric:tabular-nums;}
.wrap{max-width:900px;margin:0 auto;padding:28px 20px 48px;}

.top{display:flex;align-items:baseline;justify-content:space-between;gap:12px;
  border-bottom:1px solid var(--hair);padding-bottom:14px;margin-bottom:22px;}
.brand{font-family:ui-monospace,monospace;font-weight:600;letter-spacing:.14em;
  text-transform:uppercase;font-size:13px;color:var(--muted);}
.brand b{color:var(--ink);}
.brand .dot{color:var(--accent);}
.fresh{font-size:12px;color:var(--muted);text-align:right;}
.fresh .mono{color:var(--ink);}

.hero{display:grid;grid-template-columns:auto 1fr;gap:22px;align-items:center;
  background:var(--surface);border:1px solid var(--hair);border-radius:16px;
  padding:22px 24px;box-shadow:var(--shadow);margin-bottom:18px;}
.score{font-family:ui-monospace,monospace;font-variant-numeric:tabular-nums;
  font-size:64px;line-height:.9;font-weight:600;letter-spacing:-.02em;}
.score small{font-size:20px;color:var(--muted);font-weight:500;}
.hero-body{display:flex;flex-direction:column;gap:8px;}
.bandpill{align-self:flex-start;font-family:ui-monospace,monospace;
  text-transform:uppercase;letter-spacing:.12em;font-size:12px;font-weight:600;
  color:#fff;padding:3px 10px;border-radius:999px;}
.verdict{font-size:17px;text-wrap:balance;max-width:46ch;}
.delayscard{margin-bottom:18px;}
.dbar-row{display:grid;grid-template-columns:120px 1fr 82px;gap:12px;
  align-items:start;margin-bottom:16px;}
.dbar-row:last-child{margin-bottom:4px;}
@media(max-width:720px){.dbar-row{grid-template-columns:96px 1fr 60px;}}
.dbar-label{font-family:ui-monospace,monospace;font-size:12px;font-weight:600;
  padding-top:3px;}
.dbar-wrap{position:relative;padding-top:17px;}
.dbar{height:14px;border-radius:4px;}
.dmed{position:absolute;top:0;transform:translateX(-50%);white-space:nowrap;
  font-family:ui-monospace,monospace;font-variant-numeric:tabular-nums;
  font-size:11px;font-weight:600;color:var(--ink);}
.dtick{position:absolute;top:15px;width:2px;height:18px;background:var(--ink);
  opacity:.6;border-radius:1px;transform:translateX(-1px);}
.dlabels{display:flex;justify-content:space-between;
  font-family:ui-monospace,monospace;font-variant-numeric:tabular-nums;
  font-size:11px;color:var(--muted);margin-top:4px;}
.dbar-meta{font-family:ui-monospace,monospace;font-size:11px;
  color:var(--muted);text-align:right;padding-top:3px;}
.dbar-empty{font-size:12px;color:var(--muted);padding-top:3px;}
.missing{font-size:12px;color:var(--muted);}

.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:18px;}
@media(max-width:720px){.grid{grid-template-columns:1fr}.hero{grid-template-columns:1fr}}
.card{background:var(--surface);border:1px solid var(--hair);border-radius:16px;
  padding:18px 20px;box-shadow:var(--shadow);}
.card-title{font-family:ui-monospace,monospace;text-transform:uppercase;
  letter-spacing:.12em;font-size:12px;color:var(--muted);margin-bottom:14px;}
.card-title .dim,.dim{color:var(--muted);letter-spacing:0;text-transform:none;}

.legend{font-size:11px;color:var(--muted);margin:-8px 0 14px;}
.legend b{color:var(--ink);font-weight:600;}
.sig{margin-bottom:15px;}
.sig:last-child{margin-bottom:0;}
.sig[data-dim="1"]{opacity:.5;}
.sig-head{display:flex;align-items:baseline;gap:8px;margin-bottom:5px;}
.sig-label{font-family:ui-monospace,monospace;text-transform:capitalize;
  font-size:13px;font-weight:600;}
.sig-weight{font-family:ui-monospace,monospace;font-size:11px;color:var(--muted);}
.sig-val{margin-left:auto;font-family:ui-monospace,monospace;
  font-variant-numeric:tabular-nums;font-size:15px;font-weight:600;}
.track{height:6px;border-radius:4px;background:var(--hair);overflow:hidden;}
.fill{height:100%;border-radius:4px;transition:width .3s;}
.sig-sum{font-size:12px;color:var(--muted);margin-top:5px;}

.lounge-state{display:flex;align-items:center;gap:10px;margin-bottom:16px;}
.pill{font-family:ui-monospace,monospace;text-transform:uppercase;
  letter-spacing:.08em;font-size:12px;font-weight:600;color:#fff;
  background:var(--pill);padding:4px 11px;border-radius:999px;}
.lounge-hint{font-size:13px;color:var(--muted);}
.metrics{display:grid;grid-template-columns:1fr 1fr;gap:14px 10px;}
.metric{}
.m-val{font-family:ui-monospace,monospace;font-variant-numeric:tabular-nums;
  font-size:22px;font-weight:600;}
.m-lab{font-size:11px;color:var(--muted);text-transform:uppercase;
  letter-spacing:.06em;}
.unavail{color:var(--muted);font-size:14px;}

.trends{display:grid;grid-template-columns:1fr 1fr;gap:18px;}
@media(max-width:720px){.trends{grid-template-columns:1fr}}
.trend-title{display:flex;justify-content:space-between;align-items:baseline;
  font-family:ui-monospace,monospace;text-transform:uppercase;
  letter-spacing:.12em;font-size:12px;color:var(--muted);margin-bottom:10px;}
.trend-now{color:var(--ink);font-weight:600;}
.spark{width:100%;height:44px;display:block;}
.spark-empty{font-size:12px;color:var(--muted);padding:12px 0;}

.foot{margin-top:26px;padding-top:14px;border-top:1px solid var(--hair);
  font-size:11px;color:var(--muted);display:flex;flex-wrap:wrap;gap:6px 16px;}
.foot .mono{color:var(--ink);}
"""


def render_html(
    bundle: dict,
    airport_hist: list[dict] | None = None,
    lounge_hist: list[dict] | None = None,
    *,
    refresh_sec: int = 0,
    terminal: str | None = None,
    generated: str | None = None,
) -> str:
    airport_hist = airport_hist or []
    lounge_hist = lounge_hist or []
    comp = bundle.get("composite") or {}
    subs = bundle.get("subscores") or {}
    score = comp.get("score")
    lng = bundle.get("lounge") or {}

    # Signal rows (label, subscore-key, human summary from the module).
    from . import security, metar, faa, approach, drive
    rows = [
        ("security", subs.get("security"),
         security.summarize(bundle.get("security") or {}, terminal)),
        ("fog", subs.get("fog"), metar.summarize(bundle.get("weather") or {})),
        # Departures still scores into the composite, but is intentionally
        # not listed here -- the flight-delay bars carry the departure story.
        ("ground delay", subs.get("gdp"), faa.summarize(bundle.get("faa") or {})),
        ("approach", subs.get("approach"),
         approach.summarize(bundle.get("approach") or {})),
        ("drive", subs.get("drive"), drive.summarize(bundle.get("drive") or {})),
    ]
    from .score import WEIGHTS
    wmap = {"security": WEIGHTS["security"][0], "fog": WEIGHTS["fog"][0],
            "departures": WEIGHTS["departures"][0], "ground delay": WEIGHTS["gdp"][0],
            "approach": WEIGHTS["approach"][0], "drive": WEIGHTS["drive"][0]}
    bars = "".join(
        _signal_bar(lbl.replace("_", " ").title(), val, wmap.get(lbl), summ)
        for lbl, val, summ in rows
    )

    band_name = band(score)
    band_color = _band_color(score)
    score_txt = f'{round(score)}<small>/100</small>' if score is not None else "&mdash;"

    missing = comp.get("missing") or []
    missing_html = (f'<div class="missing">not counted: {_esc(", ".join(missing))} '
                    f'(weights renormalized)</div>' if missing else "")

    # Flight-delay gradient bars: each bucket's flights sorted by delay,
    # colored 0 -> shared max onto green -> red (see _delay_bar_row).
    dep_reading = bundle.get("departures") or {}
    dl = ((dep_reading.get("delays_by_terminal") or {}).get(terminal)
          if terminal else dep_reading.get("delays")) or {}
    dep_b = dl.get("departed") or {}
    up_b = dl.get("upcoming") or {}
    gmax = max([0] + (dep_b.get("delays_sorted") or [])
               + (up_b.get("delays_sorted") or []))
    delays_html = ""
    if dep_b.get("n") or up_b.get("n"):
        rows = (
            _delay_bar_row("took off last 2h", dep_b, gmax, "")
            + _delay_bar_row("next 3h (est)", up_b, gmax,
                             f'{up_b["cancelled"]} cancelled'
                             if up_b.get("cancelled") else "")
        )
        scope_txt = f"&middot; {_esc(terminal)} departures" if terminal \
            else "&middot; departures"
        delays_html = (
            f'<div class="card delayscard" title="Each bar: flights sorted by '
            f'delay, left (least) to right (most). Green is reserved for '
            f'on-time flights; any delay ramps yellow to red on a 0..max '
            f'scale shared between the bars. The tick marks the median delay '
            f'among late (>=15m) flights, at its true position. Took off = '
            f'actual vs schedule for the last 2 hours; next 3h = airline '
            f'estimates, which skew optimistic.">'
            f'<div class="card-title">Flight delays <span class="dim">'
            f'{scope_txt}</span></div>{rows}'
            f'<div class="legend">flights sorted by delay &middot; <b>green</b> '
            f'= on time &middot; delays <b>yellow</b> &rarr; <b>red</b> at max '
            f'(shared scale) &middot; tick marks median of late flights</div></div>')

    # Trends
    ap_scores = [r.get("score") for r in airport_hist]
    lounge_q = [r.get("numWaiting") for r in lounge_hist]
    ap_now = f'{round(score)}' if score is not None else "&mdash;"
    lq_now = lng.get("numWaiting")
    lq_now_txt = str(lq_now) if lq_now is not None else "&mdash;"
    trends = (
        '<div class="card"><div class="trend-title"><span>Airport score '
        f'&middot; last {len(ap_scores)}</span><span class="trend-now mono">'
        f'{ap_now}</span></div>{_sparkline(ap_scores, color=band_color)}</div>'
        '<div class="card"><div class="trend-title"><span>Lounge queue '
        f'&middot; last {len(lounge_q)}</span><span class="trend-now mono">'
        f'{lq_now_txt}</span></div>{_sparkline(lounge_q)}</div>'
    )

    fresh_lounge = lng.get("updated") or "&mdash;"
    fresh_board = (bundle.get("departures") or {}).get("board_updated") or "&mdash;"
    scope = f' &middot; {_esc(terminal)}' if terminal else ""
    refresh_meta = (f'<meta http-equiv="refresh" content="{refresh_sec}">'
                    if refresh_sec and refresh_sec > 0 else "")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh_meta}
<title>SFO Now{_esc(" - " + terminal) if terminal else ""}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="brand"><span class="dot">&#9679;</span> SFO <b>NOW</b>{scope}</div>
    <div class="fresh">generated <span class="mono">{_esc(generated or "")}</span>
      <br>lounge <span class="mono">{fresh_lounge}</span> &middot;
      board <span class="mono">{fresh_board}</span></div>
  </div>

  <div class="hero">
    <div class="score mono" style="color:{band_color}"
      title="Weighted blend of the signals below: 0 = frictionless, 100 = worst realistic day.">{score_txt}</div>
    <div class="hero-body">
      <span class="bandpill" style="background:{band_color}"
        title="Bands: quiet &lt;20 &middot; light &lt;40 &middot; moderate &lt;60 &middot; busy &lt;80 &middot; rough 80+">{_esc(band_name)}</span>
      <div class="verdict">{_verdict(score, lng)}</div>
      {missing_html}
    </div>
  </div>

  {delays_html}

  <div class="grid">
    <div class="card">
      <div class="card-title">Airport signals</div>
      <div class="legend">each scored <b>0</b> clear &rarr; <b>100</b> rough &middot;
        <b>wt</b> = share of the headline score</div>
      {bars}
    </div>
    {_lounge_card(lng)}
  </div>

  <div class="trends">{trends}</div>

  <div class="foot">
    <span>Composite is airport friction only; the lounge is tracked separately.</span>
    <span>Sources: flysfo &middot; NWS/AWC METAR &middot; FAA ASWS &middot; Waitwhile</span>
  </div>
</div>
</body>
</html>"""
