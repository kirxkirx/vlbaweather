"""MRMS radar coverage (vlba.mrms): the GRIB2 reader takes what MRMS writes and refuses the
rest, the ring count separates "no coverage" (-999) from "no echo" (-99) with the ring
check's own distance, and the file of the frame's time is found, or the nearest one, or the
failure is reported, never raised."""
from __future__ import annotations

import gzip
from datetime import datetime, timedelta, timezone

import pytest

from weather import geo, http, util
from vlba import mrms
from vlba.config import mirror_name_regex
from vlba.tests.conftest import ISLAND_PARAMETER, PT, grib2_around, grib2_message

UTC = timezone.utc
TS = datetime(2026, 10, 12, 17, 6, tzinfo=UTC)
LAT, LON = PT["lat"], PT["lon"]
WEST_HALF = ((LAT - 1, LAT + 1, LON - 1, LON),)


def _meta(raw):
    meta = mrms.parse_grib2(gzip.decompress(raw))
    return meta, mrms.decode(meta)


def test_parse_reads_what_mrms_writes():
    meta, im = _meta(grib2_around(LAT, LON, TS))
    assert meta["ts"] == TS
    g = meta["grid"]
    assert (g["ni"], g["nj"], g["di"], g["dj"], g["scan"]) == (80, 80, 0.01, 0.01, 0)
    assert g["la1"] == pytest.approx(LAT + 0.395) and g["lo1"] == pytest.approx((LON - 0.395) % 360)
    assert meta["drs"] == {"R": -9990.0, "E": 0, "D": 1, "nbits": 16}
    assert im.size == (80, 80) and im.getpixel((0, 0)) == 9000         # -99 dBZ: (X - 9990) / 10


@pytest.mark.parametrize("kw, words", [
    ({"template3": 30}, "grid template 3.30"),
    ({"template5": 0}, "data template 5.0"),
    ({"bitmap": 0}, "bitmap"),
    ({"scan": 0x80}, "scanning mode 0x80"),
])
def test_parse_refuses_what_it_cannot_read(kw, words):
    with pytest.raises(ValueError, match=words):
        mrms.parse_grib2(grib2_message([[-99.0] * 4] * 4, 34.0, 250.0, 0.01, 0.01, TS, gz=False, **kw))


def test_grid_coordinates_in_micro_degrees_by_default():
    """MRMS writes basic angle 1 and 10**6 subdivisions; 0 and 0 mean micro-degrees too."""
    for angle in ((1, 1000000), (0, 0), (0xFFFFFFFF, 0xFFFFFFFF)):
        meta = mrms.parse_grib2(grib2_message([[-99.0] * 4] * 4, 34.5, 251.25, 0.01, 0.01, TS, gz=False, angle=angle))
        g = meta["grid"]
        assert (g["la1"], g["lo1"], g["di"], g["dj"]) == pytest.approx((34.5, 251.25, 0.01, 0.01)), angle
    meta = mrms.parse_grib2(grib2_message([[-99.0] * 4] * 4, 34.5, 251.25, 0.01, 0.01, TS, gz=False, angle=(1, 1000)))
    assert meta["grid"]["la1"] == pytest.approx(34.5)                  # milli-degrees, read as such


def test_parse_refuses_a_broken_message():
    good = grib2_message([[-99.0] * 4] * 4, 34.0, 250.0, 0.01, 0.01, TS, gz=False)
    for raw, words in ((b"<html>NoSuchKey</html>", "not a GRIB2"), (good[:8] + b"\x01" + good[9:], None),
                       (good[:-40], "truncated"), (b"GRIB\0\0\0\x01" + good[8:], "not a GRIB2")):
        with pytest.raises(ValueError, match=words):
            mrms.parse_grib2(raw)


def test_ring_counts_uncovered_cells_with_the_ring_checks_distance():
    meta, im = _meta(grib2_around(LAT, LON, TS))
    full = mrms.ring_stats(meta, im, LAT, LON, 30.0)
    g = meta["grid"]
    lo1 = g["lo1"] - 360.0
    inside = sum(1 for j in range(g["nj"]) for i in range(g["ni"])
                 if geo.haversine_km(LAT, LON, g["la1"] - j * g["dj"], lo1 + i * g["di"]) <= 30.0)
    assert full == {"cells": inside, "uncovered": 0, "covered_pct": 100.0}
    meta, im = _meta(grib2_around(LAT, LON, TS, holes=WEST_HALF))
    half = mrms.ring_stats(meta, im, LAT, LON, 30.0)
    assert half["cells"] == inside and half["covered_pct"] == pytest.approx(50.0, abs=1.5)
    # an echo is coverage too: only -999 is "no radar here"
    meta, im = _meta(grib2_around(LAT, LON, TS, value=35.0))
    assert mrms.ring_stats(meta, im, LAT, LON, 30.0)["covered_pct"] == 100.0


