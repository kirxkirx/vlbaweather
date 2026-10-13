"""NWS alerts for local-weather-awareness: fetch the active feed, filter it, resolve zone shapes,
classify alerts per site, lift impact-based threat tags, and the watch/warning/advisory
colour table.

Three facts about the api.weather.gov alert feed drive the design here:

* Polygon-based products (Flash Flood / Severe Thunderstorm / Tornado Warnings) carry a
  GeoJSON polygon; zone-based products (watches, advisories, statements, most Flood
  Watches) carry ``geometry: null`` and a list of ``affectedZones`` URLs. Shading the
  latter on a map means fetching each zone's outline once and caching it forever.
* For a polygon-based product ``affectedZones`` lists every county the polygon merely
  touches, and many western counties are larger than the 100-mile map. So only the
  polygon decides whether a site is "AT" such an alert (NWS's own ``?point=`` query does
  the same). A zone-based product is AT a site when the site's own forecast/county/fire
  zone is listed or the point is inside the merged zone outline; the zone test also
  works when an outline could not be fetched, so a viewer never misses a watch just
  because a map layer failed.
* Impact-based warnings put the damage threat (Tornado / Flash Flood Emergency, PDS,
  "destructive" storms) in ``parameters``, not in ``event`` or ``headline``; ``normalize``
  lifts it into ``threat`` so the page can show it without the viewer opening the text.

Colours follow the official NWS chart except for inland flood products, which this page
draws in reds instead of the chart's greens (``FLOOD_COLORS``), and any per-event
override from ``cfg.alert_color_overrides`` (``WEATHER_ALERT_COLORS``).

Nothing here raises on network or data problems: fetch results carry the common status
keys and bad features are skipped with a log line.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from . import geo, http, util

log = logging.getLogger("weather.alerts")

BASE = "https://api.weather.gov"

ZONE_TTL = 365 * 86400              # zone outlines practically never change
ZONE_MAX_STALE = 10 * 365 * 86400   # ... so a last-good copy is fine for a decade
# Zone outlines not yet on disk are fetched only while at least this share of the run's
# network budget is left. A first run during a statewide event (hundreds of zones at ~0.4 s
# each) would otherwise spend the budget on outlines and leave the forecasts and
# observations unfetched; the outlines left out are fetched by the next runs instead.
ZONE_BUDGET_KEEP = 0.5

# Sort key for "end_dt or far future": open-ended alerts sort after dated ones.
FAR_FUTURE = datetime(9999, 1, 1, tzinfo=timezone.utc)

# ---- colours -------------------------------------------------------------------------
# Official NWS "Watch, Warning, Advisory Display" colours (weather.gov/help-map, checked
# row by row against the chart on 2026-09-23), plus a few legacy/marine event names the
# chart no longer lists. Keys are the exact ``event`` strings the API sends; lookups are
# case-insensitive (see color_for). This table stays verbatim; the page's own choices are
# layered on top in FLOOD_COLORS / EVENT_COLORS below.
NWS_COLORS: Dict[str, str] = {
    # -- warnings: life/property threats
    "Tsunami Warning": "#FD6347",
    "Tornado Warning": "#FF0000",
    "Extreme Wind Warning": "#FF8C00",
    "Severe Thunderstorm Warning": "#FFA500",
    "Flash Flood Warning": "#8B0000",
    "Flash Flood Statement": "#8B0000",
    "Severe Weather Statement": "#00FFFF",
    "Shelter In Place Warning": "#FA8072",
    "Evacuation Immediate": "#7FFF00",
    "Civil Danger Warning": "#FFB6C1",
    "Nuclear Power Plant Warning": "#4B0082",
    "Radiological Hazard Warning": "#4B0082",
    "Hazardous Materials Warning": "#4B0082",
    "Fire Warning": "#A0522D",
    "Civil Emergency Message": "#FFB6C1",
    "Law Enforcement Warning": "#C0C0C0",
    "Storm Surge Warning": "#B524F7",
    "Hurricane Force Wind Warning": "#CD5C5C",
    "Hurricane Warning": "#DC143C",
    "Typhoon Warning": "#DC143C",
    "Special Marine Warning": "#FFA500",
    "Blizzard Warning": "#FF4500",
    "Snow Squall Warning": "#C71585",
    "Ice Storm Warning": "#8B008B",
    "Heavy Freezing Spray Warning": "#00BFFF",
    "Winter Storm Warning": "#FF69B4",
    "Lake Effect Snow Warning": "#008B8B",
    "Dust Storm Warning": "#FFE4C4",
    "Blowing Dust Warning": "#FFE4C4",
    "High Wind Warning": "#DAA520",
    "Tropical Storm Warning": "#B22222",
    "Storm Warning": "#9400D3",
    "Gale Warning": "#DDA0DD",
    "Hazardous Seas Warning": "#D8BFD8",
    "Avalanche Warning": "#1E90FF",
    "Earthquake Warning": "#8B4513",
    "Volcano Warning": "#2F4F4F",
    "Ashfall Warning": "#A9A9A9",
    "Flood Warning": "#00FF00",
    "Flood Statement": "#00FF00",
    "Coastal Flood Warning": "#228B22",
    "Lakeshore Flood Warning": "#228B22",
    "High Surf Warning": "#228B22",
    "Extreme Heat Warning": "#C71585",
    "Excessive Heat Warning": "#C71585",
    "Extreme Cold Warning": "#0000FF",
    "Wind Chill Warning": "#B0C4DE",
    "Hard Freeze Warning": "#9400D3",
    "Freeze Warning": "#483D8B",
    "Red Flag Warning": "#FF1493",
    # -- watches: conditions favourable
    "Tornado Watch": "#FFFF00",
    "Severe Thunderstorm Watch": "#DB7093",
    "Flood Watch": "#2E8B57",
    "Flash Flood Watch": "#2E8B57",
    "Coastal Flood Watch": "#66CDAA",
    "Lakeshore Flood Watch": "#66CDAA",
    "Tsunami Watch": "#FF00FF",
    "Hurricane Watch": "#FF00FF",
    "Hurricane Force Wind Watch": "#9932CC",
    "Typhoon Watch": "#FF00FF",
    "Tropical Storm Watch": "#F08080",
    "Storm Watch": "#FFE4B5",
    "Storm Surge Watch": "#DB7FF7",
    "Gale Watch": "#FFC0CB",
    "Hazardous Seas Watch": "#483D8B",
    "Heavy Freezing Spray Watch": "#BC8F8F",
    "Winter Storm Watch": "#4682B4",
    "Blizzard Watch": "#ADFF2F",
    "Lake Effect Snow Watch": "#87CEFA",
    "Avalanche Watch": "#F4A460",
    "High Wind Watch": "#B8860B",
    "Excessive Heat Watch": "#800000",
    "Extreme Heat Watch": "#800000",
    "Extreme Cold Watch": "#5F9EA0",
    "Wind Chill Watch": "#5F9EA0",
    "Hard Freeze Watch": "#4169E1",
    "Freeze Watch": "#00FFFF",
    "Fire Weather Watch": "#FFDEAD",
    # -- advisories: inconveniences, be aware
    "Tsunami Advisory": "#D2691E",
    "Winter Weather Advisory": "#7B68EE",
    "Freezing Rain Advisory": "#7B68EE",
    "Blowing Snow Advisory": "#7B68EE",
    "Lake Effect Snow Advisory": "#48D1CC",
    "Wind Chill Advisory": "#AFEEEE",
    "Cold Weather Advisory": "#AFEEEE",
    "Heat Advisory": "#FF7F50",
    "Flood Advisory": "#00FF7F",
    "Urban and Small Stream Flood Advisory": "#00FF7F",
    "Small Stream Flood Advisory": "#00FF7F",
    "Arroyo and Small Stream Flood Advisory": "#00FF7F",
    "Hydrologic Advisory": "#00FF7F",
    "Coastal Flood Advisory": "#7CFC00",
    "Lakeshore Flood Advisory": "#7CFC00",
    "High Surf Advisory": "#BA55D3",
    "Dense Fog Advisory": "#708090",
    "Freezing Fog Advisory": "#008080",
    "Dense Smoke Advisory": "#F0E68C",
    "Small Craft Advisory": "#D8BFD8",
    "Small Craft Advisory For Hazardous Seas": "#D8BFD8",
    "Small Craft Advisory for Rough Bar": "#D8BFD8",
    "Small Craft Advisory for Winds": "#D8BFD8",
    "Brisk Wind Advisory": "#D8BFD8",
    "Freezing Spray Advisory": "#00BFFF",
    "Low Water Advisory": "#A52A2A",
    "Dust Advisory": "#BDB76B",
    "Blowing Dust Advisory": "#BDB76B",
    "Wind Advisory": "#D2B48C",
    "Lake Wind Advisory": "#D2B48C",
    "Frost Advisory": "#6495ED",
    "Ashfall Advisory": "#696969",
    "Avalanche Advisory": "#CD853F",
    "Air Stagnation Advisory": "#808080",
    # -- statements, outlooks and non-weather messages
    "Special Weather Statement": "#FFE4B5",
    "Marine Weather Statement": "#FFDAB9",
    "Coastal Flood Statement": "#6B8E23",
    "Lakeshore Flood Statement": "#6B8E23",
    "Rip Current Statement": "#40E0D0",
    "Beach Hazards Statement": "#40E0D0",
    "Hurricane Local Statement": "#FFE4B5",
    "Typhoon Local Statement": "#FFE4B5",
    "Tropical Storm Local Statement": "#FFE4B5",
    "Tropical Depression Local Statement": "#FFE4B5",
    "Tropical Cyclone Local Statement": "#FFE4B5",
    "Air Quality Alert": "#808080",
    "Hazardous Weather Outlook": "#EEE8AA",
    "Hydrologic Outlook": "#90EE90",
    "Short Term Forecast": "#98FB98",
    "Extreme Fire Danger": "#E9967A",
    "Local Area Emergency": "#C0C0C0",
    "911 Telephone Outage": "#C0C0C0",
    "Administrative Message": "#C0C0C0",
    "Child Abduction Emergency": "#FFFFFF",
    "Blue Alert": "#FFFFFF",
    "Test": "#F0FFFF",
}

# Deliberate deviation from the NWS chart: inland flood products are drawn in reds.
# The chart paints Flood Watch sea green, Flood Warning lime and Flood Advisory spring
# green; on this page green reads as "good" (and blends into the 15-35 dBZ radar greens
# a flood situation brings), so the owner asked for flood areas in red. Darker = more
# serious: advisory/statement salmon < watch red < warning dark red < flash flood warning
# (the official #8B0000, unchanged). Tornado Warning (#FF0000) is red as well; the map
# legend and the cards always name the event. Coastal/Lakeshore flood keep NWS colours.
FLOOD_COLORS: Dict[str, str] = {
    "Flood Watch": "#E53935",
    "Flash Flood Watch": "#E53935",
    "Flood Warning": "#C62828",
    "Flash Flood Warning": "#8B0000",
    "Flash Flood Statement": "#8B0000",
    "Flood Advisory": "#FA8072",
    "Flood Statement": "#FA8072",
    "Hydrologic Outlook": "#F4A6A6",
    "Arroyo and Small Stream Flood Advisory": "#FA8072",
    "Urban and Small Stream Flood Advisory": "#FA8072",
    "Small Stream Flood Advisory": "#FA8072",
    "Hydrologic Advisory": "#FA8072",
}

# What color_for actually uses: the chart with the flood rows replaced.
EVENT_COLORS: Dict[str, str] = dict(NWS_COLORS)
EVENT_COLORS.update(FLOOD_COLORS)

# Fallback per kind for events not in the table (new products appear now and then).
KIND_COLORS = {"warning": "#d00000", "watch": "#e6b800", "advisory": "#7b68ee",
               "statement": "#ffe4b5", "other": "#808080"}

KIND_RANK = {"warning": 0, "watch": 1, "advisory": 2, "statement": 3, "other": 4}
SEVERITY_RANK = {"extreme": 0, "severe": 1, "moderate": 2, "minor": 3, "unknown": 4}

_COLORS_LC = {k.lower(): v for k, v in EVENT_COLORS.items()}
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_STATEMENT_SUFFIXES = ("statement", "outlook", "alert", "message", "forecast", "emergency")


def event_kind(event: Optional[str]) -> str:
    """Classify an event name by its last word: warning | watch | advisory | statement |
    other. The suffix is what NWS itself uses to grade products, and it survives new
    product names ("Snow Squall Warning" was new in 2018 and needs no table entry)."""
    name = (event or "").strip().lower()
    if name.endswith("warning"):
        return "warning"
    if name.endswith("watch"):
        return "watch"
    if name.endswith("advisory"):
        return "advisory"
    if name.endswith(_STATEMENT_SUFFIXES):
        return "statement"
    return "other"


def color_for(event: Optional[str], kind: Optional[str] = None) -> str:
    """Colour for ``event`` from EVENT_COLORS (the NWS chart with the flood products
    recoloured; case-insensitive), else the kind's fallback. Per-deployment overrides
    (``cfg.alert_color_overrides``) are applied by fetch_active, not here."""
    c = _COLORS_LC.get((event or "").strip().lower())
    if c:
        return c
    return KIND_COLORS.get(kind or event_kind(event), KIND_COLORS["other"])


def alert_rank(kind: str, severity: Optional[str]) -> int:
    """(kind_rank, severity_rank) flattened into one int so records sort with a plain
    key: 0 = Extreme warning ... 44 = unknown-severity oddity."""
    k = KIND_RANK.get(kind, KIND_RANK["other"])
    s = SEVERITY_RANK.get((severity or "").strip().lower(), SEVERITY_RANK["unknown"])
    return k * 10 + s


# ---- normalisation -------------------------------------------------------------------
def _text(v) -> str:
    return v if isinstance(v, str) else ""


def _text_or_none(v) -> Optional[str]:
    return v if isinstance(v, str) and v.strip() else None


def zone_id_from_url(url: str) -> str:
    """'https://api.weather.gov/zones/county/NMC027' -> 'NMC027'."""
    return _text(url).rstrip("/").rsplit("/", 1)[-1]


def _param_values(params, key: str) -> List[str]:
    """String values of one CAP parameter. The API sends every parameter as a list
    (``"tornadoDamageThreat": ["CATASTROPHIC"]``); a bare string is tolerated too."""
    v = params.get(key) if isinstance(params, dict) else None
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [x.strip() for x in v if isinstance(x, str) and x.strip()]


# Impact-based-warning damage tags -> chip text, most serious first. The tornado and
# flash-flood CATASTROPHIC tags only exist on those warnings and their follow-ups, so
# the tag alone decides (an emergency must never be missed because of the event name).
# CONSIDERABLE means "particularly dangerous situation" only for tornadoes; on a Severe
# Thunderstorm Warning it is the middle damage tier (70 mph / 1.75 in hail), labelled so.
_THREATS = (
    ("tornadoDamageThreat", "CATASTROPHIC", "TORNADO EMERGENCY"),
    ("flashFloodDamageThreat", "CATASTROPHIC", "FLASH FLOOD EMERGENCY"),
    ("tornadoDamageThreat", "CONSIDERABLE", "PDS"),
    ("thunderstormDamageThreat", "DESTRUCTIVE", "DESTRUCTIVE"),
    ("flashFloodDamageThreat", "CONSIDERABLE", "CONSIDERABLE FLASH FLOODING"),
    ("thunderstormDamageThreat", "CONSIDERABLE", "CONSIDERABLE DAMAGE"),
)


def alert_threat(params) -> Optional[str]:
    """The most serious damage-threat tag in a CAP ``parameters`` block as short chip
    text ("TORNADO EMERGENCY", "FLASH FLOOD EMERGENCY", "PDS", "DESTRUCTIVE",
    "CONSIDERABLE FLASH FLOODING", "CONSIDERABLE DAMAGE"), or None for a base-level
    warning / a product without such tags."""
    for key, level, label in _THREATS:
        if level in (v.upper() for v in _param_values(params, key)):
            return label
    return None


def _polygon_geometry(g) -> Optional[dict]:
    """Keep a geometry only if it actually contains polygons; a Point or an empty
    collection is as useless for shading and point tests as ``null`` and should let the
    zone fallback kick in."""
    if isinstance(g, dict) and geo.geometry_bbox(g) is not None:
        return g
    return None


def normalize(feature: dict) -> dict:
    """One GeoJSON alert feature -> AlertRec (see DESIGN.md). Pure: no network, never
    raises on missing keys — the feed occasionally omits ``ends``, ``instruction``,
    ``web`` or ``geometry`` and a page must not die over that."""
    feature = feature if isinstance(feature, dict) else {}
    props = feature.get("properties")
    props = props if isinstance(props, dict) else {}
    event = _text(props.get("event")).strip()
    kind = event_kind(event)
    severity = _text(props.get("severity")) or "Unknown"
    zone_urls = [u for u in (props.get("affectedZones") or []) if isinstance(u, str) and u]
    geometry = _polygon_geometry(feature.get("geometry"))
    ends = _text_or_none(props.get("ends"))
    expires = _text_or_none(props.get("expires"))
    params = props.get("parameters")
    headlines = _param_values(params, "NWSheadline")
    return {
        "id": props.get("id") or feature.get("id"),
        "event": event,
        "severity": severity,
        "urgency": _text(props.get("urgency")) or "Unknown",
        "certainty": _text(props.get("certainty")) or "Unknown",
        "kind": kind,
        "rank": alert_rank(kind, severity),
        "color": color_for(event, kind),
        "headline": _text(props.get("headline")),
        "nws_headline": headlines[0] if headlines else None,
        "threat": alert_threat(params),
        "description": _text(props.get("description")),
        "instruction": _text_or_none(props.get("instruction")),
        "area_desc": _text(props.get("areaDesc")),
        "sender": _text(props.get("senderName")) or _text(props.get("sender")),
        "web": _text_or_none(props.get("web")),
        "message_type": _text_or_none(props.get("messageType")),
        "status": _text_or_none(props.get("status")),
        "sent": _text_or_none(props.get("sent")),
        "effective": _text_or_none(props.get("effective")),
        "onset": _text_or_none(props.get("onset")),
        "ends": ends,
        "expires": expires,
        "end_dt": util.parse_iso(ends) or util.parse_iso(expires),
        "affected_zone_urls": zone_urls,
        "affected_zone_ids": [zone_id_from_url(u) for u in zone_urls],
        "geometry": geometry,
        "geometry_source": "polygon" if geometry is not None else None,
        "bbox": geo.geometry_bbox(geometry),
    }


# ---- ordering ------------------------------------------------------------------------
def _sort_key(a: dict):
    end = a.get("end_dt")
    if not isinstance(end, datetime) or end.tzinfo is None:
        end = FAR_FUTURE
    rank = a.get("rank")
    return (rank if isinstance(rank, int) else 99, _text(a.get("event")), end)


def sort_alerts(alerts: Optional[Iterable[dict]]) -> list:
    """Warnings first (Extreme before Severe ...), then watches, advisories, statements;
    same product sorted by end time, open-ended last. The map draws the REVERSE of this
    so a warning is painted on top of the watch that usually surrounds it."""
    return sorted(alerts or [], key=_sort_key)


# ---- the active feed -----------------------------------------------------------------
def _is_live(rec: dict, now: datetime) -> bool:
    """Actual Alert/Update messages that have not ended. Cancel/Expire messages, tests
    and exercises are dropped here; an alert with neither ``ends`` nor ``expires`` is
    kept (rare, but "no end given" must not read as "already over")."""
    if rec.get("status") != "Actual":
        return False
    if rec.get("message_type") not in ("Alert", "Update"):
        return False
    end = rec.get("end_dt")
    return end is None or end > now


def _valid_feed(d) -> bool:
    return isinstance(d, dict) and isinstance(d.get("features"), list)


def _color_overrides(cfg) -> Dict[str, str]:
    """``cfg.alert_color_overrides`` ({lowercased event: "#RRGGBB"}, validated by the
    config) re-checked here, so a hand-built cfg or a bad value can only cost the
    override, never the alerts."""
    try:
        raw = getattr(cfg, "alert_color_overrides", None)
    except Exception as e:  # noqa: BLE001 — a bad WEATHER_ALERT_COLORS is not fatal here
        log.warning("ignoring alert colour overrides: %s", e)
        return {}
    out = {}
    for k, v in (raw.items() if isinstance(raw, dict) else ()):
        if isinstance(k, str) and k.strip() and isinstance(v, str) and _HEX_RE.match(v.strip()):
            out[k.strip().lower()] = v.strip().upper()
        else:
            log.warning("ignoring alert colour override %r=%r", k, v)
    return out


def _area_codes(cfg) -> List[str]:
    """The states, territories and marine areas to ask for: ``cfg.alert_area_codes``
    (``WEATHER_ALERT_AREAS``, where ``auto`` means those on the sites' maps); a plain
    ``alert_areas`` string for a config object without that property."""
    codes = getattr(cfg, "alert_area_codes", None)
    if codes is None:
        codes = [c.strip().upper() for c in str(cfg.alert_areas).split(",") if c.strip()]
    return list(codes)


def fetch_active(cfg, cache) -> dict:
    """Active alerts for the states and territories in ``cfg.alert_area_codes``. ttl 0 = ask
    NWS every run; on failure the last good feed is reused for ``cfg.alerts_max_stale``
    (flagged stale) so a hiccup does not silently blank the alerts banner.
    ``cfg.alert_color_overrides`` (from ``WEATHER_ALERT_COLORS``) replaces the colour of the
    named events, case-insensitively."""
    out = {"ok": False, "stale": True, "error": None, "age_s": None,
           "fetched_ts": None, "alerts": [], "count_raw": 0}
    try:
        areas = ",".join(_area_codes(cfg))
        url = "%s/alerts/active?area=%s" % (BASE, areas)
        res = http.cached_json(cfg, cache, "alerts", url, ttl=0, max_stale=cfg.alerts_max_stale,
                               validate=_valid_feed)
    except Exception as e:  # noqa: BLE001 — never let the feed take the page down
        out["error"] = "%s: %s" % (type(e).__name__, e)
        log.error("alerts fetch crashed: %s", out["error"])
        return out
    out.update({"ok": bool(res.get("ok")), "stale": bool(res.get("stale")),
                "error": res.get("error"), "age_s": res.get("age_s")})
    data = res.get("data")
    if not _valid_feed(data):
        out["ok"] = False
        out["error"] = out["error"] or "no alert data"
        return out
    if out["age_s"] is not None:
        out["fetched_ts"] = time.time() - float(out["age_s"])
    features = data["features"]
    out["count_raw"] = len(features)
    now = util.utcnow()
    overrides = _color_overrides(cfg)
    kept = []
    for f in features:
        try:
            rec = normalize(f)
        except Exception as e:  # noqa: BLE001 — one odd feature must not drop the rest
            log.warning("skipping malformed alert feature: %s", e)
            continue
        if _is_live(rec, now):
            rec["color"] = overrides.get(rec["event"].lower(), rec["color"])
            kept.append(rec)
        else:
            log.debug("dropping %s (%s/%s, ends %s)", rec.get("event"), rec.get("status"),
                      rec.get("message_type"), rec.get("ends") or rec.get("expires"))
    out["alerts"] = sort_alerts(kept)
    log.info("alerts: %d active of %d in feed for %s%s", len(kept), len(features),
             areas, " (STALE: %s)" % out["error"] if out["stale"] else "")
    return out


# ---- zone geometry -------------------------------------------------------------------
_DEFERRED = object()     # _zone_geometry: not on disk, and not fetched to save the budget


def _may_fetch_zones() -> bool:
    """Whether zone outlines that are not on disk yet may still be fetched in this run: at
    least ``ZONE_BUDGET_KEEP`` of the run's network budget is left (always, without one)."""
    left = http.budget_left()
    if left is None:
        return True
    total = http.run_status().get("budget_s")
    return not total or left >= ZONE_BUDGET_KEEP * float(total)


def _zone_geometry(cfg, cache, zone_id: str, url: str, memo: Dict[str, Optional[dict]],
                   may_fetch: bool = True):
    """Outline of one zone, memoised per run and cached on disk practically forever.
    Returns None when the zone cannot be fetched OR when NWS itself has no outline for
    it (some marine zones): both just mean "nothing to shade for this zone". Returns
    ``_DEFERRED`` (not memoised) when the outline is not on disk and ``may_fetch`` is
    False."""
    if zone_id in memo:
        return memo[zone_id]
    if not may_fetch and cache.get("zone/%s" % zone_id, ZONE_TTL) is None:
        return _DEFERRED
    geom = None
    try:
        res = http.cached_json(cfg, cache, "zone/%s" % zone_id, url, ttl=ZONE_TTL,
                               max_stale=ZONE_MAX_STALE,
                               validate=lambda d: isinstance(d, dict) and "geometry" in d)
        if res.get("ok") and isinstance(res.get("data"), dict):
            geom = _polygon_geometry(res["data"].get("geometry"))
            if geom is None:
                log.debug("zone %s has no polygon geometry", zone_id)
        else:
            log.warning("zone %s unavailable: %s", zone_id, res.get("error"))
    except Exception as e:  # noqa: BLE001
        log.warning("zone %s lookup crashed: %s", zone_id, e)
    memo[zone_id] = geom
    return geom


def resolve_geometries(cfg, cache, alerts: Optional[Iterable[dict]]) -> None:
    """Give zone-based alerts (``geometry is None``) a shape: the union of their zones'
    outlines. Mutates the records in place. Zones that fail stay missing; an alert whose
    zones all fail keeps ``geometry None`` and is still listed AT a site via zone ids.

    Outlines already on disk are always used. New ones are fetched, in the alerts' order
    (warnings first), only while ``ZONE_BUDGET_KEEP`` of the run's network budget is left;
    an alert that then still lacks an outline stays without a shape this run (listed, not
    shaded, rather than shaded in part), and the next run goes on where this one stopped."""
    memo: Dict[str, Optional[dict]] = {}
    deferred = 0
    for a in alerts or []:
        if not isinstance(a, dict) or a.get("geometry") is not None:
            continue
        urls = a.get("affected_zone_urls") or []
        if not urls:
            continue
        geoms = []
        missing = False
        for url in urls:
            zid = zone_id_from_url(url)
            if not zid:
                continue
            g = _zone_geometry(cfg, cache, zid, url, memo, _may_fetch_zones())
            if g is _DEFERRED:
                missing = True
                break
            if g is not None:
                geoms.append(g)
        if missing:
            deferred += 1
            continue
        try:
            merged = geo.merge_geometries(geoms)
        except Exception as e:  # noqa: BLE001 — a broken outline is not worth a crash
            log.warning("could not merge zones for %s: %s", a.get("id"), e)
            merged = None
        if merged is None:
            log.info("no zone outlines for %s (%s): %d zones", a.get("event"), a.get("id"),
                     len(urls))
            continue
        a["geometry"] = merged
        a["geometry_source"] = "zones"
        a["bbox"] = geo.geometry_bbox(merged)
    if deferred:
        log.warning("zone outlines: %d alert(s) left without a shape in this run, to keep "
                    "half of the network budget for the forecasts; later runs fetch the rest",
                    deferred)


# ---- per-site classification ---------------------------------------------------------
def _site_lonlat(site):
    if isinstance(site, dict):
        return float(site["lon"]), float(site["lat"])
    return float(site.lon), float(site.lat)


def _site_zone_ids(meta) -> set:
    """The site's own forecast/county/fire zone ids; empty when metadata is not ok."""
    ids = set()
    if isinstance(meta, dict):
        for k in ("forecast_zone", "county_zone", "fire_zone"):
            v = meta.get(k)
            if isinstance(v, str) and v:
                ids.add(v)
    return ids


def site_alerts(alerts: Optional[Iterable[dict]], site, meta, frame) -> dict:
    """Split ``alerts`` for one site into ``at`` and ``candidates`` (not at, but the
    shape's bbox touches the map frame — the renderer decides "near" from what actually
    lands on pixels). AT means: for a polygon-based alert (``geometry_source ==
    "polygon"``) the site is inside the polygon, and nothing else counts (its
    ``affectedZones`` are every county the polygon touches); for a zone-based alert the
    site's own forecast/county/fire zone is listed, or the site is inside the outline."""
    lon, lat = _site_lonlat(site)
    zone_ids = _site_zone_ids(meta)
    frame_bbox = getattr(frame, "bbox", None) if frame is not None else None
    at: List[dict] = []
    near: List[dict] = []
    for a in alerts or []:
        if not isinstance(a, dict):
            continue
        try:
            here = False
            if a.get("geometry_source") != "polygon":
                here = bool(zone_ids.intersection(a.get("affected_zone_ids") or []))
            if not here and a.get("geometry") is not None:
                here = geo.point_in_geometry(lon, lat, a.get("geometry"))
        except Exception as e:  # noqa: BLE001 — bad coordinates in one alert
            log.warning("point test failed for %s: %s", a.get("id"), e)
            here = False
        if here:
            at.append(a)
        elif geo.bbox_intersects(a.get("bbox"), frame_bbox):
            near.append(a)
    return {"at": sort_alerts(at), "candidates": sort_alerts(near)}
