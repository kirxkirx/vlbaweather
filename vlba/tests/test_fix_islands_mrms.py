"""Fixes of the island radar (vlba.nwsradar), of MRMS's coverage files (vlba.mrms) and of the
ring scans (vlba.radarcheck): what a hostile or broken upstream can no longer do, and the
edges of the ring scans. Network blocked (fake_http)."""
from __future__ import annotations

import gzip
import io
import math
import struct
import subprocess
import time
import tracemalloc
import zlib
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image, ImageFile

from weather import geo, http, util
from vlba import mrms, nwsradar as nr, radarcheck
from vlba.tests.conftest import PT, grib2_around, grib2_message
from vlba.tests.test_mrms import BUCKET, MIRROR, _bucket, _index_page, _island_file, _not_found, _route

MK = {"slug": "mk", "name": "Mauna Kea, HI", "lat": 19.8014, "lon": -155.4555, "tz": "Pacific/Honolulu"}
CAPS = ('<WMS_Capabilities><Layer><Name>hawaii_bref_qcd</Name>'
        '<Dimension name="time" default="2026-10-08T15:40:54Z" units="ISO8601" nearestValue="1">'
        '2026-10-08T15:38:54.000Z,2026-10-08T15:40:54.000Z</Dimension></Layer></WMS_Capabilities>')


def _frame(cfg):
    return geo.MapFrame(MK["lat"], MK["lon"], cfg.map_km, cfg.map_px, cfg.tile_zoom)


def _save(img, fmt):
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


EPS = (b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 240 240\n"
       b"(PS-RAN-ON-HOST) print flush\nshowpage\n%%EOF\n")


@pytest.fixture
def no_ghostscript(monkeypatch):
    """Records every attempt to start a process (Pillow starts Ghostscript for an EPS) and
    refuses it."""
    started = []

    def refuse(*a, **k):
        started.append(a[:1])
        raise OSError("no process may be started here")
    monkeypatch.setattr(subprocess, "Popen", refuse)
    return started


@pytest.fixture
def loads(monkeypatch):
    """Records every pixel decode (ImageFile.load) of a file Pillow opened."""
    seen = []
    real = ImageFile.ImageFile.load

    def load(self):
        seen.append(self.size)
        return real(self)
    monkeypatch.setattr(ImageFile.ImageFile, "load", load)
    return seen


# ---- S0, #45, #55: the composite is a PNG of the requested size, or nothing -----------------
def test_an_eps_reply_never_reaches_ghostscript(cfg, cache, fake_http, no_ghostscript):
    frame = _frame(cfg)
    fake_http.add("GetCapabilities", CAPS)
    fake_http.add("GetMap", EPS)
    res = nr.fetch(cfg, cache, MK, frame)
    assert not res["ok"] and res["error"] == "not an image (UnidentifiedImageError)"
    assert no_ghostscript == []
    newest = dict(res, ok=True, ts=nr.parse_frame_time(CAPS, "hawaii_bref_qcd"), stale=False)
    got = nr.fetch_at(cfg, frame, newest, nr.parse_frame_times(CAPS, "hawaii_bref_qcd")[0])
    assert not got["ok"] and got["error"] == "not an image (UnidentifiedImageError)" and no_ghostscript == []


def test_a_gif_of_the_right_size_is_refused(cfg, cache, fake_http):
    frame = _frame(cfg)
    gif = _save(Image.new("P", frame.size, 0), "GIF")
    fake_http.add("GetCapabilities", CAPS)
    fake_http.add("GetMap", gif)
    res = nr.fetch(cfg, cache, MK, frame)
    assert not res["ok"] and res["error"] == "not an image (UnidentifiedImageError)" and res["index"] is None


def test_an_oversized_png_is_refused_before_it_is_decoded(cfg, cache, fake_http, loads):
    frame = _frame(cfg)
    big = _save(Image.new("RGBA", (2000, 2000), (0, 0, 0, 0)), "PNG")      # a few KB on the wire
    fake_http.add("GetCapabilities", CAPS)
    fake_http.add("GetMap", big)
    res = nr.fetch(cfg, cache, MK, frame)
    assert not res["ok"] and res["error"] == "unexpected map size 2000x2000 (wanted %dx%d)" % frame.size
    assert (2000, 2000) not in loads


