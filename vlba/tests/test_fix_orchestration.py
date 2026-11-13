"""The run's orchestration (make_vlba_page._run_site / _run_locked): a step that fails never
reads as an answer, the order in which the outputs are written, and that nothing published
names a local path, a user or a process id."""
from __future__ import annotations

import builtins
import errno
import json
import os

import pytest

import make_vlba_page as mvp                     # noqa: E402  (conftest put vlbaweather on sys.path)
from vlba import decision, glm, maps, page
from vlba.config import VlbaConfig
from vlba.tests.conftest import PT, TEST_SITES
from vlba.tests.test_main import add_routes, alert_feature, read_page, read_status
from weather import pipeline as mwp
from weather import radar as wradar


def _states(entry):
    return {c["key"]: c["state"] for c in (entry["verdict"] or {}).get("checks") or []}


def _site(run, slug):
    return [s for s in run["sites"] if s["slug"] == slug][0]


def _boom(*a, **k):
    raise RuntimeError("injected failure")


@pytest.fixture
def no_glm(monkeypatch):
    monkeypatch.setattr(glm, "deps_available", lambda: False)


# ---- design 1: every failed step reads "no data" -------------------------------------------------
def test_failed_alert_classification_makes_the_warning_check_unknown(cfg, fake_http, monkeypatch, no_glm):
    add_routes(fake_http, cfg)
    monkeypatch.setattr(mvp.alerts, "site_alerts", _boom)
    run = mvp.run_once(cfg)
    for s in run["sites"]:
        assert s["alerts_feed"]["ok"] is False and s["alerts_feed"]["stale"] is True
        assert s["alerts_feed"]["error"] == "alert classification failed"
        assert _states(s)["warning"] == "unknown"
        assert s["verdict"]["incomplete"]
    assert any("alert classification failed" in e for e in run["errors"])


def test_a_warning_at_the_site_still_vetoes_when_another_site_is_unclassified(cfg, fake_http, monkeypatch, no_glm):
    """The not-ok feed of a failed classification never hides a warning that is AT the site."""
    poly = [[PT["lon"] - 0.2, PT["lat"] - 0.2], [PT["lon"] + 0.2, PT["lat"] - 0.2],
            [PT["lon"] + 0.2, PT["lat"] + 0.2], [PT["lon"] - 0.2, PT["lat"] + 0.2], [PT["lon"] - 0.2, PT["lat"] - 0.2]]
    add_routes(fake_http, cfg, alerts={"type": "FeatureCollection",
                                       "features": [alert_feature("w1", "High Wind Warning", geometry=poly)]})
    real = mvp.alerts.site_alerts

    def only_pt(alerts, site, meta, frame):
        if site["slug"] != "pt":
            raise RuntimeError("injected")
        return real(alerts, site, meta, frame)
    monkeypatch.setattr(mvp.alerts, "site_alerts", only_pt)
    run = mvp.run_once(cfg)
    assert _states(_site(run, "pt"))["warning"] == "bad" and _site(run, "pt")["verdict"]["status"] == "BAD"
    assert _states(_site(run, "mk"))["warning"] == "unknown"


def test_skipped_alerts_of_the_classification_are_passed_on(cfg, fake_http, monkeypatch, no_glm):
    """CONTRACT with the alerts slice: site_alerts may name events it could not place."""
    add_routes(fake_http, cfg)
    monkeypatch.setattr(mvp.alerts, "site_alerts",
                        lambda alerts, site, meta, frame: {"at": [], "candidates": [], "skipped": ["Flood Advisory"]})
    run = mvp.run_once(cfg)
    assert _site(run, "pt")["alerts_skipped"] == ["Flood Advisory"]
    assert _site(run, "pt")["alerts_feed"]["ok"] is True


