"""GOES GLM lightning for the VLBA page: the flashes of the last minutes from the
``GLM-L2-LCFA`` granules of GOES-East and GOES-West on AWS Open Data, fetched once per run,
and the per-antenna view of them (flashes within the ring, the nearest flash, the flashes to
draw on the map).

Why it looks the way it does:

* Same data source and the same in-RAM parsing idea as ttustatus's ``glm_lightning``, but
  there is no daemon here: one cron run fetches the window once, for BOTH satellites, and
  the 30-minute hold after the last flash lives in the decision module's state file, not in
  a latch of this module. Granules are parsed from the downloaded bytes (h5py, else netCDF4;
  both optional): nothing is written to disk.
* Each site is served by the satellites whose sub-satellite point is within
  ``cfg.glm_max_arc_deg`` of great-circle arc (70 deg by default): GOES-East sees roughly 80
  deg, but GLM's detection efficiency collapses towards the limb, so Mauna Kea takes
  GOES-West only and St. Croix / Hancock GOES-East only, and a site outside every field of
  view is reported as "not covered" rather than silently "clear" (ttustatus's rule).
* Each run fetches the last ``cfg.glm_window_min`` minutes plus ``WINDOW_OVERLAP_S`` of the
  previous run's window: the newest granules of a window are often not published yet when
  the run lists them, and without the overlap no run would ever fetch them.
* The downloads (up to ~36 granules of ~200 KB per run) run in a small thread pool with one
  deadline for the whole fetch (``cfg.glm_timeout_s``, shortened to what the run budget
  leaves): a slow S3 must not eat the 5-minute cron interval. Whatever is not in by the
  deadline counts as failed and the pool is abandoned without waiting.
* The transport is the module's own ``_get`` (urllib with the configured User-Agent), used
  by the listing and the granule downloads alike, because ``weather.http.get_bytes`` keeps
  per-run state that is not thread-safe; the tests monkeypatch ``_get``.
* Nothing public raises on network or data problems: ``fetch`` returns a result with the
  parent's ``ok / stale / error / age_s`` keys (``stale`` is always False, there is no cached
  copy of lightning), and ``site_view`` never raises, so one bad granule or an unreadable
  result degrades to "unavailable" on the page, never to a missing page.

Granule facts (checked 2026-10-08 on a live file): netCDF-4/HDF5; ``flash_lat`` /
``flash_lon`` float32 in current files (older ones pack them as int16 with
``scale_factor`` / ``add_offset`` / ``_Unsigned``, exactly like ``event_lat`` still is);
``flash_time_offset_of_first_event`` int16 packed the same way with ``units`` "seconds since
<granule start>"; file attributes ``platform_ID`` (b"G18"), ``time_coverage_start``
(b"2026-10-08T15:40:20.0Z"). Keys look like ``GLM-L2-LCFA/2026/281/15/OR_GLM-L2-LCFA_G18_
s20262811540200_e20262811540400_c20262811540414.nc``: the ``_s`` field is YYYYDDDHHMMSS
plus a tenth of a second.
"""
from __future__ import annotations

import bisect
import concurrent.futures
import io
import logging
import math
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import numpy as np
except Exception:                       # pragma: no cover - optional heavy dependency
    np = None
try:
    import h5py
except Exception:                       # pragma: no cover - optional heavy dependency
    h5py = None
try:
    import netCDF4
except Exception:                       # pragma: no cover - optional heavy dependency
    netCDF4 = None

from weather import geo, http, util

log = logging.getLogger("vlba.glm")

PREFIX = "GLM-L2-LCFA"
GRANULE_S = 20                      # s of lightning per granule
MIN_BUDGET_S = 10.0                 # s of run budget below which nothing is attempted
BUDGET_RESERVE_S = 5.0              # s of run budget left to the rest of the run
MAX_REQUEST_TIMEOUT_S = 20.0        # s cap on one request (a granule downloads in ~1 s)
MIN_REQUEST_TIMEOUT_S = 3.0         # s floor for a deadline-shortened request
MAP_FLASH_CAP = 2000                # flashes handed to the map renderer per site
# Each window reaches this far back into the previous run's: a granule is published some
# 10-30 s after it ends, so the newest one or two of a run's window are not listed yet and
# would otherwise fall between two runs (13 % of the time blind, simulated 2026-10-08).
# A flash seen by two runs is harmless: the check needs one flash and holds 30 min.
WINDOW_OVERLAP_S = 60
# With the newest granule a previous run examined (``since``), a satellite's window reaches
# back to just after it, however late that run was, but never further than this.
MAX_CATCHUP_S = 900
# Two satellites' detections this close in time and place are one flash: both GLMs see most
# flashes where their views overlap, a fraction of a second and a few km (parallax) apart.
SAME_FLASH_S = 1.0
SAME_FLASH_KM = 20.0
_RESULT_KEYS = ("ok", "stale", "error", "age_s", "window_start", "window_end", "flashes",
                "sats", "deps", "elapsed_s", "skipped", "partial", "newest", "retry", "seen")

_KEY_START = re.compile(r"_s(\d{13})(\d)?")
_LIST_KEY = re.compile(r"<Key>([^<]+)</Key>")
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?\s*Z?\s*$")
_SINCE = re.compile(r"seconds\s+since\s+(.+)$", re.IGNORECASE)
_BUCKET_ID = re.compile(r"goes[-_]?(\d+)", re.IGNORECASE)


