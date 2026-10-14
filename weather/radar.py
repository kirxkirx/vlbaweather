"""MRMS radar frames, cached basemaps and the per-site map renderer.

Why it is built this way:

* One MRMS CONUS composite (a 7000x3500 palette PNG on a 0.01 deg plate-carree grid,
  from the Iowa Environmental Mesonet archive) is fetched per run and reprojected for
  every site with a lookup table -- no resampling library, no numpy.
* Frame discovery HEADs candidate frames newest first and walks back only while the archive
  answers "not there"; the first probe that gets no answer at all (timeout, refused, run
  budget spent) ends the search, so an unreachable IEM costs one short HEAD, not eleven.
* Basemap tiles are fetched once per site and theme; the composited crop is cached on disk
  forever, but only when every tile arrived, so one bad tile-server minute never leaves a
  hole in the cache. No key-free tile service offers a dark style with readable labels, so
  the dark theme inverts the lightness of light tiles instead (``invert_lightness``). That
  is decided per tile as it is pasted, so a missing tile's placeholder keeps the theme's
  background colour and a partial basemap never comes out half inverted.
* Alerts are drawn through an "L" mask (outer rings 255, holes 0). The same mask decides
  whether an alert "touches" the map (``drawn_ids``), so the page legend and the shading
  can never disagree. Outlines get a casing in the theme's stroke colour (black on dark,
  white on light) under the alert colour, so a red flood outline stays visible over red
  radar echoes.
* Emergency road closures (NMDOT) and road-related NWS storm reports are drawn on top of the
  alerts in the theme's ink over a halo in its stroke colour, never in a hue: flood alerts
  are already red and the radar uses cyan to magenta, so any colour would collide with one of
  them. Kinds are told apart by line style and symbol (solid line + "no entry" disc = road
  closed, dashed line + wave disc = water on the road, triangle = storm report). Everything
  is drawn on its own layer that is composited only when it was drawn completely, so
  ``roads_drawn_ids`` (the page's "on map" markers) never lists a road that is not on the
  map; without road items the PNG is exactly what it was before roads existed.
* Labels use the first TrueType font found (DejaVu Sans, Liberation Sans, FreeSans), else
  Pillow's default font; text measuring tolerates old Pillow releases whose ``textbbox``
  rejects the built-in bitmap font, so decorations never fail for want of a font.
* Nothing public raises on network or data problems: the generator runs from a timer and
  must keep producing a (labelled) page when IEM or the tile server are down. The PNG is
  written to a temporary file, fsynced, then renamed over the old one.
"""
from __future__ import annotations

import hashlib
import io
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat
except Exception:                       # pragma: no cover - optional dependency
    Image = ImageChops = ImageDraw = ImageFont = ImageStat = None

from . import geo, http, util

log = logging.getLogger("weather.radar")

# Echoes below this are never drawn, whatever cfg.radar_dbz_min says (they are noise).
DBZ_FLOOR = 5.0

# NWS-style composite reflectivity colours in 5 dBZ steps, HIGHEST FIRST so the first
# threshold <= value wins.
DBZ_PALETTE: List[Tuple[float, Tuple[int, int, int]]] = [
    (75, (253, 253, 253)),
    (70, (152, 84, 198)),
    (65, (248, 0, 253)),
    (60, (188, 0, 0)),
    (55, (212, 0, 0)),
    (50, (253, 0, 0)),
    (45, (253, 149, 0)),
    (40, (229, 188, 0)),
    (35, (253, 248, 2)),
    (30, (0, 142, 0)),
    (25, (1, 197, 1)),
    (20, (2, 253, 2)),
    (15, (3, 0, 244)),
    (10, (1, 159, 244)),
    (5, (4, 233, 231)),
]

# Overlay colours per basemap theme: dark labels vanish on a light basemap and vice versa,
# so every piece of text gets an ink colour plus a contrasting stroke.
THEMES: Dict[str, Dict[str, Tuple[int, int, int]]] = {
    "dark": {"ink": (255, 255, 255), "stroke": (0, 0, 0), "text": (210, 222, 240),
             "warn": (255, 200, 60), "marker": (120, 210, 255), "bg": (16, 20, 30)},
    "light": {"ink": (25, 30, 40), "stroke": (255, 255, 255), "text": (40, 55, 75),
              "warn": (170, 70, 0), "marker": (10, 90, 200), "bg": (236, 239, 243)},
}

# Tried in order; the first one that loads is remembered and used for every size. When a
# file is missing Pillow also looks its base name up under $XDG_DATA_DIRS/fonts (default
# /usr/share/fonts, recursively), which is what the bare names at the end rely on.
FONT_PATHS = (
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",           # Debian/Ubuntu
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",         # RHEL/Fedora
    "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf",       # RHEL/Fedora
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",   # Debian/Ubuntu
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",  # Debian >= 11
    "/usr/share/fonts/gnu-free/FreeSans.ttf",                    # RHEL/Fedora (gnu-free-sans)
    "/usr/share/fonts/gnu-free/FreeSans.otf",
    "/usr/share/fonts/gnu-freefont/FreeSans.otf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",           # Debian/Ubuntu
    "DejaVuSans.ttf",
    "LiberationSans-Regular.ttf",
    "FreeSans.ttf",
    "FreeSans.otf",
)

MRMS_LAST_KEY = "mrms_last"
MRMS_META_KEY = "mrms_last_meta"
TILE_REFUSED_BACKOFF = 6 * 3600   # after the tile server refuses us, no tile requests
#                                  for this long (unless the User-Agent / tile URL changes)
REFUSED_KEY = "tiles_refused"


class TileRefused(Exception):
    """The tile server refused us (blocked tile, 403/418/429): stop asking for a while."""


HEAD_TIMEOUT = 8.0          # s per frame probe: a static-file HEAD answers in well under 1 s

# Road items (roads.site_roads): monochrome, in the theme's ink over a halo in its stroke
# colour. Sizes are in output pixels.
ROAD_KINDS = ("closure", "water", "report")
ROAD_HALO_W = 9             # halo (stroke colour) under every road line
ROAD_LINE_W = 4             # the line itself (ink)
ROAD_DASH = (8.0, 6.0)      # "water on road": dash / gap along the line
ROAD_SYMBOL_R = 8           # radius of the closure / water discs
ROAD_REPORT_SIDE = 12.0     # storm-report triangle side
ROAD_REPORT_HALO = 2.0      # halo around the triangle
ROAD_KEY_LABELS = (("closure", "Road closed"), ("water", "Water on road"),
                   ("report", "Storm report (NWS)"))
ROAD_KEY_ALPHA = 205        # opacity of the key box background
# Pillow fills the pixels at both ends of a line, so a dash of geometric length L covers
# L + 1 pixels along the line: dashes are cut 1 px shorter (gaps 1 px longer) to come out
# at exactly ROAD_DASH.
_DASH_DRAWN = (ROAD_DASH[0] - 1.0, ROAD_DASH[1] + 1.0)


class RadarError(Exception):
    """A frame or tile that cannot be used (bad bytes, wrong mode/size, nothing found)."""


def deps_available() -> bool:
    return Image is not None


# ---- palette ------------------------------------------------------------------------
def dbz_color(dbz: Optional[float]) -> Optional[Tuple[int, int, int]]:
    """Colour for a reflectivity value, None below the 5 dBZ floor (or for no data)."""
    if dbz is None or dbz < DBZ_FLOOR:
        return None
    for thr, rgb in DBZ_PALETTE:
        if dbz >= thr:
            return rgb
    return None


def _index_lut(dbz_min: float, alpha: int) -> list:
    """MRMS palette index (0..255) -> RGBA or None. The per-pixel loop then does a single
    list lookup instead of a dBZ conversion plus a palette walk per pixel."""
    alpha = max(0, min(255, int(alpha)))
    lut = []
    for idx in range(256):
        d = geo.mrms_dbz(idx)
        rgb = dbz_color(d) if (d is not None and d >= dbz_min) else None
        lut.append(None if rgb is None else (rgb[0], rgb[1], rgb[2], alpha))
    return lut


