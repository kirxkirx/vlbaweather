"""The radar confirmation (audit fixes #59 #119 #69 #142 #87 #90 #89 #40 #71 #88, and the pins
S21 S23 S24 S27): the frames that confirm an echo are looked for from the newest frame back,
an echo that no earlier frame could be read for is "no data" and not "watch", a frame of
unknown time or dated ahead of the clock is never "clear", and the settings under which an
echo could never be confirmed are refused at startup."""
from __future__ import annotations

import os
from datetime import timedelta

import pytest

from weather import geo, http, util
import make_vlba_page as mvp                     # noqa: E402  (conftest put vlbaweather on sys.path)
from vlba import decision as d
from vlba.config import VlbaConfig
from vlba.tests.conftest import MK, PT
from vlba.tests.test_main import (ECHO, MOVED, _archive_name, _mrms_result, _quiet_glm, _radar_state,
                                  add_routes, caps_doc, frame_png, island_png, island_ts)
from vlba.tests.test_decision import NOW, _entry, _radar

NO_EARLIER = "echo in the newest frame; no earlier frame could be read to confirm it"


def _late_frame(monkeypatch, minutes):
    """The newest MRMS frame, with an echo in Pie Town's ring, ``minutes`` old (IEM late)."""
    newest = dict(_mrms_result(ECHO), ts=(util.utcnow() - timedelta(minutes=minutes)).replace(second=0, microsecond=0))
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: newest)
    return newest["ts"]


def _radar_check(v):
    return [c for c in v["checks"] if c["key"] == "radar"][0]


# ---- #59 / #69: the look-back is anchored at the newest frame -------------------------------
@pytest.mark.parametrize("age_min,scan_min", [(11, 10), (14, 6)])
def test_a_late_newest_frame_is_still_confirmed_by_the_scan_before_it(cfg, fake_http, monkeypatch, age_min, scan_min):
    """The audit case, default settings: the newest frame 11 min old with 10-minute scans, or
    14 min old with 6-minute scans. The scan before it lies more than VLBA_RADAR_STALE before
    now but within it before the newest frame: it is fetched and confirms the echo (it stayed
    "watch" while the look-back ended at now minus VLBA_RADAR_STALE)."""
    _quiet_glm(monkeypatch)
    t = _late_frame(monkeypatch, age_min)
    for m in range(2, scan_min, 2):
        fake_http.add(_archive_name(t - timedelta(minutes=m)), frame_png(ECHO))      # the same scan
    fake_http.add(_archive_name(t - timedelta(minutes=scan_min)), frame_png(MOVED))  # the scan before
    add_routes(fake_http, cfg)
    pt = mvp.run_once(cfg)["sites"][0]
    radar = _radar_state(pt)
    assert radar["state"] == "bad" and "on 2 consecutive frames" in radar["detail"], radar
    assert [c["frame_ts"] for c in pt["radar_check"]["earlier"]] == [t - timedelta(minutes=scan_min)]
    assert pt["radar_check"]["confirm"] == "examined"


def test_an_earlier_frame_is_judged_from_the_newest_frame_not_from_the_clock(cfg):
    """An earlier frame carries ``confirms_ts``: usable up to VLBA_RADAR_STALE before a usable
    newest frame, whatever its own age; not when the newest frame itself is too old."""
    newest = NOW - timedelta(minutes=11)
    older = _radar(True, frame_ts=newest - timedelta(minutes=10), confirms_ts=newest)
    st = {}
    v = d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=newest, earlier=[older], confirm="examined")), st, NOW)
    assert _radar_check(v)["state"] == "bad" and v["status"] == d.BAD and st["radar_streak"] == 2
    # the frame before more than VLBA_RADAR_STALE before the newest: not consecutive
    far = _radar(True, frame_ts=newest - timedelta(seconds=cfg.radar_stale_s + 60), confirms_ts=newest)
    v = d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=newest, earlier=[far])), {}, NOW)
    assert _radar_check(v)["state"] == "watch"
    # the newest frame too old: neither counts
    stale_newest = NOW - timedelta(seconds=cfg.radar_stale_s + 60)
    old = _radar(True, frame_ts=stale_newest - timedelta(minutes=4), confirms_ts=stale_newest)
    st = {}
    v = d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=stale_newest, earlier=[old])), st, NOW)
    assert _radar_check(v)["state"] == "unknown" and not st.get("radar_streak")


