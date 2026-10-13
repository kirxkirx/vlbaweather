"""Tests for vlba.gridpoint: the run-length expansion, the hourly window, the status keys
and the cache behaviour. The payload is a trimmed copy of the real gridpoint of Mauna Kea
(api.weather.gov/gridpoints/HFO/255,77, 2026-10-08) with the run-length times rebuilt around
a fixed "now" that the tests pass to ``fetch`` (so the current-hour row exists at any clock)."""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timedelta, timezone

import pytest

from vlba import gridpoint
from weather import nws, util

SITE = {"slug": "mk", "name": "Mauna Kea, HI", "lat": 19.8014, "lon": -155.4555,
        "tz": "Pacific/Honolulu"}
LOC = "19.8014,-155.4555"
URL = "https://api.weather.gov/gridpoints/HFO/255,77"
META = {"ok": True, "stale": False, "error": None, "age_s": 0.0, "grid_id": "HFO",
        "grid_x": 255, "grid_y": 77, "tz": "Pacific/Honolulu", "no_grid": False}
NO_META = {"ok": False, "stale": True, "error": "fake: no route", "age_s": None,
           "grid_id": None, "grid_x": None, "grid_y": None, "tz": "UTC", "no_grid": False}

NOW = datetime(2026, 10, 8, 14, 37, 12, tzinfo=timezone.utc)      # Thu 04:37 HST
HOUR0 = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)
UPDATED = "2026-10-08T14:12:12+00:00"                              # 25 min before NOW

POINTS = {"properties": {       # api.weather.gov/points/19.8014,-155.4555, trimmed
    "cwa": "HFO", "gridId": "HFO", "gridX": 255, "gridY": 77,
    "forecast": "https://api.weather.gov/gridpoints/HFO/255,77/forecast",
    "forecastHourly": "https://api.weather.gov/gridpoints/HFO/255,77/forecast/hourly",
    "forecastGridData": "https://api.weather.gov/gridpoints/HFO/255,77",
    "observationStations": "https://api.weather.gov/gridpoints/HFO/255,77/stations",
    "relativeLocation": {"properties": {"city": "Waimea", "state": "HI"}},
    "forecastZone": "https://api.weather.gov/zones/forecast/HIZ028",
    "county": "https://api.weather.gov/zones/county/HIC001",
    "fireWeatherZone": "https://api.weather.gov/zones/fire/HIZ028",
    "timeZone": "Pacific/Honolulu", "radarStation": "PHKM"}}


def _run(offset_h, duration, value):
    """One run-length entry starting ``offset_h`` hours after HOUR0."""
    start = HOUR0 + timedelta(hours=offset_h)
    return {"validTime": "%s/%s" % (start.isoformat(), duration), "value": value}


def _series(uom, runs):
    return {"uom": uom, "values": runs}


def _wx(coverage, weather, intensity):
    return {"coverage": coverage, "weather": weather, "intensity": intensity,
            "visibility": {"unitCode": "wmoUnit:km", "value": None}, "attributes": []}


GRIDPOINT = {"properties": {
    "@id": URL, "updateTime": UPDATED, "validTimes": "2026-10-08T08:00:00+00:00/P7DT17H",
    "elevation": {"unitCode": "wmoUnit:m", "value": 3574.08},
    "forecastOffice": "https://api.weather.gov/offices/HFO",
    "gridId": "HFO", "gridX": "255", "gridY": "77",
    "temperature": _series("wmoUnit:degC", [_run(-6, "PT7H", 11.666666666666666),
                                            _run(1, "PT2H", 10.555555555555555)]),
    "relativeHumidity": _series("wmoUnit:percent", [_run(0, "PT2H", 64)]),
    "skyCover": _series("wmoUnit:percent", [_run(-2, "PT3H", 37), _run(1, "PT2H", 80),
                                            _run(3, "PT1H", None), _run(4, "P1D", 10)]),
    "windSpeed": _series("wmoUnit:km_h-1", [_run(-1, "PT4H", 14.816)]),
    "windGust": _series("wmoUnit:km_h-1", [_run(0, "PT3H", 29.632)]),
    "probabilityOfPrecipitation": _series("wmoUnit:percent", [_run(0, "PT6H", 5)]),
    "quantitativePrecipitation": _series("wmoUnit:mm", [_run(0, "PT6H", 0.254)]),
    "snowfallAmount": _series("wmoUnit:mm", [_run(-6, "PT12H", 0)]),
    "iceAccumulation": _series("wmoUnit:mm", [_run(0, "PT6H", 0)]),
    "probabilityOfThunder": {"uom": None, "values": []},     # HFO does not produce it
    "weather": {"values": [
        _run(0, "PT2H", [_wx("scattered", "rain_showers", "moderate")]),
        _run(2, "PT1H", [_wx(None, None, None)]),
        _run(3, "PT1H", [_wx("likely", "snow", "light"), _wx("chance", "freezing_rain", "light")]),
    ]},
    "hazards": {"values": []},
}}


