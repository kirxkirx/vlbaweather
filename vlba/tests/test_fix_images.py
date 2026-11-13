"""Image bytes from a server reach only the codec the caller names, and only at the size it
expects (an EPS reply never starts Ghostscript, a PNG that declares a huge size is never
decoded); a frame that cannot be cached is still used; what leaves ``weather.radar`` and
``vlba.maps`` for the page carries no path, URL or address; a tile server that answers 429
or 5xx is left alone for a while."""
from __future__ import annotations

import errno
import io
import os
import struct
import subprocess
import time
import urllib.error
import zlib

import pytest
from PIL import EpsImagePlugin, Image, ImageFile

from weather import geo, http, radar
from vlba import maps

FRAME_SIZE = (geo.MRMS_W, geo.MRMS_H)
NO_RADAR = {"ok": False, "stale": True, "error": "x", "age_s": None, "ts": None, "image": None}
# what an OSError of the cache or the web root looks like: none of it may be published
SECRET_PATH = "/home/admin/.cache/vlbaweather/mrms_last.png.tmp.4242"


# ---- payloads ----------------------------------------------------------------------------
def _encoded(fmt, size, mode="P", color=0):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format=fmt)
    return buf.getvalue()


def _eps(size):
    """An EPS that Pillow identifies (and, left alone, would hand to Ghostscript on load)."""
    return ("%%!PS-Adobe-3.0 EPSF-3.0\n%%%%BoundingBox: 0 0 %d %d\n%%%%EndComments\n"
            "newpath 0 0 moveto 10 10 lineto stroke\nshowpage\n%%%%EOF\n" % size).encode("ascii")


def _chunk(tag, body):
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)


def _png_declaring(w, h):
    """A well-formed palette PNG of a few dozen bytes whose header declares ``w`` x ``h``."""
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 3, 0, 0, 0))
            + _chunk(b"PLTE", bytes(range(6))) + _chunk(b"IDAT", zlib.compress(b"\0" * 64))
            + _chunk(b"IEND", b""))


_made = {}


def _payload(kind, size):
    """``kind`` at ``size`` (cached: the frame-sized ones are tens of MB to build)."""
    if (kind, size) not in _made:
        _made[(kind, size)] = _eps(size) if kind == "EPS" else _encoded(kind, size)
    return _made[(kind, size)]


FOREIGN = ("EPS", "GIF", "BMP", "TIFF")
# 13000 x 13000 is below Pillow's own limit (it would be decoded: 169 MB for a palette
# image); 60000 x 60000 is above it
HUGE = ((13000, 13000), (60000, 60000))


@pytest.fixture
def tripwire(monkeypatch):
    """Fails the test, loudly, if Ghostscript (or any other process) is started, and records
    every image whose pixels Pillow decodes as ``(format, size)``."""
    started, decoded = [], []

    def forbidden(*a, **k):
        started.append(a[:1])
        raise AssertionError("a process (Ghostscript?) was about to be started")
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    for name in ("Ghostscript", "has_ghostscript"):
        monkeypatch.setattr(EpsImagePlugin, name, forbidden, raising=False)
    real_load = ImageFile.ImageFile.load

    def load(self, *a, **k):
        decoded.append((self.format, self.size))
        return real_load(self, *a, **k)
    monkeypatch.setattr(ImageFile.ImageFile, "load", load)
    yield decoded
    assert not started, "Ghostscript or another process was started while decoding an image"


def _sitemap(cfg, cache, theme="light", site=0):
    return radar.SiteMap(cfg, cache, cfg.sites[site], theme, cfg.tile_url_light)


def _cache_basemap(cache, sm, data):
    cache.put_bytes(sm.basemap_key, ".png", data)
    cache.put(sm.basemap_key + "__meta", {"ok": True})


# ---- S0: the reply does not choose the codec ---------------------------------------------------
@pytest.mark.parametrize("kind", FOREIGN)
def test_frame_decoder_refuses_other_formats(kind, tripwire):
    """Each of them at the frame's own size and (where the format has one) in palette mode:
    only the format is wrong."""
    with pytest.raises(radar.RadarError):
        radar._open_frame(_payload(kind, FRAME_SIZE))
    assert tripwire == []                       # refused on the first bytes: nothing decoded