# ---- #59: settings under which an echo can never be confirmed -------------------------------
def _cfg_env(monkeypatch, tmp_path, **env):
    for k in list(os.environ):
        if k.startswith(("WEATHER_", "VLBA_")):
            monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return VlbaConfig.from_env(out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "cache"))


@pytest.mark.parametrize("env,need", [
    ({"VLBA_RADAR_STALE": "300"}, 960),
    ({"VLBA_RADAR_STALE": "959"}, 960),
    ({"VLBA_RADAR_TRIGGER_AFTER": "3"}, 1560),          # with the default VLBA_RADAR_STALE=1200
    ({"VLBA_RADAR_TRIGGER_AFTER": "6", "VLBA_RADAR_STALE": "3000"}, 3360),
    ({"VLBA_RADAR_TRIGGER_AFTER": "1", "VLBA_RADAR_STALE": "300"}, 360),
])
def test_settings_that_could_never_confirm_an_echo_are_refused(monkeypatch, tmp_path, env, need):
    with pytest.raises(ValueError) as e:
        _cfg_env(monkeypatch, tmp_path, **env)
    msg = str(e.value)
    assert "VLBA_RADAR_STALE" in msg and "VLBA_RADAR_TRIGGER_AFTER" in msg and ">= %d" % need in msg


def test_the_defaults_and_the_smallest_accepted_values_pass(monkeypatch, tmp_path):
    c = _cfg_env(monkeypatch, tmp_path)
    assert c.radar_trigger_after == 2 and c.radar_stale_s == 1200
    for trigger in range(1, 7):
        need = 360 + 600 * (trigger - 1)
        c = _cfg_env(monkeypatch, tmp_path, VLBA_RADAR_TRIGGER_AFTER=str(trigger), VLBA_RADAR_STALE=str(need))
        assert c.radar_stale_s == need


# ---- #119 / #142 / #69: an echo no earlier frame could be read for --------------------------
@pytest.mark.parametrize("how", ["missing", "get_fails", "truncated", "did_not_answer"])
def test_an_echo_whose_earlier_frames_cannot_be_read_is_no_data(cfg, fake_http, monkeypatch, how):
    """The newest frame shows rain in the ring and the frames before it cannot be had: the
    radar check is "no data" (the verdict GOOD?), on every run, never "watch" and a plain GOOD."""
    _quiet_glm(monkeypatch)
    t = _late_frame(monkeypatch, 4)
    if how == "missing":
        fake_http.fail("lcref_")
    elif how == "truncated":
        fake_http.add("lcref_", frame_png(MOVED)[:200])
    elif how == "get_fails":
        fake_http.add("lcref_", frame_png(MOVED))
        real = mvp.http.get_bytes
        monkeypatch.setattr(mvp.http, "get_bytes", lambda url, *a, **k: (_ for _ in ()).throw(
            http.HttpError("HTTP 500 for the archive")) if "lcref_" in url else real(url, *a, **k))
    else:
        monkeypatch.setattr(mvp.http, "head_ok", lambda *a, **k: None)
    add_routes(fake_http, cfg)
    for _ in range(2):
        run = mvp.run_once(cfg)
        pt = run["sites"][0]
        radar = _radar_state(pt)
        assert radar["state"] == "unknown" and NO_EARLIER in radar["detail"], radar
        assert "nearest echo" in radar["detail"] and "frame %s" % t.strftime("%H:%MZ") in radar["detail"]
        assert pt["radar_check"]["confirm"] == "unavailable"
        assert any("not confirmed by this run alone" in e for e in run["errors"]), run["errors"]
        # Pie Town is BAD by nothing else here: GOOD?, never a plain GOOD
        assert pt["verdict"]["status"] == "GOOD" and pt["verdict"]["incomplete"]
        assert "/" not in radar["detail"].split(":", 1)[1]            # no path or URL in the detail


def test_an_echo_whose_frame_before_was_examined_and_clear_stays_watch(cfg, fake_http, monkeypatch):
    """The case the two-frame rule was made for: the frame before was read and showed no echo."""
    _quiet_glm(monkeypatch)
    _late_frame(monkeypatch, 4)
    fake_http.add("lcref_", frame_png([]))
    add_routes(fake_http, cfg)
    pt = mvp.run_once(cfg)["sites"][0]
    radar = _radar_state(pt)
    assert radar["state"] == "watch" and "unconfirmed (frame 1 of 2)" in radar["detail"]
    assert pt["radar_check"]["confirm"] == "examined" and not pt["verdict"]["incomplete"]


