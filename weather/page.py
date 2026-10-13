"""HTML page and ``status.json`` writers for local-weather-awareness.

Everything a viewer sees is built here from the ``run`` dict that
``make_weather_page.run_once`` assembles (shape in DESIGN.md). Three rules, in order:

* **Never raise.** A broken site, a malformed alert record or a missing map degrades into
  a visible "unavailable" / error box, never into a missing page: the systemd timer keeps
  running and an exception here would leave a stale page up for ever.
* **Everything is escaped.** Every string that came from the network passes through
  ``html.escape``; URLs from the feeds are emitted only when they start with ``https://``;
  alert colours only when they look like ``#rrggbb`` (else a neutral grey). The links this
  module builds itself (NWS hazard page, point forecast, attribution) use fixed hosts and
  URL-encoded, validated parameters.
* **Same visual family as the ttustatus template**: its CSS variables, fonts, ``.tile`` /
  ``.pill`` / ``.mono`` / ``h2`` styles and the ``sessionStorage`` day/night toggle, so the
  two pages look like siblings. No external assets except the NWS icon URLs.

Browser behaviour (``PAGE_JS``; the page stays fully readable without JavaScript):

* Only the radar PNGs of the server-default theme (``run["night_default"]``) carry a real
  ``src``; the other theme's ``<img>`` holds its URL in ``data-src`` and is loaded the first
  time that theme is shown, so a page view downloads one PNG per site, not two.
* The day/night toggle is remembered in ``sessionStorage`` together with the server default
  it overrode, and applied only while that default is unchanged, so the sun-driven palette
  retakes control at the next civil dusk/dawn.
* The page reloads itself every ``cfg.refresh_seconds`` and restores the open ``<details>``
  (stable ``data-k`` keys) and the scroll position; ``<noscript>`` keeps a meta refresh.

Road closures (``run["roads"]`` and each site's ``"roads"``, roads contract R4): a block per
site after the alert cards (NMDOT emergency closures and water on the road, then NWS storm
reports that name a closed or flooded road, each dated and flagged "may have reopened" when
old), one banner line with the counts, source pills, credits and NMDOT's disclaimer in the
footer, and ``roads`` keys in status.json. Everything road-related is monochrome (page ink
and background only), because hazards are never green, flood alert areas are already red
and the radar uses cyan..magenta. A run dict without road keys renders exactly as before:
that is a run with roads switched off, and a run in which no site's map reaches New Mexico
(``run["roads_not_applicable"]``; status.json then reports roads as "not applicable").
The page title is ``cfg.page_title``: ``WEATHER_TITLE``, else derived from the site names.
"""
from __future__ import annotations

import html
import json
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from . import util

log = logging.getLogger("weather.page")

DASH = "—"                      # what every missing value renders as
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
# Same value as alerts.KIND_COLORS["other"]; kept local so the page module never imports
# the network-facing modules (a broken import there must not take the page down too).
_GREY = "#808080"
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
# NWS UGC zone ids as they appear in /points and affectedZones: NMZ220 (forecast/fire
# zone), NMC053 (county). Only ids of this form are ever put into a link.
_ZONE_RE = re.compile(r"^[A-Z]{2}[CZ]\d{3}$")
# 1x1 transparent GIF: the placeholder ``src`` of the radar image of the theme that is not
# shown by default (its real URL waits in ``data-src``), so the markup stays valid HTML.
_PLACEHOLDER_SRC = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
_HAZARD_URL = "https://forecast.weather.gov/showsigwx.php"
_POINT_URL = "https://forecast.weather.gov/MapClick.php"
# Sun-altitude bands for the "Sun now" label, used only when an older run dict has no
# ``phase`` (same thresholds as weather/sun.py: sunrise -0.833°, civil -6°, nautical -12°,
# astronomical -18°).
_PHASE_BANDS = ((-0.833, "day"), (-6.0, "civil twilight"), (-12.0, "nautical twilight"),
                (-18.0, "astronomical twilight"))
_AREA_MAX = 90                  # banner: area description truncated to about this many chars


# ---- small escaping / formatting helpers ------------------------------------------
def _e(x: Any) -> str:
    """HTML-escape anything (None -> '')."""
    return html.escape("" if x is None else str(x), quote=True)


def _color(c: Any) -> str:
    """An alert colour is used inline in ``style=`` attributes, so only a strict #rrggbb
    passes; anything else (None, 'red', an injection attempt) becomes grey."""
    return c if isinstance(c, str) and _COLOR_RE.match(c) else _GREY


def _https(u: Any) -> Optional[str]:
    """Only https URLs from the feeds are ever emitted (icons, alert web links)."""
    return u if isinstance(u, str) and u.startswith("https://") else None


def _text_on(color: str) -> str:
    """Black or white text for a badge on ``color`` (already validated)."""
    r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    return "#111111" if lum > 0.55 else "#ffffff"


def _num(v: Any, nd: int = 0) -> Optional[str]:
    """Number -> string with ``nd`` decimals; None for missing / NaN / non-numeric."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return ("%.*f" % (nd, f)) if nd else ("%d" % int(round(f)))


def _dual(primary: Optional[str], alt: Optional[str]) -> str:
    """Primary text with the other unit in a small muted span; DASH when missing."""
    if primary is None:
        return DASH
    if alt is None:
        return _e(primary)
    return '%s <span class="alt">%s</span>' % (_e(primary), _e(alt))


def _pct(v: Any) -> str:
    s = _num(v)
    return DASH if s is None else s + "%"


def _is_dict(x: Any) -> bool:
    return isinstance(x, dict)


def _ok(d: Any) -> bool:
    return _is_dict(d) and bool(d.get("ok"))


class _Units:
    """Renders dual-unit quantities: the primary system per ``cfg.units``, the other one in
    ``<span class="alt">``. Either input may be None; when only one is present the other
    is derived (the NWS module fills both, but the page must not depend on it)."""

    def __init__(self, units: str):
        self.metric = (units == "metric")

    def _pair(self, us: Any, metric: Any, us_unit: str, m_unit: str,
              nd_us: int = 0, nd_m: int = 0) -> str:
        su, sm = _num(us, nd_us), _num(metric, nd_m)
        us_txt = None if su is None else su + us_unit
        m_txt = None if sm is None else sm + m_unit
        return _dual(m_txt, us_txt) if self.metric else _dual(us_txt, m_txt)

    def temp(self, f: Any, c: Any) -> str:
        if f is None and c is not None:
            f = util.c_to_f(c)
        if c is None and f is not None:
            c = util.f_to_c(f)
        return self._pair(f, c, "°F", "°C")

    def speed(self, mph: Any, kmh: Any) -> str:
        if mph is None and kmh is not None:
            mph = util.kmh_to_mph(kmh)
        if kmh is None and mph is not None:
            kmh = util.mph_to_kmh(mph)
        return self._pair(mph, kmh, " mph", " km/h")

    def pressure(self, inhg: Any, hpa: Any) -> str:
        if inhg is None and hpa is not None:
            inhg = util.pa_to_inhg(hpa * 100.0)
        if hpa is None and inhg is not None:
            hpa = inhg / 0.0002952998 / 100.0
        return self._pair(inhg, hpa, " inHg", " hPa", nd_us=2)

    def distance(self, mi: Any, km: Any) -> str:
        if mi is None and km is not None:
            mi = util.km_to_mi(km)
        if km is None and mi is not None:
            km = mi * 1.609344
        return self._pair(mi, km, " mi", " km", nd_us=1, nd_m=1)

    def wind_text(self, s: Any) -> str:
        """The 7-day product gives wind as prose ('10 to 15 mph'); for metric pages the
        numbers are converted in place and the original kept as the alt."""
        if not isinstance(s, str) or not s.strip():
            return DASH
        if not self.metric or "mph" not in s:
            return _e(s)
        conv = _NUM_RE.sub(lambda m: "%d" % int(round(float(m.group()) / 0.621371)), s)
        return _dual(conv.replace("mph", "km/h"), s)


# ---- status-key helpers -----------------------------------------------------------
def _age_txt(d: Any) -> Optional[str]:
    if not _is_dict(d) or d.get("age_s") is None:
        return None
    return util.fmt_age(d.get("age_s"))


def _src_status(d: Any) -> Tuple[str, str]:
    """(dot class, label) for a footer pill describing one source's freshness."""
    if not _is_dict(d):
        return " off", "unavailable"
    age = _age_txt(d)
    if d.get("ok") and not d.get("stale"):
        return "", "fresh" + (" · %s" % age if age else "")
    if d.get("ok"):
        txt = "stale · last good %s ago" % (age or "?")
        if d.get("error"):
            txt += " · %s" % d["error"]
        return " warnc", txt
    txt = "unavailable"
    if d.get("error"):
        txt += " · %s" % d["error"]
    return " off", txt


def _short_status(d: Any) -> str:
    if _is_dict(d) and d.get("not_provided"):
        return "not provided"
    if not _ok(d):
        return "unavailable"
    if d.get("stale"):
        return "stale %s" % (_age_txt(d) or "?")
    return "ok"


def _stale_chip(d: Any) -> str:
    """Chip appended to a block heading when its data is stale or missing."""
    if not _is_dict(d):
        return ' <span class="chip off">unavailable</span>'
    if (d.get("ok") and not d.get("stale")) or d.get("not_provided"):
        return ""
    if d.get("ok"):
        return ' <span class="chip warn">stale · %s old</span>' % _e(_age_txt(d) or "?")
    return ' <span class="chip off">unavailable</span>'


def _unavail(what: str, d: Any) -> str:
    if _is_dict(d) and d.get("not_provided"):
        # e.g. American Samoa: NWS has zones and alerts there, but no forecast grid
        return ('<p class="unavail">%s: not provided by NWS for this location (it has no '
                'forecast grid).</p>\n' % _e(what))
    err = d.get("error") if _is_dict(d) else None
    extra = " (%s)" % _e(err) if err else ""
    return '<p class="unavail">%s unavailable%s</p>\n' % (_e(what), extra)


def _error_box(msg: str) -> str:
    return '<div class="errbox">%s</div>\n' % _e(msg)


def _guard(what: str, fn: Callable[..., str], *args: Any) -> str:
    """Run one page-building step; any exception becomes a visible error box (logged with
    a traceback) so that a single bad record never blanks the page."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 — every failure must become a box, never a crash
        log.exception("rendering %s failed: %s", what, e)
        return _error_box("%s could not be rendered: %s: %s" % (what, type(e).__name__, e))


# ---- run-dict accessors (defensive: the run may be partial after failures) ----------
def _generated(run: dict) -> datetime:
    gen = run.get("generated")
    if not isinstance(gen, datetime):
        gen = util.utcnow()
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=timezone.utc)
    return gen


def _generated_ts(run: dict, gen: datetime) -> float:
    try:
        return float(run.get("generated_ts"))
    except (TypeError, ValueError):
        return gen.timestamp()


def _site_ident(s: Any) -> Tuple[str, str]:
    """(slug, name) that never raises, for anchors and error boxes."""
    try:
        site = s.get("site") or {}
        slug = str(site.get("slug") or "site")
        return slug, str(site.get("name") or slug)
    except Exception:  # noqa: BLE001
        return "site", "site"


# Page anchors of a site. Prefixed, so that no slug can collide with the page's own ids
# ("page", "alerts", "modebtn") or with another site's road block: slug "x-roads" gives
# "site-x-roads", site "x"'s road block is "roads-x".
def site_anchor(slug: str) -> str:
    """The id of a site's section (and its nav link target): ``site-<slug>``."""
    return "site-%s" % slug


def roads_anchor(slug: str) -> str:
    """The id of a site's road-closure block: ``roads-<slug>``."""
    return "roads-%s" % slug


def _site_tz(s: Any) -> str:
    """The site's time zone, by the same rule as make_weather_page: the NWS metadata's
    zone when it is usable, else the site's static ``tz`` from the config, else UTC."""
    if not _is_dict(s):
        return "UTC"
    meta = s.get("meta") if _is_dict(s.get("meta")) else {}
    site = s.get("site") if _is_dict(s.get("site")) else {}
    if meta.get("ok") and meta.get("tz"):
        return str(meta["tz"])
    return str(site.get("tz") or meta.get("tz") or "UTC")


