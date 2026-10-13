"""HTTP fetching and the on-disk cache for local-weather-awareness.

* ``get_bytes`` / ``get_json``: urllib with a User-Agent, timeout and small retry.
* ``begin_run``: per-run time budget + circuit breaker. Once a run's budget
  (``cfg.run_budget_s``) is spent, or a host has failed at the transport level (timeout,
  refused/reset connection, truncated or garbled response) after its retries, further
  requests to it are refused at once with an ``HttpError`` instead of waiting on the
  network again, so a hanging upstream cannot stretch one run past the timer interval;
  callers then fall back to their last good copies. Before ``begin_run`` is called (tests,
  library use) there is no budget and no host is ever marked down.
* ``head_ok``: tri-state probe (True 2xx / False definite non-2xx / None no answer).
* ``Cache``: JSON documents + binary files under one directory, each stamped with the
  fetch time, written atomically (fsync'ed temp file + ``os.replace``).
* ``cached_json``: the one-stop "fresh if young, refetch when due, fall back to the last
  good copy for a bounded time, otherwise fail loudly" helper that every NWS client uses.
* ``fetch_conditional`` / ``cached_bytes``: the same for a raw body, revalidated with
  If-None-Match / If-Modified-Since (a 304 costs no download), used for the NMRoads feeds.
* ``run_lock``: single-instance guard for the generator.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import http.client as _httpclient
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Set, Tuple

log = logging.getLogger("weather.http")

GEOJSON = "application/geo+json"
MIN_ATTEMPT_TIMEOUT = 3.0           # s: floor for a budget-shortened per-attempt timeout

# Indirections so tests can drive the clock and skip the backoff without real waiting.
_clock = time.monotonic
_sleep = time.sleep


class HttpError(Exception):
    """Any failure to obtain a usable response (network, HTTP status, bad JSON)."""


class HttpSkipped(HttpError):
    """The request was not sent: the run budget is spent or the host failed earlier in this
    run. Still an ``HttpError``, so every caller's fallback path handles it."""


# ---- per-run budget and circuit breaker ------------------------------------------------
class _RunState:
    """Module-level state of the current generator run (single-threaded by design)."""

    def __init__(self) -> None:
        self.active = False                 # False until begin_run(): no budget, no breaker
        self.budget_s: Optional[float] = None
        self.deadline: Optional[float] = None   # _clock() value, None = unlimited
        self.down: Set[str] = set()         # hosts that failed at the transport level
        self.exhausted_logged = False


_RUN = _RunState()


def begin_run(cfg) -> None:
    """Start a new run: deadline = now + ``cfg.run_budget_s`` (a missing or non-positive
    value means no deadline) and forget the hosts marked down by the previous run."""
    global _RUN
    st = _RunState()
    st.active = True
    budget = getattr(cfg, "run_budget_s", None)
    try:
        budget = float(budget) if budget is not None else None
    except (TypeError, ValueError):
        budget = None
    if budget is not None and budget > 0:
        st.budget_s = budget
        st.deadline = _clock() + budget
    _RUN = st
    log.debug("run started, budget %s s", "unlimited" if st.deadline is None else "%.0f" % budget)


def budget_left() -> Optional[float]:
    """Seconds left in this run's budget (may be <= 0), or None when unlimited."""
    if _RUN.deadline is None:
        return None
    return _RUN.deadline - _clock()


def run_status() -> Dict[str, Any]:
    """Summary of the current run's network health, for logs / status.json:
    ``{"budget_s", "left_s", "exhausted": bool, "down_hosts": [sorted]}``."""
    left = budget_left()
    return {"budget_s": _RUN.budget_s, "left_s": None if left is None else round(left, 1),
            "exhausted": left is not None and left <= 0, "down_hosts": sorted(_RUN.down)}


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _skip_reason(url: str) -> Optional[str]:
    """Why ``url`` must not be requested now (None = go ahead)."""
    left = budget_left()
    if left is not None and left <= 0:
        if not _RUN.exhausted_logged:
            _RUN.exhausted_logged = True
            log.warning("run budget of %.0f s exhausted: skipping all further network requests",
                        _RUN.budget_s or 0)
        return "skipped: run budget exhausted"
    host = _host(url)
    if host and host in _RUN.down:
        return "skipped: %s unreachable earlier in this run" % host
    return None