class _Deadline(Exception):
    """The run's GLM deadline passed before this request could start."""


# ---- dependencies ------------------------------------------------------------------------
def deps_available() -> bool:
    """numpy plus one HDF5/netCDF reader (h5py preferred, netCDF4 accepted)."""
    return np is not None and (h5py is not None or netCDF4 is not None)


def deps_note() -> str:
    """What is missing for the GLM layer ("" when nothing is), for the log and the page."""
    missing = []
    if np is None:
        missing.append("python3-numpy")
    if h5py is None and netCDF4 is None:
        missing.append("python3-h5py (or python3-netcdf4)")
    return "needs " + " and ".join(missing) if missing else ""


# ---- satellites and geometry ---------------------------------------------------------------
def satellite_id(bucket: str) -> str:
    """``"noaa-goes19"`` -> ``"G19"`` (the platform id GLM files carry); a bucket without a
    GOES number in its name is identified by its own name."""
    m = _BUCKET_ID.search(str(bucket or ""))
    if m:
        return "G%d" % int(m.group(1))
    return str(bucket or "").strip() or "?"


def _satellites(cfg) -> List[Tuple[str, str, float]]:
    """``[(id, bucket, sub_lon), ...]`` of ``cfg.glm_satellites``; two buckets of one
    platform (a mirror) get distinct ids so ``fetch`` and ``site_view`` keep them apart."""
    out: List[Tuple[str, str, float]] = []
    taken = set()
    for bucket, sub_lon in (cfg.glm_satellites or []):
        sid = satellite_id(bucket)
        if sid in taken:
            sid = "%s (%s)" % (sid, bucket)
        taken.add(sid)
        out.append((sid, str(bucket), float(sub_lon)))
    return out


def arc_deg(lat: float, lon: float, sub_lon: float) -> float:
    """Great-circle arc in degrees from the sub-satellite point (0, ``sub_lon``) of a
    geostationary satellite to (lat, lon)."""
    return math.degrees(geo.haversine_km(0.0, float(sub_lon), float(lat), float(lon))
                        / geo.EARTH_RADIUS_KM)


def site_in_fov(lat: float, lon: float, sub_lon: float, max_arc_deg: float) -> bool:
    """Whether a site is usefully covered by the satellite at ``sub_lon``: within
    ``max_arc_deg`` of arc from its sub-satellite point, as ttustatus decides it (the disc
    extends to ~80 deg, the detection efficiency near the limb does not)."""
    try:
        return arc_deg(lat, lon, sub_lon) < float(max_arc_deg)
    except (TypeError, ValueError):
        return False


# ---- keys and the listing ----------------------------------------------------------------
def key_start(key: Any) -> Optional[datetime]:
    """Start time of a granule from its key's ``_sYYYYDDDHHMMSSt`` field (aware UTC), or
    None when the key has no such field."""
    if not isinstance(key, str):
        return None
    m = _KEY_START.search(key)
    if not m:
        return None
    try:
        t = datetime.strptime(m.group(1), "%Y%j%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if m.group(2):
        t += timedelta(milliseconds=100 * int(m.group(2)))
    return t


def select_keys(keys: Sequence[Any], window_start: datetime, window_end: datetime,
                stride: int = 1) -> List[str]:
    """The keys whose granule starts within ``[window_start, window_end]``, newest first,
    thinned to every ``stride``-th one (the newest is always kept). Keys without a readable
    ``_s`` field are skipped, duplicates dropped."""
    try:
        stride = max(1, int(stride))
    except (TypeError, ValueError):
        stride = 1
    seen = set()
    rows = []
    for k in keys or ():
        t = key_start(k)
        if t is None or k in seen:
            continue
        seen.add(k)
        if window_start <= t <= window_end:
            rows.append((t, k))
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return [k for _, k in rows][::stride]


def parse_listing(body: bytes) -> List[str]:
    """Keys of an S3 ``list-type=2`` answer. ValueError when the body is not a bucket
    listing at all (an HTML error page, a truncated download): an empty listing of an
    hour that has no granules yet is valid and gives []."""
    text = (body or b"").decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) \
        else str(body or "")
    if "ListBucketResult" not in text:
        raise ValueError("not an S3 bucket listing (%d bytes)" % len(text))
    if "<IsTruncated>true</IsTruncated>" in text:
        log.debug("S3 listing truncated at max-keys; the newest granules may be missing")
    return _LIST_KEY.findall(text)


def hour_prefixes(window_start: datetime, window_end: datetime) -> List[str]:
    """The ``GLM-L2-LCFA/YYYY/DDD/HH/`` prefixes of every UTC hour the window touches
    (the current hour, plus the previous one when the window crosses the hour)."""
    out = []
    h = window_start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = window_end.astimezone(timezone.utc)
    while h <= end and len(out) < 48:
        out.append("%s/%s/" % (PREFIX, h.strftime("%Y/%j/%H")))
        h += timedelta(hours=1)
    return out


