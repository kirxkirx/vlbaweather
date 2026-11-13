"""Fail-open paths that lay between the work packages of the audit fixes: the radar check of
an island site when its map frame or its colour check cannot be worked out."""
from __future__ import annotations

from datetime import timedelta

import pytest

import make_vlba_page as mvp
from weather import geo, roads
from vlba.tests.conftest import MK, PT
from vlba.tests.test_main import _island_setup, _mrms_result, _quiet_glm, _radar_state, add_routes, island_png


def _boom(*a, **k):
    raise RuntimeError("colour check failed")


def test_a_colour_check_that_fails_is_no_data(cfg, fake_http, monkeypatch):
    """The newest composite holds pixels of a colour the decoder does not know, and where they
    lie could not be worked out: the ring is not known to be clear (before, the failed check
    counted as "no such pixel in the ring" and the check read "no echo")."""
    _quiet_glm(monkeypatch)
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result([]))
    monkeypatch.setattr(mvp.radarcheck, "mask_ring", _boom)
    east = geo.dest_point(MK["lat"], MK["lon"], 10.0, 90.0)
    add_routes(fake_http, cfg, mk_blobs=[(east[0], east[1], (2, 253, 2))])
    run = mvp.run_once(cfg)
    mk = run["sites"][1]
    assert _radar_state(mk)["state"] == "unknown", _radar_state(mk)
    assert mk["verdict"]["incomplete"] and "Rain on radar" in mk["verdict"]["missing"]
    assert any("mk radar check" in e and "could not be placed" in e for e in run["errors"]), run["errors"]


def test_an_earlier_frame_whose_colour_check_fails_is_passed_over(cfg, fake_http, monkeypatch):
    """The composite frame before the newest has a colour the decoder does not know and no echo
    decoded in the ring. When where that colour lies cannot be worked out, the frame is no
    evidence of a clear ring: it is passed over for the one before it (before, it counted as a
    frame without the echo, so a real echo could not be confirmed)."""
    def unknown_colour(frame, echo):
        x = geo.dest_point(MK["lat"], MK["lon"], 6.0, 200.0)
        return island_png(frame, [(x[0], x[1], (2, 253, 2))])
    ts = _island_setup(cfg, fake_http, monkeypatch, unknown_colour)
    monkeypatch.setattr(mvp.radarcheck, "mask_ring", _boom)
    mk = mvp.run_once(cfg)["sites"][1]
    assert _radar_state(mk)["state"] == "bad", _radar_state(mk)
    assert [c["frame_ts"] for c in mk["radar_check"]["earlier"]] == [ts - timedelta(minutes=4)]


@pytest.mark.parametrize("coverage", [False, True])
def test_an_island_site_without_a_map_frame_is_not_read_on_the_mrms_grid(cfg, fake_http, monkeypatch, coverage):
    """Mauna Kea's map frame cannot be worked out. The site still lies outside the MRMS grid,
    whose scan finds no cell there and reads "no echo": before, the site was scanned on the
    CONUS frame and read clear (with VLBA_RADAR_COVERAGE=0). Its radar check is now no data;
    Pie Town, whose frame works, keeps the MRMS frame."""
    cfg.radar_coverage = coverage
    _quiet_glm(monkeypatch)
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result([]))
    add_routes(fake_http, cfg)
    real = geo.MapFrame

    class NoFrameAtMaunaKea(real):
        def __init__(self, lat, lon, *a, **k):
            if (lat, lon) == (MK["lat"], MK["lon"]):
                raise ValueError("no frame here")
            super().__init__(lat, lon, *a, **k)
    monkeypatch.setattr(geo, "MapFrame", NoFrameAtMaunaKea)
    run = mvp.run_once(cfg)
    pt, mk = run["sites"]
    assert _radar_state(mk)["state"] == "unknown", _radar_state(mk)
    assert mk["radar_check"]["source"] != "MRMS" and mk["verdict"]["incomplete"]
    assert pt["radar_check"]["source"] == "MRMS" and _radar_state(pt)["state"] == "ok"
    assert mvp._in_mrms_grid(cfg, MK) is False and mvp._in_mrms_grid(cfg, PT) is True


# ---- the road feed (parent library; vlbaweather has it off): a DTD in any encoding ----------
_DTD_BODY = ('<!DOCTYPE rss [<!ENTITY a "AAAA"><!ENTITY b "&a;&a;&a;">]><rss><channel><item><guid>g</guid>'
             '<title>&b;</title></item></channel></rss>')


@pytest.mark.parametrize("codec", ["utf-16", "utf-16-le", "utf-16-be"])
def test_a_doctype_in_utf16_is_refused(codec):
    """The byte search for "<!DOCTYPE" misses it in UTF-16, where ElementTree then expands the
    entities: the parser itself now refuses any document type declaration."""
    data = ('<?xml version="1.0" encoding="UTF-16"?>' + _DTD_BODY).encode(codec)
    with pytest.raises(ValueError, match="DOCTYPE"):
        roads.parse_rss(data)


def test_a_utf16_feed_without_a_doctype_still_parses():
    doc = ('<?xml version="1.0" encoding="UTF-16"?><rss><channel><lastBuildDate>Thu, 12 Nov 2026 10:00:00 -0700'
           '</lastBuildDate><item><guid>g1</guid><title>Closure - x</title></item></channel></rss>')
    items, built = roads.parse_rss(doc.encode("utf-16"))
    assert list(items) == ["g1"] and built is not None