def _attempt_timeout(timeout: float) -> float:
    """``timeout`` shortened to what is left of the budget, but never below
    ``MIN_ATTEMPT_TIMEOUT`` because of the budget (a smaller explicit timeout is kept)."""
    left = budget_left()
    if left is None:
        return timeout
    return min(timeout, max(MIN_ATTEMPT_TIMEOUT, left))


def _is_transport(e: Optional[BaseException]) -> bool:
    """No usable HTTP answer: timeout, DNS/refused/reset (URLError without a status,
    ConnectionError, other socket OSErrors) or a truncated/garbled response
    (``http.client.HTTPException``). An ``HTTPError`` is a real answer, not transport."""
    if e is None or isinstance(e, urllib.error.HTTPError):
        return False
    return isinstance(e, (OSError, _httpclient.HTTPException))


def _errtext(e: Optional[BaseException]) -> str:
    """str(e), or the class name when that is empty (e.g. a bare socket.timeout())."""
    if e is None:
        return "unknown error"
    return str(e) or e.__class__.__name__


def _mark_down(url: str, err: BaseException) -> None:
    """Circuit breaker: remember a host that did not answer (only inside a run)."""
    host = _host(url)
    if not _RUN.active or not host or host in _RUN.down:
        return
    _RUN.down.add(host)
    log.warning("%s unreachable (%s): skipping it for the rest of this run", host, _errtext(err))


# ---- requests --------------------------------------------------------------------------
def _headers(cfg, accept: Optional[str]) -> Dict[str, str]:
    h = {"User-Agent": cfg.user_agent}
    if accept:
        h["Accept"] = accept
    return h


def _request(url: str, cfg, headers: Dict[str, str], method: str, timeout: Optional[float],
             retries: Optional[int], pass_codes: Tuple[int, ...] = ()
             ) -> Tuple[int, Optional[bytes], Any]:
    """The one retry loop behind ``get_bytes`` and ``fetch_conditional``: returns
    ``(status, body, response headers)``. An HTTP answer whose code is in ``pass_codes``
    (e.g. 304) is returned as ``(code, None, headers)`` instead of raised. Retries transport
    errors and 5xx / 429 with a short backoff; other 4xx fail at once. Inside a run each
    attempt first checks the budget and the circuit breaker (``HttpSkipped`` without touching
    the network), its timeout is capped by the budget left, and a transport failure on the
    last attempt marks the host down. Every failure is raised as ``HttpError``."""
    timeout = cfg.http_timeout if timeout is None else timeout
    retries = cfg.http_retries if retries is None else retries
    last: Optional[BaseException] = None
    for attempt in range(retries + 1):
        skip = _skip_reason(url)
        if skip:
            log.debug("%s %s: %s", method, url, skip)
            raise HttpSkipped(skip) from last
        try:
            log.debug("%s %s", method, url)
            req = urllib.request.Request(url, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=_attempt_timeout(timeout)) as r:
                body = r.read()
                return (getattr(r, "status", None) or 200), body, getattr(r, "headers", None)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in pass_codes:
                return e.code, None, e.headers
            if e.code == 429 or e.code >= 500:
                log.warning("HTTP %s from %s (attempt %d/%d)", e.code, url, attempt + 1, retries + 1)
            else:
                raise HttpError("HTTP %s for %s" % (e.code, url)) from e
        except (urllib.error.URLError, OSError, ValueError, _httpclient.HTTPException) as e:
            last = e
            log.warning("fetch failed for %s (attempt %d/%d): %s", url, attempt + 1, retries + 1,
                        _errtext(e))
        if attempt < retries:
            pause = 1.5 * (attempt + 1)
            left = budget_left()
            if left is not None:
                pause = min(pause, max(0.0, left))
            _sleep(pause)
    if _is_transport(last):
        _mark_down(url, last)
    raise HttpError("%s: %s" % (url, _errtext(last))) from last