def _payload(**series):
    """A copy of GRIDPOINT with properties replaced (``name=None`` removes one)."""
    body = copy.deepcopy(GRIDPOINT)
    for name, value in series.items():
        if value is None:
            body["properties"].pop(name, None)
        else:
            body["properties"][name] = value
    return body


@pytest.fixture
def grid(fake_http):
    fake_http.add("/gridpoints/HFO/255,77", GRIDPOINT)
    return fake_http


# ---- the run-length encoding -------------------------------------------------------------
@pytest.mark.parametrize("duration,hours", [
    ("PT1H", 1), ("PT2H", 2), ("P1D", 24), ("P1DT3H", 27), ("PT30M", 1), ("PT1H30M", 2),
    ("P2DT6H", 54), ("PT0H", 1), ("garbage", 1), ("", 1), (None, 1), ("P1W", 1), ("PT90S", 1),
])
def test_duration_hours(duration, hours):
    assert gridpoint.duration_hours(duration) == hours


def test_expand_run_length_to_hourly():
    m = gridpoint.expand(_series("wmoUnit:percent", [
        _run(-2, "PT3H", 37), _run(1, "PT2H", 80), _run(3, "PT1H", None), _run(4, "P1DT1H", 10)]))
    assert len(m) == 3 + 2 + 1 + 25
    assert all(k.tzinfo is not None and k.utcoffset() == timedelta(0) for k in m)
    assert all(k.minute == 0 and k.second == 0 for k in m)
    assert m[HOUR0 - timedelta(hours=2)] == 37 and m[HOUR0] == 37
    assert m[HOUR0 + timedelta(hours=1)] == 80 and m[HOUR0 + timedelta(hours=2)] == 80
    assert HOUR0 + timedelta(hours=3) in m and m[HOUR0 + timedelta(hours=3)] is None
    assert m[HOUR0 + timedelta(hours=4)] == 10 and m[HOUR0 + timedelta(hours=28)] == 10
    assert HOUR0 + timedelta(hours=29) not in m
    # values of any type are kept as they are (the weather series carries lists)
    wx = gridpoint.expand({"values": [_run(0, "PT1H", [{"weather": "snow"}])]})
    assert wx[HOUR0] == [{"weather": "snow"}]


def test_expand_normalises_the_start():
    # a start in another offset, with minutes, floored to the UTC hour
    m = gridpoint.expand({"values": [{"validTime": "2026-10-08T04:30:00-10:00/PT1H", "value": 1}]})
    assert list(m) == [HOUR0]
    # a validTime without a duration is one hour; a later run overwrites an earlier overlap
    m = gridpoint.expand({"values": [{"validTime": "2026-10-08T14:00:00+00:00", "value": 1},
                                     {"validTime": "2026-10-08T13:00:00Z/PT2H", "value": 2}]})
    assert m == {HOUR0 - timedelta(hours=1): 2, HOUR0: 2}
    # a corrupt duration cannot expand into millions of hours
    m = gridpoint.expand({"values": [_run(0, "P99999D", 1)]})
    assert len(m) == gridpoint.MAX_RUN_HOURS


