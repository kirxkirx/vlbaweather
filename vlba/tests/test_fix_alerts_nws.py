"""The alert and NWS fixes of the audit: one malformed alert never raises for a site and never
reads as "no warning"; the liveness filter and the AT rule of the alerts; /points replies
without zones or grid; product, station and zone addresses from upstream; zone outlines;
observations dated in the future or holding infinities; unknown wind units; twilight on
grazing days; the ISO 8601 spellings parse_iso reads."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import pytest

import make_vlba_page as mvp                     # noqa: E402  (conftest put vlbaweather on sys.path)
from vlba import decision as d
from vlba import glm
from vlba.tests.conftest import PT
from vlba.tests.test_main import (FUTURE, _glm_result, _mrms_result, add_routes, alert_feature,
                                  observation_doc, points_doc, stations_doc)
from weather import alerts, geo, nws, sun, util

UTC = timezone.utc
NOW = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)
PT_ZONES = ("NMZ226", "NMC003", "NMZ106")
PT_META = {"ok": True, "forecast_zone": "NMZ226", "county_zone": "NMC003", "fire_zone": "NMZ106"}
# a box around Pie Town, and one far east of it
BOX_PT = [[-108.3, 34.2], [-108.0, 34.2], [-108.0, 34.4], [-108.3, 34.4], [-108.3, 34.2]]
BOX_FAR = [[-100.3, 30.2], [-100.0, 30.2], [-100.0, 30.4], [-100.3, 30.4], [-100.3, 30.2]]
ZONE_URL = "https://api.weather.gov/zones/forecast/"


def _feature(fid="a1", event="High Wind Warning", geometry=None, zones=(), **props):
    p = {"id": fid, "event": event, "affectedZones": list(zones), "status": "Actual",
         "messageType": "Alert", "severity": "Severe", "ends": FUTURE, "expires": FUTURE}
    p.update(props)
    return {"id": "https://api.weather.gov/alerts/" + str(fid), "type": "Feature", "geometry": geometry,
            "properties": p}


def _poly(ring):
    return {"type": "Polygon", "coordinates": [ring]}


def _split(features, meta=PT_META):
    recs = [alerts.normalize(f) for f in features]
    return alerts.site_alerts(recs, PT, meta, None)


def _events(recs):
    return [a["event"] for a in recs]


# ---- #106 #116 #78: one malformed alert never raises for a site ----------------------------
def test_geo_boxes_of_unreadable_coordinates_are_no_box():
    strings = [["-100.1", "30.2"], ["-100.0", "30.2"], ["-100.0", "30.4"], ["-100.1", "30.2"]]
    assert geo.geometry_bbox(_poly(strings)) is None
    assert geo.geometry_bbox({"type": "Polygon", "coordinates": [[BOX_FAR]]}) is None      # nested too deep
    assert geo.geometry_bbox(_poly([[float("nan"), 30.0], [1.0, 2.0], [3.0, 4.0]])) is None
    assert not geo.bbox_intersects(("-100.0", "30.2", "-100.1", "30.4"), (-101.0, 29.0, -99.0, 31.0))
    assert not geo.bbox_intersects((float("nan"), 0.0, 1.0, 1.0), (-1.0, -1.0, 2.0, 2.0))
    assert geo.bbox_intersects((-100.3, 30.2, -100.0, 30.4), (-101.0, 29.0, -99.0, 31.0))
    assert not geo.geometry_ok(_poly([[1.0, 2.0], [3.0, 4.0]]))                    # 2-point ring
    assert geo.geometry_ok(_poly(BOX_PT))


def test_a_far_alert_with_string_coordinates_leaves_the_warning_at_the_site():
    """The audited code raised in bbox_intersects, outside the per-alert guard, so the whole
    classification failed and every site read "no warning"."""
    hww = _feature("hww", "High Wind Warning", zones=[ZONE_URL + "NMZ226"])
    bad = {"geometry_source": "polygon", "event": "Flood Advisory", "id": "bad",
           "geometry": _poly([["-100.1", "30.2"], ["-100.0", "30.2"], ["-100.0", "30.4"]]),
           "bbox": ("-100.1", "30.2", "-100.0", "30.4")}
    frame = geo.MapFrame(PT["lat"], PT["lon"], 160.0, 240, 8)
    split = alerts.site_alerts([alerts.normalize(hww), bad], PT, PT_META, frame)
    assert _events(split["at"]) == ["High Wind Warning"]
    assert split["skipped"] == ["Flood Advisory"]


def test_unreadable_polygons_fall_back_to_the_zone_list():
    """#115 #123: string, null, NaN, wrongly nested coordinates and a 2-point ring make the
    geometry None (the alert keeps its zones), never an exception or a dropped alert."""
    shapes = [_poly([["-108.3", "34.2"], ["-108.0", "34.2"], ["-108.0", "34.4"]]),
              _poly([[None, None], [None, None], [None, None]]),
              {"type": "Polygon", "coordinates": None},
              _poly([[float("nan"), 34.2], [-108.0, 34.2], [-108.0, 34.4], [float("nan"), 34.2]]),
              {"type": "Polygon", "coordinates": [[BOX_PT]]},
              {"type": "Polygon", "coordinates": [[1, 2]]},
              _poly([[-108.3, 34.2], [-108.0, 34.4]])]
    for g in shapes:
        rec = alerts.normalize(_feature("w", "Blizzard Warning", geometry=g, zones=[ZONE_URL + "NMZ226"]))
        assert rec["geometry"] is None and rec["geometry_source"] is None and rec["bbox"] is None, g
        assert rec["affected_zone_ids"] == ["NMZ226"]
        split = alerts.site_alerts([rec], PT, PT_META, None)
        assert _events(split["at"]) == ["Blizzard Warning"] and split["skipped"] == [], g


def test_an_alert_id_is_a_string_or_none():
    """#3: a list or object id raised further on (a set member, a dict key)."""
    assert alerts.normalize(_feature(["x"]))["id"] == "https://api.weather.gov/alerts/['x']"
    f = _feature("x")
    f["properties"]["id"] = {"a": 1}
    f["id"] = ["y"]
    assert alerts.normalize(f)["id"] is None
    assert alerts.normalize(_feature("urn:oid:2.49.0.1"))["id"] == "urn:oid:2.49.0.1"


