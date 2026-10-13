"""NWS (api.weather.gov) client for local-weather-awareness: point metadata, the 7-day and hourly
gridpoint forecasts and the latest station observation, per site.

Why it looks the way it does:

* Every request goes through ``http.cached_json`` (as a module attribute, so tests can
  swap the transport), and every result carries the common status keys from DESIGN.md.
  A flaky NWS therefore degrades to "last good copy, labelled stale" rather than a blank
  section, and nothing here ever raises on network or data problems.
* Payloads are validated *before* they are cached (``validate=``): api.weather.gov happily
  answers HTTP 200 with a JSON problem document, and caching that for 30 minutes would
  blank a site for no reason.
* The cache keys name the site's point (``points/34.0584,-106.8914``, likewise
  ``forecast/``, ``hourly/``, ``stations/``), never its slug: a site that keeps its slug but
  gets new coordinates must not be served the old place's grid, zones or forecast.
* A point with zones and a time zone but no forecast grid (American Samoa) is valid
  metadata; its forecasts and observations are "not provided" (``not_provided``), not errors.
* Units are normalised here so ``page.py`` only formats numbers. The API mixes
  conventions: forecast temperatures are numbers with a ``temperatureUnit`` ("F" or "C"),
  hourly dew points are ``{"unitCode": "wmoUnit:degC", ...}`` quantities, hourly winds are
  strings like "5 to 10 mph", and observations use km/h or m/s with a ``unitCode``.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from . import http, util

log = logging.getLogger("weather.nws")

BASE = "https://api.weather.gov"
STATIONS_TTL = 7 * 86400            # a grid cell's station list practically never changes
STATIONS_MAX_STALE = 60 * 86400
MAX_STATIONS = 3                    # stations to try before declaring "no current conditions"
M_TO_FT = 3.28084

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_STATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")   # goes into a URL path: keep it boring

META_FIELDS = ("grid_id", "grid_x", "grid_y", "office", "forecast_url", "hourly_url",
               "stations_url", "tz", "city", "state", "radar_station", "forecast_zone",
               "county_zone", "fire_zone", "zone_urls", "no_grid")
OBS_FIELDS = ("station_id", "station_name", "timestamp", "age_min", "text", "icon",
              "temp_f", "temp_c", "dewpoint_f", "dewpoint_c", "rh",
              "wind_mph", "wind_kmh", "gust_mph", "gust_kmh", "wind_dir_deg", "wind_dir",
              "pressure_inhg", "pressure_hpa", "visibility_mi", "visibility_km",
              "heat_index_f", "wind_chill_f", "cloud_layers")


# ---- small helpers ---------------------------------------------------------------------
def _status(res: Dict[str, Any]) -> Dict[str, Any]:
    """The four common status keys, copied from a ``cached_json`` result."""
    return {"ok": bool(res.get("ok")), "stale": bool(res.get("stale")),
            "error": res.get("error"), "age_s": res.get("age_s")}


def _props(d: Any) -> Optional[dict]:
    p = d.get("properties") if isinstance(d, dict) else None
    return p if isinstance(p, dict) else None


def _valid_points(d: Any) -> bool:
    """A usable ``/points`` answer: a forecast grid, or at least the time zone and a zone
    (American Samoa has zones and a time zone but no forecast grid)."""
    p = _props(d)
    return bool(p and ((p.get("gridId") and p.get("forecast"))
                       or (p.get("timeZone") and (p.get("forecastZone") or p.get("county")))))


def _loc(site: dict) -> str:
    """The site's point as the cache keys and the ``/points`` URL write it ("34.0584,-106.8914").
    Cache keys name the location, not the slug: a site that keeps its slug but moves must
    never be served the old place's grid, zones, forecast or stations."""
    return "%.4f,%.4f" % (site["lat"], site["lon"])


NOT_PROVIDED = "not provided by NWS for this location (it has no forecast grid)"


def _not_provided(meta: Optional[dict]) -> bool:
    """The metadata is fine but NWS has no forecast grid here: the gridded products do not
    exist, which is not an error (``site_metadata`` sets ``no_grid``)."""
    return bool(meta and meta.get("ok") and meta.get("no_grid"))


