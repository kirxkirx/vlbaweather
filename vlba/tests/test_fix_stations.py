"""The station reports of the precipitation check (the audit's "station reports" findings):
which report decides, METARs that cannot show precipitation, a station without a known
position, the aviationweather.gov fallback's failures, and the rules the suite did not pin
(S13, S20, S27)."""
from __future__ import annotations

import json
import os
import time
from datetime import timedelta

import pytest

from weather import http, nws, util
import make_vlba_page as mvp                     # noqa: E402  (conftest put vlbaweather on sys.path)
from vlba import decision
from vlba.tests.test_main import (_history, _iso, _late_nws, _metar, _no_weather, _precip_check, _report,
                                  _setup_two_stations, add_routes, observation_doc, stations_doc, _mrms_result,
                                  _quiet_glm)


def _metars(fake_http, metars):
    """aviationweather.gov's answer, ahead of every other route: a list, or bytes as they are."""
    body = metars if isinstance(metars, bytes) else json.dumps(metars).encode()
    fake_http.routes.insert(0, ("aviationweather.gov/api/data/metar", body, True))


def _ago(minutes):
    return util.utcnow() - timedelta(minutes=minutes)


def _site(cfg):
    return dict(cfg.sites[0])


# ---- S6: rain at the nearest station is not overridden by a farther one's newer "clear" --------
def test_own_metar_rain_beats_a_farther_slightly_newer_clear_metar(cfg, fake_http, monkeypatch):
    """NWS serves both stations late; at aviationweather.gov KNEAR (3 km) has +RA of 37 min ago,
    KFAR (20 km) a clear METAR of 35 min ago: the rain decides (was: GOOD, complete)."""
    _setup_two_stations(cfg, fake_http, monkeypatch, {"/latest": _report("KNEAR", "Light Rain", 97)},
                        {"/latest": _report("KFAR", "Clear", 95)})
    _metars(fake_http, [_metar("KNEAR", 37, "+RA"), _metar("KFAR", 35)])
    pt, c = _precip_check(cfg)
    assert c["state"] == "bad" and pt["verdict"]["status"] == "BAD", c
    assert c["detail"].startswith("KNEAR METAR of 37 min ago from aviationweather.gov")


def test_own_rain_in_its_history_beats_a_farther_newer_clear(cfg, fake_http, monkeypatch):
    """NWS only: the nearest station's latest record is blank, it reported rain 20 min ago; the
    next station's "Clear" is 15 min old: the rain decides (was: GOOD, complete)."""
    _setup_two_stations(cfg, fake_http, monkeypatch,
                        {"/latest": _report("KNEAR", minutes_ago=5),
                         "?limit": _history(_report("KNEAR", minutes_ago=5), _report("KNEAR", "Light Rain", 20))},
                        {"/latest": _report("KFAR", "Clear", 15)})
    pt, c = _precip_check(cfg)
    assert c["state"] == "bad" and "KNEAR report of 20 min ago" in c["detail"], c


def test_rain_at_a_farther_station_counts_against_an_own_clear(cfg, fake_http, monkeypatch):
    """Precipitation in the last 90 min at any checked station counts, the newest such report
    decides; without any, the nearest station's fresh "Clear" decides, not a farther one's
    newer "Clear"."""
    _setup_two_stations(cfg, fake_http, monkeypatch,
                        {"/latest": _report("KNEAR", minutes_ago=2),
                         "?limit": _history(_report("KNEAR", minutes_ago=2), _report("KNEAR", "Clear", 10))},
                        {"/latest": _report("KFAR", "Light Rain", 60)})
    pt, c = _precip_check(cfg)
    assert c["state"] == "bad" and c["detail"].startswith("KFAR report of 1 h ago"), c
    res = mvp._precip_report(cfg, http.Cache(cfg.cache_dir), _site(cfg), pt["obs_station"], pt["obs"])
    assert res["station"] == "KFAR" and res["source"] is None
    # KFAR clear and newer: the nearest station's own fresh report answers
    fake_http.routes.insert(0, ("/stations/KFAR/observations/latest", json.dumps(_report("KFAR", "Clear", 5)).encode(),
                                True))
    for k in list(os.listdir(cfg.cache_dir)):
        if k.startswith(("obs_", "obshist_")):
            os.unlink(os.path.join(cfg.cache_dir, k))
    pt, c = _precip_check(cfg)
    assert c["state"] == "ok" and c["detail"].startswith("KNEAR report of 10 min ago (its newest with weather)"), c