def _rank(a: Any) -> int:
    try:
        return int(a.get("rank"))
    except (TypeError, ValueError, AttributeError):
        return 10 ** 6


def _top_alert(alerts: List[dict]) -> Optional[dict]:
    dicts = [a for a in alerts if _is_dict(a)]
    return min(dicts, key=_rank) if dicts else None


def _end_dt(a: dict) -> Optional[datetime]:
    dt = a.get("end_dt")
    if isinstance(dt, datetime):
        return dt
    return util.parse_iso(a.get("ends") or a.get("expires"))


def _mtime_bust(path: str, gen_ts: float) -> int:
    """Cache-busting query value: the map file's mtime, else this run's timestamp."""
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return int(gen_ts)


# ---- alert text helpers -------------------------------------------------------------
def _as_dt(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)
    return util.parse_iso(v)


def _span_txt(a: dict, tz: str, gen: datetime) -> str:
    """'from <onset> until <end>' for an alert that has not started yet (NWS issues Red
    Flag, Winter Storm, Freeze and Flood products a day ahead), else 'until <end>'."""
    end = _end_dt(a)
    until = ("until %s" % util.fmt_local(end, tz)) if end else None
    onset = _as_dt(a.get("onset"))
    if onset is not None and onset > gen:
        start = "from %s" % util.fmt_local(onset, tz)
        return ("%s %s" % (start, until)) if until else "%s (no end time given)" % start
    return until or "no end time given"


def _office(a: dict) -> str:
    """Issuing office without the 'NWS ' prefix ('NWS Albuquerque NM' -> 'Albuquerque NM')."""
    s = str(a.get("sender") or "").strip()
    return s[4:].strip() if s[:4].upper() == "NWS " else s


def _short_area(text: Any, limit: int = _AREA_MAX) -> str:
    """Area description cut to about ``limit`` characters at a '; ' boundary (NWS lists
    zones/counties separated by '; '), with an ellipsis when anything was dropped."""
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    out = ""
    for part in text.split("; "):
        cand = part if not out else out + "; " + part
        if len(cand) > limit:
            break
        out = cand
    if not out:                         # the first name alone is too long: cut at a word
        out = text[:limit].rsplit(" ", 1)[0]
    return out.rstrip(" ;,") + "…"


def _threat_chip(a: dict) -> str:
    """High-contrast chip for the damage-threat tag (TORNADO EMERGENCY, PDS, DESTRUCTIVE,
    …) that alerts.normalize derives from the CAP parameters; '' when there is none."""
    t = a.get("threat")
    if not t or not isinstance(t, str):
        return ""
    return ' <span class="chip threat">%s</span>' % _e(t)


def _point_url(site: Any) -> Optional[str]:
    """NWS point forecast page for the site ('forecast.weather.gov/MapClick.php')."""
    try:
        lat, lon = float(site.get("lat")), float(site.get("lon"))
    except (TypeError, ValueError, AttributeError):
        return None
    if math.isnan(lat) or math.isnan(lon):
        return None
    return "%s?lat=%.4f&lon=%.4f" % (_POINT_URL, lat, lon)


