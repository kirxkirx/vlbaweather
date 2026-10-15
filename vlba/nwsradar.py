"""Radar for the antennas outside the MRMS grid (Mauna Kea, St. Croix): the NWS regional
radar composites that radar.weather.gov shows, served by the NWS GeoServer at
opengeo.ncep.noaa.gov (WMS 1.3.0; layers ``hawaii_bref_qcd``, ``carib_bref_qcd``, ...,
"Quality Controlled 1km x 1km ... Radar Base Reflectivity ... MRMS algorithm", a frame every
2 minutes).

Why it looks the way it does (all checked 2026-10-08):

* The server has no reflectivity *values* to give: the coverage is a pre-coloured RGBA
  raster (GetFeatureInfo answers with RED/GREEN/BLUE/ALPHA bands, WCS serves the same four
  bands, and a dynamic ``SLD_BODY`` style is "forbidden"). So the map is requested in the
  frame's own Web-Mercator box (``EPSG:3857``, ``frame.size`` pixels: the raster is then
  pasted straight onto the basemap, no reprojection) and each pixel's colour is decoded
  back to dBZ with the radar.weather.gov reflectivity colour scale (``PALETTE``): the
  nearest point on the ramp in RGB space, interpolated between its stops, rejected beyond
  ``COLOR_TOLERANCE``. The decoded values go into an "L" image in the MRMS index encoding
  (``weather.geo``: dbz = -32 + i/2, 255 = no data), which both the ring check
  (``vlba.radarcheck.index_ring``) and the drawn layer (re-coloured in the parent's dBZ
  palette, so every map on the page reads the same) use.
* The frame time comes from the workspace's own GetCapabilities (18 KB, against 2.2 MB for
  the whole server): the ``time`` dimension's ``default`` is the latest frame, the one a
  GetMap without ``TIME`` returns. A failed capabilities fetch costs the time stamp, not
  the map ("frame time unknown"). The dimension also lists the frames of the last two
  hours (checked 2026-10-14: every 2 min); ``earlier_times`` and ``fetch_at`` give the
  ones before the latest (by ``TIME``), so a run can confirm an echo by itself.
* The last good PNG is cached per site and reused for ``cfg.nwsradar_max_stale`` seconds
  when the server is unreachable (labelled stale), like the MRMS frame in the parent.
* ``region_for`` picks the composite whose bounding box (from the capabilities) meets the
  map; the CONUS composite is listed last so it is only chosen when nothing else covers a
  map (the CONUS antennas use MRMS through the parent, not this module).
* Nothing here raises on network or data problems: ``fetch`` returns the parent's status
  keys and the generator falls back to "radar unavailable" for the site.
"""
from __future__ import annotations

import io
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

try:
    from PIL import Image
except Exception:                       # pragma: no cover - optional dependency
    Image = None

from weather import geo, http, radar, util

from . import radarcheck

log = logging.getLogger("vlba.nwsradar")

# The composites, each with its WMS workspace, layer, the label the page and the map
# caption use, and the geographic box the capabilities document gives for it.
REGIONS: Tuple[Dict[str, Any], ...] = (
    {"ws": "hawaii", "layer": "hawaii_bref_qcd", "label": "NWS Hawaii composite",
     "bbox": (-164.0, 15.0, -151.0, 26.0)},
    {"ws": "carib", "layer": "carib_bref_qcd", "label": "NWS Caribbean composite",
     "bbox": (-90.0, 10.0, -60.0, 25.0)},
    {"ws": "guam", "layer": "guam_bref_qcd", "label": "NWS Guam composite",
     "bbox": (140.0, 9.0, 150.0, 18.0)},
    {"ws": "alaska", "layer": "alaska_bref_qcd", "label": "NWS Alaska composite",
     "bbox": (-176.0, 50.0, -126.0, 72.0)},
    {"ws": "conus", "layer": "conus_bref_qcd", "label": "NWS CONUS composite",
     "bbox": (-130.0, 20.0, -60.0, 55.0)},
)

