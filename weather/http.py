"""HTTP fetching and the on-disk cache for local-weather-awareness.

* ``get_bytes`` / ``get_json``: urllib with a User-Agent, timeout and small retry.
* ``begin_run``: per-run time budget + circuit breaker. Once a run's budget
  (``cfg.run_budget_s``) is spent, or a host has failed at the transport level (timeout,
  refused/reset connection, truncated or garbled response) after its retries, further
  requests to it are refused at once with an ``HttpError`` instead of waiting on the
  network again, so a hanging upstream cannot stretch one run past the timer interval;
  callers then fall back to their last good copies. Before ``begin_run`` is called (tests,
  library use) there is no budget and no host is ever marked down.
* Every request goes through one opener that speaks http and https only (no file:, ftp:
  or data: handler), follows at most ``MAX_REDIRECTS`` redirects and only to https (or to
  http on the same host when the request was http), never to an IP literal or localhost.
  A reply must be complete within a few attempt timeouts (``BODY_DEADLINE_FACTOR``) and
  stay under ``max_bytes`` (``MAX_BYTES`` by default); a 429, or a 503 with Retry-After, is
  a refusal that is not retried and marks the host down for the run.
* ``clock_offsets``: the server time (Date + Age) of each host's latest reply against this
  host's clock, for the run's wrong-clock guard (``vlba/clock.py``).
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
import email.utils
import fcntl
import hashlib
import http.client as _httpclient
import ipaddress
import json
import logging
import os
import re
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timezone
from typing import Any, Dict, Optional, Set, Tuple

log = logging.getLogger("weather.http")

GEOJSON = "application/geo+json"
MIN_ATTEMPT_TIMEOUT = 3.0           # s: floor for a budget-shortened per-attempt timeout
# The largest body any source sends is about 1 MB (an MRMS CONUS file; an IEM frame is
# 0.7 MB, a GLM granule 0.4 MB, a gridpoint 0.3 MB): 32 MiB leaves a wide margin for growth
# and still stops an endless or bloated reply long before it costs the host its memory.
MAX_BYTES = 32 * 1024 * 1024
# A whole reply (headers and body) must arrive within this many attempt timeouts. The
# timeout given to the socket bounds each read only, so a server that sends a few bytes
# now and then would otherwise hold the run for ever; 3 x 25 s still lets 1 MB arrive at
# 14 kB/s.
BODY_DEADLINE_FACTOR = 3
ALARM_GRACE_S = 2.0                 # the signal backstop fires this long after the deadline
MAX_REDIRECTS = 5                   # object stores and CDNs need one or two
READ_CHUNK = 65536
ERROR_URL_MAX = 120                 # characters of host/path quoted in an error text

# Indirections so tests can drive the clock and skip the backoff without real waiting.
_clock = time.monotonic
_sleep = time.sleep
_wall = time.monotonic              # the reply deadline (real time, not the budget's clock)


class HttpError(Exception):
    """Any failure to obtain a usable response (network, HTTP status, bad JSON).
    ``status``: the HTTP status of the answer when there was one, else None."""

    def __init__(self, *args: Any, status: Optional[int] = None) -> None:
        super().__init__(*args)
        self.status = status


class HttpSkipped(HttpError):
    """The request was not sent: the run budget is spent or the host failed earlier in this
    run. Still an ``HttpError``, so every caller's fallback path handles it."""


class _Overrun(HttpError):
    """The reply did not complete within its deadline (raised by the read loop, or by the
    SIGALRM backstop wherever the main thread happens to be blocked)."""


class _Refused(HttpError):
    """A request or redirect this module will not make (scheme, host, invalid URL). Not a
    transport failure: no retry, no host is marked down."""


# ---- per-run budget and circuit breaker ------------------------------------------------
class _RunState:
    """Module-level state of the current generator run (single-threaded by design)."""

    def __init__(self) -> None:
        self.active = False                 # False until begin_run(): no budget, no breaker
        self.budget_s: Optional[float] = None
        self.deadline: Optional[float] = None   # _clock() value, None = unlimited
        self.down: Set[str] = set()         # hosts that failed at the transport level
        self.refused: Dict[str, int] = {}   # hosts that refused us (HTTP 429/503): status
        self.exhausted_logged = False
        self.redirects_logged: Set[Tuple[str, str]] = set()
        self.clock: Dict[str, float] = {}   # host -> server time minus local time, s


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