def _zone(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and _ZONE_RE.match(v) else None


def _hazard_url(a: dict, s: dict, rel: str) -> Tuple[Optional[str], bool]:
    """(url, is_hazard_page): the NWS human-readable page that shows this alert's full
    text, ``showsigwx.php`` for a (forecast zone, county, fire zone) triple with the event
    as ``product1``. Verified 2026-09-23: it answers 200 and shows the product when ANY of
    the three ids is covered by it, but shows nothing when one of them is missing. So an
    alert AT the site uses the site's own zones; a NEAR alert (which does not cover the
    site's zones) substitutes its own first forecast zone / county id. Without a complete
    triple the site's point forecast page (MapClick) is the fallback."""
    meta = s.get("meta") if _is_dict(s.get("meta")) else {}
    site = s.get("site") if _is_dict(s.get("site")) else {}
    own = [z for z in (_zone(x) for x in (a.get("affected_zone_ids") or [])) if z]
    own_z = [z for z in own if z[2] == "Z"]
    own_c = [z for z in own if z[2] == "C"]
    site_z, site_c = _zone(meta.get("forecast_zone")), _zone(meta.get("county_zone"))
    site_f = _zone(meta.get("fire_zone"))
    if rel == "AT":
        wz = site_z or (own_z[0] if own_z else None)
        wc = site_c or (own_c[0] if own_c else None)
    else:
        wz = own_z[0] if own_z else site_z
        wc = own_c[0] if own_c else site_c
    fz = site_f or wz
    event = str(a.get("event") or "").strip()
    if wz and wc and fz and event:
        q = urlencode([("warnzone", wz), ("warncounty", wc), ("firewxzone", fz),
                       ("local_place1", str(site.get("name") or site.get("slug") or "")),
                       ("product1", event)])
        return "%s?%s" % (_HAZARD_URL, q), True
    return _point_url(site), False


# ---- CSS / JS ---------------------------------------------------------------------
# Palette, typography and the base components are the ttustatus template's, verbatim where
# possible, so the pages look like one family. Weather-specific components follow.
PAGE_CSS = """  body { margin: 0; }
  .page {
    --bg: #fbfaf7; --card: #ffffff; --ink: #23282c; --muted: #5f6a72;
    --faint: #8b959c; --line: #e3e1da; --accent: #0e6a63;
    --good: #1e7d4f; --goodbg: #e3efe9; --warn: #a2620d; --warnbg: #f6ecdc;
    --neut: #5f6a72; --neutbg: #efede8;
    --codebg: #f1efe9; --mastline: #23282c; --nightrow: rgba(0,0,0,.035);
    background: var(--bg); color: var(--ink);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 24px 24px 52px; min-height: 100vh;
  }
  .page.night {
    --bg: #0f141d; --card: #161d29; --ink: #e9edf4; --muted: #94a0b4;
    --faint: #67748a; --line: #263042; --accent: #e3a548;
    --good: #57c98f; --goodbg: rgba(87,201,143,.13); --warn: #e0b25e; --warnbg: rgba(224,178,94,.13);
    --neut: #94a0b4; --neutbg: #1c2534;
    --codebg: #161d29; --mastline: #3a4558; --nightrow: rgba(255,255,255,.03);
  }
  @media (max-width: 600px) { .page { padding: 14px 16px 40px; } }
  .wrap { max-width: 1120px; margin: 0 auto; }
  .mono { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
  a { color: var(--accent); }

  .mast { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; flex-wrap: wrap;
          border-bottom: 2px solid var(--mastline); padding-bottom: 14px; }
  .mast h1 { font-family: Georgia, "Times New Roman", serif; font-size: 28px; font-weight: 600; margin: 0; letter-spacing: .01em; }
  .mastright { display: flex; align-items: flex-end; gap: 18px; }
  .when { text-align: right; }
  .when .t { font-size: 27px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .when .d { font-size: 13px; color: var(--muted); }
  .modebtn {
    font: 13px system-ui, sans-serif; color: var(--muted); background: var(--card);
    border: 1px solid var(--line); border-radius: 999px; padding: 7px 14px; cursor: pointer;
    display: inline-flex; align-items: center; gap: 7px; margin-bottom: 4px;
  }
  .modebtn:hover { color: var(--ink); border-color: var(--muted); }
  .modebtn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  .pills { display: flex; gap: 8px; flex-wrap: wrap; margin: 14px 0 0; }
  .pill { display: inline-flex; align-items: center; gap: 7px; font-size: 12.5px;
          padding: 5px 12px; border-radius: 999px; border: 1px solid var(--line);
          background: var(--card); color: var(--ink); white-space: nowrap; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--good); flex: none; }
  .dot.off { background: var(--faint); }
  .dot.warnc { background: var(--warn); }

  .tile { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; min-width: 0; }
  .tile .k { font-size: 11.5px; color: var(--muted); letter-spacing: .06em; text-transform: uppercase; margin-bottom: 6px; }
  .tile .v { font-size: 24px; font-weight: 600; line-height: 1.1; overflow-wrap: anywhere; }
  .tile .v.txt { font-size: 16px; font-weight: 500; line-height: 1.3; }
  .tile .s { font-size: 12.5px; color: var(--faint); margin-top: 6px; overflow-wrap: anywhere; }
  .alt { font-size: .68em; font-weight: 400; color: var(--muted); margin-left: 3px; white-space: nowrap; }

  h2 { font-size: 12px; letter-spacing: .1em; text-transform: uppercase; color: var(--muted);
       font-weight: 700; margin: 0 0 10px; padding-bottom: 6px; border-bottom: 1px solid var(--line); }
  .chip { display: inline-block; font-size: 11px; letter-spacing: 0; text-transform: none; padding: 1px 8px;
          border-radius: 999px; background: var(--neutbg); color: var(--neut); margin-left: 6px; vertical-align: 1px; white-space: nowrap; }
  .chip.warn { background: var(--warnbg); color: var(--warn); }
  .chip.off { background: var(--neutbg); color: var(--faint); }
  .chip.at { background: var(--warnbg); color: var(--warn); font-weight: 700; }
  /* damage-threat tag (TORNADO EMERGENCY, PDS, ...): maximum contrast in both palettes */
  .chip.threat { background: var(--ink); color: var(--bg); font-weight: 700; letter-spacing: .04em; }
  .unavail { color: var(--faint); font-size: 14px; margin: 4px 0 18px; }
  .muted { color: var(--faint); }
  .small { font-size: 12.5px; }
  .errbox { background: var(--warnbg); color: var(--warn); border: 1px solid var(--line); border-radius: 8px;
            padding: 10px 14px; margin: 0 0 18px; font-size: 14px; overflow-wrap: anywhere; }

  /* sticky site navigation with alert badges */
  .nav { position: sticky; top: 0; z-index: 10; background: var(--bg); display: flex; gap: 8px; flex-wrap: wrap;
         padding: 10px 0; border-bottom: 1px solid var(--line); margin: 0 0 18px; }
  .nav a.pill { text-decoration: none; }
  .nav a.pill:hover { border-color: var(--muted); }
  .badge { display: inline-block; min-width: 10px; padding: 1px 7px; border-radius: 999px; font-size: 11px;
           font-weight: 700; line-height: 16px; text-align: center; }
  /* phones: one horizontally scrollable row, so the sticky bar stays one pill high */
  @media (max-width: 600px) {
    .nav { flex-wrap: nowrap; overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width: none; }
    .nav::-webkit-scrollbar { display: none; }
    .nav a.pill { flex: none; }
  }

  /* alerts banner */
  .banner { margin: 0 0 26px; }
  .abox { background: var(--card); border: 1px solid var(--line); border-left: 6px solid var(--faint); border-radius: 8px;
          padding: 9px 14px; margin-bottom: 8px; font-size: 14px; line-height: 1.5; }
  .abox b { font-weight: 600; }
  .abox .m { color: var(--muted); font-size: 13px; }
  .abox .src { display: block; color: var(--muted); font-size: 12.5px; }
  .noalerts { display: inline-flex; align-items: center; gap: 8px; color: var(--good); font-size: 14.5px; margin: 4px 0 0; }
  .feedwarn { color: var(--warn); background: var(--warnbg); border-radius: 8px; padding: 8px 12px;
              font-size: 13.5px; margin: 0 0 10px; }

  /* per-site sections */
  /* anchor jumps land below the sticky nav; PAGE_JS keeps --navh equal to its real height */
  section.site { margin: 0 0 44px; scroll-margin-top: calc(var(--navh, 58px) + 8px); }
  .banner { scroll-margin-top: calc(var(--navh, 58px) + 8px); }
  h2.siteh { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 16px; text-transform: none;
             letter-spacing: 0; border-bottom: 2px solid var(--line); padding-bottom: 8px; margin-bottom: 16px; }
  h2.siteh .sitename { font-family: Georgia, "Times New Roman", serif; font-size: 24px; font-weight: 600; color: var(--ink); }
  h2.siteh .sitemeta { font-size: 12.5px; font-weight: 400; color: var(--muted); }
  h2.siteh a.sitelink { font-size: 12.5px; font-weight: 400; }
  .sitegrid { display: grid; grid-template-columns: minmax(0, 1fr); gap: 22px; margin-bottom: 22px; }
  @media (min-width: 900px) { .sitegrid { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); } }

  /* radar map: exactly one theme visible, matching the page style */
  figure.mapbox { margin: 0; }
  .mapbox .radar-img { width: 100%; height: auto; display: block; border: 1px solid var(--line); border-radius: 10px; }
  .mapbox .mapmiss { display: block; box-sizing: border-box; width: 100%; min-height: 220px; padding: 40px 16px;
                     border: 1px dashed var(--line); border-radius: 10px; background: var(--card);
                     color: var(--faint); text-align: center; font-size: 14px; }
  #page:not(.night) .radar-dark { display: none; }
  #page.night .radar-light { display: none; }
  figcaption { font-size: 12.5px; color: var(--faint); margin-top: 8px; line-height: 1.5; }
  .legend { display: flex; flex-wrap: wrap; gap: 4px 14px; margin-top: 6px; color: var(--muted); }
  .sw { display: inline-block; width: 12px; height: 12px; border-radius: 3px; vertical-align: -2px;
        margin-right: 5px; border: 1px solid rgba(0,0,0,.3); }

  .now { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-bottom: 18px; }
  @media (min-width: 560px) and (max-width: 899px) { .now { grid-template-columns: repeat(4, minmax(0, 1fr)); } }

  /* alert cards */
  .acard { background: var(--card); border: 1px solid var(--line); border-left: 6px solid var(--faint); border-radius: 8px;
           padding: 10px 14px; margin-bottom: 10px; font-size: 14px; line-height: 1.45; }
  .acard .ev { font-weight: 600; font-size: 15px; }
  .acard .hl { margin: 4px 0; }
  .acard .m { color: var(--muted); font-size: 13px; }
  .acard .nh { color: var(--muted); font-size: 12.5px; letter-spacing: .02em; margin: 0 0 4px; }
  .acard .links { font-size: 13px; margin-top: 2px; }
  .acard details, .day details { border: 0; }
  .acard details summary, .day details summary { padding: 6px 0 2px; font-size: 13px; }
  .txt { white-space: pre-wrap; font-size: 13px; color: var(--muted); margin: 6px 0; overflow-wrap: anywhere; }

  /* sun & twilight */
  .sunrow { display: flex; flex-wrap: wrap; gap: 6px 22px; font-size: 13.5px; margin: 0 0 18px; color: var(--muted); }
  .sunrow b { color: var(--ink); font-weight: 600; font-variant-numeric: tabular-nums; }
  .sunrow .k { text-transform: uppercase; letter-spacing: .06em; font-size: 11px; color: var(--faint); margin-right: 4px; }
  .sunrow .past, .sunrow .past b { color: var(--faint); font-weight: 400; }

  /* hourly table */
  .scroll { overflow-x: auto; margin: 0 0 20px; -webkit-overflow-scrolling: touch; }
  table.hr { border-collapse: collapse; font-size: 13px; width: 100%; min-width: 640px; }
  table.hr th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .07em; color: var(--faint);
                font-weight: 700; padding: 0 10px 7px 0; border-bottom: 1px solid var(--line); white-space: nowrap; }
  table.hr td { padding: 5px 10px 5px 0; border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums; white-space: nowrap; }
  table.hr td.sky { white-space: normal; min-width: 160px; }
  table.hr tr.n td { color: var(--muted); background: var(--nightrow); }
  table.hr td.hi { color: var(--accent); font-weight: 700; }

  /* 7-day cards */
  .days { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; margin: 0 0 22px; }
  .day { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; font-size: 13px; min-width: 0; }
  .day.n { background: var(--codebg); }
  .day .n { font-weight: 600; }
  .day img { width: 44px; height: 44px; border-radius: 6px; float: right; margin: 0 0 4px 6px; background: var(--neutbg); }
  .day .t { font-size: 20px; font-weight: 600; margin: 4px 0; }
  .day .s { color: var(--muted); }
  .day .txt { clear: both; }

  /* road closures (NMDOT) and NWS storm reports. Monochrome on purpose, from the page's own
     ink/background only: hazards are never green, flood alert areas are already red and the
     radar uses cyan..magenta. Same symbols as the map: an ink disc with a bar = road closed,
     a ring = water on the road, a triangle = NWS storm report. */
  .roads { margin: 0 0 18px; scroll-margin-top: calc(var(--navh, 58px) + 8px); }
  .roads h3 { font-size: 11.5px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted);
              font-weight: 700; margin: 12px 0 8px; }
  ul.rlist { list-style: none; margin: 0 0 8px; padding: 0; }
  .road { background: var(--card); border: 1px solid var(--line); border-left: 6px solid var(--ink); border-radius: 8px;
          padding: 8px 12px; margin-bottom: 8px; font-size: 14px; line-height: 1.45; overflow-wrap: anywhere; }
  .road.water { border-left-style: dashed; }
  .road.report { border-left-width: 3px; }
  .road .rh { font-weight: 600; }
  .road .rh .chip { margin-left: 0; margin-right: 6px; }
  .road .rc { margin: 3px 0; }
  .road .ns { font-style: italic; color: var(--muted); }
  .road .m { color: var(--muted); font-size: 12.5px; }
  .chip.rclosed { background: var(--ink); color: var(--bg); font-weight: 700; letter-spacing: .04em; }
  .chip.rwater { background: var(--card); color: var(--ink); box-shadow: inset 0 0 0 1px var(--ink);
                 font-weight: 700; letter-spacing: .04em; }
  .chip.onmap { background: var(--neutbg); color: var(--muted); font-weight: 400; }
  .rsym { display: inline-block; width: 11px; height: 11px; border-radius: 50%; box-sizing: border-box;
          vertical-align: -1px; margin-right: 5px; }
  .rsym.closed { background: linear-gradient(var(--ink), var(--ink)) center / 7px 2px no-repeat, var(--bg); }
  .rsym.water { background: var(--card); border: 2px solid var(--ink); }
  .rsym.report { width: 0; height: 0; border-radius: 0; border-left: 6px solid transparent;
                 border-right: 6px solid transparent; border-bottom: 10px solid var(--ink); }
  .rlinks { font-size: 13px; color: var(--muted); }
  .abox.roadbox { border-left-color: var(--ink); }

  .foot { font-size: 12px; color: var(--faint); margin-top: 18px; line-height: 1.6; }
  .foot.disclaimer, .official { color: var(--muted); }
  .official { font-size: 12.5px; margin: 8px 0 0; }
  details { border-top: 1px solid var(--line); }
  details summary { cursor: pointer; padding: 10px 0; font-size: 13px; color: var(--muted); }
  details summary:hover { color: var(--ink); }
  details pre { overflow-x: auto; background: var(--codebg); border: 1px solid var(--line); border-radius: 6px;
                padding: 12px; font-size: 12px; color: var(--muted); white-space: pre-wrap; }
"""

PAGE_JS = """  var page = document.getElementById('page');
  var btn = document.getElementById('modebtn');
  var nav = document.querySelector('.nav');
  // The server's day/night default for this render; a stored toggle applies only while it
  // is unchanged, so the sun-driven palette retakes control at the next dusk/dawn.
  var BASE = page.getAttribute('data-night-default') === '1' ? '1' : '0';
  var MODE_KEY = 'weather-mode', VIEW_KEY = 'weather-view';

  // The hidden theme's radar images wait in data-src until that theme is first shown.
  function loadMaps(night) {
    var imgs = document.querySelectorAll('img.radar-' + (night ? 'dark' : 'light') + '[data-src]');
    for (var i = 0; i < imgs.length; i++) {
      imgs[i].src = imgs[i].getAttribute('data-src');
      imgs[i].removeAttribute('data-src');
    }
  }
  function setMode(night, remember) {
    if (night) { page.classList.add('night'); } else { page.classList.remove('night'); }
    loadMaps(night);
    if (btn) {
      btn.textContent = night ? '\\u2600 Day mode' : '\\u263E Night mode';
      btn.setAttribute('aria-pressed', String(night));
    }
    if (remember) {
      try {
        sessionStorage.setItem(MODE_KEY, JSON.stringify({mode: night ? '1' : '0', base: BASE}));
      } catch (e) {}
    }
  }
  if (btn) {
    btn.addEventListener('click', function () { setMode(!page.classList.contains('night'), true); });
  }
  try {
    var st = JSON.parse(sessionStorage.getItem(MODE_KEY) || 'null');
    if (st && st.base === BASE && (st.mode === '1' || st.mode === '0')) {
      setMode(st.mode === '1', false);
    } else if (st) {
      sessionStorage.removeItem(MODE_KEY);
    }
  } catch (e) {}

  // Anchor jumps land below the sticky nav whatever its height (it wraps on narrow screens).
  function setNavH() {
    if (nav) { document.documentElement.style.setProperty('--navh', nav.offsetHeight + 'px'); }
  }
  setNavH();
  window.addEventListener('resize', setNavH);

  // Auto-refresh that keeps the viewer's place: open <details> (stable data-k keys) and the
  // scroll position survive the reload.
  function saveView() {
    var open = [], ds = document.querySelectorAll('details[data-k]');
    for (var i = 0; i < ds.length; i++) { if (ds[i].open) { open.push(ds[i].getAttribute('data-k')); } }
    try {
      sessionStorage.setItem(VIEW_KEY, JSON.stringify({open: open, y: window.pageYOffset || 0, t: Date.now()}));
    } catch (e) {}
  }
  function restoreView() {
    var v = null;
    try {
      v = JSON.parse(sessionStorage.getItem(VIEW_KEY) || 'null');
      sessionStorage.removeItem(VIEW_KEY);
    } catch (e) { return; }
    if (!v || typeof v !== 'object' || !(Date.now() - (+v.t || 0) < 600000)) { return; }
    var keys = v.open || [], ds = document.querySelectorAll('details[data-k]');
    for (var i = 0; i < ds.length; i++) {
      if (keys.indexOf(ds[i].getAttribute('data-k')) >= 0) { ds[i].open = true; }
    }
    var y = +v.y || 0;
    if (y > 0) {
      window.scrollTo(0, y);
      // Images still loading may change the layout (or cap how far we got): re-apply once
      // they are in, unless the viewer has scrolled in the meantime.
      var reached = window.pageYOffset || 0;
      window.addEventListener('load', function () {
        if (Math.abs((window.pageYOffset || 0) - reached) < 2) { window.scrollTo(0, y); }
      });
    }
  }
  restoreView();
  window.addEventListener('pagehide', saveView);
  var refresh = parseInt(page.getAttribute('data-refresh'), 10);
  if (refresh > 0) {
    setTimeout(function () { saveView(); window.location.reload(); }, refresh * 1000);
  }
"""


# ---- page blocks --------------------------------------------------------------------
def _title(cfg: Any) -> str:
    """The page title: ``cfg.page_title`` (``WEATHER_TITLE``, else derived from the site
    names), else a plain ``cfg.title`` for a config object without that property."""
    try:
        title = getattr(cfg, "page_title", None)
    except Exception:           # noqa: BLE001 - a broken site list must not cost the page
        title = None
    return str(title or getattr(cfg, "title", None) or "Local weather")


def _head_html(cfg) -> str:
    # The refresh is done by PAGE_JS (it keeps open <details> and the scroll position); the
    # meta refresh inside <noscript> is the fallback for viewers without JavaScript.
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<noscript><meta http-equiv="refresh" content="%d"></noscript>\n'
        '<title>%s</title>\n'
        '<style>\n%s</style>\n</head>\n<body>\n'
        % (int(cfg.refresh_seconds), _e(_title(cfg)), PAGE_CSS)
    )


def _masthead_html(cfg, gen: datetime, first_tz: str, night: bool) -> str:
    """Title, the generation time (first site's local clock + date, and UTC) and the
    day/night button, laid out exactly like the template's masthead."""
    btn = "☀ Day mode" if night else "☾ Night mode"
    local_t = util.fmt_local(gen, first_tz, "%H:%M %Z")
    local_d = util.fmt_local(gen, first_tz, "%A, %B %-d, %Y")
    utc = gen.astimezone(timezone.utc).strftime("%H:%M")
    return (
        '<div class="mast">\n'
        '  <h1>%s</h1>\n'
        '  <div class="mastright">\n'
        '    <button class="modebtn" id="modebtn" type="button" aria-pressed="%s">%s</button>\n'
        '    <div class="when">\n'
        '      <div class="t mono">%s</div>\n'
        '      <div class="d">%s · %s UTC</div>\n'
        '    </div>\n'
        '  </div>\n'
        '</div>\n'
        % (_e(_title(cfg)), "true" if night else "false", _e(btn), _e(local_t), _e(local_d), _e(utc))
    )