def test_a_page_instead_of_an_image_names_no_memory_address(cfg, cache, fake_http):
    frame = _frame(cfg)
    fake_http.add("GetCapabilities", CAPS)
    fake_http.add("GetMap", "<html>Service Unavailable</html>")
    res = nr.fetch(cfg, cache, MK, frame)
    assert not res["ok"] and res["error"] == "not an image (UnidentifiedImageError)"
    assert "0x" not in res["error"] and "BytesIO" not in res["error"]


def test_a_good_png_still_decodes(cfg, cache, fake_http):
    frame = _frame(cfg)
    fake_http.add("GetCapabilities", CAPS)
    fake_http.add("GetMap", _save(Image.new("RGBA", frame.size, (0, 0, 0, 0)), "PNG"))
    res = nr.fetch(cfg, cache, MK, frame)
    assert res["ok"] and res["error"] is None and res["index"].size == frame.size


# ---- vlba.mrms: what a GRIB2 file may make the reader do ------------------------------------
UTC = timezone.utc
TS = datetime(2026, 10, 12, 17, 6, tzinfo=UTC)
LAT, LON = PT["lat"], PT["lon"]
SEC3 = 16 + 21                  # where grib2_message puts section 3 (after sections 0 and 1)
SEC5 = SEC3 + 72 + 11           # and section 5 (after sections 3 and 4)


def _raw(**kw):
    return bytearray(grib2_message([[-99.0] * 4] * 4, LAT, LON % 360, 0.01, 0.01, TS, gz=False, **kw))


@pytest.mark.parametrize("cell_deg", [1e-6, 1e-5, 0.0009, 0.11, 1.0])
def test_a_grid_of_implausible_spacing_is_refused(cell_deg):
    raw = grib2_message([[-99.0] * 4] * 4, LAT, LON % 360, cell_deg, cell_deg, TS, gz=False)
    with pytest.raises(ValueError, match="GRIB2 grid of 4 x 4 cells"):
        mrms.parse_grib2(raw)


def test_the_grids_mrms_writes_are_read():
    for cell_deg in (0.01, 0.005, 0.001, 0.1):
        meta = mrms.parse_grib2(grib2_message([[-99.0] * 4] * 4, LAT, LON % 360, cell_deg, cell_deg, TS, gz=False))
        assert meta["grid"]["di"] == pytest.approx(cell_deg)
    raw = _raw()
    raw[SEC3 + 30:SEC3 + 38] = (7000).to_bytes(4, "big") + (3500).to_bytes(4, "big")    # CONUS: 24.5 million
    assert mrms.parse_grib2(bytes(raw))["grid"]["ni"] == 7000


def test_a_grid_of_too_many_cells_is_refused():
    raw = _raw()
    raw[SEC3 + 30:SEC3 + 38] = (8000).to_bytes(4, "big") + (4000).to_bytes(4, "big")    # 32 million
    with pytest.raises(ValueError, match="GRIB2 grid of 8000 x 4000 cells"):
        mrms.parse_grib2(bytes(raw))


def test_a_file_of_micro_degree_cells_costs_no_time(cfg, fake_http):
    cfg.mrms_conus_mirror = ""
    fake_http.add("SeamlessHSR", grib2_message([[-99.0] * 2] * 2, LAT, LON % 360, 1e-5, 1e-5, TS))
    t0 = time.monotonic()
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert not res["ok"] and "GRIB2 grid of 2 x 2 cells" in res["error"] and time.monotonic() - t0 < 5


def test_the_rings_box_is_capped():
    meta = {"grid": {"ni": 4, "nj": 4, "la1": LAT, "lo1": LON, "di": 0.001, "dj": 0.001, "scan": 0},
            "drs": {"R": -9990.0, "E": 0, "D": 1, "nbits": 16}}
    with pytest.raises(ValueError, match="the ring's box holds"):
        mrms.ring_stats(meta, Image.new("I;16", (4, 4)), LAT, LON, 120.0)


