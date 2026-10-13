"""Sun altitude and twilight times, pure Python (NOAA solar calculator equations).

Why a hand-rolled solver: the page shows sunset / civil / nautical / astronomical twilight
for observers, and pulling in astropy or ephem for four numbers a day is not worth the
dependency. The equations below are the ones behind https://gml.noaa.gov/grad/solcalc/
(Meeus-derived low-precision series), which are good to well under a minute at these
latitudes — far better than the 2-minute target in DESIGN.md.

Conventions
* ``sun_altitude_deg`` returns the *geometric* altitude of the sun's centre (no refraction).
  Twilight is defined geometrically (-6/-12/-18 deg), and sunrise/sunset use -0.833 deg,
  which already folds in the standard refraction plus the solar semi-diameter.
* Every event is found from the hour angle at the wanted altitude, measured from the local
  solar noon of that *local calendar date*; the declination and equation of time are then
  re-evaluated at the first estimate of the event time (one refinement pass, as NOAA does).
* Events that do not occur (midnight sun, polar night, or a twilight that never ends in
  summer at high latitudes) are ``None``. Nothing here touches the network and nothing
  raises for finite coordinates: acos arguments are range-checked, not trusted.
* Times keep sub-minute precision here; rounding to the displayed minute is the page's job.

What the page uses: ``sun_summary``'s ``phase`` (the altitude band the sun is in now) and
``upcoming`` (every event from 30 min ago to 24 h ahead, in time order, across local
midnight and DST changes). ``evening``/``morning`` are kept for backward compatibility only.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from . import util

log = logging.getLogger("weather.sun")

# Altitude of the sun's centre that defines each event (degrees).
ALT_SUNRISE = -0.833
ALT_CIVIL = -6.0
ALT_NAUTICAL = -12.0
ALT_ASTRO = -18.0

# The sun is "down enough" for the page to switch to its night palette below this.
NIGHT_ALT_DEG = -6.0

# Altitude bands for sun_summary()["phase"]: the first whose floor the altitude reaches wins.
PHASES = (
    (ALT_SUNRISE, "day"),
    (ALT_CIVIL, "civil twilight"),
    (ALT_NAUTICAL, "nautical twilight"),
    (ALT_ASTRO, "astronomical twilight"),
)
PHASE_NIGHT = "night"

# day_events key -> label used in sun_summary()["upcoming"].
EVENT_LABELS = {
    "astro_dawn": "Astro dawn",
    "nautical_dawn": "Nautical dawn",
    "civil_dawn": "Civil dawn",
    "sunrise": "Sunrise",
    "solar_noon": "Solar noon",
    "sunset": "Sunset",
    "civil_dusk": "Civil dusk",
    "nautical_dusk": "Nautical dusk",
    "astro_dusk": "Astro dusk",
}

# sun_summary()["upcoming"] window around "now".
UPCOMING_LOOKBACK = timedelta(minutes=30)
UPCOMING_AHEAD = timedelta(hours=24)

_J2000 = 2451545.0         # Julian Day of 2000-01-01 12:00 UTC
_UNIX_EPOCH_JD = 2440587.5  # Julian Day of 1970-01-01 00:00 UTC


# ---- time helpers ---------------------------------------------------------------
def _as_utc(dt: datetime) -> datetime:
    """Aware UTC datetime; a naive input is taken to be UTC already (never local time,
    because the generator runs under systemd where "local" is whatever the host says)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _julian_century(dt_utc: datetime) -> float:
    """Julian centuries since J2000.0 — the time argument of every NOAA series."""
    jd = dt_utc.timestamp() / 86400.0 + _UNIX_EPOCH_JD
    return (jd - _J2000) / 36525.0


