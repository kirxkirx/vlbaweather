"""Radar coverage of each antenna's ring, from MRMS's own GRIB2 files (NOAA Open Data on AWS).

The radar check reads IEM's MRMS frames inside the contiguous US and the NWS composites for
the islands. Both write "no radar covers this cell" the same as "no echo" (IEM's palette
index 0, the WMS's transparent pixel), so a radar outage near an antenna would read as
"clear". MRMS's GRIB2 files of the same products keep the two apart: -999 where no radar
covers the cell, -99 where a radar covers it and sees no echo. This module reads the file of
the frame's time and counts, per antenna, the cells of its ring that no radar covers:

* CONUS: ``SeamlessHSR_00.00``, Seamless Hybrid Scan Reflectivity, the product behind IEM's
  ``lcref`` frames (the same 7000 x 3500 grid of 0.01 deg; a file every 2 min, at :00 s;
  about 1.3 MB), keyed by the IEM frame's time;
* HAWAII, CARIB (GUAM, ALASKA): ``MergedBaseReflectivityQC_00.50``, the product behind the
  NWS composites (20-60 KB), keyed by the composite's frame time, to the second.

Where the file comes from: NOAA's bucket (``cfg.mrms_url``) first, then, for CONUS, a mirror
(``cfg.mrms_conus_mirror``, a strftime pattern; IEM's archive, which gets the same files
from NCEP directly, byte for byte the bucket's). NOAA's feed to the bucket is not real time
by contract: it fell 25-30 min behind in every domain on 2026-11-10 and stalled for an hour
on 2026-09-11, while IEM had the CONUS files within minutes (on 2026-10-07, 94 min of MRMS's
files reached neither in time). In turn: the file of the frame's time from each source; the
nearest file the sources list within ``MAX_SKEW_S`` of that time (coverage changes over
hours, not minutes), or the newest of the last ``2 * MAX_SKEW_S`` when the frame's time is
unknown (a listing page that fails leaves a gap, except a 404 for a period that began less
than ``UNBORN_S`` ago: IEM makes a day's directory with its first file). Failing that too, where ``cfg.radar_coverage_max_lag_s`` allows it (one hour by
default, the user's choice of 2026-11-10; 0 = never: a radar that failed after an older file
reads "clear" until a newer one shows it), the frame's time is known, the frame is no reused
copy and every source listed the window around that time completely without a file in it,
the newest file at most that much older stands in, marked ``older``.

What is read: a file counts only when it is MRMS's file of the product (GRIB2 discipline 209
and the product's parameter, ``PRODUCT_IDS``), its own GRIB2 time is the time its name gives
(to the second) and it decodes; anything else is passed over for the next file, never a
guess. Only grid template 3.0 (regular lat/lon) and data template 5.41 (PNG) are read, which
is what MRMS writes. Decoding needs Pillow only (the CONUS grid takes about 100 MB for a
fraction of a second).

What it costs: a normal run reads one file per domain, as before. A lookup is bounded: every
request has ``REQUEST_TIMEOUT_S`` per attempt and one retry, none starts after
``LOOKUP_DEADLINE_S``, each source gets ``TRIES_PER_SOURCE`` listed files (a host the run
already gave up on, ``HttpSkipped``, costs nothing and counts for nothing), and the mirror is
asked only while ``MIRROR_RESERVE_S`` of the run's budget is left.

``coverage`` never raises on network or data problems; it returns ``{"ok", "error",
"domain", "ts", "url", "source", "older", "behind_s", "note", "sites": {slug: {"cells",
"uncovered", "covered_pct"}}}``, where ``ts`` is the file's own time, ``source`` "bucket" or
"mirror", ``behind_s`` how far the file's time lies before the frame's (before now when the
frame's time is unknown), ``note`` why the bucket's file of the frame's time was not read
when another one was, ``cells`` the grid cells whose centre lies within the ring (the radar
ring check's own great-circle distance) and ``uncovered`` those of them no radar covers, a
part of the ring beyond the grid's edge included.
"""
from __future__ import annotations

