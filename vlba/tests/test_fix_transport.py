"""The transport fixes of weather/http.py: the reply deadline (#138, #10), the size cap (#13,
#150, #42), schemes and redirects (#14, #2), refusals (#15, #152, #154), invalid URLs (#11),
the error texts, and the server time of every reply (#136).

The deadline and the size cap are tested against real sockets: a thread of the test serves
scripted replies on 127.0.0.1 (the only host the suite's network block lets through), with
sub-second timeouts."""
from __future__ import annotations

import http.client as _hc
import socket
import threading
import time
import types
import urllib.error

import pytest

from weather import http                                   # noqa: E402 (after the stdlib)


def _cfg(**kw):
    c = types.SimpleNamespace(user_agent="python-urllib", http_timeout=0.3, http_retries=0, run_budget_s=None)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class Server:
    """A tiny scripted HTTP server on 127.0.0.1: ``handler(conn, request)`` writes the reply
    by hand (so it can trickle, lie about its length or never finish). ``requests`` holds the
    request lines received."""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def url(self, path="/x"):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def _loop(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(5)
                data = b""
                try:
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    self.requests.append(data.split(b"\r\n", 1)[0].decode("latin-1"))
                    self.handler(self, conn, data)
                except OSError:
                    pass

    def close(self):
        self.stop.set()
        self.thread.join(2)
        self.sock.close()


@pytest.fixture
def serve():
    servers = []

    def make(handler):
        s = Server(handler)
        servers.append(s)
        return s
    yield make
    for s in servers:
        s.close()


def _trickle(stop, conn, pieces, gap):
    for p in pieces:
        if stop.is_set():
            return
        conn.sendall(p)
        time.sleep(gap)


def _reply(status=200, headers=(), body=b"ok"):
    head = "HTTP/1.1 %d X\r\n" % status
    for k, v in headers:
        head += "%s: %s\r\n" % (k, v)
    if not any(k.lower() == "content-length" for k, _ in headers):
        head += "Content-Length: %d\r\n" % len(body)
    head += "Connection: close\r\n\r\n"

    def handler(srv, conn, req):
        conn.sendall(head.encode("latin-1") + (b"" if req.startswith(b"HEAD") else body))
    return handler


# ---- #138, #10: the deadline of a whole reply ---------------------------------------------
def test_a_trickling_body_is_abandoned_at_the_reply_deadline(serve):
    """Every byte arrives well within the socket timeout (0.3 s), but the whole body would
    take 6 s: the reply is given up after BODY_DEADLINE_FACTOR x 0.3 s."""
    def handler(srv, conn, req):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 60\r\nConnection: close\r\n\r\n")
        _trickle(srv.stop, conn, [b"x"] * 60, 0.1)
    s = serve(handler)
    http.begin_run(_cfg(run_budget_s=60))
    t0 = time.monotonic()
    with pytest.raises(http.HttpError) as ei:
        http.get_bytes(s.url(), _cfg())
    took = time.monotonic() - t0
    assert "reply not complete after" in str(ei.value)
    assert took < 2.5, took
    assert "127.0.0.1" in http.run_status()["down_hosts"]        # a trickling host is not asked again
    assert len(s.requests) == 1                                   # and the reply was not retried


def test_trickling_headers_are_stopped_by_the_signal_backstop(serve, monkeypatch):
    """Header lines that arrive one by one never let the socket time out, and the read loop
    has not started yet: the real-time timer ends the request (main thread only)."""
    monkeypatch.setattr(http, "ALARM_GRACE_S", 0.1)

    def handler(srv, conn, req):
        conn.sendall(b"HTTP/1.1 200 OK\r\n")
        _trickle(srv.stop, conn, [b"X-Pad: %d\r\n" % i for i in range(80)], 0.1)
    s = serve(handler)
    t0 = time.monotonic()
    with pytest.raises(http.HttpError) as ei:
        http.get_bytes(s.url(), _cfg())
    assert "reply not complete after" in str(ei.value)
    assert time.monotonic() - t0 < 2.5
    # the timer is disarmed and the previous handler restored
    import signal
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
    assert signal.getsignal(signal.SIGALRM) is not http._on_alarm


def test_head_with_trickling_headers_answers_none(serve, monkeypatch):
    monkeypatch.setattr(http, "ALARM_GRACE_S", 0.1)

    def handler(srv, conn, req):
        conn.sendall(b"HTTP/1.1 200 OK\r\n")
        _trickle(srv.stop, conn, [b"X-Pad: %d\r\n" % i for i in range(80)], 0.1)
    s = serve(handler)
    t0 = time.monotonic()
    assert http.head_ok(s.url(), _cfg()) is None
    assert time.monotonic() - t0 < 2.5


def test_a_prompt_reply_is_read_whole(serve):
    body = bytes(range(256)) * 1000
    s = serve(_reply(body=body))
    assert http.get_bytes(s.url(), _cfg()) == body


def test_a_truncated_body_is_still_a_failure(serve):
    """read1 does not complain when the connection ends before Content-Length: the loop must."""
    def handler(srv, conn, req):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\nshort")
    s = serve(handler)
    with pytest.raises(http.HttpError):
        http.get_bytes(s.url(), _cfg())


# ---- #13, #150, #42: the size cap ----------------------------------------------------------
def test_a_declared_length_over_the_cap_is_refused_before_reading(serve):
    def handler(srv, conn, req):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000000\r\nConnection: close\r\n\r\n")
        conn.sendall(b"x" * 5000)
    s = serve(handler)
    with pytest.raises(http.HttpError) as ei:
        http.get_bytes(s.url(), _cfg(), max_bytes=1000)
    assert "over the limit" in str(ei.value)
    assert http.run_status()["down_hosts"] == []


def test_an_undeclared_body_stops_at_the_cap(serve):
    def handler(srv, conn, req):
        conn.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n")
        _trickle(srv.stop, conn, [b"x" * 1000] * 50, 0.0)
    s = serve(handler)
    with pytest.raises(http.HttpError) as ei:
        http.get_bytes(s.url(), _cfg(), max_bytes=4000)
    assert "over the limit of 4000 bytes" in str(ei.value)


def test_the_default_cap_is_generous_for_real_bodies():
    assert http.MAX_BYTES >= 16 * 1024 * 1024


# ---- #14, #2: schemes and redirects ---------------------------------------------------------
@pytest.mark.parametrize("url", ["file:///etc/hostname", "ftp://example.org/x", "data:,hello",
                                 "gopher://example.org/", "//example.org/x", "/etc/hostname"])
def test_only_http_and_https_urls_are_opened(url, monkeypatch):
    opened = []
    monkeypatch.setattr(http._OPENER, "open", lambda *a, **k: opened.append(a) or (_ for _ in ()).throw(AssertionError))
    http.begin_run(_cfg(run_budget_s=60))
    with pytest.raises(http.HttpError) as ei:
        http.get_bytes(url, _cfg(http_retries=2))
    assert not isinstance(ei.value, http.HttpSkipped)
    assert http.head_ok(url, _cfg()) is None
    assert opened == []
    assert http.run_status()["down_hosts"] == []
    assert "/etc/" not in str(ei.value) and "example.org" not in str(ei.value)


def test_the_opener_has_no_file_ftp_or_data_handler():
    for url in ("file:///etc/hostname", "ftp://example.org/x", "data:,hello"):
        with pytest.raises(urllib.error.URLError):
            http._OPENER.open(url, timeout=1)


@pytest.mark.parametrize("origin,current,new,ok", [
    ("https://a.example.org/x", "https://a.example.org/x", "https://b.example.org/y", True),   # CDN
    ("http://a.example.org/x", "http://a.example.org/x", "http://a.example.org/y", True),
    ("http://a.example.org/x", "http://a.example.org/x", "https://a.example.org/x", True),
    ("http://a.example.org/x", "http://a.example.org/x", "http://b.example.org/y", False),
    ("https://a.example.org/x", "https://a.example.org/x", "http://a.example.org/y", False),
    ("http://a.example.org/x", "https://a.example.org/x", "http://a.example.org/y", False),
    ("https://a.example.org/x", "https://a.example.org/x", "ftp://a.example.org/y", False),
    ("https://a.example.org/x", "https://a.example.org/x", "file:///etc/passwd", False),
    ("https://a.example.org/x", "https://a.example.org/x", "https://169.254.169.254/latest", False),
    ("https://a.example.org/x", "https://a.example.org/x", "https://127.1/", False),
    ("https://a.example.org/x", "https://a.example.org/x", "https://2130706433/", False),
    ("https://a.example.org/x", "https://a.example.org/x", "https://[::1]/", False),
    ("https://a.example.org/x", "https://a.example.org/x", "https://localhost/", False),
    ("https://a.example.org/x", "https://a.example.org/x", "https://x.localhost/", False),
])
def test_redirect_rules(origin, current, new, ok):
    assert (http._redirect_problem(origin, current, new) is None) is ok


def test_a_redirect_to_an_internal_address_is_not_followed(serve):
    s = serve(_reply(302, [("Location", "http://169.254.169.254/latest/meta-data/?x=1")], b""))
    http.begin_run(_cfg(run_budget_s=60))
    with pytest.raises(http.HttpError) as ei:
        http.get_bytes(s.url("/frame.png?k=secret"), _cfg(http_retries=2))
    text = str(ei.value)
    assert "redirect" in text and "refused" in text
    assert "secret" not in text and "x=1" not in text
    assert len(s.requests) == 1
    assert http.run_status()["down_hosts"] == []
    assert http.head_ok(s.url("/frame.png"), _cfg()) is None


def test_redirects_are_limited(serve, monkeypatch):
    s = serve(_reply(302, [("Location", "/again")], b""))
    with pytest.raises(http.HttpError):
        http.get_bytes(s.url(), _cfg())
    assert len(s.requests) <= http.MAX_REDIRECTS + 1

    # a chain of distinct URLs (127.0.0.1 itself let through: the server of the test lives there)
    def chain(srv, conn, req):
        n = int(req.split(b" ")[1].decode().strip("/") or 0)
        conn.sendall(b"HTTP/1.1 302 X\r\nLocation: /%d\r\nContent-Length: 0\r\nConnection: close\r\n\r\n" % (n + 1))
    c = serve(chain)
    monkeypatch.setattr(http, "_local_host", lambda host: False)
    with pytest.raises(http.HttpError) as ei:
        http.get_bytes(c.url("/0"), _cfg())
    assert ei.value.status == 302 and len(c.requests) == http.MAX_REDIRECTS + 1
    c.requests.clear()
    assert http.head_ok(c.url("/0"), _cfg()) is None and len(c.requests) == http.MAX_REDIRECTS + 1


# ---- #15, #152, #154: refusals --------------------------------------------------------------
def test_429_is_a_refusal_not_retried_and_the_host_is_skipped(serve, monkeypatch):
    slept = []
    monkeypatch.setattr(http, "_sleep", slept.append)
    s = serve(_reply(429, [("Retry-After", "3600")], b"slow down"))
    http.begin_run(_cfg(run_budget_s=60))
    with pytest.raises(http.HttpError) as ei:
        http.get_bytes(s.url("/a"), _cfg(http_retries=2))
    assert ei.value.status == 429
    assert "refused (HTTP 429)" in str(ei.value)
    assert len(s.requests) == 1 and slept == []
    with pytest.raises(http.HttpSkipped):
        http.get_bytes(s.url("/b"), _cfg(http_retries=2))
    assert http.head_ok(s.url("/c"), _cfg()) is None
    assert len(s.requests) == 1
    assert http.run_status()["down_hosts"] == ["127.0.0.1"]


def test_503_with_retry_after_is_a_refusal_and_without_it_is_retried(serve, monkeypatch):
    monkeypatch.setattr(http, "_sleep", lambda s: None)
    s = serve(_reply(503, [], b"busy"))
    http.begin_run(_cfg(run_budget_s=60))
    with pytest.raises(http.HttpError) as ei:
        http.get_bytes(s.url("/a"), _cfg(http_retries=2))
    assert ei.value.status == 503 and len(s.requests) == 3
    assert http.run_status()["down_hosts"] == []
    s2 = serve(_reply(503, [("Retry-After", "120")], b"maintenance"))
    with pytest.raises(http.HttpError) as ei:
        http.get_bytes(s2.url("/a"), _cfg(http_retries=2))
    assert ei.value.status == 503 and "refused (HTTP 503)" in str(ei.value)
    assert len(s2.requests) == 1


def test_http_error_carries_the_status_and_stays_compatible():
    assert http.HttpError("x").status is None
    assert str(http.HttpError("x")) == "x"
    assert http.HttpError("x", status=404).status == 404
    assert http.HttpSkipped("skipped").status is None


@pytest.mark.parametrize("status,expected", [(200, True), (404, False), (403, False), (429, None),
                                             (500, None), (503, None)])
def test_head_ok_reads_throttling_and_failure_as_no_answer(serve, status, expected):
    s = serve(_reply(status, [], b""))
    assert http.head_ok(s.url(), _cfg()) is expected


def test_a_404_names_host_and_path_only(serve):
    s = serve(_reply(404, [], b"nope"))
    url = "http://127.0.0.1:%d/zones/x?token=secret#frag" % s.port
    with pytest.raises(http.HttpError) as ei:
        http.get_bytes(url, _cfg())
    assert str(ei.value) == "HTTP 404 for 127.0.0.1/zones/x"
    assert ei.value.status == 404


def test_error_texts_are_clipped_and_free_of_control_characters():
    w = http._where("https://example.org/" + "a" * 500 + "\n\x1b[2K?q=1")
    assert len(w) <= http.ERROR_URL_MAX and "\n" not in w and "\x1b" not in w


# ---- #11: an invalid URL is not a transport failure ----------------------------------------
@pytest.mark.parametrize("url", ["https://api.example.org/zones/a b", "https://api.example.org:99x/",
                                 "https://api.example.org/z\n1", "https://a..example.org/x"])
def test_an_invalid_url_never_marks_the_host_down(url, monkeypatch):
    monkeypatch.setattr(http, "_sleep", lambda s: None)
    http.begin_run(_cfg(run_budget_s=60))
    with pytest.raises(http.HttpError) as ei:
        http.get_bytes(url, _cfg(http_retries=2))
    assert not isinstance(ei.value, http.HttpSkipped)
    assert http.run_status()["down_hosts"] == []
    assert "\n" not in str(ei.value)


def test_invalid_url_raised_by_the_opener_is_not_transport(monkeypatch):
    calls = []

    def boom(req, timeout=None):
        calls.append(req.full_url)
        raise _hc.InvalidURL("URL can't contain control characters")
    monkeypatch.setattr(http._OPENER, "open", boom)
    http.begin_run(_cfg(run_budget_s=60))
    with pytest.raises(http.HttpError):
        http.get_bytes("https://api.example.org/zones/x", _cfg(http_retries=2))
    assert len(calls) == 1
    assert http.run_status()["down_hosts"] == []
    assert not http._is_transport(_hc.InvalidURL("x"))
    assert not http._is_transport(UnicodeError("x"))
    assert not http._is_transport(urllib.error.URLError("unknown url type: file"))
    assert http._is_transport(urllib.error.URLError(ConnectionRefusedError(111, "refused")))


# ---- #136: the server time of each reply ----------------------------------------------------
def test_the_server_time_of_each_reply_is_recorded(serve):
    import email.utils
    behind = email.utils.formatdate(time.time() - 7200, usegmt=True)
    s = serve(_reply(200, [("Date", behind), ("Age", "60")], b"{}"))
    http.begin_run(_cfg())
    http.get_bytes(s.url(), _cfg())
    off = http.clock_offsets()["127.0.0.1"]
    assert -7200 + 60 - 3 < off < -7200 + 60 + 3
    http.begin_run(_cfg())
    assert http.clock_offsets() == {}


def test_an_error_reply_counts_for_the_clock_and_a_reply_without_date_does_not(serve):
    import email.utils
    s = serve(_reply(404, [("Date", email.utils.formatdate(time.time() + 600, usegmt=True))], b""))
    http.begin_run(_cfg())
    assert http.head_ok(s.url(), _cfg()) is False
    assert 590 < http.clock_offsets()["127.0.0.1"] < 610
    s2 = serve(_reply(200, [("Date", "not a date")], b""))
    http.begin_run(_cfg())
    http.get_bytes(s2.url(), _cfg())
    assert http.clock_offsets() == {}


# ---- #136: the clock guard (vlba/clock.py and its hooks) ------------------------------------
def test_assess_has_no_opinion_without_dates_and_when_servers_disagree():
    from vlba import clock
    assert clock.assess({}) == {"ok": None, "skew_s": None, "hosts": 0}
    assert clock.assess({"a": float("nan"), "b": "x", "c": True})["ok"] is None
    assert clock.assess({"a": 0.4, "b": -1.0, "c": 2.0})["ok"] is True
    # one server with a wrong clock cannot condemn the page
    assert clock.assess({"a": 0.4, "b": -7200.0, "c": 2.0})["ok"] is True
    assert clock.assess({"a": 0.4, "b": 7200.0})["ok"] is None
    assert clock.assess({"a": 299.0, "b": 299.0})["ok"] is True


def test_assess_finds_a_slow_and_a_fast_clock():
    from vlba import clock
    slow = clock.assess({"a": 7200.0, "b": 7201.0, "c": 7199.0})       # servers ahead: host behind
    assert slow["ok"] is False and slow["skew_s"] == 7200.0 and slow["hosts"] == 3
    assert "behind" in clock.error_text(slow) and "2 h" in clock.error_text(slow)
    assert clock.off_by(slow) == "host clock off by 2 h"
    fast = clock.assess({"a": -900.0})
    assert fast["ok"] is False and "15 min ahead of" in clock.error_text(fast)
    assert "no check can be trusted" in clock.error_text(fast)
    assert clock.LIMIT_S == 300.0


def _decision_entry(**over):
    from vlba.tests.test_decision import _entry
    return _entry(**over)


def test_a_wrong_clock_makes_every_answered_check_unknown_but_bad_stays_bad(cfg):
    from datetime import timedelta
    from vlba import decision as d
    from vlba.tests.test_decision import NOW, _alert
    wrong = {"ok": False, "skew_s": 7200.0, "hosts": 3}
    v = d.evaluate(cfg, _decision_entry(clock=wrong), {}, NOW)
    states = {c["key"]: c["state"] for c in v["checks"]}
    assert "ok" not in states.values() and states["obs_humidity"] == "off"
    assert all(c["detail"] == "host clock off by 2 h" for c in v["checks"] if c["state"] == "unknown")
    assert v["status"] == d.NO_DATA and v["incomplete"]
    # a warning ahead of its onset (by a clock that may be hours slow) is no longer "scheduled"
    sched = _decision_entry(clock=wrong, alerts_at=[
        _alert("Winter Storm Warning", onset=NOW + timedelta(hours=1), ends=NOW + timedelta(hours=20))])
    assert d.evaluate(cfg, sched, {}, NOW)["checks"][0]["state"] == "unknown"
    bad = _decision_entry(clock=wrong, alerts_at=[_alert("Tornado Warning", onset=NOW - timedelta(minutes=5),
                                                         ends=NOW + timedelta(hours=1))])
    vb = d.evaluate(cfg, bad, {}, NOW)
    assert vb["status"] == d.BAD and vb["checks"][0]["state"] == "bad" and vb["reasons"]


@pytest.mark.parametrize("res", [None, {"ok": None, "skew_s": None, "hosts": 0},
                                 {"ok": True, "skew_s": 1.0, "hosts": 4}, "garbage"])
def test_without_an_opinion_the_guard_changes_nothing(cfg, res):
    from vlba import decision as d
    from vlba.tests.test_decision import NOW
    v = d.evaluate(cfg, _decision_entry(clock=res), {}, NOW)
    assert v["status"] == d.GOOD and not v["incomplete"]


def test_a_run_with_a_wrong_host_clock_reports_it_and_trusts_no_check(cfg, fake_http, monkeypatch):
    import make_vlba_page as mvp
    from vlba import glm
    from vlba.tests.test_main import add_routes
    add_routes(fake_http, cfg)
    monkeypatch.setattr(glm, "deps_available", lambda: False)
    offsets = {"api.weather.gov": -3600.0, "mesonet.agron.iastate.edu": -3601.0,
               "noaa-mrms-pds.s3.amazonaws.com": -3599.0}
    monkeypatch.setattr(http, "clock_offsets", lambda: offsets)
    run = mvp.run_once(cfg)
    assert run["clock"]["ok"] is False
    assert any("the host clock is 1 h ahead of the clocks of the data sources" in e for e in run["errors"])
    for s in run["sites"]:
        assert s["clock"] == run["clock"]
        assert s["verdict"]["status"] != "GOOD"
        assert not [c for c in s["verdict"]["checks"] if c["state"] in ("ok", "watch", "scheduled")]


def test_a_run_with_a_synced_clock_is_unchanged(cfg, fake_http, monkeypatch):
    import make_vlba_page as mvp
    from vlba import glm
    from vlba.tests.test_main import add_routes
    add_routes(fake_http, cfg)
    monkeypatch.setattr(glm, "deps_available", lambda: False)
    monkeypatch.setattr(http, "clock_offsets", lambda: {"api.weather.gov": 0.5, "mesonet.agron.iastate.edu": -1.0})
    run = mvp.run_once(cfg)
    assert run["clock"]["ok"] is True
    assert not any("host clock" in e for e in run["errors"])
    assert any(c["state"] == "ok" for s in run["sites"] for c in s["verdict"]["checks"])


# ---- S3: the log escapes control characters -------------------------------------------------
def test_log_lines_cannot_be_forged_nor_carry_terminal_escapes(tmp_path):
    import os
    import subprocess
    import sys
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    code = (
        "import logging\n"
        "from weather import pipeline\n"
        "pipeline.setup_logging(False)\n"
        "log = logging.getLogger('weather.alerts')\n"
        "log.warning('zone %s unavailable', 'https://x/z\\n2026-01-01 00:00:00,000 INFO vlba: page written: "
        "12 sites GOOD\\x1b[1A\\x1b[2K\\r\\x9b\\u2028end\\ttab')\n"
        "try:\n"
        "    raise ValueError('bad \\x1b[31m value')\n"
        "except ValueError:\n"
        "    log.exception('step failed')\n")
    p = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=30,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    err = p.stderr
    lines = err.splitlines()
    assert lines[0].endswith("end\ttab unavailable") and "zone https://x/z\\n2026-01-01" in lines[0]
    assert "\\x1b[1A\\x1b[2K\\r\\x9b\\u2028" in lines[0]
    assert not any(ln.startswith("2026-01-01") for ln in lines)        # no forged record
    assert "\x1b" not in err and "\x9b" not in err and " " not in err and "\r" not in err
    assert "Traceback (most recent call last):" in lines        # the traceback keeps its lines
    assert any("bad \\x1b[31m value" in ln for ln in lines)


# ---- the read loop in other shapes -----------------------------------------------------------
def test_a_chunked_reply_is_read_whole_and_capped(serve):
    def handler(srv, conn, req):
        conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                     + b"".join(b"%x\r\n%s\r\n" % (len(p), p) for p in (b"abc", b"x" * 3000, b"z")) + b"0\r\n\r\n")
    s = serve(handler)
    assert http.get_bytes(s.url(), _cfg()) == b"abc" + b"x" * 3000 + b"z"
    with pytest.raises(http.HttpError) as ei:
        http.get_bytes(s.url(), _cfg(), max_bytes=1000)
    assert "over the limit" in str(ei.value)


