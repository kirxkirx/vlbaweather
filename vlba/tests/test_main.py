"""End to end: one run of make_vlba_page with every source mocked, for a CONUS antenna
(Pie Town: MRMS frame, GLM flash in the ring) and an island antenna (Mauna Kea: the NWS
composite); the verdicts, the state file, the page and status.json; a run with every
source dead; the CLI and its exit codes."""
from __future__ import annotations

import io
import json
import os
import sys
from datetime import timedelta, timezone

import pytest
from PIL import Image

from weather import geo, http, util
import make_vlba_page as mvp                     # noqa: E402  (conftest put vlbaweather on sys.path)
from vlba import glm
from vlba.tests.conftest import MK, PT, grib2_around

UTC = timezone.utc
TILE_RGB = (40, 44, 52)
FUTURE = "2099-10-08T12:00:00+00:00"


def _q(unit, value):
    return {"unitCode": "wmoUnit:" + unit, "value": value}


def _iso(dt):
    return dt.isoformat()


def points_doc(grid, gx, gy, tz, zones, radar_station):
    return {"properties": {
        "gridId": grid, "gridX": gx, "gridY": gy, "cwa": grid,
        "forecast": "https://api.weather.gov/gridpoints/%s/%d,%d/forecast" % (grid, gx, gy),
        "forecastHourly": "https://api.weather.gov/gridpoints/%s/%d,%d/forecast/hourly" % (grid, gx, gy),
        "observationStations": "https://api.weather.gov/gridpoints/%s/%d,%d/stations" % (grid, gx, gy),
        "relativeLocation": {"properties": {"city": "x", "state": zones[0][:2]}},
        "timeZone": tz, "radarStation": radar_station,
        "forecastZone": "https://api.weather.gov/zones/forecast/" + zones[0],
        "county": "https://api.weather.gov/zones/county/" + zones[1],
        "fireWeatherZone": "https://api.weather.gov/zones/fire/" + zones[2]}}


def stations_doc(sid, lat, lon, elev, *more):
    """The station list of a grid cell, nearest first: (sid, lat, lon, elev) and as many
    more such tuples as given."""
    rows = [(sid, lat, lon, elev)] + list(more)
    return {"features": [{"id": "https://api.weather.gov/stations/" + r[0],
                          "geometry": {"type": "Point", "coordinates": [r[2], r[1]]},
                          "properties": {"stationIdentifier": r[0], "name": r[0] + " airport",
                                         "elevation": _q("m", r[3])}} for r in rows],
            "observationStations": ["https://api.weather.gov/stations/" + r[0] for r in rows]}


def observation_doc(sid, text="Clear", rh=40.0, present=()):
    ts = util.utcnow() - timedelta(minutes=15)
    return {"properties": {
        "station": "https://api.weather.gov/stations/" + sid, "stationId": sid, "stationName": sid,
        "timestamp": _iso(ts), "textDescription": text, "icon": None, "temperature": _q("degC", 15.7),
        "dewpoint": _q("degC", 5.1), "windDirection": _q("degree_(angle)", 350), "windSpeed": _q("km_h-1", 16.56),
        "windGust": _q("km_h-1", None), "barometricPressure": _q("Pa", 102340), "seaLevelPressure": _q("Pa", None),
        "visibility": _q("m", 16090), "relativeHumidity": _q("percent", rh), "windChill": _q("degC", None),
        "heatIndex": _q("degC", None), "cloudLayers": [], "precipitationLastHour": _q("mm", 0),
        "presentWeather": [{"intensity": i, "modifier": None, "weather": w, "rawString": "", "inVicinity": False}
                           for i, w in present]}}


def forecast_doc():
    now = util.utcnow()
    return {"properties": {"updateTime": _iso(now - timedelta(hours=1)), "periods": [
        {"number": 1, "name": "Today", "startTime": _iso(now), "endTime": _iso(now + timedelta(hours=8)),
         "isDaytime": True, "temperature": 69, "temperatureUnit": "F", "temperatureTrend": None,
         "probabilityOfPrecipitation": _q("percent", 5), "windSpeed": "5 to 10 mph", "windDirection": "SE",
         "icon": None, "shortForecast": "Sunny", "detailedForecast": "Sunny."}]}}


def hourly_doc():
    start = util.utcnow().replace(minute=0, second=0, microsecond=0)
    periods = []
    for i in range(0, 5):
        t = start + timedelta(hours=i)
        periods.append({"number": i + 1, "startTime": _iso(t), "endTime": _iso(t + timedelta(hours=1)),
                        "isDaytime": True, "temperature": 64 + i, "temperatureUnit": "F",
                        "probabilityOfPrecipitation": _q("percent", 5), "dewpoint": _q("degC", 5.5),
                        "relativeHumidity": _q("percent", 40), "windSpeed": "10 mph", "windDirection": "E",
                        "icon": None, "shortForecast": "Sunny"})
    return {"properties": {"updateTime": _iso(start), "periods": periods}}


def gridpoint_doc(sky=20, pop=5, thunder=0, snow=0, with_thunder=True):
    start = util.utcnow().replace(minute=0, second=0, microsecond=0)

    def series(uom, value, hours=12):
        return {"uom": uom, "values": [{"validTime": "%s/PT%dH" % (_iso(start), hours), "value": value}]}
    props = {"updateTime": _iso(util.utcnow() - timedelta(minutes=20)), "elevation": _q("m", 2365.0),
             "skyCover": series("wmoUnit:percent", sky), "probabilityOfPrecipitation": series("wmoUnit:percent", pop),
             "quantitativePrecipitation": series("wmoUnit:mm", 0), "snowfallAmount": series("wmoUnit:mm", snow),
             "iceAccumulation": series("wmoUnit:mm", 0), "windSpeed": series("wmoUnit:km_h-1", 20),
             "windGust": series("wmoUnit:km_h-1", 30), "temperature": series("wmoUnit:degC", 10),
             "relativeHumidity": series("wmoUnit:percent", 40),
             "weather": {"values": [{"validTime": "%s/PT12H" % _iso(start),
                                     "value": [{"coverage": None, "weather": None, "intensity": None}]}]}}
    props["probabilityOfThunder"] = series("wmoUnit:percent", thunder) if with_thunder else {"values": []}
    return {"properties": props}


def alert_feature(fid, event, geometry=None, zones=(), ends=FUTURE, onset=None):
    return {"id": "https://api.weather.gov/alerts/" + fid, "type": "Feature",
            "geometry": None if geometry is None else {"type": "Polygon", "coordinates": [geometry]},
            "properties": {"id": fid, "areaDesc": "Catron, NM", "affectedZones": list(zones),
                           "sent": "2026-10-08T09:08:00-06:00", "effective": "2026-10-08T09:08:00-06:00",
                           "onset": onset or "2026-10-08T09:08:00-06:00", "expires": ends, "ends": ends,
                           "status": "Actual", "messageType": "Alert", "severity": "Severe",
                           "certainty": "Likely", "urgency": "Expected", "event": event,
                           "senderName": "NWS Albuquerque NM", "headline": "%s issued" % event,
                           "description": "Details.", "instruction": "Take care.", "web": "https://www.weather.gov"}}


def _png(mode="RGB", size=(256, 256), color=TILE_RGB):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


def frame_png(cells=()):
    """A 7000x3500 MRMS frame with 30 dBZ cells at the given (lat, lon)s."""
    img = Image.new("P", (geo.MRMS_W, geo.MRMS_H), geo.MRMS_NODATA)
    img.putpalette([v for i in range(256) for v in (i, i, i)])
    for lat, lon in cells:
        c, r = geo.mrms_cell(lat, lon)
        img.paste(124, (c - 3, r - 3, c + 4, r + 4))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def frame_ts():
    """The synthetic MRMS frame is 4 min old at run time: fresh for the radar check."""
    return (util.utcnow() - timedelta(minutes=4)).replace(second=0, microsecond=0)


def _mrms_result(cells=()):
    data = frame_png(cells)
    img = Image.open(io.BytesIO(data))
    img.load()
    return {"ok": True, "stale": False, "error": None, "age_s": 0.0, "ts": frame_ts(), "url": "https://iem/x.png",
            "image": img}