import gzip
import io
import logging
import math
import re
import struct
import time
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

try:
    from PIL import Image
except Exception:                       # pragma: no cover - optional dependency
    Image = None

from weather import geo, http, util

from . import radarcheck
from .config import MRMS_CONUS_PRODUCT, mirror_name_regex

log = logging.getLogger("vlba.mrms")

PRODUCTS = {"CONUS": MRMS_CONUS_PRODUCT, "HAWAII": "MergedBaseReflectivityQC_00.50",
            "CARIB": "MergedBaseReflectivityQC_00.50", "GUAM": "MergedBaseReflectivityQC_00.50",
            "ALASKA": "MergedBaseReflectivityQC_00.50"}
# MRMS's GRIB2 identity of each product: (discipline, parameter category, parameter number),
# 209 being MRMS's local discipline. Read from the bucket's files on 2026-11-10 (IEM's alike).
PRODUCT_IDS = {MRMS_CONUS_PRODUCT: (209, 8, 8), "MergedBaseReflectivityQC_00.50": (209, 11, 0)}
# the workspaces of the NWS composites (vlba.nwsradar) and their MRMS domains
REGION_DOMAINS = {"hawaii": "HAWAII", "carib": "CARIB", "guam": "GUAM", "alaska": "ALASKA",
                  "conus": "CONUS"}
MAX_SKEW_S = 300                # a file this close to the frame's time stands in for its own
TIME_TOLERANCE_S = 1            # a file's own GRIB2 time against its name's (equal, measured)
LOOKUP_DEADLINE_S = 60          # no request of a lookup starts later than this
REQUEST_TIMEOUT_S = 15          # per attempt, one retry: a dead host costs ~31 s, not ~80
MIRROR_RESERVE_S = 60           # the run's budget the mirror leaves to the rest of the run
TRIES_PER_SOURCE = 2            # listed files fetched per source
UNBORN_S = 900                  # a listing period this new may have no directory yet (IEM's new day)
NO_COVERAGE_MAX = -998.0        # MRMS: -999 = no radar covers the cell (-99: covered, no echo)

_KEY_RE = re.compile(r"<Key>([^<]+)</Key>")
_KEY_TIME_RE = re.compile(r"_(\d{8})-(\d{6})\.grib2(?:\.gz)?$")
_URL_RE = re.compile(r"https?://([^/\s?;)]+)[^\s;)]*")
_clock = time.monotonic         # the lookup's deadline (tests replace it)


# ---- GRIB2 -------------------------------------------------------------------------------------
def _sint(b: bytes) -> int:
    """A GRIB2 signed integer (sign and magnitude, not two's complement)."""
    v = int.from_bytes(b, "big")
    top = 1 << (8 * len(b) - 1)
    return -(v & (top - 1)) if v & top else v


