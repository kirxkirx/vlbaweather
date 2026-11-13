"""GLM lightning, the fixes of the audit: a satellite's window counts as complete only when
what its bucket publishes covers it (lag, holes, truncated listings), the next run catches up
on what was missing, a wrong host clock is noticed, everything a broken or hostile granule or
reply can blow up is bounded, and the hold's edges are pinned. A healthy feed, as the real one
looks, must stay "ok": that decides between GOOD and GOOD? at every site on every run.

Synthetic granules are written with h5py (skipped without numpy + h5py); the feed is an
in-memory stand-in for ``vlba.glm._get``. No network."""
from __future__ import annotations

import io
import logging
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from weather import geo
from vlba import decision as d
from vlba import glm
from vlba.sites import SITES

UTC = timezone.utc
NOW = datetime(2026, 10, 8, 15, 42, 30, tzinfo=UTC)         # day 281
EAST, WEST = "noaa-goes19", "noaa-goes18"
SID = {EAST: "G19", WEST: "G18"}


def _site(slug: str) -> dict:
    return dict(next(s for s in SITES if s["slug"] == slug))


PT, MK, SC = _site("pt"), _site("mk"), _site("sc")


def _key(sat: str, start: datetime, created_s: int = 21) -> str:
    s = start.strftime("%Y%j%H%M%S") + "0"
    e = (start + timedelta(seconds=20)).strftime("%Y%j%H%M%S") + "0"
    c = (start + timedelta(seconds=created_s)).strftime("%Y%j%H%M%S") + "4"
    return "GLM-L2-LCFA/%s/OR_GLM-L2-LCFA_%s_s%s_e%s_c%s.nc" % (start.strftime("%Y/%j/%H"), sat, s, e, c)


def _listing(bucket: str, prefix: str, keys: list, token: str = None, truncated: bool = False) -> bytes:
    rows = "".join("<Contents><Key>%s</Key><Size>175000</Size></Contents>" % k for k in keys)
    more = ("<NextContinuationToken>%s</NextContinuationToken>" % token if token else "")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<ListBucketResult xmlns="http://s3.amazonaws.'
            'com/doc/2006-03-01/"><Name>%s</Name><Prefix>%s</Prefix>%s<KeyCount>%d</KeyCount>'
            '<MaxKeys>1000</MaxKeys><IsTruncated>%s</IsTruncated>%s</ListBucketResult>'
            % (bucket, prefix, more, len(keys), "true" if truncated or token else "false", rows)).encode()


@pytest.fixture
def write():
    """A GLM-L2-LCFA granule writer (float32 positions, packed time offsets, file attributes
    as bytes, like the live files); skips without numpy + h5py."""
    np = pytest.importorskip("numpy")
    h5py = pytest.importorskip("h5py")

    def make(start: datetime, flashes, sat: bytes = b"G19", epoch: datetime = None, lats=None,
             lons=None, only_lat: bool = False) -> bytes:
        lat = np.array(lats if lats is not None else [f[0] for f in flashes], dtype=np.float32)
        lon = np.array(lons if lons is not None else [f[1] for f in flashes], dtype=np.float32)
        offs = np.array([f[2] for f in flashes], dtype=np.float64)
        bio = io.BytesIO()
        with h5py.File(bio, "w") as f:
            f.attrs["platform_ID"] = np.bytes_(sat)
            f.attrs["time_coverage_start"] = np.bytes_(start.strftime("%Y-%m-%dT%H:%M:%S.0Z").encode())
            f.create_dataset("flash_lat", data=lat)
            if not only_lat:
                f.create_dataset("flash_lon", data=lon)
            raw = np.round((offs + 5.0) / 0.0003814756).astype(np.uint16).view(np.int16)
            v = f.create_dataset("flash_time_offset_of_first_event", data=raw)
            v.attrs["scale_factor"] = np.array([0.0003814756], dtype=np.float32)
            v.attrs["add_offset"] = np.array([-5.0], dtype=np.float32)
            v.attrs["_Unsigned"] = np.bytes_(b"true")
            v.attrs["units"] = np.bytes_(b"seconds since " + (epoch or start).strftime(
                "%Y-%m-%d %H:%M:%S.000").encode())
        return bio.getvalue()

    return make