def test_a_ring_beyond_the_grid_edge_is_uncovered_there():
    meta, im = _meta(grib2_around(LAT, LON, TS, span_deg=0.8))
    near_edge = mrms.ring_stats(meta, im, LAT + 0.35, LON, 30.0)      # the ring reaches past the north edge
    assert 0 < near_edge["uncovered"] < near_edge["cells"] and near_edge["covered_pct"] < 80
    far = mrms.ring_stats(meta, im, LAT + 5.0, LON, 30.0)              # wholly outside
    assert far["cells"] > 2000 and far["covered_pct"] == 0.0


def test_rows_from_the_south_are_read_too():
    rows = [[-999.0] * 80 if j < 40 else [-99.0] * 80 for j in range(80)]    # 0x40: the first rows are south
    meta, im = _meta(grib2_message(rows, LAT - 0.395, (LON - 0.395) % 360, 0.01, 0.01, TS, scan=0x40))
    s = mrms.ring_stats(meta, im, LAT, LON, 30.0)
    assert s["covered_pct"] == pytest.approx(50.0, abs=1.5)
    meta, im = _meta(grib2_message(rows[::-1], LAT + 0.395, (LON - 0.395) % 360, 0.01, 0.01, TS))
    assert mrms.ring_stats(meta, im, LAT, LON, 30.0) == s                    # the same grid, north first


def _key_url(cfg, domain, ts):
    return "%s/%s" % (cfg.mrms_url, mrms._key(domain, mrms.PRODUCTS[domain], ts))


def _listing(*keys):
    return "<ListBucketResult>%s</ListBucketResult>" % "".join(
        "<Contents><Key>%s</Key><Size>1</Size></Contents>" % k for k in keys)


def _bucket(fake_http, domain, times, hours):
    """The bucket's listing as S3 gives it: one answer per hour prefix, holding only that
    hour's files (``times``); ``hours`` are the hours a test expects to be listed."""
    product = mrms.PRODUCTS[domain]
    for h in hours:
        prefix = "prefix=%s/%s/%s/MRMS_%s_%s" % (domain, product, h.strftime("%Y%m%d"), product, h.strftime("%Y%m%d-%H"))
        keys = [mrms._key(domain, product, t) for t in times if t.replace(minute=0, second=0) == h]
        fake_http.add(prefix, _listing(*keys))


MK = {"slug": "mk", "lat": 19.8014, "lon": -155.4555}
BUCKET = "noaa-mrms-pds.s3.amazonaws.com/"
MIRROR = "mtarchive.geol.iastate.edu/2026/10/12/mrms/ncep/SeamlessHSR/"


def _island_file(ts, **kw):
    return grib2_around(MK["lat"], MK["lon"], ts, cell_deg=0.005, parameter=ISLAND_PARAMETER, **kw)


def _mirror_name(ts):
    return "SeamlessHSR_00.00_%s.grib2.gz" % ts.strftime("%Y%m%d-%H%M%S")


def _index_page(*times):
    """The mirror's index page as IEM's archive serves it (nginx, Apache-style; each name twice)."""
    rows = "".join('<tr><td valign="top"><img src="/icons/compressed.gif" alt="[   ]"></td><td><a href="%s">%s</a>'
                   '</td><td align="right">2026-10-12 12:06  </td><td align="right">958K</td></tr>\n'
                   % (_mirror_name(t), _mirror_name(t)) for t in times)
    return ('<html><head><title>Index of /2026/10/12/mrms/ncep/SeamlessHSR</title></head><body><table>'
            '<tr><td><a href="/2026/10/12/mrms/ncep/">Parent Directory</a></td></tr>\n%s</table></body></html>' % rows)


def _route(monkeypatch, fake_http, rule):
    """Send ``http.get_bytes`` through ``rule(url)`` first, which may raise (the circuit
    breaker's HttpSkipped, a timeout); ``fake_http`` answers otherwise. Returns the requests
    with their keyword arguments."""
    real = fake_http.get_bytes
    seen = []

    def get_bytes(url, cfg, *a, **k):
        seen.append((url, k))
        rule(url)
        return real(url, cfg, *a, **k)
    monkeypatch.setattr(http, "get_bytes", get_bytes)
    return seen