# The RIDGE II reflectivity colour scale: what radar.weather.gov's composites and single-
# site products are painted with. No published table exists (the GeoServer style is an empty
# RasterSymbolizer, the legend graphic a generic gradient); the stops were derived on
# 2026-10-08 by matching the MRMS source grids (the PNG-packed GRIB2 files on
# mrms.ncep.noaa.gov, same 0.005 deg grid and time stamp) pixel for pixel with the RGBA
# GeoTIFFs the server redistributes (its DataURL): every value maps to exactly one colour,
# linearly interpolated between the stops below (12.5 dBZ -> (81,134,183), 47.5 -> (255,89,0)
# are exact midpoints; CREF values at 0.1 dBZ give intermediate colours), with no blending
# at any GetMap scale and alpha 255 for every valid value (negative dBZ included; only
# "no echo" and "no coverage" are transparent). The same table paints the CONUS composites
# and the Level III SR_BREF products. Verified -19.5 .. 59 dBZ; the stops at 60 (white) and
# above are inferred from the station legend bars (pink, magenta, purple, cyan) and from the
# straight line 55 -> 60 measured up to 59 dBZ: anything that bright is a hail core, far above
# the 20 dBZ of the ring check, so an error of a few dBZ there changes nothing.
PALETTE: Tuple[Tuple[float, Tuple[int, int, int]], ...] = (
    (-30.0, (132, 110, 144)),
    (-25.0, (141, 129, 128)),
    (-20.0, (150, 145, 84)),
    (-10.0, (210, 212, 180)),
    (-5.0, (176, 182, 180)),
    (0.0, (148, 155, 181)),
    (5.0, (99, 118, 168)),
    (10.0, (67, 94, 159)),
    (15.0, (94, 173, 207)),
    (20.0, (82, 214, 162)),
    (25.0, (14, 214, 20)),
    (30.0, (11, 136, 15)),
    (35.0, (9, 94, 9)),
    (40.0, (255, 226, 0)),
    (45.0, (255, 177, 0)),
    (50.0, (255, 0, 0)),
    (55.0, (177, 0, 0)),
    (60.0, (255, 255, 255)),
    (65.0, (255, 160, 255)),
    (70.0, (255, 10, 230)),
    (75.0, (171, 0, 254)),
    (80.0, (0, 225, 236)),
)
COLOR_TOLERANCE = 24.0                  # RGB distance beyond which a colour is "not radar"
MIN_ALPHA = 64                          # pixels fainter than this are treated as empty
MAX_COLORS = 1 << 18                    # distinct colours a composite may hold (safety cap)
CAPS_TIMEOUT = 15.0
MERC_R = 20037508.342789244            # EPSG:3857 half-extent, metres


# ---- geometry and URLs ----------------------------------------------------------------
def mercator(lon: float, lat: float) -> Tuple[float, float]:
    """WGS84 lon/lat -> EPSG:3857 metres (the box the WMS wants)."""
    lat = max(-85.05112878, min(85.05112878, lat))
    x = lon * MERC_R / 180.0
    y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) / (math.pi / 180.0) * MERC_R / 180.0
    return x, y


def region_for(frame_bbox: Optional[Sequence[float]]) -> Optional[Dict[str, Any]]:
    """The first composite whose box meets the map's (lonmin, latmin, lonmax, latmax)."""
    if not frame_bbox or len(frame_bbox) != 4:
        return None
    try:
        box = tuple(float(v) for v in frame_bbox)
    except (TypeError, ValueError):
        return None
    for region in REGIONS:
        if geo.bbox_intersects(box, region["bbox"]):
            return region
    return None


