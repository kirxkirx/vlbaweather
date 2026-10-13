"""Radar coverage of each antenna's ring, from MRMS's own GRIB2 files (NOAA Open Data on AWS).

The radar check reads IEM's MRMS frames inside the contiguous US and the NWS composites for
the islands. Both write "no radar covers this cell" the same as "no echo" (IEM's palette
index 0, the WMS's transparent pixel), so a radar outage near an antenna would read as
"clear". MRMS's GRIB2 files of the same products keep the two apart: -999 where no radar
covers the cell, -99 where a radar covers it and sees no echo. This module reads the file of
the frame's time and counts, per antenna, the cells of its ring that no radar covers:

* CONUS: ``SeamlessHSR_00.00``, Seamless Hybrid Scan Reflectivity, the product behind IEM's
  ``lcref`` frames (the same 7000 x 3500 grid of 0.01 deg; a file every 2 min, at :00 s;
  about 1.3 MB), keyed by the IEM frame's time;
* HAWAII, CARIB (GUAM, ALASKA): ``MergedBaseReflectivityQC_00.50``, the product behind the
  NWS composites (20-60 KB), keyed by the composite's frame time, to the second.

A file missing at that time is looked up in the bucket's listing: the nearest one within
``MAX_SKEW_S`` (coverage changes over hours, not minutes), the newest one when the frame's
time is unknown. Only grid template 3.0 (regular lat/lon) and data template 5.41 (PNG) are
read, which is what MRMS writes; anything else is an error, never a guess. Decoding needs
Pillow only (the CONUS grid takes about 100 MB for a fraction of a second).

``coverage`` never raises on network or data problems; it returns ``{"ok", "error",
"domain", "ts", "url", "sites": {slug: {"cells", "uncovered", "covered_pct"}}}``, where
``cells`` counts the grid cells whose centre lies within the ring (the radar ring check's
own great-circle distance) and ``uncovered`` those of them no radar covers, a part of the
ring beyond the grid's edge included.
"""
from __future__ import annotations

import gzip
import io
import logging
import math
import re
import struct
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

try:
    from PIL import Image
except Exception:                       # pragma: no cover - optional dependency
    Image = None

from weather import geo, http, util

from . import radarcheck

log = logging.getLogger("vlba.mrms")

PRODUCTS = {"CONUS": "SeamlessHSR_00.00", "HAWAII": "MergedBaseReflectivityQC_00.50",
            "CARIB": "MergedBaseReflectivityQC_00.50", "GUAM": "MergedBaseReflectivityQC_00.50",
            "ALASKA": "MergedBaseReflectivityQC_00.50"}
# the workspaces of the NWS composites (vlba.nwsradar) and their MRMS domains
REGION_DOMAINS = {"hawaii": "HAWAII", "carib": "CARIB", "guam": "GUAM", "alaska": "ALASKA",
                  "conus": "CONUS"}
MAX_SKEW_S = 300                # a file this close to the frame's time stands in for its own
NO_COVERAGE_MAX = -998.0        # MRMS: -999 = no radar covers the cell (-99: covered, no echo)

_KEY_RE = re.compile(r"<Key>([^<]+)</Key>")
_KEY_TIME_RE = re.compile(r"_(\d{8})-(\d{6})\.grib2(?:\.gz)?$")


# ---- GRIB2 -------------------------------------------------------------------------------------
def _sint(b: bytes) -> int:
    """A GRIB2 signed integer (sign and magnitude, not two's complement)."""
    v = int.from_bytes(b, "big")
    top = 1 << (8 * len(b) - 1)
    return -(v & (top - 1)) if v & top else v


