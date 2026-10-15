"""Every check of the verdict, the holds and the streak, the state file, and that nothing
raises on garbage inputs."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from vlba import decision as d

UTC = timezone.utc
NOW = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)


def _alert(event, onset=None, ends=None, sender="NWS Albuquerque NM"):
    return {"id": "x-" + event, "event": event, "onset": onset, "end_dt": ends, "ends": None,
            "expires": None, "sender": sender}


def _row(**kw):
    row = {"sky_cover_pct": 10.0, "pop_pct": 5.0, "thunder_pct": 0.0, "snow_mm": 0.0, "ice_mm": 0.0,
           "gust_kmh": 20.0, "weather": []}
    row.update(kw)
    return row


def _entry(**over):
    e = {"slug": "pt", "tz": "America/Denver",
         "alerts_feed": {"ok": True, "stale": False, "error": None},
         "alerts_at": [],
         "radar_check": {"available": True, "stale": False, "source": "MRMS", "frame_ts": NOW - timedelta(minutes=4),
                         "in_ring": False, "nearest_km": None, "count": 0, "max_dbz": None, "error": None},
         "glm": {"covered": True, "sats": ["G19", "G18"], "available": True, "partial": False, "error": None,
                 "count_window": 0, "nearest_km": 210.4, "nearest_bearing": "SE", "window_min": 5},
         "gridpoint": {"ok": True, "stale": False, "error": None, "update_age_min": 30.0, "missing": [],
                       "now": _row(), "next": _row(), "hours": [_row(), _row()]},
         "obs": {"ok": True, "stale": False, "error": None, "station_id": "KONM", "age_min": 20.0,
                 "text": "Clear", "rh": 40.0, "wind_mph": 8.0, "gust_mph": None},
         "obs_station": {"id": "KONM", "usable": True, "why": None},
         "obs_raw": {"present_weather": [], "precip_last_hour_mm": 0.0, "text": "Clear"}}
    e.update(over)
    return e


def test_all_clear_is_good_and_complete(cfg):
    st = {}
    v = d.evaluate(cfg, _entry(), st, NOW)
    assert v["status"] == d.GOOD and v["incomplete"] is False and v["reasons"] == [] and v["missing"] == []
    states = {c["key"]: c["state"] for c in v["checks"]}
    assert states == {"warning": "ok", "radar": "ok", "glm": "ok", "forecast_precip": "ok",
                      "forecast_thunder": "ok", "forecast_cloud": "ok", "forecast_winter": "ok",
                      "obs_precip": "ok", "obs_humidity": "off", "wind": "ok"}
    assert [c["key"] for c in v["checks"]] == list(d.CHECK_KEYS)
    assert st["radar_streak"] == 0 and st["radar_last_hit_ts"] is None and st["glm_last_hit_ts"] is None


# ---- warnings ---------------------------------------------------------------------------
def test_warning_check_without_the_sites_zones_is_unknown(cfg):
    """Without the point metadata a zone-based warning cannot be matched: no "none in
    effect" guess; a polygon match still makes it BAD."""
    meta = {"ok": False, "error": "skipped: run budget exhausted"}
    v = d.evaluate(cfg, _entry(meta=meta), {}, NOW)
    c = v["checks"][0]
    assert c["state"] == "unknown" and "zones are unknown" in c["detail"] and v["incomplete"]
    e = _entry(meta=meta, alerts_at=[_alert("Tornado Warning", onset=NOW - timedelta(minutes=5),
                                            ends=NOW + timedelta(hours=1))])
    assert d.evaluate(cfg, e, {}, NOW)["status"] == d.BAD
    assert d.evaluate(cfg, _entry(meta={"ok": True}), {}, NOW)["checks"][0]["state"] == "ok"


def test_observed_precipitation_words():
    yes = ("light rain", "heavy snow", "freezing rain", "light freezing drizzle", "thunderstorms",
           "ice pellets", "unknown precipitation", "light rain showers")
    no = ("freezing fog", "fog mist", "haze", "squalls", "low drifting snow", "showers in the vicinity",
          "thunderstorms in the vicinity", "smoke")
    for w in yes:
        assert d.observed_precipitation({"present_weather": [w]}, ""), w
    for w in no:
        assert d.observed_precipitation({"present_weather": [w]}, "") is None, w
    # a joined description: the vicinity part does not hide rain at the station
    assert d.observed_precipitation({}, "Thunderstorm in Vicinity, Light Rain")
    assert d.observed_precipitation({}, "Light Rain and Fog/Mist")
    assert d.observed_precipitation({}, "Freezing Fog") is None
    assert d.observed_precipitation({}, "Showers in Vicinity and Haze") is None


def test_warning_check_states_its_limit_in_every_state(cfg):
    c = d.evaluate(cfg, _entry(), {}, NOW)["checks"][0]
    assert c["state"] == "ok" and c["limit"] == "one of 12 NWS warnings in effect"
    cfg.veto_events = "Tornado Warning"
    assert d.evaluate(cfg, _entry(), {}, NOW)["checks"][0]["limit"] == "Tornado Warning in effect"


def test_veto_warning_at_the_site_is_bad(cfg):
    ends = NOW + timedelta(hours=3)
    e = _entry(alerts_at=[_alert("High Wind Warning", onset=NOW - timedelta(hours=1), ends=ends),
                          _alert("Flood Watch", ends=ends)])
    v = d.evaluate(cfg, e, {}, NOW)
    assert v["status"] == d.BAD
    assert any("High Wind Warning" in r and "until" in r and "Albuquerque" in r for r in v["reasons"])
    assert not any("Flood Watch" in r for r in v["reasons"])
    assert v["checks"][0]["state"] == "bad"


def test_warning_ahead_of_its_onset_is_scheduled_not_bad(cfg):
    e = _entry(alerts_at=[_alert("Winter Storm Warning", onset=NOW + timedelta(hours=5),
                                 ends=NOW + timedelta(hours=20))])
    v = d.evaluate(cfg, e, {}, NOW)
    assert v["status"] == d.GOOD and v["scheduled"] and "Winter Storm Warning" in v["scheduled"][0]
    assert v["checks"][0]["state"] == "scheduled"
    # within the lead it counts: 45 min by default, ttustatus's 15 plus the 30-minute run
    # interval, so the run before the onset's last 15 min already shows BAD
    assert cfg.veto_onset_lead_s == 2700
    for minutes, state in ((10, "bad"), (16, "bad"), (44, "bad"), (46, "scheduled")):
        e = _entry(alerts_at=[_alert("Winter Storm Warning", onset=NOW + timedelta(minutes=minutes),
                                     ends=NOW + timedelta(hours=20))])
        assert d.evaluate(cfg, e, {}, NOW)["checks"][0]["state"] == state, minutes


def test_warning_matching_is_case_insensitive_and_list_configurable(cfg):
    e = _entry(alerts_at=[_alert("tornado WARNING", ends=NOW + timedelta(hours=1))])
    assert d.evaluate(cfg, e, {}, NOW)["status"] == d.BAD
    cfg.veto_events = "Flood Watch"
    assert d.evaluate(cfg, e, {}, NOW)["status"] == d.GOOD
    cfg.veto_events = ""
    v = d.evaluate(cfg, e, {}, NOW)
    assert v["checks"][0]["state"] == "off"


def test_alert_feed_down_makes_the_warning_check_unknown(cfg):
    e = _entry(alerts_feed={"ok": False, "stale": True, "error": "HTTP 503"})
    v = d.evaluate(cfg, e, {}, NOW)
    assert v["status"] == d.GOOD and v["incomplete"] and "NWS warning over the site" in v["missing"]