def parse_grib2(raw: bytes) -> Dict[str, Any]:
    """The reference time, the product, the grid, the packing and the PNG payload of the first
    field of a GRIB2 message: ``{"ts", "discipline", "parameter": (category, number) | None,
    "grid": {"ni", "nj", "la1", "lo1", "di", "dj", "scan"}, "drs": {"R", "E", "D", "nbits"},
    "payload"}``. ValueError for anything but a regular lat/lon grid (template 3.0) with
    PNG-packed values (template 5.41) and no bitmap."""
    if len(raw) < 16 or raw[:4] != b"GRIB" or raw[7] != 2:
        raise ValueError("not a GRIB2 message")
    total = int.from_bytes(raw[8:16], "big")
    if total > len(raw):
        raise ValueError("GRIB2 message truncated (%d of %d bytes)" % (len(raw), total))
    out: Dict[str, Any] = {"discipline": raw[6], "parameter": None}
    pos = 16
    while pos + 5 <= total and raw[pos:pos + 4] != b"7777":
        ln = int.from_bytes(raw[pos:pos + 4], "big")
        num = raw[pos + 4]
        if ln < 5 or pos + ln > total:
            raise ValueError("GRIB2 section %d has a bad length (%d)" % (num, ln))
        sec = raw[pos:pos + ln]
        if num == 1 and ln >= 19:
            year = int.from_bytes(sec[12:14], "big")
            try:
                out["ts"] = datetime(year, sec[14], sec[15], sec[16], sec[17], sec[18], tzinfo=timezone.utc)
            except ValueError:
                out["ts"] = None
        elif num == 4 and ln >= 11:
            out["parameter"] = (sec[9], sec[10])
        elif num == 3:
            tmpl = int.from_bytes(sec[12:14], "big")
            if tmpl != 0 or ln < 72:
                raise ValueError("grid template 3.%d (only 3.0, regular lat/lon, is read)" % tmpl)
            basic, sub = int.from_bytes(sec[38:42], "big"), int.from_bytes(sec[42:46], "big")
            unit = 1e-6 if basic in (0, 0xFFFFFFFF) or sub in (0, 0xFFFFFFFF) else basic / float(sub)
            out["grid"] = {"ni": int.from_bytes(sec[30:34], "big"), "nj": int.from_bytes(sec[34:38], "big"),
                           "la1": _sint(sec[46:50]) * unit, "lo1": _sint(sec[50:54]) * unit,
                           "di": int.from_bytes(sec[63:67], "big") * unit,
                           "dj": int.from_bytes(sec[67:71], "big") * unit, "scan": sec[71]}
        elif num == 5:
            tmpl = int.from_bytes(sec[9:11], "big")
            if tmpl != 41 or ln < 21:
                raise ValueError("data template 5.%d (only 5.41, PNG, is read)" % tmpl)
            out["drs"] = {"R": struct.unpack(">f", sec[11:15])[0], "E": _sint(sec[15:17]),
                          "D": _sint(sec[17:19]), "nbits": sec[19]}
        elif num == 6:
            if ln < 6 or sec[5] != 255:
                raise ValueError("a bitmap (only fields without one are read)")
        elif num == 7:
            out["payload"] = bytes(sec[5:])
            break                       # the first field is all MRMS writes
        pos += ln
    missing = [k for k in ("grid", "drs", "payload") if k not in out]
    if missing:
        raise ValueError("GRIB2 message without %s" % ", ".join(missing))
    g = out["grid"]
    if g["ni"] < 1 or g["nj"] < 1 or not g["di"] > 0 or not g["dj"] > 0:
        raise ValueError("GRIB2 grid of %d x %d cells of %g x %g deg" % (g["ni"], g["nj"], g["di"], g["dj"]))
    if g["scan"] not in (0x00, 0x40):
        raise ValueError("GRIB2 scanning mode 0x%02x (only 0x00 and 0x40 are read)" % g["scan"])
    out.setdefault("ts", None)
    return out


def decode(meta: Dict[str, Any]):
    """The PNG payload as a Pillow image of ``ni`` x ``nj`` integer pixels."""
    if Image is None:
        raise ValueError("Pillow is not installed")
    g = meta["grid"]
    im = Image.open(io.BytesIO(meta["payload"]))
    if im.format != "PNG":
        raise ValueError("payload is %s, not PNG" % im.format)
    im.load()
    if im.size != (g["ni"], g["nj"]):
        raise ValueError("PNG of %d x %d for a grid of %d x %d" % (im.size + (g["ni"], g["nj"])))
    if im.mode not in ("I", "I;16", "I;16B", "L"):
        raise ValueError("PNG mode %s (a grey scale of 8 or 16 bits is read)" % im.mode)
    return im