def clock_offsets() -> Dict[str, float]:
    """Per host, the latest reply's server time (its ``Date`` header plus ``Age``) minus
    this host's clock at receipt, in seconds (positive: this host's clock is behind), for
    the replies of this run that came from the network. Hosts without a usable ``Date``
    are absent."""
    return dict(_RUN.clock)


def _server_offset(headers: Any, now: float) -> Optional[float]:
    """Server time minus ``now`` (epoch s) from a reply's ``Date`` (+ ``Age`` when a cache
    answered), None without a usable ``Date``. As safety/servertime.py of ttustatus."""
    date = _header(headers, "Date")
    if not date:
        return None
    try:
        t = email.utils.parsedate_to_datetime(date)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    try:
        age = int(_header(headers, "Age") or "0")
    except ValueError:
        age = 0
    if not 0 <= age <= 366 * 86400:     # nonsense (no cache keeps a reply that long): ignored
        age = 0
    try:
        return t.timestamp() + age - now
    except (OverflowError, ValueError, OSError):
        return None


def _note_clock(url: Optional[str], headers: Any, now: float) -> None:
    """Remember the server time of one reply that really came from the network."""
    host = _host(url or "")
    off = _server_offset(headers, now)
    if host and off is not None:
        _RUN.clock[host] = off


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


_CTRL = re.compile("[\x00-\x1f\x7f-\x9f  ]")


def _where(url: str) -> str:
    """The host and path of ``url`` for an error text that may reach the public page: no
    scheme, user, password, port, query or fragment, no control characters, at most
    ``ERROR_URL_MAX`` characters. Not even the path of a URL that is not http(s): a file: URL
    is a local path."""
    try:
        p = urllib.parse.urlsplit(str(url))
        if p.scheme.lower() not in ("http", "https"):
            return "a URL with the scheme %r" % _CTRL.sub("?", p.scheme[:20]) if p.scheme else "a relative URL"
        text = (p.hostname or "") + p.path
    except ValueError:
        return "an invalid URL"
    text = _CTRL.sub("?", text) or "an invalid URL"
    return text if len(text) <= ERROR_URL_MAX else text[:ERROR_URL_MAX - 3] + "..."


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
    if host and host in _RUN.refused:
        return "skipped: %s refused requests earlier in this run (HTTP %d)" % (host, _RUN.refused[host])
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


def _reply_deadline(t: float) -> float:
    """Seconds one whole reply may take: ``BODY_DEADLINE_FACTOR`` attempt timeouts, but no
    more than the budget left (and, like the attempt timeout, at least
    ``MIN_ATTEMPT_TIMEOUT``, so the last requests of a run still get a chance)."""
    d = BODY_DEADLINE_FACTOR * t
    left = budget_left()
    if left is not None:
        d = min(d, max(MIN_ATTEMPT_TIMEOUT, left))
    return d


def _is_transport(e: Optional[BaseException]) -> bool:
    """No usable HTTP answer: timeout, DNS/refused/reset (URLError without a status,
    ConnectionError, other socket OSErrors) or a truncated/garbled response
    (``http.client.HTTPException``). An ``HTTPError`` is a real answer, not transport; an
    invalid URL (``InvalidURL``, ``ValueError``, ``UnicodeError``, urllib's "unknown url
    type") was never sent and is not transport either."""
    if e is None or isinstance(e, (urllib.error.HTTPError, _httpclient.InvalidURL, ValueError)):
        return False
    if isinstance(e, urllib.error.URLError) and not isinstance(e.reason, BaseException):
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


def _refusal(e: urllib.error.HTTPError) -> bool:
    """A 429, or a 503 that says when to come back: the server asks us to stop."""
    return e.code == 429 or (e.code == 503 and bool(_header(e.headers, "Retry-After")))


