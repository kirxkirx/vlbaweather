"""The VLBA page and its ``status.json``: the parent's ``weather.page`` blocks (CSS, JS,
masthead style, alert banner and cards, current conditions, sun row, hourly and 7-day
tables, footer pills, escaping helpers) assembled in the VLBA layout:

1. masthead (title, UTC clock, the good / bad count, the day/night button);
2. the summary table: every antenna with its verdict badge, the reasons in a few words,
   whether the sun is up, the local time, and a link to its section;
3. the sticky navigation (one pill per antenna, its dot in the verdict colour);
4. the NWS alerts banner across all sites (the parent's);
5. per antenna: heading, the verdict card (badge, reasons, the hold / scheduled notes,
   and every check with its state, value, limit and detail), the map, current conditions,
   GLM, the alert cards, the sun row (with "Sun up / down"), the hourly table and the 7-day
   cards;
6. the footer: source pills (the parent's plus GLM and the island radar), credits, the
   description of the cuts, the disclaimer.

The three rules of the parent module hold here too: never raise (a broken block becomes a
visible error box), everything from the network is escaped, and the visual family is the
template's. The parent's private helpers are imported on purpose: ``weather/`` is a copy of its
library, and ``vlba/tests`` (test_page, test_main) fail if a name used here is renamed or
removed, which matters when a newer copy of the library is taken in (README, Layout).
"""
from __future__ import annotations

import html
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from weather import page as wp
from weather import util

from . import decision
from .sites import site_code, site_elevation_m

log = logging.getLogger("vlba.page")

_e = wp._e
_is_dict = wp._is_dict
_ok = wp._ok
_guard = wp._guard

VERDICT_CSS = """
  /* vlbaweather: the verdict */
  .bad { --vbad: #b42318; }
  .vbadge { display: inline-block; padding: 4px 12px; border-radius: 8px; font-weight: 700; letter-spacing: .04em;
            color: #fff; background: var(--faint); font-size: 13px; white-space: nowrap; }
  .vbadge.good { background: #1a7f37; }
  .vbadge.bad { background: #b42318; }
  .vbadge.caution { background: #b54708; }
  .vbadge.sm { padding: 2px 9px; font-size: 11.5px; }
  .vcard { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; margin: 0 0 18px; }
  .vcard.bad { border-color: #b42318; box-shadow: inset 6px 0 0 #b42318; }
  .vcard.good { box-shadow: inset 6px 0 0 #1a7f37; }
  .vcard.caution { box-shadow: inset 6px 0 0 #b54708; }
  .vhead { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
  .vhead h2 { margin: 0; border: 0; padding: 0; }
  .vwhy { margin: 8px 0 4px; padding-left: 20px; color: #b42318; font-size: 14px; line-height: 1.45; }
  .page.night .vwhy { color: #ef8a7a; }
  .vnote { margin: 6px 0 0; font-size: 13px; color: var(--warn); }
  table.checks { border-collapse: collapse; width: 100%; font-size: 13px; margin-top: 10px; }
  table.checks th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .07em; color: var(--faint);
                    font-weight: 700; padding: 0 10px 6px 0; border-bottom: 1px solid var(--line); }
  table.checks td { padding: 5px 10px 5px 0; border-bottom: 1px solid var(--line); vertical-align: top; }
  table.checks td.st { white-space: nowrap; }
  table.checks td.det { color: var(--muted); overflow-wrap: break-word; }
  .cdot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; vertical-align: -1px; background: var(--faint); }
  .cdot.ok { background: var(--good); }
  .cdot.bad { background: #b42318; }
  .cdot.watch, .cdot.scheduled, .cdot.unknown { background: var(--warn); }
  /* the summary table */
  table.sum { border-collapse: collapse; width: 100%; font-size: 14px; margin: 0 0 8px; }
  table.sum th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .07em; color: var(--faint);
                 font-weight: 700; padding: 0 10px 7px 0; border-bottom: 1px solid var(--line); white-space: nowrap; }
  table.sum td { padding: 7px 10px 7px 0; border-bottom: 1px solid var(--line); vertical-align: top; }
  table.sum td.code { font-weight: 700; white-space: nowrap; }
  table.sum td.code a { text-decoration: none; color: var(--ink); }
  table.sum td.why { color: var(--muted); font-size: 13px; overflow-wrap: break-word; }
  table.sum td.sun, table.sum td.lt { white-space: nowrap; font-variant-numeric: tabular-nums; }
  .summary { margin: 18px 0 22px; }
  .counts { font-size: 14px; color: var(--muted); margin: 10px 0 0; }
  .counts b { color: var(--ink); }
  .sunchip { display: inline-block; font-size: 12px; padding: 1px 8px; border-radius: 999px; background: var(--neutbg); color: var(--muted); white-space: nowrap; }
  .sunchip.up { color: var(--ink); }      /* information only: neutral, not the caution amber */
  .nav .dot.good { background: var(--good); }
  .nav .dot.bad { background: #b42318; }
  .nav .dot.unk { background: var(--faint); }
  .nav .dot.caution { background: var(--warn); }
  table.sum td.why .watch { color: var(--warn); }
  /* phone width: the summary and the check table stack, nothing scrolls sideways */
  @media (max-width: 600px) {
    table.sum thead, table.checks thead { display: none; }
    table.sum, table.sum tbody, table.checks, table.checks tbody { display: block; }
    table.sum tr { display: grid; grid-template-columns: auto 1fr auto auto; column-gap: 8px; align-items: center;
                   padding: 7px 0; border-bottom: 1px solid var(--line); }
    table.sum td, table.checks td { padding: 0; border: 0; }
    table.sum td.code { order: 1; } table.sum td.name { order: 2; } table.sum td.vb { order: 3; }
    table.sum td.sun { order: 4; }
    table.sum td.why { order: 5; grid-column: 1 / -1; padding-top: 4px; overflow-wrap: break-word; }
    table.sum td.lt, .when .lt { display: none; }
    table.checks tr { display: grid; grid-template-columns: auto 1fr; column-gap: 8px; padding: 6px 0;
                      border-bottom: 1px solid var(--line); }
    table.checks td.cnow, table.checks td.clim { grid-column: 2; font-size: 12px; color: var(--muted); }
    table.checks td.cnow:not(:empty)::before { content: "now: "; }
    table.checks td.clim:not(:empty)::before { content: "bad when: "; }
    table.checks td.det { grid-column: 1 / -1; padding-top: 2px; overflow-wrap: break-word; }
    .pills .pill { white-space: normal; flex: 0 1 auto; max-width: 100%; }
  }
"""