def test_expand_absent_or_empty_is_empty_and_malformed_raises():
    assert gridpoint.expand(None) == {}
    assert gridpoint.expand({"uom": None, "values": []}) == {}
    assert gridpoint.expand({"uom": "wmoUnit:percent", "values": None}) == {}
    assert gridpoint.expand({"uom": "wmoUnit:percent"}) == {}
    for bad in ("nope", 12, [], {"values": "nope"}, {"values": {}}, {"values": [1]},
                {"values": [{"value": 1}]}, {"values": [{"validTime": "garbage/PT1H", "value": 1}]},
                {"values": [{"validTime": None, "value": 1}, "x"]}):
        with pytest.raises(ValueError):
            gridpoint.expand(bad)
    # one bad entry among good ones is skipped (and counted), never the whole series: an
    # entry days ahead must not blank the hour the verdict reads
    m, skipped = gridpoint.expand_runs({"values": [_run(0, "PT1H", 1), "x", {"validTime": "garbage"}]})
    assert skipped == 2 and list(m.values()) == [(1, HOUR0, 1)]
    assert gridpoint.expand({"values": [_run(0, "PT1H", 1), "x"]}) == {HOUR0: 1}


# ---- fetch -------------------------------------------------------------------------------
def test_fetch_end_to_end(cfg, cache, grid):
    r = gridpoint.fetch(cfg, cache, SITE, META, now=NOW)
    assert grid.calls == [URL]
    assert r["ok"] and not r["stale"] and r["error"] is None and r["age_s"] == 0
    assert "not_provided" not in r and r["url"] == URL
    assert r["updated"] == UPDATED and r["update_age_min"] == 25.0
    assert r["elevation_m"] == 3574.08
    assert r["missing"] == ["thunder_pct"]
    assert r["unusable"] == []
    assert set(r) == {"ok", "stale", "error", "age_s", "url", "updated", "update_age_min",
                      "elevation_m", "missing", "unusable", "hours", "now", "next"}
    assert len(r["hours"]) == 12
    assert r["now"] is r["hours"][0] and r["next"] is r["hours"][1]
    assert [h["start"] for h in r["hours"]] == [HOUR0 + timedelta(hours=k) for k in range(12)]
    assert r["hours"][0]["start"].tzinfo is not None and r["hours"][0]["start"] == HOUR0
    assert [h["local"] for h in r["hours"][:3]] == ["Thu 04:00", "Thu 05:00", "Thu 06:00"]
    assert set(r["hours"][0]) == {"start", "local", "periods"} | set(gridpoint.FIELDS)
    # an accumulation names the run it is the total of (the fixture's 12-hour run from 08Z)
    assert r["hours"][3]["periods"]["snow_mm"] == {"start": HOUR0 - timedelta(hours=6), "hours": 12}
    assert set(r["hours"][3]["periods"]) == {"qpf_mm", "snow_mm", "ice_mm"}
    assert r["hours"][8]["periods"] == {}
    col = {k: [h[k] for h in r["hours"]] for k in gridpoint.FIELDS}
    assert col["sky_cover_pct"] == [37.0, 80.0, 80.0, None] + [10.0] * 8
    assert col["pop_pct"] == [5.0] * 6 + [None] * 6
    assert col["thunder_pct"] == [None] * 12
    assert col["qpf_mm"] == [0.254] * 6 + [None] * 6
    assert col["snow_mm"] == [0.0] * 6 + [None] * 6
    assert col["ice_mm"] == [0.0] * 6 + [None] * 6
    assert col["wind_kmh"] == [14.816] * 3 + [None] * 9
    assert col["gust_kmh"] == [29.632] * 3 + [None] * 9
    assert col["temp_c"][0] == pytest.approx(11.6667, abs=1e-4)
    assert col["temp_c"][1] == pytest.approx(10.5556, abs=1e-4) and col["temp_c"][2] == col["temp_c"][1]
    assert col["temp_c"][3:] == [None] * 9
    assert col["rh_pct"] == [64.0, 64.0] + [None] * 10
    for v in (col["sky_cover_pct"][0], col["pop_pct"][0], col["snow_mm"][0], col["rh_pct"][0]):
        assert isinstance(v, float)
    assert cache.get_any("gridpoint/%s" % LOC)[0] is not None


def test_fetch_weather_entries(cfg, cache, grid):
    r = gridpoint.fetch(cfg, cache, SITE, META, now=NOW)
    wx = [h["weather"] for h in r["hours"]]
    scattered = {"coverage": "scattered", "weather": "rain_showers", "intensity": "moderate"}
    assert wx[0] == [scattered] and wx[1] == [scattered]
    assert wx[2] == []                      # an entry with coverage/weather/intensity all None
    assert wx[3] == [{"coverage": "likely", "weather": "snow", "intensity": "light"},
                     {"coverage": "chance", "weather": "freezing_rain", "intensity": "light"}]
    assert wx[4:] == [[]] * 8
    assert "weather" not in r["missing"]