def test_a_failed_station_step_makes_the_station_checks_unknown(cfg, fake_http, monkeypatch, no_glm):
    add_routes(fake_http, cfg)
    monkeypatch.setattr(mvp, "_station_info", _boom)
    run = mvp.run_once(cfg)
    pt = _site(run, "pt")
    assert pt["obs_station"]["usable"] is False and pt["obs_station"]["qualifying"] is None
    states = _states(pt)
    assert states["obs_precip"] == "unknown" and states["obs_humidity"] in ("unknown", "off")
    assert pt["verdict"]["incomplete"]
    # Mauna Kea's station is just as unknown: no "not provided" from a list nobody read
    assert _states(_site(run, "mk"))["obs_precip"] == "unknown"


def test_no_observation_and_no_station_list_is_still_no_station(cfg, fake_http, monkeypatch, no_glm):
    """None from _station_info is an answer (nothing to place), not a failed step."""
    add_routes(fake_http, cfg)
    monkeypatch.setattr(mvp, "_station_info", lambda *a, **k: None)
    run = mvp.run_once(cfg)
    assert _site(run, "pt")["obs_station"] is None
    assert not any("station" in e for e in run["errors"])


def test_a_failed_raw_observation_makes_the_precipitation_check_unknown(cfg, fake_http, monkeypatch, no_glm):
    add_routes(fake_http, cfg)
    monkeypatch.setattr(mvp, "_raw_observation", _boom)
    run = mvp.run_once(cfg)
    pt = _site(run, "pt")
    assert pt["obs_station"]["weather"] == "unknown"
    assert _states(pt)["obs_precip"] == "unknown" and pt["verdict"]["incomplete"]


# S19: the three steps whose fallback dicts decide a check
@pytest.mark.parametrize("target, attr, key", [
    (mvp, "_radar_for_site", "radar"),
    (glm, "site_view", "glm"),
    (mvp, "_precip_report", "obs_precip"),
])
def test_a_failed_step_reads_unknown(cfg, fake_http, monkeypatch, target, attr, key):
    add_routes(fake_http, cfg)
    monkeypatch.setattr(glm, "deps_available", lambda: True)
    monkeypatch.setattr(glm, "fetch", lambda *a, **k: mvp._glm_fallback("down"))
    monkeypatch.setattr(target, attr, _boom)
    run = mvp.run_once(cfg)
    pt = _site(run, "pt")
    assert _states(pt)[key] == "unknown"
    assert pt["verdict"]["incomplete"] and pt["verdict"]["status"] in ("GOOD", "BAD")
    assert any("injected failure" in e for e in run["errors"])


# ---- design 2: an alert query that leaves out the site's state (#61) --------------------------
def test_a_site_outside_the_alert_query_has_no_warning_answer(tmp_path, fake_http, monkeypatch, no_glm):
    for k in list(os.environ):
        if k.startswith(("WEATHER_", "VLBA_")):
            monkeypatch.delenv(k, raising=False)
    cfg = VlbaConfig.from_env(out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "cache"), map_px=240,
                              tile_zoom=8, sites=[dict(s) for s in TEST_SITES], alert_areas="NM")
    add_routes(fake_http, cfg)
    run = mvp.run_once(cfg)
    mk = _site(run, "mk")
    assert mk["alerts_feed"]["ok"] is False and "HIZ028" in mk["alerts_feed"]["error"]
    assert _states(mk)["warning"] == "unknown" and mk["verdict"]["incomplete"]
    assert _site(run, "pt")["alerts_feed"]["ok"] is True and _states(_site(run, "pt"))["warning"] == "ok"


