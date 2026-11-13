"""Acceptance tests for the audit fixes, written independently of the fixes themselves: on
the code as audited all but the healthy-run control fail. They check outcomes, not wording,
wherever they can."""
from __future__ import annotations

import ast
import errno
import json
import os
import re
import stat
import subprocess
import time
import urllib.request

import pytest

from weather import geo, http
import make_vlba_page as mvp
from vlba import glm
from vlba.config import VlbaConfig
from vlba.tests.conftest import MK, VLBAWEATHER_DIR
from vlba.tests.test_main import (_glm_result, _mrms_result, add_routes, alert_feature, gridpoint_doc,
                                  read_page, read_status)

_REAL_URLOPEN = urllib.request.urlopen          # saved before the autouse network block applies

EPS = (b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 16 16\n"
       b"0 0 moveto 16 16 lineto stroke\nshowpage\n%%EOF\n")


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result(()))
    monkeypatch.setattr(glm, "deps_available", lambda: True)
    monkeypatch.setattr(glm, "fetch", lambda cfg, now=None, since=None, retried=None, seen=None: _glm_result([]))


def _checks(entry):
    return {c["key"]: c["state"] for c in entry["verdict"]["checks"]}


# ---- S0: upstream bytes never choose the image codec ---------------------------------------
def _image_open_calls(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "open" \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "Image":
            yield node


def test_accept_S0_every_image_open_names_its_formats():
    missing = []
    for top in ("weather", "vlba", "make_vlba_page.py"):
        base = os.path.join(VLBAWEATHER_DIR, top)
        paths = [base] if base.endswith(".py") else [
            os.path.join(d, f) for d, _, fs in os.walk(base) if os.sep + "tests" not in d + os.sep
            for f in fs if f.endswith(".py")]
        for p in paths:
            for call in _image_open_calls(p):
                if not any(k.arg == "formats" for k in call.keywords):
                    missing.append("%s:%d" % (os.path.relpath(p, VLBAWEATHER_DIR), call.lineno))
    assert not missing, "Image.open without formats=: %s" % missing


def test_accept_S0_an_eps_reply_never_starts_ghostscript(cfg, monkeypatch):
    launched = []

    def spy(*a, **k):
        launched.append(a[:1])
        raise OSError("blocked by the test")
    for name in ("Popen", "check_call", "run", "call", "check_output"):
        monkeypatch.setattr(subprocess, name, spy)
    from weather import radar
    from vlba import nwsradar
    for fn in (lambda: radar._decode_tile(EPS), lambda: radar._open_frame(EPS)):
        with pytest.raises(Exception):
            fn()
    frame = geo.MapFrame(MK["lat"], MK["lon"], cfg.map_km, cfg.map_px, cfg.tile_zoom)
    with pytest.raises(Exception):
        nwsradar._decoded_result(cfg, EPS, frame, "hawaii", {}, layer=True)
    assert not launched, "Ghostscript (or another program) was started: %r" % launched


# ---- #59: radar settings under which an echo can never be confirmed ------------------------
@pytest.mark.parametrize("env", [{"VLBA_RADAR_STALE": "300"}, {"VLBA_RADAR_TRIGGER_AFTER": "5"}])
def test_accept_59_settings_that_can_never_confirm_are_refused(tmp_path, monkeypatch, env):
    for k in list(os.environ):
        if k.startswith(("WEATHER_", "VLBA_")):
            monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValueError):
        VlbaConfig.from_env(out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "cache"))


# ---- a healthy run stays GOOD and complete (no fix may make healthy data read "no data") ----
def test_accept_healthy_quiet_run_is_good_and_complete(cfg, fake_http, quiet):
    add_routes(fake_http, cfg)
    run = mvp.run_once(cfg)
    for entry in run["sites"]:
        states = _checks(entry)
        assert entry["verdict"]["status"] == "GOOD", (entry["slug"], states, entry["verdict"].get("reasons"))
        assert not entry["verdict"]["incomplete"], (entry["slug"], states)


# ---- #78 / #106: one malformed alert must not blind the warning check ----------------------
BOX = [[-108.3, 34.2], [-108.0, 34.2], [-108.0, 34.4], [-108.3, 34.4], [-108.3, 34.2]]
STRINGS = [["-80.0", "35.0"], ["-79.0", "35.0"], ["-79.0", "36.0"], ["-80.0", "35.0"]]


def test_accept_78_a_malformed_alert_elsewhere_keeps_the_veto(cfg, fake_http, quiet):
    feed = {"type": "FeatureCollection", "features": [
        alert_feature("hww", "High Wind Warning", geometry=BOX),
        alert_feature("odd", "Flood Advisory", geometry=STRINGS)]}
    add_routes(fake_http, cfg, alerts=feed)
    run = mvp.run_once(cfg)
    pt = run["sites"][0]
    assert pt["verdict"]["status"] == "BAD", _checks(pt)
    assert _checks(pt)["warning"] == "bad"