def island_png(frame, blobs=()):
    img = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    for lat, lon, rgb in blobs:
        x, y = frame.lonlat_to_px(lon, lat)
        img.paste(tuple(rgb) + (255,), (int(x) - 3, int(y) - 3, int(x) + 4, int(y) + 4))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def island_ts():
    return (util.utcnow() - timedelta(minutes=3)).replace(microsecond=0)


def caps_doc():
    return ('<WMS_Capabilities><Layer><Name>hawaii_bref_qcd</Name>'
            '<Dimension name="time" default="%s" units="ISO8601"/></Layer></WMS_Capabilities>'
            % island_ts().strftime("%Y-%m-%dT%H:%M:%SZ"))


def _glm_result(flashes):
    now = util.utcnow()
    return {"ok": True, "stale": False, "error": None, "age_s": 0.0, "window_start": now - timedelta(minutes=5),
            "window_end": now, "flashes": [{"lat": la, "lon": lo, "sat": sat, "t": now} for la, lo, sat in flashes],
            "sats": {"G19": {"bucket": "noaa-goes19", "sub_lon": -75.2, "ok": True, "error": None, "listed": 100,
                             "selected": 15, "fetched": 15, "failed": 0, "flashes": len(flashes)},
                     "G18": {"bucket": "noaa-goes18", "sub_lon": -137.0, "ok": True, "error": None, "listed": 100,
                             "selected": 15, "fetched": 15, "failed": 0, "flashes": 0}},
            "deps": True, "elapsed_s": 1.0, "skipped": None, "partial": False,
            "newest": {"G19": now.timestamp() - 40.0, "G18": now.timestamp() - 40.0}, "retry": {}, "seen": {}}


def add_routes(fake_http, cfg, pt_obs=None, pt_grid=None, alerts=None, mk_blobs=(), pt_holes=(), mk_holes=()):
    """Both antennas' NWS chains, the MRMS frame, the tiles, the Hawaii composite, and
    MRMS's own files of the two frames for the radar coverage (full unless ``*_holes``)."""
    fake_http.add("/points/34.3010,-108.1192", points_doc("ABQ", 50, 60, "America/Denver",
                                                          ("NMZ226", "NMC003", "NMZ106"), "KABX"))
    fake_http.add("gridpoints/ABQ/50,60/forecast/hourly", hourly_doc())
    fake_http.add("gridpoints/ABQ/50,60/forecast", forecast_doc())
    fake_http.add("gridpoints/ABQ/50,60/stations", stations_doc("KPT1", 34.32, -108.10, 2300.0))
    fake_http.add("gridpoints/ABQ/50,60", pt_grid or gridpoint_doc())
    fake_http.add("/stations/KPT1/observations/latest", pt_obs or observation_doc("KPT1"))
    fake_http.add("/points/19.8014,-155.4555", points_doc("HFO", 255, 77, "Pacific/Honolulu",
                                                          ("HIZ028", "HIC001", "HIZ343"), "PHKM"))
    fake_http.add("gridpoints/HFO/255,77/forecast/hourly", hourly_doc())
    fake_http.add("gridpoints/HFO/255,77/forecast", forecast_doc())
    fake_http.add("gridpoints/HFO/255,77/stations", stations_doc("PHSF", 19.76, -155.55, 1871.0))
    fake_http.add("gridpoints/HFO/255,77", gridpoint_doc(with_thunder=False))
    fake_http.add("/stations/PHSF/observations/latest", observation_doc("PHSF", text="Rain", rh=99.0))
    fake_http.add("/alerts/active?area=", alerts if alerts is not None else {"type": "FeatureCollection", "features": []})
    fake_http.add("openstreetmap", _png())
    fake_http.add("GetCapabilities", caps_doc())
    mk_frame = geo.MapFrame(MK["lat"], MK["lon"], cfg.map_km, cfg.map_px, cfg.tile_zoom)
    fake_http.add("GetMap", island_png(mk_frame, mk_blobs))
    fake_http.add("CONUS/SeamlessHSR_00.00/", grib2_around(PT["lat"], PT["lon"], frame_ts(), holes=pt_holes))
    fake_http.add("HAWAII/MergedBaseReflectivityQC_00.50/",
                  grib2_around(MK["lat"], MK["lon"], island_ts(), holes=mk_holes, cell_deg=0.005))


def read_status(cfg):
    with open(os.path.join(cfg.out_dir, "status.json"), encoding="utf-8") as f:
        return json.load(f)


def read_page(cfg):
    with open(os.path.join(cfg.out_dir, "index.html"), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def mrms(monkeypatch):
    """The MRMS frame step answers with a synthetic frame (30 dBZ 10 km north of Pie Town)."""
    cells = [(PT["lat"] + 0.09, PT["lon"])]
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result(cells))


@pytest.fixture
def glm_flash_pt(monkeypatch):
    flash = geo.dest_point(PT["lat"], PT["lon"], 20.0, 45.0)
    monkeypatch.setattr(glm, "deps_available", lambda: True)
    monkeypatch.setattr(glm, "fetch", lambda cfg, now=None, since=None, retried=None, seen=None: _glm_result([(flash[0], flash[1], "G19")]))


def test_full_run_two_antennas(cfg, fake_http, mrms, glm_flash_pt, monkeypatch):
    add_routes(fake_http, cfg)
    run = mvp.run_once(cfg)
    assert [s["slug"] for s in run["sites"]] == ["pt", "mk"]
    pt, mk = run["sites"]
    # Pie Town: MRMS echo in the ring (unconfirmed on the first run), GLM flash within 50 km
    assert pt["radar_check"]["in_ring"] and pt["radar_check"]["source"] == "MRMS"
    assert pt["glm"]["available"] and pt["glm"]["count_window"] == 1 and pt["glm"]["sats"] == ["G19", "G18"]
    assert pt["verdict"]["status"] == "BAD" and any("lightning" in r for r in pt["verdict"]["reasons"])
    states = {c["key"]: c["state"] for c in pt["verdict"]["checks"]}
    assert states["radar"] == "watch" and states["glm"] == "bad" and states["obs_precip"] == "ok"
    assert pt["obs_station"]["usable"] and 0 < pt["obs_station"]["dist_km"] < 10 and pt["obs_station"]["delev_m"] == -65.0
    assert pt["sun_up"] in (True, False) and "island_radar" not in pt
    # Mauna Kea: the NWS composite, the station too low to count, no thunder field (not provided)
    assert mk["island_radar"]["ok"] and mk["island_radar"]["label"] == "NWS Hawaii composite"
    assert mk["radar_check"]["source"] == "NWS Hawaii composite" and mk["radar_check"]["available"]
    assert not mk["radar_check"]["in_ring"]
    assert mk["obs_station"]["usable"] is False and "lower" in mk["obs_station"]["why"]
    # no station of the list suits the antenna: the observation checks do not apply (n/a,
    # every run), which is not missing data: the verdict is complete, no "GOOD?"
    assert mk["obs_station"]["qualifying"] == []
    mk_states = {c["key"]: c["state"] for c in mk["verdict"]["checks"]}
    assert mk_states["obs_precip"] == "not provided" and mk_states["forecast_thunder"] == "not provided"
    assert mk_states["wind"] == "ok"                      # the forecast gust alone answers it
    assert mk["verdict"]["status"] == "GOOD" and not mk["verdict"]["incomplete"]
    assert mk["glm"]["covered"] and mk["glm"]["sats"] == ["G18"] and mk["glm"]["count_window"] == 0
    assert run["summary"] == {"good": 1, "bad": 1, "no_data": 0, "incomplete": 0}
    # outputs
    for slug in ("pt", "mk"):
        for theme in ("dark", "light"):
            p = os.path.join(cfg.out_dir, cfg.map_basename(slug, theme))
            assert os.path.exists(p) and Image.open(p).format == "PNG"
    html = read_page(cfg)
    assert "could not be rendered" not in html
    assert "NWS Hawaii composite %s" % island_ts().strftime("%H:%MZ") in html
    assert "MRMS frame %s" % frame_ts().strftime("%H:%MZ") in html
    assert 'class="vbadge bad sm">BAD' in html and ">GOOD?</span>" not in html
    st = read_status(cfg)
    assert st["verdicts"]["pt"]["status"] == "BAD" and st["verdicts"]["mk"]["checks"]["forecast_thunder"] == "not provided"
    assert st["sites"]["mk"]["island_radar_ok"] is True
    # the state file holds the streak and the GLM hold
    with open(cfg.state_file, encoding="utf-8") as f:
        state = json.load(f)
    assert state["sites"]["pt"]["radar_streak"] == 1 and state["sites"]["pt"]["glm_last_hit_ts"] > 0
    assert set(state["glm_newest"]) == {"G19", "G18"}          # where the next run's GLM window starts
    # a second run that gets the same frame (IEM has nothing newer): no new evidence, the
    # echo stays unconfirmed; a newer frame with the echo confirms it
    run2 = mvp.run_once(cfg)
    radar2 = [c for c in run2["sites"][0]["verdict"]["checks"] if c["key"] == "radar"][0]
    assert radar2["state"] == "watch" and "not counted again" in radar2["detail"]
    newer = dict(_mrms_result([(PT["lat"] + 0.09, PT["lon"])]), ts=frame_ts() + timedelta(minutes=2))
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: newer)
    run3 = mvp.run_once(cfg)
    pt3 = run3["sites"][0]
    assert {c["key"]: c["state"] for c in pt3["verdict"]["checks"]}["radar"] == "bad"
    assert any("rain on radar" in r for r in pt3["verdict"]["reasons"])
    # no network call aimed at a road source or an MRMS frame from here
    assert not any("nmroads" in c or "lsr.geojson" in c for c in fake_http.calls)