def test_fetch_empty_thunder_series_is_missing_not_zero(cfg, cache, fake_http):
    fake_http.add("/gridpoints/HFO/255,77", GRIDPOINT)
    r = gridpoint.fetch(cfg, cache, SITE, META, now=NOW)
    assert r["ok"] and r["missing"] == ["thunder_pct"]
    assert all(h["thunder_pct"] is None for h in r["hours"])
    # a series that is absent altogether is missing too; a present one with values is not
    fake_http.routes.clear()
    fake_http.add("/gridpoints/HFO/255,77",
                  _payload(probabilityOfThunder=_series("wmoUnit:percent", [_run(0, "PT3H", 20)]),
                           weather=None, snowfallAmount=None))
    cfg.gridpoint_ttl = 0
    r = gridpoint.fetch(cfg, cache, SITE, META, now=NOW)
    assert r["ok"] and r["missing"] == ["snow_mm", "weather"]
    assert [h["thunder_pct"] for h in r["hours"][:4]] == [20.0, 20.0, 20.0, None]
    assert all(h["weather"] == [] and h["snow_mm"] is None for h in r["hours"])


def test_fetch_malformed_series_is_skipped_and_logged(cfg, cache, fake_http, caplog):
    fake_http.add("/gridpoints/HFO/255,77", _payload(
        skyCover="nope", windGust={"values": [{"validTime": "garbage/PT1H", "value": 1}]},
        temperature={"values": "nope"},
        relativeHumidity=_series("wmoUnit:percent", [_run(0, "PT1H", "abc"), _run(1, "PT1H", {"x": 1})]),
        quantitativePrecipitation=_series("wmoUnit:mm", [_run(0, "PT1H", float("nan"))])))
    with caplog.at_level(logging.WARNING, logger="vlba.gridpoint"):
        r = gridpoint.fetch(cfg, cache, SITE, META, now=NOW)
    assert r["ok"] and r["error"] is None
    # malformed is not "not produced": the verdict must see a gap, not an n/a
    assert r["missing"] == ["thunder_pct"]
    assert r["unusable"] == ["gust_kmh", "sky_cover_pct", "temp_c"]
    assert all(h["sky_cover_pct"] is None and h["gust_kmh"] is None and h["temp_c"] is None
               for h in r["hours"])
    # unreadable values inside a well-formed series are None, not a crash
    assert r["hours"][0]["rh_pct"] is None and r["hours"][1]["rh_pct"] is None
    assert r["hours"][0]["qpf_mm"] is None
    assert r["hours"][0]["pop_pct"] == 5.0                    # the good series are untouched
    logged = [m for m in caplog.messages if "unusable" in m]
    assert len(logged) == 3
    for name in ("skyCover", "windGust", "temperature"):
        assert any(name in m and "mk" in m for m in logged), name


def test_fetch_update_age(cfg, cache, fake_http):
    fake_http.add("/gridpoints/HFO/255,77", GRIDPOINT)
    assert gridpoint.fetch(cfg, cache, SITE, META, now=NOW)["update_age_min"] == 25.0
    later = NOW + timedelta(minutes=100, seconds=6)
    assert gridpoint.fetch(cfg, cache, SITE, META, now=later)["update_age_min"] == 125.1
    # a product dated in the future (clock skew) gives a negative age, never None
    cfg.gridpoint_ttl = 0
    fake_http.routes.clear()
    fake_http.add("/gridpoints/HFO/255,77", _payload(updateTime="2026-10-08T14:42:12+00:00"))
    assert gridpoint.fetch(cfg, cache, SITE, META, now=NOW)["update_age_min"] == -5.0
    # no / unparsable updateTime -> None, and the rest is still fine
    fake_http.routes.clear()
    fake_http.add("/gridpoints/HFO/255,77", _payload(updateTime=None))
    r = gridpoint.fetch(cfg, cache, SITE, META, now=NOW)
    assert r["ok"] and r["updated"] is None and r["update_age_min"] is None
    assert r["hours"][0]["sky_cover_pct"] == 37.0
    fake_http.routes.clear()
    fake_http.add("/gridpoints/HFO/255,77", _payload(updateTime="yesterday-ish"))
    r = gridpoint.fetch(cfg, cache, SITE, META, now=NOW)
    assert r["updated"] == "yesterday-ish" and r["update_age_min"] is None
    fake_http.routes.clear()
    fake_http.add("/gridpoints/HFO/255,77", _payload(updateTime=12345))
    assert gridpoint.fetch(cfg, cache, SITE, META, now=NOW)["updated"] is None