# ---- S14: a station whose position is unknown is not usable ------------------------------------
def test_a_station_without_a_position_is_not_usable(cfg, cache):
    site = _site(cfg)
    key = "stations/%s" % nws._loc(site)
    obs = {"ok": True, "station_id": "KLOW", "station_name": "low"}
    doc = stations_doc("KLOW", 34.32, -108.10, 1000.0, ("KPT1", 34.40, -108.15, 2300.0))
    doc["features"][0]["geometry"] = None
    cache.put(key, doc)
    info = mvp._station_info(cfg, cache, site, obs)
    assert info["usable"] is False and info["qualifying"] == ["KPT1"]
    assert "position unknown" in info["why"] and "lower than the antenna" in info["why"]
    # not in the list at all; no list at all
    cache.put(key, stations_doc("KPT1", 34.40, -108.15, 2300.0))
    info = mvp._station_info(cfg, cache, site, obs)
    assert info["usable"] is False and "not in NWS's station list" in info["why"]
    cache.put(key, {"nothing": 1})
    info = mvp._station_info(cfg, cache, site, obs)
    assert info["usable"] is False and info["qualifying"] is None and "no station list" in info["why"]


def test_an_unlocated_valley_station_hands_over_to_the_qualifying_one(cfg, fake_http, monkeypatch):
    """Kitt Peak's case with the valley station's geometry missing: its report is not used,
    the qualifying station's rain decides (was: GOOD from the valley station)."""
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result([]))
    _quiet_glm(monkeypatch)
    add_routes(fake_http, cfg)
    doc = stations_doc("KLOW", 34.32, -108.10, 1000.0, ("KPT1", 34.40, -108.15, 2300.0))
    doc["features"][0]["geometry"] = None
    fake_http.routes[:0] = [("gridpoints/ABQ/50,60/stations", json.dumps(doc).encode(), True),
                            ("/stations/KLOW/observations/latest", json.dumps(observation_doc("KLOW")).encode(), True),
                            ("/stations/KPT1/observations/latest",
                             json.dumps(observation_doc("KPT1", text="Light Rain")).encode(), True)]
    pt, c = _precip_check(cfg)
    assert pt["obs"]["station_id"] == "KPT1" and pt["verdict"]["status"] == "BAD"
    assert c["state"] == "bad" and "KPT1" in c["detail"]


# ---- S17: the station list nearest first, odd ids dropped ---------------------------------------
def test_station_rows_are_sorted_by_distance_and_ids_checked(cfg):
    site = _site(cfg)
    doc = stations_doc("KFAR", 34.60, -108.15, 2300.0, ("KNEAR", 34.32, -108.12, 2300.0),
                       ("x/../y?z", 34.30, -108.12, 2300.0), ("KNOPOS", 0, 0, 2300.0))
    doc["features"][3]["geometry"] = None
    rows = mvp._station_rows(cfg, site, doc["features"], 2300.0)
    assert [r["id"] for r in rows] == ["KNEAR", "KFAR", "KNOPOS"]
    assert rows[0]["dist_km"] < rows[1]["dist_km"] and rows[2]["dist_km"] is None


# ---- #82 #111 #149 #72: METARs that cannot show precipitation -----------------------------------
@pytest.mark.parametrize("raw", [
    "KX 121855Z AUTO 00000KT 10SM OVC020 10/09 A2990 RMK AO1",          # no precipitation discriminator
    "KX 121855Z AUTO 00000KT 10SM OVC020 10/09 A2990 RMK AO1A",
    "KX 121855Z AUTO 00000KT 10SM OVC020 10/09 A2990 RMK AO2 PWINO",
    "KX 121834Z NIL",
    "KX 121834Z",
    "KX 081553Z AUTO RMK AO2 $",
    "KX 121834Z AUTO 27005KT 10SM // OVC020 10/09 A2990 RMK AO2",
])
def test_a_metar_that_cannot_show_precipitation_says_nothing(raw):
    rec = {"rawMessage": raw, "textDescription": "Cloudy", "cloudLayers": [{"amount": "OVC"}],
           "presentWeather": [], "precipitationLastHour": {"value": 0}}
    assert decision.metar_blind(raw) and not mvp._reports_weather(rec)
    assert mvp._reports_weather(dict(rec, rawMessage=raw.replace("RMK", "-RA RMK") if "RMK" in raw else raw + " -RA"))


def test_a_raw_metar_alone_needs_a_visibility_or_sky_group():
    bare = {"rawMessage": "", "textDescription": "", "presentWeather": [], "cloudLayers": [],
            "precipitationLastHour": None}
    assert not mvp._reports_weather(dict(bare, rawMessage="KX 121834Z AUTO 27005KT 10/09 A2990 RMK AO2"))
    for raw in ("KX 121834Z AUTO 27005KT 10SM 10/09 A2990 RMK AO2", "KX 121834Z AUTO 27005KT CLR 10/09 A2990",
                "KX 121834Z 27005KT 1 1/2SM BR 10/09 A2990", "KX 121834Z 27005KT 9999 BKN030 10/09 Q1013",
                "KX 121834Z AUTO 27005KT -RA 10/09 A2990 RMK AO2"):
        assert mvp._reports_weather(dict(bare, rawMessage=raw)), raw