# ---- the file of the frame's time, or the nearest one -----------------------------------------
def test_coverage_reads_the_file_of_the_frames_time(cfg, fake_http):
    fake_http.add("CONUS/SeamlessHSR_00.00/20261012/MRMS_SeamlessHSR_00.00_20261012-170600.grib2.gz",
                  grib2_around(LAT, LON, TS, holes=WEST_HALF))
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert res["ok"] and res["error"] is None and res["ts"] == TS
    assert res["url"] == _key_url(cfg, "CONUS", TS) and fake_http.calls == [res["url"]]
    assert (res["source"], res["older"], res["behind_s"], res["note"]) == ("bucket", False, 0, None)
    assert set(res["sites"]) == {"pt"} and res["sites"]["pt"]["covered_pct"] == pytest.approx(50.0, abs=1.5)
    sc = mrms.site_coverage(res, "pt")
    assert sc["ok"] and sc["ts"] == TS and (sc["older"], sc["behind_s"], sc["source"]) == (False, 0, "bucket")


def test_a_missing_file_is_replaced_by_the_nearest_one_in_the_listing(cfg, fake_http):
    times = [TS - timedelta(minutes=4), TS + timedelta(minutes=1), TS + timedelta(minutes=6)]
    fake_http.fail("20261012-170600.grib2.gz")                      # not (yet) in the bucket, nor on the mirror
    _bucket(fake_http, "CONUS", times, [TS.replace(minute=0)])
    fake_http.add(BUCKET + "CONUS/SeamlessHSR_00.00/20261012/MRMS_SeamlessHSR_00.00_20261012-170700.grib2.gz",
                  grib2_around(LAT, LON, TS + timedelta(minutes=1)))
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert res["ok"] and res["ts"] == TS + timedelta(minutes=1) and res["url"].endswith("20261012-170700.grib2.gz")
    assert not res["older"] and res["behind_s"] == -60 and res["source"] == "bucket"


def test_the_nearest_file_can_be_in_the_hour_before(cfg, fake_http):
    frame = TS.replace(minute=1)                                    # 17:01, its file missing
    fake_http.fail("20261012-170100.grib2.gz")
    _bucket(fake_http, "CONUS", [frame - timedelta(minutes=3)], [frame.replace(minute=0) - timedelta(hours=1),
                                                                 frame.replace(minute=0)])
    fake_http.add(BUCKET + "CONUS/SeamlessHSR_00.00/20261012/MRMS_SeamlessHSR_00.00_20261012-165800.grib2.gz",
                  grib2_around(LAT, LON, frame - timedelta(minutes=3)))
    res = mrms.coverage(cfg, "CONUS", frame, [PT], 30.0)
    assert res["ok"] and res["url"].endswith("20261012-165800.grib2.gz")


def test_no_file_near_the_frame_is_an_error_not_a_guess(cfg, fake_http):
    cfg.radar_coverage_max_lag_s = 0                                # no older file either
    fake_http.fail("20261012-170600.grib2.gz")
    _bucket(fake_http, "CONUS", [TS + timedelta(minutes=8), TS - timedelta(minutes=20)], [TS.replace(minute=0)])
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert not res["ok"] and res["sites"] == {} and res["older"] is False and res["behind_s"] is None
    assert res["error"].startswith("no SeamlessHSR_00.00 file within 5 min of 17:06:00Z (")    # no older one
    assert "noaa-mrms-pds.s3.amazonaws.com" in res["error"] and "https://" not in res["error"]
    assert mrms.site_coverage(res, "pt") == {"ok": False, "error": res["error"], "ts": None}


def test_a_frame_of_unknown_time_takes_the_newest_recent_file(cfg, fake_http, monkeypatch):
    now = TS.replace(minute=9)                                      # 17:09: the last 10 min reach into hour 16
    monkeypatch.setattr(util, "utcnow", lambda: now)
    times = [now - timedelta(minutes=30)] + [now.replace(minute=m, second=59) for m in (0, 2, 4, 6)]
    _bucket(fake_http, "HAWAII", times, [now.replace(minute=0) - timedelta(hours=1), now.replace(minute=0)])
    fake_http.add("-170659.grib2.gz", _island_file(now.replace(minute=6, second=59)))
    res = mrms.coverage(cfg, "HAWAII", None, [MK], 30.0)
    assert res["ok"] and res["url"].endswith("-170659.grib2.gz") and res["sites"]["mk"]["covered_pct"] == 100.0
    assert not res["older"] and res["behind_s"] == 121                # from now: the frame's time is unknown


@pytest.mark.parametrize("max_lag", [0, 1800])
def test_a_frame_of_unknown_time_needs_a_file_of_the_last_10_min(cfg, fake_http, monkeypatch, max_lag):
    """Never an older file for a frame of unknown time, whatever VLBA_RADAR_COVERAGE_MAX_LAG says."""
    cfg.radar_coverage_max_lag_s = max_lag
    now = TS.replace(minute=9)
    monkeypatch.setattr(util, "utcnow", lambda: now)
    _bucket(fake_http, "HAWAII", [now - timedelta(minutes=11), now - timedelta(minutes=30)],
            [now.replace(minute=0) - timedelta(hours=1), now.replace(minute=0)])
    fake_http.add("-165800.grib2.gz", _island_file(now - timedelta(minutes=11)))
    res = mrms.coverage(cfg, "HAWAII", None, [MK], 30.0)
    assert not res["ok"] and res["error"] == "no MergedBaseReflectivityQC_00.50 file of the last 10 min"


