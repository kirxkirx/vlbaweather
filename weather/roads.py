"""New Mexico road closures caused by emergencies, for the per-site maps and lists.

Scope (owner's decision): **New Mexico only** and **only closures caused by an emergency**
(flooding, washouts, debris/rock/mud slides, fire, crashes, hazmat, snow/ice, wind/dust,
storm damage, law-enforcement closures). Roadwork, construction, lane closures, maintenance,
scheduled/nightly and seasonal closures, special events, convoys/oversize loads, White
Sands missile-range alerts and rest-area closures are left out. Two scoping calls, each
switchable in the config: "water on road" items (``cfg.roads_water``) and NWS Local Storm
Reports (``cfg.lsr_enabled``).

Sources
* NMDOT's open NMRoads feed (CC0): ``nmroads.json`` (GeoJSON, line geometry that follows
  the road, but only a templated title) joined on feature id == RSS guid with ``rss.xml``
  (the free-text cause, the route/mile-marker fields and the only trustworthy dates: the
  JSON ``update_date`` / ``creation_date`` run 0 to 7 h behind them, 6 h for most events on
  2026-09-23, and are used only, marked approximate, when rss.xml is down; RSS "Update
  Date" batch re-save stamps are ignored). Both are fetched with ``http.cached_bytes``
  (conditional GET: usually a 304). Either file alone still gives a (degraded) answer.
* NWS Local Storm Reports via the Iowa Environmental Mesonet: New Mexico reports whose
  remark names a road, crossing or arroyo that is closed, flooded or impassable (not the
  range roads of White Sands Missile Range), with corrections and other offices'
  duplicates folded in. Reports never say when a road reopens; the page shows them dated.

``fetch_nm_roads`` / ``fetch_storm_reports`` never raise; ``site_roads`` picks the items that
touch one map frame. Neither source is fetched when no configured site's map reaches New
Mexico (``bbox_covers_nm``; make_weather_page decides), so a configuration elsewhere makes
no request to NMDOT or for storm reports.
"""
from __future__ import annotations

import difflib
import hashlib
import html
import json
import logging
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from . import geo, http, util

log = logging.getLogger("weather.roads")

NM_BOX = (-109.1, 31.3, -103.0, 37.0)      # (lonmin, latmin, lonmax, latmax): New Mexico
# New Mexico's outline for "does this map reach New Mexico" (``bbox_covers_nm``): NM_BOX
# alone also holds West Texas south of 32 N (Van Horn, Fort Stockton) and a strip of the
# Panhandle east of the TX border at about -103.04. Twelve vertices, from the U.S. Census
# Bureau's 2024 TIGER/Line state polygon, each moved outward by about 0.01 degrees: all
# 7097 vertices of that polygon lie inside (checked when the outline was made), so a map
# that reaches New Mexico is never missed. Clockwise from the Four Corners: the Colorado
# line (37 N), the Oklahoma line (-103.002), the Texas line (-103.041 at 36 N to -103.065
# at 32 N), 32 N west to the Rio Grande, the river down to the Mexican border (31.78 N),
# and the bootheel (-108.2085 W, 31.332 N).
NM_OUTLINE = ((-109.06, 37.01), (-102.99, 37.01), (-102.99, 36.49), (-103.03, 36.49),
              (-103.03, 34.00), (-103.05, 33.00), (-103.05, 31.99), (-106.60, 31.99),
              (-106.51, 31.775), (-108.20, 31.775), (-108.20, 31.325), (-109.06, 31.325))
NMROADS_LINK = "https://nmroads.com/"
DEFAULT_JSON_URL = "https://nmroads.com/nmroads.json"
DEFAULT_RSS_URL = "https://nmroads.com/rss.xml"
DEFAULT_LSR_URL = ("https://mesonet.agron.iastate.edu/geojson/lsr.geojson"
                   "?hours={hours}&states=NM")
NM_TZ = "America/Denver"                   # NMRoads writes local wall-clock times
CAUSE_MAX = 400
_NS = {"geo": "http://www.w3.org/2003/01/geo/wgs84_pos#", "osow": "http://nmroads.com/osow/"}

# ---- classification ------------------------------------------------------------------
# NMDOT event names that are never listed on their own (roadwork and friends). An event of
# one of these types is re-admitted only when its text says the road is closed/impassable
# BECAUSE of an emergency (same sentence), or reports water on the road (water items).
_EXCLUDED_TYPES = re.compile(
    r"^(roadwork|road work|construction( closure)?|lane closures?|seasonal( closure)?|alert|"
    r"fair driving conditions|rest ?areas?.*|maintenance|special events?|oversize.*|"
    r"work[- ]zone)$", re.I)
# Full-closure event types (checked after the exclusions above).
_CLOSURE_TYPES = re.compile(r"^(closure|road closure|road closed|closed|full closure|"
                            r"emergency closure|weather closure)$", re.I)

# Dates and clock times in NMDOT's texts ("Sept 21-22", "7 a.m. to 5 p.m.").
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_WEEKDAY = r"(mon|tue|tues|wed|wednes|thu|thur|thurs|fri|sat|satur|sun)(day)?s?"
_CLOCK = r"\d{1,2}(:\d{2})?\s*(a\.?\s?m\.?|p\.?\s?m\.?|noon)"
# Never an emergency closure on its own (owner's list): convoys and oversize loads,
# missile-range activity, rest areas, seasonal closures, special events and NMDOT's
# permanent alerts (steep grades, length limits). Applied only when no sentence says the
# road is closed by an emergency: "Roadway closed due to a crash. Traffic is diverted at the
# rest area." is a crash closure, "near White Sands Missile Range" only a place.
_HARD_EXCLUDE = re.compile(
    r"\brest ?areas?\b|\bwelcome cent(er|re)s?\b|\bvisitor cent(er|re)s?\b|\bmissiles?\b|"
    r"\bwsmr\b|\bfirings?\b|\brange (roads?|routes?)\b|\bwind turbines?\b|\bturbine blades?\b|"
    r"\bconvoys?\b|\bover-?size(d)?( loads?| vehicles?)?\b|\bwide loads?\b|"
    r"\bspecial events?\b|\bparades?\b|\bmarathons?\b|\bfiesta\b|\bfestivals?\b|\brodeos?\b|"
    r"\b(county|state) fair\b|\bfilm(ing)? (shoots?|productions?|crews?)\b|\bfilm shoots?\b|"
    r"\bmovie (shoots?|productions?)\b|\bseasonal(ly)?\b|\b(winter|seasonal) closures?\b|"
    r"\bclosed for the (season|winter)\b|\bclosed (from|between) " + _MONTH + r" (to|and|"
    r"through|thru|until) " + _MONTH + r"|\bpermanent alert\b", re.I)
# A sentence about a rest area / visitor centre that names no road: "Anton Chico Rest Area
# (Eastbound) is closed due to flooding" closes the rest area, not the interstate.
_FACILITY = re.compile(r"\brest ?areas?\b|\bwelcome cent(er|re)s?\b|\bvisitor cent(er|re)s?\b",
                       re.I)
_ROAD_WORD = re.compile(r"\b(roads?|roadways?|highways?|hwy|interstate|routes?|lanes?|ramps?|"
                        r"traffic|frontage)\b|\b(I|US|NM)[- ]?\d+\b", re.I)
# Missile-range activity (not the place name): the road closes for a test, not a hazard.
_MISSILE_ACTIVITY = re.compile(
    r"\bfirings?\b|\bmissile (tests?|launch(es)?|operations?|activit(y|ies)|missions?|firings?)\b|"
    r"\brange (closures?|operations?|activit(y|ies)|missions?)\b|\bwsmr (missions?|tests?)\b",
    re.I)

# Phrases that contain a hazard word without being a hazard (neutralised before matching),
# including place names ("Ice Caves Road" on NM 53).
_BENIGN = re.compile(
    r"\bwind[- ]?(turbines?|farms?|energy|blades?|projects?)\b|\bwindmills?\b|"
    r"\bstorm[- ]?(drains?|drainage|sewers?|water|inlets?)\b|\bstormwater\b|"
    r"\bcrash (attenuators?|cushions?|barriers?|walls?|tests?)\b|"
    r"\bfire ?(place|hydrants?|stations?|works)\b|"
    r"\bdust (control|abatement|palliatives?|suppression)\b|"
    r"\b(reduce|reducing|prevent|preventing|fewer|reduction (in|of))\b[^.;]{0,50}?"
    r"\b(crash(es)?|accidents?|collisions?|fatalit(y|ies))\b|"
    r"\b(crash|accident|collision) (rates?|data|reduction|history)\b|"
    r"\bweather permitting\b|\bsnow ?fenc(e|es|ing)\b|\bice caves?\b", re.I)