def test_observation_from_the_nearest_station_that_suits_the_antenna(cfg, fake_http, monkeypatch):
    """Kitt Peak's case: the nearest station with a report lies far below the antenna, the
    next one in NWS's list is close and level enough: its report decides, and it is the one
    the page shows."""
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result([]))
    monkeypatch.setattr(glm, "fetch", lambda cfg, now=None, since=None, retried=None, seen=None: _glm_result([]))
    monkeypatch.setattr(glm, "deps_available", lambda: True)
    add_routes(fake_http, cfg)

    def first(fragment, body):          # FakeHttp answers with the first matching route
        fake_http.add(fragment, body)
        fake_http.routes.insert(0, fake_http.routes.pop())
    first("gridpoints/ABQ/50,60/stations", stations_doc(
        "KLOW", 34.32, -108.10, 1000.0, ("KPT1", 34.40, -108.15, 2300.0)))
    first("/stations/KLOW/observations/latest", observation_doc("KLOW"))
    first("/stations/KPT1/observations/latest", observation_doc("KPT1", text="Light Rain"))
    run = mvp.run_once(cfg)
    pt = run["sites"][0]
    assert pt["obs"]["station_id"] == "KPT1" and pt["obs_station"]["id"] == "KPT1"
    assert pt["obs_station"]["usable"] and pt["obs_station"]["qualifying"] == ["KPT1"]
    assert pt["verdict"]["status"] == "BAD"
    assert any("precipitation at KPT1" in r for r in pt["verdict"]["reasons"])
    html = read_page(cfg)
    assert "Station KPT1:" in html and "used for the verdict" in html
    # without a report from the qualifying station the checks have no data (not n/a)
    first("/stations/KPT1/observations/latest", {"properties": {"stationId": "KPT1"}})
    for k in list(os.listdir(cfg.cache_dir)):
        if k.startswith("obs_"):
            os.unlink(os.path.join(cfg.cache_dir, k))
    pt = mvp.run_once(cfg)["sites"][0]
    states = {c["key"]: c["state"] for c in pt["verdict"]["checks"]}
    assert pt["obs"]["station_id"] == "KLOW" and pt["obs_station"]["usable"] is False
    assert states["obs_precip"] == "unknown" and pt["verdict"]["incomplete"]


def _no_weather(sid, minutes_ago=15):
    """A report without weather: what a mesonet / RAWS station always sends, and what a METAR
    station's record sometimes is."""
    doc = observation_doc(sid, text="")
    doc["properties"].update(presentWeather=[], precipitationLastHour=_q("mm", None), rawMessage="",
                             timestamp=_iso(util.utcnow() - timedelta(minutes=minutes_ago)))
    return doc


def _history(*reports):
    """/stations/{id}/observations?limit=...: the given reports, newest first."""
    return {"type": "FeatureCollection", "features": [{"properties": r["properties"]} for r in reports]}


def test_observation_from_a_station_that_reports_weather(cfg, fake_http, monkeypatch):
    """Brewster's case: the nearest qualifying station reports no weather (CJDW), a farther
    METAR station does (KOMK): its report decides. Kitt Peak's case: no qualifying station
    reports weather: "precipitation observed" cannot apply there (n/a, never "clear")."""
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result([]))
    monkeypatch.setattr(glm, "fetch", lambda cfg, now=None, since=None, retried=None, seen=None: _glm_result([]))
    monkeypatch.setattr(glm, "deps_available", lambda: True)
    add_routes(fake_http, cfg)

    def first(fragment, body):
        fake_http.add(fragment, body)
        fake_http.routes.insert(0, fake_http.routes.pop())
    first("gridpoints/ABQ/50,60/stations", stations_doc(
        "KRAW", 34.32, -108.10, 2300.0, ("KMET", 34.45, -108.15, 2250.0)))
    first("/stations/KRAW/observations/latest", _no_weather("KRAW"))
    first("/stations/KMET/observations/latest", observation_doc("KMET", text="Light Snow"))
    pt = mvp.run_once(cfg)["sites"][0]
    # the precipitation check reads KMET's report; the wind and humidity stay KRAW's
    assert pt["obs"]["station_id"] == "KRAW" and pt["obs_station"]["weather"] == "reported"
    assert pt["obs_raw"]["report_station"] == "KMET"
    assert any("precipitation at KMET" in r for r in pt["verdict"]["reasons"])
    # only the station without weather qualifies; none of its recent reports has weather
    first("gridpoints/ABQ/50,60/stations", stations_doc("KRAW", 34.32, -108.10, 2300.0))
    first("/stations/KRAW/observations?limit", _history(*[_no_weather("KRAW", 15 + 60 * k) for k in range(12)]))
    for k in list(os.listdir(cfg.cache_dir)):
        if k.startswith(("obs_", "stations_")):
            os.unlink(os.path.join(cfg.cache_dir, k))
    pt = mvp.run_once(cfg)["sites"][0]
    c = [c for c in pt["verdict"]["checks"] if c["key"] == "obs_precip"][0]
    assert pt["obs"]["station_id"] == "KRAW" and pt["obs_station"]["weather"] == "none"
    assert c["state"] == "not provided" and "KRAW reports no weather" in c["detail"]
    assert pt["verdict"]["status"] == "GOOD" and not pt["verdict"]["incomplete"]