# ---- frame discovery and loading ----------------------------------------------------
def _probe_frames(cfg, now: Optional[datetime] = None) -> Tuple[Optional[dict], str]:
    """``(found, why_not)``: the newest frame ``{"ts", "url"}`` and ``None``, or ``None``
    and a short reason for the page ("no MRMS frame in the last 24 min" when the archive
    answered every probe with "not there", "MRMS archive unreachable ..." when a probe got
    no answer at all)."""
    now = now or util.utcnow()
    start = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    if start.minute % 2:
        start -= timedelta(minutes=1)
    start -= timedelta(minutes=4)
    steps = max(0, int(cfg.radar_max_back))
    for i in range(steps + 1):
        ts = start - timedelta(minutes=2 * i)
        try:
            url = ts.strftime(cfg.mrms_archive)
        except Exception as e:      # noqa: BLE001 - a bad pattern fails for every step alike
            log.warning("bad MRMS archive pattern %r: %s", cfg.mrms_archive, e)
            return None, "bad MRMS archive pattern: %s" % e
        try:
            there = http.head_ok(url, cfg, timeout=HEAD_TIMEOUT)
        except Exception as e:      # noqa: BLE001 - head_ok should not raise; if it does,
            log.debug("frame probe raised for %s: %s", url, e)     # treat it as no answer
            there = None
        if there is None:
            # No answer (timeout, refused, host already marked down, run budget spent): the
            # older frames live on the same server, so walking back would only burn time.
            log.warning("MRMS archive not answering (HEAD %s); frame search stopped", url)
            return None, "MRMS archive unreachable (no answer to HEAD for %s)" % (
                ts.strftime("%H:%MZ"))
        if there:
            return {"ts": ts, "url": url}, ""
    log.warning("no MRMS frame found between %s and %s",
                (start - timedelta(minutes=2 * steps)).strftime("%H:%MZ"),
                start.strftime("%H:%MZ"))
    return None, "no MRMS frame in the last %d min" % (4 + 2 * steps)


def find_latest_frame(cfg, now: Optional[datetime] = None) -> Optional[dict]:
    """Newest MRMS frame present on the archive: ``{"ts": aware UTC datetime, "url"}``.

    IEM writes a frame every 2 minutes, a few minutes behind real time, so the search
    starts at "now floored to an even minute, minus 4 min" and walks back one frame at a
    time (up to ``cfg.radar_max_back`` steps) with a HEAD per candidate (cheap; the frames
    themselves are ~0.6 MB). ``http.head_ok`` answers True (there), False (a definite HTTP
    "not there": keep walking back) or None (no answer: stop at once and return None)."""
    return _probe_frames(cfg, now)[0]