def _mark_refused(url: str, e: urllib.error.HTTPError) -> None:
    """A refusal is not retried: the host is skipped for the rest of the run (only inside a
    run) and the request is not repeated before the next one."""
    host = _host(url)
    if not _RUN.active or not host or host in _RUN.refused:
        return
    _RUN.refused[host] = e.code
    _RUN.down.add(host)
    log.warning("%s refused our requests (HTTP %d, Retry-After %s): skipping it for the rest of "
                "this run", host, e.code, _header(e.headers, "Retry-After") or "not given")


# ---- what may be requested -------------------------------------------------------------
def _url_problem(url: Any) -> Optional[str]:
    """Why ``url`` must not be opened at all (None: an http or https URL with a host)."""
    if not isinstance(url, str):
        return "not a URL"
    if re.search(r"[\x00-\x20\x7f]", url):
        return "invalid URL"
    try:
        p = urllib.parse.urlsplit(url)
        p.port                                  # noqa: B018 -- raises ValueError on a bad port
        host = p.hostname or ""
        if host:
            host.encode("idna")
    except (ValueError, UnicodeError):
        return "invalid URL"
    if p.scheme.lower() not in ("http", "https"):
        return "not an http or https URL"
    if not host or "@" in p.netloc:     # user:password@ never works here (urllib would look
        return "invalid URL"            # up "user@host" and mark the real host down)
    return None


def _local_host(host: str) -> bool:
    """An IP literal (in any of the forms the resolver accepts: 127.1, 0x7f.1, 2130706433),
    or localhost: never a redirect target."""
    host = host.lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    # a public name ends in a top-level domain that starts with a letter
    return ":" in host or not re.match(r"[a-z]", host.rsplit(".", 1)[-1])


def _redirect_problem(origin: str, current: str, new: str) -> Optional[str]:
    """Why a redirect from ``current`` (first requested as ``origin``) to ``new`` is not
    followed: only to https, or to http on the origin's host when the origin was http and
    the hop is http as well; never to an IP literal or localhost."""
    try:
        o, c, n = (urllib.parse.urlsplit(u) for u in (origin, current, new))
        nhost = (n.hostname or "").lower()
    except ValueError:
        return "invalid URL"
    if _url_problem(new):
        return _url_problem(new)
    if _local_host(nhost):
        return "an IP address or local host"
    scheme = n.scheme.lower()
    if scheme == "https":
        return None
    if scheme == "http" and o.scheme.lower() == "http" and c.scheme.lower() == "http" \
            and nhost == (o.hostname or "").lower():
        return None
    return "a downgrade to http"


class _Redirects(urllib.request.HTTPRedirectHandler):
    """urllib's redirect handling, restricted by ``_redirect_problem``."""
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        origin = getattr(req, "origin_url", None) or req.full_url
        why = _redirect_problem(origin, req.full_url, newurl)
        if why:
            with contextlib.suppress(Exception):
                fp.close()
            raise _Refused("%s: redirect to %s refused (%s)" % (_where(req.full_url), _where(newurl), why),
                           status=code)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.origin_url = origin
            pair = (_host(req.full_url), _host(newurl))
            if pair[0] != pair[1] and pair not in _RUN.redirects_logged:
                _RUN.redirects_logged.add(pair)
                log.info("%s redirects to %s", pair[0], pair[1])
        return new


def _build_opener() -> urllib.request.OpenerDirector:
    """http and https only: urllib's default opener also serves file:, ftp: and data:."""
    opener = urllib.request.OpenerDirector()
    for h in (urllib.request.ProxyHandler(), urllib.request.UnknownHandler(),
              urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(),
              urllib.request.HTTPDefaultErrorHandler(), _Redirects(),
              urllib.request.HTTPErrorProcessor()):
        opener.add_handler(h)
    return opener


_OPENER = _build_opener()


# ---- the reply deadline ----------------------------------------------------------------
_ALARM_ARMED = [False]


def _on_alarm(signum, frame) -> None:
    if _ALARM_ARMED[0]:
        _ALARM_ARMED[0] = False
        raise _Overrun("reply deadline passed")