def test_blank_record_of_a_metar_station_is_not_n_a(cfg, fake_http, monkeypatch):
    """St. Croix's case: the only qualifying station is a METAR airport whose latest record
    came back blank. Its newest report with weather of the last 90 min decides; without
    one the check has no data this run (GOOD?), never n/a."""
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result([]))
    monkeypatch.setattr(glm, "fetch", lambda cfg, now=None, since=None, retried=None, seen=None: _glm_result([]))
    monkeypatch.setattr(glm, "deps_available", lambda: True)
    add_routes(fake_http, cfg)

    def first(fragment, body):
        fake_http.add(fragment, body)
        fake_http.routes.insert(0, fake_http.routes.pop())
    rain = observation_doc("KPT1", text="Light Rain")
    rain["properties"]["timestamp"] = _iso(util.utcnow() - timedelta(minutes=50))
    first("/stations/KPT1/observations/latest", _no_weather("KPT1", 10))
    first("/stations/KPT1/observations?limit", _history(_no_weather("KPT1", 10), rain))
    pt = mvp.run_once(cfg)["sites"][0]
    c = [c for c in pt["verdict"]["checks"] if c["key"] == "obs_precip"][0]
    assert pt["obs_station"]["weather"] == "reported" and c["state"] == "bad"
    assert "report of 50 min ago (its newest with weather)" in c["detail"]
    # the newest report with weather is 3 h old: no data this run
    rain["properties"]["timestamp"] = _iso(util.utcnow() - timedelta(hours=3))
    first("/stations/KPT1/observations?limit", _history(_no_weather("KPT1", 10), rain))
    for k in list(os.listdir(cfg.cache_dir)):
        if k.startswith("obshist_"):
            os.unlink(os.path.join(cfg.cache_dir, k))
    pt = mvp.run_once(cfg)["sites"][0]
    c = [c for c in pt["verdict"]["checks"] if c["key"] == "obs_precip"][0]
    assert pt["obs_station"]["weather"] == "unknown" and c["state"] == "unknown" and pt["verdict"]["incomplete"]


def _setup_two_stations(cfg, fake_http, monkeypatch, near, far):
    """Pie Town with two qualifying stations, ``near`` (KNEAR, 3 km) and ``far`` (KFAR, 20 km):
    each a dict of route fragment suffix -> body (latest, history)."""
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result([]))
    monkeypatch.setattr(glm, "fetch", lambda cfg, now=None, since=None, retried=None, seen=None: _glm_result([]))
    monkeypatch.setattr(glm, "deps_available", lambda: True)
    add_routes(fake_http, cfg)

    def first(fragment, body):
        fake_http.add(fragment, body)
        fake_http.routes.insert(0, fake_http.routes.pop())
    first("gridpoints/ABQ/50,60/stations", stations_doc(
        "KNEAR", 34.32, -108.10, 2300.0, ("KFAR", 34.48, -108.15, 2300.0)))
    for sid, routes in (("KNEAR", near), ("KFAR", far)):
        for what, body in routes.items():
            first("/stations/%s/observations%s" % (sid, what), body)


def _report(sid, text="", minutes_ago=10, raw="", present=()):
    doc = observation_doc(sid, text=text, present=present)
    doc["properties"].update(timestamp=_iso(util.utcnow() - timedelta(minutes=minutes_ago)), rawMessage=raw,
                             precipitationLastHour=_q("mm", None))
    return doc


def test_a_current_report_with_weather_beats_an_older_one(cfg, fake_http, monkeypatch):
    """Los Alamos' case: the nearest station's latest record is blank and its newest with
    weather is 80 min old ("Clear"); the next station reports rain NOW: the rain decides."""
    _setup_two_stations(cfg, fake_http, monkeypatch,
                        {"/latest": _report("KNEAR"), "?limit": _history(_report("KNEAR"), _report("KNEAR", "Clear", 80))},
                        {"/latest": _report("KFAR", "Light Rain", 15)})
    pt = mvp.run_once(cfg)["sites"][0]
    assert pt["obs"]["station_id"] == "KNEAR" and pt["verdict"]["status"] == "BAD"
    assert any("precipitation at KFAR" in r for r in pt["verdict"]["reasons"])


def test_an_older_report_of_another_station_never_beats_a_newer_one(cfg, fake_http, monkeypatch):
    """The nearest station's latest record is blank, but it reported rain 20 min ago; the
    next station's latest says "Clear" and is 50 min old (or 170 min: no use at all): the
    newer report, the rain, decides."""
    for far_age in (50, 170):
        _setup_two_stations(cfg, fake_http, monkeypatch,
                            {"/latest": _report("KNEAR", minutes_ago=5),
                             "?limit": _history(_report("KNEAR", minutes_ago=5), _report("KNEAR", "Light Rain", 20))},
                            {"/latest": _report("KFAR", "Clear", far_age)})
        for k in list(os.listdir(cfg.cache_dir)) if os.path.isdir(cfg.cache_dir) else []:
            if k.startswith(("obs_", "obshist_")):
                os.unlink(os.path.join(cfg.cache_dir, k))
        pt = mvp.run_once(cfg)["sites"][0]
        c = [c for c in pt["verdict"]["checks"] if c["key"] == "obs_precip"][0]
        assert c["state"] == "bad" and "KNEAR report of 20 min ago (its newest with weather)" in c["detail"], far_age


def test_another_stations_report_moves_only_the_precipitation_check(cfg, fake_http, monkeypatch):
    """The nearest station's latest record has no weather but blows 80 km/h; the next
    station's newer report says "Clear" and is calm: precipitation from it, the wind (and
    the station on the page) stay the nearest one's."""
    windy = _report("KNEAR", minutes_ago=2)
    windy["properties"]["windSpeed"] = _q("km_h-1", 80.0)
    _setup_two_stations(cfg, fake_http, monkeypatch,
                        {"/latest": windy, "?limit": _history(windy, _report("KNEAR", "Clear", 25))},
                        {"/latest": _report("KFAR", "Clear", 10)})
    pt = mvp.run_once(cfg)["sites"][0]
    states = {c["key"]: c for c in pt["verdict"]["checks"]}
    assert pt["obs"]["station_id"] == "KNEAR" and pt["obs_station"]["id"] == "KNEAR"
    assert states["obs_precip"]["state"] == "ok" and states["obs_precip"]["detail"].startswith("KFAR report of 10 min ago")
    assert states["wind"]["state"] == "bad" and "KNEAR" in states["wind"]["detail"]


def test_a_report_without_temperature_still_counts_for_precipitation(cfg, fake_http, monkeypatch):
    """A station whose latest record has no temperature (NWS's QC) is no observation for the
    page, but its present weather still says whether it rains."""
    rain = _report("KFAR", "Light Rain", 10)
    rain["properties"]["temperature"] = _q("degC", None)
    _setup_two_stations(cfg, fake_http, monkeypatch,
                        {"/latest": _report("KNEAR", minutes_ago=5),
                         "?limit": _history(_report("KNEAR", minutes_ago=5), _report("KNEAR", "Clear", 120))},
                        {"/latest": rain})
    pt = mvp.run_once(cfg)["sites"][0]
    c = [c for c in pt["verdict"]["checks"] if c["key"] == "obs_precip"][0]
    assert c["state"] == "bad" and any("precipitation at KFAR" in r for r in pt["verdict"]["reasons"])


def _precip_check(cfg):
    pt = mvp.run_once(cfg)["sites"][0]
    return pt, [c for c in pt["verdict"]["checks"] if c["key"] == "obs_precip"][0]


def test_pwino_reads_only_the_precipitation_it_still_shows(cfg, fake_http, monkeypatch):
    """A METAR station whose present-weather sensor is out (PWINO) still reports mist, haze or a
    thunderstorm in the vicinity, never rain: such a report says nothing about rain (unknown,
    never "no precipitation reported"), while rain it does show still counts."""
    mist = _report("KNEAR", "Mist", 5, raw="KNEAR 092055Z AUTO 00000KT 3SM BR OVC008 12/11 A2992 RMK AO2 PWINO",
                   present=[(None, "fog_mist")])
    vcts = _report("KNEAR", "Thunderstorms in Vicinity", 25,
                   raw="KNEAR 092035Z AUTO 00000KT 10SM VCTS OVC008 12/11 A2992 RMK AO2 PWINO",
                   present=[(None, "thunderstorms")])
    vcts["properties"]["presentWeather"][0]["inVicinity"] = True
    assert not mvp._reports_weather(mist["properties"]) and not mvp._reports_weather(vcts["properties"])
    _setup_two_stations(cfg, fake_http, monkeypatch, {"/latest": mist, "?limit": _history(mist, vcts)}, {})
    pt, c = _precip_check(cfg)
    assert c["state"] == "unknown" and "PWINO" in c["detail"] and pt["verdict"]["incomplete"]
    rain = _report("KNEAR", "Light Rain", 5, raw="KNEAR 092055Z AUTO 00000KT 3SM -RA OVC008 12/11 A2992 RMK AO2 PWINO",
                   present=[("light", "rain")])
    assert mvp._reports_weather(rain["properties"])


