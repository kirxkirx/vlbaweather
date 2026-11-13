"""Geometry for local-weather-awareness: Web-Mercator/tile maths, the MRMS grid, a fixed-size
map frame centred on a site, and GeoJSON polygon helpers (point-in-polygon, bounding boxes).

Pure Python, no numpy/shapely. All lon/lat are WGS84 decimal degrees. GeoJSON order is
(lon, lat) and boxes are (lonmin, latmin, lonmax, latmax).
"""
from __future__ import annotations

import math
import numbers
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

MI_TO_KM = 1.609344
EARTH_RADIUS_KM = 6371.0088
TILE_SIZE = 256

# ---- MRMS composite-reflectivity raster (IEM archive, EPSG:4326, 0.01° cells) -----------
# World file: pixel 0.01°, upper-left pixel CENTRE at (-129.995, 54.995) => the grid's
# upper-left CORNER is (-130.0, 55.0). Palette index 255 = no data, else dBZ = -32 + i*0.5.
MRMS_W, MRMS_H = 7000, 3500
MRMS_PX = 0.01
MRMS_LEFT, MRMS_TOP = -130.0, 55.0
MRMS_NODATA = 255
# The grid's extent (lonmin, latmin, lonmax, latmax): the contiguous US plus a margin. A map
# wholly outside it (Alaska, Hawaii, Puerto Rico, ...) can never show radar echoes.
MRMS_BBOX = (MRMS_LEFT, MRMS_TOP - MRMS_H * MRMS_PX, MRMS_LEFT + MRMS_W * MRMS_PX, MRMS_TOP)


def mrms_cell(lat: float, lon: float) -> Tuple[int, int]:
    """(col, row) of the MRMS cell containing lon/lat. May be out of range: callers must
    check ``mrms_in_grid``."""
    return (int(math.floor((lon - MRMS_LEFT) / MRMS_PX)),
            int(math.floor((MRMS_TOP - lat) / MRMS_PX)))


def mrms_in_grid(col: int, row: int) -> bool:
    return 0 <= col < MRMS_W and 0 <= row < MRMS_H


def mrms_covers(lat: float, lon: float, margin_deg: float = 0.0) -> bool:
    return (MRMS_LEFT + margin_deg <= lon <= MRMS_LEFT + MRMS_W * MRMS_PX - margin_deg
            and MRMS_TOP - MRMS_H * MRMS_PX + margin_deg <= lat <= MRMS_TOP - margin_deg)


def mrms_dbz(index: int) -> Optional[float]:
    """Palette index -> dBZ (None for no-data)."""
    if index is None or index == MRMS_NODATA or index < 0:
        return None
    return -32.0 + index * 0.5