# ---- the CONUS mirror (IEM's archive): NOAA's bucket late, 2026-11-10 --------------------------
def test_the_mirror_has_the_file_the_bucket_lacks(cfg, fake_http):
    fake_http.fail(BUCKET)                                          # not in the bucket (yet)
    fake_http.add(MIRROR + _mirror_name(TS), grib2_around(LAT, LON, TS, holes=WEST_HALF))
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert res["ok"] and res["source"] == "mirror" and not res["older"] and res["behind_s"] == 0
    assert res["url"] == "https://" + MIRROR + _mirror_name(TS) and res["ts"] == TS
    assert res["note"] == "bucket: fake: no route for noaa-mrms-pds.s3.amazonaws.com"
    assert res["sites"]["pt"]["covered_pct"] == pytest.approx(50.0, abs=1.5)
    assert fake_http.calls == [_key_url(cfg, "CONUS", TS), res["url"]]          # no listing asked for
    sc = mrms.site_coverage(res, "pt")
    assert sc["ok"] and sc["source"] == "mirror" and sc["older"] is False and sc["behind_s"] == 0


def test_the_mirrors_hour_listing_gives_the_nearest_file(cfg, fake_http):
    """2026-11-10 22:00Z: IEM's own feed was minutes late too; its file of the frame's time
    was missing, the one 2 min older was listed."""
    near = TS - timedelta(minutes=2)
    fake_http.fail(BUCKET)
    fake_http.fail(MIRROR + _mirror_name(TS))
    fake_http.add(MIRROR + _mirror_name(near), grib2_around(LAT, LON, near))
    page = _index_page(TS - timedelta(minutes=40), near, TS + timedelta(minutes=8))
    fake_http.add(MIRROR + "?P=SeamlessHSR_00.00_20261012-17*", page)
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert res["ok"] and res["source"] == "mirror" and not res["older"] and res["url"].endswith(_mirror_name(near))
    assert "https://" + MIRROR + "?P=SeamlessHSR_00.00_20261012-17*" in fake_http.calls      # one hour, not the day


def test_the_mirrors_listing_crosses_midnight(cfg, fake_http):
    frame = datetime(2026, 10, 13, 0, 2, tzinfo=UTC)
    before = datetime(2026, 10, 12, 23, 58, tzinfo=UTC)
    fake_http.fail(BUCKET)
    fake_http.fail("SeamlessHSR_00.00_20261013-000200")
    fake_http.add("mtarchive.geol.iastate.edu/2026/10/12/mrms/ncep/SeamlessHSR/" + _mirror_name(before),
                  grib2_around(LAT, LON, before))
    fake_http.add("2026/10/12/mrms/ncep/SeamlessHSR/?P=SeamlessHSR_00.00_20261012-23*", _index_page(before))
    fake_http.fail("2026/10/13/mrms/ncep/SeamlessHSR/?P=")          # the new day's directory: not there yet
    res = mrms.coverage(cfg, "CONUS", frame, [PT], 30.0)
    assert res["ok"] and res["url"].endswith(_mirror_name(before)) and res["behind_s"] == 240


def test_the_mirror_is_asked_when_the_bucket_is_skipped(cfg, fake_http, monkeypatch):
    """A host the circuit breaker marked down (HttpSkipped) ends nothing: the mirror is
    another host. The skipped one is asked nothing more, not even for its listing."""
    def rule(url):
        if "noaa-mrms-pds" in url:
            raise http.HttpSkipped("skipped: noaa-mrms-pds.s3.amazonaws.com unreachable earlier in this run")
    seen = _route(monkeypatch, fake_http, rule)
    near = TS - timedelta(minutes=2)
    fake_http.fail(MIRROR + _mirror_name(TS))                       # the mirror's file of the frame's time is missing too
    fake_http.add(MIRROR + _mirror_name(near), grib2_around(LAT, LON, near))
    fake_http.add(MIRROR + "?P=", _index_page(near))
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert res["ok"] and res["source"] == "mirror" and res["url"].endswith(_mirror_name(near))
    assert len([u for u, _ in seen if "noaa-mrms-pds" in u]) == 1