def _nav_html(sites: List[dict]) -> str:
    """Sticky site links; a coloured badge shows how many alerts are AT each site, in the
    colour of the top-ranked one."""
    links = []
    for s in sites:
        slug, name = _site_ident(s)
        at = [a for a in (s.get("alerts_at") or []) if _is_dict(a)] if _is_dict(s) else []
        badge, dot = "", ""
        if at:
            top = _top_alert(at) or {}
            col = _color(top.get("color"))
            badge = (' <span class="badge" style="background:%s;color:%s" title="%d alert(s) at %s">%d</span>'
                     % (col, _text_on(col), len(at), _e(name), len(at)))
            dot = " warnc"
        links.append('  <a class="pill" href="#%s"><span class="dot%s"></span>%s%s</a>\n'
                     % (_e(site_anchor(slug)), dot, _e(name), badge))
    return '<nav class="nav" aria-label="Sites">\n%s</nav>\n' % "".join(links)


def _feed_warning_html(feed: Any) -> str:
    """A visible line whenever the alert feed is not fresh: 'no alerts' must never be shown
    silently when the truth is 'could not ask'."""
    if _is_dict(feed) and feed.get("ok") and not feed.get("stale"):
        return ""
    if _is_dict(feed) and feed.get("ok"):
        return ('<p class="feedwarn">NWS alert feed: latest fetch failed (%s); showing the last good '
                'copy from %s ago.</p>\n' % (_e(feed.get("error") or "unknown error"),
                                             _e(_age_txt(feed) or "?")))
    err = feed.get("error") if _is_dict(feed) else "not fetched"
    age = _age_txt(feed)
    last = ("last good copy %s ago is too old to use" % age) if age else "no usable copy"
    return ('<p class="feedwarn">NWS alert feed unavailable: %s — %s. Alerts below may be '
            'incomplete.</p>\n' % (_e(err or "unknown error"), _e(last)))


def _banner_html(run: dict, sites: List[dict]) -> str:
    """One coloured box per distinct alert (across all sites): event, threat and severity
    chips, which sites it is AT or NEAR (linked), its time span in the first affected
    site's zone, and the issuing office plus a shortened area description, so two
    same-named alerts from different offices / for different areas can be told apart."""
    gen = _generated(run)
    distinct: Dict[Any, dict] = {}
    for s in sites:
        if not _is_dict(s):
            continue
        slug, name = _site_ident(s)
        tz = _site_tz(s)
        for rel in ("alerts_at", "alerts_near"):
            for a in s.get(rel) or []:
                if not _is_dict(a):
                    continue
                key = a.get("id") or (a.get("event"), a.get("headline"))
                d = distinct.setdefault(key, {"alert": a, "at": [], "near": []})
                d["at" if rel == "alerts_at" else "near"].append((slug, name, tz))
    parts = ['<div class="banner" id="alerts">\n', _feed_warning_html(run.get("alerts"))]
    road_line = _guard("road closures banner", _roads_banner_html, sites)
    if not distinct:
        parts.append('<h2>NWS alerts</h2>\n<p class="noalerts"><span class="dot"></span>'
                     'No NWS alerts for these sites.</p>\n%s%s</div>\n'
                     % (road_line, BANNER_DISCLAIMER_HTML))
        return "".join(parts)
    order = sorted(distinct.values(), key=lambda d: (_rank(d["alert"]), str(d["alert"].get("event"))))
    parts.append('<h2>NWS alerts <span class="chip warn">%d active</span></h2>\n' % len(order))
    for d in order:
        a = d["alert"]
        where = []
        for label, lst in (("at", d["at"]), ("near", d["near"])):
            if lst:
                # site names contain commas ("Town, ST"), so a comma list would be ambiguous
                links = " · ".join('<a href="#%s">%s</a>' % (_e(site_anchor(sl)), _e(nm))
                                   for sl, nm, _ in lst)
                where.append("%s %s" % (label, links))
        tz = (d["at"] or d["near"])[0][2]
        sev = a.get("severity")
        chip = (' <span class="chip">%s</span>' % _e(sev)) if sev and sev != "Unknown" else ""
        src = []
        if _office(a):
            src.append(_e(_office(a)))
        area = str(a.get("area_desc") or "").strip()
        if area:
            short = _short_area(area)
            title = (' title="%s"' % _e(area)) if short != area else ""
            src.append('<span%s>%s</span>' % (title, _e(short)))
        src_html = ('<span class="src">%s</span>' % " · ".join(src)) if src else ""
        parts.append('<div class="abox" style="border-left-color:%s"><b>%s</b>%s%s — %s '
                     '<span class="m">· %s</span>%s</div>\n'
                     % (_color(a.get("color")), _e(a.get("event") or "Alert"), _threat_chip(a), chip,
                        "; ".join(where), _e(_span_txt(a, tz, gen)), src_html))
    parts.append(road_line)
    parts.append(BANNER_DISCLAIMER_HTML)
    parts.append('</div>\n')
    return "".join(parts)


def _site_head_html(s: dict, gen: datetime, tz: str) -> str:
    slug, name = _site_ident(s)
    site = s.get("site") or {}
    meta = s.get("meta") if _is_dict(s.get("meta")) else {}
    bits = []
    try:
        bits.append("%.4f, %.4f" % (float(site.get("lat")), float(site.get("lon"))))
    except (TypeError, ValueError):
        pass
    if meta.get("ok") and meta.get("grid_id"):
        office = meta.get("office") or meta.get("grid_id")
        bits.append("NWS %s %s,%s" % (office, meta.get("grid_x"), meta.get("grid_y")))
    elif meta.get("ok"):
        office = meta.get("office")
        bits.append("NWS %s, no forecast grid" % office if office else "no NWS forecast grid")
    elif not meta.get("ok"):
        bits.append("NWS metadata unavailable")
    bits.append("local time " + util.fmt_local(gen, tz))
    url = _point_url(site)
    link = ('<a class="sitelink" href="%s" rel="noopener">NWS point forecast</a>' % _e(url)) if url else ""
    return ('<h2 class="siteh"><span class="sitename">%s</span>'
            '<span class="sitemeta mono">%s</span>%s</h2>\n'
            % (_e(name), _e(" · ".join(bits)), link))


def _legend_html(s: dict) -> str:
    """The map legend, answering 'what does that shaded area mean?' truthfully:

    * ``Shaded:`` — a swatch + event for every alert (AT or NEAR) whose mask was actually
      drawn on at least one theme map (``alerts_drawn_ids``);
    * ``Listed, not shaded:`` — alerts on the page that are not on the map (an AT alert
      whose outline could not be fetched, or whose polygon misses the frame);
    * ``No map rendered`` when neither theme's map was written.

    Run dicts without ``alerts_drawn_ids`` (older builds) count NEAR alerts and AT alerts
    that have a geometry as drawn. De-duplicated by (event, colour), in rank order."""
    at = [a for a in (s.get("alerts_at") or []) if _is_dict(a)]
    near = [a for a in (s.get("alerts_near") or []) if _is_dict(a)]
    maps = s.get("maps") if _is_dict(s.get("maps")) else {}
    if not at and not near:
        return '<div class="legend">No alert areas drawn.</div>'
    if not any(_ok(m) for m in maps.values()):
        return '<div class="legend">No map rendered; the alerts are listed as text.</div>'
    ids = s.get("alerts_drawn_ids")
    if isinstance(ids, (list, tuple, set)):
        drawn = {i for i in ids if isinstance(i, str)}
    else:
        drawn = {a.get("id") for a in near} | {a.get("id") for a in at if a.get("geometry") is not None}
    shaded: Dict[Tuple[str, str], None] = {}
    unshaded: Dict[Tuple[str, str], None] = {}
    for a in sorted(at + near, key=_rank):
        key = (str(a.get("event") or "Alert"), _color(a.get("color")))
        # Two same-named alerts, one drawn and one not, appear in both groups: the colour
        # is on the map, but not for every alert of that name listed on the page.
        (shaded if a.get("id") in drawn else unshaded).setdefault(key, None)

    def items(group: Dict[Tuple[str, str], None]) -> str:
        return ''.join('<span><span class="sw" style="background:%s"></span>%s</span>' % (col, _e(ev))
                       for ev, col in group)

    out = []
    if shaded:
        out.append('<div class="legend"><span>Shaded:</span>%s</div>' % items(shaded))
    else:
        out.append('<div class="legend">No alert areas drawn.</div>')
    if unshaded:
        out.append('<div class="legend"><span>Listed, not shaded:</span>%s</div>' % items(unshaded))
    return "".join(out)


def _map_html(cfg, run: dict, s: dict, tz: str, gen_ts: float) -> str:
    """Both themes' maps; CSS shows the one matching the palette. Only the server-default
    theme's image gets a real ``src``: the other one's URL waits in ``data-src`` (with a
    1-pixel placeholder) until PAGE_JS shows that theme, so browsers do not download a
    PNG per site that is ``display: none``."""
    slug, name = _site_ident(s)
    maps = s.get("maps") if _is_dict(s.get("maps")) else {}
    default_theme = "dark" if run.get("night_default") else "light"
    parts = ['<figure class="mapbox">\n']
    for theme in ("dark", "light"):
        m = maps.get(theme) if _is_dict(maps.get(theme)) else {}
        base = os.path.basename(str(m.get("basename") or cfg.map_basename(slug, theme)))
        if m.get("ok"):
            bust = _mtime_bust(os.path.join(cfg.out_dir, base), gen_ts)
            layers = ("MRMS radar, NWS alert areas, road closures and basemap"
                      if _drawn_road_ids(_site_roads(s)) else "MRMS radar, NWS alert areas and basemap")
            alt = ("%s for %g by %g miles around %s (%s theme)"
                   % (layers, cfg.map_miles, cfg.map_miles, name, theme))
            url = "%s?t=%d" % (base, bust)
            if theme == default_theme:
                src = 'src="%s"' % _e(url)
            else:
                src = 'src="%s" data-src="%s"' % (_PLACEHOLDER_SRC, _e(url))
            parts.append('  <img class="radar-img radar-%s" %s alt="%s">\n' % (theme, src, _e(alt)))
        else:
            parts.append('  <div class="mapmiss radar-%s">Radar map unavailable (%s theme): %s</div>\n'
                         % (theme, theme, _e(m.get("error") or "not rendered")))
    radar = run.get("radar")
    if _is_dict(radar) and radar.get("ok") and isinstance(radar.get("ts"), datetime):
        ts = radar["ts"]
        frame = "MRMS frame %s · %s" % (ts.astimezone(timezone.utc).strftime("%H:%MZ"),
                                        util.fmt_local(ts, tz, "%H:%M %Z"))
        if radar.get("stale"):
            frame += " · STALE (%s old%s)" % (_age_txt(radar) or "?",
                                              ", %s" % radar["error"] if radar.get("error") else "")
    elif _is_dict(radar) and radar.get("not_applicable"):
        frame = "No radar here: MRMS covers only the contiguous US"
    elif _is_dict(radar):
        frame = "Radar unavailable: %s" % (radar.get("error") or "no frame")
    else:
        frame = "Radar layer not rendered this run"
    lines = ["%s · %g × %g mi centred on the site" % (frame, cfg.map_miles, cfg.map_miles)]
    for note in s.get("map_notes") or []:
        lines.append(str(note))
    parts.append('  <figcaption>%s%s</figcaption>\n</figure>\n'
                 % ("<br>".join(_e(x) for x in lines), _legend_html(s)))
    return "".join(parts)


def _tile(k: str, v: str, sub: str = "", txt: bool = False) -> str:
    cls = "v txt" if txt else "v"
    sub_html = ('<div class="s">%s</div>' % sub) if sub else ""
    return '<div class="tile"><div class="k">%s</div><div class="%s">%s</div>%s</div>\n' % (_e(k), cls, v, sub_html)