def get_bytes(url: str, cfg, accept: Optional[str] = None, timeout: Optional[float] = None,
              retries: Optional[int] = None, method: str = "GET") -> bytes:
    """GET ``url`` and return the body. Retries transient failures (transport errors incl.
    ``http.client.HTTPException``, and 5xx / 429) with a short backoff; 4xx other than 429
    fail immediately. Every failure is raised as ``HttpError``.

    Inside a run (see ``begin_run``) each attempt first checks the budget and the circuit
    breaker (``HttpSkipped`` without touching the network), its timeout is capped by the
    budget left, and a transport failure on the last attempt marks the host down."""
    _, body, _ = _request(url, cfg, _headers(cfg, accept), method, timeout, retries)
    return body if body is not None else b""


def _header(headers: Any, name: str) -> Optional[str]:
    """One response header (None when absent or when there are no headers at all)."""
    if headers is None:
        return None
    try:
        v = headers.get(name)
    except Exception:  # noqa: BLE001 -- an odd headers object must not fail the fetch
        return None
    v = v.strip() if isinstance(v, str) else None
    return v or None


def fetch_conditional(url: str, cfg, etag: Optional[str] = None,
                      last_modified: Optional[str] = None, accept: Optional[str] = None,
                      timeout: Optional[float] = None
                      ) -> Tuple[int, Optional[bytes], Optional[str], Optional[str]]:
    """Conditional GET: sends ``If-None-Match: <etag>`` / ``If-Modified-Since:
    <last_modified>`` when given. Returns ``(status, body, etag, last_modified)``: ``(200,
    body, ...)`` with the response's validators, or ``(304, None, ...)`` when the copy the
    caller holds is still current (the validators are the 304's own when it sends them, else
    the ones passed in). Retry policy, run budget and circuit breaker exactly as ``get_bytes``
    (the same loop); every failure is raised as ``HttpError`` (``HttpSkipped`` when refused
    without touching the network)."""
    headers = _headers(cfg, accept)
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    status, body, rh = _request(url, cfg, headers, "GET", timeout, None, pass_codes=(304,))
    new_etag = _header(rh, "ETag")
    new_lm = _header(rh, "Last-Modified")
    if status == 304:
        return 304, None, new_etag or etag, new_lm or last_modified
    return status, body if body is not None else b"", new_etag, new_lm


def cache_policy(headers: Any) -> Tuple[bool, Optional[int]]:
    """``(no_cache, max_age)`` from a response's ``Cache-Control`` (else ``Expires``).

    ``no_cache`` is True for ``no-cache``, ``no-store``, ``private`` or ``max-age=0``: such
    a reply must not be kept. ``max_age`` is the freshness lifetime in seconds, None when
    the server gives none (callers then apply their own minimum)."""
    cc = (_header(headers, "Cache-Control") or "").lower()
    if any(t in cc for t in ("no-cache", "no-store", "private")):
        return True, None
    m = re.search(r"(?:^|[,\s])max-age\s*=\s*(\d+)", cc)
    if m:
        age = int(m.group(1))
        return (age <= 0), (age if age > 0 else None)
    exp = _header(headers, "Expires")
    if exp:
        try:
            from email.utils import parsedate_to_datetime
            left = int(parsedate_to_datetime(exp).timestamp() - time.time())
            return (left <= 0), (left if left > 0 else None)
        except (TypeError, ValueError, OverflowError):
            pass
    return False, None


def fetch_cacheable(url: str, cfg, etag: Optional[str] = None,
                    last_modified: Optional[str] = None, accept: Optional[str] = None,
                    timeout: Optional[float] = None) -> Dict[str, Any]:
    """Conditional GET that also reports the server's caching instructions, for resources
    whose provider requires clients to honour them (OpenStreetMap tiles). Returns
    ``{"status": 200|304, "body": bytes|None, "etag", "last_modified", "no_cache": bool,
    "max_age": int|None}``. Same retry policy, run budget and circuit breaker as
    ``get_bytes``; every failure is raised as ``HttpError``."""
    headers = _headers(cfg, accept)
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    status, body, rh = _request(url, cfg, headers, "GET", timeout, None, pass_codes=(304,))
    no_cache, max_age = cache_policy(rh)
    return {"status": status, "body": None if status == 304 else (body or b""),
            "etag": _header(rh, "ETag") or (etag if status == 304 else None),
            "last_modified": _header(rh, "Last-Modified") or (last_modified if status == 304 else None),
            "no_cache": no_cache, "max_age": max_age}


