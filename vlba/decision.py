"""The GOOD / BAD verdict for one antenna: the weather cuts of ttustatus (the TTU Alpaca
SafetyMonitor), applied to what a site without local sensors can know.

Inputs are the site entry built by ``make_vlba_page`` (DESIGN: PLAN.md section 5) and the
persisted per-site state (holds and the radar streak). ``evaluate`` is pure apart from the
state dict it is handed, which it updates in place; nothing here fetches anything.

The checks (PLAN.md section 4; every threshold is a ``VlbaConfig`` field):

* ``warning``: an NWS product of ``cfg.veto_event_names`` AT the site (the parent's
  classification: a polygon product contains the site, a zone product lists one of its
  zones), from ``cfg.veto_onset_lead_s`` before its onset to its end. Earlier than that it
  is "scheduled" (shown, not bad), as in ttustatus.
* ``radar``: an echo of at least ``cfg.radar_dbz`` within ``cfg.radar_km`` in
  ``cfg.radar_trigger_after`` consecutive radar frames (one frame is an observation, two
  are weather: MRMS composites carry one-frame artefacts), held ``cfg.radar_hold_s`` after
  the last confirmed hit. Where the newest frame shows an echo in the ring, the run also
  has the frames just before it (``radar_check["earlier"]``), counted first, so one run
  confirms an echo by itself: runs 30 min apart are too far apart to be consecutive.
  Each frame counts once. A frame that is missing, older than ``cfg.radar_stale_s``,
  already counted by an earlier run, or reused after a failed fetch is no new data: it
  neither clears nor extends the streak.
* ``glm``: a GLM flash within ``cfg.glm_km`` in the fetch window, held ``cfg.glm_hold_s``;
  a flash counts even when a satellite's data is incomplete, while "clear" needs one
  covering satellite's complete window.
* ``forecast_cloud`` / ``forecast_precip`` / ``forecast_thunder``: this hour or next above
  the ttustatus thresholds; a field the NWS office does not produce (Honolulu and San Juan
  have no thunder probability) is "not provided", which is neither bad nor missing; a
  malformed one is unknown. A forecast whose
  update is older than ``cfg.nws_stale_min`` (12 h by default: offices update a few times a
  day) or that has no row for the current hour is no data.
* ``forecast_winter``: snowfall or ice accumulation above zero this hour or next, or a
  weather entry of snow / sleet / freezing precipitation with coverage likely or definite
  ("include snowstorms in bad").
* ``obs_precip`` / ``obs_humidity``: the report of the nearest station that is usable for
  the antenna (``entry["obs_station"]["usable"]``: within ``cfg.obs_max_km`` and
  ``cfg.obs_max_delev_m``) and fresh; the precipitation check reads the report with weather
  ``make_vlba_page._precip_report`` chose (``obs_raw["report_*"]``: an older one, or a
  newer one of the next qualifying station, when the latest says nothing). Where no
  station of NWS's list qualifies at all (``qualifying`` empty: Mauna Kea, Pie Town) the
  checks are "not provided", like a field an office does not produce; a qualifying
  station without a recent report is unknown.
* ``wind`` (off when ``cfg.wind_mph`` is 0): observed wind or gust, or the forecast wind or
  gust this hour or next, at or above the threshold.

Fail-safe in the ttustatus sense means "unknown is never clear": a check without usable
data is reported as ``unknown`` and the verdict is marked ``incomplete``; it never counts as
good. The verdict is BAD when any check is bad, GOOD otherwise.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from weather import util

log = logging.getLogger("vlba.decision")

GOOD, BAD, NO_DATA = "GOOD", "BAD", "NO DATA"      # NO DATA: no check is bad, none had data
# Check states, in the order the page lists them: a bad check first.
BAD_STATE, WATCH, OK, UNKNOWN, OFF, SCHEDULED, NOT_PROVIDED = (
    "bad", "watch", "ok", "unknown", "off", "scheduled", "not provided")

CHECK_LABELS = (
    ("warning", "NWS warning over the site"),
    ("radar", "Rain on radar"),
    ("glm", "Lightning (GOES GLM)"),
    ("forecast_precip", "Precipitation probability (forecast)"),
    ("forecast_thunder", "Thunder probability (forecast)"),
    ("forecast_cloud", "Cloud cover (forecast)"),
    ("forecast_winter", "Snow or ice (forecast)"),
    ("obs_precip", "Precipitation observed"),
    ("obs_humidity", "Humidity observed"),
    ("wind", "Wind"),
)
CHECK_KEYS = tuple(k for k, _ in CHECK_LABELS)
# The order of the verdict's reasons: what is happening at the site now first, the
# forecast after it (the summary at the top of the page shows the first few).
REASON_ORDER = ("warning", "radar", "glm", "obs_precip", "wind", "obs_humidity",
                "forecast_precip", "forecast_thunder", "forecast_winter", "forecast_cloud")

# Observation text / present-weather words that mean precipitation is falling now (freezing
# rain and drizzle by their rain / drizzle; fog, mist, haze, squalls are no precipitation).
_PRECIP_WORDS = re.compile(
    r"\b(rain|showers?|drizzle|snow|sleet|hail|graupel|thunderstorms?|ice pellets|ice crystals|"
    r"snow grains|snow pellets|precip|precipitation)\b", re.IGNORECASE)
# ... but not these: precipitation near the station, not at it; snow moved along the ground.
_NOT_PRECIP = re.compile(r"\b(no precipitation|in the vicinity|vicinity|recent|low drifting)\b", re.IGNORECASE)
_TEXT_PARTS = re.compile(r",|;|\band\b|\bwith\b", re.IGNORECASE)
# A METAR present-weather group with precipitation (or a thunderstorm) at the station: an
# intensity, a descriptor (not DR, snow drifting along the ground), the precipitation types
# (DZ RA SN SG IC PL GR GS UP), or TS alone. VC (in the vicinity) is no precipitation here.
_METAR_PRECIP = re.compile(r"^[-+]?(?:MI|PR|BC|BL|SH|TS|FZ)?(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP)+$|^[-+]?TS$")
# NWS gridpoint "weather" types that are frozen precipitation.
_WINTER_TYPES = ("snow", "sleet", "freezing_rain", "freezing_drizzle", "freezing_fog",
                 "freezing_spray", "ice_pellets", "ice_crystals", "snow_showers", "blowing_snow",
                 "snow_grains", "ice_fog")
_WINTER_COVERAGE = ("likely", "definite", "occasional", "frequent", "periods", "areas",
                    "intermittent", "numerous", "widespread")


# ---- the persisted state --------------------------------------------------------------
def load_state(path: str) -> dict:
    """``{"sites": {slug: {...}}, "saved_ts": float|None}``; an unreadable or malformed file
    is an empty state (logged): a lost hold is the lesser evil next to a crashed run, and
    the holds are at most 30 min long."""
    empty: Dict[str, Any] = {"sites": {}, "saved_ts": None}
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return empty
    except (OSError, ValueError) as e:
        log.warning("state file %s unreadable (%s); starting from an empty state", path, e)
        return empty
    if not isinstance(doc, dict) or not isinstance(doc.get("sites"), dict):
        log.warning("state file %s has an unexpected shape; starting from an empty state", path)
        return empty
    doc["sites"] = {k: v for k, v in doc["sites"].items() if isinstance(v, dict)}
    return doc


def save_state(path: str, state: dict, now: Optional[datetime] = None) -> Optional[str]:
    """Atomic write (tmp + fsync + rename). None on success, else what went wrong: a failure
    is logged and returned, never raised (the page matters more than the holds), and the
    caller reports it, because without the file the radar confirmation and the holds do not
    carry over from one run to the next."""
    now = now or util.utcnow()
    state = dict(state, saved_ts=now.timestamp())
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1, sort_keys=True, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        log.warning("could not write the state file %s: %s", path, e)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return str(e) or e.__class__.__name__
    return None


def site_state(state: dict, slug: str) -> dict:
    """The per-site dict of ``state`` (created when absent)."""
    return state.setdefault("sites", {}).setdefault(slug, {})


def _ts(v: Any) -> Optional[float]:
    """A persisted timestamp: a finite number, else None (a hand-edited or corrupt file
    must never produce an eternal hold)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _clamp_hold(last_ts: Optional[float], now_ts: float, hold_s: float) -> Optional[float]:
    """A hold stamp from the future (a clock step, a hand-edited file) reads as "now":
    the hold then lasts one full period at most, never for ever."""
    if last_ts is None:
        return None
    if last_ts > now_ts + 60:
        log.warning("hold stamp %.0f s in the future (clock step?); clamping", last_ts - now_ts)
        return now_ts
    return last_ts


