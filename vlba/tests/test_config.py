"""VlbaConfig: the built-in site list, the VLBA_* parsing and validation, the derived
values, and that the parent's WEATHER_* settings still apply."""
from __future__ import annotations

import pytest

from vlba import config as vc
from vlba.config import VlbaConfig
from vlba.sites import SITES, site_code, site_elevation_m

try:
    from zoneinfo import ZoneInfo
except ImportError:             # pragma: no cover
    ZoneInfo = None


def _cfg(monkeypatch, tmp_path, **env):
    for k in list(__import__("os").environ):
        if k.startswith(("WEATHER_", "VLBA_")):
            monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return VlbaConfig.from_env(out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "cache"))


def test_builtin_sites_are_the_vlba_antennas_the_vla_and_the_gbt(monkeypatch, tmp_path):
    c = _cfg(monkeypatch, tmp_path)
    assert [s["slug"] for s in c.sites] == ["br", "fd", "hn", "kp", "la", "mk", "nl", "ov", "pt", "sc",
                                            "vla", "gbt"]
    assert "vlba/sites.py" in c.sites_source
    assert c.refresh_seconds == 300 and c.roads_enabled is False and c.lsr_enabled is False
    assert c.title == "VLBA, VLA and GBT weather" and c.page_title == "VLBA, VLA and GBT weather"
    # the 30-minute run interval: the GLM window, the forecasts re-fetched by every run, the
    # alert feed reused over one failed run, the warning lead; GLM's measured field of view
    assert c.glm_window_min == 30 and c.glm_timeout_s == 90 and c.glm_max_arc_deg == 62.0
    assert (c.forecast_ttl, c.hourly_ttl, c.gridpoint_ttl) == (1500, 1500, 1500)
    assert c.alerts_max_stale == 2400 and c.veto_onset_lead_s == 2700
    assert c.state_file == str(tmp_path / "cache" / "vlba_state.json")
    # the alert query covers Hawaii, the Virgin Islands and their marine areas
    for code in ("HI", "VI", "PR", "NM", "WA", "NH", "PH", "WV", "VA"):
        assert code in c.alert_area_codes


def test_site_table_is_sound():
    slugs = [s["slug"] for s in SITES]
    assert len(slugs) == 12 and len(set(slugs)) == 12
    for s in SITES:
        assert s["slug"] == s["code"].lower() and len(s["code"]) == (3 if s["slug"] in ("vla", "gbt") else 2)
        assert -90 < s["lat"] < 90 and -180 < s["lon"] < 0, s
        assert isinstance(site_elevation_m(s), float) and site_code(s) == s["code"]
        if ZoneInfo is not None:
            ZoneInfo(s["tz"])
    assert site_code({"slug": "x1"}) == "X1" and site_elevation_m({"slug": "x"}) is None
    mk = next(s for s in SITES if s["slug"] == "mk")
    assert abs(mk["lat"] - 19.8014) < 1e-4 and abs(mk["lon"] + 155.4555) < 1e-4
    assert mk["tz"] == "Pacific/Honolulu" and mk["elev_m"] > 3700
    # the VLA and the GBT: SCHED's VLA (array reference) and GBT-VLBA positions
    vla, gbt = (next(s for s in SITES if s["slug"] == x) for x in ("vla", "gbt"))
    assert (vla["code"], vla["lat"], vla["lon"], vla["tz"]) == ("VLA", 34.0788, -107.6183, "America/Denver")
    assert (gbt["code"], gbt["lat"], gbt["lon"], gbt["tz"]) == ("GBT", 38.4331, -79.8398, "America/New_York")
    assert 2100 < vla["elev_m"] < 2150 and 790 < gbt["elev_m"] < 840


def test_weather_sites_override_the_builtin_list(monkeypatch, tmp_path):
    c = _cfg(monkeypatch, tmp_path, WEATHER_SITES="socorro|Socorro, NM|34.0584|-106.8914|America/Denver")
    assert [s["slug"] for s in c.sites] == ["socorro"] and c.sites_source == "WEATHER_SITES"