class Feed:
    """``glm._get`` stand-in. Each bucket publishes a granule every 20 s from ``first`` to
    ``newest`` (``missing`` starts left out, ``extra`` keys added); ``granules`` holds the
    ones with flashes, every other one is an empty granule. ``page`` keys per listing page
    (continuation tokens followed), ``no_token`` truncates without a token."""

    def __init__(self, empty: bytes, newest: datetime, first: datetime = None):
        self.empty = empty
        self.newest = {EAST: newest, WEST: newest}
        self.first = first or datetime(2026, 10, 8, 13, 40, 0, tzinfo=UTC)
        self.missing = set()
        self.extra = {EAST: [], WEST: []}
        self.granules = {}
        self.page = None
        self.no_token = False
        self.calls = []
        self.lock = threading.Lock()

    def keys(self, bucket: str) -> list:
        out, t = [], self.first
        while t <= self.newest[bucket]:
            if t not in self.missing:
                out.append(_key(SID[bucket], t))
            t += timedelta(seconds=20)
        return out + self.extra[bucket]

    def granule_calls(self) -> list:
        return [u for u in self.calls if "?list-type=2" not in u]

    def __call__(self, url: str, cfg, timeout: float) -> bytes:
        with self.lock:
            self.calls.append(url)
        m = re.match(r"https://([^/]+)\.s3\.amazonaws\.com/(.*)$", url)
        bucket, rest = m.group(1), m.group(2)
        if rest.startswith("?"):
            q = urllib.parse.parse_qs(rest[1:])
            keys = sorted(k for k in self.keys(bucket) if k.startswith(q["prefix"][0]))
            if self.no_token:
                return _listing(bucket, q["prefix"][0], keys[:100], truncated=True)
            if self.page is None:
                return _listing(bucket, q["prefix"][0], keys)
            at = int(q.get("continuation-token", ["0"])[0])
            chunk = keys[at:at + self.page]
            token = str(at + self.page) if at + self.page < len(keys) else None
            return _listing(bucket, q["prefix"][0], chunk, token=token)
        return self.granules.get(urllib.parse.unquote(rest), self.empty)


@pytest.fixture
def feed(monkeypatch, write):
    """A healthy feed as the real one looks (measured 2026-11-12): a granule every 20 s, the
    newest one listed ending 50 s before the run."""
    f = Feed(write(NOW, []), newest=datetime(2026, 10, 8, 15, 41, 20, tzinfo=UTC))
    monkeypatch.setattr(glm, "_get", f)
    return f


def _glm_check(cfg, view: dict, st: dict = None, now: datetime = NOW) -> dict:
    check, _ = d.check_glm(cfg, {"glm": view}, {} if st is None else st, now)
    return check


def _near(km: float, bearing: float = 0.0, site: dict = PT) -> tuple:
    return geo.dest_point(site["lat"], site["lon"], km, bearing)


# ---- a healthy feed stays ok ------------------------------------------------------------------
def test_a_healthy_feed_is_complete_and_clear(cfg, feed):
    res = glm.fetch(cfg, now=NOW)
    assert res["ok"] and not res["partial"] and res["error"] is None
    for s in res["sats"].values():
        assert s["ok"] and s["error"] is None and s["selected"] == s["fetched"] == 90
    view = glm.site_view(res, PT, cfg)
    assert view["available"] and not view["partial"] and view["window_min"] == 31
    check = _glm_check(cfg, view)
    assert check["state"] == "ok"
    assert check["detail"] == "no flash within 50 km in the last 31 min (G19, G18)"


def test_a_slow_but_healthy_feed_and_a_lone_missing_granule_stay_ok(cfg, feed):
    """The slowest publication seen (75 s after the granule's end) and a single granule that
    never appears (2-3 in 1000) do not make the window incomplete; the detail then names the
    span examined instead of claiming the minutes up to the run."""
    feed.newest = {EAST: datetime(2026, 10, 8, 15, 40, 40, tzinfo=UTC),          # ends 90 s before
                   WEST: datetime(2026, 10, 8, 15, 40, 40, tzinfo=UTC)}
    feed.missing = {datetime(2026, 10, 8, 15, 30, 0, tzinfo=UTC)}
    res = glm.fetch(cfg, now=NOW)
    assert not res["partial"] and all(s["ok"] for s in res["sats"].values())
    view = glm.site_view(res, PT, cfg)
    assert view["available"] and view["span_end"] == datetime(2026, 10, 8, 15, 41, 0, tzinfo=UTC)
    check = _glm_check(cfg, view)
    assert check["state"] == "ok"
    assert check["detail"].startswith("no flash within 50 km, 15:11 to 15:41 UTC examined")


# ---- #96 / #118: lagging, holed, thin and truncated feeds ---------------------------------------
def test_a_lagging_feed_is_incomplete_and_the_check_has_no_data(cfg, feed):
    feed.newest[EAST] = NOW - timedelta(minutes=25)
    res = glm.fetch(cfg, now=NOW)
    g19 = res["sats"]["G19"]
    assert not g19["ok"] and "newest granule 25 min old" in g19["error"] and g19["fetched"] > 0
    assert res["sats"]["G18"]["ok"] and res["partial"]
    sc = glm.site_view(res, SC, cfg)                         # GOES-East only
    assert not sc["available"] and sc["partial"]
    check = _glm_check(cfg, sc)
    assert check["state"] == "unknown" and "newest granule 25 min old" in check["detail"]
    pt = glm.site_view(res, PT, cfg)                         # GOES-West sees it whole
    assert pt["available"] and pt["partial"]