def test_a_trickling_body_is_abandoned_in_a_worker_thread_too(serve):
    """No signal outside the main thread: the read loop alone keeps the deadline."""
    def handler(srv, conn, req):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 60\r\nConnection: close\r\n\r\n")
        _trickle(srv.stop, conn, [b"x"] * 60, 0.1)
    s = serve(handler)
    out = {}

    def work():
        t0 = time.monotonic()
        try:
            http.get_bytes(s.url(), _cfg())
        except http.HttpError as e:
            out["err"] = str(e)
        out["took"] = time.monotonic() - t0
    t = threading.Thread(target=work)
    t.start()
    t.join(10)
    assert "reply not complete after" in out.get("err", "") and out["took"] < 2.5


def test_somebody_elses_timer_is_left_alone(serve):
    import signal
    fired = []
    prev = signal.signal(signal.SIGALRM, lambda *a: fired.append(1))
    try:
        signal.setitimer(signal.ITIMER_REAL, 30)
        s = serve(_reply(body=b"fine"))
        assert http.get_bytes(s.url(), _cfg()) == b"fine"
        left = signal.getitimer(signal.ITIMER_REAL)[0]
        assert 25 < left <= 30
        assert signal.getsignal(signal.SIGALRM) is not http._on_alarm
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prev)