@pytest.mark.parametrize("rmk", ["AO1", "AO2 PWINO"])
def test_a_late_nws_report_and_a_blind_metar_is_no_data(cfg, fake_http, monkeypatch, rmk):
    """NWS's report is 2 h 30 min old; the only METAR of the last 2 h cannot show
    precipitation: no data, and the detail says why (was: "no precipitation reported")."""
    _late_nws(cfg, fake_http, monkeypatch, [_metar("KPT1", 20, rmk=rmk)], nws_age=150)
    pt, c = _precip_check(cfg)
    assert c["state"] == "unknown" and pt["verdict"]["incomplete"]
    assert rmk.split()[-1] in c["detail"]


@pytest.mark.parametrize("raw", ["METAR KPT1 %sZ NIL", "METAR KPT1 %sZ AUTO RMK AO2 $"])
def test_a_nil_or_empty_metar_is_no_answer(cfg, fake_http, monkeypatch, raw):
    m = _metar("KPT1", 20)
    m["rawOb"] = raw % _ago(20).strftime("%d%H%M")
    _late_nws(cfg, fake_http, monkeypatch, [m], nws_age=150)
    pt, c = _precip_check(cfg)
    assert c["state"] == "unknown" and pt["verdict"]["incomplete"], c


# ---- #81: blank records of a METAR station are no proof of a station without weather -----------
def _blank_station(cfg, cache, fake_http):
    site = _site(cfg)
    cache.put("obs/%s/KPT1" % site["slug"], _no_weather("KPT1", 10))
    fake_http.add("/stations/KPT1/observations?limit", _history(*[_no_weather("KPT1", 10 + 20 * k) for k in range(4)]))
    return site, {"id": "KPT1", "usable": True, "qualifying": ["KPT1"]}, {"ok": True, "station_id": "KPT1"}


def test_blank_records_and_a_failed_metar_request_are_unknown(cfg, cache, fake_http):
    site, station, obs = _blank_station(cfg, cache, fake_http)
    fake_http.fail("aviationweather.gov/api/data/metar")
    res = mvp._precip_report(cfg, cache, site, station, obs)
    assert res["weather"] == "unknown" and "aviationweather.gov" in res["why"]
    assert res["problem"].startswith("METARs from aviationweather.gov unavailable")


def test_blank_records_and_unusable_metars_of_the_station_are_unknown(cfg, cache, fake_http):
    site, station, obs = _blank_station(cfg, cache, fake_http)
    m = _metar("KPT1", 20, "+RA")
    m["obsTime"] = _iso(_ago(20))                        # its time as text: unreadable
    fake_http.add("aviationweather.gov/api/data/metar", [m])
    res = mvp._precip_report(cfg, cache, site, station, obs)
    assert res["weather"] == "unknown" and res["problem"]
    # METARs of the station that are only too old: a METAR station all the same
    fake_http.routes.insert(0, ("aviationweather.gov/api/data/metar", json.dumps([_metar("KPT1", 150)]).encode(), True))
    res = mvp._precip_report(cfg, cache, site, station, obs)
    assert res["weather"] == "unknown" and "problem" not in res
    # aviationweather.gov's 204 (no METARs of it): a station without weather, n/a
    fake_http.routes.insert(0, ("aviationweather.gov/api/data/metar", b"", True))
    res = mvp._precip_report(cfg, cache, site, station, obs)
    assert res["weather"] == "none"


# ---- S9: a newer report saying the sensor is out ---------------------------------------------------
def test_a_newer_pwino_metar_makes_an_older_clear_unknown(cfg, fake_http, monkeypatch):
    _late_nws(cfg, fake_http, monkeypatch, [_metar("KPT1", 37, rmk="AO2 PWINO"), _metar("KPT1", 97)])
    pt, c = _precip_check(cfg)
    assert c["state"] == "unknown" and pt["verdict"]["incomplete"] and "PWINO" in c["detail"], c


def test_a_newer_pwino_metar_leaves_older_rain_counting(cfg, fake_http, monkeypatch):
    rain = _report("KPT1", "Light Rain", 97, raw="KPT1 101853Z 15005KT 10SM -RA BKN030 31/25 A3001 RMK AO2")
    _late_nws(cfg, fake_http, monkeypatch, [_metar("KPT1", 37, rmk="AO2 PWINO")], nws=rain)
    pt, c = _precip_check(cfg)
    assert c["state"] == "bad" and "KPT1's newer report: present-weather sensor out (PWINO)" in c["detail"], c
    assert "its newest with weather" not in c["detail"]