def test_every_frame_of_the_span_holding_the_newest_scan_is_no_data(cfg, fake_http, monkeypatch):
    """No other scan within VLBA_RADAR_STALE before the newest frame (the radar has not
    updated): nothing confirms or refutes the echo."""
    _quiet_glm(monkeypatch)
    _late_frame(monkeypatch, 4)
    fake_http.add("lcref_", frame_png(ECHO))
    add_routes(fake_http, cfg)
    pt = mvp.run_once(cfg)["sites"][0]
    radar = _radar_state(pt)
    assert radar["state"] == "unknown" and NO_EARLIER in radar["detail"]
    assert "(no other radar scan in the 20 min before it)" in radar["detail"]


def test_the_confirm_key_in_check_radar(cfg):
    """``confirm`` "unavailable": no data with the reason; "examined" or no key: as before
    ("watch", "unconfirmed"); a confirmed streak or a hold still wins (BAD)."""
    v = d.evaluate(cfg, _entry(radar_check=_radar(True, confirm="unavailable",
                                                  confirm_reason="the frame before it could not be fetched")), {}, NOW)
    c = _radar_check(v)
    assert c["state"] == "unknown" and v["status"] == d.GOOD and v["incomplete"] and not v["reasons"]
    assert c["detail"].startswith("MRMS: %s (the frame before it could not be fetched); nearest echo 12.3 km"
                                  % NO_EARLIER)
    for extra in ({"confirm": "examined"}, {}):
        v = d.evaluate(cfg, _entry(radar_check=_radar(True, **extra)), {}, NOW)
        assert _radar_check(v)["state"] == "watch" and not v["incomplete"]
    # the stored streak confirms it (a frame before, counted 5 min ago): BAD as before
    st = {"radar_streak": 1, "radar_last_frame_ts": (NOW - timedelta(minutes=9)).timestamp(),
          "radar_last_count_ts": (NOW - timedelta(minutes=5)).timestamp()}
    v = d.evaluate(cfg, _entry(radar_check=_radar(True, confirm="unavailable")), st, NOW)
    assert _radar_check(v)["state"] == "bad"
    # a running hold: BAD
    st = {"radar_last_hit_ts": (NOW - timedelta(minutes=5)).timestamp()}
    v = d.evaluate(cfg, _entry(radar_check=_radar(True, confirm="unavailable")), st, NOW)
    assert _radar_check(v)["state"] == "bad"
    # a one-frame trigger needs no frame before: BAD at once
    cfg.radar_trigger_after = 1
    assert mvp._confirmation(cfg, [], None) == {}
    v = d.evaluate(cfg, _entry(radar_check=_radar(True)), {}, NOW)
    assert _radar_check(v)["state"] == "bad"


def test_confirmation_from_the_frames_read(cfg):
    """``_confirmation``: examined when the frames read reach a clear one or are all there."""
    clear, echo = {"in_ring": False}, {"in_ring": True}
    assert mvp._confirmation(cfg, [], "x") == {"confirm": "unavailable", "confirm_reason": "x"}
    assert mvp._confirmation(cfg, [clear], None)["confirm"] == "examined"
    assert mvp._confirmation(cfg, [echo], None)["confirm"] == "examined"
    cfg.radar_trigger_after = 3
    assert mvp._confirmation(cfg, [echo], "y") == {"confirm": "unavailable", "confirm_reason": "y"}
    assert mvp._confirmation(cfg, [clear], None)["confirm"] == "examined"
    assert mvp._confirmation(cfg, [echo, echo], None)["confirm"] == "examined"
    assert mvp._confirmation(cfg, [], None)["confirm_reason"] == "the frames before it were not read"