def test_an_unreadable_newer_history_makes_clear_unknown(cfg, fake_http, monkeypatch):
    """The nearest station's latest record is blank and its recent reports cannot be read; the
    next station's report is older: its "Clear" is no answer (a newer report with rain may
    exist), while rain in it still is."""
    for text, state in (("Clear", "unknown"), ("Light Rain", "bad")):
        _setup_two_stations(cfg, fake_http, monkeypatch, {"/latest": _report("KNEAR", minutes_ago=5)},
                            {"/latest": _report("KFAR", text, 80)})
        fake_http.fail("/stations/KNEAR/observations?limit")
        fake_http.routes.insert(0, fake_http.routes.pop())
        for k in list(os.listdir(cfg.cache_dir)) if os.path.isdir(cfg.cache_dir) else []:
            if k.startswith(("obs_", "obshist_")):
                os.unlink(os.path.join(cfg.cache_dir, k))
        pt, c = _precip_check(cfg)
        assert c["state"] == state and c["detail"].startswith("KFAR report of 1 h 20 min ago"), (text, c)
        if state == "unknown":
            assert "KNEAR, maybe newer, could not be read" in c["detail"] and pt["verdict"]["incomplete"]


def test_a_qualifying_station_without_temperature_still_answers_precipitation(cfg, fake_http, monkeypatch):
    """St. Croix's TISX: the only qualifying station sends a report without a temperature, so
    the parent takes a station too far away to be used; the qualifying station's report is
    the observation all the same (the verdict never reads the temperature): its rain and
    its wind count, the temperature tile shows a dash."""
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result([]))
    monkeypatch.setattr(glm, "fetch", lambda cfg, now=None, since=None, retried=None, seen=None: _glm_result([]))
    monkeypatch.setattr(glm, "deps_available", lambda: True)
    add_routes(fake_http, cfg)

    def first(fragment, body):
        fake_http.add(fragment, body)
        fake_http.routes.insert(0, fake_http.routes.pop())
    rain = _report("KNEAR", "Light Rain", 10)
    rain["properties"]["temperature"] = _q("degC", None)
    first("gridpoints/ABQ/50,60/stations", stations_doc(
        "KNEAR", 34.32, -108.10, 2300.0, ("KAWAY", 35.30, -108.15, 2300.0)))
    first("/stations/KNEAR/observations/latest", rain)
    first("/stations/KAWAY/observations/latest", _report("KAWAY", "Clear", 10))
    pt, c = _precip_check(cfg)
    assert pt["obs"]["station_id"] == "KNEAR" and pt["obs_station"]["usable"] and pt["obs"]["temp_c"] is None
    assert c["state"] == "bad" and c["detail"].startswith("KNEAR 10 min ago")
    assert any("precipitation at KNEAR" in r for r in pt["verdict"]["reasons"])
    wind = [c for c in pt["verdict"]["checks"] if c["key"] == "wind"][0]
    assert wind["detail"].startswith("KNEAR: wind")
    # no qualifying station's report usable for the observation at all: the precipitation
    # check still reads the qualifying stations
    cache = http.Cache(cfg.cache_dir)
    site = dict(cfg.sites[0])
    res = mvp._precip_report(cfg, cache, site, {"id": "KAWAY", "usable": False, "qualifying": ["KNEAR"]},
                             {"ok": True, "station_id": "KAWAY"})
    assert res["weather"] == "reported" and res["station"] == "KNEAR" and res["passed"] == "KAWAY is not used"


def test_a_tie_goes_to_the_nearer_station(cfg, fake_http, monkeypatch):
    """Two reports of the same minute: the nearer station's decides, even when the
    observation comes from the farther one (the nearer one's report lacks a temperature)."""
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result([]))
    monkeypatch.setattr(glm, "fetch", lambda cfg, now=None, since=None, retried=None, seen=None: _glm_result([]))
    monkeypatch.setattr(glm, "deps_available", lambda: True)
    add_routes(fake_http, cfg)

    def first(fragment, body):
        fake_http.add(fragment, body)
        fake_http.routes.insert(0, fake_http.routes.pop())
    rain = _report("KCLOSE", "Light Rain", 15)
    rain["properties"]["temperature"] = _q("degC", None)
    clear = _report("KNEAR", "Clear", 15)
    clear["properties"]["timestamp"] = rain["properties"]["timestamp"]
    first("gridpoints/ABQ/50,60/stations", stations_doc(
        "KCLOSE", 34.302, -108.12, 2370.0, ("KNEAR", 34.32, -108.10, 2300.0)))
    first("/stations/KCLOSE/observations/latest", rain)
    first("/stations/KNEAR/observations/latest", _report("KNEAR", minutes_ago=5))
    first("/stations/KNEAR/observations?limit", _history(_report("KNEAR", minutes_ago=5), clear))
    pt, c = _precip_check(cfg)
    assert pt["obs"]["station_id"] == "KNEAR" and pt["obs_station"]["qualifying"] == ["KCLOSE", "KNEAR"]
    assert c["state"] == "bad" and c["detail"].startswith("KCLOSE report of 15 min ago")


def test_only_histories_that_could_win_are_fetched(cfg, fake_http, monkeypatch):
    """Brewster's case: the next station's latest report carries weather and is newer than the
    blank latest records of the others, whose recent reports therefore cannot win and are not
    fetched; with the precipitation check off, nothing beyond the parent's report is."""
    _setup_two_stations(cfg, fake_http, monkeypatch,
                        {"/latest": _report("KNEAR", minutes_ago=55), "?limit": _history(_report("KNEAR", "Rain", 30))},
                        {"/latest": _report("KFAR", "Clear", 20)})
    pt, c = _precip_check(cfg)
    assert c["state"] == "ok" and c["detail"].startswith("KFAR report of 20 min ago (KNEAR's latest has no weather)")
    assert not any("observations?limit" in u for u in fake_http.calls)
    fake_http.calls.clear()
    cfg.obs_precip = False
    for k in list(os.listdir(cfg.cache_dir)):
        if k.startswith(("obs_", "obshist_")):
            os.unlink(os.path.join(cfg.cache_dir, k))
    pt, c = _precip_check(cfg)
    assert c["state"] == "off" and not any("KFAR" in u or "observations?limit" in u for u in fake_http.calls)


def test_the_detail_says_why_the_latest_report_was_passed_over(cfg, fake_http, monkeypatch):
    """The nearest station's latest report says "Clear" but is 2 h old: the detail says so,
    instead of calling it a report without weather."""
    _setup_two_stations(cfg, fake_http, monkeypatch, {"/latest": _report("KNEAR", "Clear", 120)},
                        {"/latest": _report("KFAR", "Clear", 20)})
    pt, c = _precip_check(cfg)
    assert c["state"] == "ok" and c["detail"].startswith("KFAR report of 20 min ago (KNEAR's latest is 2 h ")


def test_the_precipitation_report_carries_its_own_staleness(cfg, cache, monkeypatch):
    site = dict(cfg.sites[0])
    clear = _report("KFAR", "Clear", 20)
    cache.put("obs/%s/KNEAR" % site["slug"], _report("KNEAR", minutes_ago=5))
    monkeypatch.setattr(mvp, "_fetch_latest", lambda cfg, cache, site, sid: {"ok": True, "stale": True, "data": clear})
    monkeypatch.setattr(mvp, "_weather_history", lambda cfg, cache, site, sid: {
        "able": None, "report": None, "age": None, "note": None, "stale": False, "unread": False})
    res = mvp._precip_report(cfg, cache, site, {"id": "KNEAR", "usable": True, "qualifying": ["KNEAR", "KFAR"]},
                             {"ok": True, "station_id": "KNEAR", "stale": False})
    assert res["station"] == "KFAR" and res["stale"] is True and res["doubt"] is None