def head_ok(url: str, cfg, timeout: Optional[float] = None) -> Optional[bool]:
    """Probe ``url`` with one HEAD request (used to discover the latest radar frame).

    True: a 2xx answer. False: a definite non-2xx HTTP answer (e.g. 404, the file does not
    exist). None: no answer at all -- transport failure, timeout, or skipped by the run
    budget / circuit breaker; callers must treat None as "server unreachable", not as
    "absent". ``timeout`` defaults to ``cfg.http_timeout``; a transport failure inside a
    run marks the host down like ``get_bytes`` does."""
    skip = _skip_reason(url)
    if skip:
        log.debug("HEAD %s: %s", url, skip)
        return None
    t = _attempt_timeout(cfg.http_timeout if timeout is None else timeout)
    try:
        log.debug("HEAD %s", url)
        req = urllib.request.Request(url, headers=_headers(cfg, None), method="HEAD")
        with urllib.request.urlopen(req, timeout=t) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        log.debug("HEAD %s: HTTP %s", url, e.code)
        return False
    except Exception as e:  # noqa: BLE001 -- a probe never raises
        if _is_transport(e):
            _mark_down(url, e)
        log.warning("HEAD %s failed: %s", url, _errtext(e))
        return None


def get_json(url: str, cfg, accept: str = GEOJSON, timeout: Optional[float] = None) -> Any:
    body = get_bytes(url, cfg, accept=accept, timeout=timeout)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise HttpError("bad JSON from %s: %s" % (url, e)) from e