def test_the_largest_ring_the_settings_allow_is_read_on_the_islands_grid():
    """VLBA_RADAR_KM 300 on the islands' 0.005 deg grid: about 1.25 million cells in the box
    (0.97 million in the ring), under RING_BOX_CELLS_MAX."""
    meta = {"grid": {"ni": 4, "nj": 4, "la1": MK["lat"], "lo1": MK["lon"] % 360, "di": 0.005, "dj": 0.005,
                     "scan": 0}, "drs": {"R": -9990.0, "E": 0, "D": 1, "nbits": 16}}
    stats = mrms.ring_stats(meta, Image.new("I;16", (4, 4), 9000), MK["lat"], MK["lon"], 300.0)
    assert stats["cells"] > 900000 and stats["covered_pct"] == 0.0


def _zeros_gz(n_mb):
    z = zlib.compressobj(9, zlib.DEFLATED, 31)
    out = [z.compress(b"\0" * (1 << 20)) for _ in range(n_mb)]
    return b"".join(out) + z.flush()


def test_a_gzip_bomb_is_not_inflated(cfg, fake_http):
    cfg.mrms_conus_mirror = ""
    body = _zeros_gz(mrms.INFLATED_MAX // (1 << 20) + 8)
    assert len(body) < 100000
    fake_http.add("SeamlessHSR", body)
    tracemalloc.start()
    try:
        res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert not res["ok"] and "the file inflates to more than 32 MB" in res["error"]
    assert peak < mrms.INFLATED_MAX + (8 << 20)


def test_gzip_is_read_whole_or_not_at_all():
    good = grib2_around(LAT, LON, TS)
    assert mrms._read(good + b"\0\0\0", mrms.PRODUCTS["CONUS"], TS)[0]["ts"] == TS     # zero padding
    with pytest.raises(ValueError, match="cut short"):
        mrms._read(good[:-12], mrms.PRODUCTS["CONUS"], TS)
    with pytest.raises(ValueError, match="after the gzip stream"):
        mrms._read(good + good, mrms.PRODUCTS["CONUS"], TS)


def _png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_the_payload_is_checked_before_it_is_decoded(loads, no_ghostscript):
    grid = {"ni": 80, "nj": 80}
    with pytest.raises(ValueError, match="PNG of 2000 x 2000 for a grid of 80 x 80"):
        mrms.decode({"grid": grid, "payload": _png_bytes(Image.new("I;16", (2000, 2000)))})
    assert (2000, 2000) not in loads
    with pytest.raises(ValueError, match="PNG mode RGB"):
        mrms.decode({"grid": grid, "payload": _png_bytes(Image.new("RGB", (80, 80)))})
    assert (80, 80) not in loads
    for payload in (EPS, b"<html>x</html>"):
        with pytest.raises(ValueError) as e:
            mrms.decode({"grid": grid, "payload": payload})
        assert "0x" not in str(e.value) and "BytesIO" not in str(e.value)
    assert no_ghostscript == []


@pytest.mark.parametrize("ref", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_reference_value_is_refused(ref):
    raw = _raw()
    raw[SEC5 + 11:SEC5 + 15] = struct.pack(">f", ref)
    with pytest.raises(ValueError, match="reference value"):
        mrms.parse_grib2(bytes(raw))


def test_a_packing_without_values_is_refused():
    meta, im = _meta(grib2_around(LAT, LON, TS))
    for e, dd in ((-2000, 1), (0, -400)):
        with pytest.raises(ValueError, match="gives no values"):
            mrms.ring_stats(dict(meta, drs=dict(meta["drs"], E=e, D=dd)), im, LAT, LON, 30.0)


def _meta(raw):
    meta = mrms.parse_grib2(gzip.decompress(raw))
    return meta, mrms.decode(meta)


def test_only_plausible_values_count_as_covered():
    """-99 is covered without echo, -999 and anything outside -100 .. 100 dBZ is no coverage."""
    for value, covered in ((-99.0, 100.0), (-30.0, 100.0), (78.3, 100.0), (-999.0, 0.0), (-500.0, 0.0),
                           (150.0, 0.0)):
        meta, im = _meta(grib2_around(LAT, LON, TS, value=value))
        assert mrms.ring_stats(meta, im, LAT, LON, 30.0)["covered_pct"] == covered, value


def _ring_rows(lat, lon, km, n=80, cell=0.01):
    la1, lo1 = lat + n * cell / 2.0 - cell / 2.0, lon - n * cell / 2.0 + cell / 2.0
    ring = [(j, i) for j in range(n) for i in range(n)
            if geo.haversine_km(lat, lon, la1 - j * cell, lo1 + i * cell) <= km]
    return la1, lo1, ring


def test_coverage_is_rounded_down_not_to_the_nearest():
    """279 of Pie Town's 2784 ring cells uncovered is 89.978 %: below a 90 % limit, so it must
    not be stored as 90.0."""
    la1, lo1, ring = _ring_rows(LAT, LON, 30.0)
    holes = len(ring) // 10 + 1
    rows = [[-99.0] * 80 for _ in range(80)]
    for j, i in ring[:holes]:
        rows[j][i] = -999.0
    exact = 100.0 * (len(ring) - holes) / len(ring)
    assert 89.95 <= exact < 90.0
    meta, im = _meta(grib2_message(rows, la1, lo1 % 360, 0.01, 0.01, TS))
    stats = mrms.ring_stats(meta, im, LAT, LON, 30.0)
    assert stats["uncovered"] == holes and stats["covered_pct"] == math.floor(exact * 10) / 10 < 90.0


# ---- #88: the file's own echoes, for the radar check to compare with the raster --------------
def test_echo_cells_count_the_files_own_echoes_at_or_above_the_limit():
    la1, lo1, ring = _ring_rows(LAT, LON, 30.0)
    rows = [[-99.0] * 80 for _ in range(80)]
    for (j, i), v in zip(ring[:3], (20.0, 19.9, 45.5)):
        rows[j][i] = v
    meta, im = _meta(grib2_message(rows, la1, lo1 % 360, 0.01, 0.01, TS))
    assert mrms.ring_stats(meta, im, LAT, LON, 30.0, 20.0)["echo_cells"] == 2      # 20.0 counts, 19.9 not
    assert mrms.ring_stats(meta, im, LAT, LON, 30.0, 46.0)["echo_cells"] == 0
    assert "echo_cells" not in mrms.ring_stats(meta, im, LAT, LON, 30.0)


def test_coverage_reports_the_echo_cells_per_site(cfg, fake_http):
    cfg.mrms_conus_mirror = ""
    fake_http.add("SeamlessHSR", grib2_around(LAT, LON, TS, value=50.0))
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    s = res["sites"]["pt"]
    assert res["ok"] and s["covered_pct"] == 100.0 and s["echo_cells"] == s["cells"] > 2000
    assert mrms.site_coverage(res, "pt")["echo_cells"] == s["cells"]
    fake_http.routes = []
    fake_http.add("SeamlessHSR", grib2_around(LAT, LON, TS))
    assert mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)["sites"]["pt"]["echo_cells"] == 0
    cfg.radar_dbz = None
    assert mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)["sites"]["pt"]["echo_cells"] is None