GLM_CREDIT_HTML = (
    'Lightning: NOAA GOES-East and GOES-West Geostationary Lightning Mapper (GLM-L2-LCFA) via the '
    '<a href="https://registry.opendata.aws/noaa-goes/" rel="noopener">NOAA Open Data Dissemination '
    'program on AWS</a>')
NWSRADAR_CREDIT_HTML = (
    'Radar outside the contiguous US: NWS radar composites (MRMS-based) from '
    '<a href="https://radar.weather.gov/" rel="noopener">radar.weather.gov</a> '
    '(opengeo.ncep.noaa.gov)')
SITES_CREDIT_HTML = "Antenna positions: NRAO (VLBA Observational Status Summary)"
# The parent's disclaimer, naming NRAO: on a page titled "VLBA weather" with a verdict per
# antenna, readers could take it for the observatory's own operations status.
DISCLAIMER_HTML = (
    'Not an official product: this page is not from, or endorsed by, NOAA, the National '
    'Weather Service, NRAO or any other agency, and it must not be the only source for '
    'safety or operations decisions. It can be late, incomplete or wrong whenever a source '
    'is. For official forecasts, watches and warnings use '
    '<a href="https://www.weather.gov/" rel="noopener">weather.gov</a>.')


def cuts_text(cfg) -> str:
    """The footer's account of the verdict, spelled out from the configuration: only the
    checks that are on, with their thresholds, so the page never describes a cut it does
    not apply."""
    checks = []
    names = [n[:-len(" Warning")] if n.endswith(" Warning") else n for n in cfg.veto_event_names]
    if names:
        checks.append("an NWS warning in effect over the site (%s)" % ", ".join(n.lower() for n in names))
    runs = int(cfg.radar_trigger_after)
    if cfg.check_enabled("radar"):
        checks.append("rain on radar (an echo of %g dBZ or more within %g km %s, held %s%s)" % (
            cfg.radar_dbz, cfg.radar_km,
            "in one frame" if runs == 1 else "in %s consecutive frames" % ({2: "two", 3: "three"}.get(runs) or runs),
            util.fmt_age(cfg.radar_hold_s),
            "; no echo is clear only where radar covers at least %g %% of the ring" % cfg.radar_min_coverage
            if cfg.radar_coverage else ""))
    if cfg.check_enabled("glm"):
        checks.append("lightning (a GOES GLM flash within %g km, held %s)" % (cfg.glm_km, util.fmt_age(cfg.glm_hold_s)))
    fc = ["%s over %g %%" % (what, limit) for what, key, limit in (
        ("precipitation probability", "forecast_precip", cfg.nws_precip_max),
        ("thunder probability", "forecast_thunder", cfg.nws_thunder_max),
        ("cloud cover", "forecast_cloud", cfg.nws_cloud_max)) if cfg.check_enabled(key)]
    if cfg.check_enabled("forecast_winter"):
        fc.append("any snow or ice")
    if fc:
        checks.append("the NWS forecast for this hour or next (%s)" % ", ".join(fc))
    obs = (["precipitation"] if cfg.check_enabled("obs_precip") else []) + (
        ["humidity over %g %%" % cfg.obs_humidity_max] if cfg.check_enabled("obs_humidity") else [])
    if obs:
        checks.append("the report (%s) of the nearest NWS station within %g km and %g m of elevation of the "
                      "antenna that has a recent report, one that reports weather where there is one"
                      % (", ".join(obs), cfg.obs_max_km, cfg.obs_max_delev_m))
    if cfg.check_enabled("wind"):
        checks.append("wind or gusts of %g mph or more (observed, or forecast for this hour or next)" % cfg.wind_mph)
    listed = "; ".join(checks[:-1]) + "; and " + checks[-1] if len(checks) > 1 else checks[0]
    return ("The verdict is for people, not a safety interface: BAD when any check is bad, GOOD "
            "otherwise, “GOOD?” when a check had no usable data (unknown is never counted as "
            "clear), NO DATA when no check could answer. The checks follow the weather cuts of the "
            "ttustatus observatory monitor, applied to public data: %s. Thresholds are configuration; "
            "see the README. The sun is shown for information only." % listed)


# ---- small helpers --------------------------------------------------------------------
def _verdict(s: Any) -> dict:
    v = s.get("verdict") if _is_dict(s) and _is_dict(s.get("verdict")) else {}
    return v


def _status_cls(v: dict) -> str:
    """The badge's colour: good, bad, unk, or caution for a GOOD with inputs missing."""
    st = v.get("status")
    if st == decision.GOOD:
        return "caution" if v.get("incomplete") else "good"
    return "bad" if st == decision.BAD else "unk"


def _dot_cls(v: dict) -> str:
    """The nav dot's colour: like the badge, and caution too for a GOOD that has an
    unconfirmed echo or a scheduled warning (look at this antenna)."""
    cls = _status_cls(v)
    if cls == "good" and (_strs(v.get("watch")) or _strs(v.get("scheduled"))):
        return "caution"
    return cls


def _badge(v: dict, small: bool = False) -> str:
    st = v.get("status")
    if st == decision.GOOD:
        txt = "GOOD" + ("?" if v.get("incomplete") else "")
    elif st == decision.BAD:
        txt = "BAD"
    elif st == decision.NO_DATA:
        txt = "NO DATA"
    else:
        txt = "NO VERDICT"
    title = ""
    if v.get("incomplete"):
        title = ' title="inputs missing: %s"' % _e(", ".join(_strs(v.get("missing"))))
    return '<span class="vbadge %s%s"%s>%s</span>' % (_status_cls(v), " sm" if small else "", title, txt)