def test_a_flash_found_in_a_lagging_feed_still_counts(cfg, feed, write):
    feed.newest[EAST] = NOW - timedelta(minutes=25)
    t = datetime(2026, 10, 8, 15, 16, 0, tzinfo=UTC)                         # 26.5 min before NOW
    lat, lon = _near(10.0, site=SC)
    feed.granules[_key("G19", t)] = write(t, [(lat, lon, 3.0)])
    res = glm.fetch(cfg, now=NOW)
    sc = glm.site_view(res, SC, cfg)
    assert not sc["available"] and sc["count_window"] == 1
    st = {}
    check = _glm_check(cfg, sc, st)
    assert check["state"] == "bad" and abs(st["glm_last_hit_ts"] - (t + timedelta(seconds=3)).timestamp()) < 0.01


def test_a_hole_in_the_feed_is_incomplete(cfg, feed):
    feed.missing = {datetime(2026, 10, 8, 15, 20, 0, tzinfo=UTC) + timedelta(seconds=20 * i) for i in range(60)}
    res = glm.fetch(cfg, now=NOW)
    for s in res["sats"].values():
        assert not s["ok"] and "20 min of the window not published (from 15:20 UTC)" in s["error"]
    check = _glm_check(cfg, glm.site_view(res, PT, cfg))
    assert check["state"] == "unknown" and "not published" in check["detail"]


def test_one_granule_per_satellite_is_no_window(cfg, feed):
    feed.first = feed.newest[EAST]                       # the bucket holds the newest one only
    res = glm.fetch(cfg, now=NOW)
    for s in res["sats"].values():
        assert s["fetched"] == 1 and not s["ok"] and "not published" in s["error"]
    assert _glm_check(cfg, glm.site_view(res, PT, cfg))["state"] == "unknown"


def test_a_truncated_listing_is_followed_or_refused(cfg, feed):
    feed.page = 50                                       # 128 keys of hour 15: three pages
    res = glm.fetch(cfg, now=NOW)
    assert all(s["ok"] and s["fetched"] == 90 for s in res["sats"].values())
    assert sum(1 for u in feed.calls if "continuation-token=" in u) == 4      # 2 per bucket
    feed.page = 5                                        # 26 pages: more than the cap
    res = glm.fetch(cfg, now=NOW)
    assert all(not s["ok"] and "still truncated" in s["error"] for s in res["sats"].values())
    feed.page, feed.no_token = None, True                # truncated without a token
    res = glm.fetch(cfg, now=NOW)
    assert all(not s["ok"] and "truncated" in s["error"] for s in res["sats"].values())
    with pytest.raises(ValueError):
        glm.parse_listing(_listing(EAST, "p/", ["p/a.nc"], token="abc"))


def test_the_lag_limit_is_a_setting(cfg, feed):
    feed.newest[EAST] = NOW - timedelta(minutes=4)                   # ends 3 min 40 s before the run
    assert glm.fetch(cfg, now=NOW)["sats"]["G19"]["ok"]
    cfg.glm_max_lag_s = 120
    g19 = glm.fetch(cfg, now=NOW)["sats"]["G19"]
    assert not g19["ok"] and "newest granule 4 min old" in g19["error"]


# ---- catching up on what was not published -------------------------------------------------------
def test_the_next_run_fetches_what_was_published_late(cfg, feed, write):
    """Run 1 finds 8 minutes not published; run 2 (a 10-minute interval) starts where run 1's
    span ended, so a flash in the minutes published late is found while it still counts."""
    hole = [datetime(2026, 10, 8, 15, 22, 0, tzinfo=UTC) + timedelta(seconds=20 * i) for i in range(24)]
    feed.missing = set(hole)
    first = glm.fetch(cfg, now=NOW)
    assert first["partial"] and first["newest"]["G19"] == datetime(2026, 10, 8, 15, 21, 40, tzinfo=UTC).timestamp()
    t = hole[9]                                                              # 15:25:00
    lat, lon = _near(15.0)
    feed.granules[_key("G19", t)] = write(t, [(lat, lon, 1.0)])
    feed.missing = set()
    later = NOW + timedelta(minutes=10)
    feed.newest = {EAST: later - timedelta(seconds=70), WEST: later - timedelta(seconds=70)}
    cfg.glm_window_min = 10
    feed.calls.clear()
    res = glm.fetch(cfg, now=later, since=first["newest"], retried=first["retry"], seen=first["seen"])
    assert any(_key("G19", t).rsplit("/", 1)[1] in u for u in feed.granule_calls())
    assert not res["partial"]
    view = glm.site_view(res, PT, cfg)
    assert view["available"] and view["count_window"] == 1
    assert _glm_check(cfg, view, now=later)["state"] == "bad"


def test_a_complete_run_adds_no_downloads_to_the_next(cfg, feed):
    first = glm.fetch(cfg, now=NOW)
    later = NOW + timedelta(minutes=30)
    feed.newest = {EAST: later - timedelta(seconds=70), WEST: later - timedelta(seconds=70)}
    res = glm.fetch(cfg, now=later, since=first["newest"], retried=first["retry"], seen=first["seen"])
    assert all(s["ok"] and s["selected"] <= 93 for s in res["sats"].values())