# ---- transport ---------------------------------------------------------------------------
def _get(url: str, cfg, timeout: float) -> bytes:
    """One anonymous GET (the listing and the granules alike). The only place in this
    module that touches the network; the tests replace it."""
    req = urllib.request.Request(url, headers={"User-Agent": cfg.user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _errtext(e: BaseException) -> str:
    s = str(e) or e.__class__.__name__
    return s if len(s) <= 160 else s[:157] + "..."


def _request_timeout(cfg, deadline_at: float) -> float:
    """The per-request timeout: ``cfg.http_timeout`` capped at 20 s and at what is left
    until the fetch deadline (never below 3 s, like the parent's budget-shortened
    attempts); raises ``_Deadline`` when the deadline has already passed."""
    left = deadline_at - time.monotonic()
    if left <= 0:
        raise _Deadline("deadline reached")
    base = min(float(getattr(cfg, "http_timeout", MAX_REQUEST_TIMEOUT_S) or MAX_REQUEST_TIMEOUT_S),
               MAX_REQUEST_TIMEOUT_S)
    return min(base, max(MIN_REQUEST_TIMEOUT_S, left))


def _list_bucket(bucket: str, prefixes: Sequence[str], cfg, deadline_at: float) -> List[str]:
    keys: List[str] = []
    for prefix in prefixes:
        url = "https://%s.s3.amazonaws.com/?list-type=2&max-keys=1000&prefix=%s" % (
            bucket, urllib.parse.quote(prefix, safe="/"))
        keys.extend(parse_listing(_get(url, cfg, _request_timeout(cfg, deadline_at))))
    return keys


def _fetch_granule(bucket: str, key: str, cfg, deadline_at: float) -> dict:
    url = "https://%s.s3.amazonaws.com/%s" % (bucket, urllib.parse.quote(key, safe="/"))
    return parse_granule(_get(url, cfg, _request_timeout(cfg, deadline_at)))


# ---- granule parsing (in RAM) ------------------------------------------------------------
def _text(v: Any) -> Optional[str]:
    """A string attribute as h5py / netCDF4 hand it over (bytes, numpy bytes_, str, or a
    one-element array of those) -> str, or None."""
    if v is None:
        return None
    if np is not None and isinstance(v, np.ndarray):
        if v.size == 0:
            return None
        v = v.ravel()[0]
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    return str(v)


def _scalar(v: Any) -> Optional[float]:
    """A numeric attribute (scalar or one-element array) -> float, or None."""
    if v is None:
        return None
    try:
        return float(np.asarray(v, dtype=np.float64).ravel()[0])
    except (TypeError, ValueError, IndexError):
        return None


def _parse_time(s: Optional[str]) -> Optional[datetime]:
    """``2026-10-08T15:40:20.0Z`` / ``2026-10-08 15:40:20.000`` -> aware UTC datetime
    (``datetime.fromisoformat`` of Python 3.9 rejects a one-digit fraction)."""
    if not s:
        return None
    m = _ISO.match(s.strip())
    if not m:
        return None
    frac = (m.group(7) or "0")[:6].ljust(6, "0")
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)),
                        int(m.group(5)), int(m.group(6)), int(frac), tzinfo=timezone.utc)
    except ValueError:
        return None


def _epoch_of(units: Optional[str]) -> Optional[datetime]:
    """The epoch of a ``seconds since <time>`` units attribute."""
    if not units:
        return None
    m = _SINCE.search(units.strip())
    return _parse_time(m.group(1)) if m else None


def _unpack(raw: Any, attrs: Any) -> Any:
    """A packed variable -> float64 values: ``_Unsigned`` int16 is reinterpreted as uint16
    first (the files store unsigned counts in a signed type, with the attribute saying so),
    then ``scale_factor`` and ``add_offset`` are applied (``np.ma`` fill values stay NaN)."""
    arr = np.ma.filled(np.ma.asarray(raw).astype(np.float64), np.nan) \
        if np.ma.isMaskedArray(raw) else np.asarray(raw)
    if arr.ndim == 0:
        arr = arr.reshape(1)
    if arr.dtype.kind == "i" and (_text(attrs.get("_Unsigned")) or "").strip().lower() == "true":
        arr = arr.view(np.dtype("u%d" % arr.dtype.itemsize))
    out = arr.astype(np.float64)
    scale = _scalar(attrs.get("scale_factor"))
    offset = _scalar(attrs.get("add_offset"))
    if scale is not None:
        out = out * scale
    if offset is not None:
        out = out + offset
    return out


def _read_h5(buf: bytes) -> Tuple[Optional[str], Optional[datetime], Any, Any, Any, Optional[str]]:
    with h5py.File(io.BytesIO(buf), "r") as f:
        sat = _text(f.attrs.get("platform_ID"))
        start = _parse_time(_text(f.attrs.get("time_coverage_start")))
        if "flash_lat" not in f or "flash_lon" not in f:
            # A granule without the flash variables is "no flashes" when it carries the
            # GLM file attributes, and not a granule at all otherwise.
            if sat is None and start is None:
                raise ValueError("no flash_lat / flash_lon variables")
            return sat, start, np.zeros(0), np.zeros(0), None, None
        lats = _unpack(f["flash_lat"][()], f["flash_lat"].attrs)
        lons = _unpack(f["flash_lon"][()], f["flash_lon"].attrs)
        offs = units = None
        if "flash_time_offset_of_first_event" in f:
            v = f["flash_time_offset_of_first_event"]
            offs = _unpack(v[()], v.attrs)
            units = _text(v.attrs.get("units"))
    return sat, start, lats, lons, offs, units