def _not_provided_status() -> Dict[str, Any]:
    return {"ok": False, "stale": False, "error": NOT_PROVIDED, "age_s": None,
            "not_provided": True}


def _valid_forecast(d: Any) -> bool:
    """A forecast is only worth caching when it actually has periods."""
    p = _props(d)
    return bool(p and isinstance(p.get("periods"), list) and p["periods"])


def _valid_stations(d: Any) -> bool:
    return isinstance(d, dict) and (isinstance(d.get("observationStations"), list)
                                    or isinstance(d.get("features"), list))


def _valid_observation(d: Any) -> bool:
    p = _props(d)
    return bool(p and "timestamp" in p)


def _fetch(cfg, cache, key: str, url: Optional[str], ttl: float, max_stale: float,
           validate) -> Dict[str, Any]:
    """``http.cached_json`` that also copes with a missing URL (site metadata unavailable):
    the last good copy is still served for ``max_stale`` so one NWS hiccup does not take
    every product of a site down at once."""
    if url:
        return http.cached_json(cfg, cache, key, url, ttl, max_stale, validate=validate)
    err = "no URL for %s (site metadata unavailable)" % key
    old, age = cache.get_any(key)
    if old is not None and age is not None and age <= max_stale and validate(old):
        return {"data": old, "ok": True, "stale": True, "age_s": age, "error": err,
                "from_cache": True}
    return {"data": None, "ok": False, "stale": True, "age_s": age, "error": err,
            "from_cache": False}


def _tidy(v: Optional[float], nd: int = 1):
    """Round for display but keep whole numbers as ints (80, not 80.0)."""
    if v is None:
        return None
    r = round(float(v), nd)
    return int(r) if r == int(r) else r


def _int_or_none(v: Optional[float]) -> Optional[int]:
    return None if v is None else int(round(v))


def _last_segment(url: Any) -> Optional[str]:
    """'https://api.weather.gov/zones/forecast/NMZ220' -> 'NMZ220' (also accepts a bare id)."""
    if not url or not isinstance(url, str):
        return None
    return url.rstrip("/").rsplit("/", 1)[-1] or None


def _unit(q: Any) -> str:
    return str(q.get("unitCode") or "") if isinstance(q, dict) else ""


def _tz(meta: Optional[dict]) -> str:
    return (meta or {}).get("tz") or "UTC"


def _updated(props: dict) -> Optional[str]:
    # The forecast products carry ``updateTime`` (``updated`` is usually null nowadays).
    return props.get("updateTime") or props.get("updated") or props.get("generatedAt")


# ---- unit handling ---------------------------------------------------------------------
def _temp_pair(raw: Any, unit_hint: Optional[str] = None,
               default: str = "F") -> Tuple[Optional[float], Optional[float]]:
    """(°F, °C) from a forecast temperature (number + ``temperatureUnit``) or an observation
    quantity (dict with ``unitCode``). Both filled or both None."""
    v = util.quantity(raw)
    if v is None:
        return None, None
    unit = (unit_hint or _unit(raw) or default).upper()
    if unit == "C" or unit.endswith("DEGC"):
        return _tidy(util.c_to_f(v)), _tidy(v)
    return _tidy(v), _tidy(util.f_to_c(v))


def _speed_kmh(q: Any) -> Optional[float]:
    """Observation wind quantity -> km/h honouring ``unitCode`` (km_h-1, m_s-1, mi_h-1, kn)."""
    v = util.quantity(q)
    if v is None:
        return None
    u = _unit(q)
    if "m_s" in u:
        return v * 3.6
    if "mi_h" in u or u.lower().endswith("mph"):
        return util.mph_to_kmh(v)
    if u.endswith(":kn") or u == "kn":
        return v * 1.852
    return v


def _pressure_pa(q: Any) -> Optional[float]:
    v = util.quantity(q)
    if v is None:
        return None
    u = _unit(q)
    if u.endswith("hPa") or u.endswith("mbar"):
        return v * 100.0
    if u.endswith("kPa"):
        return v * 1000.0
    return v