@contextlib.contextmanager
def _alarm(seconds: float):
    """A real-time timer that raises ``_Overrun`` in the main thread after ``seconds``: the
    backstop for what the read loop cannot see (headers that trickle in line by line, one
    read blocked for its whole socket timeout). Only in the main thread, only where
    ``signal.setitimer`` exists and no other timer is armed; elsewhere the socket timeout
    and the read loop's deadline must do."""
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread() \
            or signal.getitimer(signal.ITIMER_REAL)[0] > 0:     # somebody else's timer: leave it alone
        yield
        return
    try:
        prev = signal.signal(signal.SIGALRM, _on_alarm)
    except (ValueError, OSError):
        yield
        return
    try:
        _ALARM_ARMED[0] = True
        signal.setitimer(signal.ITIMER_REAL, max(0.01, seconds))
        yield
    finally:
        _ALARM_ARMED[0] = False
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prev if prev is not None else signal.SIG_DFL)


def _content_length(headers: Any) -> Optional[int]:
    v = _header(headers, "Content-Length")
    try:
        n = int(v) if v is not None else None
    except ValueError:
        return None
    return n if n is not None and n >= 0 else None


def _read_body(r: Any, url: str, until: float, limit: float, max_bytes: int) -> bytes:
    """The body of ``r``, read in chunks against the wall-clock ``until`` (``_wall()``
    value; ``limit`` is its length in s, for the text) and the size cap. ``read1`` returns
    whatever has arrived, so a trickling server meets the deadline check after each
    piece (a plain ``read(n)`` would wait for all n bytes)."""
    declared = _content_length(getattr(r, "headers", None))
    if declared is not None and declared > max_bytes:
        raise HttpError("%s: reply of %d bytes is over the limit of %d" % (_where(url), declared, max_bytes))
    read = getattr(r, "read1", None) or r.read
    parts = []
    got = 0
    while True:
        if _wall() > until:
            raise _Overrun("%s: reply not complete after %.0f s" % (_where(url), limit))
        chunk = read(min(READ_CHUNK, max_bytes + 1 - got))
        if not chunk:
            break
        got += len(chunk)
        if got > max_bytes:
            raise HttpError("%s: reply over the limit of %d bytes" % (_where(url), max_bytes))
        parts.append(chunk)
    # read1 ends quietly when the connection closes before the declared length (read() would
    # raise): what http.client still expects is a truncated reply, a transport failure
    short = getattr(r, "length", None)
    if isinstance(short, int) and not isinstance(short, bool) and short > 0:
        raise _httpclient.IncompleteRead(b"".join(parts), short)
    return b"".join(parts)


def _exchange(url: str, headers: Dict[str, str], method: str, timeout: float, max_bytes: int,
              read_body: bool = True) -> Tuple[int, Optional[bytes], Any]:
    """One request through ``_OPENER``: ``(status, body or None, headers)``. The whole reply
    must arrive within ``_reply_deadline`` of the attempt timeout (``_Overrun`` otherwise);
    the server time of every reply, an HTTP error status included, is noted for the clock
    guard. Raises urllib's ``HTTPError`` for a non-2xx answer, ``_Refused`` for a URL or
    redirect that is not allowed, ``HttpError`` for a body over ``max_bytes``, and the
    transport's own exceptions."""
    why = _url_problem(url)
    if why:
        raise _Refused("%s: %s" % (_where(url), why))
    t = _attempt_timeout(timeout)
    limit = _reply_deadline(t)
    until = _wall() + limit
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with _alarm(limit + ALARM_GRACE_S):
            try:
                r = _OPENER.open(req, timeout=t)
            except urllib.error.HTTPError as e:
                _note_clock(getattr(e, "filename", None) or url, e.headers, time.time())
                raise
            with r:
                _note_clock(getattr(r, "url", None) or url, getattr(r, "headers", None), time.time())
                status = getattr(r, "status", None) or 200
                body = _read_body(r, url, until, limit, max_bytes) if read_body else None
                return status, body, getattr(r, "headers", None)
    except _Overrun as e:
        if str(e) == "reply deadline passed":
            raise _Overrun("%s: reply not complete after %.0f s" % (_where(url), limit)) from None
        raise


