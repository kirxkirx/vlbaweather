#!/usr/bin/env python3
# vlbaweather: local-weather-awareness's make_weather_page.py (commit c3a13b4), copied as
# weather.pipeline: the run steps vlbaweather's generator reuses. Its own command line
# (main) makes that project's page and is not used here.
"""local-weather-awareness entry point: one run of the static weather-page generator.

One run does, in order:

1. take the single-instance lock (a second copy started by the scheduler while a slow run
   is still going exits 0 with a log line instead of fighting over the output files) and arm
   the network budget (``http.begin_run``: once ``cfg.run_budget_s`` seconds are spent, or
   once a host has failed at the transport level, further requests fail at once and every
   source falls back to its last good copy, so a hanging upstream cannot stall the run),
2. fetch the active NWS alerts once and resolve the outlines of zone-based alerts,
3. fetch the latest MRMS radar frame once (it is shared by every site's maps), unless no
   site's map reaches the MRMS grid (the contiguous US): radar is then "not applicable",
4. unless ``WEATHER_ROADS=0``, and only when at least one site's map reaches New Mexico:
   fetch New Mexico's emergency road closures once (NMDOT's NMRoads feed) plus, unless
   ``WEATHER_LSR=0``, the NWS storm reports from New Mexico that name a closed or flooded
   road (``weather.roads``),
5. per site: point metadata, current conditions, the 7-day and hourly forecasts, sun and
   twilight times, the alerts AT / NEAR the site, the road closures and storm reports on its
   map, and one radar map per theme (alerts shaded, road closures drawn),
6. write ``index.html`` and ``status.json``.

Local times use the site's NWS time zone, or the static ``tz`` from the site config when
the ``/points`` metadata is unavailable (UTC only when neither is known). Nothing here
depends on which sites are configured: the alert query covers the states, territories and
marine areas on the sites' maps (``WEATHER_ALERT_AREAS=auto``; a site whose own state an
explicit list leaves out is reported), the road sources are skipped when no map reaches New
Mexico, and a map outside the MRMS grid (contiguous US) says that it can show no radar.

Every fetch and render step is guarded: a failing source becomes a "not ok" status dict on
the page plus a line in the run's error list, never a crash, because the scheduler (cron,
or a systemd timer) must keep publishing whatever is available. The exit code is 0 even
when every source is down; it is non-zero only for a configuration error or an unwritable
output or cache directory — the things a retry in five minutes will not fix. The run dict
also records the network budget / circuit-breaker state (``run["network"]``) so
status.json can name the cause.

Road closures are an overlay: a road source that is down, a road step that crashes, or a
renderer that fails while drawing them never costs a site its maps (the map is then drawn
again without the road layer) or the page.

usage: make_weather_page.py [--out DIR] [--cache DIR] [--verbose] [--skip-radar]
                            [--sites SPEC] [--once]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from weather import alerts, geo, http, nws, page, radar, roads, sun, util
from weather.config import Config, parse_alert_areas, parse_sites

log = logging.getLogger("weather.main")

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Field lists of the per-site products, so a step that *crashed* (as opposed to one that
# reported "not ok" itself) still yields the full DESIGN.md dict shape for the page.
META_FIELDS = tuple(getattr(nws, "META_FIELDS", ()) or (
    "grid_id", "grid_x", "grid_y", "office", "forecast_url", "hourly_url", "stations_url",
    "tz", "city", "state", "radar_station", "forecast_zone", "county_zone", "fire_zone",
    "zone_urls"))
OBS_FIELDS = tuple(getattr(nws, "OBS_FIELDS", ()) or (
    "station_id", "station_name", "timestamp", "age_min", "text", "icon", "temp_f", "temp_c",
    "dewpoint_f", "dewpoint_c", "rh", "wind_mph", "wind_kmh", "gust_mph", "gust_kmh",
    "wind_dir_deg", "wind_dir", "pressure_inhg", "pressure_hpa", "visibility_mi",
    "visibility_km", "heat_index_f", "wind_chill_f", "cloud_layers"))


# ---- status-dict helpers -----------------------------------------------------------------
def _failed(error: str, **extra: Any) -> Dict[str, Any]:
    """The common status keys for a step that produced nothing, plus product keys."""
    d: Dict[str, Any] = {"ok": False, "stale": True, "error": error, "age_s": None}
    d.update(extra)
    return d


def _meta_fallback(error: str, tz: Optional[str] = None) -> Dict[str, Any]:
    d = _failed(error, **{k: None for k in META_FIELDS})
    d["tz"] = tz or "UTC"       # every other product formats times with it: never None
    d["zone_urls"] = []
    return d


def _site_tz(site: dict, meta: Dict[str, Any]) -> str:
    """The zone for the site's local times: the NWS ``/points`` value while that metadata
    is usable (fresh or last-good), otherwise the static ``tz`` from the site config, and
    UTC only when neither is known."""
    if meta.get("ok") and meta.get("tz"):
        return str(meta["tz"])
    return site.get("tz") or "UTC"


def _obs_fallback(error: str) -> Dict[str, Any]:
    d = _failed(error, **{k: None for k in OBS_FIELDS})
    d["cloud_layers"] = []
    return d


def _forecast_fallback(error: str) -> Dict[str, Any]:
    return _failed(error, updated=None, periods=[])


def _hourly_fallback(error: str) -> Dict[str, Any]:
    return _failed(error, updated=None, hours=[])


def _alerts_fallback(error: str) -> Dict[str, Any]:
    return _failed(error, fetched_ts=None, alerts=[], count_raw=0)


def _radar_fallback(error: str, stale: bool = True) -> Dict[str, Any]:
    d = _failed(error, ts=None, url=None, image=None)
    d["stale"] = stale
    return d


def _nm_roads_fallback(error: str) -> Dict[str, Any]:
    return _failed(error, events=[], count_raw=0, count_region=0, feed_time=None, excluded={})


def _lsr_fallback(error: str) -> Dict[str, Any]:
    return _failed(error, reports=[])


def _lsr_off() -> Dict[str, Any]:
    """Storm reports switched off (``WEATHER_LSR=0``): nothing is fetched and nothing is
    wrong, so the status keys say "ok, nothing to show" (a deliberate switch must not make
    every run "degraded") and ``disabled`` tells the page why the list is empty."""
    return {"ok": True, "stale": False, "error": None, "age_s": None, "reports": [],
            "disabled": True}


def _dict_list(value: Any) -> List[dict]:
    """The dict items of a list (anything else -> []): road items come from a module that
    parses third-party feeds, so the run guards the shapes it passes on."""
    if not isinstance(value, (list, tuple)):
        return []
    return [v for v in value if isinstance(v, dict)]


def _exc_text(e: BaseException) -> str:
    return "%s: %s" % (type(e).__name__, e)


# ---- step guards --------------------------------------------------------------------------
def _step(run: dict, what: str, fn, *args: Any, **kwargs: Any) -> Any:
    """Run one step of the pipeline. An exception (a bug or a surprise in a payload) is
    logged with its traceback, listed in ``run["errors"]`` and turned into ``None`` so the
    caller substitutes a not-ok dict and the run goes on with the next source."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:      # noqa: BLE001 — one failing source must never stop the run
        log.exception("%s failed: %s", what, e)
        run["errors"].append("%s failed: %s" % (what, _exc_text(e)))
        return None