def _read_netcdf(buf: bytes) -> Tuple[Optional[str], Optional[datetime], Any, Any, Any, Optional[str]]:
    """The same with netCDF4, which unpacks ``scale_factor`` / ``add_offset`` /
    ``_Unsigned`` itself (auto mask-and-scale) and masks fill values."""
    ds = netCDF4.Dataset("inmem.nc", mode="r", memory=buf)
    try:
        sat = _text(getattr(ds, "platform_ID", None))
        start = _parse_time(_text(getattr(ds, "time_coverage_start", None)))
        v = ds.variables
        if "flash_lat" not in v or "flash_lon" not in v:
            if sat is None and start is None:
                raise ValueError("no flash_lat / flash_lon variables")
            return sat, start, np.zeros(0), np.zeros(0), None, None
        lats = _unpack(v["flash_lat"][:], {})
        lons = _unpack(v["flash_lon"][:], {})
        offs = units = None
        if "flash_time_offset_of_first_event" in v:
            offs = _unpack(v["flash_time_offset_of_first_event"][:], {})
            units = _text(getattr(v["flash_time_offset_of_first_event"], "units", None))
    finally:
        ds.close()
    return sat, start, lats, lons, offs, units


def _assemble(sat, start, lats, lons, offs, units) -> dict:
    lats = np.asarray(lats, dtype=np.float64).ravel()
    lons = np.asarray(lons, dtype=np.float64).ravel()
    if lats.shape != lons.shape:
        raise ValueError("flash_lat and flash_lon differ in length (%d vs %d)"
                         % (lats.size, lons.size))
    good = (np.isfinite(lats) & np.isfinite(lons) & (np.abs(lats) <= 90.0)
            & (np.abs(lons) <= 180.0))
    epoch = _epoch_of(units) or start
    times: List[Optional[datetime]] = []
    if offs is not None and epoch is not None:
        offs = np.asarray(offs, dtype=np.float64).ravel()
        if offs.shape == lats.shape:
            for i in np.flatnonzero(good):
                o = float(offs[i])
                times.append(epoch + timedelta(seconds=o) if math.isfinite(o) and abs(o) < 3600
                             else start)
    if len(times) != int(good.sum()):
        times = [start] * int(good.sum())
    return {"sat": sat.strip() if isinstance(sat, str) else None, "start": start,
            "lats": [float(x) for x in lats[good]], "lons": [float(x) for x in lons[good]],
            "times": times}


def parse_granule(buf: bytes) -> dict:
    """A GLM-L2-LCFA granule's bytes -> ``{"sat": "G18"|None, "start": aware UTC datetime
    |None, "lats": [float], "lons": [float], "times": [aware datetime|None]}`` (one entry
    per flash; a flash's time is the granule start plus ``flash_time_offset_of_first_event``,
    the start alone when the offset is missing). Flashes with a non-finite or out-of-range
    position are dropped. ValueError when the bytes are not a GLM granule, or when the
    parsing libraries are missing; ``fetch`` catches it per granule."""
    if not isinstance(buf, (bytes, bytearray, memoryview)):
        raise ValueError("granule must be bytes, not %s" % type(buf).__name__)
    if not deps_available():
        raise ValueError("cannot parse GLM granules: %s" % deps_note())
    buf = bytes(buf)
    try:
        if h5py is not None:
            parts = _read_h5(buf)
        else:
            parts = _read_netcdf(buf)
        return _assemble(*parts)
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001 - h5py/netCDF4 raise OSError, KeyError, ... on junk
        raise ValueError("not a GLM granule: %s: %s" % (type(e).__name__, _errtext(e)))


# ---- the fetch -----------------------------------------------------------------------------
def _as_utc(now: Optional[datetime]) -> datetime:
    if now is None:
        return util.utcnow()
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _sat_entry(bucket: str, sub_lon: float) -> dict:
    return {"bucket": bucket, "sub_lon": float(sub_lon), "ok": False, "error": None,
            "listed": 0, "selected": 0, "fetched": 0, "failed": 0, "flashes": 0}


def _base_result(cfg, now: Optional[datetime]) -> dict:
    end = _as_utc(now)
    try:
        minutes = int(cfg.glm_window_min)
    except (TypeError, ValueError, AttributeError):
        minutes = 5
    return {"ok": False, "stale": False, "error": None, "age_s": None,
            "window_start": end - timedelta(minutes=minutes, seconds=WINDOW_OVERLAP_S), "window_end": end,
            "flashes": [], "sats": {}, "deps": deps_available(), "elapsed_s": 0.0,
            "skipped": None, "partial": False, "newest": {}, "retry": {}, "seen": {}}


def _skip_reason(cfg, satellites: list) -> Optional[str]:
    """Why nothing is fetched this run (None = go ahead)."""
    if not getattr(cfg, "glm_enabled", False):
        return "GLM layer disabled (VLBA_GLM=0)"
    if not satellites:
        return "no GLM satellite configured (VLBA_GLM_BUCKETS is empty)"
    if not deps_available():
        return "GLM parsing libraries missing: %s" % deps_note()
    left = http.budget_left()
    if left is not None and left < MIN_BUDGET_S:
        return "run budget nearly spent (%.0f s left): GLM fetch skipped" % max(0.0, left)
    return None


def _deadline_s(cfg) -> float:
    """The whole fetch's deadline: ``cfg.glm_timeout_s``, shortened to leave 5 s of the run
    budget to the rest of the run."""
    try:
        d = float(cfg.glm_timeout_s)
    except (TypeError, ValueError, AttributeError):
        d = 45.0
    left = http.budget_left()
    if left is not None:
        d = min(d, left - BUDGET_RESERVE_S)
    return max(1.0, d)