def test_accept_78_an_unplaceable_veto_alert_is_not_clear(cfg, fake_http, quiet):
    veto = cfg.veto_event_names[0]
    feed = {"type": "FeatureCollection", "features": [alert_feature("odd", veto, geometry=STRINGS)]}
    add_routes(fake_http, cfg, alerts=feed)
    run = mvp.run_once(cfg)
    for entry in run["sites"]:
        assert _checks(entry)["warning"] != "ok", (entry["slug"], veto, _checks(entry))


# ---- #0: one lone surrogate in upstream text does not stop the page ------------------------
def test_accept_0_a_lone_surrogate_does_not_stop_the_page(cfg, fake_http, quiet):
    feat = alert_feature("sws", "Special Weather Statement", geometry=BOX)
    feat["properties"]["headline"] = "Special Weather Statement \ud83d issued"
    feat["properties"]["description"] = "Gusty \ud83d winds."
    add_routes(fake_http, cfg, alerts={"type": "FeatureCollection", "features": [feat]})
    mvp.run_once(cfg)
    with open(os.path.join(cfg.out_dir, "index.html"), "rb") as f:
        f.read().decode("utf-8")            # strict: the file is valid UTF-8
    with open(os.path.join(cfg.out_dir, "status.json"), "rb") as f:
        json.loads(f.read().decode("utf-8"))


# ---- #68 / #107: an empty gridpoint series is "no data", not "n/a" -------------------------
def test_accept_68_an_empty_series_is_not_n_a(cfg, fake_http, quiet):
    grid = gridpoint_doc()
    grid["properties"]["skyCover"] = {"uom": "wmoUnit:percent", "values": []}
    add_routes(fake_http, cfg, pt_grid=grid)
    run = mvp.run_once(cfg)
    pt = run["sites"][0]
    assert _checks(pt)["forecast_cloud"] == "unknown", _checks(pt)
    assert pt["verdict"]["incomplete"]


# ---- #126: the page carries its age and the limit -------------------------------------------
def test_accept_126_the_page_carries_its_age(cfg, fake_http, quiet):
    add_routes(fake_http, cfg)
    mvp.run_once(cfg)
    html = read_page(cfg)
    m = re.search(r'data-generated="(\d+(?:\.\d+)?)"', html)
    assert m and abs(float(m.group(1)) - time.time()) < 600, "no generation epoch on the page"
    m2 = re.search(r'data-stale-after="(\d+(?:\.\d+)?)"', html)
    assert m2 and float(m2.group(1)) > 0
    st = read_status(cfg)
    assert st.get("stale_after_s", 0) > 0


# ---- #2 / #14: only http(s) reaches the network layer ----------------------------------------
def test_accept_2_a_file_url_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _REAL_URLOPEN)
    p = tmp_path / "local.json"
    p.write_text('{"secret": 1}')
    cfg = VlbaConfig.from_env(out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "cache"))
    with pytest.raises(http.HttpError) as ei:
        http.get_json("file://" + str(p), cfg)
    assert str(tmp_path) not in str(ei.value)


# ---- #9 / #139: a cache stamp far in the future counts as absent ---------------------------
def test_accept_9_a_future_cache_stamp_is_absent(tmp_path):
    c = http.Cache(str(tmp_path / "c"))
    c.put("far", {"a": 1}, ts=time.time() + 3600)
    assert c.get_any("far")[0] is None
    c.put("near", {"a": 1}, ts=time.time() + 30)    # small skew: still usable
    assert c.get_any("near")[0] == {"a": 1}


# ---- #137 / #12 / #19 / #20: the run lock ----------------------------------------------------
def test_accept_137_the_lock_is_private_and_only_ewouldblock_means_running(tmp_path, monkeypatch):
    path = str(tmp_path / "run.lock")
    with open(path, "w") as f:
        f.write("old\n")
    os.chmod(path, 0o644)                       # as older versions left it
    with http.run_lock(path):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    real = http.fcntl.flock

    def failing(code):
        def fl(fd, op):
            if op & http.fcntl.LOCK_EX:
                raise OSError(code, os.strerror(code))
            return real(fd, op)
        return fl
    monkeypatch.setattr(http.fcntl, "flock", failing(errno.EWOULDBLOCK))
    with pytest.raises(http.AlreadyRunning):
        with http.run_lock(path):
            pass
    monkeypatch.setattr(http.fcntl, "flock", failing(errno.ENOLCK))
    with pytest.raises(OSError) as ei:
        with http.run_lock(path):
            pass
    assert not isinstance(ei.value, http.AlreadyRunning)


def test_accept_137_a_lock_error_is_a_failed_run(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith(("WEATHER_", "VLBA_")):
            monkeypatch.delenv(k, raising=False)
    real = http.fcntl.flock

    def fl(fd, op):
        if op & http.fcntl.LOCK_EX:
            raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))
        return real(fd, op)
    monkeypatch.setattr(http.fcntl, "flock", fl)
    rc = mvp.main(["--out", str(tmp_path / "out"), "--cache", str(tmp_path / "cache"), "--skip-glm", "--skip-radar"])
    assert rc == 1