def test_fetch_default_now_is_utcnow(cfg, cache, grid, monkeypatch):
    monkeypatch.setattr(util, "utcnow", lambda: NOW)
    r = gridpoint.fetch(cfg, cache, SITE, META)
    assert r["hours"][0]["start"] == HOUR0 and r["update_age_min"] == 25.0
    # a naive "now" is taken as UTC; a local-time "now" is converted
    assert gridpoint.fetch(cfg, cache, SITE, META, now=NOW.replace(tzinfo=None))["now"]["start"] == HOUR0
    local = NOW.astimezone(util.tzinfo_for("Pacific/Honolulu"))
    assert gridpoint.fetch(cfg, cache, SITE, META, now=local)["now"]["start"] == HOUR0


def test_fetch_window_length(cfg, cache, grid):
    r = gridpoint.fetch(cfg, cache, SITE, META, hours=2, now=NOW)
    assert len(r["hours"]) == 2 and r["now"] is r["hours"][0] and r["next"] is r["hours"][1]
    r = gridpoint.fetch(cfg, cache, SITE, META, hours=1, now=NOW)
    assert len(r["hours"]) == 1 and r["now"]["sky_cover_pct"] == 37.0 and r["next"] is None
    r = gridpoint.fetch(cfg, cache, SITE, META, hours=0, now=NOW)
    assert r["ok"] and r["hours"] == [] and r["now"] is None and r["next"] is None
    # rows exist beyond the forecast's end, every value None
    r = gridpoint.fetch(cfg, cache, SITE, META, hours=48, now=NOW)
    last = r["hours"][-1]
    assert last["start"] == HOUR0 + timedelta(hours=47) and last["local"] == "Sat 03:00"
    assert all(last[k] is None for k in gridpoint.FIELDS if k != "weather") and last["weather"] == []
    assert len(grid.calls) == 1                          # all served from the cache


def test_fetch_labels_follow_site_timezone(cfg, cache, grid):
    assert gridpoint.fetch(cfg, cache, SITE, META, now=NOW)["now"]["local"] == "Thu 04:00"
    denver = gridpoint.fetch(cfg, cache, SITE, dict(META, tz="America/Denver"), now=NOW)
    assert denver["now"]["local"] == "Thu 08:00"
    assert gridpoint.fetch(cfg, cache, SITE, dict(META, tz=None), now=NOW)["now"]["local"] == "Thu 14:00"


def test_fetch_not_provided_without_a_grid(cfg, cache, fake_http):
    samoa = {"ok": True, "stale": False, "error": None, "age_s": 0.0, "grid_id": None,
             "grid_x": None, "grid_y": None, "tz": "Pacific/Pago_Pago", "no_grid": True}
    r = gridpoint.fetch(cfg, cache, SITE, samoa, now=NOW)
    assert not r["ok"] and not r["stale"] and r["not_provided"] is True
    assert r["error"] == nws.NOT_PROVIDED and r["age_s"] is None and r["url"] is None
    assert r["hours"] == [] and r["now"] is None and r["next"] is None
    assert r["updated"] is None and r["update_age_min"] is None and r["elevation_m"] is None
    assert r["missing"] == [] and fake_http.calls == []


def test_fetch_without_metadata(cfg, cache, fake_http):
    # nothing cached: not ok, no request (there is no URL to ask)
    r = gridpoint.fetch(cfg, cache, SITE, NO_META, now=NOW)
    assert not r["ok"] and r["stale"] and "metadata" in r["error"] and r["url"] is None
    assert r["hours"] == [] and r["now"] is None and "not_provided" not in r
    assert fake_http.calls == []
    assert not gridpoint.fetch(cfg, cache, SITE, None, now=NOW)["ok"]
    # ...but once a good copy exists it is served, labelled stale, for max_stale
    fake_http.add("/gridpoints/HFO/255,77", GRIDPOINT)
    assert gridpoint.fetch(cfg, cache, SITE, META, now=NOW)["ok"]
    r = gridpoint.fetch(cfg, cache, SITE, NO_META, now=NOW)
    assert r["ok"] and r["stale"] and "metadata" in r["error"] and r["age_s"] >= 0
    assert r["url"] is None and r["updated"] == UPDATED and len(r["hours"]) == 12
    assert r["now"]["sky_cover_pct"] == 37.0 and r["now"]["local"] == "Thu 14:00"   # NO_META: UTC
    assert len(fake_http.calls) == 1
    cfg.gridpoint_max_stale = 0
    r = gridpoint.fetch(cfg, cache, SITE, NO_META, now=NOW)
    assert not r["ok"] and r["hours"] == [] and len(fake_http.calls) == 1