def test_the_mirror_reads_only_conus_and_none_switches_it_off(cfg, fake_http):
    assert mrms.mirror_pattern(cfg, "CONUS").startswith("https://mtarchive.geol.iastate.edu/%Y/%m/%d/")
    assert mrms.mirror_pattern(cfg, "HAWAII") == "" and mrms.mirror_pattern(cfg, "CARIB") == ""
    cfg.mrms_conus_mirror = ""
    fake_http.fail("SeamlessHSR")
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert not res["ok"] and not [u for u in fake_http.calls if "mtarchive" in u]
    assert "bucket:" not in res["error"]                            # one source: no names needed


def test_the_mirror_is_not_asked_when_the_runs_budget_is_low(cfg, fake_http, monkeypatch):
    monkeypatch.setattr(http, "budget_left", lambda: 30.0)
    fake_http.fail(BUCKET)
    fake_http.add(MIRROR + _mirror_name(TS), grib2_around(LAT, LON, TS))
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert not res["ok"] and "mirror: not asked, 30 s left of the run's budget" in res["error"]
    assert not [u for u in fake_http.calls if "mtarchive" in u]


# ---- what is read: MRMS's file of the product and of its time, or the next one ----------------
def test_a_file_of_another_product_is_refused_and_the_next_source_asked(cfg, fake_http):
    fake_http.add(BUCKET, grib2_around(LAT, LON, TS, parameter=ISLAND_PARAMETER))      # the islands' product
    fake_http.add(MIRROR + _mirror_name(TS), grib2_around(LAT, LON, TS))
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert res["ok"] and res["source"] == "mirror"
    assert "not MRMS's SeamlessHSR_00.00 (GRIB2 discipline 209, parameter 11.0)" in res["note"]
    fake_http.routes = []
    fake_http.add("SeamlessHSR", grib2_around(LAT, LON, TS, discipline=0))                # a WMO discipline
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert not res["ok"] and "not MRMS's SeamlessHSR_00.00 (GRIB2 discipline 0, parameter 8.8)" in res["error"]


def test_a_page_instead_of_a_file_falls_through_to_the_next_source(cfg, fake_http):
    fake_http.add(BUCKET, b"<html>Service temporarily unavailable</html>")     # a 200 that is no file
    fake_http.add(MIRROR + _mirror_name(TS), grib2_around(LAT, LON, TS))
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert res["ok"] and res["source"] == "mirror" and "not a GRIB2 message" in res["note"]


@pytest.mark.parametrize("skew, ok", [(timedelta(0), True), (timedelta(seconds=1), True),
                                      (timedelta(seconds=-1), True), (timedelta(seconds=2), False),
                                      (timedelta(minutes=-4), False), (timedelta(days=-1), False)])
def test_a_files_own_time_must_be_its_names(cfg, fake_http, skew, ok):
    """MRMS's GRIB2 time equals the name's to the second (bucket and IEM, 2026-11-10)."""
    cfg.mrms_conus_mirror = ""
    fake_http.add(BUCKET, grib2_around(LAT, LON, TS + skew))
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert res["ok"] is ok
    if not ok:
        assert "the file of 2026-10-12 17:06:00Z holds the data of %s" % (TS + skew).strftime(
            "%Y-%m-%d %H:%M:%SZ") in res["error"]


def test_a_file_without_its_own_time_is_refused(cfg, fake_http):
    raw = bytearray(grib2_message([[-99.0] * 80] * 80, LAT + 0.395, (LON - 0.395) % 360, 0.01, 0.01, TS, gz=False))
    raw[16 + 14] = 13                                               # section 1's month
    cfg.mrms_conus_mirror = ""
    fake_http.add(BUCKET, gzip.compress(bytes(raw)))
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert not res["ok"] and "the file's own time cannot be read" in res["error"]


# ---- what a lookup may cost --------------------------------------------------------------------
def test_requests_are_bounded(cfg, fake_http, monkeypatch):
    seen = _route(monkeypatch, fake_http, lambda url: None)
    fake_http.fail("20261012-170600.grib2.gz")
    _bucket(fake_http, "CONUS", [TS - timedelta(minutes=2)], [TS.replace(minute=0)])
    fake_http.add(BUCKET + "CONUS/SeamlessHSR_00.00/20261012/MRMS_SeamlessHSR_00.00_20261012-170400.grib2.gz",
                  grib2_around(LAT, LON, TS - timedelta(minutes=2)))
    assert mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)["ok"]
    assert len(seen) == 5 and all(k.get("retries") == 1 and 3.0 <= k.get("timeout") <= mrms.REQUEST_TIMEOUT_S
                                  for _, k in seen)


