"""Fixes in weather/roads.py (the parent library's road layer; this page never calls it):
time bounds on hostile feed text (#161, #165), one malformed geometry no longer discards the
whole NMDOT file (#166), a broken nmroads.json / rss.xml join is reported (#162), and the
expiration date and future-dated times are honoured (#163)."""
from __future__ import annotations

import json
import signal
import time
from contextlib import contextmanager
from datetime import timedelta
from xml.sax.saxutils import escape

import pytest

from weather import page as wpage
from weather import roads, util


@pytest.fixture
def rcfg(cfg):
    """The test config with the road layer on (this page ships it off)."""
    cfg.roads_enabled = True
    cfg.lsr_enabled = True
    return cfg


class _Slow(Exception):
    pass


@contextmanager
def time_limit(seconds):
    """Fail (instead of hanging the suite) when the block runs longer than ``seconds``."""
    def _raise(*_a):
        raise _Slow()
    old = signal.signal(signal.SIGALRM, _raise)
    signal.alarm(seconds)
    t0 = time.monotonic()
    try:
        yield
    except _Slow:
        pytest.fail("took longer than %d s" % seconds)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert time.monotonic() - t0 < seconds


def _local(dt):
    return util.to_local(dt, roads.NM_TZ).strftime("%Y-%m-%d %H:%M:%S.0")


def rss(items):
    return ("<?xml version='1.0'?><rss xmlns:geo='http://www.w3.org/2003/01/geo/wgs84_pos#' "
            "xmlns:osow='http://nmroads.com/osow/'><channel><lastBuildDate>Thu, 12 Nov 2026 "
            "10:00:00 -0700</lastBuildDate>%s</channel></rss>" % "".join(items)).encode("utf-8")


def item(guid, cat, title, desc, post=None, upd=None, exp=None, lat=34.3, lon=-108.1):
    now = util.utcnow()
    d = "Title: %s<br/>Description: %s<br/>Post Date: %s<br/>Update Date: %s" % (
        title, desc, _local(post or now - timedelta(hours=3)),
        _local(upd or now - timedelta(hours=2)))
    if exp is not None:
        d += "<br/>Expiration Date: %s" % _local(exp)
    return ("<item><guid>%s</guid><category>%s</category><title>%s - %s</title>"
            "<description>%s</description><geo:lat>%s</geo:lat><geo:long>%s</geo:long></item>"
            % (guid, cat, cat, escape(title), escape(d), lat, lon))


def feature(fid, title, coords=None, gtype="LineString", cat="Closure"):
    return {"type": "Feature", "id": str(fid),
            "geometry": {"type": gtype, "coordinates": (coords if coords is not None else
                                                        [[-108.1, 34.3], [-108.0, 34.3]])},
            "properties": {"core_details": {"name": cat, "description": title,
                                            "road_names": ["NM-12"],
                                            "creation_date": "2026-11-01T08:00:00Z"}}}


def fc(feats):
    return json.dumps({"type": "FeatureCollection", "features": feats}).encode("utf-8")


T1 = "Closure, NM 12 from mile marker 1 to mile marker 5. Roadway closed."


def fetch(cfg, cache, fake_http, jbody=None, rbody=None):
    if jbody is None:
        fake_http.fail("nmroads.json")
    else:
        fake_http.add("nmroads.json", jbody)
    if rbody is None:
        fake_http.fail("rss.xml")
    else:
        fake_http.add("rss.xml", rbody)
    return roads.fetch_nm_roads(cfg, cache)


# ---- #161 exponential regex in make_cause -------------------------------------------------
def test_make_cause_non_ascii_repeated_title_is_fast():
    title = " ".join(["aña"] * 260)                       # about 1 kB
    with time_limit(5):
        roads.make_cause([title + ", due to flooding."], title)
    t = " ".join(["aña"] * 40)
    with time_limit(5):
        items, _ = roads.parse_rss(rss([item("g1", "Closure", t, t)]))
        roads.build_event(None, items["g1"])


def test_make_cause_still_strips_the_repeated_title():
    title = "Closure, NM 273 from mile marker 0 to mile marker 3, State Line."
    got = roads.make_cause([title + ", due to flooding."], title)
    assert got == "Due to flooding."
    # words with other letters are cut the way _norm counts them
    got = roads.make_cause(["Cañon Rd closed., due to a crash."], "Cañon Rd closed.")
    assert got == "Due to a crash."
    # a segment that does not start with the title is kept whole
    assert roads.make_cause(["Closed due to a crash."], "Closure, NM 1") == \
        "Closed due to a crash."


