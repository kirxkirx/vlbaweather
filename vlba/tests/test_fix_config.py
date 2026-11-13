"""The configuration audit's findings: values that weakened a verdict check, published a
private file or broke a run in the middle, now refused at startup or reported on every run
(#58, #62/#144, #60, #54/#25/#32, #63/#67/#145, #66, #7, #17/#57, #167, S12)."""
from __future__ import annotations

import logging
import os
import re

import pytest

import make_vlba_page as mvp                     # noqa: E402  (conftest put vlbaweather on sys.path)
from vlba import config as vc
from vlba.config import VlbaConfig
from weather import config as wc


def _clear(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("WEATHER_", "VLBA_")):
            monkeypatch.delenv(k, raising=False)


def _cfg(monkeypatch, tmp_path, **env):
    _clear(monkeypatch)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return VlbaConfig.from_env(out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "cache"))


# ---- #58: VLBA_VETO_EVENTS ------------------------------------------------------------
@pytest.mark.parametrize("value", [
    "Tornado Warning;Dust Storm Warning;High Wind Warning",       # another separator
    "Tornado Warning,High Wind Warning # the two that matter",    # a trailing comment
    '"Tornado Warning,High Wind Warning"',                         # quoted
    "'Tornado Warning'",
    "Tornado Warning,High Wind Warning,\\",                        # a continued line (cron wrapper)
    "1", "on", "Yes", "TRUE", "0", "off", "12",                    # a switch, not a list
    "Tornado Warning,no",
])
def test_a_veto_list_that_matches_no_product_is_refused(monkeypatch, tmp_path, value):
    with pytest.raises(ValueError, match="VLBA_VETO_EVENTS"):
        _cfg(monkeypatch, tmp_path, VLBA_VETO_EVENTS=value)


def test_an_unknown_but_plausible_veto_name_is_still_accepted(monkeypatch, tmp_path):
    c = _cfg(monkeypatch, tmp_path, VLBA_VETO_EVENTS="Tornado Warning, Frobnicate Warning")
    assert c.veto_event_names == ["Tornado Warning", "Frobnicate Warning"]
    for off in ("none", ""):
        assert _cfg(monkeypatch, tmp_path, VLBA_VETO_EVENTS=off).veto_event_names == []


def test_unknown_or_empty_veto_list_and_plain_http_sources_are_run_errors(cfg, fake_http):
    """A veto name NWS does not use, an empty list and a plain-http source of a verdict check
    are reported on every run, in status.json's errors and the footer, not only in the log."""
    cfg.glm_enabled = False             # no granule listing to fake
    cfg.veto_events = "Tornado Warning,Frobnicate Warning"
    cfg.metar_url = "http://m.example/metar"
    errors = mvp.run_once(cfg)["errors"]
    assert any("Frobnicate Warning" in e and "VLBA_VETO_EVENTS" in e for e in errors), errors
    assert any(e.startswith("VLBA_METAR_URL is a plain http:// URL") for e in errors), errors
    assert not any("m.example" in e for e in errors if "plain http" in e)
    cfg.veto_events = ""
    cfg.metar_url = "https://m.example/metar"
    errors = mvp.run_once(cfg)["errors"]
    assert any(e.startswith("VLBA_VETO_EVENTS is empty") for e in errors), errors
    assert not any("plain http" in e for e in errors), errors


# ---- #62 / #144: GLM coverage of the built-in sites -------------------------------------
@pytest.mark.parametrize("env, lost", [
    ({"VLBA_GLM_BUCKETS": "noaa-goes19=75.2;noaa-goes18=137.0"}, "br (Brewster"),     # signs dropped
    ({"VLBA_GLM_MAX_ARC": "6.2"}, "pt (Pie Town"),
    ({"VLBA_GLM_BUCKETS": "noaa-goes19=-75.2"}, "mk (Mauna Kea"),                    # GOES-East only
    ({"VLBA_GLM_BUCKETS": "noaa-goes19=-75.2;noaa-goes18=137.0"}, "mk (Mauna Kea"),  # one sign
    ({"VLBA_GLM_BUCKETS": ";"}, "pt (Pie Town"),                                     # no satellite
])
def test_glm_on_with_a_built_in_site_no_satellite_covers_is_refused(monkeypatch, tmp_path, env, lost):
    with pytest.raises(ValueError, match="VLBA_GLM_BUCKETS / VLBA_GLM_MAX_ARC") as e:
        _cfg(monkeypatch, tmp_path, **env)
    assert lost in str(e.value)
    # with lightning switched off the same values are no error
    c = _cfg(monkeypatch, tmp_path, VLBA_GLM="0", **env)
    assert c.check_enabled("glm") is False