# ---- #44, #40: the capabilities document, searched in linear time and read tolerantly ---------
LAYER = "hawaii_bref_qcd"
HEAD = "<WMS_Capabilities><Layer><Name>%s</Name>" % LAYER


def test_a_hostile_capabilities_document_costs_no_time():
    """Unclosed time dimensions over 184 KB took 5.9 s (quadratic); now linear."""
    doc = HEAD + '<Dimension name="time" ' * 8000 + "x"
    t0 = time.monotonic()
    assert nr.parse_frame_time(doc, LAYER) is None and nr.parse_frame_times(doc, LAYER) == []
    assert time.monotonic() - t0 < 1.0
    doc = HEAD + ('<Dimension' + ' a="' * 400 + '>') * 400 + "</Layer>"
    t0 = time.monotonic()
    assert nr.parse_frame_time(doc, LAYER) is None
    assert time.monotonic() - t0 < 1.0


def test_a_capabilities_document_too_large_is_not_searched(cfg, cache, fake_http):
    frame = _frame(cfg)
    fake_http.add("GetCapabilities", CAPS + " " * nr.CAPS_MAX_BYTES)
    fake_http.add("GetMap", _save(Image.new("RGBA", frame.size, (0, 0, 0, 0)), "PNG"))
    res = nr.fetch(cfg, cache, MK, frame)
    assert res["ts"] is None and res["times"] == [] and "at most %d are read" % nr.CAPS_MAX_BYTES in res["error"]