def test_a_reused_copy_with_an_echo_never_counted_is_no_data(cfg):
    """A copy reused after a failed fetch: no frame before it is fetched, nothing confirms it."""
    frame_res = dict(_mrms_result(ECHO), stale=True, age_s=120.0)
    run = {"errors": []}
    check = mvp._radar_for_site(cfg, None, run, PT, None, frame_res, {})["check"]
    assert check["in_ring"] and check["confirm"] == "unavailable" and "earlier" not in check
    v = d.evaluate(cfg, _entry(radar_check=check), {}, util.utcnow())
    c = _radar_check(v)
    assert c["state"] == "unknown" and NO_EARLIER in c["detail"] and "reused" in c["detail"]


# ---- #87 / #89 / #40: an island composite of unknown time -----------------------------------
def _mk_blob(km, bearing, rgb=(11, 136, 15)):
    p = geo.dest_point(MK["lat"], MK["lon"], km, bearing)
    return [(p[0], p[1], rgb)]                                      # 30 dBZ on the composite's scale


def _caps_fail(cfg, fake_http, monkeypatch, blobs):
    _quiet_glm(monkeypatch)
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result([]))
    fake_http.add("GetCapabilities", "<html>maintenance</html>")     # HTTP 200, no time in it
    add_routes(fake_http, cfg, mk_blobs=blobs)


def test_an_island_echo_of_unknown_frame_time_is_no_data(cfg, fake_http, monkeypatch):
    """#87: no frame time, so no earlier frame can be asked for and runs 30 min apart cannot
    confirm each other: the echo is "no data" with a problem of the run, never "watch"."""
    _caps_fail(cfg, fake_http, monkeypatch, _mk_blob(10.0, 90.0))
    for _ in range(2):
        run = mvp.run_once(cfg)
        mk = run["sites"][1]
        assert mk["radar_check"]["frame_ts"] is None and mk["radar_check"]["in_ring"]
        radar = _radar_state(mk)
        assert radar["state"] == "unknown" and NO_EARLIER in radar["detail"] and "frame time unknown" in radar["detail"]
        assert any("mk radar composite: frame time unknown" in e and "not confirmed by this run alone" in e
                   for e in run["errors"]), run["errors"]
        assert not any("TIME=" in c for c in fake_http.calls)


def test_an_island_composite_of_unknown_time_is_never_clear(cfg, fake_http, monkeypatch):
    """#89 / #40: without a frame time its age cannot be judged: no echo is "no data"."""
    _caps_fail(cfg, fake_http, monkeypatch, ())
    mk = mvp.run_once(cfg)["sites"][1]
    radar = _radar_state(mk)
    assert radar["state"] == "unknown" and "frame time is unknown" in radar["detail"]
    assert mk["verdict"]["incomplete"]
    v = d.evaluate(cfg, _entry(radar_check=_radar(False, frame_ts=None, age_s=0.0,
                                                  coverage={"ok": True, "covered_pct": 100.0, "ts": None})), {}, NOW)
    assert _radar_check(v)["state"] == "unknown" and v["incomplete"]


# ---- #71: a frame dated ahead of the clock, a reused copy -----------------------------------
def test_a_frame_from_the_future_is_unusable_and_a_copy_ages_with_itself(cfg):
    for ahead_min, state in ((30, "unknown"), (6 * 60, "unknown"), (11, "unknown"), (5, "ok"), (0, "ok")):
        for stale in (False, True):
            st = {}
            v = d.evaluate(cfg, _entry(radar_check=_radar(False, frame_ts=NOW + timedelta(minutes=ahead_min),
                                                          stale=stale, age_s=60.0)), st, NOW)
            c = _radar_check(v)
            assert c["state"] == state, (ahead_min, stale, c)
            if state == "unknown":
                assert "ahead of this host's clock" in c["detail"] and "radar_last_frame_ts" not in st
    # a reused copy: as old as the older of its frame time and the copy's own age
    v = d.evaluate(cfg, _entry(radar_check=_radar(False, frame_ts=NOW - timedelta(minutes=4), stale=True,
                                                  age_s=cfg.radar_stale_s + 60.0)), {}, NOW)
    assert _radar_check(v)["state"] == "unknown" and "old" in _radar_check(v)["detail"]
    v = d.evaluate(cfg, _entry(radar_check=_radar(False, frame_ts=NOW - timedelta(minutes=4), stale=True,
                                                  age_s=300.0)), {}, NOW)
    assert _radar_check(v)["state"] == "ok"
    # a fresh frame: the cache entry's age is not the frame's
    v = d.evaluate(cfg, _entry(radar_check=_radar(False, frame_ts=NOW - timedelta(minutes=4),
                                                  age_s=cfg.radar_stale_s + 60.0)), {}, NOW)
    assert _radar_check(v)["state"] == "ok"