def test_a_pwino_latest_at_nws_makes_its_older_clear_unknown(cfg, fake_http, monkeypatch):
    """NWS only: the latest report (37 min) says PWINO, its history has "Clear" of 80 min."""
    pw = _report("KNEAR", "Mostly Cloudy", 37, raw="KNEAR 092055Z AUTO 00000KT 10SM BKN060 21/15 A3033 RMK AO2 PWINO")
    _setup_two_stations(cfg, fake_http, monkeypatch, {"/latest": pw, "?limit": _history(pw, _report("KNEAR", "Clear", 80))},
                        {})
    _metars(fake_http, b"")
    pt, c = _precip_check(cfg)
    assert c["state"] == "unknown" and "PWINO" in c["detail"], c


# ---- S11: a METAR is that station's, of that time, in upper case --------------------------------
def test_a_metar_of_another_station_or_time_is_not_read(cfg, fake_http, monkeypatch):
    m = _metar("KPT1", 5)
    for bad in (dict(m, rawOb=m["rawOb"].replace("KPT1", "KXYZ")),
                dict(m, rawOb=m["rawOb"].replace(_ago(5).strftime("%d%H%M"), _ago(3 * 1440).strftime("%d%H%M"))),
                dict(m, rawOb=m["rawOb"].lower()), dict(m, rawOb="METAR KPT1 " + "RMK " * 600)):
        assert mvp._metar_props(bad) is None, bad["rawOb"][:60]
    assert mvp._metar_props(m) is not None
    rain = _report("KPT1", "Light Rain", 97, raw="KPT1 101853Z 15005KT 10SM -RA BKN030 31/25 A3001 RMK AO2")
    _late_nws(cfg, fake_http, monkeypatch, [dict(m, rawOb=m["rawOb"].replace("KPT1", "KXYZ"))], nws=rain)
    pt, c = _precip_check(cfg)
    assert c["state"] == "bad" and pt["verdict"]["status"] == "BAD"


# ---- S7: an odd answer from aviationweather.gov never discards NWS's rain ----------------------
@pytest.mark.parametrize("answer", ["huge time", "deep", "raises"])
def test_an_odd_metar_answer_keeps_nws_rain(cfg, fake_http, monkeypatch, answer):
    rain = _report("KPT1", "Light Rain", 97, raw="KPT1 101853Z 15005KT 10SM -RA BKN030 31/25 A3001 RMK AO2")
    m = _metar("KPT1", 37)
    m["obsTime"] = 10 ** 400
    body = {"huge time": json.dumps([m]).encode(), "deep": b"[" * 200000 + b"]" * 200000, "raises": b"[]"}[answer]
    _late_nws(cfg, fake_http, monkeypatch, body, nws=rain)
    if answer == "raises":
        def boom(cfg, stations, seen=None):
            raise RuntimeError("odd")
        monkeypatch.setattr(mvp, "_metar_reports", boom)
    run = mvp.run_once(cfg)
    pt = run["sites"][0]
    c = [c for c in pt["verdict"]["checks"] if c["key"] == "obs_precip"][0]
    assert c["state"] == "bad" and pt["verdict"]["status"] == "BAD" and not pt["verdict"]["incomplete"], c
    assert not any("precipitation report failed" in e for e in run["errors"]), run["errors"]
    assert any(e.startswith("pt precipitation report: METARs from aviationweather.gov unavailable") for e in run["errors"])


# ---- #85 and S12: the fallback's failures are run errors, naming the host only ----------------------
def test_a_failed_metar_request_is_a_run_error(cfg, fake_http, monkeypatch):
    _late_nws(cfg, fake_http, monkeypatch, None)
    run = mvp.run_once(cfg)
    assert any(e.startswith("pt precipitation report: METARs from aviationweather.gov unavailable: fake: no route")
               for e in run["errors"]), run["errors"]


def test_the_failure_note_names_the_host_only(cfg, fake_http, monkeypatch):
    cfg.metar_url = "https://user:secret%smirror.example.org/metar" % "@"     # credentials in the URL
    _late_nws(cfg, fake_http, monkeypatch, None, nws_age=150)
    fake_http.routes.insert(0, ("mirror.example.org", None, False))
    run = mvp.run_once(cfg)
    pt = run["sites"][0]
    c = [c for c in pt["verdict"]["checks"] if c["key"] == "obs_precip"][0]
    assert c["state"] == "unknown" and "mirror.example.org" in c["detail"], c
    public = c["detail"] + " ".join(run["errors"])
    assert "secret" not in public and "ids=" not in public and "https://" not in public, public