# ---- the NOAA series --------------------------------------------------------------
def _sun_position(jc: float) -> Tuple[float, float]:
    """(declination deg, equation of time min) for a Julian century ``jc``.

    Straight transcription of the NOAA spreadsheet columns: geometric mean longitude and
    anomaly, orbital eccentricity, equation of centre, apparent longitude (with the
    nutation/aberration term in omega), corrected obliquity, then declination and the
    equation of time. Kept as one function so the intermediate names read like the sheet."""
    mean_long = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360.0
    mean_anom = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    eccent = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    m = math.radians(mean_anom)
    eq_centre = (math.sin(m) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
                 + math.sin(2 * m) * (0.019993 - 0.000101 * jc)
                 + math.sin(3 * m) * 0.000289)
    true_long = mean_long + eq_centre
    omega = math.radians(125.04 - 1934.136 * jc)
    app_long = true_long - 0.00569 - 0.00478 * math.sin(omega)
    mean_obliq = 23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
    obliq = math.radians(mean_obliq + 0.00256 * math.cos(omega))
    decl = math.degrees(math.asin(math.sin(obliq) * math.sin(math.radians(app_long))))

    y = math.tan(obliq / 2.0) ** 2
    l0 = math.radians(mean_long)
    eot = 4.0 * math.degrees(
        y * math.sin(2 * l0)
        - 2 * eccent * math.sin(m)
        + 4 * eccent * y * math.sin(m) * math.cos(2 * l0)
        - 0.5 * y * y * math.sin(4 * l0)
        - 1.25 * eccent * eccent * math.sin(2 * m))
    return decl, eot


def _hour_angle_deg(lat: float, decl: float, alt: float) -> Optional[float]:
    """Hour angle (deg, >= 0) at which the sun's centre is at altitude ``alt``; None when
    the sun never reaches that altitude on this day (midnight sun / polar night)."""
    lat_r, decl_r = math.radians(lat), math.radians(decl)
    denom = math.cos(lat_r) * math.cos(decl_r)
    if abs(denom) < 1e-12:                                  # at the pole itself
        return None
    cos_ha = (math.sin(math.radians(alt)) - math.sin(lat_r) * math.sin(decl_r)) / denom
    if cos_ha < -1.0 or cos_ha > 1.0:
        return None
    return math.degrees(math.acos(cos_ha))


# ---- public API -------------------------------------------------------------------
def sun_altitude_deg(lat: float, lon: float, dt_utc: datetime) -> float:
    """Geometric altitude of the sun's centre in degrees at ``dt_utc`` (aware, or naive
    taken as UTC). Positive above the horizon; -6 is the civil-twilight threshold."""
    dt = _as_utc(dt_utc)
    decl, eot = _sun_position(_julian_century(dt))
    minutes = dt.hour * 60.0 + dt.minute + dt.second / 60.0 + dt.microsecond / 6.0e7
    true_solar_min = (minutes + eot + 4.0 * lon) % 1440.0
    hour_angle = math.radians(true_solar_min / 4.0 - 180.0)
    lat_r, decl_r = math.radians(lat), math.radians(decl)
    cos_zen = (math.sin(lat_r) * math.sin(decl_r)
               + math.cos(lat_r) * math.cos(decl_r) * math.cos(hour_angle))
    cos_zen = max(-1.0, min(1.0, cos_zen))
    return 90.0 - math.degrees(math.acos(cos_zen))


def _solar_noon_utc(lon: float, ref_utc: datetime) -> datetime:
    """The solar noon (sun due south/north, hour angle 0) nearest to ``ref_utc``.

    Anchoring on the *nearest* noon rather than "the UTC date's noon" is what makes the
    local-calendar-date semantics right for any longitude: the caller passes local clock
    noon of the wanted date, and this returns the solar noon of that same local day."""
    day0 = ref_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    t = ref_utc
    for _ in range(2):                                      # second pass refines the EoT
        _decl, eot = _sun_position(_julian_century(t))
        cand = day0 + timedelta(minutes=720.0 - 4.0 * lon - eot)
        t = min((cand + timedelta(days=k) for k in (-1, 0, 1)),
                key=lambda c: abs((c - ref_utc).total_seconds()))
    return t