def _note_status(run: dict, what: str, res: Dict[str, Any]) -> None:
    """A source that is unavailable or serving a last-good copy is a problem worth listing
    in the page footer and status.json, even though the module handled it gracefully. A
    product NWS does not provide for the location (``not_provided``) is not a problem."""
    if res.get("not_provided"):
        return
    if not res.get("ok"):
        run["errors"].append("%s unavailable: %s" % (what, res.get("error") or "no data"))
    elif res.get("stale"):
        run["errors"].append("%s stale (%s old): %s"
                             % (what, util.fmt_age(res.get("age_s")),
                                res.get("error") or "refresh failed"))


def _source(run: dict, what: str, fallback, fn, *args: Any) -> Dict[str, Any]:
    """A fetch step: the result always carries the status keys (``fallback(error)`` when
    the step raised or returned garbage) and its problems are recorded."""
    res = _step(run, what, fn, *args)
    if not isinstance(res, dict):
        err = run["errors"][-1] if res is None and run["errors"] else \
            "%s returned %s instead of a dict" % (what, type(res).__name__)
        res = fallback(err)
    _note_status(run, what, res)
    return res


# ---- the alert areas --------------------------------------------------------------------------
def _log_alert_areas(cfg) -> None:
    """Log which states, territories and marine areas the alert query covers; with the
    default ``WEATHER_ALERT_AREAS=auto`` they are worked out from the sites' maps. An
    explicit list that leaves out the state of a site (the site list changed, the area list
    did not) gets a warning per site: that site's alerts are never fetched."""
    try:
        codes = cfg.alert_area_codes
        auto, extra = parse_alert_areas(cfg.alert_areas)
    except Exception as e:      # noqa: BLE001 - fetch_active reports it as the feed error
        log.warning("alert areas: %s", _exc_text(e))
        return
    if not auto:
        how = "WEATHER_ALERT_AREAS"
    elif extra:
        how = "auto: the areas on the sites' maps, plus %s from WEATHER_ALERT_AREAS" % ",".join(extra)
    else:
        how = "auto: the states, territories and marine areas on the sites' maps"
    log.info("alert areas: %s (%s)", ",".join(codes), how)
    try:
        gaps = cfg.sites_outside_alert_areas
    except Exception:           # noqa: BLE001 - only a warning
        gaps = []
    for site, here in gaps:
        log.warning("alert areas: WEATHER_ALERT_AREAS=%s leaves out %s, where site %s lies: "
                    "its alerts are not fetched (use auto, or add the code)",
                    cfg.alert_areas, " or ".join(here), site.get("slug"))