def parse_grib2(raw: bytes) -> Dict[str, Any]:
    """The reference time, the grid, the packing and the PNG payload of the first field of a
    GRIB2 message: ``{"ts", "grid": {"ni", "nj", "la1", "lo1", "di", "dj", "scan"}, "drs":
    {"R", "E", "D", "nbits"}, "payload"}``. ValueError for anything but a regular lat/lon
    grid (template 3.0) with PNG-packed values (template 5.41) and no bitmap."""
    if len(raw) < 16 or raw[:4] != b"GRIB" or raw[7] != 2:
        raise ValueError("not a GRIB2 message")
    total = int.from_bytes(raw[8:16], "big")
    if total > len(raw):
        raise ValueError("GRIB2 message truncated (%d of %d bytes)" % (len(raw), total))
    out: Dict[str, Any] = {}
    pos = 16
    while pos + 5 <= total and raw[pos:pos + 4] != b"7777":
        ln = int.from_bytes(raw[pos:pos + 4], "big")
        num = raw[pos + 4]
        if ln < 5 or pos + ln > total:
            raise ValueError("GRIB2 section %d has a bad length (%d)" % (num, ln))
        sec = raw[pos:pos + ln]
        if num == 1 and ln >= 19:
            year = int.from_bytes(sec[12:14], "big")
            try:
                out["ts"] = datetime(year, sec[14], sec[15], sec[16], sec[17], sec[18], tzinfo=timezone.utc)
            except ValueError:
                out["ts"] = None
        elif num == 3:
            tmpl = int.from_bytes(sec[12:14], "big")
            if tmpl != 0 or ln < 72:
                raise ValueError("grid template 3.%d (only 3.0, regular lat/lon, is read)" % tmpl)
            basic, sub = int.from_bytes(sec[38:42], "big"), int.from_bytes(sec[42:46], "big")
            unit = 1e-6 if basic in (0, 0xFFFFFFFF) or sub in (0, 0xFFFFFFFF) else basic / float(sub)
            out["grid"] = {"ni": int.from_bytes(sec[30:34], "big"), "nj": int.from_bytes(sec[34:38], "big"),
                           "la1": _sint(sec[46:50]) * unit, "lo1": _sint(sec[50:54]) * unit,
                           "di": int.from_bytes(sec[63:67], "big") * unit,
                           "dj": int.from_bytes(sec[67:71], "big") * unit, "scan": sec[71]}
        elif num == 5:
            tmpl = int.from_bytes(sec[9:11], "big")
            if tmpl != 41 or ln < 21:
                raise ValueError("data template 5.%d (only 5.41, PNG, is read)" % tmpl)
            out["drs"] = {"R": struct.unpack(">f", sec[11:15])[0], "E": _sint(sec[15:17]),
                          "D": _sint(sec[17:19]), "nbits": sec[19]}
        elif num == 6:
            if ln < 6 or sec[5] != 255:
                raise ValueError("a bitmap (only fields without one are read)")
        elif num == 7:
            out["payload"] = bytes(sec[5:])
            break                       # the first field is all MRMS writes
        pos += ln
    missing = [k for k in ("grid", "drs", "payload") if k not in out]
    if missing:
        raise ValueError("GRIB2 message without %s" % ", ".join(missing))
    g = out["grid"]
    if g["ni"] < 1 or g["nj"] < 1 or not g["di"] > 0 or not g["dj"] > 0:
        raise ValueError("GRIB2 grid of %d x %d cells of %g x %g deg" % (g["ni"], g["nj"], g["di"], g["dj"]))
    if g["scan"] not in (0x00, 0x40):
        raise ValueError("GRIB2 scanning mode 0x%02x (only 0x00 and 0x40 are read)" % g["scan"])
    out.setdefault("ts", None)
    return out


def decode(meta: Dict[str, Any]):
    """The PNG payload as a Pillow image of ``ni`` x ``nj`` integer pixels."""
    if Image is None:
        raise ValueError("Pillow is not installed")
    g = meta["grid"]
    im = Image.open(io.BytesIO(meta["payload"]))
    if im.format != "PNG":
        raise ValueError("payload is %s, not PNG" % im.format)
    im.load()
    if im.size != (g["ni"], g["nj"]):
        raise ValueError("PNG of %d x %d for a grid of %d x %d" % (im.size + (g["ni"], g["nj"])))
    if im.mode not in ("I", "I;16", "I;16B", "L"):
        raise ValueError("PNG mode %s (a grey scale of 8 or 16 bits is read)" % im.mode)
    return im