# Unambiguous hazards: an emergency in any sentence that is not about planned work.
EMERGENCY_STRONG = re.compile(
    r"\bflash[- ]?flood\w*|\bflood(s|ed|ing|waters?)?\b|\bstanding water\b|\bhigh water\b|"
    r"\bwater (is |was )?(running|flowing|rushing|going|pooling|ponding)?\s*(over|across)\b|"
    r"\bponding\b|\bwash(ed)?[- ]?(out|outs|away)\b|\bwashouts?\b|"
    r"\brock ?(slides?|falls?)\b|\bfalling rocks?\b|\bmud ?(slides?|flows?)\b|"
    r"\bland ?slides?\b|\bdebris (flows?|slides?)\b|\bsink ?holes?\b|"
    r"\b(wild|brush|grass|forest|vehicle|structure|truck)? ?fires?\b|\bwildfires?\b|\bsmoke\b|"
    r"\bcrash(es)?\b|\baccidents?\b|\bcollisions?\b|\broll-?over\b|\boverturned\b|"
    r"\bjack-?knifed?\b|\bfatal(ity|ities)?\b|\bhaz-?mat\b|\bhazardous (materials?|spill)\b|"
    r"\b(fuel|chemical|diesel|cargo) spill\b|\bspill(ed)?\b|\bgas leak\b|"
    r"\bsnow\w*|\bice\b|\bicy\b|\bblizzard\w*|\bwhite-?outs?\b|\bfreezing (rain|drizzle)\b|"
    r"\bsleet\b|\b(high|strong|gusty|damaging|severe) winds?\b|\bwind gusts?\b|"
    r"\bdust storms?\b|\bblowing (dust|snow|sand)\b|\bhaboob\b|"
    r"\b(low|poor|reduced|limited|zero|near[- ]zero) visibility\b|\bvisibility (is )?(reduced|"
    r"limited|poor|low|near zero)\b|\btornado\w*|\bthunderstorms?\b|"
    r"\b(downed|fallen) (power ?lines?|trees?)\b|\b(power ?lines?|trees?) down\b|"
    r"\bemergency (services|responders|crews|personnel|management|vehicles)\b|"
    r"\blaw enforcement\b|\bpolice\b|\bsheriff\w*\b|\bnmsp\b|\binvestigation\b|"
    r"\bevacuat\w*|\bimpass[ai]ble\b|\b(heavy|ongoing|torrential|recent) rains?\b|\brainfall\b|"
    r"\bdue to (the )?rains?\b", re.I)
# Hazard words that roadwork texts also use ("damaged pavement", "emergency repairs"): an
# emergency only in a sentence without construction wording.
EMERGENCY_WEAK = re.compile(
    r"\bdamage[sd]?\b|\bemergenc(y|ies)\b|\bstorms?\b|\bwinds?\b|\bdebris\b|\bmud(dy)?\b|"
    r"\bdust\b|\bblowing\b|\bvisibility\b|\bincidents?\b|\bcollapsed?\b|\berosion\b|"
    r"\bundermined\b|\brains?\b|\bboulders?\b|\bstalled\b|\bdisabled vehicles?\b", re.I)
# Planned work that no hazard word in the same sentence overrides: a schedule ("nightly",
# "daily", "scheduled", a clock-time window, a date or weekday range) or a "project". "The
# ramp will be closed nightly to replace the crash cushion damaged in an earlier accident"
# is roadwork, not an accident.
PLANNED = re.compile(
    r"\bscheduled\b|\bnightly\b|\b(each|every) (night|day|morning|evening|weekday|weekend)\b|"
    r"\bdaily\b|\bprojects?\b|"
    r"\b" + _CLOCK + r"\s*(to|until|till|through|thru|-|–|—)\s*" + _CLOCK + r"|"
    r"\b" + _MONTH + r"\s+\d{1,2}(st|nd|rd|th)?\s*(-|–|—|to|through|thru|until)\s*("
    + _MONTH + r"\s+)?\d{1,2}(st|nd|rd|th)?\b|"
    r"\b" + _WEEKDAY + r"\s*(-|–|—|to|through|thru)\s*" + _WEEKDAY + r"\b|"
    r"\bfrom " + _MONTH + r" (to|through|thru|until) " + _MONTH, re.I)
# Construction, maintenance and scheduled-event wording. A weak hazard word never counts in a
# sentence with it; a strong one only when the sentence names it as the cause ("closed due to
# flooding in the construction zone": yes; "closed for construction of a flood control
# channel": no). An event of a closure type with this wording and no hazard is left out.
CONSTRUCTION = re.compile(
    r"\bconstruction\b|\breconstruction\b|\broad ?work\b|\bpaving\b|\brepaving\b|"
    r"\bresurfac\w*|\bmill(ing)?\b|\bmill and (fill|inlay)\b|\binlay\b|\boverlay\b|"
    r"\bchip ?seal\w*|\bfog ?seal\w*|\bcrack ?seal\w*|\bre-?striping\b|\bstriping\b|"
    r"\bpatching\b|\bpotholes?\b|\bbridge (work|replacement|rehab\w*|repairs?|maintenance|deck|"
    r"demolition|construction)\b|\bmaintenance\b|\bprojects?\b|\bscheduled\b|\bnightly\b|"
    r"\bdaily\b|\beach night\b|\bovernight\b|\bnight ?work\b|\blane (closures?|restrictions?|"
    r"reductions?)\b|\bshoulder (closures?|closed|work|restrictions?)\b|\bspecial event\b|"
    r"\bparade\b|\b(marathon|races?|bike ride)\b|\bconvoys?\b|\boversized?\b|\bturbines?\b|"
    r"\bmissiles?\b|\brange road\b|\brest ?areas?\b|\bcontractors?\b|\bwork ?zones?\b|"
    r"\bflaggers?\b|\bflagging\b|\bpilot (car|vehicle)s?\b|\butility (work|maintenance)\b|"
    r"\bguard ?rails?\b|\bsewer\b|\bpipeline\b|\bdetours? provided\b|\binstall(ing|ation)?\b|"
    r"\brehabilitation\b|\bimprovements?\b|\bwidening\b|\bdemolish\w*|\bdemolition\b|"
    r"\bmitigation\b|\b(flood|erosion|avalanche|rockfall|rock ?fall|drainage|slope|weed|dust) "
    r"control\b|\bfenc(e|es|ing)\b|\bcushions?\b|\bstabiliz\w*|\bgirders?\b|\bblasting\b|"
    r"\bculverts?\b|\bscaling\b|\breplac(e|es|ed|ing|ement)\b|\bfiesta\b|\bfestivals?\b|"
    r"\bfilm(ing)?\b|\b(winter|seasonal) closures?\b", re.I)
# "... due to / because of / after <a few adjectives> <strong hazard>": the hazard is named
# as the cause, so construction wording in the same sentence is only context.
_CAUSED = re.compile(
    r"\b(due to|because of|caused by|as a result of|following|after|from)\s+"
    r"((the|a|an|heavy|recent|ongoing|severe|major|minor|serious|significant|large|several|"
    r"multiple|reported|active|deep|fast[- ]moving|continued|continuing|earlier|new|local|"
    r"localized|\w+-vehicle)\s+){0,3}(" + EMERGENCY_STRONG.pattern + ")", re.I)
# The road (or a whole direction of it) is closed. Lane / shoulder closures are removed from
# the text first (_PARTIAL), so "the left lane is closed" is not a road closure.
_PARTIAL = re.compile(
    r"\b(left|right|inside|outside|driving|passing|travel|turn(ing)?|middle|center|one|single|"
    r"a|1|slow|fast|auxiliary|merge)\s+(travel\s+|driving\s+|passing\s+|turn\s+)?lanes?\b"
    r"[^.;]*?\b(closed|closures?|closing|blocked)\b|\blanes? (closures?|restrictions?|"
    r"reductions?|shifts?)\b|\bshoulders?\b[^.;]*?\bclosed\b|\breduced to (one|1|a single) "
    r"lanes?\b", re.I)