def test_glm_coverage_follows_the_configured_sites(monkeypatch, tmp_path):
    # the defaults cover all twelve antennas
    assert _cfg(monkeypatch, tmp_path).check_enabled("glm")
    # Mauna Kea named through WEATHER_SITES is still a built-in antenna
    with pytest.raises(ValueError, match="mk"):
        _cfg(monkeypatch, tmp_path, WEATHER_SITES="mk|Mauna Kea|19.8014|-155.4555",
             VLBA_GLM_BUCKETS="noaa-goes19=-75.2")
    # another site outside every satellite's view keeps the documented "off"
    c = _cfg(monkeypatch, tmp_path, WEATHER_SITES="hilo|Hilo|19.72|-155.08", VLBA_GLM_BUCKETS="noaa-goes19=-75.2")
    assert c.glm_satellites == [("noaa-goes19", -75.2)]


# ---- #60: WEATHER_MRMS_ARCHIVE -----------------------------------------------------------
@pytest.mark.parametrize("value", [
    "https://mirror.example/mrms/lcref_latest.png",              # one fixed file
    "https://mirror.example/%Y/%m/%d/lcref_%Y%m%d%H.png",        # hourly
    "https://mirror.example/%Y/%m/%d/%H/%M/lcref.png",           # the time in the directory only
    "file:///etc/passwd", "lcref.png", "none", "ftp://mirror.example/lcref_%Y%m%d%H%M.png",
])
def test_an_mrms_archive_without_the_full_time_in_the_file_name_is_refused(monkeypatch, tmp_path, value):
    with pytest.raises(ValueError, match="WEATHER_MRMS_ARCHIVE"):
        _cfg(monkeypatch, tmp_path, WEATHER_MRMS_ARCHIVE=value)


def test_mrms_archive_patterns_with_the_full_time_are_accepted(monkeypatch, tmp_path):
    assert "%M.png" in _cfg(monkeypatch, tmp_path).mrms_archive
    v = "http://mirror.example:8080/mrms/%Y%m%d/lcref_%Y%m%d%H%M.png"
    assert _cfg(monkeypatch, tmp_path, WEATHER_MRMS_ARCHIVE=v).mrms_archive == v


# ---- #54 / #25 / #32: nothing private in the published directory -----------------------
def test_cache_lock_or_state_in_the_output_directory_is_refused(monkeypatch, tmp_path):
    _clear(monkeypatch)
    out = tmp_path / "www" / "vlbaweather"

    def make(**kw):
        kw.setdefault("out_dir", str(out))
        kw.setdefault("cache_dir", str(tmp_path / "cache"))
        return VlbaConfig.from_env(**kw)

    make()
    for kw in ({"cache_dir": str(out)}, {"cache_dir": str(out / "cache")},
               {"lock_file": str(out / "run.lock")}, {"state_file": str(out / "state" / "s.json")},
               {"out_dir": str(tmp_path / "cache"), "cache_dir": str(tmp_path / "cache")},
               {"out_dir": str(tmp_path / "cache" / "www")}):
        with pytest.raises(ValueError, match="WEATHER_OUT_DIR"):
            make(**kw)
    # through a symbolic link as well
    out.mkdir(parents=True)
    (tmp_path / "link").symlink_to(out)
    with pytest.raises(ValueError, match="WEATHER_CACHE_DIR"):
        make(cache_dir=str(tmp_path / "link" / "cache"))
    # a neighbour whose name merely begins like the output directory is no problem
    make(cache_dir=str(tmp_path / "www" / "vlbaweather-cache"))