# ---- #165 quadratic scans, no size caps ---------------------------------------------------
@pytest.mark.parametrize("fn,arg", [
    (roads._sentences, "a.m. " * 40000),
    (roads._clean_title, ", " * 80000),
    (roads.clean_text, "<" * 400000),
], ids=["sentences", "clean_title", "clean_text"])
def test_long_texts_are_bounded(fn, arg):
    with time_limit(5):
        fn(arg)


def test_classify_and_storm_remarks_are_bounded():
    with time_limit(5):
        roads.classify_event("Closure", "x", "a lane " * 40000)
    with time_limit(5):
        roads.classify_event("Closure", "a lane " * 40000, "")
    f = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-108.1, 34.3]},
         "properties": {"state": "NM", "valid": "2026-11-12T10:00:00Z", "typetext": "FLOOD",
                        "remark": "NM 12 closed. " + "a.m. " * 40000}}
    with time_limit(5):
        roads.lsr_road_reports([f])


def test_sentence_split_keeps_abbreviations():
    assert roads._sentences("Closed at 3 a.m. Tuesday. Due to a crash.") == \
        ["Closed at 3 a.m. Tuesday.", "Due to a crash."]
    assert roads._sentences("Near St. Francis Dr. Crash. Use caution.") == \
        ["Near St. Francis Dr. Crash.", "Use caution."]
    assert roads._sentences("A verylongwordhere. Next one.") == \
        ["A verylongwordhere.", "Next one."]


def test_event_count_is_capped(rcfg, cache, fake_http, monkeypatch):
    monkeypatch.setattr(roads, "NM_MAX_EVENTS", 5)
    monkeypatch.setattr(roads, "NM_MAX_LISTED", 3)
    feats = [feature(i, "Closure, NM %d. Roadway closed due to a crash." % i,
                     coords=[[-108.1 + i * 0.01, 34.3], [-108.0 + i * 0.01, 34.3]])
             for i in range(8)]
    res = fetch(rcfg, cache, fake_http, jbody=fc(feats))
    assert res["ok"] and res["count_raw"] == 5
    assert len(res["events"]) == 3
    assert "only the first 5 read" in res["error"]
    assert "only the first 3 listed" in res["error"]


def test_storm_report_count_is_capped(rcfg, cache, fake_http, monkeypatch):
    monkeypatch.setattr(roads, "LSR_MAX_FEATURES", 2)
    now = util.utcnow()
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [-108.1 + i, 34.3]},
              "properties": {"state": "NM", "typetext": "FLASH FLOOD", "wfo": "ABQ",
                             "valid": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "remark": "NM %d closed due to flooding." % i}} for i in range(4)]
    fake_http.add("lsr.geojson", {"type": "FeatureCollection", "features": feats})
    res = roads.fetch_storm_reports(rcfg, cache)
    assert res["ok"] and len(res["reports"]) == 2
    assert "only the first 2 read" in res["error"]


# ---- #166 one malformed geometry ---------------------------------------------------------
@pytest.mark.parametrize("geom", [
    {"type": "LineString", "coordinates": 5},
    {"type": "MultiLineString", "coordinates": [5]},
    {"type": "MultiPoint", "coordinates": None},
    {"type": "Point", "coordinates": "x"},
])
def test_bad_geometry_skips_one_feature(rcfg, cache, fake_http, geom):
    good = feature(1, "Closure, NM 12. Roadway closed due to a crash.")
    bad = dict(feature(2, "Closure, NM 13. Roadway closed due to a crash."), geometry=geom)
    res = fetch(rcfg, cache, fake_http, jbody=fc([good, bad]))
    assert res["ok"] is True
    assert [e["id"] for e in res["events"]] == ["1"]
    assert "TypeError" not in (res["error"] or "")


def test_clean_geometry_keeps_good_parts():
    g = roads.clean_geometry({"type": "MultiLineString",
                              "coordinates": [5, [[-108.1, 34.3], [-108.0, 34.3]], None]})
    assert g == {"type": "MultiLineString", "coordinates": [[[-108.1, 34.3], [-108.0, 34.3]]]}
    assert roads.clean_geometry({"type": "LineString", "coordinates": 5}) is None