def _distance_m(q: Any) -> Optional[float]:
    v = util.quantity(q)
    if v is None:
        return None
    u = _unit(q)
    if u.endswith(":km"):
        return v * 1000.0
    if u.endswith(":mi_i") or u.endswith(":mi"):
        return v * 1609.344
    if u.endswith(":ft"):
        return v / M_TO_FT
    return v


def parse_wind_mph(s: Any) -> Optional[float]:
    """Forecast ``windSpeed`` -> mph. The gridpoint products give a string ("10 mph",
    "10 to 15 mph"); we take the LAST number, i.e. the upper bound, because that is what
    matters for planning. A quantity dict (``units=si`` responses) is honoured too."""
    if s is None:
        return None
    if isinstance(s, dict):
        v = util.quantity(s)
        if v is None:
            return None
        return util.kmh_to_mph(v) if "km_h" in _unit(s) else v
    text = str(s)
    nums = _NUMBER.findall(text)
    if not nums:
        return None
    v = float(nums[-1])
    if "km/h" in text.lower() or "kph" in text.lower():
        v = util.kmh_to_mph(v)
    return v


def _wind_text(s: Any) -> str:
    """Keep the NWS wording for the 7-day cards; render a quantity as '<n> mph'."""
    if isinstance(s, str):
        return s
    mph = parse_wind_mph(s)
    return "" if mph is None else "%d mph" % int(round(mph))


def _cloud_layers(layers: Any) -> List[str]:
    """['FEW 1500 ft', 'BKN 600 ft', 'CLR'] — METAR style. Bases are rounded to 100 ft
    because that is the resolution of the original report (NWS gives metres rounded to
    10 m: 180 m really is BKN006 = 600 ft)."""
    out: List[str] = []
    for layer in layers or []:
        if not isinstance(layer, dict):
            continue
        amount = str(layer.get("amount") or "").strip().upper()
        if not amount:
            continue
        base_m = _distance_m(layer.get("base"))
        if base_m is None:
            out.append(amount)
        else:
            out.append("%s %d ft" % (amount, int(round(base_m * M_TO_FT / 100.0)) * 100))
    return out


# ---- /points ---------------------------------------------------------------------------
def site_metadata(cfg, cache, site: dict) -> dict:
    """Grid, product URLs, time zone and zone ids for a site. ``tz`` is always a usable
    name ("UTC" when the lookup failed) because every other product formats times with it.
    The cache key is the point (``points/<lat>,<lon>``), so moving a site fetches its new
    metadata at once. ``no_grid`` is True when NWS answers with zones and a time zone but
    no forecast grid (American Samoa): the forecasts and observations are then "not
    provided" rather than failed."""
    url = "%s/points/%s" % (BASE, _loc(site))
    res = http.cached_json(cfg, cache, "points/%s" % _loc(site), url,
                           cfg.points_ttl, cfg.points_max_stale, validate=_valid_points)
    out = _status(res)
    out.update({k: None for k in META_FIELDS})
    out["tz"] = "UTC"
    out["zone_urls"] = []
    out["no_grid"] = False
    if not res["ok"]:
        log.warning("%s: point metadata unavailable: %s", site["slug"], res["error"])
        return out
    p = res["data"]["properties"]
    out["no_grid"] = not (p.get("gridId") and p.get("forecast"))
    rel = ((p.get("relativeLocation") or {}).get("properties") or {})
    zones = [p.get("forecastZone"), p.get("county"), p.get("fireWeatherZone")]
    out.update({
        "grid_id": p.get("gridId"), "grid_x": p.get("gridX"), "grid_y": p.get("gridY"),
        "office": p.get("cwa") or p.get("gridId"),
        "forecast_url": p.get("forecast"), "hourly_url": p.get("forecastHourly"),
        "stations_url": p.get("observationStations"),
        "tz": p.get("timeZone") or "UTC",
        "city": rel.get("city"), "state": rel.get("state"),
        "radar_station": p.get("radarStation"),
        "forecast_zone": _last_segment(zones[0]), "county_zone": _last_segment(zones[1]),
        "fire_zone": _last_segment(zones[2]),
        "zone_urls": [z for z in zones if isinstance(z, str) and z],
    })
    if out["no_grid"]:
        log.info("%s: NWS has no forecast grid here (zones %s, tz %s)", site["slug"],
                 out["forecast_zone"], out["tz"])
    log.debug("%s: grid %s %s,%s tz %s", site["slug"], out["grid_id"], out["grid_x"],
              out["grid_y"], out["tz"])
    return out