# ---- #63 / #67 / #145: bounds ---------------------------------------------------------------
@pytest.mark.parametrize("env", [
    {"VLBA_VETO_ONSET_LEAD": "99999999999"}, {"VLBA_VETO_ONSET_LEAD": "604801"},
    {"VLBA_NWS_STALE_MIN": "99999999999999999999"}, {"VLBA_NWS_STALE_MIN": "10081"},
    {"VLBA_RADAR_STALE": "604801"}, {"VLBA_GRIDPOINT_MAX_STALE": "1e3"}, {"VLBA_GLM_TIMEOUT": "3601"},
    {"WEATHER_RUN_BUDGET": "99999999999999999999"}, {"WEATHER_RUN_BUDGET": "3601"},
    {"WEATHER_HTTP_RETRIES": "-1"}, {"WEATHER_HTTP_RETRIES": "6"},
    {"WEATHER_HTTP_TIMEOUT": "nan"}, {"WEATHER_HTTP_TIMEOUT": "inf"}, {"WEATHER_HTTP_TIMEOUT": "0"},
    {"WEATHER_HTTP_TIMEOUT": "-1"}, {"WEATHER_HTTP_TIMEOUT": "121"},
    {"WEATHER_RADAR_MAX_BACK": "-3"}, {"WEATHER_RADAR_MAX_BACK": "31"},
    {"WEATHER_ALERTS_MAX_STALE": "-1"}, {"WEATHER_ALERTS_MAX_STALE": "999999999"},
    {"WEATHER_ALERTS_MAX_STALE": "86401"},                         # a day at most: see below
    {"WEATHER_FORECAST_TTL": "-5"}, {"WEATHER_OBS_MAX_STALE": "604801"}, {"WEATHER_RADAR_MAX_STALE": "-1"},
    {"WEATHER_POINTS_TTL": "99999999999"},
    {"WEATHER_MAP_MILES": "1000", "WEATHER_TILE_ZOOM": "12"},       # 61500 tiles per map
    {"WEATHER_MAP_MILES": "200", "WEATHER_TILE_ZOOM": "11"},
])
def test_out_of_range_numbers_are_startup_errors(monkeypatch, tmp_path, env):
    with pytest.raises(ValueError):
        _cfg(monkeypatch, tmp_path, **env)


def test_the_bounds_admit_generous_values(monkeypatch, tmp_path):
    c = _cfg(monkeypatch, tmp_path, VLBA_VETO_ONSET_LEAD="604800", VLBA_NWS_STALE_MIN="10080",
             WEATHER_RUN_BUDGET="3600", WEATHER_HTTP_RETRIES="0", WEATHER_HTTP_TIMEOUT="1.5",
             WEATHER_RADAR_MAX_BACK="0", WEATHER_ALERTS_MAX_STALE="86400", VLBA_GLM_TIMEOUT="3600",
             WEATHER_MAP_MILES="100", WEATHER_TILE_ZOOM="11")
    assert (c.veto_onset_lead_s, c.run_budget_s, c.http_retries, c.http_timeout) == (604800, 3600, 0, 1.5)
    assert _cfg(monkeypatch, tmp_path, WEATHER_HTTP_RETRIES="5", WEATHER_HTTP_TIMEOUT="120").http_retries == 5