# ---- radar ------------------------------------------------------------------------------
def _radar(in_ring, **over):
    r = {"available": True, "stale": False, "source": "MRMS", "frame_ts": NOW - timedelta(minutes=4),
         "in_ring": in_ring, "nearest_km": 12.3 if in_ring else None, "count": 40 if in_ring else 0,
         "max_dbz": 38.0 if in_ring else None, "error": None}
    r.update(over)
    return r


def test_radar_needs_two_consecutive_runs_then_holds(cfg):
    st = {}
    v1 = d.evaluate(cfg, _entry(radar_check=_radar(True)), st, NOW)
    assert v1["status"] == d.GOOD and st["radar_streak"] == 1
    radar = [c for c in v1["checks"] if c["key"] == "radar"][0]
    assert radar["state"] == "watch" and "unconfirmed" in radar["detail"] and "1 of 2" in radar["detail"]
    t2 = NOW + timedelta(minutes=5)
    v2 = d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=t2 - timedelta(minutes=3))), st, t2)
    assert v2["status"] == d.BAD and st["radar_streak"] == 2 and st["radar_last_hit_ts"] == t2.timestamp()
    assert any("rain on radar within 30 km" in r and "12.3 km" in r for r in v2["reasons"])
    # a clear frame 5 min later: still bad (15-min hold), streak reset
    t3 = t2 + timedelta(minutes=5)
    v3 = d.evaluate(cfg, _entry(radar_check=_radar(False, frame_ts=t3 - timedelta(minutes=3))), st, t3)
    assert v3["status"] == d.BAD and st["radar_streak"] == 0 and "hold" in v3["reasons"][0]
    # 16 min after the hit: clear again
    t4 = t2 + timedelta(minutes=16)
    v4 = d.evaluate(cfg, _entry(radar_check=_radar(False, frame_ts=t4 - timedelta(minutes=3))), st, t4)
    assert v4["status"] == d.GOOD


def _radar_state(cfg, cov, in_ring=False, st=None):
    v = d.evaluate(cfg, _entry(radar_check=_radar(in_ring, coverage=cov)), {} if st is None else st, NOW)
    return v, [c for c in v["checks"] if c["key"] == "radar"][0]


def test_no_echo_is_clear_only_where_radar_covers_the_ring(cfg):
    """The frames write "no coverage" like "no echo" (asked 2026-10-12: a radar outage is
    "no data", never "clear"): MRMS's own file says how much of the ring radar covers."""
    ts = NOW - timedelta(minutes=4)
    v, c = _radar_state(cfg, {"ok": True, "covered_pct": 100.0, "ts": ts})
    assert c["state"] == "ok" and "covers" not in c["detail"] and v["status"] == d.GOOD and not v["incomplete"]
    v, c = _radar_state(cfg, {"ok": True, "covered_pct": 95.5, "ts": ts})
    assert c["state"] == "ok" and c["detail"].endswith("; radar covers 95.5 % of the ring (MRMS 15:56Z)")
    v, c = _radar_state(cfg, {"ok": True, "covered_pct": 64.0, "ts": ts})
    assert c["state"] == "unknown" and v["status"] == d.GOOD and v["incomplete"] and "Rain on radar" in v["missing"]
    assert c["detail"].startswith("MRMS: no radar covers 36 % of the 30 km ring (MRMS 15:56Z), so no echo there "
                                  "would show (clear needs 90 % covered)")
    v, c = _radar_state(cfg, {"ok": False, "error": "HTTP 503 for https://noaa-mrms-pds/x", "ts": None})
    assert c["state"] == "unknown" and "radar coverage of the ring is unknown (HTTP 503" in c["detail"]
    v, c = _radar_state(cfg, {"off": True})
    assert c["state"] == "ok" and c["detail"].endswith("radar coverage not checked (VLBA_RADAR_COVERAGE=0)")
    v, c = _radar_state(cfg, None)                      # a check that knows nothing of coverage
    assert c["state"] == "ok" and "coverage" not in c["detail"]
    v, c = _radar_state(cfg, {"ok": True, "covered_pct": 90.0, "ts": ts})         # "at least 90 %"
    assert c["state"] == "ok" and not v["incomplete"]
    cfg.radar_min_coverage = 60.0
    assert _radar_state(cfg, {"ok": True, "covered_pct": 64.0, "ts": ts})[1]["state"] == "ok"


def test_an_echo_counts_whatever_the_coverage(cfg):
    st = {}
    low = {"ok": True, "covered_pct": 20.0, "ts": NOW}
    assert _radar_state(cfg, low, in_ring=True, st=st)[1]["state"] == "watch"
    t2 = NOW + timedelta(minutes=5)
    v = d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=t2 - timedelta(minutes=3), coverage=low)), st, t2)
    assert v["status"] == d.BAD
    unknown = {"ok": False, "error": "x", "ts": None}
    t3 = t2 + timedelta(minutes=5)          # held after the confirmed echo, whatever the coverage says
    v = d.evaluate(cfg, _entry(radar_check=_radar(False, frame_ts=t3 - timedelta(minutes=3), coverage=unknown)), st, t3)
    assert v["status"] == d.BAD and "hold" in v["reasons"][0]


def test_radar_single_frame_trigger_and_missing_frames(cfg):
    cfg.radar_trigger_after = 1
    st = {}
    assert d.evaluate(cfg, _entry(radar_check=_radar(True)), st, NOW)["status"] == d.BAD
    # a missing frame neither clears the streak nor ends the hold early
    st = {"radar_streak": 1}
    v = d.evaluate(cfg, _entry(radar_check=_radar(False, available=False, error="IEM down")), st, NOW)
    assert st["radar_streak"] == 1 and v["incomplete"] and "Rain on radar" in v["missing"]
    # an old frame is no data
    v = d.evaluate(cfg, _entry(radar_check=_radar(False, frame_ts=NOW - timedelta(minutes=40))), {}, NOW)
    assert "Rain on radar" in v["missing"]


def test_radar_counts_each_frame_once(cfg):
    """Two runs that see the same frame (no newer one at IEM, or the last good copy reused
    after a failed fetch) have one frame of evidence, not two: the echo stays unconfirmed."""
    st = {}
    f1 = NOW - timedelta(minutes=4)
    d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=f1)), st, NOW)
    assert st["radar_streak"] == 1 and st["radar_last_frame_ts"] == f1.timestamp()
    t2 = NOW + timedelta(minutes=5)
    v = d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=f1)), st, t2)
    radar = [c for c in v["checks"] if c["key"] == "radar"][0]
    assert v["status"] == d.GOOD and radar["state"] == "watch" and st["radar_streak"] == 1
    assert "the frame the last run saw, not counted again; 1 of 2" in radar["detail"]
    # the reused copy of a newer frame after a failed fetch: no new evidence either
    f2 = NOW - timedelta(minutes=1)
    v = d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=f2, stale=True)), st, t2)
    assert v["status"] == d.GOOD and st["radar_streak"] == 1
    assert "a copy reused after a failed fetch" in [c for c in v["checks"] if c["key"] == "radar"][0]["detail"]
    # a frame that is really new confirms
    v = d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=f2)), st, t2)
    assert v["status"] == d.BAD and st["radar_streak"] == 2
    assert "on 2 consecutive frames" in [c for c in v["checks"] if c["key"] == "radar"][0]["detail"]
    # a repeated CLEAR frame does not reset the streak either
    st = {"radar_streak": 1, "radar_last_frame_ts": f2.timestamp()}
    d.evaluate(cfg, _entry(radar_check=_radar(False, frame_ts=f2)), st, t2)
    assert st["radar_streak"] == 1
    # a frame without a time stamp counts as new; a recorded frame from the future blocks nothing
    counted = (NOW - timedelta(minutes=5)).timestamp()
    st = {"radar_streak": 1, "radar_last_frame_ts": NOW.timestamp() + 86400, "radar_last_count_ts": counted}
    v = d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=f1)), st, NOW)
    assert v["status"] == d.BAD and st["radar_last_frame_ts"] == f1.timestamp()
    st = {"radar_streak": 1, "radar_last_frame_ts": f1.timestamp(), "radar_last_count_ts": counted}
    assert d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=None)), st, NOW)["status"] == d.BAD
    # an echo before a 3-h outage and one after it are not two consecutive frames
    st = {"radar_streak": 1, "radar_last_frame_ts": (f1 - timedelta(hours=3)).timestamp()}
    v = d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=f1)), st, NOW)
    assert v["status"] == d.GOOD and st["radar_streak"] == 1