# ---- design 3: the order of the writes (#86, #131, #135) -------------------------------------
def test_a_failed_page_write_still_rewrites_status_json(cfg, fake_http, monkeypatch, no_glm):
    add_routes(fake_http, cfg)
    mvp.run_once(cfg)
    assert read_status(cfg)["page_written"] is True
    old_gen = read_status(cfg)["generated"]
    os.utime(os.path.join(cfg.out_dir, "index.html"), (1e9, 1e9))   # the last good page, long ago

    def full(cfg, run):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), os.path.join(cfg.out_dir, "index.html.tmp.777013579"))
    monkeypatch.setattr(page, "write_page", full)
    with pytest.raises(OSError):
        mvp.run_once(cfg)
    st = read_status(cfg)
    assert st["generated"] != old_gen
    assert st["page_written"] is False and st["ok"] is False
    assert any("index.html not written" in e and "ENOSPC" in e for e in st["errors"])
    assert cfg.out_dir not in json.dumps(st) and "777013579" not in json.dumps(st)
    assert mvp.main(["--out", cfg.out_dir, "--cache", cfg.cache_dir, "--sites", "pt|Pie Town|34.301|-108.1192"]) == 1


# ---- design 4: maps wait until the page is written (#133) -------------------------------------
def test_maps_of_a_run_that_dies_before_its_page_are_not_published(cfg, fake_http, monkeypatch, no_glm):
    add_routes(fake_http, cfg)
    mvp.run_once(cfg)
    published = os.path.join(cfg.out_dir, cfg.map_basename("pt", "dark"))
    with open(published, "rb") as f:
        before = f.read()
    os.utime(published, (1e9, 1e9))
    # the second run draws a different map (an echo now), then dies before its page
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: __import__(
        "vlba.tests.test_main", fromlist=["_mrms_result"])._mrms_result([(PT["lat"] + 0.09, PT["lon"])]))
    monkeypatch.setattr(decision, "summary", _boom)
    with pytest.raises(RuntimeError):
        mvp.run_once(cfg)
    with open(published, "rb") as f:
        assert f.read() == before
    assert os.path.getmtime(published) == 1e9
    # the next run that completes publishes its maps and no pending file stays behind
    monkeypatch.undo()
    add_routes(fake_http, cfg)
    monkeypatch.setattr(glm, "deps_available", lambda: False)
    mvp.run_once(cfg)
    assert os.path.getmtime(published) > 1e9
    assert not [n for n in os.listdir(cfg.out_dir) if n.endswith(".next") or ".tmp." in n]


def test_a_map_that_cannot_be_published_is_reported(cfg, fake_http, monkeypatch, no_glm):
    add_routes(fake_http, cfg)
    real = os.replace

    light = os.path.join(cfg.out_dir, cfg.map_basename("pt", "light"))

    def replace(src, dst):
        if dst == light:
            raise PermissionError(errno.EACCES, "Permission denied", dst)
        return real(src, dst)
    monkeypatch.setattr(mvp.os, "replace", replace)
    run = mvp.run_once(cfg)
    pt = _site(run, "pt")
    assert pt["maps"]["light"]["ok"] is False and "not published" in pt["maps"]["light"]["error"]
    assert pt["maps"]["dark"]["ok"] is True and not os.path.exists(light)
    assert any(e.startswith("%s not published" % cfg.map_basename("pt", "light")) for e in run["errors"])
    _assert_private(cfg)


# ---- design 5: nothing published names a path, a user or a pid (#1, #21, #49, #64, #80, #127) ------
def _assert_private(cfg, extra=()):
    texts = [read_page(cfg), json.dumps(read_status(cfg))]
    for t in texts:
        for p in (cfg.cache_dir, cfg.out_dir, os.path.dirname(cfg.state_file), os.path.expanduser("~")) + tuple(extra):
            assert p not in t, p
        assert "777013579" not in t          # the pid of the temp file


