"""The VLBA map: an RGBA radar layer in frame space is composited as is, the rings and the
flashes land where they should, the overlay never breaks the map, and a map without an
overlay is the parent's."""
from __future__ import annotations

import io
import os
from datetime import datetime, timezone

import pytest
from PIL import Image

from weather import geo
from vlba import maps

UTC = timezone.utc
BASE = (40, 44, 52)


def _png(color=BASE):
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def tiles(fake_http):
    fake_http.add("openstreetmap", _png())
    return fake_http


def _mk(cfg):
    return cfg.sites[1]                 # Mauna Kea


def test_frame_space_layer_is_used_directly(cfg, cache, tiles):
    site = _mk(cfg)
    sm = maps.VlbaSiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    layer = Image.new("RGBA", sm.frame.size, (0, 0, 0, 0))
    cx, cy = sm.frame.lonlat_to_px(site["lon"], site["lat"])
    layer.paste((253, 0, 0, 255), (int(cx) - 30, int(cy) - 30, int(cx) - 10, int(cy) - 10))
    radar = {"ok": True, "stale": False, "error": None, "age_s": 0.0,
             "ts": datetime(2026, 10, 8, 15, 40, tzinfo=UTC), "label": "NWS Hawaii composite",
             "image": layer}
    out = os.path.join(cfg.out_dir, "mk.png")
    res = sm.render(radar, [], out)
    assert res["ok"] and res["radar_drawn"] and res["error"] is None
    px = Image.open(out).convert("RGB").getpixel((int(cx) - 20, int(cy) - 20))
    assert px[0] > 200 and px[1] < 80                 # the red block came through


def test_rings_and_flashes_are_drawn_and_counted(cfg, cache, tiles):
    site = _mk(cfg)
    inside = geo.dest_point(site["lat"], site["lon"], 20.0, 180.0)   # south: clear of the label
    outside = (site["lat"] + 3.0, site["lon"])          # far off the map
    overlay = {"radar_km": 30.0, "glm_km": 50.0, "flashes": [inside, outside, ("x", None)],
               "glm_label": "GLM flashes, last 5 min"}
    sm = maps.VlbaSiteMap(cfg, cache, site, "light", cfg.tile_url_light, overlay=overlay)
    out = os.path.join(cfg.out_dir, "rings.png")
    res = sm.render({"ok": False, "stale": True, "error": "no frame", "age_s": None, "ts": None,
                     "image": None, "not_applicable": True}, [], out)
    assert res["ok"] and sm.flashes_drawn == 1 and sm.overlay_note is None
    img = Image.open(out).convert("RGB")
    fx, fy = sm.frame.lonlat_to_px(inside[1], inside[0])
    assert img.getpixel((int(fx), int(fy)))[0] > 200 and img.getpixel((int(fx), int(fy)))[2] < 60
    # the radar ring: some pixel 30 km north of the site is ink-coloured (dark on light)
    rlat, rlon = geo.dest_point(site["lat"], site["lon"], 30.0, 0.0)
    rx, ry = sm.frame.lonlat_to_px(rlon, rlat)
    found = any(sum(img.getpixel((int(rx) + dx, int(ry) + dy))) < 200
                for dx in range(-2, 3) for dy in range(-2, 3))
    assert found


def test_overlay_failure_costs_only_the_overlay(cfg, cache, tiles, monkeypatch):
    site = _mk(cfg)
    sm = maps.VlbaSiteMap(cfg, cache, site, "dark", cfg.tile_url_dark, overlay={"radar_km": 30.0})
    monkeypatch.setattr(maps, "_circle_points", lambda *a, **k: 1 / 0)
    out = os.path.join(cfg.out_dir, "broken.png")
    res = sm.render({"ok": False, "stale": True, "error": "x", "age_s": None, "ts": None, "image": None}, [], out)
    assert res["ok"] and sm.overlay_note and "not drawn" in sm.overlay_note