def _sun_up(s: dict) -> Optional[bool]:
    v = s.get("sun_up") if _is_dict(s) else None
    if isinstance(v, bool):
        return v
    sun = s.get("sun") if _is_dict(s) and _is_dict(s.get("sun")) else {}
    alt = wp._num(sun.get("sun_alt_deg"), 1)
    if alt is None:
        return None
    return float(alt) >= -0.833


def _sun_chip(s: dict) -> str:
    up = _sun_up(s)
    sun = s.get("sun") if _is_dict(s.get("sun")) else {}
    alt = wp._num(sun.get("sun_alt_deg"), 1)
    alt_txt = (" %s°" % alt) if alt is not None else ""
    if up is None:
        return '<span class="sunchip">sun ?</span>'
    if up:
        return '<span class="sunchip up" title="sun altitude%s">☀ Sun up%s</span>' % (alt_txt, alt_txt)
    return '<span class="sunchip" title="sun altitude%s">☾ Sun down%s</span>' % (alt_txt, alt_txt)


def _short_reasons(v: dict, limit: int = 3) -> str:
    """The summary's "Why": the reasons of a BAD verdict (the most immediate first); for a
    GOOD one what still deserves a look (an unconfirmed echo, a scheduled warning, missing
    inputs), and "all checks clear" only when there is nothing of the kind."""
    reasons = [r for r in _strs(v.get("reasons")) if r]
    if not reasons:
        notes = _strs(v.get("watch")) + _strs(v.get("scheduled"))
        if v.get("incomplete"):
            notes.append("inputs missing: " + ", ".join(_strs(v.get("missing"))))
        if notes:
            return ("no check is bad; " if v.get("incomplete") and len(notes) == 1 else "") + "; ".join(notes)
        if v.get("status") == decision.GOOD:
            return "all checks clear"
        return ""
    txt = "; ".join(reasons[:limit])
    if len(reasons) > limit:
        txt += "; +%d more" % (len(reasons) - limit)
    if v.get("incomplete"):
        txt += " (some inputs missing)"
    return txt


def _local_time(gen: datetime, tz: str) -> str:
    return util.fmt_local(gen, tz, "%H:%M %Z")


def _code(s: dict) -> str:
    site = s.get("site") if _is_dict(s.get("site")) else {}
    return site_code(site) if site else "?"


# ---- page blocks --------------------------------------------------------------------
def _head_html(cfg) -> str:
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<noscript><meta http-equiv="refresh" content="%d"></noscript>\n'
        '<title>%s</title>\n'
        '<style>\n%s%s</style>\n</head>\n<body>\n'
        % (int(cfg.refresh_seconds), _e(wp._title(cfg)), wp.PAGE_CSS, VERDICT_CSS))


def _masthead_html(cfg, run: dict, gen: datetime, night: bool) -> str:
    btn = "☀ Day mode" if night else "☾ Night mode"
    utc_t = gen.astimezone(timezone.utc).strftime("%H:%M")
    utc_d = gen.astimezone(timezone.utc).strftime("%A, %B %-d, %Y")
    return (
        '<div class="mast">\n'
        '  <h1>%s</h1>\n'
        '  <div class="mastright">\n'
        '    <button class="modebtn" id="modebtn" type="button" aria-pressed="%s">%s</button>\n'
        '    <div class="when">\n'
        '      <div class="t mono">%s UTC</div>\n'
        '      <div class="d">%s<span class="lt"> · local times in each row</span></div>\n'
        '    </div>\n'
        '  </div>\n'
        '</div>\n'
        % (_e(wp._title(cfg)), "true" if night else "false", _e(btn), _e(utc_t), _e(utc_d)))


def _summary_html(run: dict, sites: List[dict], gen: datetime) -> str:
    rows = []
    for s in sites:
        slug, name = wp._site_ident(s)
        v = _verdict(s)
        tz = wp._site_tz(s)
        why = _e(_short_reasons(v))
        if v.get("status") == decision.GOOD and not v.get("reasons") and \
                (_strs(v.get("watch")) or _strs(v.get("scheduled"))):
            why = '<span class="watch">%s</span>' % why
        rows.append(
            '<tr><td class="code"><a href="#%s">%s</a></td><td class="name">%s</td><td class="vb">%s</td>'
            '<td class="why">%s</td><td class="sun">%s</td><td class="lt mono">%s</td></tr>\n'
            % (_e(wp.site_anchor(slug)), _e(_code(s)), _e(name), _badge(v, small=True),
               why, _sun_chip(s), _e(_local_time(gen, tz))))
    counts = run.get("summary") if _is_dict(run.get("summary")) else decision.summary([_verdict(s) for s in sites])
    n = len(sites)
    line = ('<p class="counts"><b>%d</b> of %d antennas GOOD, <b>%d</b> BAD%s%s.</p>\n'
            % (int(counts.get("good") or 0), n, int(counts.get("bad") or 0),
               (", %d without data" % int(counts.get("no_data") or 0)) if counts.get("no_data") else "",
               (", %d with inputs missing" % int(counts.get("incomplete") or 0)) if counts.get("incomplete") else ""))
    return (
        '<div class="summary" id="summary">\n<h2>Weather at the antennas</h2>\n'
        '<table class="sum">\n<thead><tr><th>Site</th><th></th><th>Verdict</th><th>Why</th>'
        '<th>Sun</th><th class="lt">Local time</th></tr></thead>\n<tbody>\n%s</tbody></table>\n%s</div>\n'
        % ("".join(rows), line))


