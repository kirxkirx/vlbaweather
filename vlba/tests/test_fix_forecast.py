"""The forecast and wind checks after the audit: "n/a" only for a series the site's office is
known not to produce, no "clear" from an hour without a value, the units of the gridpoint
series, the wind check's silent halves, and the checks that raise."""
from __future__ import annotations

import copy
import itertools
import os

import pytest

from vlba import decision as d
from vlba import gridpoint, sites
from weather import http as _http
from vlba.tests.test_decision import NOW, _entry, _row
from vlba.tests.test_gridpoint import GRIDPOINT, META, NOW as GP_NOW, _payload, _run, _series, _wx

PT_SITE = next(dict(s) for s in sites.SITES if s["slug"] == "pt")
MK_SITE = next(dict(s) for s in sites.SITES if s["slug"] == "mk")
SC_SITE = next(dict(s) for s in sites.SITES if s["slug"] == "sc")
PT_META = dict(META, grid_id="ABQ", grid_x=10, grid_y=20, tz="America/Denver")


def _state(v, key):
    return [c for c in v["checks"] if c["key"] == key][0]


def _gp(**kw):
    gp = {"ok": True, "stale": False, "update_age_min": 10.0, "missing": [], "now": _row(), "next": _row()}
    gp.update(kw)
    return gp


_CACHES = itertools.count()


def _fetch(cfg, cache, fake_http, site, body, meta=PT_META, keep=False):
    """``gridpoint.fetch`` of ``body``: through ``cache`` when ``keep``, else through a
    fresh cache (the gridpoint is cached for ``cfg.gridpoint_ttl``)."""
    fake_http.routes.clear()
    fake_http.add("/gridpoints/", body)
    if not keep:
        cache = _http.Cache(os.path.join(cfg.cache_dir, "fresh%d" % next(_CACHES)))
    return gridpoint.fetch(cfg, cache, site, meta, now=GP_NOW)


def _with_thunder(body):
    """The fixture (Mauna Kea's, where HFO sends no thunder) with a thunder series, as a
    contiguous-states office sends it."""
    body["properties"]["probabilityOfThunder"] = {"values": [_run(-2, "PT12H", 0)]}
    return body


# ---- #68 #107 #117: "n/a" only for what the office does not produce ----------------------
def test_the_tuples_of_the_built_in_sites():
    """Checked against api.weather.gov on 2026-11-12: HFO no thunder, SJU no thunder, snow
    or ice, TWC no ice; every other verdict series had values at every site."""
    got = {s["slug"]: sites.series_not_provided(s) for s in sites.SITES}
    assert got.pop("mk") == ("thunder_pct",)
    assert got.pop("sc") == ("thunder_pct", "snow_mm", "ice_mm")
    assert got.pop("kp") == ("ice_mm",)
    assert set(got.values()) == {()}
    # a built-in site named through WEATHER_SITES (no key) is judged by its place
    assert sites.series_not_provided({"slug": "x", "lat": 17.76, "lon": -64.58}) == SC_SITE["not_provided"]
    assert sites.series_not_provided({"slug": "pt", "lat": 34.301, "lon": -108.1192}) == ()
    # any other site may lack the thunder probability only
    assert sites.series_not_provided({"slug": "socorro", "lat": 34.06, "lon": -106.89}) == ("thunder_pct",)
    assert sites.series_not_provided(None) == ("thunder_pct",)
    assert sites.series_not_provided(dict(PT_SITE, not_provided="snow_mm")) == ()


@pytest.mark.parametrize("name,key", [("skyCover", "forecast_cloud"),
                                      ("probabilityOfPrecipitation", "forecast_precip"),
                                      ("probabilityOfThunder", "forecast_thunder")])