def _wait(futures: Sequence, deadline_at: float):
    """``concurrent.futures.wait`` until the deadline: (done, not done)."""
    if not futures:
        return set(), set()
    return concurrent.futures.wait(list(futures), timeout=max(0.0, deadline_at - time.monotonic()))


def _since(since: Any, sid: str) -> Optional[datetime]:
    """The start of the newest granule of ``sid`` a previous run examined (``since`` as the
    state file keeps it: {id: epoch seconds}), or None."""
    v = since.get(sid) if isinstance(since, dict) else None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)                            # an int beyond float's range raises here
        return datetime.fromtimestamp(f, tz=timezone.utc) if math.isfinite(f) else None
    except (OverflowError, OSError, ValueError):
        return None


def _retry_set(retried: Any, sid: str) -> set:
    """The granule starts (epoch s, to 0.1 s) of ``sid`` in ``retried`` (as the state file
    keeps the granules to fetch again and those examined: {id: [epoch s]}); junk is
    ignored."""
    v = retried.get(sid) if isinstance(retried, dict) else None
    out = set()
    for x in v if isinstance(v, list) else []:
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            try:
                if math.isfinite(float(x)):
                    out.add(round(float(x), 1))
            except OverflowError:
                continue
    return out


def _pending(retried: Any) -> Dict[str, List[float]]:
    """``retried`` (or ``seen``) cleaned for the state file again (a run that fetched
    nothing keeps it)."""
    if not isinstance(retried, dict):
        return {}
    return {str(k): sorted(_retry_set(retried, k)) for k in retried if _retry_set(retried, k)}