def _event_utc(lat: float, lon: float, noon_utc: datetime, alt: float,
               before_noon: bool) -> Optional[datetime]:
    """UTC instant at which the sun crosses ``alt`` before (rising) or after (setting) the
    given solar noon, or None if it never does that day.

    NOAA's formula is  t = 720 - 4*(lon +/- HA) - EoT  with HA and EoT evaluated at t itself,
    so relative to solar noon (720 - 4*lon - EoT_noon) the offset is
    (EoT_noon - EoT_t) -/+ 4*HA_t. Two passes: first with noon values, then re-evaluated
    at the first estimate — the sun's declination moves ~0.4 deg/day near the equinoxes,
    which alone would shift a sunrise by tens of seconds if left at the noon value."""
    _decl_noon, eot_noon = _sun_position(_julian_century(noon_utc))
    t = noon_utc
    for _ in range(2):
        decl, eot = _sun_position(_julian_century(t))
        ha = _hour_angle_deg(lat, decl, alt)
        if ha is None:
            return None
        offset = (eot_noon - eot) + (-4.0 * ha if before_noon else 4.0 * ha)
        t = noon_utc + timedelta(minutes=offset)
    return t


def day_events(lat: float, lon: float, date_local, tzname: Optional[str]) -> Dict[str, Optional[datetime]]:
    """Sun events for one local calendar date, as aware datetimes in ``tzname``.

    Keys: sunrise, solar_noon, sunset, civil_dawn, civil_dusk, nautical_dawn, nautical_dusk,
    astro_dawn, astro_dusk. A value is None when the event does not occur on that date.
    ``date_local`` may be a ``date`` or a ``datetime`` (its date part is used)."""
    if isinstance(date_local, datetime):
        date_local = date_local.date()
    elif not isinstance(date_local, date):
        raise TypeError("date_local must be a date or datetime, got %r" % (date_local,))
    tz = util.tzinfo_for(tzname)
    local_noon = datetime.combine(date_local, time(12, 0), tzinfo=tz)
    noon_utc = _solar_noon_utc(lon, local_noon.astimezone(timezone.utc))

    def local(dt: Optional[datetime]) -> Optional[datetime]:
        return None if dt is None else dt.astimezone(tz)

    events = {"solar_noon": local(noon_utc)}
    for name, alt in (("sun", ALT_SUNRISE), ("civil", ALT_CIVIL),
                      ("nautical", ALT_NAUTICAL), ("astro", ALT_ASTRO)):
        rise = local(_event_utc(lat, lon, noon_utc, alt, before_noon=True))
        sett = local(_event_utc(lat, lon, noon_utc, alt, before_noon=False))
        if name == "sun":
            events["sunrise"], events["sunset"] = rise, sett
        else:
            events[name + "_dawn"], events[name + "_dusk"] = rise, sett
    log.debug("sun events %s %.4f,%.4f: rise=%s set=%s", date_local, lat, lon,
              events["sunrise"], events["sunset"])
    return events


def sun_phase(alt_deg: float) -> str:
    """Name of the altitude band the sun's centre is in: "day" (at or above the -0.833 deg
    sunrise/sunset altitude, i.e. the sun's upper limb is visible), "civil twilight" (down to
    -6), "nautical twilight" (-12), "astronomical twilight" (-18), else "night"."""
    for floor, name in PHASES:
        if alt_deg >= floor:
            return name
    return PHASE_NIGHT


