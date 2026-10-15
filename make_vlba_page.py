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
import logging
import os
import sys
import time
from datetime import datetime, timedelta
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
WEATHER_REPORT_MAX_AGE_MIN = 90  # an older report with weather says nothing about rain now


def _station_rows(cfg, site: dict, feats: Any, site_elev: Optional[float]) -> List[Dict[str, Any]]:
    """The stations of NWS's list for the grid cell, nearest first: ``{"id", "lat", "lon",
    "elev_m", "dist_km", "delev_m", "ok"}`` with ``ok`` = within ``cfg.obs_max_km`` and
    ``cfg.obs_max_delev_m`` of the antenna (an unknown height difference passes, an unknown
    position does not). An entry that cannot be read is skipped, never the whole list."""
    rows: List[Dict[str, Any]] = []
    for feat in feats if isinstance(feats, list) else []:
        try:
            props = feat.get("properties") if isinstance(feat.get("properties"), dict) else {}
            fid = props.get("stationIdentifier") or nws._last_segment(feat.get("id"))
            if not fid:
                continue
            geom = feat.get("geometry") if isinstance(feat.get("geometry"), dict) else {}
            try:
                lon, lat = float(geom["coordinates"][0]), float(geom["coordinates"][1])
            except (KeyError, IndexError, TypeError, ValueError):
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
        info["why"] = "distance to the antenna unknown; used as NWS's nearest station"
        if row is not None:
            info["elev_m"] = row["elev_m"]
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
    from either would be unknown passed off as clear. So would a METAR whose present-weather
    sensor is out (PWINO in its remarks): only the precipitation it still shows counts."""
    if not isinstance(props, dict):
        return False
    plh = props.get("precipitationLastHour")
    raw = props.get("rawMessage")
    if decision.metar_pwino(raw):
        # mist, haze or a thunderstorm in the vicinity still come through; rain does not
        return decision.observed_precipitation({"present_weather": _present_weather_texts(props), "metar": raw,
                                                "precip_last_hour_mm": util.quantity(plh)},
                                               str(props.get("textDescription") or "")) is not None
    return bool(str(props.get("textDescription") or "").strip() or str(raw or "").strip()
                or props.get("presentWeather") or props.get("cloudLayers")
                or (isinstance(plh, dict) and plh.get("value") is not None))


def _present_weather_texts(props: dict) -> List[str]:
    """NWS's decoded present weather of a report in words ("light rain", "thunderstorms in
    the vicinity"), as ``decision.observed_precipitation`` reads them."""
    out = []
    for w in props.get("presentWeather") or []:
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
    reads the temperature), else (None, None)."""
    ores = _fetch_latest(cfg, cache, site, sid)
    if not ores["ok"]:
        return None, None
    props = ores["data"]["properties"]
    rep = nws._parse_observation(props)
    if nws._unusable(dict(rep, temp_c=0.0), cfg.obs_max_age_min):
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
    age in minutes | None, "note", "stale": a last good copy of the history, "unread": the
    history could not be had}``."""
    url = "%s/stations/%s/observations?limit=%d" % (nws.BASE, sid, WEATHER_HISTORY)
    res = http.cached_json(cfg, cache, "obshist/%s/%s" % (site["slug"], sid), url, cfg.obs_ttl,
                           cfg.obs_max_stale, validate=_valid_history)
    out: Dict[str, Any] = {"able": None, "report": None, "age": None, "note": None,
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
    weather = [p for p in reports.values() if _reports_weather(p)]
    if not weather:
        if any(decision.metar_pwino(p.get("rawMessage")) for p in reports.values()):
            out["note"] = "its present-weather sensor is out of service (PWINO)"
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
    if isinstance(props, dict) and decision.metar_pwino(props.get("rawMessage")):
        return "%s's present-weather sensor is out (PWINO)" % own
    age = _report_age(props)
    if _reports_weather(props) and age is not None and age > WEATHER_REPORT_MAX_AGE_MIN:
        return "%s's latest is %s old" % (own, util.fmt_duration_minutes(age))
    return "%s's latest has no weather" % own


def _precip_report(cfg, cache, site: dict, station: dict, obs: dict) -> Dict[str, Any]:
    """Which report the precipitation check reads: ``{"weather": "reported" | "none" |
    "unknown", "station": the id of the station whose report it is, "props": that report's
    properties (None: the station's own latest), "age": its age in minutes | None, "stale":
    it is a last good copy, "passed": why the station's own latest is not read | None,
    "doubt": what may hold a newer report that could not be read | None, "why": the reason
    for "none" / "unknown"}``.

    The station's latest report when the station is usable, its report says anything about
    the weather and is recent (``_recent``). Otherwise the newest recent report with weather
    of the station and the next qualifying stations (at most ``nws.MAX_STATIONS``; all of
    them qualifying ones when the station itself is not usable: a qualifying station whose
    report lacks a temperature, which the observation needs, still says whether it rains),
    the nearer station on a tie (Brewster: CJDW reports no weather, KOMK does; a METAR
    station's blank record: Los Alamos, St. Croix). The latest reports first; a station's
    recent reports only when its latest is blank and newer than the best report found (older
    ones cannot beat that), so a history that could not be read is a ``doubt`` only when it
    could have held a newer report. Only the precipitation check reads the report: the
    wind and the humidity stay the station's. "none" only when the stations checked are all
    ones without weather (Kitt Peak's QSBA3); "unknown" otherwise, never "clear"."""
    qual = station.get("qualifying")
    own = station["id"] if station.get("usable") and obs.get("ok") and station.get("id") else None
    data, _ = cache.get_any("obs/%s/%s" % (site["slug"], own)) if own else (None, None)
    own_props = data.get("properties") if isinstance(data, dict) else None
    out: Dict[str, Any] = {"weather": "reported", "station": own, "props": None, "age": None,
                           "stale": bool(obs.get("stale")), "passed": None, "doubt": None, "why": None}
    if own and _reports_weather(own_props) and _recent(_report_age(own_props)):
        return out
    out["passed"] = _passed_over(station, own, own_props)
    checked = ([own] if own else []) + [q for q in (qual or []) if q != own][:nws.MAX_STATIONS]
    order = list(qual or []) + [own]
    rank = {q: order.index(q) for q in checked}           # nearest first
    latest: Dict[str, Any] = {}
    for q in checked:
        if q == own:
            latest[q] = (own_props, bool(obs.get("stale")))
        else:
            ores = _fetch_latest(cfg, cache, site, q)
            latest[q] = (ores["data"]["properties"], bool(ores.get("stale"))) if ores["ok"] else (None, False)
    found = []                  # (time, rank, station, properties, stale) of recent reports with weather
    able: Dict[str, Optional[bool]] = {}
    for q in checked:
        p, stale = latest[q]
        if _reports_weather(p):
            able[q] = True      # its latest is its newest report: the older ones cannot beat it
            if _recent(_report_age(p)):
                found.append((_report_time(p), rank[q], q, p, stale))
    notes, unread = [], []
    for q in sorted((q for q in checked if q not in able), key=lambda q: rank[q]):
        q_time = _report_time(latest[q][0])
        best = max(f[0] for f in found) if found else None
        if best is not None and q_time is not None and q_time < best:
            continue            # its newest report is older than the best one found
        h = _weather_history(cfg, cache, site, q)
        able[q] = h["able"]
        if h["note"]:
            notes.append("%s: %s" % (q, h["note"]))
        if h["report"] is not None:
            found.append((_report_time(h["report"]), rank[q], q, h["report"], h["stale"]))
        elif h["unread"]:
            unread.append((q, q_time))
    if found:
        t, _, q, p, stale = min(found, key=lambda f: (-f[0], f[1]))
        hidden = [u for u, u_time in unread if u_time is None or u_time >= t]
        out.update(station=q, props=p, age=max(0.0, _report_age(p)), stale=stale,
                   doubt="the recent reports of %s, maybe newer, could not be read" % ", ".join(hidden)
                   if hidden else None)
        return out
    out.update(station=None, stale=False)
    if qual is not None and checked and all(able.get(q) is False for q in checked):
        out.update(weather="none", why="%s report%s no weather (no present-weather sensor)" % (
            ", ".join(checked), "s" if len(checked) == 1 else ""))
        return out
    out.update(weather="unknown", why="no report with weather of the last %d min from %s%s" % (
        WEATHER_REPORT_MAX_AGE_MIN, ", ".join(checked) or "a qualifying station",
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
                   report_doubt=precip.get("doubt"),
                   text=str(props.get("textDescription") or "") if isinstance(props, dict) else "")
    elif not obs.get("ok") or not obs.get("station_id"):
        return out
    else:
        data, _ = cache.get_any("obs/%s/%s" % (site["slug"], obs["station_id"]))
        props = data.get("properties") if isinstance(data, dict) else None
    if not isinstance(props, dict):
        return out
    out["reports_weather"] = _reports_weather(props)
    out["metar"] = str(props.get("rawMessage") or "")
    out["present_weather"] = _present_weather_texts(props)
    out["precip_last_hour_mm"] = util.quantity(props.get("precipitationLastHour"))
    return out


# ---- radar per site --------------------------------------------------------------------------
def _in_mrms_grid(cfg, site: dict, frame=None) -> bool:
    """Whether the site's radar is the MRMS frame: its map reaches into the grid (a frame
    that cannot be worked out counts as inside: the parent's radar then applies)."""
    try:
        if frame is None:
            frame = geo.MapFrame(site["lat"], site["lon"], cfg.map_km, cfg.map_px, cfg.tile_zoom)
        return geo.bbox_intersects(tuple(frame.bbox), geo.MRMS_BBOX)
    except Exception:           # noqa: BLE001
        return True


def _radar_coverage(cfg, run: dict, domain: str, frame_ts, sites: List[dict], what: str) -> Dict[str, Any]:
    """The radar coverage of the sites' rings in MRMS's own file of the frame's time
    (``vlba.mrms.coverage``), kept in ``run["radar_coverage"][domain]``; failing is a problem
    of the run."""
    cov = _step(run, what, mrms.coverage, cfg, domain, frame_ts, sites, float(cfg.radar_km))
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


def _stale_limit(cfg) -> datetime:
    """The oldest frame time the verdict can still use (``cfg.radar_stale_s``): an earlier
    frame older than this would be "no data", so none is asked for."""
    return util.utcnow() - timedelta(seconds=float(cfg.radar_stale_s))


def _earlier_mrms(cfg, run: dict, frame_res: dict, sites: List[dict]) -> Dict[str, List[Dict[str, Any]]]:
    """For every site whose ring shows an echo in the newest MRMS frame, the ring checks of
    the frames before it, ``cfg.radar_trigger_after - 1`` of them, oldest first: with them a
    run confirms an echo by itself, which runs 30 min apart could not (frames that far apart
    are not consecutive). IEM writes a frame every 2 min, but a radar completes a volume scan
    only every 4.5-10 min, so a frame whose ring holds the same data as the next newer one
    counted (``radarcheck.ring_digest``) is no second observation: it is passed over and the
    search goes further back, never to a frame older than ``cfg.radar_stale_s``. Each frame is
    fetched once for all sites and dropped after their ring scans (an MRMS frame takes ~25 MB
    of memory). Only after a fresh newest frame, not a copy reused after a failed fetch. A
    frame missing from the archive is passed over (twice at most); a fetch that fails ends
    the search and is a problem of the run. Returns ``{slug: [check, ...]}``."""
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
    oldest = _stale_limit(cfg)
    ts, misses, err, fetched, same = newest, 0, None, 0, 0
    while any(len(n["checks"]) < want for n in need.values()):
        ts = ts - timedelta(minutes=2)
        if ts < oldest:
            break
        try:
            url = ts.strftime(cfg.mrms_archive)
            there = http.head_ok(url, cfg, timeout=wradar.HEAD_TIMEOUT)
        except Exception as e:  # noqa: BLE001 - head_ok should not raise; if it does, no answer
            err = "%s: %s" % (type(e).__name__, e)
            break
        if there is None:
            err = "the MRMS archive did not answer"
            break
        if not there:
            misses += 1
            if misses > 2:
                err = "frames missing from the archive"
                break
            continue
        try:
            older = wradar._open_frame(http.get_bytes(url, cfg, accept="image/png"))
        except (http.HttpError, wradar.RadarError) as e:
            err = str(e).replace(url, "the archive")
            break
        except Exception as e:  # noqa: BLE001 - any surprise = no earlier frame, never a crash
            err = "%s: %s" % (type(e).__name__, e)
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
                                        frame_ts=ts, digest=digest))
                n["digest"] = digest
        older = None
    log.info("MRMS: %d earlier frame(s) read for %s (%d ring(s) with the same data passed over)", fetched,
             ", ".join("%s %d" % (slug, len(n["checks"])) for slug, n in need.items()), same)
    if err:
        run["errors"].append("MRMS frame before %s unavailable (%s): an echo in a ring is not confirmed by "
                             "this run alone" % (newest.strftime("%H:%MZ"), err))
    return {slug: list(reversed(n["checks"])) for slug, n in need.items()}