# ---- requests --------------------------------------------------------------------------
def _headers(cfg, accept: Optional[str]) -> Dict[str, str]:
    h = {"User-Agent": cfg.user_agent}
    if accept:
        h["Accept"] = accept
    return h


def _request(url: str, cfg, headers: Dict[str, str], method: str, timeout: Optional[float],
             retries: Optional[int], pass_codes: Tuple[int, ...] = (), max_bytes: int = MAX_BYTES
             ) -> Tuple[int, Optional[bytes], Any]:
    """The one retry loop behind ``get_bytes`` and ``fetch_conditional``: returns
    ``(status, body, response headers)``. An HTTP answer whose code is in ``pass_codes``
    (e.g. 304) is returned as ``(code, None, headers)`` instead of raised. Retries transport
    errors and 5xx with a short backoff; other 4xx fail at once, and a 429 (or a 503 with
    Retry-After) is a refusal: not retried, the host is skipped for the rest of the run. A
    URL that is not http(s), an invalid URL, a refused redirect or a body over
    ``max_bytes`` fail at once without marking the host down; a reply not complete within
    its deadline fails at once and marks the host down. Inside a run each attempt first
    checks the budget and the circuit breaker (``HttpSkipped`` without touching the
    network), its timeout is capped by the budget left, and a transport failure on the last
    attempt marks the host down. Every failure is raised as ``HttpError`` (with ``status``
    when the server answered), whose text names the URL's host and path only."""
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
            return _exchange(url, headers, method, timeout, max_bytes)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in pass_codes:
                return e.code, None, e.headers
            if _refusal(e):
                _mark_refused(url, e)
                raise HttpError("%s: refused (HTTP %d)" % (_where(url), e.code), status=e.code) from e
            if e.code >= 500:
                log.warning("HTTP %s from %s (attempt %d/%d)", e.code, url, attempt + 1, retries + 1)
            else:
                raise HttpError("HTTP %s for %s" % (e.code, _where(url)), status=e.code) from e
        except _Overrun as e:
            log.warning("%s %s: %s", method, url, e)
            _mark_down(url, e)
            raise
        except HttpError as e:                  # refused URL or redirect, body over the cap
            log.warning("%s %s: %s", method, url, e)
            raise
        except (_httpclient.InvalidURL, UnicodeError) as e:
            log.warning("%s %s: invalid URL (%s)", method, url, _errtext(e))
            raise HttpError("%s: invalid URL" % _where(url)) from e
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
    if isinstance(last, urllib.error.HTTPError):
        raise HttpError("HTTP %s for %s" % (last.code, _where(url)), status=last.code) from last
    raise HttpError("%s: %s" % (_where(url), _errtext(last))) from last


def get_bytes(url: str, cfg, accept: Optional[str] = None, timeout: Optional[float] = None,
              retries: Optional[int] = None, method: str = "GET", max_bytes: int = MAX_BYTES) -> bytes:
    """GET ``url`` and return the body. Retries transient failures (transport errors incl.
    ``http.client.HTTPException``, and 5xx) with a short backoff; 4xx fail immediately, a
    429 (or a 503 with Retry-After) also marks the host down. A body over ``max_bytes``
    fails. Every failure is raised as ``HttpError``.

    Inside a run (see ``begin_run``) each attempt first checks the budget and the circuit
    breaker (``HttpSkipped`` without touching the network), its timeout is capped by the
    budget left, and a transport failure on the last attempt marks the host down."""
    _, body, _ = _request(url, cfg, _headers(cfg, accept), method, timeout, retries, max_bytes=max_bytes)
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
                      timeout: Optional[float] = None, max_bytes: int = MAX_BYTES
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
    status, body, rh = _request(url, cfg, headers, "GET", timeout, None, pass_codes=(304,), max_bytes=max_bytes)
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
                    timeout: Optional[float] = None, max_bytes: int = MAX_BYTES) -> Dict[str, Any]:
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
    status, body, rh = _request(url, cfg, headers, "GET", timeout, None, pass_codes=(304,), max_bytes=max_bytes)
    no_cache, max_age = cache_policy(rh)
    return {"status": status, "body": None if status == 304 else (body or b""),
            "etag": _header(rh, "ETag") or (etag if status == 304 else None),
            "last_modified": _header(rh, "Last-Modified") or (last_modified if status == 304 else None),
            "no_cache": no_cache, "max_age": max_age}