def _open_frame(data: bytes):
    """Decode an MRMS PNG and insist on palette indices: the dBZ values ARE the indices
    (-32 + i/2), so an RGB rendering of the same picture would be useless."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise RadarError("bad MRMS image: %s" % e)
    if img.mode not in ("P", "L"):
        raise RadarError("unusable MRMS image mode %r (need palette indices)" % img.mode)
    if img.size != (geo.MRMS_W, geo.MRMS_H):
        raise RadarError("unexpected MRMS frame size %dx%d" % img.size)
    return img


def load_frame(cfg, cache) -> dict:
    """Fetch the latest MRMS frame, caching bytes + metadata; fall back to the cached
    frame for up to ``cfg.radar_max_stale`` seconds when IEM is unreachable.

    Returns ``{"ok", "stale", "error", "age_s", "ts", "url", "image"}``; never raises."""
    res = {"ok": False, "stale": False, "error": None, "age_s": None,
           "ts": None, "url": None, "image": None}
    if not deps_available():
        res["error"] = "Pillow not installed"
        return res
    try:
        found, why_not = _probe_frames(cfg)
        if not found:
            raise RadarError(why_not)
        meta, age = cache.get_any(MRMS_META_KEY)
        img = data = None
        if isinstance(meta, dict) and meta.get("url") == found["url"]:
            # Same frame as last run (IEM lagging): reuse the bytes, spare the 0.6 MB GET.
            data = cache.get_bytes(MRMS_LAST_KEY, ".png")
            if data is not None:
                try:
                    img = _open_frame(data)
                except RadarError as e:
                    log.warning("cached MRMS frame unreadable (%s); refetching", e)
                    img = None
        if img is None:
            data = http.get_bytes(found["url"], cfg, accept="image/png")
            img = _open_frame(data)
            cache.put_bytes(MRMS_LAST_KEY, ".png", data)
            cache.put(MRMS_META_KEY, {"ts": found["ts"].isoformat(), "url": found["url"]})
            age = 0.0
        res.update(ok=True, age_s=age if age is not None else 0.0,
                   ts=found["ts"], url=found["url"], image=img)
        return res
    except (http.HttpError, RadarError) as e:
        err = str(e)
    except Exception as e:      # noqa: BLE001 - any surprise = unavailable, never a crash
        err = "%s: %s" % (type(e).__name__, e)
    log.warning("MRMS frame unavailable: %s", err)
    res["error"] = err

    meta, age = cache.get_any(MRMS_META_KEY)
    res["age_s"] = age
    data = cache.get_bytes(MRMS_LAST_KEY, ".png")
    if data is None or not isinstance(meta, dict) or age is None or age > cfg.radar_max_stale:
        return res
    try:
        img = _open_frame(data)
    except RadarError as e:
        log.warning("cached MRMS frame unusable: %s", e)
        return res
    log.info("using cached MRMS frame from %s (%s old)", meta.get("ts"), util.fmt_age(age))
    res.update(ok=True, stale=True, ts=util.parse_iso(meta.get("ts")), url=meta.get("url"),
               image=img)
    return res


# ---- small drawing helpers ----------------------------------------------------------
_fonts: Dict[int, object] = {}
# Which FONT_PATHS entry loaded ("path"), and whether the list was already searched in vain
# ("searched"), so every further size costs one truetype() call, not a directory walk.
_font_state: Dict[str, object] = {"path": None, "searched": False}


def _default_font(size: int):
    """Pillow's own font: scalable (Aileron) with the requested size on Pillow >= 10.1 when
    FreeType is present, else the fixed ~11 px bitmap font (older ``load_default`` takes
    no size argument)."""
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()
    except Exception:           # noqa: BLE001 - e.g. FreeType broken: the bitmap font
        return ImageFont.load_default()


def _font(size: int):
    """The first loadable ``FONT_PATHS`` font at ``size`` (cached per size), else Pillow's
    default font (``_default_font``)."""
    size = max(6, int(size))
    f = _fonts.get(size)
    if f is not None:
        return f
    good = _font_state.get("path")
    if good:
        candidates = [good]
    elif _font_state.get("searched"):
        candidates = []
    else:
        candidates = list(FONT_PATHS)
    for p in candidates:
        try:
            f = ImageFont.truetype(p, size)
            _font_state["path"] = p
            break
        except Exception:           # noqa: BLE001 - missing file, no FreeType, bad font
            continue
    if f is None:
        if not good:
            _font_state["searched"] = True
            log.warning("no TrueType font found (tried %s); map labels use Pillow's "
                        "default font", ", ".join(FONT_PATHS) or "nothing")
        f = _default_font(size)
    _fonts[size] = f
    return f


# Pillow's built-in bitmap font encodes text as Latin-1 and raises on anything else ("…" from
# _fit_text, dashes, a site name with non-Latin letters): plain stand-ins for the usual ones.
_LATIN1_STANDINS = (("…", "..."), ("–", "-"), ("—", "-"), ("−", "-"), ("‘", "'"), ("’", "'"),
                    ("“", '"'), ("”", '"'))


def _safe_text(text: str, font) -> str:
    """``text`` as ``font`` can draw it: unchanged for TrueType fonts, reduced to Latin-1
    (known stand-ins, else "?") for the bitmap font."""
    text = str(text)
    if ImageFont is not None and isinstance(font, ImageFont.FreeTypeFont):
        return text
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        for a, b in _LATIN1_STANDINS:
            text = text.replace(a, b)
        return text.encode("latin-1", "replace").decode("latin-1")


def _draw_text(draw, xy, text: str, font, **kw) -> None:
    draw.text(xy, _safe_text(text, font), font=font, **kw)


def _text_size(draw, text: str, font) -> Tuple[int, int]:
    """(width, height) of ``text``. ``ImageDraw.textbbox`` only accepts TrueType fonts
    before Pillow 9.2 and ``font.getsize`` is gone since Pillow 10, so fall through the
    APIs until one answers -- decorations must not fail because of the font."""
    text = _safe_text(text, font)
    try:
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        return right - left, bottom - top
    except Exception:           # noqa: BLE001 - ValueError "Only supported for TrueType"
        pass
    try:
        left, top, right, bottom = font.getbbox(text)      # Pillow >= 9.2
        return right - left, bottom - top
    except Exception:           # noqa: BLE001
        pass
    try:
        w, h = font.getsize(text)                           # Pillow < 10
        return int(w), int(h)
    except Exception:           # noqa: BLE001
        pass
    try:
        w, h = font.getmask(text).size                      # every Pillow release
        return int(w), int(h)
    except Exception:           # noqa: BLE001
        return 6 * len(text), 11                            # bitmap-font sized guess


def _fit_text(draw, text: str, font, max_w: float) -> str:
    """Trim with an ellipsis until the text fits ``max_w`` pixels (the result is already
    in the form ``font`` can draw, see ``_safe_text``)."""
    text = _safe_text(text, font)
    if _text_size(draw, text, font)[0] <= max_w:
        return text
    ell = _safe_text("…", font)
    while len(text) > 1:
        text = text[:-1]
        cand = text.rstrip() + ell
        if _text_size(draw, cand, font)[0] <= max_w:
            return cand
    return ell


def _hex_rgb(s, default=(128, 128, 128)) -> Tuple[int, int, int]:
    try:
        s = str(s).strip().lstrip("#")
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        if len(s) != 6:
            return default
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except (TypeError, ValueError):
        return default


def _sget(site, key: str, default=None):
    """Sites are dicts (cfg.sites), but accept attribute objects too."""
    if isinstance(site, dict):
        return site.get(key, default)
    return getattr(site, key, default)


def _draw_order(alerts) -> list:
    """Reverse of ``alerts.sort_alerts`` (rank, event, end) -- statements are painted first
    and warnings last, so a warning is never hidden under a watch. Kept local (same key
    as the contract) so this module has no import-time dependency on alerts.py."""
    def key(a):
        rank = a.get("rank")
        rank = rank if isinstance(rank, (int, float)) else 10 ** 6
        try:
            end = a.get("end_dt").timestamp()
        except Exception:
            end = float("inf")
        return (rank, str(a.get("event") or ""), end)
    recs = [a for a in (alerts or ()) if isinstance(a, dict)]
    return list(reversed(sorted(recs, key=key)))


def _resample():
    return getattr(Image, "Resampling", Image).LANCZOS


# A tile whose mean luminance is above this is "light" and gets inverted for the dark theme
# (decided per tile, see SiteMap._basemap_from_tiles).
LIGHT_TILE_LUMINANCE = 128.0


def invert_lightness(img):
    """A dark-theme basemap from light tiles: every channel becomes ``c + 255 - (max + min)``,
    which flips lightness but keeps hue and saturation (the same result as CSS
    ``filter: invert(1) hue-rotate(180deg)``): a white background turns black, blue water
    stays blue, green forest stays green, black labels turn white."""
    rgb = img.convert("RGB")
    r, g, b = rgb.split()
    hi = ImageChops.lighter(ImageChops.lighter(r, g), b)
    lo = ImageChops.darker(ImageChops.darker(r, g), b)
    pos = ImageChops.add(hi, lo, 1.0, -255)                 # max(0, hi + lo - 255)
    neg = ImageChops.subtract(ImageChops.invert(hi), lo)    # max(0, 255 - hi - lo)
    return Image.merge("RGB", [ImageChops.add(ImageChops.subtract(c, pos), neg)
                               for c in (r, g, b)])


def mean_luminance(img) -> float:
    return float(ImageStat.Stat(img.convert("L")).mean[0])


def _decode_tile(data: bytes):
    tile = Image.open(io.BytesIO(data))
    tile.load()
    return tile.convert("RGB")          # tile servers send palette PNGs (or JPEGs)


# ---- road items: geometry ---------------------------------------------------------------
Pt = Tuple[float, float]


def _road_items(roads) -> List[Tuple[str, dict]]:
    """``(kind, item)`` pairs in input order from a ``roads.site_roads()`` dict (``events``
    then ``reports``; a report without a ``kind`` is a report) or from a plain list of
    items. Anything that is not a dict with a known kind is dropped, and so is an
    ``area_wide`` event (a patrol-area condition placed at one point: listed on the page,
    never drawn)."""
    if isinstance(roads, dict):
        groups = ((roads.get("events"), None), (roads.get("reports"), "report"))
    elif isinstance(roads, (list, tuple)):
        groups = ((roads, None),)
    else:
        return []
    out: List[Tuple[str, dict]] = []
    for items, default_kind in groups:
        if not isinstance(items, (list, tuple)):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            kind = it.get("kind") or default_kind
            if it.get("area_wide") is True:
                # a condition for a whole NMDOT patrol area, placed at one point of it: a
                # symbol there would claim a road where there may be none (listed only)
                log.debug("road item %r is area-wide: listed, not drawn", it.get("id"))
            elif kind in ROAD_KINDS:
                out.append((kind, it))
            else:
                log.debug("road item %r of unknown kind %r not drawn", it.get("id"), kind)
    return out


def _lonlat(p) -> Optional[Pt]:
    """``(lon, lat)`` of a GeoJSON position (a third value is ignored); None when it is not
    a finite number pair. Raises on a position that is not a sequence at all."""
    lon, lat = float(p[0]), float(p[1])
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    return lon, lat


def _road_parts(geom, depth: int = 0) -> Tuple[List[List[Pt]], List[Pt]]:
    """``(lines, points)`` of a GeoJSON geometry in lon/lat: LineString, MultiLineString,
    Point, MultiPoint and GeometryCollection (nested a few levels at most); other types
    contribute nothing. Non-finite positions are dropped."""
    lines: List[List[Pt]] = []
    points: List[Pt] = []
    if not isinstance(geom, dict) or depth > 4:
        return lines, points
    t, c = geom.get("type"), geom.get("coordinates")

    def line(coords):
        pts = [q for q in (_lonlat(p) for p in (coords or ())) if q is not None]
        if pts:
            lines.append(pts)

    if t == "Point" and c is not None:
        q = _lonlat(c)
        if q is not None:
            points.append(q)
    elif t == "MultiPoint":
        points.extend(q for q in (_lonlat(p) for p in (c or ())) if q is not None)
    elif t == "LineString":
        line(c)
    elif t == "MultiLineString":
        for part in c or ():
            line(part)
    elif t == "GeometryCollection":
        for g in geom.get("geometries") or ():
            ls, ps = _road_parts(g, depth + 1)
            lines.extend(ls)
            points.extend(ps)
    return lines, points


def _item_geometry(item: dict):
    """The item's geometry, or a Point built from its ``lat``/``lon`` (storm reports carry
    both) when it has none."""
    geom = item.get("geometry")
    if isinstance(geom, dict):
        return geom
    lat, lon = item.get("lat"), item.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return {"type": "Point", "coordinates": [lon, lat]}
    return None


def _clip_segment(x0: float, y0: float, x1: float, y1: float,
                  w: float, h: float) -> Optional[Tuple[float, float]]:
    """Liang-Barsky: the parameter range ``(t0, t1)`` of the segment inside the rectangle
    [0, w] x [0, h], or None when no part of it is inside."""
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0), (dx, w - x0), (-dy, y0), (dy, h - y0)):
        if p == 0:
            if q < 0:
                return None             # parallel to this edge and outside it
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    return t0, t1


def _clip_polyline(pts: Sequence[Pt], w: float, h: float) -> List[List[Pt]]:
    """The parts of a pixel polyline inside [0, w] x [0, h], each a list of >= 2 points (a
    one-point line inside the frame gives one zero-length part)."""
    if len(pts) == 1:
        x, y = pts[0]
        return [[pts[0], pts[0]]] if (0 <= x <= w and 0 <= y <= h) else []
    pieces: List[List[Pt]] = []
    cur: List[Pt] = []
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        tt = _clip_segment(ax, ay, bx, by, w, h)
        if tt is None:
            if cur:
                pieces.append(cur)
                cur = []
            continue
        t0, t1 = tt
        start = (ax + t0 * (bx - ax), ay + t0 * (by - ay))
        end = (ax + t1 * (bx - ax), ay + t1 * (by - ay))
        if t0 > 0 or not cur:           # entering the frame (or the very first segment)
            if cur:
                pieces.append(cur)
            cur = [start]
        cur.append(end)
        if t1 < 1:                      # leaving the frame
            pieces.append(cur)
            cur = []
    if cur:
        pieces.append(cur)
    return pieces


def _thin(pts: Sequence[Pt], min_d: float = 0.75) -> List[Pt]:
    """Drop vertices closer than ``min_d`` px to the previous kept one, keeping both ends.
    Road geometries carry a vertex every few metres, i.e. many per output pixel; wide lines
    through such clusters get ragged joints."""
    if len(pts) <= 2:
        return list(pts)
    out = [pts[0]]
    for p in pts[1:-1]:
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) >= min_d:
            out.append(p)
    last = pts[-1]
    if len(out) > 1 and math.hypot(last[0] - out[-1][0], last[1] - out[-1][1]) < min_d:
        out[-1] = last
    else:
        out.append(last)
    return out


def _length(pts: Sequence[Pt]) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))


def _midpoint(pieces: Sequence[Sequence[Pt]]) -> Optional[Pt]:
    """The point halfway along the total length of ``pieces`` (the first point when they
    have no length at all)."""
    if not pieces:
        return None
    half = sum(_length(p) for p in pieces) / 2.0
    if half <= 0:
        return pieces[0][0]
    for pc in pieces:
        for a, b in zip(pc, pc[1:]):
            seg = math.hypot(b[0] - a[0], b[1] - a[1])
            if seg >= half and seg > 0:
                f = half / seg
                return (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))
            half -= seg
    return pieces[-1][-1]


def _dashes(pts: Sequence[Pt], on: float, off: float) -> List[List[Pt]]:
    """Split a polyline into dash polylines of length ``on`` separated by gaps of ``off``,
    starting with a dash; the pattern runs on across vertices."""
    out: List[List[Pt]] = []
    if len(pts) < 2 or on <= 0:
        return out
    cur: List[Pt] = [pts[0]]
    drawing, left = True, float(on)
    for a, b in zip(pts, pts[1:]):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        pos = 0.0
        while seg - pos > left:                 # a dash/gap boundary inside this segment
            pos += left
            f = pos / seg
            p = (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))
            if drawing:
                cur.append(p)
                out.append(cur)
                cur = []
            else:
                cur = [p]
            drawing = not drawing
            left = float(on if drawing else off)
        left -= seg - pos
        if drawing:
            cur.append(b)
    if drawing and len(cur) >= 2 and _length(cur) > 0:
        out.append(cur)
    return out


# ---- road items: symbols ----------------------------------------------------------------
def _round_line(d, pts: Sequence[Pt], width: int, fill, caps: bool = True) -> None:
    """A wide polyline with round joins and (optionally) round caps: Pillow ends wide lines
    square, so a disc of the line's width goes on each end."""
    d.line([tuple(p) for p in pts], fill=fill, width=width, joint="curve")
    if caps:
        r = width / 2.0
        for x, y in (pts[0], pts[-1]):
            d.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def _disc(d, x: float, y: float, r: float, fill) -> None:
    d.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def _closure_symbol(d, x: float, y: float, ink, stroke) -> None:
    """"No entry", monochrome: an ink disc with a bar in the stroke colour, ringed by 1 px
    of stroke so it separates from the ink line it sits on."""
    r = ROAD_SYMBOL_R
    _disc(d, x, y, r + 1, stroke)
    _disc(d, x, y, r, ink)
    d.rectangle([x - r + 3, y - 1.5, x + r - 3, y + 1.5], fill=stroke)


