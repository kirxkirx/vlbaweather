"""The NWS raw gridpoint forecast (``https://api.weather.gov/gridpoints/{office}/{x},{y}``)
for the VLBA page: the hourly series the 7-day and hourly products do not carry (cloud cover,
the probability of thunder, snowfall and ice accumulation, wind gusts, the "weather" entries)
that the GOOD / BAD verdict needs. The run-length expansion is ttustatus's
``safety/nws_forecast.py`` (``_expand`` / ``_dur_hours``) ported to the parent's style.

Why it looks the way it does:

* The gridpoint product is run-length encoded: every value carries a ``validTime`` such as
  ``2026-10-08T08:00:00+00:00/PT2H`` (start / ISO-8601 duration). ``expand`` turns a series
  into ``{hour: value}`` keyed by aware UTC datetimes on the hour; ``fetch`` reads the
  current UTC hour and the ones after it out of the expanded series. A null inside a series
  stays None (NWS says "no value", so do we).
* Fetching goes through ``weather.nws._fetch`` / ``weather.http.cached_json`` with the
  parent's status keys (``ok / stale / error / age_s``), validated before caching because
  api.weather.gov answers HTTP 200 problem documents now and then. The cache key is
  ``gridpoint/<lat>,<lon>`` (the point, never the slug: a site that moves must not inherit
  the old grid). Without usable metadata (NWS down) the last good copy is served for
  ``cfg.gridpoint_max_stale``, labelled stale, like every other NWS product.
* Nothing here raises on data problems. A malformed entry is skipped with a log line; a
  malformed series is listed in ``unusable`` (the verdict: "unknown"); a series NWS does
  not produce for a grid (HFO and SJU have no probabilityOfThunder, SJU no snowfall or ice
  accumulation: an empty ``values`` list) is listed in ``missing`` (the verdict: "not
  provided"). Either way its column is None, never a fake 0: the distinction between
  "0 %" and "not forecast" matters.
* Precipitation, snowfall and ice are amounts over each run (often 6 h): every hour of the
  run carries the run's total, and ``periods`` says which run, so a reason can name it.
* Units are what the API sends (the raw gridpoint is always SI): percent, mm, km/h, degC,
  metres. Values are floats or None; nothing is rounded here, the page formats.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from weather import nws, util

log = logging.getLogger("vlba.gridpoint")

# Row key -> property of the gridpoint payload, in the column order of the page.
FIELDS: Dict[str, str] = {
    "sky_cover_pct": "skyCover",
    "pop_pct": "probabilityOfPrecipitation",
    "thunder_pct": "probabilityOfThunder",
    "qpf_mm": "quantitativePrecipitation",
    "snow_mm": "snowfallAmount",
    "ice_mm": "iceAccumulation",
    "wind_kmh": "windSpeed",
    "gust_kmh": "windGust",
    "temp_c": "temperature",
    "rh_pct": "relativeHumidity",
    "weather": "weather",
}
WEATHER_KEYS = ("coverage", "weather", "intensity")     # what a row's weather entry keeps
# Amounts accumulated over the run they belong to (a PT6H run's value is the 6-hour total,
# repeated in every hour of the run): a row gives their run in ``periods``.
ACCUMULATIONS = ("qpf_mm", "snow_mm", "ice_mm")

# A run longer than this is truncated: the grid covers about a week, and a corrupt
# duration must not expand into millions of hours.
MAX_RUN_HOURS = 24 * 14

# A weather type is "winter" when one of its words is one of these: snow, snow_showers,
# blowing_snow, sleet, ice_pellets, freezing_rain, freezing_drizzle, and also freezing_fog,
# freezing_spray, ice_fog and ice_crystals (rime / icing on the structure). Not hail, frost.
WINTER_WORDS = frozenset(("snow", "sleet", "freezing", "ice"))

_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")
_GRID_ID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")        # goes into a URL path: keep it boring
_WORDS = re.compile(r"[^a-z0-9]+")


# ---- the run-length encoding ------------------------------------------------------------
def duration_hours(duration: Any) -> int:
    """ISO-8601 duration of a run ("PT1H", "PT2H", "P1D", "P1DT3H") -> whole hours, at least
    1. Minutes round UP to a whole hour ("PT30M" -> 1, "PT1H30M" -> 2), which is what
    ttustatus does; anything unparsable counts as one hour."""
    m = _DURATION.match(duration) if isinstance(duration, str) else None
    if not m:
        return 1
    days, hours, minutes = (int(x) if x else 0 for x in m.groups())
    return max(days * 24 + hours + (1 if minutes else 0), 1)


def expand_runs(series: Any) -> Tuple[Dict[datetime, Tuple[Any, datetime, int]], int]:
    """A gridpoint series (``{"uom": ..., "values": [{"validTime": "<start>/<duration>",
    "value": v}, ...]}``) -> (``{hour: (v, run start, run hours)}``, entries skipped), with
    one entry per hour of every run, keyed by aware UTC datetimes on the hour (a start with
    minutes is floored; any offset is converted). Null values are kept as None; a later run
    overwrites an earlier one where they overlap. A series that is None or has no
    ``values`` gives ({}, 0). An entry that is not an object or has no parsable
    ``validTime`` is skipped and counted: one bad entry days ahead must not blank the hour
    the verdict reads. ValueError for a malformed series (not an object, ``values`` not a
    list) and for one whose every entry is bad; the caller decides what that means."""
    if series is None:
        return {}, 0
    if not isinstance(series, dict):
        raise ValueError("series is not an object but %s" % type(series).__name__)
    values = series.get("values")
    if values is None:
        return {}, 0
    if not isinstance(values, list):
        raise ValueError("'values' is not a list but %s" % type(values).__name__)
    out: Dict[datetime, Tuple[Any, datetime, int]] = {}
    bad: List[str] = []
    for n, item in enumerate(values):
        if not isinstance(item, dict):
            bad.append("values[%d] is not an object" % n)
            continue
        valid = item.get("validTime")
        start_s, _, duration = (valid if isinstance(valid, str) else "").partition("/")
        start = util.parse_iso(start_s)
        if start is None:
            bad.append("values[%d] has no usable validTime: %r" % (n, valid))
            continue
        start = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        hours = min(duration_hours(duration), MAX_RUN_HOURS)
        for k in range(hours):
            out[start + timedelta(hours=k)] = (item.get("value"), start, hours)
    if bad and len(bad) == len(values):
        raise ValueError("no usable entry: %s" % bad[0])
    return out, len(bad)


def expand(series: Any) -> Dict[datetime, Any]:
    """``expand_runs`` reduced to ``{hour: value}``."""
    return {h: v[0] for h, v in expand_runs(series)[0].items()}


# ---- small helpers ----------------------------------------------------------------------
def _grid_int(v: Any) -> Optional[int]:
    """A grid coordinate: an int (not a bool) or a whole float; else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None


