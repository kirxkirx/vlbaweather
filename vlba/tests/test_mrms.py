"""MRMS radar coverage (vlba.mrms): the GRIB2 reader takes what MRMS writes and refuses the
rest, the ring count separates "no coverage" (-999) from "no echo" (-99) with the ring
check's own distance, and the file of the frame's time is found, or the nearest one, or the
failure is reported, never raised."""
from __future__ import annotations

import gzip
from datetime import datetime, timedelta, timezone

import pytest

from weather import geo, util
from vlba import mrms
from vlba.tests.conftest import PT, grib2_around, grib2_message

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


def test_coverage_reads_the_file_of_the_frames_time(cfg, fake_http):
    fake_http.add("CONUS/SeamlessHSR_00.00/20261012/MRMS_SeamlessHSR_00.00_20261012-170600.grib2.gz",
                  grib2_around(LAT, LON, TS, holes=WEST_HALF))
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert res["ok"] and res["error"] is None and res["ts"] == TS
    assert res["url"] == _key_url(cfg, "CONUS", TS) and fake_http.calls == [res["url"]]
    assert set(res["sites"]) == {"pt"} and res["sites"]["pt"]["covered_pct"] == pytest.approx(50.0, abs=1.5)
    assert mrms.site_coverage(res, "pt")["ok"] and mrms.site_coverage(res, "pt")["ts"] == TS


def test_a_missing_file_is_replaced_by_the_nearest_one_in_the_listing(cfg, fake_http):
    times = [TS - timedelta(minutes=4), TS + timedelta(minutes=1), TS + timedelta(minutes=6)]
    fake_http.fail("20261012-170600.grib2.gz")                      # not (yet) in the bucket
    _bucket(fake_http, "CONUS", times, [TS.replace(minute=0)])
    fake_http.add("20261012-170700.grib2.gz", grib2_around(LAT, LON, TS + timedelta(minutes=1)))
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert res["ok"] and res["ts"] == TS + timedelta(minutes=1) and res["url"].endswith("20261012-170700.grib2.gz")


def test_the_nearest_file_can_be_in_the_hour_before(cfg, fake_http):
    frame = TS.replace(minute=1)                                    # 17:01, its file missing
    fake_http.fail("20261012-170100.grib2.gz")
    _bucket(fake_http, "CONUS", [frame - timedelta(minutes=3)], [frame.replace(minute=0) - timedelta(hours=1),
                                                                 frame.replace(minute=0)])
    fake_http.add("20261012-165800.grib2.gz", grib2_around(LAT, LON, frame - timedelta(minutes=3)))
    res = mrms.coverage(cfg, "CONUS", frame, [PT], 30.0)
    assert res["ok"] and res["url"].endswith("20261012-165800.grib2.gz")


def test_no_file_near_the_frame_is_an_error_not_a_guess(cfg, fake_http):
    fake_http.fail("20261012-170600.grib2.gz")
    _bucket(fake_http, "CONUS", [TS + timedelta(minutes=8)], [TS.replace(minute=0)])
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert not res["ok"] and res["sites"] == {} and "no SeamlessHSR_00.00 file within 5 min of 17:06:00Z" in res["error"]
    assert mrms.site_coverage(res, "pt") == {"ok": False, "error": res["error"], "ts": None}


def test_a_frame_of_unknown_time_takes_the_newest_recent_file(cfg, fake_http, monkeypatch):
    now = TS.replace(minute=9)                                      # 17:09: the last 10 min reach into hour 16
    monkeypatch.setattr(util, "utcnow", lambda: now)
    mk = {"slug": "mk", "lat": 19.8014, "lon": -155.4555}
    times = [now - timedelta(minutes=30)] + [now.replace(minute=m, second=59) for m in (0, 2, 4, 6)]
    _bucket(fake_http, "HAWAII", times, [now.replace(minute=0) - timedelta(hours=1), now.replace(minute=0)])
    fake_http.add("-170659.grib2.gz", grib2_around(mk["lat"], mk["lon"], now.replace(minute=6, second=59),
                                                   cell_deg=0.005))
    res = mrms.coverage(cfg, "HAWAII", None, [mk], 30.0)
    assert res["ok"] and res["url"].endswith("-170659.grib2.gz") and res["sites"]["mk"]["covered_pct"] == 100.0


def test_a_frame_of_unknown_time_needs_a_file_of_the_last_10_min(cfg, fake_http, monkeypatch):
    now = TS.replace(minute=9)
    monkeypatch.setattr(util, "utcnow", lambda: now)
    _bucket(fake_http, "HAWAII", [now - timedelta(minutes=11), now - timedelta(minutes=30)],
            [now.replace(minute=0) - timedelta(hours=1), now.replace(minute=0)])
    res = mrms.coverage(cfg, "HAWAII", None, [{"slug": "mk", "lat": 19.8014, "lon": -155.4555}], 30.0)
    assert not res["ok"] and res["error"] == "no MergedBaseReflectivityQC_00.50 file of the last 10 min"


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