def ring_stats(meta: Dict[str, Any], im, lat: float, lon: float, radius_km: float) -> Dict[str, Any]:
    """``{"cells", "uncovered", "covered_pct"}`` of the ring of ``radius_km`` around
    (lat, lon). A ring cell beyond the grid's edge counts as uncovered."""
    g, d = meta["grid"], meta["drs"]
    lo1 = g["lo1"] - 360.0 if g["lo1"] > 180.0 else g["lo1"]
    north_up = not (g["scan"] & 0x40)           # 0x00: rows run north to south
    di, dj, ni, nj = g["di"], g["dj"], g["ni"], g["nj"]
    scale, offset = 2.0 ** d["E"] / 10.0 ** d["D"], d["R"] / 10.0 ** d["D"]
    dlat = radius_km / 110.5 + dj
    dlon = radius_km / (111.3 * max(0.01, math.cos(math.radians(lat)))) + di
    if north_up:
        j0, j1 = math.floor((g["la1"] - (lat + dlat)) / dj), math.ceil((g["la1"] - (lat - dlat)) / dj)
    else:
        j0, j1 = math.floor(((lat - dlat) - g["la1"]) / dj), math.ceil(((lat + dlat) - g["la1"]) / dj)
    i0, i1 = math.floor(((lon - dlon) - lo1) / di), math.ceil(((lon + dlon) - lo1) / di)
    ci0, ci1, cj0, cj1 = max(0, i0), min(ni - 1, i1), max(0, j0), min(nj - 1, j1)
    vals: List[int] = []
    width = ci1 - ci0 + 1
    if ci0 <= ci1 and cj0 <= cj1:
        vals = radarcheck.pixels(im.crop((ci0, cj0, ci1 + 1, cj1 + 1)))
    cells = uncovered = 0
    for j in range(j0, j1 + 1):
        clat = g["la1"] - j * dj if north_up else g["la1"] + j * dj
        for i in range(i0, i1 + 1):
            if geo.haversine_km(lat, lon, clat, lo1 + i * di) > radius_km:
                continue
            cells += 1
            if not (cj0 <= j <= cj1 and ci0 <= i <= ci1):
                uncovered += 1          # beyond the grid: no radar of this domain
            elif offset + vals[(j - cj0) * width + (i - ci0)] * scale <= NO_COVERAGE_MAX:
                uncovered += 1
    pct = round(100.0 * (cells - uncovered) / cells, 1) if cells else None
    return {"cells": cells, "uncovered": uncovered, "covered_pct": pct}


# ---- the bucket --------------------------------------------------------------------------------
def _key(domain: str, product: str, ts: datetime) -> str:
    day = ts.strftime("%Y%m%d")
    return "%s/%s/%s/MRMS_%s_%s.grib2.gz" % (domain, product, day, product, ts.strftime("%Y%m%d-%H%M%S"))


def _listing(cfg, domain: str, product: str, start: datetime, end: datetime) -> List[Tuple[datetime, str]]:
    """``(time, key)`` of every file of the hours from ``start`` to ``end``."""
    out: List[Tuple[datetime, str]] = []
    t = start.replace(minute=0, second=0, microsecond=0)
    while t <= end:
        prefix = "%s/%s/%s/MRMS_%s_%s" % (domain, product, t.strftime("%Y%m%d"), product, t.strftime("%Y%m%d-%H"))
        url = "%s/?list-type=2&prefix=%s" % (cfg.mrms_url, quote(prefix, safe="/"))
        body = http.get_bytes(url, cfg).decode("utf-8", "replace")
        for key in _KEY_RE.findall(body):
            m = _KEY_TIME_RE.search(key)
            if m:
                try:
                    kt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                out.append((kt, key))
        t += timedelta(hours=1)
    return out