def test_an_alert_that_cannot_be_placed_is_skipped_not_absent():
    nowhere = _feature("n", "Winter Storm Warning")                                 # no shape, no zones
    zones_only = _feature("z", "High Wind Warning", zones=[ZONE_URL + "NMZ999"])
    split = _split([nowhere, zones_only])
    assert split["at"] == [] and split["skipped"] == ["Winter Storm Warning"]
    # without the site's own zones a zone list places nothing either
    split = _split([zones_only], meta={"ok": False})
    assert split["at"] == [] and split["skipped"] == ["High Wind Warning"]
    # a zone id that is not one is not a zone
    rec = alerts.normalize(_feature("q", zones=[ZONE_URL + "NMZ226?x=1", "https://api.weather.gov/zones/x/",
                                                ZONE_URL + "NMZ226", ZONE_URL + "NMZ226"]))
    assert rec["affected_zone_ids"] == ["NMZ226"]


def test_a_feature_normalize_cannot_read_is_counted_and_kept(cfg, cache, fake_http, monkeypatch):
    real = alerts.normalize

    def flaky(feature):
        if (feature.get("properties") or {}).get("id") == "bad":
            raise ValueError("odd feature")
        return real(feature)
    monkeypatch.setattr(alerts, "normalize", flaky)
    fake_http.add("/alerts/active", {"features": [
        _feature("bad", "Blizzard Warning"), _feature("ok", "Wind Advisory", zones=[ZONE_URL + "NMZ226"])]})
    feed = alerts.fetch_active(cfg, cache)
    assert feed["ok"] and feed["count_unread"] == 1
    assert "1 of 2 alert(s) in the feed could not be read" in feed["error"]
    stub = next(a for a in feed["alerts"] if a.get("unread"))
    assert stub["event"] == "Blizzard Warning" and stub["id"] is None and stub["geometry"] is None
    split = alerts.site_alerts(feed["alerts"], PT, PT_META, None)
    assert split["skipped"] == ["Blizzard Warning"] and _events(split["at"]) == ["Wind Advisory"]