# ---- 7-day forecast --------------------------------------------------------------------
def _forecast_period(p: dict) -> dict:
    f, c = _temp_pair(p.get("temperature"), p.get("temperatureUnit"))
    return {"number": p.get("number"), "name": p.get("name") or "",
            "start": p.get("startTime"), "end": p.get("endTime"),
            "is_day": bool(p.get("isDaytime")),
            "temp_f": f, "temp_c": c, "temp_trend": p.get("temperatureTrend") or None,
            "pop": _int_or_none(util.quantity(p.get("probabilityOfPrecipitation"))),
            "wind": _wind_text(p.get("windSpeed")), "wind_dir": p.get("windDirection") or "",
            "short": p.get("shortForecast") or "", "detailed": p.get("detailedForecast") or "",
            "icon": p.get("icon") or None}


def _drop_ended(periods: List[dict], now) -> List[dict]:
    """Drop the LEADING periods whose ``endTime`` is at or before ``now``. A cached copy
    (normal for up to ``forecast_ttl``) or a last-good copy during an outage would otherwise
    open with e.g. "Today, high 69" at 19:00. A period with an unparsable end is kept."""
    i = 0
    while i < len(periods):
        end = util.parse_iso(periods[i].get("endTime"))
        if end is None or end > now:
            break
        i += 1
    return periods[i:]


def forecast(cfg, cache, site: dict, meta: Optional[dict]) -> dict:
    """The day/night periods (up to ``cfg.forecast_periods``) with both °F and °C filled,
    starting with the first period that has not ended yet. ``ok`` is False (with an
    explanatory ``error``) when every period of the available copy has already ended."""
    if _not_provided(meta):
        return dict(_not_provided_status(), updated=None, periods=[])
    res = _fetch(cfg, cache, "forecast/%s" % _loc(site), (meta or {}).get("forecast_url"),
                 cfg.forecast_ttl, cfg.forecast_max_stale, _valid_forecast)
    out = _status(res)
    out.update({"updated": None, "periods": []})
    if not res["ok"]:
        return out
    props = res["data"]["properties"]
    out["updated"] = _updated(props)
    periods = _drop_ended([p for p in props["periods"] if isinstance(p, dict)], util.utcnow())
    out["periods"] = [_forecast_period(p) for p in periods[:max(0, cfg.forecast_periods)]]
    if not periods:
        # Typically a last-good copy that outlived its own periods: not usable data.
        msg = "7-day forecast has only periods that have already ended"
        out["ok"] = False
        out["error"] = "%s (%s)" % (msg, out["error"]) if out["error"] else msg
        log.warning("%s: %s", site["slug"], out["error"])
    return out


# ---- hourly forecast -------------------------------------------------------------------
def _hourly_period(p: dict, start, tz: str) -> dict:
    f, c = _temp_pair(p.get("temperature"), p.get("temperatureUnit"))
    df, dc = _temp_pair(p.get("dewpoint"), default="C")
    mph = parse_wind_mph(p.get("windSpeed"))
    return {"start": p.get("startTime"),
            "local": util.fmt_local(start, tz, "%a %H:%M"),
            "day": util.fmt_local(start, tz, "%a"), "hour": util.fmt_local(start, tz, "%H:%M"),
            "is_day": bool(p.get("isDaytime")), "temp_f": f, "temp_c": c,
            "pop": _int_or_none(util.quantity(p.get("probabilityOfPrecipitation"))),
            "rh": _int_or_none(util.quantity(p.get("relativeHumidity"))),
            "dewpoint_f": df, "dewpoint_c": dc,
            "wind_mph": _tidy(mph), "wind_kmh": _tidy(util.mph_to_kmh(mph)),
            "wind_dir": p.get("windDirection") or "", "short": p.get("shortForecast") or "",
            "icon": p.get("icon") or None}