FULL_CLOSED = re.compile(
    r"\bclosed\b|\bclosures?\b|\bshut down\b|\bimpass[ai]ble\b|\bblocked in both directions\b|"
    r"\b(all|both) (lanes|directions)( of travel)? (are |is |have been |remain )?blocked\b|"
    r"\broad(way)? (is |has been )?blocked\b", re.I)
IMPASSABLE = re.compile(r"\bimpass[ai]ble\b", re.I)
# "Water on road": flooding reported on the roadway itself.
WATER = re.compile(
    r"\bflood(s|ed|ing)?\b|\bflash[- ]?flood\w*|\bstanding water\b|\bhigh water\b|"
    r"\bwater (is |was )?(running |flowing |rushing |going |pooling )?(over|across)\b|"
    r"\bponding\b|\bwash(ed)?[- ]?(out|outs|away)\b|\bwashouts?\b|\bimpass[ai]ble\b|"
    r"\bunder ?water\b", re.I)
# Sentence boundaries: ". ", "! ", "? ", "; ", "~~", bullets and newlines -- but not after
# an abbreviation ("3 a.m. Tuesday", "St. Francis Dr.", "approx. 6 p.m.", "U. S. 70") or
# before a lower-case word, which would split one sentence into a "closed" half and a
# "due to a crash" half.
_BOUNDARY = re.compile(r"([.!?;])\s+|\s*~{2,}\s*|\s*[•·]\s*|\s*\n\s*")
_ABBREV = {
    "a.m", "p.m", "am", "pm", "st", "sts", "dr", "rd", "rds", "ave", "av", "blvd", "hwy",
    "hwys", "pkwy", "ln", "ct", "pl", "cir", "jct", "mt", "mtn", "ft", "co", "cty", "cnty",
    "approx", "appr", "est", "no", "nos", "mi", "min", "mins", "hr", "hrs", "u.s", "u.s.a",
    "n.m", "e.g", "i.e", "vs", "nb", "sb", "eb", "wb", "mm", "mp", "jan", "feb", "mar", "apr",
    "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "mon", "tue", "tues", "wed",
    "thu", "thur", "thurs", "fri", "sat", "sun", "sr", "jr", "dept", "gov", "hwy",
}


def _neutral(text: str) -> str:
    return _BENIGN.sub(" ", text or "")


def _is_abbrev(before: str) -> bool:
    """Does the text before a period end with an abbreviation (or a single initial)?"""
    m = re.search(r"([A-Za-z](?:[A-Za-z.]*[A-Za-z])?)\.$", before)
    if not m:
        return False
    word = m.group(1).lower()
    return len(word) == 1 or word in _ABBREV


def _sentences(text: str) -> List[str]:
    text = text or ""
    out: List[str] = []
    start = 0
    for m in _BOUNDARY.finditer(text):
        if m.group(1) is not None:
            nxt = text[m.end():m.end() + 1]
            if m.group(1) == "." and _is_abbrev(text[start:m.start() + 1]):
                continue
            if m.group(1) in ".!?" and nxt.islower():
                continue
            out.append(text[start:m.start() + 1])
        else:
            out.append(text[start:m.start()])
        start = m.end()
    out.append(text[start:])
    return [s for s in out if s and s.strip()]


def _hazard(s: str) -> bool:
    """One sentence names an emergency as something happening: a hazard word, but not in a
    sentence about planned work (``PLANNED``), and with construction wording only when the
    sentence names the hazard as the cause (``_CAUSED``) -- weak words never then."""
    if PLANNED.search(s):
        return False
    n = _neutral(s)
    if CONSTRUCTION.search(s):
        return bool(_CAUSED.search(n))
    return bool(EMERGENCY_STRONG.search(n) or EMERGENCY_WEAK.search(n))


def emergency_in(sentences: Sequence[str]) -> bool:
    """True when one of the sentences names an emergency (see ``_hazard``)."""
    return any(_hazard(s) for s in sentences)


def _full_closed(text: str) -> bool:
    return bool(FULL_CLOSED.search(_PARTIAL.sub(" ", text or "")))


def _closed_by_emergency(s: str) -> bool:
    """One sentence says the road is closed or impassable because of an emergency, and is
    not about a rest area without a road, or about missile-range activity."""
    if not _full_closed(s) or not _hazard(s):
        return False
    if _FACILITY.search(s) and not _ROAD_WORD.search(_FACILITY.sub(" ", s)):
        return False
    return not _MISSILE_ACTIVITY.search(s)


def _water_sentence(s: str) -> bool:
    """One narrative sentence reports water on the road: not planned work, not construction
    wording (unless the water is named as the cause), not one of the owner's exclusions."""
    n = _neutral(s)
    if not WATER.search(n) or PLANNED.search(s) or _HARD_EXCLUDE.search(s):
        return False
    return not CONSTRUCTION.search(s) or bool(_CAUSED.search(n))


def classify_event(category: str, title: str, narrative: str,
                   water: bool = True) -> Tuple[Optional[str], bool, str]:
    """Classify one NMDOT event: ``(kind, cause_known, reason)`` where ``kind`` is
    "closure", "water" or None (excluded) and ``reason`` says why (for logs and tests).

    ``title`` is the templated "Closure, NM 94 ... Roadway closed." line, ``narrative`` the
    free-text description ("" when only the JSON is available). Rules, in order:
    1. a sentence (of the narrative or the title) that says the road is closed or impassable
       because of an emergency (``_closed_by_emergency``) -> closure, cause known, whatever
       the event type and whatever else the text mentions;
    2. roadwork-type events (Roadwork, Construction Closure, Lane Closure, Alert, Fair
       Driving Conditions, ...) -> water when a narrative sentence reports water on the road
       (``_water_sentence``), else excluded;
    3. the owner's exclusions anywhere in the text (convoys, missile range, rest areas,
       seasonal, special events, permanent alerts) -> excluded;
    4. "Closure" events -> closure when a sentence names an emergency (cause known), else
       excluded with construction / scheduling wording, else closure, cause not stated;
    5. driving-conditions and other types -> closure when the text says the road is closed
       (cause known: the conditions, or an emergency), water when it reports flooding /
       standing water / water over the road, else excluded.
    Water items are produced only when ``water`` is True."""
    cat = " ".join((category or "").split())
    title = title or ""
    narrative = narrative or ""
    title_rest = re.sub(r"^\s*%s\s*[,:-]?\s*" % re.escape(cat), "", title, flags=re.I) if cat \
        else title
    sents = _sentences(narrative)
    for s in sents + _sentences(title_rest):
        if _closed_by_emergency(s):
            return "closure", True, "%s: closed by an emergency" % (cat or "event")
    wet = water and any(_water_sentence(s) for s in sents)
    if _EXCLUDED_TYPES.match(cat):
        if wet:
            return "water", True, "%s re-admitted: water on the road" % cat
        return None, False, "%s" % (cat or "unknown type")
    everything = "%s %s %s" % (cat, title, narrative)
    hard = _HARD_EXCLUDE.search(everything)
    if hard:
        return None, False, "owner exclusion (%s)" % hard.group(0)
    emergency = emergency_in(sents)
    construction = any(CONSTRUCTION.search(s) or PLANNED.search(s)
                       for s in sents + [title_rest])
    if _CLOSURE_TYPES.match(cat):
        if emergency:
            return "closure", True, "closure, emergency cause"
        if construction:
            return None, False, "%s with construction or scheduling wording" % cat
        return "closure", False, "closure, cause not stated"
    conditions = "driving conditions" in cat.lower()
    if not conditions and cat and emergency_in([cat]):
        emergency = True                   # an event type such as "Crash" or "Hazmat"
    if _full_closed("%s %s" % (title_rest, narrative)):
        if emergency or conditions:
            return "closure", True, "%s: road closed" % (cat or "event")
        if not construction:
            return "closure", False, "%s: road closed, cause not stated" % (cat or "event")
        return None, False, "%s with construction wording" % (cat or "event")
    if wet:
        return "water", True, "%s: water on the road" % (cat or "event")
    return None, False, "%s" % (cat or "unknown type")