def _earlier_island_checks(cfg, run: dict, site: dict, frame, island: dict, label: str,
                           digest: Optional[str]) -> List[Dict[str, Any]]:
    """As ``_earlier_mrms``, for an island site whose ring shows an echo in the newest
    composite: the frames the capabilities list before it, newest first, fetched with
    ``TIME`` one at a time; a frame with the same data in the ring as the next newer one
    counted (``digest``: the newest frame's) is passed over, none older than
    ``cfg.radar_stale_s`` is asked for. A frame that fails ends the search and is a problem of
    the run; one with a pixel of unknown colour in the ring is unusable and passed over, like
    the newest frame would be. Returns the checks oldest first."""
    slug, lat, lon = site["slug"], site["lat"], site["lon"]
    want = max(0, int(cfg.radar_trigger_after) - 1)
    km = float(cfg.radar_km)
    times = nwsradar.earlier_times(island, _stale_limit(cfg)) if want else []
    out: List[Dict[str, Any]] = []
    for t in times:
        if len(out) >= want:
            break
        when = t.strftime("%H:%MZ")
        f = _step(run, "%s radar composite of %s" % (slug, when), nwsradar.fetch_at, cfg, frame, island, t)
        if not isinstance(f, dict) or not f.get("ok") or f.get("index") is None:
            run["errors"].append("%s radar composite of %s unavailable (%s): an echo in the ring is not "
                                 "confirmed by this run alone" % (slug, when, (f or {}).get("error") or "no data"))
            break
        d = radarcheck.ring_digest(f["index"], lat, lon, km, frame=frame)
        if d is not None and d == digest:
            continue                    # the same radar data in the ring: no second observation
        colors = f.get("colors") if isinstance(f.get("colors"), dict) else {}
        if colors.get("unknown_px") and _step(run, "%s radar colours (%s)" % (slug, when), radarcheck.mask_ring,
                                              f.get("unknown_mask"), frame, lat, lon, km):
            continue                    # unknown colours in the ring: no usable data
        ring = _step(run, "%s radar ring (%s)" % (slug, when), radarcheck.index_ring, f["index"], frame, lat, lon,
                     km, float(cfg.radar_dbz))
        if not isinstance(ring, dict):
            continue
        out.append(dict(ring, available=True, stale=False, error=None, source=label, frame_ts=t, digest=d))
        digest = d
    if want and not times and isinstance(island.get("ts"), datetime):
        run["errors"].append("%s radar composite: no earlier frame listed within %d min: an echo in the ring is "
                             "not confirmed by this run alone" % (slug, int(cfg.radar_stale_s) // 60))
    return list(reversed(out))


def _radar_for_site(cfg, cache, run: dict, site: dict, frame, frame_res: dict,
                    earlier_mrms: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    """``{"check": radar_check dict, "render": the radar dict for the renderer, "island":
    the island result without images | None}`` for one site: MRMS inside the grid, the NWS
    composite outside it. A check with a ring scan carries the ring's radar ``coverage``."""
    slug = site["slug"]
    lat, lon = site["lat"], site["lon"]
    in_grid = _in_mrms_grid(cfg, site, frame) if frame is not None else True
    if in_grid:
        check: Dict[str, Any] = {"available": False, "stale": bool(frame_res.get("stale")), "source": "MRMS",
                                 "frame_ts": frame_res.get("ts"), "in_ring": False, "nearest_km": None,
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
        if unknown:
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
                                      "%s radar coverage (MRMS %s)" % (slug, domain))
                check["coverage"] = mrms.site_coverage(cov, slug)
        elif check["available"]:
            check["coverage"] = {"off": True}
        if check["available"]:
            check["digest"] = radarcheck.ring_digest(island["index"], lat, lon, float(cfg.radar_km), frame=frame)
        if check["available"] and check.get("in_ring") and not island.get("stale"):
            check["earlier"] = _earlier_island_checks(cfg, run, site, frame, island, label, check["digest"])
        if check["error"]:
            run["errors"].append("%s radar check: %s" % (slug, check["error"]))
        elif not island.get("stale") and island.get("ts") is None and island.get("error"):
            run["errors"].append("%s radar composite: %s" % (slug, island["error"]))     # frame time unknown
    render = {k: v for k, v in island.items() if k not in ("index", "unknown_mask", "times")}
    render["label"] = label
    return {"check": check, "render": render,
            "island": {k: v for k, v in island.items() if k not in ("image", "index", "unknown_mask", "times")}}


# ---- one antenna ---------------------------------------------------------------------------------
def _render_maps(cfg, cache, run: dict, site: dict, tz: str, render_radar: dict, drawable: List[dict],
                 notes: List[str], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Both theme maps through ``maps.VlbaSiteMap``; returns ``{"maps", "drawn", "notes",
    "flashes_drawn"}`` the way the parent's per-site step collects them."""
    slug = site["slug"]
    map_site = dict(site, tz=tz)
    out: Dict[str, Dict[str, Any]] = {}
    drawn: set = set()
    map_notes = list(notes)
    flashes_drawn = 0
    for theme, tile_url in cfg.themes:
        basename = cfg.map_basename(slug, theme)
        path = os.path.join(cfg.out_dir, basename)
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
            drawn.update(i for i in (res.get("drawn_ids") or []) if i is not None)
        else:
            run["errors"].append("%s not written: %s" % (what, res.get("error") or "unknown error"))
        if note:
            run["errors"].append("%s: %s" % (what, note))
            map_notes.append("%s theme: %s" % (theme, note))
    return {"maps": out, "drawn": drawn, "notes": map_notes, "flashes_drawn": flashes_drawn}


def _run_site(cfg, cache, run: dict, site: dict, feed: dict, frame_res: dict, glm_res: dict,
              state: dict, earlier_mrms: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> Dict[str, Any]:
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
    station = _step(run, label + "station", _station_info, cfg, cache, site, obs, grid.get("elevation_m"))
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
        if precip.get("props") is not None:
            log.info("%s: precipitation from the %s report of %s ago (%s)", slug, precip.get("station"),
                     util.fmt_duration_minutes(precip.get("age") or 0.0), precip.get("passed"))
    raw = _step(run, label + "raw observation", _raw_observation, cache, site, obs, precip)
    raw = raw if isinstance(raw, dict) else {"present_weather": [], "precip_last_hour_mm": None,
                                             "text": str(obs.get("text") or "")}

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
    split = split if isinstance(split, dict) else {}
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
    if entry["island_radar"] is None:
        del entry["island_radar"]
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
    rendered = _render_maps(cfg, cache, run, site, tz, radar_bits["render"], at + candidates, notes, overlay)
    drawn = rendered["drawn"]
    rendered_any = any(m.get("ok") for m in rendered["maps"].values())
    near = [a for a in candidates if a.get("id") in drawn] if rendered_any else list(candidates)
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


# ---- the run -------------------------------------------------------------------------------------
def run_once(cfg, skip_radar: bool = False) -> Dict[str, Any]:
    with http.run_lock(cfg.lock_file):
        return _run_locked(cfg, skip_radar)


def _run_locked(cfg, skip_radar: bool) -> Dict[str, Any]:
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
    if not names:
        log.warning("VLBA_VETO_EVENTS is empty: no NWS warning will make an antenna's weather BAD")

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
        _radar_coverage(cfg, run, "CONUS", frame_res.get("ts"), conus, "radar coverage (MRMS CONUS)")
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

    # 5. the antennas
    for site in cfg.sites:
        try:
            entry = _run_site(cfg, cache, run, site, feed, frame_res, glm_res, state, earlier_mrms)
        except Exception as e:  # noqa: BLE001 — one antenna must never take the others down
            log.exception("site %s failed: %s", site.get("slug"), e)
            run["errors"].append("site %s failed: %s" % (site.get("slug"), mwp._exc_text(e)))
            entry = _site_placeholder(cfg, site, mwp._exc_text(e))
        run["sites"].append(entry)
    err = decision.save_state(cfg.state_file, state, generated)
    if err:
        run["errors"].append("state file %s not written (%s): the radar confirmation and the holds do not "
                             "carry over to the next run" % (cfg.state_file, err))

    net = _step(run, "network status", http.run_status)
    run["network"] = net if isinstance(net, dict) else None
    verdicts = [s.get("verdict") for s in run["sites"]]
    run["summary"] = decision.summary(verdicts)
    # the palette follows the sun at the antennas: dark when it is down at most of them
    downs = [s.get("sun_up") for s in run["sites"] if isinstance(s.get("sun_up"), bool)]
    run["night_default"] = bool(downs) and sum(1 for up in downs if not up) * 2 > len(downs)

    # 6. output
    page.write_page(cfg, run)
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
        log.info("already running (%s); nothing to do", e)
        return 0
    except OSError as e:
        log.error("cannot write the output or cache directory: %s", e)
        return 1
    except Exception as e:      # noqa: BLE001 — a bug: report it, but do not hide it
        log.exception("run failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