def test_tries_are_per_source(cfg, fake_http):
    """The bucket lists three near files that all download as a page instead of a file: the
    two it may try use up its own tries, not the mirror's, whose index lists a file 3 min
    from the frame (one budget for both would leave the mirror unasked)."""
    fake_http.fail("SeamlessHSR_00.00_20261012-170600")
    _bucket(fake_http, "CONUS", [TS - timedelta(minutes=1), TS + timedelta(minutes=1), TS - timedelta(minutes=2)],
            [TS.replace(minute=0)])
    fake_http.add(BUCKET + "CONUS/", b"<html>Service temporarily unavailable</html>")
    mirror_file = TS - timedelta(minutes=3)
    fake_http.add(MIRROR + _mirror_name(mirror_file), grib2_around(LAT, LON, mirror_file))
    fake_http.add(MIRROR + "?P=", _index_page(mirror_file))
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert res["ok"] and res["source"] == "mirror" and res["url"].endswith(_mirror_name(mirror_file))
    assert len([u for u in fake_http.calls if "noaa-mrms-pds" in u and u.endswith(".grib2.gz")]) == 3   # its own + 2 tries


def test_a_skipped_host_costs_no_try(cfg, fake_http, monkeypatch):
    """The first of the bucket's near files times out (sent: a try), the breaker refuses the
    next (not sent: no try, the bucket asked nothing more); the mirror's file is read."""
    state = {"down": False}

    def rule(url):
        if "noaa-mrms-pds" in url and "list-type" not in url and not url.endswith("-170600.grib2.gz"):
            if state["down"]:
                raise http.HttpSkipped("skipped: noaa-mrms-pds.s3.amazonaws.com unreachable earlier in this run")
            state["down"] = True
            raise http.HttpError("timed out")
    seen = _route(monkeypatch, fake_http, rule)
    fake_http.fail("SeamlessHSR_00.00_20261012-170600")
    _bucket(fake_http, "CONUS", [TS - timedelta(minutes=1), TS + timedelta(minutes=1), TS - timedelta(minutes=2)],
            [TS.replace(minute=0)])
    mirror_file = TS - timedelta(minutes=3)
    fake_http.add(MIRROR + _mirror_name(mirror_file), grib2_around(LAT, LON, mirror_file))
    fake_http.add(MIRROR + "?P=", _index_page(mirror_file))
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert res["ok"] and res["source"] == "mirror" and res["url"].endswith(_mirror_name(mirror_file))
    bucket_files = [u for u, _ in seen if "noaa-mrms-pds" in u and u.endswith(".grib2.gz")]
    assert len(bucket_files) == 3 and bucket_files[-1].endswith("-170700.grib2.gz")     # the third never asked


def test_no_request_starts_after_the_lookups_deadline(cfg, fake_http, monkeypatch):
    ticks = iter([0.0, 0.0, 0.0])
    monkeypatch.setattr(mrms, "_clock", lambda: next(ticks, 1000.0))    # the deadline has passed after two requests
    fake_http.fail("20261012-170600.grib2.gz")
    _bucket(fake_http, "CONUS", [TS - timedelta(minutes=2)], [TS.replace(minute=0)])
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert not res["ok"] and "the lookup's 60 s ran out" in res["error"]
    assert len(fake_http.calls) == 2                                # the two files of the frame's time only


def test_a_lookup_that_could_send_nothing_says_so(cfg, fake_http, monkeypatch):
    def rule(url):
        raise http.HttpSkipped("skipped: run budget exhausted")
    _route(monkeypatch, fake_http, rule)
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert not res["ok"] and res["error"] == "not looked up: skipped: run budget exhausted"


def test_a_retry_the_budget_refused_still_names_what_went_out(cfg, fake_http, monkeypatch):
    """weather.http refuses a retry once the run's budget is spent (HttpSkipped from the
    first attempt's failure): that attempt went out, and its timeout is the news."""
    def rule(url):
        try:
            raise OSError("timed out")
        except OSError as cause:
            raise http.HttpSkipped("skipped: run budget exhausted") from cause
    _route(monkeypatch, fake_http, rule)
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert not res["ok"] and not res["error"].startswith("not looked up")
    assert "bucket: timed out, then skipped: run budget exhausted" in res["error"]


# ---- an older file: within VLBA_RADAR_COVERAGE_MAX_LAG (an hour; 0 = never), and only when safe -
FRAME = datetime(2026, 10, 12, 17, 6, 56, tzinfo=UTC)
NEWEST = FRAME - timedelta(minutes=24, seconds=1)                  # the islands at 22:30Z on 2026-11-10
LATE = [NEWEST - timedelta(minutes=2 * k) for k in range(12)]       # 16:20:55 .. 16:42:55
HOURS = [FRAME.replace(minute=0, second=0) - timedelta(hours=1), FRAME.replace(minute=0, second=0)]


