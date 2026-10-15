"""The ring scans on synthetic rasters."""
from __future__ import annotations

from PIL import Image

from weather import geo
from vlba import radarcheck as rc

PT = (34.3010, -108.1192)


def _mrms(cells, index=124):
    img = Image.new("P", (geo.MRMS_W, geo.MRMS_H), geo.MRMS_NODATA)
    for lat, lon in cells:
        c, r = geo.mrms_cell(lat, lon)
        img.putpixel((c, r), index)
    return img


def test_mrms_ring_clear_and_hit():
    lat, lon = PT
    assert rc.mrms_ring(_mrms([]), lat, lon, 30, 20) == rc.CLEAR
    # 30 dBZ cell 10 km north: in a 30 km ring, out of a 5 km ring
    hit = rc.mrms_ring(_mrms([(lat + 0.09, lon)]), lat, lon, 30, 20)
    assert hit["in_ring"] and hit["count"] == 1 and 9.5 <= hit["nearest_km"] <= 10.5
    assert hit["max_dbz"] == 30.0
    assert rc.mrms_ring(_mrms([(lat + 0.09, lon)]), lat, lon, 5, 20) == rc.CLEAR
    # below the threshold: 15 dBZ (index 94)
    assert rc.mrms_ring(_mrms([(lat + 0.09, lon)], index=94), lat, lon, 30, 20) == rc.CLEAR
    # a cell just outside the ring, at 31 km east, is not counted
    far = geo.dest_point(lat, lon, 31.5, 90.0)
    assert rc.mrms_ring(_mrms([far]), lat, lon, 30, 20)["count"] == 0
    near = geo.dest_point(lat, lon, 28.5, 90.0)
    assert rc.mrms_ring(_mrms([near]), lat, lon, 30, 20)["count"] == 1


def test_mrms_ring_rejects_wrong_sizes_and_sites_off_grid():
    assert rc.mrms_ring(Image.new("P", (10, 10), 0), *PT, 30, 20) == rc.CLEAR
    assert rc.mrms_ring(None, *PT, 30, 20) == rc.CLEAR
    assert rc.mrms_ring(_mrms([]), 19.8, -155.45, 30, 20) == rc.CLEAR      # Hawaii: off the grid


def test_index_ring_on_a_frame():
    lat, lon = 19.8014, -155.4555
    frame = geo.MapFrame(lat, lon, 160.9, 240, 8)
    idx = Image.new("L", frame.size, geo.MRMS_NODATA)
    assert rc.index_ring(idx, frame, lat, lon, 30, 20) == rc.CLEAR
    # a 35 dBZ pixel 12 km north-east
    plat, plon = geo.dest_point(lat, lon, 12.0, 45.0)
    x, y = frame.lonlat_to_px(plon, plat)
    idx.putpixel((int(x), int(y)), rc.dbz_index(35.0))
    hit = rc.index_ring(idx, frame, lat, lon, 30, 20)
    assert hit["in_ring"] and hit["count"] == 1 and 10.5 <= hit["nearest_km"] <= 13.5
    assert hit["max_dbz"] == 35.0
    assert rc.index_ring(idx, frame, lat, lon, 30, 40) == rc.CLEAR       # threshold above it
    assert rc.index_ring(idx, frame, lat, lon, 8, 20) == rc.CLEAR        # ring too small
    assert rc.index_ring(Image.new("L", (3, 3), 0), frame, lat, lon, 30, 20) == rc.CLEAR


def test_dbz_index_round_trip():
    assert rc.dbz_index(None) == geo.MRMS_NODATA
    assert geo.mrms_dbz(rc.dbz_index(20.0)) == 20.0
    assert geo.mrms_dbz(rc.dbz_index(-40.0)) == -32.0
    assert rc.dbz_index(200.0) == 254 and geo.mrms_dbz(254) == 95.0


def test_pixels_reads_old_and_new_pillow_alike():
    """Pillow 12.1 deprecates Image.getdata (gone in Pillow 14) for get_flattened_data; the
    older Pillow of RHEL 9 or Gentoo has only getdata. Both give the same values."""
    im = Image.new("RGBA", (3, 2), (1, 2, 3, 4))
    im.putpixel((2, 1), (9, 9, 9, 255))
    want = [(1, 2, 3, 4)] * 5 + [(9, 9, 9, 255)]
    assert rc.pixels(im) == want

    class New:                      # a Pillow with get_flattened_data, and getdata gone
        def get_flattened_data(self):
            return tuple(want)
    assert rc.pixels(New()) == want

    class Old:                      # a Pillow before 12.1
        def getdata(self):
            return iter(want)
    assert rc.pixels(Old()) == want