@pytest.mark.parametrize("how", ["empty", "absent"])
def test_an_empty_core_series_is_no_data(cfg, cache, fake_http, name, key, how):
    body = _with_thunder(copy.deepcopy(GRIDPOINT))
    if how == "empty":
        body["properties"][name] = {"uom": "wmoUnit:percent", "values": []}
    else:
        del body["properties"][name]
    gp = _fetch(cfg, cache, fake_http, PT_SITE, body)
    field = {v: k for k, v in gridpoint.FIELDS.items()}[name]
    assert gp["missing"] == [field] and gp["absent"] == [field]
    assert d.forecast_unreadable(cfg, gp) == [field]           # status.json: gridpoint not ok
    gp["update_age_min"] = 10.0
    v = d.evaluate(cfg, _entry(site=PT_SITE, gridpoint=gp), {}, NOW)
    c = _state(v, key)
    assert c["state"] == "unknown" and "the forecast has no" in c["detail"]
    assert v["incomplete"] and c["label"] in v["missing"]


def test_every_series_empty_is_no_data_not_a_complete_good(cfg, cache, fake_http):
    body = copy.deepcopy(GRIDPOINT)
    for name in gridpoint.FIELDS.values():
        body["properties"][name] = {"values": []}
    gp = _fetch(cfg, cache, fake_http, PT_SITE, body)
    gp["update_age_min"] = 10.0
    v = d.evaluate(cfg, _entry(site=PT_SITE, gridpoint=gp), {}, NOW)
    for key in ("forecast_precip", "forecast_thunder", "forecast_cloud", "forecast_winter"):
        assert _state(v, key)["state"] == "unknown", key
    assert _state(v, "wind")["state"] == "unknown"
    assert v["incomplete"]


def test_what_the_office_does_not_produce_stays_not_provided(cfg, cache, fake_http):
    # Mauna Kea: no thunder (the fixture is HFO's)
    gp = _fetch(cfg, cache, fake_http, MK_SITE, copy.deepcopy(GRIDPOINT))
    assert gp["missing"] == ["thunder_pct"] and gp["absent"] == []
    gp["update_age_min"] = 10.0
    v = d.evaluate(cfg, _entry(site=MK_SITE, gridpoint=gp), {}, NOW)
    assert _state(v, "forecast_thunder")["state"] == "not provided" and "Thunder" not in str(v["missing"])
    # the same product at Pie Town: ABQ produces thunder, so it is a gap there
    gp = _fetch(cfg, cache, fake_http, PT_SITE, copy.deepcopy(GRIDPOINT))
    assert gp["absent"] == ["thunder_pct"]
    gp["update_age_min"] = 10.0
    assert _state(d.evaluate(cfg, _entry(site=PT_SITE, gridpoint=gp), {}, NOW), "forecast_thunder")["state"] == "unknown"
    # St. Croix: no snow, ice or thunder; the weather entries answer the winter check
    body = _payload(snowfallAmount={"values": []}, iceAccumulation={"values": []})
    gp = _fetch(cfg, cache, fake_http, SC_SITE, body)
    assert gp["absent"] == []
    gp["update_age_min"] = 10.0
    v = d.evaluate(cfg, _entry(site=SC_SITE, gridpoint=gp), {}, NOW)
    c = _state(v, "forecast_winter")
    assert c["state"] == "ok" and "office's product" in c["detail"]


def test_a_result_without_absent_is_judged_by_the_site(cfg):
    gp = _gp(missing=["thunder_pct", "pop_pct"], now=_row(thunder_pct=None, pop_pct=None),
             next=_row(thunder_pct=None, pop_pct=None))
    v = d.evaluate(cfg, _entry(site=MK_SITE, gridpoint=gp), {}, NOW)
    assert _state(v, "forecast_thunder")["state"] == "not provided"
    assert _state(v, "forecast_precip")["state"] == "unknown"
    v = d.evaluate(cfg, _entry(site=PT_SITE, gridpoint=gp), {}, NOW)
    assert _state(v, "forecast_thunder")["state"] == "unknown"