def _fetch(cfg, now: Optional[datetime], since: Any = None, retried: Any = None, seen: Any = None) -> dict:
    res = _base_result(cfg, now)
    satellites = _satellites(cfg)
    res["newest"] = {sid: v for sid, v in (since.items() if isinstance(since, dict) else ())
                     if _since(since, sid) is not None and sid in {x[0] for x in satellites}}
    skip = _skip_reason(cfg, satellites)
    if skip:
        res["skipped"] = res["error"] = skip
        res["retry"], res["seen"] = _pending(retried), _pending(seen)
        log.info("GLM: %s", skip)
        return res

    deadline_at = time.monotonic() + _deadline_s(cfg)
    end = res["window_end"]
    reach = end - timedelta(seconds=MAX_CATCHUP_S)      # nothing older is fetched any more
    sats: Dict[str, dict] = {}
    order: List[str] = []
    lo: Dict[str, datetime] = {}
    again: Dict[str, set] = {}          # granules that failed in the last run: fetched first
    examined: Dict[str, set] = {}       # granules the last run examined (or gave up)
    given_up: Dict[str, int] = {}
    for sid, bucket, sub_lon in satellites:
        sats[sid] = _sat_entry(bucket, sub_lon)
        order.append(sid)
        # just after the newest granule the last run examined, when that is older than the
        # usual window (a late or a missed run): nothing in between goes unexamined
        lo[sid] = res["window_start"]
        t = _since(since, sid)
        if t is not None and t < lo[sid]:
            lo[sid] = max(t + timedelta(milliseconds=100), min(lo[sid], reach))
        # no granule starts in the future or before 1970: junk (a hand edit) is dropped
        again[sid] = {x for x in _retry_set(retried, sid) if 0 < x <= end.timestamp() + 3600}
        examined[sid] = _retry_set(seen, sid)
        old = {x for x in again[sid] if x < reach.timestamp()}
        again[sid] -= old
        given_up[sid] = len(old)
    starts = [lo[sid] for sid in order] + [datetime.fromtimestamp(min(again[sid]), tz=timezone.utc)
                                           for sid in order if again[sid]]
    res["window_start"] = min(starts) if starts else res["window_start"]
    prefixes = hour_prefixes(res["window_start"], end)
    res["sats"] = sats

    try:
        workers = max(1, int(cfg.glm_workers))
    except (TypeError, ValueError, AttributeError):
        workers = 4
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="glm")
    try:
        # 1. the hour listings, one task per satellite
        listing = {sid: pool.submit(_list_bucket, sats[sid]["bucket"], prefixes, cfg, deadline_at)
                   for sid in order}
        done, _ = _wait(list(listing.values()), deadline_at)
        keys: Dict[str, List[str]] = {}
        for sid in order:
            fut = listing[sid]
            if fut not in done:
                fut.cancel()
                sats[sid]["error"] = "listing: deadline reached"
                continue
            try:
                keys[sid] = fut.result()
            except _Deadline:
                sats[sid]["error"] = "listing: deadline reached"
            except Exception as e:  # noqa: BLE001 - any failure = this satellite unavailable
                sats[sid]["error"] = "listing failed: %s" % _errtext(e)
        # 2. the granules: the ones to fetch again first (the oldest of all: the newest-first
        #    rest would leave them to the deadline), then the window newest first, every
        #    satellite interleaved so a deadline hits both evenly
        tasks: List[Tuple[str, str, bool, Any]] = []
        selected: Dict[str, List[str]] = {}
        retry_keys: Dict[str, List[str]] = {}
        for sid in order:
            if sid not in keys:
                # not listed: what was to be fetched again, and what was examined, stays so
                if again[sid]:
                    res["retry"][sid] = sorted(again[sid])
                if examined[sid]:
                    res["seen"][sid] = sorted(examined[sid])
                continue
            sats[sid]["listed"] = len(keys[sid])
            selected[sid] = select_keys(keys[sid], lo[sid], end, getattr(cfg, "glm_stride", 1))
            by_start = {}
            for k in keys[sid]:
                t = key_start(k)
                if t is not None and round(t.timestamp(), 1) in again[sid]:
                    by_start.setdefault(round(t.timestamp(), 1), k)
            retry_keys[sid] = sorted(by_start.values(), key=lambda k: key_start(k), reverse=True)
            given_up[sid] += len(again[sid]) - len(by_start)        # gone from the store
            first = set(retry_keys[sid])
            selected[sid] = [k for k in selected[sid] if k not in first]
            sats[sid]["selected"] = len(selected[sid]) + len(retry_keys[sid])
            if not sats[sid]["selected"]:
                sats[sid]["error"] = "no granules in the window (%d listed)" % len(keys[sid])
        for group, is_retry in ((retry_keys, True), (selected, False)):
            for i in range(max((len(v) for v in group.values()), default=0)):
                for sid in order:
                    if sid in group and i < len(group[sid]):
                        key = group[sid][i]
                        tasks.append((sid, key, is_retry, pool.submit(_fetch_granule, sats[sid]["bucket"], key,
                                                                      cfg, deadline_at)))
        done, _ = _wait([t[3] for t in tasks], deadline_at)
        first_error: Dict[str, str] = {}
        for sid, key, is_retry, fut in tasks:
            entry = sats[sid]
            sent = True
            if fut not in done:
                sent = not fut.cancel()         # cancelled before it started: never asked for
                err = "deadline reached"
            else:
                try:
                    g = fut.result()
                    err = None
                except _Deadline:
                    err = "deadline reached"
                except Exception as e:  # noqa: BLE001 - one bad granule never ends the run
                    err = _errtext(e)
            started = key_start(key)
            ts = round(started.timestamp(), 1) if started is not None else None
            if err is not None:
                log.debug("GLM %s: %s: %s", sid, key.rsplit("/", 1)[-1], err)
                if not is_retry and ts is not None and ts in examined[sid]:
                    # the overlap with the last run, which examined it: no gap
                    entry["refetch_failed"] = entry.get("refetch_failed", 0) + 1
                    continue
                entry["failed"] += 1
                first_error.setdefault(sid, err)
                if is_retry and sent:
                    entry["dropped"] = entry.get("dropped", 0) + 1      # its second failure
                    res["seen"].setdefault(sid, set()).add(ts)
                elif ts is not None:
                    res["retry"].setdefault(sid, []).append(ts)
                continue
            entry["fetched"] += 1
            if ts is not None:
                res["seen"].setdefault(sid, set()).add(ts)
            fallback = g.get("start") or started or res["window_end"]
            if g.get("sat") and not sid.startswith(g["sat"]):
                log.debug("GLM %s: granule %s says platform %s", sid, key, g["sat"])
            n = 0
            for lat, lon, t in zip(g["lats"], g["lons"], g["times"]):
                res["flashes"].append({"lat": lat, "lon": lon, "sat": sid, "t": t or fallback})
                n += 1
            entry["flashes"] += n
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    res["seen"] = {sid: sorted(v) for sid, v in res["seen"].items()}
    # the marker moves to the newest granule of the window: every one up to it was examined,
    # is to be fetched again (``retry``) or was given up (the run is partial then)
    for sid in order:
        newest = [key_start(k) for k in selected.get(sid, [])]
        newest = [t.timestamp() for t in newest if t is not None]
        if newest and max(newest) > (res["newest"].get(sid) or 0.0):
            res["newest"][sid] = max(newest)

    # 3. the verdict on the run
    problems = []
    for sid in order:
        entry = sats[sid]
        if given_up[sid]:
            entry["failed"] += given_up[sid]
            entry["dropped"] = entry.get("dropped", 0) + given_up[sid]
            first_error.setdefault(sid, "a granule to fetch again is gone or too old")
        if entry["error"] is None and entry["failed"]:
            entry["error"] = "%d of %d granules failed (%s)%s" % (
                entry["failed"], entry["selected"] + given_up[sid], first_error.get(sid, "?"),
                "; %d of them for the second run in a row, not tried again" % entry["dropped"]
                if entry.get("dropped") else "")
        entry["ok"] = entry["error"] is None and entry["fetched"] > 0
        if not entry["ok"]:
            problems.append("%s: %s" % (sid, entry["error"] or "nothing fetched"))
            log.warning("GLM %s (%s): %s", sid, entry["bucket"], entry["error"] or "nothing fetched")
    res["partial"] = bool(problems)
    res["ok"] = any(sid in keys and sats[sid]["fetched"] > 0 for sid in order)
    res["age_s"] = 0.0 if res["ok"] else None
    res["error"] = "; ".join(problems) if problems else None
    summary = ["%s %d/%d granules, %d flashes"
               % (sid, sats[sid]["fetched"], sats[sid]["selected"], sats[sid]["flashes"])
               for sid in order]
    log.info("GLM %s: %s", "ok" if res["ok"] else "FAILED", "; ".join(summary))
    return res