def _late_bucket(fake_http):
    fake_http.fail("20261012-170656.grib2.gz")
    _bucket(fake_http, "HAWAII", LATE, HOURS)
    fake_http.add(NEWEST.strftime("-%H%M%S.grib2.gz"), _island_file(NEWEST))


def test_an_older_file_stands_in_by_default_and_never_at_0(cfg, fake_http):
    _late_bucket(fake_http)
    cfg.radar_coverage_max_lag_s = 0                                # the strict rule: the frame's own file only
    res = mrms.coverage(cfg, "HAWAII", FRAME, [MK], 30.0)
    assert not res["ok"] and res["error"].startswith("no MergedBaseReflectivityQC_00.50 file within 5 min of 17:06:56Z (")
    assert "nor one" not in res["error"]
    fake_http.calls.clear()
    cfg.radar_coverage_max_lag_s = 3600                             # the default: up to an hour before the frame
    res = mrms.coverage(cfg, "HAWAII", FRAME, [MK], 30.0)
    assert res["ok"] and res["older"] and res["source"] == "bucket" and res["behind_s"] == 24 * 60 + 1
    assert res["ts"] == NEWEST and res["sites"]["mk"]["covered_pct"] == 100.0
    assert res["note"] == "fake: no route for noaa-mrms-pds.s3.amazonaws.com"
    sc = mrms.site_coverage(res, "mk")
    assert sc["ok"] and sc["older"] and sc["behind_s"] == 24 * 60 + 1
    # the frame's file, the frame's hour listed once, the hour before, the older file
    assert len(fake_http.calls) == 4 and len(set(fake_http.calls)) == 4


def test_an_older_file_must_lie_within_the_max_lag(cfg, fake_http):
    _late_bucket(fake_http)
    cfg.radar_coverage_max_lag_s = 1200                             # 24 min is too old
    res = mrms.coverage(cfg, "HAWAII", FRAME, [MK], 30.0)
    assert not res["ok"] and res["error"].startswith(
        "no MergedBaseReflectivityQC_00.50 file within 5 min of 17:06:56Z, nor one up to 20 min before it (")


def test_no_older_file_for_a_reused_frame(cfg, fake_http):
    _late_bucket(fake_http)
    cfg.radar_coverage_max_lag_s = 1800
    res = mrms.coverage(cfg, "HAWAII", FRAME, [MK], 30.0, allow_older=False)
    assert not res["ok"] and "nor one" not in res["error"]


def test_no_older_file_while_the_listing_near_the_frame_is_incomplete(cfg, fake_http):
    """A listing page that failed may have held the frame's file: the gap it leaves is no
    reason to read an older one."""
    cfg.radar_coverage_max_lag_s = 1800
    fake_http.fail("20261012-170656.grib2.gz")
    fake_http.fail("MRMS_MergedBaseReflectivityQC_00.50_20261012-17")      # the frame's hour cannot be listed
    _bucket(fake_http, "HAWAII", LATE, HOURS[:1])
    fake_http.add(NEWEST.strftime("-%H%M%S.grib2.gz"), _island_file(NEWEST))
    res = mrms.coverage(cfg, "HAWAII", FRAME, [MK], 30.0)
    assert not res["ok"] and "no older file stands in while the listing near that time is incomplete" in res["error"]


def test_no_older_file_instead_of_a_near_one_that_could_not_be_read(cfg, fake_http):
    cfg.radar_coverage_max_lag_s = 1800
    near = FRAME - timedelta(minutes=2)
    fake_http.fail("20261012-170656.grib2.gz")
    fake_http.fail(near.strftime("-%H%M%S.grib2.gz"))
    _bucket(fake_http, "HAWAII", LATE + [near], HOURS)
    fake_http.add(NEWEST.strftime("-%H%M%S.grib2.gz"), _island_file(NEWEST))
    res = mrms.coverage(cfg, "HAWAII", FRAME, [MK], 30.0)
    assert not res["ok"] and "no older file stands in for files near that time that could not be read" in res["error"]


def test_a_near_file_beats_an_older_one(cfg, fake_http):
    cfg.mrms_conus_mirror = ""
    cfg.radar_coverage_max_lag_s = 1800
    fake_http.fail("20261012-170600.grib2.gz")
    hours = [TS.replace(minute=0) - timedelta(hours=1), TS.replace(minute=0)]
    _bucket(fake_http, "CONUS", [TS - timedelta(minutes=m) for m in (26, 12, 8)] + [TS + timedelta(minutes=5)], hours)
    fake_http.add("20261012-171100.grib2.gz", grib2_around(LAT, LON, TS + timedelta(minutes=5)))
    fake_http.add("20261012-165800.grib2.gz", grib2_around(LAT, LON, TS - timedelta(minutes=8)))
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert res["ok"] and not res["older"] and res["url"].endswith("-171100.grib2.gz") and res["behind_s"] == -300