@pytest.mark.parametrize("kind", FOREIGN)
def test_tile_decoder_refuses_other_formats(kind, tripwire):
    with pytest.raises(radar.RadarError):
        radar._decode_tile(_payload(kind, (256, 256)))
    assert tripwire == []


@pytest.mark.parametrize("kind", FOREIGN + ("JPEG",))
def test_cached_basemap_in_another_format_is_rebuilt(kind, cfg, cache, tripwire):
    sm = _sitemap(cfg, cache)
    mode = "RGB" if kind == "JPEG" else "P"
    data = _eps(sm.frame.size) if kind == "EPS" else _encoded(kind, sm.frame.size, mode)
    _cache_basemap(cache, sm, data)
    assert sm._basemap_from_cache(sm.basemap_key) is None
    assert tripwire == []


def test_jpeg_is_a_tile_format_but_no_frame(tripwire):
    with pytest.raises(radar.RadarError):
        radar._open_frame(_encoded("JPEG", FRAME_SIZE, "L"))
    assert tripwire == []
    assert radar._decode_tile(_encoded("JPEG", (256, 256), "RGB", (10, 20, 30))).mode == "RGB"


def test_non_image_bytes_are_refused():
    for data in (b"", b"<html>Service Unavailable</html>", b"\x89PNG\r\n\x1a\n" + b"junk" * 20, None):
        with pytest.raises(radar.RadarError):
            radar._open_frame(data)
        with pytest.raises(Exception):
            radar._decode_tile(data)


def test_eps_tile_from_the_server_costs_the_tile_only(cfg, cache, fake_http, tripwire):
    """End to end: the tile server answers every tile with an EPS. The map is still written
    (plain background, a note), nothing is cached, Ghostscript is not started."""
    fake_http.add("openstreetmap", _eps((256, 256)))
    sm = _sitemap(cfg, cache)
    res = sm.render(NO_RADAR, [], os.path.join(cfg.out_dir, "eps.png"))
    assert res["ok"] and "basemap incomplete (0/" in res["error"]
    assert fake_http.calls                      # the tiles were asked for
    assert not [f for f in os.listdir(cfg.cache_dir) if f.startswith(("tile_", "basemap_"))]
    assert all(fmt == "PNG" for fmt, _ in tripwire)     # nothing but our own output, if anything


def test_poisoned_tile_in_the_cache_is_fetched_again(cfg, cache, fake_http, tripwire):
    good = _encoded("PNG", (256, 256), "RGB", (1, 2, 3))
    fake_http.add("openstreetmap", good)
    sm = _sitemap(cfg, cache)
    url = sm.tile_url.format(z=8, x=1, y=1)
    assert sm._tile(url).getpixel((0, 0)) == (1, 2, 3)
    key = [f for f in os.listdir(cfg.cache_dir) if f.startswith("tile_") and f.endswith(".png")]
    assert len(key) == 1 and len(fake_http.calls) == 1
    assert sm._tile(url).getpixel((0, 0)) == (1, 2, 3) and len(fake_http.calls) == 1   # from the cache
    with open(os.path.join(cfg.cache_dir, key[0]), "wb") as f:
        f.write(_eps((256, 256)))
    assert sm._tile(url).getpixel((0, 0)) == (1, 2, 3) and len(fake_http.calls) == 2   # refetched


# ---- #155, #45: the declared size is checked before anything is decoded -----------------------
@pytest.mark.parametrize("size", HUGE)
def test_frame_declaring_a_huge_size_is_not_decoded(size, tripwire):
    with pytest.raises(radar.RadarError):
        radar._open_frame(_png_declaring(*size))
    assert tripwire == []


@pytest.mark.parametrize("size", HUGE + ((300, 300), (256, 512), (1024, 1024), (1, 1)))
def test_tile_of_another_size_is_not_decoded(size, tripwire):
    with pytest.raises(Exception):
        radar._decode_tile(_png_declaring(*size))
    assert tripwire == []


@pytest.mark.parametrize("size", HUGE)
def test_cached_basemap_declaring_a_huge_size_is_rebuilt(size, cfg, cache, tripwire):
    sm = _sitemap(cfg, cache)
    _cache_basemap(cache, sm, _png_declaring(*size))
    assert sm._basemap_from_cache(sm.basemap_key) is None
    assert tripwire == []