def _rc(minutes_ago, in_ring, **kw):
    """A ring check of the frame of ``minutes_ago`` min before NOW."""
    c = {"available": True, "stale": False, "source": "MRMS", "frame_ts": NOW - timedelta(minutes=minutes_ago),
         "in_ring": in_ring, "nearest_km": 12.0 if in_ring else None, "count": 5 if in_ring else 0,
         "max_dbz": 30.0 if in_ring else None, "error": None}
    c.update(kw)
    return c


def _radar_of(v):
    return {c["key"]: c for c in v["checks"]}["radar"]


def test_radar_one_run_confirms_with_the_frame_before(cfg):
    """Runs are 30 min apart (2026-10-14): where the newest frame shows an echo, the run also
    has the frame before it (``earlier``, oldest first), counted first, so the run confirms
    an echo on two consecutive frames by itself; the frames of the run 30 min before are too
    far apart to continue a streak."""
    st = {}
    # the run half an hour ago saw an echo on its newest frame only (the one before was dry)
    v = d.evaluate(cfg, _entry(radar_check=dict(_rc(34, True), earlier=[_rc(36, False)])), st,
                   NOW - timedelta(minutes=30))
    assert _radar_of(v)["state"] == "watch" and st["radar_streak"] == 1
    # now the echo is on the newest frame and on the one before: BAD from this run alone
    v = d.evaluate(cfg, _entry(radar_check=dict(_rc(4, True), earlier=[_rc(6, True)])), st, NOW)
    assert _radar_of(v)["state"] == "bad" and "on 2 consecutive frames" in _radar_of(v)["detail"]
    assert v["status"] == d.BAD and st["radar_streak"] == 2
    assert st["radar_last_frame_ts"] == (NOW - timedelta(minutes=4)).timestamp()
    # without the frame before, the same newest frame is only "unconfirmed": the last frame
    # counted is 30 min older, too far back to be consecutive
    st = {"radar_streak": 1, "radar_last_frame_ts": (NOW - timedelta(minutes=34)).timestamp(),
          "radar_last_count_ts": (NOW - timedelta(minutes=30)).timestamp()}
    v = d.evaluate(cfg, _entry(radar_check=_rc(4, True)), st, NOW)
    assert _radar_of(v)["state"] == "watch" and st["radar_streak"] == 1
    # a dry frame before the echo: unconfirmed, frame 1 of 2
    st = {}
    v = d.evaluate(cfg, _entry(radar_check=dict(_rc(4, True), earlier=[_rc(6, False)])), st, NOW)
    assert _radar_of(v)["state"] == "watch" and "frame 1 of 2" in _radar_of(v)["detail"]


def test_radar_frames_before_the_newest_count_once(cfg):
    """A frame before the newest that the run before already counted (runs close together)
    is not counted again; an unusable one is no new data; a check switched off ignores them;
    junk in the list is skipped."""
    st = {"radar_streak": 1, "radar_last_frame_ts": (NOW - timedelta(minutes=6)).timestamp(),
          "radar_last_count_ts": (NOW - timedelta(minutes=2)).timestamp()}
    v = d.evaluate(cfg, _entry(radar_check=dict(_rc(4, True), earlier=[_rc(6, True)])), st, NOW)
    assert _radar_of(v)["state"] == "bad" and st["radar_streak"] == 2         # 6 min ago: counted once
    st = {}
    bad_scan = _rc(6, True, available=False, error="12 px of unrecognised colour within 30 km")
    v = d.evaluate(cfg, _entry(radar_check=dict(_rc(4, True), earlier=[bad_scan])), st, NOW)
    assert _radar_of(v)["state"] == "watch" and st["radar_streak"] == 1
    st = {}
    v = d.evaluate(cfg, _entry(radar_check=dict(_rc(4, True), off=True, earlier=[_rc(6, True)])), st, NOW)
    assert _radar_of(v)["state"] == "off" and not st.get("radar_streak")
    st = {}
    v = d.evaluate(cfg, _entry(radar_check=dict(_rc(4, True), earlier=[None, 3, "x", _rc(6, True)])), st, NOW)
    assert _radar_of(v)["state"] == "bad"
    st = {}
    v = d.evaluate(cfg, _entry(radar_check=dict(_rc(4, True), earlier="junk")), st, NOW)
    assert _radar_of(v)["state"] == "watch"


def test_radar_three_frame_trigger_counts_the_frames_before_oldest_first(cfg):
    """With VLBA_RADAR_TRIGGER_AFTER=3 a run has two frames before the newest, oldest first
    (the producers' contract): counted in that order they complete the streak; newest first,
    the older one would not be newer than the frame counted last."""
    cfg.radar_trigger_after = 3
    st = {}
    v = d.evaluate(cfg, _entry(radar_check=dict(_rc(4, True), earlier=[_rc(8, True), _rc(6, True)])), st, NOW)
    assert _radar_of(v)["state"] == "bad" and st["radar_streak"] == 3
    st = {}
    v = d.evaluate(cfg, _entry(radar_check=dict(_rc(4, True), earlier=[_rc(6, True), _rc(8, True)])), st, NOW)
    assert _radar_of(v)["state"] == "watch" and st["radar_streak"] == 2


def test_radar_counts_each_scan_once(cfg):
    """A frame whose ring holds the same data as the frame counted last (the same ``digest``:
    the radar had not completed a new scan) is no new observation: it neither counts nor
    resets the streak, and the detail says why. Within a run and across runs alike."""
    st = {}
    v = d.evaluate(cfg, _entry(radar_check=dict(_rc(4, True, digest="a"), earlier=[_rc(6, True, digest="a")])), st, NOW)
    assert _radar_of(v)["state"] == "watch" and st["radar_streak"] == 1
    assert "the same radar data as the frame counted last" in _radar_of(v)["detail"]
    st = {}
    v = d.evaluate(cfg, _entry(radar_check=dict(_rc(4, True, digest="b"), earlier=[_rc(6, True, digest="a")])), st, NOW)
    assert _radar_of(v)["state"] == "bad" and st["radar_streak"] == 2 and st["radar_last_digest"] == "b"
    # the next run, 2 min later, sees the counted scan once more: still confirmed, nothing counted
    v = d.evaluate(cfg, _entry(radar_check=_rc(2, True, digest="b")), st, NOW + timedelta(minutes=2))
    assert _radar_of(v)["state"] == "bad" and st["radar_streak"] == 2
    assert st["radar_last_frame_ts"] == (NOW - timedelta(minutes=4)).timestamp()
    # a run that starts a streak afresh does not let one scan confirm itself across runs
    st = {}
    d.evaluate(cfg, _entry(radar_check=_rc(4, True, digest="c")), st, NOW)
    v = d.evaluate(cfg, _entry(radar_check=_rc(2, True, digest="c")), st, NOW + timedelta(minutes=5))
    assert _radar_of(v)["state"] == "watch" and st["radar_streak"] == 1
    # frames without a digest (older producers, tests) are told apart by their time, as before
    st = {}
    v = d.evaluate(cfg, _entry(radar_check=dict(_rc(4, True), earlier=[_rc(6, True)])), st, NOW)
    assert _radar_of(v)["state"] == "bad" and st["radar_last_digest"] is None


