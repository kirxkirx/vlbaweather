"""The island radar: region choice, the WMS URL, the frame time from a capabilities
snippet, colour decoding on a synthetic composite, the drawn layer, the stale fallback and
the failure modes. Network blocked (fake_http)."""
from __future__ import annotations

import io
from datetime import datetime, timezone

from PIL import Image

from weather import geo, http
from vlba import nwsradar as nr, radarcheck

UTC = timezone.utc
MK = {"slug": "mk", "name": "Mauna Kea, HI", "lat": 19.8014, "lon": -155.4555, "tz": "Pacific/Honolulu"}
CAPS = ('<WMS_Capabilities><Layer><Name>hawaii_cref_qcd</Name><Dimension name="time" default="2026-10-08T15:30:00Z" '
        'units="ISO8601"/></Layer><Layer><Name>hawaii_bref_qcd</Name>'
        '<Dimension name="time" default="2026-10-08T15:40:54Z" units="ISO8601" nearestValue="1">'
        '2026-10-08T13:38:56.000Z,2026-10-08T15:40:54.000Z</Dimension></Layer></WMS_Capabilities>')


def _frame(cfg):
    return geo.MapFrame(MK["lat"], MK["lon"], cfg.map_km, cfg.map_px, cfg.tile_zoom)


def _png(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _composite(frame, blobs):
    """An RGBA map of the frame with coloured blobs: [(lat, lon, half_px, rgb), ...]."""
    img = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    for lat, lon, half, rgb in blobs:
        x, y = frame.lonlat_to_px(lon, lat)
        img.paste(tuple(rgb) + (255,), (int(x) - half, int(y) - half, int(x) + half + 1, int(y) + half + 1))
    return img


def test_region_for_and_urls(cfg):
    frame = _frame(cfg)
    assert nr.region_for(frame.bbox)["ws"] == "hawaii"
    sc = geo.MapFrame(17.7566, -64.5836, cfg.map_km, cfg.map_px, cfg.tile_zoom)
    assert nr.region_for(sc.bbox)["ws"] == "carib"
    pt = geo.MapFrame(34.3010, -108.1192, cfg.map_km, cfg.map_px, cfg.tile_zoom)
    assert nr.region_for(pt.bbox)["ws"] == "conus"
    assert nr.region_for(geo.MapFrame(-14.3, -170.7, cfg.map_km, cfg.map_px, cfg.tile_zoom).bbox) is None  # Pago Pago
    assert nr.region_for(None) is None and nr.region_for(("a", 1, 2, 3)) is None
    url = nr.getmap_url(cfg, nr.region_for(frame.bbox), frame)
    assert url.startswith("https://opengeo.ncep.noaa.gov/geoserver/hawaii/ows?")
    assert "LAYERS=hawaii%3Ahawaii_bref_qcd" in url and "CRS=EPSG%3A3857" in url
    assert "WIDTH=%d" % frame.width in url and "HEIGHT=%d" % frame.height in url and "TRANSPARENT=true" in url
    x0, y0 = nr.mercator(frame.west, frame.south)
    assert "BBOX=%.3f%%2C%.3f" % (x0, y0) in url
    assert nr.capabilities_url(cfg, nr.REGIONS[0]).endswith("/hawaii/ows?service=WMS&version=1.3.0&request=GetCapabilities")


def test_parse_frame_time_picks_the_layers_own_dimension():
    assert nr.parse_frame_time(CAPS, "hawaii_bref_qcd") == datetime(2026, 10, 8, 15, 40, 54, tzinfo=UTC)
    assert nr.parse_frame_time(CAPS, "hawaii_cref_qcd") == datetime(2026, 10, 8, 15, 30, tzinfo=UTC)
    assert nr.parse_frame_time(CAPS, "nope") is None and nr.parse_frame_time("<x/>", "hawaii_bref_qcd") is None
    assert nr.parse_frame_time(None, "hawaii_bref_qcd") is None


def test_decode_matches_colours_along_the_ramp():
    pal = ((10.0, (0, 0, 255)), (20.0, (0, 255, 0)), (30.0, (255, 0, 0)))
    img = Image.new("RGBA", (4, 1), (0, 0, 0, 0))
    img.putpixel((0, 0), (0, 0, 255, 255))          # exactly 10 dBZ
    img.putpixel((1, 0), (0, 128, 127, 255))        # half way 10 -> 20: 15 dBZ
    img.putpixel((2, 0), (255, 0, 0, 255))          # 30 dBZ
    img.putpixel((3, 0), (200, 200, 200, 255))      # not on the ramp
    idx, counts = nr.decode(img, palette=pal)
    vals = [geo.mrms_dbz(idx.getpixel((i, 0))) for i in range(4)]
    assert vals[0] == 10.0 and abs(vals[1] - 15.0) <= 0.5 and vals[2] == 30.0 and vals[3] is None
    assert counts == {"decoded": 3, "unknown": 1, "unknown_px": 1}
    # a nearly-transparent pixel is empty
    img.putpixel((0, 0), (0, 0, 255, 10))
    idx, _ = nr.decode(img, palette=pal)
    assert idx.getpixel((0, 0)) == geo.MRMS_NODATA


def test_real_palette_decodes_its_own_stops_and_measured_colours():
    img = Image.new("RGBA", (len(nr.PALETTE), 1), (0, 0, 0, 0))
    for i, (dbz, rgb) in enumerate(nr.PALETTE):
        img.putpixel((i, 0), tuple(rgb) + (255,))
    idx, counts = nr.decode(img)
    assert counts["unknown"] == 0
    for i, (dbz, rgb) in enumerate(nr.PALETTE):
        assert geo.mrms_dbz(idx.getpixel((i, 0))) == dbz
    assert [d for d, _ in nr.PALETTE] == sorted(d for d, _ in nr.PALETTE)
    # colours measured in live composites on 2026-10-08 against the MRMS source values
    measured = (((81, 134, 183), 12.5), ((48, 214, 91), 22.5), ((255, 89, 0), 47.5),
                ((75, 118, 173), 11.5), ((91, 165, 202), 14.5), ((89, 189, 189), 17.0),
                ((84, 206, 171), 19.0), ((68, 214, 134), 21.0), ((55, 214, 105), 22.0),
                ((21, 214, 34), 24.5), ((13, 198, 19), 26.0), ((13, 183, 18), 27.0),
                ((13, 175, 18), 27.5), ((216, 128, 128), 57.5), ((153, 148, 88), -19.5))
    img = Image.new("RGBA", (len(measured), 1), (0, 0, 0, 0))
    for i, (rgb, _) in enumerate(measured):
        img.putpixel((i, 0), tuple(rgb) + (255,))
    idx, counts = nr.decode(img)
    assert counts["unknown"] == 0
    for i, (rgb, dbz) in enumerate(measured):
        assert abs(geo.mrms_dbz(idx.getpixel((i, 0))) - dbz) <= 0.5, (rgb, dbz)


def test_fetch_decodes_the_map_and_caches_it(cfg, cache, fake_http):
    frame = _frame(cfg)
    north = geo.dest_point(MK["lat"], MK["lon"], 15.0, 0.0)
    comp = _composite(frame, [(north[0], north[1], 3, dict(nr.PALETTE)[35.0])])  # a 35 dBZ blob 15 km north
    fake_http.add("GetCapabilities", CAPS)
    fake_http.add("GetMap", _png(comp))
    res = nr.fetch(cfg, cache, MK, frame)
    assert res["ok"] and not res["stale"] and res["error"] is None
    assert res["ts"] == datetime(2026, 10, 8, 15, 40, 54, tzinfo=UTC)
    assert res["label"] == "NWS Hawaii composite" and res["source"] == "hawaii_bref_qcd" and res["region"] == "hawaii"
    assert res["index"].size == frame.size and res["image"].size == frame.size and res["image"].mode == "RGBA"
    x, y = frame.lonlat_to_px(north[1], north[0])
    assert geo.mrms_dbz(res["index"].getpixel((int(x), int(y)))) == 35.0
    px = res["image"].getpixel((int(x), int(y)))
    assert px[3] == cfg.radar_alpha and px[:3] == (253, 248, 2)                 # the parent's 35 dBZ colour
    assert res["image"].getpixel((0, 0)) == (0, 0, 0, 0)
    assert res["colors"]["decoded"] == 1 and res["colors"]["unknown"] == 0
    assert cache.get_bytes("nwsradar_mk", ".png") == _png(comp)
    # the server goes away: the cached map is served, stale
    fake_http.routes = []
    fake_http.fail("GetMap")
    fake_http.fail("GetCapabilities")
    res2 = nr.fetch(cfg, cache, MK, frame)
    assert res2["ok"] and res2["stale"] and "fake" in res2["error"] and res2["ts"] == res["ts"]
    assert geo.mrms_dbz(res2["index"].getpixel((int(x), int(y)))) == 35.0
    # ... but not for ever
    cfg.nwsradar_max_stale = 0
    cache.put("nwsradar_mk_meta", cache.get_any("nwsradar_mk_meta")[0], ts=1.0)
    res3 = nr.fetch(cfg, cache, MK, frame)
    assert not res3["ok"] and res3["image"] is None


def test_fetch_without_capabilities_still_maps(cfg, cache, fake_http):
    frame = _frame(cfg)
    fake_http.fail("GetCapabilities")
    fake_http.add("GetMap", _png(_composite(frame, [])))
    res = nr.fetch(cfg, cache, MK, frame)
    assert res["ok"] and res["ts"] is None and "capabilities" in res["error"]


def test_fetch_failure_modes(cfg, cache, fake_http):
    frame = _frame(cfg)
    fake_http.add("GetCapabilities", CAPS)
    fake_http.add("GetMap", "<ServiceExceptionReport>boom</ServiceExceptionReport>")
    res = nr.fetch(cfg, cache, MK, frame)
    assert not res["ok"] and "not an image" in res["error"]
    fake_http.routes = [r for r in fake_http.routes if "GetMap" not in r[0]]
    fake_http.add("GetMap", _png(Image.new("RGBA", (10, 10))))
    res = nr.fetch(cfg, cache, MK, frame)
    assert not res["ok"] and "unexpected map size" in res["error"]
    assert not nr.fetch(cfg, cache, MK, None)["ok"]
    pago = geo.MapFrame(-14.3, -170.7, cfg.map_km, cfg.map_px, cfg.tile_zoom)
    res = nr.fetch(cfg, cache, {"slug": "pp", "lat": -14.3, "lon": -170.7}, pago)
    assert not res["ok"] and "no NWS radar composite" in res["error"]
    assert fake_http.calls and not any("pp" in c for c in fake_http.calls)


def test_budget_exhaustion_is_an_ordinary_failure(cfg, cache, fake_http, monkeypatch):
    frame = _frame(cfg)
    http.begin_run(cfg)
    monkeypatch.setattr(http, "_clock", lambda: 10 ** 9)          # the budget is spent
    res = nr.fetch(cfg, cache, MK, frame)
    assert not res["ok"] and res["error"]


def test_unknown_colours_are_masked_and_counted_in_the_ring(cfg):
    """A pixel of a colour the decoder does not recognise may be an echo of any strength:
    it is kept apart in a mask, and the ring check can count it."""
    frame = _frame(cfg)
    near = geo.dest_point(MK["lat"], MK["lon"], 10.0, 90.0)       # 10 km east
    far = geo.dest_point(MK["lat"], MK["lon"], 60.0, 0.0)         # 60 km north
    comp = _composite(frame, [(near[0], near[1], 2, (2, 253, 2)), (far[0], far[1], 2, (2, 253, 2))])
    idx, counts, mask = nr.decode(comp, with_mask=True)
    assert counts["unknown"] == 1 and counts["unknown_px"] == 2 * 25
    x, y = frame.lonlat_to_px(near[1], near[0])
    assert idx.getpixel((int(x), int(y))) == geo.MRMS_NODATA and mask.getpixel((int(x), int(y))) == 255
    assert radarcheck.mask_ring(mask, frame, MK["lat"], MK["lon"], 30.0) == 25      # only the near blob
    assert radarcheck.mask_ring(None, frame, MK["lat"], MK["lon"], 30.0) == 0
    assert radarcheck.ring_inside(frame, MK["lat"], MK["lon"], 30.0)
    assert not radarcheck.ring_inside(frame, MK["lat"], MK["lon"], 90.0)


def test_stale_copy_only_for_the_same_map(cfg, cache, fake_http):
    """The cached composite is reused only for the request it was fetched for: one drawn for
    another map extent would put its echoes in the wrong places."""
    frame = _frame(cfg)
    fake_http.add("GetCapabilities", CAPS)
    fake_http.add("GetMap", _png(_composite(frame, [])))
    assert nr.fetch(cfg, cache, MK, frame)["ok"]
    fake_http.routes = []
    fake_http.fail("GetMap")
    fake_http.fail("GetCapabilities")
    assert nr.fetch(cfg, cache, MK, frame)["stale"]
    other = geo.MapFrame(MK["lat"] + 0.2, MK["lon"], cfg.map_km, cfg.map_px, cfg.tile_zoom)
    res = nr.fetch(cfg, cache, MK, other)
    assert not res["ok"] and res["index"] is None
    # and the URL is not pasted into the error the map caption shows
    assert "https://" not in (res["error"] or "") and "GetMap" in res["error"]