# ---- #97: the host clock -----------------------------------------------------------------------
def test_granules_after_the_runs_time_mean_a_slow_host_clock(cfg, feed, caplog):
    """The host clock is 70 min behind: the bucket lists granules up to the real time."""
    feed.newest = {EAST: NOW + timedelta(minutes=70), WEST: NOW + timedelta(minutes=70)}
    with caplog.at_level(logging.ERROR, logger="vlba.glm"):
        res = glm.fetch(cfg, now=NOW)
    for s in res["sats"].values():
        assert not s["ok"] and "host clock is behind" in s["error"]
    assert any("host clock is behind" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    assert _glm_check(cfg, glm.site_view(res, PT, cfg))["state"] == "unknown"


# ---- #101 and the span named on the page ------------------------------------------------------------
def _res(sats_spans: dict, flashes=(), window_start=None) -> dict:
    sats = {}
    for sid, (bucket, sub) in {"G19": (EAST, -75.2), "G18": (WEST, -137.0)}.items():
        a, b = sats_spans[sid]
        sats[sid] = {"bucket": bucket, "sub_lon": sub, "ok": True, "error": None, "listed": 100, "selected": 30,
                     "fetched": 30, "failed": 0, "flashes": 0, "span_start": a.timestamp(), "span_end": b.timestamp()}
    return {"ok": True, "stale": False, "error": None, "age_s": 0.0,
            "window_start": window_start or NOW - timedelta(minutes=20), "window_end": NOW,
            "flashes": list(flashes), "sats": sats, "deps": True, "elapsed_s": 1.0, "skipped": None,
            "partial": False, "newest": {}, "retry": {}, "seen": {}}


def test_the_window_named_is_the_span_the_covering_satellites_examined(cfg):
    res = _res({"G19": (NOW - timedelta(minutes=20), NOW), "G18": (NOW - timedelta(minutes=6), NOW)})
    assert glm.site_view(res, SC, cfg)["window_min"] == 20          # GOES-East only
    assert glm.site_view(res, MK, cfg)["window_min"] == 6           # GOES-West only
    assert glm.site_view(res, PT, cfg)["window_min"] == 6           # both: what both examined
    assert glm.site_view(res, MK, cfg)["span_start"] == NOW - timedelta(minutes=6)


def test_the_detail_names_a_span_that_ends_before_the_run(cfg):
    res = _res({"G19": (NOW - timedelta(minutes=31), NOW - timedelta(minutes=3)),
                "G18": (NOW - timedelta(minutes=31), NOW - timedelta(minutes=2))})
    view = glm.site_view(res, PT, cfg)
    assert view["span_end"] == NOW - timedelta(minutes=3)
    check = _glm_check(cfg, view)
    assert check["state"] == "ok" and check["detail"].startswith("no flash within 50 km, 15:11 to 15:39 UTC examined")
    res = _res({"G19": (NOW - timedelta(minutes=31), NOW - timedelta(seconds=40)),
                "G18": (NOW - timedelta(minutes=31), NOW)})
    assert _glm_check(cfg, glm.site_view(res, PT, cfg))["detail"].startswith(
        "no flash within 50 km in the last 31 min")


# ---- #99: a retry the deadline never reached ---------------------------------------------------------
def test_a_retry_started_after_the_deadline_stays_pending(cfg, feed, monkeypatch):
    a = datetime(2026, 10, 8, 15, 32, 0, tzinfo=UTC)
    real = glm._fetch_granule

    def fetch_granule(bucket, key, cfg_, deadline_at):
        if glm.key_start(key) == a and bucket == EAST:
            raise glm._Deadline("deadline reached")         # its worker started too late
        return real(bucket, key, cfg_, deadline_at)
    monkeypatch.setattr(glm, "_fetch_granule", fetch_granule)
    res = glm.fetch(cfg, now=NOW, retried={"G19": [a.timestamp()]})
    g19 = res["sats"]["G19"]
    assert not g19.get("dropped") and res["retry"] == {"G19": [a.timestamp()]}
    assert a.timestamp() not in res["seen"].get("G19", [])


def test_a_retry_given_up_is_not_seen(cfg, feed, monkeypatch):
    """A granule failing for the second time is given up, but not recorded as examined: a
    later run that fails it again inside the hold is incomplete."""
    a = datetime(2026, 10, 8, 15, 32, 0, tzinfo=UTC)

    def fetch_granule(bucket, key, cfg_, deadline_at):
        if glm.key_start(key) == a and bucket == EAST:
            raise OSError("download failed")
        return glm.parse_granule(feed.empty)
    monkeypatch.setattr(glm, "_fetch_granule", fetch_granule)
    res = glm.fetch(cfg, now=NOW, retried={"G19": [a.timestamp()]})
    assert res["sats"]["G19"]["dropped"] == 1 and a.timestamp() not in res["seen"].get("G19", [])
    later = glm.fetch(cfg, now=NOW + timedelta(minutes=2), since={"G19": (a - timedelta(minutes=1)).timestamp()},
                      retried=res["retry"], seen=res["seen"])
    assert not later["sats"]["G19"]["ok"] and later["partial"]


# ---- #102: no marker, a window shorter than the hold ---------------------------------------------
def test_a_first_run_with_a_short_window_covers_the_hold(cfg, feed, write):
    cfg.glm_window_min = 5
    t = datetime(2026, 10, 8, 15, 30, 20, tzinfo=UTC)                 # 12 min before NOW
    lat, lon = _near(8.0)
    feed.granules[_key("G19", t)] = write(t, [(lat, lon, 1.0)])
    res = glm.fetch(cfg, now=NOW)
    view = glm.site_view(res, PT, cfg)
    assert view["count_window"] == 1
    assert _glm_check(cfg, view)["state"] == "bad"


# ---- #104: one granule under two keys ------------------------------------------------------------
def test_a_granule_published_twice_is_fetched_and_counted_once(cfg, feed, write):
    t = datetime(2026, 10, 8, 15, 40, 0, tzinfo=UTC)
    lat, lon = _near(8.0)
    body = write(t, [(lat, lon, 1.0)])
    again = _key("G19", t, created_s=95)
    feed.granules[_key("G19", t)] = feed.granules[again] = body
    feed.extra[EAST].append(again)
    res = glm.fetch(cfg, now=NOW)
    got = [u for u in feed.granule_calls() if "G19_s20262811540000" in u]
    assert len(got) == 1 and got[0].endswith(again.rsplit("/", 1)[1])          # the later copy
    view = glm.site_view(res, PT, cfg)
    assert view["count_by_sat"]["G19"] == 1 and view["count_window"] == 1
    assert len(glm.select_keys([_key("G19", t), again], t, t)) == 1


# ---- #140: a flash dated by a wrong epoch ------------------------------------------------------------
def test_a_flash_time_outside_its_granule_takes_the_granules_end(cfg, feed, write):
    t = datetime(2026, 10, 8, 15, 37, 0, tzinfo=UTC)
    lat, lon = _near(8.0)
    feed.granules[_key("G19", t)] = write(t, [(lat, lon, 1.0)], epoch=t - timedelta(hours=2))
    res = glm.fetch(cfg, now=NOW)
    fl = [f for f in res["flashes"] if abs(f["lat"] - lat) < 1e-4]
    assert fl and fl[0]["t"] == t + timedelta(seconds=20)
    view = glm.site_view(res, PT, cfg)
    assert view["last_in_ring_t"] == t + timedelta(seconds=20)
    assert _glm_check(cfg, view)["state"] == "bad"


# ---- #47, #43: broken and hostile granules --------------------------------------------------------
def test_broken_granules_are_refused(write):
    t = NOW - timedelta(minutes=1)
    for bad in (write(t, [(PT["lat"], PT["lon"], 1.0)], only_lat=True),           # flash_lon missing
                write(t, [(PT["lat"], PT["lon"] + 360.0, 1.0)]),                  # longitudes 0..360
                write(t, [(0.0, 0.0, 1.0), (0.0, 0.0, 2.0)], lats=[float("nan")] * 2)):   # none readable
        with pytest.raises(ValueError):
            glm.parse_granule(bad)
    g = glm.parse_granule(write(t, [(PT["lat"], PT["lon"], 1.0), (0.0, 0.0, 2.0)], lats=[PT["lat"], float("nan")]))
    assert len(g["lats"]) == 1                                 # a lone fill value is dropped
    for junk in (b"<html>ListBucketResult</html>", b"<Error><Code>x</Code>ListBucketResult</Error>"):
        with pytest.raises(ValueError):
            glm.parse_listing(junk)


def test_declared_shapes_are_checked_before_reading():
    np = pytest.importorskip("numpy")
    h5py = pytest.importorskip("h5py")

    def granule(shape_lat, shape_lon, n_off=None) -> bytes:
        bio = io.BytesIO()
        with h5py.File(bio, "w") as f:
            f.attrs["platform_ID"] = np.bytes_(b"G19")
            f.attrs["time_coverage_start"] = np.bytes_(b"2026-10-08T15:40:20.0Z")
            # declared and never written: reads back as fill values, a few KB on the wire
            f.create_dataset("flash_lat", shape=shape_lat, dtype="f4", fillvalue=PT["lat"])
            f.create_dataset("flash_lon", shape=shape_lon, dtype="f4", fillvalue=PT["lon"])
            if n_off is not None:
                f.create_dataset("flash_time_offset_of_first_event", shape=(n_off,), dtype="i2")
        return bio.getvalue()
    t0 = time.monotonic()
    for bad in (granule((2000000,), (2000000,)), granule((2, 2), (2, 2)), granule((3,), (3,), n_off=2)):
        assert len(bad) < 20000
        with pytest.raises(ValueError):
            glm.parse_granule(bad)
    assert time.monotonic() - t0 < 5.0
    assert len(glm.parse_granule(granule((3,), (3,), n_off=3))["lats"]) == 3


def test_the_flashes_of_a_run_are_capped(cfg, feed, write, monkeypatch):
    monkeypatch.setattr(glm, "MAX_FLASHES_PER_RUN", 2)
    t = datetime(2026, 10, 8, 15, 40, 0, tzinfo=UTC)
    feed.granules[_key("G19", t)] = write(t, [(10.0, -60.0, 1.0)] * 3)
    res = glm.fetch(cfg, now=NOW)
    assert len(res["flashes"]) == 2
    g19 = res["sats"]["G19"]
    assert not g19["ok"] and "more than 2 flashes" in g19["error"]


def test_the_de_duplication_is_not_quadratic(cfg, monkeypatch):
    """Thousands of detections of one second at one spot from both satellites (a crafted
    granule): the comparisons grow with the rows, not with their square."""
    calls = [0]
    real = geo.haversine_km

    def counting(*a):
        calls[0] += 1
        return real(*a)
    t = NOW - timedelta(minutes=1)
    n = 1500
    far = _near(30.0)                    # the G18 ones 30 km off: never the same flash
    rows = [(5.0, PT["lat"], PT["lon"], t, "G19") for _ in range(n)] + \
        [(30.0, far[0], far[1], t, "G18") for _ in range(n)]
    monkeypatch.setattr(geo, "haversine_km", counting)
    kept = glm._one_per_flash(rows)
    assert calls[0] <= 100 * len(rows)
    assert len(kept) == 2 * n
    calls[0] = 0
    rows = [(5.0, PT["lat"], PT["lon"], t, sat) for sat in ("G19", "G18") for _ in range(n)]
    kept = glm._one_per_flash(rows)
    assert calls[0] <= 100 * len(rows) and n <= len(kept) <= 2 * n


# ---- #103, #98: the transport's caps -------------------------------------------------------------
class _Resp:
    def __init__(self, body: bytes, delay: float = 0.0, length: str = None):
        self.body, self.delay = io.BytesIO(body), delay
        self.headers = {"Content-Length": length} if length else {}
        self.total = len(body)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        if n is None or n < 0:                    # the whole body, however long it trickles
            time.sleep(self.delay * self.total)
            return self.body.read()
        time.sleep(self.delay)
        return self.body.read(1)

    read1 = read


def test_a_trickling_reply_is_cut_at_the_time_limit(cfg, monkeypatch):
    monkeypatch.setattr(glm, "READ_GRACE_S", 0.1, raising=False)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp(b"x" * 40, delay=0.05))
    t0 = time.monotonic()
    with pytest.raises(OSError):
        glm._get("https://noaa-goes19.s3.amazonaws.com/x", cfg, 0.2)
    assert time.monotonic() - t0 < 1.5