# ---- spherical helpers ---------------------------------------------------------------
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def dest_point(lat: float, lon: float, dist_km: float, bearing_deg: float) -> Tuple[float, float]:
    """Point ``dist_km`` from (lat, lon) along ``bearing_deg`` (0 = north, 90 = east)."""
    br, p1, l1 = math.radians(bearing_deg), math.radians(lat), math.radians(lon)
    dr = dist_km / EARTH_RADIUS_KM
    p2 = math.asin(math.sin(p1) * math.cos(dr) + math.cos(p1) * math.sin(dr) * math.cos(br))
    l2 = l1 + math.atan2(math.sin(br) * math.sin(dr) * math.cos(p1),
                         math.cos(dr) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


# ---- Web Mercator / slippy tiles ------------------------------------------------------
def deg2num(lat: float, lon: float, zoom: int) -> Tuple[float, float]:
    """Fractional tile coordinates (x, y) at ``zoom``."""
    lat = max(-85.05112878, min(85.05112878, lat))
    n = 2.0 ** zoom
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def num2deg(x: float, y: float, zoom: int) -> Tuple[float, float]:
    """Fractional tile coordinates -> (lat, lon)."""
    n = 2.0 ** zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lat, lon


class MapFrame:
    """A Web-Mercator canvas exactly ``size_km`` wide and tall, centred on (lat0, lon0).

    Width is ``width_px``; height follows the Mercator aspect so that px/km is equal on
    both axes at the centre (a circle is a circle, one scale bar is valid in every
    direction). Extent: ``size_km/2`` measured along the meridian (north/south) and along
    the parallel at the centre latitude (east/west).
    """

    def __init__(self, lat0: float, lon0: float, size_km: float, width_px: int, zoom: int):
        if size_km <= 0 or width_px <= 0:
            raise ValueError("size_km and width_px must be positive")
        self.lat0, self.lon0 = float(lat0), float(lon0)
        self.size_km = float(size_km)
        self.zoom = int(zoom)
        half = self.size_km / 2.0
        self.north = dest_point(lat0, lon0, half, 0.0)[0]
        self.south = dest_point(lat0, lon0, half, 180.0)[0]
        self.east = dest_point(lat0, lon0, half, 90.0)[1]
        self.west = dest_point(lat0, lon0, half, 270.0)[1]
        x0, y0 = deg2num(self.north, self.west, self.zoom)
        x1, y1 = deg2num(self.south, self.east, self.zoom)
        # global pixel coordinates of the canvas at this zoom (floats: no tile snapping)
        self.gx0, self.gy0 = x0 * TILE_SIZE, y0 * TILE_SIZE
        self.gx1, self.gy1 = x1 * TILE_SIZE, y1 * TILE_SIZE
        self.width = int(width_px)
        self.height = max(1, int(round(self.width * (self.gy1 - self.gy0) / (self.gx1 - self.gx0))))

    # -- coordinate transforms
    def lonlat_to_px(self, lon: float, lat: float) -> Tuple[float, float]:
        xf, yf = deg2num(lat, lon, self.zoom)
        return ((xf * TILE_SIZE - self.gx0) / (self.gx1 - self.gx0) * self.width,
                (yf * TILE_SIZE - self.gy0) / (self.gy1 - self.gy0) * self.height)

    def px_to_lonlat(self, x: float, y: float) -> Tuple[float, float]:
        gx = self.gx0 + x / self.width * (self.gx1 - self.gx0)
        gy = self.gy0 + y / self.height * (self.gy1 - self.gy0)
        lat, lon = num2deg(gx / TILE_SIZE, gy / TILE_SIZE, self.zoom)
        return lon, lat

    def ring_to_px(self, ring: Sequence[Sequence[float]]) -> List[Tuple[float, float]]:
        return [self.lonlat_to_px(pt[0], pt[1]) for pt in ring]

    # -- extents
    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        """(lonmin, latmin, lonmax, latmax)"""
        return (self.west, self.south, self.east, self.north)

    @property
    def size(self) -> Tuple[int, int]:
        return (self.width, self.height)

    @property
    def km_per_px(self) -> float:
        """East-west scale at the centre latitude."""
        return self.size_km / self.width

    def px_for_km(self, km: float) -> float:
        """Length in pixels of ``km`` measured east-west from the centre."""
        x1, _ = self.lonlat_to_px(self.lon0, self.lat0)
        lat, lon = dest_point(self.lat0, self.lon0, km, 90.0)
        x2, _ = self.lonlat_to_px(lon, lat)
        return abs(x2 - x1)

    def tile_range(self) -> Tuple[int, int, int, int]:
        """Inclusive (tx0, ty0, tx1, ty1) of the tiles that cover the canvas at self.zoom."""
        return (int(math.floor(self.gx0 / TILE_SIZE)), int(math.floor(self.gy0 / TILE_SIZE)),
                int(math.floor((self.gx1 - 1e-9) / TILE_SIZE)),
                int(math.floor((self.gy1 - 1e-9) / TILE_SIZE)))

    def tile_origin(self) -> Tuple[int, int]:
        """Global pixel coordinates of the top-left corner of the tile canvas (the first
        covering tile's corner)."""
        tx0, ty0, _, _ = self.tile_range()
        return (tx0 * TILE_SIZE, ty0 * TILE_SIZE)

    def canvas_size(self) -> Tuple[int, int]:
        """Size of the un-scaled canvas that holds every covering tile."""
        tx0, ty0, tx1, ty1 = self.tile_range()
        return ((tx1 - tx0 + 1) * TILE_SIZE, (ty1 - ty0 + 1) * TILE_SIZE)

    def tile_paste_origin(self, tx: int, ty: int) -> Tuple[int, int]:
        """Where tile (tx, ty) is pasted on the tile canvas."""
        ox, oy = self.tile_origin()
        return (tx * TILE_SIZE - ox, ty * TILE_SIZE - oy)

    def canvas_crop(self) -> Tuple[int, int, int, int]:
        """Crop box (left, top, right, bottom) on the tile canvas that is exactly this frame;
        resize the crop to ``self.size`` to get the output canvas."""
        ox, oy = self.tile_origin()
        return (int(round(self.gx0 - ox)), int(round(self.gy0 - oy)),
                int(round(self.gx1 - ox)), int(round(self.gy1 - oy)))

    def cache_key(self) -> str:
        return "%.5f_%.5f_%g_%d_z%d" % (self.lat0, self.lon0, self.size_km, self.width, self.zoom)


# ---- GeoJSON polygons ------------------------------------------------------------------
Ring = Sequence[Sequence[float]]


def _rings(v) -> bool:
    """A polygon as GeoJSON writes it: a non-empty list whose first member (the outer ring)
    is a non-empty list. Anything else (a number, a string, an object) is no polygon."""
    return (isinstance(v, (list, tuple)) and bool(v)
            and isinstance(v[0], (list, tuple)) and bool(v[0]))


def iter_polygons(geometry: Optional[dict]) -> Iterator[List[Ring]]:
    """Yield each polygon of a GeoJSON geometry as a list of rings (outer first). Supports
    Polygon, MultiPolygon, GeometryCollection; anything else yields nothing, and so does a
    member that is not a list of rings (never raises on a wrongly shaped geometry)."""
    if not geometry or not isinstance(geometry, dict):
        return
    t = geometry.get("type")
    if t == "Polygon":
        rings = geometry.get("coordinates")
        if _rings(rings):
            yield rings
    elif t == "MultiPolygon":
        polys = geometry.get("coordinates")
        for rings in polys if isinstance(polys, (list, tuple)) else []:
            if _rings(rings):
                yield rings
    elif t == "GeometryCollection":
        members = geometry.get("geometries")
        for g in members if isinstance(members, (list, tuple)) else []:
            for rings in iter_polygons(g):
                yield rings


def point_in_ring(lon: float, lat: float, ring: Ring) -> bool:
    """Ray casting (even-odd). Points exactly on an edge count as inside."""
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        # edge check (collinear and within the segment's box)
        if (min(xi, xj) - 1e-12 <= lon <= max(xi, xj) + 1e-12
                and min(yi, yj) - 1e-12 <= lat <= max(yi, yj) + 1e-12):
            cross = (xj - xi) * (lat - yi) - (yj - yi) * (lon - xi)
            if abs(cross) < 1e-12:
                return True
        if (yi > lat) != (yj > lat):
            x_at = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_at:
                inside = not inside
        j = i
    return inside


def point_in_polygon(lon: float, lat: float, rings: Sequence[Ring]) -> bool:
    """Inside the outer ring and outside every hole."""
    if not rings or not point_in_ring(lon, lat, rings[0]):
        return False
    for hole in rings[1:]:
        if point_in_ring(lon, lat, hole):
            return False
    return True


def point_in_geometry(lon: float, lat: float, geometry: Optional[dict]) -> bool:
    return any(point_in_polygon(lon, lat, rings) for rings in iter_polygons(geometry))


def _finite(v) -> bool:
    # numbers.Real, not (int, float): a caller's numpy scalar is a number too
    return isinstance(v, numbers.Real) and not isinstance(v, bool) and math.isfinite(v)


def _position(p) -> bool:
    """A GeoJSON position: a list that starts with two finite numbers (lon, lat). Numbers
    sent as strings, nulls, NaN and a ring nested one level too deep are not positions."""
    return isinstance(p, (list, tuple)) and len(p) >= 2 and _finite(p[0]) and _finite(p[1])


def ring_ok(ring) -> bool:
    """A ring the point test can work with: at least 3 positions, each two finite numbers."""
    return (isinstance(ring, (list, tuple)) and len(ring) >= 3
            and all(_position(p) for p in ring))


_NOT_AREAS = ("Point", "MultiPoint", "LineString", "MultiLineString")


def _all_polygons(geometry) -> Optional[list]:
    """Every polygon of a geometry, or None when any member is not one that can be read:
    unlike ``iter_polygons``, which passes over such a member, this tells it apart (a
    MultiPolygon with one member "x", a collection holding a string)."""
    if not isinstance(geometry, dict):
        return None
    t = geometry.get("type")
    if t == "Polygon":
        rings = geometry.get("coordinates")
        return [rings] if _rings(rings) else None
    if t == "MultiPolygon":
        polys = geometry.get("coordinates")
        if not isinstance(polys, (list, tuple)) or not all(_rings(p) for p in polys):
            return None
        return list(polys)
    if t == "GeometryCollection":
        members = geometry.get("geometries")
        if not isinstance(members, (list, tuple)):
            return None
        out: list = []
        for g in members:
            if isinstance(g, dict) and g.get("type") in _NOT_AREAS:
                continue                # a point or a line is no area, as in iter_polygons
            sub = _all_polygons(g)
            if sub is None:
                return None
            out.extend(sub)
        return out
    return []


def geometry_ok(geometry: Optional[dict]) -> bool:
    """Whether a geometry holds at least one polygon, every member of it can be read as a
    polygon (``_all_polygons``) and every ring of every polygon (the holes too) is
    ``ring_ok``. One bad ring or member spoils the whole geometry: a shape of which a part
    cannot be read answers neither "inside" nor "outside" reliably. Never raises."""
    try:
        polys = _all_polygons(geometry)
    except RecursionError:
        return False
    return bool(polys) and all(all(ring_ok(r) for r in rings) for rings in polys)


def ring_bbox(ring: Ring) -> Optional[Tuple[float, float, float, float]]:
    """Box of a ring; None for an empty ring and for one that holds anything but positions
    of finite numbers (never raises: a box of strings would only fail later, in a
    comparison far from here)."""
    if not ring or not isinstance(ring, (list, tuple)) or not all(_position(p) for p in ring):
        return None
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return (min(xs), min(ys), max(xs), max(ys))


def geometry_bbox(geometry: Optional[dict]) -> Optional[Tuple[float, float, float, float]]:
    """Box of the outer rings of a geometry, or None ("no box") when it has no polygon or
    when one of its outer rings has no box (see ``ring_bbox``)."""
    box = None
    for rings in iter_polygons(geometry):
        b = ring_bbox(rings[0])
        if b is None:
            return None
        box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]),
                                     max(box[2], b[2]), max(box[3], b[3]))
    return box


def _box(b) -> bool:
    return isinstance(b, (list, tuple)) and len(b) == 4 and all(_finite(v) for v in b)


def bbox_intersects(a: Optional[Tuple[float, float, float, float]],
                    b: Optional[Tuple[float, float, float, float]]) -> bool:
    """Whether two boxes overlap. False when either is missing or is not four finite
    numbers: "no box" intersects nothing."""
    if not _box(a) or not _box(b):
        return False
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def merge_geometries(geoms: Iterable[Optional[dict]]) -> Optional[dict]:
    """Combine several geometries into one MultiPolygon (no dissolve: overlapping members
    are kept as-is, which is fine for filling with one colour and for point tests)."""
    polys = []
    for g in geoms:
        for rings in iter_polygons(g):
            polys.append([list(map(list, r)) for r in rings])
    if not polys:
        return None
    return {"type": "MultiPolygon", "coordinates": polys}