def test_an_undecoded_metar_is_read_for_precipitation(cfg, fake_http, monkeypatch):
    """A record NWS did not decode (raw METAR only, no description, no present weather):
    its weather groups decide, never "no precipitation reported" by default."""
    metar = "KNEAR 091715Z AUTO 18005KT 10SM -RA BKN030 18/09 A3002 RMK AO2 RAB10"
    _setup_two_stations(cfg, fake_http, monkeypatch, {"/latest": _report("KNEAR", raw=metar)}, {})
    pt = mvp.run_once(cfg)["sites"][0]
    c = [c for c in pt["verdict"]["checks"] if c["key"] == "obs_precip"][0]
    assert c["state"] == "bad" and "METAR reports -RA" in c["detail"]


def test_another_stations_recent_report_counts(cfg, fake_http, monkeypatch):
    """Brewster's case with a blank METAR record: the nearest station never reports weather,
    the next one's latest record is blank but it reported rain 20 min ago."""
    blank12 = _history(*[_report("KNEAR", minutes_ago=15 + 60 * k) for k in range(12)])
    _setup_two_stations(cfg, fake_http, monkeypatch, {"/latest": _report("KNEAR"), "?limit": blank12},
                        {"/latest": _report("KFAR", minutes_ago=5),
                         "?limit": _history(_report("KFAR", minutes_ago=5), _report("KFAR", "Light Rain", 20))})
    pt = mvp.run_once(cfg)["sites"][0]
    c = [c for c in pt["verdict"]["checks"] if c["key"] == "obs_precip"][0]
    assert pt["obs"]["station_id"] == "KNEAR" and c["state"] == "bad"
    assert "KFAR report of 20 min ago (KNEAR's latest has no weather)" in c["detail"]


def test_veto_warning_and_forecast_cuts(cfg, fake_http, monkeypatch):
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result([]))
    monkeypatch.setattr(glm, "fetch", lambda cfg, now=None, since=None, retried=None, seen=None: _glm_result([]))
    monkeypatch.setattr(glm, "deps_available", lambda: True)
    box = [[-108.3, 34.2], [-108.0, 34.2], [-108.0, 34.4], [-108.3, 34.4], [-108.3, 34.2]]
    feed = {"type": "FeatureCollection", "features": [
        alert_feature("hww", "High Wind Warning", geometry=box),
        alert_feature("wsw", "Winter Storm Warning", geometry=box, onset=FUTURE)]}
    add_routes(fake_http, cfg, alerts=feed, pt_grid=gridpoint_doc(sky=80, snow=2))
    run = mvp.run_once(cfg)
    v = run["sites"][0]["verdict"]
    assert v["status"] == "BAD"
    assert any("High Wind Warning" in r for r in v["reasons"])
    assert any("cloud cover 80 %" in r for r in v["reasons"]) and any("snowfall 2 mm" in r for r in v["reasons"])
    assert v["scheduled"] and "Winter Storm Warning" in v["scheduled"][0]
    assert run["sites"][1]["verdict"]["status"] == "GOOD"


def test_every_source_dead_still_writes_the_page(cfg, fake_http, monkeypatch):
    monkeypatch.setattr(glm, "deps_available", lambda: False)
    monkeypatch.setattr(glm, "deps_note", lambda: "needs python3-numpy and python3-h5py")
    run = mvp.run_once(cfg)
    assert len(run["sites"]) == 2 and run["glm"]["skipped"].startswith("GLM lightning unavailable")
    for s in run["sites"]:
        assert s["verdict"]["status"] == "NO DATA" and s["verdict"]["incomplete"]
        # a map either renders (on a plain background without tiles) or says why it did not
        for m in s["maps"].values():
            assert (m["ok"] and os.path.exists(os.path.join(cfg.out_dir, m["basename"]))) or \
                (not m["ok"] and m["error"])
    html = read_page(cfg)
    assert "NO DATA" in html and "could not be rendered" not in html
    st = read_status(cfg)
    assert st["page_written"] and st["degraded"] and not st["ok"]
    assert any("GLM" in p or "glm" in p.lower() for p in st["problems"] + st["errors"])


def test_unrecognised_composite_colours_near_the_antenna_are_no_data(cfg, fake_http, monkeypatch):
    """A colour the decoder does not know (NWS changed its scale?) inside the ring may be
    rain of any strength: the radar check has no data instead of "no echo"."""
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result([]))
    monkeypatch.setattr(glm, "fetch", lambda cfg, now=None, since=None, retried=None, seen=None: _glm_result([]))
    monkeypatch.setattr(glm, "deps_available", lambda: True)
    east = geo.dest_point(MK["lat"], MK["lon"], 10.0, 90.0)
    add_routes(fake_http, cfg, mk_blobs=[(east[0], east[1], (2, 253, 2))])
    run = mvp.run_once(cfg)
    mk = run["sites"][1]
    radar = [c for c in mk["verdict"]["checks"] if c["key"] == "radar"][0]
    assert radar["state"] == "unknown" and "unrecognised colour" in radar["detail"]
    assert mk["verdict"]["incomplete"] and "Rain on radar" in mk["verdict"]["missing"]
    assert any("mk radar check" in e and "unrecognised" in e for e in run["errors"])


def test_glm_disabled_and_island_radar_off(cfg, fake_http, monkeypatch):
    cfg.glm_enabled = False
    cfg.island_radar = False
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result([]))
    add_routes(fake_http, cfg)
    run = mvp.run_once(cfg)
    assert run["glm"]["skipped"] == "GLM layer disabled (VLBA_GLM=0 or --skip-glm)"
    assert not any("GetMap" in c for c in fake_http.calls)
    mk = run["sites"][1]
    assert mk["island_radar"]["not_applicable"] and mk["radar_check"]["available"] is False
    states = {c["key"]: c["state"] for c in mk["verdict"]["checks"]}
    # switched off on purpose: "off", not missing data, so no permanent GOOD?
    assert states["glm"] == "off" and states["radar"] == "off" and not mk["verdict"]["incomplete"]
    html = read_page(cfg)
    assert "GLM layer switched off" in html
    # Mauna Kea's caption says it has no radar (never the MRMS frame of the CONUS antennas),
    # no map mentions the lightning ring, and the switched-off radar is no status problem
    assert "No radar here: no radar for this site (VLBA_ISLAND_RADAR=0)" in html
    assert "dashed ring" not in html
    mk_caption = html[html.index('id="site-mk"'):].split("<figcaption>")[1].split("</figcaption>")[0]
    assert "solid ring" not in mk_caption                  # no radar check there: no ring drawn either
    st = read_status(cfg)
    assert not any("radar composite" in p for p in st["problems"]) and st["sites"]["mk"]["island_radar_ok"] is None