def _water_symbol(d, x: float, y: float, ink, stroke) -> None:
    """A disc in the stroke colour with an ink ring and two ink wave strokes."""
    r = ROAD_SYMBOL_R
    _disc(d, x, y, r + 1, stroke)               # 1 px separation from the line / basemap
    _disc(d, x, y, r, ink)
    _disc(d, x, y, r - 2, stroke)
    for dy in (-2.5, 2.5):
        wave = [(x + k * 0.5, y + dy - 1.2 * math.sin(k * 0.5 * 2.0 * math.pi / 5.0))
                for k in range(-10, 11)]
        d.line(wave, fill=ink, width=2, joint="curve")


def _triangle(x: float, y: float, side: float) -> List[Pt]:
    """Upward equilateral triangle with its centroid at (x, y)."""
    return [(x, y - side / math.sqrt(3.0)),
            (x + side / 2.0, y + side / (2.0 * math.sqrt(3.0))),
            (x - side / 2.0, y + side / (2.0 * math.sqrt(3.0)))]


def _report_symbol(d, x: float, y: float, ink, stroke) -> None:
    """A small upward ink triangle with a halo in the stroke colour (the halo is the same
    triangle grown by ``ROAD_REPORT_HALO`` px on every side)."""
    grown = ROAD_REPORT_SIDE + 2.0 * math.sqrt(3.0) * ROAD_REPORT_HALO
    d.polygon(_triangle(x, y, grown), fill=stroke)
    d.polygon(_triangle(x, y, ROAD_REPORT_SIDE), fill=ink)


def _symbol_extent(kind: str) -> float:
    """How far a kind's symbol reaches from its anchor (to keep it on the map)."""
    if kind == "report":
        grown = ROAD_REPORT_SIDE + 2.0 * math.sqrt(3.0) * ROAD_REPORT_HALO
        return grown / math.sqrt(3.0)
    return ROAD_SYMBOL_R + 1.0


_SYMBOLS = {"closure": _closure_symbol, "water": _water_symbol, "report": _report_symbol}


def _report_clear_of(x: float, y: float, discs: Sequence[Pt], h: float) -> Pt:
    """Where a storm-report triangle goes so that no closure / water disc (drawn later, on
    top) hides it: its own anchor when that is clear, else just above the disc it would sit
    under (base 1 px above the disc), or just below it when there is no room above. Storm
    reports often repeat an NMDOT item at the same spot (e.g. "water over NM 252" next to
    NMDOT's NM 252 flooding), and a hidden triangle would make the key and the page's
    "on map" marker claim something no viewer can see."""
    r_disc = ROAD_SYMBOL_R + 1.0
    grown = ROAD_REPORT_SIDE + 2.0 * math.sqrt(3.0) * ROAD_REPORT_HALO
    circ = grown / math.sqrt(3.0)               # centroid -> vertex
    inr = grown / (2.0 * math.sqrt(3.0))        # centroid -> base
    for _ in range(len(discs) + 1):
        hit = next(((dx, dy) for dx, dy in discs
                    if math.hypot(x - dx, y - dy) < r_disc + circ), None)
        if hit is None:
            break
        above = hit[1] - r_disc - inr - 1.0
        y = above if above - circ >= 0 else hit[1] + r_disc + circ + 1.0
    return x, min(max(y, circ), h - 1 - inr) if h - 1 > circ + inr else y