def head_ok(url: str, cfg, timeout: Optional[float] = None) -> Optional[bool]:
    """Probe ``url`` with one HEAD request (used to discover the latest radar frame).

    True: a 2xx answer. False: a definite 4xx answer (e.g. 404, the file does not exist).
    None: no usable answer -- transport failure, timeout, a 429 or 5xx (the server is
    throttling or failing, which says nothing about the file), a refused redirect, a URL
    that is not http(s), or skipped by the run budget / circuit breaker; callers must treat
    None as "server unreachable", not as "absent". ``timeout`` defaults to
    ``cfg.http_timeout``; a transport failure inside a run marks the host down like
    ``get_bytes`` does, and so does a refusal (429, or 503 with Retry-After)."""
    skip = _skip_reason(url)
    if skip:
        log.debug("HEAD %s: %s", url, skip)
        return None
    try:
        log.debug("HEAD %s", url)
        status, _, _ = _exchange(url, _headers(cfg, None), "HEAD",
                                 cfg.http_timeout if timeout is None else timeout, 0, read_body=False)
        return 200 <= status < 300
    except urllib.error.HTTPError as e:
        log.debug("HEAD %s: HTTP %s", url, e.code)
        if _refusal(e):
            _mark_refused(url, e)
            return None
        if e.code >= 500 or e.code < 400:      # failing, or a redirect loop: no answer on the file
            return None
        return False
    except _Overrun as e:
        log.warning("HEAD %s failed: %s", url, e)
        _mark_down(url, e)
        return None
    except Exception as e:  # noqa: BLE001 -- a probe never raises
        if _is_transport(e):
            _mark_down(url, e)
        log.warning("HEAD %s failed: %s", url, _errtext(e))
        return None


def get_json(url: str, cfg, accept: str = GEOJSON, timeout: Optional[float] = None,
             max_bytes: int = MAX_BYTES) -> Any:
    body = get_bytes(url, cfg, accept=accept, timeout=timeout, max_bytes=max_bytes)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise HttpError("bad JSON from %s: %s" % (_where(url), e)) from e


# ---- disk cache ---------------------------------------------------------------
# (imported here, not at the top: the cache and the lock below use them alone)
import errno                         # noqa: E402
import math                          # noqa: E402
import stat                          # noqa: E402

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
# A stamp further ahead of the clock than this is not a time this host wrote in a sane
# state (a clock stepped back, a hand edit): the entry is treated as absent. Within it, a
# small step or a coarse file time, the age is clamped to 0 as before.
FUTURE_STAMP_S = 120.0


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


def _stamp_age(ts: Any) -> Optional[float]:
    """Age in seconds of a fetch stamp, clamped to 0 within ``FUTURE_STAMP_S`` ahead of the
    clock; None for a stamp that is no number, not finite, or further ahead (such a copy
    would otherwise pass every ttl, even 0, until the clock caught up with it)."""
    if isinstance(ts, bool):
        return None
    try:
        ts = float(ts)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(ts):
        return None
    age = time.time() - ts
    return max(0.0, age) if age >= -FUTURE_STAMP_S else None