def _alert_area_gap(cfg, site: dict, meta: Dict[str, Any]) -> Optional[str]:
    """A run error when the alert query leaves out the state or territory of the site's own
    NWS zones (their ids start with its code: ``NMZ220`` is in NM), e.g. an explicit
    ``WEATHER_ALERT_AREAS`` that predates a change of the site list. None when it is
    covered, or when the zones are unknown."""
    zone = meta.get("forecast_zone") or meta.get("county_zone")
    if not meta.get("ok") or not isinstance(zone, str) or len(zone) < 3:
        return None
    try:
        codes = cfg.alert_area_codes
    except Exception:           # noqa: BLE001 - the alerts feed step reports this
        return None
    code = zone[:2].upper()
    if code in codes:
        return None
    return ("%s alerts not fetched: its NWS zone %s is in %s, which the alert query (%s) "
            "leaves out; set WEATHER_ALERT_AREAS=auto or add %s"
            % (site.get("slug"), zone, code, ",".join(codes), code))


# ---- the radar frame ------------------------------------------------------------------------
RADAR_NOT_APPLICABLE = "no radar here: MRMS covers only the contiguous US"


def _maps_reach_mrms(cfg) -> bool:
    """Whether any site's map reaches the MRMS grid (the contiguous US plus a margin). When
    the map boxes cannot be worked out, the answer is yes: the frame is fetched as usual."""
    try:
        return any(geo.bbox_intersects(tuple(b), geo.MRMS_BBOX) for b in cfg.frame_bboxes)
    except Exception as e:      # noqa: BLE001 - fetch rather than silently drop the radar
        log.warning("cannot tell whether a map reaches the MRMS grid (%s); fetching the frame",
                    _exc_text(e))
        return True


def _radar_not_applicable() -> Dict[str, Any]:
    """No site's map reaches the MRMS grid (Hawaii, Alaska, the territories): nothing is
    fetched and nothing is wrong, so the run is not degraded by it."""
    d = _radar_fallback(RADAR_NOT_APPLICABLE, stale=False)
    d["not_applicable"] = True
    return d


def _load_radar(cfg, cache, run: dict, skip_radar: bool) -> Dict[str, Any]:
    """The MRMS frame for this run, or a not-ok dict saying why there is none. A deliberate
    skip is not an error: the maps are still drawn (basemap + alerts) and labelled. When no
    site's map reaches the MRMS grid, nothing is fetched (``not_applicable``)."""
    if not _maps_reach_mrms(cfg):
        log.info("radar frame not loaded: no site's map reaches the MRMS grid (contiguous US)")
        return _radar_not_applicable()
    why = None
    if skip_radar:
        why = "radar skipped (--skip-radar)"
    elif not cfg.radar_enabled:
        why = "radar disabled (WEATHER_RADAR=0)"
    elif not radar.deps_available():
        why = "Pillow not installed"
    if why:
        log.info("radar frame not loaded: %s", why)
        return _radar_fallback(why, stale=False)
    res = _source(run, "radar frame", _radar_fallback, radar.load_frame, cfg, cache)
    if res.get("ok"):
        ts = res.get("ts")
        log.info("radar frame %s%s", ts.strftime("%H:%MZ") if ts else "?",
                 " (STALE, %s old)" % util.fmt_age(res.get("age_s")) if res.get("stale") else "")
    return res