# ---- S18: the liveness filter and the AT rule ------------------------------------------------
def test_the_liveness_filter(cfg, cache, fake_http):
    past = "2020-01-01T00:00:00+00:00"
    fake_http.add("/alerts/active", {"features": [
        _feature("update", "High Wind Warning", messageType="Update"),
        _feature("cancel", "Wind Advisory", messageType="Cancel"),
        _feature("test", "Tornado Warning", status="Test"),
        _feature("noend", "Blizzard Warning", ends=None, expires=None),
        _feature("ended", "Winter Storm Warning", ends=past, expires=past),
        # expires passed but ends tomorrow: the product is in effect until it ends
        _feature("longer", "Ice Storm Warning", ends=FUTURE, expires=past),
        # no ends, expires passed: over
        _feature("expired", "Freeze Warning", ends=None, expires=past)]})
    feed = alerts.fetch_active(cfg, cache)
    assert sorted(_events(feed["alerts"])) == ["Blizzard Warning", "High Wind Warning", "Ice Storm Warning"]


@pytest.mark.parametrize("zone", PT_ZONES)
def test_a_zone_alert_without_outline_is_at_the_site_through_each_zone_kind(zone):
    split = _split([_feature("z", "High Wind Warning", zones=["https://api.weather.gov/zones/x/" + zone])])
    assert _events(split["at"]) == ["High Wind Warning"] and split["skipped"] == []


def test_a_polygon_that_misses_the_site_is_not_at_it_though_its_county_is_listed():
    split = _split([_feature("p", "Severe Thunderstorm Warning", geometry=_poly(BOX_FAR),
                             zones=["https://api.weather.gov/zones/county/NMC003"])])
    assert split["at"] == [] and split["skipped"] == []
    split = _split([_feature("p", "Severe Thunderstorm Warning", geometry=_poly(BOX_PT))])
    assert _events(split["at"]) == ["Severe Thunderstorm Warning"]


def test_a_zone_alert_is_at_the_site_inside_its_outline_alone():
    rec = alerts.normalize(_feature("z", "High Wind Warning", zones=[ZONE_URL + "NMZ999"]))
    rec.update(geometry=_poly(BOX_PT), geometry_source="zones", bbox=geo.geometry_bbox(_poly(BOX_PT)))
    split = alerts.site_alerts([rec], PT, PT_META, None)
    assert _events(split["at"]) == ["High Wind Warning"]
    rec.update(geometry=_poly(BOX_FAR), bbox=geo.geometry_bbox(_poly(BOX_FAR)))
    assert alerts.site_alerts([rec], PT, PT_META, None)["at"] == []


@pytest.fixture
def quiet_sky(monkeypatch):
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result([]))
    monkeypatch.setattr(glm, "fetch", lambda cfg, now=None, since=None, retried=None, seen=None: _glm_result([]))
    monkeypatch.setattr(glm, "deps_available", lambda: True)


def test_run_zone_based_veto_without_outline(cfg, fake_http, quiet_sky):
    """S18: a High Wind Warning that lists Pie Town's forecast zone and has no polygon; its
    outline cannot be fetched. The site is BAD on the zone match alone."""
    fake_http.fail("/zones/")
    feed = {"type": "FeatureCollection", "features": [
        alert_feature("hww", "High Wind Warning", zones=[ZONE_URL + "NMZ226"])]}
    add_routes(fake_http, cfg, alerts=feed)
    run = mvp.run_once(cfg)
    pt = run["sites"][0]
    states = {c["key"]: c["state"] for c in pt["verdict"]["checks"]}
    assert pt["verdict"]["status"] == "BAD" and states["warning"] == "bad"
    assert run["sites"][1]["verdict"]["status"] == "GOOD"


def test_run_far_malformed_alert_leaves_the_veto(cfg, fake_http, quiet_sky):
    """#106 #116 #78 through a whole run: a Flood Advisory far away with string coordinates
    must not switch the High Wind Warning at Pie Town off."""
    fake_http.fail("/zones/")
    bad = alert_feature("fa", "Flood Advisory",
                        geometry=[["-100.1", "30.2"], ["-100.0", "30.2"], ["-100.0", "30.4"], ["-100.1", "30.2"]])
    nested = alert_feature("fb", "Flood Advisory", geometry=[BOX_FAR])
    feed = {"type": "FeatureCollection", "features": [
        alert_feature("hww", "High Wind Warning", zones=[ZONE_URL + "NMZ226"]), bad, nested]}
    add_routes(fake_http, cfg, alerts=feed)
    run = mvp.run_once(cfg)
    pt = run["sites"][0]
    assert pt["verdict"]["status"] == "BAD"
    assert not any("classification failed" in e for e in run["errors"])


