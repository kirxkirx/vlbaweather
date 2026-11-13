"""The wrong-clock guard: this host's clock against the clocks of the data sources.

Every time-keyed source is asked for and judged by this host's clock: the IEM frame name
and the MRMS file of "now minus 4 min", the GOES window ending "now", an alert's onset and
expiry, the age of a station report or a forecast hour. A clock two hours slow (the first
run after a boot before NTP has stepped it, an RTC kept in local time, a resumed VM) reads
two-hour-old radar and lightning as current and turns a warning in effect into "scheduled".

Every HTTP reply carries its server's time (the ``Date`` header, plus ``Age`` when a cache
answered); ``weather.http`` notes it per host for the replies of a run
(``http.clock_offsets()``). ``assess`` compares them with this host's clock once per run;
when they disagree, every check that answered with something other than BAD is made
"no data" (``vlba/decision.py``), since none of them can be trusted. The same guard as
safety/servertime.py of the sister project ttustatus.
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, Optional

from weather import util

# How far this host's clock may stand from the servers' before no check is trusted. A synced
# host is off by milliseconds and the Date header has a 1-s resolution (the live hosts agreed
# with a synced clock to 0-2 s); the failures this guards against are off by tens of minutes
# to hours. 300 s is a quarter of the age a radar frame may have (VLBA_RADAR_STALE, 20 min)
# and a sixth of the GLM window (VLBA_GLM_WINDOW_MIN, 30 min), so a clock off by less shifts
# them by little, while transit and the Date header's whole seconds can never trip it.
LIMIT_S = 300.0


def _finite(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def assess(offsets: Any, limit_s: float = LIMIT_S) -> Dict[str, Any]:
    """``{"ok": True | False | None, "skew_s": float | None, "hosts": n}`` from ``offsets``
    (host -> server time minus this host's time, s; ``http.clock_offsets()``).

    ``skew_s`` is the median over the hosts (positive: this host's clock is behind). ``ok``
    is False when the median is beyond ``limit_s`` and more than half of the hosts are each
    beyond it on the same side, True when the median is within it, and None (no opinion)
    when no host gave a usable Date or the servers disagree among themselves (one server
    with a wrong clock cannot condemn the page)."""
    vals = []
    if isinstance(offsets, dict):
        vals = [f for f in (_finite(v) for v in offsets.values()) if f is not None]
    if not vals:
        return {"ok": None, "skew_s": None, "hosts": 0}
    skew = statistics.median(vals)
    if abs(skew) > limit_s:
        same = sum(1 for v in vals if abs(v) > limit_s and (v > 0) == (skew > 0))
        ok = False if same * 2 > len(vals) else None     # the servers disagree: no opinion
    else:
        ok = True
    return {"ok": ok, "skew_s": round(skew, 1), "hosts": len(vals)}


def _skew(res: Any) -> Optional[float]:
    return _finite(res.get("skew_s")) if isinstance(res, dict) else None


def off_by(res: Any) -> str:
    """'host clock off by 2 h 05 min' (the detail of a check the guard made unknown)."""
    skew = _skew(res)
    return "host clock off by %s" % (util.fmt_age(abs(skew)) if skew is not None else "an unknown amount")


def error_text(res: Any) -> str:
    """The run error when the guard finds the host clock wrong."""
    skew = _skew(res) or 0.0
    return ("the host clock is %s %s the clocks of the data sources: no check can be trusted"
            % (util.fmt_age(abs(skew)), "behind" if skew > 0 else "ahead of"))


def is_off(res: Any) -> bool:
    """True only when the guard has found the clock wrong (None, a missing or odd result:
    no opinion, nothing changes)."""
    return isinstance(res, dict) and res.get("ok") is False