def test_fetch_problem_document_is_not_cached(cfg, cache, fake_http):
    fake_http.add("/gridpoints/HFO/255,77", {"type": "urn:noaa:nws:api:UnexpectedProblem",
                                             "title": "Unexpected Problem", "status": 500,
                                             "detail": "An unexpected problem has occurred."})
    r = gridpoint.fetch(cfg, cache, SITE, META, now=NOW)
    assert not r["ok"] and r["stale"] and "unexpected payload" in r["error"]
    assert r["hours"] == [] and r["url"] == URL and r["updated"] is None
    assert cache.get_any("gridpoint/%s" % LOC) == (None, None)
    # properties without a single series are no better
    fake_http.routes.clear()
    fake_http.add("/gridpoints/HFO/255,77", {"properties": {"updateTime": UPDATED, "skyCover": "x"}})
    assert not gridpoint.fetch(cfg, cache, SITE, META, now=NOW)["ok"]
    assert cache.get_any("gridpoint/%s" % LOC) == (None, None)
    # one series present as an object is enough (an empty one included)
    assert gridpoint._valid_gridpoint({"properties": {"probabilityOfThunder": {"values": []}}})
    assert not gridpoint._valid_gridpoint({"properties": {"hazards": {"values": []}}})
    assert not gridpoint._valid_gridpoint([]) and not gridpoint._valid_gridpoint(None)


def test_fetch_cache_ttl_and_last_good_fallback(cfg, cache, grid):
    assert gridpoint.fetch(cfg, cache, SITE, META, now=NOW)["ok"]
    r = gridpoint.fetch(cfg, cache, SITE, META, now=NOW)
    assert r["ok"] and not r["stale"] and grid.calls == [URL]          # served from the cache
    cfg.gridpoint_ttl = 0                     # force a refetch...
    grid.routes.clear()
    grid.fail("api.weather.gov")              # ...which now fails
    r = gridpoint.fetch(cfg, cache, SITE, META, now=NOW)
    assert grid.calls == [URL, URL]
    assert r["ok"] and r["stale"] and r["error"] and r["age_s"] >= 0
    assert r["updated"] == UPDATED and r["now"]["sky_cover_pct"] == 37.0
    cfg.gridpoint_max_stale = 0               # beyond max_stale nothing is usable
    r = gridpoint.fetch(cfg, cache, SITE, META, now=NOW)
    assert not r["ok"] and r["stale"] and r["error"] and r["hours"] == [] and r["updated"] is None
    assert r["missing"] == [] and r["elevation_m"] is None


def test_fetch_never_hits_the_network_for_a_fresh_copy(cfg, cache, grid):
    assert gridpoint.fetch(cfg, cache, SITE, META, now=NOW)["ok"]
    grid.routes.clear()
    grid.fail("api.weather.gov")
    r = gridpoint.fetch(cfg, cache, SITE, META, hours=3, now=NOW + timedelta(hours=1))
    assert r["ok"] and not r["stale"] and len(grid.calls) == 1
    assert r["now"]["start"] == HOUR0 + timedelta(hours=1) and r["now"]["sky_cover_pct"] == 80.0
    assert r["update_age_min"] == 85.0


# ---- the URL from the real metadata ------------------------------------------------------------
def test_url_from_site_metadata(cfg, cache, fake_http):
    fake_http.add("/points/%s" % LOC, POINTS)
    fake_http.add("/gridpoints/HFO/255,77", GRIDPOINT)
    meta = nws.site_metadata(cfg, cache, SITE)
    assert meta["ok"] and meta["no_grid"] is False
    assert gridpoint.grid_url(meta) == URL
    r = gridpoint.fetch(cfg, cache, SITE, meta, now=NOW)
    assert r["ok"] and r["url"] == URL and r["now"]["local"] == "Thu 04:00"
    assert fake_http.calls == ["https://api.weather.gov/points/%s" % LOC, URL]
    # the key names the point, not the slug
    assert cache.get_any("gridpoint/%s" % LOC)[0] is not None
    assert cache.get_any("gridpoint/mk") == (None, None)