# ---- road closures ----------------------------------------------------------------------------
NMDOT_WHAT = "road closures (NMDOT)"
LSR_WHAT = "storm reports (NWS)"
ROADS_NOT_APPLICABLE = "no site's map reaches New Mexico, the only state with a road source"


def _maps_reach_nm(cfg) -> bool:
    """Whether any site's map reaches New Mexico, the only state the road sources cover.
    When the map boxes cannot be worked out, the answer is yes: the roads are then fetched
    as usual and the per-site steps report the map-frame problem."""
    try:
        return any(roads.bbox_covers_nm(b) for b in cfg.frame_bboxes)
    except Exception as e:      # noqa: BLE001 - fetch rather than silently drop the roads
        log.warning("cannot tell whether a map reaches New Mexico (%s); fetching the roads",
                    _exc_text(e))
        return True


def _load_roads(cfg, cache, run: dict) -> Optional[Dict[str, Dict[str, Any]]]:
    """New Mexico's emergency road closures and the road-related NWS storm reports, fetched
    once per run: ``{"nmdot": <fetch_nm_roads result>, "lsr": <fetch_storm_reports result>}``
    (full results, events and reports included, for the per-site selection), or None when
    ``WEATHER_ROADS=0`` or when no site's map reaches New Mexico. Then nothing is fetched and
    the run dict has no ``roads`` key; in the second case ``run["roads_not_applicable"]``
    gives the reason (status.json reports roads as "not applicable").

    ``run["roads"]`` gets both status dicts without their item lists (DESIGN.md: the page's
    footer and status.json read it). A source that fails is a not-ok dict plus a line in
    ``run["errors"]``, like every other source."""
    if not cfg.roads_enabled:
        log.info("road closures not loaded: disabled (WEATHER_ROADS=0)")
        return None
    if not _maps_reach_nm(cfg):
        log.info("road closures not loaded: %s", ROADS_NOT_APPLICABLE)
        run["roads_not_applicable"] = ROADS_NOT_APPLICABLE
        return None
    nm = _source(run, NMDOT_WHAT, _nm_roads_fallback, roads.fetch_nm_roads, cfg, cache)
    nm["events"] = _dict_list(nm.get("events"))
    if nm.get("ok") and not nm.get("stale") and nm.get("error"):
        # e.g. rss.xml down while nmroads.json answered: the closures are there, but their
        # causes (and so the "water on road" items) are not
        run["errors"].append("%s incomplete: %s" % (NMDOT_WHAT, nm["error"]))
    if cfg.lsr_enabled:
        lsr = _source(run, LSR_WHAT, _lsr_fallback, roads.fetch_storm_reports, cfg, cache)
    else:
        log.info("storm reports not loaded: disabled (WEATHER_LSR=0)")
        lsr = _lsr_off()
    lsr["reports"] = _dict_list(lsr.get("reports"))
    run["roads"] = {"nmdot": {k: v for k, v in nm.items() if k != "events"},
                    "lsr": {k: v for k, v in lsr.items() if k != "reports"}}
    kinds = [e.get("kind") for e in nm["events"]]
    log.info("road closures: NMDOT %s (%d closed, %d water on road in NM; %s feed items), "
             "storm reports %s (%d)", _short(nm), kinds.count("closure"), kinds.count("water"),
             nm.get("count_raw", "?"), "off" if lsr.get("disabled") else _short(lsr),
             len(lsr["reports"]))
    return {"nmdot": nm, "lsr": lsr}


def _site_roads(run: dict, label: str, road_src: Dict[str, Dict[str, Any]],
                frame: Any) -> Optional[Dict[str, Any]]:
    """The road items on one site's map: ``{"covers_nm", "events", "reports"}``, or None
    when they cannot be worked out (no map frame, or the selection crashed — recorded in
    ``run["errors"]``; the page then shows no road block rather than a wrong "none")."""
    if frame is None:
        return None                         # the map-frame step failed and said so
    res = _step(run, label + "road closures", roads.site_roads,
                road_src["nmdot"], road_src["lsr"], frame)
    if res is None:
        return None
    if not isinstance(res, dict):
        run["errors"].append("%sroad closures failed: site_roads returned %s instead of a dict"
                             % (label, type(res).__name__))
        return None
    return {"covers_nm": bool(res.get("covers_nm")), "events": _dict_list(res.get("events")),
            "reports": _dict_list(res.get("reports"))}