class Cache:
    """Keyed JSON/binary cache under ``cache_dir``. Keys are free text; they are sanitised
    (and hashed when long) into file names."""

    def __init__(self, cache_dir: str):
        self.dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self._warned: Set[str] = set()

    def _bad_stamp(self, key: str, ts: Any, *paths: str) -> None:
        """An entry whose stamp cannot be trusted is removed (``paths``), so that it cannot
        come back as a young copy once the clock has passed the stamp; it is fetched again.
        One warning per key and run."""
        for p in paths:
            with contextlib.suppress(OSError):
                os.unlink(p)
        warned = getattr(self, "_warned", None)
        if warned is None:
            warned = self._warned = set()
        if key not in warned:
            warned.add(key)
            log.warning("cache %s: time stamp %s is not a time or lies ahead of the clock; "
                        "the copy is dropped and fetched again", key, repr(ts)[:40])

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
        """(data, age_seconds) or (None, None). A corrupt file counts as absent, and so does
        one whose stamp is not finite or lies more than ``FUTURE_STAMP_S`` ahead of the
        clock (a clock stepped back): it is fetched again, never served as fresh or as a
        last good copy. A stamp just ahead reads as age 0."""
        try:
            with open(self.path(key), "rb") as f:
                doc = json.loads(f.read().decode("utf-8"))
            data, ts = doc["data"], doc["ts"]
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            return None, None
        age = _stamp_age(ts)
        if age is None:
            self._bad_stamp(key, ts, self.path(key))
            return None, None
        return data, age

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
    # ttl <= 0 (the alert feed) asks the network every time: a copy of age 0 is no answer
    data = cache.get(key, ttl) if ttl > 0 else None
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
    its file time and carries no validators. A time that is not finite or lies more than
    ``FUTURE_STAMP_S`` ahead of the clock makes the copy absent: it is fetched again."""
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
    age = _stamp_age(ts)
    if age is None:
        cache._bad_stamp(key, ts, cache.path(key, BYTES_EXT), cache.path(key + "__meta"))
        return None, {}, None
    return body, meta, age


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
LOCK_MODE = 0o600       # flock needs only an open descriptor: nobody else may open the file


class AlreadyRunning(Exception):
    """Another process holds the run lock. ``held_s``: seconds since the holder stamped the
    lock file when it took the lock (its mtime), or None when that cannot be told."""

    def __init__(self, msg: str = "", held_s: Optional[float] = None):
        super().__init__(msg)
        self.held_s = held_s


class LockError(OSError):
    """The run lock cannot be taken for another reason than a run holding it: the lock file
    is a link, not a regular file or someone else's, or flock itself fails (NFS without a
    lock service answers ENOLCK, some FUSE mounts ENOSYS). An error, never "already running":
    otherwise every run would end quietly with nothing written."""


def _lock_held_s(fd: int) -> Optional[float]:
    """Seconds since the lock file was last stamped; None when its time cannot be read or
    lies ahead of the clock by more than ``FUTURE_STAMP_S`` (a clock step)."""
    try:
        held = time.time() - os.fstat(fd).st_mtime
    except OSError:
        return None
    return max(0.0, held) if held >= -FUTURE_STAMP_S else None


@contextlib.contextmanager
def run_lock(path: str):
    """Non-blocking flock on ``path`` (created 0600; a wider mode left by an older version
    is narrowed). Raises AlreadyRunning only when another process holds the lock, and
    LockError (an OSError) for every other failure. Once taken, the file is rewritten with
    the pid and the start time, so its mtime tells a waiting run since when it is held."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags, LOCK_MODE)
    except OSError as e:
        why = "it is a symbolic link" if e.errno == errno.ELOOP else (e.strerror or str(e))
        raise LockError(e.errno, "cannot open the lock file %s: %s" % (path, why)) from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            raise LockError(errno.EPERM, "the lock file %s is not a regular file owned by this user "
                                         "(uid %d); remove it" % (path, os.geteuid()))
        if st.st_mode & 0o077:
            try:
                os.fchmod(fd, LOCK_MODE)
                log.info("lock file %s: mode %03o narrowed to %03o", path, st.st_mode & 0o777, LOCK_MODE)
            except OSError as e:        # a mount without modes: the run still goes ahead
                log.warning("lock file %s: mode %03o could not be narrowed (%s)", path,
                            st.st_mode & 0o777, e)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise AlreadyRunning("another run holds %s" % path, _lock_held_s(fd))
            raise LockError(e.errno, "cannot lock %s: %s" % (path, e.strerror or e)) from e
        os.ftruncate(fd, 0)
        os.write(fd, ("%d %s\n" % (os.getpid(), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))).encode())
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