# ---- text and date helpers -----------------------------------------------------------
_TAG = re.compile(r"<[^>]*>")
_DELIM_RE = re.compile(r"(?:<|&lt;)\s*autodescriptiondelimiter\s*(?:/\s*)?(?:>|&gt;)", re.I)
_BR = re.compile(r"<br\s*/?>", re.I)
_BOILERPLATE = {
    "use extreme caution.", "use caution.", "please use caution.", "expect delays.",
    "expect delays and use caution while travelling through the area.",
    "expect delays and use caution while traveling through the area.",
    "please drive with caution, reduce speed, and obey all posted traffic signs.",
    "this event will be updated as conditions change.", "cloudy conditions exist.",
}


def clean_text(s: Optional[str]) -> str:
    """HTML fragment -> plain text: the delimiter token and tags removed, entities decoded,
    whitespace collapsed."""
    if not s:
        return ""
    s = _DELIM_RE.sub(" ", s)
    s = _BR.sub(" ", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s).replace(" ", " ")
    return " ".join(s.split())


def _clip(s: str, n: int = CAUSE_MAX) -> str:
    if len(s) <= n:
        return s
    cut = s[:n - 1]
    sp = cut.rfind(" ")
    if sp > n // 2:
        cut = cut[:sp]
    return cut.rstrip(" ,;:") + "…"


def _segments(raw_description: str) -> List[str]:
    """The narrative split at NMDOT's delimiter token (the first segment is NMDOT's
    templated condition, e.g. "Standing water on roadway.", the rest free text), cleaned."""
    parts = _DELIM_RE.split(raw_description or "")
    return [p for p in (clean_text(x) for x in parts) if p]


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split())


def make_cause(segments: Sequence[str], title: str) -> str:
    """The cause shown to viewers: the narrative segments joined, a repeated copy of the
    title at the start of a segment and boilerplate sentences ("Use extreme caution.")
    dropped, repeated sentences removed, at most ``CAUSE_MAX`` characters."""
    out: List[str] = []
    seen = set()
    nt = _norm(re.sub(r"[.\s]+$", "", title or ""))
    for seg in segments:
        if nt and _norm(seg).startswith(nt):
            # "Closure, NM 273 ... State Line., due to flooding." -> "Due to flooding."
            words = len(nt.split())
            m = re.match(r"(?:\W*\w+){%d}" % words, seg)
            rest = seg[m.end():] if m else seg
            if re.match(r"^\s*[.,;:]", rest):
                rest = rest.lstrip(" .,;:")
                seg = rest[:1].upper() + rest[1:] if rest else ""
        for sent in _sentences(seg):
            key = _norm(sent)
            if not key or key in seen:
                continue
            seen.add(key)
            if sent.strip().lower() in _BOILERPLATE:
                continue
            out.append(sent.strip())
    text = " ".join(out)
    if text and text[-1] not in ".!?…":
        text += "."
    return _clip(text)


_TZ_ABBR = {"MST": -7, "MDT": -6, "CST": -6, "CDT": -5, "PST": -8, "PDT": -7, "UTC": 0,
            "GMT": 0, "Z": 0}
_JAVA_DATE = re.compile(r"^[A-Za-z]{3} ([A-Za-z]{3}) (\d{1,2}) (\d{1,2}):(\d{2}):(\d{2}) "
                        r"([A-Za-z]{1,5}) (\d{4})$")
_ISOISH = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?"
                     r"\s*(Z|[+-]\d{2}:?\d{2})?$")
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                       "sep", "oct", "nov", "dec"), 1)}


def _local(dt: datetime) -> datetime:
    return dt.replace(tzinfo=util.tzinfo_for(NM_TZ))


def parse_nm_date(s: Optional[str]) -> Optional[datetime]:
    """An NMRoads RSS date -> aware datetime (None for "unknown" / garbage). Formats seen:
    "2026-09-16 12:26:04.998" and "2026-07-20 06:40:46.0" (no offset: America/Denver wall
    clock), "Tue Jun 16 09:30:10 MDT 2026" (Java style, zone abbreviation honoured; an
    unknown one reads as Denver time), RFC 822 "Mon, 20 Jul 2026 06:40:46 -0600" (the
    item pubDate) and ISO 8601 with an offset."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s or s.lower() in ("unknown", "null", "none", "n/a", "-"):
        return None
    try:
        m = _ISOISH.match(s)
        if m:
            y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
            sec = int(m.group(6) or 0)
            frac = (m.group(7) or "0")[:6].ljust(6, "0")
            dt = datetime(y, mo, d, h, mi, sec, int(frac))
            off = m.group(8)
            if not off:
                return _local(dt)
            if off == "Z":
                return dt.replace(tzinfo=timezone.utc)
            off = off.replace(":", "")
            sign = -1 if off[0] == "-" else 1
            delta = timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))
            return dt.replace(tzinfo=timezone(sign * delta))
        m = _JAVA_DATE.match(s)
        if m:
            mon = _MONTHS.get(m.group(1).lower())
            if mon is None:
                return None
            dt = datetime(int(m.group(7)), mon, int(m.group(2)), int(m.group(3)),
                          int(m.group(4)), int(m.group(5)))
            off = _TZ_ABBR.get(m.group(6).upper())
            if off is None:
                return _local(dt)
            return dt.replace(tzinfo=timezone(timedelta(hours=off)))
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if dt is None:
        return None
    return _local(dt) if dt.tzinfo is None else dt


def _http_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ---- geometry helpers ----------------------------------------------------------------
def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pt(p: Any) -> Optional[List[float]]:
    if not isinstance(p, (list, tuple)) or len(p) < 2:
        return None
    lon, lat = _num(p[0]), _num(p[1])
    if lon is None or lat is None or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return [round(lon, 6), round(lat, 6)]


def clean_geometry(g: Any) -> Optional[dict]:
    """A GeoJSON Point / MultiPoint / LineString / MultiLineString with valid lon/lat
    (2-D, rounded to 1e-6 degrees), or None. Invalid vertices are dropped."""
    if not isinstance(g, dict):
        return None
    t, c = g.get("type"), g.get("coordinates")
    if t == "Point":
        p = _pt(c)
        return {"type": "Point", "coordinates": p} if p else None
    if t in ("LineString", "MultiPoint"):
        pts = [q for q in (_pt(p) for p in (c or [])) if q]
        if not pts:
            return None
        if t == "LineString" and len(pts) == 1:
            return {"type": "Point", "coordinates": pts[0]}
        return {"type": t, "coordinates": pts}
    if t == "MultiLineString":
        lines = []
        for line in c or []:
            pts = [q for q in (_pt(p) for p in (line or [])) if q]
            if pts:
                lines.append(pts)
        if not lines:
            return None
        return {"type": "MultiLineString", "coordinates": lines}
    return None


def _parts(g: Optional[dict]) -> Iterator[List[List[float]]]:
    """Each connected piece of a cleaned geometry as a vertex list (a point: one vertex;
    a MultiPoint: one piece per point)."""
    if not g:
        return
    t, c = g.get("type"), g.get("coordinates")
    if t == "Point":
        yield [c]
    elif t == "MultiPoint":
        for p in c:
            yield [p]
    elif t == "LineString":
        yield c
    elif t == "MultiLineString":
        for line in c:
            yield line


def line_bbox(g: Optional[dict]) -> Optional[Tuple[float, float, float, float]]:
    xs: List[float] = []
    ys: List[float] = []
    for part in _parts(g):
        for x, y in part:
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _in_box(x: float, y: float, box: Sequence[float]) -> bool:
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _segment_hits_box(x0: float, y0: float, x1: float, y1: float,
                      box: Sequence[float]) -> bool:
    """Liang-Barsky: does the segment cross the box (touching counts)?"""
    t0, t1 = 0.0, 1.0
    dx, dy = x1 - x0, y1 - y0
    for p, q in ((-dx, x0 - box[0]), (dx, box[2] - x0), (-dy, y0 - box[1]), (dy, box[3] - y0)):
        if p == 0:
            if q < 0:
                return False
            continue
        r = q / p
        if p < 0:
            t0 = max(t0, r)
        else:
            t1 = min(t1, r)
        if t0 > t1:
            return False
    return True


def geometry_touches(g: Optional[dict], box: Sequence[float]) -> bool:
    """Any vertex inside ``box`` (lonmin, latmin, lonmax, latmax) or any segment crossing
    it. The map frames are Web Mercator, so a lon/lat box is exactly the map's extent."""
    for part in _parts(g):
        if any(_in_box(x, y, box) for x, y in part):
            return True
        for (x0, y0), (x1, y1) in zip(part, part[1:]):
            if _segment_hits_box(x0, y0, x1, y1, box):
                return True
    return False