def _now_html(s: dict, units: _Units, tz: str) -> str:
    """Current conditions as a tile grid. Values are already converted by nws.py; here we
    only choose the primary unit and render '—' for anything missing."""
    obs = s.get("obs")
    head = '<h2>Now%s</h2>\n' % _stale_chip(obs)
    if not _ok(obs):
        return head + _unavail("Current conditions", obs)
    feels = obs.get("heat_index_f") if obs.get("heat_index_f") is not None else obs.get("wind_chill_f")
    feels_sub = ("feels like " + units.temp(feels, None)) if feels is not None else ""
    wind_dir = obs.get("wind_dir") or util.deg_to_cardinal(obs.get("wind_dir_deg"))
    wind = units.speed(obs.get("wind_mph"), obs.get("wind_kmh"))
    if wind != DASH and wind_dir:
        wind = "%s %s" % (_e(wind_dir), wind)
    gust = units.speed(obs.get("gust_mph"), obs.get("gust_kmh"))
    gust_sub = ("gusts " + gust) if gust != DASH else ""
    layers = obs.get("cloud_layers") or []
    sky_sub = _e(", ".join(str(x) for x in layers)) if layers else ""
    ts = util.parse_iso(obs.get("timestamp"))
    age_min = obs.get("age_min")
    when = []
    if ts is not None:
        when.append(util.fmt_local(ts, tz, "%H:%M %Z"))
    if age_min is not None:
        when.append("%s ago" % util.fmt_age(float(age_min) * 60.0))
    st_sub = _e(" · ".join([str(obs.get("station_name") or "")] + when).strip(" ·"))
    tiles = [
        _tile("Temperature", units.temp(obs.get("temp_f"), obs.get("temp_c")), feels_sub),
        _tile("Dew point", units.temp(obs.get("dewpoint_f"), obs.get("dewpoint_c"))),
        _tile("Humidity", _pct(obs.get("rh"))),
        _tile("Wind", wind, gust_sub),
        _tile("Pressure", units.pressure(obs.get("pressure_inhg"), obs.get("pressure_hpa"))),
        _tile("Visibility", units.distance(obs.get("visibility_mi"), obs.get("visibility_km"))),
        _tile("Sky", _e(obs.get("text") or DASH), sky_sub, txt=True),
        _tile("Station", _e(obs.get("station_id") or DASH), st_sub, txt=True),
    ]
    return head + '<div class="now">\n%s</div>\n' % "".join(tiles)


def _alert_card_html(a: dict, rel: str, tz: str, s: Optional[dict] = None,
                     gen: Optional[datetime] = None) -> str:
    s = s if _is_dict(s) else {}
    gen = gen if isinstance(gen, datetime) else util.utcnow()
    slug, _ = _site_ident(s) if s else ("site", "site")
    chips = []
    for key in ("severity", "urgency"):
        v = a.get(key)
        if v and str(v) != "Unknown":
            chips.append('<span class="chip">%s</span>' % _e(v))
    rel_cls, rel_txt = ("at", "AT THIS SITE") if rel == "AT" else ("near", "NEARBY")
    chips.append('<span class="chip %s">%s</span>' % (rel_cls, rel_txt))
    meta = [_span_txt(a, tz, gen)]
    if a.get("sender"):
        meta.append(str(a["sender"]))
    if a.get("area_desc"):
        meta.append(str(a["area_desc"]))
    headline = str(a.get("headline") or "")
    nws_hl = a.get("nws_headline")
    nh_html = ""
    if isinstance(nws_hl, str) and nws_hl.strip() and nws_hl.strip().lower() != headline.strip().lower():
        nh_html = '  <div class="nh">%s</div>\n' % _e(nws_hl.strip())
    body = []
    if a.get("description"):
        body.append('<div class="txt">%s</div>' % _e(a["description"]))
    if a.get("instruction"):
        body.append('<div class="txt"><b>Instruction:</b> %s</div>' % _e(a["instruction"]))
    details = ""
    if body:
        key = "%s:%s" % (slug, a.get("id") or a.get("event") or "alert")
        details = ('  <details data-k="%s"><summary>Details</summary>%s</details>\n'
                   % (_e(key), "".join(body)))
    links = []
    url, is_hazard = _hazard_url(a, s, rel) if s else (None, False)
    if url:
        links.append('<a href="%s" rel="noopener">%s</a>'
                     % (_e(url), "Full text on weather.gov" if is_hazard else "NWS forecast for this site"))
    web = _https(a.get("web"))
    # api.weather.gov usually sends the bare weather.gov home page here: not worth a link
    if web and web.rstrip("/") not in ("https://www.weather.gov", "https://weather.gov"):
        links.append('<a href="%s" rel="noopener">NWS alert page</a>' % _e(web))
    links_html = ('  <div class="links">%s</div>\n' % " · ".join(links)) if links else ""
    return (
        '<article class="acard" style="border-left-color:%s">\n'
        '  <div class="ev">%s%s %s</div>\n'
        '  <div class="hl">%s</div>\n'
        '%s'
        '  <div class="m">%s</div>\n'
        '%s%s'
        '</article>\n'
        % (_color(a.get("color")), _e(a.get("event") or "Alert"), _threat_chip(a), " ".join(chips),
           _e(headline), nh_html, _e(" · ".join(meta)), links_html, details)
    )


def _alert_cards_html(s: dict, tz: str, gen: Optional[datetime] = None) -> str:
    at = [a for a in (s.get("alerts_at") or []) if _is_dict(a)]
    near = [a for a in (s.get("alerts_near") or []) if _is_dict(a)]
    if not at and not near:
        return '<h2>Alerts</h2>\n<p class="unavail">No NWS alerts at or near this site.</p>\n'
    parts = ['<h2>Alerts <span class="chip warn">%d at site · %d nearby</span></h2>\n' % (len(at), len(near))]
    for a in at:
        parts.append(_guard("alert card", _alert_card_html, a, "AT", tz, s, gen))
    for a in near:
        parts.append(_guard("alert card", _alert_card_html, a, "NEAR", tz, s, gen))
    return "".join(parts)


# ---- road closures: NMDOT events and NWS storm reports -------------------------------
# Inputs (DESIGN.md, roads contract R1/R4): ``run["roads"] = {"nmdot": <fetch_nm_roads
# status>, "lsr": <fetch_storm_reports status>}`` and per site ``s["roads"] = {"covers_nm",
# "events", "reports", "drawn_ids"}``; either may be missing (older run dicts, roads
# disabled) and then nothing road-related is rendered. The events arrive already filtered
# (New Mexico, emergencies only) and ordered by roads.site_roads; the page keeps that order.
_NMROADS_URL = "https://nmroads.com/"
_CC0_URL = "https://creativecommons.org/publicdomain/zero/1.0/"
_IEM_URL = "https://mesonet.agron.iastate.edu/"
_NM_TZ = "America/Denver"           # NMDOT's own clock, for the feed time in the footer
_ROAD_KINDS = {"closure": ("rclosed", "closed", "ROAD CLOSED"),
               "water": ("rwater", "water", "WATER ON ROAD")}
# An area-wide NMDOT condition ("... exist throughout the Socorro - 41-57 area."): one point
# for a whole patrol area, listed but not drawn on the map.
_AREA_LABELS = {"closure": "ROADS CLOSED IN AREA", "water": "WATER ON ROADS IN AREA"}
_JUNK_ROUTES = ("", "-", "null", "null-null", "none")
_NO_DIRECTION = ("", "na", "n/a", "none", "null", "unknown", "-")
_ROADS_HEADING = "Road closures (New Mexico, emergencies only)"
_ROADS_OUTSIDE = ("Road closures cover New Mexico state routes only; this map is outside "
                  "New Mexico.")
_ROADS_NONE = "No emergency road closures reported on New Mexico state routes within this map."
# ... and when NWS storm reports are listed below: they may well report a closure NMDOT has
# not posted (live 2026-09-22: "NM 304 closed at MM 11"), so "no closures" would be untrue.
_ROADS_NONE_NMDOT = ("NMDOT lists no emergency road closures within this map; see the NWS "
                     "storm reports below.")
_TEXT_MAX = 400                     # longest cause / remark / title shown (roads.py cuts to 400)
# Page credits for the road sources, as fixed markup (links to fixed hosts only). The plain
# text reads "Road information: NMDOT / NMRoads.com (public domain, CC0); storm reports: NWS
# via Iowa Environmental Mesonet", followed by NMDOT's own disclaimer.
ROADS_CREDIT_HTML = (
    'Road information: <a href="%s" rel="noopener">NMDOT / NMRoads.com</a> (public domain, '
    '<a href="%s" rel="noopener">CC0</a>); storm reports: NWS via '
    '<a href="%s" rel="noopener">Iowa Environmental Mesonet</a>. '
    "NMDOT's disclaimer: road information may not reflect all incidents or road conditions; "
    "it is not to be your only source." % (_NMROADS_URL, _CC0_URL, _IEM_URL)
)


def _cfg_flag(cfg: Any, name: str, default: Any) -> Any:
    """A roads setting from the config, with the contract default when the field is absent
    (a config object from before the roads fields, or a stub in a test)."""
    v = getattr(cfg, name, None)
    return default if v is None else v