# ---- disk cache ---------------------------------------------------------------
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _atomic_write(path: str, data: bytes) -> None:
    """Write via a temp file that is fsync'ed before ``os.replace``: without the fsync the
    rename can reach the disk before the data (XFS has no rename-flush heuristic) and a
    crash would leave an empty file. The temp file is removed if anything fails."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class Cache:
    """Keyed JSON/binary cache under ``cache_dir``. Keys are free text; they are sanitised
    (and hashed when long) into file names."""

    def __init__(self, cache_dir: str):
        self.dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def path(self, key: str, ext: str = ".json") -> str:
        safe = _SAFE.sub("_", key).strip("_")
        if len(safe) > 80 or safe != key:
            safe = safe[:60] + "_" + hashlib.sha1(key.encode()).hexdigest()[:12]
        return os.path.join(self.dir, safe + ext)

    # JSON documents (stamped)
    def put(self, key: str, data: Any, ts: Optional[float] = None) -> None:
        doc = {"ts": time.time() if ts is None else ts, "key": key, "data": data}
        _atomic_write(self.path(key), json.dumps(doc).encode("utf-8"))

    def get_any(self, key: str) -> Tuple[Any, Optional[float]]:
        """(data, age_seconds) or (None, None). A corrupt file counts as absent."""
        try:
            with open(self.path(key), "rb") as f:
                doc = json.loads(f.read().decode("utf-8"))
            ts = float(doc["ts"])
            age = max(0.0, time.time() - ts)     # future stamps (clock step) read as fresh
            return doc["data"], age
        except (OSError, ValueError, KeyError, TypeError):
            return None, None

    def get(self, key: str, max_age: float) -> Any:
        data, age = self.get_any(key)
        if data is None or age is None or age > max_age:
            return None
        return data

    # binary files (e.g. basemaps, the last radar frame)
    def put_bytes(self, key: str, ext: str, data: bytes) -> str:
        p = self.path(key, ext)
        _atomic_write(p, data)
        return p

    def get_bytes(self, key: str, ext: str, max_age: Optional[float] = None) -> Optional[bytes]:
        p = self.path(key, ext)
        try:
            if max_age is not None and time.time() - os.path.getmtime(p) > max_age:
                return None
            with open(p, "rb") as f:
                return f.read()
        except OSError:
            return None


def cached_json(cfg, cache: Cache, key: str, url: str, ttl: float, max_stale: float,
                accept: str = GEOJSON, validate=None) -> Dict[str, Any]:
    """Fetch-with-cache. Returns a result dict:

        {"data": <json or None>, "ok": bool, "stale": bool, "age_s": float|None,
         "error": str|None, "from_cache": bool}

    * cached copy younger than ``ttl``  -> returned as-is (ok, not stale)
    * otherwise fetch; success          -> stored and returned (ok, age 0); if storing it
                                           fails (disk full, unwritable cache) the fresh
                                           data is still returned as ok (warning logged)
    * fetch fails, copy younger than ``max_stale`` -> returned with stale=True + error
    * fetch fails, nothing usable       -> data None, ok False, error set

    A request refused by the run budget / circuit breaker (``HttpSkipped``) counts as a
    failed fetch, so the last good copy is served without touching the network.

    ``validate(data) -> bool`` can reject a syntactically valid but wrong-shaped payload
    (it is then treated like a fetch failure and never cached).
    """
    data = cache.get(key, ttl)
    if data is not None:
        _, age = cache.get_any(key)
        return {"data": data, "ok": True, "stale": False, "age_s": age, "error": None,
                "from_cache": True}
    err = None
    skipped = False
    try:
        fresh = get_json(url, cfg, accept=accept)
        if validate is not None and not validate(fresh):
            raise HttpError("unexpected payload shape from %s" % url)
    except HttpError as e:
        err = str(e)
        skipped = isinstance(e, HttpSkipped)
    except Exception as e:  # noqa: BLE001 — any surprise = unavailable, never a crash
        err = "%s: %s" % (type(e).__name__, e)
    else:
        try:
            cache.put(key, fresh)
        except OSError as e:
            log.warning("%s: fetched but could not be cached (%s); using it anyway", key, e)
        return {"data": fresh, "ok": True, "stale": False, "age_s": 0.0, "error": None,
                "from_cache": False}
    if skipped:
        log.info("%s unavailable: %s", key, err)       # the cause was logged once already
    else:
        log.warning("%s unavailable: %s", key, err)
    old, age = cache.get_any(key)
    if old is not None and age is not None and age <= max_stale:
        return {"data": old, "ok": True, "stale": True, "age_s": age, "error": err,
                "from_cache": True}
    return {"data": None, "ok": False, "stale": True, "age_s": age, "error": err,
            "from_cache": False}


BYTES_EXT = ".bin"                   # cached_bytes bodies: never clash with a JSON document


def _bytes_meta(cache: Cache, key: str) -> Tuple[Optional[bytes], Dict[str, Any], Optional[float]]:
    """The cached body of ``key``, its validators and its age in seconds (time since it was
    last fetched or confirmed by a 304). A body without a readable meta record is dated by
    its file time and carries no validators."""
    body = cache.get_bytes(key, BYTES_EXT)
    if body is None:
        return None, {}, None
    meta, _ = cache.get_any(key + "__meta")
    meta = meta if isinstance(meta, dict) else {}
    ts = meta.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        meta = {}
        try:
            ts = os.path.getmtime(cache.path(key, BYTES_EXT))
        except OSError:
            return body, {}, None
    return body, meta, max(0.0, time.time() - float(ts))


def cached_bytes(cfg, cache: Cache, key: str, url: str, ttl: float, max_stale: float,
                 accept: Optional[str] = None, validate=None) -> Dict[str, Any]:
    """``cached_json`` for a raw body, revalidated with a conditional GET. Returns

        {"ok", "stale", "error", "age_s", "from_cache", "not_modified": bool,
         "data": bytes|None, "last_modified": str|None}

    The body is kept with ``cache.put_bytes(key, ".bin")`` and its validators as
    ``cache.put(key + "__meta", {"etag", "last_modified", "ts"})`` (body first, so a failed
    write never pairs new validators with an old body).

    * copy younger than ``ttl``          -> returned as-is, nothing sent
    * otherwise ``fetch_conditional`` with the copy's ETag / Last-Modified: a 304 refreshes
      the stamp and returns the cached body (ok, age 0, ``not_modified``); a 200 is checked
      with ``validate(bytes) -> bool`` (a rejected payload counts as a failed fetch and is
      never cached), stored and returned (ok, age 0); a body that cannot be stored is still
      returned as ok (warning logged)
    * fetch fails, copy younger than ``max_stale`` -> returned with stale=True + error
    * fetch fails, nothing usable        -> data None, ok False, error set

    Validators are sent only while a cached body exists, so a 304 always has a body to
    return. A request refused by the run budget / circuit breaker counts as a failed fetch."""
    body, meta, age = _bytes_meta(cache, key)
    last_mod = meta.get("last_modified") if isinstance(meta.get("last_modified"), str) else None
    if body is not None and age is not None and age <= ttl:
        return {"data": body, "ok": True, "stale": False, "age_s": age, "error": None,
                "from_cache": True, "not_modified": False, "last_modified": last_mod}
    etag = meta.get("etag") if isinstance(meta.get("etag"), str) else None
    err = None
    skipped = False
    try:
        if body is None:
            status, fresh, new_etag, new_lm = fetch_conditional(url, cfg, accept=accept)
        else:
            status, fresh, new_etag, new_lm = fetch_conditional(
                url, cfg, etag=etag, last_modified=last_mod, accept=accept)
        if status == 304:
            if body is None:
                raise HttpError("HTTP 304 for %s without a cached copy" % url)
            fresh = body
        elif not isinstance(fresh, (bytes, bytearray)):
            raise HttpError("no body from %s (HTTP %s)" % (url, status))
        elif validate is not None and not validate(fresh):
            raise HttpError("unexpected payload shape from %s" % url)
    except HttpError as e:
        err = str(e)
        skipped = isinstance(e, HttpSkipped)
    except Exception as e:  # noqa: BLE001 -- any surprise = unavailable, never a crash
        err = "%s: %s" % (type(e).__name__, e)
    else:
        not_modified = status == 304
        if not_modified:
            log.debug("%s: HTTP 304 not modified, cached copy reused (%d bytes)", key, len(fresh))
        else:
            log.debug("%s: HTTP %s, %d bytes (%s)", key, status, len(fresh),
                      "conditional GET, changed" if body is not None else "no cached copy")
        new_meta = {"etag": new_etag, "last_modified": new_lm, "ts": time.time()}
        try:
            if not not_modified:
                cache.put_bytes(key, BYTES_EXT, bytes(fresh))
            cache.put(key + "__meta", new_meta)
        except OSError as e:
            log.warning("%s: fetched but could not be cached (%s); using it anyway", key, e)
        return {"data": bytes(fresh), "ok": True, "stale": False, "age_s": 0.0, "error": None,
                "from_cache": not_modified, "not_modified": not_modified,
                "last_modified": new_lm}
    if skipped:
        log.info("%s unavailable: %s", key, err)       # the cause was logged once already
    else:
        log.warning("%s unavailable: %s", key, err)
    if body is not None and age is not None and age <= max_stale:
        return {"data": body, "ok": True, "stale": True, "age_s": age, "error": err,
                "from_cache": True, "not_modified": False, "last_modified": last_mod}
    return {"data": None, "ok": False, "stale": True, "age_s": age, "error": err,
            "from_cache": False, "not_modified": False, "last_modified": None}


# ---- single-instance lock ------------------------------------------------------
class AlreadyRunning(Exception):
    pass


@contextlib.contextmanager
def run_lock(path: str):
    """Non-blocking flock; raises AlreadyRunning if another generator holds it."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AlreadyRunning("another run holds %s" % path)
        os.ftruncate(fd, 0)
        os.write(fd, ("%d\n" % os.getpid()).encode())
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