def test_cli_exit_codes(tmp_path, monkeypatch, fake_http):
    for k in list(os.environ):
        if k.startswith(("WEATHER_", "VLBA_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WEATHER_USER_AGENT", "vlbaweather-test (+https://example.invalid/not-a-placeholder)")
    assert mvp.main(["--sites", ";", "--out", str(tmp_path / "o"), "--cache", str(tmp_path / "c")]) == 2
    monkeypatch.setenv("VLBA_RADAR_KM", "nope")
    assert mvp.main(["--out", str(tmp_path / "o"), "--cache", str(tmp_path / "c")]) == 2
    monkeypatch.delenv("VLBA_RADAR_KM")
    monkeypatch.setattr(mvp, "run_once", lambda cfg, skip_radar=False: (_ for _ in ()).throw(http.AlreadyRunning("busy")))
    assert mvp.main(["--out", str(tmp_path / "o"), "--cache", str(tmp_path / "c")]) == 0
    monkeypatch.setattr(mvp, "run_once", lambda cfg, skip_radar=False: (_ for _ in ()).throw(OSError("read-only")))
    assert mvp.main(["--out", str(tmp_path / "o"), "--cache", str(tmp_path / "c")]) == 1
    seen = {}
    monkeypatch.setattr(mvp, "run_once", lambda cfg, skip_radar=False: seen.update(glm=cfg.glm_enabled, n=len(cfg.sites)))
    assert mvp.main(["--skip-glm", "--out", str(tmp_path / "o"), "--cache", str(tmp_path / "c")]) == 0
    assert seen == {"glm": False, "n": 10}


def test_script_runs_as_a_program(tmp_path):
    env = dict(os.environ, WEATHER_USER_AGENT="vlbaweather-test (+https://example.invalid/x)")
    import subprocess
    proc = subprocess.run([sys.executable, os.path.join(mvp.HERE, "make_vlba_page.py"), "--sites", ";",
                           "--out", str(tmp_path / "o"), "--cache", str(tmp_path / "c")],
                          capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert proc.returncode == 2 and "configuration error" in proc.stderr


def test_station_info_reads_the_list_entry_by_entry(cfg, cache):
    """One rule for "usable" and "qualifying" (no rounding between them); a list whose
    stations cannot be located says nothing (qualifying unknown, never "none qualifies");
    an entry that cannot be read is skipped, not the whole list."""
    from weather import nws
    pt = dict(cfg.sites[0])
    key = "stations/%s" % nws._loc(pt)
    obs = {"ok": True, "station_id": "KAAA", "station_name": "A"}
    far = geo.dest_point(pt["lat"], pt["lon"], 50.04, 0.0)          # rounds to 50.0, is over 50
    cache.put(key, stations_doc("KAAA", far[0], far[1], 2300.0))
    info = mvp._station_info(cfg, cache, pt, obs)
    assert info["dist_km"] == 50.0 and info["usable"] is False and info["qualifying"] == []
    nogeo = stations_doc("KAAA", 34.3, -108.1, 2300.0)
    for f in nogeo["features"]:
        f["geometry"] = None
    cache.put(key, nogeo)
    info = mvp._station_info(cfg, cache, pt, obs)
    assert info["usable"] is True and info["qualifying"] is None and "distance" in info["why"]
    odd = stations_doc("KBAD", 34.3, -108.1, 2300.0, ("KAAA", 34.32, -108.10, 2300.0))
    odd["features"][0]["geometry"] = {"type": "Point", "coordinates": {"lon": 1, "lat": 2}}
    cache.put(key, odd)
    info = mvp._station_info(cfg, cache, pt, obs)
    assert info["usable"] is True and info["qualifying"] == ["KAAA"] and info["dist_km"] < 5


def test_weather_history_odd_payloads(cfg, cache, fake_http):
    """A report a few minutes ahead of this host's clock counts, one far ahead hides nothing;
    copies of one blank record are one report (three of them are no proof of a station
    without weather)."""
    site = dict(cfg.sites[0])

    def hist(*reports):
        for k in list(os.listdir(cfg.cache_dir)):
            if k.startswith("obshist_"):
                os.unlink(os.path.join(cfg.cache_dir, k))
        fake_http.routes = [r for r in fake_http.routes if "observations?limit" not in r[0]]
        fake_http.add("/stations/KX/observations?limit", _history(*reports))
        h = mvp._weather_history(cfg, cache, site, "KX")
        return h["able"], h["report"], h["age"], h["note"]
    able, p, age, _ = hist(_report("KX", "Light Rain", -2), _report("KX", minutes_ago=30))
    assert able is True and p["textDescription"] == "Light Rain" and age == 0.0
    able, p, age, _ = hist(_report("KX", "Clear", -60), _report("KX", "Light Rain", 20))
    assert able is True and p["textDescription"] == "Light Rain" and 19 < age < 21
    blank = _report("KX", minutes_ago=30)
    assert hist(blank, blank, blank)[0] is None
    assert hist(*[_report("KX", minutes_ago=30 + 60 * k) for k in range(3)])[0] is False
    # two copies of one report, a blank one listed first: the one with weather counts
    rain = _report("KX", "Light Rain", 20)
    copy = _report("KX", minutes_ago=20)
    copy["properties"]["timestamp"] = rain["properties"]["timestamp"]
    able, p, age, _ = hist(_report("KX", minutes_ago=5), copy, rain, _report("KX", minutes_ago=40))
    assert able is True and p["textDescription"] == "Light Rain"
    # a METAR station whose present-weather sensor is out: unknown, not "no sensor"
    metar = "KX 091715Z AUTO 00000KT 10SM BKN060 21/15 A3033 RMK AO2 PWINO"
    pwino = [_report("KX", "Mostly Cloudy", 10 + 20 * k, raw=metar) for k in range(4)]
    assert not mvp._reports_weather(pwino[0]["properties"])
    able, p, age, note = hist(*pwino)
    assert able is None and p is None and "PWINO" in note


def test_unknown_station_list_never_gives_n_a(cfg, cache, fake_http):
    """Only a known station list can show that no station near the antenna reports weather:
    with the list unknown (no station could be located) a station without weather gives
    "unknown", not n/a."""
    site = dict(cfg.sites[0])
    fake_http.add("/stations/KRAW/observations?limit", _history(*[_report("KRAW", minutes_ago=15 + 60 * k)
                                                                  for k in range(12)]))
    cache.put("obs/%s/KRAW" % site["slug"], _report("KRAW"))
    obs = {"ok": True, "station_id": "KRAW"}
    res = mvp._precip_report(cfg, cache, site, {"id": "KRAW", "usable": True, "qualifying": None}, obs)
    assert res["weather"] == "unknown"
    res = mvp._precip_report(cfg, cache, site, {"id": "KRAW", "usable": True, "qualifying": ["KRAW"]}, obs)
    assert res["weather"] == "none" and "KRAW reports no weather" in res["why"]
    # a PWINO record with the rain group still shows rain
    rain = _report("KRAW", "Mostly Cloudy", 5, raw="KRAW 091715Z AUTO 00000KT 3SM -RA BKN060 21/15 A3033 RMK AO2 PWINO")
    assert mvp._reports_weather(rain["properties"])


def test_a_run_without_glm_keeps_the_newest_granule_marker(cfg, fake_http, monkeypatch):
    """--skip-glm, VLBA_GLM=0 or missing libraries must not erase where the next fetching
    run's GLM window starts, which granules it fetches again and which were examined. A
    fetching run hands them to the next one."""
    calls = []

    def fetch(cfg, now=None, since=None, retried=None, seen=None):
        calls.append((since, retried, seen))
        return dict(_glm_result([]), retry={"G19": [1791474000.0]}, seen={"G19": [1791473980.0]})
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result([]))
    monkeypatch.setattr(glm, "fetch", fetch)
    monkeypatch.setattr(glm, "deps_available", lambda: True)
    add_routes(fake_http, cfg)
    mvp.run_once(cfg)
    with open(cfg.state_file, encoding="utf-8") as f:
        state = json.load(f)
    marker = state["glm_newest"]
    assert state["glm_retry"] == {"G19": [1791474000.0]} and calls == [(None, None, None)]
    assert state["glm_seen"] == {"G19": [1791473980.0]}
    cfg.glm_enabled = False
    mvp.run_once(cfg)
    with open(cfg.state_file, encoding="utf-8") as f:
        state = json.load(f)
    assert state["glm_newest"] == marker and state["glm_retry"] == {"G19": [1791474000.0]}
    assert state["glm_seen"] == {"G19": [1791473980.0]}
    cfg.glm_enabled = True
    mvp.run_once(cfg)
    assert calls[-1] == (marker, {"G19": [1791474000.0]}, {"G19": [1791473980.0]})


# ---- radar coverage, the shown-only observation (asked 2026-10-12) --------------------------
@pytest.fixture
def quiet_sky(monkeypatch):
    """An MRMS frame without any echo, GLM without any flash: every radar check would read clear."""
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result(()))
    monkeypatch.setattr(glm, "deps_available", lambda: True)
    monkeypatch.setattr(glm, "fetch", lambda cfg, now=None, since=None, retried=None, seen=None: _glm_result([]))


def _radar_row(entry):
    return [c for c in entry["verdict"]["checks"] if c["key"] == "radar"][0]