def test_without_overlay_the_map_is_the_parents(cfg, cache, tiles):
    """Apart from its own scale bars (next test)."""
    from weather import radar

    class Base(radar.SiteMap):
        SCALE_BARS = maps.VlbaSiteMap.SCALE_BARS
    site = cfg.sites[0]
    base = Base(cfg, cache, site, "dark", cfg.tile_url_dark)
    ours = maps.VlbaSiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    no_radar = {"ok": False, "stale": True, "error": "x", "age_s": None, "ts": None, "image": None}
    p1, p2 = os.path.join(cfg.out_dir, "a.png"), os.path.join(cfg.out_dir, "b.png")
    assert base.render(no_radar, [], p1)["ok"] and ours.render(no_radar, [], p2)["ok"]
    assert open(p1, "rb").read() == open(p2, "rb").read()


def test_scale_bars_are_10_mi_and_10_km(cfg, cache, tiles, monkeypatch):
    """The parent's maps have 25 mi and 10 mi; the antennas' 10 mi and 10 km (the rings and
    the page's distances are in km). The bars are measured against the same map without
    them: from the left end, each runs as long as its distance at the map's scale."""
    from weather import radar
    drawn = []
    real_text = radar._draw_text

    def spy(d, xy, text, *a, **k):
        drawn.append(text)
        return real_text(d, xy, text, *a, **k)
    monkeypatch.setattr(radar, "_draw_text", spy)

    class NoBars(maps.VlbaSiteMap):
        SCALE_BARS = ()
    site = cfg.sites[0]
    no_radar = {"ok": False, "stale": True, "error": "x", "age_s": None, "ts": None, "image": None}
    images = []
    for cls in (maps.VlbaSiteMap, NoBars):
        sm = cls(cfg, cache, site, "light", cfg.tile_url_light)
        out = os.path.join(cfg.out_dir, cls.__name__ + ".png")
        assert sm.render(no_radar, [], out)["ok"]
        images.append(Image.open(out).convert("RGB"))
        if cls is maps.VlbaSiteMap:
            assert "10 mi" in drawn and "10 km" in drawn and "25 mi" not in drawn
    bars, plain = images
    w, h = bars.size
    runs = set()
    for y in range(h // 2, h):
        x = 12                              # the bars' left end (SiteMap._decorate)
        while x < w and bars.getpixel((x, y)) != plain.getpixel((x, y)):
            x += 1
        runs.add(x - 12)
    mi, km = sm.frame.px_for_km(10 * geo.MI_TO_KM), sm.frame.px_for_km(10.0)
    assert km > 10 and mi > km + 5          # far enough apart at the test map's scale
    assert max(runs) == pytest.approx(mi, abs=2)
    assert any(abs(r - km) <= 2 for r in runs)


def test_dashes_helper():
    pts = [(0.0, 0.0), (100.0, 0.0)]
    dashes = maps._dashes(pts, 10.0, 10.0)
    assert len(dashes) == 5 and dashes[0] == [(0.0, 0.0), (10.0, 0.0)]
    assert maps._dashes([(0.0, 0.0)], 5, 5) == []


def test_key_starts_below_the_parents_caption(cfg, cache, tiles, monkeypatch):
    """The parent's caption runs top-left as wide as the map (a long "radar unavailable"
    reason, the notes): the key goes below it, never over it."""
    site = _mk(cfg)
    starts = []
    real = maps.VlbaSiteMap._draw_key
    monkeypatch.setattr(maps.VlbaSiteMap, "_draw_key", lambda self, im, y0: (starts.append(y0), real(self, im, y0)))
    overlay = {"radar_km": 30.0, "glm_km": 50.0, "flashes": [], "glm_label": "GLM flashes, last 6 min"}
    one = {"ok": True, "stale": False, "error": None, "age_s": 0.0, "image": None,
           "ts": datetime(2026, 10, 8, 15, 40, tzinfo=UTC), "label": "NWS Hawaii composite"}
    long = {"ok": False, "stale": True, "error": "MRMS archive unreachable (no answer to HEAD for 16:24Z)" * 2,
            "age_s": None, "ts": None, "image": None}
    for radar_res, notes in ((one, ()), (long, ("2 alert areas not drawn", "a note"))):
        sm = maps.VlbaSiteMap(cfg, cache, site, "dark", cfg.tile_url_dark, overlay=overlay)
        assert sm.render(radar_res, [], os.path.join(cfg.out_dir, "k.png"), notes=notes)["ok"]
    assert len(starts) == 2 and 15 < starts[0] < starts[1]
    # three caption lines in the second map: the key starts below all of them
    assert starts[1] > starts[0] + 15