# ---- #79: an overlong raw text costs nothing -------------------------------------------------------
def test_an_overlong_raw_text_is_not_read():
    evil = "KX 121834Z 10SM" + " RMK" * 16000 + " X"
    t0 = time.monotonic()
    assert not decision.metar_pwino(evil) and decision.metar_precipitation(evil) == []
    assert decision.metar_blind(evil) == "raw METAR too long to read"
    assert not mvp._reports_weather({"rawMessage": evil, "textDescription": "Clear"})
    assert mvp._metar_props({"icaoId": "KX", "obsTime": 1794358380, "rawOb": evil}) is None
    # just under the limit: still quick, split once
    near = ("KX 121834Z 10SM" + " RMK" * 495)[:decision.METAR_MAX_LEN]
    assert not decision.metar_pwino(near)
    assert time.monotonic() - t0 < 1.0


# ---- #77 and S20: what counts as precipitation ---------------------------------------------------------
@pytest.mark.parametrize("text, found", [
    ("Thunderstorm in Vicinity Light Rain", True),
    ("Thunderstorm in Vicinity and Light Rain", True),
    ("Thunderstorm in Vicinity Rain and Fog/Mist", True),
    ("Hail", True), ("Light Showers", True), ("Light Rain", True), ("Freezing Drizzle", True),
    ("Showers in Vicinity", False), ("Thunderstorm in Vicinity", False), ("Rain Showers in Vicinity", False),
    ("Blowing Snow in Vicinity", False), ("Fog/Mist", False), ("Clear", False), ("Recent Rain", False),
])
def test_text_precipitation(text, found):
    assert (decision.observed_precipitation({}, text) is not None) is found


def test_present_weather_in_the_vicinity():
    assert decision.observed_precipitation({"present_weather": ["thunderstorms in the vicinity"]}, "") is None
    assert decision.observed_precipitation({"present_weather": ["light rain"]}, "") is not None


@pytest.mark.parametrize("pw", [{"modifier": "showers", "weather": "rain", "inVicinity": True},
                                {"modifier": "thunderstorms", "weather": "rain", "inVicinity": True},
                                {"weather": "showers", "inVicinity": True}])
def test_a_decoded_group_in_the_vicinity_is_no_precipitation(pw):
    """NWS's decoded VCSH ("showers rain in the vicinity" as the check reads it) is one METAR
    group, all of it in the vicinity: no precipitation at the station (#77's phrase rule is
    for descriptions; it read the "showers" left over as rain)."""
    texts = mvp._present_weather_texts({"presentWeather": [dict(pw, intensity=None)]})
    assert decision.observed_precipitation({"present_weather": texts}, "") is None
    assert mvp._precipitation_in({"presentWeather": [dict(pw, intensity=None)], "textDescription": ""}) is None


@pytest.mark.parametrize("group", ["-DZ", "DZ", "+RA", "-SN", "SG", "IC", "PL", "GR", "GS", "UP", "FZRA", "-FZDZ",
                                   "-RASN", "+TSRA", "SHGS", "TS", "BLSN", "MISN", "-SHRASN"])
def test_every_metar_precipitation_code_counts(group):
    metar = "KONM 081553Z 27005KT 10SM %s OVC020 05/03 A3000 RMK AO2" % group
    assert decision.metar_precipitation(metar) == [group]
    assert decision.observed_precipitation({"metar": metar}, "") == "METAR reports %s" % group


@pytest.mark.parametrize("metar", ["KONM 081553Z 27005KT 10SM VCSH OVC020 05/03 A3000 RMK AO2",
                                   "KONM 081553Z 27005KT 10SM BR OVC020 05/03 A3000 RMK AO2 TSB20E40",
                                   "KONM 081553Z 27005KT 10SM OVC020 05/03 A3000 RMK AO2 TS OHD",
                                   "KONM 081553Z 27005KT 10SM DRSN OVC020 05/03 A3000 RMK AO2"])
def test_no_precipitation_in_a_metar(metar):
    assert decision.metar_precipitation(metar) == []


def test_an_fzra_metar_from_aviationweather_makes_the_verdict_bad(cfg, fake_http, monkeypatch):
    _late_nws(cfg, fake_http, monkeypatch, [_metar("KPT1", 20, "FZRA")])
    pt, c = _precip_check(cfg)
    assert c["state"] == "bad" and "METAR reports FZRA" in c["detail"] and pt["verdict"]["status"] == "BAD"


# ---- S10: the source comes from the report's pick, not from an upstream property -----------------
def test_an_nws_source_property_is_not_a_metar(cfg, fake_http, monkeypatch):
    nws_rep = _report("KPT1", "Clear", 97, raw="KPT1 101853Z 15005KT 10SM CLR 31/25 A3001 RMK AO2")
    nws_rep["properties"]["source"] = "anything"
    _late_nws(cfg, fake_http, monkeypatch, b"", nws=nws_rep)
    pt, c = _precip_check(cfg)
    assert c["state"] == "ok" and c["detail"].startswith("KPT1 report of 1 h 37 min ago (its newest with weather)"), c
    assert pt["obs_raw"].get("report_source") is None