def _float_or_none(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _clip(text: Any, limit: int = _TEXT_MAX) -> str:
    """Whitespace collapsed and cut to ``limit`` characters at a word (with an ellipsis)."""
    t = " ".join(str(text or "").split())
    if len(t) <= limit:
        return t
    return t[:limit].rsplit(" ", 1)[0].rstrip(" ,;.") + "…"


def _site_roads(s: Any) -> Optional[dict]:
    r = s.get("roads") if _is_dict(s) else None
    return r if _is_dict(r) else None


def _road_list(r: Optional[dict], key: str) -> List[dict]:
    """The events (``key="events"``) or storm reports listed for one site: nothing for a map
    outside New Mexico (``covers_nm`` False), whatever the lists hold."""
    if not r or not r.get("covers_nm", True):
        return []
    items = r.get(key)
    return [x for x in items if _is_dict(x)] if isinstance(items, (list, tuple)) else []


def _drawn_road_ids(r: Optional[dict]) -> set:
    ids = r.get("drawn_ids") if r else None
    return {i for i in ids if isinstance(i, str)} if isinstance(ids, (list, tuple, set)) else set()


def _road_key(ev: dict) -> Any:
    """Identity of an event across sites (maps overlap: NM 252 is on two of them)."""
    i = ev.get("id")
    if isinstance(i, str) and i:
        return i
    return (str(ev.get("title") or ""), str(ev.get("route") or ""), str(ev.get("mm_from")))


def _local_short(dt: datetime, tz: str, gen: datetime) -> str:
    """'15:30 MDT' on the generation's local date, else 'Tue Sep 22 15:46 MDT'."""
    same_day = util.to_local(dt, tz).date() == util.to_local(gen, tz).date()
    return util.fmt_local(dt, tz, "%H:%M %Z" if same_day else "%a %b %-d %H:%M %Z")


def _ago(dt: datetime, gen: datetime) -> str:
    secs = (gen - dt).total_seconds()
    return "just now" if secs < 60 else "%s ago" % util.fmt_age(secs)


def _route_txt(v: Any) -> Optional[str]:
    t = " ".join(str(v).split()) if isinstance(v, str) else ""
    return None if t.lower() in _JUNK_ROUTES else t


def _direction_txt(v: Any) -> Optional[str]:
    """NMDOT directions ('both', 'northbound and southbound', 'eastbound', 'NA') as words."""
    t = " ".join(str(v).split()).lower() if isinstance(v, str) else ""
    if t in _NO_DIRECTION:
        return None
    if t.startswith("both") or " and " in t:
        return "both directions"
    return t


def _mm_txt(ev: dict) -> Optional[str]:
    """'mile 17–19' for a stretch, 'mile 43' for a spot. NMDOT fills the unused end of a
    spot event ('at mile marker 43') with 0, so a 0 is dropped when the title says 'at'."""
    a, b = _float_or_none(ev.get("mm_from")), _float_or_none(ev.get("mm_to"))
    title = str(ev.get("title") or "").lower()
    if "at mile marker" in title and "from mile marker" not in title:
        if a == 0 and b:
            a = None
        elif b == 0 and a:
            b = None
    vals = [x for x in (a, b) if x is not None]
    if not vals:
        return None
    lo, hi = min(vals), max(vals)
    return ("mile %g" % lo) if lo == hi else ("mile %g–%g" % (lo, hi))


def _road_event_html(cfg, ev: dict, tz: str, gen: datetime, drawn: set) -> str:
    """One NMDOT event: kind chip (monochrome, from the page ink), route / mile markers /
    direction, the cause text (or "cause not stated"), the NMDOT title with its place names,
    and 'NMDOT · updated <age> ago (<local time>)' with a 'may have reopened' flag once the
    event has not been updated for ``cfg.roads_old_days``."""
    kind = str(ev.get("kind") or "")
    cls, sym, label = _ROAD_KINDS.get(kind, ("", "", ""))
    area = ev.get("area_wide") is True
    if area and kind in _AREA_LABELS:
        label = _AREA_LABELS[kind]
    if cls:
        chip = ('<span class="chip %s"><span class="rsym %s" aria-hidden="true"></span>%s</span>'
                % (cls, sym, label))
    else:                           # not a kind this page knows: say what NMDOT calls it
        chip = '<span class="chip">%s</span>' % _e(str(ev.get("category") or "road event").upper())
    route = _route_txt(ev.get("route"))
    title = _clip(ev.get("title"))
    head = []
    if route:
        head.append("<b>%s</b>" % _e(route))
        for bit in (_mm_txt(ev), _direction_txt(ev.get("direction"))):
            if bit:
                head.append(_e(bit))
    else:                           # 'null-null' road names: the title is all there is
        head.append(_e(title or ev.get("category") or "New Mexico road"))
    onmap = ' <span class="chip onmap">on map</span>' if _road_key(ev) in drawn else ""
    if area:
        onmap = ' <span class="chip onmap">whole area, not drawn on the map</span>'
    cause = _clip(ev.get("cause"))
    if ev.get("cause_known") is False or not cause:
        cause_html = '<span class="ns">cause not stated</span>' + (" · %s" % _e(cause) if cause else "")
    else:
        cause_html = _e(cause)
    where = ('  <div class="m">%s</div>\n' % _e(title)) if route and title else ""
    when = _as_dt(ev.get("updated"))
    verb = "updated"
    if when is None:
        when, verb = _as_dt(ev.get("posted")), "posted"
    src = str(ev.get("source") or "NMDOT")
    if when is not None and ev.get("time_approx") is True:
        # JSON-only mode: the map feed's creation date, up to ~7 h early
        meta = "%s · %s about %s (time approximate)" % (src, verb, _ago(when, gen))
    elif when is not None:
        meta = "%s · %s %s (%s)" % (src, verb, _ago(when, gen), _local_short(when, tz, gen))
    else:
        meta = "%s · update time not given" % src
    old = ""
    age_days = (gen - when).total_seconds() / 86400.0 if when is not None else None
    old_days = _float_or_none(_cfg_flag(cfg, "roads_old_days", 3.0))
    if age_days is not None and old_days is not None and age_days > old_days:
        n = int(age_days)
        old = (' <span class="chip warn">not updated for %d day%s, may have reopened</span>'
               % (n, "" if n == 1 else "s"))
    return ('<li class="road %s">\n'
            '  <div class="rh">%s %s%s</div>\n'
            '  <div class="rc">%s</div>\n'
            '%s'
            '  <div class="m">%s%s</div>\n'
            '</li>\n'
            % (_e(kind or "other"), chip, " · ".join(head), onmap, cause_html, where, _e(meta), old))


def _storm_report_html(rep: dict, tz: str, gen: datetime, drawn: set) -> str:
    """One NWS Local Storm Report that mentions a closed or flooded road: report time,
    place and county, type, the remark, and 'may have reopened' (a report never says when a
    road opens again)."""
    when = _as_dt(rep.get("time"))
    bits = ["<b>%s</b>" % _e(_local_short(when, tz, gen)) if when is not None else "<b>time not given</b>"]
    place = " ".join(str(rep.get("place") or "").split())
    county = " ".join(str(rep.get("county") or "").split())
    if county and not county.lower().endswith("county"):
        county += " County"
    loc = ", ".join(x for x in (place, county) if x)
    if loc:
        bits.append(_e(loc))
    typ = " ".join(str(rep.get("type") or "").split())
    if typ:
        bits.append(_e(typ.capitalize()))
    onmap = ' <span class="chip onmap">on map</span>' if _road_key(rep) in drawn else ""
    meta = ["%s storm report" % (" ".join(str(rep.get("source") or "NWS").split()) or "NWS")]
    if when is not None:
        meta.append(_ago(when, gen))
    meta.append("may have reopened")
    return ('<li class="road report">\n'
            '  <div class="rh"><span class="rsym report" aria-hidden="true"></span>%s%s</div>\n'
            '  <div class="rc">%s</div>\n'
            '  <div class="m">%s</div>\n'
            '</li>\n'
            % (" · ".join(bits), onmap, _e(_clip(rep.get("remark")) or DASH), _e(" · ".join(meta))))


def _roads_feed_html(nmdot: Any) -> str:
    """A visible line when the NMDOT feed is stale or down: 'no closures' must never be
    shown silently when the truth is 'could not ask'."""
    if not _is_dict(nmdot):
        return ""
    if nmdot.get("ok") and not nmdot.get("stale"):
        if not nmdot.get("error"):
            return ""
        # one of the two NMDOT files is missing (e.g. rss.xml down): the closures are
        # listed, but without their causes, and water on the road cannot be detected
        return ('<p class="feedwarn">NMDOT road feed incomplete (%s).</p>\n'
                % _e(nmdot.get("error")))
    if nmdot.get("ok"):
        err = _e(nmdot.get("error") or "unknown error")
        return ('<p class="feedwarn">NMDOT road feed: latest fetch failed (%s); showing the last '
                'good copy from %s ago.</p>\n' % (err, _e(_age_txt(nmdot) or "?")))
    return ('<p class="feedwarn">NMDOT road feed unavailable (%s); road closures cannot be '
            'listed right now.</p>\n' % _e(nmdot.get("error") or "unknown error"))


def _roads_html(cfg, run: dict, s: dict, tz: str, gen: datetime) -> str:
    """The per-site block 'Road closures (New Mexico, emergencies only)', after the alert
    cards; '' when the run carries no road data for this site."""
    r = _site_roads(s)
    if r is None:
        return ""
    slug, _ = _site_ident(s)
    open_tag = '<div class="roads" id="%s">\n' % _e(roads_anchor(slug))
    if not r.get("covers_nm", True):
        return ('%s<h2>%s</h2>\n<p class="unavail">%s</p>\n</div>\n'
                % (open_tag, _e(_ROADS_HEADING), _e(_ROADS_OUTSIDE)))
    roads = run.get("roads") if _is_dict(run.get("roads")) else {}
    nmdot = roads.get("nmdot")
    lsr = roads.get("lsr")
    events = _road_list(r, "events")
    reports = _road_list(r, "reports")
    drawn = _drawn_road_ids(r)
    n_closed = sum(1 for ev in events if ev.get("kind") == "closure")
    n_water = sum(1 for ev in events if ev.get("kind") == "water")
    counts = [txt for n, txt in ((n_closed, "%d closed" % n_closed),
                                 (n_water, "%d water on road" % n_water)) if n]
    chip = (' <span class="chip">%s</span>' % " · ".join(counts)) if counts else ""
    parts = [open_tag, '<h2>%s%s</h2>\n' % (_e(_ROADS_HEADING), chip), _roads_feed_html(nmdot)]
    if events:
        parts.append('<ul class="rlist">\n')
        for ev in events:
            parts.append(_guard("road event", _road_event_html, cfg, ev, tz, gen, drawn))
        parts.append('</ul>\n')
    elif not (_is_dict(nmdot) and not nmdot.get("ok")):
        none = _ROADS_NONE_NMDOT if reports else _ROADS_NONE
        parts.append('<p class="unavail">%s</p>\n' % _e(none))
    hours = _num(_cfg_flag(cfg, "lsr_hours", 24)) or "24"
    if reports:
        parts.append('<h3>NWS storm reports (last %s h)</h3>\n<ul class="rlist">\n' % _e(hours))
        for rep in reports:
            parts.append(_guard("storm report", _storm_report_html, rep, tz, gen, drawn))
        parts.append('</ul>\n')
    elif _cfg_flag(cfg, "lsr_enabled", True) and _is_dict(lsr) and not lsr.get("ok"):
        parts.append('<p class="unavail small">NWS storm reports unavailable (%s).</p>\n'
                     % _e(lsr.get("error") or "unknown error"))
    links = ['<a href="%s" rel="noopener">NMRoads map</a>' % _NMROADS_URL]
    feed_time = _as_dt(nmdot.get("feed_time")) if _is_dict(nmdot) else None
    if feed_time is not None:
        links.append("NMDOT feed as of %s" % _e(_local_short(feed_time, tz, gen)))
    parts.append('<div class="rlinks">%s</div>\n</div>\n' % " · ".join(links))
    return "".join(parts)


def _roads_banner_html(sites: List[dict]) -> str:
    """The banner line 'Emergency road closures (NM): N closed, M water on road' with links
    to the road blocks that list them; '' when no site lists any. Events shown on several
    maps count once."""
    closed: Dict[Any, None] = {}
    water: Dict[Any, None] = {}
    where = []
    for s in sites:
        events = _road_list(_site_roads(s), "events")
        listed = False
        for ev in events:
            if ev.get("kind") == "closure":
                closed.setdefault(_road_key(ev), None)
                listed = True
            elif ev.get("kind") == "water":
                water.setdefault(_road_key(ev), None)
                listed = True
        if listed:
            slug, name = _site_ident(s)
            where.append('<a href="#%s">%s</a>' % (_e(roads_anchor(slug)), _e(name)))
    if not where:
        return ""
    return ('<div class="abox roadbox"><b>Emergency road closures (NM): %d closed, %d water on '
            'road</b> <span class="m">— %s</span></div>\n' % (len(closed), len(water), " · ".join(where)))


def _sun_phase(sun: dict) -> Optional[str]:
    """sun_summary's ``phase`` (day / civil, nautical, astronomical twilight / night); for
    an older run dict without it, derived from the altitude with the same bands."""
    ph = sun.get("phase")
    if isinstance(ph, str) and ph.strip():
        return ph.strip()
    try:
        alt = float(sun.get("sun_alt_deg"))
    except (TypeError, ValueError):
        return None
    if math.isnan(alt):
        return None
    for limit, name in _PHASE_BANDS:
        if alt >= limit:
            return name
    return "night"


def _sun_html(s: dict, tz: str, gen: Optional[datetime] = None) -> str:
    """'Sun now <alt>° (<phase>)', today's sunrise / solar noon / sunset, then every
    twilight event from 30 min ago to 24 h ahead (``upcoming``, passed ones muted) — so at
    03:00 the coming dawn is listed, not tomorrow evening's. Times are rounded to the
    nearest minute here, at display (sun.py keeps sub-minute precision), and carry the
    weekday when they fall on another local date than today's."""
    sun = s.get("sun")
    if not _is_dict(sun):
        return ""
    now = sun.get("now_local") if isinstance(sun.get("now_local"), datetime) else gen
    now = now if isinstance(now, datetime) else util.utcnow()
    today_date = util.to_local(now, tz).date()

    def t(dt: Any) -> str:
        if not isinstance(dt, datetime):
            return DASH
        loc = util.to_local(dt + timedelta(seconds=30), tz)
        return loc.strftime("%H:%M" if loc.date() == today_date else "%a %H:%M")

    today = sun.get("today") if _is_dict(sun.get("today")) else {}
    alt = _num(sun.get("sun_alt_deg"), 1)
    phase = _sun_phase(sun)
    first = ['<span><span class="k">Sun now</span><b>%s</b>%s</span>'
             % (_e((alt + "°") if alt is not None else DASH), (" (%s)" % _e(phase)) if phase else "")]
    for label, key in (("Sunrise", "sunrise"), ("Solar noon", "solar_noon"), ("Sunset", "sunset")):
        first.append('<span><span class="k">%s</span><b>%s</b></span>' % (label, _e(t(today.get(key)))))
    rows = ['<div class="sunrow">%s</div>\n' % "".join(first)]
    items = []
    for ev in sun.get("upcoming") or []:
        if not _is_dict(ev) or not isinstance(ev.get("dt"), datetime):
            continue
        past = bool(ev.get("passed"))
        items.append('<span%s>%s <b>%s</b></span>'
                     % (' class="past" title="passed"' if past else "", _e(ev.get("label") or ""),
                        _e(t(ev["dt"]))))
    if items:
        rows.append('<div class="sunrow"><span class="k">Next 24 h</span>%s</div>\n' % "".join(items))
    zone = util.fmt_local(now, tz, "%Z")
    return '<h2>Sun &amp; twilight <span class="chip">times in %s</span></h2>\n%s' % (_e(zone), "".join(rows))


def _hourly_html(cfg, s: dict, units: _Units, tz: str) -> str:
    h = s.get("hourly")
    head = '<h2>Next %d hours%s</h2>\n' % (int(cfg.hourly_hours), _stale_chip(h))
    if not _ok(h):
        return head + _unavail("Hourly forecast", h)
    hours = [x for x in (h.get("hours") or []) if _is_dict(x)][:int(cfg.hourly_hours)]
    if not hours:
        return head + _unavail("Hourly forecast", {"error": "no periods in the product"})
    rows = []
    for hr in hours:
        pop = hr.get("pop")
        hi = pop is not None and _num(pop) is not None and float(pop) >= float(cfg.pop_highlight_pct)
        local = hr.get("local") or ("%s %s" % (hr.get("day") or "", hr.get("hour") or "")).strip()
        wind = units.speed(hr.get("wind_mph"), hr.get("wind_kmh"))
        if wind != DASH and hr.get("wind_dir"):
            wind = "%s %s" % (_e(hr["wind_dir"]), wind)
        rows.append(
            '<tr class="%s"><td class="mono">%s</td><td>%s</td><td class="%s">%s</td><td>%s</td>'
            '<td>%s</td><td>%s</td><td class="sky">%s</td></tr>\n'
            % ("d" if hr.get("is_day", True) else "n", _e(local),
               units.temp(hr.get("temp_f"), hr.get("temp_c")),
               "hi" if hi else "", _pct(pop), _pct(hr.get("rh")),
               units.temp(hr.get("dewpoint_f"), hr.get("dewpoint_c")), wind,
               _e(hr.get("short") or DASH)))
    upd = util.parse_iso(h.get("updated"))
    sub = (' <span class="chip">NWS update %s</span>' % _e(util.fmt_local(upd, tz))) if upd else ""
    head = '<h2>Next %d hours%s%s</h2>\n' % (int(cfg.hourly_hours), sub, _stale_chip(h))
    return (head + '<div class="scroll"><table class="hr">\n<thead><tr><th>Local</th><th>Temp</th>'
            '<th>Precip</th><th>Humidity</th><th>Dew pt</th><th>Wind</th><th>Sky</th></tr></thead>\n'
            '<tbody>\n%s</tbody></table></div>\n' % "".join(rows))


def _forecast_html(cfg, s: dict, units: _Units, tz: str) -> str:
    f = s.get("forecast")
    head = '<h2>7-day forecast%s</h2>\n' % _stale_chip(f)
    if not _ok(f):
        return head + _unavail("7-day forecast", f)
    periods = [p for p in (f.get("periods") or []) if _is_dict(p)][:int(cfg.forecast_periods)]
    if not periods:
        return head + _unavail("7-day forecast", {"error": "no periods in the product"})
    slug, _ = _site_ident(s)
    cards = []
    for idx, p in enumerate(periods, 1):
        icon = _https(p.get("icon"))
        short = str(p.get("short") or "")
        img = ('<img src="%s" alt="%s" width="44" height="44" loading="lazy">' % (_e(icon), _e(short))
               if icon else "")
        temp = units.temp(p.get("temp_f"), p.get("temp_c"))
        trend = (' <span class="alt">%s</span>' % _e(p.get("temp_trend"))) if p.get("temp_trend") else ""
        wind = units.wind_text(p.get("wind"))
        if wind != DASH and p.get("wind_dir"):
            wind = "%s %s" % (_e(p["wind_dir"]), wind)
        pnum = _num(p.get("number")) or str(idx)
        details = ('<details data-k="%s:p%s"><summary>Details</summary><div class="txt">%s</div></details>'
                   % (_e(slug), _e(pnum), _e(p["detailed"]))) if p.get("detailed") else ""
        cards.append(
            '<div class="day %s">%s<div class="n">%s</div><div class="t">%s%s</div>'
            '<div class="s">Precip %s · %s</div><div class="s">%s</div>%s</div>\n'
            % ("d" if p.get("is_day", True) else "n", img, _e(p.get("name") or DASH), temp, trend,
               _pct(p.get("pop")), wind, _e(short or DASH), details))
    upd = util.parse_iso(f.get("updated"))
    sub = (' <span class="chip">NWS update %s</span>' % _e(util.fmt_local(upd, tz))) if upd else ""
    head = '<h2>7-day forecast%s%s</h2>\n' % (sub, _stale_chip(f))
    return head + '<div class="days">\n%s</div>\n' % "".join(cards)


def _site_section_html(cfg, run: dict, s: dict, units: _Units, gen: datetime, gen_ts: float) -> str:
    slug, name = _site_ident(s)
    tz = _site_tz(s)
    parts = ['<section class="site" id="%s">\n' % _e(site_anchor(slug))]
    parts.append(_guard("site heading", _site_head_html, s, gen, tz))
    parts.append('<div class="sitegrid">\n<div>\n')
    parts.append(_guard("radar map", _map_html, cfg, run, s, tz, gen_ts))
    parts.append('</div>\n<div>\n')
    parts.append(_guard("current conditions", _now_html, s, units, tz))
    parts.append(_guard("alerts", _alert_cards_html, s, tz, gen))
    parts.append(_guard("road closures", _roads_html, cfg, run, s, tz, gen))
    parts.append('</div>\n</div>\n')
    if s.get("sun"):
        parts.append(_guard("sun and twilight", _sun_html, s, tz, gen))
    parts.append(_guard("hourly forecast", _hourly_html, cfg, s, units, tz))
    parts.append(_guard("7-day forecast", _forecast_html, cfg, s, units, tz))
    parts.append('</section>\n')
    return "".join(parts)


def _site_error_html(s: Any, err: str) -> str:
    """Fallback for a site whose whole section failed: keep the anchor so the nav link
    still lands somewhere, and say what happened."""
    slug, name = _site_ident(s)
    box = _error_box("This site could not be rendered: " + err)
    return ('<section class="site" id="%s">\n<h2 class="siteh"><span class="sitename">%s</span></h2>\n%s'
            '</section>\n' % (_e(site_anchor(slug)), _e(name), box))


# Page credits as HTML with links (OpenStreetMap's licence terms ask for a link to its
# copyright page). Fixed page code, not ``cfg.attribution``: that plain-text line is what
# the radar PNGs carry, and config text is never emitted as markup.
ATTRIBUTION_HTML = (
    'Map data © <a href="https://www.openstreetmap.org/copyright" rel="noopener">OpenStreetMap</a> '
    'contributors · Radar: NOAA/NSSL MRMS via '
    '<a href="https://mesonet.agron.iastate.edu/" rel="noopener">Iowa Environmental Mesonet</a> · '
    'Forecast &amp; alerts: <a href="https://www.weather.gov/" rel="noopener">NOAA/NWS</a>'
)

# The page's own disclaimer, on every page whatever the configuration (NMDOT's disclaimer
# for the road data is separate and shown only with road data). Fixed markup, fixed link.
DISCLAIMER_HTML = (
    'Not an official product: this page is not from, or endorsed by, NOAA, the National '
    'Weather Service or any other agency, and it must not be the only source for safety '
    'decisions. It can be late, incomplete or wrong whenever a source is. For official '
    'forecasts, watches and warnings use '
    '<a href="https://www.weather.gov/" rel="noopener">weather.gov</a>.'
)
# ... and its short form under the alerts banner, where people look for warnings.
BANNER_DISCLAIMER_HTML = (
    '<p class="official">Not an official NWS product; it can be late or incomplete. '
    'For warnings use <a href="https://www.weather.gov/" rel="noopener">weather.gov</a>.</p>\n'
)


def _footer_html(cfg, run: dict, sites: List[dict], gen: datetime) -> str:
    pills = []
    dot, txt = _src_status(run.get("alerts"))
    pills.append('<span class="pill"><span class="dot%s"></span>Alerts: %s</span>' % (dot, _e(txt)))
    radar = run.get("radar")
    dot, txt = _src_status(radar)
    if _is_dict(radar) and radar.get("not_applicable"):
        dot, txt = " off", "not applicable (no map in the MRMS grid)"
    elif _is_dict(radar) and isinstance(radar.get("ts"), datetime):
        txt = "frame %s · %s" % (radar["ts"].astimezone(timezone.utc).strftime("%H:%MZ"), txt)
    pills.append('<span class="pill"><span class="dot%s"></span>Radar: %s</span>' % (dot, _e(txt)))
    for s in sites:
        if not _is_dict(s):
            continue
        slug, name = _site_ident(s)
        srcs = [("obs", s.get("obs")), ("forecast", s.get("forecast")), ("hourly", s.get("hourly"))]
        worst = ""
        for _, d in srcs:
            if not _ok(d):
                worst = " off"
                break
            if d.get("stale"):
                worst = " warnc"
        label = " · ".join("%s %s" % (k, _short_status(d)) for k, d in srcs)
        pills.append('<span class="pill"><span class="dot%s"></span>%s: %s</span>' % (worst, _e(name), _e(label)))
    pills.extend(_roads_pills(cfg, run, gen))
    errors = [str(x) for x in (run.get("errors") or [])]
    err_html = ""
    if errors:
        err_html = ('<details data-k="run:problems"><summary>%d problem(s) during this run</summary>'
                    '<pre>%s</pre></details>\n' % (len(errors), _e("\n".join(errors))))
    page_url = str(getattr(cfg, "page_url", "") or "").strip()
    url = _https(page_url)
    url_html = ('<a href="%s">%s</a>' % (_e(url), _e(url))) if url else _e(page_url)
    stamp = "generated %s · refreshes every %d s" % (
        gen.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), int(cfg.refresh_seconds))
    return (
        '<h2>Sources</h2>\n<div class="pills">\n%s\n</div>\n%s%s'
        '<p class="foot disclaimer">%s</p>\n'
        '<p class="foot">%s<br>%s</p>\n'
        % ("\n".join(pills), err_html, _roads_credit_html(cfg, run, sites), DISCLAIMER_HTML,
           ATTRIBUTION_HTML, (url_html + " · " + _e(stamp)) if page_url else _e(stamp))
    )


