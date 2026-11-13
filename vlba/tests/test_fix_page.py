"""Fixes of the page and status.json (audit #126, #135, #0, #5, #4, #52, #128, #130, #3,
#129, #134, #147, #168, S16, #6): the page's own age, upstream text that is no text,
URLs from the feeds, radar frames the verdict does not use, alerts that could not be had,
the GOOD? count, the METARs from aviationweather.gov, and status.json's form."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import timedelta
from html.parser import HTMLParser

import pytest

import make_vlba_page as mvp
from vlba import decision, page
from vlba.config import VlbaConfig
from vlba.tests import test_main as tm
from vlba.tests.test_page import EVIL, GEN, _alert, _status, run  # noqa: F401  (run: the fixture)
from weather import page as wp

NODE = shutil.which("node")


def _page_attrs(html):
    """The attributes of the element with id "page"."""
    found = {}

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            d = dict(attrs)
            if d.get("id") == "page" and not found:
                found.update(d)

    P().feed(html)
    return found


def _script(html):
    return html.split("<script>\n", 1)[1].split("</script>", 1)[0]


def _js_age(html, now_s):
    """Run the page's own script under node with a minimal DOM at the clock ``now_s``: what
    the age slot then shows and whether the page is marked outdated."""
    harness = """
var attrs = %s;
function cl() { var s = {}; return {add: function (c) { s[c] = 1; }, remove: function (c) { delete s[c]; },
  contains: function (c) { return !!s[c]; }}; }
var page = {getAttribute: function (k) { return k in attrs ? attrs[k] : null; }, classList: cl()};
var box = {hidden: true, textContent: '', className: 'pageage'};
var document = {getElementById: function (id) { return id === 'page' ? page : (id === 'pageage' ? box : null); },
  querySelector: function () { return null; }, querySelectorAll: function () { return []; },
  documentElement: {style: {setProperty: function () {}}}};
var events = [];
var window = {addEventListener: function (e) { events.push(e); }, pageYOffset: 0, scrollTo: function () {},
  location: {reload: function () {}}};
var sessionStorage = {getItem: function () { return null; }, setItem: function () {}, removeItem: function () {}};
var intervals = [];
function setInterval(f, ms) { intervals.push(ms); }
function setTimeout() {}
Date.now = function () { return %d; };
%s
console.log(JSON.stringify({hidden: box.hidden, text: box.textContent, cls: box.className,
  outdated: page.classList.contains('outdated'), intervals: intervals, events: events}));