# ---- #120: no grid is "n/a" only at a gridless site --------------------------------------
def test_a_points_answer_without_a_grid_is_no_data(cfg, cache, fake_http):
    nogrid = dict(PT_META, grid_id=None, grid_x=None, grid_y=None, no_grid=True)
    gp = gridpoint.fetch(cfg, cache, PT_SITE, nogrid, now=GP_NOW)
    assert not gp.get("not_provided") and not gp["ok"] and gp["error"] == gridpoint.NO_GRID
    v = d.evaluate(cfg, _entry(site=PT_SITE, gridpoint=gp), {}, NOW)
    for key in ("forecast_precip", "forecast_thunder", "forecast_cloud", "forecast_winter"):
        assert _state(v, key)["state"] == "unknown", key
    assert v["incomplete"]
    # the last good copy is served meanwhile
    good = _fetch(cfg, cache, fake_http, PT_SITE, _with_thunder(copy.deepcopy(GRIDPOINT)), keep=True)
    assert good["ok"]
    gp = gridpoint.fetch(cfg, cache, PT_SITE, nogrid, now=GP_NOW)
    assert gp["ok"] and gp["stale"] and not gp.get("not_provided")
    # a grid id without a forecast link: the gridpoint is fetched as usual
    gp = gridpoint.fetch(cfg, cache, PT_SITE, dict(PT_META, no_grid=True), now=GP_NOW)
    assert gp["ok"] and not gp["stale"] and gp["error"] is None
    # a site entered as gridless: n/a
    gp = gridpoint.fetch(cfg, cache, dict(PT_SITE, gridless=True), nogrid, now=GP_NOW)
    assert gp["not_provided"] is True
    v = d.evaluate(cfg, _entry(site=PT_SITE, gridpoint=gp), {}, NOW)
    assert _state(v, "forecast_cloud")["state"] == "not provided"
    assert sites.is_gridless({"gridless": "yes"}) is False


# ---- #70 #108 #122: the winter check needs an answer for this hour ------------------------
def test_winter_without_any_value_for_the_hour_is_unknown(cfg):
    rows = dict(snow_mm=None, ice_mm=None, weather=[], weather_covered=False)
    v = d.evaluate(cfg, _entry(gridpoint=_gp(now=_row(**rows), next=_row(**rows))), {}, NOW)
    c = _state(v, "forecast_winter")
    assert c["state"] == "unknown" and v["incomplete"] and "for this hour" in c["detail"]
    # the same with only the weather series missing
    v = d.evaluate(cfg, _entry(gridpoint=_gp(missing=["weather"], now=_row(**rows), next=_row(**rows))), {}, NOW)
    assert _state(v, "forecast_winter")["state"] == "unknown"


def test_a_snowfall_series_without_a_value_for_the_hour_is_unknown(cfg):
    row = _row(snow_mm=None, ice_mm=0.0, weather=[], weather_covered=True)
    c = _state(d.evaluate(cfg, _entry(gridpoint=_gp(now=row, next=row)), {}, NOW), "forecast_winter")
    assert c["state"] == "unknown" and "no snowfall value for this hour" in c["detail"]


def test_an_absent_amount_is_answered_by_the_weather(cfg):
    covered = _row(ice_mm=None, weather=[], weather_covered=True)
    gp = _gp(missing=["ice_mm"], absent=["ice_mm"], now=covered, next=covered)
    assert _state(d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW), "forecast_winter")["state"] == "ok"
    bare = _row(ice_mm=None, weather=[], weather_covered=False)
    gp = _gp(missing=["ice_mm"], absent=["ice_mm"], now=bare, next=bare)
    c = _state(d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW), "forecast_winter")
    assert c["state"] == "unknown" and "no ice accumulation series" in c["detail"]