def test_vlba_env_values_and_derived(monkeypatch, tmp_path):
    c = _cfg(monkeypatch, tmp_path, VLBA_RADAR_KM="40", VLBA_NWS_CLOUD_MAX="-1", VLBA_GLM="0",
             VLBA_VETO_EVENTS="tornado warning, high  wind warning, Frobnicate Warning",
             VLBA_GLM_BUCKETS="noaa-goes19=-75.2", VLBA_WIND_MPH="45", VLBA_STATE_FILE="/tmp/s.json",
             VLBA_NWSRADAR_URL="https://example.net/geoserver/", VLBA_RADAR_COVERAGE="0",
             VLBA_RADAR_MIN_COVERAGE="75.5", VLBA_MRMS_URL="http://mirror.example/mrms/")
    assert c.radar_km == 40.0 and c.check_enabled("forecast_cloud") is False
    assert c.radar_coverage is False and c.radar_min_coverage == 75.5 and c.mrms_url == "http://mirror.example/mrms"
    assert c.check_enabled("glm") is False and c.check_enabled("wind") is True
    assert c.veto_event_names == ["Tornado Warning", "High Wind Warning", "Frobnicate Warning"]
    assert vc.parse_event_names(c.veto_events)[1] == ["Frobnicate Warning"]
    assert c.glm_satellites == [("noaa-goes19", -75.2)]
    assert c.state_file == "/tmp/s.json" and c.nwsradar_url == "https://example.net/geoserver"


def test_empty_veto_list_switches_the_warning_check_off(monkeypatch, tmp_path):
    c = _cfg(monkeypatch, tmp_path, VLBA_VETO_EVENTS="")
    assert c.veto_event_names == [] and c.check_enabled("warning") is False
    # "none" does the same and survives the cron wrapper, which skips empty values
    for off in ("none", " None ", "NONE"):
        c = _cfg(monkeypatch, tmp_path, VLBA_VETO_EVENTS=off)
        assert c.veto_event_names == [] and c.check_enabled("warning") is False


def test_switches_are_strict_and_thresholds_finite(monkeypatch, tmp_path):
    """A switch meant to be off must never silently stay on, a NaN threshold never silently
    switch a check off."""
    for word, val in (("0", False), ("off", False), ("No", False), ("1", True), ("on", True), ("TRUE", True)):
        assert _cfg(monkeypatch, tmp_path, VLBA_NWS_WINTER=word).nws_winter is val
    for name in ("VLBA_NWS_WINTER", "VLBA_OBS_PRECIP", "VLBA_GLM", "VLBA_ISLAND_RADAR", "VLBA_RADAR_COVERAGE"):
        for bad in ("n", "disabled", "2"):
            with pytest.raises(ValueError, match=name):
                _cfg(monkeypatch, tmp_path, **{name: bad})
    for name in ("VLBA_NWS_PRECIP_MAX", "VLBA_NWS_CLOUD_MAX", "VLBA_OBS_HUMIDITY_MAX", "VLBA_WIND_MPH",
                 "VLBA_OBS_MAX_KM", "VLBA_OBS_MAX_DELEV_M", "VLBA_GLM_KM", "VLBA_RADAR_MIN_COVERAGE"):
        for bad in ("nan", "inf"):
            with pytest.raises(ValueError):
                _cfg(monkeypatch, tmp_path, **{name: bad})


def test_radar_ring_must_fit_the_island_composites(monkeypatch, tmp_path):
    """The NWS composites are fetched for the map only: a ring wider than half the map would
    be partly unseen at Mauna Kea and St. Croix."""
    with pytest.raises(ValueError, match="half the map width"):
        _cfg(monkeypatch, tmp_path, WEATHER_MAP_MILES="30")          # 24 km half-width < 30 km
    c = _cfg(monkeypatch, tmp_path, WEATHER_MAP_MILES="30", VLBA_ISLAND_RADAR="0")
    assert c.radar_km == 30.0 and c.check_enabled("radar")
    c = _cfg(monkeypatch, tmp_path, VLBA_ISLAND_RADAR="0", WEATHER_RADAR="0")
    assert not c.check_enabled("radar")


