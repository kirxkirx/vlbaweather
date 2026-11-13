"""The run lock, the cache's time stamps, the state file and the leftover temporary files
(audit findings #137 #12 #19 #20, #139 #9 #18 #114, #141 #26, #27)."""
from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import stat
import time

import pytest

from weather import http
import make_vlba_page as mvp                     # noqa: E402  (conftest put vlbaweather on sys.path)
from vlba import decision as d


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _cli(tmp_path):
    return ["--out", str(tmp_path / "o"), "--cache", str(tmp_path / "c")]


@pytest.fixture
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("WEATHER_", "VLBA_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WEATHER_USER_AGENT", "vlbaweather-test (+https://example.invalid/not-a-placeholder)")


# ---- the run lock (#137 #12 #19 #20) -------------------------------------------------------
def test_lock_file_is_private_and_stamped(tmp_path):
    p = str(tmp_path / "c" / "run.lock")
    before = time.time()
    with http.run_lock(p):
        assert _mode(p) == 0o600
        pid, started = open(p).read().split()
        assert int(pid) == os.getpid() and started.endswith("Z")
        assert os.stat(p).st_mtime >= before - 2
    assert _mode(p) == 0o600


def test_an_older_wider_lock_file_is_narrowed(tmp_path):
    p = str(tmp_path / "run.lock")
    with open(p, "w") as f:
        f.write("123\n")
    os.chmod(p, 0o644)                       # as versions before this one created it
    with http.run_lock(p):
        assert _mode(p) == 0o600


def test_only_a_held_lock_is_already_running(tmp_path):
    p = str(tmp_path / "run.lock")
    with http.run_lock(p):
        with pytest.raises(http.AlreadyRunning) as ei:
            with http.run_lock(p):
                pass
    assert ei.value.held_s is not None and 0 <= ei.value.held_s < 60


@pytest.mark.parametrize("code", [errno.ENOLCK, errno.ENOSYS, errno.EIO, errno.EOPNOTSUPP, errno.EBADF])
def test_any_other_flock_error_is_an_error(tmp_path, monkeypatch, code):
    def broken(fd, op):
        if op & fcntl.LOCK_EX:
            raise OSError(code, os.strerror(code))
    monkeypatch.setattr(http.fcntl, "flock", broken)
    with pytest.raises(http.LockError) as ei:
        with http.run_lock(str(tmp_path / "run.lock")):
            pass
    assert not isinstance(ei.value, http.AlreadyRunning) and ei.value.errno == code