def test_a_full_cache_disk_publishes_no_path(cfg, fake_http, monkeypatch, no_glm):
    add_routes(fake_http, cfg)
    real_open = builtins.open

    def full_open(path, *a, **k):
        if isinstance(path, str) and path.startswith(cfg.cache_dir) and ".tmp." in path:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), path)
        return real_open(path, *a, **k)
    monkeypatch.setattr(decision, "open", full_open, raising=False)
    # the MRMS frame's cache write fails the same way (its error text quotes the temp file)
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: {
        "ok": False, "stale": True, "age_s": None, "ts": None, "url": None, "image": None,
        "error": "PermissionError: [Errno 13] Permission denied: '%s/mrms_last.png.tmp.777013579'" % cfg.cache_dir})
    run = mvp.run_once(cfg)
    st = read_status(cfg)
    assert any(e.startswith("state file not written ([ENOSPC] No space left on device)") for e in st["errors"])
    assert any("<cache>/mrms_last.png.tmp'" in e for e in st["errors"])
    _assert_private(cfg)
    assert run["sites"][0]["verdict"] is not None


def test_a_full_output_disk_publishes_no_path(cfg, fake_http, monkeypatch, no_glm):
    add_routes(fake_http, cfg)
    real_open = builtins.open

    def full_open(path, *a, **k):
        if isinstance(path, str) and path.startswith(cfg.out_dir) and ".png" in path:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), path + ".tmp.777013579")
        return real_open(path, *a, **k)
    monkeypatch.setattr(wradar, "open", full_open, raising=False)
    run = mvp.run_once(cfg)
    assert not _site(run, "pt")["maps"]["dark"]["ok"]
    assert "Radar map unavailable" in read_page(cfg)
    _assert_private(cfg)


def test_an_exception_in_a_site_publishes_no_path(cfg, fake_http, monkeypatch, no_glm):
    add_routes(fake_http, cfg)

    def denied(*a, **k):
        raise PermissionError(errno.EACCES, "Permission denied", os.path.join(cfg.cache_dir, "x.tmp.777013579"))
    monkeypatch.setattr(mvp, "_run_site", denied)
    run = mvp.run_once(cfg)
    assert run["sites"][0]["verdict"] is None
    assert "site pt failed: PermissionError: [EACCES] Permission denied" in run["errors"]
    _assert_private(cfg)


def test_exc_text_of_an_oserror_names_no_file():
    e = PermissionError(errno.EACCES, "Permission denied", "/home/admin/.cache/vlbaweather/a.tmp.777013579")
    assert mwp._exc_text(e) == "PermissionError: [EACCES] Permission denied"
    assert mwp._exc_text(OSError("plain")) == "OSError: plain"
    assert mwp._exc_text(ValueError("x")) == "ValueError: x"


def test_public_text_replaces_local_paths_only(tmp_path, monkeypatch):
    home = tmp_path / "home" / "admin"
    monkeypatch.setenv("HOME", str(home))

    class C:
        cache_dir = str(home / ".cache" / "vlbaweather")
        out_dir = "/srv/www/vlbaweather"
        state_file = str(home / ".cache" / "vlbaweather" / "vlba_state.json")
    subs = mwp._public_paths(C)
    t = mwp._public_text
    assert t("x '%s/a.png.tmp.123' y" % C.cache_dir, subs) == "x '<cache>/a.png.tmp' y"
    assert t("%s/notes" % home, subs) == "~/notes"
    assert t("/srv/www/vlbaweather/index.html", subs) == "<out>/index.html"
    assert t("/srv/www/vlbaweather2/x", subs) == "/srv/www/vlbaweather2/x"
    assert t("cannot write /srv/www/vlbaweather.", subs) == "cannot write <out>."
    assert t("/srv/www/vlbaweather.old/x", subs) == "/srv/www/vlbaweather.old/x"
    assert t("https://example.org/srv/www/vlbaweather/x", subs) == "https://example.org/srv/www/vlbaweather/x"
    assert t("<PIL.Image image mode=P at 0x7F12AB34CD56>", subs) == "<PIL.Image image mode=P>"
    assert t("https://user:secret@example.org/a", subs) == "https://example.org/a"
    run = {"errors": ["%s/x" % C.cache_dir], "sites": [{"a": ["%s" % C.out_dir, 3, None]}], "n": 1.5}
    mwp._scrub_run(C, run, subs)
    assert run == {"errors": ["<cache>/x"], "sites": [{"a": ["<out>", 3, None]}], "n": 1.5}