# ---- NMRoads parsing -----------------------------------------------------------------
_SECTION = re.compile(r"(?:^|<br\s*/?>)\s*(Title|Description|Post Date|Update Date|"
                      r"Expiration Date)\s*:", re.I)
_ROUTE_NAME = re.compile(r"^(I|US|NM|SR|CR)$", re.I)
_ROUTE_NUM = re.compile(r"^\d{1,4}[A-Z]?$", re.I)
_ROAD_NAME = re.compile(r"^(I|US|NM|SR|CR)[- ](\d{1,4}[A-Z]?)$", re.I)
_TITLE_ROUTE = re.compile(r"\b(I|US|NM)[ -](\d{1,4})\b")
_MM = r"mile ?(?:marker|post)\s*([0-9]+(?:\.[0-9]+)?)"
_MM_FROM = re.compile(r"\bfrom " + _MM, re.I)
_MM_TO = re.compile(r"\bto " + _MM, re.I)
_MM_AT = re.compile(r"\bat " + _MM, re.I)


def _rss_sections(raw: str) -> Dict[str, str]:
    """{"title", "description", "post date", "update date", "expiration date"} of an
    NMRoads RSS description (raw HTML, split at its "<br/>Label:" markers)."""
    out: Dict[str, str] = {}
    ms = list(_SECTION.finditer(raw or ""))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(raw)
        key = m.group(1).lower()
        if key not in out:
            out[key] = raw[m.end():end]
    return out


def parse_rss(data: bytes) -> Tuple[Dict[str, dict], Optional[datetime]]:
    """rss.xml -> ({guid: item}, lastBuildDate). Each item: {"guid", "category",
    "rss_title", "title", "segments", "posted", "updated", "updated_raw" (the "Update Date"
    text, for spotting batch re-saves), "route_name", "route_number", "mm_from", "mm_to",
    "direction", "point"}. Raises ValueError on unparseable XML."""
    if re.search(rb"<!(DOCTYPE|ENTITY)", data or b"", re.I):
        raise ValueError("rss.xml declares a DOCTYPE/ENTITY (refused)")    # no entity games
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError("rss.xml is not XML: %s" % e)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("rss.xml has no <channel>")
    built = parse_nm_date(channel.findtext("lastBuildDate") or channel.findtext("pubDate"))
    items: Dict[str, dict] = {}
    for it in channel.findall("item"):
        guid = (it.findtext("guid") or "").strip()
        if not guid:
            continue
        raw = it.findtext("description") or ""
        sec = _rss_sections(raw)
        lat = _num(it.findtext("geo:lat", namespaces=_NS))
        lon = _num(it.findtext("geo:long", namespaces=_NS))
        items[guid] = {
            "guid": guid,
            "category": " ".join((it.findtext("category") or "").split()),
            "rss_title": clean_text(it.findtext("title")),
            "title": clean_text(sec.get("title")),
            "segments": _segments(sec["description"] if "description" in sec
                                  else ("" if sec else raw)),
            "posted": (parse_nm_date(clean_text(sec.get("post date")))
                       or parse_nm_date(it.findtext("pubDate"))),
            "updated": parse_nm_date(clean_text(sec.get("update date"))),
            "updated_raw": clean_text(sec.get("update date")),
            "route_name": (it.findtext("osow:routeName", namespaces=_NS) or "").strip(),
            "route_number": (it.findtext("osow:routeNumber", namespaces=_NS) or "").strip(),
            "mm_from": _num(it.findtext("osow:mileMarkerFrom", namespaces=_NS)),
            "mm_to": _num(it.findtext("osow:mileMarkerTo", namespaces=_NS)),
            "direction": (it.findtext("osow:direction", namespaces=_NS) or "").strip(),
            "point": _pt([lon, lat]) if lon is not None and lat is not None else None,
        }
    return items, built


def parse_json(data: bytes) -> List[dict]:
    """nmroads.json -> [{"id", "category", "title", "road_names", "direction",
    "geometry", "created"}] (``created``: the feature's creation / start date, used only when
    the RSS text is missing, see ``build_event``). Raises ValueError when it is not the
    expected FeatureCollection."""
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError("nmroads.json is not JSON: %s" % e)
    feats = doc.get("features") if isinstance(doc, dict) else None
    if not isinstance(feats, list):
        raise ValueError("nmroads.json has no feature list")
    out = []
    for f in feats:
        if not isinstance(f, dict):
            continue
        props = f.get("properties") if isinstance(f.get("properties"), dict) else {}
        core = props.get("core_details") if isinstance(props.get("core_details"), dict) else {}
        fid = f.get("id") or core.get("data_source_id")
        if not isinstance(fid, (str, int)) or isinstance(fid, bool) or str(fid).strip() == "":
            continue
        names = core.get("road_names") if isinstance(core.get("road_names"), list) else []
        desc = core.get("description")
        direction = core.get("direction")
        created = core.get("creation_date") or props.get("start_date")
        out.append({
            "id": str(fid).strip(),
            "category": " ".join(str(core.get("name") or core.get("event_type") or "").split()),
            "title": clean_text(desc if isinstance(desc, str) else ""),
            "road_names": [n for n in names if isinstance(n, str)],
            "direction": direction if isinstance(direction, str) else "",
            "geometry": clean_geometry(f.get("geometry")),
            "created": util.parse_iso(created) if isinstance(created, str) else None,
        })
    return out


def _route(item: Optional[dict], feat: Optional[dict], title: str) -> Optional[str]:
    if item:
        name, num = item.get("route_name") or "", item.get("route_number") or ""
        if _ROUTE_NAME.match(name) and _ROUTE_NUM.match(num):
            return "%s %s" % (name.upper(), num.upper())
    for n in (feat or {}).get("road_names") or []:
        m = _ROAD_NAME.match(n.strip())
        if m:
            return "%s %s" % (m.group(1).upper(), m.group(2).upper())
    head = re.split(r"\b(?:from|at) mile", title or "", maxsplit=1, flags=re.I)[0]
    m = _TITLE_ROUTE.search(head)
    return "%s %s" % (m.group(1), m.group(2)) if m else None


def _mileposts(title: str, item: Optional[dict], route: Optional[str]
               ) -> Tuple[Optional[float], Optional[float]]:
    m_from, m_to, m_at = _MM_FROM.search(title or ""), _MM_TO.search(title or ""), \
        _MM_AT.search(title or "")
    if m_from:
        return float(m_from.group(1)), (float(m_to.group(1)) if m_to else None)
    if m_at:
        return float(m_at.group(1)), None
    if item and route:
        a, b = item.get("mm_from"), item.get("mm_to")
        if a is not None and (a or b):
            return a, (b if b and b != a else None)
    return None, None


def _direction(item: Optional[dict], feat: Optional[dict]) -> Optional[str]:
    for raw in ((item or {}).get("direction"), (feat or {}).get("direction")):
        d = " ".join(re.split(r"[\s,/]+", (raw or "").strip().lower())).strip()
        if not d or d in ("na", "n/a", "unknown", "none", "null"):
            continue
        words = set(d.split()) - {"and"}
        if d == "both" or words in ({"northbound", "southbound"}, {"eastbound", "westbound"}):
            return "both directions"
        if len(words) == 1 and d in ("northbound", "southbound", "eastbound", "westbound"):
            return d
        return d
    return None


def _clean_title(t: str) -> str:
    t = " ".join((t or "").split())
    t = re.sub(r"\s+([.,;:])", r"\1", t)
    t = re.sub(r"(,\s*)+\.$", "", t)
    t = re.sub(r"\.{2,}", ".", t)
    return t.strip(" ,")


# "Difficult Driving Conditions exist throughout the Socorro - 41-57 area.": a condition for
# a whole NMDOT patrol area, placed at one point of it (not on a road).
_AREA_WIDE = re.compile(r"\bexists? throughout the\b.*\barea\b", re.I)
# NMDOT re-saves many events at once: an "Update Date" with a non-zero fraction of a second
# shared by this many items is a batch stamp, not an update (live 2026-09-23: 19, 14, 12 and 7
# events shared four such stamps, posted between 2021 and 2026).
BATCH_MIN = 3
_FRACTION = re.compile(r"\.\d*[1-9]\d*\s*$")