def ring_stats(meta: Dict[str, Any], im, lat: float, lon: float, radius_km: float) -> Dict[str, Any]:
    """``{"cells", "uncovered", "covered_pct"}`` of the ring of ``radius_km`` around
    (lat, lon). A ring cell beyond the grid's edge counts as uncovered."""
    g, d = meta["grid"], meta["drs"]
    lo1 = g["lo1"] - 360.0 if g["lo1"] > 180.0 else g["lo1"]
    north_up = not (g["scan"] & 0x40)           # 0x00: rows run north to south
    di, dj, ni, nj = g["di"], g["dj"], g["ni"], g["nj"]
    scale, offset = 2.0 ** d["E"] / 10.0 ** d["D"], d["R"] / 10.0 ** d["D"]
    dlat = radius_km / 110.5 + dj
    dlon = radius_km / (111.3 * max(0.01, math.cos(math.radians(lat)))) + di
    if north_up:
        j0, j1 = math.floor((g["la1"] - (lat + dlat)) / dj), math.ceil((g["la1"] - (lat - dlat)) / dj)
    else:
        j0, j1 = math.floor(((lat - dlat) - g["la1"]) / dj), math.ceil(((lat + dlat) - g["la1"]) / dj)
    i0, i1 = math.floor(((lon - dlon) - lo1) / di), math.ceil(((lon + dlon) - lo1) / di)
    ci0, ci1, cj0, cj1 = max(0, i0), min(ni - 1, i1), max(0, j0), min(nj - 1, j1)
    vals: List[int] = []
    width = ci1 - ci0 + 1
    if ci0 <= ci1 and cj0 <= cj1:
        vals = radarcheck.pixels(im.crop((ci0, cj0, ci1 + 1, cj1 + 1)))
    cells = uncovered = 0
    for j in range(j0, j1 + 1):
        clat = g["la1"] - j * dj if north_up else g["la1"] + j * dj
        for i in range(i0, i1 + 1):
            if geo.haversine_km(lat, lon, clat, lo1 + i * di) > radius_km:
                continue
            cells += 1
            if not (cj0 <= j <= cj1 and ci0 <= i <= ci1):
                uncovered += 1          # beyond the grid: no radar of this domain
            elif offset + vals[(j - cj0) * width + (i - ci0)] * scale <= NO_COVERAGE_MAX:
                uncovered += 1
    pct = round(100.0 * (cells - uncovered) / cells, 1) if cells else None
    return {"cells": cells, "uncovered": uncovered, "covered_pct": pct}


# ---- the sources -------------------------------------------------------------------------------
def _key(domain: str, product: str, ts: datetime) -> str:
    day = ts.strftime("%Y%m%d")
    return "%s/%s/%s/MRMS_%s_%s.grib2.gz" % (domain, product, day, product, ts.strftime("%Y%m%d-%H%M%S"))


def _hours(start: datetime, end: datetime) -> List[datetime]:
    """The whole hours (UTC) from ``start``'s to ``end``."""
    t = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    out = []
    while t <= end:
        out.append(t)
        t += timedelta(hours=1)
    return out


def _bucket_pages(cfg, domain: str, product: str, start: datetime, end: datetime) -> List[Tuple[datetime, str]]:
    """``(the hour, its S3 listing request)`` of the hours from ``start`` to ``end``, one per
    key prefix."""
    out = []
    for t in _hours(start, end):
        prefix = "%s/%s/%s/MRMS_%s_%s" % (domain, product, t.strftime("%Y%m%d"), product, t.strftime("%Y%m%d-%H"))
        out.append((t, "%s/?list-type=2&prefix=%s" % (cfg.mrms_url, quote(prefix, safe="/"))))
    return out


def _bucket_files(cfg, body: str) -> List[Tuple[datetime, str]]:
    """``(time, url)`` of the files an S3 listing page names."""
    out = []
    for key in _KEY_RE.findall(body):
        m = _KEY_TIME_RE.search(key)
        if m:
            try:
                kt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            out.append((kt, "%s/%s" % (cfg.mrms_url, key)))
    return out


def mirror_pattern(cfg, domain: str) -> str:
    """The strftime pattern of the domain's mirror, "" when it has none (only CONUS can)."""
    pattern = getattr(cfg, "mrms_conus_mirror", "") if domain == "CONUS" else ""
    return pattern if isinstance(pattern, str) else ""


