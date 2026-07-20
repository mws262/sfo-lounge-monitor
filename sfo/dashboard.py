"""Render a self-contained HTML dashboard from a live bundle + SQLite history.

Design: a split-flap departure-board vernacular -- monospace data, amber accent,
cool night-tarmac slate -- but pitched as a scan-and-operate ops panel, not a
document. Flight-delay bars first, then plain airport stats (real numbers,
colored by severity -- the composite score is computed internally but not
displayed), the lounge, and trend sparklines.

The output is one static HTML string with everything inlined (no external CSS,
JS, fonts, or images), so it can be written to a file and served from anywhere.
"""
from __future__ import annotations

import html
from typing import Any

_MUTED = "#8b93a1"

_LOUNGE_COLORS = {
    "CLOSED": _MUTED,
    "FULL": "#d64545",
    "WAITLIST": "#d99a2b",
    "OPEN/list-on": "#2fa76a",
    "OPEN/walk-in": "#2fa76a",
}


def _delay_trend_color(minutes: int | None) -> str:
    """Delay severity for the trend line: 0m green -> 60m+ red."""
    if minutes is None:
        return "#f5a524"
    t = min(1.0, minutes / 60)
    return f"hsl({round(120 * (1 - t))},65%,45%)"


def _sev_color(score: float | None) -> str:
    """Continuous severity color from an internal 0..100 score: green -> red."""
    if score is None:
        return _MUTED
    t = min(1.0, max(0.0, score / 100))
    return f"hsl({round(120 * (1 - t))},65%,42%)"


def signal_stats(bundle: dict, terminal: str | None = None) -> dict[str, str]:
    """Plain display numbers for the stat rows (no composite involved)."""
    from . import security, faa as _faa
    sec = bundle.get("security") or {}
    fa = bundle.get("faa") or {}
    ap = bundle.get("approach") or {}
    dr = bundle.get("drive") or {}

    m = security.best_general_min(sec, terminal)
    sec_val = f"{m}m" if m is not None else "n/a"

    if not fa.get("ok"):
        faa_val = "n/a"
    elif fa.get("ground_stop"):
        faa_val = "STOP"
    elif fa.get("closure"):
        faa_val = "CLOSED"
    else:
        mm = _faa._max_delay_minutes(fa.get("events") or [])
        faa_val = f"{mm}m" if mm else "none"

    if ap.get("ok"):
        r = ap.get("worst_ratio")
        app_val = "clear" if (r or 1.0) < 1.15 else f"+{round((r - 1) * 100)}%"
    else:
        app_val = "n/a"

    drv_val = f"{dr.get('minutes')}m" if dr.get("ok") else "n/a"
    return {"security": sec_val, "gdp": faa_val,
            "approach": app_val, "drive": drv_val}


def _esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


# --------------------------------------------------------------------------- #
# Fragments
# --------------------------------------------------------------------------- #
_TREND_COLOR = {"up": "#d64545", "down": "#2fa76a"}
_TREND_ARROW = {"up": "&uarr;", "down": "&darr;"}