def test_weather_coverage_from_the_gridpoint(cfg, cache, fake_http):
    """NWS writes "nothing forecast" as an entry of nulls; a weather series that starts
    later does not cover the hour, and the rows used to show both as an empty list."""
    gp = _fetch(cfg, cache, fake_http, PT_SITE, _with_thunder(copy.deepcopy(GRIDPOINT)))
    assert [r["weather_covered"] for r in gp["hours"][:5]] == [True, True, True, True, False]
    assert gp["hours"][2]["weather"] == []          # "nothing", covered
    body = _with_thunder(_payload(weather={"values": [_run(2, "PT3H", [_wx("definite", "snow", "heavy")])]},
                                  snowfallAmount=_series("wmoUnit:mm", [_run(0, "PT6H", None)]),
                                  iceAccumulation=_series("wmoUnit:mm", [_run(2, "PT6H", 3.0)])))
    gp = _fetch(cfg, cache, fake_http, PT_SITE, body)
    assert gp["now"]["weather"] == [] and gp["now"]["weather_covered"] is False
    gp["update_age_min"] = 10.0
    c = _state(d.evaluate(cfg, _entry(site=PT_SITE, gridpoint=gp), {}, NOW), "forecast_winter")
    assert c["state"] == "unknown"


# ---- #75 #112: a percentage needs this hour's value -------------------------------------
@pytest.mark.parametrize("field,key", [("pop_pct", "forecast_precip"), ("thunder_pct", "forecast_thunder"),
                                       ("sky_cover_pct", "forecast_cloud")])
def test_no_value_for_this_hour_is_unknown(cfg, field, key):
    gp = _gp(now=_row(**{field: None}), next=_row(**{field: 5.0}))
    v = d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)
    c = _state(v, key)
    assert c["state"] == "unknown" and c["detail"].startswith("no value for this hour; next hour at or below")
    assert v["incomplete"]
    # over the limit next hour is still bad
    gp["next"] = _row(**{field: 99.0})
    assert _state(d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW), key)["state"] == "bad"
    # the detail names the hours read
    gp = _gp(now=_row(), next=_row(**{field: None}))
    c = _state(d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW), key)
    assert c["state"] == "ok" and c["detail"].startswith("this hour at or below")
    c = _state(d.evaluate(cfg, _entry(gridpoint=_gp()), {}, NOW), key)
    assert c["detail"].startswith("this hour and next at or below")


# ---- #74 #109 #153: the units of the series ------------------------------------------------
@pytest.mark.parametrize("uom,factor", [("wmoUnit:km_h-1", 1.0), ("wmoUnit:m_s-1", 3.6), ("wmoUnit:kn", 1.852),
                                        ("wmoUnit:mi_h-1", 1.609344)])
def test_wind_units_are_converted(cfg, cache, fake_http, uom, factor):
    body = _with_thunder(_payload(windGust=_series(uom, [_run(-1, "PT4H", 25.0)])))
    gp = _fetch(cfg, cache, fake_http, PT_SITE, body)
    assert gp["now"]["gust_kmh"] == pytest.approx(25.0 * factor, rel=1e-4) and gp["unusable"] == []


def test_a_gust_in_m_per_s_reaches_the_limit(cfg, cache, fake_http):
    body = _with_thunder(_payload(windGust=_series("wmoUnit:m_s-1", [_run(-1, "PT4H", 25.0)])))
    gp = _fetch(cfg, cache, fake_http, PT_SITE, body)
    gp["update_age_min"] = 10.0
    v = d.evaluate(cfg, _entry(site=PT_SITE, gridpoint=gp), {}, NOW)
    assert v["status"] == d.BAD and _state(v, "wind")["state"] == "bad"


@pytest.mark.parametrize("name,uom", [("windGust", "wmoUnit:furlong_fortnight-1"), ("windGust", None),
                                      ("skyCover", "wmoUnit:degC"), ("skyCover", None),
                                      ("probabilityOfPrecipitation", 7)])
def test_an_unknown_unit_makes_the_series_unusable(cfg, cache, fake_http, name, uom):
    body = _with_thunder(copy.deepcopy(GRIDPOINT))
    body["properties"][name]["uom"] = uom
    gp = _fetch(cfg, cache, fake_http, PT_SITE, body)
    field = {v: k for k, v in gridpoint.FIELDS.items()}[name]
    assert gp["unusable"] == [field] and gp["now"][field] is None
    gp["update_age_min"] = 10.0
    v = d.evaluate(cfg, _entry(site=PT_SITE, gridpoint=gp), {}, NOW)
    assert v["incomplete"]