""" % (json.dumps(_page_attrs(html)), int(now_s * 1000), _script(html))
    out = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ---- #126 / #135: a page that is no longer regenerated says so by itself -------------------
def test_page_carries_its_generation_time_and_limit(cfg, run):  # noqa: F811
    html = page.render_html(cfg, run)
    a = _page_attrs(html)
    assert a["data-generated"] == str(int(GEN.timestamp())) and a["data-stale-after"] == str(75 * 60)
    assert a["data-stale-what"] == "the verdicts below are"
    assert '<div class="pageage" id="pageage" role="alert" hidden></div>' in html
    # the masthead says what its time is; the noscript line says when to take the page as stopped
    assert '<span class="gl">generated</span> 16:12 UTC</div>' in html
    assert ("<noscript><p class=\"pageage note\">Generated 16:12 UTC on Thursday, October 8, 2026. This page "
            "cannot check its own age without JavaScript: when that time is more than 75 min ago, the page is no "
            "longer being updated and the verdicts below are not current.</p></noscript>") in html
    # nothing of the run is interpolated into the script: it is the module's constant
    assert _script(html) == wp.PAGE_JS
    doc = page.status_doc(cfg, run)
    assert doc["stale_after_s"] == 4500 and doc["generated"] == GEN.isoformat()


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_page_script_judges_its_age_by_the_viewers_clock(cfg, run):  # noqa: F811
    html = page.render_html(cfg, run)
    t0 = GEN.timestamp()
    fresh = _js_age(html, t0 + 60)
    assert fresh["hidden"] and not fresh["outdated"] and 30000 in fresh["intervals"] and "pageshow" in fresh["events"]
    assert _js_age(html, t0 + 75 * 60 - 5)["hidden"]                    # within the limit: nothing
    old = _js_age(html, t0 + 76 * 60 + 10)
    assert not old["hidden"] and old["outdated"] and old["cls"] == "pageage"
    assert old["text"] == ("This page was generated 76 min ago and is no longer being updated: the verdicts below "
                           "are not current.")
    days = _js_age(html, t0 + 5 * 3600 + 120)
    assert days["text"].startswith("This page was generated 5 h ago") and days["outdated"]
    # a viewer's clock far behind: no banner, a note that the age cannot be judged
    behind = _js_age(html, t0 - 10 * 60)
    assert not behind["hidden"] and not behind["outdated"] and behind["cls"] == "pageage note"
    assert "cannot be judged" in behind["text"]
    assert _js_age(html, t0 - 2 * 60)["hidden"]                         # a little behind: nothing


def test_page_stale_setting(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("WEATHER_", "VLBA_")):
            monkeypatch.delenv(k, raising=False)
    assert VlbaConfig.from_env(out_dir=str(tmp_path / "o"), cache_dir=str(tmp_path / "c")).page_stale_min == 75
    monkeypatch.setenv("VLBA_PAGE_STALE_MIN", "180")
    assert VlbaConfig.from_env(out_dir=str(tmp_path / "o"), cache_dir=str(tmp_path / "c")).page_stale_min == 180
    for bad in ("9", "1441", "0", "-75", "75.5", "soon"):
        monkeypatch.setenv("VLBA_PAGE_STALE_MIN", bad)
        with pytest.raises(ValueError, match="VLBA_PAGE_STALE_MIN"):
            VlbaConfig.from_env(out_dir=str(tmp_path / "o"), cache_dir=str(tmp_path / "c"))


# ---- #0: one unpaired surrogate in upstream text never stops the page write -----------------
def _surrogate_routes(fake_http, cfg, where):
    alerts = {"type": "FeatureCollection", "features": []}
    if where == "alert":
        a = tm.alert_feature("urn:s1", "Special Weather Statement",
                             zones=["https://api.weather.gov/zones/forecast/NMZ226"])
        a["properties"]["headline"] = "Special Weather Statement issued \ud83d"
        alerts["features"].append(a)
    tm.add_routes(fake_http, cfg, alerts=alerts)
    if where == "points":
        pd = tm.points_doc("ABQ", 50, 60, "America/Denver", ("NMZ226", "NMC003", "NMZ106"), "KABX")
        pd["properties"]["cwa"] = "ABQ\udc00"
        pd["properties"]["relativeLocation"] = {"properties": {"city": "Pie Town \ud800", "state": "NM"}}
        fake_http.routes.insert(0, ("/points/34.3010,-108.1192", json.dumps(pd).encode("ascii"), True))
    if where == "station":
        sd = tm.stations_doc("KPT1", 34.32, -108.10, 2300.0)
        sd["features"][0]["properties"]["name"] = "PIE TOWN \ud800"
        od = tm.observation_doc("KPT1")
        od["properties"]["stationName"] = None
        fake_http.routes.insert(0, ("gridpoints/ABQ/50,60/stations", json.dumps(sd).encode("ascii"), True))
        fake_http.routes.insert(0, ("/stations/KPT1/observations/latest", json.dumps(od).encode("ascii"), True))


@pytest.mark.parametrize("where", ["alert", "station", "points"])
def test_a_lone_surrogate_upstream_still_writes_the_page(cfg, fake_http, monkeypatch, where):
    monkeypatch.setattr(mvp.mwp.radar, "load_frame", lambda cfg, cache: tm._mrms_result([]))
    tm._quiet_glm(monkeypatch)
    _surrogate_routes(fake_http, cfg, where)
    rc = mvp.main(["--out", cfg.out_dir, "--cache", cfg.cache_dir,
                   "--sites", "pt|Pie Town, NM|34.3010|-108.1192|America/Denver"])
    assert rc == 0
    for name in ("index.html", "status.json"):
        with open(os.path.join(cfg.out_dir, name), "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8")                      # strict: the file is valid UTF-8
        assert not re.search("[\ud800-\udfff]", text)
        if name == "status.json":
            json.loads(text)
            assert "\\ud8" not in text and "\\udc" not in text      # no surrogate escapes either
    html = tm.read_page(cfg)
    assert "�" in html and "Page generation failed" not in html
    assert {"alert": "Special Weather Statement issued �", "station": "PIE TOWN �",
            "points": "ABQ�"}[where] in html


def test_the_writer_replaces_surrogates(tmp_path):
    p = str(tmp_path / "x.html")
    wp._atomic_write_text(p, "a\ud83db\udc00c\U0001F600")
    with open(p, encoding="utf-8") as f:
        assert f.read() == "a�b�c\U0001F600"
    assert os.listdir(str(tmp_path)) == ["x.html"]


# ---- #5: control and text-direction characters ---------------------------------------------
def test_escaper_drops_controls_and_direction_marks():
    nasty = ("A\x00B\x1bC\x08D\x0cE\rF\x7fG\x85H\x9bI‮J‪K⁦L⁩M﻿N￾O￿P"
             "\U000E0001Q\U000E007FR")
    assert wp._e(nasty) == "ABCDEFGHIJKLMNOPQR"
    assert wp._e("line\nnext\ttab") == "line\nnext\ttab"
    assert wp._e("a b") == "a\nb"
    assert wp._e('<b a="x">&') == "&lt;b a=&quot;x&quot;&gt;&amp;"
    assert wp._e("Mauna Kea · 4205 m – ok ☀ é") == "Mauna Kea · 4205 m – ok ☀ é"


def test_controls_from_an_alert_never_reach_the_page(cfg, run):  # noqa: F811
    run["sites"][0]["alerts_at"] = [_alert(headline="‮GNINRAW ON\x00", sender="NWS\x1b[31m Albuquerque",
                                           description="Heavy\x9b rain.﻿")]
    html = page.render_html(cfg, run)
    for ch in ("\x00", "\x1b", "\x9b", "‮", "﻿"):
        assert ch not in html
    assert "GNINRAW ON" in html


# ---- #4 / #52: URLs from the feeds ---------------------------------------------------------
@pytest.mark.parametrize("url, ok", [
    ("https://api.weather.gov/icons/land/day/tsra_hi,40/tsra_hi,30?size=medium", True),
    ("https://www.weather.gov", True),
    ("https://weather.gov/x", True),
    ("https://alerts.weather.gov/cap/x.php?x=NMZ226#a", True),
    ("https://forecast.weather.gov:443/MapClick.php?lat=1", True),
    ("https://radar.noaa.gov/x.png", True),
    ("https://tracker.invalid/pixel.png", False),
    ("https://login.weather.gov.evil.invalid/session?next=x", False),
    ("https://evilweather.gov/x", False),
    ("https://noaa.gov.evil.invalid/", False),
    ("https://evil.invalid\\@api.weather.gov/x", False),
    ("https://user:pw@api.weather.gov/x", False),
    ("https://evil.invalid@api.weather.gov/x", False),
    ("https://api.weather.gov:8443/x", False),
    ("https://api.weather.gov/x y", False),
    ("http://api.weather.gov/x", False),
    ("//api.weather.gov/x", False),
    ("javascript:alert(1)//https://api.weather.gov", False),
    (" https://api.weather.gov/x", False),
    ("https://api.weather.gov/x\n", False),
    (None, False), (["https://api.weather.gov/x"], False),
])
def test_feed_urls_are_nws_or_noaa_https_only(url, ok):
    assert wp._feed_url(url) == (url if ok else None)


def test_foreign_icon_and_alert_link_are_dropped(cfg, run):  # noqa: F811
    good_icon = "https://api.weather.gov/icons/land/day/few?size=medium"
    run["sites"][0]["forecast"]["periods"][0]["icon"] = "https://tracker.invalid/px.gif?page=x"
    run["sites"][1]["forecast"]["periods"][0]["icon"] = good_icon
    run["sites"][0]["alerts_at"] = [_alert(web="https://login.weather.gov.evil.invalid/session")]
    html = page.render_html(cfg, run)
    assert "tracker.invalid" not in html and "evil.invalid" not in html
    assert ('<img src="%s" alt="Sunny" width="44" height="44" loading="lazy" referrerpolicy="no-referrer">'
            % good_icon.replace("&", "&amp;")) in html
    assert '<meta name="referrer" content="no-referrer">' in html.split("</head>")[0]
    run["sites"][0]["alerts_at"] = [_alert(web="https://alerts.weather.gov/x?id=1")]
    assert '<a href="https://alerts.weather.gov/x?id=1" rel="noopener">NWS alert page</a>' in page.render_html(cfg, run)


def test_parent_page_head_sends_no_referrer(cfg, run):  # noqa: F811
    assert '<meta name="referrer" content="no-referrer">' in wp.render_html(cfg, run).split("</head>")[0]


# ---- #128: a frame the radar check rejects as too old is never "fresh" ---------------------
def test_island_frame_too_old_for_the_check(cfg, run):  # noqa: F811
    mk = run["sites"][1]
    mk["island_radar"]["ts"] = GEN - timedelta(hours=3)
    html = page.render_html(cfg, run)
    assert ('<span class="pill"><span class="dot warnc"></span>MK radar (NWS Hawaii composite): frame 3 h old, '
            'not used</span>') in html
    assert "NWS Hawaii composite 13:12Z · 03:12 HST · frame 3 h old, not used for the verdict" in html
    doc = page.status_doc(cfg, run)
    assert "mk radar composite: frame too old" in doc["problems"] and doc["ok"] is False
    # within the limit (20 min by default): as before
    mk["island_radar"]["ts"] = GEN - timedelta(minutes=19)
    html = page.render_html(cfg, run)
    assert "not used" not in html
    assert not any("frame too old" in p for p in page.status_doc(cfg, run)["problems"])


def test_island_frame_without_time_is_as_old_as_its_copy(cfg, run):  # noqa: F811
    mk = run["sites"][1]
    mk["island_radar"].update(ts=None, age_s=3600.0, stale=True)
    assert "mk radar composite: frame too old" in page.status_doc(cfg, run)["problems"]
    mk["island_radar"]["age_s"] = 600.0
    probs = page.status_doc(cfg, run)["problems"]
    assert "mk radar composite: stale" in probs and "mk radar composite: frame too old" not in probs


def test_mrms_frame_too_old_for_the_check(cfg, run):  # noqa: F811
    run["radar"]["ts"] = GEN - timedelta(minutes=22)
    html = page.render_html(cfg, run)
    assert ('<span class="pill"><span class="dot warnc"></span>Radar: frame 15:50Z · frame 22 min old, not used'
            '</span>') in html
    assert "MRMS frame 15:50Z · 09:50 MDT · frame 22 min old, not used for the verdict" in html
    doc = page.status_doc(cfg, run)
    assert "radar: frame too old" in doc["problems"] and "radar: stale" not in doc["problems"]
    # judged at the time the check was: a site evaluated later than the run's start
    run["radar"]["ts"] = GEN - timedelta(minutes=19)
    assert "radar: frame too old" not in page.status_doc(cfg, run)["problems"]
    run["sites"][0]["verdict"]["evaluated"] = GEN + timedelta(minutes=2)
    assert "radar: frame too old" in page.status_doc(cfg, run)["problems"]


def test_healthy_radar_is_unchanged(cfg, run):  # noqa: F811
    html = page.render_html(cfg, run)
    assert "not used" not in html and "Radar: frame 16:06Z · fresh" in html
    assert not any("radar" in p for p in page.status_doc(cfg, run)["problems"])


# ---- #130 / #3: alerts that could not be had are "unknown", never "none" -------------------
def test_alert_block_says_unknown_when_the_feed_failed(cfg, run):  # noqa: F811
    run["alerts"] = _status(ok=False, error="HTTP 503", age_s=None)
    for s in run["sites"]:
        s["alerts_feed"] = _status(ok=False, error="HTTP 503", age_s=None)
        s["alerts_at"] = []
    html = page.render_html(cfg, run)
    assert "No NWS alerts at or near this site" not in html and "No NWS alerts for these sites" not in html
    assert html.count("Alerts unknown: the NWS alert feed could not be read.") == 3      # banner + two sites


def test_alert_block_names_a_last_good_copy(cfg, run):  # noqa: F811
    run["alerts"] = _status(ok=True, stale=True, error="HTTP 503", age_s=1800.0)
    pt = run["sites"][0]
    pt["alerts_feed"] = _status(ok=True, stale=True, error="HTTP 503", age_s=1800.0)
    sec = page.render_html(cfg, run).split('id="site-pt"')[1].split('id="site-mk"')[0]
    assert '<span class="chip warn">feed copy from 09:42 MDT</span>' in sec
    pt["alerts_at"] = []
    sec = page.render_html(cfg, run).split('id="site-pt"')[1].split('id="site-mk"')[0]
    assert ("No NWS alerts at or near this site in the last good copy of the feed (from 09:42 MDT): the latest "
            "fetch failed.") in sec


def test_an_absurd_copy_age_keeps_the_alert_cards(cfg, run):  # noqa: F811
    """Review: an age of the feed copy no clock can show (a broken cache stamp) must not turn
    the site's alert block, with its alerts, into an error box."""
    run["alerts"] = _status(ok=True, stale=True, error="HTTP 503", age_s=1e15)
    run["sites"][0]["alerts_feed"] = _status(ok=True, stale=True, error="HTTP 503", age_s=1e15)
    sec = page.render_html(cfg, run).split('id="site-pt"')[1].split('id="site-mk"')[0]
    assert "could not be rendered" not in sec
    assert '<span class="chip warn">feed copy of an earlier run</span>' in sec and "Flash Flood Warning" in sec