def batch_stamps(items: Iterable[dict]) -> set:
    """The "Update Date" texts that are batch re-saves (see ``BATCH_MIN``)."""
    counts: Dict[str, int] = {}
    for it in items:
        raw = (it or {}).get("updated_raw") or ""
        if _FRACTION.search(raw):
            counts[raw] = counts.get(raw, 0) + 1
    return {raw for raw, n in counts.items() if n >= BATCH_MIN}


def build_event(feat: Optional[dict], item: Optional[dict], water: bool = True,
                batch: Iterable[str] = ()) -> Tuple[Optional[dict], Optional[str], str, str]:
    """One NMDOT event from its JSON feature and/or RSS item: ``(RoadEvent or None,
    category, reason, dedupe title)``. The RoadEvent is returned for kept AND excluded
    events (its ``kind`` is None when excluded); None only when there is no geometry.

    ``updated`` is the RSS "Update Date", except a batch stamp (``batch``, from
    ``batch_stamps``), which says nothing about the event: then None, and the page ages the
    event from ``posted``. Without the RSS item (JSON only) ``posted`` is the JSON creation
    date and ``time_approx`` is True: those dates run 0 to 7 h behind the RSS post dates
    (live 2026-09-23), so the age is approximate (at most 7 h too old), but a stale event
    still gets its "may have reopened" flag. ``area_wide`` marks a condition reported for a
    whole patrol area at one point: listed, never drawn as if it were on a road."""
    geometry = (feat or {}).get("geometry")
    if geometry is None and item and item.get("point"):
        geometry = {"type": "Point", "coordinates": item["point"]}
    category = ((item or {}).get("category") or (feat or {}).get("category") or "").strip()
    rss_title = (item or {}).get("rss_title") or ""
    if category:                        # the RSS <title> is "<category> - <title or nothing>"
        rss_title = re.sub(r"^\s*%s\s*-\s*" % re.escape(category), "", rss_title)
    title = _clean_title((item or {}).get("title") or (feat or {}).get("title") or rss_title
                         or category)
    segments = (item or {}).get("segments") or []
    narrative = " ".join(s if s[-1:] in ".!?" else s + "." for s in segments)
    kind, known, reason = classify_event(category, title, narrative, water=water)
    if geometry is None:
        return None, category, "no usable geometry", title
    route = _route(item, feat, title)
    mm_from, mm_to = _mileposts(title, item, route)
    ev = {
        "id": (feat or {}).get("id") or (item or {}).get("guid"),
        "kind": kind,
        "category": category or "Event",
        "title": title,
        "route": route,
        "mm_from": mm_from,
        "mm_to": mm_to,
        "direction": _direction(item, feat),
        "cause": make_cause(segments, title),
        "cause_known": bool(known),
        "updated": (item or {}).get("updated"),
        "posted": (item or {}).get("posted"),
        "time_approx": False,
        "area_wide": bool(_AREA_WIDE.search(title) or _AREA_WIDE.search(rss_title)),
        "geometry": geometry,
        "bbox": line_bbox(geometry),
        "source": "NMDOT",
        "link": NMROADS_LINK,
    }
    if item and (item.get("updated_raw") or "") in set(batch or ()):
        ev["updated"] = None
    if item is None and feat and isinstance(feat.get("created"), datetime):
        ev["posted"], ev["time_approx"] = feat["created"], True
    return ev, category, reason, title


def _dedupe_key(ev: dict) -> Tuple[str, Tuple[float, ...]]:
    return _norm(ev["title"]), tuple(round(v, 3) for v in (ev["bbox"] or ()))


def _ts(dt: Optional[datetime]) -> float:
    return dt.timestamp() if isinstance(dt, datetime) else float("-inf")


def _cfgv(cfg, name: str, default: Any) -> Any:
    v = getattr(cfg, name, None)
    return default if v is None else v


def _valid_json(b: bytes) -> bool:
    try:
        parse_json(b)
    except ValueError:
        return False
    return True


def _valid_rss(b: bytes) -> bool:
    try:
        parse_rss(b)
    except ValueError:
        return False
    return True


def _failed_nm(error: str) -> Dict[str, Any]:
    return {"ok": False, "stale": True, "error": error, "age_s": None, "events": [],
            "count_raw": 0, "count_region": 0, "feed_time": None, "excluded": {}}


def fetch_nm_roads(cfg, cache) -> Dict[str, Any]:
    """New Mexico's emergency road closures from NMRoads (see the module docstring):
    ``{"ok", "stale", "error", "age_s", "events": [RoadEvent kept], "count_raw",
    "count_region", "feed_time", "excluded": {category: count}}``. Never raises."""
    try:
        return _fetch_nm_roads(cfg, cache)
    except Exception as e:  # noqa: BLE001 -- a feed surprise must never cost the page
        log.exception("NMRoads processing failed")
        return _failed_nm("NMRoads processing failed: %s: %s" % (type(e).__name__, e))


def _fetch_nm_roads(cfg, cache) -> Dict[str, Any]:
    max_stale = _cfgv(cfg, "roads_max_stale", 3600)
    water = bool(_cfgv(cfg, "roads_water", True))
    jres = http.cached_bytes(cfg, cache, "nmroads_json",
                             _cfgv(cfg, "nmroads_json_url", DEFAULT_JSON_URL), 0, max_stale,
                             accept="application/json, application/geo+json;q=0.9, */*;q=0.5",
                             validate=_valid_json)
    rres = http.cached_bytes(cfg, cache, "nmroads_rss",
                             _cfgv(cfg, "nmroads_rss_url", DEFAULT_RSS_URL), 0, max_stale,
                             accept="application/rss+xml, application/xml;q=0.9, text/xml;q=0.9,"
                                    " */*;q=0.5",
                             validate=_valid_rss)
    errors: List[str] = []
    feats: Optional[List[dict]] = None
    items: Optional[Dict[str, dict]] = None
    built: Optional[datetime] = None
    if jres.get("ok") and jres.get("data") is not None:
        try:
            feats = parse_json(jres["data"])
        except ValueError as e:
            errors.append("nmroads.json: %s" % e)
    if jres.get("error"):
        errors.append("nmroads.json: %s" % jres["error"])
    if rres.get("ok") and rres.get("data") is not None:
        try:
            items, built = parse_rss(rres["data"])
        except ValueError as e:
            errors.append("rss.xml: %s" % e)
    if rres.get("error"):
        errors.append("rss.xml: %s" % rres["error"])
    if feats is None and items is None:
        return _failed_nm("; ".join(errors) or "NMRoads unavailable")
    if feats is None:
        errors.append("nmroads.json unavailable: road lines shown as points")
    if items is None:
        errors.append("rss.xml unavailable: causes unknown (closures listed may include "
                      "planned work), water on road and closures filed as driving "
                      "conditions not detected, times approximate")
    used = [r for r, d in ((jres, feats), (rres, items)) if d is not None]
    ages = [r.get("age_s") for r in used if isinstance(r.get("age_s"), (int, float))]

    # The fresher file decides which events exist (a stale copy of the other may still
    # list an event that has ended, or miss a new one); the other only adds detail.
    json_first = feats is not None and (items is None or (jres.get("age_s") or 0)
                                        <= (rres.get("age_s") or 0))
    by_id = {f["id"]: f for f in feats or []}
    pairs: List[Tuple[Optional[dict], Optional[dict]]]
    if json_first:
        pairs = [(f, (items or {}).get(f["id"])) for f in feats or []]
    else:
        pairs = [(by_id.get(g), it) for g, it in (items or {}).items()]

    excluded: Dict[str, int] = {}
    in_region: List[Tuple[dict, str, str]] = []
    batch = batch_stamps((items or {}).values())
    if batch:
        log.debug("NMRoads: %d batch re-save stamp(s) ignored as update times: %s",
                  len(batch), ", ".join(sorted(batch)))
    for feat, item in pairs:
        ev, category, reason, _ = build_event(feat, item, water=water, batch=batch)
        if ev is None:
            log.debug("NMRoads %s: skipped (%s)", (feat or item or {}).get("id")
                      or (item or {}).get("guid"), reason)
            continue
        if not geo.bbox_intersects(ev["bbox"], NM_BOX):
            continue
        in_region.append((ev, category, reason))
    # duplicates (same title, same place): keep the most recently updated one
    best: Dict[Tuple[str, Tuple[float, ...]], Tuple[dict, str, str]] = {}
    for rec in in_region:
        key = _dedupe_key(rec[0])
        cur = best.get(key)
        if cur is None or _ts(_when(rec[0])) > _ts(_when(cur[0])):
            best[key] = rec
    keep_ids = {id(v[0]) for v in best.values()}
    events = []
    for ev, category, reason in in_region:
        if id(ev) not in keep_ids:
            excluded["duplicate"] = excluded.get("duplicate", 0) + 1
            log.debug("NMRoads %s: duplicate of another event (%s)", ev["id"], ev["title"])
            continue
        if ev["kind"] is None:
            k = category or "unknown type"
            excluded[k] = excluded.get(k, 0) + 1
            log.debug("NMRoads %s excluded: %s (%s)", ev["id"], reason, ev["title"])
            continue
        log.debug("NMRoads %s kept as %s: %s (%s)", ev["id"], ev["kind"], reason, ev["title"])
        events.append(ev)
    events.sort(key=_event_order)
    feed_time = (_http_date(jres.get("last_modified") if feats is not None else None)
                 or _http_date(rres.get("last_modified") if items is not None else None)
                 or built)
    return {"ok": True, "stale": any(bool(r.get("stale")) for r in used),
            "error": "; ".join(errors) or None, "age_s": max(ages) if ages else None,
            "events": events, "count_raw": len(pairs), "count_region": len(in_region),
            "feed_time": feed_time, "excluded": dict(sorted(excluded.items()))}