T0 = datetime(2026, 10, 8, 15, 40, 54, tzinfo=UTC)


@pytest.mark.parametrize("tag, want", [
    ('<Dimension name="time" default="2026-10-08T15:40:54Z" units="ISO8601"/>', T0),
    ('<Dimension units="ISO8601" name="time" default="2026-10-08T15:40:54Z"/>', T0),
    ('<Dimension default="2026-10-08T15:40:54Z" name="time"/>', T0),
    ("<Dimension name='time' default='2026-10-08T15:40:54Z'/>", T0),
    ('<dimension NAME="TIME" default = "2026-10-08T15:40:54.0Z" />', T0),
    ('<Dimension name="time" default="2026-10-08T15:40:54.5Z"/>', T0.replace(microsecond=500000)),
    ('<Dimension name="time" default="2026-10-08T15:40:54.123456789Z"/>', T0.replace(microsecond=123456)),
    ('<Dimension name="time" default="2026-10-08T15:40:54"/>', T0),
    ('<Dimension name="time" default="2026-10-08T05:40:54-10:00"/>', T0),
    ('<Dimension name="time" default="2026-10-08T15:40Z"/>', T0.replace(second=0)),
    ('<Dimension name="time" default="current">2026-10-08T15:38:54.000Z,2026-10-08T15:40:54.000Z</Dimension>', T0),
    ('<Dimension name="time" default="current"/>', None),
    ('<Dimension name="time" default="2026-13-08T15:40:54Z"/>', None),
    ('<Dimension name="elevation" default="2026-10-08T15:40:54Z"/>', None),
    ('<Dimension name="time" default="yesterday"/>', None),
])
def test_the_frame_time_is_read_tolerantly(tag, want):
    assert nr.parse_frame_time(HEAD + tag + "</Layer>", LAYER) == want


def test_listed_times_are_read_tolerantly():
    doc = (HEAD + "<Dimension units='ISO8601' name='time' default='2026-10-08T15:40:54Z'>"
           " 2026-10-08T15:36:54.0Z, 2026-10-08T15:38:54Z ,2026-10-08T15:40:54.000Z </Dimension></Layer>")
    assert nr.parse_frame_times(doc, LAYER) == [T0.replace(minute=36), T0.replace(minute=38), T0]


@pytest.mark.parametrize("ahead_s, known", [(0, True), (590, True), (610, False), (10 ** 9, False)])
def test_a_frame_time_ahead_of_the_clock_is_unknown(cfg, cache, fake_http, monkeypatch, ahead_s, known):
    monkeypatch.setattr(util, "utcnow", lambda: T0 - timedelta(seconds=ahead_s))
    frame = _frame(cfg)
    fake_http.add("GetCapabilities", CAPS)
    fake_http.add("GetMap", _save(Image.new("RGBA", frame.size, (0, 0, 0, 0)), "PNG"))
    res = nr.fetch(cfg, cache, MK, frame)
    assert res["ok"]                                     # the map is still drawn
    if known:
        assert res["ts"] == T0 and res["error"] is None and res["times"][-1] == T0
    else:
        assert res["ts"] is None and res["times"] == [] and "ahead of the clock" in res["error"]
        assert nr.earlier_times(res) == []


