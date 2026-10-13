"""US states, territories and marine areas whose NWS alerts a configuration needs
(``WEATHER_ALERT_AREAS``).

api.weather.gov serves the active alerts of whole states and territories
(``/alerts/active?area=NM,TX``), and those of the marine zones by marine area (``LM``, Lake
Michigan; ``PZ``, the Pacific off the West Coast; ...): a state's feed has no marine zones,
so a Small Craft Advisory or a Special Marine Warning on Lake Michigan is only in ``LM``.
With ``WEATHER_ALERT_AREAS=auto`` (the default) the query names every state, territory and
marine area whose bounding box touches at least one site's map frame (``areas_touching``),
so the list follows the configured sites. A bounding box over-includes near borders: for
example, Oklahoma's box, set by its panhandle in the north-west and the Red River in the
south-east, also covers a strip of West Texas and eastern New Mexico. That only adds
alerts to the download: every alert is kept for a site by its own geometry afterwards
(``alerts.site_alerts``), never by its state.

Source of ``STATE_BOXES``: U.S. Census Bureau, 2024 TIGER/Line Shapefiles, "States and
Equivalent Entities", ``tl_2024_us_state.zip`` from
https://www2.census.gov/geo/tiger/TIGER2024/STATE/ (downloaded 2026-09-23, SHA-256
ad00cbe66c7177091b668cee202e93d4a1ddcee271c28d1c9f9874af59c04b92). It has 56 entities:
the 50 states, the District of Columbia, Puerto Rico and the four Island Areas (American
Samoa, Guam, the Northern Mariana Islands, the US Virgin Islands), keyed here by their USPS
code (``STUSPS``), which is also the NWS area code; api.weather.gov accepted every one of
them on 2026-09-23. Each box is the minimum and maximum of all the entity's vertices,
rounded outward to 0.01 degrees. TIGER/Line boundaries include coastal and Great Lakes
water, so the boxes are a little larger than the land: the safe side. Alaska gets two
boxes, split at the prime meridian, because its western Aleutians lie beyond 180 degrees at
positive longitudes; one box would span the globe. ``tools/state_bboxes.py`` regenerates the
table from the shapefile (the 2024 cartographic boundary file, cb_2024_us_state_500k,
agrees within 0.1 degrees, except where it leaves out water).

Source of ``MARINE_BOXES``: National Weather Service, "Coastal and Offshore Marine Zones"
(https://www.weather.gov/gis/MarineZones), the files valid from 16 April 2026:
``mz16ap26.zip`` (coastal marine zones, including the Great Lakes; 569 zones; SHA-256
97f6317f4ebef4994940b3851e9f889a71b06914eb41d77dd6edbebd46b5184d) and ``oz16ap26.zip``
(offshore zones; 130 zones; SHA-256
07d1514e9bf4b434b2d1f14f803751cd2a3a1dc8119507ec88edcb5c99b4932f), downloaded 2026-09-23.
A zone's id starts with its marine area code (``LMZ740`` is in ``LM``); api.weather.gov
accepts exactly these 15 codes. Every zone is boxed on its own, and the boxes of one area are
merged only while a merge adds little area (``tools/state_bboxes.py --marine``, which
regenerates the table), because one box per area would reach far inland: the Atlantic area
``AM`` runs from North Carolina to the Caribbean. The boxes are still larger than the water,
so a map near a coast may include a marine area whose zones it does not reach; that too only
adds alerts to the download. The high-seas zones are left out: NWS issues no alerts for them.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Tuple

from . import geo

Box = Tuple[float, float, float, float]      # (lonmin, latmin, lonmax, latmax)

STATE_BOXES: Dict[str, Tuple[Box, ...]] = {
    "AK": ((-179.24, 51.17, -129.97, 71.44), (172.34, 51.29, 179.86, 53.07)),  # Alaska
    "AL": ((-88.48, 30.14, -84.88, 35.01),),  # Alabama
    "AR": ((-94.62, 33.00, -89.64, 36.50),),  # Arkansas
    "AS": ((-171.15, -14.61, -168.10, -10.99),),  # American Samoa
    "AZ": ((-114.82, 31.33, -109.04, 37.01),),  # Arizona
    "CA": ((-124.49, 32.52, -114.13, 42.01),),  # California
    "CO": ((-109.07, 36.99, -102.04, 41.01),),  # Colorado
    "CT": ((-73.73, 40.95, -71.78, 42.06),),  # Connecticut
    "DC": ((-77.12, 38.79, -76.90, 39.00),),  # District of Columbia
    "DE": ((-75.79, 38.45, -74.98, 39.84),),  # Delaware
    "FL": ((-87.64, 24.39, -79.97, 31.01),),  # Florida
    "GA": ((-85.61, 30.35, -80.78, 35.01),),  # Georgia
    "GU": ((144.56, 13.18, 145.01, 13.71),),  # Guam
    "HI": ((-178.45, 18.86, -154.75, 28.52),),  # Hawaii
    "IA": ((-96.64, 40.37, -90.14, 43.51),),  # Iowa
    "ID": ((-117.25, 41.98, -111.04, 49.01),),  # Idaho
    "IL": ((-91.52, 36.97, -87.01, 42.51),),  # Illinois
    "IN": ((-88.10, 37.77, -84.78, 41.77),),  # Indiana
    "KS": ((-102.06, 36.99, -94.58, 40.01),),  # Kansas
    "KY": ((-89.58, 36.49, -81.96, 39.15),),  # Kentucky
    "LA": ((-94.05, 28.85, -88.75, 33.02),),  # Louisiana
    "MA": ((-73.51, 41.18, -69.85, 42.89),),  # Massachusetts
    "MD": ((-79.49, 37.88, -74.98, 39.73),),  # Maryland
    "ME": ((-71.09, 42.91, -66.88, 47.46),),  # Maine
    "MI": ((-90.42, 41.69, -82.12, 48.31),),  # Michigan
    "MN": ((-97.24, 43.49, -89.48, 49.39),),  # Minnesota
    "MO": ((-95.78, 35.99, -89.09, 40.62),),  # Missouri
    "MP": ((144.81, 14.03, 146.16, 20.62),),  # Commonwealth of the Northern Mariana Islands
    "MS": ((-91.66, 30.13, -88.09, 35.00),),  # Mississippi
    "MT": ((-116.05, 44.35, -104.03, 49.01),),  # Montana
    "NC": ((-84.33, 33.75, -75.40, 36.59),),  # North Carolina
    "ND": ((-104.05, 45.93, -96.55, 49.01),),  # North Dakota
    "NE": ((-104.06, 39.99, -95.30, 43.01),),  # Nebraska
    "NH": ((-72.56, 42.69, -70.57, 45.31),),  # New Hampshire
    "NJ": ((-75.57, 38.78, -73.88, 41.36),),  # New Jersey
    "NM": ((-109.06, 31.33, -103.00, 37.01),),  # New Mexico
    "NV": ((-120.01, 35.00, -114.03, 42.01),),  # Nevada
    "NY": ((-79.77, 40.47, -71.77, 45.02),),  # New York
    "OH": ((-84.83, 38.40, -80.51, 42.33),),  # Ohio
    "OK": ((-103.01, 33.61, -94.43, 37.01),),  # Oklahoma
    "OR": ((-124.71, 41.99, -116.46, 46.30),),  # Oregon
    "PA": ((-80.52, 39.71, -74.68, 42.52),),  # Pennsylvania
    "PR": ((-68.00, 17.83, -65.16, 18.57),),  # Puerto Rico
    "RI": ((-71.91, 41.09, -71.08, 42.02),),  # Rhode Island
    "SC": ((-83.36, 31.99, -78.49, 35.22),),  # South Carolina
    "SD": ((-104.06, 42.47, -96.43, 45.95),),  # South Dakota
    "TN": ((-90.32, 34.98, -81.64, 36.68),),  # Tennessee
    "TX": ((-106.65, 25.83, -93.50, 36.51),),  # Texas
    "UT": ((-114.06, 36.99, -109.04, 42.01),),  # Utah
    "VA": ((-83.68, 36.54, -75.16, 39.47),),  # Virginia
    "VI": ((-65.16, 17.62, -64.51, 18.47),),  # United States Virgin Islands
    "VT": ((-73.44, 42.72, -71.46, 45.02),),  # Vermont
    "WA": ((-124.85, 45.54, -116.91, 49.01),),  # Washington
    "WI": ((-92.89, 42.49, -86.24, 47.31),),  # Wisconsin
    "WV": ((-82.65, 37.20, -77.71, 40.64),),  # West Virginia
    "WY": ((-111.06, 40.99, -104.05, 45.01),),  # Wyoming
}

# NWS marine areas: zone boxes merged per area (see the module docstring); made with
# tools/state_bboxes.py --marine mz16ap26 oz16ap26
MARINE_BOXES: Dict[str, Tuple[Box, ...]] = {
    "AM": (  # Atlantic south of Currituck Beach Light NC, Florida Keys, Caribbean
        (-88.46, 15.92, -84.99, 18.01), (-87.67, 18.00, -84.99, 22.01),
        (-85.00, 15.00, -79.99, 20.01), (-85.00, 19.98, -77.31, 22.49),
        (-83.66, 8.98, -79.99, 15.01), (-81.69, 29.89, -80.20, 31.31),
        (-81.41, 31.11, -79.26, 32.51), (-81.34, 28.82, -79.63, 29.93),
        (-81.13, 26.68, -80.61, 27.21), (-80.87, 26.96, -54.99, 31.01),
        (-80.49, 22.00, -72.02, 24.02), (-80.39, 24.00, -74.02, 27.01),
        (-80.34, 32.03, -78.24, 33.12), (-80.01, 8.64, -75.34, 10.99),
        (-80.01, 10.98, -75.99, 20.00), (-79.28, 32.61, -76.59, 34.21),
        (-77.97, 33.61, -75.63, 34.45), (-77.54, 33.91, -74.23, 36.24),
        (-77.34, 19.83, -69.99, 22.01), (-77.00, 25.00, -54.99, 27.01),
        (-76.01, 10.98, -71.99, 18.07), (-76.01, 17.99, -72.96, 20.02),
        (-75.00, 22.00, -64.99, 25.01), (-72.02, 10.69, -67.99, 18.27),
        (-70.00, 17.98, -64.99, 22.01), (-69.13, 10.26, -63.99, 15.01),
        (-68.01, 15.00, -63.99, 19.51), (-65.00, 18.99, -59.99, 25.01),
        (-64.02, 8.52, -59.99, 19.01), (-60.00, 7.00, -54.99, 25.01)),
    "AN": (  # Atlantic from the Canadian border to Currituck Beach Light NC
        (-80.31, 30.99, -72.28, 33.65), (-77.33, 37.89, -75.77, 39.60),
        (-76.79, 36.08, -74.12, 38.06), (-76.15, 33.35, -74.06, 34.70),
        (-75.62, 38.53, -73.05, 39.74), (-75.44, 32.07, -70.80, 35.55),
        (-75.37, 36.71, -73.51, 38.81), (-74.68, 33.86, -70.22, 37.99),
        (-74.55, 39.12, -70.81, 40.78), (-73.86, 36.48, -68.99, 39.77),
        (-73.83, 40.43, -71.61, 41.53), (-71.88, 39.69, -66.05, 42.38),
        (-71.08, 41.94, -68.52, 44.13), (-69.20, 42.25, -66.88, 45.13),
        (-69.01, 39.00, -64.99, 41.01), (-69.01, 37.06, -65.74, 39.02)),
    "GM": (  # Gulf of Mexico
        (-97.90, 22.00, -80.48, 26.01), (-97.80, 25.95, -90.99, 28.76),
        (-97.75, 18.14, -91.99, 22.02), (-96.67, 28.04, -94.00, 29.23),
        (-95.33, 28.44, -92.14, 30.06), (-92.22, 28.03, -90.05, 29.86),
        (-92.05, 18.37, -86.99, 22.01), (-91.00, 26.00, -82.35, 29.45),
        (-90.60, 29.16, -86.39, 30.52), (-89.42, 28.51, -87.94, 29.88),
        (-88.12, 30.31, -85.98, 30.95), (-86.41, 28.58, -83.13, 30.38),
        (-83.30, 25.46, -81.35, 27.00), (-81.15, 23.65, -79.53, 25.33)),
    "LC": ((-83.22, 42.01, -82.41, 43.01),),  # Lake St. Clair
    "LE": (  # Lake Erie
        (-83.48, 41.38, -81.47, 42.08), (-81.54, 41.64, -79.72, 42.48),
        (-79.86, 42.28, -78.84, 43.09)),
    "LH": (  # Lake Huron
        (-84.85, 45.33, -83.15, 46.06), (-83.96, 43.00, -82.12, 45.63)),
    "LM": (  # Lake Michigan
        (-88.05, 41.60, -86.18, 44.89), (-87.85, 44.70, -84.84, 46.11)),
    "LO": (  # Lake Ontario
        (-79.21, 43.07, -76.96, 43.64), (-76.97, 43.27, -76.04, 44.21)),
    "LS": (  # Lake Superior
        (-92.30, 46.56, -89.63, 47.88), (-90.45, 46.41, -85.98, 48.31),
        (-85.99, 46.44, -84.63, 47.35), (-84.64, 45.98, -83.43, 46.54)),
    "PH": ((-164.25, 14.91, -150.80, 26.24),),  # Central Pacific, Hawaiian waters
    "PK": (  # North Pacific, Alaskan waters
        (-180.00, 50.43, -176.88, 53.21), (-180.00, 53.09, -170.99, 59.01),
        (-180.00, 59.00, -165.74, 62.00), (-178.00, 52.17, -172.68, 53.64),
        (-177.28, 50.43, -171.60, 52.68), (-177.20, 61.99, -168.61, 63.65),
        (-173.46, 52.58, -165.68, 54.31), (-173.14, 63.37, -166.01, 65.61),
        (-172.69, 52.03, -168.94, 53.34), (-172.41, 62.68, -168.13, 64.04),
        (-172.29, 51.06, -167.99, 52.59), (-171.00, 53.95, -161.99, 59.01),
        (-170.00, 53.21, -166.55, 55.25), (-170.00, 68.05, -163.85, 70.10),
        (-169.06, 65.49, -163.72, 68.14), (-169.00, 57.00, -159.97, 59.75),
        (-169.00, 60.63, -164.58, 63.35), (-168.98, 69.52, -160.77, 73.67),
        (-168.95, 52.00, -164.46, 53.72), (-168.23, 62.56, -164.90, 64.35),
        (-167.55, 69.18, -159.25, 71.89), (-166.56, 53.71, -164.39, 54.83),
        (-166.44, 63.01, -161.49, 64.72), (-166.29, 58.29, -161.35, 60.95),
        (-165.69, 52.70, -159.63, 55.13), (-165.15, 71.88, -151.46, 74.71),
        (-165.13, 54.09, -161.05, 56.12), (-164.41, 69.02, -158.58, 71.13),
        (-164.33, 65.96, -160.19, 67.13), (-162.75, 55.69, -159.02, 57.65),
        (-162.52, 63.42, -160.68, 64.98), (-161.77, 56.74, -156.83, 59.19),
        (-161.68, 55.12, -158.08, 56.21), (-161.06, 53.59, -156.99, 55.83),
        (-160.78, 70.74, -151.99, 72.60), (-158.68, 55.82, -153.68, 57.58),
        (-158.09, 54.30, -153.01, 56.46), (-155.79, 57.27, -152.53, 58.86),
        (-154.27, 58.58, -151.99, 59.80), (-154.14, 55.41, -150.01, 58.31),
        (-154.13, 54.99, -134.74, 57.06), (-153.29, 59.31, -150.92, 60.58),
        (-152.65, 57.49, -148.98, 59.33), (-152.62, 70.13, -145.99, 72.00),
        (-152.15, 60.36, -148.97, 61.53), (-152.00, 71.00, -141.00, 74.37),
        (-151.00, 58.50, -147.73, 60.14), (-150.60, 57.04, -137.07, 58.80),
        (-148.72, 59.51, -145.60, 61.28), (-147.85, 58.63, -143.90, 59.82),
        (-146.01, 69.63, -141.00, 71.50), (-145.96, 59.64, -143.87, 60.64),
        (-144.03, 58.59, -137.92, 60.15), (-141.99, 57.99, -138.34, 59.69),
        (-139.00, 56.37, -136.24, 58.07), (-138.35, 58.06, -135.76, 59.10),
        (-137.08, 56.86, -132.87, 58.60), (-136.25, 55.74, -132.33, 57.40),
        (-136.21, 54.07, -133.17, 55.75), (-135.63, 58.19, -134.87, 59.49),
        (-134.75, 54.52, -129.97, 56.53), (170.89, 50.55, 177.75, 54.31),
        (171.50, 53.20, 180.00, 59.01), (177.40, 59.00, 180.00, 60.45),
        (177.61, 50.49, 180.00, 53.40)),
    "PM": (  # Western Pacific, Mariana Islands waters
        (-121.08, 27.00, -113.84, 30.01), (-119.48, 22.53, -112.02, 27.85),
        (-118.12, 29.99, -115.78, 32.54), (-116.28, 20.00, -109.99, 24.89),
        (-115.61, 16.98, -109.98, 20.01), (-114.90, 27.99, -111.84, 31.83),
        (-112.78, 25.41, -109.25, 28.66), (-111.01, 23.21, -107.47, 26.48),
        (-110.00, 15.59, -103.73, 20.01), (-110.00, 20.00, -105.18, 24.32),
        (-107.22, 13.03, -98.50, 18.69), (-102.20, 11.98, -96.49, 16.30),
        (-99.83, 10.69, -93.99, 16.44), (-96.22, 9.29, -87.91, 14.54),
        (-94.01, 12.82, -92.21, 16.11), (-93.20, -3.45, -78.78, 1.67),
        (-92.18, 7.07, -85.62, 13.48), (-89.07, 3.27, -80.02, 10.33),
        (-83.17, 1.66, -77.01, 9.03), (133.81, 6.66, 135.15, 8.01),
        (137.41, 8.81, 138.75, 10.15), (143.94, 12.56, 145.96, 14.80),
        (144.91, 14.25, 146.50, 15.96), (151.17, 6.77, 152.51, 8.12),
        (157.56, 6.30, 158.90, 7.64), (162.28, 4.68, 163.62, 6.02),
        (165.94, 18.61, 167.33, 20.00), (170.71, 6.41, 172.05, 7.75)),
    "PS": (  # South Central Pacific, American Samoa waters
        (-171.76, -11.73, -170.40, -10.38), (-171.52, -15.21, -167.49, -13.49)),
    "PZ": (  # Eastern North Pacific, US West Coast
        (-131.12, 46.72, -124.21, 48.52), (-130.58, 43.98, -125.48, 46.74),
        (-130.35, 41.76, -124.04, 44.03), (-130.07, 38.49, -123.68, 41.79),
        (-129.25, 36.63, -125.61, 38.78), (-128.09, 35.03, -124.52, 36.98),
        (-126.79, 32.88, -122.97, 35.40), (-126.70, 36.97, -123.89, 38.95),
        (-125.95, 45.76, -123.21, 47.31), (-125.63, 43.99, -123.86, 45.77),
        (-125.62, 35.39, -122.89, 37.21), (-125.18, 30.00, -119.95, 33.33),
        (-125.11, 37.99, -122.82, 38.96), (-124.53, 33.32, -120.62, 35.67),
        (-124.45, 36.60, -121.56, 38.18), (-124.23, 48.11, -123.12, 48.36),
        (-123.50, 35.65, -121.28, 36.63), (-123.33, 47.01, -122.19, 49.01),
        (-122.98, 31.53, -117.09, 34.10), (-122.05, 33.67, -119.07, 34.91),
        (-119.96, 29.99, -116.92, 31.76)),
    "SL": ((-76.28, 44.17, -74.87, 45.01),),  # St. Lawrence River
}


def _touches(frame: Sequence[float], box: Box) -> bool:
    """``frame`` intersects ``box``, also for a frame across the antimeridian: written
    wrapped (``geo.MapFrame`` gives west 178.7, east -178.9 there) it is split in two, and
    written past +-180 degrees (181 = -179) it is also tried shifted by 360 degrees."""
    lonmin, latmin, lonmax, latmax = (float(v) for v in frame)
    parts = [(lonmin, lonmax)] if lonmin <= lonmax else [(lonmin, 180.0), (-180.0, lonmax)]
    return any(geo.bbox_intersects((lo + shift, latmin, hi + shift, latmax), box)
               for lo, hi in parts for shift in (0.0, -360.0, 360.0))


def _codes_touching(table: Dict[str, Tuple[Box, ...]],
                    frames: Iterable[Sequence[float]]) -> List[str]:
    frames = [tuple(f) for f in frames]
    return sorted(code for code, boxes in table.items()
                  if any(_touches(f, b) for f in frames for b in boxes))


def states_touching(frames: Iterable[Sequence[float]]) -> List[str]:
    """The sorted codes of the states and territories whose box touches any of ``frames``
    (map boxes as (lonmin, latmin, lonmax, latmax)); [] when none does (a site outside the
    US and its territories). Land only: marine areas are ``marine_areas_touching``."""
    return _codes_touching(STATE_BOXES, frames)


def marine_areas_touching(frames: Iterable[Sequence[float]]) -> List[str]:
    """The sorted NWS marine area codes (``MARINE_BOXES``) whose zones may lie on any of
    ``frames``."""
    return _codes_touching(MARINE_BOXES, frames)


def areas_touching(frames: Iterable[Sequence[float]]) -> List[str]:
    """The sorted codes of every alert area on the maps: states and territories plus marine
    areas (the two sets of codes never overlap). This is what ``WEATHER_ALERT_AREAS=auto``
    queries."""
    frames = [tuple(f) for f in frames]
    return sorted(states_touching(frames) + marine_areas_touching(frames))


def states_at(lon: float, lat: float) -> List[str]:
    """The sorted codes of the states and territories whose box contains the point (lon,
    lat). A box is larger than its state, so near a border there can be several; [] means
    the point is certainly in none of them."""
    return _codes_touching(STATE_BOXES, [(lon, lat, lon, lat)])