def _mirror_pages(pattern: str, start: datetime, end: datetime) -> List[Tuple[datetime, str]]:
    """``(the first hour it lists, the mirror's index page)`` of the hours from ``start`` to
    ``end``: the directory the pattern gives each hour, narrowed to that hour's names with
    ``?P=`` where the file name holds the hour before the minutes (IEM: 9 KB instead of the
    day's 190 KB; a server that ignores it sends the whole page, which reads the same)."""
    head, _, name = pattern.rpartition("/")
    cut = min([i for i in (name.find("%M"), name.find("%S")) if i >= 0] or [len(name)])
    prefix = name[:cut]
    hourly = all(f in prefix for f in ("%Y", "%m", "%d", "%H"))
    out: List[Tuple[datetime, str]] = []
    for t in _hours(start, end):
        page = t.strftime(head) + "/"
        if hourly:
            page += "?P=" + quote(t.strftime(prefix) + "*", safe="*")
        if page not in (p for _, p in out):
            out.append((t, page))
    return out


def _mirror_files(pattern: str, page: str, body: str) -> List[Tuple[datetime, str]]:
    """``(time, url)`` of the files an index page names. Any page the server writes will do:
    only names the pattern gives are read, never the page's own dates (IEM writes them in US
    Central time, without a zone)."""
    rx = mirror_name_regex(pattern.rpartition("/")[2])
    base = page.split("?", 1)[0]
    out: Dict[str, datetime] = {}
    for m in rx.finditer(body):
        g = m.groupdict()
        try:
            kt = datetime(int(g["Y"]), int(g["m"]), int(g["d"]), int(g["H"]), int(g["M"]), int(g["S"]),
                          tzinfo=timezone.utc)
        except ValueError:
            continue
        out[base + m.group(0)] = kt
    return [(kt, url) for url, kt in out.items()]


def _sources(cfg, domain: str, product: str) -> List[Dict[str, Any]]:
    """Where a file is looked for, in this order: NOAA's bucket, then the domain's mirror if it
    has one. Each ``{"name", "url": time -> url, "pages": (start, end) -> listing requests,
    "files": (page, body) -> [(time, url)]}``."""
    out: List[Dict[str, Any]] = [
        {"name": "bucket", "url": lambda ts: "%s/%s" % (cfg.mrms_url, _key(domain, product, ts)),
         "pages": lambda start, end: _bucket_pages(cfg, domain, product, start, end),
         "files": lambda page, body: _bucket_files(cfg, body)}]
    pattern = mirror_pattern(cfg, domain)
    if pattern:
        out.append({"name": "mirror", "url": lambda ts: ts.strftime(pattern),
                    "pages": lambda start, end: _mirror_pages(pattern, start, end),
                    "files": lambda page, body: _mirror_files(pattern, page, body)})
    return out


def _max_lag_s(cfg) -> int:
    v = getattr(cfg, "radar_coverage_max_lag_s", 0)
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0


def _short(e: Any) -> str:
    """An error's text with its URLs cut to their hosts (the frame's time names the file)."""
    return _URL_RE.sub(r"\1", str(e))


def _bad_data() -> Tuple[type, ...]:
    """What a broken or wrong file raises while it is read: Pillow raises SyntaxError for a
    broken PNG chunk, and DecompressionBombError."""
    bomb = getattr(Image, "DecompressionBombError", None) if Image is not None else None
    return (ValueError, OSError, EOFError, zlib.error, struct.error, IndexError, ArithmeticError,
            SyntaxError) + ((bomb,) if isinstance(bomb, type) else ())