def _when(ev: dict) -> Optional[datetime]:
    """When an event was last touched: its update time, else its post time."""
    return ev.get("updated") or ev.get("posted")


def _event_order(ev: dict) -> Tuple[int, float]:
    """Closures with a known cause, closures without, then water; newest first (update
    time, else post time)."""
    rank = {"closure": 0, "water": 2}.get(ev.get("kind"), 3)
    if ev.get("kind") == "closure" and not ev.get("cause_known"):
        rank = 1
    return rank, -_ts(_when(ev))


# ---- NWS Local Storm Reports ---------------------------------------------------------
_ROADISH = (r"(road|roads|roadway|roadways|rd|street|streets|st|highway|hwy|drive|dr|lane|ln|"
            r"avenue|ave|route|intersection|crossing)")
LSR_ROAD = re.compile(
    r"\b(roads?|roadways?|rd|routes?|rt|highways?|hwy|interstate|streets?|st|avenue|ave|"
    r"drive|dr|lanes?|ln|blvd|boulevard|frontage|crossings?|arroyos?|bridges?|underpass|"
    r"intersections?|culverts?|county road|cr)\b|\b(i|us|nm|sr|sh|fm|cr)[ -]?\d+\b|"
    r"\bu\.\s?s\.\s?(highway\s+|hwy\s+|route\s+)?\d+\b|\bn\.\s?m\.\s?\d+\b", re.I)
LSR_CLOSED = re.compile(
    r"\bclos(ed|ure|ures|ing)\b|\bimpass[ai]ble\b|\bflood(ed|ing)\b|\bflood ?waters?\b|"
    r"\bwaters?\b[^.;]{0,30}\b(over|across|covering|overtopping|overtopped)\b|\bovertop\w*|"
    r"\bwaters?\b[^.;]{0,20}\b(on|in|onto|into)\s+(the\s+)?" + _ROADISH + r"s?\b|"
    r"\binundat\w*|\bwash(ed)?[- ]?(out|outs|away)\b|\bwashouts?\b|\bstanding water\b|"
    r"\bbarricad\w*|\bunder ?water\b|\bsubmerged\b|\bblock(ed|ing)\b|"
    r"\b(debris|mud|rocks?|boulders?)\b[^.;]{0,40}\b(across|over|covering)\b|"
    r"\b(debris|mud|rocks?|boulders?)\b[^.;]{0,60}?\b(on|onto|into|in|down|blocking)\s+"
    r"(\w+\s+){0,4}?(the\s+)?" + _ROADISH + r"s?\b|"
    r"\bstranded (motorists?|vehicles?|drivers?|cars?)\b|\b(vehicles?|cars?|motorists?|"
    r"drivers?) (were |are |was |is )?(stranded|stuck|trapped|stalled) in (the )?(flood|high "
    r"water|water|mud)|\bdamag(ed|ing) (the )?(road|roadway|highway|pavement)\b", re.I)
# Things that flood without being a road ("Marshalls parking lot flooded near US 491"), and
# words that contain "road" without being one ("a road grader"): removed before matching.
_LSR_NOT_ROAD = re.compile(
    r"\b(parking lots?|yards?|homes?|houses?|basements?|buildings?|propert(y|ies)|fields?|"
    r"campgrounds?|paddocks?|playgrounds?|parks?|school)\s+(was |were |is |are |has been |"
    r"have been )?(flooded|inundated|under ?water)\b|\broad ?graders?\b|\broadrunners?\b|"
    r"\b(in|en) route\b", re.I)
# Roads on White Sands Missile Range are military range routes, not public roads (the same
# missile-range exclusion as for NMDOT's alerts); "WSMR Met reports flooding along Dripping
# Springs Rd." is a public road reported by the range's meteorologists and stays.
_LSR_RANGE = re.compile(
    r"\brange (routes?|roads?)\b|\b(on|within|inside) (the )?(wsmr|white sands missile range)\b",
    re.I)
# IEM prefixes of re-issued reports: "Corrects previous flash flood report from Las
# Nutrias.", "Corrects time on previous ...", "Corrected time.", "Report duplicated with WFO
# ABQ." (the neighbouring office re-issues a report near its border).
_CORRECTION = re.compile(r"^\s*correct(s|ed|ion)\b[^.]*\.\s*", re.I)
_CORRECTS_WHAT = re.compile(r"\bprevious\s+(?P<type>.+?)\s+report\s+from\s+(?P<place>.+?)\s*\.?"
                            r"\s*$", re.I)
_DUPLICATE = re.compile(r"^\s*report duplicated with wfo\s+\w+\s*\.\s*", re.I)
LSR_NEAR_DEG = 0.05         # two reports this close (lat and lon) are at the same place
LSR_CORRECTION_WINDOW = timedelta(hours=48)


def _lsr_about_road(remark: str) -> bool:
    """A sentence of the remark names a road, crossing or arroyo (``LSR_ROAD``) together with
    it being closed, flooded, impassable or covered (``LSR_CLOSED``); not a range route."""
    if _LSR_RANGE.search(remark):
        return False
    for s in _sentences(_LSR_NOT_ROAD.sub(" ", remark)):
        if LSR_ROAD.search(s) and LSR_CLOSED.search(s):
            return True
    return False


def _lsr_parse(f: Any) -> Optional[dict]:
    """One IEM storm-report feature from New Mexico with a time and a point (no road filter
    yet), with its re-issue prefix split off."""
    if not isinstance(f, dict):
        return None
    p = f.get("properties") if isinstance(f.get("properties"), dict) else {}
    state = p.get("state") or p.get("st")
    if not isinstance(state, str) or state.strip().upper() != "NM":
        return None
    remark = " ".join(str(p.get("remark") or "").split())
    if not remark:
        return None
    t = util.parse_iso(p.get("valid")) if isinstance(p.get("valid"), str) else None
    if t is None:
        return None
    g = clean_geometry(f.get("geometry"))
    if g is None or g["type"] != "Point":
        pt = _pt([p.get("lon"), p.get("lat")])
        g = {"type": "Point", "coordinates": pt} if pt else None
    if g is None:
        return None
    lon, lat = g["coordinates"]
    typetext = " ".join(str(p.get("typetext") or "").split())
    wfo = " ".join(str(p.get("wfo") or "").split()).upper()
    product = str(p.get("product_id") or "")
    ident = "%s|%s|%s|%s|%s|%s" % (product, p.get("valid"), lat, lon, typetext, remark)
    rid = hashlib.sha1(ident.encode("utf-8")).hexdigest()[:12]
    place = " ".join(str(p.get("city") or "").split())
    core, reissue, ptype, pplace = remark, None, "", ""
    m = _DUPLICATE.match(remark)
    if m:
        core, reissue = remark[m.end():], "duplicate"
    else:
        m = _CORRECTION.match(remark)
        if m:
            core, reissue = remark[m.end():], "correction"
            w = _CORRECTS_WHAT.search(m.group(0))
            if w:
                ptype, pplace = _norm(w.group("type")), _norm(w.group("place"))
    return {"id": "lsr-" + rid, "kind": "report", "type": typetext, "time": t, "place": place,
            "county": " ".join(str(p.get("county") or "").split()), "remark": remark,
            "lat": lat, "lon": lon, "geometry": g, "bbox": (lon, lat, lon, lat),
            "source": "NWS %s" % wfo if wfo else "NWS", "_product": product,
            "_core": _norm(core), "_core_text": core, "_reissue": reissue, "_ptype": ptype,
            "_pplace": pplace}