def test_vlba_antennas_named_through_sites_keep_code_and_height(monkeypatch, tmp_path):
    c = _cfg(monkeypatch, tmp_path, WEATHER_SITES="mk|Mauna Kea|19.8014|-155.4555;xx|Elsewhere|35.0|-106.0")
    mk, xx = c.sites
    assert mk["code"] == "MK" and mk["elev_m"] == 3763 and "elev_m" not in xx
    # a slug of the list at another place is another site
    c = _cfg(monkeypatch, tmp_path, WEATHER_SITES="mk|Not Mauna Kea|35.0|-106.0")
    assert "elev_m" not in c.sites[0]


def test_state_file_path_is_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    c = _cfg(monkeypatch, tmp_path, VLBA_STATE_FILE="~/state/vlba.json")
    assert c.state_file == str(tmp_path / "home" / "state" / "vlba.json")


@pytest.mark.parametrize("env", [
    {"VLBA_RADAR_KM": "0"}, {"VLBA_RADAR_KM": "x"}, {"VLBA_RADAR_DBZ": "99"},
    {"VLBA_RADAR_TRIGGER_AFTER": "0"}, {"VLBA_RADAR_HOLD": "-1"}, {"VLBA_NWS_CLOUD_MAX": "101"},
    {"VLBA_NWS_STALE_MIN": "0"}, {"VLBA_OBS_MAX_KM": "-5"}, {"VLBA_WIND_MPH": "-1"},
    {"VLBA_GLM_KM": "1000"}, {"VLBA_GLM_WINDOW_MIN": "0"}, {"VLBA_GLM_STRIDE": "31"},
    {"VLBA_GLM_HOLD": "0"}, {"VLBA_GLM_HOLD": "59"}, {"VLBA_RADAR_TRIGGER_AFTER": "7"},
    {"VLBA_GLM_BUCKETS": "noaa-goes19"}, {"VLBA_GLM_BUCKETS": "a=1;a=2"},
    {"VLBA_GLM_BUCKETS": "bad bucket=-75"}, {"VLBA_GLM_MAX_ARC": "95"},
    {"VLBA_GLM_TIMEOUT": "1"}, {"VLBA_GLM_WORKERS": "0"}, {"VLBA_NWSRADAR_URL": "ftp://x"},
    {"VLBA_VETO_ONSET_LEAD": "-1"}, {"VLBA_RADAR_MIN_COVERAGE": "101"}, {"VLBA_RADAR_MIN_COVERAGE": "-1"},
    {"VLBA_MRMS_URL": "s3://noaa-mrms-pds"},
])
def test_bad_values_are_startup_errors(monkeypatch, tmp_path, env):
    with pytest.raises(ValueError):
        _cfg(monkeypatch, tmp_path, **env)


def test_parse_helpers():
    assert vc.parse_glm_buckets("") == []
    assert vc.parse_glm_buckets(" noaa-goes18 = -137 ;") == [("noaa-goes18", -137.0)]
    names, unknown = vc.parse_event_names("Blizzard Warning,blizzard warning,, Ice Storm Warning")
    assert names == ["Blizzard Warning", "Ice Storm Warning"] and unknown == []


def test_keyword_overrides_win(monkeypatch, tmp_path):
    monkeypatch.setenv("VLBA_RADAR_KM", "40")
    c = VlbaConfig.from_env(out_dir=str(tmp_path / "o"), cache_dir=str(tmp_path / "c"), radar_km=25.0)
    assert c.radar_km == 25.0
    with pytest.raises(TypeError):
        VlbaConfig.from_env(out_dir=str(tmp_path / "o"), cache_dir=str(tmp_path / "c"), nosuch=1)