# ---- #90: unknown colours veto only "no echo" ------------------------------------------------
def test_an_island_echo_beside_unknown_colours_counts(cfg, fake_http, monkeypatch):
    """A 30 dBZ echo 10 km out decodes with certainty; a pixel of a colour the decoder does not
    know 20 km out takes nothing from it: the echo counts (here confirmed with the frame before,
    which also holds an echo beside unknown colours), and the run says the colours changed."""
    _quiet_glm(monkeypatch)
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result([]))
    ts = island_ts()
    frame = geo.MapFrame(MK["lat"], MK["lon"], cfg.map_km, cfg.map_px, cfg.tile_zoom)
    odd = _mk_blob(20.0, 270.0, rgb=(2, 253, 2))
    fake_http.add("GetCapabilities", caps_doc([ts - timedelta(minutes=2), ts]))
    from urllib.parse import urlencode
    from vlba import nwsradar
    fake_http.add(urlencode([("TIME", nwsradar._iso_time(ts - timedelta(minutes=2)))]),
                  island_png(frame, _mk_blob(12.0, 80.0) + _mk_blob(20.0, 200.0, rgb=(2, 253, 2))))
    add_routes(fake_http, cfg, mk_blobs=_mk_blob(10.0, 90.0) + odd)
    run = mvp.run_once(cfg)
    mk = run["sites"][1]
    assert mk["radar_check"]["available"] and mk["radar_check"]["in_ring"]
    radar = _radar_state(mk)
    assert radar["state"] == "bad" and "on 2 consecutive frames" in radar["detail"], radar
    assert [c["frame_ts"] for c in mk["radar_check"]["earlier"]] == [ts - timedelta(minutes=2)]
    assert any("mk radar check:" in e and "unrecognised colour" in e and "counted" in e for e in run["errors"])

    # unknown colours alone in the ring are still "no data", never "no echo"
    fake_http.routes = []
    os.remove(cfg.state_file)
    _quiet_glm(monkeypatch)
    fake_http.add("GetCapabilities", caps_doc([ts]))
    add_routes(fake_http, cfg, mk_blobs=odd)
    mk = mvp.run_once(cfg)["sites"][1]
    assert _radar_state(mk)["state"] == "unknown" and "unrecognised colour" in _radar_state(mk)["detail"]


# ---- #88: the MRMS file holds an echo the image does not show --------------------------------
def test_an_echo_in_the_mrms_file_the_image_does_not_show_is_no_data(cfg):
    ts = NOW - timedelta(minutes=4)
    cov = {"ok": True, "covered_pct": 100.0, "ts": ts, "echo_cells": 7}
    v = d.evaluate(cfg, _entry(radar_check=_radar(False, coverage=cov)), {}, NOW)
    c = _radar_check(v)
    assert c["state"] == "unknown" and "the MRMS file shows an echo the image does not" in c["detail"]
    assert v["incomplete"]
    for cells in (0, None, "x", float("nan")):
        v = d.evaluate(cfg, _entry(radar_check=_radar(False, coverage=dict(cov, echo_cells=cells))), {}, NOW)
        assert _radar_check(v)["state"] == "ok", cells
    v = d.evaluate(cfg, _entry(radar_check=_radar(False, coverage={"ok": True, "covered_pct": 100.0, "ts": ts})),
                   {}, NOW)
    assert _radar_check(v)["state"] == "ok"                      # no key: no opinion


# ---- S21: an island ring reaching past the composite ------------------------------------------
def test_an_island_ring_reaching_past_the_composite_is_no_data(cfg, fake_http, monkeypatch):
    """The composite is fetched for the map only; a ring wider than half the map is partly
    unseen (refused at startup; here set past that check): no data, with the run's problem."""
    _quiet_glm(monkeypatch)
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: _mrms_result([]))
    add_routes(fake_http, cfg)
    cfg.radar_km = cfg.map_km / 2.0 + 1.0
    run = mvp.run_once(cfg)
    mk = run["sites"][1]
    radar = _radar_state(mk)
    assert radar["state"] == "unknown" and "reaches past the map" in radar["detail"]
    assert any("mk radar check: the %g km ring reaches past the map" % cfg.radar_km in e for e in run["errors"])
    assert mk["verdict"]["incomplete"]