def test_thunder_without_a_unit_is_fine(cfg, cache, fake_http):
    """NWS sends probabilityOfThunder without any uom (at the ten sites whose office
    produces it, 2026-11-12)."""
    gp = _fetch(cfg, cache, fake_http, PT_SITE, _with_thunder(copy.deepcopy(GRIDPOINT)))
    assert gp["unusable"] == [] and gp["now"]["thunder_pct"] == 0.0


# ---- #73 #143: the wind check's silent halves ---------------------------------------------
def test_a_qualifying_station_without_a_report_leaves_the_wind_unknown(cfg):
    e = _entry(obs={"ok": False, "error": "KONM: report is 4 h old"},
               obs_station={"id": "KONM", "usable": False, "qualifying": ["KONM"]})
    v = d.evaluate(cfg, e, {}, NOW)
    c = _state(v, "wind")
    assert c["state"] == "unknown" and "Wind" in v["missing"] and "report is 4 h old" in c["detail"]
    # ... unless a value already reaches the limit
    e["gridpoint"] = _gp(now=_row(gust_kmh=80.0))
    assert _state(d.evaluate(cfg, e, {}, NOW), "wind")["state"] == "bad"
    # where no station qualifies (Mauna Kea, Pie Town) the forecast is the check
    e = _entry(obs={"ok": False, "error": "no data"}, obs_station={"id": None, "usable": False, "qualifying": []})
    assert _state(d.evaluate(cfg, e, {}, NOW), "wind")["state"] == "ok"
    # the nearest station unusable, a qualifying one silent: unknown
    e = _entry(obs_station={"id": "KONM", "usable": False, "why": "too far", "qualifying": ["KXYZ"]})
    assert _state(d.evaluate(cfg, e, {}, NOW), "wind")["state"] == "unknown"


def test_a_missing_forecast_half_leaves_the_wind_unknown(cfg):
    for gp in ({"ok": False, "error": "HTTP 503"},
               _gp(missing=["gust_kmh"], now=_row(gust_kmh=None, wind_kmh=60.0), next=_row(gust_kmh=None, wind_kmh=60.0)),
               _gp(now=_row(gust_kmh=None), next=_row(gust_kmh=30.0))):
        c = _state(d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW), "wind")
        assert c["state"] == "unknown", (gp, c)
    # a gridless site: the station alone answers
    c = _state(d.evaluate(cfg, _entry(gridpoint={"ok": False, "not_provided": True}), {}, NOW), "wind")
    assert c["state"] == "ok"


# ---- #132 and S25: the wind limit as displayed -------------------------------------------
@pytest.mark.parametrize("gust,state", [(45.0, "bad"), (44.96, "bad"), (44.94, "ok"), (44.9, "ok"),
                                        (45.5, "bad")])
def test_the_wind_limit_as_shown(cfg, gust, state):
    assert cfg.wind_mph == 45.0
    c = _state(d.evaluate(cfg, _entry(obs=dict(_entry()["obs"], gust_mph=gust)), {}, NOW), "wind")
    assert c["state"] == state
    if state == "ok":
        assert "45 mph" not in c["detail"]


def test_the_forecast_wind_limit_as_shown(cfg):
    kmh = 44.96 / 0.621371
    c = _state(d.evaluate(cfg, _entry(gridpoint=_gp(now=_row(gust_kmh=kmh))), {}, NOW), "wind")
    assert c["state"] == "bad" and c["value"] == "forecast gust 45 mph (this hour)"


# ---- S25: every winter type and coverage, the smallest amount ---------------------------
@pytest.mark.parametrize("kind", d._WINTER_TYPES)
@pytest.mark.parametrize("coverage", d._WINTER_COVERAGE)
def test_every_winter_type_and_coverage(cfg, kind, coverage):
    row = _row(weather=[{"coverage": coverage, "weather": kind, "intensity": None}])
    v = d.evaluate(cfg, _entry(gridpoint=_gp(now=row)), {}, NOW)
    assert v["status"] == d.BAD and _state(v, "forecast_winter")["state"] == "bad"