def _lsr_near(a: dict, b: dict) -> bool:
    return abs(a["lat"] - b["lat"]) <= LSR_NEAR_DEG and abs(a["lon"] - b["lon"]) <= LSR_NEAR_DEG


def _lsr_corrected(r: dict, kept: Sequence[dict]) -> Optional[dict]:
    """The earlier report that the re-issue ``r`` replaces, or None: the same text at the same
    place and type (a re-issue, a "Corrected time." copy); for "Corrects previous <type>
    report from <place>." the most similar report of that type at that place, else the one
    with the same time, else the only one within 48 h (never a guess among several); for a
    "Report duplicated with WFO X." copy the report with the same text and time."""
    same = [o for o in kept if o["_core"] == r["_core"] and _lsr_near(o, r)
            and (o["type"].upper() == r["type"].upper() or r["_reissue"] == "duplicate")]
    if r["_reissue"] == "duplicate":
        same = [o for o in same if o["time"] == r["time"]]
    if same:
        return same[-1]
    if r["_reissue"] != "correction":
        return None
    want = r["_ptype"] or _norm(r["type"])
    cands = [o for o in kept if _norm(o["type"]) == want
             and ((r["_pplace"] and _norm(o["place"]) == r["_pplace"]) or _lsr_near(o, r))]
    if not cands:
        return None
    scored = sorted(((difflib.SequenceMatcher(None, o["_core"], r["_core"]).ratio(), i, o)
                     for i, o in enumerate(cands)), key=lambda x: (x[0], x[1]))
    if scored[-1][0] >= 0.6:
        return scored[-1][2]
    timed = [o for o in cands if o["time"] == r["time"]]
    if timed:
        return timed[-1]
    close = [o for o in cands if abs(o["time"] - r["time"]) <= LSR_CORRECTION_WINDOW]
    return close[0] if len(close) == 1 else None


def lsr_road_reports(features: Iterable[Any]) -> List[dict]:
    """New Mexico storm reports that name a closed / flooded / impassable road, after the
    re-issues are folded in (in product order, a correction or a repeat replaces the report
    it re-issues, and another office's duplicate is dropped). A correction whose own text no
    longer names the road ("NM-149 should read NM-143.") keeps the corrected report's text
    with the correction appended. Newest first; the time window is the caller's."""
    recs = [r for r in (_lsr_parse(f) for f in features or []) if r is not None]
    recs.sort(key=lambda r: (r["_product"], _ts(r["time"])))
    kept: List[dict] = []
    for r in recs:
        old = _lsr_corrected(r, kept) if (r["_reissue"] or kept) else None
        if old is not None and r["_reissue"] == "duplicate":
            continue                                   # the originating office's copy stays
        if old is not None:
            kept.remove(old)
            if (r["_reissue"] == "correction" and not _lsr_about_road(r["remark"])
                    and _lsr_about_road(old["remark"])
                    and _norm(old["type"]) == _norm(r["type"])):
                fixed = "%s (Corrected by NWS: %s)" % (old["_core_text"], r["_core_text"])
                r = dict(r, remark=fixed)
        kept.append(r)
    out = [r for r in kept if _lsr_about_road(r["remark"])]
    out.sort(key=lambda r: -_ts(r["time"]))
    for r in out:
        for k in ("_product", "_core", "_core_text", "_reissue", "_ptype", "_pplace"):
            r.pop(k, None)
    return out


def _lsr_report(f: Any) -> Optional[dict]:
    """One feature as a RoadReport when it is a New Mexico report about a road (no re-issue
    handling; see ``lsr_road_reports``)."""
    got = lsr_road_reports([f])
    return got[0] if got else None


def fetch_storm_reports(cfg, cache) -> Dict[str, Any]:
    """NWS Local Storm Reports from New Mexico that name a closed / flooded / impassable
    road, crossing or arroyo, from the last ``cfg.lsr_hours`` hours:
    ``{"ok", "stale", "error", "age_s", "reports": [RoadReport]}``, newest first. A
    corrected or duplicated re-issue replaces the report it re-issues. Never raises."""
    if not _cfgv(cfg, "lsr_enabled", True):
        return {"ok": True, "stale": False, "error": None, "age_s": None, "reports": [],
                "disabled": True}
    try:
        return _fetch_storm_reports(cfg, cache)
    except Exception as e:  # noqa: BLE001
        log.exception("storm report processing failed")
        return {"ok": False, "stale": True, "error": "storm report processing failed: %s: %s"
                % (type(e).__name__, e), "age_s": None, "reports": []}


def _lsr_url(cfg) -> str:
    hours = int(_cfgv(cfg, "lsr_hours", 24))
    url = _cfgv(cfg, "lsr_url", DEFAULT_LSR_URL)
    try:
        return url.format(hours=hours)
    except (KeyError, IndexError, ValueError):
        return url


def _fetch_storm_reports(cfg, cache) -> Dict[str, Any]:
    hours = int(_cfgv(cfg, "lsr_hours", 24))
    res = http.cached_json(cfg, cache, "lsr", _lsr_url(cfg), 0,
                           _cfgv(cfg, "roads_max_stale", 3600),
                           accept="application/geo+json, application/json;q=0.9, */*;q=0.5",
                           validate=lambda d: isinstance(d, dict)
                           and isinstance(d.get("features"), list))
    out = {"ok": bool(res.get("ok")), "stale": bool(res.get("stale")), "error": res.get("error"),
           "age_s": res.get("age_s"), "reports": []}
    if not res.get("ok"):
        return out
    # re-issues are folded in before the time window is applied (a correction may move
    # the time), also on a stale copy
    reports = lsr_road_reports((res.get("data") or {}).get("features") or [])
    cutoff = util.utcnow() - timedelta(hours=hours)
    out["reports"] = [r for r in reports if r["time"] >= cutoff]
    return out


# ---- per-site selection --------------------------------------------------------------
def _items(src: Any, key: str) -> Iterable[dict]:
    if not isinstance(src, dict):
        return []
    v = src.get(key)
    return [x for x in v if isinstance(x, dict)] if isinstance(v, (list, tuple)) else []


_NM_RING = [list(p) for p in NM_OUTLINE + NM_OUTLINE[:1]]
_NM_LINE = {"type": "LineString", "coordinates": _NM_RING}


def bbox_covers_nm(bbox: Sequence[float]) -> bool:
    """Whether a map box (lonmin, latmin, lonmax, latmax) reaches New Mexico (``NM_OUTLINE``),
    the only state these road sources cover: the outline crosses or touches the box, or the
    box lies wholly inside it. make_weather_page fetches nothing when no site's map does;
    ``site_roads`` reports it per map as ``covers_nm``."""
    box = tuple(float(v) for v in bbox)
    if geometry_touches(_NM_LINE, box):
        return True
    return geo.point_in_ring((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0, _NM_RING)


def site_roads(nm: Optional[dict], lsr: Optional[dict], frame) -> Dict[str, Any]:
    """The road items on one map: ``{"covers_nm": bool, "events": [...], "reports": [...]}``.
    ``covers_nm`` says whether the frame reaches New Mexico at all (a map wholly in another
    state does not); an item is on the map when a vertex lies inside ``frame.bbox`` or a
    segment crosses it. Events: closures (cause known first) then water on road, each newest
    first; reports newest first."""
    box = tuple(frame.bbox)
    events = [e for e in _items(nm, "events") if e.get("kind") in ("closure", "water")
              and geometry_touches(e.get("geometry"), box)]
    events.sort(key=_event_order)
    reports = [r for r in _items(lsr, "reports") if geometry_touches(r.get("geometry"), box)]
    reports.sort(key=lambda r: -_ts(r.get("time")))
    return {"covers_nm": bbox_covers_nm(box), "events": events, "reports": reports}