def fetch(cfg, now: Optional[datetime] = None, since: Any = None, retried: Any = None,
          seen: Any = None) -> dict:
    """The flashes of the last ``cfg.glm_window_min`` minutes, plus ``WINDOW_OVERLAP_S`` of
    the previous run's window (granules published late), from every configured satellite.
    ``since`` ({satellite id: start of the newest granule a previous run examined, epoch
    s}, as ``newest`` gives it) stretches a satellite's window back to just after that
    granule when it is older (a late or missed run), by at most ``MAX_CATCHUP_S``. A
    granule that fails (or is not reached before the deadline) is fetched again by the next
    run, before anything else (``retry`` in the result, handed back as ``retried``): once;
    failing again, or older than ``MAX_CATCHUP_S`` by then, it is given up and that run is
    partial. A granule the last run examined (``seen``, as the result gives it), fetched
    again in the overlap, that fails is no gap; one published too late for the last run
    is. Returns

        {"ok": bool, "stale": False, "error": str|None, "age_s": 0.0|None,
         "window_start", "window_end": aware UTC datetimes (window_end = now),
         "flashes": [{"lat", "lon", "sat": "G19", "t": aware datetime}, ...],
         "sats": {"G19": {"bucket", "sub_lon", "ok", "error", "listed", "selected",
                          "fetched", "failed", "flashes", ["dropped"], ["refetch_failed"]},
                  ...},
         "deps": bool, "elapsed_s": float, "skipped": str|None, "partial": bool,
         "newest": {satellite id: start of the newest granule of the window, epoch s}
                   (carried over from ``since`` for a satellite not listed),
         "retry": {satellite id: [starts of the granules to fetch again, epoch s]},
         "seen": {satellite id: [starts of the granules examined or given up, epoch s]}}

    ``ok`` when at least one satellite was listed and gave at least one parsed granule;
    ``partial`` when any satellite failed (listing, no granules, a granule that did not
    arrive or parse), with ``error`` naming what failed even when ``ok``. Nothing is
    attempted (``skipped`` says why, ``error`` repeats it) when the layer is disabled, no
    satellite is configured, the parsing libraries are missing or the run budget is below
    10 s. The listing covers the current hour and, when the window crosses it, the previous
    one. Downloads run in ``cfg.glm_workers`` threads under one deadline
    (``cfg.glm_timeout_s``, or the run budget minus 5 s when that is shorter); granules
    not in by then count as failed. Never raises."""
    t0 = time.monotonic()
    try:
        res = _fetch(cfg, now, since, retried, seen)
    except Exception as e:  # noqa: BLE001 - any surprise = unavailable, never a crash
        log.warning("GLM fetch failed: %s: %s", type(e).__name__, e)
        res = _base_result(cfg, now)
        res["error"] = "%s: %s" % (type(e).__name__, _errtext(e))
        res["newest"] = {k: v for k, v in (since.items() if isinstance(since, dict) else ())
                         if _since(since, k) is not None}
        res["retry"], res["seen"] = _pending(retried), _pending(seen)
    res["elapsed_s"] = round(time.monotonic() - t0, 3)
    return res