@pytest.mark.parametrize("coverage", ["slight_chance", "chance", "isolated", "scattered", "patchy"])
def test_a_low_coverage_is_not_a_snowstorm(cfg, coverage):
    row = _row(weather=[{"coverage": coverage, "weather": "snow", "intensity": None}])
    assert _state(d.evaluate(cfg, _entry(gridpoint=_gp(now=row)), {}, NOW), "forecast_winter")["state"] == "ok"


@pytest.mark.parametrize("field", ["snow_mm", "ice_mm"])
def test_a_tenth_of_a_millimetre_is_bad(cfg, field):
    for which in ("now", "next"):
        v = d.evaluate(cfg, _entry(gridpoint=_gp(**{which: _row(**{field: 0.1})})), {}, NOW)
        assert v["status"] == d.BAD and _state(v, "forecast_winter")["state"] == "bad"


# ---- S26: no value is no answer --------------------------------------------------------------
@pytest.mark.parametrize("field,key", [("pop_pct", "forecast_precip"), ("thunder_pct", "forecast_thunder"),
                                       ("sky_cover_pct", "forecast_cloud")])
def test_null_values_in_both_hours_are_unknown(cfg, field, key):
    v = d.evaluate(cfg, _entry(gridpoint=_gp(now=_row(**{field: None}), next=_row(**{field: None}))), {}, NOW)
    assert _state(v, key)["state"] == "unknown" and v["status"] == d.GOOD and v["incomplete"]


def test_no_humidity_value_with_the_check_on_is_unknown(cfg):
    cfg.obs_humidity_max = 95.0
    v = d.evaluate(cfg, _entry(obs=dict(_entry()["obs"], rh=None)), {}, NOW)
    assert _state(v, "obs_humidity")["state"] == "unknown" and v["incomplete"]


def test_unknown_or_not_provided_everywhere_is_no_data(cfg):
    e = {"slug": "pt", "tz": "UTC", "alerts_feed": {"ok": False, "error": "down"}, "alerts_at": [],
         "radar_check": {"available": False, "error": "down"}, "glm": {"covered": True, "available": False},
         "gridpoint": {"ok": False, "not_provided": True},
         "obs": {"ok": False, "error": "down"}, "obs_station": {"id": None, "usable": False, "qualifying": []}}
    v = d.evaluate(cfg, e, {}, NOW)
    states = {c["state"] for c in v["checks"]}
    assert states <= {"unknown", "not provided", "off"} and "not provided" in states
    assert v["status"] == d.NO_DATA and v["incomplete"]
    assert d.summary([v]) == {"good": 0, "bad": 0, "no_data": 1, "incomplete": 1}


# ---- #76 and S19: a check that raises -------------------------------------------------------
CHECKS = {"check_warning": ("warning",), "check_radar": ("radar",), "check_glm": ("glm",),
          "check_forecast": ("forecast_precip", "forecast_thunder", "forecast_cloud", "forecast_winter"),
          "check_observation": ("obs_precip", "obs_humidity"), "check_wind": ("wind",)}


@pytest.mark.parametrize("fn", sorted(CHECKS))
def test_a_check_that_raises_is_unknown(cfg, monkeypatch, fn):
    cfg.obs_humidity_max = 95.0                 # every check on

    def boom(*a, **k):
        raise RuntimeError("surprise")
    monkeypatch.setattr(d, fn, boom)
    v = d.evaluate(cfg, _entry(), {}, NOW)
    for key in CHECKS[fn]:
        c = _state(v, key)
        assert c["state"] == "unknown" and c["detail"] == "check failed (see the log)", key
        assert c["label"] in v["missing"]
    assert v["status"] == d.GOOD and v["incomplete"]


