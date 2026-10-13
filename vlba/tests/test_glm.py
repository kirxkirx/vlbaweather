"""GLM lightning: key parsing and selection, satellite ids and fields of view, granule
parsing in RAM (synthetic HDF5 files written with h5py; those tests are skipped without
numpy + h5py), the fetch end to end against a fake S3 served through ``vlba.glm._get``
(both buckets, an hour boundary, partial and total failures, the skips, the deadline), the
per-site view, and that nothing public raises on garbage. No network: the conftest blocks
``urllib.request.urlopen`` and ``_get`` is the only caller."""
from __future__ import annotations

import io
import re
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from weather import geo
from weather import http as _http
from vlba import glm
from vlba.sites import VLBA_SITES

UTC = timezone.utc
NOW = datetime(2026, 10, 8, 15, 42, 30, tzinfo=UTC)         # day 281
EAST, WEST = "noaa-goes19", "noaa-goes18"


def _site(slug: str) -> dict:
    return dict(next(s for s in VLBA_SITES if s["slug"] == slug))


PT, MK, SC, HN = _site("pt"), _site("mk"), _site("sc"), _site("hn")


# ---- synthetic granules and a fake S3 ------------------------------------------------------
def _key(sat: str, start: datetime) -> str:
    s = start.strftime("%Y%j%H%M%S") + "0"
    e = (start + timedelta(seconds=20)).strftime("%Y%j%H%M%S") + "0"
    c = (start + timedelta(seconds=21)).strftime("%Y%j%H%M%S") + "4"
    return "GLM-L2-LCFA/%s/OR_GLM-L2-LCFA_%s_s%s_e%s_c%s.nc" % (start.strftime("%Y/%j/%H"), sat, s, e, c)


def _hour_keys(sat: str, hour: datetime) -> list:
    return [_key(sat, hour + timedelta(seconds=20 * i)) for i in range(180)]


def _listing_xml(bucket: str, prefix: str, keys: list) -> bytes:
    rows = "".join("<Contents><Key>%s</Key><LastModified>2026-10-08T15:40:41.000Z</LastModified>"
                   "<ETag>&quot;abc&quot;</ETag><Size>175000</Size><StorageClass>STANDARD"
                   "</StorageClass></Contents>" % k for k in keys)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<ListBucketResult xmlns="http://s3.amazonaws.'
            'com/doc/2006-03-01/"><Name>%s</Name><Prefix>%s</Prefix><KeyCount>%d</KeyCount>'
            '<MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated>%s</ListBucketResult>'
            % (bucket, prefix, len(keys), rows)).encode()