def test_frame_of_the_wrong_mode_is_not_decoded(tripwire):
    with pytest.raises(radar.RadarError) as e:
        radar._open_frame(_encoded("PNG", FRAME_SIZE, "RGB"))
    assert "mode" in str(e.value) and tripwire == []


# ---- healthy inputs still decode ------------------------------------------------------------
@pytest.mark.parametrize("mode", ("P", "L"))
def test_real_frame_still_decodes(mode):
    img = Image.new(mode, FRAME_SIZE, 0)
    if mode == "P":
        img.putpalette([v for i in range(256) for v in (i, i, i)])
    img.putpixel((3500, 1750), 124)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    out = radar._open_frame(buf.getvalue())
    assert out.mode == mode and out.size == FRAME_SIZE and out.getpixel((3500, 1750)) == 124


@pytest.mark.parametrize("fmt,mode,side", (("PNG", "RGB", 256), ("PNG", "P", 256), ("PNG", "RGBA", 256),
                                           ("PNG", "L", 256), ("PNG", "RGB", 512), ("JPEG", "RGB", 256),
                                           ("JPEG", "L", 512)))
def test_real_tiles_still_decode(fmt, mode, side):
    tile = radar._decode_tile(_encoded(fmt, (side, side), mode))
    assert tile.mode == "RGB" and tile.size == (side, side)


def test_basemap_is_built_cached_and_read_back(cfg, cache, fake_http):
    fake_http.add("openstreetmap", _encoded("PNG", (256, 256), "RGB", (200, 210, 220)))
    first = _sitemap(cfg, cache)
    built = first.basemap()
    asked = len(fake_http.calls)
    assert first.basemap_note is None and asked > 0
    again = _sitemap(cfg, cache)
    cached = again.basemap()
    assert len(fake_http.calls) == asked        # from the cache: no tile request
    assert cached.mode == "RGB" and cached.size == again.frame.size
    assert cached.tobytes() == built.tobytes()


def test_512_px_tiles_make_a_basemap(cfg, cache, fake_http):
    fake_http.add("openstreetmap", _encoded("PNG", (512, 512), "RGB", (200, 210, 220)))
    sm = _sitemap(cfg, cache)
    assert sm.basemap().getpixel((5, 5)) == (200, 210, 220) and sm.basemap_note is None


# ---- #23: a frame that cannot be cached is used ------------------------------------------------
def _frame_png():
    return _payload("PNG", FRAME_SIZE)


def _full_disk(*a, **k):
    raise OSError(errno.ENOSPC, "No space left on device", SECRET_PATH)


def test_frame_is_used_when_the_cache_cannot_be_written(cfg, cache, fake_http, monkeypatch):
    fake_http.add("mesonet.agron", _frame_png())
    monkeypatch.setattr(cache, "put_bytes", _full_disk)
    monkeypatch.setattr(cache, "put", _full_disk)
    res = radar.load_frame(cfg, cache)
    assert res["ok"] and not res["stale"] and res["error"] is None
    assert res["image"] is not None and res["image"].size == FRAME_SIZE and res["age_s"] == 0.0


def test_frame_is_used_when_only_its_metadata_cannot_be_written(cfg, cache, fake_http, monkeypatch):
    fake_http.add("mesonet.agron", _frame_png())
    monkeypatch.setattr(cache, "put", _full_disk)
    res = radar.load_frame(cfg, cache)
    assert res["ok"] and not res["stale"] and res["error"] is None and res["image"] is not None


def test_cached_frame_is_reused_for_the_same_url(cfg, cache, fake_http, monkeypatch):
    """The healthy path is what it was: one GET, then the cached bytes."""
    now = radar.util.utcnow()
    monkeypatch.setattr(radar.util, "utcnow", lambda: now)     # the same frame both times
    fake_http.add("mesonet.agron", _frame_png())
    first = radar.load_frame(cfg, cache)
    assert first["ok"] and first["image"].size == FRAME_SIZE
    gets = [c for c in fake_http.calls if not c.startswith("HEAD")]
    assert len(gets) == 1
    res = radar.load_frame(cfg, cache)
    assert res["ok"] and not res["stale"] and res["url"] == first["url"]
    assert [c for c in fake_http.calls if not c.startswith("HEAD")] == gets