# ---- check_warning: skipped alerts, metadata without zones -----------------------------------
def _entry(**over):
    e = {"slug": "pt", "tz": "America/Denver", "alerts_feed": {"ok": True, "stale": False, "error": None},
         "alerts_at": [], "meta": dict(PT_META)}
    e.update(over)
    return e


def test_check_warning_skipped_veto_product_is_unknown(cfg):
    c, _r, _s = d.check_warning(cfg, _entry(alerts_skipped=["High Wind Warning"]), NOW)
    assert c["state"] == d.UNKNOWN and "an alert (High Wind Warning) could not be placed" in c["detail"]
    c, _r, _s = d.check_warning(cfg, _entry(alerts_skipped=[None]), NOW)
    assert c["state"] == d.UNKNOWN and "event name unreadable" in c["detail"]
    c, _r, _s = d.check_warning(cfg, _entry(alerts_skipped=["Small Craft Advisory"]), NOW)
    assert c["state"] == d.OK
    c, _r, _s = d.check_warning(cfg, _entry(alerts_skipped="High Wind Warning"), NOW)      # not a list
    assert c["state"] == d.OK


def test_check_warning_metadata_without_zones_is_unknown(cfg):
    """#8 #110"""
    c, _r, _s = d.check_warning(cfg, _entry(meta={"ok": True, "forecast_zone": None, "county_zone": None}), NOW)
    assert c["state"] == d.UNKNOWN and "zones are unknown" in c["detail"]
    c, _r, _s = d.check_warning(cfg, _entry(meta={"ok": True, "county_zone": "NMC003"}), NOW)
    assert c["state"] == d.OK
    c, _r, _s = d.check_warning(cfg, _entry(meta={"ok": False, "not_provided": True}), NOW)
    assert c["state"] == d.OK


# ---- /points: zones required, no grid heals after an hour, upstream addresses ---------------
def _points(**over):
    doc = points_doc("ABQ", 50, 60, "America/Denver", PT_ZONES, "KABX")
    doc["properties"].update(over)
    return doc


def test_points_without_zones_is_not_valid(cfg, cache, fake_http):
    assert nws._valid_points(_points())
    assert not nws._valid_points(_points(forecastZone=None, county=None, fireWeatherZone=None))
    assert nws._valid_points(_points(forecastZone=None))                                # the county is enough
    assert not nws._valid_points(_points(forecastZone="https://api.weather.gov/zones/forecast/NMZ 226",
                                         county=None))
    gridless = _points(gridId=None, forecast=None)
    assert nws._valid_points(gridless)
    fake_http.add("/points/", _points(forecastZone=None, county=None, fireWeatherZone=None))
    meta = nws.site_metadata(cfg, cache, PT)
    assert not meta["ok"] and meta["forecast_zone"] is None
    assert cache.get_any("points/%s" % nws._loc(PT)) == (None, None)


def test_a_cached_copy_without_zones_is_not_used(cfg, cache, fake_http):
    key = "points/%s" % nws._loc(PT)
    cache.put(key, _points(forecastZone=None, county=None, fireWeatherZone=None))
    fake_http.fail("/points/")
    meta = nws.site_metadata(cfg, cache, PT)
    assert not meta["ok"] and "names no zone" in meta["error"]
    assert any("/points/" in u for u in fake_http.calls)                       # asked again at once


def test_points_without_grid_is_asked_again_after_an_hour(cfg, cache, fake_http):
    """#120"""
    key = "points/%s" % nws._loc(PT)
    cache.put(key, _points(gridId=None, forecast=None), ts=time.time() - 2 * 3600)
    fake_http.add("/points/", _points())
    meta = nws.site_metadata(cfg, cache, PT)
    assert meta["ok"] and not meta["no_grid"] and meta["grid_id"] == "ABQ"
    assert nws._points_ttl(cfg, cache, key) == cfg.points_ttl
    cache.put(key, _points(gridId=None, forecast=None))
    assert nws._points_ttl(cfg, cache, key) == min(cfg.points_ttl, nws.NO_GRID_TTL)
    assert nws.site_metadata(cfg, cache, PT)["no_grid"]                         # still means "no grid"


