"""The ring scans behind the radar check: is there an echo of at least ``dbz_min`` within
``radius_km`` of a site?

Two rasters are scanned, both carrying reflectivity as MRMS palette indices (``weather.geo``:
``dbz = -32 + i / 2``, 255 = no data):

* ``mrms_ring``: the IEM MRMS composite itself (7000 x 3500 cells of 0.01 deg, plate
  carree), for the sites inside the grid: the cells of the bounding box of the ring are
  visited and each one's great-circle distance decides. This is ttustatus's ``check_rain``,
  with the parent's grid constants.
* ``index_ring``: an "L" image in the pixel space of a ``weather.geo.MapFrame`` (what
  ``vlba.nwsradar`` decodes from the NWS composites for the island sites): a pixel's
  distance comes from its lon/lat through the frame's inverse projection, and the pixel's
  footprint is about ``frame.km_per_px`` (1.3 km at 100 miles / 600 px), so the ring
  boundary is coarse by that much.

Both return ``{"in_ring": bool, "nearest_km": float|None, "count": int, "max_dbz": float|None}``
and never raise on finite coordinates; ``count`` is the number of cells / pixels at or above
the threshold inside the ring, ``nearest_km`` and ``max_dbz`` describe those cells.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

from weather import geo

CLEAR: Dict[str, Any] = {"in_ring": False, "nearest_km": None, "count": 0, "max_dbz": None}


def _result(nearest: Optional[float], count: int, max_dbz: Optional[float]) -> Dict[str, Any]:
    return {"in_ring": count > 0, "nearest_km": None if nearest is None else round(nearest, 1),
            "count": count, "max_dbz": max_dbz}


def pixels(im) -> list:
    """The pixel values of ``im``, row by row (an int per pixel of a single band, a tuple of
    several): ``get_flattened_data`` where Pillow has it (12.1 on; ``getdata`` is deprecated
    there and goes in Pillow 14), ``getdata`` in the older Pillow of RHEL 9 or Gentoo."""
    flat = getattr(im, "get_flattened_data", None)
    return list(flat() if flat is not None else im.getdata())


def mrms_ring(img, lat: float, lon: float, radius_km: float, dbz_min: float) -> Dict[str, Any]:
    """Scan the MRMS composite ``img`` (a Pillow "P" or "L" image of ``geo.MRMS_W`` x
    ``geo.MRMS_H``) within ``radius_km`` of (lat, lon). A site outside the grid, or a ring
    that reaches no cell, is clear with ``count`` 0 (the caller knows from the frame bbox
    whether the site is covered at all)."""
    if img is None or img.size != (geo.MRMS_W, geo.MRMS_H):
        return dict(CLEAR)
    px = img.load()
    coslat = max(1e-6, math.cos(math.radians(lat)))
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * coslat)
    c0, r0 = geo.mrms_cell(lat, lon)
    dc = int(dlon / geo.MRMS_PX) + 1
    dr = int(dlat / geo.MRMS_PX) + 1
    nearest: Optional[float] = None
    max_dbz: Optional[float] = None
    count = 0
    for r in range(max(0, r0 - dr), min(geo.MRMS_H, r0 + dr + 1)):
        cell_lat = geo.MRMS_TOP - (r + 0.5) * geo.MRMS_PX
        for c in range(max(0, c0 - dc), min(geo.MRMS_W, c0 + dc + 1)):
            v = px[c, r]
            v = v if isinstance(v, int) else v[0]
            d = geo.mrms_dbz(v)
            if d is None or d < dbz_min:
                continue
            cell_lon = geo.MRMS_LEFT + (c + 0.5) * geo.MRMS_PX
            dist = geo.haversine_km(lat, lon, cell_lat, cell_lon)
            if dist <= radius_km:
                count += 1
                if nearest is None or dist < nearest:
                    nearest = dist
                if max_dbz is None or d > max_dbz:
                    max_dbz = d
    return _result(nearest, count, max_dbz)


def index_ring(idx, frame, lat: float, lon: float, radius_km: float, dbz_min: float) -> Dict[str, Any]:
    """Scan an index image in ``frame``'s pixel space (same size as ``frame.size``) within
    ``radius_km`` of (lat, lon). Pixels whose centre lies within the ring count."""
    if idx is None or frame is None or tuple(idx.size) != tuple(frame.size):
        return dict(CLEAR)
    px = idx.load()
    w, h = frame.size
    cx, cy = frame.lonlat_to_px(lon, lat)
    rad_px = frame.px_for_km(radius_km) + 1.0
    x0, x1 = max(0, int(cx - rad_px)), min(w - 1, int(cx + rad_px) + 1)
    y0, y1 = max(0, int(cy - rad_px)), min(h - 1, int(cy + rad_px) + 1)
    nearest: Optional[float] = None
    max_dbz: Optional[float] = None
    count = 0
    for j in range(y0, y1 + 1):
        for i in range(x0, x1 + 1):
            v = px[i, j]
            v = v if isinstance(v, int) else v[0]
            d = geo.mrms_dbz(v)
            if d is None or d < dbz_min:
                continue
            plon, plat = frame.px_to_lonlat(i + 0.5, j + 0.5)
            dist = geo.haversine_km(lat, lon, plat, plon)
            if dist <= radius_km:
                count += 1
                if nearest is None or dist < nearest:
                    nearest = dist
                if max_dbz is None or d > max_dbz:
                    max_dbz = d
    return _result(nearest, count, max_dbz)


def mask_ring(mask, frame, lat: float, lon: float, radius_km: float) -> int:
    """How many non-zero pixels of ``mask`` (an "L" image in ``frame``'s pixel space) have
    their centre within ``radius_km`` of (lat, lon); 0 for no mask."""
    if mask is None or frame is None or tuple(mask.size) != tuple(frame.size):
        return 0
    px = mask.load()
    w, h = frame.size
    cx, cy = frame.lonlat_to_px(lon, lat)
    rad_px = frame.px_for_km(radius_km) + 1.0
    n = 0
    for j in range(max(0, int(cy - rad_px)), min(h - 1, int(cy + rad_px) + 1) + 1):
        for i in range(max(0, int(cx - rad_px)), min(w - 1, int(cx + rad_px) + 1) + 1):
            if px[i, j] and geo.haversine_km(lat, lon, *reversed(frame.px_to_lonlat(i + 0.5, j + 0.5))) <= radius_km:
                n += 1
    return n


def ring_inside(frame, lat: float, lon: float, radius_km: float) -> bool:
    """Whether the whole ring lies inside ``frame`` (a frame-space raster such as the NWS
    composites covers the map only: a ring reaching past its edge is partly unseen)."""
    if frame is None:
        return False
    w, h = frame.size
    cx, cy = frame.lonlat_to_px(lon, lat)
    r = frame.px_for_km(radius_km)
    return cx - r >= 0 and cy - r >= 0 and cx + r <= w and cy + r <= h


def dbz_index(dbz: Optional[float]) -> int:
    """The MRMS palette index of a reflectivity (``geo.MRMS_NODATA`` for None), rounded to
    the half-dBZ step and clamped to 0..254."""
    if dbz is None or (isinstance(dbz, float) and math.isnan(dbz)):
        return geo.MRMS_NODATA
    return max(0, min(254, int(round((float(dbz) + 32.0) * 2.0))))