# ---- S23: where the radar hold ends -----------------------------------------------------------
def test_the_radar_hold_ends_at_its_length(cfg):
    hold = float(cfg.radar_hold_s)
    st = {"radar_last_hit_ts": NOW.timestamp() - (hold - 30)}
    v = d.evaluate(cfg, _entry(radar_check=_radar(False)), st, NOW)
    assert _radar_check(v)["state"] == "bad" and v["status"] == d.BAD and "hold" in v["reasons"][0]
    st = {"radar_last_hit_ts": NOW.timestamp() - (hold + 30)}
    v = d.evaluate(cfg, _entry(radar_check=_radar(False)), st, NOW)
    assert _radar_check(v)["state"] == "ok" and v["status"] == d.GOOD


# ---- S24: the coverage limit, a failed coverage with a number ---------------------------------
def test_coverage_just_under_the_limit_and_a_failed_coverage_are_no_data(cfg):
    ts = NOW - timedelta(minutes=4)
    lim = float(cfg.radar_min_coverage)
    for pct, state in ((lim - 1, "unknown"), (lim - 0.1, "unknown"), (lim, "ok")):
        v = d.evaluate(cfg, _entry(radar_check=_radar(False, coverage={"ok": True, "covered_pct": pct, "ts": ts})),
                       {}, NOW)
        assert _radar_check(v)["state"] == state, pct
    v = d.evaluate(cfg, _entry(radar_check=_radar(False, coverage={"ok": False, "covered_pct": 100.0, "ts": ts,
                                                                   "error": "HTTP 503"})), {}, NOW)
    assert _radar_check(v)["state"] == "unknown" and "HTTP 503" in _radar_check(v)["detail"]


# ---- S27: odd stored stamps -------------------------------------------------------------------
@pytest.mark.parametrize("key", ["radar_last_hit_ts", "radar_last_frame_ts", "radar_last_count_ts",
                                 "radar_streak"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, -1e12, -3])
def test_odd_stored_radar_stamps_never_hold_or_block(cfg, key, bad):
    """A state file holding NaN (json.load accepts it), infinity or a negative number for any
    radar key: no eternal hold, and an echo on two consecutive frames of this run is BAD."""
    newest = NOW - timedelta(minutes=4)
    older = _radar(True, frame_ts=newest - timedelta(minutes=6), confirms_ts=newest)
    st = {key: bad}
    v = d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=newest, earlier=[older])), st, NOW)
    assert _radar_check(v)["state"] == "bad", (key, bad)
    st = {key: bad}
    v = d.evaluate(cfg, _entry(radar_check=_radar(False)), st, NOW)
    assert _radar_check(v)["state"] == "ok" and v["status"] == d.GOOD, (key, bad)


def test_a_negative_stored_streak_and_a_future_count_stamp_confirm_nothing(cfg):
    """S27, the two guards the case above cannot reach. A negative stored streak is no count:
    with a recent frame stored (no reset), an echo on two consecutive frames of this run is
    BAD, not one short of it. A stored count stamp from the future is no recent count: a
    repeat of the scan counted last does not take the stored streak for its own."""
    newest = NOW - timedelta(minutes=4)
    older = _radar(True, frame_ts=newest - timedelta(minutes=6), confirms_ts=newest)
    for streak in (-1, -5):
        st = {"radar_streak": streak, "radar_last_frame_ts": (newest - timedelta(minutes=12)).timestamp(),
              "radar_last_count_ts": (NOW - timedelta(minutes=10)).timestamp()}
        v = d.evaluate(cfg, _entry(radar_check=_radar(True, frame_ts=newest, earlier=[older])), st, NOW)
        assert _radar_check(v)["state"] == "bad" and st["radar_streak"] == 2, (streak, st)
    for ahead in (timedelta(minutes=5), timedelta(days=1)):
        st = {"radar_streak": 2, "radar_last_digest": "scan-1", "radar_last_count_ts": (NOW + ahead).timestamp()}
        v = d.evaluate(cfg, _entry(radar_check=_radar(True, digest="scan-1")), st, NOW)
        c = _radar_check(v)
        assert c["state"] == "watch" and "not counted again" in c["detail"] and not v["reasons"], (ahead, c)