def _roads_short(view: Optional[Dict[str, Any]], enabled: bool,
                 applicable: bool = True) -> str:
    if not enabled:
        return "off" if applicable else "not applicable (no map in NM)"
    if view is None:
        return "unavailable"
    if not view["covers_nm"]:
        return "outside NM"
    kinds = [e.get("kind") for e in view["events"]]
    return "%d closed / %d water / %d report(s)" % (
        kinds.count("closure"), kinds.count("water"), len(view["reports"]))


# ---- one site --------------------------------------------------------------------------------
def _undrawn_notes(at: List[dict]) -> List[str]:
    """Alerts listed AT the site whose outline could not be fetched cannot be shaded; say so
    on the map itself (the page legend, built from ``alerts_drawn_ids``, lists every AT alert
    that did not land on a map under "Listed, not shaded")."""
    events: List[str] = []
    for a in at:
        if a.get("geometry") is None:
            ev = str(a.get("event") or "alert")
            if ev not in events:
                events.append(ev)
    return ["not shaded (no outline available): %s" % ", ".join(events)] if events else []


MRMS_OUTSIDE_NOTE = RADAR_NOT_APPLICABLE


def _radar_coverage_notes(frame: Any, frame_res: Dict[str, Any]) -> List[str]:
    """A map wholly outside the MRMS grid (Alaska, Hawaii, Puerto Rico, ...) can show no
    echoes; say so, so that an empty radar layer is not read as "no rain". Only while a
    radar frame is loaded: otherwise the caption already says why there is no radar."""
    if frame is None or not frame_res.get("ok"):
        return []
    try:
        outside = not geo.bbox_intersects(tuple(frame.bbox), geo.MRMS_BBOX)
    except Exception:           # noqa: BLE001 - a note is not worth a failed site
        return []
    return [MRMS_OUTSIDE_NOTE] if outside else []


def _render_map(cfg, cache, run: dict, site: dict, theme: str, tile_url: str,
                frame_res: dict, drawable: List[dict], out_path: str,
                notes: Sequence[str], road_view: Optional[Dict[str, Any]] = None
                ) -> Tuple[Dict[str, Any], Optional[str]]:
    """One theme's map for one site: (render result, basemap note). Never raises.

    ``road_view`` (the site's road items, None when roads are off or unknown) is passed as
    ``roads=`` only when there is one, so a run without roads calls the renderer exactly as
    before. If the render with roads raises, the map is drawn once more without them: the
    road layer is an extra and must not cost the site its radar and alert map."""
    what = "%s %s map" % (site["slug"], theme)
    try:
        sm = radar.SiteMap(cfg, cache, site, theme, tile_url)
        if road_view is None:
            res = sm.render(frame_res, drawable, out_path, notes=notes)
        else:
            try:
                res = sm.render(frame_res, drawable, out_path, notes=notes, roads=road_view)
            except Exception as e:  # noqa: BLE001 — retried below without the road layer
                log.exception("%s failed with road closures (%s); drawing it without them",
                              what, e)
                res = sm.render(frame_res, drawable, out_path, notes=notes)
                if isinstance(res, dict):
                    res = dict(res, roads_drawn_ids=[])
                    if res.get("ok"):
                        run["errors"].append("%s drawn without road closures: %s"
                                             % (what, _exc_text(e)))
        note = sm.basemap_note
    except Exception as e:      # noqa: BLE001 — a renderer bug must not take the page down
        log.exception("%s failed: %s", what, e)
        run["errors"].append("%s failed: %s" % (what, _exc_text(e)))
        return ({"ok": False, "error": _exc_text(e), "path": out_path, "drawn_ids": [],
                 "radar_drawn": False}, None)
    if not isinstance(res, dict):
        res = {"ok": False, "error": "renderer returned no result", "path": out_path,
               "drawn_ids": [], "radar_drawn": False}
    if not res.get("ok"):
        run["errors"].append("%s not written: %s" % (what, res.get("error") or "unknown error"))
    elif note:
        run["errors"].append("%s: %s" % (what, note))
    if res.get("ok") and road_view is not None:
        # the renderer catches its own road-layer errors and still writes the map; the
        # problem only shows in its error text ("...; road layer failed: ...") — list it
        for part in str(res.get("error") or "").split("; "):
            if part.startswith("road layer failed"):
                run["errors"].append("%s: %s" % (what, part))
    return res, note