# ---- design 6: a map whose decorations failed (#157) ------------------------------------------
def test_failed_map_decorations_are_reported(cfg, fake_http, monkeypatch, no_glm):
    add_routes(fake_http, cfg)
    monkeypatch.setattr(maps.VlbaSiteMap, "_decorate", _boom)
    run = mvp.run_once(cfg)
    pt = _site(run, "pt")
    assert pt["maps"]["dark"]["ok"]
    assert any(e.startswith("pt dark map: decorations failed") for e in run["errors"])
    assert any("decorations failed" in n for n in pt["map_notes"])


# ---- design 7: WEATHER_RADAR=0 is off, not missing (S16) ---------------------------------------
def test_radar_switched_off_is_marked_disabled(tmp_path, cfg, monkeypatch):
    off = VlbaConfig.from_env(out_dir=str(tmp_path / "o2"), cache_dir=str(tmp_path / "c2"), map_px=240,
                              tile_zoom=8, sites=[dict(s) for s in TEST_SITES], radar_enabled=False)
    run = {"errors": []}
    assert mwp._load_radar(off, None, run, False).get("disabled") is True
    assert not mwp._load_radar(cfg, None, run, True).get("disabled")      # --skip-radar: still a problem


# ---- #3: an alert id that is a list costs the site nothing ---------------------------------------
def test_an_alert_with_a_list_id_keeps_the_site_verdict(cfg, fake_http, monkeypatch, no_glm):
    poly = [[PT["lon"] - 0.2, PT["lat"] - 0.2], [PT["lon"] + 0.2, PT["lat"] - 0.2],
            [PT["lon"] + 0.2, PT["lat"] + 0.2], [PT["lon"] - 0.2, PT["lat"] + 0.2], [PT["lon"] - 0.2, PT["lat"] - 0.2]]
    near = [[PT["lon"] + 0.3, PT["lat"] + 0.3], [PT["lon"] + 0.5, PT["lat"] + 0.3],
            [PT["lon"] + 0.5, PT["lat"] + 0.5], [PT["lon"] + 0.3, PT["lat"] + 0.3]]
    at_f = alert_feature("w1", "High Wind Warning", geometry=poly)
    near_f = alert_feature("w2", "Flood Advisory", geometry=near)
    for f in (at_f, near_f):
        f["id"] = ["x"]
        f["properties"]["id"] = ["x"]
    add_routes(fake_http, cfg, alerts={"type": "FeatureCollection", "features": [at_f, near_f]})
    run = mvp.run_once(cfg)
    pt = _site(run, "pt")
    assert pt["verdict"] is not None and pt["verdict"]["status"] == "BAD"
    assert not any(e.startswith("site pt failed") for e in run["errors"])


def test_the_placeholder_of_a_failed_site_has_no_alert_answer(cfg):
    entry = mvp._site_placeholder(cfg, dict(TEST_SITES[0]), "boom")
    assert entry["verdict"] is None and entry["alerts_feed"]["ok"] is False


def test_maps_of_a_site_that_failed_late_are_not_published(cfg, fake_http, monkeypatch, no_glm):
    add_routes(fake_http, cfg)
    real = mvp._render_maps

    def render_then_fail(cfg, cache, run, site, *a, **k):
        res = real(cfg, cache, run, site, *a, **k)
        if site["slug"] == "pt":
            raise RuntimeError("after the maps")
        return res
    monkeypatch.setattr(mvp, "_render_maps", render_then_fail)
    run = mvp.run_once(cfg)
    assert _site(run, "pt")["verdict"] is None
    assert not os.path.exists(os.path.join(cfg.out_dir, cfg.map_basename("pt", "dark")))
    assert os.path.exists(os.path.join(cfg.out_dir, cfg.map_basename("mk", "dark")))