def test_an_oversized_reply_is_refused(cfg, monkeypatch):
    monkeypatch.setattr(glm, "MAX_BODY_BYTES", 100, raising=False)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp(b"x" * 1000))
    with pytest.raises(ValueError):
        glm._get("https://noaa-goes19.s3.amazonaws.com/x", cfg, 5.0)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp(b"x", length="999999"))
    with pytest.raises(ValueError):
        glm._get("https://noaa-goes19.s3.amazonaws.com/x", cfg, 5.0)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp(b"x" * 100))
    assert glm._get("https://noaa-goes19.s3.amazonaws.com/x", cfg, 5.0) == b"x" * 100


# ---- #44: time attributes -------------------------------------------------------------------------
def test_time_attributes_are_parsed_in_linear_time():
    t0 = time.monotonic()
    assert glm._parse_time("2026-10-08T15:40:20" + " " * 40000 + "x") is None
    assert glm._epoch_of("seconds since 2026-10-08 15:40:20" + " " * 40000 + "x") is None
    assert time.monotonic() - t0 < 0.5
    assert glm._parse_time(" 2026-10-08T15:40:20.0Z ") == datetime(2026, 10, 8, 15, 40, 20, tzinfo=UTC)
    assert glm._epoch_of("seconds since 2026-10-08 15:40:20.000") == datetime(2026, 10, 8, 15, 40, 20, tzinfo=UTC)
    assert glm._parse_time("2026-10-08T15:40:20Zx") is None