def test_placeholder_site_has_unknown_alerts(cfg, run):  # noqa: F811
    """A site whose own step failed carries alerts_feed ok False (make_vlba_page's placeholder)."""
    run["sites"][0] = mvp._site_placeholder(cfg, run["sites"][0]["site"], "TypeError: boom")
    html = page.render_html(cfg, run)
    banner = html.split('id="alerts"')[1].split("</div>")[0]
    assert "Alerts unknown for" in banner and "Pie Town" in banner
    sec = html.split('id="site-pt"')[1].split('id="site-mk"')[0]
    assert "Alerts unknown: the NWS alert feed could not be read." in sec
    assert "No NWS alerts at or near this site" not in sec


def test_an_alert_id_that_is_no_text_still_shows_the_alert(cfg, run):  # noqa: F811
    run["sites"][0]["alerts_at"] = [_alert(id=["x"], event="Tornado Warning")]
    run["sites"][1]["alerts_near"] = [_alert(id={"a": 1}, event="Tornado Warning")]
    html = page.render_html(cfg, run)
    assert "could not be rendered" not in html
    banner = html.split('id="alerts"')[1].split('class="site"')[0]
    assert "<b>Tornado Warning</b>" in banner and "No NWS alerts" not in banner


# ---- #129: the GOOD? count -----------------------------------------------------------------
def test_status_json_counts_incomplete_good(cfg, run):  # noqa: F811
    doc = page.status_doc(cfg, run)
    assert doc["summary"]["good"] == 1 and doc["summary"]["good_incomplete"] == 1     # mk is "GOOD?"
    assert doc["sites"]["mk"]["verdict"] == "GOOD" and doc["sites"]["mk"]["verdict_incomplete"] is True
    assert doc["sites"]["pt"]["verdict_incomplete"] is False
    run["sites"][1]["verdict"]["incomplete"] = False
    run["sites"][1]["verdict"]["missing"] = []
    html = page.render_html(cfg, run)
    assert "2 sites: <b>1</b> GOOD, <b>0</b> GOOD? (inputs missing), <b>1</b> BAD." in html
    assert page.status_doc(cfg, run)["summary"]["good_incomplete"] == 0


