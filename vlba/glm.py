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
  ``cfg.glm_max_arc_deg`` of great-circle arc (62 deg by default: GLM's field of view ends
  about there diagonally, measured in the granules on 2026-10-14, where GOES-18 recorded
  nothing near the GBT, 64.9 deg away), so Mauna Kea takes GOES-West only and St. Croix,
  Hancock and the GBT GOES-East only, and a site outside every field of view is reported
  as "not covered" rather than silently "clear" (ttustatus's rule).
* Each run fetches the last ``cfg.glm_window_min`` minutes plus ``WINDOW_OVERLAP_S`` of the
  previous run's window: the newest granules of a window are often not published yet when
  the run lists them, and without the overlap no run would ever fetch them.
* A satellite's window is complete only when what its bucket publishes covers it: the
  newest granule ends at most ``cfg.glm_max_lag_s`` before the run, no more than
  ``MAX_GAP_S`` in a row is missing, the listing is whole, and nothing is listed after the
  run's own time (a slow host clock). Fetching every listed granule is not enough: a
  lagging or holed feed would otherwise read "no flash in the last 31 min". The next run
  starts where the span examined in one piece ended, so minutes published late are
  examined while their flashes can still count.
* The downloads (about 185 granules per run with both satellites and the 30-minute window,
  45-75 MB: GOES-19's granules run 300-600 KB, GOES-18's about 200 KB; ~25-30 s measured
  2026-10-14) run in a small thread pool with one deadline
  for the whole fetch (``cfg.glm_timeout_s``, shortened to what the run budget leaves): a
  slow S3 must not eat the run's network budget. Whatever is not in by the deadline counts
  as failed and the pool is abandoned without waiting.
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

import concurrent.futures
import io
import logging
import math
import re
import threading
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
# back to just after it, however late that run was, but never further than ``max_catchup_s``:
# this, the window with its overlap, or the hold, whichever is longest. An older flash cannot
# change the verdict.
MAX_CATCHUP_S = 900
# Two satellites' detections this close in time and place are one flash: both GLMs see most
# flashes where their views overlap, a fraction of a second and a few km (parallax) apart.
SAME_FLASH_S = 1.0
SAME_FLASH_KM = 20.0
# At most this many kept detections are compared with a new one: real storms put a few
# flashes per second within the map radius, so the match is found long before; a crafted
# granule of thousands of same-second flashes costs N times this, not N squared. A match
# missed this way counts a flash twice, never zero times.
SAME_FLASH_SCAN = 64
# Completeness of a satellite's window, from what its bucket lists. Measured 2026-11-12
# (two hours and the first 1000 keys of a day per bucket): a granule starts every 20 s, a
# single one is missing two or three times in a thousand (40 s between starts), and a
# granule is published 5-75 s after its end, so the newest one listed ends 10-95 s before
# the listing. Unpublished time between two granules (or between the window's start and the
# first one) longer than MAX_GAP_S, six granules in a row, is a hole: the satellite's
# window is incomplete. The lag limit for the newest granule is ``cfg.glm_max_lag_s``.
MAX_GAP_S = 120
DEFAULT_MAX_LAG_S = 300
# A listed granule that starts this long after the run's own time cannot exist yet: the
# host clock is behind (the listings above end 30-60 s before the reply's Date).
CLOCK_SLACK_S = 60
# A listing of one hour holds 180 keys; continuation pages are followed up to this many.
MAX_LIST_PAGES = 5
# Caps on what one reply or granule can cost. Real granules are 0.17-0.6 MB (measured
# 2026-10-14 and 2026-11-12) and hold a few hundred flashes (23 and 556 in the two read on
# 2026-11-12); a page of 1000 listed keys is 0.4 MB. A run holds about 185 granules.
MAX_BODY_BYTES = 8 * 1024 * 1024
READ_CHUNK = 64 * 1024
READ_GRACE_S = 2.0                  # s a body may take beyond the request timeout
MAX_FLASHES_PER_GRANULE = 100000
MAX_FLASHES_PER_RUN = 500000
# A flash's first event may lie before its granule's start (seen: -1.3 s; the packing of
# ``flash_time_offset_of_first_event`` reaches back 5 s), never after its end.
FLASH_EARLY_S = 5.0
_RESULT_KEYS = ("ok", "stale", "error", "age_s", "window_start", "window_end", "flashes",
                "sats", "deps", "elapsed_s", "skipped", "partial", "newest", "retry", "seen")

_KEY_START = re.compile(r"_s(\d{13})(\d)?")
_KEY_CREATED = re.compile(r"_c(\d{13,14})")
_LIST_KEY = re.compile(r"<Key>([^<]+)</Key>")
_LIST_ROOT = re.compile(r"\s*(?:<\?xml[^<>]{0,200}\?>\s*)?<ListBucketResult[\s>]")
_LIST_TRUNCATED = re.compile(r"<IsTruncated>\s*true\s*</IsTruncated>", re.IGNORECASE)
_LIST_TOKEN = re.compile(r"<NextContinuationToken>([^<]{1,2048})</NextContinuationToken>")
# Anchored at both ends and applied to a stripped string of bounded length (_parse_time):
# no backtracking over runs of blanks.
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?Z?$")
_SINCE = re.compile(r"seconds\s+since\s+(.+)$", re.IGNORECASE)
_BUCKET_ID = re.compile(r"goes[-_]?(\d+)", re.IGNORECASE)
_TEXT_MAX = 64                      # characters of a time attribute worth reading

# netCDF4-python releases the GIL around netcdf-c / HDF5 calls, which are not thread-safe
# as packaged: granules parsed with it go one at a time (the downloads stay parallel).
# h5py serialises its calls with a lock of its own.
_NETCDF_LOCK = threading.Lock()


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
    ``_s`` field are skipped, duplicates dropped; of two keys with the same start (a granule
    published again) only the one with the latest ``_c`` creation stamp is kept, so its
    flashes are not counted twice."""
    try:
        stride = max(1, int(stride))
    except (TypeError, ValueError):
        stride = 1
    best: Dict[datetime, str] = {}
    for k in keys or ():
        t = key_start(k)
        if t is None or not (window_start <= t <= window_end):
            continue
        if t not in best or _newer_copy(k, best[t]):
            best[t] = k
    rows = sorted(best.items(), reverse=True)
    return [k for _, k in rows][::stride]


def _newer_copy(a: str, b: str) -> bool:
    """Whether key ``a`` is a later publication of the same granule than key ``b`` (its
    ``_c`` creation stamp is later; the key itself breaks a tie)."""
    def rank(k: str) -> Tuple[str, str]:
        m = _KEY_CREATED.search(k)
        return (m.group(1) if m else "", k)
    return rank(a) > rank(b)


def _listing_page(body: Any) -> Tuple[List[str], Optional[str]]:
    """Keys of one page of an S3 ``list-type=2`` answer and its continuation token (None when
    the listing is complete). ValueError when the body is not a bucket listing (an HTML
    error page, a truncated download), or says it is truncated without a token."""
    text = (body or b"").decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) \
        else str(body or "")
    if not _LIST_ROOT.match(text):
        raise ValueError("not an S3 bucket listing (%d bytes)" % len(text))
    token = None
    if _LIST_TRUNCATED.search(text):
        m = _LIST_TOKEN.search(text)
        if not m:
            raise ValueError("S3 listing truncated without a continuation token")
        token = m.group(1)
    return _LIST_KEY.findall(text), token


def parse_listing(body: bytes) -> List[str]:
    """Keys of an S3 ``list-type=2`` answer. ValueError when the body is not a bucket
    listing at all (an HTML error page, a truncated download), or is only the first page of
    a longer one (its newest granules would be missing: S3 lists oldest first). An empty
    listing of an hour that has no granules yet is valid and gives []."""
    keys, token = _listing_page(body)
    if token is not None:
        raise ValueError("S3 listing truncated after %d keys" % len(keys))
    return keys


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
    module that touches the network; the tests replace it. ``timeout`` bounds each socket
    operation, not a reply fed a byte at a time, so the request runs in a daemon thread the
    worker waits for until ``timeout`` plus ``READ_GRACE_S``: a status line and headers sent
    a byte at a time cannot hold the worker, nor the interpreter at exit (it waits for the
    pool's workers, never for a daemon thread). The body is read in chunks against the same
    limit and refused beyond ``MAX_BODY_BYTES``, so no reply fills the memory."""
    limit = float(timeout) + READ_GRACE_S
    stop = time.monotonic() + limit
    box: Dict[str, Any] = {}

    def work() -> None:
        try:
            box["body"] = _get_body(url, cfg, timeout, stop)
        except BaseException as e:  # noqa: BLE001 - handed to the worker as it is
            box["error"] = e
    th = threading.Thread(target=work, name="glm-get", daemon=True)
    th.start()
    th.join(limit + 1.0)            # the body loop stops itself at ``stop``; this is the headers'
    if "error" in box:
        raise box["error"]
    if "body" not in box:
        raise TimeoutError("reply not complete after %.0f s" % limit)
    return box["body"]


def _get_body(url: str, cfg, timeout: float, stop: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": cfg.user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        headers = getattr(r, "headers", None)
        length = headers.get("Content-Length") if headers is not None else None
        if length and length.strip().isdigit() and int(length) > MAX_BODY_BYTES:
            raise ValueError("reply of %d bytes refused (cap %d MB)" % (int(length), MAX_BODY_BYTES >> 20))
        read = getattr(r, "read1", None) or r.read      # read1: whatever has arrived
        parts: List[bytes] = []
        size = 0
        while True:
            chunk = read(READ_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise ValueError("reply larger than %d MB refused" % (MAX_BODY_BYTES >> 20))
            parts.append(chunk)
            if time.monotonic() > stop:
                raise TimeoutError("reply not complete after %.0f s" % (float(timeout) + READ_GRACE_S))
    return b"".join(parts)


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
    """Every key under ``prefixes``, continuation pages followed (``MAX_LIST_PAGES`` per
    prefix; a listing still truncated after them raises ValueError: its newest keys would
    be missing)."""
    keys: List[str] = []
    for prefix in prefixes:
        base = "https://%s.s3.amazonaws.com/?list-type=2&max-keys=1000&prefix=%s" % (
            bucket, urllib.parse.quote(prefix, safe="/"))
        url = base
        for _ in range(MAX_LIST_PAGES):
            page, token = _listing_page(_get(url, cfg, _request_timeout(cfg, deadline_at)))
            keys.extend(page)
            if token is None:
                break
            url = base + "&continuation-token=" + urllib.parse.quote(token, safe="")
        else:
            raise ValueError("S3 listing still truncated after %d pages (%d keys)" % (MAX_LIST_PAGES, len(keys)))
    return keys


def _fetch_granule(bucket: str, key: str, cfg, deadline_at: float) -> dict:
    url = "https://%s.s3.amazonaws.com/%s" % (bucket, urllib.parse.quote(key, safe="/"))
    return parse_granule(_get(url, cfg, _request_timeout(cfg, deadline_at)))


class _FlashBudget:
    """The flashes a run may still hold, shared by the download workers. The parsed granules
    wait in their futures until every download is in, so the cap of the run is applied
    where they are parsed: a granule past it keeps only what is left (``capped`` set), and
    no more than ``MAX_FLASHES_PER_RUN`` flashes are ever held, however many granules a
    broken or hostile bucket fills to ``MAX_FLASHES_PER_GRANULE``."""

    def __init__(self, n: int):
        self.left = max(0, int(n))
        self.lock = threading.Lock()

    def take(self, n: int) -> int:
        with self.lock:
            k = min(max(0, int(n)), self.left)
            self.left -= k
            return k


def _fetch_within(budget: _FlashBudget, bucket: str, key: str, cfg, deadline_at: float) -> dict:
    """``_fetch_granule`` within the run's flash budget."""
    g = _fetch_granule(bucket, key, cfg, deadline_at)
    n = len(g.get("lats") or ())
    k = budget.take(n)
    if k < n:
        g = dict(g, lats=list(g["lats"][:k]), lons=list(g["lons"][:k]), times=list(g["times"][:k]), capped=True)
    return g


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
    (``datetime.fromisoformat`` of Python 3.9 rejects a one-digit fraction). A string
    longer than any such time is no time (and never reaches the pattern)."""
    if not s or len(s) > 4 * _TEXT_MAX:
        return None
    s = s.strip()
    if len(s) > _TEXT_MAX:
        return None
    m = _ISO.match(s)
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
    if not units or len(units) > 4 * _TEXT_MAX:
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


def _check_shapes(shapes: Sequence[Tuple[str, Any]]) -> None:
    """Refuse flash variables that are not one-dimensional, differ in length or hold more
    than ``MAX_FLASHES_PER_GRANULE`` flashes, from their declared shapes, before anything is
    read: a dataset that was declared huge and never written reads back as millions of fill
    values from a file of a few KB."""
    n = None
    for name, shape in shapes:
        shape = tuple(shape)
        if len(shape) != 1:
            raise ValueError("%s is not one-dimensional (shape %s)" % (name, shape))
        if shape[0] > MAX_FLASHES_PER_GRANULE:
            raise ValueError("%s declares %d flashes (cap %d)" % (name, shape[0], MAX_FLASHES_PER_GRANULE))
        if n is not None and shape[0] != n:
            raise ValueError("the flash variables differ in length (%d vs %d)" % (n, shape[0]))
        n = shape[0]


def _flash_vars(present: Sequence[str], sat: Optional[str], start: Optional[datetime]) -> bool:
    """Whether the granule has the flash variables. A granule without both is "no flashes"
    when it carries the GLM file attributes, and not a granule at all otherwise; one with only
    one of them is broken."""
    has = [name in present for name in ("flash_lat", "flash_lon")]
    if all(has):
        return True
    if any(has):
        raise ValueError("only one of flash_lat / flash_lon present")
    if sat is None and start is None:
        raise ValueError("no flash_lat / flash_lon variables")
    return False


def _read_h5(buf: bytes) -> Tuple[Optional[str], Optional[datetime], Any, Any, Any, Optional[str]]:
    with h5py.File(io.BytesIO(buf), "r") as f:
        sat = _text(f.attrs.get("platform_ID"))
        start = _parse_time(_text(f.attrs.get("time_coverage_start")))
        if not _flash_vars([k for k in ("flash_lat", "flash_lon") if k in f], sat, start):
            return sat, start, np.zeros(0), np.zeros(0), None, None
        names = [k for k in ("flash_lat", "flash_lon", "flash_time_offset_of_first_event") if k in f]
        _check_shapes([(k, f[k].shape) for k in names])
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
    ``_Unsigned`` itself (auto mask-and-scale) and masks fill values. One granule at a
    time (``_NETCDF_LOCK``)."""
    with _NETCDF_LOCK:
        ds = netCDF4.Dataset("inmem.nc", mode="r", memory=buf)
        try:
            sat = _text(getattr(ds, "platform_ID", None))
            start = _parse_time(_text(getattr(ds, "time_coverage_start", None)))
            v = ds.variables
            if not _flash_vars([k for k in ("flash_lat", "flash_lon") if k in v], sat, start):
                return sat, start, np.zeros(0), np.zeros(0), None, None
            names = [k for k in ("flash_lat", "flash_lon", "flash_time_offset_of_first_event") if k in v]
            _check_shapes([(k, v[k].shape) for k in names])
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
    good = np.isfinite(lats) & np.isfinite(lons)
    # A finite position out of range is another convention (longitudes 0..360) or a broken
    # file, not a fill value: nothing in it can be placed with confidence. Non-finite ones
    # (masked fill values) are dropped, unless no flash is left.
    if (good & ((np.abs(lats) > 90.0) | (np.abs(lons) > 180.0))).any():
        raise ValueError("flash positions out of range (latitude beyond 90 or longitude beyond 180 deg)")
    if lats.size and not good.any():
        raise ValueError("none of the %d flash positions is readable" % lats.size)
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
    the start alone when the offset is missing). Flashes with a non-finite position (a fill
    value) are dropped. ValueError when the bytes are not a GLM granule, when only one of
    ``flash_lat`` / ``flash_lon`` is present, when a flash variable is not one-dimensional,
    the variables differ in length or declare more than ``MAX_FLASHES_PER_GRANULE``
    flashes, when a finite position is out of range or none is readable, or when the
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


def _hold_s(cfg) -> float:
    try:
        return float(cfg.glm_hold_s)
    except (TypeError, ValueError, AttributeError):
        return 1800.0


def max_catchup_s(cfg) -> float:
    """How far back a run reaches for a late or missed run's granules and for those to fetch
    again: ``MAX_CATCHUP_S``, the window plus its overlap, or the hold plus one granule,
    whichever is longest: every granule whose flashes still count is within reach, and none
    is fetched whose flashes no longer can (31 min at the 30-minute window, 30 min 20 s at a
    5-minute one)."""
    try:
        window_s = 60.0 * int(cfg.glm_window_min)
    except (TypeError, ValueError, AttributeError):
        window_s = 300.0
    return max(float(MAX_CATCHUP_S), window_s + WINDOW_OVERLAP_S, _hold_s(cfg) + GRANULE_S)


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


def _max_lag_s(cfg) -> float:
    """``cfg.glm_max_lag_s``: how long before the run the newest granule of a complete
    window may end."""
    try:
        v = float(cfg.glm_max_lag_s)
    except (TypeError, ValueError, AttributeError):
        return float(DEFAULT_MAX_LAG_S)
    return v if math.isfinite(v) and v > 0 else float(DEFAULT_MAX_LAG_S)


def _span_words(seconds: float) -> str:
    """``95`` -> ``"95 s"``, ``1500`` -> ``"25 min"``."""
    return "%.0f s" % seconds if seconds < 90 else "%.0f min" % (seconds / 60.0)


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")


def _coverage(starts: Sequence[float], lo: float, end: float,
              max_lag: float) -> Tuple[List[str], Optional[float], Optional[float]]:
    """How the granules a bucket publishes for one satellite (``starts``: their start
    times, epoch s) cover its window ``[lo, end]``: (what is missing, in plain words; the
    start of the newest granule reached from ``lo`` without a hole; the end of that span,
    at most ``end``). A hole is more than ``MAX_GAP_S`` of unpublished time between two
    granules or before the first; a lag is a newest granule that ends more than
    ``max_lag`` before ``end``. ([], None, None) when nothing starts in the window (the
    caller says so in its own words)."""
    ts = sorted({float(s) for s in starts if lo <= s <= end})
    if not ts:
        return [], None, None
    problems = []
    holes = []
    reached = None
    covered_to = lo
    for s in ts:
        if s - covered_to > MAX_GAP_S:
            holes.append((covered_to, s))
        elif not holes:
            reached = s
        covered_to = max(covered_to, s + GRANULE_S)
    if holes:
        problems.append("%s of the window not published (from %s UTC)"
                        % (_span_words(sum(b - a for a, b in holes)), _hhmm(holes[0][0])))
    lag = end - (ts[-1] + GRANULE_S)
    if lag > max_lag:
        problems.append("newest granule %s old" % _span_words(lag))
    return problems, reached, (min(end, reached + GRANULE_S) if reached is not None else None)


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
    reach = end - timedelta(seconds=max_catchup_s(cfg))     # nothing older is fetched any more
    hold_s = _hold_s(cfg)
    # a granule that ended before the hold began can only hold flashes that no longer count:
    # losing it leaves no gap that matters
    past_hold = end.timestamp() - hold_s - GRANULE_S
    sats: Dict[str, dict] = {}
    order: List[str] = []
    lo: Dict[str, datetime] = {}
    again: Dict[str, set] = {}          # granules that failed in the last run: fetched first
    examined: Dict[str, set] = {}       # granules the last run examined (or gave up)
    given_up: Dict[str, int] = {}
    for sid, bucket, sub_lon in satellites:
        sats[sid] = _sat_entry(bucket, sub_lon)
        order.append(sid)
        # just after the newest granule the last run examined in one piece, when that is
        # older than the usual window (a late or a missed run, a feed that lagged or had a
        # hole then): nothing in between goes unexamined. Without that marker (a first run,
        # a lost state file) the whole reach: flashes up to the hold old still count.
        lo[sid] = res["window_start"]
        t = _since(since, sid)
        if t is None:
            lo[sid] = min(lo[sid], reach)
        elif t < lo[sid]:
            lo[sid] = max(t + timedelta(milliseconds=100), min(lo[sid], reach))
        # no granule starts in the future or before 1970: junk (a hand edit) is dropped
        again[sid] = {x for x in _retry_set(retried, sid) if 0 < x <= end.timestamp() + 3600}
        examined[sid] = _retry_set(seen, sid)
        old = {x for x in again[sid] if x < reach.timestamp()}
        again[sid] -= old
        given_up[sid] = len({x for x in old if x > past_hold})
    starts = [lo[sid] for sid in order] + [datetime.fromtimestamp(min(again[sid]), tz=timezone.utc)
                                           for sid in order if again[sid]]
    # the window the page names is the span every satellite examined in one piece (the
    # latest of their starts); a granule fetched again from before it adds no minutes to it
    res["window_start"] = max(lo.values()) if lo else res["window_start"]
    prefixes = hour_prefixes(min(starts) if starts else res["window_start"], end)
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
        gaps: Dict[str, List[str]] = {}         # why a listed satellite's window is incomplete
        marker: Dict[str, Optional[float]] = {}     # newest granule reached without a hole
        max_lag = _max_lag_s(cfg)
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
            by_start: Dict[float, str] = {}
            listed_starts = []
            for k in keys[sid]:
                t = key_start(k)
                if t is None:
                    continue
                listed_starts.append(t.timestamp())
                x = round(t.timestamp(), 1)
                if x in again[sid] and (x not in by_start or _newer_copy(k, by_start[x])):
                    by_start[x] = k
            retry_keys[sid] = sorted(by_start.values(), key=lambda k: key_start(k), reverse=True)
            given_up[sid] += sum(1 for x in again[sid] if x not in by_start and x > past_hold)    # gone
            first = set(retry_keys[sid])
            selected[sid] = [k for k in selected[sid] if k not in first]
            sats[sid]["selected"] = len(selected[sid]) + len(retry_keys[sid])
            if not sats[sid]["selected"]:
                sats[sid]["error"] = "no granules in the window (%d listed)" % len(keys[sid])
            # does what the bucket publishes cover the window? (the granules the last run
            # examined count, whether listed now or not)
            gaps[sid], marker[sid], span_end = _coverage(
                listed_starts + sorted(examined[sid]), lo[sid].timestamp(), end.timestamp(), max_lag)
            sats[sid]["span_start"] = lo[sid].timestamp()
            sats[sid]["span_end"] = span_end
            ahead = max(listed_starts, default=0.0) - end.timestamp()
            if ahead > CLOCK_SLACK_S:
                # a granule that starts after the run's own time cannot have been published:
                # the host clock is behind, and every window it computes lies in the past
                gaps[sid].insert(0, "granules listed up to %s after this run's time: the host clock is "
                                    "behind" % _span_words(ahead))
                log.error("GLM %s: the bucket lists granules up to %.0f s after the host's time: "
                          "the host clock is behind; check its time synchronisation (NTP)", sid, ahead)
        budget = _FlashBudget(MAX_FLASHES_PER_RUN)
        for group, is_retry in ((retry_keys, True), (selected, False)):
            for i in range(max((len(v) for v in group.values()), default=0)):
                for sid in order:
                    if sid in group and i < len(group[sid]):
                        key = group[sid][i]
                        tasks.append((sid, key, is_retry, pool.submit(_fetch_within, budget, sats[sid]["bucket"],
                                                                      key, cfg, deadline_at)))
        done, _ = _wait([t[3] for t in tasks], deadline_at)
        first_error: Dict[str, str] = {}
        capped: Dict[str, bool] = {}
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
                    sent = False                # started after the deadline: never asked for
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
                if ts is not None and ts <= past_hold:
                    # ended before the hold began: its flashes no longer count, no gap
                    entry["past_hold_failed"] = entry.get("past_hold_failed", 0) + 1
                    continue
                entry["failed"] += 1
                first_error.setdefault(sid, err)
                if is_retry and sent:
                    # its second failure: not tried again, and not "seen" either, so that a
                    # run whose window still holds it and fails it again is incomplete too
                    entry["dropped"] = entry.get("dropped", 0) + 1
                elif ts is not None:
                    res["retry"].setdefault(sid, []).append(ts)
                continue
            entry["fetched"] += 1
            if ts is not None:
                res["seen"].setdefault(sid, set()).add(ts)
            fallback = started or g.get("start") or res["window_end"]
            if g.get("sat") and not sid.startswith(g["sat"]):
                log.debug("GLM %s: granule %s says platform %s", sid, key, g["sat"])
            n = moved = 0
            if g.get("capped"):
                capped[sid] = True
            for lat, lon, t in zip(g["lats"], g["lons"], g["times"]):
                if len(res["flashes"]) >= MAX_FLASHES_PER_RUN:
                    capped[sid] = True
                    break
                t = t if isinstance(t, datetime) else fallback
                if started is not None and not (started - timedelta(seconds=FLASH_EARLY_S) <= t
                                                <= started + timedelta(seconds=GRANULE_S + 1)):
                    # the file's own epoch disagrees with its key: the flash happened within
                    # the granule all the same; its end is the latest it can have been, so
                    # the hold that runs from it never ends early
                    t = started + timedelta(seconds=GRANULE_S)
                    moved += 1
                res["flashes"].append({"lat": lat, "lon": lon, "sat": sid, "t": t})
                n += 1
            if moved:
                log.warning("GLM %s: %d flash time(s) of %s outside the granule's own 20 s; dated by its key",
                            sid, moved, key.rsplit("/", 1)[-1])
            entry["flashes"] += n
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    res["seen"] = {sid: sorted(v) for sid, v in res["seen"].items()}
    # the marker moves to the newest granule of the window reached without a hole in what
    # the bucket publishes: every one up to it was examined, is to be fetched again
    # (``retry``) or was given up (the run is partial then). Behind a hole, or the minutes
    # not published yet, the next run starts there and fetches what was published late.
    for sid in order:
        newest = marker.get(sid)
        if newest is not None and newest > (res["newest"].get(sid) or 0.0):
            res["newest"][sid] = newest

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
        extra = list(gaps.get(sid) or [])
        if capped.get(sid):
            extra.append("more than %d flashes this run: the rest not read" % MAX_FLASHES_PER_RUN)
        if extra:
            entry["error"] = "; ".join(([entry["error"]] if entry["error"] else []) + extra)
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
    granule when it is older (a late or missed run), by at most ``max_catchup_s(cfg)``. A
    granule that fails (or is not reached before the deadline) is fetched again by the next
    run, before anything else (``retry`` in the result, handed back as ``retried``): once;
    failing again, or older than ``max_catchup_s(cfg)`` by then, it is given up and that run
    is partial. A granule that ended before ``cfg.glm_hold_s`` began is never a gap (its
    flashes no longer count): it is not fetched again, and failing it marks nothing. A
    granule the last run examined (``seen``, as the result gives it), fetched again in the
    overlap, that fails is no gap; one published too late for the last run is. Returns

        {"ok": bool, "stale": False, "error": str|None, "age_s": 0.0|None,
         "window_start", "window_end": aware UTC datetimes (window_end = now),
         "flashes": [{"lat", "lon", "sat": "G19", "t": aware datetime}, ...],
         "sats": {"G19": {"bucket", "sub_lon", "ok", "error", "listed", "selected",
                          "fetched", "failed", "flashes", ["dropped"], ["refetch_failed"],
                          ["past_hold_failed"], ["span_start", "span_end": the span the
                          satellite's published granules cover in one piece, epoch s; the
                          end None when nothing does]},
                  ...},
         "deps": bool, "elapsed_s": float, "skipped": str|None, "partial": bool,
         "newest": {satellite id: start of the newest granule of the window reached without
                   a hole, epoch s} (carried over from ``since`` for a satellite not listed or
                   with a hole at the start of its window),
         "retry": {satellite id: [starts of the granules to fetch again, epoch s]},
         "seen": {satellite id: [starts of the granules examined, epoch s]}}

    ``ok`` when at least one satellite was listed and gave at least one parsed granule;
    ``partial`` when any satellite failed (listing, no granules, a granule that did not
    arrive or parse) or its window is incomplete (the newest granule ends more than
    ``cfg.glm_max_lag_s`` before the run, more than ``MAX_GAP_S`` in a row not published,
    a listing still truncated after ``MAX_LIST_PAGES`` pages, granules listed after the
    run's time, more than ``MAX_FLASHES_PER_RUN`` flashes), with ``error`` naming what
    failed even when ``ok``; such a satellite is not ``ok``. Without a ``since`` for a
    satellite (a first run, a lost state file) its window reaches back ``max_catchup_s``.
    A flash dated outside its granule's own 20 s (by the file's epoch) takes the
    granule's end. Nothing is
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
    proximity). A detection without a time is always kept. The result is in distance order.

    The kept detections are filed per satellite in one-second buckets of time, and at most
    ``SAME_FLASH_SCAN`` of the other satellites' candidates are looked at per detection: the
    cost is linear in the rows, however many detections share a second (a match missed that
    way counts a flash twice, never zero times). Of the candidates that match, the earliest
    absorbs the detection."""
    kept: List[tuple] = []
    times: List[float] = []                     # per kept detection: its time (or nan)
    absorbed: List[set] = []                    # per kept detection: satellites it absorbed
    filed: Dict[Tuple[Any, int], List[int]] = {}    # (satellite, second) -> places in ``kept``
    sats: List[Any] = []
    for r in sorted(rows, key=lambda row: row[0]):
        t = r[3].timestamp() if isinstance(r[3], datetime) else None
        if t is not None and not math.isfinite(t):
            t = None
        if r[4] not in sats:
            sats.append(r[4])
        if t is not None:
            sec = int(math.floor(t / SAME_FLASH_S))
            match = None
            scanned = 0
            for other in sats:
                if other == r[4]:
                    continue
                for b in (sec - 1, sec, sec + 1):
                    for k in filed.get((other, b), ()):
                        if scanned >= SAME_FLASH_SCAN:
                            break
                        scanned += 1
                        if r[4] not in absorbed[k] and abs(times[k] - t) <= SAME_FLASH_S \
                                and (match is None or times[k] < times[match]) \
                                and geo.haversine_km(r[1], r[2], kept[k][1], kept[k][2]) <= SAME_FLASH_KM:
                            match = k
            if match is not None:
                absorbed[match].add(r[4])
                continue
        kept.append(r)
        times.append(t if t is not None else math.nan)
        absorbed.append(set())
        if t is not None:
            filed.setdefault((r[4], int(math.floor(t / SAME_FLASH_S))), []).append(len(kept) - 1)
    return kept


def _empty_view(cfg) -> dict:
    try:
        window_min = int(cfg.glm_window_min)
    except (TypeError, ValueError, AttributeError):
        window_min = None
    return {"covered": False, "sats": [], "available": False, "partial": False, "error": None,
            "count_window": 0, "count_by_sat": {}, "last_in_ring_t": None, "nearest_km": None,
            "nearest_bearing": None, "nearest_t": None, "nearest_sat": None, "flashes_on_map": [],
            "window_min": window_min, "span_start": None, "span_end": None, "window_end": None}


def _span(sats: dict, sids: Sequence[str]) -> Optional[Tuple[datetime, datetime]]:
    """(start, end) of the span all of ``sids`` examined: the latest of their ``span_start``
    and the earliest of their ``span_end`` (epoch s, as ``fetch`` gives them), or None when
    one of them does not say (a result of an older version) or none is given."""
    starts, ends = [], []
    for sid in sids:
        a, b = _finite(sats[sid].get("span_start")), _finite(sats[sid].get("span_end"))
        if a is None or b is None:
            return None
        starts.append(a)
        ends.append(b)
    if not starts or max(starts) >= min(ends):
        return None
    try:
        return (datetime.fromtimestamp(max(starts), tz=timezone.utc),
                datetime.fromtimestamp(min(ends), tz=timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None


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
    # complete answer; the others' problems only make the view partial. No margin inside
    # cfg.glm_max_arc_deg for this: the closest case, Brewster from GOES-East, lies 61.56 deg
    # out and its 50-km ring reaches 62.01, while GLM records flashes at 61 deg (GOES-18,
    # 2026-11-12) and nothing only at 64.9 (the GBT): the edge is not known to 0.01 deg, and
    # the limit itself was set from that measurement (audit #100, left as it is).
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
    # the minutes the view speaks for: the span every covering satellite with a complete
    # window examined in one piece (the latest start, the earliest end), back to the oldest
    # flash it counts (one from a granule fetched again may be older than that span)
    w0, w1 = res.get("window_start"), res.get("window_end")
    if isinstance(w1, datetime):
        out["window_end"] = w1
    span = _span(sats, [sid for sid in covering if isinstance(sats.get(sid), dict) and sats[sid].get("ok")])
    if span is not None and isinstance(w1, datetime):
        w0 = span[0]
        out["span_start"], out["span_end"] = span[0], min(span[1], w1)
    if isinstance(w0, datetime) and isinstance(w1, datetime) and w1 > w0:
        w0 = min([w0] + [t for t in in_ring_t if t < w0])
        out["window_min"] = int(round((w1 - w0).total_seconds() / 60.0))
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
         ``map_radius_km`` (the 2000 nearest at most), "window_min": minutes from the start
         of the span examined to the run (``cfg.glm_window_min`` plus the overlap, rounded,
         as a rule), "span_start", "span_end": aware datetimes of the span every covering
         satellite with a complete window examined in one piece | None, "window_end": the
         fetch's time (aware datetime) | None}

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
                   nearest_km=None, span_start=None, span_end=None, window_end=None,
                   nearest_bearing=None, nearest_t=None, nearest_sat=None, flashes_on_map=[],
                   error="bad GLM result: %s: %s" % (type(e).__name__, _errtext(e)))
        return out