# ---- S15: an unparsable record costs that station only -------------------------------------------
def test_an_unparsable_latest_report_skips_the_station(cfg, cache, fake_http, monkeypatch):
    site = _site(cfg)
    fake_http.add("/stations/KBAD/observations/latest", _report("KBAD", "Clear", 10))
    fake_http.add("/stations/KGOOD/observations/latest", _report("KGOOD", "Clear", 10))
    parse = nws._parse_observation

    def odd(props):
        if props.get("stationId") == "KBAD":
            raise ValueError("cannot convert float NaN to integer")
        return parse(props)
    monkeypatch.setattr(nws, "_parse_observation", odd)
    assert mvp._latest_report(cfg, cache, site, "KBAD") == (None, None)
    got = mvp._qualifying_observation(cfg, cache, site, {"qualifying": ["KBAD", "KGOOD"]})
    assert got is not None and got["station_id"] == "KGOOD"


# ---- #156: a time ahead of the clock is not "just now" ------------------------------------------------
def test_ago_ahead_of_the_clock():
    assert decision._ago(-20, " %s ago") == ", time ahead of the clock"
    assert decision._ago(-5, " %s ago") == " just now"
    assert decision._ago(30, " %s ago") == " 30 min ago"
    assert decision._ago(None, " %s ago") == ""


def test_an_observation_ahead_of_the_clock_says_so(cfg):
    entry = {"obs": {"ok": True, "station_id": "KPT1", "age_min": -120.0, "rh": 40.0, "text": "Clear"},
             "obs_station": {"id": "KPT1", "usable": True, "qualifying": ["KPT1"], "weather": "reported"},
             "obs_raw": {"present_weather": [], "text": "Clear", "reports_weather": True}}
    checks, _ = decision.check_observation(cfg, entry, util.utcnow())
    precip = [c for c in checks if c["key"] == "obs_precip"][0]
    assert precip["detail"].startswith("KPT1, time ahead of the clock: "), precip


# ---- S13: the doubt-clearing rule, the future bound, staleness and the request's limits -------------
def _doubt_setup(cfg, fake_http, monkeypatch, metars, blank_at=None):
    t = blank_at or _ago(37).replace(microsecond=0)
    blank = _no_weather("KPT1", 37)
    blank["properties"]["timestamp"] = _iso(t)
    _late_nws(cfg, fake_http, monkeypatch, metars(t), nws=blank)
    fake_http.routes.insert(0, ("/stations/KPT1/observations?limit", None, False))


def test_an_older_metar_keeps_the_doubt(cfg, fake_http, monkeypatch):
    """KPT1's blank NWS record is 10 min old and its history unreadable; its METAR is 50 min old:
    the history may hold something newer (t_q >= ut)."""
    _doubt_setup(cfg, fake_http, monkeypatch, lambda t: [_metar("KPT1", 50)], blank_at=_ago(10).replace(microsecond=0))
    pt, c = _precip_check(cfg)
    assert c["state"] == "unknown" and "maybe newer" in c["detail"], c


def test_another_stations_metar_keeps_the_doubt(cfg, fake_http, monkeypatch):
    """KNEAR's blank latest (10 min) and unreadable history; KFAR's METAR of the same minute
    answers for KFAR, not for KNEAR's history (u == q)."""
    t = _ago(10).replace(second=0, microsecond=0)
    blank = _no_weather("KNEAR", 10)
    blank["properties"]["timestamp"] = _iso(t)
    _setup_two_stations(cfg, fake_http, monkeypatch, {"/latest": blank}, {"/latest": _report("KFAR", "Clear", 150)})
    fake_http.routes.insert(0, ("/stations/KNEAR/observations?limit", None, False))
    _metars(fake_http, [_metar("KFAR", 0, at=t)])
    pt, c = _precip_check(cfg)
    assert c["state"] == "unknown" and "KNEAR, maybe newer" in c["detail"], c


def test_an_unread_latest_keeps_the_doubt(cfg, cache, fake_http, monkeypatch):
    """KFAR's latest cannot be read at all (its time unknown, ut None), nor its history: a
    METAR of KFAR does not clear that doubt."""
    site = _site(cfg)
    cache.put("obs/%s/KNEAR" % site["slug"], _report("KNEAR", "Clear", 150))
    fake_http.fail("/stations/KFAR/observations")
    fake_http.add("aviationweather.gov/api/data/metar", [_metar("KFAR", 30), _metar("KNEAR", 150)])
    res = mvp._precip_report(cfg, cache, site, {"id": "KNEAR", "usable": True, "qualifying": ["KNEAR", "KFAR"]},
                             {"ok": True, "station_id": "KNEAR"})
    assert res["station"] == "KFAR" and res["doubt"] and "KFAR" in res["doubt"], res