@pytest.mark.parametrize("bad", ["file:///etc/passwd", "http://api.weather.gov/gridpoints/ABQ/50,60/forecast",
                                 "https://api.weather.gov.example.org/gridpoints/ABQ/50,60/forecast",
                                 "https://api.weather.gov/gridpoints/ABQ/50,60/forecast?x=1",
                                 "https://user:pw@api.weather.gov/gridpoints/ABQ/50,60/forecast",
                                 "https://api.weather.gov/../x", "https://169.254.169.254/latest", ["x"]])
def test_product_addresses_must_be_https_on_the_nws_base(cfg, cache, fake_http, bad):
    """#2 #14: the product is unavailable, nothing is fetched, the error does not repeat it."""
    fake_http.add("/points/", _points(forecast=bad, forecastHourly=bad, observationStations=bad))
    meta = nws.site_metadata(cfg, cache, PT)
    assert meta["ok"] and meta["forecast_url"] is None and meta["hourly_url"] is None
    assert meta["stations_url"] is None
    for res in (nws.forecast(cfg, cache, PT, meta), nws.hourly(cfg, cache, PT, meta),
                nws.observation(cfg, cache, PT, meta)):
        assert not res["ok"] and "no usable address" in res["error"]
        for part in ("file:", "http:", "example.org", "169.254", "?x=1", "pw@"):
            assert part not in res["error"]
    assert [u for u in fake_http.calls if "/points/" not in u] == []


def test_good_product_addresses_are_fetched(cfg, cache, fake_http):
    add_routes(fake_http, cfg)
    meta = nws.site_metadata(cfg, cache, PT)
    assert meta["forecast_url"].endswith("/gridpoints/ABQ/50,60/forecast")
    assert meta["zone_urls"] == [ZONE_URL + "NMZ226", "https://api.weather.gov/zones/county/NMC003",
                                 "https://api.weather.gov/zones/fire/NMZ106"]
    assert nws.forecast(cfg, cache, PT, meta)["ok"] and nws.observation(cfg, cache, PT, meta)["ok"]


def test_station_and_zone_ids_are_checked_before_a_url():
    """#11: an id with a blank or a newline would make http.client refuse the URL before
    sending, which marks the whole host down."""
    doc = {"observationStations": ["https://api.weather.gov/stations/KPT1\n",
                                   "https://api.weather.gov/stations/K PT", "https://api.weather.gov/stations/KPT2"]}
    assert nws._station_list(doc) == [("KPT2", "")]
    assert nws._zone_id("https://api.weather.gov/zones/forecast/NMZ226\n") is None
    assert nws._zone_id("https://api.weather.gov/zones/forecast/NMZ226") == "NMZ226"
    assert alerts.zone_ref("https://api.weather.gov/zones/forecast/WAZ 043") is None
    assert alerts.zone_ref("https://elsewhere.example/zones/forecast/NMZ207") is None
    assert alerts.zone_ref("https://api.weather.gov/zones/county/NMC003") == ("county", "NMC003")


# ---- #16: zone outlines ----------------------------------------------------------------------
def _zone_doc(zid, ring=BOX_PT, kind="forecast", geometry="poly"):
    g = _poly(ring) if geometry == "poly" else geometry
    url = "https://api.weather.gov/zones/%s/%s" % (kind, zid)
    return {"id": url, "type": "Feature", "geometry": g, "properties": {"@id": url, "id": zid, "type": "public"}}


def _zone_alert(*urls):
    return alerts.normalize(_feature("z", "High Wind Warning", zones=list(urls)))


def test_zone_outline_from_another_host_is_not_fetched(cfg, cache, fake_http):
    fake_http.add("elsewhere.example", _zone_doc("NMZ207", ring=[[-180, -85], [180, -85], [180, 85], [-180, -85]]))
    rec = _zone_alert("https://elsewhere.example/zones/forecast/NMZ207")
    alerts.resolve_geometries(cfg, cache, [rec])
    assert rec["geometry"] is None and not any("elsewhere" in u for u in fake_http.calls)