def _read(body: bytes, product: str, file_ts: datetime):
    """``(meta, image)`` of a downloaded file when it is MRMS's ``product`` file of ``file_ts``
    (``PRODUCT_IDS``, its own GRIB2 time its name's to the second) and decodes; ValueError, or
    what gzip, zlib or Pillow raise, otherwise."""
    meta = parse_grib2(gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body)
    want = PRODUCT_IDS.get(product)
    got = (meta.get("discipline"),) + tuple(meta.get("parameter") or (None, None))
    if want is not None and got != want:
        raise ValueError("not MRMS's %s (GRIB2 discipline %s, parameter %s.%s)" % ((product,) + got))
    ts = meta.get("ts")
    if ts is None:
        raise ValueError("the file's own time cannot be read")
    if abs((ts - file_ts).total_seconds()) > TIME_TOLERANCE_S:
        raise ValueError("the file of %s holds the data of %s" % (
            file_ts.strftime("%Y-%m-%d %H:%M:%SZ"), ts.strftime("%Y-%m-%d %H:%M:%SZ")))
    return meta, decode(meta)


class _Lookup:
    """One search for a domain's file: its requests (bounded, see the module docstring), the
    sources it gave up on, the tries each has left and what failed, for the error or the
    note of the result."""

    def __init__(self, cfg, domain: str, product: str):
        self.cfg, self.product = cfg, product
        self.sources = _sources(cfg, domain, product)
        self.deadline = _clock() + LOOKUP_DEADLINE_S
        self.down: Dict[str, str] = {}          # source -> why it is asked nothing more
        self.tries = {s["name"]: 0 for s in self.sources}
        self.pages: Dict[str, Any] = {}         # listing request -> its text, or False
        self.tried = set()
        self.why: List[str] = []
        self.sent = 0                           # requests that went out (not skipped)
        self.failed: Optional[Exception] = None     # why the last request came back empty-handed

    def note(self, name: str, what: Any) -> None:
        text = _short(what)
        self.why.append("%s: %s" % (name, text) if len(self.sources) > 1 else text)

    def get(self, name: str, url: str) -> Optional[bytes]:
        """``url`` from source ``name``, within the lookup's bounds; None when it failed or was
        not sent (noted; a source the run gave up on is asked nothing more)."""
        if name in self.down:
            return None
        left = self.deadline - _clock()
        if left <= 0:
            self.down[name] = "the lookup's %d s ran out" % LOOKUP_DEADLINE_S
            self.note(name, self.down[name])
            return None
        if name == "mirror":
            budget = http.budget_left()
            if budget is not None and budget < MIRROR_RESERVE_S:
                self.down[name] = "not asked, %d s left of the run's budget" % max(0, int(budget))
                self.note(name, self.down[name])
                return None
        self.failed = None
        try:
            body = http.get_bytes(url, self.cfg, timeout=max(3.0, min(REQUEST_TIMEOUT_S, left)), retries=1)
        except http.HttpSkipped as e:
            # the host failed earlier, or the budget is spent; with a cause, an attempt went out
            # first and only its retry was refused: that attempt's failure is the news
            self.failed = e
            self.down[name] = _short(e)
            if e.__cause__ is not None:
                self.sent += 1
                self.note(name, "%s, then %s" % (str(e.__cause__) or type(e.__cause__).__name__, e))
            else:
                self.note(name, e)
            return None
        except http.HttpError as e:
            self.failed = e
            self.sent += 1
            self.note(name, e)
            return None
        self.sent += 1
        return body

    def file(self, name: str, url: str, t: datetime, ref: datetime, older: bool, max_lag: int):
        """The file of time ``t`` at ``url``, read and checked, as ``_find``'s result; None
        when it could not be had or is not what it should be (noted)."""
        self.tried.add(url)
        body = self.get(name, url)
        if body is None:
            return None
        try:
            meta, im = _read(body, self.product, t)
            if older and (ref - meta["ts"]).total_seconds() > max_lag:
                raise ValueError("its data lie more than %d s before %s" % (max_lag, ref.strftime("%H:%M:%SZ")))
        except _bad_data() as e:
            self.note(name, "the file of %s: %s: %s" % (t.strftime("%H:%M:%SZ"), type(e).__name__, e))
            return None
        return {"url": url, "source": name, "ref": ref, "older": older, "meta": meta, "im": im,
                "note": "; ".join(dict.fromkeys(self.why)) or None}

    def listed(self, start: datetime, end: datetime) -> Tuple[List[Tuple[int, datetime, str, str]], bool]:
        """``([(source order, time, url, source)], complete)`` of the files the sources list
        from ``start`` to ``end``, each listing page fetched once per lookup; ``complete``:
        every page of every source answered."""
        out = []
        complete = True
        for order, src in enumerate(self.sources):
            name = src["name"]
            for born, page in src["pages"](start, end):
                if page not in self.pages:
                    body = self.get(name, page)
                    if body is None and _unborn(born, self.failed):
                        self.why.pop()          # IEM makes a day's directory with its first file
                        body = b""
                    self.pages[page] = False if body is None else body.decode("utf-8", "replace")
                if self.pages[page] is False:
                    complete = False
                    continue
                out.extend((order, t, url, name) for t, url in src["files"](page, self.pages[page])
                           if start <= t <= end)
        return out, complete

    def asking(self) -> bool:
        """Whether some source can still be asked for a file."""
        return any(s["name"] not in self.down and self.tries[s["name"]] < TRIES_PER_SOURCE for s in self.sources)