def test_radar_a_repeated_scan_trusts_only_a_recent_streak(cfg):
    """A frame with the data of the frame counted last speaks for the stored streak only while
    that count is recent (VLBA_RADAR_STALE): a source serving unchanged data must not revive a
    confirmation from an hour ago."""
    old = {"radar_streak": 2, "radar_last_frame_ts": (NOW - timedelta(minutes=64)).timestamp(),
           "radar_last_count_ts": (NOW - timedelta(minutes=60)).timestamp(), "radar_last_digest": "x"}
    v = d.evaluate(cfg, _entry(radar_check=_rc(4, True, digest="x")), dict(old), NOW)
    assert _radar_of(v)["state"] == "watch" and v["status"] == d.GOOD
    recent = dict(old, radar_last_frame_ts=(NOW - timedelta(minutes=8)).timestamp(),
                  radar_last_count_ts=(NOW - timedelta(minutes=5)).timestamp())
    v = d.evaluate(cfg, _entry(radar_check=_rc(4, True, digest="x")), recent, NOW)
    assert _radar_of(v)["state"] == "bad"


def _glm_entry(**over):
    g = {"covered": True, "sats": ["G19"], "available": True, "partial": False, "error": None,
         "count_window": 0, "nearest_km": None, "nearest_bearing": None, "window_min": 31}
    g.update(over)
    return _entry(glm=g)


def test_glm_flashes_past_the_hold_in_incomplete_data_are_no_data(cfg):
    """Flashes of the window that are past the hold are no hit; with a satellite's data
    incomplete the check then has no data, never "clear"; a past-hold flash leaves the stored
    last hit alone."""
    st = {"glm_last_hit": {"nearest_km": 3.0, "nearest_bearing": "E", "count": 4, "at": 1.0}}
    e = _glm_entry(available=False, partial=True, error="G19: 1 of 93 granules failed", count_window=1,
                   nearest_km=12.0, nearest_bearing="N", last_in_ring_t=NOW - timedelta(minutes=30, seconds=30))
    v = d.evaluate(cfg, e, st, NOW)
    glm_check = [c for c in v["checks"] if c["key"] == "glm"][0]
    assert glm_check["state"] == "unknown" and v["incomplete"]
    assert st["glm_last_hit"] == {"nearest_km": 3.0, "nearest_bearing": "E", "count": 4, "at": 1.0}


def test_glm_held_detail_says_only_what_the_window_shows(cfg):
    """The held detail opens with what this run's window says: flashes past the hold, "no
    flash" when the window does not reach the last flash, and nothing when the last flash lies
    inside the window but this run missed it (a refetch that failed)."""
    def detail(e, ago_min):
        st = {"glm_last_hit_ts": (NOW - timedelta(minutes=ago_min)).timestamp(),
              "glm_last_hit": {"nearest_km": 5.6, "nearest_bearing": "N", "count": 1, "at": 1.0}}
        return [c for c in d.evaluate(cfg, e, st, NOW)["checks"] if c["key"] == "glm"][0]["detail"]
    past = _glm_entry(count_window=1, nearest_km=11.0, nearest_bearing="N",
                      last_in_ring_t=NOW - timedelta(minutes=30, seconds=30))
    assert detail(past, 15).startswith("1 flash(es) within 50 km, the newest 30 min ago: past the 30 min hold; "
                                       "held 30 min after the last one (15 min ago")
    assert detail(_glm_entry(), 15).startswith("held 30 min after the last one (15 min ago")
    assert detail(_glm_entry(window_min=6), 15).startswith("no flash within 50 km in the last 6 min; held 30 min")


def test_confirmed_echo_stays_bad_on_a_repeated_frame(cfg):
    """Rain confirmed on two frames, then the source repeats its last frame (IEM publishes
    nothing newer, or the composite's last copy is reused): past the 15-min hold that frame
    still shows confirmed rain while it is usable, so the check stays BAD, never 'watch'."""
    st = {}
    t0 = NOW
    d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=t0 - timedelta(minutes=2))), st, t0)
    f2 = t0 + timedelta(minutes=1)
    assert d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=f2)), st, t0 + timedelta(minutes=2))["status"] == d.BAD
    for later, stale in ((17, False), (18, True)):        # hold over, frame 16-17 min old, still usable
        v = d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=f2, stale=stale)), st, t0 + timedelta(minutes=later))
        radar = [c for c in v["checks"] if c["key"] == "radar"][0]
        assert v["status"] == d.BAD and radar["state"] == "bad" and v["watch"] == [], later
    # once the frame is too old it is no data, and the hold is over
    v = d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=f2)), st, t0 + timedelta(minutes=25))
    assert [c for c in v["checks"] if c["key"] == "radar"][0]["state"] == "unknown"


def test_metar_pwino():
    """PWINO in the remarks: the present-weather sensor is out of service."""
    phsf = "PHSF 100105Z AUTO 00000KT M M SCT016 SCT043 BKN060 21/15 A3033 RMK AO2 SLP149 RVRNO PWINO $"
    assert d.metar_pwino(phsf) and d.metar_precipitation(phsf) == []
    assert not d.metar_pwino("KONM 091715Z AUTO 18005KT 10SM CLR 18/09 A3002 RMK AO2")
    assert not d.metar_pwino("PWINO 091715Z") and not d.metar_pwino(None)


def test_metar_precipitation_groups():
    m = d.metar_precipitation
    assert m("KABC 091715Z AUTO 18005KT 10SM -RA BKN030 18/09 A3002") == ["-RA"]
    assert m("KABC 091715Z 18005KT 2SM TSRA BR OVC010CB 18/17 A2990") == ["TSRA"]
    assert m("KABC 091715Z 18005KT 1/2SM +SHSN BLSN FZFG VV005 M05/M06 A3002") == ["+SHSN", "BLSN"]
    assert m("KABC 091715Z 18005KT 10SM VCSH VCTS FEW030 18/09 A3002") == []          # in the vicinity
    assert m("KABC 091715Z 18005KT 10SM DRSN FZFG BR HZ SQ 18/09 A3002") == []        # no precipitation
    assert m("KABC 091715Z 18005KT 10SM TS SCT030CB 18/09 A3002") == ["TS"]
    assert m("KABC 091715Z 18005KT 10SM CLR 18/09 A3002 RMK AO2 RAE05 P0001") == []    # recent, in remarks
    assert m("") == [] and m(None) == []