# ---- #162 the json / rss join ------------------------------------------------------------
N1 = "Roadway closed nightly for bridge deck replacement project."


def test_matching_ids_no_warning(rcfg, cache, fake_http):
    res = fetch(rcfg, cache, fake_http, jbody=fc([feature(7, T1)]),
                rbody=rss([item("7", "Closure", T1, N1)]))
    assert res["ok"] and res["error"] is None
    assert res["events"] == [] and res["excluded"] == {"Closure": 1}


def test_broken_join_is_reported(rcfg, cache, fake_http):
    res = fetch(rcfg, cache, fake_http, jbody=fc([feature(7, T1)]),
                rbody=rss([item("https://nmroads.com/event/7", "Closure", T1, N1)]))
    assert res["ok"] and not res["stale"]
    assert "not found in rss.xml" in res["error"]
    assert "1 listed without the rss.xml text" in res["error"]
    assert "feed incomplete" in wpage._roads_feed_html(res)


def test_join_share_threshold(rcfg, cache, fake_http):
    """One excluded event missing from the other file (a copy a little older) is no alarm;
    most of them missing is."""
    feats = [feature(i, "Roadwork, NM %d." % i, cat="Roadwork",
                     coords=[[-108.1 + i * 0.01, 34.3], [-108.0 + i * 0.01, 34.3]])
             for i in range(20)]
    items = [item(str(i), "Roadwork", "Roadwork, NM %d." % i, "Paving.") for i in range(1, 20)]
    res = fetch(rcfg, cache, fake_http, jbody=fc(feats), rbody=rss(items))
    assert res["error"] is None


def test_join_share_threshold_broken(rcfg, cache, fake_http):
    feats = [feature(i, "Roadwork, NM %d." % i, cat="Roadwork",
                     coords=[[-108.1 + i * 0.01, 34.3], [-108.0 + i * 0.01, 34.3]])
             for i in range(20)]
    items = [item("x%d" % i, "Roadwork", "Roadwork, NM %d." % i, "Paving.") for i in range(20)]
    res = fetch(rcfg, cache, fake_http, jbody=fc(feats), rbody=rss(items))
    assert "20 of 20 events not found in rss.xml" in res["error"]


# ---- #163 expiration date and future times -----------------------------------------------
def test_expiration_date_is_kept_and_honoured(rcfg, cache, fake_http):
    now = util.utcnow()
    t2 = T1.replace("NM 12", "NM 13")
    t3 = T1.replace("NM 12", "NM 14")
    body = rss([
        item("e1", "Closure", T1, "Closed due to flooding.", post=now - timedelta(days=2),
             upd=now - timedelta(days=1), exp=now - timedelta(hours=20)),
        item("e2", "Closure", t2, "Closed due to a crash.", exp=now + timedelta(hours=5)),
        item("e3", "Closure", t3, "Closed due to a crash.",
             exp=now - timedelta(minutes=20), lat=34.5),
    ])
    items, _ = roads.parse_rss(body)
    assert items["e1"]["expires"] is not None
    assert abs((items["e1"]["expires"] - (now - timedelta(hours=20))).total_seconds()) < 2
    res = fetch(rcfg, cache, fake_http, rbody=body)
    assert sorted(e["id"] for e in res["events"]) == ["e2", "e3"]   # e3: within the grace
    assert res["excluded"].get("expired") == 1


def test_future_times_are_not_given(rcfg, cache, fake_http):
    now = util.utcnow()
    items, _ = roads.parse_rss(rss([
        item("f1", "Closure", T1, "Closed due to a crash.", post=now + timedelta(days=3),
             upd=now + timedelta(days=3)),
        item("f2", "Closure", T1, "Closed due to a crash.", post=now - timedelta(hours=2),
             upd=now + timedelta(minutes=2)),
    ]))
    ev = roads.build_event(None, items["f1"], now=now)[0]
    assert ev["updated"] is None and ev["posted"] is None
    html = wpage._road_event_html(rcfg, ev, roads.NM_TZ, now, set())
    assert "just now" not in html and "time not given" in html
    ev = roads.build_event(None, items["f2"], now=now)[0]      # a small clock offset: kept
    assert ev["updated"] is not None and ev["posted"] is not None


