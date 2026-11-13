"""Small shared helpers: time parsing/formatting and unit conversion."""
from __future__ import annotations

import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except Exception:                       # pragma: no cover - stdlib on 3.9+
    ZoneInfo = None

log = logging.getLogger("weather.util")
_tz_warned = set()
_iso_warned = set()

# The spellings of ISO 8601 that Python 3.9's fromisoformat refuses although they are valid
# and mean one instant: a lower-case "t" or "z", a fraction of other than 3 or 6 digits, an
# offset without its colon (+0000) or without minutes (+00), blanks around the text.
# (A fraction only after the seconds: "10:53.5" is a fraction of a minute, not 10:53:00.5.)
_ISO_LOOSE = re.compile(r"^(\d{4}-\d{2}-\d{2})[Tt ](\d{2}:\d{2}(?::\d{2})?)(?:(?<=:\d{2}:\d{2})[.,](\d{1,12}))?"
                        r"(?:([Zz])|([+-])(\d{2}):?(\d{2})?)?\Z")


# ---- time ----------------------------------------------------------------------
def parse_iso(s: Optional[str]) -> Optional[datetime]:
    """ISO-8601 with offset (NWS style, incl. trailing 'Z') -> aware datetime, or None.

    Also read, the same on every Python from 3.9 on: the spellings ``_ISO_LOOSE`` lists.
    A text without an offset is taken as UTC, and a date alone as its midnight UTC: some
    feeds (the road and storm reports) write UTC that way, and refusing it would cost them
    their times. A text that is not a time is None, with one log line per distinct text."""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        dt = _parse_iso_loose(s)
        if dt is None:
            if s not in _iso_warned and len(_iso_warned) < 100:
                _iso_warned.add(s)
                log.warning("not an ISO 8601 time: %.60r", s)
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_iso_loose(s: str) -> Optional[datetime]:
    """``parse_iso`` for the spellings of ``_ISO_LOOSE``, rewritten into the one form
    fromisoformat reads on 3.9 (6-digit fraction, +hh:mm). None when it is none of them."""
    m = _ISO_LOOSE.match(s.strip())
    if not m:
        return None
    day, clock, frac, zulu, sign, oh, om = m.groups()
    text = "%sT%s" % (day, clock if clock.count(":") == 2 else clock + ":00")
    if frac:
        text += "." + (frac + "000000")[:6]
    if zulu:
        text += "+00:00"
    elif sign:
        text += "%s%s:%s" % (sign, oh, om or "00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:                  # 25:00, February 30, an offset of 24 h or more
        return None


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


# How far ahead of this host's clock a time may be and still read "just now": clocks and
# report stamps differ by seconds, and the observation and precipitation checks accept a
# report up to 10 min ahead (nws.FUTURE_TOLERANCE_MIN, make_vlba_page._recent).
FUTURE_SLACK_S = 600


def fmt_age(seconds: Optional[float]) -> str:
    """'just now', '4 min', '2 h 10 min', '3 d' — for staleness labels."""
    if seconds is None:
        return "?"
    try:
        s = int(seconds)
    except (TypeError, ValueError, OverflowError):     # NaN, infinity, not a number
        return "?"
    if s < -FUTURE_SLACK_S:
        # a time well ahead of this host's clock is not "just now": the clock or the
        # source is wrong
        return "in the future"
    s = max(0, s)
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
    if deg is None or (isinstance(deg, float) and not math.isfinite(deg)):
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
    except (TypeError, ValueError, OverflowError):  # OverflowError: an integer of 400 digits
        return None
    if not math.isfinite(v):          # NaN, and 1e999, which the JSON reader makes infinity
        return None
    if not convert:
        return v
    v = convert(v)
    # a conversion of a value near the float limit (1e308 degC to degF) overflows to infinity
    return None if isinstance(v, float) and not math.isfinite(v) else v


def round_or_none(v: Optional[float], nd: int = 0):
    if v is None:
        return None
    return round(v, nd) if nd else int(round(v))