def test_ring_digest_follows_the_data_in_the_ring_only():
    """``ring_digest`` changes with a pixel in the ring's box and not with one far outside it,
    for the MRMS composite and for an index image in a map frame; None for a raster that does
    not fit."""
    lat, lon = 34.3, -108.1
    img = Image.new("P", (geo.MRMS_W, geo.MRMS_H), geo.MRMS_NODATA)
    d0 = rc.ring_digest(img, lat, lon, 30.0)
    assert d0 and d0 == rc.ring_digest(img.copy(), lat, lon, 30.0)
    inside, outside = img.copy(), img.copy()
    inside.putpixel(geo.mrms_cell(lat + 0.1, lon), 124)
    outside.putpixel(geo.mrms_cell(lat + 2.0, lon), 124)
    assert rc.ring_digest(inside, lat, lon, 30.0) != d0
    assert rc.ring_digest(outside, lat, lon, 30.0) == d0
    assert rc.ring_digest(None, lat, lon, 30.0) is None
    assert rc.ring_digest(Image.new("P", (10, 10)), lat, lon, 30.0) is None
    mk_lat, mk_lon = 19.8014, -155.4555
    frame = geo.MapFrame(mk_lat, mk_lon, 160.934, 240, 8)
    idx = Image.new("L", frame.size, geo.MRMS_NODATA)
    e0 = rc.ring_digest(idx, mk_lat, mk_lon, 30.0, frame=frame)
    x, y = frame.lonlat_to_px(mk_lon, mk_lat + 0.1)
    near = idx.copy()
    near.putpixel((int(x), int(y)), 124)
    assert e0 and rc.ring_digest(near, mk_lat, mk_lon, 30.0, frame=frame) != e0
    assert rc.ring_digest(Image.new("L", (5, 5)), mk_lat, mk_lon, 30.0, frame=frame) is None


def test_ring_digest_covers_the_whole_ring_and_nothing_outside_it():
    """A cell just inside the ring's edge changes the digest at every bearing; a cell in the
    corner of the ring's bounding box, outside the ring, does not (the ring scan never sees it,
    so it must not make a repeat of the same scan look new)."""
    lat, lon, km = 34.3, -108.1, 30.0
    img = Image.new("P", (geo.MRMS_W, geo.MRMS_H), geo.MRMS_NODATA)
    d0 = rc.ring_digest(img, lat, lon, km)
    for bearing in (0, 45, 90, 135, 180, 225, 270, 315):
        edge = img.copy()
        edge.putpixel(geo.mrms_cell(*geo.dest_point(lat, lon, 28.5, bearing)), 124)
        assert rc.mrms_ring(edge, lat, lon, km, 20.0)["in_ring"], bearing
        assert rc.ring_digest(edge, lat, lon, km) != d0, bearing
    corner = img.copy()
    corner.putpixel(geo.mrms_cell(*geo.dest_point(lat, lon, 40.0, 45)), 124)
    assert not rc.mrms_ring(corner, lat, lon, km, 20.0)["in_ring"]
    assert rc.ring_digest(corner, lat, lon, km) == d0
    mk_lat, mk_lon = 19.8014, -155.4555
    frame = geo.MapFrame(mk_lat, mk_lon, 160.934, 240, 8)
    idx = Image.new("L", frame.size, geo.MRMS_NODATA)
    e0 = rc.ring_digest(idx, mk_lat, mk_lon, km, frame=frame)
    for bearing in (0, 90, 180, 270):
        edge = idx.copy()
        x, y = frame.lonlat_to_px(*reversed(geo.dest_point(mk_lat, mk_lon, 28.0, bearing)))
        edge.putpixel((int(x), int(y)), 124)
        assert rc.ring_digest(edge, mk_lat, mk_lon, km, frame=frame) != e0, bearing
    corner = idx.copy()
    x, y = frame.lonlat_to_px(*reversed(geo.dest_point(mk_lat, mk_lon, 41.0, 45)))
    corner.putpixel((int(x), int(y)), 124)
    assert rc.ring_digest(corner, mk_lat, mk_lon, km, frame=frame) == e0