def test_stored_streak_confirms_only_its_own_frame(cfg):
    """A reused copy of a frame that was never counted, or a stamp-less old state, does not
    turn a stored streak into a confirmed echo."""
    st = {"radar_streak": 2}
    v = d.evaluate(cfg, _entry(radar_check=_radar(True, stale=True)), st, NOW)
    assert [c for c in v["checks"] if c["key"] == "radar"][0]["state"] == "watch" and v["status"] == d.GOOD
    f = NOW - timedelta(minutes=4)
    st = {"radar_streak": 2, "radar_last_frame_ts": (f - timedelta(minutes=5)).timestamp(),
          "radar_last_count_ts": (NOW - timedelta(minutes=5)).timestamp()}
    v = d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=f, stale=True)), st, NOW)
    assert [c for c in v["checks"] if c["key"] == "radar"][0]["state"] == "watch"
    st["radar_last_frame_ts"] = f.timestamp()                 # the very frame that was counted
    assert d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=f, stale=True)), st, NOW)["status"] == d.BAD


def test_humidity_keeps_the_latest_reports_age(cfg):
    cfg.obs_humidity_max = 95.0
    e = _entry(obs_raw={"present_weather": [], "precip_last_hour_mm": None, "text": "Clear", "report_age_min": 80.0})
    checks = {c["key"]: c for c in d.evaluate(cfg, e, {}, NOW)["checks"]}
    assert "KONM report of 1 h 20 min ago (its newest with weather)" in checks["obs_precip"]["detail"]
    assert checks["obs_humidity"]["detail"].startswith("KONM 20 min ago")
    # another station's report: named, and its own words only (the latest's "Clear" is not its)
    e = _entry(obs_raw={"present_weather": [], "precip_last_hour_mm": None, "text": "", "report_age_min": 10.0,
                        "report_station": "KFAR", "metar": "KFAR 091715Z AUTO 18005KT 10SM -RA BKN030 18/09 A3002"})
    checks = {c["key"]: c for c in d.evaluate(cfg, e, {}, NOW)["checks"]}
    assert checks["obs_precip"]["state"] == "bad" and checks["obs_precip"]["value"] == "precipitation"
    assert checks["obs_precip"]["detail"].startswith("KFAR report of 10 min ago (KONM's latest has no weather)")
    # the staleness and any doubt are the report's own, not the station's latest
    e["obs_raw"].update(metar="", report_stale=True, report_doubt="the recent reports of KONM could not be read")
    e["obs"]["stale"] = False
    c = {c["key"]: c for c in d.evaluate(cfg, e, {}, NOW)["checks"]}["obs_precip"]
    assert c["state"] == "unknown" and c["stale"] and "but the recent reports of KONM" in c["detail"]


def test_streak_without_stamps_is_no_evidence(cfg):
    """A state file of the previous version (a streak, no frame or count stamp), or frames
    without a time after an outage: the old streak does not confirm the next echo."""
    st = {"radar_streak": 1}
    v = d.evaluate(cfg, _entry(radar_check=_radar(True)), st, NOW)
    assert v["status"] == d.GOOD and st["radar_streak"] == 1
    st = {"radar_streak": 1, "radar_last_count_ts": (NOW - timedelta(hours=3)).timestamp()}
    v = d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=None)), st, NOW)
    assert v["status"] == d.GOOD and st["radar_streak"] == 1 and st["radar_last_count_ts"] == NOW.timestamp()


def test_unreadable_winter_or_wind_series_is_no_answer(cfg):
    for bad in (["snow_mm"], ["weather"], ["ice_mm"]):
        gp = {"ok": True, "stale": False, "update_age_min": 10.0, "missing": [], "unusable": bad,
              "now": _row(**{bad[0]: None if bad[0] != "weather" else []}), "next": _row()}
        v = d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)
        c = [c for c in v["checks"] if c["key"] == "forecast_winter"][0]
        assert c["state"] == "unknown" and "unreadable" in c["detail"] and v["incomplete"], bad
    gp = {"ok": True, "stale": False, "update_age_min": 10.0, "missing": [], "unusable": ["gust_kmh"],
          "now": _row(gust_kmh=None, wind_kmh=30.0), "next": _row(gust_kmh=None, wind_kmh=30.0)}
    c = [c for c in d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)["checks"] if c["key"] == "wind"][0]
    assert c["state"] == "unknown" and "gust series unreadable" in c["detail"]
    # a gust over the limit in what could be read is still BAD
    gp["now"] = _row(gust_kmh=None, wind_kmh=80.0)
    assert d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)["status"] == d.BAD


def test_radar_frame_of_unknown_time_ages_with_its_copy(cfg):
    """An island composite whose time is unknown (capabilities failed) and that comes from a
    25-min-old cached copy is too old for the verdict, like any frame of known age."""
    v = d.evaluate(cfg, _entry(radar_check=_radar(False, frame_ts=None, stale=True, age_s=1500.0)), {}, NOW)
    assert [c for c in v["checks"] if c["key"] == "radar"][0]["state"] == "unknown" and v["incomplete"]
    v = d.evaluate(cfg, _entry(radar_check=_radar(False, frame_ts=None, age_s=0.0)), {}, NOW)
    assert [c for c in v["checks"] if c["key"] == "radar"][0]["state"] == "ok"


def test_future_hold_stamp_is_clamped(cfg):
    st = {"radar_last_hit_ts": NOW.timestamp() + 86400, "glm_last_hit_ts": NOW.timestamp() + 86400}
    v = d.evaluate(cfg, _entry(), st, NOW)
    assert st["radar_last_hit_ts"] == NOW.timestamp() and v["status"] == d.BAD
    later = NOW + timedelta(minutes=31)
    assert d.evaluate(cfg, _entry(), st, later)["status"] == d.GOOD


# ---- GLM --------------------------------------------------------------------------------
def test_glm_flash_in_ring_is_bad_and_held_30_min(cfg):
    st = {}
    g = {"covered": True, "sats": ["G19"], "available": True, "partial": False, "error": None,
         "count_window": 3, "nearest_km": 22.0, "nearest_bearing": "NW", "window_min": 5}
    v = d.evaluate(cfg, _entry(glm=g), st, NOW)
    assert v["status"] == d.BAD and "lightning within 50 km" in v["reasons"][0] and "22 km NW" in v["reasons"][0]
    g2 = dict(g, count_window=0, nearest_km=90.0)
    t2 = NOW + timedelta(minutes=20)
    v2 = d.evaluate(cfg, _entry(glm=g2), st, t2)
    assert v2["status"] == d.BAD and "hold" in v2["reasons"][0] and "22 km" in [c for c in v2["checks"] if c["key"] == "glm"][0]["detail"]
    t3 = NOW + timedelta(minutes=31)
    assert d.evaluate(cfg, _entry(glm=g2), st, t3)["status"] == d.GOOD


def test_glm_hold_runs_from_the_flash_not_from_the_run(cfg):
    """A late run (or a catch-up) finds a flash 19 min old: 11 min of the 30-min hold are
    left, not 30."""
    st = {}
    g = {"covered": True, "sats": ["G19"], "available": True, "partial": False, "error": None,
         "count_window": 1, "nearest_km": 20.0, "nearest_bearing": "N", "window_min": 15,
         "last_in_ring_t": NOW - timedelta(minutes=19)}
    assert d.evaluate(cfg, _entry(glm=g), st, NOW)["status"] == d.BAD
    assert st["glm_last_hit_ts"] == (NOW - timedelta(minutes=19)).timestamp()
    g2 = dict(g, count_window=0, last_in_ring_t=None, nearest_km=None)
    v = d.evaluate(cfg, _entry(glm=g2), st, NOW + timedelta(minutes=5))
    assert v["status"] == d.BAD and "6 min left" in v["reasons"][0]
    assert d.evaluate(cfg, _entry(glm=g2), st, NOW + timedelta(minutes=12))["status"] == d.GOOD