def _run_site(cfg, cache, run: dict, site: dict, feed: dict, frame_res: dict,
              road_src: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Everything for one site, each source guarded on its own so that, say, a dead
    observation station never costs the site its forecast or its maps. ``road_src`` is
    ``_load_roads``'s result (None: roads off, and the entry gets no ``roads`` key)."""
    slug = site["slug"]
    label = "%s " % slug
    meta = _source(run, label + "metadata", _meta_fallback, nws.site_metadata, cfg, cache, site)
    tz = _site_tz(site, meta)
    if not meta.get("ok"):
        meta["tz"] = tz         # the page formats this site's times with meta["tz"]
    gap = _alert_area_gap(cfg, site, meta)
    if gap:
        log.warning("%s", gap)
        run["errors"].append(gap)
    obs = _source(run, label + "observation", _obs_fallback, nws.observation, cfg, cache, site, meta)
    forecast = _source(run, label + "forecast", _forecast_fallback, nws.forecast, cfg, cache, site, meta)
    hourly = _source(run, label + "hourly forecast", _hourly_fallback, nws.hourly, cfg, cache, site, meta)

    sun_info = None
    if cfg.show_sun:
        sun_info = _step(run, label + "sun times", sun.sun_summary, site["lat"], site["lon"], tz)
        if not isinstance(sun_info, dict):
            sun_info = None

    # The map frame decides which alerts are candidates for "NEAR"; the renderer's own
    # frame is identical (same inputs), but building it here keeps the classification
    # independent of whether Pillow is around.
    frame = _step(run, label + "map frame", geo.MapFrame, site["lat"], site["lon"],
                  cfg.map_km, cfg.map_px, cfg.tile_zoom)
    split = _step(run, label + "alert classification", alerts.site_alerts,
                  feed.get("alerts") or [], site, meta, frame)
    split = split if isinstance(split, dict) else {}
    at = [a for a in (split.get("at") or []) if isinstance(a, dict)]
    candidates = [a for a in (split.get("candidates") or []) if isinstance(a, dict)]
    road_view = _site_roads(run, label, road_src, frame) if road_src is not None else None

    # Both AT and candidate alerts go to the renderer; what actually lands on pixels of a map
    # that was written (``drawn_ids`` of the renders that succeeded) is what the page may call
    # NEAR and what its legend lists as shaded, so shading and listing agree.
    notes = _undrawn_notes(at) + _radar_coverage_notes(frame, frame_res)
    map_site = dict(site, tz=tz)            # the renderer reads site["tz"] for its caption
    maps: Dict[str, Dict[str, Any]] = {}
    map_notes = list(notes)
    drawn: set = set()
    roads_drawn: set = set()
    rendered_any = False
    for theme, tile_url in cfg.themes:
        basename = cfg.map_basename(slug, theme)
        res, note = _render_map(cfg, cache, run, map_site, theme, tile_url, frame_res,
                                at + candidates, os.path.join(cfg.out_dir, basename), notes,
                                road_view)
        maps[theme] = {"basename": basename, "ok": bool(res.get("ok")), "error": res.get("error")}
        if res.get("ok"):
            rendered_any = True
            drawn.update(i for i in (res.get("drawn_ids") or []) if i is not None)
            road_ids = res.get("roads_drawn_ids")
            if road_view is not None and isinstance(road_ids, (list, tuple, set)):
                roads_drawn.update(i for i in road_ids if i is not None)
        if note:
            map_notes.append("%s theme: %s" % (theme, note))
    if rendered_any:
        near = [a for a in candidates if a.get("id") in drawn]
    else:
        near = list(candidates)             # no map to consult: list every candidate

    log.info("%s: obs %s, forecast %s, hourly %s, alerts AT %d / NEAR %d, roads %s, maps %s",
             slug, _short(obs), _short(forecast), _short(hourly), len(at), len(near),
             _roads_short(road_view, road_src is not None,
                          not run.get("roads_not_applicable")),
             "/".join("%s %s" % (t, "ok" if m["ok"] else "FAILED") for t, m in maps.items()))
    entry = {"site": site, "meta": meta, "obs": obs, "forecast": forecast, "hourly": hourly,
             "sun": sun_info, "alerts_at": at, "alerts_near": near,
             "alerts_drawn_ids": sorted(drawn, key=str), "maps": maps,
             "map_notes": map_notes}
    if road_view is not None:
        # "drawn_ids": road items on at least one theme map that was written (the page marks
        # those "on map"); [] when no map was written.
        entry["roads"] = dict(road_view, drawn_ids=sorted(roads_drawn, key=str))
    return entry


def _site_placeholder(cfg, site: dict, error: str) -> Dict[str, Any]:
    """A site entry for the page when the per-site code itself blew up."""
    tz = site.get("tz") if isinstance(site, dict) else None
    return {"site": site, "meta": _meta_fallback(error, tz), "obs": _obs_fallback(error),
            "forecast": _forecast_fallback(error), "hourly": _hourly_fallback(error),
            "sun": None, "alerts_at": [], "alerts_near": [], "alerts_drawn_ids": [],
            "maps": {theme: {"basename": cfg.map_basename(site["slug"], theme), "ok": False,
                             "error": error} for theme, _ in cfg.themes},
            "map_notes": []}


def _short(res: Dict[str, Any]) -> str:
    if res.get("not_provided"):
        return "not provided"
    if not res.get("ok"):
        return "unavailable"
    return "stale" if res.get("stale") else "ok"


def _site_ok(entry: Dict[str, Any]) -> bool:
    """"ok" for the summary line: every product present (fresh or stale, or not provided by
    NWS for the location) and both maps written."""
    products = (entry.get("meta"), entry.get("obs"), entry.get("forecast"), entry.get("hourly"))
    maps = entry.get("maps") or {}
    return (all(isinstance(p, dict) and (p.get("ok") or p.get("not_provided")) for p in products)
            and bool(maps) and all(m.get("ok") for m in maps.values()))


# ---- the run ----------------------------------------------------------------------------------
def run_once(cfg, skip_radar: bool = False) -> Dict[str, Any]:
    """One complete run: fetch, render the maps, write the page and status file. Returns
    the run dict (shape in DESIGN.md).

    Raises ``http.AlreadyRunning`` when another run holds the lock and ``OSError`` when the
    output directory cannot be written; every other problem degrades into the run dict."""
    with http.run_lock(cfg.lock_file):
        return _run_locked(cfg, skip_radar)


def _run_locked(cfg, skip_radar: bool) -> Dict[str, Any]:
    t0 = time.monotonic()
    os.makedirs(cfg.out_dir, exist_ok=True)         # unwritable -> OSError -> exit 1
    cache = http.Cache(cfg.cache_dir)
    generated = util.utcnow()
    run: Dict[str, Any] = {"generated": generated, "generated_ts": generated.timestamp(),
                           "night_default": False, "alerts": None, "radar": None,
                           "network": None, "errors": [], "sites": []}
    # 1. a fresh network budget and circuit breaker for this run (before any fetch)
    _step(run, "network budget", http.begin_run, cfg)
    log.info("run started: %d site(s) from %s, out %s, network budget %s s", len(cfg.sites),
             getattr(cfg, "sites_source", "the configuration"), cfg.out_dir, cfg.run_budget_s)
    _log_alert_areas(cfg)

    # 2. alerts, once for every site
    feed = _source(run, "alerts feed", _alerts_fallback, alerts.fetch_active, cfg, cache)
    run["alerts"] = feed
    if feed.get("alerts"):
        _step(run, "alert zone outlines", alerts.resolve_geometries, cfg, cache, feed["alerts"])

    # 3. radar frame, once for every map
    frame_res = _load_radar(cfg, cache, run, skip_radar)
    run["radar"] = {k: v for k, v in frame_res.items() if k != "image"}

    # 4. New Mexico road closures and storm reports, once for every map (sets run["roads"])
    road_src = _load_roads(cfg, cache, run)

    # 5. sites
    for site in cfg.sites:
        try:
            entry = _run_site(cfg, cache, run, site, feed, frame_res, road_src)
        except Exception as e:  # noqa: BLE001 — one site must never take the others down
            log.exception("site %s failed: %s", site.get("slug"), e)
            run["errors"].append("site %s failed: %s" % (site.get("slug"), _exc_text(e)))
            entry = _site_placeholder(cfg, site, _exc_text(e))
        run["sites"].append(entry)

    # The network budget / circuit-breaker state after every fetch, so status.json can name
    # the cause (budget spent, host unreachable) behind the stale or unavailable sources.
    net = _step(run, "network status", http.run_status)
    run["network"] = net if isinstance(net, dict) else None

    # 6. the page opens in its night palette when the sun is down at the first site
    first_sun = run["sites"][0].get("sun") if run["sites"] else None
    run["night_default"] = bool(isinstance(first_sun, dict) and first_sun.get("is_night"))

    # 7. output (OSError propagates: an unwritable output directory is a deployment error)
    page.write_page(cfg, run)
    page.write_status_json(cfg, run)

    ts = run["radar"].get("ts")
    if run["radar"].get("ok") and ts is not None:
        radar_txt = ts.strftime("%H:%MZ") + (" stale" if run["radar"].get("stale") else "")
    elif run["radar"].get("not_applicable"):
        radar_txt = "not applicable (no map in the MRMS grid)"
    else:
        radar_txt = "none"
    down = (run["network"] or {}).get("down_hosts") or []
    if road_src is None:
        roads_txt = "not applicable (no map in NM)" if run.get("roads_not_applicable") else "off"
    else:
        kinds = [e.get("kind") for e in road_src["nmdot"]["events"]]
        roads_txt = "NM %d closed / %d water / %d report(s)%s" % (
            kinds.count("closure"), kinds.count("water"), len(road_src["lsr"]["reports"]),
            "" if road_src["nmdot"].get("ok") else " (NMDOT unavailable)")
    log.info("done: sites ok %d/%d, alerts %d%s, radar %s, roads %s, errors %d, %.1f s "
             "(budget %s s)%s",
             sum(1 for s in run["sites"] if _site_ok(s)), len(run["sites"]),
             len(feed.get("alerts") or []), "" if feed.get("ok") else " (feed unavailable)",
             radar_txt, roads_txt, len(run["errors"]), time.monotonic() - t0, cfg.run_budget_s,
             ", unreachable: %s" % ", ".join(down) if down else "")
    return run


# ---- CLI -----------------------------------------------------------------------------------------
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="make_weather_page.py",
        description="Generate the local-weather-awareness page (one run). Every option can "
                    "also be set with a WEATHER_* environment variable; see weather.env.example.")
    p.add_argument("--out", metavar="DIR", help="output directory (default: $WEATHER_OUT_DIR or ./out)")
    p.add_argument("--cache", metavar="DIR",
                   help="cache directory (default: $WEATHER_CACHE_DIR or "
                        "~/.cache/local-weather-awareness)")
    p.add_argument("--verbose", "-v", action="store_true", help="DEBUG logging to stderr")
    p.add_argument("--skip-radar", action="store_true",
                   help="do not fetch the MRMS frame; maps show the basemap and alerts only")
    p.add_argument("--sites", metavar="SPEC",
                   help="'slug|Name|lat|lon[|tz];slug|Name|lat|lon[|tz]', tz an IANA name "
                        "such as America/Denver (overrides $WEATHER_SITES and "
                        "$WEATHER_SITES_FILE)")
    p.add_argument("--once", action="store_true",
                   help="accepted for compatibility: every invocation is exactly one run")
    return p.parse_args(argv)


