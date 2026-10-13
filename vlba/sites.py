"""The ten VLBA antennas.

Coordinates: Table 4.1 "Antenna Sites" of the NRAO VLBA Observational Status Summary
(the "Antenna Sites" chapter of the 2027A edition, checked 2026-10-08: "All locations are
based on the WGS84 ellipsoid used by the GPS system"), to four decimals (about 10 m); the
geodetic positions derived from the SCHED station catalog (GSFC 2019d / ITRF2014, epoch
2020.0) agree with them to better than 0.1 arcsecond.
``elev_m`` is the height of the antenna in metres: the OSS table's WGS84 ellipsoidal
height for nine sites (within a few tens of metres of the height above sea level, which
is what matters for weather), and for Saint Croix the height above sea level (16 m; the
OSS prints -15 m, an ellipsoidal height, because the geoid lies about 40 m below the
ellipsoid there). ``tz`` is the static fall-back zone of the parent's site format (NWS
``/points`` normally supplies it): Kitt Peak keeps Arizona time (no daylight saving),
St. Croix is Atlantic Standard Time all year, Mauna Kea is Hawaii Standard Time.

Order: the VLBA's own (alphabetical by code). ``slug`` is the lower-case code, so the map
files are ``radar_mk_dark.png`` and the page anchors ``#site-mk``. The entries carry two keys
beyond the parent's ``slug|name|lat|lon|tz``: ``code`` and ``elev_m``; everything in the
parent ignores them.
"""
from __future__ import annotations

from typing import List

VLBA_SITES: List[dict] = [
    {"slug": "br", "code": "BR", "name": "Brewster, WA", "lat": 48.1312, "lon": -119.6833,
     "elev_m": 250, "tz": "America/Los_Angeles"},
    {"slug": "fd", "code": "FD", "name": "Fort Davis, TX", "lat": 30.6350, "lon": -103.9448,
     "elev_m": 1606, "tz": "America/Chicago"},
    {"slug": "hn", "code": "HN", "name": "Hancock, NH", "lat": 42.9336, "lon": -71.9866,
     "elev_m": 296, "tz": "America/New_York"},
    {"slug": "kp", "code": "KP", "name": "Kitt Peak, AZ", "lat": 31.9563, "lon": -111.6124,
     "elev_m": 1902, "tz": "America/Phoenix"},
    {"slug": "la", "code": "LA", "name": "Los Alamos, NM", "lat": 35.7751, "lon": -106.2456,
     "elev_m": 1962, "tz": "America/Denver"},
    {"slug": "mk", "code": "MK", "name": "Mauna Kea, HI", "lat": 19.8014, "lon": -155.4555,
     "elev_m": 3763, "tz": "Pacific/Honolulu"},
    {"slug": "nl", "code": "NL", "name": "North Liberty, IA", "lat": 41.7714, "lon": -91.5741,
     "elev_m": 222, "tz": "America/Chicago"},
    {"slug": "ov", "code": "OV", "name": "Owens Valley, CA", "lat": 37.2316, "lon": -118.2771,
     "elev_m": 1196, "tz": "America/Los_Angeles"},
    {"slug": "pt", "code": "PT", "name": "Pie Town, NM", "lat": 34.3010, "lon": -108.1192,
     "elev_m": 2365, "tz": "America/Denver"},
    {"slug": "sc", "code": "SC", "name": "St. Croix, VI", "lat": 17.7566, "lon": -64.5836,
     "elev_m": 16, "tz": "America/St_Thomas"},
]


def site_code(site: dict) -> str:
    """The antenna's two-letter code: ``code`` when the entry has one, else the slug in
    upper case (a site from ``WEATHER_SITES`` has no code)."""
    code = site.get("code") if isinstance(site, dict) else None
    if isinstance(code, str) and code.strip():
        return code.strip().upper()
    return str(site.get("slug") or "").upper()


def site_elevation_m(site: dict):
    """``elev_m`` as a float, or None when the entry has none / it is not a number."""
    try:
        v = float(site.get("elev_m"))
    except (TypeError, ValueError, AttributeError):
        return None
    return v