def test_glm_flash_past_the_hold_is_no_hit(cfg):
    """The hold runs from the flash: a flash still in a long window but older than
    VLBA_GLM_HOLD (a window that reached back) no longer makes the weather BAD, and the detail
    names it rather than saying "no flash"; a flash inside the hold is a hit."""
    st = {}
    g = {"covered": True, "sats": ["G19"], "available": True, "partial": False, "error": None,
         "count_window": 1, "nearest_km": 20.0, "nearest_bearing": "N", "window_min": 45,
         "last_in_ring_t": NOW - timedelta(minutes=40)}
    v = d.evaluate(cfg, _entry(glm=g), st, NOW)
    glm_check = [c for c in v["checks"] if c["key"] == "glm"][0]
    assert v["status"] == d.GOOD and glm_check["state"] == "ok" and st["glm_last_hit_ts"] is None
    assert glm_check["detail"].startswith("1 flash(es) within 50 km, the newest 40 min ago: past the 30 min hold; "
                                          "nearest flash 20 km N")
    v = d.evaluate(cfg, _entry(glm=dict(g, last_in_ring_t=NOW - timedelta(minutes=20))), {}, NOW)
    glm_check = [c for c in v["checks"] if c["key"] == "glm"][0]
    assert v["status"] == d.BAD and glm_check["detail"].startswith("1 flash(es) within 50 km in the last 45 min")
    st = {}
    v = d.evaluate(cfg, _entry(glm=g), st, NOW)
    g2 = dict(g, count_window=0, last_in_ring_t=None, nearest_km=None, window_min=6)
    assert d.evaluate(cfg, _entry(glm=g2), st, NOW + timedelta(minutes=2))["status"] == d.GOOD


def test_glm_flash_in_partial_data_counts(cfg):
    """One satellite incomplete: a flash the data does hold is a flash (BAD, hold armed);
    without one, the answer is clear only when a covering satellite's window is complete."""
    st = {}
    g = {"covered": True, "sats": ["G19", "G18"], "available": False, "partial": True,
         "error": "G18: 1 of 18 granules failed (timed out)", "count_window": 1, "nearest_km": 7.8,
         "nearest_bearing": "N", "window_min": 6}
    v = d.evaluate(cfg, _entry(glm=g), st, NOW)
    assert v["status"] == d.BAD and "lightning within 50 km" in v["reasons"][0]
    assert st["glm_last_hit_ts"] == NOW.timestamp() and not v["incomplete"]
    g = dict(g, count_window=0, nearest_km=None)
    v = d.evaluate(cfg, _entry(glm=g), {}, NOW)
    assert v["incomplete"] and "Lightning (GOES GLM)" in v["missing"]
    v = d.evaluate(cfg, _entry(glm=dict(g, available=True)), {}, NOW)
    glm_c = [c for c in v["checks"] if c["key"] == "glm"][0]
    assert glm_c["state"] == "ok" and "partial: G18: 1 of 18" in glm_c["detail"] and not v["incomplete"]


def test_glm_unavailable_off_and_uncovered(cfg):
    v = d.evaluate(cfg, _entry(glm={"covered": True, "available": False, "error": "deadline"}), {}, NOW)
    assert v["incomplete"] and "Lightning (GOES GLM)" in v["missing"]
    v = d.evaluate(cfg, _entry(glm={"covered": False, "available": False}), {}, NOW)
    assert not v["incomplete"] and [c for c in v["checks"] if c["key"] == "glm"][0]["state"] == "off"
    cfg.glm_enabled = False
    v = d.evaluate(cfg, _entry(), {}, NOW)
    assert [c for c in v["checks"] if c["key"] == "glm"][0]["state"] == "off"


# ---- forecast ---------------------------------------------------------------------------
@pytest.mark.parametrize("field,value,key", [
    ("pop_pct", 25.0, "forecast_precip"), ("thunder_pct", 16.0, "forecast_thunder"),
    ("sky_cover_pct", 61.0, "forecast_cloud")])
def test_forecast_thresholds_this_or_next_hour(cfg, field, value, key):
    gp = {"ok": True, "stale": False, "update_age_min": 10.0, "missing": [], "now": _row(), "next": _row(**{field: value})}
    v = d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)
    assert v["status"] == d.BAD and "(next hour)" in v["reasons"][0]
    assert [c for c in v["checks"] if c["key"] == key][0]["state"] == "bad"
    # exactly the limit is fine
    limit = {"pop_pct": 20.0, "thunder_pct": 15.0, "sky_cover_pct": 60.0}[field]
    gp["next"] = _row(**{field: limit})
    assert d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)["status"] == d.GOOD


def test_forecast_field_not_provided_is_neither_bad_nor_missing(cfg):
    gp = {"ok": True, "stale": False, "update_age_min": 10.0, "missing": ["thunder_pct"],
          "now": _row(thunder_pct=None), "next": _row(thunder_pct=None)}
    v = d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)
    assert v["status"] == d.GOOD and not v["incomplete"]
    assert [c for c in v["checks"] if c["key"] == "forecast_thunder"][0]["state"] == "not provided"


def test_forecast_stale_or_missing_is_unknown(cfg):
    gp = {"ok": True, "stale": False, "update_age_min": 200.0, "missing": [], "now": _row(), "next": _row()}
    v = d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)
    assert not v["incomplete"]                       # 3 h 20 min since the office's update is normal
    assert "NWS update 3 h 20 min ago" in [c for c in v["checks"] if c["key"] == "forecast_cloud"][0]["detail"]
    gp["update_age_min"] = 800.0
    v = d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)
    assert v["incomplete"] and "Cloud cover (forecast)" in v["missing"]
    assert "13 h 20 min ago" in [c for c in v["checks"] if c["key"] == "forecast_cloud"][0]["detail"]
    v = d.evaluate(cfg, _entry(gridpoint={"ok": False, "error": "HTTP 500"}), {}, NOW)
    assert len([m for m in v["missing"] if "(forecast)" in m]) == 4
    v = d.evaluate(cfg, _entry(gridpoint={"ok": False, "not_provided": True}), {}, NOW)
    assert not v["incomplete"]


def test_forecast_reasons_one_per_check(cfg):
    """Both hours over the limit make one reason (the summary at the top shows few)."""
    gp = {"ok": True, "stale": False, "update_age_min": 10.0, "missing": [],
          "now": _row(pop_pct=50.0, sky_cover_pct=70.0), "next": _row(pop_pct=50.0, sky_cover_pct=90.0)}
    v = d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)
    assert v["reasons"] == ["precipitation probability 50 % > 20 % (this hour and next)",
                            "cloud cover 70 % (this hour), 90 % (next hour) > 60 %"]
    gp["next"] = _row(weather=[{"coverage": "likely", "weather": "snow", "intensity": None}])
    gp["now"] = _row(weather=[{"coverage": "likely", "weather": "snow", "intensity": None}])
    assert d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)["reasons"] == [
        "snow or ice forecast: likely snow (this hour and next)"]
    # 1-h amounts that differ, ice and a weather entry: still one reason for the check
    gp["now"] = _row(snow_mm=3.0, weather=[{"coverage": "likely", "weather": "snow", "intensity": None}])
    gp["next"] = _row(snow_mm=5.0, ice_mm=0.3, weather=[{"coverage": "likely", "weather": "snow", "intensity": None}])
    assert d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)["reasons"] == [
        "snow or ice forecast: snowfall 3 mm (this hour), likely snow (this hour and next), snowfall 5 mm "
        "(next hour), ice accumulation 0.3 mm (next hour)"]