# ---- small helpers ---------------------------------------------------------------------
def _is_dict(x: Any) -> bool:
    return isinstance(x, dict)


def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _as_dt(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return util.parse_iso(v) if isinstance(v, str) else None


def _check(key: str, state: str, value: str = "", limit: str = "", detail: str = "",
           stale: bool = False) -> dict:
    label = dict(CHECK_LABELS).get(key, key)
    return {"key": key, "label": label, "state": state, "value": value, "limit": limit,
            "detail": detail, "stale": bool(stale)}


def _fmt_min(seconds: float) -> str:
    return util.fmt_age(seconds)


# ---- the checks ------------------------------------------------------------------------
def check_warning(cfg, entry: dict, now: datetime) -> Tuple[dict, List[str], List[str]]:
    """(check, reasons, scheduled texts). An alert AT the site whose event is a veto event.
    Onset handling as in ttustatus: from ``veto_onset_lead_s`` before the onset (a High
    Wind Warning for tomorrow is issued the afternoon before); a product without an onset
    counts at once."""
    names = {n.lower() for n in cfg.veto_event_names}
    if not names:
        return _check("warning", OFF, detail="no warning products configured"), [], []
    limit = ("%s in effect" % cfg.veto_event_names[0] if len(names) == 1
             else "one of %d NWS warnings in effect" % len(names))
    feed = entry.get("alerts_feed") if _is_dict(entry.get("alerts_feed")) else None
    at = [a for a in (entry.get("alerts_at") or []) if _is_dict(a)]
    tz = entry.get("tz") or "UTC"
    lead = timedelta(seconds=max(0, int(cfg.veto_onset_lead_s)))
    active: List[str] = []
    scheduled: List[str] = []
    for a in at:
        event = str(a.get("event") or "").strip()
        if event.lower() not in names:
            continue
        onset = _as_dt(a.get("onset")) or _as_dt(a.get("effective"))
        end = _as_dt(a.get("end_dt")) or _as_dt(a.get("ends")) or _as_dt(a.get("expires"))
        office = str(a.get("sender") or "").strip()
        until = util.fmt_local(end, tz, "%a %H:%M %Z") if end else "no end time given"
        text = "NWS %s in effect for the site until %s%s" % (
            event, until, " (%s)" % office if office else "")
        if onset is not None and onset - lead > now:
            scheduled.append("NWS %s scheduled from %s%s" % (
                event, util.fmt_local(onset, tz, "%a %H:%M %Z"), " (%s)" % office if office else ""))
            continue
        active.append(text)
    if active:
        return (_check("warning", BAD_STATE, value="%d in effect" % len(active), limit=limit,
                       detail="; ".join(active)), active, scheduled)
    if feed is not None and not feed.get("ok"):
        return (_check("warning", UNKNOWN, limit=limit, detail="NWS alert feed unavailable: %s"
                       % (feed.get("error") or "no data")), [], scheduled)
    meta = entry.get("meta")
    if _is_dict(meta) and not meta.get("ok") and not meta.get("not_provided"):
        # without the site's forecast / county / fire zones a zone-based warning (most
        # winter and wind products) cannot be matched: "none in effect" would be a guess
        return (_check("warning", UNKNOWN, limit=limit,
                       detail="the site's NWS zones are unknown (point metadata unavailable): zone-based "
                              "warnings cannot be matched"), [], scheduled)
    detail = "none of the %d warning products is in effect over the site" % len(names)
    if scheduled:
        detail += "; scheduled: " + "; ".join(scheduled)
    return (_check("warning", SCHEDULED if scheduled else OK, value="none", limit=limit,
                   detail=detail, stale=bool(feed and feed.get("stale"))), [], scheduled)


def check_radar(cfg, entry: dict, st: dict, now: datetime) -> Tuple[dict, List[str]]:
    """The ring check with the ttustatus confirmation and hold, on ``entry["radar_check"]``
    (``available``, ``stale``, ``frame_ts``, ``in_ring``, ``nearest_km``, ``count``,
    ``max_dbz``, ``source``, ``error``, ``digest``). State keys: ``radar_streak``,
    ``radar_last_hit_ts``, ``radar_last_frame_ts``, ``radar_last_digest``.

    The streak counts radar SCANS, each once: a frame whose ring holds the same data as the
    frame counted last (``digest``, ``radarcheck.ring_digest``: the composites are written
    every 2 min, a radar completes a scan every 4.5-10 min) is no new observation, like a
    frame an earlier run already counted (two
    runs inside one radar update) and a copy reused after a failed fetch (``stale``) are no
    new evidence and neither extend nor reset it, as in ttustatus, where a failed fetch
    never counts. Otherwise one echo frame, seen fresh by one run and reused by the next,
    would confirm itself. A frame without a time stamp counts as new (its age is that of
    the copy it came from, ``age_s``); a frame more than ``cfg.radar_stale_s`` newer than the
    last one counted, or counted more than that long after it (``radar_last_count_ts``, for
    frames without a time and for a state file without either stamp), starts a new streak.
    A usable frame with an echo in the ring is BAD while the streak is complete and speaks
    for that frame: a new frame, or the very frame last counted (one without a time: counted
    within ``cfg.radar_stale_s``); a reused copy of a frame never counted confirms nothing
    (ttustatus: in the ring, fresh and confirmed).

    No echo is "clear" only where radar covers the ring: the frames write "no coverage" like
    "no echo", so ``coverage`` (MRMS's own file, ``vlba.mrms.site_coverage``) must say radar
    covers at least ``cfg.radar_min_coverage`` % of it; less, or coverage that could not be
    read, is "no data". ``{"off": True}`` (VLBA_RADAR_COVERAGE=0) and a check without the key
    keep the frame's "clear". A new frame without an echo restarts the count whatever its
    coverage: it is real data where radar covers the ring, and holding the count over frames
    of unknown coverage would let two echo frames far apart confirm each other."""
    rc = entry.get("radar_check") if _is_dict(entry.get("radar_check")) else {}
    now_ts = now.timestamp()
    hold_s = float(cfg.radar_hold_s)
    last = _clamp_hold(_ts(st.get("radar_last_hit_ts")), now_ts, hold_s)
    st["radar_last_hit_ts"] = last
    streak = st.get("radar_streak")
    streak = int(streak) if isinstance(streak, int) and not isinstance(streak, bool) and streak >= 0 else 0
    last_frame = _ts(st.get("radar_last_frame_ts"))
    if last_frame is not None and last_frame > now_ts + 600:
        last_frame = None           # a frame "from the future" (clock step, hand edit) blocks nothing
    last_count = _ts(st.get("radar_last_count_ts"))
    if last_count is not None and last_count > now_ts + 60:
        last_count = None
    limit = "%g dBZ within %g km" % (cfg.radar_dbz, cfg.radar_km)
    src = str(rc.get("source") or "radar")
    if rc.get("off"):               # switched off on purpose (WEATHER_RADAR=0, VLBA_ISLAND_RADAR=0)
        return _check("radar", OFF, limit=limit, detail=str(rc.get("error") or "radar switched off")), []
    frame_ts = _as_dt(rc.get("frame_ts"))
    # a frame of unknown time is at least as old as the copy it came from (age_s)
    age_s = (now - frame_ts).total_seconds() if frame_ts else _num(rc.get("age_s"))
    usable = bool(rc.get("available")) and (age_s is None or age_s <= float(cfg.radar_stale_s))
    stale = bool(rc.get("stale"))
    frame_key = frame_ts.timestamp() if frame_ts else None
    digest = rc.get("digest") if isinstance(rc.get("digest"), str) else None
    last_digest = st.get("radar_last_digest") if isinstance(st.get("radar_last_digest"), str) else None
    same_scan = digest is not None and digest == last_digest
    # a repeat of the scan counted last speaks for the stored streak only while that count is
    # recent: a source serving unchanged data must not revive a streak from hours ago
    same_recent = same_scan and last_count is not None and now_ts - last_count <= float(cfg.radar_stale_s)
    new_frame = usable and not stale and not same_scan and \
        (frame_key is None or last_frame is None or frame_key > last_frame)
    confirmed_now = False
    if new_frame:
        if frame_key is not None and last_frame is not None:
            gap = frame_key - last_frame
        else:
            gap = now_ts - last_count if last_count is not None else None
        if streak and (gap is None or gap > float(cfg.radar_stale_s)):
            streak = 0              # frames either side of an outage are not consecutive
        st["radar_last_count_ts"] = now_ts
        if rc.get("in_ring"):
            streak += 1
            if streak >= int(cfg.radar_trigger_after):
                confirmed_now = True
                last = now_ts
        else:
            streak = 0
        if frame_key is not None:
            st["radar_last_frame_ts"] = frame_key
        st["radar_last_digest"] = digest
    st["radar_streak"] = streak
    st["radar_last_hit_ts"] = last
    held = last is not None and (now_ts - last) < hold_s
    where = ""
    if rc.get("nearest_km") is not None:
        where = "nearest echo %s km" % _num_txt(rc.get("nearest_km"))
    if rc.get("max_dbz") is not None:
        where += (", " if where else "") + "up to %s dBZ" % _num_txt(rc.get("max_dbz"))
    frame_txt = ("frame %s" % frame_ts.astimezone(timezone.utc).strftime("%H:%MZ")) if frame_ts else ""
    reasons: List[str] = []
    # the stored streak speaks for this frame only if it is new, or is the frame last counted
    # (a frame without a time: counted recently); a reused copy of a frame never counted, or a
    # state file from before an outage, confirms nothing
    trusted = new_frame or same_recent or (frame_key is not None and frame_key == last_frame) or \
        (frame_key is None and last_count is not None and now_ts - last_count <= float(cfg.radar_stale_s))
    confirmed = usable and rc.get("in_ring") and trusted and streak >= int(cfg.radar_trigger_after)
    if confirmed_now or confirmed or (usable and rc.get("in_ring") and held):
        detail = "%s: echo within %g km on %d consecutive frames (%s)%s" % (
            src, cfg.radar_km, streak, where or "no distance", "; " + frame_txt if frame_txt else "")
        reasons.append("rain on radar within %g km%s" % (
            cfg.radar_km, " (%s)" % where if where else ""))
        return _check("radar", BAD_STATE, value="echo in the ring", limit=limit, detail=detail, stale=stale), reasons
    if held:
        left = hold_s - (now_ts - last)
        detail = "%s: no echo in the ring now; held %s after the last confirmed echo (%s left)" % (
            src, _fmt_min(hold_s), _fmt_min(left))
        if not usable:
            detail = "%s: no usable frame now; held %s after the last confirmed echo (%s left)" % (
                src, _fmt_min(hold_s), _fmt_min(left))
        reasons.append("recent rain on radar within %g km (hold, %s left)" % (cfg.radar_km, _fmt_min(left)))
        return _check("radar", BAD_STATE, value="recent echo", limit=limit, detail=detail, stale=stale), reasons
    if not usable:
        why = rc.get("error") or ("frame %s old" % _fmt_min(age_s) if age_s is not None else "no frame")
        return _check("radar", UNKNOWN, limit=limit, detail="%s: %s" % (src, why), stale=stale), []
    if rc.get("in_ring"):
        if new_frame:
            count_txt = "frame %d of %d" % (streak, int(cfg.radar_trigger_after))
        else:
            count_txt = "%s, not counted again; %d of %d frames so far" % (
                "a copy reused after a failed fetch" if stale else
                "the same radar data as the frame counted last" if same_scan else "the frame the last run saw",
                streak, int(cfg.radar_trigger_after))
        detail = "%s: echo within %g km, unconfirmed (%s)%s%s" % (
            src, cfg.radar_km, count_txt, "; " + where if where else "", "; " + frame_txt if frame_txt else "")
        c = _check("radar", WATCH, value="unconfirmed echo", limit=limit, detail=detail, stale=stale)
        c["note"] = "unconfirmed echo on radar within %g km%s" % (
            cfg.radar_km, " (nearest %s km)" % _num_txt(rc.get("nearest_km")) if rc.get("nearest_km") is not None else "")
        return c, []
    detail = "%s: no echo of %g dBZ or more within %g km%s" % (
        src, cfg.radar_dbz, cfg.radar_km, "; " + frame_txt if frame_txt else "")
    cov = rc.get("coverage") if _is_dict(rc.get("coverage")) else None
    if cov is not None and cov.get("off"):
        detail += "; radar coverage not checked (VLBA_RADAR_COVERAGE=0)"
    elif cov is not None:
        pct = _num(cov.get("covered_pct"))
        cov_ts = _as_dt(cov.get("ts"))
        cov_txt = " (MRMS %s)" % cov_ts.astimezone(timezone.utc).strftime("%H:%MZ") if cov_ts else ""
        if not cov.get("ok") or pct is None:
            return _check("radar", UNKNOWN, limit=limit, stale=stale, detail=(
                "%s, but the radar coverage of the ring is unknown (%s): a radar outage would look "
                "the same" % (detail, cov.get("error") or "no data"))), []
        if pct < float(cfg.radar_min_coverage):
            why = "%s: no radar covers %s %% of the %g km ring%s, so no echo there would show" % (
                src, _num_txt(100.0 - pct), cfg.radar_km, cov_txt)
            why += " (clear needs %g %% covered)%s" % (cfg.radar_min_coverage, "; " + frame_txt if frame_txt else "")
            return _check("radar", UNKNOWN, limit=limit, stale=stale, detail=why), []
        if pct < 100.0:
            detail += "; radar covers %s %% of the ring%s" % (_num_txt(pct), cov_txt)
    return _check("radar", OK, value="clear", limit=limit, detail=detail, stale=stale), []


def check_glm(cfg, entry: dict, st: dict, now: datetime) -> Tuple[dict, List[str]]:
    """Lightning: a flash within ``glm_km`` in the fetch window, held ``glm_hold_s`` from the
    flash (a flash of the window already older than that is no hit).
    State keys: ``glm_last_hit_ts``, ``glm_last_hit`` (nearest_km, bearing)."""
    g = entry.get("glm") if _is_dict(entry.get("glm")) else {}
    now_ts = now.timestamp()
    hold_s = float(cfg.glm_hold_s)
    last = _clamp_hold(_ts(st.get("glm_last_hit_ts")), now_ts, hold_s)
    limit = "a flash within %g km" % cfg.glm_km
    if not cfg.check_enabled("glm"):
        st["glm_last_hit_ts"] = last
        return _check("glm", OFF, limit=limit, detail="GLM layer switched off"), []
    if not g.get("covered", True):
        st["glm_last_hit_ts"] = last
        return _check("glm", OFF, limit=limit, detail="outside the GOES GLM field of view"), []
    available = bool(g.get("available"))
    count = _num(g.get("count_window")) or 0.0
    hit = {}
    past_hold = None                # (count, age) of flashes in the window but past the hold
    if count > 0:                   # a flash in a partial window is still a flash
        # the hold runs from the flash, not from this run: a window that reached back (a late
        # run, a catch-up) must not stretch it, and a flash already past the hold is no hit
        t_flash = g.get("last_in_ring_t")
        t_flash = min(now_ts, t_flash.timestamp()) if isinstance(t_flash, datetime) else now_ts
        if now_ts - t_flash < hold_s:
            last = max(last or 0.0, t_flash)
            hit = {"nearest_km": g.get("nearest_km"), "nearest_bearing": g.get("nearest_bearing"),
                   "count": int(count), "at": now_ts}
            st["glm_last_hit"] = hit
        else:
            past_hold = (int(count), now_ts - t_flash)
    st["glm_last_hit_ts"] = last
    # bad from a flash until VLBA_GLM_HOLD after the last one
    held = bool(hit) or (last is not None and (now_ts - last) < hold_s)
    reasons: List[str] = []
    if held:
        prev = st.get("glm_last_hit") if _is_dict(st.get("glm_last_hit")) else {}
        near = prev.get("nearest_km")
        bearing = prev.get("nearest_bearing")
        ago = now_ts - last
        where = ""
        if near is not None:
            where = "nearest %s km %s" % (_num_txt(near), bearing or "")
        if hit:
            detail = "%d flash(es) within %g km in the last %s min%s" % (
                int(count), cfg.glm_km, _num_txt(g.get("window_min")), "; " + where if where else "")
            reasons.append("lightning within %g km%s" % (cfg.glm_km, " (%s)" % where.strip() if where else ""))
        else:
            # what this run's window says: flashes past the hold, nothing (when the window does
            # not reach the last flash), or nothing worth saying (the last flash lies inside the
            # window but this run's data missed it, e.g. a refetch that failed)
            window_s = (_num(g.get("window_min")) or 0.0) * 60.0
            if past_hold:
                opening = "%d flash(es) within %g km, the newest %s ago: past the %s hold; " % (
                    past_hold[0], cfg.glm_km, _fmt_min(past_hold[1]), _fmt_min(hold_s))
            elif not window_s or ago >= window_s:
                opening = "no flash within %g km in the last %s min; " % (cfg.glm_km, _num_txt(g.get("window_min")))
            else:
                opening = ""
            detail = "%sheld %s after the last one (%s ago%s)" % (
                opening, _fmt_min(hold_s), _fmt_min(ago), "; " + where if where else "")
            reasons.append("lightning within %g km %s ago (hold, %s left)" % (
                cfg.glm_km, _fmt_min(ago), _fmt_min(hold_s - ago)))
        return _check("glm", BAD_STATE, value="%d flash(es)" % int(count) if hit else "recent",
                      limit=limit, detail=detail, stale=False), reasons
    if not available:
        why = g.get("error") or "no GLM data this run"
        return _check("glm", UNKNOWN, limit=limit, detail=str(why)), []
    near = g.get("nearest_km")
    detail = "no flash within %g km in the last %s min" % (cfg.glm_km, _num_txt(g.get("window_min")))
    if past_hold:
        detail = "%d flash(es) within %g km, the newest %s ago: past the %s hold" % (
            past_hold[0], cfg.glm_km, _fmt_min(past_hold[1]), _fmt_min(hold_s))
    if near is not None:
        detail += "; nearest flash %s km %s" % (_num_txt(near), g.get("nearest_bearing") or "")
    sats = g.get("sats") or []
    if sats:
        detail += " (%s)" % ", ".join(str(s) for s in sats)
    if g.get("partial"):
        detail += "; partial: %s" % (g.get("error") or "a satellite's data is missing this run")
    return _check("glm", OK, value="none", limit=limit, detail=detail.strip()), []


def forecast_age_text(entry: dict) -> str:
    """"; NWS update 5 h 23 min ago" for the check details (empty when unknown)."""
    gp = entry.get("gridpoint") if _is_dict(entry.get("gridpoint")) else {}
    age = _num(gp.get("update_age_min"))
    if age is None or age < 0:
        return ""
    return "; NWS update %s ago" % util.fmt_duration_minutes(age)


# The gridpoint series each verdict check reads (temperature, humidity and the amount of
# rain are only shown): an unreadable one of a check that is on is a gap in the verdict.
VERDICT_SERIES = {"forecast_cloud": ("sky_cover_pct",), "forecast_precip": ("pop_pct",),
                  "forecast_thunder": ("thunder_pct",), "forecast_winter": ("snow_mm", "ice_mm", "weather"),
                  "wind": ("wind_kmh", "gust_kmh")}


def forecast_unreadable(cfg, gp: Any) -> List[str]:
    """The series that the checks switched on read and that could not be read this run."""
    gp = gp if _is_dict(gp) else {}
    used = {f for key, fields in VERDICT_SERIES.items() if cfg.check_enabled(key) for f in fields}
    return [str(x) for x in (gp.get("unusable") or []) if x in used]


def forecast_fitness(cfg, gp: Any) -> Optional[str]:
    """None when a gridpoint result can feed the forecast checks (a last good copy can),
    else the short, stable reason it cannot: "unavailable", "NWS update too old" (older
    than ``cfg.nws_stale_min``), "update time unknown or in the future" (more than 2 h
    ahead). The page and status.json use it too, so they flag exactly what the verdict
    rejects. A grid NWS does not provide is no problem (None)."""
    gp = gp if _is_dict(gp) else {}
    if gp.get("not_provided"):
        return None
    if not gp.get("ok"):
        return "unavailable"
    age = _num(gp.get("update_age_min"))
    if age is None or age < -120:
        return "update time unknown or in the future"
    if age > float(cfg.nws_stale_min):
        return "NWS update too old"
    return None


def _forecast_rows(cfg, entry: dict, now: datetime) -> Tuple[Optional[dict], Optional[dict], Optional[str], bool,
                                                             list, list]:
    """(this hour, next hour, why unusable, stale, missing fields, unusable fields) from
    ``entry["gridpoint"]``; ``stale`` means an old copy is in use (never for no copy)."""
    gp = entry.get("gridpoint") if _is_dict(entry.get("gridpoint")) else {}
    missing = [str(m) for m in (gp.get("missing") or [])]
    unusable = [str(m) for m in (gp.get("unusable") or [])]
    if gp.get("not_provided"):
        return None, None, "not provided by NWS for this location", False, missing, unusable
    if not gp.get("ok"):
        return None, None, "forecast unavailable: %s" % (gp.get("error") or "no data"), False, missing, unusable
    if forecast_fitness(cfg, gp):
        age = _num(gp.get("update_age_min"))
        why = ("NWS last updated this forecast %s ago (limit %s)"
               % (util.fmt_duration_minutes(age), util.fmt_duration_minutes(cfg.nws_stale_min))
               if age is not None and age >= 0 else "forecast time unknown or in the future")
        return None, None, why, True, missing, unusable
    rows = [r for r in (gp.get("hours") or []) if _is_dict(r)]
    this_h = gp.get("now") if _is_dict(gp.get("now")) else (rows[0] if rows else None)
    next_h = gp.get("next") if _is_dict(gp.get("next")) else (rows[1] if len(rows) > 1 else None)
    if this_h is None:
        return None, None, "forecast has no hour for now", False, missing, unusable
    return this_h, next_h, None, bool(gp.get("stale")), missing, unusable


def _merge_hours(items: List[Tuple[str, Optional[str]]]) -> List[str]:
    """``[(text, "this hour" | "next hour" | None)]`` -> one reason per text, the hours
    joined: "likely snow forecast (this hour and next)" instead of two reasons."""
    order: List[str] = []
    hours: Dict[str, List[str]] = {}
    for text, label in items:
        if text not in hours:
            order.append(text)
            hours[text] = []
        if label and label not in hours[text]:
            hours[text].append(label)
    out = []
    for text in order:
        h = hours[text]
        out.append(text if not h else "%s (%s)" % (text, "this hour and next" if len(h) > 1 else h[0]))
    return out


def _pct_check(key: str, field: str, limit: float, rows: List[Tuple[str, dict]], stale: bool,
               missing: List[str], age_txt: str = "", unusable: Tuple[str, ...] = ()) -> Tuple[dict, List[str]]:
    what = dict(CHECK_LABELS)[key].split(" (")[0].lower()
    limit_txt = "> %g %%" % limit
    worst = None
    over: List[Tuple[float, str]] = []
    for label, row in rows:
        v = _num(row.get(field))
        if v is None:
            continue
        if worst is None or v > worst[0]:
            worst = (v, label)
        if v > limit:
            over.append((v, label))
    if over:
        # one reason for the check, both hours in it: the summary has room for few
        if len(over) == 1 or over[0][0] == over[1][0]:
            reason = "%s %g %% > %g %% (%s)" % (what, over[0][0], limit,
                                                over[0][1] if len(over) == 1 else "this hour and next")
        else:
            reason = "%s %s > %g %%" % (what, ", ".join("%g %% (%s)" % o for o in over), limit)
        return _check(key, BAD_STATE, value="%g %% (%s)" % worst, limit=limit_txt,
                      detail=reason + age_txt, stale=stale), [reason]
    if worst is None:
        if field in unusable:
            return _check(key, UNKNOWN, limit=limit_txt,
                          detail="the NWS gridpoint series is unreadable this run"), []
        if field in missing:
            return _check(key, NOT_PROVIDED, limit=limit_txt,
                          detail="not in this NWS office's gridpoint product"), []
        return _check(key, UNKNOWN, limit=limit_txt, detail="no value for this hour or next"), []
    return _check(key, OK, value="%g %% (%s)" % worst, limit=limit_txt,
                  detail="this hour and next at or below %g %%%s" % (limit, age_txt), stale=stale), []


def check_forecast(cfg, entry: dict, now: datetime) -> Tuple[List[dict], List[str]]:
    """The three ttustatus forecast cuts and the winter cut on the raw gridpoint."""
    this_h, next_h, why, stale, missing, unusable = _forecast_rows(cfg, entry, now)
    checks: List[dict] = []
    reasons: List[str] = []
    specs = (("forecast_precip", "pop_pct", cfg.nws_precip_max),
             ("forecast_thunder", "thunder_pct", cfg.nws_thunder_max),
             ("forecast_cloud", "sky_cover_pct", cfg.nws_cloud_max))
    for key, field, limit in specs:
        if not cfg.check_enabled(key):
            checks.append(_check(key, OFF, detail="switched off"))
            continue
        if this_h is None:
            state = NOT_PROVIDED if why and why.startswith("not provided") else UNKNOWN
            checks.append(_check(key, state, limit="> %g %%" % limit, detail=why or "no forecast", stale=stale))
            continue
        rows = [("this hour", this_h)] + ([("next hour", next_h)] if next_h else [])
        c, r = _pct_check(key, field, float(limit), rows, stale, missing, forecast_age_text(entry),
                          tuple(unusable))
        c["reasons"] = r
        checks.append(c)
        reasons.extend(r)
    if not cfg.check_enabled("forecast_winter"):
        checks.append(_check("forecast_winter", OFF, detail="switched off"))
    elif this_h is None:
        state = NOT_PROVIDED if why and why.startswith("not provided") else UNKNOWN
        checks.append(_check("forecast_winter", state, limit="snow or ice > 0", detail=why or "no forecast", stale=stale))
    else:
        c, r = _winter_check(this_h, next_h, stale, missing, entry.get("tz") or "UTC", unusable)
        c["reasons"] = r
        checks.append(c)
        reasons.extend(r)
    return checks, reasons


def winter_entries(row: dict) -> List[str]:
    """Readable "coverage type" strings of a row's frozen-precipitation weather entries whose
    coverage is at least likely (a slight chance of snow is the precipitation-probability
    cut's business, not a snowstorm)."""
    out: List[str] = []
    for w in row.get("weather") or []:
        if not _is_dict(w):
            continue
        kind = str(w.get("weather") or "").lower()
        cov = str(w.get("coverage") or "").lower()
        if kind in _WINTER_TYPES and cov in _WINTER_COVERAGE:
            out.append(("%s %s" % (cov, kind)).replace("_", " "))
    return out


def _amount(what: str, mm: float, row: dict, key: str, label: str, tz: str) -> Tuple[str, Optional[str]]:
    """(text, hour label) of a forecast amount: an amount of a run longer than an hour is the
    run's total, so the text names the run ("snowfall 12.7 mm forecast for 12:00-18:00
    MDT") instead of an hour that would read as 12.7 mm in that hour."""
    per = (row.get("periods") or {}).get(key) if _is_dict(row.get("periods")) else None
    start = per.get("start") if _is_dict(per) else None
    hours = _num(per.get("hours")) if _is_dict(per) else None
    if isinstance(start, datetime) and hours is not None and hours > 1:
        end = start + timedelta(hours=hours)
        fmt = "%a %H:%M" if hours >= 24 else "%H:%M"
        span = "%s-%s" % (util.fmt_local(start, tz, fmt), util.fmt_local(end, tz, fmt + " %Z"))
        return "%s %s mm forecast for %s" % (what, _num_txt(mm), span), None
    return "%s %s mm forecast" % (what, _num_txt(mm)), label


def _winter_check(this_h: dict, next_h: Optional[dict], stale: bool, missing: List[str], tz: str = "UTC",
                  unusable: Optional[List[str]] = None) -> Tuple[dict, List[str]]:
    items: List[Tuple[str, Optional[str]]] = []
    seen = False
    for label, row in (("this hour", this_h), ("next hour", next_h)):
        if row is None:
            continue
        snow, ice = _num(row.get("snow_mm")), _num(row.get("ice_mm"))
        if snow is not None or ice is not None:
            seen = True
        if snow is not None and snow > 0:
            items.append(_amount("snowfall", snow, row, "snow_mm", label, tz))
        if ice is not None and ice > 0:
            items.append(_amount("ice accumulation", ice, row, "ice_mm", label, tz))
        for txt in winter_entries(row):
            seen = True
            items.append(("%s forecast" % txt, label))
    parts = _merge_hours(items)
    if parts:
        # one reason for the check (the summary has room for few): "snow or ice forecast:
        # snowfall 3 mm (this hour), likely snow (this hour and next)"
        reason = "snow or ice forecast: " + ", ".join(p.replace(" forecast", "", 1) for p in parts)
        return _check("forecast_winter", BAD_STATE, value="snow / ice", limit="snow or ice > 0",
                      detail="; ".join(parts), stale=stale), [reason]
    fields = ("snow_mm", "ice_mm", "weather")
    bad = [f for f in fields if f in (unusable or [])]
    if bad:
        # no snow in what could be read is no answer while a series could not be read
        return _check("forecast_winter", UNKNOWN, limit="snow or ice > 0",
                      detail="the NWS gridpoint's %s series %s unreadable this run" % (
                          " and ".join(bad), "is" if len(bad) == 1 else "are")), []
    if not seen and all(f in missing for f in fields):
        return _check("forecast_winter", NOT_PROVIDED, limit="snow or ice > 0",
                      detail="no snowfall, ice or weather fields in this gridpoint product"), []
    return _check("forecast_winter", OK, value="none", limit="snow or ice > 0",
                  detail="no snow or ice in the forecast for this hour or next", stale=stale), []


def metar_precipitation(metar: Any) -> List[str]:
    """The present-weather groups of a raw METAR that mean precipitation (or a thunderstorm)
    at the station, e.g. ["-RA"], ["TSRA", "BR"] -> ["TSRA"]; the remarks (after RMK) are
    not read (their RE.. groups are recent weather, not current)."""
    if not isinstance(metar, str):
        return []
    body = re.split(r"\sRMK\b", metar, 1)[0]
    return [g for g in body.split()[2:] if _METAR_PRECIP.match(g)]


def metar_pwino(metar: Any) -> bool:
    """Whether a raw METAR says its present-weather sensor is out of service (PWINO in the
    remarks): then the absence of weather groups says nothing about precipitation."""
    return isinstance(metar, str) and bool(re.search(r"\sRMK\b.*\sPWINO\b", metar))


def observed_precipitation(obs_raw: Optional[dict], text: str) -> Optional[str]:
    """What says precipitation is falling at the station now: the present-weather
    entries, the text description, the raw METAR's weather groups (a record NWS did not
    decode has only those), or precipitation in the last hour; None when nothing."""
    raw = obs_raw if _is_dict(obs_raw) else {}
    for w in raw.get("present_weather") or []:
        s = str(w)
        if _PRECIP_WORDS.search(s) and not _NOT_PRECIP.search(s):
            return "present weather: %s" % s
    # a description may join phenomena ("Light Rain and Fog/Mist", "Thunderstorm in Vicinity,
    # Light Rain"): each part on its own, so a vicinity part does not hide rain at the station
    if text and any(_PRECIP_WORDS.search(p) and not _NOT_PRECIP.search(p) for p in _TEXT_PARTS.split(text)):
        return "station reports %s" % text.strip()
    groups = metar_precipitation(raw.get("metar"))
    if groups:
        return "METAR reports %s" % " ".join(groups)
    mm = _num(raw.get("precip_last_hour_mm"))
    if mm is not None and mm > 0:
        return "%s mm of precipitation in the last hour" % _num_txt(mm)
    return None


def check_observation(cfg, entry: dict, now: datetime) -> Tuple[List[dict], List[str]]:
    """``obs_precip`` and ``obs_humidity`` from the nearest station's report, used only when
    the station is usable for the antenna and the report is fresh."""
    obs = entry.get("obs") if _is_dict(entry.get("obs")) else {}
    station = entry.get("obs_station") if _is_dict(entry.get("obs_station")) else {}
    raw = entry.get("obs_raw") if _is_dict(entry.get("obs_raw")) else {}
    checks: List[dict] = []
    reasons: List[str] = []
    sid = str(obs.get("station_id") or station.get("id") or "station")
    age_min = _num(obs.get("age_min"))
    ago = (" %s ago" % util.fmt_duration_minutes(age_min)) if age_min is not None else ""
    # No station of NWS's list lies within the limits (Mauna Kea, Pie Town): the checks do
    # not apply to this antenna at all, in every run, which is not a gap of this run.
    qual = station.get("qualifying")
    gap = UNKNOWN
    if isinstance(qual, list) and not qual:
        gap = NOT_PROVIDED
        why = "no NWS station within %g km and %g m of elevation of the antenna" % (
            cfg.obs_max_km, cfg.obs_max_delev_m)
        if station.get("id") and station.get("why"):
            why += " (nearest report: %s, %s)" % (sid, station["why"])
        usable_reason = None
    elif not obs.get("ok"):
        why = _no_obs(obs)
        usable_reason = None
    elif not station.get("usable", True):
        why = "%s not used: %s" % (sid, station.get("why") or "station too far from the antenna")
        if qual:
            why += "; no recent report from %s" % ", ".join(str(q) for q in qual[:3])
        usable_reason = None
    else:
        why = None
        usable_reason = "%s%s" % (sid, ago)
    stale = bool(obs.get("stale"))
    # precipitation: a report _precip_report chose (report_station), or the latest one
    weather = station.get("weather")
    report = bool(raw.get("report_station"))
    if not cfg.check_enabled("obs_precip"):
        checks.append(_check("obs_precip", OFF, detail="switched off"))
    elif gap == NOT_PROVIDED:
        checks.append(_check("obs_precip", NOT_PROVIDED, limit="any precipitation", detail=why))
    elif report or (not why and weather in (None, "reported")):
        precip_reason, psid, p_stale = usable_reason, sid, stale
        text = str(raw.get("text") or obs.get("text") or "")
        if report or _num(raw.get("report_age_min")) is not None:
            # the latest report says nothing (or is old, or is not usable): the newest recent
            # one with weather of the station or of the next qualifying station; its own words
            psid, text = str(raw.get("report_station") or sid), str(raw.get("text") or "")
            age = _num(raw.get("report_age_min"))
            p_stale = bool(raw.get("report_stale")) if report else stale
            precip_reason = "%s report%s (%s)" % (
                psid, "" if age is None else " of %s ago" % util.fmt_duration_minutes(age),
                "its newest with weather" if psid == sid else
                str(raw.get("report_passed") or "%s's latest has no weather" % sid))
        found = observed_precipitation(raw, text)
        if found:
            why_bad = "precipitation at %s (%s)" % (psid, found)
            reasons.append(why_bad)
            c = _check("obs_precip", BAD_STATE, value=text or "precipitation",
                       limit="any precipitation", detail="%s: %s" % (precip_reason, found), stale=p_stale)
            c["reasons"] = [why_bad]
            checks.append(c)
        elif raw.get("report_doubt"):
            # a newer report may say otherwise: no precipitation in this one is no answer
            checks.append(_check("obs_precip", UNKNOWN, value=text or None, limit="any precipitation",
                                 detail="%s: no precipitation reported, but %s" % (precip_reason, raw["report_doubt"]),
                                 stale=p_stale))
        elif not report and raw.get("reports_weather") is False:
            checks.append(_check("obs_precip", UNKNOWN, limit="any precipitation",
                                 detail="%s: the report says nothing about the weather" % precip_reason, stale=p_stale))
        else:
            checks.append(_check("obs_precip", OK, value=text or "none", limit="any precipitation",
                                 detail="%s: no precipitation reported" % precip_reason, stale=p_stale))
    elif weather in ("none", "unknown"):
        # no report that says whether it rains: "no precipitation reported" would be unknown
        # passed off as clear. n/a when no qualifying station reports weather at all (mesonet
        # / RAWS stations, judged by their recent reports), unknown for this run otherwise
        none = weather == "none"
        why_txt = str(station.get("weather_why") or (
            "%s reports no weather (no present-weather sensor)" % sid if none
            else "no report of %s says anything about the weather this run" % sid))
        if why and not none:
            why_txt = "%s; %s" % (why, why_txt)
        checks.append(_check("obs_precip", NOT_PROVIDED if none else UNKNOWN, limit="any precipitation",
                             detail=why_txt, stale=stale and not none and not why))
    else:
        checks.append(_check("obs_precip", UNKNOWN, limit="any precipitation", detail=why or "no report to read",
                             stale=stale))
    # humidity
    if not cfg.check_enabled("obs_humidity"):
        checks.append(_check("obs_humidity", OFF, detail="switched off"))
    elif why:
        checks.append(_check("obs_humidity", gap, limit="> %g %%" % cfg.obs_humidity_max,
                             detail=why, stale=stale and gap == UNKNOWN))
    else:
        rh = _num(obs.get("rh"))
        if rh is None:
            checks.append(_check("obs_humidity", UNKNOWN, limit="> %g %%" % cfg.obs_humidity_max,
                                 detail="%s: no humidity in the report" % usable_reason, stale=stale))
        elif rh > float(cfg.obs_humidity_max):
            why_bad = "humidity %s %% at %s" % (_num_txt(rh), sid)
            reasons.append(why_bad)
            c = _check("obs_humidity", BAD_STATE, value="%s %%" % _num_txt(rh),
                       limit="> %g %%" % cfg.obs_humidity_max,
                       detail="%s: %s %% > %g %%" % (usable_reason, _num_txt(rh), cfg.obs_humidity_max),
                       stale=stale)
            c["reasons"] = [why_bad]
            checks.append(c)
        else:
            checks.append(_check("obs_humidity", OK, value="%s %%" % _num_txt(rh),
                                 limit="> %g %%" % cfg.obs_humidity_max,
                                 detail="%s: %s %%" % (usable_reason, _num_txt(rh)), stale=stale))
    return checks, reasons


def check_wind(cfg, entry: dict, now: datetime) -> Tuple[dict, List[str]]:
    """Observed wind or gust, or the forecast wind or gust this hour or next, at or above
    ``cfg.wind_mph`` (the antennas stow in high wind; off when it is 0). The observation
    counts only while the station is usable for the antenna, as for the other observation
    checks: a valley station says nothing about a ridge. One reason per source (the
    strongest observed value, the strongest forecast hour), so the summary stays short."""
    if not cfg.check_enabled("wind"):
        return _check("wind", OFF, detail="no wind threshold configured (VLBA_WIND_MPH)"), []
    limit = float(cfg.wind_mph)
    limit_txt = ">= %g mph" % limit
    obs = entry.get("obs") if _is_dict(entry.get("obs")) else {}
    station = entry.get("obs_station") if _is_dict(entry.get("obs_station")) else {}
    sid = str(obs.get("station_id") or station.get("id") or "the station")
    details: List[str] = []
    worst: List[Tuple[float, str]] = []         # the strongest value of each source
    windless: Optional[int] = None              # the detail of a report without a wind value
    if not obs.get("ok"):
        details.append(_no_obs(obs))
    elif not station.get("usable", True):
        details.append("%s not used: %s" % (sid, station.get("why") or "station too far from the antenna"))
    else:
        got = [(what, _num(obs.get(key))) for key, what in (("wind_mph", "wind"), ("gust_mph", "gust"))]
        got = [(what, v) for what, v in got if v is not None]
        if got:
            details.append("%s: %s" % (sid, ", ".join("%s %s mph" % (w, _num_txt(v)) for w, v in got)))
            what, v = max(got, key=lambda wv: wv[1])
            worst.append((v, "%s %s mph at %s" % (what, _num_txt(v), sid)))
        else:
            # a report without a wind value (NWS's decoding left it out): said, not skipped
            details.append("%s: no wind value in its report" % sid)
            windless = len(details) - 1
    this_h, next_h, why, stale, _, unusable = _forecast_rows(cfg, entry, now)
    hours: List[str] = []
    best_fc: Optional[Tuple[float, str]] = None
    for label, row in (("this hour", this_h), ("next hour", next_h)):
        if row is None:
            continue
        got = [(what, _num(row.get(key))) for key, what in (("wind_kmh", "wind"), ("gust_kmh", "gust"))]
        got = [(what, util.kmh_to_mph(v)) for what, v in got if v is not None]
        if not got:
            continue
        hours.append("%s %s" % (label, ", ".join("%s %s mph" % (w, _num_txt(v)) for w, v in got)))
        what, v = max(got, key=lambda wv: wv[1])
        if best_fc is None or v > best_fc[0]:
            best_fc = (v, "forecast %s %s mph (%s)" % (what, _num_txt(v), label))
    if best_fc is not None:
        worst.append(best_fc)
        details.append("forecast: " + "; ".join(hours))
        if windless is not None:
            details[windless] += ", the forecast decides alone"
    elif this_h is None and why:
        details.append(why if why.startswith("forecast") else "forecast: %s" % why)
    stale = bool(stale or (obs.get("ok") and obs.get("stale")))
    if not worst:
        return _check("wind", UNKNOWN, limit=limit_txt,
                      detail="no wind value: " + "; ".join(details) if details else "no wind value"), []
    reasons = [txt for v, txt in worst if v >= limit]
    top = max(worst, key=lambda vt: vt[0])[1]
    if reasons:
        return _check("wind", BAD_STATE, value=top, limit=limit_txt, detail="; ".join(details),
                      stale=stale), reasons
    bad = [f for f in ("wind_kmh", "gust_kmh") if f in unusable]
    if bad and this_h is not None:
        # the gusts are what usually reach the limit: no answer without them
        return _check("wind", UNKNOWN, value=top, limit=limit_txt, stale=stale,
                      detail="; ".join(details + ["the NWS gridpoint's %s series unreadable this run"
                                                  % " and ".join("gust" if f == "gust_kmh" else "wind"
                                                                 for f in bad)])), []
    return _check("wind", OK, value=top, limit=limit_txt, detail="; ".join(details), stale=stale), []


def _num_txt(v: Any, nd: int = 1) -> str:
    f = _num(v)
    if f is None:
        return "?"
    r = round(f, nd)
    return "%d" % int(r) if r == int(r) else ("%.*f" % (nd, r))


def _no_obs(obs: dict) -> str:
    """"no usable observation: <why>", the prefix once: the parent's error carries it."""
    err = str(obs.get("error") or "no data")
    return err if err.startswith("no usable observation") else "no usable observation: " + err


# ---- the verdict -----------------------------------------------------------------------
def evaluate(cfg, entry: dict, st: dict, now: Optional[datetime] = None) -> dict:
    """The verdict of one antenna. ``st`` is this site's persisted state (updated in place).

    Returns ``{"status": GOOD|BAD|NO DATA, "incomplete": bool, "reasons": [str] (in
    REASON_ORDER), "missing": [str], "checks": [check dicts in CHECK_LABELS order],
    "scheduled": [str], "watch": [str] (short notes of the checks in "watch"), "evaluated":
    dt}``. A check dict: ``{"key", "label", "state", "value", "limit", "detail", "stale",
    "reasons", ["note"]}`` with ``state`` one of bad, watch, ok, unknown, off, scheduled, not
    provided. Never raises: a check that blows up on a surprise in its inputs is reported
    as unknown."""
    now = now or util.utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    checks: Dict[str, dict] = {}
    scheduled: List[str] = []

    def guarded(what: str, fn, *args):
        try:
            return fn(*args)
        except Exception as e:      # noqa: BLE001 - one check must never cost the verdict
            log.exception("%s check failed for %s: %s", what, entry.get("slug"), e)
            return None

    res = guarded("warning", check_warning, cfg, entry, now)
    if res is None:
        checks["warning"] = _check("warning", UNKNOWN, detail="check failed (see the log)")
    else:
        checks["warning"], r, scheduled = res
        checks["warning"]["reasons"] = r
    # the frames before the newest one (``radar_check["earlier"]``, oldest first, fetched where
    # the newest shows an echo in the ring) go through the same count first: each frame counts
    # once, so a run confirms an echo on consecutive frames by itself
    rc = entry.get("radar_check")
    for older in (rc.get("earlier") or []) if _is_dict(rc) and not rc.get("off") else []:
        if _is_dict(older):
            guarded("radar (earlier frame)", check_radar, cfg, {"radar_check": older}, st, now)
    res = guarded("radar", check_radar, cfg, entry, st, now)
    if res is None:
        checks["radar"] = _check("radar", UNKNOWN, detail="check failed (see the log)")
    else:
        checks["radar"], r = res
        checks["radar"]["reasons"] = r
    res = guarded("glm", check_glm, cfg, entry, st, now)
    if res is None:
        checks["glm"] = _check("glm", UNKNOWN, detail="check failed (see the log)")
    else:
        checks["glm"], r = res
        checks["glm"]["reasons"] = r
    res = guarded("forecast", check_forecast, cfg, entry, now)
    if res is None:
        for key in ("forecast_precip", "forecast_thunder", "forecast_cloud", "forecast_winter"):
            checks[key] = _check(key, UNKNOWN, detail="check failed (see the log)")
    else:
        for c in res[0]:
            checks[c["key"]] = c
    res = guarded("observation", check_observation, cfg, entry, now)
    if res is None:
        for key in ("obs_precip", "obs_humidity"):
            checks[key] = _check(key, UNKNOWN, detail="check failed (see the log)")
    else:
        for c in res[0]:
            checks[c["key"]] = c
    res = guarded("wind", check_wind, cfg, entry, now)
    if res is None:
        checks["wind"] = _check("wind", UNKNOWN, detail="check failed (see the log)")
    else:
        checks["wind"], r = res
        checks["wind"]["reasons"] = r

    reasons = [r for k in REASON_ORDER for r in (checks.get(k, {}).get("reasons") or [])]
    ordered = [checks[k] for k in CHECK_KEYS if k in checks]
    bad = [c for c in ordered if c["state"] == BAD_STATE]
    missing = [c["label"] for c in ordered if c["state"] == UNKNOWN]
    answered = [c for c in ordered if c["state"] in (OK, WATCH, SCHEDULED)]
    # BAD on any bad check; GOOD only when at least one check could answer; a run in which
    # every enabled check lacked data has no verdict (never a green "GOOD?" on nothing)
    status = BAD if bad else (GOOD if answered else NO_DATA)
    watch = [str(c["note"]) for c in ordered if c["state"] == WATCH and c.get("note")]
    out = {"status": status, "incomplete": bool(missing), "reasons": reasons, "missing": missing,
           "checks": ordered, "scheduled": scheduled, "watch": watch, "evaluated": now}
    log.info("%s: %s%s%s", entry.get("slug"), status,
             " (%s)" % "; ".join(reasons) if reasons else "",
             " — no data: %s" % ", ".join(missing) if missing else "")
    return out


def summary(verdicts: List[dict]) -> Dict[str, int]:
    """``{"good", "bad", "no_data", "incomplete"}`` counts for the page's masthead
    (``no_data``: antennas without a verdict, including a missing verdict dict)."""
    good = sum(1 for v in verdicts if _is_dict(v) and v.get("status") == GOOD)
    bad = sum(1 for v in verdicts if _is_dict(v) and v.get("status") == BAD)
    inc = sum(1 for v in verdicts if _is_dict(v) and v.get("incomplete"))
    return {"good": good, "bad": bad, "no_data": len(verdicts) - good - bad, "incomplete": inc}