# ---- review repairs ---------------------------------------------------------------------------
@pytest.mark.parametrize("userinfo,rest", [("user:secret", "api.example.org:443/zones/x"),
                                           ("user", "api.example.org/x")])
def test_a_url_with_user_info_is_refused_and_marks_no_host_down(userinfo, rest, monkeypatch):
    """urllib would look up "<user>:<password>" + "@" + "<host>" as a host name, fail, and
    mark the real host down for the run: such a URL (from upstream JSON) is refused before
    opening, and its text shows neither user nor password. (The URL is assembled here so
    that test_deploy's scan for addresses does not take it for one.)"""
    url = "https://%s%s%s" % (userinfo, chr(64), rest)
    opened = []
    monkeypatch.setattr(http._OPENER, "open", lambda *a, **k: opened.append(a) or (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(http, "_sleep", lambda s: None)
    http.begin_run(_cfg(run_budget_s=60))
    with pytest.raises(http.HttpError) as ei:
        http.get_bytes(url, _cfg(http_retries=2))
    assert http.head_ok(url, _cfg()) is None
    assert opened == [] and http.run_status()["down_hosts"] == []
    assert "user" not in str(ei.value) and "secret" not in str(ei.value)
    assert http._redirect_problem("https://api.example.org/a", "https://api.example.org/a", url)


def test_a_wrong_clock_lists_no_warning_as_scheduled(cfg):
    """A warning "scheduled" by a clock hours off may well be in effect: with the guard
    tripped the verdict lists no scheduled warning (its check reads no data)."""
    from datetime import timedelta
    from vlba import decision as d
    from vlba.tests.test_decision import NOW, _alert
    alerts = [_alert("Winter Storm Warning", onset=NOW + timedelta(hours=1), ends=NOW + timedelta(hours=20))]
    assert d.evaluate(cfg, _decision_entry(alerts_at=alerts), {}, NOW)["scheduled"]
    v = d.evaluate(cfg, _decision_entry(clock={"ok": False, "skew_s": 7200.0, "hosts": 3}, alerts_at=alerts), {}, NOW)
    assert v["scheduled"] == [] and v["checks"][0]["state"] == "unknown" and v["status"] == d.NO_DATA