def _fetch(cfg, domain: str, product: str, frame_ts: Optional[datetime]) -> Tuple[str, bytes]:
    """``(url, body)`` of the file of ``frame_ts`` (or of the nearest one, see the module
    docstring). HttpError when there is none."""
    if frame_ts is not None:
        ts = frame_ts.astimezone(timezone.utc).replace(microsecond=0)
        url = "%s/%s" % (cfg.mrms_url, _key(domain, product, ts))
        try:
            return url, http.get_bytes(url, cfg)
        except http.HttpSkipped:
            raise
        except http.HttpError as e:
            first = e                   # missing at that time: the nearest one will do
        skew = timedelta(seconds=MAX_SKEW_S)
        near = sorted((abs((t - ts).total_seconds()), t, k)
                      for t, k in _listing(cfg, domain, product, ts - skew, ts + skew)
                      if abs((t - ts).total_seconds()) <= MAX_SKEW_S)
        if not near:
            raise http.HttpError("no %s file within %d min of %s (%s)" % (
                product, MAX_SKEW_S // 60, ts.strftime("%H:%M:%SZ"), first))
        key = near[0][2]
    else:
        now = util.utcnow()
        files = [(t, k) for t, k in _listing(cfg, domain, product, now - timedelta(seconds=2 * MAX_SKEW_S), now)
                 if now - t <= timedelta(seconds=2 * MAX_SKEW_S)]
        if not files:
            raise http.HttpError("no %s file of the last %d min" % (product, 2 * MAX_SKEW_S // 60))
        key = max(files)[1]
    url = "%s/%s" % (cfg.mrms_url, key)
    return url, http.get_bytes(url, cfg)


def coverage(cfg, domain: str, frame_ts: Optional[datetime], sites: Iterable[dict],
             radius_km: float) -> Dict[str, Any]:
    """The radar coverage of the ring of ``radius_km`` around each site (``slug``, ``lat``,
    ``lon``) in the MRMS file of ``domain`` nearest to ``frame_ts``; see the module docstring."""
    res: Dict[str, Any] = {"ok": False, "error": None, "domain": domain, "ts": None, "url": None, "sites": {}}
    product = PRODUCTS.get(domain)
    if product is None:
        res["error"] = "no MRMS product for the domain %r" % (domain,)
        return res
    try:
        url, raw = _fetch(cfg, domain, product, frame_ts)
        res["url"] = url
        meta = parse_grib2(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
        res["ts"] = meta.get("ts")
        im = decode(meta)
        for s in sites:
            stats = ring_stats(meta, im, float(s["lat"]), float(s["lon"]), float(radius_km))
            res["sites"][str(s.get("slug"))] = stats
        res["ok"] = True
        log.info("radar coverage from MRMS %s %s: %s", domain,
                 res["ts"].strftime("%H:%M:%SZ") if res["ts"] else "(time unknown)",
                 ", ".join("%s %s %%" % (k, v["covered_pct"]) for k, v in sorted(res["sites"].items())) or "no site")
    except http.HttpError as e:
        res["error"] = str(e)
    except (ValueError, OSError, EOFError, zlib.error, struct.error, IndexError, ArithmeticError,
            SyntaxError) as e:       # Pillow raises SyntaxError for a broken PNG chunk
        res["error"] = "%s: %s" % (type(e).__name__, e)
    except Exception as e:      # noqa: BLE001 - Pillow's DecompressionBombError and the like
        if Image is not None and isinstance(e, getattr(Image, "DecompressionBombError", ())):
            res["error"] = "%s: %s" % (type(e).__name__, e)
        else:
            raise
    if res["error"]:
        res["sites"] = {}
        log.warning("radar coverage from MRMS %s unavailable: %s", domain, res["error"])
    return res


def site_coverage(cov: Optional[Dict[str, Any]], slug: str) -> Dict[str, Any]:
    """One site's part of a ``coverage`` result, for its radar check: ``{"ok", "error",
    "ts", "cells", "uncovered", "covered_pct"}``."""
    if not isinstance(cov, dict):
        return {"ok": False, "error": "radar coverage not read", "ts": None}
    s = (cov.get("sites") or {}).get(slug)
    if not cov.get("ok") or not isinstance(s, dict):
        return {"ok": False, "error": cov.get("error") or "no coverage for %s" % slug, "ts": cov.get("ts")}
    if s.get("covered_pct") is None:
        return {"ok": False, "error": "the ring holds no grid cell", "ts": cov.get("ts")}
    return dict(s, ok=True, error=None, ts=cov.get("ts"))