@pytest.mark.parametrize("fn,off", [("check_observation", "obs_humidity"), ("check_forecast", "forecast_cloud")])
def test_a_switched_off_sibling_stays_off(cfg, monkeypatch, fn, off):
    cfg.nws_cloud_max = -1
    assert not cfg.check_enabled("obs_humidity") and not cfg.check_enabled("forecast_cloud")

    def boom(*a, **k):
        raise RuntimeError("surprise")
    monkeypatch.setattr(d, fn, boom)
    v = d.evaluate(cfg, _entry(), {}, NOW)
    assert _state(v, off)["state"] == "off" and _state(v, off)["label"] not in v["missing"]
    assert [k for k in CHECKS[fn] if k != off and _state(v, k)["state"] != "unknown"] == []


# ---- review: #73 by another route ---------------------------------------------------------
def test_a_gust_series_without_a_value_for_this_hour_leaves_the_wind_unknown(cfg):
    """The gust series exists but holds no value for this hour (a null run): the sustained
    wind alone is no answer, as for an absent gust series; a value at the limit is still
    BAD, and a gust for this hour with none for the next is clear."""
    gp = _gp(now=_row(gust_kmh=None, wind_kmh=40.0), next=_row(gust_kmh=30.0, wind_kmh=40.0))
    c = _state(d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW), "wind")
    assert c["state"] == "unknown" and "no forecast gust for this hour" in c["detail"], c
    gp = _gp(now=_row(gust_kmh=None, wind_kmh=80.0), next=_row(gust_kmh=30.0))
    assert _state(d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW), "wind")["state"] == "bad"
    gp = _gp(now=_row(gust_kmh=30.0, wind_kmh=20.0), next=_row(gust_kmh=None, wind_kmh=20.0))
    assert _state(d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW), "wind")["state"] == "ok"


def test_a_null_gust_run_from_the_gridpoint_leaves_the_wind_unknown(cfg, cache, fake_http):
    body = _with_thunder(copy.deepcopy(GRIDPOINT))
    for v in body["properties"]["windGust"]["values"]:
        v["value"] = None
    gp = _fetch(cfg, cache, fake_http, PT_SITE, body)
    assert gp["ok"] and "gust_kmh" not in gp["missing"] and gp["now"]["gust_kmh"] is None
    c = _state(d.evaluate(cfg, _entry(site=PT_SITE, gridpoint=gp), {}, NOW), "wind")
    assert c["state"] == "unknown" and "no forecast gust for this hour" in c["detail"], c


def test_a_missing_weather_series_covers_no_hour(cfg):
    """A row without ``weather_covered`` (not from ``gridpoint.fetch``) whose ``weather`` is
    an empty list does not answer the hour when the weather series is missing: with every
    series missing and nothing exempt, the winter check is unknown, not "none"."""
    rows = [dict(_row(), snow_mm=None, ice_mm=None) for _ in range(2)]
    gp = _gp(missing=["ice_mm", "snow_mm", "weather"], absent=["ice_mm", "snow_mm", "weather"],
             now=rows[0], next=rows[1])
    c = _state(d.evaluate(cfg, _entry(site=PT_SITE, gridpoint=gp), {}, NOW), "forecast_winter")
    assert c["state"] == "unknown", c
    # at St. Croix the amounts are exempt, the weather is not: its absence is a gap there too
    gp = _gp(missing=["ice_mm", "snow_mm", "weather"], absent=["weather"], now=rows[0], next=rows[1])
    assert _state(d.evaluate(cfg, _entry(site=SC_SITE, gridpoint=gp), {}, NOW),
                  "forecast_winter")["state"] == "unknown"
    # the amounts alone still answer when the weather series is missing
    rows = [_row(), _row()]
    gp = _gp(missing=["weather"], absent=["weather"], now=rows[0], next=rows[1])
    assert _state(d.evaluate(cfg, _entry(site=PT_SITE, gridpoint=gp), {}, NOW), "forecast_winter")["state"] == "ok"