def _nav_html(sites: List[dict]) -> str:
    links = []
    for s in sites:
        slug, name = wp._site_ident(s)
        v = _verdict(s)
        title = _short_reasons(v)
        links.append('  <a class="pill" href="#%s" title="%s"><span class="dot %s"></span>%s</a>\n'
                     % (_e(wp.site_anchor(slug)), _e(title), _dot_cls(v), _e(_code(s))))
    return '<nav class="nav" aria-label="Antennas">\n%s</nav>\n' % "".join(links)


def _site_head_html(s: dict, gen: datetime, tz: str) -> str:
    slug, name = wp._site_ident(s)
    site = s.get("site") if _is_dict(s.get("site")) else {}
    meta = s.get("meta") if _is_dict(s.get("meta")) else {}
    bits = []
    try:
        bits.append("%.4f, %.4f" % (float(site.get("lat")), float(site.get("lon"))))
    except (TypeError, ValueError):
        pass
    elev = site_elevation_m(site) if site else None
    if elev is not None:
        bits.append("%d m" % int(round(elev)))
    if meta.get("ok") and meta.get("grid_id"):
        bits.append("NWS %s %s,%s" % (meta.get("office") or meta.get("grid_id"), meta.get("grid_x"), meta.get("grid_y")))
    elif not meta.get("ok"):
        bits.append("NWS metadata unavailable")
    bits.append("local time " + util.fmt_local(gen, tz))
    url = wp._point_url(site)
    link = ('<a class="sitelink" href="%s" rel="noopener">NWS point forecast</a>' % _e(url)) if url else ""
    return ('<h2 class="siteh"><span class="sitename">%s · %s</span>'
            '<span class="sitemeta mono">%s</span>%s %s</h2>\n'
            % (_e(_code(s)), _e(name), _e(" · ".join(bits)), link, _sun_chip(s)))


def _check_row(c: dict) -> str:
    st = str(c.get("state") or "unknown")
    cls = {"ok": "ok", "bad": "bad", "watch": "watch", "scheduled": "scheduled", "unknown": "unknown"}.get(st, "")
    words = {"ok": "clear", "bad": "BAD", "watch": "watch", "unknown": "no data", "off": "off",
             "scheduled": "scheduled", "not provided": "n/a"}.get(st, st)
    stale = ' <span class="chip warn">stale</span>' if c.get("stale") else ""
    return ('<tr><td class="st"><span class="cdot %s"></span>%s%s</td><td class="lbl">%s</td><td class="cnow">%s</td>'
            '<td class="clim">%s</td><td class="det">%s</td></tr>\n'
            % (cls, _e(words), stale, _e(c.get("label") or c.get("key") or ""), _e(c.get("value") or ""),
               _e(c.get("limit") or ""), _e(c.get("detail") or "")))


def _verdict_html(s: dict, tz: str) -> str:
    v = _verdict(s)
    if not v:
        return ('<div class="vcard"><div class="vhead"><h2>Verdict</h2><span class="vbadge">NO VERDICT</span></div>'
                '<p class="vnote">The verdict could not be computed this run.</p></div>\n')
    cls = _status_cls(v)
    parts = ['<div class="vcard %s"><div class="vhead"><h2>Weather verdict</h2>%s</div>\n' % (cls, _badge(v))]
    reasons = [r for r in _strs(v.get("reasons")) if r]
    if reasons:
        parts.append('<ul class="vwhy">%s</ul>\n' % "".join("<li>%s</li>" % _e(r) for r in reasons))
    elif v.get("status") == decision.GOOD:
        parts.append('<p class="vnote" style="color:var(--good)">No check is bad.</p>\n')
    if v.get("incomplete"):
        parts.append('<p class="vnote">Inputs missing this run (not counted as clear): %s.</p>\n'
                     % _e(", ".join(_strs(v.get("missing")))))
    for note in _strs(v.get("watch")):
        parts.append('<p class="vnote">⚠ %s</p>\n' % _e(note))
    for sch in _strs(v.get("scheduled")):
        parts.append('<p class="vnote">⏳ %s</p>\n' % _e(sch))
    checks = v.get("checks") if isinstance(v.get("checks"), (list, tuple)) else []
    rows = "".join(_check_row(c) for c in checks if _is_dict(c))
    parts.append('<div class="scroll"><table class="checks"><thead><tr><th>State</th><th>Check</th><th>Now</th>'
                 '<th>Bad when</th><th>Detail</th></tr></thead><tbody>\n%s</tbody></table></div>\n' % rows)
    ev = v.get("evaluated")
    if isinstance(ev, datetime):
        parts.append('<p class="foot" style="margin:6px 0 0">Evaluated %s · %s UTC.</p>\n'
                     % (_e(util.fmt_local(ev, tz, "%H:%M %Z")), _e(ev.astimezone(timezone.utc).strftime("%H:%M"))))
    parts.append('</div>\n')
    return "".join(parts)


def _radar_caption(run: dict, s: dict, tz: str) -> str:
    """The frame line of the map caption: MRMS for the sites inside the grid (from
    ``run["radar"]``), the NWS composite for the others (``s["island_radar"]``)."""
    island = s.get("island_radar") if _is_dict(s.get("island_radar")) else None
    if island is not None and island.get("not_applicable"):
        return "No radar here: %s" % (island.get("error") or "outside the MRMS grid")
    if island is not None:
        label = str(island.get("label") or "NWS radar composite")
        ts = island.get("ts")
        if island.get("ok") and isinstance(ts, datetime):
            frame = "%s %s · %s" % (label, ts.astimezone(timezone.utc).strftime("%H:%MZ"),
                                    util.fmt_local(ts, tz, "%H:%M %Z"))
            if island.get("stale"):
                frame += " · STALE (%s old%s)" % (wp._age_txt(island) or "?",
                                                  ", %s" % island["error"] if island.get("error") else "")
            return frame
        if island.get("ok"):
            frame = "%s (frame time unknown)" % label
            if island.get("stale"):
                frame += " · STALE (%s old%s)" % (wp._age_txt(island) or "?",
                                                  ", %s" % island["error"] if island.get("error") else "")
            return frame
        return "%s unavailable: %s" % (label, island.get("error") or "no frame")
    radar = run.get("radar")
    if _is_dict(radar) and radar.get("ok") and isinstance(radar.get("ts"), datetime):
        ts = radar["ts"]
        frame = "MRMS frame %s · %s" % (ts.astimezone(timezone.utc).strftime("%H:%MZ"),
                                        util.fmt_local(ts, tz, "%H:%M %Z"))
        if radar.get("stale"):
            frame += " · STALE (%s old%s)" % (wp._age_txt(radar) or "?",
                                              ", %s" % radar["error"] if radar.get("error") else "")
        return frame
    if _is_dict(radar) and radar.get("not_applicable"):
        return "No MRMS radar here"
    if _is_dict(radar):
        return "Radar unavailable: %s" % (radar.get("error") or "no frame")
    return "Radar layer not rendered this run"