def test_zone_outline_naming_another_zone_or_too_large_is_not_used(cfg, cache, fake_http):
    fake_http.add("/zones/forecast/NMZ207", _zone_doc("NMZ999"))
    rec = _zone_alert(ZONE_URL + "NMZ207")
    alerts.resolve_geometries(cfg, cache, [rec])
    assert rec["geometry"] is None and cache.get_any("zone/NMZ207") == (None, None)
    world = [[-170.0, -60.0], [170.0, -60.0], [170.0, 70.0], [-170.0, -60.0]]
    fake_http.add("/zones/forecast/NMZ208", _zone_doc("NMZ208", ring=world))
    rec = _zone_alert(ZONE_URL + "NMZ208")
    alerts.resolve_geometries(cfg, cache, [rec])
    assert rec["geometry"] is None
    fake_http.add("/zones/forecast/NMZ226", _zone_doc("NMZ226"))
    rec = _zone_alert(ZONE_URL + "NMZ226")
    alerts.resolve_geometries(cfg, cache, [rec])
    assert rec["geometry_source"] == "zones" and geo.point_in_geometry(PT["lon"], PT["lat"], rec["geometry"])


def test_a_zone_without_outline_is_asked_again_after_a_day(cfg, cache, fake_http):
    fake_http.add("/zones/forecast/PZZ530", _zone_doc("PZZ530", geometry=None))
    alerts.resolve_geometries(cfg, cache, [_zone_alert(ZONE_URL + "PZZ530")])
    n = len(fake_http.calls)
    alerts.resolve_geometries(cfg, cache, [_zone_alert(ZONE_URL + "PZZ530")])
    assert len(fake_http.calls) == n                                            # cached ...
    data, _age = cache.get_any("zone/PZZ530")
    cache.put("zone/PZZ530", data, ts=time.time() - alerts.ZONE_NULL_TTL - 60)
    alerts.resolve_geometries(cfg, cache, [_zone_alert(ZONE_URL + "PZZ530")])
    assert len(fake_http.calls) == n + 1                                        # ... for a day, not a year


# ---- #113 #121 #156: observations dated in the future; S15: infinities ----------------------
def _obs_at(sid, ts, **over):
    doc = observation_doc(sid)
    doc["properties"]["timestamp"] = ts.isoformat()
    doc["properties"].update(over)
    return doc


def _two_stations(fake_http, first, second=None):
    fake_http.add("/points/", _points())
    fake_http.add("gridpoints/ABQ/50,60/stations",
                  stations_doc("KAA1", 34.32, -108.10, 2300.0, ("KAA2", 34.40, -108.20, 2300.0)))
    fake_http.add("/stations/KAA1/observations/latest", first)
    fake_http.add("/stations/KAA2/observations/latest", second or observation_doc("KAA2"))


def test_a_report_dated_in_the_future_is_not_used(cfg, cache, fake_http):
    _two_stations(fake_http, _obs_at("KAA1", util.utcnow() + timedelta(hours=2)))
    meta = nws.site_metadata(cfg, cache, PT)
    obs = nws.observation(cfg, cache, PT, meta)
    assert obs["ok"] and obs["station_id"] == "KAA2"
    assert nws._unusable({"timestamp": "x", "age_min": -120.0, "temp_c": 1.0}, 180) == "report dated in the future"
    assert nws._unusable({"timestamp": "x", "age_min": -5.0, "temp_c": 1.0}, 180) is None      # clock skew


def test_fmt_age_of_a_future_time():
    assert util.fmt_age(-30) == "just now" and util.fmt_age(30) == "just now"
    assert util.fmt_age(-3600) == "in the future"
    assert util.fmt_age(float("nan")) == "?" and util.fmt_age(float("inf")) == "?"
    assert util.fmt_age(7200) == "2 h"


def test_an_infinite_value_does_not_cost_the_site_its_observation(cfg, cache, fake_http):
    assert util.quantity({"value": float("inf")}) is None and util.quantity("1e999") is None
    assert util.deg_to_cardinal(float("inf")) == ""
    first = _obs_at("KAA1", util.utcnow() - timedelta(minutes=10), windDirection={"unitCode": "x", "value": 1e999})
    _two_stations(fake_http, first)
    meta = nws.site_metadata(cfg, cache, PT)
    obs = nws.observation(cfg, cache, PT, meta)
    assert obs["ok"] and obs["station_id"] == "KAA1" and obs["wind_dir"] == "" and obs["wind_dir_deg"] is None


def test_a_record_that_cannot_be_parsed_is_skipped(cfg, cache, fake_http, monkeypatch):
    _two_stations(fake_http, observation_doc("KAA1"))
    real = nws._parse_observation

    def parse(props):
        if props.get("stationId") == "KAA1":
            raise ValueError("odd record")
        return real(props)
    monkeypatch.setattr(nws, "_parse_observation", parse)
    meta = nws.site_metadata(cfg, cache, PT)
    obs = nws.observation(cfg, cache, PT, meta)
    assert obs["ok"] and obs["station_id"] == "KAA2"