def test_a_ring_radar_does_not_cover_is_no_data(cfg, fake_http, quiet_sky):
    add_routes(fake_http, cfg, pt_holes=((PT["lat"] - 1, PT["lat"] + 1, PT["lon"] - 1, PT["lon"]),))
    run = mvp.run_once(cfg)
    pt, mk = run["sites"]
    assert pt["radar_check"]["available"] and not pt["radar_check"]["in_ring"]
    assert pt["radar_check"]["coverage"]["ok"] and 48 < pt["radar_check"]["coverage"]["covered_pct"] < 52
    c = _radar_row(pt)
    assert c["state"] == "unknown" and "no radar covers" in c["detail"] and "of the 30 km ring" in c["detail"]
    assert pt["verdict"]["status"] == "GOOD" and "Rain on radar" in pt["verdict"]["missing"]
    assert _radar_row(mk)["state"] == "ok" and mk["radar_check"]["coverage"]["covered_pct"] == 100.0
    calls = [u for u in fake_http.calls if "noaa-mrms-pds" in u]
    assert len(calls) == 2 and "CONUS/SeamlessHSR_00.00/" in calls[0] and "HAWAII/" in calls[1]
    # the files of the frames' own times: the MRMS frame's minute, the composite's second
    assert calls[0].endswith(run["radar"]["ts"].strftime("%Y%m%d-%H%M%S") + ".grib2.gz")
    assert calls[1].endswith(mk["island_radar"]["ts"].strftime("%Y%m%d-%H%M%S") + ".grib2.gz")
    st = read_status(cfg)
    assert any(p.startswith("pt radar coverage: ") for p in st["problems"]) and st["degraded"]
    assert set(st["radar_coverage"]) == {"CONUS", "HAWAII"} and st["radar_coverage"]["HAWAII"]["covered_pct"] == {"mk": 100.0}
    assert "Radar coverage (MRMS CONUS): " in read_page(cfg)


def test_radar_coverage_that_cannot_be_read_is_no_data_and_a_problem(cfg, fake_http, quiet_sky):
    add_routes(fake_http, cfg)
    fake_http.routes.insert(0, ("SeamlessHSR", None, False))      # the bucket refuses the CONUS files
    run = mvp.run_once(cfg)
    pt, mk = run["sites"]
    c = _radar_row(pt)
    assert c["state"] == "unknown" and "radar coverage of the ring is unknown" in c["detail"]
    assert _radar_row(mk)["state"] == "ok"
    assert any(e.startswith("radar coverage (MRMS CONUS) unavailable: ") for e in run["errors"])
    assert "radar coverage (MRMS CONUS): unavailable" in read_status(cfg)["problems"]


def test_radar_coverage_off_reads_nothing(cfg, fake_http, quiet_sky):
    cfg.radar_coverage = False
    add_routes(fake_http, cfg)
    run = mvp.run_once(cfg)
    assert not [u for u in fake_http.calls if "noaa-mrms-pds" in u]
    for s in run["sites"]:
        c = _radar_row(s)
        assert c["state"] == "ok" and c["detail"].endswith("radar coverage not checked (VLBA_RADAR_COVERAGE=0)")
    assert read_status(cfg)["radar_coverage"] is None and "Radar coverage (MRMS): not checked" in read_page(cfg)


def _empty_record(sid):
    """A record as NWS sent them for Mauna Kea's stations on 2026-10-12: every value null."""
    rec = observation_doc(sid)
    for k in ("temperature", "dewpoint", "windSpeed", "relativeHumidity"):
        rec["properties"][k] = _q(rec["properties"][k]["unitCode"].split(":")[-1], None)
    return json.dumps(rec).encode("utf-8")


def test_an_empty_record_where_no_station_qualifies_is_no_problem(cfg, fake_http, quiet_sky):
    """Mauna Kea: no station qualifies, the report is only shown, so the run is not degraded
    when NWS sends nothing usable there."""
    add_routes(fake_http, cfg)
    fake_http.routes.insert(0, ("/stations/PHSF/observations/latest", _empty_record("PHSF"), True))
    run = mvp.run_once(cfg)
    pt, mk = run["sites"]
    assert not mk["obs"]["ok"] and mk["obs"]["display_only"] and mk["obs_station"]["qualifying"] == []
    assert "no temperature" in mk["obs"]["error"]               # the record was read: no transport error
    assert not any(e.startswith("mk observation") for e in run["errors"])
    st = read_status(cfg)
    assert not any(p.startswith("mk observation") for p in st["problems"])
    assert st["sites"]["mk"]["obs_ok"] is False and not st["degraded"] and st["ok"]
    assert mk["verdict"]["status"] == "GOOD" and not mk["verdict"]["incomplete"]
    assert "display_only" not in pt["obs"] and pt["obs"]["ok"]


def test_an_observation_the_checks_read_is_still_a_problem(cfg, fake_http, quiet_sky):
    """The test world's Pie Town has a qualifying station (KPT1, 2.7 km): its observation
    failing is a problem of the run, as before; only Mauna Kea's kind is exempt."""
    add_routes(fake_http, cfg)
    fake_http.routes.insert(0, ("/stations/KPT1/observations/latest", None, False))
    run = mvp.run_once(cfg)
    pt = run["sites"][0]
    assert not pt["obs"]["ok"] and "display_only" not in pt["obs"] and pt["obs_station"]["qualifying"] == ["KPT1"]
    assert any(e.startswith("pt observation unavailable") for e in run["errors"])
    st = read_status(cfg)
    assert "pt observation: unavailable" in st["problems"] and st["degraded"]


def test_a_qualifying_report_without_temperature_is_no_problem(cfg, fake_http, quiet_sky):
    """The nearest-station observation wants a temperature; the qualifying station's report
    serves without one. The run notes the observation the checks use: no error."""
    add_routes(fake_http, cfg)
    rec = observation_doc("KPT1")
    rec["properties"]["temperature"] = _q("degC", None)
    fake_http.routes.insert(0, ("/stations/KPT1/observations/latest", json.dumps(rec).encode("utf-8"), True))
    run = mvp.run_once(cfg)
    pt = run["sites"][0]
    assert pt["obs"]["ok"] and pt["obs"]["station_id"] == "KPT1" and pt["obs_station"]["usable"]
    assert not any(e.startswith("pt observation") for e in run["errors"])
    assert not any(p.startswith("pt observation") for p in read_status(cfg)["problems"])


def test_island_coverage_of_two_sites_in_one_domain_is_merged(cfg, monkeypatch):
    """An island read is per site: a second antenna of the same MRMS domain must not replace
    the first one's rings (status.json, the footer pill), and a failed read stays visible."""
    reads = iter([
        {"ok": True, "error": None, "domain": "HAWAII", "ts": None, "url": "u1",
         "sites": {"mk": {"cells": 10, "uncovered": 0, "covered_pct": 100.0}}},
        {"ok": True, "error": None, "domain": "HAWAII", "ts": None, "url": "u2",
         "sites": {"hi": {"cells": 10, "uncovered": 5, "covered_pct": 50.0}}},
        {"ok": False, "error": "HTTP 503", "domain": "HAWAII", "ts": None, "url": None, "sites": {}}])
    monkeypatch.setattr(mvp.mrms, "coverage", lambda *a, **k: next(reads))
    run = {"errors": []}
    mvp._radar_coverage(cfg, run, "HAWAII", None, [MK], "mk radar coverage (MRMS HAWAII)")
    second = mvp._radar_coverage(cfg, run, "HAWAII", None, [{"slug": "hi"}], "hi radar coverage (MRMS HAWAII)")
    assert mvp.mrms.site_coverage(second, "hi")["covered_pct"] == 50.0          # each site its own read
    assert run["radar_coverage"]["HAWAII"]["ok"] and set(run["radar_coverage"]["HAWAII"]["sites"]) == {"mk", "hi"}
    mvp._radar_coverage(cfg, run, "HAWAII", None, [{"slug": "x"}], "x radar coverage (MRMS HAWAII)")
    merged = run["radar_coverage"]["HAWAII"]
    assert not merged["ok"] and merged["error"] == "HTTP 503" and set(merged["sites"]) == {"mk", "hi"}
    assert run["errors"] == ["x radar coverage (MRMS HAWAII) unavailable: HTTP 503"]