def _map_html(cfg, run: dict, s: dict, tz: str, gen_ts: float) -> str:
    slug, name = wp._site_ident(s)
    maps = s.get("maps") if _is_dict(s.get("maps")) else {}
    default_theme = "dark" if run.get("night_default") else "light"
    parts = ['<figure class="mapbox">\n']
    for theme in ("dark", "light"):
        m = maps.get(theme) if _is_dict(maps.get(theme)) else {}
        base = os.path.basename(str(m.get("basename") or cfg.map_basename(slug, theme)))
        if m.get("ok"):
            bust = wp._mtime_bust(os.path.join(cfg.out_dir, base), gen_ts)
            alt = ("radar, NWS alert areas, the radar and lightning rings, GLM flashes and basemap for "
                   "%g by %g miles around %s (%s theme)" % (cfg.map_miles, cfg.map_miles, name, theme))
            url = "%s?t=%d" % (base, bust)
            if theme == default_theme:
                src = 'src="%s"' % _e(url)
            else:
                src = 'src="%s" data-src="%s"' % (wp._PLACEHOLDER_SRC, _e(url))
            parts.append('  <img class="radar-img radar-%s" %s alt="%s">\n' % (theme, src, _e(alt)))
        else:
            parts.append('  <div class="mapmiss radar-%s">Radar map unavailable (%s theme): %s</div>\n'
                         % (theme, theme, _e(m.get("error") or "not rendered")))
    g = s.get("glm") if _is_dict(s.get("glm")) else {}
    glm_ring = cfg.check_enabled("glm") and g.get("covered", True)
    rc = s.get("radar_check") if _is_dict(s.get("radar_check")) else {}
    rings = (["solid ring %g km (radar check)" % cfg.radar_km] if not rc.get("off") else []) + (
        ["dashed ring %g km (lightning check)" % cfg.glm_km] if glm_ring else [])
    lines = ["%s · %g × %g mi centred on the antenna%s"
             % (_radar_caption(run, s, tz), cfg.map_miles, cfg.map_miles,
                (" · " + ", ".join(rings)) if rings else "")]
    for note in s.get("map_notes") or []:
        lines.append(str(note))
    parts.append('  <figcaption>%s%s</figcaption>\n</figure>\n'
                 % ("<br>".join(_e(x) for x in lines), wp._legend_html(s)))
    return "".join(parts)


def _glm_html(cfg, s: dict, tz: str, run: dict) -> str:
    g = s.get("glm") if _is_dict(s.get("glm")) else {}
    feed = run.get("glm") if _is_dict(run.get("glm")) else {}
    head = '<h2>Lightning (GOES GLM)%s</h2>\n'
    if not cfg.glm_enabled:
        return head % "" + '<p class="unavail">GLM layer switched off (VLBA_GLM=0).</p>\n'
    if not g:
        return head % ' <span class="chip off">unavailable</span>' + '<p class="unavail">No GLM result this run.</p>\n'
    if not g.get("covered", True):
        return head % "" + '<p class="unavail">Outside the GOES GLM field of view.</p>\n'
    if not g.get("available") and not g.get("partial"):
        err = g.get("error") or feed.get("error") or feed.get("skipped") or "no data"
        chip = ' <span class="chip off">unavailable</span>'
        return head % chip + '<p class="unavail">GLM lightning unavailable: %s</p>\n' % _e(err)
    count = int(wp._num(g.get("count_window")) or 0)
    near = wp._num(g.get("nearest_km"), 1)
    sats = ", ".join(str(x) for x in (g.get("sats") or []))
    window = wp._num(g.get("window_min")) or wp._num(cfg.glm_window_min)
    # the check row's number format ("912 km", "23.2 km")
    nearest = "none seen" if near is None else "%s km %s" % (
        decision._num_txt(g.get("nearest_km")), _e(g.get("nearest_bearing") or ""))
    t = g.get("nearest_t")
    when = ("at " + util.fmt_local(t, tz, "%H:%M:%S %Z")) if isinstance(t, datetime) else ""
    by_sat = g.get("count_by_sat") if _is_dict(g.get("count_by_sat")) else {}
    count_sub = "flashes in the last %s min" % window
    if len(by_sat) > 1 and any(int(wp._num(v) or 0) for v in by_sat.values()):     # _num gives text
        # both satellites see most flashes: each counted once, the detections listed
        count_sub += " (detected by %s; one flash seen by both counts once)" % ", ".join(
            "%s %d" % (k, int(wp._num(v) or 0)) for k, v in sorted(by_sat.items()))
    tiles = [
        wp._tile("Within %g km" % cfg.glm_km, "%d" % count, _e(count_sub)),
        wp._tile("Nearest flash", nearest, _e(when or "in the last %s min" % window), txt=True),
        wp._tile("Satellites", _e(sats or "—"),
                 _e("partial: %s" % (g.get("error") or "a satellite's data is missing")) if g.get("partial") else "",
                 txt=True),
    ]
    chip = ' <span class="chip warn">partial</span>' if g.get("partial") else ""
    note = ""
    if not g.get("available"):
        note = ('<p class="vnote">Incomplete data this run: the count is a lower bound, and no flash '
                'is not a clear sky.</p>\n')
    return head % chip + '<div class="now">\n%s</div>\n' % "".join(tiles) + note


