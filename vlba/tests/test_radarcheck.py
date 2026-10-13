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