# ---- #48, #55: the frame's error text ----------------------------------------------------------
def _clean(text):
    assert text
    for bad in ("/home", "/tmp", SECRET_PATH, "admin", "0x", "BytesIO", "://", "[Errno", str(os.getpid())):
        assert bad not in text, text


def test_unexpected_oserror_is_published_by_errno_name_only(cfg, cache, monkeypatch):
    def boom(cfg, now=None):
        raise PermissionError(errno.EACCES, "Permission denied", SECRET_PATH)
    monkeypatch.setattr(radar, "_probe_frames", boom)
    res = radar.load_frame(cfg, cache)
    assert not res["ok"] and res["error"] == "MRMS frame failed (EACCES)"
    _clean(res["error"])


def test_unexpected_exception_is_published_by_class_only(cfg, cache, monkeypatch):
    def boom(cfg, now=None):
        raise RuntimeError("state at 0x7f00deadbeef in " + SECRET_PATH)
    monkeypatch.setattr(radar, "_probe_frames", boom)
    res = radar.load_frame(cfg, cache)
    assert res["error"] == "MRMS frame failed (RuntimeError)"


def test_http_error_text_does_not_carry_the_archive_url(cfg, cache, fake_http, monkeypatch):
    fake_http.add("mesonet.agron", b"x")        # the HEAD probe finds a frame

    def refuse(url, cfg, **k):
        raise http.HttpError("HTTP 500 for %s" % url)
    monkeypatch.setattr(http, "get_bytes", refuse)
    res = radar.load_frame(cfg, cache)
    assert not res["ok"] and res["error"] == "HTTP 500 for the MRMS archive"
    _clean(res["error"])


@pytest.mark.parametrize("body", (b"<html>Service Unavailable</html>",
                                  b"\x89PNG\r\n\x1a\n" + b"not really a PNG" * 8))
def test_undecodable_frame_publishes_no_memory_address(body, cfg, cache, fake_http):
    fake_http.add("mesonet.agron", body)
    res = radar.load_frame(cfg, cache)
    assert not res["ok"]
    _clean(res["error"])
    with pytest.raises(radar.RadarError) as e:
        radar._open_frame(body)
    _clean(str(e.value))