def _documented_values():
    """Every 'NAME=value' line of vlbaweather.env.example (commented out or not)."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "vlbaweather.env.example")
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^#?\s*((?:WEATHER|VLBA)_[A-Z0-9_]+)=(.*)$", line.rstrip("\n"))
            if m and m.group(2).strip():
                out.append((m.group(1), m.group(2).strip()))
    return out


def test_every_value_the_env_example_shows_is_accepted(monkeypatch, tmp_path):
    shown = _documented_values()
    assert len(shown) > 40
    for name, value in shown:
        if name in ("WEATHER_OUT_DIR", "WEATHER_CACHE_DIR", "WEATHER_LOCK_FILE", "VLBA_STATE_FILE"):
            continue                    # paths: the tests' own directories stand in
        _cfg(monkeypatch, tmp_path, **{name: value})


@pytest.mark.parametrize("name, field", [("WEATHER_RADAR", "radar_enabled"), ("WEATHER_SHOW_SUN", "show_sun"),
                                         ("WEATHER_TILE_DARK_INVERT", "tile_dark_invert"),
                                         ("WEATHER_VERBOSE", "verbose")])
def test_the_parents_switches_are_read_strictly(monkeypatch, tmp_path, name, field):
    for bad in ("n", "disabled", "2", "nope"):
        with pytest.raises(ValueError, match=name):
            _cfg(monkeypatch, tmp_path, **{name: bad})
    for word, val in (("0", False), ("off", False), ("No", False), ("1", True), ("yes", True)):
        assert getattr(_cfg(monkeypatch, tmp_path, **{name: word}), field) is val


# ---- #66: the paths are expanded alike ------------------------------------------------------
def test_out_cache_and_lock_paths_are_expanded_alike(monkeypatch, tmp_path):
    _clear(monkeypatch)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEATHER_OUT_DIR", "~/public_html/vlbaweather")
    monkeypatch.setenv("WEATHER_CACHE_DIR", "cache")
    monkeypatch.setenv("WEATHER_LOCK_FILE", "~/run.lock")
    monkeypatch.setenv("VLBA_STATE_FILE", "state.json")
    c = VlbaConfig.from_env()
    assert c.out_dir == str(home / "public_html" / "vlbaweather")
    assert c.cache_dir == str(tmp_path / "cache")
    assert c.lock_file == str(home / "run.lock") and c.state_file == str(tmp_path / "state.json")
    # the lock file's default follows the expanded cache directory
    monkeypatch.delenv("WEATHER_LOCK_FILE")
    monkeypatch.setenv("WEATHER_CACHE_DIR", "~/.cache/vlbaweather")
    c = VlbaConfig.from_env()
    assert c.lock_file == str(home / ".cache" / "vlbaweather" / "run.lock")


@pytest.mark.parametrize("name", ["WEATHER_OUT_DIR", "WEATHER_CACHE_DIR", "WEATHER_LOCK_FILE", "VLBA_STATE_FILE"])
@pytest.mark.parametrize("value", ["$HOME/www", "~no_such_user_x9/www", "/srv/${X}/w"])
def test_unexpanded_paths_are_refused(monkeypatch, tmp_path, name, value):
    _clear(monkeypatch)
    paths = {"WEATHER_OUT_DIR": str(tmp_path / "out"), "WEATHER_CACHE_DIR": str(tmp_path / "cache")}
    paths[name] = value
    for k, v in paths.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValueError, match=re.escape(name)):
        VlbaConfig.from_env()


# ---- #7: ASCII slugs ------------------------------------------------------------------------
@pytest.mark.parametrize("slug", ["piñ", "pt²", "٣x", "pé", "\u2167"])
def test_a_non_ascii_slug_is_refused(slug):
    with pytest.raises(ValueError, match="slug"):
        wc.parse_sites("%s|Pie Town|34.3|-108.1" % slug)
    assert wc.parse_sites("P-t_1|Pie Town|34.3|-108.1")[0]["slug"] == "P-t_1"


# ---- #17 / #57: plain-http sources of a verdict check ----------------------------------------
def test_plain_http_sources_of_a_verdict_check_are_listed(monkeypatch, tmp_path):
    assert _cfg(monkeypatch, tmp_path).insecure_sources() == []
    c = _cfg(monkeypatch, tmp_path, VLBA_METAR_URL="http://m.example/metar", VLBA_MRMS_URL="http://m.example/mrms",
             VLBA_NWSRADAR_URL="http://m.example/geoserver",
             WEATHER_MRMS_ARCHIVE="http://m.example/lcref_%Y%m%d%H%M.png",
             VLBA_MRMS_CONUS_MIRROR="http://m.example/%Y/SeamlessHSR_00.00_%Y%m%d-%H%M%S.grib2.gz")
    assert c.insecure_sources() == ["WEATHER_MRMS_ARCHIVE", "VLBA_MRMS_URL", "VLBA_MRMS_CONUS_MIRROR",
                                    "VLBA_NWSRADAR_URL", "VLBA_METAR_URL"]
    # a source that is not used is not listed
    c = _cfg(monkeypatch, tmp_path, VLBA_METAR_URL="http://m.example/metar", VLBA_OBS_PRECIP="0",
             VLBA_NWSRADAR_URL="http://m.example/geoserver", VLBA_ISLAND_RADAR="0")
    assert c.insecure_sources() == []


# ---- #167: no road layers on this page --------------------------------------------------------
def test_road_layers_are_forced_off_and_the_request_is_logged(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="vlba.config"):
        c = _cfg(monkeypatch, tmp_path, WEATHER_ROADS="1", WEATHER_LSR="yes")
    assert c.roads_enabled is False and c.lsr_enabled is False
    assert any("WEATHER_ROADS and WEATHER_LSR" in r.getMessage() and "ignored" in r.getMessage()
               for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="vlba.config"):
        c = _cfg(monkeypatch, tmp_path, WEATHER_ROADS="0")
    assert not caplog.records
    # the road variables the env example calls unused can no longer stop the page
    c = _cfg(monkeypatch, tmp_path, WEATHER_LSR_HOURS="0", WEATHER_NMROADS_JSON_URL="x")
    assert c.roads_enabled is False


# ---- S12: VLBA_METAR_URL ------------------------------------------------------------------------
@pytest.mark.parametrize("value", [
    "https://aviationweather.gov/api/data/metar?format=json", "https://aviationweather.gov/api/data/metar?",
    "https://aviationweather.gov/api/data/metar#x", "https://user:pw@10.0.0.5/metar",
    "https://proxy.example/me tar", "https:///metar", "https://proxy.example:port/metar", "metar.example/api",
])
def test_a_metar_url_with_query_credentials_or_blanks_is_refused(monkeypatch, tmp_path, value):
    with pytest.raises(ValueError, match="VLBA_METAR_URL"):
        _cfg(monkeypatch, tmp_path, VLBA_METAR_URL=value)


def test_plain_metar_base_urls_are_accepted(monkeypatch, tmp_path):
    for v in ("https://m.example:8443/api/data/metar/", "http://10.0.0.5/metar"):
        assert _cfg(monkeypatch, tmp_path, VLBA_METAR_URL=v).metar_url == v.rstrip("/")
    assert vc.check_url_base("https://aviationweather.gov/api/data/metar", "x")


def test_a_long_but_legal_alert_reuse_is_reported_on_every_run(monkeypatch, tmp_path, cfg, fake_http):
    """#145: WEATHER_ALERTS_MAX_STALE=24000 (a digit too many) is legal, but a 6.7-h-old feed
    answers "no warning in effect": a warning and a run error on every run."""
    assert _cfg(monkeypatch, tmp_path, WEATHER_ALERTS_MAX_STALE="10800").doubtful_settings() == []
    c = _cfg(monkeypatch, tmp_path, WEATHER_ALERTS_MAX_STALE="24000")
    assert len(c.doubtful_settings()) == 1 and "WEATHER_ALERTS_MAX_STALE=24000" in c.doubtful_settings()[0]
    assert _cfg(monkeypatch, tmp_path, WEATHER_ALERTS_MAX_STALE="24000", VLBA_VETO_EVENTS="none").doubtful_settings() == []
    cfg.glm_enabled = False
    cfg.alerts_max_stale = 24000
    assert any(e.startswith("WEATHER_ALERTS_MAX_STALE=24000") for e in mvp.run_once(cfg)["errors"])


# ---- review: the keyword overrides are expanded too ------------------------------------------
def test_out_and_cache_overrides_are_expanded_like_the_environment(monkeypatch, tmp_path):
    """VlbaConfig.from_env applies its keyword overrides again after the parent's validate():
    a "~" or relative out_dir / cache_dir passed that way must still come out absolute."""
    _clear(monkeypatch)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    c = VlbaConfig.from_env(out_dir="~/www/vlbaweather", cache_dir="cache")
    assert c.out_dir == str(home / "www" / "vlbaweather")
    assert c.cache_dir == str(tmp_path / "cache")
    assert c.lock_file == str(tmp_path / "cache" / "run.lock")
    assert c.state_file == str(tmp_path / "cache" / "vlba_state.json")