def test_a_metar_stamped_ahead_of_the_clock_is_ignored(cfg, fake_http, monkeypatch):
    """A clear METAR 30 min ahead of this host's clock does not override NWS's rain of 97 min."""
    rain = _report("KPT1", "Light Rain", 97, raw="KPT1 101853Z 15005KT 10SM -RA BKN030 31/25 A3001 RMK AO2")
    _late_nws(cfg, fake_http, monkeypatch, [_metar("KPT1", -30)], nws=rain)
    pt, c = _precip_check(cfg)
    assert c["state"] == "bad" and pt["obs_raw"].get("report_source") is None, c


def test_a_metar_pick_is_never_stale(cfg, cache, fake_http):
    site = _site(cfg)
    cache.put("obs/%s/KPT1" % site["slug"], _report("KPT1", "Clear", 150))
    fake_http.add("aviationweather.gov/api/data/metar", [_metar("KPT1", 20)])
    res = mvp._precip_report(cfg, cache, site, {"id": "KPT1", "usable": True, "qualifying": ["KPT1"]},
                             {"ok": True, "station_id": "KPT1", "stale": True})
    assert res["source"] == "aviationweather.gov" and res["stale"] is False


def test_the_metar_request_is_short(cfg, monkeypatch):
    seen = {}

    def get_bytes(url, cfg, accept=None, timeout=None, retries=None, method="GET"):
        seen.update(url=url, timeout=timeout, retries=retries)
        return b""
    monkeypatch.setattr(http, "get_bytes", get_bytes)
    assert mvp._metar_reports(cfg, ["KPT1"]) == ({}, None)
    assert seen["timeout"] == mvp.METAR_TIMEOUT_S == 15 and seen["retries"] == 1
    assert seen["url"].endswith("?ids=KPT1&format=json&hours=2")


# ---- S27: report selection ------------------------------------------------------------------------
def test_history_newer_rain_beats_older_clear(cfg, cache, fake_http):
    site = _site(cfg)
    fake_http.add("/stations/KX/observations?limit",
                  _history(_report("KX", minutes_ago=5), _report("KX", "Light Rain", 30), _report("KX", "Clear", 100)))
    h = mvp._weather_history(cfg, cache, site, "KX")
    assert h["report"]["textDescription"] == "Light Rain" and [p["textDescription"] for p in h["reports"]] == [
        "Light Rain", "Clear"]


def test_mixed_stations_one_without_weather_one_unreadable_are_unknown(cfg, cache, fake_http):
    """KNEAR never reports weather, KFAR's history cannot be read: not n/a (all, not any)."""
    site = _site(cfg)
    cache.put("obs/%s/KNEAR" % site["slug"], _report("KNEAR", minutes_ago=10))
    fake_http.add("/stations/KNEAR/observations?limit", _history(*[_report("KNEAR", minutes_ago=10 + 60 * k)
                                                                   for k in range(12)]))
    fake_http.add("/stations/KFAR/observations/latest", _report("KFAR", minutes_ago=10))
    fake_http.fail("/stations/KFAR/observations?limit")
    fake_http.add("aviationweather.gov/api/data/metar", b"")
    res = mvp._precip_report(cfg, cache, site, {"id": "KNEAR", "usable": True, "qualifying": ["KNEAR", "KFAR"]},
                             {"ok": True, "station_id": "KNEAR"})
    assert res["weather"] == "unknown"


# ---- review: the repairs of the review ---------------------------------------------------------
@pytest.mark.parametrize("near", [
    {"/latest": _report("KNEAR", "Light Rain", 97)},                       # NWS serves KNEAR late
    {"/latest": _report("KNEAR", minutes_ago=5),                           # KNEAR's records blank (TISX)
     "?limit": _history(_report("KNEAR", minutes_ago=5), _report("KNEAR", minutes_ago=65),
                        _report("KNEAR", "Clear", 125))},
], ids=["late", "blank"])
def test_the_nearest_stations_metar_is_asked_when_a_farther_one_is_fresh_at_nws(cfg, fake_http, monkeypatch, near):
    """S6 by another route: KFAR's "Clear" at NWS is fresh, so NWS has a report of the last 90
    min, but none of KNEAR's: KNEAR's METAR with rain must still be read (was: GOOD,
    complete, aviationweather.gov never asked)."""
    _setup_two_stations(cfg, fake_http, monkeypatch, near, {"/latest": _report("KFAR", "Clear", 30)})
    _metars(fake_http, [_metar("KNEAR", 20, "-RA"), _metar("KFAR", 30)])
    pt, c = _precip_check(cfg)
    assert c["state"] == "bad" and c["detail"].startswith("KNEAR METAR of 20 min ago from aviationweather.gov"), c