def _not_found(url):
    raise http.HttpError("HTTP 404 for %s" % url)


@pytest.mark.parametrize("now, older", [(datetime(2026, 10, 13, 0, 0, 30, tzinfo=UTC), True),
                                        (datetime(2026, 10, 13, 0, 20, tzinfo=UTC), False)])
def test_a_new_days_directory_not_made_yet_is_an_empty_listing(cfg, fake_http, monkeypatch, now, older):
    """The 00:00Z run, both sources late: IEM makes a day's directory with its first file, so
    its new day answers 404 for a few minutes. That is no gap in the listing (the older file
    stands in); a 404 for a period that began long ago is one (no older file)."""
    monkeypatch.setattr(util, "utcnow", lambda: now)
    frame = datetime(2026, 10, 12, 23, 58, tzinfo=UTC)
    old = frame - timedelta(minutes=24)
    _route(monkeypatch, fake_http, lambda url: _not_found(url) if "/2026/10/13/" in url else None)
    fake_http.fail("SeamlessHSR_00.00_20261012-235800")
    _bucket(fake_http, "CONUS", [old], [datetime(2026, 10, 12, h, tzinfo=UTC) for h in (22, 23)] +
            [datetime(2026, 10, 13, 0, tzinfo=UTC)])
    fake_http.add(BUCKET + "CONUS/SeamlessHSR_00.00/20261012/MRMS_SeamlessHSR_00.00_20261012-233400.grib2.gz",
                  grib2_around(LAT, LON, old))
    fake_http.add("2026/10/12/mrms/ncep/SeamlessHSR/?P=SeamlessHSR_00.00_20261012-2", _index_page(old))
    res = mrms.coverage(cfg, "CONUS", frame, [PT], 30.0)
    assert res["ok"] is older
    if older:
        assert res["older"] and res["behind_s"] == 24 * 60 and "2026/10/13" not in (res["note"] or "")
    else:
        assert "no older file stands in while the listing near that time is incomplete" in res["error"]


def test_mirror_names_are_read_strictly():
    rx = mirror_name_regex("SeamlessHSR_00.00_%Y%m%d-%H%M%S.grib2.gz")
    body = _index_page(TS) + "SeamlessHSR_00.00_20261012-170600.grib2.gz.idx x_SeamlessHSR_00.00_20261012-170800.grib2.gz"
    assert [m.group(0) for m in rx.finditer(body)] == [_mirror_name(TS)] * 2
    assert mirror_name_regex("a_%Y%m%d_%Y%H%M%S").match("a_20261012_2026170600")
    assert not mirror_name_regex("a_%Y%m%d_%Y%H%M%S").match("a_20261012_2025170600")
    for bad in ("a_%Y%m%d%H%M.gz", "a_%j%Y%m%d%H%M%S.gz", "a_%Y%m%d%H%M%S%%.gz", "a_%Y%m%d%H%M%S.gz%"):
        with pytest.raises(ValueError):
            mirror_name_regex(bad)
    hour = TS.replace(minute=0)
    assert mrms._mirror_pages("https://m.example/%Y/x/SeamlessHSR_00.00_%Y%m%d-%H%M%S.grib2.gz", TS, TS) == [
        (hour, "https://m.example/2026/x/?P=SeamlessHSR_00.00_20261012-17*")]
    assert mrms._mirror_pages("https://m.example/%Y%m%d%H/SeamlessHSR_00.00_%M%S-%Y%m%d%H.gz", TS, TS) == [
        (hour, "https://m.example/2026101217/")]                 # the hour comes after the minutes: no filter


def test_coverage_never_raises_on_bad_data(cfg, fake_http):
    fake_http.add("SeamlessHSR", b"\x1f\x8b not really gzip")
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert not res["ok"] and res["error"] and res["sites"] == {}
    fake_http.routes.insert(0, ("SeamlessHSR", gzip.compress(b"<Error><Code>AccessDenied</Code></Error>"), True))
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert not res["ok"] and "not a GRIB2 message" in res["error"]
    assert mrms.coverage(cfg, "MARS", TS, [PT], 30.0)["error"] == "no MRMS product for the domain 'MARS'"


def test_site_coverage_of_odd_results():
    assert mrms.site_coverage(None, "pt") == {"ok": False, "error": "radar coverage not read", "ts": None}
    ok = {"ok": True, "ts": TS, "sites": {"pt": {"cells": 0, "uncovered": 0, "covered_pct": None}}}
    assert mrms.site_coverage(ok, "pt")["error"] == "the ring holds no grid cell"
    assert mrms.site_coverage(ok, "kp")["error"] == "no coverage for kp"