# ---- the per-site view ------------------------------------------------------------------
def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from (lat1, lon1) to (lat2, lon2), 0..360 clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _finite(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _one_per_flash(rows: List[tuple]) -> List[tuple]:
    """``rows`` ((distance, lat, lon, time, sat), ...) with a flash that several satellites
    detected kept once. Rows are taken nearest first; a detection is dropped when a kept one
    of another satellite lies within ``SAME_FLASH_S`` and ``SAME_FLASH_KM`` of it and has
    not absorbed a detection of that satellite yet (a kept detection absorbs at most one per
    other satellite). The nearest detection is always kept, so the count is at least 1
    whenever any detection lies in the ring; two real flashes close in time and place, each
    seen by one satellite only, can still be counted once (the accepted price of matching by
    proximity). A detection without a time is always kept. The result is in distance order."""
    kept: List[tuple] = []
    absorbed: List[set] = []                    # per kept detection: satellites it absorbed
    times: List[float] = []                     # kept detections with a time, sorted ...
    where: List[int] = []                       # ... and their places in ``kept``
    for r in sorted(rows, key=lambda row: row[0]):
        t = r[3].timestamp() if isinstance(r[3], datetime) else None
        if t is not None:
            i = bisect.bisect_left(times, t - SAME_FLASH_S)
            match = None
            while i < len(times) and times[i] <= t + SAME_FLASH_S:
                k = where[i]
                if kept[k][4] != r[4] and r[4] not in absorbed[k] \
                        and geo.haversine_km(r[1], r[2], kept[k][1], kept[k][2]) <= SAME_FLASH_KM:
                    match = k
                    break
                i += 1
            if match is not None:
                absorbed[match].add(r[4])
                continue
        kept.append(r)
        absorbed.append(set())
        if t is not None:
            j = bisect.bisect(times, t)
            times.insert(j, t)
            where.insert(j, len(kept) - 1)
    return kept


def _empty_view(cfg) -> dict:
    try:
        window_min = int(cfg.glm_window_min)
    except (TypeError, ValueError, AttributeError):
        window_min = None
    return {"covered": False, "sats": [], "available": False, "partial": False, "error": None,
            "count_window": 0, "count_by_sat": {}, "last_in_ring_t": None, "nearest_km": None,
            "nearest_bearing": None, "nearest_t": None, "nearest_sat": None, "flashes_on_map": [],
            "window_min": window_min}


def _site_view(out: dict, res: Any, site: dict, cfg, map_radius_km: float) -> dict:
    lat, lon = float(site["lat"]), float(site["lon"])
    satellites = _satellites(cfg)
    covering = [sid for sid, _, sub_lon in satellites
                if site_in_fov(lat, lon, sub_lon, cfg.glm_max_arc_deg)]
    out["sats"] = covering
    out["covered"] = bool(covering)
    if not covering:
        out["error"] = ("no GLM satellite configured" if not satellites else
                        "outside the field of view of every configured GLM satellite")
        return out
    if not isinstance(res, dict) or not res.get("ok"):
        err = res.get("error") or res.get("skipped") if isinstance(res, dict) else None
        out["error"] = str(err) if err else "GLM data unavailable"
        return out
    w0, w1 = res.get("window_start"), res.get("window_end")
    if isinstance(w0, datetime) and isinstance(w1, datetime) and w1 > w0:
        out["window_min"] = int(round((w1 - w0).total_seconds() / 60.0))
    sats = res.get("sats") if isinstance(res.get("sats"), dict) else {}
    problems = []
    complete = got_data = False
    for sid in covering:
        entry = sats.get(sid)
        if not isinstance(entry, dict):
            problems.append("%s: not fetched this run" % sid)
            continue
        if entry.get("fetched"):
            got_data = True
        if entry.get("ok"):
            complete = True
        else:
            problems.append("%s: %s" % (sid, entry.get("error") or "nothing fetched"))
    # Every covering satellite sees the whole site, so one satellite's complete window is a
    # complete answer; the others' problems only make the view partial.
    out["available"] = complete
    out["partial"] = bool(problems) and got_data
    out["error"] = "; ".join(problems) if problems else None

    cover = set(covering)
    ring_km = float(cfg.glm_km)
    rows = []                                   # (distance, lat, lon, time, sat)
    for fl in res.get("flashes") if isinstance(res.get("flashes"), list) else []:
        if not isinstance(fl, dict) or fl.get("sat") not in cover:
            continue
        flat, flon = _finite(fl.get("lat")), _finite(fl.get("lon"))
        if flat is None or flon is None or abs(flat) > 90.0 or abs(flon) > 180.0:
            continue
        rows.append((geo.haversine_km(lat, lon, flat, flon), flat, flon, fl.get("t"), fl["sat"]))
    rows.sort(key=lambda r: r[0])
    out["count_by_sat"] = {sid: sum(1 for r in rows if r[4] == sid and r[0] <= ring_km) for sid in covering}
    # both satellites detect most flashes where both see the site: each flash once
    near = _one_per_flash([r for r in rows if r[0] <= max(float(map_radius_km), ring_km)])
    out["count_window"] = sum(1 for r in near if r[0] <= ring_km)
    in_ring_t = [r[3] for r in near if r[0] <= ring_km and isinstance(r[3], datetime)]
    out["last_in_ring_t"] = max(in_ring_t) if in_ring_t else None
    out["flashes_on_map"] = [(r[1], r[2]) for r in near if r[0] <= float(map_radius_km)][:MAP_FLASH_CAP]
    if rows:
        d, flat, flon, t, sid = rows[0]
        out["nearest_km"] = round(d, 1)
        out["nearest_bearing"] = util.deg_to_cardinal(_bearing_deg(lat, lon, flat, flon), points=8)
        out["nearest_t"] = t if isinstance(t, datetime) else None
        out["nearest_sat"] = sid
    return out


def site_view(res: Any, site: dict, cfg, now: Optional[datetime] = None,
              map_radius_km: float = 150.0) -> dict:
    """What one antenna sees in a ``fetch`` result:

        {"covered": bool (a configured satellite within cfg.glm_max_arc_deg of arc),
         "sats": [ids of the covering satellites], "available": bool, "partial": bool,
         "error": str|None, "count_window": flashes within cfg.glm_km of the site (a flash
         both satellites detected counted once), "count_by_sat": {"G19": n, ...} (each
         satellite's own detections within cfg.glm_km), "last_in_ring_t": time of the newest
         flash counted in the ring | None,
         "nearest_km": float|None, "nearest_bearing": "NW"|None, "nearest_t": aware datetime
         |None, "nearest_sat": "G19"|None, "flashes_on_map": [(lat, lon), ...] within
         ``map_radius_km`` (the 2000 nearest at most), "window_min": minutes the fetch
         window spans (``cfg.glm_window_min`` plus the overlap, rounded)}

    Only the covering satellites' flashes count. ``available`` = covered, the result ok
    and at least one covering satellite delivered its whole window (each covering satellite
    sees the whole site); ``partial`` = some covering satellite failed or is incomplete
    while some granules did arrive (without ``available`` the count is then a lower bound;
    a flash it holds is still a flash). ``now`` is accepted for symmetry with the other
    views and unused: the flashes carry the fetch window. Never raises (a bad result ->
    ``available`` False)."""
    out = _empty_view(cfg)
    try:
        return _site_view(out, res, site, cfg, map_radius_km)
    except Exception as e:  # noqa: BLE001 - a malformed result must not take the page down
        log.warning("GLM site view failed for %s: %s: %s", (site or {}).get("slug") if
                    isinstance(site, dict) else site, type(e).__name__, e)
        out.update(available=False, partial=False, count_window=0, count_by_sat={}, last_in_ring_t=None,
                   nearest_km=None,
                   nearest_bearing=None, nearest_t=None, nearest_sat=None, flashes_on_map=[],
                   error="bad GLM result: %s: %s" % (type(e).__name__, _errtext(e)))
        return out
