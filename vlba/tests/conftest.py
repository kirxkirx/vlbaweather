"""Shared fixtures of the vlbaweather tests. The network is BLOCKED in every test: every
fetch goes through ``weather.http`` (``get_bytes``, ``head_ok``, ``fetch_cacheable``,
``fetch_conditional``), which the ``fake_http`` fixture replaces with the in-memory
``FakeHttp`` registry (``vlba/tests/fakehttp.py``); modules with their own
transport (``vlba.glm``, which downloads GLM granules in threads) expose a module-level
``_get`` hook that their tests monkeypatch.

``cfg`` is a ``VlbaConfig`` for two antennas: Pie Town (inside the MRMS grid) and Mauna Kea
(outside it: the island radar path), with small maps so the renderer tests stay quick.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
VLBAWEATHER_DIR = os.path.dirname(os.path.dirname(HERE))          # the checkout: weather/, vlba/
if VLBAWEATHER_DIR not in sys.path:
    sys.path.insert(0, VLBAWEATHER_DIR)

from weather import http as _http                                  # noqa: E402
from vlba.tests.fakehttp import FakeHttp                           # noqa: E402,F401
from vlba.config import VlbaConfig                                 # noqa: E402
from vlba.sites import VLBA_SITES                                  # noqa: E402

PT = next(dict(s) for s in VLBA_SITES if s["slug"] == "pt")
MK = next(dict(s) for s in VLBA_SITES if s["slug"] == "mk")
SC = next(dict(s) for s in VLBA_SITES if s["slug"] == "sc")
TEST_SITES = [PT, MK]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _blocked(*a, **k):
        raise AssertionError("network access attempted in a test: %r" % (a[:1],))
    monkeypatch.setattr("urllib.request.urlopen", _blocked)


@pytest.fixture
def fake_http(monkeypatch):
    fh = FakeHttp()
    monkeypatch.setattr(_http, "get_bytes", fh.get_bytes)
    monkeypatch.setattr(_http, "head_ok", fh.head_ok)
    monkeypatch.setattr(_http, "fetch_conditional", fh.fetch_conditional)
    monkeypatch.setattr(_http, "fetch_cacheable", fh.fetch_cacheable)
    return fh


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith(("WEATHER_", "VLBA_")):
            monkeypatch.delenv(k, raising=False)
    return VlbaConfig.from_env(out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "cache"),
                               map_px=240, tile_zoom=8, sites=[dict(s) for s in TEST_SITES])


@pytest.fixture
def cache(cfg):
    return _http.Cache(cfg.cache_dir)


@pytest.fixture(autouse=True)
def _fresh_http_run_state(monkeypatch):
    """The parent's per-run network budget / circuit breaker is module state; every test
    starts from a pristine one."""
    if hasattr(_http, "_RunState") and hasattr(_http, "_RUN"):
        monkeypatch.setattr(_http, "_RUN", _http._RunState())


# ---- MRMS GRIB2 files (vlba.mrms): what NOAA's bucket serves, in small ----------------------
def _sm(v: int, n: int) -> bytes:
    """A GRIB2 signed integer of ``n`` bytes (sign and magnitude)."""
    return ((1 << (8 * n - 1)) | -v).to_bytes(n, "big") if v < 0 else v.to_bytes(n, "big")


def grib2_message(rows, la1, lo1, di, dj, ts, scan=0x00, R=-9990.0, E=0, D=1, template3=0, template5=41,
                  bitmap=255, gz=True, angle=(1, 1000000)):
    """A GRIB2 message as MRMS writes it: grid template 3.0 (``la1``/``lo1`` the first
    cell's centre, degrees; ``lo1`` as given, MRMS uses 0..360), data template 5.41 (a
    16-bit grey PNG of ``X``, value = (R + X * 2**E) / 10**D), no bitmap. ``rows`` are the
    values (dBZ, -99 no echo, -999 no coverage) in the scanning order. ``angle`` is the basic
    angle and its subdivisions (MRMS: 1 and 10**6, so coordinates count micro-degrees; (0, 0)
    means the same by default). gzip'ed like the bucket's ``.grib2.gz`` unless ``gz`` is false."""
    import gzip
    import io
    import struct
    from PIL import Image

    nj, ni = len(rows), len(rows[0])
    im = Image.new("I;16", (ni, nj))
    im.putdata([int(round((v * 10 ** D - R) / 2 ** E)) for row in rows for v in row])
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    png = buf.getvalue()
    sec1 = bytearray(21)
    sec1[0:4] = (21).to_bytes(4, "big")
    sec1[4] = 1
    sec1[12:14] = ts.year.to_bytes(2, "big")
    sec1[14:19] = bytes([ts.month, ts.day, ts.hour, ts.minute, ts.second])
    sec3 = bytearray(72)
    sec3[0:4] = (72).to_bytes(4, "big")
    sec3[4] = 3
    sec3[6:10] = (ni * nj).to_bytes(4, "big")
    sec3[12:14] = template3.to_bytes(2, "big")
    sec3[30:34] = ni.to_bytes(4, "big")
    sec3[34:38] = nj.to_bytes(4, "big")
    sec3[38:42] = angle[0].to_bytes(4, "big")
    sec3[42:46] = angle[1].to_bytes(4, "big")
    missing = (0, 0xFFFFFFFF)                   # either one 0 or all ones: micro-degrees
    per = 1e6 if angle[0] in missing or angle[1] in missing else angle[1] / float(angle[0])
    sec3[46:50] = _sm(int(round(la1 * per)), 4)
    sec3[50:54] = _sm(int(round(lo1 * per)), 4)
    sec3[63:67] = int(round(di * per)).to_bytes(4, "big")
    sec3[67:71] = int(round(dj * per)).to_bytes(4, "big")
    sec3[71] = scan
    sec4 = (9).to_bytes(4, "big") + bytes([4, 0, 0, 0, 0])
    sec5 = (21).to_bytes(4, "big") + bytes([5]) + (ni * nj).to_bytes(4, "big") + template5.to_bytes(2, "big") + \
        struct.pack(">f", R) + _sm(E, 2) + _sm(D, 2) + bytes([16, 0])
    sec6 = (6).to_bytes(4, "big") + bytes([6, bitmap])
    sec7 = (5 + len(png)).to_bytes(4, "big") + bytes([7]) + png
    body = bytes(sec1) + bytes(sec3) + sec4 + sec5 + sec6 + sec7 + b"7777"
    raw = b"GRIB" + bytes([0, 0, 0, 2]) + (16 + len(body)).to_bytes(8, "big") + body
    return gzip.compress(raw) if gz else raw


def grib2_around(lat, lon, ts, value=-99.0, holes=(), span_deg=0.8, cell_deg=0.01, **kw):
    """An MRMS-like file of a small grid centred on (lat, lon), ``span_deg`` across: every
    cell ``value`` (-99: radar sees no echo) except the boxes ``holes`` ((lat0, lat1, lon0,
    lon1) each), where no radar covers (-999)."""
    n = int(round(span_deg / cell_deg))
    la1 = lat + span_deg / 2.0 - cell_deg / 2.0
    lo1 = lon - span_deg / 2.0 + cell_deg / 2.0
    rows = []
    for j in range(n):
        clat = la1 - j * cell_deg
        row = []
        for i in range(n):
            clon = lo1 + i * cell_deg
            row.append(-999.0 if any(a <= clat <= b and c <= clon <= d for a, b, c, d in holes) else value)
        rows.append(row)
    return grib2_message(rows, la1, lo1 % 360.0, cell_deg, cell_deg, ts, **kw)
