"""The VLBA page: summary table, verdict cards, navigation, GLM block, the island radar
caption, the sun statement, escaping, status.json, and that older / partial run dicts and
garbage still render without exceptions."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import pytest

from vlba import decision, page
from vlba.sites import SITES

UTC = timezone.utc
GEN = datetime(2026, 10, 8, 16, 12, tzinfo=UTC)
FRAME_TS = datetime(2026, 10, 8, 16, 6, tzinfo=UTC)
EVIL = "Flash Flood Warning <script>alert(1)</script> & co"


def _status(ok=True, stale=False, error=None, age_s=0.0):
    return {"ok": ok, "stale": stale, "error": error, "age_s": age_s}


def _check(key, state, value="", limit="", detail="", stale=False):
    return {"key": key, "label": dict(decision.CHECK_LABELS)[key], "state": state, "value": value,
            "limit": limit, "detail": detail, "stale": stale}


def _verdict(status, reasons=(), missing=(), scheduled=(), checks=None):
    checks = checks or [_check(k, "ok", "clear", "x") for k in decision.CHECK_KEYS]
    return {"status": status, "incomplete": bool(missing), "reasons": list(reasons), "missing": list(missing),
            "checks": checks, "scheduled": list(scheduled), "evaluated": GEN}


def _alert(**over):
    a = {"id": "urn:x:1", "event": "Flash Flood Warning", "severity": "Severe", "urgency": "Immediate",
         "certainty": "Likely", "kind": "warning", "rank": 1, "color": "#8B0000", "headline": EVIL,
         "description": "Heavy rain.", "instruction": None, "area_desc": "Catron, NM", "sender": "NWS Albuquerque NM",
         "web": None, "message_type": "Alert", "status": "Actual", "sent": None, "effective": None,
         "onset": None, "ends": "2026-10-08T22:00:00+00:00", "expires": None,
         "end_dt": datetime(2026, 10, 8, 22, 0, tzinfo=UTC), "threat": None, "nws_headline": None,
         "affected_zone_urls": [], "affected_zone_ids": [], "geometry": None, "geometry_source": None, "bbox": None}
    a.update(over)
    return a


def _sun(alt, tz):
    now_local = GEN.astimezone(timezone.utc)
    return {"tz": tz, "now_local": now_local, "sun_alt_deg": alt, "phase": "day" if alt > 0 else "night",
            "is_night": alt < -6, "today": {"sunrise": GEN - timedelta(hours=4), "solar_noon": GEN + timedelta(hours=2),
                                            "sunset": GEN + timedelta(hours=8)},
            "tomorrow": {}, "upcoming": [{"label": "Sunset", "dt": GEN + timedelta(hours=8), "passed": False}],
            "evening": [], "morning": []}


def _site_entry(site, verdict, sun_alt=30.0, island=None, glm=None, maps_ok=True):
    slug = site["slug"]
    tz = site["tz"]
    e = {"site": site, "slug": slug, "tz": tz,
         "meta": dict(_status(), grid_id="ABQ", grid_x=1, grid_y=2, office="ABQ", tz=tz, forecast_zone="NMZ220",
                      county_zone="NMC053", fire_zone="NMZ106", city="x", state="NM", radar_station="KABX",
                      forecast_url=None, hourly_url=None, stations_url=None, zone_urls=[], no_grid=False),
         "obs": dict(_status(), station_id="KONM", station_name="Socorro", timestamp="2026-10-08T15:55:00+00:00",
                     age_min=17.0, text="Clear", icon=None, temp_f=70.0, temp_c=21.1, dewpoint_f=40.0, dewpoint_c=4.4,
                     rh=35.0, wind_mph=8.0, wind_kmh=12.9, gust_mph=None, gust_kmh=None, wind_dir_deg=180, wind_dir="S",
                     pressure_inhg=30.01, pressure_hpa=1016.0, visibility_mi=10.0, visibility_km=16.1,
                     heat_index_f=None, wind_chill_f=None, cloud_layers=["CLR"]),
         "obs_station": {"id": "KONM", "name": "Socorro", "lat": 34.0, "lon": -106.9, "elev_m": 1400.0,
                         "dist_km": 12.3, "delev_m": -900.0, "usable": True, "why": None},
         "obs_raw": {"present_weather": [], "precip_last_hour_mm": 0.0, "text": "Clear"},
         "forecast": dict(_status(), updated=None, periods=[
             {"number": 1, "name": "Today", "start": None, "end": None, "is_day": True, "temp_f": 70,
              "temp_c": 21, "temp_trend": None, "pop": 10, "wind": "5 mph", "wind_dir": "S",
              "short": "Sunny", "detailed": "Sunny.", "icon": None}]),
         "hourly": dict(_status(), updated=None, hours=[
             {"start": None, "local": "Thu 10:00", "day": "Thu", "hour": "10:00", "is_day": True,
              "temp_f": 70, "temp_c": 21, "pop": 10, "rh": 30, "dewpoint_f": 40, "dewpoint_c": 4,
              "wind_mph": 8, "wind_kmh": 13, "wind_dir": "S", "short": "Sunny", "icon": None}]),
         "gridpoint": dict(_status(), hours=[], now=None, next=None, missing=[], update_age_min=10.0),
         "sun": _sun(sun_alt, tz), "sun_up": sun_alt >= -0.833,
         "alerts_feed": _status(), "alerts_at": [], "alerts_near": [], "alerts_drawn_ids": [],
         "radar_check": {"available": True, "stale": False, "source": "MRMS", "frame_ts": FRAME_TS, "in_ring": False,
                         "nearest_km": None, "count": 0, "max_dbz": None, "error": None},
         "glm": glm or {"covered": True, "sats": ["G19", "G18"], "available": True, "partial": False, "error": None,
                        "count_window": 0, "nearest_km": 312.7, "nearest_bearing": "SE", "nearest_t": GEN,
                        "nearest_sat": "G19", "flashes_on_map": [], "window_min": 5},
         "verdict": verdict,
         "maps": {t: {"basename": "radar_%s_%s.png" % (slug, t), "ok": maps_ok, "error": None if maps_ok else "boom"}
                  for t in ("dark", "light")},
         "map_notes": []}
    if island is not None:
        e["island_radar"] = island
    return e


@pytest.fixture
def run(cfg):
    pt = next(dict(s) for s in SITES if s["slug"] == "pt")
    mk = next(dict(s) for s in SITES if s["slug"] == "mk")
    bad = _verdict(decision.BAD, reasons=["rain on radar within 30 km (nearest echo 12.3 km)", "humidity 97 % at KONM"],
                   checks=[_check("warning", "ok", "none", "x", "none in effect"),
                           _check("radar", "bad", "echo in the ring", "20 dBZ within 30 km", "MRMS: echo <b>bold</b>"),
                           _check("glm", "ok", "none", "a flash within 50 km")]
                   + [_check(k, "ok") for k in decision.CHECK_KEYS[3:]])
    good_inc = _verdict(decision.GOOD, missing=["Lightning (GOES GLM)"], scheduled=["NWS High Wind Warning scheduled from Fri 10:00 HST"])
    e_pt = _site_entry(pt, bad, sun_alt=35.0)
    e_pt["alerts_at"] = [_alert()]
    e_mk = _site_entry(mk, good_inc, sun_alt=-12.0,
                       island=dict(_status(), ts=FRAME_TS, url="https://opengeo.example/x", label="NWS Hawaii composite",
                                   source="hawaii_bref_qcd", region="hawaii"),
                       glm={"covered": True, "sats": ["G18"], "available": False, "partial": False,
                            "error": "deadline reached", "count_window": 0, "nearest_km": None, "nearest_bearing": None,
                            "nearest_t": None, "nearest_sat": None, "flashes_on_map": [], "window_min": 5})
    return {"generated": GEN, "generated_ts": GEN.timestamp(), "night_default": False,
            "alerts": dict(_status(), fetched_ts=GEN.timestamp(), alerts=[_alert()], count_raw=1),
            "radar": dict(_status(), ts=FRAME_TS, url="https://iem.example/f.png"),
            "glm": dict(_status(), window_start=GEN - timedelta(minutes=5), window_end=GEN, flash_count=12,
                        sats={"G19": {"ok": True, "selected": 15, "fetched": 15}, "G18": {"ok": False, "selected": 15, "fetched": 3}},
                        deps=True, elapsed_s=4.0, skipped=None, partial=True),
            "network": {"budget_s": 150, "left_s": 100.0, "exhausted": False, "down_hosts": []},
            "errors": [], "sites": [e_pt, e_mk], "summary": {"good": 1, "bad": 1, "no_data": 0, "incomplete": 1}}


class _Checker(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.errors = []
        self.scripts = 0

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.scripts += 1
        if tag not in ("br", "img", "meta", "link", "input", "hr"):
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:
            self.errors.append("unbalanced </%s>" % tag)


def test_page_renders_the_layout(cfg, run):
    html = page.render_html(cfg, run)
    assert "could not be rendered" not in html and "Page generation failed" not in html
    c = _Checker()
    c.feed(html)
    assert not c.errors and c.scripts == 1 and not c.stack[1:]      # one <script> (PAGE_JS), tags balanced
    # order: masthead, summary, nav, banner, PT section, MK section, footer
    for a, b in (("Weather at the sites", 'class="nav"'), ('class="nav"', 'id="alerts"'),
                 ('id="site-pt"', 'id="site-mk"'), ('id="site-mk"', "<h2>Sources</h2>")):
        assert html.index(a) < html.index(b)
    # the summary table: both antennas, badges, reasons, sun chips, local times
    assert '<span class="vbadge bad sm">BAD</span>' in html
    assert '<span class="vbadge caution sm" title="inputs missing: Lightning (GOES GLM)">GOOD?</span>' in html
    assert "rain on radar within 30 km" in html and "+" not in html.split("<table class=\"sum\">")[1].split("</table>")[0].split("humidity 97")[0][-5:]
    assert "Sun up 35" in html and "Sun down -12" in html
    assert "<b>1</b> of 2 sites GOOD, <b>1</b> BAD, 1 with inputs missing." in html
    assert html.count("UTC") >= 2 and "local times in each row" in html
    # at phone width the summary's local-time column is hidden, and so is the note about it
    assert '<span class="lt"> · local times in each row</span>' in html
    assert "table.sum td.lt, .when .lt { display: none; }" in html
    # the nav: one pill per antenna with the verdict colour
    assert '<span class="dot bad"></span>PT' in html and '<span class="dot caution"></span>MK' in html
    # the verdict cards
    assert '<div class="vcard bad">' in html and '<div class="vcard caution">' in html
    # the summary names what a GOOD verdict still has pending, never "all checks clear"
    summary = html.split('<table class="sum">')[1].split("</table>")[0]
    assert ('<span class="watch">NWS High Wind Warning scheduled from Fri 10:00 HST; inputs missing: '
            'Lightning (GOES GLM)</span>') in summary and "all checks clear" not in summary
    assert "<li>humidity 97 % at KONM</li>" in html
    assert "Inputs missing this run (not counted as clear): Lightning (GOES GLM)." in html
    assert "NWS High Wind Warning scheduled from Fri 10:00 HST" in html
    assert "MRMS: echo &lt;b&gt;bold&lt;/b&gt;" in html         # check detail escaped
    assert '<span class="cdot bad"></span>BAD' in html and '<span class="cdot ok"></span>clear' in html
    # the island caption and the MRMS caption
    assert "NWS Hawaii composite 16:06Z" in html and "MRMS frame 16:06Z" in html
    assert "solid ring 30 km (radar check), dashed ring 50 km (lightning check)" in html
    # GLM block: tiles for PT, unavailable for MK
    assert "flashes in the last 5 min" in html and "312.7 km SE" in html
    assert "GLM lightning unavailable: deadline reached" in html
    # sun statement
    assert "The sun is above the horizon." in html and "The sun is below the horizon." in html
    assert "for information only" in html
    # station note
    assert "Station KONM: 12.3 km from the antenna, 900 m lower; used for the verdict." in html
    # escaping of the alert headline
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # footer: GLM pill with partial, MK radar pill, credits, cuts
    assert "GLM lightning:" in html and "G18 3/15 granules" in html
    assert "MK radar (NWS Hawaii composite): fresh" in html
    assert "Geostationary Lightning Mapper" in html and "ttustatus" in html
    assert "page reloads every 300 s" in html and "refreshes every" not in html


def test_status_json_carries_verdicts(cfg, run):
    os.makedirs(cfg.out_dir, exist_ok=True)
    page.write_page(cfg, run)
    path = page.write_status_json(cfg, run)
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["page_written"] is True
    assert doc["verdicts"]["pt"]["status"] == "BAD" and doc["verdicts"]["pt"]["checks"]["radar"] == "bad"
    assert doc["verdicts"]["mk"]["incomplete"] is True and doc["verdicts"]["mk"]["missing"] == ["Lightning (GOES GLM)"]
    assert doc["summary"] == {"good": 1, "bad": 1, "no_data": 0, "incomplete": 1}
    assert doc["sites"]["mk"]["island_radar_ok"] is True and doc["sites"]["pt"]["sun_up"] is True
    assert doc["sites"]["mk"]["verdict"] == "GOOD"
    assert doc["glm"]["partial"] is True and doc["glm"]["flashes"] == 12
    assert "GLM lightning: partial" in doc["problems"] and doc["degraded"] is True and doc["ok"] is False


def test_status_json_island_and_glm_problems(cfg, run):
    os.makedirs(cfg.out_dir, exist_ok=True)
    run["sites"][1]["island_radar"] = dict(_status(ok=False, error="HTTP 502"), ts=None, label="NWS Hawaii composite")
    run["glm"] = dict(_status(ok=False, error="all failed"), skipped=None, partial=False, flash_count=0, sats={})
    doc = page.status_doc(cfg, run)
    assert "mk radar composite: unavailable" in doc["problems"] and "GLM lightning: unavailable" in doc["problems"]
    run["glm"]["skipped"] = "GLM layer disabled (VLBA_GLM=0)"
    doc = page.status_doc(cfg, run)
    assert "GLM lightning: unavailable" not in doc["problems"]


def test_night_default_and_glm_off(cfg, run):
    cfg.glm_enabled = False
    run["night_default"] = True
    html = page.render_html(cfg, run)
    assert 'class="page night"' in html and "GLM layer switched off" in html


def test_older_and_partial_run_dicts_render(cfg, run):
    for s in run["sites"]:
        for key in ("verdict", "glm", "island_radar", "obs_station", "obs_raw", "sun_up", "gridpoint", "radar_check"):
            s.pop(key, None)
    run.pop("glm")
    run.pop("summary")
    html = page.render_html(cfg, run)
    assert "could not be rendered" not in html
    assert "NO VERDICT" in html and "No GLM result this run" in html
    assert "<b>0</b> of 2 sites GOOD, <b>0</b> BAD, 2 without data." in html


def test_garbage_never_raises(cfg):
    for run in (None, {}, {"sites": [None, 3, {"site": None, "verdict": "x", "glm": 5}]},
                {"sites": [{"site": {"slug": "x"}, "verdict": {"status": "BAD", "checks": [None, {"key": 1}],
                                                               "reasons": [None], "missing": 7}}]}):
        html = page.render_html(cfg, run)
        assert "<title>VLBA, VLA and GBT weather</title>" in html
        page.status_doc(cfg, run)


def test_verdict_badge_variants():
    assert page._badge({"status": "GOOD", "incomplete": False}) == '<span class="vbadge good">GOOD</span>'
    assert "GOOD?" in page._badge({"status": "GOOD", "incomplete": True, "missing": ["a"]})
    assert "NO VERDICT" in page._badge({})
    assert page._short_reasons({"status": "GOOD", "reasons": [], "incomplete": False}) == "all checks clear"
    assert page._short_reasons({"status": "BAD", "reasons": ["a", "b", "c", "d"]}) == "a; b; c; +1 more"


def test_check_row_words():
    for state, word in (("ok", "clear"), ("bad", "BAD"), ("watch", "watch"), ("unknown", "no data"), ("off", "off"),
                        ("scheduled", "scheduled"), ("not provided", "n/a")):
        assert word in page._check_row(_check("radar", state))
    assert re.search(r'chip warn">stale', page._check_row(_check("radar", "ok", stale=True)))


def test_colours_of_no_data_and_of_the_sun(cfg, run):
    """Asked 2026-10-12: a check without data gets the amber dot of a caution (not the grey of
    n/a and off); the sun chips are information only and take no caution colour."""
    assert '<span class="cdot unknown"></span>no data' in page._check_row(_check("radar", "unknown"))
    for state in ("off", "not provided"):
        assert '<span class="cdot "></span>' in page._check_row(_check("radar", state))
    html = page.render_html(cfg, run)
    assert ".cdot.watch, .cdot.scheduled, .cdot.unknown { background: var(--warn); }" in html
    rule = re.search(r"\.sunchip\.up \{([^}]*)\}", html).group(1)
    assert "warn" not in rule and "var(--ink)" in rule
    assert '<span class="sunchip up"' in html and '<span class="sunchip" title' in html


def test_radar_coverage_in_the_footer_and_status(cfg, run):
    os.makedirs(cfg.out_dir, exist_ok=True)
    run["radar_coverage"] = {
        "CONUS": {"ok": True, "error": None, "ts": FRAME_TS, "sites": {"pt": {"cells": 2774, "uncovered": 1000,
                                                                              "covered_pct": 63.95}}},
        "HAWAII": {"ok": False, "error": "HTTP 503 for https://noaa-mrms-pds/x", "ts": None, "sites": {}}}
    html = page.render_html(cfg, run)
    assert '<span class="dot warnc"></span>Radar coverage (MRMS CONUS): %s · PT ring 64 %% covered' % (
        FRAME_TS.strftime("%H:%MZ")) in html
    assert '<span class="dot off"></span>Radar coverage (MRMS HAWAII): unavailable · HTTP 503' in html
    doc = page.status_doc(cfg, run)
    assert doc["radar_coverage"]["CONUS"] == {"ok": True, "ts": FRAME_TS.isoformat(), "error": None,
                                              "covered_pct": {"pt": 63.95}}
    assert "pt radar coverage: below 90 %" in doc["problems"]
    assert "radar coverage (MRMS HAWAII): unavailable" in doc["problems"] and doc["degraded"]
    run["radar_coverage"]["CONUS"]["sites"]["pt"]["covered_pct"] = 99.5      # partial, above the minimum
    html = page.render_html(cfg, run)
    assert '<span class="dot"></span>Radar coverage (MRMS CONUS): %s · PT ring 99.5 %% covered' % (
        FRAME_TS.strftime("%H:%MZ")) in html
    assert not any(p.startswith("pt radar coverage") for p in page.status_doc(cfg, run)["problems"])
    run["radar_coverage"]["CONUS"]["sites"]["pt"]["covered_pct"] = 90.0       # at the minimum: enough
    assert not any(p.startswith("pt radar coverage") for p in page.status_doc(cfg, run)["problems"])
    assert '<span class="dot"></span>Radar coverage (MRMS CONUS)' in page.render_html(cfg, run)
    run["radar_coverage"]["CONUS"]["sites"]["pt"]["covered_pct"] = 100.0
    assert "every ring fully covered" in page.render_html(cfg, run)
    cfg.radar_coverage = False
    assert "Radar coverage (MRMS): not checked" in page.render_html(cfg, run)
    assert page.status_doc(cfg, run)["radar_coverage"] is None


def test_an_observation_only_shown_is_no_problem(cfg, run):
    """Mauna Kea and Pie Town: no station qualifies, the report is only shown, so NWS sending
    nothing usable there (empty records, 2026-10-12) does not make the run degraded."""
    run["glm"] = dict(run["glm"], ok=True, partial=False)
    mk = run["sites"][1]
    mk["obs"] = dict(_status(ok=False, error="no usable observation: PHSF: no temperature"), station_id=None)
    assert "mk observation: unavailable" in page.status_doc(cfg, run)["problems"]
    mk["obs"]["display_only"] = True
    doc = page.status_doc(cfg, run)
    assert not any(p.startswith("mk observation") for p in doc["problems"])
    run["sites"][0]["obs"] = dict(_status(ok=False, error="no usable observation: KONM: no temperature"))
    # this test world's PT reads KONM for its checks: its outage is still a problem
    assert "pt observation: unavailable" in page.status_doc(cfg, run)["problems"]


def test_glm_block_shows_partial_data_and_counts_each_flash_once(cfg, run):
    pt = run["sites"][0]
    pt["glm"] = {"covered": True, "sats": ["G19", "G18"], "available": False, "partial": True,
                 "error": "G18: 1 of 18 granules failed (timed <out>)", "count_window": 2,
                 "count_by_sat": {"G19": 2, "G18": 1}, "nearest_km": 7.8, "nearest_bearing": "N",
                 "nearest_t": None, "nearest_sat": "G19", "flashes_on_map": [], "window_min": 6}

    def section():
        html = page.render_html(cfg, run)
        return html[html.index('id="site-pt"'):html.index('id="site-mk"')]
    sec = section()
    assert 'Lightning (GOES GLM) <span class="chip warn">partial</span>' in sec
    assert "flashes in the last 6 min (detected by G18 1, G19 2; one flash seen by both counts once)" in sec
    assert "partial: G18: 1 of 18 granules failed (timed &lt;out&gt;)" in sec      # escaped
    assert "Incomplete data this run" in sec and "GLM lightning unavailable" not in sec
    pt["glm"].update(available=True, partial=False, error=None, count_window=0,
                     count_by_sat={"G19": 0, "G18": 0}, nearest_km=None)
    sec = section()
    assert "detected by" not in sec and "Incomplete data" not in sec and "150 km" not in sec
    assert '<div class="v txt">none seen</div><div class="s">in the last 6 min</div>' in sec
    # the nearest flash in the check row's format: "912 km", not "912.0 km"
    pt["glm"].update(nearest_km=912.0, nearest_bearing="S")
    sec = section()
    assert '<div class="v txt">912 km S</div>' in sec and "912.0" not in sec


def test_summary_cell_class_and_gridpoint_fitness(cfg, run):
    """The verdict cell must not take the parent's .badge pill style; a gridpoint forecast
    the verdict cannot use (too old an NWS update, an unreadable series) is no "fresh" one
    in the footer or status.json."""
    html = page.render_html(cfg, run)
    summary = html.split('<table class="sum">')[1].split("</table>")[0]
    assert '<td class="vb">' in summary and 'class="badge"' not in summary
    pt = run["sites"][0]
    pt["gridpoint"] = {"ok": True, "stale": False, "error": None, "age_s": 0.0, "update_age_min": 780.0,
                       "missing": [], "unusable": [], "hours": [], "now": None, "next": None}
    html = page.render_html(cfg, run)
    assert "Gridpoint forecasts: 1 of 2 fresh; PT NWS update 13 h old" in html
    doc = page.status_doc(cfg, run)
    assert doc["sites"]["pt"]["gridpoint_ok"] is False
    assert "pt gridpoint forecast: NWS update too old" in doc["problems"] and doc["degraded"]   # stable text
    pt["gridpoint"].update(update_age_min=720.4)             # over the limit by less than a minute
    assert page.status_doc(cfg, run)["sites"]["pt"]["gridpoint_ok"] is False
    pt["gridpoint"].update(update_age_min=-200.0)            # 3 h ahead of this host's clock
    assert "pt gridpoint forecast: update time unknown or in the future" in page.status_doc(cfg, run)["problems"]
    pt["gridpoint"].update(update_age_min=30.0, unusable=["sky_cover_pct"])
    doc = page.status_doc(cfg, run)
    assert "pt gridpoint forecast: unreadable series" in doc["problems"] and doc["sites"]["pt"]["gridpoint_ok"] is False
    assert "PT unreadable series" in page.render_html(cfg, run)
    # a series only shown (temperature, the amount of rain) or one of a check switched off
    # is no gap in the verdict
    pt["gridpoint"].update(unusable=["temp_c", "qpf_mm", "wind_kmh"])
    cfg.wind_mph = 0.0
    doc = page.status_doc(cfg, run)
    assert doc["sites"]["pt"]["gridpoint_ok"] is True and not any("pt gridpoint" in p for p in doc["problems"])
    assert "Gridpoint forecasts: 2 of 2 fresh" in page.render_html(cfg, run)
    cfg.wind_mph = 45.0
    assert page.status_doc(cfg, run)["sites"]["pt"]["gridpoint_ok"] is False
    pt["gridpoint"].update(unusable=[], stale=True)          # a usable last good copy: ok, but stale
    doc = page.status_doc(cfg, run)
    assert doc["sites"]["pt"]["gridpoint_ok"] is True and "pt gridpoint forecast: stale" in doc["problems"]