# ---- #105, #148: the netCDF4 reader --------------------------------------------------------------
def test_the_netcdf4_reader_parses_one_granule_at_a_time(monkeypatch):
    pytest.importorskip("numpy")
    state = {"now": 0, "max": 0}
    lock = threading.Lock()

    class FakeDataset:
        def __init__(self, name, mode="r", memory=None):
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            time.sleep(0.05)
            self.platform_ID = "G19"
            self.time_coverage_start = "2026-10-08T15:40:20.0Z"
            self.variables = {}

        def close(self):
            with lock:
                state["now"] -= 1

    class FakeNetCDF4:
        Dataset = FakeDataset
    monkeypatch.setattr(glm, "h5py", None)
    monkeypatch.setattr(glm, "netCDF4", FakeNetCDF4)
    threads = [threading.Thread(target=glm.parse_granule, args=(b"granule",)) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert state["max"] == 1


def test_the_netcdf4_reader_reads_a_real_layout(tmp_path, monkeypatch):
    """The granule parser through netCDF4 (when it is installed; CI installs it): float32
    positions, packed int16 time offsets with ``_Unsigned``, the file attributes."""
    np = pytest.importorskip("numpy")
    netCDF4 = pytest.importorskip("netCDF4")
    path = tmp_path / "g.nc"
    start = datetime(2026, 10, 8, 15, 40, 20, tzinfo=UTC)
    with netCDF4.Dataset(str(path), "w", format="NETCDF4") as ds:
        ds.setncattr("platform_ID", "G18")
        ds.setncattr("time_coverage_start", "2026-10-08T15:40:20.0Z")
        ds.createDimension("number_of_flashes", 2)
        lat = ds.createVariable("flash_lat", "f4", ("number_of_flashes",))
        lon = ds.createVariable("flash_lon", "f4", ("number_of_flashes",))
        lat[:] = np.array([PT["lat"], MK["lat"]], dtype=np.float32)
        lon[:] = np.array([PT["lon"], MK["lon"]], dtype=np.float32)
        off = ds.createVariable("flash_time_offset_of_first_event", "i2", ("number_of_flashes",))
        off.set_auto_maskandscale(False)
        off.setncattr("scale_factor", np.float32(0.0003814756))
        off.setncattr("add_offset", np.float32(-5.0))
        off.setncattr("_Unsigned", "true")
        off.setncattr("units", "seconds since 2026-10-08 15:40:20.000")
        off[:] = np.round((np.array([2.5, 7.0]) + 5.0) / 0.0003814756).astype(np.uint16).view(np.int16)
    monkeypatch.setattr(glm, "h5py", None)
    g = glm.parse_granule(path.read_bytes())
    assert g["sat"] == "G18" and g["start"] == start and len(g["lats"]) == 2
    assert abs(g["lats"][0] - PT["lat"]) < 1e-4 and abs(g["lons"][1] - MK["lon"]) < 1e-4
    assert abs((g["times"][0] - start).total_seconds() - 2.5) < 0.01
    assert abs((g["times"][1] - start).total_seconds() - 7.0) < 0.01


# ---- S23, S27: the hold's edges and the ring's ----------------------------------------------------------
def _view(count: int, last: datetime = None, **over) -> dict:
    v = {"covered": True, "sats": ["G19"], "available": True, "partial": False, "error": None,
         "count_window": count, "nearest_km": 12.0 if count else None, "nearest_bearing": "N" if count else None,
         "window_min": 31, "last_in_ring_t": last}
    v.update(over)
    return v


def test_the_hold_ends_at_its_length(cfg):
    hold = cfg.glm_hold_s
    assert _glm_check(cfg, _view(1, NOW - timedelta(seconds=hold - 30)))["state"] == "bad"
    check = _glm_check(cfg, _view(1, NOW - timedelta(seconds=hold + 30)))
    assert check["state"] == "ok" and "past the 30 min hold" in check["detail"]
    st = {"glm_last_hit_ts": (NOW - timedelta(seconds=hold - 30)).timestamp()}
    assert _glm_check(cfg, _view(0), st)["state"] == "bad"
    st = {"glm_last_hit_ts": (NOW - timedelta(seconds=hold + 30)).timestamp()}
    assert _glm_check(cfg, _view(0), st)["state"] == "ok"


def test_a_newer_stored_flash_outlives_an_older_one_of_the_window(cfg):
    newer = (NOW - timedelta(minutes=5)).timestamp()
    st = {"glm_last_hit_ts": newer}
    assert _glm_check(cfg, _view(1, NOW - timedelta(minutes=20)), st)["state"] == "bad"
    assert st["glm_last_hit_ts"] == newer
    later = NOW + timedelta(minutes=20)                 # the window's flash is 40 min old then
    assert _glm_check(cfg, _view(0), st, now=later)["state"] == "bad"


def test_the_newest_flash_in_the_ring_times_the_hold(cfg):
    t_old, t_new = NOW - timedelta(minutes=25), NOW - timedelta(minutes=3)
    a, b = _near(20.0, 0.0), _near(30.0, 180.0)
    res = _res({"G19": (NOW - timedelta(minutes=31), NOW), "G18": (NOW - timedelta(minutes=31), NOW)})
    for flashes in ([(a, "G19", t_old), (b, "G18", t_new)], [(b, "G18", t_new), (a, "G19", t_old)],
                    [(a, "G19", t_new), (b, "G19", t_old)]):
        res["flashes"] = [{"lat": p[0], "lon": p[1], "sat": s, "t": t} for p, s, t in flashes]
        v = glm.site_view(res, PT, cfg)
        assert v["count_window"] == 2 and v["last_in_ring_t"] == t_new
        st = {}
        assert _glm_check(cfg, v, st)["state"] == "bad" and st["glm_last_hit_ts"] == t_new.timestamp()


def test_a_flash_just_outside_the_ring_does_not_count(cfg):
    res = _res({"G19": (NOW - timedelta(minutes=31), NOW), "G18": (NOW - timedelta(minutes=31), NOW)})
    for km, n in ((cfg.glm_km + 1.0, 0), (cfg.glm_km - 1.0, 1)):
        p = _near(km, 45.0)
        res["flashes"] = [{"lat": p[0], "lon": p[1], "sat": "G19", "t": NOW - timedelta(minutes=2)}]
        v = glm.site_view(res, PT, cfg)
        assert v["count_window"] == n
        assert _glm_check(cfg, v)["state"] == ("bad" if n else "ok")


# ---- the setting -----------------------------------------------------------------------------
@pytest.mark.parametrize("value, ok", [("300", 300), ("60", 60), ("1800", 1800), ("59", None), ("1801", None),
                                       ("90.5", None), ("x", None)])
def test_vlba_glm_max_lag(monkeypatch, tmp_path, value, ok):
    from vlba.config import VlbaConfig
    for k in list(__import__("os").environ):
        if k.startswith(("WEATHER_", "VLBA_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VLBA_GLM_MAX_LAG", value)
    if ok is None:
        with pytest.raises(ValueError):
            VlbaConfig.from_env(out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "cache"))
    else:
        assert VlbaConfig.from_env(out_dir=str(tmp_path / "out"),
                                   cache_dir=str(tmp_path / "cache")).glm_max_lag_s == ok
    assert VlbaConfig.glm_max_lag_s == 300


# ---- review: the run's cap holds where granules are parsed; antennas read alike ------------------
def test_the_flash_cap_of_the_run_bounds_the_parsed_granules(cfg, feed, write, monkeypatch):
    """Every granule of a hostile bucket filled to the per-granule cap: what the workers hand
    back (and what waits in their futures until all downloads are in) stays within the cap of
    the run, not granules times the per-granule cap."""
    monkeypatch.setattr(glm, "MAX_FLASHES_PER_RUN", 25)
    held = []
    real = glm._fetch_within

    def counting(*a, **k):
        g = real(*a, **k)
        held.append(len(g["lats"]))
        return g
    monkeypatch.setattr(glm, "_fetch_within", counting)
    t = datetime(2026, 10, 8, 15, 30, 0, tzinfo=UTC)
    for i in range(6):
        ti = t + timedelta(seconds=20 * i)
        feed.granules[_key("G19", ti)] = write(ti, [(10.0, -60.0, 1.0)] * 10)
    res = glm.fetch(cfg, now=NOW)
    assert sum(held) == 25 and len(res["flashes"]) == 25
    g19 = res["sats"]["G19"]
    assert not g19["ok"] and "more than 25 flashes this run" in g19["error"]


def test_the_examined_span_is_named_against_the_fetch_not_the_antennas_clock(cfg):
    """A healthy feed (newest granule ending 40 s before the fetch) reads "in the last 31 min"
    at every antenna, also those evaluated minutes after the fetch."""
    res = _res({"G19": (NOW - timedelta(minutes=31), NOW - timedelta(seconds=40)),
                "G18": (NOW - timedelta(minutes=31), NOW - timedelta(seconds=40))})
    view = glm.site_view(res, PT, cfg)
    assert view["window_end"] == NOW
    for late in (0, 30, 180):
        check = _glm_check(cfg, view, now=NOW + timedelta(seconds=late))
        assert check["detail"].startswith("no flash within 50 km in the last 31 min"), late
    view = glm.site_view(_res({"G19": (NOW - timedelta(minutes=31), NOW - timedelta(minutes=3)),
                               "G18": (NOW - timedelta(minutes=31), NOW - timedelta(minutes=3))}), PT, cfg)
    assert _glm_check(cfg, view, now=NOW + timedelta(seconds=30))["detail"].startswith(
        "no flash within 50 km, 15:11 to 15:39 UTC examined")


def test_headers_sent_a_byte_at_a_time_do_not_hold_the_worker(cfg, monkeypatch):
    """The status line and headers arrive before ``urlopen`` returns, each byte within the
    socket timeout: the worker gives up at the time limit all the same (#103)."""
    monkeypatch.setattr(glm, "READ_GRACE_S", 0.1, raising=False)
    release = threading.Event()

    def slow_headers(req, timeout=None):
        release.wait(10)                    # stands in for headers fed a byte at a time
        return _Resp(b"late")
    monkeypatch.setattr("urllib.request.urlopen", slow_headers)
    t0 = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            glm._get("https://noaa-goes19.s3.amazonaws.com/x", cfg, 0.2)
        assert time.monotonic() - t0 < 2.0
    finally:
        release.set()
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp(b"x" * 100))
    assert glm._get("https://noaa-goes19.s3.amazonaws.com/x", cfg, 5.0) == b"x" * 100