def upcoming_events(lat: float, lon: float, tzname: Optional[str],
                    now_utc: Optional[datetime] = None) -> List[dict]:
    """Every sun event from 30 min before ``now_utc`` to 24 h after it, in time order.

    Each item is ``{"label": str, "dt": aware local datetime, "passed": dt < now}`` with the
    labels of ``EVENT_LABELS``. Candidates come from ``day_events`` of the local dates
    yesterday .. the day after tomorrow and are filtered by the absolute-time window
    [now - 30 min, now + 24 h] (both ends inclusive), so the list is right across local
    midnight and DST changes: which *date* an event belongs to never matters, only when it
    happens. Yesterday covers a dusk just before midnight inside the 30-min look-back; the
    day after tomorrow only matters where an event of that date can fall before its local
    midnight (a dawn near solar midnight in a zone whose clock runs behind the sun), and is
    filtered out everywhere else. Events that do not occur (polar day/night) are skipped,
    so the list may be short, or hold only solar noons. At mid latitudes it holds 9 events,
    up to 11 when some fall in the look-back and recur just before the window closes."""
    now = _as_utc(now_utc or util.utcnow())
    tz = util.tzinfo_for(tzname)
    today = now.astimezone(tz).date()
    lo, hi = now - UPCOMING_LOOKBACK, now + UPCOMING_AHEAD
    items: List[dict] = []
    for k in (-1, 0, 1, 2):
        events = day_events(lat, lon, today + timedelta(days=k), tzname)
        for key, label in EVENT_LABELS.items():
            dt = events.get(key)
            if dt is not None and lo <= dt <= hi:
                items.append({"label": label, "dt": dt, "passed": dt < now})
    # Sort by absolute time: aware datetimes sharing one tzinfo compare by wall clock and
    # ignore ``fold``, which would misorder two events inside a repeated fall-back hour.
    items.sort(key=lambda it: it["dt"].timestamp())
    return items


def sun_summary(lat: float, lon: float, tzname: Optional[str],
                now_utc: Optional[datetime] = None) -> dict:
    """Everything the page's sun/twilight row needs for one site.

    ``sun_alt_deg`` is the sun's altitude now and ``phase`` its band (``sun_phase``);
    ``is_night`` (below -6 deg, i.e. past civil dusk) drives the page's default palette only.
    ``today``/``tomorrow`` are ``day_events`` for the local date of ``now`` and the next one.
    ``upcoming`` is ``upcoming_events``: the chronological list of events from 30 min ago to
    24 h ahead, each ``{"label", "dt", "passed"}`` — what the page renders, because it is
    right at any hour (before dawn it starts with *today's* dawn, not tomorrow's).

    ``evening`` (today's dusk sequence) and ``morning`` (tomorrow's dawn sequence), each four
    ``(label, aware local datetime or None)`` pairs, are kept for backward compatibility; the
    page no longer uses them because they are wrong between midnight and sunrise."""
    now = _as_utc(now_utc or util.utcnow())
    tz = util.tzinfo_for(tzname)
    now_local = now.astimezone(tz)
    sun_alt = sun_altitude_deg(lat, lon, now)
    today = day_events(lat, lon, now_local.date(), tzname)
    tomorrow = day_events(lat, lon, now_local.date() + timedelta(days=1), tzname)
    evening: List[Tuple[str, Optional[datetime]]] = [
        ("Sunset", today["sunset"]),
        ("Civil dusk", today["civil_dusk"]),
        ("Nautical dusk", today["nautical_dusk"]),
        ("Astro dusk", today["astro_dusk"]),
    ]
    morning: List[Tuple[str, Optional[datetime]]] = [
        ("Astro dawn", tomorrow["astro_dawn"]),
        ("Nautical dawn", tomorrow["nautical_dawn"]),
        ("Civil dawn", tomorrow["civil_dawn"]),
        ("Sunrise", tomorrow["sunrise"]),
    ]
    return {
        "tz": tzname,
        "now_local": now_local,
        "sun_alt_deg": sun_alt,
        "phase": sun_phase(sun_alt),
        "is_night": sun_alt < NIGHT_ALT_DEG,
        "today": today,
        "tomorrow": tomorrow,
        "upcoming": upcoming_events(lat, lon, tzname, now),
        "evening": evening,
        "morning": morning,
    }