# ---- #38, #91: what the colour decoder cannot read is unknown, never empty --------------------
def test_an_image_of_too_many_colours_is_no_composite(cfg, cache, fake_http):
    """The default 600 px map has 360000 pixels, more than MAX_COLORS: an image of that many
    colours is refused, not decoded as all empty."""
    frame = geo.MapFrame(MK["lat"], MK["lon"], cfg.map_km, 600, cfg.tile_zoom)
    w, h = frame.size
    noise = Image.new("RGBA", (w, h))
    noise.putdata([(k & 255, (k >> 8) & 255, (k >> 16) & 255, 255) for k in range(w * h)])
    with pytest.raises(ValueError, match="not a radar composite"):
        nr.decode(noise)
    fake_http.add("GetCapabilities", CAPS)
    fake_http.add("GetMap", _save(noise, "PNG"))
    res = nr.fetch(cfg, cache, MK, frame)
    assert not res["ok"] and res["error"] == "not a radar composite (more than %d colours)" % nr.MAX_COLORS


@pytest.mark.parametrize("alpha, unknown", [(0, False), (1, True), (63, True), (64, False), (255, False)])
def test_a_faint_pixel_is_unknown_not_empty(cfg, alpha, unknown):
    frame = _frame(cfg)
    east = geo.dest_point(MK["lat"], MK["lon"], 10.0, 90.0)
    x, y = frame.lonlat_to_px(east[1], east[0])
    img = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    img.paste((255, 0, 0, alpha), (int(x) - 2, int(y) - 2, int(x) + 3, int(y) + 3))        # 50 dBZ red
    idx, counts, mask = nr.decode(img, with_mask=True)
    in_mask = radarcheck.mask_ring(mask, frame, MK["lat"], MK["lon"], 30.0)
    ring = radarcheck.index_ring(idx, frame, MK["lat"], MK["lon"], 30.0, 20.0)
    if unknown:
        assert counts["unknown_px"] == 25 and in_mask == 25 and not ring["in_ring"]
    elif alpha:
        assert counts["unknown_px"] == 0 and ring["in_ring"] and ring["max_dbz"] == 50.0
    else:
        assert counts == {"decoded": 0, "unknown": 0, "unknown_px": 0} and in_mask == 0 and not ring["in_ring"]


# ---- #46, #93, #94: only a complete listing lets an older file stand in ---------------------
FRAME = datetime(2026, 10, 12, 17, 6, 56, tzinfo=UTC)
OLDER = FRAME - timedelta(minutes=24, seconds=1)


def _island_bucket(fake_http, hour17):
    """The bucket lacks the frame's file; its hour 16 lists an older file, its hour 17 answers
    ``hour17``."""
    fake_http.fail("20261012-170656.grib2.gz")
    product = mrms.PRODUCTS["HAWAII"]
    fake_http.add("prefix=HAWAII/%s/20261012/MRMS_%s_20261012-17" % (product, product), hour17)
    _bucket(fake_http, "HAWAII", [OLDER], [OLDER.replace(minute=0, second=0)])
    fake_http.add(OLDER.strftime("-%H%M%S.grib2.gz"), _island_file(OLDER))


@pytest.mark.parametrize("hour17, why", [
    ("<html><body>Please log in</body></html>", "a listing page is no S3 listing"),
    ("", "a listing page is no S3 listing"),
    ("<ListBucketResult><IsTruncated>true</IsTruncated></ListBucketResult>", "a listing page is truncated"),
])
def test_a_listing_page_that_is_no_complete_listing_is_a_gap(cfg, fake_http, hour17, why):
    cfg.radar_coverage_max_lag_s = 3600
    _island_bucket(fake_http, hour17)
    res = mrms.coverage(cfg, "HAWAII", FRAME, [MK], 30.0)
    assert not res["ok"] and "no older file stands in while the listing near that time is incomplete" in res["error"]
    assert why in res["error"]