# ---- #134 / #147: the METARs from aviationweather.gov --------------------------------------
def test_footer_names_the_metars_used(cfg, fake_http, monkeypatch):
    tm._late_nws(cfg, fake_http, monkeypatch, [tm._metar("KPT1", 37)])
    mvp.run_once(cfg)
    html = tm.read_page(cfg)
    assert '<span class="pill"><span class="dot"></span>METARs (aviationweather.gov): used for 1 site (PT)</span>' in html
    assert "METARs from the NOAA/NWS Aviation Weather Center" in html
    assert "its METAR from aviationweather.gov when NWS has no recent one" in html
    st = tm.read_status(cfg)
    assert st["metars"] == {"source": "aviationweather.gov", "used": ["pt"], "unavailable": []}


def test_footer_and_status_name_metars_that_were_needed_and_failed(cfg, fake_http, monkeypatch):
    tm._late_nws(cfg, fake_http, monkeypatch, None, nws_age=150)
    mvp.run_once(cfg)
    html = tm.read_page(cfg)
    assert ('<span class="pill"><span class="dot warnc"></span>METARs (aviationweather.gov): unavailable for 1 site '
            '(PT)</span>') in html
    st = tm.read_status(cfg)
    assert st["metars"]["unavailable"] == ["pt"]
    assert "pt METARs (aviationweather.gov): unavailable" in st["problems"]