def _roads_in_run(run: dict, sites: List[dict]) -> bool:
    """True when this run carries road data at all (older run dicts, or roads disabled:
    no road pill, credit or status problem)."""
    return _is_dict(run.get("roads")) or any(_site_roads(s) is not None for s in sites)


def _roads_pills(cfg, run: dict, gen: datetime) -> List[str]:
    """Footer pills for the NMDOT feed (with its Last-Modified time) and, when enabled, the
    NWS storm reports; none for a run without ``run["roads"]``."""
    roads = run.get("roads")
    if not _is_dict(roads):
        return []
    out = []
    nmdot = roads.get("nmdot")
    dot, txt = _src_status(nmdot)
    if _is_dict(nmdot) and nmdot.get("ok") and not nmdot.get("stale") and nmdot.get("error"):
        dot, txt = " warnc", "incomplete · %s" % nmdot["error"]
    feed_time = _as_dt(nmdot.get("feed_time")) if _is_dict(nmdot) else None
    if feed_time is not None:
        txt = "feed %s · %s" % (_local_short(feed_time, _NM_TZ, gen), txt)
    out.append('<span class="pill"><span class="dot%s"></span>Roads (NMDOT): %s</span>' % (dot, _e(txt)))
    if _cfg_flag(cfg, "lsr_enabled", True):
        dot, txt = _src_status(roads.get("lsr"))
        out.append('<span class="pill"><span class="dot%s"></span>NWS storm reports: %s</span>'
                   % (dot, _e(txt)))
    return out