def _obs_station_note(s: dict) -> str:
    st = s.get("obs_station") if _is_dict(s.get("obs_station")) else None
    none_qualify = bool(st) and isinstance(st.get("qualifying"), list) and not st["qualifying"]
    if not st or not st.get("id"):
        if none_qualify:
            return ('<p class="foot" style="margin:-10px 0 16px">No NWS station lies close to and level with '
                    'the antenna: the observation checks do not apply here.</p>\n')
        return ""
    bits = []
    if st.get("dist_km") is not None:
        bits.append("%s km from the antenna" % wp._num(st.get("dist_km"), 1))
    if st.get("delev_m") is not None:
        d = float(st["delev_m"])
        bits.append("%d m %s" % (int(round(abs(d))), "lower" if d < 0 else "higher"))
    if st.get("usable"):
        use = "used for the verdict" + ("; %s" % st["why"] if st.get("why") else "")
    else:
        use = "not used for the verdict: %s" % (st.get("why") or "too far")
        if none_qualify:
            use += "; no NWS station near the antenna qualifies, so the observation checks do not apply here"
    return '<p class="foot" style="margin:-10px 0 16px">Station %s: %s; %s.</p>\n' % (
        _e(st.get("id") or "?"), _e(", ".join(bits) or "distance unknown"), _e(use))


def _sun_html(s: dict, tz: str, gen: datetime) -> str:
    """The parent's sun row, headed by the up/down statement (information only)."""
    base = wp._sun_html(s, tz, gen)
    if not base:
        return ""
    up = _sun_up(s)
    sun = s.get("sun") if _is_dict(s.get("sun")) else {}
    phase = wp._sun_phase(sun) if sun else None
    if up is None:
        line = ""
    else:
        line = ('<p class="small" style="margin:0 0 8px"><b>%s</b>%s — for information only; '
                'the verdict does not use the sun.</p>\n'
                % ("The sun is above the horizon." if up else "The sun is below the horizon.",
                   (" (%s)" % _e(phase)) if phase else ""))
    head, _, rest = base.partition("</h2>\n")
    return head + "</h2>\n" + line + rest


def _site_section_html(cfg, run: dict, s: dict, units, gen: datetime, gen_ts: float) -> str:
    slug, name = wp._site_ident(s)
    tz = wp._site_tz(s)
    parts = ['<section class="site" id="%s">\n' % _e(wp.site_anchor(slug))]
    parts.append(_guard("site heading", _site_head_html, s, gen, tz))
    parts.append(_guard("verdict", _verdict_html, s, tz))
    parts.append('<div class="sitegrid">\n<div>\n')
    parts.append(_guard("radar map", _map_html, cfg, run, s, tz, gen_ts))
    parts.append('</div>\n<div>\n')
    parts.append(_guard("current conditions", wp._now_html, s, units, tz))
    parts.append(_guard("station note", _obs_station_note, s))
    parts.append(_guard("lightning", _glm_html, cfg, s, tz, run))
    parts.append(_guard("alerts", wp._alert_cards_html, s, tz, gen))
    parts.append('</div>\n</div>\n')
    if s.get("sun"):
        parts.append(_guard("sun and twilight", _sun_html, s, tz, gen))
    parts.append(_guard("hourly forecast", wp._hourly_html, cfg, s, units, tz))
    parts.append(_guard("7-day forecast", wp._forecast_html, cfg, s, units, tz))
    parts.append('</section>\n')
    return "".join(parts)


def _extra_pills(cfg, run: dict, sites: List[dict]) -> str:
    pills = []
    g = run.get("glm")
    if not cfg.glm_enabled:
        dot, txt = " off", "off"
    elif _is_dict(g) and g.get("skipped"):
        dot, txt = " off", "not fetched · %s" % g["skipped"]
    else:
        dot, txt = wp._src_status(g)
        if _is_dict(g) and g.get("ok"):
            sats = g.get("sats") if _is_dict(g.get("sats")) else {}
            txt = "%s · %s" % (txt, ", ".join("%s %s/%s granules" % (k, v.get("fetched", "?"), v.get("selected", "?"))
                                              for k, v in sorted(sats.items()) if _is_dict(v)))
            if g.get("partial"):
                dot = " warnc"
    pills.append('<span class="pill"><span class="dot%s"></span>GLM lightning: %s</span>' % (dot, _e(txt)))
    pills.append(_gridpoint_pill(cfg, sites))
    for s in sites:
        island = s.get("island_radar") if _is_dict(s.get("island_radar")) else None
        if island is None:
            continue
        dot, txt = (" off", "off") if island.get("not_applicable") else wp._src_status(island)
        pills.append('<span class="pill"><span class="dot%s"></span>%s radar (%s): %s</span>'
                     % (dot, _e(_code(s)), _e(island.get("label") or "NWS composite"), _e(txt)))
    pills.extend(_coverage_pills(cfg, run, sites))
    return "\n".join(pills)