# ---- per-site map -----------------------------------------------------------------------
class SiteMap:
    """Basemap + radar + alerts + decorations for one site and one theme.

    The remap table (frame pixel -> MRMS cell) and the basemap are built lazily and kept
    for the life of the object, so rendering a second frame costs only the compositing."""

    # The scale bars, (label, length in km), drawn bottom-left from the bottom up. A
    # subclass may draw others, e.g. 10 mi and 10 km for a map with range rings in km.
    SCALE_BARS: Tuple[Tuple[str, float], ...] = (("25 mi", 25 * geo.MI_TO_KM),
                                                 ("10 mi", 10 * geo.MI_TO_KM))

    def __init__(self, cfg, cache, site, theme: str, tile_url: str):
        self.cfg = cfg
        self.cache = cache
        self.site = site
        self.theme = theme
        self.tile_url = tile_url
        self.slug = str(_sget(site, "slug", "site"))
        self.name = str(_sget(site, "name", self.slug))
        self.lat = float(_sget(site, "lat"))
        self.lon = float(_sget(site, "lon"))
        self.frame = geo.MapFrame(self.lat, self.lon, cfg.map_km, cfg.map_px, cfg.tile_zoom)
        self.colors = THEMES.get(theme, THEMES["dark"])
        self.basemap_note: Optional[str] = None     # e.g. "basemap incomplete (3/9 tiles)"
        self._basemap = None
        self._cols: Optional[list] = None
        self._rows: Optional[list] = None
        self._lut: Optional[list] = None
        self._road_key: Optional[dict] = None       # key box of the last render (tests)
        self._road_marks: List[Tuple[object, str, float, float]] = []   # (id, kind, x, y)
        self._road_alpha = None                      # alpha of the last road layer

    # -- basemap -------------------------------------------------------------------
    def _invert_wanted(self) -> bool:
        return self.theme == "dark" and bool(getattr(self.cfg, "tile_dark_invert", True))

    @property
    def basemap_key(self) -> str:
        """Disk-cache key of the composited basemap: site, theme and frame plus a hash of
        the tile URL and the inversion setting, so a changed WEATHER_TILE_URL_* or
        WEATHER_TILE_DARK_INVERT can never keep serving a basemap built from the old source."""
        tag = hashlib.sha1(("%s|%d" % (self.tile_url, self._invert_wanted()))
                           .encode("utf-8")).hexdigest()[:8]
        return "basemap_%s_%s_%s_%s" % (self.slug, self.theme, self.frame.cache_key(), tag)

    def basemap(self):
        """RGB image of ``frame.size``; from the disk cache, else composited from tiles
        (and cached only when every tile arrived). Never raises: with no tiles at all the
        result is a plain background and ``basemap_note`` says so."""
        if self._basemap is not None:
            return self._basemap
        key = self.basemap_key
        img = self._basemap_from_cache(key)
        if img is None:
            img = self._basemap_from_tiles(key)
        self._basemap = img
        return img

    def _basemap_from_cache(self, key: str):
        """The cached composite. Tiles and composites are kept for good once verified (the
        owner's choice: download each tile once, reuse it for ever); a composite without the
        "ok" meta was written by an older version that could cache the tile server's
        "Access blocked" tile, so it is rebuilt once."""
        meta, _ = self.cache.get_any(key + "__meta")
        if not (isinstance(meta, dict) and meta.get("ok")):
            return None
        data = self.cache.get_bytes(key, ".png")
        if not data:
            return None
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
            img = img.convert("RGB")
        except Exception as e:
            log.warning("cached basemap %s unreadable (%s); rebuilding", key, e)
            return None
        if img.size != self.frame.size:
            log.info("cached basemap %s has size %s, want %s; rebuilding",
                     key, img.size, self.frame.size)
            return None
        log.debug("basemap %s loaded from cache (tiles not fetched)", key)
        return img

    def _refusal_tag(self) -> str:
        """Who was refused: the User-Agent and the tile URL template. A changed
        WEATHER_USER_AGENT or WEATHER_TILE_URL_* lifts the back-off at once."""
        return hashlib.sha1(("%s|%s" % (self.cfg.user_agent, self.tile_url))
                            .encode("utf-8")).hexdigest()[:12]

    def _tile(self, url: str):
        """One decoded RGB tile. A good tile is downloaded ONCE and then served from the
        disk cache for good (both themes and every rebuild share it). Only a tile whose
        cache entry carries the "ok" meta counts: tiles cached by older versions may be the
        tile server's "Access blocked" image and are fetched once more. A reply the server
        marks ``no-cache`` / ``no-store`` (OpenStreetMap sends its blocked tile with HTTP 200
        and ``Cache-Control: no-cache``) or a 403/418/429 raises ``TileRefused`` and is never
        stored; bytes that do not decode are not stored either."""
        key = "tile_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        meta, _ = self.cache.get_any(key + "__meta")
        if isinstance(meta, dict) and meta.get("ok"):
            data = self.cache.get_bytes(key, ".png")
            if data is not None:
                try:
                    return _decode_tile(data)
                except Exception as e:  # noqa: BLE001 - a corrupt cache file: refetch
                    log.warning("cached tile %s unreadable (%s); refetching", url, e)
        try:
            res = http.fetch_cacheable(url, self.cfg, accept="image/png")
        except http.HttpError as e:
            if re.match(r"HTTP (403|418|429)\b", str(e)):
                raise TileRefused(str(e))
            raise
        if res["no_cache"]:
            raise TileRefused("the tile server sent a tile marked not cacheable "
                              "(its 'Access blocked' tile)")
        data = res["body"] or b""
        tile = _decode_tile(data)
        try:
            self.cache.put_bytes(key, ".png", data)
            self.cache.put(key + "__meta", {"ok": True, "url": url})
        except Exception as e:          # noqa: BLE001 - the cache is an optimisation
            log.warning("could not cache tile %s: %s", url, e)
        return tile

    def _basemap_from_tiles(self, key: str):
        """Composite the covering tiles on a canvas of the theme's background colour. For
        the dark theme each light tile is inverted on its own before it is pasted: deciding
        on the whole composite would let the placeholder of missing tiles tip the balance
        (a bright block where tiles are missing, or light tiles left un-inverted).

        When the tile server refuses us, the build stops at once (no further tile requests
        this run) and a back-off marker keeps every map of the next ``TILE_REFUSED_BACKOFF``
        from asking again; the maps are drawn on a plain background with a note."""
        fr = self.frame
        canvas = Image.new("RGB", fr.canvas_size(), self.colors["bg"])
        got = expected = inverted = 0
        invert = self._invert_wanted()
        refused = self._refused()
        try:
            tx0, ty0, tx1, ty1 = fr.tile_range()
            expected = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
            for tx in range(tx0, tx1 + 1):
                for ty in range(ty0, ty1 + 1):
                    if refused:
                        break
                    url = self.tile_url.format(z=fr.zoom, x=tx, y=ty)
                    try:
                        tile = self._tile(url)
                        if tile.size != (geo.TILE_SIZE, geo.TILE_SIZE):
                            tile = tile.resize((geo.TILE_SIZE, geo.TILE_SIZE), _resample())
                        if invert and mean_luminance(tile) > LIGHT_TILE_LUMINANCE:
                            tile = invert_lightness(tile)
                            inverted += 1
                        canvas.paste(tile, fr.tile_paste_origin(tx, ty))
                        got += 1
                    except TileRefused as e:
                        refused = str(e)
                        self._mark_refused(refused)
                    except Exception as e:      # noqa: BLE001 - one tile must not kill the map
                        log.warning("tile z%d/%d/%d failed: %s", fr.zoom, tx, ty, e)
                if refused:
                    break
        except Exception as e:                  # noqa: BLE001 - bad URL template etc.
            log.error("basemap build failed for %s/%s: %s", self.slug, self.theme, e)
        if inverted:
            log.debug("%s/%s: %d of %d light tiles inverted for the dark theme",
                      self.slug, self.theme, inverted, got)
        img = canvas.crop(fr.canvas_crop()).resize(fr.size, _resample())
        if refused:
            self.basemap_note = "basemap unavailable: the tile server refused the requests"
            return img
        if expected and got == expected:
            self.basemap_note = None
            try:
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                self.cache.put_bytes(key, ".png", buf.getvalue())
                self.cache.put(key + "__meta", {"ok": True})
                log.info("basemap %s built from %d tiles and cached", key, got)
            except Exception as e:              # noqa: BLE001 - cache is an optimisation
                log.warning("could not cache basemap %s: %s", key, e)
        else:
            # A partial basemap is fine for THIS run but must never be cached.
            self.basemap_note = "basemap incomplete (%d/%d tiles)" % (got, expected)
            log.warning("%s/%s: %s -- not cached, will retry next run",
                        self.slug, self.theme, self.basemap_note)
        return img

    def _refused(self) -> Optional[str]:
        """The reason of a still-running back-off for this User-Agent + tile URL, else None."""
        mark = self.cache.get(REFUSED_KEY, TILE_REFUSED_BACKOFF)
        if isinstance(mark, dict) and mark.get("tag") == self._refusal_tag():
            log.debug("%s/%s: tile requests paused: %s", self.slug, self.theme, mark.get("reason"))
            return str(mark.get("reason") or "refused")
        return None

    def _mark_refused(self, reason: str) -> None:
        log.error("tile server refused the map tiles (%s). No tile requests for the next %d h. "
                  "OpenStreetMap blocks User-Agents that are a library default or carry a "
                  "placeholder contact such as example.org: check WEATHER_USER_AGENT "
                  "(https://osm.wiki/Blocked, https://operations.osmfoundation.org/policies/tiles/)",
                  reason, TILE_REFUSED_BACKOFF // 3600)
        try:
            self.cache.put(REFUSED_KEY, {"tag": self._refusal_tag(), "reason": reason})
        except Exception as e:                  # noqa: BLE001
            log.warning("could not record the tile back-off: %s", e)

    # -- radar ---------------------------------------------------------------------
    def _remap(self) -> Tuple[list, list]:
        """Frame pixel -> MRMS cell as two 1-D tables: ``cols[i]`` and ``rows[j]``.

        Web-Mercator x depends only on longitude and y only on latitude, and the MRMS grid
        is plate carree, so the per-pixel lookup separates: pixel (i, j) reads MRMS cell
        (cols[i], rows[j]). Building it costs w + h projections instead of w * h, and the
        table is built once per SiteMap. Cells off the grid are None."""
        if self._cols is None:
            fr = self.frame
            cols, rows = [], []
            for i in range(fr.width):
                lon, _ = fr.px_to_lonlat(i + 0.5, fr.height / 2.0)
                c, _ = geo.mrms_cell(fr.lat0, lon)
                cols.append(c if 0 <= c < geo.MRMS_W else None)
            for j in range(fr.height):
                _, lat = fr.px_to_lonlat(fr.width / 2.0, j + 0.5)
                _, r = geo.mrms_cell(lat, fr.lon0)
                rows.append(r if 0 <= r < geo.MRMS_H else None)
            self._cols, self._rows = cols, rows
        return self._cols, self._rows

    def _radar_layer(self, img):
        """RGBA overlay: the MRMS frame reprojected onto the frame, transparent below
        ``cfg.radar_dbz_min`` and where there is no data."""
        if img.mode not in ("P", "L"):
            raise RadarError("unusable radar image mode %r" % img.mode)
        if img.size != (geo.MRMS_W, geo.MRMS_H):
            raise RadarError("unexpected radar frame size %dx%d" % img.size)
        cols, rows = self._remap()
        if self._lut is None:
            self._lut = _index_lut(max(DBZ_FLOOR, float(self.cfg.radar_dbz_min)),
                                   self.cfg.radar_alpha)
        lut = self._lut
        src = img.load()
        layer = Image.new("RGBA", self.frame.size, (0, 0, 0, 0))
        dst = layer.load()
        for j, r in enumerate(rows):
            if r is None:
                continue
            for i, c in enumerate(cols):
                if c is None:
                    continue
                rgba = lut[src[c, r]]
                if rgba is not None:
                    dst[i, j] = rgba
        return layer

    # -- alerts --------------------------------------------------------------------
    def _alert_mask(self, alert: dict):
        """("L" mask of the alert's polygons inside the frame, outline rings in px) or
        (None, []) when nothing of it lands on the map."""
        fr = self.frame
        geom = alert.get("geometry")
        if not geom:
            return None, []
        bbox = alert.get("bbox") or geo.geometry_bbox(geom)
        if not geo.bbox_intersects(bbox, fr.bbox):
            return None, []
        mask = Image.new("L", fr.size, 0)
        md = ImageDraw.Draw(mask)
        outlines = []
        for rings in geo.iter_polygons(geom):
            px_rings = [fr.ring_to_px(r) for r in rings if len(r) >= 3]
            if not px_rings:
                continue
            if len(px_rings) == 1:
                md.polygon(px_rings[0], fill=255)
            else:
                # Rasterise a holed polygon on its own (outer 255, holes 0) and union it
                # in, so its hole cannot punch through a neighbouring polygon of the same
                # alert (MultiPolygons from merged zones overlap freely).
                part = Image.new("L", fr.size, 0)
                pd = ImageDraw.Draw(part)
                pd.polygon(px_rings[0], fill=255)
                for hole in px_rings[1:]:
                    pd.polygon(hole, fill=0)
                mask.paste(255, (0, 0), part)
            outlines.extend(px_rings)
        if mask.getbbox() is None:
            return None, []
        return mask, outlines

    def _draw_alerts(self, im, alerts) -> Tuple[object, List[str]]:
        """Composite every alert that touches the frame; returns (image, drawn ids)."""
        cfg = self.cfg
        fill_alpha = max(0, min(255, int(cfg.alert_fill_alpha)))
        alpha_table = [0] + [fill_alpha] * 255
        width = max(0, int(cfg.alert_outline_width))
        casing = self.colors["stroke"] + (255,)     # black on dark, white on light
        drawn: List[str] = []
        for a in _draw_order(alerts):
            try:
                mask, outlines = self._alert_mask(a)
                if mask is None:
                    continue
                rgb = _hex_rgb(a.get("color"))
                layer = Image.new("RGBA", self.frame.size, rgb + (0,))
                layer.putalpha(mask.point(alpha_table))
                if width:
                    # Casing first (all rings), then the coloured line on top: the outline
                    # shows up as colour between two thin stroke-coloured edges, visible even
                    # over echoes of the same hue. Doing all casings before any colour keeps
                    # one zone's casing from cutting the line of its neighbour.
                    d = ImageDraw.Draw(layer)
                    for ring in outlines:
                        d.line(ring + ring[:1], fill=casing, width=width + 2, joint="curve")
                    for ring in outlines:
                        d.line(ring + ring[:1], fill=rgb + (255,), width=width, joint="curve")
                im = Image.alpha_composite(im, layer)
                drawn.append(a.get("id"))
            except Exception as e:              # noqa: BLE001 - one bad alert, not the map
                log.warning("alert %s not drawn on %s: %s", a.get("id"), self.slug, e)
        return im, drawn

    # -- roads ---------------------------------------------------------------------
    def _road_shape(self, kind: str, item: dict) -> Tuple[List[List[Pt]], Optional[Pt]]:
        """``(line parts, anchor)`` of one road item in frame pixels: the parts of its lines
        inside the frame (none for storm reports) and where its symbol goes -- halfway along
        the visible line, else its first point inside the frame -- pulled in just far enough
        that the whole symbol stays on the map. ``anchor`` is None when nothing of the item
        lands on the map (it is then not drawn and not in ``roads_drawn_ids``)."""
        fr = self.frame
        w, h = fr.size
        lines, points = _road_parts(_item_geometry(item))
        if not lines and not points:
            return [], None
        # exact and cheap: Web-Mercator x depends only on lon and y only on lat, so the
        # frame is a lon/lat box and anything whose own box misses it is off the map
        lons = [p[0] for ln in lines for p in ln] + [p[0] for p in points]
        lats = [p[1] for ln in lines for p in ln] + [p[1] for p in points]
        if not geo.bbox_intersects((min(lons), min(lats), max(lons), max(lats)), fr.bbox):
            return [], None
        pieces: List[List[Pt]] = []
        for ln in lines:
            px = [fr.lonlat_to_px(lon, lat) for lon, lat in ln]
            pieces.extend(_thin(pc) for pc in _clip_polyline(px, w, h))
        anchor = _midpoint(pieces)
        if anchor is None:
            for lon, lat in points:
                x, y = fr.lonlat_to_px(lon, lat)
                if 0 <= x <= w and 0 <= y <= h:
                    anchor = (x, y)
                    break
        if anchor is None:
            return [], None
        e = _symbol_extent(kind)
        x = min(max(anchor[0], e), w - 1 - e) if w - 1 > 2 * e else anchor[0]
        y = min(max(anchor[1], e), h - 1 - e) if h - 1 > 2 * e else anchor[1]
        if kind == "report":
            pieces = []
        return [pc for pc in pieces if _length(pc) > 0], (x, y)

    def _draw_roads(self, im, roads) -> Tuple[object, List[str], Dict[str, bool]]:
        """Composite the road items that land on the map (``roads`` is a
        ``roads.site_roads()`` dict). Returns ``(image, drawn ids, {kind: drawn with a
        line})`` -- the last one feeds the key box. Bottom to top: water lines, closure lines
        (each kind's halos before its ink, so crossing roads of one kind merge), then storm
        report triangles, water discs and closure discs; a triangle that a disc would cover
        is moved just above (or below) that disc. Everything goes on one layer that is
        composited only when all of it was drawn, so the ids and the pixels agree."""
        shapes = []
        for kind, item in _road_items(roads):
            try:
                pieces, anchor = self._road_shape(kind, item)
            except Exception as e:              # noqa: BLE001 - one bad item, not the map
                log.warning("road item %s not drawn on %s: %s", item.get("id"), self.slug, e)
                continue
            if anchor is not None:
                shapes.append((kind, item, pieces, anchor))
        if not shapes:
            return im, [], {}
        ink, stroke = self.colors["ink"] + (255,), self.colors["stroke"] + (255,)
        layer = Image.new("RGBA", self.frame.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        on, off = _DASH_DRAWN
        for want in ("water", "closure"):
            mine = [pieces for kind, _, pieces, _ in shapes if kind == want]
            for pieces in mine:
                for pc in pieces:
                    _round_line(d, pc, ROAD_HALO_W, stroke)
            for pieces in mine:
                for pc in pieces:
                    if want == "closure":
                        _round_line(d, pc, ROAD_LINE_W, ink)
                    else:
                        for dash in _dashes(pc, on, off):
                            _round_line(d, dash, ROAD_LINE_W, ink, caps=False)
        discs = [anchor for kind, _, _, anchor in shapes if kind in ("water", "closure")]
        marks: List[Tuple[object, str, float, float]] = []
        for want in ("report", "water", "closure"):
            for kind, item, _, (x, y) in shapes:
                if kind == want:
                    if kind == "report" and discs:
                        x, y = _report_clear_of(x, y, discs, self.frame.size[1])
                    _SYMBOLS[kind](d, x, y, ink, stroke)
                    marks.append((item.get("id"), kind, x, y))
        drawn: List[str] = []
        kinds: Dict[str, bool] = {}
        for kind, item, pieces, _ in shapes:
            iid = item.get("id")
            if iid is not None and iid not in drawn:
                drawn.append(iid)
            kinds[kind] = kinds.get(kind, False) or bool(pieces)
        out = Image.alpha_composite(im, layer)
        self._road_marks, self._road_alpha = marks, layer.getchannel("A")
        return out, drawn, kinds

    def _draw_road_key(self, im, kinds: Dict[str, bool], font, spots: Sequence[tuple],
                       avoid: Sequence[Tuple[float, float, float, float]] = ()) -> None:
        """The key for the road symbols on this map: one row per kind drawn, on a
        semi-opaque box in the theme's background colour. Each row ends in the kind's symbol;
        a sample of its line (only when a line of that kind was drawn) runs into the symbol
        from the left, long enough to show the water dash pattern.

        Where: of ``spots`` (``(name, x, y, corner)``: the box's ``corner`` "bl", "br", "tr"
        or "tl" at ``(x, y)``; bottom-left above the scale bars comes first), the first that
        fits on the map, covers none of the ``avoid`` rectangles (the site marker and its
        label) and no road symbol, and then the fewest road pixels. When no place is free of
        the marker and of every road symbol (a small map, or symbols in every corner) the key
        is left out: a hidden symbol would make the page's "on map" untrue, and the page
        lists every item with the same symbols anyway."""
        rows = [(k, label) for k, label in ROAD_KEY_LABELS if k in kinds]
        if not rows:
            return
        col = self.colors
        ink, stroke = col["ink"] + (255,), col["stroke"] + (255,)
        w, h = self.frame.size
        d = ImageDraw.Draw(im)
        pad, sample_w, gap = 5, 48, 6
        text_h = max(_text_size(d, label, font)[1] for _, label in rows)
        row_h = max(2 * ROAD_SYMBOL_R + 4, text_h + 4)
        max_text = max(20, w - 8 - 2 * pad - sample_w - gap - 8)
        labels = [(k, _fit_text(d, label, font, max_text)) for k, label in rows]
        text_w = max(_text_size(d, label, font)[0] for _, label in labels)
        bw = pad + sample_w + gap + text_w + pad
        bh = 2 * pad + len(labels) * row_h
        best = None
        for order, (name, ax, ay, corner) in enumerate(spots):
            x0 = int(ax) if corner in ("bl", "tl") else int(ax) - bw
            y0 = int(ay) - bh if corner in ("bl", "br") else int(ay)
            x1, y1 = x0 + bw, y0 + bh
            if x0 < 0 or y0 < 0 or x1 > w - 1 or y1 > h - 1:
                continue
            covers_marker = any(x0 <= r[2] and r[0] <= x1 and y0 <= r[3] and r[1] <= y1
                                for r in avoid)
            under = [m for m in self._road_marks if x0 <= m[2] <= x1 and y0 <= m[3] <= y1]
            pixels = 0
            if self._road_alpha is not None:
                hist = self._road_alpha.crop((x0, y0, x1 + 1, y1 + 1)).histogram()
                pixels = sum(hist) - hist[0]
            score = (covers_marker, len(under), pixels, order)
            if best is None or score < best[0]:
                best = (score, name, (x0, y0, x1, y1), under)
        if best is None or best[0][0] or best[0][1]:
            log.info("road key left out on %s/%s: no place for it that keeps the site marker "
                     "and the road symbols clear (map %dx%d)", self.slug, self.theme, w, h)
            return
        _, where, (x0, y0, x1, y1), _ = best
        box = Image.new("RGBA", (x1 - x0 + 1, y1 - y0 + 1), col["bg"] + (ROAD_KEY_ALPHA,))
        ImageDraw.Draw(box).rectangle([0, 0, x1 - x0, y1 - y0], outline=col["text"] + (150,))
        im.alpha_composite(box, dest=(x0, y0))
        d = ImageDraw.Draw(im)
        on, off = _DASH_DRAWN
        for i, (kind, label) in enumerate(labels):
            cy = y0 + pad + i * row_h + row_h / 2.0
            cx = x0 + pad + sample_w - ROAD_SYMBOL_R - 1
            if kind in ("closure", "water") and kinds.get(kind):
                seg = [(x0 + pad + ROAD_HALO_W / 2.0, cy), (cx, cy)]
                _round_line(d, seg, ROAD_HALO_W, stroke)
                if kind == "closure":
                    _round_line(d, seg, ROAD_LINE_W, ink)
                else:
                    for dash in _dashes(seg, on, off):
                        _round_line(d, dash, ROAD_LINE_W, ink, caps=False)
            _SYMBOLS[kind](d, cx, cy, ink, stroke)
            th = _text_size(d, label, font)[1]
            _draw_text(d, (x0 + pad + sample_w + gap, cy - th / 2.0 - 1), label, font,
                       fill=ink)
        self._road_key = {"box": (x0, y0, x1, y1), "labels": [label for _, label in labels],
                          "where": where}

    # -- decorations ---------------------------------------------------------------
    def _decorate(self, im, radar: dict, notes: Sequence[str], tz: Optional[str],
                  road_kinds: Optional[Dict[str, bool]] = None) -> None:
        fr, cfg, col = self.frame, self.cfg, self.colors
        w, h = fr.size
        d = ImageDraw.Draw(im)
        ink, stroke = col["ink"] + (255,), col["stroke"] + (255,)
        base = max(9, min(14, w // 45))             # 600 px -> 13, 240 px -> 9
        f_main, f_small, f_tiny = _font(base), _font(max(8, base - 2)), _font(max(7, base - 4))

        # attribution along the bottom edge; everything else stacks above it
        attr = _fit_text(d, str(cfg.attribution), f_tiny, w - 8)
        _, ah = _text_size(d, attr, f_tiny)
        _draw_text(d, (4, h - ah - 3), attr, f_tiny, fill=col["text"] + (255,),
                   stroke_width=1, stroke_fill=stroke)
        bottom = h - ah - 8

        # site crosshair + label
        cx, cy = fr.lonlat_to_px(self.lon, self.lat)
        r = max(4, w // 120)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ink, width=2)
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            d.line([cx + dx * (r + 2), cy + dy * (r + 2), cx + dx * (r + 8), cy + dy * (r + 8)],
                   fill=ink, width=2)
        _draw_text(d, (cx + r + 8, cy - base), self.name, f_main, fill=col["marker"] + (255,),
                   stroke_width=2, stroke_fill=stroke)
        lw, lh = _text_size(d, self.name, f_main)
        marker = [(cx - r - 10, cy - r - 10, cx + r + 10, cy + r + 10),
                  (cx + r + 6, cy - base - 2, cx + r + 10 + lw, cy - base + lh + 4)]

        # scale bars, bottom-left (px_for_km is exact east-west at the centre latitude)
        bx, by = 12, bottom - 5
        scale_top = by
        for label, km in self.SCALE_BARS:
            length = fr.px_for_km(km)
            d.line([bx, by, bx + length, by], fill=ink, width=3)
            d.line([bx, by - 3, bx, by + 3], fill=ink, width=2)
            d.line([bx + length, by - 3, bx + length, by + 3], fill=ink, width=2)
            _draw_text(d, (bx + length + 5, by - base * 0.65), label, f_small, fill=ink,
                       stroke_width=2, stroke_fill=stroke)
            scale_top = min(scale_top, by - 4, by - base * 0.65)
            by -= base + 6

        # geometry of what is drawn after the key: the dBZ bar and the caption
        steps = list(reversed(DBZ_PALETTE))          # ascending 5..75
        sw = max(4, min(12, w // 45))
        sh = max(5, sw - 3)
        bar_w = sw * len(steps)
        x0, y0 = w - bar_w - 10, bottom - sh - 4
        lines = []
        ts = radar.get("ts")
        # The caption names the radar product: MRMS unless the result says otherwise
        # (``radar["label"]``: for a subclass whose ``_radar_layer`` draws another product's
        # frame, such as an NWS radar composite for a site outside the MRMS grid).
        label = str(radar.get("label") or "MRMS")
        if radar.get("ok") and isinstance(ts, datetime):
            cap = label + " " + ts.astimezone(timezone.utc).strftime("%H:%MZ")
            if tz:
                cap += " · " + util.fmt_local(ts, tz, "%H:%M %Z")
            lines.append((cap, col["text"]))
            if radar.get("stale"):
                lines.append(("radar STALE (%s)" % util.fmt_age(radar.get("age_s")), col["warn"]))
        elif radar.get("ok"):
            lines.append((label + " (frame time unknown)", col["text"]))
        elif radar.get("not_applicable"):
            # no map of this run reaches the MRMS grid: nothing was fetched, nothing failed
            lines.append((str(radar.get("error") or "no radar here"), col["warn"]))
        else:
            lines.append(("radar unavailable: %s" % (radar.get("error") or "no frame"),
                          col["warn"]))
        for n in notes:
            if n:
                lines.append((str(n), col["warn"]))
        caption = []
        y = 6
        for i, (text, colour) in enumerate(lines):
            f = f_main if i == 0 else f_small
            text = _fit_text(d, text, f, w - 12)
            caption.append((text, colour, f, y))
            y += _text_size(d, text, f)[1] + 5

        # key for the road symbols (only when roads were drawn): just above the scale bars,
        # else above the dBZ bar, else top-right or top-left under the caption -- wherever
        # it hides no road symbol and keeps the site marker clear
        if road_kinds:
            dbz_top = y0 - _text_size(d, "65", f_tiny)[1] - 8
            spots = (("bottom-left", bx - 4, scale_top - 5, "bl"),
                     ("bottom-right", w - 6, dbz_top, "br"),
                     ("top-right", w - 6, y + 2, "tr"),
                     ("top-left", 4, y + 2, "tl"))
            try:
                self._draw_road_key(im, road_kinds, f_small, spots, marker)
            except Exception as e:                  # noqa: BLE001 - the rest still matters
                log.warning("road key failed for %s/%s: %s", self.slug, self.theme, e)
            d = ImageDraw.Draw(im)

        # dBZ colour bar, bottom-right, labelled every 15 dBZ
        for k, (thr, rgb) in enumerate(steps):
            d.rectangle([x0 + k * sw, y0, x0 + (k + 1) * sw - 1, y0 + sh], fill=rgb + (255,))
        d.rectangle([x0 - 1, y0 - 1, x0 + bar_w, y0 + sh + 1], outline=stroke)
        for k, (thr, rgb) in enumerate(steps):
            if thr % 15 == 5:                        # 5, 20, 35, 50, 65
                t = "%d" % thr
                tw, th = _text_size(d, t, f_tiny)
                _draw_text(d, (x0 + k * sw + sw / 2.0 - tw / 2.0, y0 - th - 4), t, f_tiny,
                           fill=ink, stroke_width=1, stroke_fill=stroke)
        tw, th = _text_size(d, "dBZ", f_tiny)
        _draw_text(d, (x0 - tw - 6, y0 + sh / 2.0 - th / 2.0 - 1), "dBZ", f_tiny, fill=ink,
                   stroke_width=1, stroke_fill=stroke)

        # caption, top-left: frame time or why there is no radar, then notes
        for text, colour, f, ty in caption:
            _draw_text(d, (8, ty), text, f, fill=colour + (255,), stroke_width=2,
                       stroke_fill=stroke)

    # -- the whole thing -----------------------------------------------------------
    def render(self, radar, alerts, out_path: str, notes: Sequence[str] = (),
               tz: Optional[str] = None, roads=None) -> dict:
        """Write the map PNG. ``radar`` is a ``load_frame`` result (may be not ok);
        ``alerts`` are AlertRecs; ``tz`` (or ``site["tz"]``) adds local time to the caption;
        ``roads`` is a ``roads.site_roads()`` dict (or None): its events and storm reports
        are drawn over the alerts, with a key above the scale bars when any landed on the
        map. Without road items the PNG is exactly the same as without the argument.

        Returns ``{"ok", "error", "path", "drawn_ids", "radar_drawn", "roads_drawn_ids"}``.
        ``ok`` means the file was written; ``error`` is set for a degraded map (radar/tiles
        missing) too."""
        res = {"ok": False, "error": None, "path": out_path, "drawn_ids": [],
               "radar_drawn": False, "roads_drawn_ids": []}
        self._road_key = None
        self._road_marks, self._road_alpha = [], None
        if not deps_available():
            res["error"] = "Pillow not installed"
            return res
        if not isinstance(radar, dict):
            radar = {"ok": False, "error": "no radar result"}
        problems: List[str] = []
        try:
            im = self.basemap().convert("RGBA")
        except Exception as e:                      # noqa: BLE001 - never lose the map
            log.error("basemap failed for %s/%s: %s", self.slug, self.theme, e)
            im = Image.new("RGBA", self.frame.size, self.colors["bg"] + (255,))
            self.basemap_note = "basemap unavailable"
        if self.basemap_note:
            problems.append(self.basemap_note)

        img = radar.get("image")
        if radar.get("ok") and img is not None:
            try:
                im = Image.alpha_composite(im, self._radar_layer(img))
                res["radar_drawn"] = True
            except Exception as e:                  # noqa: BLE001
                log.warning("radar layer failed for %s: %s", self.slug, e)
                radar = dict(radar, ok=False, error=str(e))
        elif radar.get("ok"):
            radar = dict(radar, ok=False, error="no image")
        if not radar.get("ok") and not radar.get("not_applicable"):
            problems.append("radar unavailable: %s" % (radar.get("error") or "no frame"))

        im, res["drawn_ids"] = self._draw_alerts(im, alerts)

        road_kinds: Dict[str, bool] = {}
        if roads:
            try:
                im, res["roads_drawn_ids"], road_kinds = self._draw_roads(im, roads)
            except Exception as e:                  # noqa: BLE001 - map without roads
                log.warning("road layer failed for %s/%s: %s", self.slug, self.theme, e)
                problems.append("road layer failed: %s" % e)
                road_kinds = {}

        try:
            self._decorate(im, radar, [self.basemap_note] + list(notes or ()),
                           tz or _sget(self.site, "tz"), road_kinds=road_kinds)
        except Exception as e:                      # noqa: BLE001 - cosmetics; still write
            log.exception("decorations failed for %s/%s: %s", self.slug, self.theme, e)
            problems.append("decorations failed")

        tmp = "%s.tmp.%d" % (out_path, os.getpid())
        try:
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            with open(tmp, "wb") as f:
                im.convert("RGB").save(f, format="PNG")
                # data on disk before the rename: after a crash the old or the new map is
                # served, never an empty or truncated file
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, out_path)
            res["ok"] = True
        except Exception as e:                      # noqa: BLE001
            log.error("could not write %s: %s", out_path, e)
            problems.insert(0, "write failed: %s" % e)
            try:
                os.remove(tmp)
            except OSError:
                pass
        res["error"] = "; ".join(problems) if problems else None
        return res