def _roads_credit_html(cfg, run: dict, sites: List[dict]) -> str:
    """Credits, NMDOT's disclaimer and what the road blocks do and do not list (the scoping
    switches WEATHER_ROADS_WATER / WEATHER_LSR change the text), with the numbers of NMDOT
    items left out as not emergencies (``excluded``)."""
    if not _roads_in_run(run, sites):
        return ""
    listed = ["closures caused by emergencies (flooding, washouts, rock and mud slides, fire, "
              "crashes, hazardous materials, snow and ice, wind and dust, storm damage, law "
              "enforcement) and NMDOT closures that state no cause"]
    if _cfg_flag(cfg, "roads_water", True):
        listed.append("water on the road (NMDOT driving-condition reports of flooding or "
                      "standing water)")
    if _cfg_flag(cfg, "lsr_enabled", True):
        listed.append("NWS storm reports from New Mexico of the last %s h that mention a closed "
                      "or flooded road" % (_num(_cfg_flag(cfg, "lsr_hours", 24)) or "24"))
    scope = ("Listed under road closures: %s. Not listed: roadwork, construction, lane, scheduled and "
             "seasonal closures, special events, oversize loads, missile-range and rest-area "
             "notices, and roads on the White Sands missile range." % "; ".join(listed))
    roads = run.get("roads") if _is_dict(run.get("roads")) else {}
    nmdot = roads.get("nmdot") if _is_dict(roads.get("nmdot")) else {}
    excluded = nmdot.get("excluded") if _is_dict(nmdot.get("excluded")) else {}
    counts = []
    for cat, n in excluded.items():
        v = _float_or_none(n)
        if v is not None and v > 0:
            counts.append((-int(v), str(cat)))
    if counts:
        scope += " Left out right now: %s." % " · ".join(
            "%d %s" % (-n, cat) for n, cat in sorted(counts))
    return '<p class="foot">%s<br>%s</p>\n' % (ROADS_CREDIT_HTML, _e(scope))


def _render(cfg, run: dict) -> str:
    run = run if _is_dict(run) else {}
    gen = _generated(run)
    gen_ts = _generated_ts(run, gen)
    sites = [s for s in (run.get("sites") or []) if _is_dict(s)]
    first_tz = _site_tz(sites[0]) if sites else "UTC"
    night = bool(run.get("night_default"))
    units = _Units(cfg.units)
    parts = [_head_html(cfg)]
    parts.append('<div class="%s" id="page" data-night-default="%s" data-refresh="%d">\n<div class="wrap">\n'
                 % ("page night" if night else "page", "1" if night else "0", int(cfg.refresh_seconds)))
    parts.append(_guard("masthead", _masthead_html, cfg, gen, first_tz, night))
    parts.append(_guard("navigation", _nav_html, sites))
    parts.append(_guard("alerts banner", _banner_html, run, sites))
    if not sites:
        parts.append(_error_box("No site data in this run."))
    for s in sites:
        try:
            parts.append(_site_section_html(cfg, run, s, units, gen, gen_ts))
        except Exception as e:  # noqa: BLE001 — one bad site must not kill the page
            log.exception("site %s failed to render: %s", _site_ident(s)[0], e)
            parts.append(_site_error_html(s, "%s: %s" % (type(e).__name__, e)))
    parts.append(_guard("footer", _footer_html, cfg, run, sites, gen))
    parts.append('</div>\n</div>\n<script>\n%s</script>\n</body>\n</html>\n' % PAGE_JS)
    return "".join(parts)


# ---- public API ---------------------------------------------------------------------
def render_html(cfg, run: dict) -> str:
    """The whole page as a string. Never raises: if even the skeleton fails, a minimal page
    that states the error is returned (the timer must still have something to publish)."""
    try:
        return _render(cfg, run)
    except Exception as e:  # noqa: BLE001
        log.exception("page render failed: %s", e)
        return (
            '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta http-equiv="refresh" content="%d"><title>%s</title></head>\n'
            '<body><div class="page" id="page"><h1>%s</h1><p>Page generation failed: %s</p>'
            '</div></body></html>\n'
            % (int(getattr(cfg, "refresh_seconds", 300)), _e(_title(cfg)),
               _e(_title(cfg)), _e("%s: %s" % (type(e).__name__, e)))
        )


def _atomic_write_text(path: str, text: str) -> None:
    """tmp + fsync + os.replace: a crash mid-write must never leave a half page behind."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            # data on disk before the rename is: on XFS a crash right after os.replace could
            # otherwise leave a zero-length index.html in place
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def write_page(cfg, run: dict) -> str:
    """Render and atomically write ``<out_dir>/index.html``; returns the path. Rendering
    never raises, but an unwritable output directory does (OSError) — that is a deployment
    error the CLI must report with a non-zero exit."""
    path = os.path.join(cfg.out_dir, "index.html")
    _atomic_write_text(path, render_html(cfg, run))
    log.info("wrote %s", path)
    return path


def _problems(run: dict, cfg: Any = None) -> List[str]:
    """Short, stable strings for everything that makes this run less than complete: a
    source that is unavailable or serving a last-good (stale) copy, a map that was not
    written, errors logged during the run. Details are in ``errors``. The road sources
    count only while roads are enabled (``cfg.roads_enabled``, storm reports also
    ``cfg.lsr_enabled``) and the run carries ``run["roads"]``."""
    probs: List[str] = []

    def source(label: str, d: Any) -> None:
        if _is_dict(d) and (d.get("not_provided") or d.get("not_applicable")):
            return      # nothing NWS provides here / no map in the MRMS grid: not a fault
        if not _is_dict(d):
            probs.append("%s: missing" % label)
        elif not d.get("ok"):
            probs.append("%s: unavailable" % label)
        elif d.get("stale"):
            probs.append("%s: stale" % label)

    source("alerts feed", run.get("alerts"))
    source("radar", run.get("radar"))
    roads = run.get("roads")
    if _is_dict(roads) and _cfg_flag(cfg, "roads_enabled", True):
        source("NMDOT road feed", roads.get("nmdot"))
        nmdot = roads.get("nmdot")
        if _is_dict(nmdot) and nmdot.get("ok") and not nmdot.get("stale") \
                and nmdot.get("error"):
            probs.append("NMDOT road feed: incomplete")
        if _cfg_flag(cfg, "lsr_enabled", True):
            source("NWS storm reports", roads.get("lsr"))
    sites = [s for s in (run.get("sites") or []) if _is_dict(s)]
    if not sites:
        probs.append("no sites in this run")
    for s in sites:
        slug, _ = _site_ident(s)
        for key, label in (("meta", "metadata"), ("obs", "observation"), ("forecast", "forecast"),
                           ("hourly", "hourly forecast")):
            source("%s %s" % (slug, label), s.get(key))
        maps = s.get("maps") if _is_dict(s.get("maps")) else {}
        if not maps:
            probs.append("%s maps: missing" % slug)
        for theme in sorted(maps):
            if not _ok(maps[theme]):
                probs.append("%s %s map: not written" % (slug, theme))
    net = run.get("network")
    if _is_dict(net):
        if net.get("exhausted"):
            probs.append("network: run budget of %s s used up" % _num(net.get("budget_s")))
        for host in net.get("down_hosts") or []:
            probs.append("network: %s unreachable" % host)
    errors = run.get("errors") or []
    if errors:
        probs.append("%d error(s) logged in this run" % len(errors))
    return probs


def _status_doc(cfg, run: dict) -> dict:
    """``page_written``: index.html is at least as new as this run. ``degraded``: any
    source unavailable or stale, any map not written, or any error logged. ``ok`` is
    ``page_written and not degraded`` — the one flag a monitor should alert on (together
    with the age of ``generated``)."""
    run = run if _is_dict(run) else {}
    gen = _generated(run)
    gen_ts = _generated_ts(run, gen)
    index = os.path.join(cfg.out_dir, "index.html")
    try:
        # "page written" == index.html is at least as new as this run (1 s slack for
        # coarse filesystem timestamps); an older file means this run's write failed.
        page_written = os.path.getmtime(index) >= gen_ts - 1.0
    except OSError:
        page_written = False
    feed = run.get("alerts") if _is_dict(run.get("alerts")) else {}
    radar = run.get("radar") if _is_dict(run.get("radar")) else {}
    ts = radar.get("ts")
    sites: Dict[str, dict] = {}
    for s in run.get("sites") or []:
        if not _is_dict(s):
            continue
        slug, _ = _site_ident(s)
        maps = s.get("maps") if _is_dict(s.get("maps")) else {}
        sites[slug] = {
            "meta_ok": _ok(s.get("meta")),
            "obs_ok": _ok(s.get("obs")),
            "forecast_ok": _ok(s.get("forecast")),
            "hourly_ok": _ok(s.get("hourly")),
            "alerts_at": len(s.get("alerts_at") or []),
            "alerts_near": len(s.get("alerts_near") or []),
            "maps_ok": bool(maps) and all(_ok(m) for m in maps.values()),
            "roads_events": len(_road_list(_site_roads(s), "events")),
            "roads_reports": len(_road_list(_site_roads(s), "reports")),
        }
    problems = _problems(run, cfg)
    degraded = bool(problems)
    net = run.get("network") if _is_dict(run.get("network")) else None
    if net is not None:
        net = {"budget_s": net.get("budget_s"), "left_s": net.get("left_s"),
               "exhausted": bool(net.get("exhausted")),
               "down_hosts": [str(h) for h in (net.get("down_hosts") or [])]}
    return {
        "generated": gen.isoformat(),
        "page_written": bool(page_written),
        "degraded": degraded,
        "ok": bool(page_written) and not degraded,
        "problems": problems,
        "alerts": {"ok": bool(feed.get("ok")), "stale": bool(feed.get("stale")),
                   "count": len(feed.get("alerts") or [])},
        "radar": (RADAR_NOT_APPLICABLE if radar.get("not_applicable") else
                  {"ok": bool(radar.get("ok")), "stale": bool(radar.get("stale")),
                   "ts": ts.isoformat() if isinstance(ts, datetime) else None}),
        "network": net,
        "roads": _roads_status(run),
        "sites": sites,
        "errors": [str(x) for x in (run.get("errors") or [])],
    }


ROADS_NOT_APPLICABLE = "not applicable"
RADAR_NOT_APPLICABLE = "not applicable"     # no site's map reaches the MRMS grid


def _roads_status(run: dict) -> Any:
    """status.json ``roads``: None for a run without road data (roads switched off, or an
    older run dict), ``"not applicable"`` when roads are on but no site's map reaches New
    Mexico (``run["roads_not_applicable"]``: nothing was fetched), else ``{"nmdot": {ok,
    stale, events, feed_time}, "lsr": {ok, stale, reports}}``. ``events`` / ``reports``
    count the distinct items listed on the page (an item on two maps counts once): the run
    dict carries the sources' statuses, not their item lists."""
    roads = run.get("roads")
    if not _is_dict(roads):
        return ROADS_NOT_APPLICABLE if run.get("roads_not_applicable") else None
    events: Dict[Any, None] = {}
    reports: Dict[Any, None] = {}
    for s in run.get("sites") or []:
        r = _site_roads(s)
        for ev in _road_list(r, "events"):
            events.setdefault(_road_key(ev), None)
        for rep in _road_list(r, "reports"):
            reports.setdefault(_road_key(rep), None)
    nmdot = roads.get("nmdot") if _is_dict(roads.get("nmdot")) else {}
    lsr = roads.get("lsr") if _is_dict(roads.get("lsr")) else {}
    feed_time = _as_dt(nmdot.get("feed_time"))
    return {"nmdot": {"ok": bool(nmdot.get("ok")), "stale": bool(nmdot.get("stale")),
                      "events": len(events),
                      "feed_time": feed_time.isoformat() if feed_time is not None else None},
            "lsr": {"ok": bool(lsr.get("ok")), "stale": bool(lsr.get("stale")),
                    "reports": len(reports)}}


def write_status_json(cfg, run: dict) -> str:
    """Write ``<out_dir>/status.json``, a small machine-readable health summary for
    monitoring; returns the path. Like the page, a bad run dict yields a document that says
    so rather than an exception."""
    try:
        doc = _status_doc(cfg, run)
    except Exception as e:  # noqa: BLE001
        log.exception("status summary failed: %s", e)
        msg = "status summary failed: %s: %s" % (type(e).__name__, e)
        doc = {"generated": util.utcnow().isoformat(), "page_written": False, "degraded": True,
               "ok": False, "problems": [msg], "alerts": {}, "radar": {}, "network": None,
               "roads": None, "sites": {},
               "errors": [msg]}
    path = os.path.join(cfg.out_dir, "status.json")
    _atomic_write_text(path, json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")
    return path