def _unborn(born: datetime, failed: Optional[Exception]) -> bool:
    """Whether a listing page that answered ``failed`` lists a period that began only minutes
    ago (or is still to come), whose directory may not exist yet: a 404 then means "no files
    yet", not a gap in the listing. Any other failure, or a 404 for an older period, is one."""
    return isinstance(failed, http.HttpError) and not isinstance(failed, http.HttpSkipped) \
        and str(failed).startswith("HTTP 404") \
        and (util.utcnow() - born).total_seconds() < UNBORN_S


def _fmt_s(s: float) -> str:
    return "%g min" % round(s / 60.0, 1)


def _find(cfg, domain: str, product: str, frame_ts: Optional[datetime], allow_older: bool = True) -> Dict[str, Any]:
    """The file of the frame's time, or the one standing in for it, read and checked:
    ``{"url", "source", "ref", "older", "meta", "im", "note"}`` (``ref``: the frame's time,
    or now when that is unknown). HttpError when there is none; "not looked up: ..." when no
    request could be sent at all. See the module docstring for the order."""
    lk = _Lookup(cfg, domain, product)
    max_lag = _max_lag_s(cfg)
    if frame_ts is not None:
        ref = frame_ts.astimezone(timezone.utc).replace(microsecond=0)
        for src in lk.sources:
            got = lk.file(src["name"], src["url"](ref), ref, ref, False, 0)
            if got:
                return got
        near = (ref - timedelta(seconds=MAX_SKEW_S), ref + timedelta(seconds=MAX_SKEW_S))
        what = "no %s file within %d min of %s" % (product, MAX_SKEW_S // 60, ref.strftime("%H:%M:%SZ"))
    else:
        ref = util.utcnow()
        near = (ref - timedelta(seconds=2 * MAX_SKEW_S), ref)
        what = "no %s file of the last %d min" % (product, 2 * MAX_SKEW_S // 60)

    def first_good(found, older: bool) -> Optional[Dict[str, Any]]:
        """The first file of ``found`` that reads, nearest to ``ref`` first (the bucket's on a
        tie), each source tried ``TRIES_PER_SOURCE`` times at most."""
        for _, order, t, url, name in sorted({(abs((t - ref).total_seconds()), o, t, u, n) for o, t, u, n in found}):
            if url in lk.tried or name in lk.down or lk.tries[name] >= TRIES_PER_SOURCE:
                continue
            sent = lk.sent
            got = lk.file(name, url, t, ref, older, max_lag)
            if got:
                return got
            if lk.sent > sent:
                lk.tries[name] += 1     # a request not sent (a host the run gave up on) costs no try
        return None

    found, complete = lk.listed(*near)
    got = first_good(found, False)
    if got:
        return got
    if max_lag > MAX_SKEW_S and frame_ts is not None and allow_older:
        # an older file only while the window around the frame's time is known to be empty
        if found:
            lk.why.append("no older file stands in for files near that time that could not be read")
        elif not complete:
            lk.why.append("no older file stands in while the listing near that time is incomplete")
        elif lk.asking():
            older, _ = lk.listed(ref - timedelta(seconds=max_lag), near[0])
            got = first_good([c for c in older if c[1] < near[0]], True)
            if got:
                return got
            what += ", nor one up to %s before it" % _fmt_s(max_lag)
    if lk.sent == 0 and lk.down:
        raise http.HttpError("not looked up: %s" % "; ".join(dict.fromkeys(lk.down.values())))
    raise http.HttpError("%s%s" % (what, " (%s)" % "; ".join(dict.fromkeys(lk.why)) if lk.why else ""))


def coverage(cfg, domain: str, frame_ts: Optional[datetime], sites: Iterable[dict],
             radius_km: float, allow_older: bool = True) -> Dict[str, Any]:
    """The radar coverage of the ring of ``radius_km`` around each site (``slug``, ``lat``,
    ``lon``) in MRMS's file of ``domain`` of ``frame_ts``, or the one standing in for it;
    ``allow_older`` False for a reused copy of the frame. See the module docstring."""
    res: Dict[str, Any] = {"ok": False, "error": None, "domain": domain, "ts": None, "url": None,
                           "source": None, "older": False, "behind_s": None, "note": None, "sites": {}}
    product = PRODUCTS.get(domain)
    if product is None:
        res["error"] = "no MRMS product for the domain %r" % (domain,)
        return res
    try:
        got = _find(cfg, domain, product, frame_ts, allow_older)
        meta, im = got["meta"], got["im"]
        res.update(url=got["url"], source=got["source"], ts=meta["ts"], older=bool(got["older"]),
                   behind_s=int(round((got["ref"] - meta["ts"]).total_seconds())), note=got["note"])
        for s in sites:
            stats = ring_stats(meta, im, float(s["lat"]), float(s["lon"]), float(radius_km))
            res["sites"][str(s.get("slug"))] = stats
        res["ok"] = True
        how = ""
        if res["older"]:
            how = " (an older file: none near the frame's time %s; %s)" % (
                got["ref"].strftime("%H:%M:%SZ"), res["note"] or "")
        elif res["source"] != "bucket":
            how = " (from the %s: %s)" % (res["source"], res["note"] or "the bucket's failed")
        (log.warning if how else log.info)(
            "radar coverage from MRMS %s %s%s: %s", domain, res["ts"].strftime("%H:%M:%SZ"), how,
            ", ".join("%s %s %%" % (k, v["covered_pct"]) for k, v in sorted(res["sites"].items())) or "no site")
    except http.HttpError as e:
        res["error"] = str(e)
    except _bad_data() as e:
        res["error"] = "%s: %s" % (type(e).__name__, e)
    if res["error"]:
        res.update(sites={}, older=False, behind_s=None, note=None)
        log.warning("radar coverage from MRMS %s unavailable: %s", domain, res["error"])
    return res


def site_coverage(cov: Optional[Dict[str, Any]], slug: str) -> Dict[str, Any]:
    """One site's part of a ``coverage`` result, for its radar check: ``{"ok", "error",
    "ts", "cells", "uncovered", "covered_pct", "older", "behind_s", "source"}``."""
    if not isinstance(cov, dict):
        return {"ok": False, "error": "radar coverage not read", "ts": None}
    s = (cov.get("sites") or {}).get(slug)
    if not cov.get("ok") or not isinstance(s, dict):
        return {"ok": False, "error": cov.get("error") or "no coverage for %s" % slug, "ts": cov.get("ts")}
    if s.get("covered_pct") is None:
        return {"ok": False, "error": "the ring holds no grid cell", "ts": cov.get("ts")}
    return dict(s, ok=True, error=None, ts=cov.get("ts"), older=bool(cov.get("older")),
                behind_s=cov.get("behind_s"), source=cov.get("source"))
