"""The built-in sites: the ten VLBA antennas, then the VLA and the GBT.

VLBA coordinates: Table 4.1 "Antenna Sites" of the NRAO VLBA Observational Status Summary
(the "Antenna Sites" chapter of the 2027A edition, checked 2026-10-08: "All locations are
based on the WGS84 ellipsoid used by the GPS system"), to four decimals (about 10 m); the
geodetic positions derived from the SCHED station catalog (GSFC 2019d / ITRF2014, epoch
2020.0) agree with them to better than 0.1 arcsecond.
VLA and GBT coordinates: the SCHED station catalog (``locations.dat``), converted on
2026-10-14 from its ITRF X, Y, Z to WGS84 latitude, longitude and ellipsoidal height, to four
decimals like the VLBA entries: for the VLA the array's reference position (``VLA``, where
its three arms meet; frame "VLA antenna data Sep. 28, 2011 adjusted to N8 from
gsfc2014a"), for the GBT ``GBT-VLBA`` (frame "GSFC 2020-Jan-12").
``elev_m`` is the height of the antenna in metres: the OSS table's WGS84 ellipsoidal
height for nine VLBA sites and the SCHED-derived one for the VLA and the GBT (within a few
tens of metres of the height above sea level, which is what matters for weather), and for
Saint Croix the height above sea level (16 m; the OSS prints -15 m, an ellipsoidal height,
because the geoid lies about 40 m below the ellipsoid there). ``tz`` is the static fall-back
zone of the parent's site format (NWS ``/points`` normally supplies it): Kitt Peak keeps
Arizona time (no daylight saving), St. Croix is Atlantic Standard Time all year, Mauna Kea
is Hawaii Standard Time.

Order: the VLBA's own (alphabetical by code), then the VLA and the GBT. ``slug`` is the
lower-case code, so the map files are ``radar_mk_dark.png`` and the page anchors
``#site-mk``. The codes are the VLBA's two-letter station codes, and for the VLA and the GBT
their usual names (their VLBI station codes are Y and GB). The entries carry two keys beyond
the parent's ``slug|name|lat|lon|tz``: ``code`` and ``elev_m``; everything in the parent
ignores them.

Two optional keys say what NWS does not forecast at a site, because only that may read
"n/a" on the page; anything else that is absent from a forecast is a gap ("no data"):

* ``not_provided``: a tuple of the raw gridpoint series (the row keys of
  ``vlba/gridpoint.py``: ``thunder_pct``, ``snow_mm``, ``ice_mm``, ...) the site's forecast
  office is known not to produce, i.e. to send with an empty ``values`` list in every run.
  A check whose series is absent and listed here reads "not provided" (neither bad nor
  missing); absent and not listed, it reads "no data" and the verdict is GOOD?. A built-in
  entry without the key lacks nothing. The tuples were checked against api.weather.gov on
  2026-11-12 (all twelve gridpoints, every verdict series): Honolulu (HFO) sent no
  probabilityOfThunder, San Juan (SJU) no probabilityOfThunder, snowfallAmount or
  iceAccumulation, Tucson (TWC) no iceAccumulation; every other verdict series had values
  at every site. A wrong tuple is not harmless either way: a series listed here that the
  office does produce hides its check when it is absent, and one not listed that the office
  stops producing leaves the site at GOOD? until it is added. A site that is not in this
  list (``WEATHER_SITES``) has no entry to say so and may lack ``thunder_pct`` only
  (``DEFAULT_NOT_PROVIDED``: the two offices outside the contiguous states above).
* ``gridless``: True for a place NWS has no forecast grid for at all (American Samoa: its
  ``/points`` answer names zones and a time zone but no grid). Only then do the forecast
  checks read "not provided"; at any other site an answer without a grid is a broken
  answer, and the forecast checks read "no data". None of the twelve is gridless.
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

SITES: List[dict] = [
    {"slug": "br", "code": "BR", "name": "Brewster, WA", "lat": 48.1312, "lon": -119.6833,
     "elev_m": 250, "tz": "America/Los_Angeles"},
    {"slug": "fd", "code": "FD", "name": "Fort Davis, TX", "lat": 30.6350, "lon": -103.9448,
     "elev_m": 1606, "tz": "America/Chicago"},
    {"slug": "hn", "code": "HN", "name": "Hancock, NH", "lat": 42.9336, "lon": -71.9866,
     "elev_m": 296, "tz": "America/New_York"},
    # Tucson (TWC) sent an empty iceAccumulation on 2026-11-12, its snowfallAmount had values
    {"slug": "kp", "code": "KP", "name": "Kitt Peak, AZ", "lat": 31.9563, "lon": -111.6124,
     "elev_m": 1902, "tz": "America/Phoenix", "not_provided": ("ice_mm",)},
    {"slug": "la", "code": "LA", "name": "Los Alamos, NM", "lat": 35.7751, "lon": -106.2456,
     "elev_m": 1962, "tz": "America/Denver"},
    {"slug": "mk", "code": "MK", "name": "Mauna Kea, HI", "lat": 19.8014, "lon": -155.4555,
     "elev_m": 3763, "tz": "Pacific/Honolulu", "not_provided": ("thunder_pct",)},
    {"slug": "nl", "code": "NL", "name": "North Liberty, IA", "lat": 41.7714, "lon": -91.5741,
     "elev_m": 222, "tz": "America/Chicago"},
    {"slug": "ov", "code": "OV", "name": "Owens Valley, CA", "lat": 37.2316, "lon": -118.2771,
     "elev_m": 1196, "tz": "America/Los_Angeles"},
    {"slug": "pt", "code": "PT", "name": "Pie Town, NM", "lat": 34.3010, "lon": -108.1192,
     "elev_m": 2365, "tz": "America/Denver"},
    {"slug": "sc", "code": "SC", "name": "St. Croix, VI", "lat": 17.7566, "lon": -64.5836,
     "elev_m": 16, "tz": "America/St_Thomas", "not_provided": ("thunder_pct", "snow_mm", "ice_mm")},
    {"slug": "vla", "code": "VLA", "name": "Plains of San Agustin, NM", "lat": 34.0788,
     "lon": -107.6183, "elev_m": 2114, "tz": "America/Denver"},
    {"slug": "gbt", "code": "GBT", "name": "Green Bank, WV", "lat": 38.4331, "lon": -79.8398,
     "elev_m": 824, "tz": "America/New_York"},
]


def site_code(site: dict) -> str:
    """The site's code: ``code`` when the entry has one (two letters for a VLBA antenna,
    VLA, GBT), else the slug in upper case (a site from ``WEATHER_SITES`` has no code)."""
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


# What a site without a built-in entry may lack: the probability of thunder, which the
# offices outside the contiguous states (HFO, SJU) do not produce.
DEFAULT_NOT_PROVIDED: Tuple[str, ...] = ("thunder_pct",)
# How near a configured site must lie to a built-in one to be that site (degrees; the rule
# of ``VlbaConfig.from_env`` for ``code`` and ``elev_m``).
_SAME_PLACE_DEG = 0.05


def _builtin(site: dict) -> Optional[dict]:
    """The built-in entry at the place of ``site`` (within ``_SAME_PLACE_DEG``), or None."""
    try:
        lat, lon = float(site["lat"]), float(site["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    for s in SITES:
        if abs(lat - s["lat"]) < _SAME_PLACE_DEG and abs(lon - s["lon"]) < _SAME_PLACE_DEG:
            return s
    return None


def series_not_provided(site: Any) -> Tuple[str, ...]:
    """The gridpoint series the forecast office of ``site`` is known not to produce (see the
    module docstring): the entry's own ``not_provided``; without the key, that of the
    built-in entry at the same place (a built-in site named through ``WEATHER_SITES`` has
    the parent's keys only), where no key means nothing; ``DEFAULT_NOT_PROVIDED`` for a
    site that is not built in, and for anything that is not a site entry. A ``not_provided``
    that is not a list or tuple of strings exempts nothing."""
    if not isinstance(site, dict):
        return DEFAULT_NOT_PROVIDED
    if "not_provided" not in site:
        known = _builtin(site)
        if known is None:
            return DEFAULT_NOT_PROVIDED
        site = known
    names = site.get("not_provided", ())
    if not isinstance(names, (list, tuple)):
        return ()
    return tuple(n for n in names if isinstance(n, str))


def is_gridless(site: Any) -> bool:
    """Whether the entry says NWS has no forecast grid here (``"gridless": True``, nothing
    else counts): only then is a ``/points`` answer without a grid not a fault."""
    return isinstance(site, dict) and site.get("gridless") is True