def test_future_json_creation_date_is_dropped():
    now = util.utcnow()
    f = roads.parse_json(fc([dict(feature(1, T1))]))[0]
    f["created"] = now + timedelta(days=2)
    ev = roads.build_event(f, None, now=now)[0]
    assert ev["posted"] is None and ev["time_approx"] is False


def test_future_storm_reports_are_left_out(rcfg, cache, fake_http):
    now = util.utcnow()

    def rep(dt, n):
        return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-108.1 + n, 34.3]},
                "properties": {"state": "NM", "typetext": "FLASH FLOOD", "wfo": "ABQ",
                               "valid": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "remark": "NM %d closed due to flooding." % n}}
    fake_http.add("lsr.geojson", {"type": "FeatureCollection", "features": [
        rep(now + timedelta(days=30), 1), rep(now - timedelta(hours=1), 2),
        rep(now + timedelta(minutes=3), 3)]})
    res = roads.fetch_storm_reports(rcfg, cache)
    assert res["ok"]
    assert sorted(r["remark"] for r in res["reports"]) == [
        "NM 2 closed due to flooding.", "NM 3 closed due to flooding."]


def test_classification_time_is_budgeted(rcfg, cache, fake_http, monkeypatch):
    monkeypatch.setattr(roads, "NM_TIME_BUDGET", -1.0)
    feats = [feature(i, "Closure, NM %d. Roadway closed due to a crash." % i,
                     coords=[[-108.1 + i * 0.01, 34.3], [-108.0 + i * 0.01, 34.3]])
             for i in range(3)]
    res = fetch(rcfg, cache, fake_http, jbody=fc(feats))
    assert res["ok"] and "processing stopped" in res["error"]
    assert len(res["events"]) < 3


# ---- review ------------------------------------------------------------------------------
def test_orphan_outside_region_does_not_mark_a_listed_event(rcfg, cache, fake_http):
    """An orphan feature outside New Mexico is never kept; the id of its freed event must
    not make a later event with its rss.xml text count as "listed without the text"."""
    far = [feature(100 + i, "Closure, far away %d." % i, coords=[[-80.0, 40.0], [-79.9, 40.0]])
           for i in range(3)]
    titles = [T1.replace("NM 12", "NM %d" % (20 + i)) for i in range(5)]
    feats = far + [feature(i, titles[i], coords=[[-108.1, 34.3 + i * 0.01],
                                                 [-108.0, 34.3 + i * 0.01]])
                   for i in range(5)]
    items = [item(str(i), "Closure", titles[i], "Roadway closed due to flooding.",
                  lat=34.3 + i * 0.01, lon=-108.05) for i in range(5)]
    res = fetch(rcfg, cache, fake_http, jbody=fc(feats), rbody=rss(items))
    assert len(res["events"]) == 5
    assert res["error"] is None


def test_expired_leftover_does_not_hide_a_live_duplicate(rcfg, cache, fake_http):
    """Two events with the same title at the same place: an expired one updated later must
    not win the duplicate check and take the live one with it."""
    now = util.utcnow()
    body = rss([
        item("old", "Closure", T1, "Closed due to flooding.", post=now - timedelta(days=2),
             upd=now - timedelta(hours=21), exp=now - timedelta(hours=20)),
        item("new", "Closure", T1, "Closed due to flooding.", post=now - timedelta(hours=30),
             upd=now - timedelta(hours=25)),
    ])
    res = fetch(rcfg, cache, fake_http, rbody=body)
    assert [e["id"] for e in res["events"]] == ["new"]
    assert res["excluded"].get("expired") == 1


def test_event_updated_after_its_expiration_stays_listed(rcfg, cache, fake_http):
    """An update after the expiration date means the closure was extended without a new
    date: unknown, so it stays listed."""
    now = util.utcnow()
    body = rss([item("x", "Closure", T1, "Closed due to flooding.", post=now - timedelta(days=2),
                     upd=now - timedelta(hours=3), exp=now - timedelta(hours=20))])
    res = fetch(rcfg, cache, fake_http, rbody=body)
    assert [e["id"] for e in res["events"]] == ["x"]
    assert "expired" not in res["excluded"]