def test_snow_amount_names_its_period(cfg):
    """A 6-hour run's snowfall is the run's total, not this hour's."""
    start = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    per = {"snow_mm": {"start": start, "hours": 6}}
    gp = {"ok": True, "stale": False, "update_age_min": 10.0, "missing": [],
          "now": _row(snow_mm=12.7, periods=per), "next": _row(snow_mm=12.7, periods=per)}
    v = d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)
    assert v["reasons"] == ["snow or ice forecast: snowfall 12.7 mm for 06:00-12:00 MDT"]


def test_malformed_forecast_series_is_unknown_not_not_provided(cfg):
    gp = {"ok": True, "stale": False, "update_age_min": 10.0, "missing": [], "unusable": ["sky_cover_pct"],
          "now": _row(sky_cover_pct=None), "next": _row(sky_cover_pct=None)}
    v = d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)
    c = [c for c in v["checks"] if c["key"] == "forecast_cloud"][0]
    assert c["state"] == "unknown" and "unreadable" in c["detail"] and v["incomplete"]
    # an unavailable forecast has no data at all: "no data", without a "stale" chip
    v = d.evaluate(cfg, _entry(gridpoint={"ok": False, "stale": True, "error": "HTTP 500"}), {}, NOW)
    assert all(not c["stale"] for c in v["checks"] if c["key"].startswith("forecast"))


def test_cloud_cut_can_be_switched_off(cfg):
    cfg.nws_cloud_max = -1
    gp = {"ok": True, "stale": False, "update_age_min": 10.0, "missing": [], "now": _row(sky_cover_pct=100.0), "next": _row()}
    v = d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)
    assert v["status"] == d.GOOD and [c for c in v["checks"] if c["key"] == "forecast_cloud"][0]["state"] == "off"


def test_winter_precipitation(cfg):
    gp = {"ok": True, "stale": False, "update_age_min": 10.0, "missing": [], "now": _row(snow_mm=3.0), "next": _row()}
    v = d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)
    assert v["status"] == d.BAD and "snowfall 3 mm" in v["reasons"][0]
    gp["now"] = _row(weather=[{"coverage": "likely", "weather": "snow", "intensity": "light"}])
    v = d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)
    assert v["status"] == d.BAD and "likely snow" in v["reasons"][0]
    gp["now"] = _row(weather=[{"coverage": "slight_chance", "weather": "snow", "intensity": None}])
    assert d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)["status"] == d.GOOD
    gp["now"] = _row(ice_mm=0.5)
    assert "ice accumulation" in d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)["reasons"][0]
    cfg.nws_winter = False
    assert d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)["status"] == d.GOOD


# ---- observation ------------------------------------------------------------------------
def test_observed_precipitation_and_humidity(cfg):
    cfg.obs_humidity_max = 95.0                 # off by default; ttustatus's value
    e = _entry(obs=dict(_entry()["obs"], text="Light Rain"), obs_raw={"present_weather": [], "text": "Light Rain"})
    v = d.evaluate(cfg, e, {}, NOW)
    assert v["status"] == d.BAD and "precipitation at KONM" in v["reasons"][0]
    e = _entry(obs_raw={"present_weather": ["light snow"], "precip_last_hour_mm": None, "text": "Overcast"})
    assert "present weather: light snow" in d.evaluate(cfg, e, {}, NOW)["reasons"][0]
    e = _entry(obs_raw={"present_weather": [], "precip_last_hour_mm": 0.3, "text": "Overcast"})
    assert "0.3 mm" in d.evaluate(cfg, e, {}, NOW)["reasons"][0]
    e = _entry(obs=dict(_entry()["obs"], text="Thunderstorms in the Vicinity"), obs_raw={"present_weather": [], "text": ""})
    assert d.evaluate(cfg, e, {}, NOW)["status"] == d.GOOD
    e = _entry(obs=dict(_entry()["obs"], rh=96.0))
    v = d.evaluate(cfg, e, {}, NOW)
    assert v["status"] == d.BAD and "humidity 96 %" in v["reasons"][0]
    e = _entry(obs=dict(_entry()["obs"], rh=95.0))
    assert d.evaluate(cfg, e, {}, NOW)["status"] == d.GOOD


def test_no_qualifying_station_is_not_provided_not_missing(cfg):
    """Mauna Kea, Pie Town: no station of NWS's list lies within the limits. The checks do
    not apply there, in every run: n/a, and the verdict is complete."""
    cfg.obs_humidity_max = 95.0
    station = {"id": "PHSF", "usable": False, "why": "station 1892 m lower than the antenna", "qualifying": []}
    e = _entry(obs=dict(_entry()["obs"], station_id="PHSF", text="Rain", rh=99.0), obs_station=station)
    v = d.evaluate(cfg, e, {}, NOW)
    states = {c["key"]: c for c in v["checks"]}
    assert v["status"] == d.GOOD and not v["incomplete"] and v["missing"] == []
    assert states["obs_precip"]["state"] == "not provided" and states["obs_humidity"]["state"] == "not provided"
    assert "no NWS station within 50 km and 1000 m" in states["obs_precip"]["detail"]
    assert "PHSF" in states["obs_precip"]["detail"]
    # no observation at all there: still n/a, never a gap
    nowhere = {"id": None, "usable": False, "qualifying": []}
    v = d.evaluate(cfg, _entry(obs={"ok": False, "error": "down"}, obs_station=nowhere), {}, NOW)
    assert [c for c in v["checks"] if c["key"] == "obs_precip"][0]["state"] == "not provided"
    # a qualifying station exists but has no recent report: that IS a gap
    station = dict(station, qualifying=["QSBA3"])
    v = d.evaluate(cfg, _entry(obs=dict(_entry()["obs"], station_id="PHSF"), obs_station=station), {}, NOW)
    c = [c for c in v["checks"] if c["key"] == "obs_precip"][0]
    assert c["state"] == "unknown" and "no recent report from QSBA3" in c["detail"] and v["incomplete"]


def test_station_without_weather_is_never_clear(cfg):
    station = {"id": "QSBA3", "usable": True, "qualifying": ["QSBA3"], "weather": "none"}
    v = d.evaluate(cfg, _entry(obs_station=station), {}, NOW)
    c = [c for c in v["checks"] if c["key"] == "obs_precip"][0]
    assert c["state"] == "not provided" and not v["incomplete"]
    v = d.evaluate(cfg, _entry(obs_station=dict(station, weather="unknown")), {}, NOW)
    c = [c for c in v["checks"] if c["key"] == "obs_precip"][0]
    assert c["state"] == "unknown" and v["incomplete"] and "says anything about the weather" in c["detail"]
    # the wind half of the observation still counts from such a station
    v = d.evaluate(cfg, _entry(obs=dict(_entry()["obs"], gust_mph=50.0), obs_station=station), {}, NOW)
    assert v["status"] == d.BAD and "gust 50 mph" in v["reasons"][0]