def _coverage_pills(cfg, run: dict, sites: List[dict]) -> List[str]:
    """One pill per MRMS domain read for the radar coverage: its file's time and the rings
    radar does not fully cover (amber when one is below the minimum), or why it failed."""
    if not cfg.radar_coverage:
        return ['<span class="pill"><span class="dot off"></span>Radar coverage (MRMS): not checked</span>']
    codes = {wp._site_ident(s)[0]: _code(s) for s in sites if _is_dict(s)}
    cov = run.get("radar_coverage") if _is_dict(run.get("radar_coverage")) else {}
    out = []
    for domain in sorted(cov):
        c = cov[domain]
        if not _is_dict(c):
            continue
        if c.get("ok"):
            ts = c.get("ts")
            txt = ts.astimezone(timezone.utc).strftime("%H:%MZ") if isinstance(ts, datetime) else "time unknown"
            rings = c.get("sites") if _is_dict(c.get("sites")) else {}
            part = [(codes.get(slug, slug.upper()), float(r["covered_pct"])) for slug, r in sorted(rings.items())
                    if _is_dict(r) and wp._num(r.get("covered_pct")) is not None and float(r["covered_pct"]) < 100.0]
            txt += (" · " + ", ".join("%s ring %s %% covered" % (code, decision._num_txt(pct)) for code, pct in part)) \
                if part else " · every ring fully covered"
            dot = " warnc" if any(pct < float(cfg.radar_min_coverage) for _, pct in part) else ""
        else:
            dot, txt = " off", "unavailable · %s" % (c.get("error") or "no data")
        out.append('<span class="pill"><span class="dot%s"></span>Radar coverage (MRMS %s): %s</span>'
                   % (dot, _e(domain), _e(txt)))
    return out


def _gridpoint_state(cfg, gp: dict) -> Optional[str]:
    """None when a gridpoint forecast is fully fit for the verdict, else a short, stable
    reason (status.json's problems): what the verdict rejects (``decision.forecast_fitness``),
    series it could not read, or a last good copy in use (usable, but stale)."""
    why = decision.forecast_fitness(cfg, gp)
    if why:
        return why
    if decision.forecast_unreadable(cfg, gp):
        return "unreadable series"
    if gp.get("stale"):
        return "stale"
    return None


def _gridpoint_pill(cfg, sites: List[dict]) -> str:
    """One pill for the raw gridpoint forecasts behind the forecast checks (the parent's
    per-site pills cover the observation and the two text forecasts only): how many are
    fit for the verdict, and why the others are not."""
    fresh, problems, worst = 0, [], ""
    for s in sites:
        gp = s.get("gridpoint") if _is_dict(s.get("gridpoint")) else None
        if gp is None or gp.get("not_provided"):
            continue
        why = _gridpoint_state(cfg, gp)
        if why is None:
            fresh += 1
        else:
            age = wp._num(gp.get("update_age_min"))
            if why == "NWS update too old" and age is not None:
                why = "NWS update %s old" % util.fmt_duration_minutes(float(age))      # the pill shows the age
            problems.append("%s %s" % (_code(s), why))
            worst = " off" if why == "unavailable" else (worst or " warnc")
    total = fresh + len(problems)
    txt = "%d of %d fresh" % (fresh, total) + ("; " + ", ".join(problems) if problems else "")
    return '<span class="pill"><span class="dot%s"></span>Gridpoint forecasts: %s</span>' % (worst, _e(txt))


def _footer_html(cfg, run: dict, sites: List[dict], gen: datetime) -> str:
    base = wp._footer_html(cfg, run, sites, gen)
    extra = _extra_pills(cfg, run, sites)
    if extra:
        base = base.replace("\n</div>\n", "\n%s\n</div>\n" % extra, 1)
    base = base.replace(wp.DISCLAIMER_HTML, DISCLAIMER_HTML, 1)
    credits = ('<p class="foot">%s · %s · %s</p>\n<p class="foot">%s</p>\n'
               % (GLM_CREDIT_HTML, NWSRADAR_CREDIT_HTML, SITES_CREDIT_HTML, _e(cuts_text(cfg))))
    marker = '<p class="foot disclaimer">'
    if marker in base:
        return base.replace(marker, credits + marker, 1)
    return base + credits


def _render(cfg, run: dict) -> str:
    run = run if _is_dict(run) else {}
    gen = wp._generated(run)
    gen_ts = wp._generated_ts(run, gen)
    sites = [s for s in (run.get("sites") or []) if _is_dict(s)]
    night = bool(run.get("night_default"))
    units = wp._Units(cfg.units)
    parts = [_head_html(cfg)]
    parts.append('<div class="%s" id="page" data-night-default="%s" data-refresh="%d">\n<div class="wrap">\n'
                 % ("page night" if night else "page", "1" if night else "0", int(cfg.refresh_seconds)))
    parts.append(_guard("masthead", _masthead_html, cfg, run, gen, night))
    parts.append(_guard("summary", _summary_html, run, sites, gen))
    parts.append(_guard("navigation", _nav_html, sites))
    parts.append(_guard("alerts banner", wp._banner_html, run, sites))
    if not sites:
        parts.append(wp._error_box("No site data in this run."))
    for s in sites:
        try:
            parts.append(_site_section_html(cfg, run, s, units, gen, gen_ts))
        except Exception as e:  # noqa: BLE001 - one bad site must not kill the page
            log.exception("site %s failed to render: %s", wp._site_ident(s)[0], e)
            parts.append(wp._site_error_html(s, "%s: %s" % (type(e).__name__, e)))
    parts.append(_guard("footer", _footer_html, cfg, run, sites, gen))
    parts.append('</div>\n</div>\n<script>\n%s</script>\n</body>\n</html>\n' % wp.PAGE_JS)
    return "".join(parts)


# ---- public API -----------------------------------------------------------------------
def render_html(cfg, run: dict) -> str:
    """The whole page; never raises (a minimal page naming the error at worst)."""
    try:
        return _render(cfg, run)
    except Exception as e:  # noqa: BLE001
        log.exception("page render failed: %s", e)
        title = wp._title(cfg)
        return ('<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
                '<meta http-equiv="refresh" content="%d"><title>%s</title></head>\n'
                '<body><div class="page" id="page"><h1>%s</h1><p>Page generation failed: %s</p>'
                '</div></body></html>\n'
                % (int(getattr(cfg, "refresh_seconds", 300)), _e(title), _e(title),
                   _e("%s: %s" % (type(e).__name__, e))))


def write_page(cfg, run: dict) -> str:
    path = os.path.join(cfg.out_dir, "index.html")
    wp._atomic_write_text(path, render_html(cfg, run))
    log.info("wrote %s", path)
    return path