def hourly(cfg, cache, site: dict, meta: Optional[dict]) -> dict:
    """``cfg.hourly_hours`` periods from the current local hour onward. The period that
    contains "now" is kept (its start is the top of the current hour), earlier ones are
    dropped even from a stale copy, so the table never opens in the past."""
    tz = _tz(meta)
    if _not_provided(meta):
        return dict(_not_provided_status(), updated=None, hours=[])
    res = _fetch(cfg, cache, "hourly/%s" % _loc(site), (meta or {}).get("hourly_url"),
                 cfg.hourly_ttl, cfg.hourly_max_stale, _valid_forecast)
    out = _status(res)
    out.update({"updated": None, "hours": []})
    if not res["ok"]:
        return out
    props = res["data"]["properties"]
    out["updated"] = _updated(props)
    hour_start = util.to_local(util.utcnow(), tz).replace(minute=0, second=0, microsecond=0)
    hours: List[dict] = []
    for p in props["periods"]:
        if not isinstance(p, dict):
            continue
        start = util.parse_iso(p.get("startTime"))
        if start is None or start < hour_start:
            continue
        hours.append(_hourly_period(p, start, tz))
        if len(hours) >= cfg.hourly_hours:
            break
    out["hours"] = hours
    if not hours:
        # Typically a last-good copy so old that it ends before now: not usable data.
        out["ok"] = False
        out["error"] = out["error"] or "hourly forecast has no periods from the current hour on"
        log.warning("%s: %s", site["slug"], out["error"])
    return out


# ---- latest observation ----------------------------------------------------------------
def _station_list(data: dict) -> List[Tuple[str, str]]:
    """[(id, name), ...] nearest first. ``observationStations`` carries the order; the
    ``features`` carry the names."""
    names: Dict[str, str] = {}
    order: List[Optional[str]] = []
    feats = data.get("features")
    for feat in feats if isinstance(feats, list) else []:
        if not isinstance(feat, dict):
            continue
        props = feat.get("properties") or {}
        sid = props.get("stationIdentifier") or _last_segment(feat.get("id"))
        if sid:
            names[sid] = props.get("name") or ""
            order.append(sid)
    urls = data.get("observationStations")
    if isinstance(urls, list) and urls:
        order = [_last_segment(u) for u in urls]
    out: List[Tuple[str, str]] = []
    for sid in order:
        if sid and _STATION_ID.match(sid) and sid not in [s for s, _ in out]:
            out.append((sid, names.get(sid, "")))
    return out