def setup_logging(verbose: bool) -> None:
    """INFO (or DEBUG) to stderr in the DESIGN.md format. ``basicConfig`` is a no-op when a
    handler already exists (pytest, an embedding program), so the level is set explicitly
    on our own logger namespace as well."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT, stream=sys.stderr)
    logging.getLogger("weather").setLevel(level)
    # Pillow logs every PNG chunk it parses at DEBUG (hundreds of lines per run): keep it quiet.
    logging.getLogger("PIL").setLevel(logging.INFO)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    try:
        sites = None
        if args.sites is not None:
            # given but empty (or only ';') is an error, never a fall-back to other sites
            sites = parse_sites(args.sites, "--sites")
            if not sites:
                raise ValueError("--sites lists no sites: %r" % args.sites)
        cfg = Config.from_env(
            out_dir=os.path.abspath(os.path.expanduser(args.out)) if args.out else None,
            cache_dir=os.path.abspath(os.path.expanduser(args.cache)) if args.cache else None,
            sites=sites,
            verbose=True if args.verbose else None)
    except (ValueError, TypeError) as e:
        log.error("configuration error: %s", e)
        return 2
    if cfg.verbose and not args.verbose:            # WEATHER_VERBOSE=1
        setup_logging(True)
    try:
        run_once(cfg, skip_radar=args.skip_radar)
    except http.AlreadyRunning as e:
        log.info("already running (%s); nothing to do", e)
        return 0
    except OSError as e:
        # the output directory, or the cache directory holding the run lock
        log.error("cannot write the output or cache directory: %s", e)
        return 1
    except Exception as e:      # noqa: BLE001 — a bug: report it, but do not hide it
        log.exception("run failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