def _stat_row(label: str, score: float | None, value: str,
              summary: str = "", note: str = "",
              trend: dict | None = None) -> str:
    """Label + optional trend + plain colored number (no bar).

    Rising delays get a red up arrow, falling ones a green down arrow -- the
    direction of change, independent of the value's severity color.
    """
    color = _sev_color(score)
    tip = note or "Colored by severity: green = good, red = bad."
    dim = ' data-dim="1"' if value == "n/a" else ""
    trend_html = ""
    if trend:
        d = trend.get("dir", "")
        trend_html = (
            f'<span class="sig-trend" style="color:{_TREND_COLOR.get(d, _MUTED)}">'
            f'{_TREND_ARROW.get(d, "")} {_esc(trend.get("word", ""))}</span>')
    sum_html = f'<div class="sig-sum">{_esc(summary)}</div>' if summary else ""
    return (
        f'<div class="sig"{dim}>'
        f'<div class="sig-head">'
        f'<span class="sig-label">{_esc(label)}</span>'
        f'{trend_html}'
        f'<span class="sig-val" style="color:{color}" title="{_esc(tip)}">'
        f'{_esc(value)}</span>'
        f'</div>{sum_html}</div>'
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


# Join-planner constants (keep in sync with docs/index.html).
GRACE_MIN = 10   # minutes allowed to reach the lounge once called
WALK_MIN = 2     # walking, airport entrance -> security -> lounge door


def _join_planner(lng: dict, sec_wait_min: int | None) -> str:
    """'join ~Nm before you arrive' + waitlist link, while joins are open."""
    if not (lng.get("isWaitlistOpen") and not lng.get("isWaitlistFull")):
        return ""
    from .lounge import QR_PAGE
    wm = lng.get("waitMin")
    if wm is None:
        lead_html = "no wait estimate right now"
    elif sec_wait_min is None:
        lead_html = "security wait unknown - can't estimate"
    else:
        lead = wm - sec_wait_min - WALK_MIN
        if lead > 0:
            lead_html = f"join <b>~{lead}m</b> before you arrive at SFO"
        elif lead >= -GRACE_MIN:
            lead_html = "join when you arrive at SFO"
        else:
            lead_html = "short wait - join after clearing security"
    tip = (f"Timed so you're called about when you reach the lounge door: "
           f"quoted wait {'~' + str(wm) + 'm' if wm is not None else 'n/a'}, "
           f"entrance to door = security "
           f"{'~' + str(sec_wait_min) + 'm' if sec_wait_min is not None else '?'} "
           f"+ {WALK_MIN}m walk. The {GRACE_MIN}-min grace after being called "
           f"is kept as buffer in case the line moves faster than quoted.")
    return (f'<div class="planner" title="{_esc(tip)}">'
            f'<div class="planner-lead">{lead_html}</div>'
            f'<a href="{QR_PAGE}" target="_blank" rel="noopener">'
            f'open waitlist &#8599;</a></div>')


def _lounge_card(lng: dict, sec_wait_min: int | None = None) -> str:
    if lng.get("ok") is False:
        return ('<div class="card lounge"><div class="card-title">Club SFO</div>'
                f'<div class="unavail">unavailable &mdash; {_esc(lng.get("error"))}'
                '</div></div>')
    state = lng.get("state", "?")
    color = _LOUNGE_COLORS.get(state, _MUTED)
    wm = lng.get("waitMin")
    wait_txt = f"{wm} min" if wm is not None else "&mdash;"
    from .lounge import hint

    metrics = [
        (wait_txt, "est wait", ""),
        (lng.get("numWaiting") or 0, "parties ahead", ""),
        (lng.get("numWaitingGuests") or 0, "guests waiting", ""),
    ]
    # "Now serving" counts parties called off the list and checked in (the
    # lounge labels this state "Arrived"). Walk-ins never enter the system, so
    # outside an active waitlist it reads 0 for a room that may well be full --
    # misleading, so hide it entirely then.
    if lng.get("isWaitlistOpen") or lng.get("isWaitlistFull"):
        metrics.append((
            lng.get("numServing") or 0, "now serving",
            "Parties called off the waitlist and checked in at the door (the "
            "lounge's own label is \"Arrived\") -- not a headcount of the "
            "lounge, since walk-ins never enter the waitlist system."))
    def _metric(val, lab: str, tip: str) -> str:
        title = f' title="{_esc(tip)}"' if tip else ""
        return (f'<div class="metric"{title}><div class="m-val">{val}</div>'
                f'<div class="m-lab">{lab}</div></div>')

    met_html = "".join(_metric(*m) for m in metrics)
    return (
        '<div class="card lounge">'
        '<div class="card-title">Club SFO <span class="dim">&middot; T1 '
        'Priority Pass</span></div>'
        f'<div class="lounge-state"><span class="pill" style="--pill:{color}">'
        f'{_esc(state)}</span><span class="lounge-hint">{_esc(hint(state))}'
        '</span></div>'
        f'{_join_planner(lng, sec_wait_min)}'
        f'<div class="metrics">{met_html}</div></div>'
    )


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

.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:18px;}
@media(max-width:720px){.grid{grid-template-columns:1fr}}
.card{background:var(--surface);border:1px solid var(--hair);border-radius:16px;
  padding:18px 20px;box-shadow:var(--shadow);}
.card-title{font-family:ui-monospace,monospace;text-transform:uppercase;
  letter-spacing:.1em;font-size:15px;font-weight:600;color:var(--ink);
  margin-bottom:14px;}
.card-title .dim,.dim{color:var(--muted);letter-spacing:0;text-transform:none;}