def _parse_observation(props: dict) -> dict:
    ts = util.parse_iso(props.get("timestamp"))
    age = util.age_minutes(ts)
    tf, tc = _temp_pair(props.get("temperature"), default="C")
    df, dc = _temp_pair(props.get("dewpoint"), default="C")
    hf, _ = _temp_pair(props.get("heatIndex"), default="C")
    wf, _ = _temp_pair(props.get("windChill"), default="C")
    wind_kmh = _speed_kmh(props.get("windSpeed"))
    gust_kmh = _speed_kmh(props.get("windGust"))
    wdir = util.quantity(props.get("windDirection"))
    # Altimeter setting first (always reported), sea-level pressure as the fallback.
    pa = _pressure_pa(props.get("barometricPressure"))
    if pa is None:
        pa = _pressure_pa(props.get("seaLevelPressure"))
    vis_m = _distance_m(props.get("visibility"))
    # NWS encodes calm as speed 0 with direction 0; a calm wind has no direction, so both the
    # bearing and the compass text are blanked (a 0.0 bearing would read as "N").
    calm = wind_kmh is not None and wind_kmh == 0
    return {"station_id": props.get("stationId") or _last_segment(props.get("station")),
            "station_name": props.get("stationName") or None,
            "timestamp": props.get("timestamp") if ts else None,
            "age_min": None if age is None else round(age, 1),
            "text": props.get("textDescription") or "", "icon": props.get("icon") or None,
            "temp_f": tf, "temp_c": tc, "dewpoint_f": df, "dewpoint_c": dc,
            "rh": _tidy(util.quantity(props.get("relativeHumidity"))),
            "wind_mph": _tidy(util.kmh_to_mph(wind_kmh)), "wind_kmh": _tidy(wind_kmh),
            "gust_mph": _tidy(util.kmh_to_mph(gust_kmh)), "gust_kmh": _tidy(gust_kmh),
            "wind_dir_deg": None if calm else wdir,
            "wind_dir": "" if calm else util.deg_to_cardinal(wdir),
            "pressure_inhg": _tidy(util.pa_to_inhg(pa), 2),
            "pressure_hpa": _tidy(util.pa_to_hpa(pa)),
            "visibility_mi": _tidy(util.m_to_mi(vis_m)), "visibility_km": _tidy(util.m_to_km(vis_m)),
            "heat_index_f": hf, "wind_chill_f": wf,
            "cloud_layers": _cloud_layers(props.get("cloudLayers"))}


def _unusable(obs: dict, max_age_min: float) -> Optional[str]:
    """Why an observation should be skipped (None = usable). A METAR with no temperature
    is usually a broken sensor; a report older than ``max_age_min`` is history, not 'now'."""
    if obs["timestamp"] is None:
        return "no timestamp"
    if obs["age_min"] is None or obs["age_min"] > max_age_min:
        return "report is %s old" % util.fmt_duration_minutes(obs["age_min"])
    if obs["temp_c"] is None:
        return "no temperature"
    return None


def observation(cfg, cache, site: dict, meta: Optional[dict]) -> dict:
    """Current conditions from the first of the nearest ``MAX_STATIONS`` stations with a
    recent, complete report. ``ok`` is False (with ``error`` naming each station's problem)
    when none qualifies."""
    slug = site["slug"]
    out: Dict[str, Any] = {"ok": False, "stale": True, "error": None, "age_s": None}
    out.update({k: None for k in OBS_FIELDS})
    out["cloud_layers"] = []
    if _not_provided(meta) and not (meta or {}).get("stations_url"):
        out.update(_not_provided_status())
        return out
    sres = _fetch(cfg, cache, "stations/%s" % _loc(site), (meta or {}).get("stations_url"),
                  STATIONS_TTL, STATIONS_MAX_STALE, _valid_stations)
    if not sres["ok"]:
        out["error"] = "station list unavailable: %s" % sres["error"]
        log.warning("%s: %s", slug, out["error"])
        return out
    stations = _station_list(sres["data"])
    if not stations:
        out["error"] = "no observation stations listed for this grid point"
        log.warning("%s: %s", slug, out["error"])
        return out
    reasons: List[str] = []
    for sid, sname in stations[:MAX_STATIONS]:
        url = "%s/stations/%s/observations/latest" % (BASE, sid)
        ores = http.cached_json(cfg, cache, "obs/%s/%s" % (slug, sid), url,
                                cfg.obs_ttl, cfg.obs_max_stale, validate=_valid_observation)
        if not ores["ok"]:
            reasons.append("%s: %s" % (sid, ores["error"]))
            continue
        obs = _parse_observation(ores["data"]["properties"])
        why = _unusable(obs, cfg.obs_max_age_min)
        if why:
            reasons.append("%s: %s" % (sid, why))
            log.info("%s: skipping station %s (%s)", slug, sid, why)
            continue
        out.update(obs)
        out["station_id"] = obs["station_id"] or sid
        out["station_name"] = obs["station_name"] or sname or sid
        out.update(_status(ores))
        log.debug("%s: using %s, %s min old", slug, out["station_id"], out["age_min"])
        return out
    out["error"] = "no usable observation: " + "; ".join(reasons)
    log.warning("%s: %s", slug, out["error"])
    return out