def test_truncated_frame_is_refused_without_pillows_text(cfg, cache, fake_http):
    data = _frame_png()
    with pytest.raises(radar.RadarError) as e:
        radar._open_frame(data[:len(data) // 2])
    _clean(str(e.value))


@pytest.mark.parametrize("pattern", [
    # a lone surrogate: strftime raised for it up to Python 3.11, and lets it through from 3.12
    "https://example.org/%Y/\ud800.png?key=s3cret",
    # a URL is ASCII: no request could carry this one, on any Python
    "https://example.org/%Y/café_%Y%m%d-%H%M00.png?key=s3cret"])
def test_bad_archive_pattern_is_named_by_class(cfg, fake_http, pattern):
    """The pattern fails before any request, and the reason names no URL (nor its key)."""
    cfg.mrms_archive = pattern
    found, why = radar._probe_frames(cfg)
    assert found is None and why == "bad MRMS archive pattern (UnicodeEncodeError)"
    assert fake_http.calls == []
    _clean(why)


# ---- #50: the map's own errors -----------------------------------------------------------------
def test_write_failure_names_the_errno_not_the_paths(cfg, cache, fake_http):
    fake_http.add("openstreetmap", _encoded("PNG", (256, 256), "RGB"))
    sm = _sitemap(cfg, cache)
    out = os.path.join(cfg.out_dir, "radar_pt_light.png")
    os.makedirs(out)                            # a directory sits where the map belongs
    res = sm.render(NO_RADAR, [], out)
    assert not res["ok"] and res["error"].startswith("write failed (EISDIR)")
    assert cfg.out_dir not in res["error"] and str(os.getpid()) not in res["error"]
    assert os.listdir(cfg.out_dir) == ["radar_pt_light.png"]      # no temporary file left


def test_radar_layer_failure_is_named_by_class(cfg, cache, fake_http, monkeypatch):
    fake_http.add("openstreetmap", _encoded("PNG", (256, 256), "RGB"))
    sm = _sitemap(cfg, cache)
    captions = []
    real_fit = radar._fit_text

    def spy(d, text, font, max_w):
        captions.append(text)
        return real_fit(d, text, font, max_w)
    monkeypatch.setattr(radar, "_fit_text", spy)

    def boom(img):
        raise OSError(errno.EIO, "Input/output error", SECRET_PATH)
    monkeypatch.setattr(sm, "_radar_layer", boom)
    frame = {"ok": True, "stale": False, "error": None, "age_s": 0.0, "ts": None, "image": object()}
    res = sm.render(frame, [], os.path.join(cfg.out_dir, "m.png"))
    assert res["ok"] and res["error"] == "radar unavailable: radar layer failed (EIO)"
    assert any("radar layer failed (EIO)" in c for c in captions)
    assert not any("admin" in c for c in captions)
    # a refusal this module words itself is published as it is
    wrong = dict(frame, image=Image.new("RGB", (10, 10)))
    monkeypatch.undo()
    res = sm.render(wrong, [], os.path.join(cfg.out_dir, "m.png"))
    assert res["ok"] and "unusable radar image mode 'RGB'" in res["error"]


def test_road_layer_failure_is_named_by_class(cfg, cache, fake_http, monkeypatch):
    fake_http.add("openstreetmap", _encoded("PNG", (256, 256), "RGB"))
    sm = _sitemap(cfg, cache)

    def boom(im, roads):
        raise ValueError("bad geometry in " + SECRET_PATH)
    monkeypatch.setattr(sm, "_draw_roads", boom)
    res = sm.render(NO_RADAR, [], os.path.join(cfg.out_dir, "m.png"), roads={"events": [{"kind": "closure"}]})
    assert res["ok"] and "road layer failed (ValueError)" in res["error"]
    # weather.pipeline recognises the part by its start
    assert any(p.startswith("road layer failed") for p in res["error"].split("; "))
    assert "admin" not in res["error"]


def test_overlay_note_is_named_by_class(cfg, cache, fake_http, monkeypatch):
    fake_http.add("openstreetmap", _encoded("PNG", (256, 256), "RGB"))
    sm = maps.VlbaSiteMap(cfg, cache, cfg.sites[1], "dark", cfg.tile_url_dark, overlay={"radar_km": 30.0})

    def boom(*a, **k):
        raise OSError(errno.ENOMEM, "Cannot allocate memory", SECRET_PATH)
    monkeypatch.setattr(maps, "_circle_points", boom)
    res = sm.render(NO_RADAR, [], os.path.join(cfg.out_dir, "o.png"))
    assert res["ok"] and sm.overlay_note == "rings and flashes not drawn (ENOMEM)"


# ---- #154: 429 and 5xx stop the build and start a back-off ------------------------------------
def _tile_server(monkeypatch, make_error):
    """Every tile request fails with ``make_error(url)``; returns the list of requests."""
    asked = []

    def fetch(url, cfg, **k):
        asked.append(url)
        raise make_error(url)
    monkeypatch.setattr(http, "fetch_cacheable", fetch)
    return asked


def _with_status(status, text="request failed"):
    def make(url):
        e = http.HttpError(text)
        e.status = status
        return e
    return make


def _mark(cache):
    return cache.get_any(radar.REFUSED_KEY)[0]


@pytest.mark.parametrize("make,pause", (
    # http's text after its retries of a 429 or 5xx, while HttpError has no status
    (lambda url: http.HttpError("%s: HTTP Error 429: Too Many Requests" % url), radar.TILE_REFUSED_BACKOFF),
    (lambda url: http.HttpError("%s: HTTP Error 503: Service Unavailable" % url), radar.TILE_ERROR_BACKOFF),
    (lambda url: http.HttpError("HTTP 403 for %s" % url), radar.TILE_REFUSED_BACKOFF),
    # the attribute, whatever the text says
    (_with_status(429), radar.TILE_REFUSED_BACKOFF),
    (_with_status(418), radar.TILE_REFUSED_BACKOFF),
    (_with_status(500), radar.TILE_ERROR_BACKOFF),
    (_with_status(502, "HTTP 404 for a tile"), radar.TILE_ERROR_BACKOFF),
    # the attribute is there but empty: the text decides
    (_with_status(None, "tile: HTTP Error 504: Gateway Timeout"), radar.TILE_ERROR_BACKOFF),
))
def test_refusing_tile_server_is_asked_once(make, pause, cfg, cache, monkeypatch):
    asked = _tile_server(monkeypatch, make)
    sm = _sitemap(cfg, cache)
    res = sm.render(NO_RADAR, [], os.path.join(cfg.out_dir, "a.png"))
    assert res["ok"] and len(asked) == 1        # the build stopped at the first tile
    assert sm.basemap_note == "basemap unavailable: the tile server refused the requests"
    mark = _mark(cache)
    assert mark["backoff_s"] == pause and "://" not in mark["reason"]
    # every other map of this run and of the next ones asks nothing
    for theme, site in (("dark", 0), ("light", 1)):
        other = _sitemap(cfg, cache, theme, site)
        assert other.render(NO_RADAR, [], os.path.join(cfg.out_dir, "b.png"))["ok"]
        assert other.basemap_note == sm.basemap_note
    assert len(asked) == 1


@pytest.mark.parametrize("make", (
    lambda url: http.HttpError("HTTP 404 for %s" % url),
    lambda url: http.HttpError("%s: timed out" % url),
    lambda url: http.HttpSkipped("skipped: run budget exhausted"),
    _with_status(404, "HTTP Error 503 said the text"),
    _with_status(400),
))
def test_other_failures_start_no_back_off(make, cfg, cache, monkeypatch):
    asked = _tile_server(monkeypatch, make)
    sm = _sitemap(cfg, cache)
    assert sm.render(NO_RADAR, [], os.path.join(cfg.out_dir, "a.png"))["ok"]
    tx0, ty0, tx1, ty1 = sm.frame.tile_range()
    assert len(asked) == (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
    assert sm.basemap_note.startswith("basemap incomplete (0/") and _mark(cache) is None


def test_tile_numbers_in_the_url_are_no_status():
    url = "https://tiles.example.org/HTTP 503/8/500/429.png"
    assert radar._tile_refusal(http.HttpError("HTTP 404 for %s" % url), url) is None
    assert radar._tile_refusal(http.HttpError("%s: timed out" % url), url) is None
    assert radar._tile_refusal(http.HttpError("HTTP 429 for %s" % url), url) == \
        ("HTTP 429", radar.TILE_REFUSED_BACKOFF)
    e = http.HttpError("x")
    e.status = True                             # not a status
    assert radar._tile_refusal(e, url) is None


@pytest.mark.parametrize("code,want", ((429, radar.TILE_REFUSED_BACKOFF), (503, radar.TILE_ERROR_BACKOFF),
                                       (404, None)))
def test_status_of_the_urllib_error_behind_the_http_error(code, want):
    """http raises its HttpError ``from`` urllib's HTTPError: its code counts whatever the
    text says."""
    url = "https://tiles.example.org/8/1/2.png"
    cause = urllib.error.HTTPError(url, code, "x", {}, None)
    try:
        raise http.HttpError("request failed") from cause
    except http.HttpError as e:
        got = radar._tile_refusal(e, url)
    assert (got[1] if got else None) == want


def test_back_off_ends_after_its_own_time(cfg, cache, monkeypatch):
    """One hour after a 5xx, six after a refusal; a marker of an older version (no
    ``backoff_s``) or with a nonsense value holds for the six hours."""
    asked = _tile_server(monkeypatch, _with_status(503))
    sm = _sitemap(cfg, cache)
    sm.basemap()
    assert len(asked) == 1
    mark = _mark(cache)

    def aged(mark, seconds):
        cache.put(radar.REFUSED_KEY, mark, ts=time.time() - seconds)
        return _sitemap(cfg, cache)._refused()
    assert aged(mark, radar.TILE_ERROR_BACKOFF - 60)
    assert aged(mark, radar.TILE_ERROR_BACKOFF + 60) is None
    for long_mark in (dict(mark, backoff_s=radar.TILE_REFUSED_BACKOFF),
                      {"tag": mark["tag"], "reason": "old"},
                      dict(mark, backoff_s="soon"), dict(mark, backoff_s=-5), dict(mark, backoff_s=10 ** 9),
                      dict(mark, backoff_s=True)):
        assert aged(long_mark, radar.TILE_ERROR_BACKOFF + 60)
        assert aged(long_mark, radar.TILE_REFUSED_BACKOFF - 60)
        assert aged(long_mark, radar.TILE_REFUSED_BACKOFF + 60) is None
    # another User-Agent or tile URL is not held back by this marker
    cache.put(radar.REFUSED_KEY, mark)
    assert radar.SiteMap(cfg, cache, cfg.sites[0], "light", "https://tiles.example.org/{z}/{x}/{y}.png")._refused() is None


def test_healthy_tiles_after_the_back_off_build_the_basemap(cfg, cache, fake_http, monkeypatch):
    asked = _tile_server(monkeypatch, _with_status(503))
    _sitemap(cfg, cache).basemap()
    assert len(asked) == 1
    cache.put(radar.REFUSED_KEY, _mark(cache), ts=time.time() - radar.TILE_ERROR_BACKOFF - 60)
    monkeypatch.setattr(http, "fetch_cacheable", fake_http.fetch_cacheable)
    fake_http.add("openstreetmap", _encoded("PNG", (256, 256), "RGB", (9, 9, 9)))
    sm = _sitemap(cfg, cache)
    assert sm.basemap().getpixel((3, 3)) == (9, 9, 9) and sm.basemap_note is None


# ---- review: the same defects through the real transport and a real unwritable cache -----------
@pytest.mark.parametrize("code,pause", ((429, radar.TILE_REFUSED_BACKOFF), (503, radar.TILE_ERROR_BACKOFF),
                                        (403, radar.TILE_REFUSED_BACKOFF), (404, None)))
def test_refusal_through_the_real_http_stack(code, pause, cfg, cache, monkeypatch):
    """Not a stand-in for ``fetch_cacheable``: the opener answers ``code`` and weather.http
    retries, words and chains the error as it does in a run. A refusal costs one tile URL
    (with http's own retries) and writes the marker; a 404 costs every tile and none."""
    asked = []

    class Opener:                       # weather.http's own opener, below its retry loop
        def open(self, req, timeout=None):
            asked.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b""))
    monkeypatch.setattr(http, "_OPENER", Opener())
    monkeypatch.setattr(http, "_sleep", lambda s: None)
    sm = _sitemap(cfg, cache)
    assert sm.render(NO_RADAR, [], os.path.join(cfg.out_dir, "a.png"))["ok"]
    mark = _mark(cache)
    if pause is None:
        tx0, ty0, tx1, ty1 = sm.frame.tile_range()
        assert len(set(asked)) == (tx1 - tx0 + 1) * (ty1 - ty0 + 1) and mark is None
    else:
        assert len(set(asked)) == 1 and mark["backoff_s"] == pause
        assert sm.basemap_note == "basemap unavailable: the tile server refused the requests"


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root ignores file modes")
def test_frame_is_used_with_a_read_only_cache_directory(cfg, cache, fake_http):
    """#23 by the route of the audit: the cache directory itself refuses the write."""
    fake_http.add("mesonet.agron", _frame_png())
    os.makedirs(cfg.cache_dir, exist_ok=True)
    os.chmod(cfg.cache_dir, 0o555)
    try:
        res = radar.load_frame(cfg, cache)
    finally:
        os.chmod(cfg.cache_dir, 0o755)
    assert res["ok"] and not res["stale"] and res["error"] is None and res["age_s"] == 0.0
    assert res["image"].size == FRAME_SIZE


def test_frame_http_error_after_retries_does_not_carry_the_archive_url(cfg, cache, fake_http, monkeypatch):
    """The text http raises after its retries starts with the URL ("<url>: HTTP Error 503")."""
    fake_http.add("mesonet.agron", b"x")

    def failing(url, cfg, **k):
        raise http.HttpError("%s: HTTP Error 503: Service Unavailable" % url)
    monkeypatch.setattr(http, "get_bytes", failing)
    res = radar.load_frame(cfg, cache)
    assert res["error"] == "the MRMS archive: HTTP Error 503: Service Unavailable"
    _clean(res["error"])
