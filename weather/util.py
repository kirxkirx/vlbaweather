"""Small shared helpers: time parsing/formatting and unit conversion."""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except Exception:                       # pragma: no cover - stdlib on 3.9+
    ZoneInfo = None

log = logging.getLogger("weather.util")
_tz_warned = set()


# ---- time ----------------------------------------------------------------------
def parse_iso(s: Optional[str]) -> Optional[datetime]:
    """ISO-8601 with offset (NWS style, incl. trailing 'Z') -> aware datetime, or None."""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def tzinfo_for(name: Optional[str]):
    """ZoneInfo for an IANA name, UTC if unknown (warned once per name)."""
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            if name not in _tz_warned:
                _tz_warned.add(name)
                log.warning("unknown time zone %r, using UTC", name)
    return timezone.utc


def to_local(dt: Optional[datetime], tzname: Optional[str]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.astimezone(tzinfo_for(tzname))


def fmt_local(dt: Optional[datetime], tzname: Optional[str], fmt: str = "%a %b %-d %H:%M %Z") -> str:
    """Format an aware datetime in ``tzname``; '?' for None."""
    loc = to_local(dt, tzname)
    if loc is None:
        return "?"
    try:
        return loc.strftime(fmt)
    except ValueError:                  # platforms without %-d
        return loc.strftime(fmt.replace("%-d", "%d"))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def age_minutes(dt: Optional[datetime], now: Optional[datetime] = None) -> Optional[float]:
    if dt is None:
        return None
    now = now or utcnow()
    return (now - dt).total_seconds() / 60.0


def fmt_age(seconds: Optional[float]) -> str:
    """'just now', '4 min', '2 h 10 min', '3 d' — for staleness labels."""
    if seconds is None:
        return "?"
    s = max(0, int(seconds))
    if s < 60:
        return "just now"
    m = s // 60
    if m < 60:
        return "%d min" % m
    h, m = divmod(m, 60)
    if h < 48:
        return "%d h %02d min" % (h, m) if m else "%d h" % h
    return "%d d" % (h // 24)


def fmt_duration_minutes(minutes: Optional[float]) -> str:
    if minutes is None:
        return "?"
    return fmt_age(minutes * 60.0)


def unix_ts() -> float:
    return time.time()


def next_hour(dt: datetime) -> datetime:
    return (dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))


# ---- units ---------------------------------------------------------------------
def c_to_f(c: Optional[float]) -> Optional[float]:
    return None if c is None else c * 9.0 / 5.0 + 32.0


def f_to_c(f: Optional[float]) -> Optional[float]:
    return None if f is None else (f - 32.0) * 5.0 / 9.0


def kmh_to_mph(v: Optional[float]) -> Optional[float]:
    return None if v is None else v * 0.621371


def mph_to_kmh(v: Optional[float]) -> Optional[float]:
    return None if v is None else v / 0.621371


def ms_to_mph(v: Optional[float]) -> Optional[float]:
    return None if v is None else v * 2.236936


def pa_to_inhg(v: Optional[float]) -> Optional[float]:
    return None if v is None else v * 0.0002952998


def pa_to_hpa(v: Optional[float]) -> Optional[float]:
    return None if v is None else v / 100.0


def m_to_mi(v: Optional[float]) -> Optional[float]:
    return None if v is None else v / 1609.344


def m_to_km(v: Optional[float]) -> Optional[float]:
    return None if v is None else v / 1000.0


def km_to_mi(v: Optional[float]) -> Optional[float]:
    return None if v is None else v / 1.609344


def mm_to_in(v: Optional[float]) -> Optional[float]:
    return None if v is None else v / 25.4


_CARDINALS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def deg_to_cardinal(deg: Optional[float], points: int = 16) -> str:
    """Compass direction for a bearing in degrees ('' for None)."""
    if deg is None or (isinstance(deg, float) and math.isnan(deg)):
        return ""
    step = 16 // points
    idx = int((deg % 360) / (360.0 / points) + 0.5) % points
    return _CARDINALS[idx * step]


def quantity(q, convert=None) -> Optional[float]:
    """Unwrap an NWS ``{"unitCode": ..., "value": x}`` quantity (or a bare number). Returns
    None for missing values; applies ``convert`` when given."""
    if q is None:
        return None
    v = q.get("value") if isinstance(q, dict) else q
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(v):
        return None
    return convert(v) if convert else v


def round_or_none(v: Optional[float], nd: int = 0):
    if v is None:
        return None
    return round(v, nd) if nd else int(round(v))