def _iso_time(t: datetime) -> str:
    """A frame time as the time dimension lists it: ``2026-10-14T19:32:55.000Z``."""
    t = t.astimezone(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S") + ".%03dZ" % (t.microsecond // 1000)


def getmap_url(cfg, region: Dict[str, Any], frame, at: Optional[datetime] = None) -> str:
    """The GetMap request for the frame's box; ``at``: that frame of the time dimension (no
    ``TIME``: the latest)."""
    x0, y0 = mercator(frame.west, frame.south)
    x1, y1 = mercator(frame.east, frame.north)
    w, h = frame.size
    q = [("SERVICE", "WMS"), ("VERSION", "1.3.0"), ("REQUEST", "GetMap"),
         ("LAYERS", "%s:%s" % (region["ws"], region["layer"])), ("STYLES", ""),
         ("CRS", "EPSG:3857"), ("BBOX", "%.3f,%.3f,%.3f,%.3f" % (x0, y0, x1, y1)),
         ("WIDTH", str(int(w))), ("HEIGHT", str(int(h))), ("FORMAT", "image/png"),
         ("TRANSPARENT", "true")]
    if at is not None:
        q.append(("TIME", _iso_time(at)))
    return "%s/%s/ows?%s" % (cfg.nwsradar_url.rstrip("/"), region["ws"], urlencode(q))


def capabilities_url(cfg, region: Dict[str, Any]) -> str:
    return "%s/%s/ows?service=WMS&version=1.3.0&request=GetCapabilities" % (
        cfg.nwsradar_url.rstrip("/"), region["ws"])


_DIM_RE = re.compile(r'<Dimension\s+name="time"[^>]*\bdefault="([^"]+)"', re.IGNORECASE)
_DIM_LIST_RE = re.compile(r'<Dimension\s+name="time"[^>]*>([^<]*)</Dimension>', re.IGNORECASE)


def _layer_chunk(xml_text: Any, layer: str) -> Optional[str]:
    if not isinstance(xml_text, str):
        return None
    i = xml_text.find("<Name>%s</Name>" % layer)
    if i < 0:
        return None
    j = xml_text.find("</Layer>", i)
    return xml_text[i:j if j > 0 else None]


def parse_frame_time(xml_text: str, layer: str) -> Optional[datetime]:
    """The ``default`` of the layer's time dimension in a capabilities document (the latest
    frame), as an aware UTC datetime; None when it is not there."""
    chunk = _layer_chunk(xml_text, layer)
    m = _DIM_RE.search(chunk) if chunk else None
    if not m:
        return None
    return util.parse_iso(m.group(1).strip())


def parse_frame_times(xml_text: str, layer: str) -> List[datetime]:
    """Every time the layer's time dimension lists, oldest first (aware UTC datetimes); an
    empty list when there is none, or only a range (``start/end/period``), not a list."""
    chunk = _layer_chunk(xml_text, layer)
    m = _DIM_LIST_RE.search(chunk) if chunk else None
    if not m:
        return []
    out = []
    for item in m.group(1).split(","):
        item = item.strip()
        t = util.parse_iso(item) if item and "/" not in item else None
        if t is not None:
            out.append(t)
    return sorted(set(out))


def frame_time(cfg, region: Dict[str, Any]) -> Tuple[Optional[datetime], Optional[str]]:
    """(latest frame time, error text)."""
    ts, _times, err = frame_times(cfg, region)
    return ts, err


def frame_times(cfg, region: Dict[str, Any]) -> Tuple[Optional[datetime], List[datetime], Optional[str]]:
    """(latest frame time, every listed frame time oldest first, error text), from one
    capabilities document."""
    url = capabilities_url(cfg, region)
    try:
        data = http.get_bytes(url, cfg, accept="text/xml", timeout=CAPS_TIMEOUT)
    except http.HttpError as e:
        return None, [], "capabilities: %s" % str(e).replace(url, "GetCapabilities")
    except Exception as e:              # noqa: BLE001 - the time stamp is not worth a failure
        return None, [], "capabilities: %s: %s" % (type(e).__name__, e)
    text = data.decode("utf-8", "replace")
    ts = parse_frame_time(text, region["layer"])
    if ts is None:
        return None, [], "capabilities: no time dimension for %s" % region["layer"]
    return ts, parse_frame_times(text, region["layer"]), None


# ---- colour decoding --------------------------------------------------------------------
def _segment_dbz(rgb: Tuple[int, int, int], palette) -> Tuple[Optional[float], float]:
    """(dBZ, distance): the nearest point on the ramp's polyline in RGB space, the dBZ
    interpolated along the segment; (None, inf) for an empty palette."""
    best: Tuple[Optional[float], float] = (None, float("inf"))
    r, g, b = rgb
    for k in range(len(palette)):
        d0, c0 = palette[k]
        if k + 1 < len(palette):
            d1, c1 = palette[k + 1]
            vx, vy, vz = c1[0] - c0[0], c1[1] - c0[1], c1[2] - c0[2]
            denom = vx * vx + vy * vy + vz * vz
            t = 0.0
            if denom > 0:
                t = ((r - c0[0]) * vx + (g - c0[1]) * vy + (b - c0[2]) * vz) / float(denom)
                t = max(0.0, min(1.0, t))
            px, py, pz = c0[0] + t * vx, c0[1] + t * vy, c0[2] + t * vz
            dist = math.sqrt((r - px) ** 2 + (g - py) ** 2 + (b - pz) ** 2)
            dbz = d0 + t * (d1 - d0)
        else:
            dist = math.sqrt((r - c0[0]) ** 2 + (g - c0[1]) ** 2 + (b - c0[2]) ** 2)
            dbz = float(d0)
        if dist < best[1]:
            best = (dbz, dist)
    return best


def decode(img, palette=None, tolerance: float = COLOR_TOLERANCE, with_mask: bool = False):
    """An RGBA composite -> ("L" image of MRMS indices, counts): every distinct opaque
    colour is matched once (the ramp is continuous, so a composite holds a few hundred to
    a few thousand colours), then the pixels are mapped through the table. Counts:
    ``decoded`` / ``unknown`` distinct colours, ``unknown_px`` pixels left as no data. With
    ``with_mask`` a third item, an "L" image that is 255 where a pixel's colour was not
    recognised: such a pixel may be an echo of any strength, never "no echo"."""
    palette = tuple(palette) if palette else PALETTE
    rgba = img.convert("RGBA")
    w, h = rgba.size
    colors = rgba.getcolors(MAX_COLORS) or []
    table: Dict[Tuple[int, int, int, int], int] = {}
    decoded = unknown = unknown_px = 0
    for n, c in colors:
        if c[3] < MIN_ALPHA:
            table[c] = geo.MRMS_NODATA
            continue
        dbz, dist = _segment_dbz(c[:3], palette)
        if dbz is None or dist > tolerance:
            table[c] = geo.MRMS_NODATA
            unknown += 1
            unknown_px += n
            continue
        table[c] = radarcheck.dbz_index(dbz)
        decoded += 1
    idx = Image.new("L", (w, h), geo.MRMS_NODATA)
    idx.putdata([table.get(c, geo.MRMS_NODATA) for c in radarcheck.pixels(rgba)])
    counts = {"decoded": decoded, "unknown": unknown, "unknown_px": unknown_px}
    if not with_mask:
        return idx, counts
    mask = Image.new("L", (w, h), 0)
    if unknown_px:
        bad = {c for n, c in colors if c[3] >= MIN_ALPHA and table.get(c) == geo.MRMS_NODATA}
        mask.putdata([255 if c in bad else 0 for c in radarcheck.pixels(rgba)])
    return idx, counts, mask


def layer_from_index(idx, cfg):
    """The drawn radar layer: the index image coloured with the parent's dBZ palette at
    ``cfg.radar_alpha``, transparent below the drawing floor and where there is no data."""
    floor = max(radar.DBZ_FLOOR, float(getattr(cfg, "radar_dbz_min", radar.DBZ_FLOOR)))
    alpha = int(getattr(cfg, "radar_alpha", 170))
    lut = radar._index_lut(floor, alpha)
    rgb = Image.new("RGBA", idx.size, (0, 0, 0, 0))
    src = radarcheck.pixels(idx)
    rgb.putdata([lut[v] if lut[v] is not None else (0, 0, 0, 0) for v in src])
    return rgb


# ---- the fetch --------------------------------------------------------------------------
def _cache_key(site: dict) -> str:
    return "nwsradar_%s" % site.get("slug", "site")


def _decoded_result(cfg, data: bytes, frame, region, res: dict, layer: bool = True) -> dict:
    """Decode PNG bytes into ``res`` (``image`` unless ``layer`` is False, ``index``,
    ``unknown_mask``, ``colors``); ValueError when the bytes are not a usable map of the
    frame's size."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise ValueError("not an image (%s)" % (str(e).strip() or type(e).__name__))
    if tuple(img.size) != tuple(frame.size):
        raise ValueError("unexpected map size %dx%d (wanted %dx%d)" % (img.size + frame.size))
    idx, counts, mask = decode(img, with_mask=True)
    res["index"] = idx
    res["unknown_mask"] = mask if counts.get("unknown_px") else None
    res["image"] = layer_from_index(idx, cfg) if layer else None
    res["colors"] = counts
    return res


def fetch(cfg, cache, site: dict, frame, now: Optional[datetime] = None) -> dict:
    """The composite for one site's map frame, decoded.

    Returns ``{"ok", "stale", "error", "age_s", "ts", "url", "label", "source", "region",
    "image" (RGBA layer of frame.size or None), "index" ("L" image or None), "unknown_mask"
    ("L" image of the pixels whose colour was not recognised, or None), "colors", "times"
    (the frame times the capabilities list, oldest first, for ``earlier_times``)}``; never
    raises. ``ok`` with ``stale`` True is the last good copy (within
    ``cfg.nwsradar_max_stale``, and only for the same map request) after a failed fetch."""
    res: Dict[str, Any] = {"ok": False, "stale": False, "error": None, "age_s": None, "ts": None,
                           "url": None, "label": "NWS radar composite", "source": None, "region": None,
                           "image": None, "index": None, "unknown_mask": None, "colors": None,
                           "times": []}
    if Image is None:
        res["error"] = "Pillow not installed"
        return res
    if frame is None:
        res["error"] = "no map frame"
        return res
    region = region_for(getattr(frame, "bbox", None))
    if region is None:
        res["error"] = "no NWS radar composite covers this map"
        return res
    res.update(label=region["label"], source=region["layer"], region=region["ws"])
    key = _cache_key(site)
    ts, times, ts_err = frame_times(cfg, region)
    res["times"] = times
    url = getmap_url(cfg, region, frame)
    res["url"] = url
    err = None
    try:
        data = http.get_bytes(url, cfg, accept="image/png")
        _decoded_result(cfg, data, frame, region, res)
    except (http.HttpError, ValueError) as e:
        err = str(e).replace(url, "GetMap")             # the URL would fill the map caption
    except Exception as e:              # noqa: BLE001 - any surprise = unavailable, never a crash
        err = "%s: %s" % (type(e).__name__, e)
    if err is None:
        res.update(ok=True, stale=False, age_s=0.0, ts=ts,
                   error=ts_err if ts_err else None)
        try:
            cache.put_bytes(key, ".png", data)
            cache.put(key + "_meta", {"ts": ts.isoformat() if ts else None, "url": url,
                                      "region": region["ws"]})
        except Exception as e:          # noqa: BLE001 - the cache is an optimisation
            log.warning("could not cache the %s composite: %s", site.get("slug"), e)
        c = res["colors"] or {}
        log.info("%s: %s %s decoded (%d colours%s)", site.get("slug"), region["label"],
                 ts.strftime("%H:%MZ") if ts else "(time unknown)", c.get("decoded", 0),
                 ", %d unknown over %d px" % (c["unknown"], c["unknown_px"]) if c.get("unknown") else "")
        if ts_err:
            log.warning("%s: %s", site.get("slug"), ts_err)
        return res
    log.warning("%s: %s unavailable: %s", site.get("slug"), region["label"], err)
    res["error"] = err
    meta, age = cache.get_any(key + "_meta")
    data = cache.get_bytes(key, ".png")
    res["age_s"] = age
    if data is None or not isinstance(meta, dict) or age is None or age > cfg.nwsradar_max_stale:
        return res
    if meta.get("url") != url:
        # drawn for another map (a moved antenna, another WEATHER_MAP_MILES): its echoes
        # would land in the wrong places, inside or outside the ring
        log.info("%s: cached composite is for another map extent; not used", site.get("slug"))
        return res
    try:
        _decoded_result(cfg, data, frame, region, res)
    except ValueError as e:
        log.warning("%s: cached composite unusable: %s", site.get("slug"), e)
        return res
    res.update(ok=True, stale=True, ts=util.parse_iso(meta.get("ts")), url=meta.get("url") or url)
    log.info("%s: using the cached %s from %s (%s old)", site.get("slug"), region["label"],
             meta.get("ts"), util.fmt_age(age))
    return res


def _region(newest: dict) -> Optional[Dict[str, Any]]:
    return next((r for r in REGIONS if r["ws"] == newest.get("region")), None)


def earlier_times(newest: dict, oldest: Optional[datetime] = None) -> List[datetime]:
    """The frame times the capabilities list before ``newest["ts"]``, newest first, none
    before ``oldest``; empty without a fresh latest frame of known time (``newest``: a
    ``fetch`` result)."""
    ts = newest.get("ts") if isinstance(newest, dict) else None
    if not isinstance(ts, datetime) or not newest.get("ok") or newest.get("stale") or _region(newest) is None:
        return []
    return sorted((t for t in (newest.get("times") or [])
                   if isinstance(t, datetime) and t < ts and (oldest is None or t >= oldest)), reverse=True)


def fetch_at(cfg, frame, newest: dict, at: datetime) -> Dict[str, Any]:
    """One listed frame of ``newest``'s composite for the same map, fetched with ``TIME`` and
    decoded like the latest one but without the drawn layer (only the ring check reads it):
    ``{"ok", "ts", "index", "unknown_mask", "colors", "error"}``. Nothing is cached and nothing
    raises."""
    r: Dict[str, Any] = {"ok": False, "ts": at, "index": None, "unknown_mask": None, "colors": None,
                         "error": None, "image": None}
    region = _region(newest) if isinstance(newest, dict) else None
    if region is None or frame is None or Image is None:
        r["error"] = "no composite to ask"
        return r
    url = getmap_url(cfg, region, frame, at=at)
    try:
        _decoded_result(cfg, http.get_bytes(url, cfg, accept="image/png"), frame, region, r, layer=False)
        r["ok"] = True
    except (http.HttpError, ValueError) as e:
        r["error"] = str(e).replace(url, "GetMap")
    except Exception as e:              # noqa: BLE001 - any surprise = that frame unavailable
        r["error"] = "%s: %s" % (type(e).__name__, e)
    return r
