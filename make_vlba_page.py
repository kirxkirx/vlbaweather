#!/usr/bin/env python3
"""vlbaweather entry point: one run of the VLBA weather-page generator.

The run steps of local-weather-awareness's generator (``weather.pipeline``, copied with its
library into ``weather/``) are reused for everything they already do: the single-instance lock, the network budget and circuit breaker, the step guards
that turn a failing source into a labelled "unavailable" block instead of a crash, the NWS
alert feed and zone outlines, the MRMS frame, and the per-site NWS products. One run does:

1. lock, budget, log the sites, the alert areas and the warning products of the verdict;
2. the active NWS alerts (once) and the outlines of zone-based ones;
3. the latest MRMS frame (once; skipped when no site's map reaches the MRMS grid) and,
   where a ring shows an echo, the frames before it that a confirmation needs (each
   fetched once, for all sites; repeats of the same radar scan passed over);
4. GLM lightning: the granules of the last ``VLBA_GLM_WINDOW_MIN`` minutes from GOES-East
   and GOES-West (once; both satellites, every site uses the ones that see it);
5. per antenna: point metadata, observation (plus the station's distance and height
   relative to the antenna, and the raw present-weather / precipitation fields), the 7-day,
   hourly and raw gridpoint forecasts, sun altitude and times, the alerts AT / NEAR the
   site, the radar ring check (MRMS, or the NWS composite for a site outside the MRMS
   grid, fetched here; where the ring shows an echo, also on the frames before the newest,
   so a run confirms an echo on consecutive scans by itself), the GLM view, the verdict
   (``vlba.decision``, with the holds and streak of the state file), and the two theme
   maps with the rings and flashes drawn;
6. the state file, ``index.html`` and ``status.json``.

Exit status 0 even when every source is down (cron must keep publishing whatever there
is); 2 for a configuration error, 1 when the output or cache directory cannot be written.

usage: make_vlba_page.py [--out DIR] [--cache DIR] [--verbose] [--skip-radar] [--skip-glm]
                         [--sites SPEC] [--once]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))       # the checkout: weather/ and vlba/
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from weather import pipeline as mwp                     # noqa: E402  the base generator's run steps
from weather import alerts, geo, http, nws, sun, util   # noqa: E402
from weather import radar as wradar                     # noqa: E402
from weather.config import parse_sites                  # noqa: E402
from vlba import decision, glm, gridpoint, maps, mrms, nwsradar, page, radarcheck   # noqa: E402
from vlba.config import VlbaConfig, parse_event_names   # noqa: E402
from vlba.sites import site_elevation_m                 # noqa: E402

log = logging.getLogger("vlba.main")

_step = mwp._step
_source = mwp._source


# ---- fallbacks of the new products ------------------------------------------------------
def _gridpoint_fallback(error: str) -> Dict[str, Any]:
    return mwp._failed(error, url=None, updated=None, update_age_min=None, elevation_m=None,
                       missing=[], hours=[], now=None, next=None)


def _glm_fallback(error: str) -> Dict[str, Any]:
    d = mwp._failed(error, window_start=None, window_end=None, flashes=[], sats={}, deps=glm.deps_available(),
                    elapsed_s=0.0, skipped=None, partial=False, newest=None, retry=None, seen=None)
    d["stale"] = False
    return d


def _island_fallback(error: str) -> Dict[str, Any]:
    d = mwp._failed(error, ts=None, url=None, label="NWS radar composite", source=None, region=None,
                    image=None, index=None)
    return d


def _glm_off(reason: str) -> Dict[str, Any]:
    d = _glm_fallback(reason)
    d["skipped"] = reason
    return d


# ---- the station and the raw observation ---------------------------------------------------
# How a station's recent reports are judged: a report that carries no weather is either a
# station without a present-weather sensor (mesonet, RAWS: none of its recent reports has
# any) or a METAR station's blank record (St. Croix's TISX leaves about a third blank).
WEATHER_HISTORY = 12            # recent reports fetched to tell the two apart
# A report with weather says something about rain now for 2 h (90 min until 2026-11-10: NWS's
# API serves St. Croix's TISX about 40 min late, so at the :30 runs its newest report there
# was 97 min old and the check read "no data"). When NWS has no report with weather of the
# last 90 min from the stations (an hourly station's next one is overdue there, or its records
# came back blank), their METARs are asked for at aviationweather.gov (cfg.metar_url), which
# has them minutes after they are made; a station without METARs (Kitt Peak's QSBA3) costs a
# request that is answered 204, nothing.
WEATHER_REPORT_MAX_AGE_MIN = 120
WEATHER_REPORT_FRESH_MIN = 90
METAR_TIMEOUT_S = 15            # per attempt, one retry: a fallback must not hold up the run


def _station_rows(cfg, site: dict, feats: Any, site_elev: Optional[float]) -> List[Dict[str, Any]]:
    """The stations of NWS's list for the grid cell, nearest first: ``{"id", "lat", "lon",
    "elev_m", "dist_km", "delev_m", "ok"}`` with ``ok`` = within ``cfg.obs_max_km`` and
    ``cfg.obs_max_delev_m`` of the antenna (an unknown height difference passes, an unknown
    position does not). An entry that cannot be read is skipped, never the whole list, and so
    is one whose id is no station id (it goes into request URLs). Sorted by the distance to
    the antenna (NWS orders its list from the grid cell's point), unknown distances last."""
    rows: List[Dict[str, Any]] = []
    for feat in feats if isinstance(feats, list) else []:
        try:
            props = feat.get("properties") if isinstance(feat.get("properties"), dict) else {}
            fid = props.get("stationIdentifier") or nws._last_segment(feat.get("id"))
            if not fid or not isinstance(fid, str) or not nws._STATION_ID.fullmatch(fid):    # "$" passes "\n"
                continue
            geom = feat.get("geometry") if isinstance(feat.get("geometry"), dict) else {}
            try:
                lon, lat = float(geom["coordinates"][0]), float(geom["coordinates"][1])
            except (KeyError, IndexError, TypeError, ValueError):
                lon = lat = None
            if lat is not None and not (math.isfinite(lat) and math.isfinite(lon)):
                lon = lat = None
            elev = util.quantity(props.get("elevation"))
            dist = geo.haversine_km(site["lat"], site["lon"], lat, lon) if lat is not None else None
            delev = float(elev) - site_elev if elev is not None and site_elev is not None else None
            ok = dist is not None and dist <= float(cfg.obs_max_km) \
                and (delev is None or abs(delev) <= float(cfg.obs_max_delev_m))
            rows.append({"id": str(fid), "lat": lat, "lon": lon, "elev_m": elev, "dist_km": dist,
                         "delev_m": delev, "ok": ok})
        except Exception:       # noqa: BLE001 - one odd entry costs that entry only
            continue
    rows.sort(key=lambda r: (r["dist_km"] is None, r["dist_km"] or 0.0))       # stable: NWS's order on a tie
    return rows


def _station_info(cfg, cache, site: dict, obs: dict, grid_elev: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Where the observation's station is relative to the antenna, from the station list
    NWS gave for the grid cell (cached by ``nws.observation`` a moment ago): distance,
    height difference, and whether the report may be used for the verdict
    (``cfg.obs_max_km`` / ``cfg.obs_max_delev_m``); plus ``qualifying``, the stations of
    the list that pass both limits, nearest first ([] = no station suits the antenna at
    all; None = unknown: no list, or no station of it could be located). ``usable`` follows
    the same rule as ``qualifying``. ``id`` is None without an observation. None when there
    is neither an observation nor a station list."""
    sid = str(obs["station_id"]) if obs.get("ok") and obs.get("station_id") else None
    data, _ = cache.get_any("stations/%s" % nws._loc(site))
    feats = data.get("features") if isinstance(data, dict) else None
    if sid is None and not isinstance(feats, list):
        return None
    info: Dict[str, Any] = {"id": sid, "name": obs.get("station_name") if sid else None, "lat": None,
                            "lon": None, "elev_m": None, "dist_km": None, "delev_m": None,
                            "usable": sid is not None, "why": None, "qualifying": None}
    # the antenna's own height; for a site without one (--sites) its NWS grid cell's
    site_elev = site_elevation_m(site)
    if site_elev is None and isinstance(grid_elev, (int, float)) and not isinstance(grid_elev, bool):
        site_elev = float(grid_elev)
    rows = _station_rows(cfg, site, feats, site_elev)
    if any(r["dist_km"] is not None for r in rows):
        info["qualifying"] = [r["id"] for r in rows if r["ok"]]
    if sid is None:
        info["why"] = "no current observation"
        return info
    row = next((r for r in rows if r["id"] == sid), None)
    if row is None or row["dist_km"] is None:
        # an unknown position does not qualify, here as in "qualifying": a valley station
        # without coordinates is no more use than one with them; the next qualifying
        # station's report is then read (_qualifying_observation)
        info["usable"] = False
        reasons = ["position unknown (%s)" % (
            "no station list" if not isinstance(feats, list) else
            "not in NWS's station list" if row is None else "no coordinates in NWS's station list")]
        if row is not None:
            info["elev_m"] = row["elev_m"]
            if row["delev_m"] is not None:
                info["delev_m"] = round(row["delev_m"], 0)
                if abs(row["delev_m"]) > float(cfg.obs_max_delev_m):
                    reasons.append("station %d m %s than the antenna, limit %g m" % (
                        int(round(abs(row["delev_m"]))), "lower" if row["delev_m"] < 0 else "higher",
                        cfg.obs_max_delev_m))
        info["why"] = "; ".join(reasons)
        return info
    info.update(lat=row["lat"], lon=row["lon"], elev_m=row["elev_m"], dist_km=round(row["dist_km"], 1),
                delev_m=None if row["delev_m"] is None else round(row["delev_m"], 0), usable=row["ok"])
    reasons = []
    if row["dist_km"] > float(cfg.obs_max_km):
        reasons.append("station %s km from the antenna, limit %g km" % (info["dist_km"], cfg.obs_max_km))
    if row["delev_m"] is not None and abs(row["delev_m"]) > float(cfg.obs_max_delev_m):
        reasons.append("station %d m %s than the antenna, limit %g m" % (
            int(round(abs(row["delev_m"]))), "lower" if row["delev_m"] < 0 else "higher", cfg.obs_max_delev_m))
    if reasons:
        info["why"] = "; ".join(reasons)
    elif site_elev is None:
        info["why"] = "the antenna's elevation is unknown: the %g m rule is not applied" % cfg.obs_max_delev_m
    return info


def _reports_weather(props: Any) -> bool:
    """Whether a station report says anything about the weather: a description ("Clear"),
    present weather, cloud layers, a precipitation amount, or a raw METAR (whose weather
    groups ``decision.metar_precipitation`` reads when NWS did not decode them). A mesonet /
    RAWS station (Kitt Peak's QSBA3, Brewster's CJDW) sends temperature, humidity and wind
    only, and a METAR station's record is blank now and then: "no precipitation reported"
    from either would be unknown passed off as clear. So would a METAR that cannot show
    precipitation (``decision.metar_blind``: its present-weather sensor out (PWINO), a station
    without a precipitation discriminator (AO1), a NIL report, nothing after the time): only
    the precipitation it still shows counts. A raw METAR alone (no description, present
    weather, clouds or amount) speaks only with a visibility or a sky group in it."""
    if not isinstance(props, dict):
        return False
    plh = props.get("precipitationLastHour")
    raw = props.get("rawMessage")
    if decision.metar_blind(raw):
        # mist, haze or a thunderstorm in the vicinity still come through; rain does not
        return _precipitation_in(props) is not None
    if (str(props.get("textDescription") or "").strip() or props.get("presentWeather") or props.get("cloudLayers")
            or (isinstance(plh, dict) and plh.get("value") is not None)):
        return True
    return bool(str(raw or "").strip()) and (decision.metar_observes(raw) or _precipitation_in(props) is not None)


def _precipitation_in(props: Any) -> Optional[str]:
    """What in a report says precipitation is falling (``decision.observed_precipitation``
    on its present weather, text, raw METAR and hourly amount), None when nothing does."""
    if not isinstance(props, dict):
        return None
    return decision.observed_precipitation(
        {"present_weather": _present_weather_texts(props), "metar": props.get("rawMessage"),
         "precip_last_hour_mm": util.quantity(props.get("precipitationLastHour"))},
        str(props.get("textDescription") or ""))


def _report_kind(props: Any) -> Optional[str]:
    """What a report says about precipitation: "wet" (it shows some), "dry" (it says
    something about the weather and shows none), "blind" (a METAR that cannot show it:
    ``decision.metar_blind``), None (a blank record)."""
    if _precipitation_in(props) is not None:
        return "wet"
    if _reports_weather(props):
        return "dry"
    if isinstance(props, dict) and decision.metar_blind(props.get("rawMessage")):
        return "blind"
    return None


def _present_weather_texts(props: dict) -> List[str]:
    """NWS's decoded present weather of a report in words ("light rain", "thunderstorms in
    the vicinity"), as ``decision.observed_precipitation`` reads them."""
    out = []
    pw = props.get("presentWeather")
    for w in pw if isinstance(pw, list) else []:
        if not isinstance(w, dict):
            continue
        words = [str(w.get(k)) for k in ("intensity", "modifier", "weather") if w.get(k)]
        if not words:
            continue
        if w.get("weather") == "unknown":           # UP: precipitation of an unknown type
            words[-1] = "unknown precipitation"
        text = " ".join(words).replace("_", " ")
        if w.get("inVicinity"):
            text += " in the vicinity"
        out.append(text)
    return out


def _valid_history(d: Any) -> bool:
    return isinstance(d, dict) and isinstance(d.get("features"), list)


def _report_age(props: Any) -> Optional[float]:
    """Minutes since a report's time (negative: ahead of this host's clock); None without."""
    return util.age_minutes(util.parse_iso(props.get("timestamp"))) if isinstance(props, dict) else None


def _report_time(props: Any) -> Optional[float]:
    """A report's time, epoch s (None without): reports compare by it, not by ages taken a
    moment apart."""
    t = util.parse_iso(props.get("timestamp")) if isinstance(props, dict) else None
    return t.timestamp() if t is not None else None


def _recent(age: Optional[float]) -> bool:
    """Whether a report this many minutes old still says something about the weather now
    (a few minutes ahead of this host's clock pass)."""
    return age is not None and -10 <= age <= WEATHER_REPORT_MAX_AGE_MIN


def _fresh(age: Optional[float]) -> bool:
    """Whether a report this many minutes old is as new as NWS should have it
    (``WEATHER_REPORT_FRESH_MIN``); an older one sends the check to aviationweather.gov."""
    return age is not None and -10 <= age <= WEATHER_REPORT_FRESH_MIN


def _metar_props(m: Any) -> Optional[Dict[str, Any]]:
    """An aviationweather.gov METAR (its JSON: ``icaoId``, ``obsTime`` epoch s, ``rawOb``) as
    the properties of an NWS report the precipitation check reads: the raw METAR without its
    METAR / SPECI word (``decision.metar_precipitation`` reads the groups after the station
    and the time), the hourly precipitation of its remarks' P group (inches / 100), no
    decoded fields; None when it lacks the station, the time or the text, when the text is
    no METAR of that station and time (its station is not ``icaoId``, its day-and-time group
    not ``obsTime``'s UTC minute: a record of something else is not read as this one), or
    too long to be one (``decision.METAR_MAX_LEN``)."""
    if not isinstance(m, dict):
        return None
    raw = str(m.get("rawOb") or "").strip()
    if len(raw) > decision.METAR_MAX_LEN:
        return None
    raw = re.sub(r"^(?:METAR|SPECI)\s+", "", raw)
    sid = str(m.get("icaoId") or "").strip().upper()
    t = m.get("obsTime")
    if not raw or not sid or isinstance(t, bool) or not isinstance(t, (int, float)):
        return None
    try:
        when = datetime.fromtimestamp(float(t), timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None                     # out of range (milliseconds, NaN, 400 digits): no report to read
    groups = raw.split()
    if len(groups) < 2 or groups[0] != sid or groups[1] != when.strftime("%d%H%MZ"):
        return None
    remarks = (decision.metar_groups(raw) or ([], []))[1]
    plh = next((g for g in remarks if re.fullmatch(r"P\d{4}", g)), None)
    return {"stationId": sid, "timestamp": when.isoformat(),
            "rawMessage": raw, "textDescription": "", "presentWeather": [], "cloudLayers": [],
            "precipitationLastHour": {"unitCode": "wmoUnit:mm", "value": round(int(plh[1:]) * 0.254, 2)}
            if plh else None, "source": "aviationweather.gov"}


def _metar_host(cfg) -> str:
    """The host of ``cfg.metar_url``, the only part of it that public text names."""
    return http._host(cfg.metar_url) or "the METAR server"


def _metar_reports(cfg, stations: List[str], seen: Optional[set] = None):
    """``({station: [report properties, newest first]}, why they could not be had | None)``:
    the METARs of ``stations`` of the last ``WEATHER_REPORT_MAX_AGE_MIN`` from
    aviationweather.gov's data API (``cfg.metar_url``). A station without METARs (mesonet,
    RAWS) is just absent; the server answers 204 when none has any. An answer with reports
    of which none can be read is a failure too. ``seen`` (a set) gets every station asked
    for that the answer has any entry of, readable or not. The reason names the host only:
    it goes on the page (the log has the full error)."""
    ids = [s for s in stations if isinstance(s, str) and re.fullmatch(r"[A-Z0-9]{3,5}", s)]
    if not cfg.metar_url or not ids:
        return {}, None
    host = _metar_host(cfg)
    url = "%s?ids=%s&format=json&hours=%d" % (cfg.metar_url, ",".join(ids), -(-WEATHER_REPORT_MAX_AGE_MIN // 60))
    try:
        body = http.get_bytes(url, cfg, accept="application/json", timeout=METAR_TIMEOUT_S, retries=1)
    except http.HttpError as e:
        log.warning("METARs from %s: %s", host, e)
        # the error text holds the URL, which may hold a query or credentials: the host stands in
        return {}, _public_error(str(e), host, url, cfg.metar_url)
    if not body.strip():
        return {}, None
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, RecursionError) as e:     # deep nesting raises RecursionError
        return {}, "unreadable answer (%s)" % _public_error(str(e), host)
    if not isinstance(data, list):
        return {}, "unreadable answer (%s instead of a list)" % type(data).__name__
    out: Dict[str, List[Dict[str, Any]]] = {}
    for m in data:
        if seen is not None and isinstance(m, dict) and str(m.get("icaoId") or "").strip().upper() in ids:
            seen.add(str(m.get("icaoId")).strip().upper())
        p = _metar_props(m)
        if p is not None and p["stationId"] in ids:
            out.setdefault(p["stationId"], []).append(p)
    for reports in out.values():
        reports.sort(key=lambda p: p["timestamp"], reverse=True)
    if data and not out:
        return {}, "none of the %d reports in its answer could be read" % len(data)
    return out, None


def _public_error(text: str, host: str, *urls: str) -> str:
    """An error text with ``urls`` named in it, and any other URL-like word, replaced by
    ``host``; at most 200 characters."""
    for u in sorted((u for u in urls if u), key=len, reverse=True):
        text = text.replace(u, host)
    text = re.sub(r"\b[A-Za-z][A-Za-z0-9+.-]{0,15}://\S*", host, text)
    return text if len(text) <= 200 else text[:199] + "…"


def _fetch_latest(cfg, cache, site: dict, sid: str) -> dict:
    """The ``http.cached_json`` result of a station's latest report (the key and the
    lifetime of the parent's observation, so a report is fetched once per run)."""
    url = "%s/stations/%s/observations/latest" % (nws.BASE, sid)
    return http.cached_json(cfg, cache, "obs/%s/%s" % (site["slug"], sid), url, cfg.obs_ttl,
                            cfg.obs_max_stale, validate=nws._valid_observation)


def _latest_report(cfg, cache, site: dict, sid: str):
    """(observation dict of the parent's shape, report properties) of a station's latest
    report when it is recent (``nws._unusable``, but a report without a temperature passes:
    NWS's quality control often drops it, St. Croix's TISX for one, and the verdict never
    reads the temperature), else (None, None). A record that cannot be parsed (a number out
    of range, say) costs that station only: the reason is logged, the next one is tried."""
    ores = _fetch_latest(cfg, cache, site, sid)
    if not ores["ok"]:
        return None, None
    props = ores["data"]["properties"]
    try:
        rep = nws._parse_observation(props)
        unusable = nws._unusable(dict(rep, temp_c=0.0), cfg.obs_max_age_min)
    except Exception as e:      # noqa: BLE001 - one odd record costs that station only
        log.warning("%s: the latest report of %s cannot be read, skipped: %s: %s", site["slug"], sid,
                    type(e).__name__, e)
        return None, None
    if unusable:
        return None, None
    out: Dict[str, Any] = {k: None for k in nws.OBS_FIELDS}
    out["cloud_layers"] = []
    out.update(rep)
    out["station_id"] = rep.get("station_id") or sid
    out["station_name"] = rep.get("station_name") or sid
    out.update(nws._status(ores))
    return out, props


def _weather_history(cfg, cache, site: dict, sid: str) -> Dict[str, Any]:
    """What a station's last ``WEATHER_HISTORY`` reports say: ``{"able": True when one of
    them says anything about the weather, False when none of at least 3 distinct reports
    does (a station without a present-weather sensor), None when that cannot be told (no
    history, too few reports, the sensor out of service: ``note`` says which), "report":
    the newest of them with weather that is still recent (``_recent``) | None, "age": its
    age in minutes | None, "reports": its recent reports with weather or unable to show
    precipitation (``_report_kind``), newest first, "note", "stale": a last good copy of the
    history, "unread": the history could not be had}``."""
    url = "%s/stations/%s/observations?limit=%d" % (nws.BASE, sid, WEATHER_HISTORY)
    res = http.cached_json(cfg, cache, "obshist/%s/%s" % (site["slug"], sid), url, cfg.obs_ttl,
                           cfg.obs_max_stale, validate=_valid_history)
    out: Dict[str, Any] = {"able": None, "report": None, "age": None, "reports": [], "note": None,
                           "stale": bool(res.get("stale")), "unread": not res["ok"]}
    if not res["ok"]:
        out["note"] = "its recent reports could not be read"
        return out
    reports = {}
    for f in res["data"]["features"]:
        p = f.get("properties") if isinstance(f, dict) else None
        if not isinstance(p, dict):
            continue
        t = str(p.get("timestamp"))
        if t not in reports or (_reports_weather(p) and not _reports_weather(reports[t])):
            reports[t] = p          # one per report time: a copy with weather over a blank one
    # its recent reports that say something, with weather or unable to (a METAR that cannot
    # show precipitation), newest first: _precip_report reads the station's newest
    out["reports"] = [p for _, p in sorted(((_report_time(p), p) for p in reports.values()
                                            if _report_kind(p) and _recent(_report_age(p))),
                                           key=lambda tp: -tp[0])]
    weather = [p for p in reports.values() if _reports_weather(p)]
    if not weather:
        blind = [p for p in reports.values() if _report_kind(p) == "blind"]
        if blind:
            # a METAR station whose sensor is out (or that cannot report precipitation): no
            # proof of a station without weather
            newest = max(blind, key=lambda p: _report_time(p) or 0.0)
            out["note"] = "its reports cannot show precipitation: %s" % decision.metar_blind(newest.get("rawMessage"))
        elif len(reports) >= 3:
            out["able"] = False
        else:
            out["note"] = "too few recent reports to tell"
        return out
    out["able"] = True
    timed = [(age, p) for age, p in ((_report_age(p), p) for p in weather) if _recent(age)]
    if timed:
        age, out["report"] = min(timed, key=lambda ap: ap[0])
        out["age"] = max(0.0, age)
    return out


def _qualifying_observation(cfg, cache, site: dict, station: dict):
    """The report of the nearest station that suits the antenna, when the station the parent
    picked (the nearest with a recent, complete report) does not: the first of
    ``station["qualifying"]`` (at most ``nws.MAX_STATIONS`` tried) with a recent report, a
    temperature or not (``_latest_report``), else None. Kitt Peak's case: its nearest
    station lies 1200 m below the antenna, the next one qualifies; St. Croix's: TISX, the
    only qualifying station, often sends no temperature. Whether it reports weather is
    ``_precip_report``'s business."""
    for sid in (station.get("qualifying") or [])[:nws.MAX_STATIONS]:
        out, _ = _latest_report(cfg, cache, site, sid)
        if out is not None:
            return out
    return None


def _passed_over(station: dict, own: Optional[str], props: Any) -> str:
    """Why the precipitation check does not read the station's latest report (for the check
    detail and the log)."""
    if not own:
        return "%s is not used" % station["id"] if station.get("id") else "no usable observation"
    blind = decision.metar_blind(props.get("rawMessage")) if _report_kind(props) == "blind" else None
    if blind:
        return "%s's latest report: %s" % (own, blind)
    age = _report_age(props)
    if _reports_weather(props) and age is not None and age > WEATHER_REPORT_FRESH_MIN:
        return "%s's latest at NWS is %s old" % (own, util.fmt_duration_minutes(age))
    return "%s's latest has no weather" % own


def _answer(reports: List[Dict[str, Any]]):
    """(the report that answers for one station | None, its newest report that cannot show
    precipitation, when newer than that answer | None), from the station's recent reports
    (``_precip_report``'s entries): its newest one that says something, NWS's copy first on
    a tie. Below a newer report that cannot show precipitation (PWINO, ...) only rain
    counts: "no precipitation" from before the sensor went out says nothing about now."""
    blind = None
    for r in sorted(reports, key=lambda r: (-r["t"], r["src"] is not None)):
        if r["kind"] == "blind":
            blind = blind or r
        elif r["kind"] == "wet" or blind is None:
            return r, blind
    return None, blind


def _precip_report(cfg, cache, site: dict, station: dict, obs: dict) -> Dict[str, Any]:
    """Which report the precipitation check reads: ``{"weather": "reported" | "none" |
    "unknown", "station": the id of the station whose report it is, "props": that report's
    properties (None: the station's own latest), "age": its age in minutes | None, "stale":
    it is a last good copy, "source": the host of a METAR read from ``cfg.metar_url`` |
    None (NWS's report), "passed": why the station's own latest is not read | None,
    "doubt": what may hold a report that could not be read | None, "blind": that the chosen
    report's station sent a newer one that cannot show precipitation | None, "why": the
    reason for "none" / "unknown"; "problem": why the METAR fallback, asked, gave nothing
    (a run error) | absent}``.

    The station's latest report when the station is usable, its report says anything about
    the weather and is fresh (``_fresh``, 90 min). Otherwise the reports of the last 2 h
    (``_recent``) of the station and the next qualifying stations (at most
    ``nws.MAX_STATIONS``; all of
    them qualifying ones when the station itself is not usable: a qualifying station whose
    report lacks a temperature, which the observation needs, still says whether it rains),
    each station answering with its newest report with weather (``_answer``). Of these:
    precipitation in any of the last 90 min, the newest such report; else the nearest
    station's when it is of the last 90 min (a slightly newer clear report of a farther
    station does not override it); else the newest. The nearer station on a tie (Brewster:
    CJDW reports no weather, KOMK does; a METAR station's blank record: Los Alamos, St.
    Croix). The latest reports first; a station's recent reports only when its latest is
    blank and could still change the answer (newer than the best report found, or of the
    last 90 min), so a history that could not be read is a ``doubt`` only when it could have.
    When NWS has no fresh one among them, or none of the nearest station (unless it is known
    to have no sensor) and no fresh rain, their METARs from aviationweather.gov count too
    (NWS serves St. Croix's TISX about 40 min late), NWS's copy on a tie (it has the decoded
    words). Only the precipitation check reads the report: the wind and the humidity stay
    the station's. "none" only when the stations checked are all ones without weather (Kitt
    Peak's QSBA3) and the METAR fallback, asked, did not fail or show METARs of them;
    "unknown" otherwise, never "clear"."""
    qual = station.get("qualifying")
    own = station["id"] if station.get("usable") and obs.get("ok") and station.get("id") else None
    data, _ = cache.get_any("obs/%s/%s" % (site["slug"], own)) if own else (None, None)
    own_props = data.get("properties") if isinstance(data, dict) else None
    out: Dict[str, Any] = {"weather": "reported", "station": own, "props": None, "age": None,
                           "stale": bool(obs.get("stale")), "source": None, "passed": None, "doubt": None,
                           "blind": None, "why": None}
    if own and _reports_weather(own_props) and _fresh(_report_age(own_props)):
        return out
    out["passed"] = _passed_over(station, own, own_props)
    checked = ([own] if own else []) + [q for q in (qual or []) if q != own][:nws.MAX_STATIONS]
    order = list(qual or []) + [own]
    rank = {q: order.index(q) for q in checked}           # nearest first
    home = min(checked, key=lambda q: rank[q]) if checked else None
    now_s = util.utcnow().timestamp()
    reports: Dict[str, List[Dict[str, Any]]] = {q: [] for q in checked}

    def add(q: str, p: Any, stale: bool, src: Optional[str] = None) -> None:
        kind, t = _report_kind(p), _report_time(p)
        if kind and t is not None and _recent(_report_age(p)):
            reports[q].append({"t": t, "rank": rank[q], "q": q, "p": p, "stale": stale, "src": src, "kind": kind})

    def answers() -> List[Dict[str, Any]]:
        return [a for a in (_answer(reports[q])[0] for q in checked) if a is not None]

    def fresh(t: Optional[float]) -> bool:
        return t is not None and _fresh((now_s - t) / 60.0)

    latest: Dict[str, Any] = {}
    for q in checked:
        if q == own:
            latest[q] = (own_props, bool(obs.get("stale")))
        else:
            ores = _fetch_latest(cfg, cache, site, q)
            latest[q] = (ores["data"]["properties"], bool(ores.get("stale"))) if ores["ok"] else (None, False)
    able: Dict[str, Optional[bool]] = {}
    for q in checked:
        p, stale = latest[q]
        if _reports_weather(p):
            able[q] = True      # its latest is its newest report: the older ones cannot beat it
        add(q, p, stale)
    notes, unread = [], []
    for q in sorted((q for q in checked if q not in able), key=lambda q: rank[q]):
        q_time = _report_time(latest[q][0])
        got = answers()
        best = max((a["t"] for a in got), default=None)
        wet = max((a["t"] for a in got if a["kind"] == "wet" and fresh(a["t"])), default=None)
        if best is not None and q_time is not None and q_time < best \
                and (not fresh(q_time) or (wet is not None and q_time < wet)):
            continue            # nothing in its history, all older than its latest, could change the answer
        h = _weather_history(cfg, cache, site, q)
        able[q] = h["able"]
        if h["note"]:
            notes.append("%s: %s" % (q, h["note"]))
        for p in h.get("reports") or ([h["report"]] if h.get("report") is not None else []):
            add(q, p, h["stale"])
        if h["unread"]:
            unread.append((q, q_time))
    failed = None
    got = answers()
    newest = max((a["t"] for a in got), default=None)
    # the nearest station (not one known to have no sensor) has nothing of the last 90 min at
    # NWS and no rain is known: its METAR may hold rain, which a farther station's fresh
    # "clear" must not hide (NWS serves St. Croix's TISX late, its records often blank)
    home_open = (home is not None and able.get(home) is not False
                 and not any(fresh(a["t"]) and (a["q"] == home or a["kind"] == "wet") for a in got))
    if newest is None or not fresh(newest) or home_open:
        # NWS has nothing as new as it should be: aviationweather.gov's METARs of the stations
        host, seen = _metar_host(cfg), set()
        try:
            metars, failed = _metar_reports(cfg, checked, seen=seen)
        except Exception as e:      # noqa: BLE001 - an odd answer costs the fallback, never NWS's reports
            log.warning("%s: METARs from %s unreadable: %s: %s", site["slug"], host, type(e).__name__, e)
            metars, failed = {}, "unreadable answer (%s)" % type(e).__name__
        if failed:
            notes.append("%s: %s" % (host, failed))
            out["problem"] = "METARs from %s unavailable: %s" % (host, failed)
            log.warning("%s: METARs of %s from %s unavailable: %s", site["slug"], ", ".join(checked), host, failed)
        for q in checked:
            if q in seen and able.get(q) is False:
                able[q] = None  # blank records at NWS, METARs at aviationweather.gov: no station without weather
            for p in metars.get(q) or []:
                add(q, p, False, host)
                if _reports_weather(p) and _recent(_report_age(p)):
                    able[q] = True
                    t_q = _report_time(p)
                    # NWS's history of q cannot hold a report newer than q's latest there: a METAR at
                    # least as new answers for the history that could not be read
                    unread = [(u, ut) for u, ut in unread if not (u == q and ut is not None and t_q >= ut)]
    blinds: Dict[str, Dict[str, Any]] = {}
    for q in checked:
        _, b = _answer(reports[q])
        if b is not None:
            blinds[q] = b
            why = decision.metar_blind(b["p"].get("rawMessage")) or "it cannot show precipitation"
            if q == own and b["p"] is not own_props:
                out["passed"] = "%s's newest report: %s" % (q, why)
            if not any(n.startswith(q + ":") for n in notes):
                notes.append("%s: newest report: %s" % (q, why))
    got = answers()
    pool = ([a for a in got if a["kind"] == "wet" and fresh(a["t"])]       # rain anywhere, of the last 90 min
            or [a for a in got if a["q"] == home and fresh(a["t"])]          # else the nearest station, when fresh
            or got)
    if pool:
        a = min(pool, key=lambda a: (-a["t"], a["rank"]))
        hidden = [u for u, u_time in unread if u_time is None or u_time >= a["t"] or fresh(u_time)]
        b = blinds.get(a["q"])
        out.update(station=a["q"], props=a["p"], age=max(0.0, _report_age(a["p"])), stale=a["stale"], source=a["src"],
                   doubt="the recent reports of %s, maybe newer, could not be read" % ", ".join(hidden)
                   if hidden else None,
                   blind="%s's newer report: %s" % (a["q"], decision.metar_blind(b["p"].get("rawMessage")))
                   if b is not None else None)
        return out
    out.update(station=None, stale=False)
    if qual is not None and checked and not failed and all(able.get(q) is False for q in checked):
        out.update(weather="none", why="%s report%s no weather (no present-weather sensor)" % (
            ", ".join(checked), "s" if len(checked) == 1 else ""))
        return out
    out.update(weather="unknown", why="no report with weather of the last %s from %s%s" % (
        util.fmt_duration_minutes(WEATHER_REPORT_MAX_AGE_MIN), ", ".join(checked) or "a qualifying station",
        "; " + "; ".join(notes) if notes else ""))
    return out


def _raw_observation(cache, site: dict, obs: dict, precip: Optional[dict] = None) -> Dict[str, Any]:
    """The fields of the station report that the parent's observation drops and the
    precipitation check needs: present weather, precipitation of the last hour, text, the
    raw METAR; from the report ``_precip_report`` chose when that is not the station's
    latest (``precip["props"]``: the station's older one or another station's, with its
    station, age, staleness, why the latest was passed over and any doubt as
    ``report_*``), else from the cached latest report."""
    out: Dict[str, Any] = {"present_weather": [], "precip_last_hour_mm": None,
                           "text": str(obs.get("text") or ""), "reports_weather": None}
    props = (precip or {}).get("props")
    if props is not None:
        age = precip.get("age")
        out.update(report_station=str(precip.get("station") or obs.get("station_id") or "station"),
                   report_age_min=None if age is None else round(float(age), 1),
                   report_stale=bool(precip.get("stale")), report_passed=precip.get("passed"),
                   report_doubt=precip.get("doubt"), report_blind=precip.get("blind"),
                   report_source=precip.get("source"),
                   text=str(props.get("textDescription") or "") if isinstance(props, dict) else "")
    elif not obs.get("ok") or not obs.get("station_id"):
        return out
    else:
        data, _ = cache.get_any("obs/%s/%s" % (site["slug"], obs["station_id"]))
        props = data.get("properties") if isinstance(data, dict) else None
    if not isinstance(props, dict):
        return out
    out["reports_weather"] = _reports_weather(props)
    raw = str(props.get("rawMessage") or "")
    out["metar"] = raw if len(raw) <= decision.METAR_MAX_LEN else ""       # longer is no METAR: not carried
    out["present_weather"] = _present_weather_texts(props)
    out["precip_last_hour_mm"] = util.quantity(props.get("precipitationLastHour"))
    return out


# ---- radar per site --------------------------------------------------------------------------
def _in_mrms_grid(cfg, site: dict, frame=None) -> bool:
    """Whether the site's radar is the MRMS frame: its map reaches into the grid. Without a
    map frame (none could be worked out) the site's own position decides: the MRMS scan of a
    point outside the grid finds no cell and would read "no echo" (Mauna Kea, St. Croix)."""
    try:
        if frame is None:
            frame = geo.MapFrame(site["lat"], site["lon"], cfg.map_km, cfg.map_px, cfg.tile_zoom)
        return geo.bbox_intersects(tuple(frame.bbox), geo.MRMS_BBOX)
    except Exception:           # noqa: BLE001
        left, bottom, right, top = geo.MRMS_BBOX
        try:
            return left <= float(site["lon"]) <= right and bottom <= float(site["lat"]) <= top
        except (KeyError, TypeError, ValueError):
            return False        # the island branch then has no frame either: its check reads no data


def _radar_coverage(cfg, run: dict, domain: str, frame_ts, sites: List[dict], what: str,
                    reused: bool = False) -> Dict[str, Any]:
    """The radar coverage of the sites' rings in MRMS's own file of the frame's time
    (``vlba.mrms.coverage``; ``reused``: the frame is a copy reused after a failed fetch, for
    which no older file may stand in), kept in ``run["radar_coverage"][domain]``; failing is a
    problem of the run."""
    cov = _step(run, what, mrms.coverage, cfg, domain, frame_ts, sites, float(cfg.radar_km),
                allow_older=not reused)
    if not isinstance(cov, dict):
        err = run["errors"][-1] if cov is None and run["errors"] else "%s returned no result" % what
        cov = {"ok": False, "error": err, "domain": domain, "ts": None, "url": None, "sites": {}}
    elif not cov.get("ok"):
        run["errors"].append("%s unavailable: %s" % (what, cov.get("error") or "no data"))
    have = run.setdefault("radar_coverage", {})
    prev = have.get(domain)
    if isinstance(prev, dict):
        # another site of the domain (an island read is per site): every ring kept, a failure shown
        merged = dict(prev, sites=dict(prev.get("sites") or {}, **(cov.get("sites") or {})))
        if not cov.get("ok"):
            merged.update(ok=False, error=cov.get("error"))
        elif cov.get("older") and (not prev.get("older") or (cov.get("behind_s") or 0) >= (prev.get("behind_s") or 0)):
            # one site's older file marks the domain: the farthest behind is shown, its own time with it
            merged.update({k: cov.get(k) for k in ("ts", "url", "source", "older", "behind_s", "note")})
        have[domain] = merged
    else:
        have[domain] = cov
    return cov


def _site_coverage(cfg, run: dict, domain: str, slug: str) -> Dict[str, Any]:
    """The site's part of the coverage for its radar check; ``{"off": True}`` when
    VLBA_RADAR_COVERAGE=0."""
    if not cfg.radar_coverage:
        return {"off": True}
    return mrms.site_coverage((run.get("radar_coverage") or {}).get(domain), slug)


def _radar_word(verdict: Optional[dict], check: dict) -> str:
    """The radar part of a site's log line: the verdict's own word for its radar check."""
    for c in (verdict or {}).get("checks") or []:
        if isinstance(c, dict) and c.get("key") == "radar":
            state = str(c.get("state"))
            return {"ok": "clear", "bad": "BAD", "watch": "echo (unconfirmed)", "unknown": "no data",
                    "not provided": "n/a"}.get(state, state)
    return "ring" if check.get("in_ring") else ("clear" if check.get("available") else "n/a")


def _stale_limit(cfg, newest: Optional[datetime] = None) -> datetime:
    """The oldest frame time that can still confirm the frame of ``newest``:
    ``cfg.radar_stale_s`` before it, the widest gap the verdict lets two consecutive frames
    have (``decision.check_radar``), so no earlier frame is asked for. Measured from the
    newest frame and not from the clock: an archive that is late serves a newest frame of
    10-15 min ago, and the scan before it (up to 10 min older) must still be found. The
    newest frame itself is usable only while younger than ``cfg.radar_stale_s``. Without
    ``newest``: that long before now."""
    anchor = newest if isinstance(newest, datetime) else util.utcnow()
    return anchor - timedelta(seconds=float(cfg.radar_stale_s))


# What a radar check says of an echo no earlier frame could confirm (``check["confirm_reason"]``,
# shown in the check's detail: fixed texts, the exception's own text goes to the run's problems).
_NO_EARLIER = "no other radar scan in the %d min before it"
_REUSED = "it is a copy reused after a failed fetch"


def _confirmation(cfg, earlier: List[Dict[str, Any]], why: Optional[str]) -> Dict[str, Any]:
    """The ``confirm`` keys of a radar check whose newest frame shows an echo in the ring,
    from the ring checks of the frames before it (``earlier``, oldest first). "examined": the
    frames before could say whether this echo is one in a row: all
    ``cfg.radar_trigger_after - 1`` of them were read, or those read end, going back from the
    newest, at a frame without an echo (the echo is then rightly "unconfirmed"). "unavailable":
    they could not (none was read, or fewer than needed and each with an echo), with ``why``
    as ``confirm_reason``: ``decision.check_radar`` then answers "no data" instead of
    "unconfirmed", because runs 30 min apart cannot confirm an echo between themselves and
    rain would read GOOD for as long as the frames before cannot be had. Nothing with a
    one-frame trigger, which needs no frame before."""
    want = max(0, int(cfg.radar_trigger_after) - 1)
    if not want:
        return {}
    echoes = 0
    for c in reversed(earlier):
        if not (isinstance(c, dict) and c.get("in_ring")):
            return {"confirm": "examined", "confirm_reason": None}
        echoes += 1
    if echoes >= want:
        return {"confirm": "examined", "confirm_reason": None}
    return {"confirm": "unavailable", "confirm_reason": why or "the frames before it were not read"}


def _earlier_mrms(cfg, run: dict, frame_res: dict, sites: List[dict]) -> Dict[str, List[Dict[str, Any]]]:
    """For every site whose ring shows an echo in the newest MRMS frame, the ring checks of
    the frames before it, ``cfg.radar_trigger_after - 1`` of them, oldest first: with them a
    run confirms an echo by itself, which runs 30 min apart could not (frames that far apart
    are not consecutive). IEM writes a frame every 2 min, but a radar completes a volume scan
    only every 4.5-10 min, so a frame whose ring holds the same data as the next newer one
    counted (``radarcheck.ring_digest``) is no second observation: it is passed over and the
    search goes further back, never further than ``cfg.radar_stale_s`` before the newest frame
    (``_stale_limit``; each check carries ``confirms_ts``, the newest frame's time, from which
    the verdict judges its age). Each frame is
    fetched once for all sites and dropped after their ring scans (an MRMS frame takes ~25 MB
    of memory). Only after a fresh newest frame, not a copy reused after a failed fetch. A
    frame missing from the archive is passed over (twice at most); a fetch that fails ends
    the search and is a problem of the run. Why a site's frames are wanting (a failure, or
    no other scan within the span) is kept in ``run["radar_confirm"][slug]`` for its check
    (``_confirmation``). Returns ``{slug: [check, ...]}``."""
    want = max(0, int(cfg.radar_trigger_after) - 1)
    img, newest = frame_res.get("image"), frame_res.get("ts")
    if not want or not cfg.radar_enabled or not frame_res.get("ok") or frame_res.get("stale") \
            or img is None or not isinstance(newest, datetime):
        return {}
    km, dbz = float(cfg.radar_km), float(cfg.radar_dbz)
    need: Dict[str, Dict[str, Any]] = {}
    for s in sites:
        ring = _step(run, "%s radar ring" % s["slug"], radarcheck.mrms_ring, img, s["lat"], s["lon"], km, dbz)
        if isinstance(ring, dict) and ring.get("in_ring"):
            need[s["slug"]] = {"site": s, "digest": radarcheck.ring_digest(img, s["lat"], s["lon"], km),
                               "checks": []}
    if not need:
        return {}
    oldest = _stale_limit(cfg, newest)
    ts, misses, err, why, fetched, same = newest, 0, None, None, 0, 0
    while any(len(n["checks"]) < want for n in need.values()):
        ts = ts - timedelta(minutes=2)
        if ts < oldest:
            why = _NO_EARLIER % (int(cfg.radar_stale_s) // 60)
            break
        try:
            url = ts.strftime(cfg.mrms_archive)
            there = http.head_ok(url, cfg, timeout=wradar.HEAD_TIMEOUT)
        except Exception as e:  # noqa: BLE001 - head_ok should not raise; if it does, no answer
            err, why = "%s: %s" % (type(e).__name__, e), "the MRMS archive could not be asked"
            break
        if there is None:
            err = why = "the MRMS archive did not answer"
            break
        if not there:
            misses += 1
            if misses > 2:
                err = why = "frames missing from the archive"
                break
            continue
        try:
            older = wradar._open_frame(http.get_bytes(url, cfg, accept="image/png"))
        except (http.HttpError, wradar.RadarError) as e:
            err, why = str(e).replace(url, "the archive"), "the frame before it could not be fetched"
            break
        except Exception as e:  # noqa: BLE001 - any surprise = no earlier frame, never a crash
            err, why = "%s: %s" % (type(e).__name__, e), "the frame before it could not be read"
            break
        fetched += 1
        for slug, n in need.items():
            if len(n["checks"]) >= want:
                continue
            s = n["site"]
            digest = radarcheck.ring_digest(older, s["lat"], s["lon"], km)
            if digest is not None and digest == n["digest"]:
                same += 1               # the same radar data in the ring: no second observation
                continue
            ring = _step(run, "%s radar ring (%s)" % (slug, ts.strftime("%H:%MZ")), radarcheck.mrms_ring,
                         older, s["lat"], s["lon"], km, dbz)
            if isinstance(ring, dict):
                n["checks"].append(dict(ring, available=True, stale=False, error=None, source="MRMS",
                                        frame_ts=ts, digest=digest, confirms_ts=newest))
                n["digest"] = digest
        older = None
    log.info("MRMS: %d earlier frame(s) read for %s (%d ring(s) with the same data passed over)", fetched,
             ", ".join("%s %d" % (slug, len(n["checks"])) for slug, n in need.items()), same)
    if err:
        run["errors"].append("MRMS frame before %s unavailable (%s): an echo in a ring is not confirmed by "
                             "this run alone" % (newest.strftime("%H:%MZ"), err))
    for slug, n in need.items():
        if len(n["checks"]) < want:
            # every frame of the span held the newest frame's data in the ring (the check's
            # detail says so), or the search failed (a problem of the run, above)
            run.setdefault("radar_confirm", {})[slug] = why or _NO_EARLIER % (int(cfg.radar_stale_s) // 60)
    return {slug: list(reversed(n["checks"])) for slug, n in need.items()}


def _earlier_island_checks(cfg, run: dict, site: dict, frame, island: dict, label: str,
                           digest: Optional[str]) -> List[Dict[str, Any]]:
    """As ``_earlier_mrms``, for an island site whose ring shows an echo in the newest
    composite: the frames the capabilities list before it, newest first, fetched with
    ``TIME`` one at a time; a frame with the same data in the ring as the next newer one
    counted (``digest``: the newest frame's) is passed over, none more than
    ``cfg.radar_stale_s`` older than the newest is asked for. A frame that fails ends the
    search and is a problem of the run; one with a pixel of unknown colour in the ring and no
    decoded echo there is unusable and passed over, like the newest frame would be (with an
    echo decoded in the ring it counts as one). Why the frames are wanting is kept in
    ``run["radar_confirm"][slug]``, like ``_earlier_mrms`` does. Returns the checks oldest
    first."""
    slug, lat, lon = site["slug"], site["lat"], site["lon"]
    want = max(0, int(cfg.radar_trigger_after) - 1)
    km = float(cfg.radar_km)
    newest = island.get("ts") if isinstance(island.get("ts"), datetime) else None
    times = nwsradar.earlier_times(island, _stale_limit(cfg, newest)) if want and newest is not None else []
    out: List[Dict[str, Any]] = []
    why = _NO_EARLIER % (int(cfg.radar_stale_s) // 60)
    for t in times:
        if len(out) >= want:
            break
        when = t.strftime("%H:%MZ")
        f = _step(run, "%s radar composite of %s" % (slug, when), nwsradar.fetch_at, cfg, frame, island, t)
        if not isinstance(f, dict) or not f.get("ok") or f.get("index") is None:
            run["errors"].append("%s radar composite of %s unavailable (%s): an echo in the ring is not "
                                 "confirmed by this run alone" % (slug, when, (f or {}).get("error") or "no data"))
            why = "the frame before it could not be fetched"
            break
        d = radarcheck.ring_digest(f["index"], lat, lon, km, frame=frame)
        if d is not None and d == digest:
            continue                    # the same radar data in the ring: no second observation
        ring = _step(run, "%s radar ring (%s)" % (slug, when), radarcheck.index_ring, f["index"], frame, lat, lon,
                     km, float(cfg.radar_dbz))
        if not isinstance(ring, dict):
            continue
        colors = f.get("colors") if isinstance(f.get("colors"), dict) else {}
        if not ring.get("in_ring") and colors.get("unknown_px"):
            unknown = _step(run, "%s radar colours (%s)" % (slug, when), radarcheck.mask_ring, f.get("unknown_mask"),
                            frame, lat, lon, km)
            if unknown is None or unknown:
                continue                # unknown colours in the ring (or not placed) and no echo decoded: no usable data
        out.append(dict(ring, available=True, stale=False, error=None, source=label, frame_ts=t, digest=d,
                        confirms_ts=newest))
        digest = d
    if want and newest is None:
        why = "frame time unknown"
        run["errors"].append("%s radar composite: frame time unknown, so no earlier frame can be asked for: an "
                             "echo in the ring is not confirmed by this run alone" % slug)
    elif want and not times:
        run["errors"].append("%s radar composite: no earlier frame listed within %d min: an echo in the ring is "
                             "not confirmed by this run alone" % (slug, int(cfg.radar_stale_s) // 60))
    if len(out) < want:
        run.setdefault("radar_confirm", {})[slug] = why
    return list(reversed(out))


def _radar_for_site(cfg, cache, run: dict, site: dict, frame, frame_res: dict,
                    earlier_mrms: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    """``{"check": radar_check dict, "render": the radar dict for the renderer, "island":
    the island result without images | None}`` for one site: MRMS inside the grid, the NWS
    composite outside it. A check with a ring scan carries the ring's radar ``coverage``; one
    whose newest frame shows an echo in the ring carries ``confirm`` (``_confirmation``:
    whether the frames before it could be examined)."""
    slug = site["slug"]
    lat, lon = site["lat"], site["lon"]
    in_grid = _in_mrms_grid(cfg, site, frame)
    if in_grid:
        # age_s: the age of the copy, which a reused frame is at least as old as
        check: Dict[str, Any] = {"available": False, "stale": bool(frame_res.get("stale")), "source": "MRMS",
                                 "frame_ts": frame_res.get("ts"), "age_s": frame_res.get("age_s"),
                                 "in_ring": False, "nearest_km": None,
                                 "count": 0, "max_dbz": None, "error": frame_res.get("error"),
                                 "off": not cfg.radar_enabled}      # WEATHER_RADAR=0: off, not missing
        img = frame_res.get("image")
        if frame_res.get("ok") and img is not None:
            ring = _step(run, "%s radar ring" % slug, radarcheck.mrms_ring, img, lat, lon,
                         float(cfg.radar_km), float(cfg.radar_dbz))
            if isinstance(ring, dict):
                check.update(ring)
                check["available"] = True
                check["error"] = None
                check["coverage"] = _site_coverage(cfg, run, "CONUS", slug)
                check["digest"] = radarcheck.ring_digest(img, lat, lon, float(cfg.radar_km))
                if check.get("in_ring") and not check.get("stale"):
                    check["earlier"] = list((earlier_mrms or {}).get(slug) or [])
                    why = (run.get("radar_confirm") or {}).get(slug)
                    if not isinstance(frame_res.get("ts"), datetime):
                        why = "frame time unknown"
                    check.update(_confirmation(cfg, check["earlier"], why))
                elif check.get("in_ring"):
                    check.update(_confirmation(cfg, [], _REUSED))
            else:
                check["error"] = "ring scan failed"
        return {"check": check, "render": frame_res, "island": None}
    # outside the MRMS grid: the NWS composite of the region
    label = "NWS radar composite"
    if not cfg.island_radar:
        why = "no radar for this site (VLBA_ISLAND_RADAR=0)"
        check = {"available": False, "stale": False, "source": label, "frame_ts": None, "in_ring": False,
                 "nearest_km": None, "count": 0, "max_dbz": None, "error": why, "off": True}
        render = mwp._radar_fallback(why, stale=False)
        render["not_applicable"] = True
        # the site still gets an island entry: its caption and footer pill must not borrow
        # the MRMS frame's, and a deliberate "off" is no problem in status.json
        island = {"ok": False, "stale": False, "error": why, "age_s": None, "ts": None, "label": label,
                  "not_applicable": True}
        return {"check": check, "render": render, "island": island}
    island = _source(run, "%s radar composite" % slug, _island_fallback, nwsradar.fetch, cfg, cache, site, frame)
    label = str(island.get("label") or label)
    # age_s: a frame of unknown time is at least as old as the copy it came from
    check = {"available": False, "stale": bool(island.get("stale")), "source": label,
             "frame_ts": island.get("ts"), "age_s": island.get("age_s"), "in_ring": False, "nearest_km": None,
             "count": 0, "max_dbz": None, "error": island.get("error")}
    if island.get("ok") and island.get("index") is not None:
        ring = _step(run, "%s radar ring" % slug, radarcheck.index_ring, island["index"], frame, lat, lon,
                     float(cfg.radar_km), float(cfg.radar_dbz))
        if isinstance(ring, dict):
            check.update(ring)
            check["available"] = True
            check["error"] = None
        else:
            check["error"] = "ring scan failed"
        colors = island.get("colors") if isinstance(island.get("colors"), dict) else {}
        unknown = _step(run, "%s radar colours" % slug, radarcheck.mask_ring, island.get("unknown_mask"), frame,
                        lat, lon, float(cfg.radar_km)) if colors.get("unknown_px") else 0
        if unknown is None:
            # the composite holds pixels of unknown colour and where they lie could not be
            # worked out: any of them may be in the ring, which is then not known to be clear
            check["available"] = False
            check["error"] = "%d px of unrecognised colour that could not be placed" % colors["unknown_px"]
        elif unknown and check["available"] and check.get("in_ring"):
            # ... but it takes nothing from the echo that was decoded in the ring: that one counts
            run["errors"].append("%s radar check: %d px of unrecognised colour within %g km (the echo decoded "
                                 "in the ring is counted): the composite's colour scale may have changed"
                                 % (slug, unknown, cfg.radar_km))
        elif unknown:
            # a pixel of a colour the decoder does not know may be an echo of any strength
            check["available"] = False
            check["error"] = ("%d px of unrecognised colour within %g km: the composite's colour scale may "
                              "have changed" % (unknown, cfg.radar_km))
        elif (colors.get("unknown_px") or 0) > 100:
            run["errors"].append("%s radar composite: %d px of unrecognised colour (outside the ring); the "
                                 "colour scale may have changed" % (slug, colors["unknown_px"]))
        if not radarcheck.ring_inside(frame, lat, lon, float(cfg.radar_km)):
            check["available"] = False
            check["error"] = "the %g km ring reaches past the map, all the composite covers" % cfg.radar_km
        if check["available"] and cfg.radar_coverage:
            domain = mrms.REGION_DOMAINS.get(str(island.get("region") or ""))
            if domain is None:
                run["errors"].append("%s radar coverage: no MRMS domain for the NWS composite %r" % (
                    slug, island.get("region")))
                check["coverage"] = {"ok": False, "ts": None,
                                     "error": "no MRMS domain for the composite %r" % island.get("region")}
            else:
                cov = _radar_coverage(cfg, run, domain, island.get("ts"), [site],
                                      "%s radar coverage (MRMS %s)" % (slug, domain), reused=bool(island.get("stale")))
                check["coverage"] = mrms.site_coverage(cov, slug)
        elif check["available"]:
            check["coverage"] = {"off": True}
        if check["available"]:
            check["digest"] = radarcheck.ring_digest(island["index"], lat, lon, float(cfg.radar_km), frame=frame)
        if check["available"] and check.get("in_ring") and not island.get("stale"):
            check["earlier"] = _earlier_island_checks(cfg, run, site, frame, island, label, check["digest"])
            check.update(_confirmation(cfg, check["earlier"], (run.get("radar_confirm") or {}).get(slug)))
        elif check["available"] and check.get("in_ring"):
            check.update(_confirmation(cfg, [], _REUSED))
        if check["error"]:
            run["errors"].append("%s radar check: %s" % (slug, check["error"]))
        elif not island.get("stale") and island.get("ts") is None and island.get("error"):
            run["errors"].append("%s radar composite: %s" % (slug, island["error"]))     # frame time unknown
    render = {k: v for k, v in island.items() if k not in ("index", "unknown_mask", "times")}
    render["label"] = label
    return {"check": check, "render": render,
            "island": {k: v for k, v in island.items() if k not in ("image", "index", "unknown_mask", "times")}}


# ---- one antenna ---------------------------------------------------------------------------------
def _hashable(v: Any) -> bool:
    """Whether ``v`` can be a set member: an alert id comes from the feed, and a list or an
    object there must cost the site nothing but that alert's "near" listing."""
    try:
        hash(v)
    except TypeError:
        return False
    return True


def _pending_path(path: str) -> str:
    """Where a map of this run waits until the page that shows it is written: a fixed name
    next to the published file (same file system, so the rename is atomic; a run that dies
    leaves one such file per map, replaced by the next run, never a growing heap)."""
    head, tail = os.path.split(path)
    return os.path.join(head, ".%s.next" % tail)


def _publish_maps(run: dict, pending: List[Any]) -> None:
    """Rename the maps of this run (``_render_maps`` with ``pending``) to their published
    names, all at once just before index.html is written: an old page must not show new
    radar pictures under its old verdicts and captions while a run is going, nor after a
    run that died before its page. A map that cannot be renamed is reported not written,
    so the new page shows no picture for it rather than the previous run's."""
    for m, tmp, final in pending:
        try:
            os.replace(tmp, final)
        except OSError as e:
            log.error("could not publish %s as %s: %s", tmp, final, e)
            m["ok"] = False
            m["error"] = "not published: %s" % mwp._exc_text(e)
            run["errors"].append("%s not published: %s" % (os.path.basename(final), mwp._exc_text(e)))


def _render_maps(cfg, cache, run: dict, site: dict, tz: str, render_radar: dict, drawable: List[dict],
                 notes: List[str], overlay: Dict[str, Any], pending: Optional[List[Any]] = None) -> Dict[str, Any]:
    """Both theme maps through ``maps.VlbaSiteMap``; returns ``{"maps", "drawn", "notes",
    "flashes_drawn"}`` the way the parent's per-site step collects them. With ``pending`` (a
    list), each map is written under ``_pending_path`` and ``(maps entry, that path, the
    published path)`` is appended, for ``_publish_maps``."""
    slug = site["slug"]
    map_site = dict(site, tz=tz)
    out: Dict[str, Dict[str, Any]] = {}
    drawn: set = set()
    map_notes = list(notes)
    flashes_drawn = 0
    # the map is published too: its caption must not name a local path (an unwritable cache
    # puts the MRMS frame's temp file into the radar error)
    subs = mwp._public_paths(cfg)
    if isinstance(render_radar, dict) and isinstance(render_radar.get("error"), str):
        render_radar = dict(render_radar, error=mwp._public_text(render_radar["error"], subs))
    notes = [mwp._public_text(n, subs) if isinstance(n, str) else n for n in notes]
    for theme, tile_url in cfg.themes:
        basename = cfg.map_basename(slug, theme)
        final = os.path.join(cfg.out_dir, basename)
        path = _pending_path(final) if pending is not None else final
        what = "%s %s map" % (slug, theme)
        try:
            sm = maps.VlbaSiteMap(cfg, cache, map_site, theme, tile_url, overlay=overlay)
            res = sm.render(render_radar, drawable, path, notes=notes)
            note = sm.basemap_note
            flashes_drawn = max(flashes_drawn, sm.flashes_drawn)
            if sm.overlay_note:
                run["errors"].append("%s: %s" % (what, sm.overlay_note))
        except Exception as e:      # noqa: BLE001 - a renderer bug must not take the page down
            log.exception("%s failed: %s", what, e)
            run["errors"].append("%s failed: %s" % (what, mwp._exc_text(e)))
            res, note = {"ok": False, "error": mwp._exc_text(e), "drawn_ids": []}, None
        if not isinstance(res, dict):
            res = {"ok": False, "error": "renderer returned no result", "drawn_ids": []}
        out[theme] = {"basename": basename, "ok": bool(res.get("ok")), "error": res.get("error")}
        if res.get("ok"):
            drawn.update(i for i in (res.get("drawn_ids") or []) if i is not None and _hashable(i))
            if pending is not None:
                pending.append((out[theme], path, final))
            # the renderer still writes a map whose caption, frame time, STALE line, marker
            # and key failed (ok, with "decorations failed" in its error): the page must say so
            for part in str(res.get("error") or "").split("; "):
                if part.startswith("decorations failed"):
                    run["errors"].append("%s: %s (no caption, frame time or site marker)" % (what, part))
                    map_notes.append("%s theme: %s; the map has no caption, frame time or site marker"
                                     % (theme, part))
        else:
            run["errors"].append("%s not written: %s" % (what, res.get("error") or "unknown error"))
        if note:
            run["errors"].append("%s: %s" % (what, note))
            map_notes.append("%s theme: %s" % (theme, note))
    return {"maps": out, "drawn": drawn, "notes": map_notes, "flashes_drawn": flashes_drawn}


def _run_site(cfg, cache, run: dict, site: dict, feed: dict, frame_res: dict, glm_res: dict,
              state: dict, earlier_mrms: Optional[Dict[str, List[Dict[str, Any]]]] = None,
              pending_maps: Optional[List[Any]] = None) -> Dict[str, Any]:
    """Everything for one antenna. A step that fails never reads as an answer: its check
    gets "no data" (the station step: the station counts as not usable; the raw report:
    its weather is unknown; the alert classification or an alert query that leaves out the
    site's state: the site's alert feed is not ok)."""
    slug = site["slug"]
    label = "%s " % slug
    now = util.utcnow()
    meta = _source(run, label + "metadata", mwp._meta_fallback, nws.site_metadata, cfg, cache, site)
    tz = mwp._site_tz(site, meta)
    if not meta.get("ok"):
        meta["tz"] = tz
    gap = mwp._alert_area_gap(cfg, site, meta)
    if gap:
        log.warning("%s", gap)
        run["errors"].append(gap)
    # the observation's problems are noted below, once it is known whether a check reads it
    obs = _step(run, label + "observation", nws.observation, cfg, cache, site, meta)
    if not isinstance(obs, dict):
        obs = mwp._obs_fallback(run["errors"][-1] if obs is None and run["errors"] else
                                "%sobservation returned %s instead of a dict" % (label, type(obs).__name__))
    forecast = _source(run, label + "forecast", mwp._forecast_fallback, nws.forecast, cfg, cache, site, meta)
    hourly = _source(run, label + "hourly forecast", mwp._hourly_fallback, nws.hourly, cfg, cache, site, meta)
    grid = _source(run, label + "gridpoint forecast", _gridpoint_fallback, gridpoint.fetch, cfg, cache, site, meta)
    n_errors = len(run["errors"])
    station = _step(run, label + "station", _station_info, cfg, cache, site, obs, grid.get("elevation_m"))
    if not isinstance(station, dict) and (station is not None or len(run["errors"]) > n_errors):
        # the step failed (None is also its answer for "no observation, no station list"):
        # nothing is known about the station, so its report must not count, and no
        # qualifying station is known either: the checks that read it say "no data"
        station = {"id": str(obs["station_id"]) if obs.get("ok") and obs.get("station_id") else None,
                   "usable": False, "why": "station check failed", "qualifying": None}
    station = station if isinstance(station, dict) else None
    if station is not None and not station.get("usable") and station.get("qualifying"):
        # the nearest station with a report does not suit the antenna, a farther one does
        alt = _step(run, label + "qualifying station", _qualifying_observation, cfg, cache, site, station)
        info = _step(run, label + "station", _station_info, cfg, cache, site, alt,
                     grid.get("elevation_m")) if isinstance(alt, dict) else None
        if isinstance(alt, dict) and isinstance(info, dict):
            log.info("%s: %s not used (%s); observation from %s instead", slug, station.get("id") or "no station",
                     station.get("why"), alt.get("station_id"))
            obs, station = alt, info
    if station is not None and station.get("qualifying") == [] and (not obs.get("ok") or obs.get("stale")):
        # no station qualifies (Mauna Kea, Pie Town): the report is only shown, so NWS
        # sending records without values there is no problem of the run (asked 2026-10-12)
        obs["display_only"] = True
        log.info("%s: current observation %s (%s); shown only: no station here counts for the verdict", slug,
                 "stale" if obs.get("ok") else "unavailable", obs.get("error") or "no data")
    else:
        mwp._note_status(run, label + "observation", obs)
    precip = None
    usable = station is not None and station.get("usable") and obs.get("ok") and station.get("id")
    if cfg.check_enabled("obs_precip") and station is not None and (usable or station.get("qualifying")):
        # a qualifying station whose report the observation cannot use (no temperature)
        # still says whether it rains
        precip = _step(run, label + "precipitation report", _precip_report, cfg, cache, site, station, obs)
        if not isinstance(precip, dict):
            precip = {"weather": "unknown", "station": None, "props": None, "age": None, "stale": False,
                      "passed": None, "doubt": None, "why": "precipitation report step failed"}
        station["weather"] = precip["weather"]
        if precip.get("why"):
            station["weather_why"] = precip["why"]
        if precip.get("problem"):
            run["errors"].append("%sprecipitation report: %s" % (label, precip["problem"]))
        if precip.get("props") is not None:
            log.info("%s: precipitation from the %s report of %s ago (%s)", slug, precip.get("station"),
                     util.fmt_duration_minutes(precip.get("age") or 0.0), precip.get("passed"))
    raw = _step(run, label + "raw observation", _raw_observation, cache, site, obs, precip)
    if not isinstance(raw, dict):
        # the report's present weather, METAR and chosen report are lost: the latest report's
        # text alone would read "no precipitation reported" for rain shown only there
        raw = {"present_weather": [], "precip_last_hour_mm": None, "text": str(obs.get("text") or ""),
               "reports_weather": None}
        if station is not None:
            station["weather"] = "unknown"
            station["weather_why"] = "the station's report could not be read (raw observation step failed)"

    sun_info = None
    sun_up = None
    if cfg.show_sun:
        sun_info = _step(run, label + "sun times", sun.sun_summary, site["lat"], site["lon"], tz)
        if not isinstance(sun_info, dict):
            sun_info = None
        else:
            try:
                sun_up = float(sun_info.get("sun_alt_deg")) >= sun.ALT_SUNRISE
            except (TypeError, ValueError):
                sun_up = None

    frame = _step(run, label + "map frame", geo.MapFrame, site["lat"], site["lon"], cfg.map_km, cfg.map_px,
                  cfg.tile_zoom)
    split = _step(run, label + "alert classification", alerts.site_alerts, feed.get("alerts") or [], site, meta,
                  frame)
    classified = isinstance(split, dict)
    split = split if classified else {}
    at = [a for a in (split.get("at") or []) if isinstance(a, dict)]
    candidates = [a for a in (split.get("candidates") or []) if isinstance(a, dict)]

    radar_bits = _step(run, label + "radar", _radar_for_site, cfg, cache, run, site, frame, frame_res,
                       earlier_mrms)
    if not isinstance(radar_bits, dict):
        err = "radar step failed"
        radar_bits = {"check": {"available": False, "stale": True, "source": "radar", "frame_ts": None,
                                "in_ring": False, "nearest_km": None, "count": 0, "max_dbz": None, "error": err},
                      "render": mwp._radar_fallback(err), "island": None}

    glm_view = _step(run, label + "GLM view", glm.site_view, glm_res, site, cfg)
    if not isinstance(glm_view, dict):
        glm_view = {"covered": True, "sats": [], "available": False, "partial": False,
                    "error": "GLM site view failed", "count_window": 0, "count_by_sat": {}, "last_in_ring_t": None,
                    "nearest_km": None,
                    "nearest_bearing": None, "nearest_t": None, "nearest_sat": None, "flashes_on_map": [],
                    "window_min": cfg.glm_window_min}

    entry: Dict[str, Any] = {
        "site": site, "slug": slug, "tz": tz, "meta": meta, "obs": obs, "obs_station": station, "obs_raw": raw,
        "forecast": forecast, "hourly": hourly, "gridpoint": grid, "sun": sun_info, "sun_up": sun_up,
        "alerts_feed": {k: feed.get(k) for k in ("ok", "stale", "error", "age_s")},
        "alerts_at": at, "radar_check": radar_bits["check"], "island_radar": radar_bits["island"],
        "glm": glm_view,
    }
    # an alert the classification could not place (bad coordinates): the check reads the list
    skipped = split.get("skipped")
    entry["alerts_skipped"] = list(skipped) if isinstance(skipped, (list, tuple)) else []
    if not classified:
        # no classification is no answer: "none in effect" would be a guess at every site
        entry["alerts_feed"] = {"ok": False, "stale": True, "error": "alert classification failed",
                                "age_s": feed.get("age_s")}
    elif gap and feed.get("ok"):
        # the site's own state is not in the alert query: its warnings were never fetched
        entry["alerts_feed"] = {"ok": False, "stale": True, "error": gap, "age_s": feed.get("age_s")}
    if entry["island_radar"] is None:
        del entry["island_radar"]
    entry["clock"] = run.get("clock")              # clock guard
    st = decision.site_state(state, slug)
    verdict = _step(run, label + "verdict", decision.evaluate, cfg, entry, st, now)
    entry["verdict"] = verdict if isinstance(verdict, dict) else None

    notes = mwp._undrawn_notes(at)
    if radar_bits["island"] is None:
        notes += mwp._radar_coverage_notes(frame, frame_res)
    # flashes from a partial window are real flashes: drawn, with the key saying so
    glm_data = bool(glm_view.get("available") or glm_view.get("partial"))
    overlay = {"radar_km": None if radar_bits["check"].get("off") else float(cfg.radar_km),
               "glm_km": float(cfg.glm_km) if cfg.check_enabled("glm") and glm_view.get("covered", True) else None,
               "flashes": list(glm_view.get("flashes_on_map") or []) if glm_data else [],
               "glm_label": "GLM flashes, last %s min%s" % (
                   glm_view.get("window_min") or cfg.glm_window_min,
                   " (partial data)" if glm_view.get("partial") else "") if glm_data else None}
    rendered = _render_maps(cfg, cache, run, site, tz, radar_bits["render"], at + candidates, notes, overlay,
                            pending=pending_maps)
    drawn = rendered["drawn"]
    rendered_any = any(m.get("ok") for m in rendered["maps"].values())
    near = [a for a in candidates if _hashable(a.get("id")) and a.get("id") in drawn] if rendered_any \
        else list(candidates)
    entry.update({"alerts_near": near, "alerts_drawn_ids": sorted(drawn, key=str), "maps": rendered["maps"],
                  "map_notes": rendered["notes"]})
    v = entry["verdict"] or {}
    log.info("%s: %s%s | obs %s (%s), forecast %s, hourly %s, gridpoint %s, radar %s, GLM %s, alerts AT %d / "
             "NEAR %d, sun %s, maps %s",
             slug, v.get("status", "no verdict"), " (%s)" % "; ".join(v.get("reasons") or []) if v.get("reasons") else "",
             mwp._short(obs), (station or {}).get("id") or "-", mwp._short(forecast), mwp._short(hourly),
             mwp._short(grid), _radar_word(entry["verdict"], radar_bits["check"]),
             ("%d flash(es)%s" % (int(glm_view.get("count_window") or 0), "" if glm_view.get("available")
                                  else " (partial)")) if glm_data else "n/a",
             len(at), len(near), "up" if sun_up else ("down" if sun_up is False else "?"),
             "/".join("%s %s" % (t, "ok" if m["ok"] else "FAILED") for t, m in rendered["maps"].items()))
    return entry


def _site_placeholder(cfg, site: dict, error: str) -> Dict[str, Any]:
    tz = site.get("tz") if isinstance(site, dict) else None
    entry = mwp._site_placeholder(cfg, site, error)
    entry.update({"slug": site.get("slug"), "tz": tz or "UTC", "gridpoint": _gridpoint_fallback(error),
                  "obs_station": None,
                  "obs_raw": {"present_weather": [], "precip_last_hour_mm": None, "text": ""},
                  "radar_check": {"available": False, "error": error},
                  "glm": {"covered": True, "available": False, "error": error},
                  "verdict": None, "sun_up": None,
                  "alerts_feed": {"ok": False, "stale": True, "error": error}})
    return entry


def _state_error_text(err: Any) -> str:
    """The published part of ``decision.save_state``'s error: the errno name and message
    ("[EACCES] Permission denied"), not the OSError's file name, which holds the state
    path and a temp file's process id. Any other text is passed on (the run's scrub
    replaces local paths in it)."""
    text = str(err or "unknown error")
    m = re.match(r"\s*\[Errno (-?\d+)\] ([^:]+)", text)
    if m:
        import errno as _errno
        code = int(m.group(1))
        return "[%s] %s" % (_errno.errorcode.get(code, "Errno %d" % code), m.group(2).strip())
    return text


# ---- the run -------------------------------------------------------------------------------------
# A lock held longer than one scheduled interval (cron and the timer run every 30 min) is no
# overlapping run but a hung one, or another process holding the file: an error, exit 1.
LOCK_HELD_ERROR_S = 1800
# The temporary files of the atomic writers (weather.http._atomic_write, weather.page,
# weather.radar, vlba.decision.save_state): "<target>.tmp.<pid>". A run killed while
# writing (timeout(1), systemd's TimeoutStartSec) leaves one behind; a run takes seconds to
# write one, so a file an hour old is a leftover.
TMP_NAME = re.compile(r".\.tmp\.\d+$")
TMP_MAX_AGE_S = 3600
TMP_SCAN_MAX = 20000            # directory entries looked at in the cache at most
TMP_SCAN_DEPTH = 3


def _sweep_temp_files(cfg) -> None:
    """Delete leftover temporary files: in the output directory (top level), the cache
    directory (walked, bounded) and the state file's directory (its own temporary names).
    Only regular files (lstat: a link is never followed) of this user, named like a
    temporary file and older than ``TMP_MAX_AGE_S``. Every error is ignored."""
    import stat
    now = time.time()
    uid = os.geteuid()
    removed = []
    budget = [TMP_SCAN_MAX]

    def sweep(d: str, depth: int, match) -> None:
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for ent in entries:
            if budget[0] <= 0:
                return
            budget[0] -= 1
            try:
                st = os.lstat(ent.path)
                if stat.S_ISDIR(st.st_mode):
                    if depth > 0:
                        sweep(ent.path, depth - 1, match)
                    continue
                if (stat.S_ISREG(st.st_mode) and st.st_uid == uid and match(ent.name)
                        and now - st.st_mtime > TMP_MAX_AGE_S):
                    os.unlink(ent.path)
                    removed.append(ent.path)
            except OSError:
                continue

    try:
        is_tmp = TMP_NAME.search
        sweep(cfg.out_dir, 0, is_tmp)
        sweep(cfg.cache_dir, TMP_SCAN_DEPTH, is_tmp)
        state = getattr(cfg, "state_file", "") or ""
        if state:
            own = re.compile(re.escape(os.path.basename(state)) + r"\.tmp\.\d+$").match
            sweep(os.path.dirname(state) or ".", 0, own)
    except Exception as e:  # noqa: BLE001 — housekeeping must never stop a run
        log.debug("temporary file sweep failed: %s", e)
    if removed:
        log.info("removed %d leftover temporary file(s) of an interrupted run", len(removed))
        log.debug("removed: %s", ", ".join(removed))


def run_once(cfg, skip_radar: bool = False) -> Dict[str, Any]:
    with http.run_lock(cfg.lock_file):
        return _run_locked(cfg, skip_radar)


def _run_locked(cfg, skip_radar: bool) -> Dict[str, Any]:
    _sweep_temp_files(cfg)
    t0 = time.monotonic()
    os.makedirs(cfg.out_dir, exist_ok=True)
    cache = http.Cache(cfg.cache_dir)
    generated = util.utcnow()
    run: Dict[str, Any] = {"generated": generated, "generated_ts": generated.timestamp(),
                           "night_default": False, "alerts": None, "radar": None, "glm": None,
                           "radar_coverage": {}, "network": None, "errors": [], "sites": [], "summary": None}
    _step(run, "network budget", http.begin_run, cfg)
    log.info("run started: %d site(s) from %s, out %s, network budget %s s", len(cfg.sites),
             cfg.sites_source, cfg.out_dir, cfg.run_budget_s)
    mwp._log_alert_areas(cfg)
    names, unknown = parse_event_names(cfg.veto_events)
    log.info("verdict: warning products %s; radar %g dBZ within %g km (%d frame(s), hold %d s); GLM %s within "
             "%g km (hold %d s); forecast cloud %s, precip %s, thunder %s; winter %s; "
             "observation precip %s, humidity %s (station within %g km / %g m); wind %s",
             ", ".join(names) or "none", cfg.radar_dbz, cfg.radar_km, cfg.radar_trigger_after, cfg.radar_hold_s,
             "on" if cfg.check_enabled("glm") else "off", cfg.glm_km, cfg.glm_hold_s,
             *(("> %g %%" % v) if v >= 0 else "off"
               for v in (cfg.nws_cloud_max, cfg.nws_precip_max, cfg.nws_thunder_max)),
             "on" if cfg.nws_winter else "off", "on" if cfg.obs_precip else "off",
             ("> %g %%" % cfg.obs_humidity_max) if cfg.obs_humidity_max >= 0 else "off",
             cfg.obs_max_km, cfg.obs_max_delev_m, (">= %g mph" % cfg.wind_mph) if cfg.wind_mph > 0 else "off")
    for name in unknown:
        log.warning("VLBA_VETO_EVENTS: %r is not a known NWS event name (kept; check the spelling)", name)
        run["errors"].append("VLBA_VETO_EVENTS: %r is not a known NWS event name: it counts only if NWS "
                             "issues a product of exactly that name (check the spelling)" % name)
    if not names:
        log.warning("VLBA_VETO_EVENTS is empty: no NWS warning will make an antenna's weather BAD")
        run["errors"].append("VLBA_VETO_EVENTS is empty: no NWS warning makes an antenna's weather BAD")
    for text in cfg.doubtful_settings():
        log.warning("%s", text)
        run["errors"].append(text)
    for name in cfg.insecure_sources():
        log.warning("%s is a plain http:// URL: the data of a verdict check can be altered in transit", name)
        run["errors"].append("%s is a plain http:// URL: the data of a verdict check can be altered in "
                             "transit; use https://" % name)

    # 2. alerts, once
    feed = _source(run, "alerts feed", mwp._alerts_fallback, alerts.fetch_active, cfg, cache)
    run["alerts"] = feed
    if feed.get("alerts"):
        _step(run, "alert zone outlines", alerts.resolve_geometries, cfg, cache, feed["alerts"])

    # 3. the MRMS frame, once, and which part of each ring no radar covers (MRMS's own file
    # of the frame's time: the frame writes "no coverage" like "no echo")
    frame_res = mwp._load_radar(cfg, cache, run, skip_radar)
    run["radar"] = {k: v for k, v in frame_res.items() if k != "image"}
    conus = [s for s in cfg.sites if _in_mrms_grid(cfg, s)]
    if cfg.radar_coverage and frame_res.get("ok") and frame_res.get("image") is not None and conus:
        _radar_coverage(cfg, run, "CONUS", frame_res.get("ts"), conus, "radar coverage (MRMS CONUS)",
                        reused=bool(frame_res.get("stale")))
    # where a ring shows an echo, the frames before the newest (each fetched once, for all sites)
    earlier_mrms = _step(run, "earlier MRMS frames", _earlier_mrms, cfg, run, frame_res, conus) or {}

    # 4. GLM, once (the state file says which granules the last run examined)
    state = decision.load_state(cfg.state_file)
    if not cfg.glm_enabled:
        glm_res = _glm_off("GLM layer disabled (VLBA_GLM=0 or --skip-glm)")
        log.info("GLM lightning not fetched: disabled")
    elif not cfg.glm_satellites:
        glm_res = _glm_off("no GLM satellite configured (VLBA_GLM_BUCKETS)")
    elif not glm.deps_available():
        glm_res = _glm_off("GLM lightning unavailable: %s" % glm.deps_note())
        run["errors"].append(glm_res["skipped"])
        log.warning("%s", glm_res["skipped"])
    else:
        glm_res = _source(run, "GLM lightning", _glm_fallback, glm.fetch, cfg, None, state.get("glm_newest"),
                          state.get("glm_retry"), state.get("glm_seen"))
        if glm_res.get("ok") and glm_res.get("partial"):
            run["errors"].append("GLM lightning partial: %s" % (glm_res.get("error") or "a satellite failed"))
    run["glm"] = dict({k: v for k, v in glm_res.items() if k != "flashes"},
                      flash_count=len(glm_res.get("flashes") or []))
    if isinstance(glm_res.get("newest"), dict) and not glm_res.get("skipped"):
        state["glm_newest"] = dict(glm_res["newest"])       # only valid stamps survive a fetch
        state["glm_retry"] = dict(glm_res.get("retry") or {})
        state["glm_seen"] = dict(glm_res.get("seen") or {})

    # clock guard: the alert feed, the radar frame and GLM have answered; their servers' time
    # (Date) against this host's clock decides whether any time-keyed check can be trusted
    from vlba import clock
    run["clock"] = _step(run, "clock guard", clock.assess, http.clock_offsets(), clock.LIMIT_S)
    if clock.is_off(run["clock"]):
        run["errors"].append(clock.error_text(run["clock"]))
        log.error("%s (offsets per host: %s)", clock.error_text(run["clock"]), http.clock_offsets())

    # 5. the antennas
    pending_maps: List[Any] = []        # their maps wait under pending names until the page is written
    for site in cfg.sites:
        n_pending = len(pending_maps)
        try:
            entry = _run_site(cfg, cache, run, site, feed, frame_res, glm_res, state, earlier_mrms,
                              pending_maps=pending_maps)
        except Exception as e:  # noqa: BLE001 — one antenna must never take the others down
            del pending_maps[n_pending:]        # its placeholder shows no map: publish none
            log.exception("site %s failed: %s", site.get("slug"), e)
            run["errors"].append("site %s failed: %s" % (site.get("slug"), mwp._exc_text(e)))
            entry = _site_placeholder(cfg, site, mwp._exc_text(e))
        run["sites"].append(entry)
    # The state file is saved before the page: it carries the holds and the radar streak to
    # the next run, and losing it after a published page is the worse failure (a page that
    # cannot be written is retried by the next run; a hold that is lost is not).
    err = decision.save_state(cfg.state_file, state, generated)
    if err:
        # the path is in the log (save_state's warning); the page and status.json are public
        run["errors"].append("state file not written (%s): the radar confirmation and the holds do not "
                             "carry over to the next run" % _state_error_text(err))

    net = _step(run, "network status", http.run_status)
    run["network"] = net if isinstance(net, dict) else None
    verdicts = [s.get("verdict") for s in run["sites"]]
    run["summary"] = decision.summary(verdicts)
    # the palette follows the sun at the antennas: dark when it is down at most of them
    downs = [s.get("sun_up") for s in run["sites"] if isinstance(s.get("sun_up"), bool)]
    run["night_default"] = bool(downs) and sum(1 for up in downs if not up) * 2 > len(downs)

    # 6. output: nothing published names a local path, a user or a process id (the log does)
    subs = mwp._public_paths(cfg)
    mwp._scrub_run(cfg, run, subs)
    _publish_maps(run, pending_maps)
    try:
        page.write_page(cfg, run)
    except Exception as e:      # noqa: BLE001 - re-raised below: the exit status stays 1
        # status.json still says this run happened and its page did not (page_written false,
        # ok false): the previous one would go on saying ok
        log.error("index.html not written: %s", e)
        run["errors"].append(mwp._public_text("index.html not written: %s" % mwp._exc_text(e), subs))
        try:
            page.write_status_json(cfg, run)
        except Exception as e2:     # noqa: BLE001 - the page error is the one to raise
            log.error("status.json not written either: %s", e2)
        raise
    page.write_status_json(cfg, run)

    ts = run["radar"].get("ts")
    if run["radar"].get("ok") and ts is not None:
        radar_txt = ts.strftime("%H:%MZ") + (" stale" if run["radar"].get("stale") else "")
    elif run["radar"].get("not_applicable"):
        radar_txt = "not applicable"
    else:
        radar_txt = "none"
    g = run["glm"]
    glm_txt = ("%d flashes" % g.get("flash_count", 0)) if g.get("ok") else (g.get("skipped") or "unavailable")
    down = (run["network"] or {}).get("down_hosts") or []
    log.info("done: %s, sites ok %d/%d, alerts %d%s, MRMS %s, GLM %s, errors %d, %.1f s (budget %s s)%s",
             "GOOD %d / BAD %d%s" % (run["summary"]["good"], run["summary"]["bad"],
                                     " (%d incomplete)" % run["summary"]["incomplete"] if run["summary"]["incomplete"] else ""),
             sum(1 for s in run["sites"] if mwp._site_ok(s)), len(run["sites"]),
             len(feed.get("alerts") or []), "" if feed.get("ok") else " (feed unavailable)", radar_txt, glm_txt,
             len(run["errors"]), time.monotonic() - t0, cfg.run_budget_s,
             ", unreachable: %s" % ", ".join(down) if down else "")
    return run


# ---- CLI ------------------------------------------------------------------------------------------
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="make_vlba_page.py",
        description="Generate the VLBA weather page (one run). Settings come from WEATHER_* and VLBA_* "
                    "environment variables (see vlbaweather.env.example); the flags below override them.")
    p.add_argument("--out", metavar="DIR", help="output directory (default: $WEATHER_OUT_DIR or ./out)")
    p.add_argument("--cache", metavar="DIR",
                   help="cache directory (default: $WEATHER_CACHE_DIR or ~/.cache/vlbaweather)")
    p.add_argument("--verbose", "-v", action="store_true", help="DEBUG logging to stderr")
    p.add_argument("--skip-radar", action="store_true", help="do not fetch the MRMS frame")
    p.add_argument("--skip-glm", action="store_true", help="do not fetch GLM lightning (same as VLBA_GLM=0)")
    p.add_argument("--sites", metavar="SPEC",
                   help="'slug|Name|lat|lon[|tz];...' overrides the VLBA site list for one run")
    p.add_argument("--once", action="store_true", help="accepted for compatibility: every invocation is one run")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    mwp.setup_logging(args.verbose)
    logging.getLogger("vlba").setLevel(logging.DEBUG if args.verbose else logging.INFO)
    try:
        sites = None
        if args.sites is not None:
            sites = parse_sites(args.sites, "--sites")
            if not sites:
                raise ValueError("--sites lists no sites: %r" % args.sites)
        cfg = VlbaConfig.from_env(
            out_dir=os.path.abspath(os.path.expanduser(args.out)) if args.out else None,
            cache_dir=os.path.abspath(os.path.expanduser(args.cache)) if args.cache else None,
            sites=sites, verbose=True if args.verbose else None,
            glm_enabled=False if args.skip_glm else None)
    except (ValueError, TypeError) as e:
        log.error("configuration error: %s", e)
        return 2
    if cfg.verbose and not args.verbose:
        mwp.setup_logging(True)
        logging.getLogger("vlba").setLevel(logging.DEBUG)
    try:
        run_once(cfg, skip_radar=args.skip_radar)
    except http.AlreadyRunning as e:
        held = getattr(e, "held_s", None)
        if held is None:
            log.warning("already running (%s; held since an unknown time); nothing to do", e)
        elif held >= LOCK_HELD_ERROR_S:
            # a hung run, or another process holding the file: the page is not updated
            log.error("another run has held the lock for %d min (%s): no page is written until "
                      "it ends", held // 60, e)
            return 1
        else:
            log.info("already running (%s); nothing to do", e)
        return 0
    except http.LockError as e:
        log.error("run lock: %s", e)
        return 1
    except OSError as e:
        log.error("cannot write the output or cache directory: %s", e)
        return 1
    except Exception as e:      # noqa: BLE001 — a bug: report it, but do not hide it
        log.exception("run failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