.legend{font-size:11px;color:var(--muted);margin:-8px 0 14px;}
.legend b{color:var(--ink);font-weight:600;}
.sig{margin-bottom:16px;}
.sig:last-child{margin-bottom:0;}
.sig[data-dim="1"]{opacity:.5;}
.sig-head{display:flex;align-items:baseline;gap:10px;}
.sig-label{font-family:ui-monospace,monospace;font-size:13px;font-weight:600;}
/* Two auto margins split the free space: label | trend | value. */
.sig-trend{margin-left:auto;font-family:ui-monospace,monospace;font-size:12px;
  font-weight:600;white-space:nowrap;}
.sig-val{margin-left:auto;font-family:ui-monospace,monospace;
  font-variant-numeric:tabular-nums;font-size:19px;font-weight:700;}
.sig-sum{font-size:12px;color:var(--muted);margin-top:3px;}

.lounge-state{display:flex;align-items:center;gap:10px;margin-bottom:16px;}
.pill{font-family:ui-monospace,monospace;text-transform:uppercase;
  letter-spacing:.08em;font-size:12px;font-weight:600;color:#fff;
  background:var(--pill);padding:4px 11px;border-radius:999px;}
.lounge-hint{font-size:13px;color:var(--muted);}
.planner{text-align:center;margin:4px 0 18px;padding:14px 12px;
  border-top:1px solid var(--hair);border-bottom:1px solid var(--hair);}
.planner-lead{font-size:16px;color:var(--ink);line-height:1.3;text-wrap:balance;}
.planner-lead b{font-size:30px;font-weight:700;color:var(--accent);
  font-family:ui-monospace,monospace;font-variant-numeric:tabular-nums;
  vertical-align:-3px;padding:0 2px;}
.planner a{display:inline-block;margin-top:8px;font-size:13px;color:var(--accent);
  font-weight:600;text-decoration:none;white-space:nowrap;}
.planner a:hover,.planner a:focus{text-decoration:underline;}
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
  gap:10px;font-family:ui-monospace,monospace;text-transform:uppercase;
  letter-spacing:.1em;font-size:13px;font-weight:600;color:var(--muted);
  margin-bottom:10px;}
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
    subs = bundle.get("subscores") or {}
    lng = bundle.get("lounge") or {}

    # Stat rows: plain numbers, colored by internal severity score. The
    # composite is still computed/logged but deliberately not displayed;
    # measured delays live on the Flight delays card, not here.
    from . import security, faa, approach, drive
    vals = signal_stats(bundle, terminal)
    parts = [_stat_row("Security", subs.get("security"), vals["security"],
                       security.summarize(bundle.get("security") or {}, terminal))]
    # Inbound / outbound FAA delays, each with its own trend arrow.
    for r in faa.direction_rows(bundle.get("faa") or {}):
        parts.append(_stat_row(r["label"], r["score"], r["value"],
                               note=r["note"], trend=r["trend"]))
    parts.append(_stat_row("Approach", subs.get("approach"), vals["approach"],
                           approach.summarize(bundle.get("approach") or {})))
    parts.append(_stat_row("Drive", subs.get("drive"), vals["drive"],
                           drive.summarize(bundle.get("drive") or {})))
    bars = "".join(parts)

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

    # Trends: median departure delay (rearward) + lounge queue.
    from . import departures as _departures
    ap_delays = [r.get("delay_median") for r in airport_hist]
    lounge_q = [r.get("numWaiting") for r in lounge_hist]
    now_med = _departures.median_departed_delay(
        bundle.get("departures") or {}, terminal)
    med_txt = f"{now_med}m" if now_med is not None else "&mdash;"
    lq_now = lng.get("numWaiting")
    lq_now_txt = str(lq_now) if lq_now is not None else "&mdash;"
    n_delays = sum(1 for v in ap_delays if v is not None)
    trends = (
        '<div class="card"><div class="trend-title"><span>Median departure '
        f'delay &middot; {n_delays} samples</span><span class="trend-now mono">'
        f'{med_txt}</span></div>'
        f'{_sparkline(ap_delays, color=_delay_trend_color(now_med))}</div>'
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

  {delays_html}

  <div class="grid">
    <div class="card">
      <div class="card-title">Airport status</div>
      {bars}
    </div>
    {_lounge_card(lng, security.best_general_min(bundle.get("security") or {}, terminal))}
  </div>

  <div class="trends">{trends}</div>

  <div class="foot">
    <span>Sources: flysfo &middot; FAA ASWS &middot; Waitwhile &middot; TomTom</span>
  </div>
</div>
</body>
</html>"""