def test_metars_not_needed_and_switched_off(cfg, run):  # noqa: F811
    html = page.render_html(cfg, run)
    assert "METARs (aviationweather.gov): not needed this run</span>" in html
    assert page.status_doc(cfg, run)["metars"] == {"source": "aviationweather.gov", "used": [], "unavailable": []}
    cfg.metar_url = ""
    html = page.render_html(cfg, run)
    assert "METARs (aviationweather.gov): off</span>" in html and "Aviation Weather Center" not in html
    assert page.status_doc(cfg, run)["metars"] is None


# ---- #168: no road counts for a run without roads ------------------------------------------
def test_status_json_has_no_road_counts_without_roads(cfg, run):  # noqa: F811
    doc = page.status_doc(cfg, run)
    assert doc["roads"] is None
    assert all("roads_events" not in s and "roads_reports" not in s for s in doc["sites"].values())
    run["sites"][0]["roads"] = {"events": [], "reports": []}
    doc = page.status_doc(cfg, run)
    assert doc["sites"]["pt"]["roads_events"] == 0 and doc["sites"]["pt"]["roads_reports"] == 0


# ---- S16: radar switched off by configuration ----------------------------------------------
def test_radar_switched_off_is_no_problem(cfg, run):  # noqa: F811
    run["glm"]["partial"] = False
    run["sites"][1]["verdict"] = dict(run["sites"][1]["verdict"], incomplete=False, missing=[])
    os.makedirs(cfg.out_dir, exist_ok=True)
    page.write_page(cfg, run)
    assert page.status_doc(cfg, run)["ok"] is True
    run["radar"] = {"ok": False, "stale": False, "error": "radar layer disabled (WEATHER_RADAR=0)", "age_s": None,
                    "ts": None, "disabled": True}
    page.write_page(cfg, run)
    doc = page.status_doc(cfg, run)
    assert doc["problems"] == [] and doc["degraded"] is False and doc["ok"] is True
    assert doc["radar"]["disabled"] is True and doc["radar"]["ok"] is False
    html = page.render_html(cfg, run)
    assert '<span class="pill"><span class="dot off"></span>Radar: off</span>' in html
    assert "Radar switched off: radar layer disabled (WEATHER_RADAR=0)" in html
    # the same dict without the marker (an older orchestration): still a problem, as before
    del run["radar"]["disabled"]
    assert "radar: unavailable" in page.status_doc(cfg, run)["problems"]