@pytest.fixture
def h5():
    """The synthetic-granule writer; skips the test without numpy + h5py."""
    np = pytest.importorskip("numpy")
    h5py = pytest.importorskip("h5py")

    def make(start: datetime, flashes, sat: bytes = b"G18", packed: bool = False,
             scalar_attrs: bool = False, with_times: bool = True) -> bytes:
        """``flashes``: [(lat, lon, offset_s)]. Written like a real GLM-L2-LCFA file:
        float32 positions (or int16 packed with scale/offset/_Unsigned when ``packed``),
        the time offsets always packed, the file attributes as bytes."""
        lats = np.array([f[0] for f in flashes], dtype=np.float64)
        lons = np.array([f[1] for f in flashes], dtype=np.float64)
        offs = np.array([f[2] for f in flashes], dtype=np.float64)
        bio = io.BytesIO()
        with h5py.File(bio, "w") as f:
            f.attrs["platform_ID"] = np.bytes_(sat)
            f.attrs["time_coverage_start"] = np.bytes_(start.strftime("%Y-%m-%dT%H:%M:%S.0Z").encode())
            f.attrs["time_coverage_end"] = np.bytes_(
                (start + timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%S.0Z").encode())

            def packed_var(name, values, scale, offset, units):
                raw = np.round((values - offset) / scale).astype(np.uint16).view(np.int16)
                d = f.create_dataset(name, data=raw)
                if scalar_attrs:
                    d.attrs["scale_factor"] = np.float32(scale)
                    d.attrs["add_offset"] = np.float32(offset)
                else:
                    d.attrs["scale_factor"] = np.array([scale], dtype=np.float32)
                    d.attrs["add_offset"] = np.array([offset], dtype=np.float32)
                d.attrs["_Unsigned"] = np.bytes_(b"true")
                d.attrs["units"] = np.bytes_(units)

            if packed:
                packed_var("flash_lat", lats, 180.0 / 65536, -90.0, b"degrees_north")
                packed_var("flash_lon", lons, 360.0 / 65536, -180.0, b"degrees_east")
            else:
                d = f.create_dataset("flash_lat", data=lats.astype(np.float32))
                d.attrs["units"] = np.bytes_(b"degrees_north")
                d = f.create_dataset("flash_lon", data=lons.astype(np.float32))
                d.attrs["units"] = np.bytes_(b"degrees_east")
            if with_times:
                packed_var("flash_time_offset_of_first_event", offs, 0.0003814756, -5.0,
                           b"seconds since " + start.strftime("%Y-%m-%d %H:%M:%S.000").encode())
        return bio.getvalue()

    return make


class FakeS3:
    """Stand-in for ``vlba.glm._get``: the hour listings and the granules of the buckets."""

    def __init__(self, keys: dict, granules: dict, empty: bytes):
        self.keys = keys                # bucket -> [key]
        self.granules = granules        # bucket -> {key: bytes}; any other key is ``empty``
        self.empty = empty
        self.calls = []
        self.fail_listing = set()       # buckets whose listing raises
        self.fail_keys = set()          # keys whose download raises
        self.garbage_keys = set()       # keys that download as junk
        self.block = None               # an Event every granule download waits on
        self.lock = threading.Lock()

    def listing_calls(self) -> list:
        return [u for u, _ in self.calls if "?list-type=2" in u]

    def granule_calls(self) -> list:
        return [u for u, _ in self.calls if "?list-type=2" not in u]

    def __call__(self, url: str, cfg, timeout: float) -> bytes:
        with self.lock:
            self.calls.append((url, timeout))
        assert 0 < timeout <= 20, timeout
        assert cfg.user_agent
        m = re.match(r"https://([^/]+)\.s3\.amazonaws\.com/(.*)$", url)
        assert m, url
        bucket, rest = m.group(1), m.group(2)
        if rest.startswith("?"):
            q = urllib.parse.parse_qs(rest[1:])
            assert q["list-type"] == ["2"] and q["max-keys"] == ["1000"]
            prefix = q["prefix"][0]
            if bucket in self.fail_listing:
                raise OSError("listing down")
            return _listing_xml(bucket, prefix,
                                [k for k in self.keys.get(bucket, []) if k.startswith(prefix)])
        key = urllib.parse.unquote(rest)
        if self.block is not None:
            self.block.wait(10)
            raise OSError("released")
        if key in self.fail_keys:
            raise OSError("granule download failed")
        if key in self.garbage_keys:
            return b"<html>not a granule</html>"
        return self.granules.get(bucket, {}).get(key, self.empty)


# Flashes of the scenario: A 11 km north of Pie Town (East), B near St. Croix (East), C
# 28 km west of Pie Town (West), D 22 km north of Mauna Kea (West), E 80 km north of Pie
# Town (East, an older granule still in the window), F near Pie Town in a granule just
# before the window (5 min plus the 60-s overlap: must never be fetched).
A = (PT["lat"] + 0.1, PT["lon"], 2.5)
B = (SC["lat"] + 0.1, SC["lon"], 7.0)
C = (PT["lat"], PT["lon"] - 0.3, 1.0)
D = (MK["lat"] + 0.2, MK["lon"], 3.0)
E = (PT["lat"] + 0.72, PT["lon"], 0.0)
F = (PT["lat"], PT["lon"], 1.0)


@pytest.fixture
def s3(h5):
    day = datetime(2026, 10, 8, tzinfo=UTC)
    hours = [day + timedelta(hours=h) for h in (14, 15)]
    keys = {EAST: sum((_hour_keys("G19", h) for h in hours), []),
            WEST: sum((_hour_keys("G18", h) for h in hours), [])}
    newest = datetime(2026, 10, 8, 15, 42, 20, tzinfo=UTC)
    older = datetime(2026, 10, 8, 15, 40, 0, tzinfo=UTC)
    before = datetime(2026, 10, 8, 15, 36, 20, tzinfo=UTC)
    granules = {
        EAST: {_key("G19", newest): h5(newest, [A, B], sat=b"G19"),
               _key("G19", older): h5(older, [E], sat=b"G19"),
               _key("G19", before): h5(before, [F], sat=b"G19")},
        WEST: {_key("G18", newest): h5(newest, [C, D], sat=b"G18")},
    }
    return FakeS3(keys, granules, h5(newest, [], sat=b"G18"))


@pytest.fixture
def fake_get(monkeypatch, s3):
    monkeypatch.setattr(glm, "_get", s3)
    return s3


# ---- keys, listing, satellites, field of view ----------------------------------------------
def test_key_start_reads_the_s_field():
    t = glm.key_start("GLM-L2-LCFA/2026/281/15/OR_GLM-L2-LCFA_G18_s20262811540200_"
                      "e20262811540400_c20262811540414.nc")
    assert t == datetime(2026, 10, 8, 15, 40, 20, tzinfo=UTC)
    assert glm.key_start("x_s20262811540205_e1.nc") == t + timedelta(milliseconds=500)
    assert glm.key_start("x_s2026281154020") == t                 # 13 digits, no tenth
    assert glm.key_start("OR_GLM-L2-LCFA_G18_e20262811540400.nc") is None
    assert glm.key_start("OR_GLM_s20269991540200_e.nc") is None   # day 999
    assert glm.key_start(None) is None and glm.key_start(42) is None


def test_select_keys_window_edges_order_stride_and_junk():
    t0 = datetime(2026, 10, 8, 15, 0, 0, tzinfo=UTC)
    keys = [_key("G19", t0 + timedelta(seconds=20 * i)) for i in range(30)]
    start, end = t0 + timedelta(seconds=100), t0 + timedelta(seconds=300)
    sel = glm.select_keys(keys + ["junk", None, 7, keys[5]], start, end, 1)
    starts = [glm.key_start(k) for k in sel]
    assert starts == sorted(starts, reverse=True)                     # newest first
    assert starts[0] == end and starts[-1] == start                   # both edges inclusive
    assert len(sel) == 11 and len(set(sel)) == 11
    assert glm.select_keys(keys, start, end, 2) == sel[::2]
    assert glm.select_keys(keys, start, end, 3)[0] == sel[0]          # the newest is kept
    assert glm.select_keys(keys, start, end, 0) == sel                # a bad stride = 1
    assert glm.select_keys(keys, start, end, "x") == sel
    assert glm.select_keys([], start, end, 1) == []
    assert glm.select_keys(["junk"], start, end, 1) == []
    assert glm.select_keys(keys, end + timedelta(hours=1), end + timedelta(hours=2), 1) == []


def test_parse_listing_and_hour_prefixes():
    keys = ["GLM-L2-LCFA/2026/281/15/a.nc", "GLM-L2-LCFA/2026/281/15/b.nc"]
    assert glm.parse_listing(_listing_xml(EAST, "GLM-L2-LCFA/2026/281/15/", keys)) == keys
    assert glm.parse_listing(_listing_xml(EAST, "GLM-L2-LCFA/2026/281/16/", [])) == []
    with pytest.raises(ValueError):
        glm.parse_listing(b"<html>Service Unavailable</html>")
    with pytest.raises(ValueError):
        glm.parse_listing(b"")
    assert glm.hour_prefixes(NOW - timedelta(minutes=5), NOW) == ["GLM-L2-LCFA/2026/281/15/"]
    t = datetime(2026, 10, 8, 15, 2, 0, tzinfo=UTC)
    assert glm.hour_prefixes(t - timedelta(minutes=5), t) == ["GLM-L2-LCFA/2026/281/14/",
                                                              "GLM-L2-LCFA/2026/281/15/"]
    t = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)                    # across the year
    assert glm.hour_prefixes(t - timedelta(minutes=5), t) == ["GLM-L2-LCFA/2025/365/23/",
                                                              "GLM-L2-LCFA/2026/001/00/"]


def test_satellite_id():
    assert glm.satellite_id("noaa-goes19") == "G19"
    assert glm.satellite_id("noaa-goes18") == "G18"
    assert glm.satellite_id("NOAA-GOES-16") == "G16"
    assert glm.satellite_id("my-mirror") == "my-mirror"
    assert glm.satellite_id("") == "?" and glm.satellite_id(None) == "?"


def test_site_in_fov_per_satellite():
    east, west = -75.2, -137.0
    assert glm.site_in_fov(MK["lat"], MK["lon"], east, 70.0) is False      # 80.8 deg of arc
    assert glm.site_in_fov(MK["lat"], MK["lon"], west, 70.0) is True       # 26.8
    assert glm.site_in_fov(SC["lat"], SC["lon"], east, 70.0) is True       # 20.6
    assert glm.site_in_fov(SC["lat"], SC["lon"], west, 70.0) is False      # 73.3
    assert glm.site_in_fov(HN["lat"], HN["lon"], east, 70.0) is True       # 43.0
    assert glm.site_in_fov(HN["lat"], HN["lon"], west, 70.0) is False      # 72.0
    assert glm.site_in_fov(PT["lat"], PT["lon"], east, 70.0) is True
    assert glm.site_in_fov(PT["lat"], PT["lon"], west, 70.0) is True
    assert abs(glm.arc_deg(0.0, east, east)) < 1e-9
    assert abs(glm.arc_deg(0.0, east + 60.0, east) - 60.0) < 1e-6
    assert glm.site_in_fov(HN["lat"], HN["lon"], west, 72.5) is True        # the limit matters
    assert glm.site_in_fov("x", 0.0, east, 70.0) is False                   # junk: not covered


# ---- granule parsing ---------------------------------------------------------------------------
def test_parse_granule_float32(h5):
    start = datetime(2026, 10, 8, 15, 40, 20, tzinfo=UTC)
    g = glm.parse_granule(h5(start, [A, B], sat=b"G19"))
    assert g["sat"] == "G19" and g["start"] == start
    assert len(g["lats"]) == len(g["lons"]) == len(g["times"]) == 2
    assert abs(g["lats"][0] - A[0]) < 1e-4 and abs(g["lons"][0] - A[1]) < 1e-4
    assert abs(g["lats"][1] - B[0]) < 1e-4 and abs(g["lons"][1] - B[1]) < 1e-4
    assert isinstance(g["lats"][0], float) and isinstance(g["lons"][0], float)
    for t, (_, _, off) in zip(g["times"], (A, B)):
        assert t.tzinfo is not None
        assert abs((t - start).total_seconds() - off) < 0.01     # packed to 0.4 ms


@pytest.mark.parametrize("scalar_attrs", [False, True])
def test_parse_granule_packed_int16(h5, scalar_attrs):
    """Older files pack lat/lon as unsigned int16 in a signed container (``_Unsigned``),
    with ``scale_factor`` / ``add_offset`` as 1-element arrays (live files) or scalars."""
    start = datetime(2026, 10, 8, 15, 40, 20, tzinfo=UTC)
    g = glm.parse_granule(h5(start, [A, D], sat=b"G18", packed=True, scalar_attrs=scalar_attrs))
    assert g["sat"] == "G18" and len(g["lats"]) == 2
    assert abs(g["lats"][0] - A[0]) < 0.01 and abs(g["lons"][0] - A[1]) < 0.01
    assert abs(g["lats"][1] - D[0]) < 0.01 and abs(g["lons"][1] - D[1]) < 0.01   # -155.46 W
    assert abs((g["times"][1] - start).total_seconds() - D[2]) < 0.01


def test_parse_granule_without_time_offsets_and_without_flashes(h5):
    start = datetime(2026, 10, 8, 15, 40, 20, tzinfo=UTC)
    g = glm.parse_granule(h5(start, [A], with_times=False))
    assert g["times"] == [start]                                 # the granule start stands in
    g = glm.parse_granule(h5(start, []))
    assert g["lats"] == [] and g["lons"] == [] and g["times"] == [] and g["start"] == start


def test_parse_granule_rejects_junk(h5):
    for junk in (b"", b"garbage", b"\x89HDF\r\n\x1a\n" + b"\0" * 100, "text", None, 3):
        with pytest.raises(ValueError):
            glm.parse_granule(junk)


def test_parse_granule_without_deps_raises_value_error(monkeypatch):
    monkeypatch.setattr(glm, "np", None)
    with pytest.raises(ValueError):
        glm.parse_granule(b"\x89HDF\r\n\x1a\n")


def test_deps_note(monkeypatch):
    if glm.deps_available():
        assert glm.deps_note() == ""
    monkeypatch.setattr(glm, "np", None)
    assert not glm.deps_available() and "numpy" in glm.deps_note()
    monkeypatch.setattr(glm, "h5py", None)
    monkeypatch.setattr(glm, "netCDF4", None)
    assert "h5py" in glm.deps_note() and "netcdf4" in glm.deps_note().lower()


# ---- the transport ----------------------------------------------------------------------------
def test_get_uses_urlopen_with_the_user_agent(cfg, monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"body"

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["ua"] = req.get_header("User-agent")
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert glm._get("https://noaa-goes19.s3.amazonaws.com/x", cfg, 7.5) == b"body"
    assert seen == {"url": "https://noaa-goes19.s3.amazonaws.com/x", "ua": cfg.user_agent,
                    "timeout": 7.5}


# ---- fetch ------------------------------------------------------------------------------------
def test_fetch_both_satellites(cfg, fake_get):
    res = glm.fetch(cfg, now=NOW)
    assert set(res) == set(glm._RESULT_KEYS)
    assert res["ok"] is True and res["stale"] is False and res["error"] is None
    assert res["age_s"] == 0.0 and res["partial"] is False and res["skipped"] is None
    assert res["deps"] is True and 0.0 <= res["elapsed_s"] < 30
    # the last 5 min plus the 60 s the previous run's window may have missed
    assert res["window_end"] == NOW and res["window_start"] == NOW - timedelta(minutes=6)
    # one listing per bucket (the window stays inside hour 15), 18 granules each
    assert len(fake_get.listing_calls()) == 2
    assert all(u.endswith("prefix=GLM-L2-LCFA/2026/281/15/") for u in fake_get.listing_calls())
    assert len(fake_get.granule_calls()) == 36
    assert set(res["sats"]) == {"G19", "G18"}
    for sid, bucket, sub_lon, n in (("G19", EAST, -75.2, 3), ("G18", WEST, -137.0, 2)):
        s = res["sats"][sid]
        assert set(s) == {"bucket", "sub_lon", "ok", "error", "listed", "selected", "fetched",
                          "failed", "flashes"}
        assert s["bucket"] == bucket and s["sub_lon"] == sub_lon and s["ok"] is True
        assert s["error"] is None and s["listed"] == 180 and s["selected"] == 18
        assert s["fetched"] == 18 and s["failed"] == 0 and s["flashes"] == n
    assert len(res["flashes"]) == 5
    assert {f["sat"] for f in res["flashes"]} == {"G19", "G18"}
    fa = next(f for f in res["flashes"] if abs(f["lat"] - A[0]) < 1e-4 and f["sat"] == "G19")
    assert fa["t"].tzinfo is not None
    assert abs((fa["t"] - datetime(2026, 10, 8, 15, 42, 20, tzinfo=UTC)).total_seconds() - 2.5) < 0.01
    # F sits in the granule just before the window: never fetched
    assert not any(abs(f["lat"] - F[0]) < 1e-6 and abs(f["lon"] - F[1]) < 1e-6 for f in res["flashes"])
    assert all(set(f) == {"lat", "lon", "sat", "t"} for f in res["flashes"])


def test_fetch_window_crossing_the_hour_lists_two_prefixes(cfg, fake_get):
    now = datetime(2026, 10, 8, 15, 2, 0, tzinfo=UTC)
    res = glm.fetch(cfg, now=now)
    assert res["ok"] is True
    prefixes = sorted(u.rsplit("prefix=", 1)[1] for u in fake_get.listing_calls())
    assert prefixes == ["GLM-L2-LCFA/2026/281/14/", "GLM-L2-LCFA/2026/281/14/",
                        "GLM-L2-LCFA/2026/281/15/", "GLM-L2-LCFA/2026/281/15/"]
    for s in res["sats"].values():
        assert s["listed"] == 360 and s["selected"] == 19 and s["fetched"] == 19   # 14:56:00..15:02:00


def test_fetch_stride_thins_the_granules(cfg, fake_get):
    cfg.glm_stride = 2
    res = glm.fetch(cfg, now=NOW)
    assert res["ok"] is True
    for s in res["sats"].values():
        assert s["selected"] == 9 and s["fetched"] == 9
    newest = "s20262811542200"
    assert all(any(newest in u for u in fake_get.granule_calls()) for _ in (0,))   # newest kept
    assert len(fake_get.granule_calls()) == 18


def test_fetch_one_listing_fails_is_partial(cfg, fake_get):
    fake_get.fail_listing.add(WEST)
    res = glm.fetch(cfg, now=NOW)
    assert res["ok"] is True and res["partial"] is True
    assert res["error"] and "G18" in res["error"] and "listing" in res["error"]
    assert res["sats"]["G19"]["ok"] is True and res["sats"]["G19"]["flashes"] == 3
    g18 = res["sats"]["G18"]
    assert g18["ok"] is False and "listing failed" in g18["error"] and "listing down" in g18["error"]
    assert g18["listed"] == g18["selected"] == g18["fetched"] == g18["failed"] == 0
    assert {f["sat"] for f in res["flashes"]} == {"G19"}
    assert len(fake_get.granule_calls()) == 18


def test_fetch_one_granule_fails_or_is_garbage(cfg, fake_get):
    keys = sorted(fake_get.keys[EAST])
    fake_get.fail_keys.add(_key("G19", datetime(2026, 10, 8, 15, 41, 0, tzinfo=UTC)))
    fake_get.garbage_keys.add(_key("G19", datetime(2026, 10, 8, 15, 41, 20, tzinfo=UTC)))
    assert all(k in keys for k in fake_get.fail_keys | fake_get.garbage_keys)
    res = glm.fetch(cfg, now=NOW)
    assert res["ok"] is True and res["partial"] is True
    g19 = res["sats"]["G19"]
    assert g19["ok"] is False and g19["fetched"] == 16 and g19["failed"] == 2
    assert g19["error"].startswith("2 of 18 granules failed (")
    assert g19["flashes"] == 3                       # the good granules still count
    assert res["sats"]["G18"]["ok"] is True
    assert "G19" in res["error"] and "G18" not in res["error"]


def test_fetch_everything_failing(cfg, fake_get):
    fake_get.fail_listing.update({EAST, WEST})
    res = glm.fetch(cfg, now=NOW)
    assert res["ok"] is False and res["partial"] is True and res["age_s"] is None
    assert "G19" in res["error"] and "G18" in res["error"]
    assert res["flashes"] == [] and res["skipped"] is None
    assert all(s["ok"] is False for s in res["sats"].values())


def test_fetch_no_granules_in_the_window(cfg, fake_get):
    res = glm.fetch(cfg, now=datetime(2026, 10, 8, 3, 0, 0, tzinfo=UTC))    # hour 02/03: nothing
    assert res["ok"] is False and res["flashes"] == []
    assert all("no granules" in s["error"] for s in res["sats"].values())
    assert fake_get.granule_calls() == []


def test_fetch_survives_a_transport_that_raises_anything(cfg, monkeypatch):
    def broken(url, cfg_, timeout):
        raise TypeError("very broken")
    monkeypatch.setattr(glm, "_get", broken)
    monkeypatch.setattr(glm, "deps_available", lambda: True)      # CI may lack numpy / h5py
    res = glm.fetch(cfg, now=NOW)
    assert res["ok"] is False and "very broken" in res["error"]
    res = glm.fetch(cfg, now=NOW.replace(tzinfo=None))        # a naive now is taken as UTC
    assert res["window_end"] == NOW


def test_fetch_skips(cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(glm, "_get", lambda url, c, t: calls.append(url))
    cfg.glm_enabled = False
    res = glm.fetch(cfg, now=NOW)
    assert res["ok"] is False and "disabled" in res["skipped"] and res["error"] == res["skipped"]
    assert set(res) == set(glm._RESULT_KEYS) and res["sats"] == {} and res["flashes"] == []
    cfg.glm_enabled = True
    cfg.glm_buckets = ""
    res = glm.fetch(cfg, now=NOW)
    assert "VLBA_GLM_BUCKETS" in res["skipped"]
    cfg.glm_buckets = "noaa-goes19=-75.2;noaa-goes18=-137.0"
    monkeypatch.setattr(glm, "np", None)
    res = glm.fetch(cfg, now=NOW)
    assert res["deps"] is False and "numpy" in res["skipped"] and res["error"] == res["skipped"]
    monkeypatch.undo()
    monkeypatch.setattr(glm, "_get", lambda url, c, t: calls.append(url))
    monkeypatch.setattr(glm, "deps_available", lambda: True)
    monkeypatch.setattr(_http, "budget_left", lambda: 9.0)
    res = glm.fetch(cfg, now=NOW)
    assert "budget" in res["skipped"] and res["ok"] is False
    assert calls == []


def test_deadline_follows_the_run_budget(cfg, monkeypatch):
    assert glm._deadline_s(cfg) == 45.0
    monkeypatch.setattr(_http, "budget_left", lambda: 12.0)
    assert glm._deadline_s(cfg) == 7.0                   # 12 s left minus the 5 s reserve
    monkeypatch.setattr(_http, "budget_left", lambda: 100.0)
    assert glm._deadline_s(cfg) == 45.0


def test_fetch_deadline_abandons_slow_granules(cfg, fake_get):
    cfg.glm_timeout_s = 1
    fake_get.block = threading.Event()
    try:
        res = glm.fetch(cfg, now=NOW)
    finally:
        fake_get.block.set()
    assert res["elapsed_s"] < 5.0
    assert res["ok"] is False and "deadline reached" in res["error"]
    for s in res["sats"].values():
        assert s["selected"] == 18 and s["fetched"] == 0 and s["failed"] == 18
        assert s["ok"] is False and "deadline reached" in s["error"]
    assert len(fake_get.granule_calls()) <= cfg.glm_workers


def test_fetch_result_without_any_satellite_in_sats_dict_keys(cfg, fake_get):
    """The run dict drops ``flashes``; everything else must be JSON-friendly scalars."""
    res = glm.fetch(cfg, now=NOW)
    for s in res["sats"].values():
        assert all(isinstance(v, (str, float, int, bool, type(None))) for v in s.values())


# ---- the per-site view ---------------------------------------------------------------------
VIEW_KEYS = {"covered", "sats", "available", "partial", "error", "count_window", "count_by_sat",
             "last_in_ring_t", "nearest_km", "nearest_bearing", "nearest_t", "nearest_sat", "flashes_on_map",
             "window_min"}


def _res(flashes, sats=None, ok=True, **extra):
    base = {"ok": ok, "stale": False, "error": None, "age_s": 0.0 if ok else None,
            "window_start": NOW - timedelta(minutes=5), "window_end": NOW, "flashes": flashes,
            "sats": sats if sats is not None else {
                "G19": {"bucket": EAST, "sub_lon": -75.2, "ok": True, "error": None, "listed": 180,
                        "selected": 15, "fetched": 15, "failed": 0, "flashes": 0},
                "G18": {"bucket": WEST, "sub_lon": -137.0, "ok": True, "error": None, "listed": 180,
                        "selected": 15, "fetched": 15, "failed": 0, "flashes": 0}},
            "deps": True, "elapsed_s": 1.0, "skipped": None, "partial": False}
    base.update(extra)
    return base


def _fl(lat, lon, sat, t=None):
    return {"lat": lat, "lon": lon, "sat": sat, "t": t or NOW}


def test_site_view_counts_nearest_and_map(cfg, fake_get):
    res = glm.fetch(cfg, now=NOW)
    pt = glm.site_view(res, PT, cfg, now=NOW)
    assert set(pt) == VIEW_KEYS
    assert pt["covered"] is True and pt["sats"] == ["G19", "G18"]
    assert pt["available"] is True and pt["partial"] is False and pt["error"] is None
    assert pt["count_window"] == 2                       # A (11 km) and C (28 km); E is 80 km out
    assert pt["nearest_km"] == round(geo.haversine_km(PT["lat"], PT["lon"], A[0], A[1]), 1)
    assert 11.0 < pt["nearest_km"] < 11.2 and pt["nearest_bearing"] == "N" and pt["nearest_sat"] == "G19"
    assert pt["nearest_t"] == next(f["t"] for f in res["flashes"] if abs(f["lat"] - A[0]) < 1e-4)
    assert len(pt["flashes_on_map"]) == 3 and pt["flashes_on_map"][0] == (
        next(f["lat"] for f in res["flashes"] if abs(f["lat"] - A[0]) < 1e-4),
        next(f["lon"] for f in res["flashes"] if abs(f["lat"] - A[0]) < 1e-4))
    assert all(isinstance(p, tuple) and len(p) == 2 for p in pt["flashes_on_map"])
    assert pt["count_by_sat"] == {"G19": 1, "G18": 1}
    assert pt["window_min"] == 6                          # the fetch window, overlap included
    mk = glm.site_view(res, MK, cfg)
    assert mk["sats"] == ["G18"] and mk["available"] is True
    assert mk["count_window"] == 1 and 21 < mk["nearest_km"] < 23 and mk["nearest_bearing"] == "N"
    assert mk["nearest_sat"] == "G18" and len(mk["flashes_on_map"]) == 1
    sc = glm.site_view(res, SC, cfg)
    assert sc["sats"] == ["G19"] and sc["count_window"] == 1 and sc["nearest_sat"] == "G19"
    assert glm.site_view(res, PT, cfg, map_radius_km=50.0)["flashes_on_map"] == pt["flashes_on_map"][:2]


def test_site_view_bearings_and_ring(cfg):
    d = 0.3
    res = _res([_fl(PT["lat"], PT["lon"] + d, "G19"), _fl(PT["lat"] - d, PT["lon"] - d, "G18"),
                _fl(PT["lat"] + 1.0, PT["lon"], "G19")])
    v = glm.site_view(res, PT, cfg)
    assert v["nearest_bearing"] == "E" and v["count_window"] == 2      # 28 km E and 43 km SW
    assert 27 < v["nearest_km"] < 28 and v["nearest_sat"] == "G19"
    assert v["flashes_on_map"] == [(PT["lat"], PT["lon"] + d), (PT["lat"] - d, PT["lon"] - d),
                                   (PT["lat"] + 1.0, PT["lon"])]
    cfg.glm_km = 30.0
    assert glm.site_view(res, PT, cfg)["count_window"] == 1
    cfg.glm_km = 120.0
    assert glm.site_view(res, PT, cfg)["count_window"] == 3
    res = _res([_fl(PT["lat"] - d, PT["lon"] - d, "G18")])
    assert glm.site_view(res, PT, cfg)["nearest_bearing"] == "SW"
    res = _res([])
    v = glm.site_view(res, PT, cfg)
    assert v["available"] is True and v["count_window"] == 0 and v["nearest_km"] is None
    assert v["nearest_bearing"] is None and v["nearest_t"] is None and v["nearest_sat"] is None
    assert v["flashes_on_map"] == []


def test_site_view_only_the_covering_satellites_count(cfg):
    # a G19 flash right at Mauna Kea must not count there (MK takes GOES-West only), a G18
    # flash at St. Croix not there either
    res = _res([_fl(MK["lat"] + 0.05, MK["lon"], "G19")])
    mk = glm.site_view(res, MK, cfg)
    assert mk["count_window"] == 0 and mk["nearest_km"] is None and mk["flashes_on_map"] == []
    res = _res([_fl(MK["lat"] + 0.05, MK["lon"], "G19"), _fl(SC["lat"] + 0.05, SC["lon"], "G18")])
    mk = glm.site_view(res, MK, cfg)
    assert mk["count_window"] == 0 and mk["nearest_sat"] == "G18"
    assert mk["nearest_km"] > 9000 and mk["flashes_on_map"] == []       # the G18 one, at St. Croix
    sc = glm.site_view(res, SC, cfg)
    assert sc["count_window"] == 0 and sc["nearest_sat"] == "G19" and sc["nearest_km"] > 9000


def test_site_view_map_cap_keeps_the_nearest(cfg):
    pts = [(PT["lat"] - 0.5 + i * 0.02 + i * 1e-4, PT["lon"] - 0.5 + j * 0.02 + j * 3e-4)
           for i in range(50) for j in range(50)]
    assert len(pts) == 2500
    res = _res([_fl(lat, lon, "G19") for lat, lon in pts])
    v = glm.site_view(res, PT, cfg)
    assert len(v["flashes_on_map"]) == 2000
    by_dist = sorted(pts, key=lambda p: geo.haversine_km(PT["lat"], PT["lon"], p[0], p[1]))
    assert set(v["flashes_on_map"]) == set(by_dist[:2000])
    assert v["flashes_on_map"][0] == by_dist[0]


def test_site_view_not_covered(cfg):
    res = _res([_fl(0.1, 60.0, "G19")])
    v = glm.site_view(res, {"slug": "io", "name": "Indian Ocean", "lat": 0.0, "lon": 60.0}, cfg)
    assert v["covered"] is False and v["sats"] == [] and v["available"] is False
    assert v["partial"] is False and "field of view" in v["error"]
    assert v["count_window"] == 0 and v["flashes_on_map"] == [] and v["nearest_km"] is None
    cfg.glm_buckets = ""
    v = glm.site_view(res, PT, cfg)
    assert v["covered"] is False and "no GLM satellite configured" in v["error"]
    cfg.glm_buckets = "noaa-goes19=-75.2"
    v = glm.site_view(res, MK, cfg)
    assert v["covered"] is False and v["sats"] == []
    v = glm.site_view(res, PT, cfg)
    assert v["covered"] is True and v["sats"] == ["G19"]


def test_site_view_partial_and_unavailable(cfg):
    sats = {"G19": {"bucket": EAST, "sub_lon": -75.2, "ok": True, "error": None, "listed": 180,
                    "selected": 15, "fetched": 15, "failed": 0, "flashes": 1},
            "G18": {"bucket": WEST, "sub_lon": -137.0, "ok": False, "error": "listing failed: down",
                    "listed": 0, "selected": 0, "fetched": 0, "failed": 0, "flashes": 0}}
    res = _res([_fl(PT["lat"] + 0.1, PT["lon"], "G19")], sats=sats, partial=True,
               error="G18: listing failed: down")
    pt = glm.site_view(res, PT, cfg)
    # GOES-East alone sees Pie Town whole: available, flagged partial
    assert pt["covered"] and pt["available"] is True and pt["partial"] is True
    assert "G18" in pt["error"] and pt["count_window"] == 1 and pt["nearest_sat"] == "G19"
    mk = glm.site_view(res, MK, cfg)
    assert mk["available"] is False and mk["partial"] is False       # nothing of G18 arrived
    assert "G18" in mk["error"] and mk["count_window"] == 0
    sc = glm.site_view(res, SC, cfg)
    assert sc["available"] is True and sc["partial"] is False and sc["error"] is None
    # a satellite with some granules missing: partial, the count is a lower bound
    sats["G18"].update(ok=False, error="3 of 15 granules failed (timed out)", listed=180,
                       selected=15, fetched=12, failed=3)
    mk = glm.site_view(res, MK, cfg)
    assert mk["available"] is False and mk["partial"] is True and "3 of 15" in mk["error"]
    # the whole fetch failed / skipped
    v = glm.site_view(_res([], ok=False, error="G19: listing failed; G18: listing failed"), PT, cfg)
    assert v["covered"] is True and v["available"] is False and v["partial"] is False
    assert "listing failed" in v["error"]
    v = glm.site_view(_res([], ok=False, skipped="GLM layer disabled (VLBA_GLM=0)"), PT, cfg)
    assert v["available"] is False and "disabled" in v["error"]
    # a satellite the result does not know (an older run dict): the other one suffices
    v = glm.site_view(_res([], sats={"G19": sats["G19"]}), PT, cfg)
    assert v["available"] is True and v["partial"] is True and "G18" in v["error"]
    v = glm.site_view(_res([], sats={"G19": sats["G19"]}), MK, cfg)
    assert v["available"] is False and v["partial"] is False and "G18" in v["error"]


def test_site_view_keeps_the_nearer_copy_and_matches_one_to_one(cfg):
    """One flash, G19's copy 52 km out, G18's 47 km: it is in the 50-km ring (the nearer copy
    is kept). A G19 detection never absorbs two G18 flashes."""
    t = NOW - timedelta(seconds=30)
    out52 = geo.dest_point(PT["lat"], PT["lon"], 52.0, 0.0)
    in47 = geo.dest_point(PT["lat"], PT["lon"], 47.0, 0.0)
    v = glm.site_view(_res([_fl(out52[0], out52[1], "G19", t),
                            _fl(in47[0], in47[1], "G18", t + timedelta(milliseconds=300))]), PT, cfg)
    assert v["count_window"] == 1 and v["count_by_sat"] == {"G19": 0, "G18": 1}
    a = geo.dest_point(PT["lat"], PT["lon"], 20.0, 0.0)
    b = geo.dest_point(PT["lat"], PT["lon"], 20.0, 90.0)          # ~28 km from a: never the same flash
    c = geo.dest_point(PT["lat"], PT["lon"], 24.0, 10.0)          # ~5 km from a
    v = glm.site_view(_res([_fl(a[0], a[1], "G19", t), _fl(c[0], c[1], "G18", t + timedelta(milliseconds=200)),
                            _fl(a[0] + 0.01, a[1], "G18", t + timedelta(milliseconds=400)),
                            _fl(b[0], b[1], "G18", t + timedelta(milliseconds=600))]), PT, cfg)
    # G19's flash absorbs ONE of the two nearby G18 detections; b is a flash of its own
    assert v["count_window"] == 3


def test_site_view_counts_a_flash_both_satellites_saw_once(cfg):
    t = NOW - timedelta(seconds=30)
    near = (PT["lat"] + 0.1, PT["lon"])
    res = _res([_fl(near[0], near[1], "G19", t),
                _fl(near[0] + 0.03, near[1] + 0.02, "G18", t + timedelta(milliseconds=300)),   # same flash, 4 km off
                _fl(near[0], near[1], "G18", t + timedelta(seconds=5)),                       # a later flash
                _fl(PT["lat"] - 0.3, PT["lon"], "G18", t)])                                   # 33 km S, same time
    v = glm.site_view(res, PT, cfg)
    assert v["count_by_sat"] == {"G19": 1, "G18": 3}
    assert v["count_window"] == 3 and len(v["flashes_on_map"]) == 3
    assert v["flashes_on_map"][0] == near and v["nearest_sat"] == "G19"


def test_fetch_window_overlap_catches_granules_published_late(cfg, fake_get):
    """The newest granule is not listed yet when a run looks (published some seconds after
    its end); the next run, five minutes later, must still fetch it."""
    late = _key("G19", datetime(2026, 10, 8, 15, 42, 20, tzinfo=UTC))
    full = list(fake_get.keys[EAST])
    fake_get.keys[EAST] = [k for k in full if k != late]
    glm.fetch(cfg, now=NOW)
    assert not any("G19_s20262811542200" in u for u in fake_get.granule_calls())
    fake_get.keys[EAST] = full
    fake_get.calls.clear()
    res = glm.fetch(cfg, now=NOW + timedelta(minutes=5))
    assert any("G19_s20262811542200" in u for u in fake_get.granule_calls())
    assert res["window_start"] <= datetime(2026, 10, 8, 15, 42, 20, tzinfo=UTC)


def test_site_view_never_raises(cfg):
    for bad in (None, {}, [], "x", 7, {"ok": True}, {"ok": True, "flashes": "zzz", "sats": 5},
                {"ok": True, "flashes": [None, 1, {"lat": "a", "lon": 1, "sat": "G19"},
                                         {"lat": float("nan"), "lon": 0, "sat": "G19"},
                                         {"lat": 95.0, "lon": 0, "sat": "G19"},
                                         {"lon": PT["lon"], "sat": "G19"}],
                 "sats": {"G19": "junk", "G18": None}}):
        v = glm.site_view(bad, PT, cfg)
        assert set(v) == VIEW_KEYS and v["available"] is False and v["count_window"] == 0
        assert v["flashes_on_map"] == [] and v["error"]
    good = _res([_fl(PT["lat"] + 0.1, PT["lon"], "G19"), {"lat": "junk", "lon": 1, "sat": "G19"},
                 {"lat": PT["lat"], "lon": PT["lon"], "sat": "G18", "t": "not a datetime"}])
    v = glm.site_view(good, PT, cfg)
    assert v["available"] is True and v["count_window"] == 2 and v["nearest_t"] is None
    v = glm.site_view(good, {"slug": "x"}, cfg)                       # no coordinates
    assert set(v) == VIEW_KEYS and v["available"] is False and "bad GLM result" in v["error"]
    v = glm.site_view(good, PT, object())                             # not a config
    assert v["available"] is False and v["window_min"] is None


def test_fetch_catches_up_on_what_the_last_run_could_not_see(cfg, fake_get):
    """The newest granules were not published when run k listed them, and run k+1 comes
    late (the GLM step runs later in a slow run): the 60-s overlap alone would miss them,
    the newest granule run k examined (``since``) does not."""
    hidden = [_key(sat, datetime(2026, 10, 8, 15, 42, s, tzinfo=UTC)) for sat in ("G19", "G18") for s in (0, 20)]
    full = {b: list(k) for b, k in fake_get.keys.items()}
    for b in fake_get.keys:
        fake_get.keys[b] = [k for k in full[b] if k not in hidden]
    first = glm.fetch(cfg, now=NOW)
    t_last = datetime(2026, 10, 8, 15, 41, 40, tzinfo=UTC).timestamp()
    assert first["newest"] == {"G19": t_last, "G18": t_last}
    fake_get.keys = full
    late = NOW + timedelta(minutes=6, seconds=30)            # its own window starts 15:43:00
    fake_get.calls.clear()
    glm.fetch(cfg, now=late)
    assert not any("_s20262811542" in u for u in fake_get.granule_calls())
    fake_get.calls.clear()
    res = glm.fetch(cfg, now=late, since=first["newest"])
    got = fake_get.granule_calls()
    assert all(any(k.rsplit("/", 1)[1] in u for u in got) for k in hidden)
    assert res["window_start"] < datetime(2026, 10, 8, 15, 42, 0, tzinfo=UTC)
    assert res["newest"]["G19"] > t_last
    # a "since" older than MAX_CATCHUP_S reaches back MAX_CATCHUP_S, not further; junk is ignored,
    # and a number too big for a float (a corrupt state file) too
    res = glm.fetch(cfg, now=late, since={"G19": (late - timedelta(hours=1)).timestamp(), "G18": "x"})
    assert res["window_start"] == late - timedelta(seconds=glm.MAX_CATCHUP_S)
    res = glm.fetch(cfg, now=late, since={"G19": 10 ** 400, "G18": True})
    assert res["ok"] and res["window_start"] == late - timedelta(minutes=6) and set(res["newest"]) == {"G19", "G18"}


def test_failed_granules_are_retried_by_the_next_run(cfg, fake_get):
    """A granule that fails is fetched again by the next run, before anything else, even
    when that run's window no longer reaches it; the marker of the newest granule examined
    does not move back (a slow store would otherwise fetch whole windows again)."""
    cfg.glm_workers = 1                 # one download at a time: the order is the queue's
    t_failed = datetime(2026, 10, 8, 15, 40, 0, tzinfo=UTC)
    failed = _key("G19", t_failed)
    fake_get.fail_keys.add(failed)
    res = glm.fetch(cfg, now=NOW)
    assert res["sats"]["G19"]["failed"] == 1 and res["partial"]
    assert res["retry"] == {"G19": [t_failed.timestamp()]}
    newest = datetime(2026, 10, 8, 15, 42, 20, tzinfo=UTC).timestamp()
    assert res["newest"] == {"G19": newest, "G18": newest}
    fake_get.fail_keys.clear()
    fake_get.calls.clear()
    res = glm.fetch(cfg, now=NOW + timedelta(minutes=8), since=res["newest"], retried=res["retry"])
    assert failed.rsplit("/", 1)[1] in fake_get.granule_calls()[0]            # first of all
    assert res["window_start"] == t_failed
    assert res["sats"]["G19"]["ok"] and not res["partial"] and res["retry"] == {}


def test_a_failed_granule_is_tried_twice_then_dropped(cfg, fake_get):
    """A granule that does not arrive (here one of the catch-up part, older than the regular
    window) leaves the run partial and is fetched again by the next run; failing again, it
    is given up, that run still partial. The run after that no longer looks at it."""
    t_old = datetime(2026, 10, 8, 15, 32, 0, tzinfo=UTC)                    # before the regular window
    old = _key("G19", t_old)
    fake_get.fail_keys.add(old)
    res = glm.fetch(cfg, now=NOW, since={"G19": datetime(2026, 10, 8, 15, 30, 0, tzinfo=UTC).timestamp()})
    g19 = res["sats"]["G19"]
    assert res["window_start"] < t_old
    assert g19["ok"] is False and g19["failed"] == 1 and res["partial"] and "not tried again" not in g19["error"]
    assert res["retry"] == {"G19": [t_old.timestamp()]}
    assert res["newest"]["G19"] == datetime(2026, 10, 8, 15, 42, 20, tzinfo=UTC).timestamp()
    fake_get.calls.clear()
    res = glm.fetch(cfg, now=NOW + timedelta(minutes=2), since=res["newest"], retried=res["retry"])
    assert any(old.rsplit("/", 1)[1] in u for u in fake_get.granule_calls())
    g19 = res["sats"]["G19"]
    assert g19["failed"] == 1 and g19["dropped"] == 1 and res["partial"] and "not tried again" in g19["error"]
    assert res["retry"] == {}
    fake_get.calls.clear()
    res = glm.fetch(cfg, now=NOW + timedelta(minutes=4), since=res["newest"], retried=res["retry"])
    assert not any(old.rsplit("/", 1)[1] in u for u in fake_get.granule_calls())
    assert res["sats"]["G19"]["ok"] and not res["partial"]


def test_a_failed_refetch_of_the_overlap_is_no_gap(cfg, fake_get):
    """The window reaches 60 s into the last run's: a granule that run examined and that
    fails now leaves nothing unexamined, so the run is complete and nothing is retried. One
    published too late for the last run (it never saw it) is a gap: retried."""
    t_over = datetime(2026, 10, 8, 15, 42, 0, tzinfo=UTC)
    over = _key("G19", t_over)
    res = glm.fetch(cfg, now=NOW)
    assert t_over.timestamp() in res["seen"]["G19"]
    fake_get.fail_keys.add(over)
    later = glm.fetch(cfg, now=NOW + timedelta(minutes=5), since=res["newest"], retried=res["retry"],
                      seen=res["seen"])
    g19 = later["sats"]["G19"]
    assert g19["ok"] and g19["failed"] == 0 and g19["refetch_failed"] == 1
    assert not later["partial"] and later["retry"] == {}
    late = {"G19": [x for x in res["seen"]["G19"] if x != t_over.timestamp()]}      # not listed then
    later = glm.fetch(cfg, now=NOW + timedelta(minutes=5), since=res["newest"], retried=res["retry"], seen=late)
    assert later["partial"] and later["retry"] == {"G19": [t_over.timestamp()]}


def test_granules_to_fetch_again_are_kept_or_given_up(cfg, fake_get, monkeypatch):
    """A satellite that cannot be listed (or a fetch that fails as a whole) keeps what was
    to be fetched again; one older than MAX_CATCHUP_S, or gone from the store, is given up,
    and the run says so."""
    t = datetime(2026, 10, 8, 15, 40, 0, tzinfo=UTC).timestamp()
    fake_get.fail_listing.add(EAST)
    res = glm.fetch(cfg, now=NOW, retried={"G19": [t]})
    assert res["retry"] == {"G19": [t]} and res["partial"]
    fake_get.fail_listing.clear()
    too_old = (NOW - timedelta(seconds=glm.MAX_CATCHUP_S + 60)).timestamp()
    gone = datetime(2026, 10, 8, 15, 40, 5, tzinfo=UTC).timestamp()        # no granule starts then
    res = glm.fetch(cfg, now=NOW, retried={"G19": [too_old, gone]})
    g19 = res["sats"]["G19"]
    assert g19["dropped"] == 2 and g19["failed"] == 2 and res["partial"] and res["retry"] == {}

    def boom(*a, **k):
        raise RuntimeError("surprise")
    monkeypatch.setattr(glm, "_fetch", boom)
    res = glm.fetch(cfg, now=NOW, since={"G19": t}, retried={"G19": [t, "junk"], "G18": []}, seen={"G18": [t]})
    assert res["retry"] == {"G19": [t]} and res["newest"] == {"G19": t} and res["seen"] == {"G18": [t]}


def test_a_retry_the_deadline_never_reached_stays_pending(cfg, fake_get):
    """A granule to fetch again that was asked for and hung is given up; one the deadline
    cut before it was even asked for was never tried, and stays to be fetched again."""
    cfg.glm_workers = 1
    cfg.glm_timeout_s = 1
    a = datetime(2026, 10, 8, 15, 40, 0, tzinfo=UTC).timestamp()
    b = datetime(2026, 10, 8, 15, 40, 20, tzinfo=UTC).timestamp()
    fake_get.block = threading.Event()
    try:
        res = glm.fetch(cfg, now=NOW, retried={"G19": [a, b]})
    finally:
        fake_get.block.set()
    g19 = res["sats"]["G19"]
    assert g19["dropped"] == 1 and res["partial"]
    assert a in res["retry"]["G19"] and b not in res["retry"]["G19"]       # b went first and hung


def test_a_junk_retry_list_is_ignored(cfg, fake_get):
    res = glm.fetch(cfg, now=NOW, retried={"G19": ["x", None, True, float("nan"), 10 ** 400, 1e300, -5.0], "G18": 5},
                    seen={"G19": [1e300, "x"], "G18": None})
    assert res["ok"] and not res["partial"] and res["retry"] == {}
    assert glm.fetch(cfg, now=NOW, retried=[1, 2])["ok"]


def test_catch_up_never_shrinks_a_long_window(cfg, fake_get):
    cfg.glm_window_min = 30
    res = glm.fetch(cfg, now=NOW, since={"G19": (NOW - timedelta(hours=2)).timestamp()})
    assert res["window_start"] == NOW - timedelta(minutes=31)


def test_site_view_gives_the_time_of_the_newest_flash_in_the_ring(cfg):
    t1, t2 = NOW - timedelta(minutes=12), NOW - timedelta(minutes=3)
    near = geo.dest_point(PT["lat"], PT["lon"], 20.0, 0.0)
    far = geo.dest_point(PT["lat"], PT["lon"], 80.0, 0.0)
    v = glm.site_view(_res([_fl(near[0], near[1], "G19", t1), _fl(far[0], far[1], "G19", t2)]), PT, cfg)
    assert v["count_window"] == 1 and v["last_in_ring_t"] == t1