def test_a_complete_empty_listing_still_lets_an_older_file_stand_in(cfg, fake_http):
    cfg.radar_coverage_max_lag_s = 3600
    _island_bucket(fake_http, "<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>")
    res = mrms.coverage(cfg, "HAWAII", FRAME, [MK], 30.0)
    assert res["ok"] and res["older"] and res["ts"] == OLDER


@pytest.mark.parametrize("page, complete", [("", False), ("<html>Service Unavailable</html>", False),
                                            (None, True)])
def test_a_mirror_page_must_look_like_an_index(cfg, fake_http, page, complete):
    cfg.radar_coverage_max_lag_s = 3600
    old = TS - timedelta(minutes=24)
    fake_http.fail("SeamlessHSR_00.00_20261012-170600")
    _bucket(fake_http, "CONUS", [old], [TS.replace(minute=0) - timedelta(hours=1), TS.replace(minute=0)])
    fake_http.add(BUCKET + "CONUS/SeamlessHSR_00.00/20261012/MRMS_SeamlessHSR_00.00_20261012-164200.grib2.gz",
                  grib2_around(LAT, LON, old))
    fake_http.add(MIRROR + "?P=SeamlessHSR_00.00_20261012-17*", _index_page() if page is None else page)
    fake_http.add(MIRROR + "?P=SeamlessHSR_00.00_20261012-16*", _index_page(old))
    res = mrms.coverage(cfg, "CONUS", TS, [PT], 30.0)
    assert res["ok"] is complete
    if not complete:
        assert "mirror: a listing page is no directory index" in res["error"]


@pytest.mark.parametrize("now", [datetime(2026, 10, 12, 17, 10, tzinfo=UTC), datetime(2026, 10, 12, 17, 20, tzinfo=UTC)])
def test_a_404_of_the_mirrors_day_directory_is_a_gap_at_any_minute(cfg, fake_http, monkeypatch, now):
    """The day's directory of the mirror answers 404 all day (moved, a mistyped pattern): at
    17:10Z as at 17:20Z the listing is incomplete; only the first minutes of a day excuse it."""
    cfg.radar_coverage_max_lag_s = 3600
    monkeypatch.setattr(util, "utcnow", lambda: now)
    frame = now - timedelta(minutes=4)
    old = frame - timedelta(minutes=36)
    _route(monkeypatch, fake_http, lambda url: _not_found(url) if "mtarchive" in url else None)
    fake_http.fail(frame.strftime("%Y%m%d-%H%M%S.grib2.gz"))
    _bucket(fake_http, "CONUS", [old], [frame.replace(minute=0) - timedelta(hours=1), frame.replace(minute=0)])
    fake_http.add(old.strftime("%Y%m%d-%H%M%S.grib2.gz"), grib2_around(LAT, LON, old))
    res = mrms.coverage(cfg, "CONUS", frame, [PT], 30.0)
    assert not res["ok"] and "no older file stands in while the listing near that time is incomplete" in res["error"]


def test_the_directory_period_of_a_mirror_pattern():
    t = datetime(2026, 10, 12, 17, 0, tzinfo=UTC)
    day = "https://m.example/%Y/%m/%d/x/SeamlessHSR_00.00_%Y%m%d-%H%M%S.grib2.gz"
    assert mrms._dir_born(day, t) == datetime(2026, 10, 12, tzinfo=UTC)
    assert mrms._dir_born("https://m.example/%Y%m%d%H/a_%M%S-%Y%m%d%H.gz", t) == t
    assert mrms._dir_born("https://m.example/%Y/a_%Y%m%d%H%M%S.gz", t) == datetime(2026, 1, 1, tzinfo=UTC)
    assert mrms._dir_born("https://m.example/all/a_%Y%m%d%H%M%S.gz", t) is None
    assert not mrms._unborn(None, http.HttpError("HTTP 404 for x"))