# ---- #6: status.json carries no raw markup characters --------------------------------------
def test_status_json_escapes_markup_characters(cfg, run):  # noqa: F811
    run["sites"][0]["verdict"]["reasons"] = [EVIL]
    run["errors"] = ["x > y & <z>"]
    os.makedirs(cfg.out_dir, exist_ok=True)
    path = page.write_status_json(cfg, run)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    assert "<" not in text and ">" not in text and "&" not in text
    assert "\\u003cscript\\u003e" in text
    doc = json.loads(text)
    assert doc["verdicts"]["pt"]["reasons"] == [EVIL] and doc["errors"] == ["x > y & <z>"]
    assert doc == json.loads(json.dumps(page.status_doc(cfg, run), default=str))


def test_status_json_fallback_carries_the_limit(cfg, run, monkeypatch):  # noqa: F811
    def boom(cfg, run):
        raise RuntimeError("<b>x</b>")
    monkeypatch.setattr(page, "status_doc", boom)
    os.makedirs(cfg.out_dir, exist_ok=True)
    with open(page.write_status_json(cfg, run), encoding="utf-8") as f:
        text = f.read()
    doc = json.loads(text)
    assert doc["stale_after_s"] == 4500 and doc["ok"] is False and "<" not in text


def test_decision_summary_is_untouched(run):  # noqa: F811
    """The page counts GOOD? itself; decision.summary keeps its keys (other code reads them)."""
    assert set(decision.summary([s["verdict"] for s in run["sites"]])) == {"good", "bad", "no_data", "incomplete"}