# ---- #109: wind units ------------------------------------------------------------------------
def test_wind_speed_units():
    assert nws._speed_kmh({"unitCode": "wmoUnit:km_h-1", "value": 40}) == 40
    assert nws._speed_kmh({"value": 40}) == 40 and nws._speed_kmh({"unitCode": "", "value": 40}) == 40
    assert nws._speed_kmh(40) == 40
    assert nws._speed_kmh({"unitCode": "wmoUnit:m_s-1", "value": 10}) == pytest.approx(36.0)
    for kn in ("wmoUnit:kn", "unit:kt", "wmoUnit:kt", "nwsUnit:kt", "uc:[kn_i]"):
        assert nws._speed_kmh({"unitCode": kn, "value": 40}) == pytest.approx(74.08), kn
    assert nws._speed_kmh({"unitCode": "wmoUnit:furlong_fortnight-1", "value": 40}) is None


# ---- #159: twilight on grazing days ----------------------------------------------------------
@pytest.mark.parametrize("lat,lon,tz,day,event", [
    (64.84, -147.72, "America/Anchorage", date(2026, 5, 16), "civil_dawn"),
    (64.84, -147.72, "America/Anchorage", date(2026, 8, 17), "nautical_dusk"),
    (55.95, -3.19, "Europe/London", date(2026, 5, 4), "astro_dawn")])
def test_twilight_near_solar_midnight_on_grazing_days(lat, lon, tz, day, event):
    t = sun.day_events(lat, lon, day, tz)[event]
    assert t is not None
    alt = {"civil": sun.ALT_CIVIL, "nautical": sun.ALT_NAUTICAL, "astro": sun.ALT_ASTRO}[event.split("_")[0]]
    assert abs(sun.sun_altitude_deg(lat, lon, t) - alt) < 0.05


def test_twilight_of_ordinary_days_is_unchanged():
    ev = sun.day_events(PT["lat"], PT["lon"], date(2026, 6, 21), "America/Denver")
    for name, alt in (("sunrise", sun.ALT_SUNRISE), ("civil_dusk", sun.ALT_CIVIL), ("astro_dawn", sun.ALT_ASTRO)):
        assert abs(sun.sun_altitude_deg(PT["lat"], PT["lon"], ev[name]) - alt) < 0.02
    # midsummer at 70 N: no astronomical night, so no astronomical dawn
    assert sun.day_events(70.0, 20.0, date(2026, 6, 21), "Europe/Oslo")["astro_dawn"] is None


# ---- #160: parse_iso ----------------------------------------------------------------------
@pytest.mark.parametrize("text,expect", [
    # read before and now
    ("2026-11-12T10:53:00Z", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),
    ("2026-11-12T10:53:00+00:00", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),
    ("2026-11-12T03:53:00-07:00", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),
    ("2026-11-12T10:53:00.123Z", datetime(2026, 11, 12, 10, 53, 0, 123000, tzinfo=UTC)),
    ("2026-11-12T10:53:00", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),      # no offset: UTC
    ("2026-11-12", datetime(2026, 11, 12, tzinfo=UTC)),                         # a date: its midnight UTC
    # read now (refused on 3.9 before)
    ("2026-11-12T10:53:00.5Z", datetime(2026, 11, 12, 10, 53, 0, 500000, tzinfo=UTC)),
    ("2026-11-12T10:53:00.123456789Z", datetime(2026, 11, 12, 10, 53, 0, 123456, tzinfo=UTC)),
    ("2026-11-12T10:53:00+0000", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),
    ("2026-11-12T03:53:00-0700", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),
    ("2026-11-12T03:53:00-07", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),
    ("2026-11-12t10:53:00z", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),
    (" 2026-11-12T10:53:00Z\n", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),
    ("2026-11-12T10:53Z", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),
    # not times
    ("2026-11-12T25:00:00Z", None), ("garbage", None), ("", None), (None, None), (12345, None),
    ("2026-11-12T10:53:00+2500", None)])
def test_parse_iso(text, expect):
    got = util.parse_iso(text)
    assert got == expect
    assert got is None or got.tzinfo is not None