def test_a_nearest_station_without_weather_does_not_send_the_check_to_aviationweather(cfg, fake_http, monkeypatch):
    """Brewster's case: CJDW (here KNEAR) never reports weather, KOMK's (KFAR's) fresh report
    answers; aviationweather.gov is not asked for it. And a METAR of the nearest station
    answers before a farther station's report at NWS when NWS is late with it."""
    _setup_two_stations(cfg, fake_http, monkeypatch,
                        {"/latest": _report("KNEAR", minutes_ago=5),
                         "?limit": _history(*[_report("KNEAR", minutes_ago=5 + 60 * k) for k in range(4)])},
                        {"/latest": _report("KFAR", "Clear", 30)})
    pt, c = _precip_check(cfg)
    assert c["state"] == "ok" and c["detail"].startswith("KFAR report of 30 min ago"), c
    assert not [u for u in fake_http.calls if "aviationweather.gov" in u]


def test_the_nearest_stations_clear_metar_answers_before_a_farther_report(cfg, fake_http, monkeypatch):
    _setup_two_stations(cfg, fake_http, monkeypatch, {"/latest": _report("KNEAR", "Light Rain", 97)},
                        {"/latest": _report("KFAR", "Clear", 30)})
    _metars(fake_http, [_metar("KNEAR", 40)])
    pt, c = _precip_check(cfg)
    assert c["state"] == "ok" and c["detail"].startswith("KNEAR METAR of 40 min ago"), c


def test_a_station_id_with_a_line_break_is_dropped(cfg):
    doc = stations_doc("KPT1\n", 34.30, -108.12, 2300.0, ("KNEAR", 34.32, -108.12, 2300.0))
    assert [r["id"] for r in mvp._station_rows(cfg, _site(cfg), doc["features"], 2300.0)] == ["KNEAR"]


def test_an_overlong_text_is_read_whole(monkeypatch):
    """#79's cost on the text: a description longer than any station's is not searched for
    vicinity phrases (megabytes of it took seconds per read); its precipitation words still
    count."""
    class Boom:
        def sub(self, *a):
            raise AssertionError("vicinity phrases searched in an overlong text")
    monkeypatch.setattr(decision, "_VICINITY", Boom())
    long = "Light Rain " + "x" * decision.METAR_MAX_LEN
    assert decision.observed_precipitation({"present_weather": [long]}, "") == "present weather: %s" % long
    assert decision.observed_precipitation({}, "Fog " + "x" * decision.METAR_MAX_LEN) is None


def test_a_pwino_metar_above_a_rain_metar_is_named_once(cfg, fake_http, monkeypatch):
    """S9's detail: the station's newer PWINO METAR is said once, not as "newest" and "newer"."""
    _late_nws(cfg, fake_http, monkeypatch, [_metar("KPT1", 10, rmk="AO2 PWINO"), _metar("KPT1", 40, "-RA")])
    pt, c = _precip_check(cfg)
    assert c["state"] == "bad" and c["detail"] == (
        "KPT1 METAR of 40 min ago from aviationweather.gov (KPT1's newest report: present-weather sensor out "
        "(PWINO)): METAR reports -RA"), c


def test_a_fresh_blank_latest_older_than_a_farther_clear_still_has_its_history_read(cfg, fake_http, monkeypatch):
    """S6: KNEAR's blank latest (40 min) is older than KFAR's "Clear" (15 min), but its history
    can hold rain of the last 90 min, which counts: it is read (it was skipped)."""
    _setup_two_stations(cfg, fake_http, monkeypatch,
                        {"/latest": _report("KNEAR", minutes_ago=40),
                         "?limit": _history(_report("KNEAR", minutes_ago=40), _report("KNEAR", "Light Rain", 50))},
                        {"/latest": _report("KFAR", "Clear", 15)})
    _metars(fake_http, b"")
    pt, c = _precip_check(cfg)
    assert c["state"] == "bad" and c["detail"].startswith("KNEAR report of 50 min ago"), c


def test_an_unread_history_that_may_hold_fresh_rain_is_a_doubt(cfg, fake_http, monkeypatch):
    """KNEAR's blank latest is 40 min old, its history cannot be read, aviationweather.gov has
    no METAR of it: KFAR's newer "Clear" is no answer (KNEAR may have reported rain since
    1 h 30 min ago)."""
    _setup_two_stations(cfg, fake_http, monkeypatch, {"/latest": _report("KNEAR", minutes_ago=40)},
                        {"/latest": _report("KFAR", "Clear", 15)})
    fake_http.routes.insert(0, ("/stations/KNEAR/observations?limit", None, False))
    _metars(fake_http, b"")
    pt, c = _precip_check(cfg)
    assert c["state"] == "unknown" and "KNEAR, maybe newer, could not be read" in c["detail"], c