def test_unusable_station_is_unknown_not_bad(cfg):
    cfg.obs_humidity_max = 95.0
    e = _entry(obs=dict(_entry()["obs"], text="Rain", rh=99.0),
               obs_station={"id": "PHTO", "usable": False, "why": "station 1900 m lower than the antenna"})
    v = d.evaluate(cfg, e, {}, NOW)
    assert v["status"] == d.GOOD and v["incomplete"]
    assert {"Precipitation observed", "Humidity observed"} <= set(v["missing"])
    assert "1900 m lower" in [c for c in v["checks"] if c["key"] == "obs_precip"][0]["detail"]
    e = _entry(obs={"ok": False, "error": "no usable observation"})
    assert "Humidity observed" in d.evaluate(cfg, e, {}, NOW)["missing"]
    cfg.obs_precip = False
    cfg.obs_humidity_max = -1
    v = d.evaluate(cfg, e, {}, NOW)
    assert not v["incomplete"]


def test_the_observation_error_is_prefixed_once(cfg):
    """The parent's error already starts with "no usable observation" (weather.nws): the
    details used to read "no usable observation: no usable observation: PHSF: ..."."""
    for err in ("no usable observation: PHSF: no temperature; PHTO: no temperature",
                "station list unavailable"):
        v = d.evaluate(cfg, _entry(obs={"ok": False, "error": err}), {}, NOW)
        for key in ("wind", "obs_precip"):
            detail = [c for c in v["checks"] if c["key"] == key][0]["detail"]
            assert detail.count("no usable observation") == 1, (key, detail)
            assert "station list unavailable" in detail or "PHSF: no temperature" in detail


# ---- wind -------------------------------------------------------------------------------
def test_a_report_without_a_wind_value_says_so(cfg):
    """NWS's decoding leaves the wind out of some reports (KBIH, TISX on 2026-10-12): the
    observed half of the check used to drop out without a word."""
    e = _entry(obs=dict(_entry()["obs"], wind_mph=None, gust_mph=None))
    c = [c for c in d.evaluate(cfg, e, {}, NOW)["checks"] if c["key"] == "wind"][0]
    assert c["state"] == "ok" and c["detail"].startswith("KONM: no wind value in its report, the forecast decides alone")
    # no forecast wind either: no data, and nothing "decides alone"
    rows = [dict(r, wind_kmh=None, gust_kmh=None) for r in (_row(), _row())]
    e["gridpoint"] = dict(e["gridpoint"], now=rows[0], next=rows[1], hours=rows)
    c = [c for c in d.evaluate(cfg, e, {}, NOW)["checks"] if c["key"] == "wind"][0]
    assert c["state"] == "unknown" and "KONM: no wind value in its report" in c["detail"]
    assert "decides alone" not in c["detail"]


def test_wind_cut_when_configured(cfg):
    cfg.wind_mph = 40.0
    e = _entry(obs=dict(_entry()["obs"], wind_mph=25.0, gust_mph=44.0))
    v = d.evaluate(cfg, e, {}, NOW)
    assert v["status"] == d.BAD and "gust 44 mph" in v["reasons"][0]
    gp = {"ok": True, "stale": False, "update_age_min": 10.0, "missing": [], "now": _row(gust_kmh=80.0), "next": _row()}
    v = d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)
    assert v["status"] == d.BAD and "forecast gust" in v["reasons"][0]
    assert d.evaluate(cfg, _entry(), {}, NOW)["status"] == d.GOOD


def test_wind_cut_is_on_at_45_mph_by_default(cfg):
    assert cfg.wind_mph == 45.0 and cfg.obs_humidity_max < 0
    c = [c for c in d.evaluate(cfg, _entry(), {}, NOW)["checks"] if c["key"] == "wind"][0]
    assert c["state"] == "ok" and "KONM: wind 8 mph" in c["detail"] and "this hour gust 12.4 mph" in c["detail"]
    v = d.evaluate(cfg, _entry(obs=dict(_entry()["obs"], gust_mph=46.0)), {}, NOW)
    assert v["status"] == d.BAD and v["reasons"] == ["gust 46 mph at KONM"]
    # the forecast's sustained wind counts too (an office may leave the gust series empty),
    # and both hours over the limit make ONE reason: the strongest
    gp = {"ok": True, "stale": False, "update_age_min": 10.0, "missing": [],
          "now": _row(gust_kmh=None, wind_kmh=75.0), "next": _row(gust_kmh=90.0)}
    v = d.evaluate(cfg, _entry(gridpoint=gp), {}, NOW)
    assert v["status"] == d.BAD and v["reasons"] == ["forecast gust 55.9 mph (next hour)"]
    # an unusable station's wind is ignored; with no forecast either, the check has no data
    e = _entry(obs=dict(_entry()["obs"], station_id="PHSF", wind_mph=60.0),
               obs_station={"id": "PHSF", "usable": False, "why": "station 1892 m lower than the antenna"},
               gridpoint={"ok": False, "error": "down"})
    c = [c for c in d.evaluate(cfg, e, {}, NOW)["checks"] if c["key"] == "wind"][0]
    assert c["state"] == "unknown" and "PHSF not used" in c["detail"] and "; forecast unavailable: down" in c["detail"]


# ---- state file and robustness ----------------------------------------------------------
def test_state_file_round_trip_and_garbage(tmp_path):
    p = str(tmp_path / "state.json")
    assert d.load_state(p) == {"sites": {}, "saved_ts": None}
    st = {"sites": {"pt": {"radar_streak": 1, "radar_last_hit_ts": 5.0}}}
    d.save_state(p, st, NOW)
    back = d.load_state(p)
    assert back["sites"]["pt"]["radar_streak"] == 1 and back["saved_ts"] == NOW.timestamp()
    assert not [f for f in os.listdir(tmp_path) if ".tmp." in f]
    with open(p, "w") as f:
        f.write("{not json")
    assert d.load_state(p)["sites"] == {}
    with open(p, "w") as f:
        json.dump({"sites": {"pt": "garbage", "mk": {"radar_last_hit_ts": "soon"}}}, f)
    st = d.load_state(p)
    assert st["sites"] == {"mk": {"radar_last_hit_ts": "soon"}}
    assert d.site_state(st, "pt") == {}
    # an unwritable path is logged, not raised
    d.save_state("/proc/no/such/dir/state.json", st, NOW)


def test_garbage_entry_never_raises(cfg):
    for e in ({}, {"radar_check": "x", "glm": 3, "gridpoint": [], "obs": None, "alerts_at": [None, 1]},
              {"radar_check": {"frame_ts": "not a date", "available": True, "in_ring": "yes"}}):
        v = d.evaluate(cfg, dict(_entry(), **e) if e else {}, {"radar_streak": "x"}, NOW)
        assert v["status"] in (d.GOOD, d.BAD) and len(v["checks"]) == len(d.CHECK_KEYS)


def test_summary_counts():
    vs = [{"status": "GOOD", "incomplete": False}, {"status": "BAD", "incomplete": True}, None, {"status": "GOOD", "incomplete": True}]
    assert d.summary(vs) == {"good": 2, "bad": 1, "no_data": 1, "incomplete": 2}


def test_no_data_at_all_is_no_verdict(cfg):
    e = {"slug": "pt", "tz": "UTC", "alerts_feed": {"ok": False, "error": "down"}, "alerts_at": [],
         "radar_check": {"available": False, "error": "down"}, "glm": {"covered": True, "available": False},
         "gridpoint": {"ok": False, "error": "down"}, "obs": {"ok": False, "error": "down"}}
    v = d.evaluate(cfg, e, {}, NOW)
    assert v["status"] == d.NO_DATA and v["incomplete"] and len(v["missing"]) == 9
    # one answered check is enough for GOOD
    e["alerts_feed"] = {"ok": True}
    assert d.evaluate(cfg, e, {}, NOW)["status"] == d.GOOD