def test_a_source_not_asked_leaves_no_failure_behind(cfg):
    """A request never sent (a source the run gave up on) must not inherit the previous
    request's 404, which could excuse its page as "not made yet"."""
    lk = mrms._Lookup(cfg, "CONUS", mrms.PRODUCTS["CONUS"])
    lk.failed = http.HttpError("HTTP 404 for the mirror's file")
    lk.down["bucket"] = "skipped: the host failed earlier in this run"
    assert lk.get("bucket", "https://b.example/x") is None and lk.failed is None


# ---- S22: the edges of the ring scans -------------------------------------------------------
RING_KM, LIMIT = 30.0, 20.0


def _island_px(frame, lat, lon, km, bearing):
    """The pixel of ``frame`` holding the point ``km`` from (lat, lon) on ``bearing``, and the
    great-circle distance of its centre (what the scans compare)."""
    plat, plon = geo.dest_point(lat, lon, km, bearing)
    x, y = frame.lonlat_to_px(plon, plat)
    i, j = int(x), int(y)
    clon, clat = frame.px_to_lonlat(i + 0.5, j + 0.5)
    return (i, j), geo.haversine_km(lat, lon, clat, clon)


@pytest.mark.parametrize("bearing", [90.0, 45.0, 0.0, 225.0])
def test_index_ring_and_mask_ring_end_at_the_ring(bearing):
    lat, lon = MK["lat"], MK["lon"]
    frame = geo.MapFrame(lat, lon, 160.9, 240, 8)                   # 0.67 km per pixel
    for km, inside in ((29.0, True), (31.5, False)):
        px, dist = _island_px(frame, lat, lon, km, bearing)
        assert (dist < RING_KM - 0.4) if inside else (dist > RING_KM + 0.9)
        idx = Image.new("L", frame.size, geo.MRMS_NODATA)
        idx.putpixel(px, radarcheck.dbz_index(35.0))
        hit = radarcheck.index_ring(idx, frame, lat, lon, RING_KM, LIMIT)
        assert hit["in_ring"] is inside and hit["count"] == int(inside), (km, bearing)
        mask = Image.new("L", frame.size, 0)
        mask.putpixel(px, 255)
        assert radarcheck.mask_ring(mask, frame, lat, lon, RING_KM) == int(inside), (km, bearing)


@pytest.mark.parametrize("bearing", [90.0, 45.0, 0.0, 225.0])
def test_mrms_ring_ends_at_the_ring(bearing):
    lat, lon = PT["lat"], PT["lon"]
    for km, inside in ((29.0, True), (31.5, False)):
        img = Image.new("P", (geo.MRMS_W, geo.MRMS_H), geo.MRMS_NODATA)
        img.putpixel(geo.mrms_cell(*geo.dest_point(lat, lon, km, bearing)), radarcheck.dbz_index(35.0))
        assert radarcheck.mrms_ring(img, lat, lon, RING_KM, LIMIT)["count"] == int(inside), (km, bearing)


def test_an_echo_of_exactly_the_limit_counts_in_both_scans():
    lat, lon = PT["lat"], PT["lon"]
    at = radarcheck.dbz_index(LIMIT)
    assert geo.mrms_dbz(at) == LIMIT and geo.mrms_dbz(at - 1) < LIMIT
    for v, counted in ((at, True), (at - 1, False)):
        img = Image.new("P", (geo.MRMS_W, geo.MRMS_H), geo.MRMS_NODATA)
        img.putpixel(geo.mrms_cell(*geo.dest_point(lat, lon, 10.0, 0.0)), v)
        hit = radarcheck.mrms_ring(img, lat, lon, RING_KM, LIMIT)
        assert hit["in_ring"] is counted and hit["max_dbz"] == (LIMIT if counted else None)
        frame = geo.MapFrame(MK["lat"], MK["lon"], 160.9, 240, 8)
        idx = Image.new("L", frame.size, geo.MRMS_NODATA)
        idx.putpixel(_island_px(frame, MK["lat"], MK["lon"], 10.0, 0.0)[0], v)
        hit = radarcheck.index_ring(idx, frame, MK["lat"], MK["lon"], RING_KM, LIMIT)
        assert hit["in_ring"] is counted and hit["max_dbz"] == (LIMIT if counted else None)