def test_lock_file_that_is_a_link_a_fifo_or_someone_elses_is_refused(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere"
    target.write_text("x")
    link = tmp_path / "link.lock"
    os.symlink(str(target), str(link))
    with pytest.raises(http.LockError, match="symbolic link"):
        with http.run_lock(str(link)):
            pass
    assert target.read_text() == "x"
    fifo = str(tmp_path / "fifo.lock")
    os.mkfifo(fifo)
    with pytest.raises(http.LockError, match="not a regular file"):
        with http.run_lock(fifo):
            pass
    me = os.geteuid()
    monkeypatch.setattr(http.os, "geteuid", lambda: me + 1)
    with pytest.raises(http.LockError, match="owned by this user"):
        with http.run_lock(str(tmp_path / "mine.lock")):
            pass


def test_main_exit_codes_of_the_lock(tmp_path, monkeypatch, clean_env, caplog):
    """A run holding the lock briefly: INFO, exit 0. Held for more than one interval: ERROR,
    exit 1, so cron and systemd show the failure. A failing flock: ERROR, exit 1."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(mvp, "_run_locked", lambda cfg, skip_radar: {})
    lock = str(tmp_path / "c" / "run.lock")
    with http.run_lock(lock):
        caplog.clear()
        assert mvp.main(_cli(tmp_path)) == 0
        rec = [r for r in caplog.records if "already running" in r.getMessage()]
        assert rec and rec[-1].levelno == logging.INFO
        old = time.time() - 2 * 3600
        os.utime(lock, (old, old))           # the holder took it two hours ago
        caplog.clear()
        assert mvp.main(_cli(tmp_path)) == 1
        rec = [r for r in caplog.records if "held the lock for" in r.getMessage()]
        assert rec and rec[-1].levelno == logging.ERROR and "for 120 min" in rec[-1].getMessage()
        old = time.time() - (mvp.LOCK_HELD_ERROR_S - 120)
        os.utime(lock, (old, old))
        assert mvp.main(_cli(tmp_path)) == 0
    assert mvp.main(_cli(tmp_path)) == 0     # released: the run goes ahead

    def enolck(fd, op):
        if op & fcntl.LOCK_EX:
            raise OSError(errno.ENOLCK, "No locks available")
    monkeypatch.setattr(http.fcntl, "flock", enolck)
    caplog.clear()
    assert mvp.main(_cli(tmp_path)) == 1
    assert any(r.levelno == logging.ERROR and "cannot lock" in r.getMessage() for r in caplog.records)
    assert not any("already running" in r.getMessage() for r in caplog.records)


def test_a_reader_that_held_the_lock_before_the_upgrade_shows_as_an_error(tmp_path, monkeypatch, clean_env):
    """Another account that opened the old 0644 file and sleeps on LOCK_EX: the mode is
    narrowed at once (nobody can open it again) and, once the hold is older than one
    interval, every run exits 1."""
    monkeypatch.setattr(mvp, "_run_locked", lambda cfg, skip_radar: {})
    os.makedirs(str(tmp_path / "c"))
    lock = str(tmp_path / "c" / "run.lock")
    with open(lock, "w") as f:
        f.write("1\n")
    os.chmod(lock, 0o644)
    fd = os.open(lock, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert mvp.main(_cli(tmp_path)) == 0
        assert _mode(lock) == 0o600
        old = time.time() - 3600
        os.utime(lock, (old, old))
        assert mvp.main(_cli(tmp_path)) == 1
    finally:
        os.close(fd)


def test_a_lock_stamp_from_the_future_is_no_known_hold(tmp_path):
    p = str(tmp_path / "run.lock")
    with http.run_lock(p):
        ahead = time.time() + 3600
        os.utime(p, (ahead, ahead))
        with pytest.raises(http.AlreadyRunning) as ei:
            with http.run_lock(p):
                pass
    assert ei.value.held_s is None


# ---- cache stamps ahead of the clock (#139 #9 #18 #114) -------------------------------------
TORNADO = {"type": "FeatureCollection", "features": [
    {"id": "x", "type": "Feature", "geometry": None, "properties": {"event": "Tornado Warning"}}]}


@pytest.mark.parametrize("ts", [6 * 3600.0, 600.0, float("nan"), float("inf"), 10 ** 400, "soon", True],
                         ids=["6h", "10min", "nan", "inf", "huge", "text", "bool"])
def test_a_future_or_broken_stamp_is_no_copy(cache, ts, caplog):
    stamp = time.time() + ts if isinstance(ts, float) and ts < 1e5 else ts
    with open(cache.path("alerts"), "w") as f:
        f.write('{"ts": %s, "key": "alerts", "data": {"features": []}}' % (
            json.dumps(stamp) if not isinstance(stamp, int) or isinstance(stamp, bool) else str(stamp)))
    caplog.set_level(logging.WARNING)
    assert cache.get_any("alerts") == (None, None)
    assert cache.get("alerts", 10 ** 9) is None
    cache.get_any("alerts")
    assert len([r for r in caplog.records if "time stamp" in r.getMessage()]) == 1     # one warning
    # dropped: it cannot come back as a young copy once the clock has passed its stamp
    assert not os.path.exists(cache.path("alerts"))


def test_a_stamp_just_ahead_reads_as_age_zero(cache):
    cache.put("k", {"a": 1}, ts=time.time() + 60)
    assert cache.get_any("k") == ({"a": 1}, 0.0)
    assert cache.get("k", 300) == {"a": 1}


def test_ttl_zero_always_asks_the_network(cfg, cache, fake_http):
    url = "https://api.weather.gov/alerts/active?area=NM"
    fake_http.add("/alerts/active", TORNADO)
    cache.put("alerts", {"features": []}, ts=time.time() + 30)    # a small clock step: age 0
    res = http.cached_json(cfg, cache, "alerts", url, ttl=0, max_stale=3600)
    assert res["from_cache"] is False and len(res["data"]["features"]) == 1
    assert len(fake_http.calls) == 1


def test_a_feed_cached_ahead_of_the_clock_is_fetched_again(cfg, cache, fake_http):
    url = "https://api.weather.gov/alerts/active?area=NM"
    cache.put("alerts", {"features": []}, ts=time.time() + 6 * 3600)
    fake_http.add("/alerts/active", TORNADO)
    res = http.cached_json(cfg, cache, "alerts", url, ttl=0, max_stale=3600)
    assert res["ok"] and not res["stale"] and not res["from_cache"]
    assert res["data"]["features"][0]["properties"]["event"] == "Tornado Warning"


def test_a_copy_ahead_of_the_clock_is_no_last_good_copy(cfg, cache, fake_http):
    url = "https://api.weather.gov/alerts/active?area=NM"
    cache.put("alerts", {"features": []}, ts=time.time() + 6 * 3600)
    fake_http.fail("/alerts/active")
    res = http.cached_json(cfg, cache, "alerts", url, ttl=0, max_stale=10 ** 9)
    assert res["ok"] is False and res["data"] is None and res["error"]


def test_ttl_zero_keeps_the_last_good_copy_on_a_failed_fetch(cfg, cache, fake_http):
    url = "https://api.weather.gov/alerts/active?area=NM"
    cache.put("alerts", {"features": []}, ts=time.time() - 120)
    fake_http.fail("/alerts/active")
    res = http.cached_json(cfg, cache, "alerts", url, ttl=0, max_stale=3600)
    assert res["ok"] and res["stale"] and res["from_cache"] and res["data"] == {"features": []}
    assert 100 < res["age_s"] < 200


def test_alerts_feed_after_a_clock_step(cfg, cache, fake_http):
    """The audit's scenario end to end: a run while the clock was 6 h ahead cached an empty
    feed; after the clock is corrected the feed is asked again and the warning is seen."""
    from weather import alerts
    cache.put("alerts", {"type": "FeatureCollection", "features": []}, ts=time.time() + 6 * 3600)
    fake_http.add("/alerts/active", TORNADO)
    feed = alerts.fetch_active(cfg, cache)
    assert any("/alerts/active" in c for c in fake_http.calls)
    assert feed.get("ok") and not feed.get("stale")


@pytest.mark.parametrize("meta_ts", [time.time() + 6 * 3600, float("inf"), 10 ** 400], ids=["6h", "inf", "huge"])
def test_bytes_with_a_future_or_broken_stamp_are_fetched_again(cfg, cache, fake_http, meta_ts):
    cache.put_bytes("roads", http.BYTES_EXT, b"old")
    cache.put("roads__meta", {"etag": '"e"', "last_modified": None, "ts": meta_ts})
    assert http._bytes_meta(cache, "roads") == (None, {}, None)
    assert not os.path.exists(cache.path("roads", http.BYTES_EXT))
    fake_http.add("example.org/roads", b"new")
    res = http.cached_bytes(cfg, cache, "roads", "https://example.org/roads", ttl=600, max_stale=3600)
    assert res["data"] == b"new" and not res["from_cache"]
    assert fake_http.calls == ["COND https://example.org/roads"]


def test_bytes_dated_by_a_future_file_time_are_no_copy(cfg, cache, fake_http):
    p = cache.put_bytes("roads", http.BYTES_EXT, b"old")
    ahead = time.time() + 3 * 3600
    os.utime(p, (ahead, ahead))                          # no meta record: the file time dates it
    fake_http.fail("example.org/roads")
    res = http.cached_bytes(cfg, cache, "roads", "https://example.org/roads", ttl=600, max_stale=10 ** 9)
    assert res["ok"] is False and res["data"] is None


# ---- the state file (#141 #26) ---------------------------------------------------------------
EMPTY = {"sites": {}, "saved_ts": None}


def test_a_deeply_nested_state_file_is_an_empty_state(tmp_path, caplog):
    p = tmp_path / "state.json"
    p.write_text("[" * 200000)
    caplog.set_level(logging.WARNING)
    assert d.load_state(str(p)) == EMPTY
    assert any("RecursionError" in r.getMessage() for r in caplog.records)
    p.write_text('{"sites": {"pt": ' + "[" * 200000 + "]" * 200000 + "}}")
    assert d.load_state(str(p)) == EMPTY


def test_an_oversized_state_file_is_an_empty_state(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"sites": {"pt": {"radar_streak": 1}}, "junk": "x" * (d.STATE_MAX_BYTES + 10)}))
    assert d.load_state(str(p)) == EMPTY
    p.write_text(json.dumps({"sites": {"pt": {"radar_streak": 1}}, "junk": "x" * 1000}))
    assert d.load_state(str(p))["sites"] == {"pt": {"radar_streak": 1}}


def test_any_parser_failure_is_an_empty_state(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    p.write_text('{"sites": {}}')

    def oom(*a, **k):
        raise MemoryError()
    monkeypatch.setattr(d.json, "loads", oom)
    assert d.load_state(str(p)) == EMPTY


def test_a_state_that_cannot_be_written_is_reported_not_raised(tmp_path, monkeypatch):
    p = str(tmp_path / "state.json")

    def deep(*a, **k):
        raise RecursionError("maximum recursion depth exceeded")
    monkeypatch.setattr(d.json, "dump", deep)
    err = d.save_state(p, {"sites": {}})
    assert err and "recursion" in err
    assert not os.listdir(str(tmp_path))                 # the temporary file is gone too


def test_a_run_with_a_hostile_state_file_still_publishes(cfg, fake_http, monkeypatch):
    """The run calls load_state outside any step guard: a RecursionError there used to end
    every run with exit 1 and no page."""
    from vlba import glm
    monkeypatch.setattr(glm, "deps_available", lambda: False)
    monkeypatch.setattr(glm, "deps_note", lambda: "needs python3-numpy and python3-h5py")
    os.makedirs(os.path.dirname(cfg.state_file), exist_ok=True)
    with open(cfg.state_file, "w") as f:
        f.write("[" * 100000)
    run = mvp.run_once(cfg)
    assert os.path.exists(os.path.join(cfg.out_dir, "index.html"))
    assert len(run["sites"]) == 2
    assert d.load_state(cfg.state_file)["sites"] is not None


# ---- leftover temporary files (#27) ---------------------------------------------------------
def _old(path, age=2 * 3600):
    t = time.time() - age
    os.utime(path, (t, t), follow_symlinks=False)


def test_leftover_temporary_files_are_swept(cfg, tmp_path):
    out, cache = cfg.out_dir, cfg.cache_dir
    os.makedirs(os.path.join(cache, "sub", "deeper"), exist_ok=True)
    os.makedirs(out, exist_ok=True)
    gone = [os.path.join(out, "index.html.tmp.624648"), os.path.join(out, "radar_pt_dark.png.tmp.12"),
            os.path.join(cache, "alerts.json.tmp.77"), os.path.join(cache, "sub", "deeper", "x.bin.tmp.5"),
            cfg.state_file + ".tmp.991"]
    kept = [os.path.join(out, "index.html"), os.path.join(out, "notes.tmp"),
            os.path.join(out, "status.json.tmp.31337x"), os.path.join(cache, "fresh.json.tmp.9")]
    for p in gone + kept:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("x")
        if p != os.path.join(cache, "fresh.json.tmp.9"):
            _old(p)
    outside = tmp_path / "precious.txt"
    outside.write_text("keep")
    link = os.path.join(out, "status.json.tmp.4")
    os.symlink(str(outside), link)
    _old(link)
    tmpdir = os.path.join(out, "dir.tmp.8")              # a directory with such a name stays
    os.makedirs(tmpdir)
    _old(tmpdir)
    mvp._sweep_temp_files(cfg)
    assert [p for p in gone if os.path.exists(p)] == []
    assert [p for p in kept if not os.path.exists(p)] == []
    assert os.path.islink(link) and outside.read_text() == "keep" and os.path.isdir(tmpdir)


def test_a_sweep_survives_missing_directories(cfg):
    mvp._sweep_temp_files(cfg)                         # nothing exists yet: no error


def test_a_locked_run_sweeps_first(cfg, monkeypatch):
    os.makedirs(cfg.out_dir, exist_ok=True)
    stale = os.path.join(cfg.out_dir, "index.html.tmp.4242")
    with open(stale, "w") as f:
        f.write("<html>half")
    _old(stale)

    class Stop(Exception):
        pass

    def stop(*a, **k):
        raise Stop()
    monkeypatch.setattr(mvp.http, "Cache", stop)
    with pytest.raises(Stop):
        mvp.run_once(cfg)
    assert not os.path.exists(stale)


def test_a_lock_file_whose_mode_cannot_be_narrowed_still_locks(tmp_path, monkeypatch, caplog):
    p = str(tmp_path / "run.lock")
    with open(p, "w") as f:
        f.write("1\n")
    os.chmod(p, 0o644)

    def no_modes(fd, mode):
        raise OSError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(http.os, "fchmod", no_modes)
    caplog.set_level(logging.WARNING)
    with http.run_lock(p):
        pass
    assert any("could not be narrowed" in r.getMessage() for r in caplog.records)