def grid_url(meta: Optional[dict]) -> Optional[str]:
    """The raw gridpoint URL of a site from its ``nws.site_metadata`` (``grid_id``,
    ``grid_x``, ``grid_y``), or None when the metadata has no usable grid (NWS was down,
    or the point has no forecast grid)."""
    if not isinstance(meta, dict):
        return None
    gid, x, y = meta.get("grid_id"), _grid_int(meta.get("grid_x")), _grid_int(meta.get("grid_y"))
    if not (isinstance(gid, str) and _GRID_ID.match(gid)) or x is None or y is None:
        return None
    return "%s/gridpoints/%s/%d,%d" % (nws.BASE, gid, x, y)


def _valid_gridpoint(d: Any) -> bool:
    """Worth caching: ``properties`` with at least one of our series present as an object
    (a problem document has none)."""
    p = nws._props(d)
    return bool(p and any(isinstance(p.get(name), dict) for name in FIELDS.values()))


def _utc(dt: datetime) -> datetime:
    """An aware UTC datetime (a naive one is taken as UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _text(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _weather_entries(value: Any) -> List[dict]:
    """The hour's weather value (a list of NWS entries with coverage / weather / intensity /
    visibility / attributes) -> ``[{"coverage", "weather", "intensity"}, ...]``. An entry
    with all three None means "nothing" and is dropped; so is anything else odd."""
    out: List[dict] = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict):
            continue
        kept = {k: _text(entry.get(k)) for k in WEATHER_KEYS}
        if any(kept.values()):
            out.append(kept)
    return out


def _expand_all(props: dict, slug: str) -> Tuple[Dict[str, Dict[datetime, Tuple[Any, datetime, int]]],
                                                 List[str], List[str]]:
    """Every FIELDS series expanded (``expand_runs``), the sorted row keys of the series
    that are absent or empty (``missing``: what the office does not produce, like HFO's and
    SJU's thunder probability) and of the ones that are malformed (``unusable``: logged, a
    payload problem of this run, never a quiet "not produced")."""
    maps: Dict[str, Dict[datetime, Tuple[Any, datetime, int]]] = {}
    missing: List[str] = []
    unusable: List[str] = []
    for key, name in FIELDS.items():
        try:
            m, skipped = expand_runs(props.get(name))
            if skipped:
                log.warning("%s: gridpoint series %s: %d malformed entr%s skipped", slug, name, skipped,
                            "y" if skipped == 1 else "ies")
        except Exception as e:  # noqa: BLE001 — a bad series is an unusable series, never a crash
            log.warning("%s: gridpoint series %s unusable: %s", slug, name, e)
            m = {}
            unusable.append(key)
        else:
            if not m:
                missing.append(key)
                log.debug("%s: gridpoint has no %s values", slug, name)
        maps[key] = m
    return maps, sorted(missing), sorted(unusable)


def _result(status: dict, url: Optional[str]) -> dict:
    out = dict(status)
    out.update({"url": url, "updated": None, "update_age_min": None, "elevation_m": None,
                "missing": [], "unusable": [], "hours": [], "now": None, "next": None})
    return out


# ---- the product ------------------------------------------------------------------------
def fetch(cfg, cache, site: dict, meta: Optional[dict], hours: int = 12,
          now: Optional[datetime] = None) -> dict:
    """The raw gridpoint of a site as hourly rows from the current UTC hour on. Keys:

    * ``ok / stale / error / age_s``: the parent's status keys (``http.cached_json``), plus
      ``not_provided`` True when NWS has no forecast grid here (``meta["no_grid"]``);
    * ``url``: the gridpoint URL, None without usable metadata;
    * ``updated``: the product's ``updateTime`` string, or None;
    * ``update_age_min``: minutes from ``updated`` to ``now`` (1 decimal; negative when the
      product is dated in the future), or None;
    * ``elevation_m``: the grid cell's elevation, or None;
    * ``missing``: sorted row keys whose series is absent or empty, i.e. not produced for
      this grid (meaningful only when ``ok``; an empty list otherwise);
    * ``unusable``: sorted row keys whose series is malformed this run (logged);
    * ``hours``: ``hours`` rows, one per hour from the hour containing ``now`` (UTC): ``start``
      (aware UTC datetime), ``local`` ("%a %H:%M" in the site's time zone, ``meta["tz"]``
      or UTC), every FIELDS key: a float or None, except ``weather``, a list of
      ``{"coverage", "weather", "intensity"}`` entries (empty when nothing is forecast or
      the series is missing: see ``missing``), and ``periods``: for the ACCUMULATIONS
      keys, the run the hour's amount belongs to, ``{"start": aware UTC datetime,
      "hours": n}`` (the amount is the total of the whole run);
    * ``now`` / ``next``: ``hours[0]`` / ``hours[1]``, or None.

    Rows exist even where no series covers the hour (every value None), so the window is
    always ``hours`` long when ``ok``; when not ``ok`` it is empty. ``now`` defaults to
    ``util.utcnow()``. Never raises on network or data problems."""
    slug = str(site.get("slug") or "?")
    tz = nws._tz(meta)
    if nws._not_provided(meta):
        return _result(nws._not_provided_status(), None)
    url = grid_url(meta)
    res = nws._fetch(cfg, cache, "gridpoint/%s" % nws._loc(site), url,
                     cfg.gridpoint_ttl, cfg.gridpoint_max_stale, _valid_gridpoint)
    out = _result(nws._status(res), url)
    if not res["ok"]:
        log.debug("%s: gridpoint unavailable: %s", slug, res["error"])
        return out
    now = _utc(now or util.utcnow())
    props = res["data"]["properties"]
    updated = nws._updated(props)
    out["updated"] = updated if isinstance(updated, str) else None
    upd = util.parse_iso(out["updated"])
    if upd is not None:
        out["update_age_min"] = round((now - upd).total_seconds() / 60.0, 1)
    out["elevation_m"] = nws._distance_m(props.get("elevation"))
    maps, out["missing"], out["unusable"] = _expand_all(props, slug)
    hour0 = now.replace(minute=0, second=0, microsecond=0)
    rows: List[dict] = []
    for k in range(max(0, int(hours))):
        start = hour0 + timedelta(hours=k)
        row: Dict[str, Any] = {"start": start, "local": util.fmt_local(start, tz, "%a %H:%M"), "periods": {}}
        for key in FIELDS:
            v, run_start, run_hours = maps[key].get(start) or (None, None, None)
            row[key] = _weather_entries(v) if key == "weather" else util.quantity(v)
            if key in ACCUMULATIONS and run_start is not None:
                row["periods"][key] = {"start": run_start, "hours": run_hours}
        rows.append(row)
    out["hours"] = rows
    out["now"] = rows[0] if rows else None
    out["next"] = rows[1] if len(rows) > 1 else None
    log.debug("%s: gridpoint %s updated %s (%s min ago), %d rows from %s, missing %s", slug,
              url, out["updated"], out["update_age_min"], len(rows),
              hour0.strftime("%Y-%m-%dT%H:%MZ"), ", ".join(out["missing"]) or "nothing")
    return out


# ---- the weather entries ----------------------------------------------------------------
def is_winter(weather_type: Any) -> bool:
    """Whether an NWS weather type is frozen precipitation / icing (``WINTER_WORDS``)."""
    if not isinstance(weather_type, str):
        return False
    return bool(WINTER_WORDS & set(_WORDS.split(weather_type.lower())))


def winter_weather(row: Optional[dict]) -> List[dict]:
    """The weather entries of a row (``{"coverage", "weather", "intensity"}`` dicts, as in
    ``row["weather"]``, same order, any coverage) whose type is snow, sleet, freezing rain /
    drizzle or ice (``is_winter``). The verdict combines them with their ``coverage``
    (likely / definite count); ``weather_text`` spells one out."""
    wx = row.get("weather") if isinstance(row, dict) else None
    return [e for e in (wx if isinstance(wx, list) else [])
            if isinstance(e, dict) and is_winter(e.get("weather"))]


def weather_text(entry: Optional[dict]) -> str:
    """A weather entry as words: "likely snow (light)", "slight chance rain showers",
    "definite freezing rain (heavy)"; "" for nothing."""
    if not isinstance(entry, dict):
        return ""
    cov, wx, inten = (" ".join(_WORDS.split(str(entry.get(k) or "").lower())).strip()
                      for k in WEATHER_KEYS)
    text = " ".join(p for p in (cov, wx) if p)
    if inten:
        text = "%s (%s)" % (text, inten) if text else inten
    return text