def test_grid_url_refuses_odd_grids():
    assert gridpoint.grid_url({"grid_id": "HFO", "grid_x": 255.0, "grid_y": 77}) == URL
    for meta in (None, {}, {"grid_id": None, "grid_x": 255, "grid_y": 77},
                 {"grid_id": "HFO", "grid_x": None, "grid_y": 77},
                 {"grid_id": "HFO", "grid_x": 255, "grid_y": "77"},
                 {"grid_id": "HFO", "grid_x": True, "grid_y": 77},
                 {"grid_id": "HFO", "grid_x": 255.5, "grid_y": 77},
                 {"grid_id": "../points", "grid_x": 255, "grid_y": 77},
                 {"grid_id": "HFO X", "grid_x": 255, "grid_y": 77},
                 {"grid_id": "", "grid_x": 255, "grid_y": 77}):
        assert gridpoint.grid_url(meta) is None, meta


# ---- the weather entries -------------------------------------------------------------------------
def test_winter_weather():
    row = {"weather": [{"coverage": "likely", "weather": "snow", "intensity": "light"},
                       {"coverage": "scattered", "weather": "rain_showers", "intensity": "moderate"},
                       {"coverage": "chance", "weather": "freezing_rain", "intensity": None},
                       {"coverage": "definite", "weather": "sleet", "intensity": "heavy"},
                       {"coverage": "slight_chance", "weather": "thunderstorms", "intensity": None},
                       {"coverage": "areas", "weather": "blowing_snow", "intensity": None},
                       {"coverage": "patchy", "weather": "freezing_fog", "intensity": None},
                       {"coverage": "chance", "weather": "ice_pellets", "intensity": "light"},
                       {"coverage": "chance", "weather": "hail", "intensity": None},
                       {"coverage": "chance", "weather": None, "intensity": "light"},
                       "garbage"]}
    got = gridpoint.winter_weather(row)
    assert [e["weather"] for e in got] == ["snow", "freezing_rain", "sleet", "blowing_snow",
                                           "freezing_fog", "ice_pellets"]
    assert got[0] is row["weather"][0]                   # the row's own entries, coverage included
    assert [e["coverage"] for e in got[:2]] == ["likely", "chance"]
    assert gridpoint.winter_weather({"weather": []}) == []
    assert gridpoint.winter_weather({"weather": None}) == []
    assert gridpoint.winter_weather({}) == [] and gridpoint.winter_weather(None) == []
    for wx in ("snow", "snow_showers", "Freezing Drizzle", "ice_crystals", "freezing-rain"):
        assert gridpoint.is_winter(wx), wx
    for wx in ("rain", "rain_showers", "drizzle", "fog", "frost", "hail", "smoke", "", None, 3):
        assert not gridpoint.is_winter(wx), wx


def test_winter_weather_from_fetched_rows(cfg, cache, grid):
    r = gridpoint.fetch(cfg, cache, SITE, META, now=NOW)
    assert gridpoint.winter_weather(r["now"]) == [] and gridpoint.winter_weather(r["next"]) == []
    assert [e["weather"] for e in gridpoint.winter_weather(r["hours"][3])] == ["snow", "freezing_rain"]


def test_weather_text():
    assert gridpoint.weather_text({"coverage": "likely", "weather": "snow",
                                   "intensity": "light"}) == "likely snow (light)"
    assert gridpoint.weather_text({"coverage": "slight_chance", "weather": "rain_showers",
                                   "intensity": None}) == "slight chance rain showers"
    assert gridpoint.weather_text({"coverage": "definite", "weather": "freezing_rain",
                                   "intensity": "heavy"}) == "definite freezing rain (heavy)"
    assert gridpoint.weather_text({"coverage": None, "weather": "fog", "intensity": None}) == "fog"
    assert gridpoint.weather_text({"coverage": None, "weather": None, "intensity": "heavy"}) == "heavy"
    assert gridpoint.weather_text({}) == "" and gridpoint.weather_text(None) == ""