def test_parse_iso_logs_an_unreadable_text_once(caplog):
    util._iso_warned.discard("not a time at all")
    for _ in range(3):
        assert util.parse_iso("not a time at all") is None
    assert sum("not an ISO 8601 time" in r.getMessage() for r in caplog.records) == 1


# ---- review repairs ---------------------------------------------------------------------------
@pytest.mark.parametrize("geom", [
    {"type": "MultiPolygon", "coordinates": ["x", [BOX_FAR]]},
    {"type": "MultiPolygon", "coordinates": [[BOX_FAR], []]},
    {"type": "GeometryCollection", "geometries": [_poly(BOX_FAR), "x"]},
    {"type": "GeometryCollection", "geometries": [_poly(BOX_FAR), {"type": "Polygon", "coordinates": "abc"}]},
    {"type": "GeometryCollection", "geometries": {"a": _poly(BOX_FAR)}}])
def test_a_shape_with_an_unreadable_member_falls_back_to_the_zone_list(geom):
    """iter_polygons passes over a member it cannot read; geometry_ok must not, or the
    readable rest (here far from the site) decides alone for a warning that lists the site's
    zone, and the warning is "not here"."""
    assert not geo.geometry_ok(geom)
    rec = alerts.normalize(_feature("w", "Blizzard Warning", geometry=geom, zones=[ZONE_URL + "NMZ226"]))
    assert rec["geometry"] is None
    assert _events(_split([_feature("w", "Blizzard Warning", geometry=geom, zones=[ZONE_URL + "NMZ226"])])["at"]) \
        == ["Blizzard Warning"]
    # points and lines in a collection are no area, as before
    assert geo.geometry_ok({"type": "GeometryCollection", "geometries": [
        {"type": "Point", "coordinates": [1.0, 2.0]}, _poly(BOX_PT)]})


def test_a_number_too_large_for_a_float_is_no_value():
    """S15 by another route: an integer of 400 digits (JSON keeps it an int) made float()
    raise OverflowError out of util.quantity, and a conversion near the float limit gave
    infinity."""
    assert util.quantity({"value": 10 ** 400}) is None and util.quantity(-10 ** 400) is None
    assert util.quantity({"value": 1e308}, util.c_to_f) is None
    assert util.quantity({"value": 20.0}, util.c_to_f) == pytest.approx(68.0)


def test_parse_iso_reads_no_fraction_of_a_minute():
    """"10:53.5" is half a minute past 10:53 in ISO 8601, not 10:53:00.5: refused."""
    assert util.parse_iso("2026-11-12 10:53.5") is None
    assert util.parse_iso("2026-11-12T10:53,5Z") is None
    assert util.parse_iso("2026-11-12T10:53:00,25+0530") == datetime(2026, 11, 12, 5, 23, 0, 250000, tzinfo=UTC)


@pytest.mark.parametrize("text", [
    "2026-11-12T10:53.500Z",                # a fraction of a minute (3.9's fromisoformat: 10:53:00.5)
    "2026-11-12T10.5Z",                     # a fraction of an hour (likewise: 10:00:00.5)
    "20261112T105300Z", "2026-W46-4", "2026-316",      # basic format, week and ordinal dates (3.11+)
    "2026-11-12+00:00",                     # an offset on a date (3.9: its midnight, "+" taken as the "T")
    "2026-11-12x10:53:00Z",                 # another separator
    "2026-11-12T10:53:00+05:30:15",         # an offset with seconds
    "2026-11-12T10:53:00+05:75",            # an offset of 75 minutes
    "2026-11-12T24:00:00Z",                 # ISO 8601's end of the day: not read
    "２０２６-11-12"])      # full-width digits
def test_parse_iso_reads_the_same_on_every_python(text, monkeypatch):
    """What datetime.fromisoformat reads besides the forms parse_iso lists, on one Python
    version or all, is no time here on any: the CI runs 3.9 and a current Python, and a
    production host may run either."""
    monkeypatch.setattr(util, "_iso_warned", set())
    assert util.parse_iso(text) is None


def test_parse_iso_reads_a_time_to_the_hour():
    """Read by fromisoformat on every Python, and kept: ISO 8601's reduced precision."""
    assert util.parse_iso("2026-11-12T10") == datetime(2026, 11, 12, 10, tzinfo=UTC)
    assert util.parse_iso("2026-11-12T03-07") == datetime(2026, 11, 12, 10, tzinfo=UTC)