def _strs(v: Any) -> List[str]:
    """A list of strings from whatever a run dict holds (a non-list is nothing)."""
    return [str(x) for x in v if x is not None] if isinstance(v, (list, tuple)) else []


def _verdict_doc(v: dict) -> dict:
    checks = v.get("checks") if isinstance(v.get("checks"), (list, tuple)) else []
    return {"status": v.get("status"), "incomplete": bool(v.get("incomplete")),
            "reasons": _strs(v.get("reasons")), "missing": _strs(v.get("missing")),
            "checks": {str(c.get("key")): str(c.get("state")) for c in checks if _is_dict(c)}}


def status_doc(cfg, run: dict) -> dict:
    """The parent's health document plus the verdicts, GLM and the island radar."""
    doc = wp._status_doc(cfg, run)
    run = run if _is_dict(run) else {}
    verdicts: Dict[str, Any] = {}
    for s in run.get("sites") or []:
        if not _is_dict(s):
            continue
        slug, _ = wp._site_ident(s)
        v = _verdict(s)
        verdicts[slug] = _verdict_doc(v) if v else None
        island = s.get("island_radar") if _is_dict(s.get("island_radar")) else None
        if island is not None and slug in doc.get("sites", {}):
            # switched off on purpose: not a failure (null, as the parent's "not applicable")
            doc["sites"][slug]["island_radar_ok"] = None if island.get("not_applicable") else bool(island.get("ok"))
        gp = s.get("gridpoint") if _is_dict(s.get("gridpoint")) else None
        if gp is not None and slug in doc.get("sites", {}):
            # like the parent's *_ok: a usable last good copy is ok (its staleness is a problem)
            doc["sites"][slug]["gridpoint_ok"] = None if gp.get("not_provided") else \
                decision.forecast_fitness(cfg, gp) is None and not decision.forecast_unreadable(cfg, gp)
        sun_up = _sun_up(s)
        if slug in doc.get("sites", {}):
            doc["sites"][slug]["sun_up"] = sun_up
            doc["sites"][slug]["verdict"] = v.get("status") if v else None
    doc["verdicts"] = verdicts
    doc["summary"] = run.get("summary") if _is_dict(run.get("summary")) else decision.summary(
        [_verdict(s) for s in (run.get("sites") or []) if _is_dict(s)])
    g = run.get("glm") if _is_dict(run.get("glm")) else None
    doc["glm"] = None if g is None else {
        "ok": bool(g.get("ok")), "partial": bool(g.get("partial")), "skipped": g.get("skipped"),
        "flashes": int(wp._num(g.get("flash_count")) or 0), "error": g.get("error")}
    probs = list(doc.get("problems") or [])
    if cfg.glm_enabled and g is not None and not g.get("ok") and not g.get("skipped"):
        probs.append("GLM lightning: unavailable")
    elif cfg.glm_enabled and g is not None and g.get("partial"):
        probs.append("GLM lightning: partial")
    for s in run.get("sites") or []:
        if not _is_dict(s):
            continue
        slug, _ = wp._site_ident(s)
        gp = s.get("gridpoint") if _is_dict(s.get("gridpoint")) else None
        if gp is not None and not gp.get("not_provided"):
            why = _gridpoint_state(cfg, gp)
            if why:
                probs.append("%s gridpoint forecast: %s" % (slug, why))
        island = s.get("island_radar") if _is_dict(s.get("island_radar")) else None
        if island is None or island.get("not_applicable"):
            continue
        if not island.get("ok"):
            probs.append("%s radar composite: unavailable" % slug)
        elif island.get("stale"):
            probs.append("%s radar composite: stale" % slug)
    # an observation that is only shown (no station qualifies: Mauna Kea, Pie Town) is no
    # problem when NWS sends nothing usable there
    shown_only = {wp._site_ident(s)[0] for s in run.get("sites") or []
                  if _is_dict(s) and _is_dict(s.get("obs")) and s["obs"].get("display_only")}
    probs = [p for p in probs if not any(p.startswith("%s observation: " % slug) for slug in shown_only)]
    # the radar coverage: a domain that could not be read, a ring below the minimum
    cov = run.get("radar_coverage") if _is_dict(run.get("radar_coverage")) else {}
    doc["radar_coverage"] = None if not cfg.radar_coverage else {
        domain: {"ok": bool(c.get("ok")), "error": c.get("error"),
                 "ts": c["ts"].isoformat() if isinstance(c.get("ts"), datetime) else None,
                 "covered_pct": {slug: r.get("covered_pct") for slug, r in sorted((c.get("sites") or {}).items())
                                 if _is_dict(r)}}
        for domain, c in sorted(cov.items()) if _is_dict(c)}
    for domain, c in sorted((doc["radar_coverage"] or {}).items()):
        if not c["ok"]:
            probs.append("radar coverage (MRMS %s): unavailable" % domain)
            continue
        for slug, pct in c["covered_pct"].items():
            if pct is not None and float(pct) < float(cfg.radar_min_coverage):
                # a stable text (the percentage is in radar_coverage)
                probs.append("%s radar coverage: below %g %%" % (slug, cfg.radar_min_coverage))
    doc["problems"] = probs
    doc["degraded"] = bool(probs)
    doc["ok"] = bool(doc.get("page_written")) and not probs
    return doc


def write_status_json(cfg, run: dict) -> str:
    try:
        doc = status_doc(cfg, run)
    except Exception as e:  # noqa: BLE001
        log.exception("status summary failed: %s", e)
        msg = "status summary failed: %s: %s" % (type(e).__name__, e)
        doc = {"generated": util.utcnow().isoformat(), "page_written": False, "degraded": True,
               "ok": False, "problems": [msg], "alerts": {}, "radar": {}, "network": None,
               "roads": None, "sites": {}, "verdicts": {}, "summary": {}, "glm": None, "errors": [msg]}
    path = os.path.join(cfg.out_dir, "status.json")
    wp._atomic_write_text(path, json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")
    return path


__all__ = ["render_html", "write_page", "write_status_json", "status_doc", "html"]
