"""Configuration for local-weather-awareness.

Everything lives on a ``Config`` object built by ``Config.from_env()``; every value can be
overridden with a ``WEATHER_*`` environment variable (see ``weather.env.example``) or with
keyword overrides (used by the CLI and by tests). No global mutable state.

Sites: ``--sites`` (command line), else ``WEATHER_SITES`` (``slug|Name|lat|lon[|tz];...``),
else ``WEATHER_SITES_FILE`` (the same entries, one per line), else ``DEFAULT_SITES``: the
example deployment's five sites. Nothing else in the code depends on which sites are
configured: the NWS grid, zones and time zone come from api.weather.gov, the page title is
derived from the site names (unless ``WEATHER_TITLE`` is set), and ``WEATHER_ALERT_AREAS=auto``
queries the states, territories and marine areas that the sites' maps touch
(``weather.states``). A site carries an optional static IANA time zone (``"tz"``) so local
times (sun and twilight, map captions) stay right even when the ``/points`` metadata, the
usual source of the zone, is unavailable. ``validate`` refuses a site whose map reaches no US
state or territory (typically a longitude without its minus sign) or crosses the 180th
meridian, and slugs that repeat in any case, naming where the site was configured.

Road closures (``WEATHER_ROADS``) cover **New Mexico only** and only closures caused by an
emergency (flooding, washouts, slides, fire, crashes, hazmat, snow/ice, wind/dust, storm
damage, law enforcement); roadwork, lane closures and other scheduled events are left out.
Sources: NMDOT's open NMRoads feed (``nmroads.json`` joined with ``rss.xml``) and, as a
supplement, NWS Local Storm Reports from New Mexico that name a closed or flooded road (via
the Iowa Environmental Mesonet). Each part can be switched off on its own.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from typing import Dict, List, Tuple

from . import geo, states

try:
    from zoneinfo import ZoneInfo
except ImportError:                 # pragma: no cover - Python < 3.9 is not supported anyway
    ZoneInfo = None                 # type: ignore[assignment,misc]

# ---- sites -------------------------------------------------------------------
# The EXAMPLE DEPLOYMENT's configuration (https://tau.kirx.net/myweather/): used only when
# neither --sites, WEATHER_SITES nor WEATHER_SITES_FILE names the sites (sites.example is the
# same list as a sites file). Nothing outside this table may depend on these sites.
# slug: file/anchor-safe id; name: display; lat/lon: WGS84 decimal degrees (town centres,
# adjust to the actual site of interest if needed); tz: IANA time zone, used for local times
# whenever the NWS /points metadata (which normally supplies it) is unavailable.
DEFAULT_SITES = [
    {"slug": "socorro", "name": "Socorro, NM", "lat": 34.0584, "lon": -106.8914,
     "tz": "America/Denver"},
    {"slug": "albuquerque", "name": "Albuquerque, NM", "lat": 35.0844, "lon": -106.6504,
     "tz": "America/Denver"},
]


def _check_tz(tz: str, what: str) -> str:
    """``tz`` if it names a time zone this system knows, else ValueError (a typo would
    otherwise silently turn every local time on the page into UTC)."""
    if ZoneInfo is not None:
        try:
            ZoneInfo(tz)
        except Exception:           # ZoneInfoNotFoundError (a KeyError) or ValueError
            raise ValueError("%s: unknown time zone %r (want an IANA name such as "
                             "America/Denver)" % (what, tz))
    return tz


def parse_sites(spec: str, what: str = "WEATHER_SITES") -> List[dict]:
    """Parse ``WEATHER_SITES="slug|Name|lat|lon[|tz];slug|Name|lat|lon[|tz]"``.

    Each site becomes ``{"slug", "name", "lat", "lon", "tz"}``; ``tz`` is the optional
    fifth field (an IANA name such as ``America/Denver``) or None when it is left out.
    Returns [] for empty input; raises ValueError on a malformed entry or a repeated slug (a
    silently dropped site is worse than a loud failure at startup). ``what`` names the
    source in the error messages (``load_sites_file`` passes the file and line)."""
    return [site for _, site in _parse_sites_where(spec, what)]


def _slug_key(slug: str) -> str:
    """Slugs are compared without regard to case: they name the map files, and some file
    systems (macOS, Windows shares) do not tell ``radar_a_dark.png`` and ``radar_A_dark.png``
    apart."""
    return slug.lower()


def _parse_sites_where(spec: str, what: str) -> List[Tuple[str, dict]]:
    """``parse_sites`` with each site's position: [("<what> entry N", site), ...]."""
    out: List[Tuple[str, dict]] = []
    first: Dict[str, Tuple[int, str]] = {}
    n = 0
    for raw in (spec or "").split(";"):
        raw = raw.strip()
        if not raw:
            continue
        n += 1
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) not in (4, 5):
            raise ValueError("%s: an entry must be slug|Name|lat|lon or "
                             "slug|Name|lat|lon|tz: %r" % (what, raw))
        slug, name, lat, lon = parts[:4]
        tz = parts[4] if len(parts) == 5 and parts[4] else None
        if not slug or not all(c.isalnum() or c in "_-" for c in slug):
            raise ValueError("%s: the slug must be [A-Za-z0-9_-]: %r" % (what, slug))
        try:
            latf, lonf = float(lat), float(lon)
        except ValueError:
            raise ValueError("%s: lat/lon must be numbers: %r" % (what, raw))
        if not (-90 <= latf <= 90 and -180 <= lonf <= 180):
            raise ValueError("%s: lat/lon out of range: %r" % (what, raw))
        if tz is not None:
            _check_tz(tz, "%s: entry %r" % (what, raw))
        key = _slug_key(slug)
        if key in first:
            k, other = first[key]
            if other == slug:
                raise ValueError("%s: slug %r is used twice (entries %d and %d)"
                                 % (what, slug, k, n))
            raise ValueError("%s: slug %r repeats %r (entries %d and %d; slugs are compared "
                             "without regard to case)" % (what, slug, other, k, n))
        first[key] = (n, slug)
        out.append(("%s entry %d" % (what, n),
                    {"slug": slug, "name": name or slug, "lat": latf, "lon": lonf, "tz": tz}))
    return out


def load_sites_file(path: str) -> List[dict]:
    """The sites listed in ``WEATHER_SITES_FILE``: one ``slug|Name|lat|lon[|tz]`` entry per
    line, checked exactly like ``WEATHER_SITES`` (``parse_sites``); blank lines and lines
    whose first non-blank character is ``#`` are skipped. UTF-8 (a byte-order mark is
    tolerated). ValueError, naming the file and line, when the file cannot be read, a line
    is malformed or holds more than one entry, a slug repeats (in any case), or no site is
    listed: a broken sites file is a configuration error, never a silent fall-back to other
    sites."""
    return [site for _, site in _load_sites_file_where(path)]


def _load_sites_file_where(path: str) -> List[Tuple[str, dict]]:
    """``load_sites_file`` with each site's position: [("WEATHER_SITES_FILE <path> line N",
    site), ...]."""
    p = os.path.expanduser(path)
    try:
        with open(p, encoding="utf-8-sig") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError("WEATHER_SITES_FILE %s cannot be read: %s" % (p, e))
    out: List[Tuple[str, dict]] = []
    first_line: Dict[str, Tuple[int, str]] = {}
    for n, line in enumerate(text.splitlines(), 1):
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        where = "WEATHER_SITES_FILE %s line %d" % (p, n)
        if ";" in entry:
            raise ValueError("%s: one site per line (no ';'): %r" % (where, entry))
        site = parse_sites(entry, where)[0]
        key = _slug_key(site["slug"])
        if key in first_line:
            k, other = first_line[key]
            if other == site["slug"]:
                raise ValueError("%s: slug %r is already used on line %d"
                                 % (where, site["slug"], k))
            raise ValueError("%s: slug %r repeats %r of line %d (slugs are compared without "
                             "regard to case)" % (where, site["slug"], other, k))
        first_line[key] = (n, site["slug"])
        out.append((where, site))
    if not out:
        raise ValueError("WEATHER_SITES_FILE %s lists no sites" % p)
    return out


# ---- page title --------------------------------------------------------------
TITLE_PREFIX = "Local weather: "
TITLE_NAMES_MAX = 80    # characters of site names in a derived title; more -> "+N more"


def _short_name(site: dict) -> str:
    """A site's display name up to its first comma ("Fort Sumner, NM" -> "Fort Sumner")."""
    name = str(site.get("name") or site.get("slug") or "").strip()
    return name.split(",", 1)[0].strip() or name


def default_title(sites: List[dict]) -> str:
    """The page title when ``WEATHER_TITLE`` is not set: "Local weather: " and the site names
    cut at their first comma, joined with " · ". Two sites whose short names are the same
    keep their full names. When the names would take more than ``TITLE_NAMES_MAX``
    characters, as many as fit are kept, followed by "+N more"."""
    shorts = [_short_name(s) for s in sites]
    names = [str(s.get("name") or s.get("slug") or "").strip() if shorts.count(short) > 1
             else short for s, short in zip(sites, shorts)]
    names = [n for n in names if n]
    if not names:
        return TITLE_PREFIX.rstrip(": ")
    text = " · ".join(names)
    if len(text) > TITLE_NAMES_MAX:
        for keep in range(len(names) - 1, 0, -1):
            text = "%s · +%d more" % (" · ".join(names[:keep]), len(names) - keep)
            if len(text) <= TITLE_NAMES_MAX:
                break
        else:                       # even the first name alone is too long: shorten it
            suffix = " · +%d more" % (len(names) - 1) if len(names) > 1 else ""
            room = max(2, TITLE_NAMES_MAX - len(suffix))
            first = names[0] if len(names[0]) <= room else names[0][:room - 1].rstrip() + "…"
            text = first + suffix
    return TITLE_PREFIX + text


# ---- alert areas -------------------------------------------------------------
_AREA_CODE = re.compile(r"^[A-Z]{2}$")


def parse_alert_areas(spec: str) -> Tuple[bool, List[str]]:
    """``WEATHER_ALERT_AREAS`` as ``(auto, codes)``, from a comma-separated list of ``auto``
    (any case) and two-letter NWS area codes: states and territories (``NM``, ``PR``) or
    marine areas (``LM``). ``auto`` stands for every state, territory and marine area on the
    sites' maps (``Config.alert_area_codes``); codes listed with it are queried as well
    (``auto,LM``); without it the codes alone are queried. The codes come back upper-case,
    in order, without repeats. ValueError when the value is empty or malformed."""
    auto = False
    codes: List[str] = []
    for part in (spec or "").split(","):
        item = part.strip()
        if not item:
            continue
        if item.lower() == "auto":
            auto = True
            continue
        code = item.upper()
        if not _AREA_CODE.match(code):
            raise ValueError("WEATHER_ALERT_AREAS must be 'auto' and/or two-letter NWS area "
                             "codes separated by commas (e.g. auto, NM,TX or auto,LM): %r"
                             % spec)
        if code not in codes:
            codes.append(code)
    if not auto and not codes:
        raise ValueError("WEATHER_ALERT_AREAS is empty: use 'auto' or codes such as NM,TX")
    return auto, codes


_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def parse_alert_colors(spec: str) -> Dict[str, str]:
    """Parse ``WEATHER_ALERT_COLORS="Event Name=#rrggbb;Other Event=#rrggbb"`` into
    ``{lowercased event name: "#RRGGBB"}`` (runs of whitespace in the name collapse to one
    space; a later entry for the same event wins). Empty input gives {}; a malformed entry
    raises ValueError."""
    out: Dict[str, str] = {}
    for raw in (spec or "").split(";"):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split("=")
        if len(parts) != 2:
            raise ValueError("WEATHER_ALERT_COLORS entry must be 'Event Name=#rrggbb': %r" % raw)
        event = " ".join(parts[0].split()).lower()
        color = parts[1].strip()
        if not event:
            raise ValueError("WEATHER_ALERT_COLORS entry has no event name: %r" % raw)
        if not _HEX_COLOR.match(color):
            raise ValueError("WEATHER_ALERT_COLORS colour must be #rrggbb: %r" % raw)
        out[event] = color.upper()
    return out


_HTTP_URL = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]+)?(/[^\s]*)?$")


def _check_url(url: str, what: str) -> str:
    """``url`` if it is an absolute http(s) URL without whitespace, else ValueError."""
    if not isinstance(url, str) or not _HTTP_URL.match(url):
        raise ValueError("%s must be an http:// or https:// URL, got %r" % (what, url))
    return url


def lsr_feed_url(template: str, hours: int) -> str:
    """The storm-report URL for ``hours``: ``template`` with ``{hours}`` filled in (a
    template without the placeholder is used as it is). ValueError when the template holds
    any other ``{...}`` field or an unbalanced brace."""
    try:
        return template.format(hours=int(hours))
    except (KeyError, IndexError, ValueError) as e:
        raise ValueError("WEATHER_LSR_URL may contain only the {hours} placeholder "
                         "(write a literal brace as {{ or }}): %r (%s)" % (template, e))


# ---- env helpers -------------------------------------------------------------
# Placeholder contacts copied from documentation. OpenStreetMap blocks User-Agents that
# contain them (verified 2026-09-24: any "example.org"/"example.com" in the string gets the
# "Access blocked" tile, while the same string with a real address gets real tiles).
_UA_PLACEHOLDER = re.compile(
    r"example\.(?:org|com|net)|\bCONTACT[_ ]?E-?MAIL\b|\byou@|\byour[-_. ]?(?:e-?mail|address)\b",
    re.IGNORECASE)


def _env_str(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v.strip())
    except ValueError:
        raise ValueError("%s must be an integer, got %r" % (name, v))


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    try:
        return float(v.strip())
    except ValueError:
        raise ValueError("%s must be a number, got %r" % (name, v))


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() not in ("0", "false", "no", "off")


# ---- the config object -------------------------------------------------------
@dataclass
class Config:
    # sites
    sites: List[dict] = field(default_factory=lambda: [dict(s) for s in DEFAULT_SITES])
    sites_file: str = ""            # WEATHER_SITES_FILE: one site per line (load_sites_file)
    # Where ``sites`` came from, for the run log (set by from_env).
    sites_source: str = "built-in default list (the example deployment's sites)"

    # paths
    out_dir: str = os.path.abspath("out")
    cache_dir: str = os.path.expanduser("~/.cache/local-weather-awareness")

    # page
    title: str = ""                 # empty: derived from the site names (page_title)
    page_url: str = ""              # the page's public address for the footer; empty: none
    refresh_seconds: int = 90       # browser reload; the page is regenerated every 2 min
    units: str = "us"               # "us" (°F/mph primary) or "metric"
    hourly_hours: int = 24
    forecast_periods: int = 14
    pop_highlight_pct: float = 30.0  # precipitation probability at/above this is highlighted
    show_sun: bool = True

    # HTTP
    # api.weather.gov asks for a contact: deployers append an e-mail address (WEATHER_USER_AGENT)
    user_agent: str = ("local-weather-awareness"
                       " (+https://github.com/kirxkirx/local-weather-awareness)")
    http_timeout: float = 25.0
    http_retries: int = 2
    # Wall-clock budget for one run's network traffic: once it is spent, every further fetch
    # fails at once and the page is built from last-good copies (see weather.http.begin_run),
    # so a hanging upstream cannot stretch a run past the 2-minute run interval (a cold first
    # run with basemap tile downloads takes about 30-50 s).
    run_budget_s: int = 90

    # NWS caching (seconds): ttl = re-fetch after; max_stale = keep using last-good until
    points_ttl: int = 7 * 86400
    points_max_stale: int = 60 * 86400
    forecast_ttl: int = 1800
    forecast_max_stale: int = 7200
    hourly_ttl: int = 1800
    hourly_max_stale: int = 7200
    obs_ttl: int = 600
    obs_max_stale: int = 3600
    obs_max_age_min: int = 180      # ignore station reports older than this when picking one

    # alerts
    # api.weather.gov ?area= list: "auto" = the states, territories and marine areas on the
    # sites' maps (alert_area_codes), two-letter codes such as "NM,TX", or both ("auto,LM")
    alert_areas: str = "auto"
    alerts_max_stale: int = 1200
    alert_fill_alpha: int = 55      # 0..255 fill opacity on the map
    alert_outline_width: int = 3
    # Per-event colour overrides, "Event Name=#rrggbb;Other Event=#rrggbb" (case-insensitive
    # event names); parsed by the ``alert_color_overrides`` property, checked in validate().
    alert_colors: str = ""

    # radar map
    map_miles: float = 100.0        # full width AND height of the map, statute miles
    map_px: int = 600               # output width in pixels (height follows the Mercator aspect)
    tile_zoom: int = 9
    # Raster tile templates. OpenStreetMap's standard tiles are the one key-free source with
    # readable town labels (CARTO's free rasters now answer with an "API KEY REQUIRED" tile);
    # the dark theme is derived from the same tiles by inverting their lightness.
    tile_url_dark: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    tile_url_light: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    tile_dark_invert: bool = True   # dark theme: invert lightness when the tiles come out light
    radar_enabled: bool = True
    radar_dbz_min: float = 5.0      # echoes below this are not drawn
    radar_alpha: int = 170          # 0..255 opacity of the radar layer
    radar_max_back: int = 10        # how many 2-min frames to look back for the latest
    radar_max_stale: int = 1800     # reuse the last good frame for this long if IEM is down
    mrms_archive: str = ("https://mesonet.agron.iastate.edu/archive/data/"
                         "%Y/%m/%d/GIS/mrms/lcref_%Y%m%d%H%M.png")
    attribution: str = ("© OpenStreetMap contributors · Radar: NOAA/NSSL MRMS via "
                        "Iowa Environmental Mesonet · Forecast & alerts: NOAA/NWS")

    # road closures: New Mexico only, emergencies only (see the module docstring)
    roads_enabled: bool = True      # NMDOT closures listed per site and drawn on the maps
    roads_water: bool = True        # also "water on road" (flooded / standing water) conditions
    lsr_enabled: bool = True        # also NWS storm reports naming a closed or flooded NM road
    lsr_hours: int = 24             # storm-report window, hours (1..168)
    roads_max_stale: int = 3600     # s: keep showing the last good road data this long
    roads_old_days: float = 3.0     # flag an NMDOT item not updated for this many days
    nmroads_json_url: str = "https://nmroads.com/nmroads.json"
    nmroads_rss_url: str = "https://nmroads.com/rss.xml"
    # IEM's GeoJSON of NWS Local Storm Reports; {hours} is replaced by lsr_hours. ``states=NM``
    # is the filter IEM honours (checked 2026-09-23: a ``wfos=`` list is silently ignored and
    # returns every office in the country, ~50-400 KB instead of ~7-10 KB).
    lsr_url: str = ("https://mesonet.agron.iastate.edu/geojson/lsr.geojson"
                    "?hours={hours}&states=NM")

    # misc
    lock_file: str = ""             # default: <cache_dir>/run.lock
    verbose: bool = False

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        """Build from defaults <- WEATHER_* environment <- explicit keyword overrides."""
        c = cls()
        env_sites_spec = _env_str("WEATHER_SITES", "")
        c.sites_file = _env_str("WEATHER_SITES_FILE", c.sites_file)
        c.out_dir = os.path.abspath(_env_str("WEATHER_OUT_DIR", c.out_dir))
        c.cache_dir = os.path.expanduser(_env_str("WEATHER_CACHE_DIR", c.cache_dir))
        c.title = _env_str("WEATHER_TITLE", c.title)
        c.page_url = _env_str("WEATHER_PAGE_URL", c.page_url)
        c.refresh_seconds = _env_int("WEATHER_REFRESH_SECONDS", c.refresh_seconds)
        c.units = _env_str("WEATHER_UNITS", c.units).lower()
        c.hourly_hours = _env_int("WEATHER_HOURLY_HOURS", c.hourly_hours)
        c.forecast_periods = _env_int("WEATHER_FORECAST_PERIODS", c.forecast_periods)
        c.pop_highlight_pct = _env_float("WEATHER_POP_HIGHLIGHT_PCT", c.pop_highlight_pct)
        c.show_sun = _env_bool("WEATHER_SHOW_SUN", c.show_sun)
        c.user_agent = _env_str("WEATHER_USER_AGENT", c.user_agent)
        c.http_timeout = _env_float("WEATHER_HTTP_TIMEOUT", c.http_timeout)
        c.http_retries = _env_int("WEATHER_HTTP_RETRIES", c.http_retries)
        c.run_budget_s = _env_int("WEATHER_RUN_BUDGET", c.run_budget_s)
        c.points_ttl = _env_int("WEATHER_POINTS_TTL", c.points_ttl)
        c.forecast_ttl = _env_int("WEATHER_FORECAST_TTL", c.forecast_ttl)
        c.forecast_max_stale = _env_int("WEATHER_FORECAST_MAX_STALE", c.forecast_max_stale)
        c.hourly_ttl = _env_int("WEATHER_HOURLY_TTL", c.hourly_ttl)
        c.hourly_max_stale = _env_int("WEATHER_HOURLY_MAX_STALE", c.hourly_max_stale)
        c.obs_ttl = _env_int("WEATHER_OBS_TTL", c.obs_ttl)
        c.obs_max_stale = _env_int("WEATHER_OBS_MAX_STALE", c.obs_max_stale)
        c.alert_areas = _env_str("WEATHER_ALERT_AREAS", c.alert_areas)
        c.alerts_max_stale = _env_int("WEATHER_ALERTS_MAX_STALE", c.alerts_max_stale)
        c.alert_fill_alpha = _env_int("WEATHER_ALERT_FILL_ALPHA", c.alert_fill_alpha)
        c.alert_colors = _env_str("WEATHER_ALERT_COLORS", c.alert_colors)
        c.map_miles = _env_float("WEATHER_MAP_MILES", c.map_miles)
        c.map_px = _env_int("WEATHER_MAP_PX", c.map_px)
        c.tile_zoom = _env_int("WEATHER_TILE_ZOOM", c.tile_zoom)
        c.tile_url_dark = _env_str("WEATHER_TILE_URL_DARK", c.tile_url_dark)
        c.tile_url_light = _env_str("WEATHER_TILE_URL_LIGHT", c.tile_url_light)
        c.tile_dark_invert = _env_bool("WEATHER_TILE_DARK_INVERT", c.tile_dark_invert)
        c.radar_enabled = _env_bool("WEATHER_RADAR", c.radar_enabled)
        c.radar_dbz_min = _env_float("WEATHER_RADAR_DBZ_MIN", c.radar_dbz_min)
        c.radar_alpha = _env_int("WEATHER_RADAR_ALPHA", c.radar_alpha)
        c.radar_max_back = _env_int("WEATHER_RADAR_MAX_BACK", c.radar_max_back)
        c.radar_max_stale = _env_int("WEATHER_RADAR_MAX_STALE", c.radar_max_stale)
        c.mrms_archive = _env_str("WEATHER_MRMS_ARCHIVE", c.mrms_archive)
        c.roads_enabled = _env_bool("WEATHER_ROADS", c.roads_enabled)
        c.roads_water = _env_bool("WEATHER_ROADS_WATER", c.roads_water)
        c.lsr_enabled = _env_bool("WEATHER_LSR", c.lsr_enabled)
        c.lsr_hours = _env_int("WEATHER_LSR_HOURS", c.lsr_hours)
        c.roads_max_stale = _env_int("WEATHER_ROADS_MAX_STALE", c.roads_max_stale)
        c.roads_old_days = _env_float("WEATHER_ROADS_OLD_DAYS", c.roads_old_days)
        c.nmroads_json_url = _env_str("WEATHER_NMROADS_JSON_URL", c.nmroads_json_url)
        c.nmroads_rss_url = _env_str("WEATHER_NMROADS_RSS_URL", c.nmroads_rss_url)
        c.lsr_url = _env_str("WEATHER_LSR_URL", c.lsr_url)
        c.lock_file = _env_str("WEATHER_LOCK_FILE", c.lock_file)
        c.verbose = _env_bool("WEATHER_VERBOSE", c.verbose)
        for k, v in overrides.items():
            if v is None:
                continue
            if k not in {f.name for f in fields(cls)}:
                raise TypeError("unknown config override %r" % k)
            setattr(c, k, v)
        c._choose_sites(env_sites_spec, overrides.get("sites") is not None)
        c.validate()
        return c

    def _choose_sites(self, env_sites_spec: str, overridden: bool) -> None:
        """The site list by precedence: a ``sites`` override (the CLI's ``--sites``), else
        ``WEATHER_SITES``, else ``WEATHER_SITES_FILE`` (read only then; any problem with it
        is a ValueError), else ``DEFAULT_SITES``. ``sites_source`` says which, for the log.
        A source is parsed only when it is the one used, so ``--sites`` also works while
        ``WEATHER_SITES`` is malformed; a ``WEATHER_SITES`` that is set but lists no site
        (only ``;``) is an error, never a fall-back to other sites. Each site's position in
        its source is kept for the messages of ``validate``."""
        if overridden:
            self.sites_source = "the command line (--sites)"
            self._site_where = {s.get("slug"): "--sites entry %d" % n
                                for n, s in enumerate(self.sites, 1) if isinstance(s, dict)}
        elif env_sites_spec:
            where_sites = _parse_sites_where(env_sites_spec, "WEATHER_SITES")
            if not where_sites:
                raise ValueError("WEATHER_SITES lists no sites: %r" % env_sites_spec)
            self.sites = [s for _, s in where_sites]
            self._site_where = {s["slug"]: w for w, s in where_sites}
            self.sites_source = "WEATHER_SITES"
        elif self.sites_file:
            where_sites = _load_sites_file_where(self.sites_file)
            self.sites = [s for _, s in where_sites]
            self._site_where = {s["slug"]: w for w, s in where_sites}
            self.sites_source = "WEATHER_SITES_FILE %s" % os.path.expanduser(self.sites_file)
            return
        else:
            self._site_where = {s.get("slug"): "built-in site %d" % n
                                for n, s in enumerate(self.sites, 1) if isinstance(s, dict)}
        if self.sites_file and (overridden or env_sites_spec):
            self.sites_source += " (WEATHER_SITES_FILE not read: this takes precedence)"

    def _where(self, n: int, site: dict) -> str:
        """Where site number ``n`` (0-based) of ``self.sites`` was configured, for messages:
        "WEATHER_SITES_FILE <path> line 3", "WEATHER_SITES entry 2", ..."""
        where = getattr(self, "_site_where", None) or {}
        return where.get(site.get("slug")) or "site %d" % (n + 1)

    def _check_sites(self) -> None:
        """Every site: a unique slug (compared without regard to case), and a map that
        can be drawn and reaches the US. A site whose map reaches no US state or territory
        (typically a longitude without its minus sign) would get no forecast and no alerts,
        only a map of somewhere else. A map across the 180th meridian (the western Aleutians)
        cannot be drawn by the renderer. Each error names the site's source and position."""
        seen: Dict[str, Tuple[int, str]] = {}
        for n, s in enumerate(self.sites):
            slug = str(s.get("slug"))
            key = _slug_key(slug)
            if key in seen:
                k, other = seen[key]
                raise ValueError("%s: slug %r repeats %r of %s (slugs name the map files and "
                                 "are compared without regard to case)"
                                 % (self._where(n, s), slug, other, self._where(k, self.sites[k])))
            seen[key] = (n, slug)
        for n, s in enumerate(self.sites):
            what = "%s: site %r (%s, %s)" % (self._where(n, s), s.get("slug"),
                                             s.get("lat"), s.get("lon"))
            try:
                frame = geo.MapFrame(s["lat"], s["lon"], self.map_km, self.map_px,
                                     self.tile_zoom)
                west, south, east, north = frame.bbox
            except Exception as e:      # noqa: BLE001 - e.g. a map around a pole
                raise ValueError("%s: no map can be drawn there (%s)" % (what, e))
            if west > east:
                raise ValueError("%s: its %g-mile map would cross the 180th meridian, which "
                                 "the map renderer cannot draw; move the site or lower "
                                 "WEATHER_MAP_MILES" % (what, self.map_miles))
            if not states.states_touching([frame.bbox]):
                raise ValueError("%s: its map reaches no US state or territory, and NWS "
                                 "forecasts and alerts cover only those (US longitudes are "
                                 "negative, e.g. -106.65)" % what)

    def validate(self) -> None:
        if not self.sites:
            raise ValueError("no sites configured")
        if self.units not in ("us", "metric"):
            raise ValueError("WEATHER_UNITS must be 'us' or 'metric'")
        if not (10 <= self.map_miles <= 1000):
            raise ValueError("WEATHER_MAP_MILES must be 10..1000")
        if not (200 <= self.map_px <= 2000):
            raise ValueError("WEATHER_MAP_PX must be 200..2000")
        if not (5 <= self.tile_zoom <= 12):
            raise ValueError("WEATHER_TILE_ZOOM must be 5..12")
        self._check_sites()
        if not (0 <= self.alert_fill_alpha <= 255 and 0 <= self.radar_alpha <= 255):
            raise ValueError("alpha values must be 0..255")
        if self.refresh_seconds < 30:
            raise ValueError("WEATHER_REFRESH_SECONDS must be >= 30")
        if not self.user_agent.strip():
            raise ValueError("WEATHER_USER_AGENT must not be empty (api.weather.gov requires it)")
        bad = _UA_PLACEHOLDER.search(self.user_agent)
        if bad:
            raise ValueError(
                "WEATHER_USER_AGENT contains the placeholder %r. OpenStreetMap's tile servers "
                "block any User-Agent with a placeholder contact such as example.org (the maps "
                "then show an 'Access blocked' tile). Put a real contact address or URL there, "
                "or leave the contact out." % bad.group(0))
        if self.run_budget_s < 10:
            raise ValueError("WEATHER_RUN_BUDGET must be >= 10 (seconds)")
        for s in self.sites:
            tz = s.get("tz")
            if tz is not None and (not isinstance(tz, str) or not tz.strip()):
                raise ValueError("site %r: tz must be an IANA time zone name or None"
                                 % s.get("slug"))
        parse_alert_colors(self.alert_colors)   # ValueError on a malformed WEATHER_ALERT_COLORS
        self.alert_area_codes                   # ValueError: malformed, or auto finds no area
        self._validate_roads()
        if not self.lock_file:
            self.lock_file = os.path.join(self.cache_dir, "run.lock")

    def _validate_roads(self) -> None:
        """The road-closure settings. Checked even while ``roads_enabled`` is off, so that
        switching the feature on later never meets a bad value in the middle of the night."""
        if isinstance(self.lsr_hours, bool) or not isinstance(self.lsr_hours, int) \
                or not (1 <= self.lsr_hours <= 168):
            raise ValueError("WEATHER_LSR_HOURS must be a whole number of hours, 1..168")
        if isinstance(self.roads_max_stale, bool) or not isinstance(self.roads_max_stale, int) \
                or not (0 <= self.roads_max_stale <= 7 * 86400):
            raise ValueError("WEATHER_ROADS_MAX_STALE must be 0..604800 (seconds)")
        try:
            old_days = float(self.roads_old_days)
        except (TypeError, ValueError):
            old_days = float("nan")
        if isinstance(self.roads_old_days, bool) or not (0 < old_days <= 365):
            raise ValueError("WEATHER_ROADS_OLD_DAYS must be a number of days, > 0 and <= 365")
        _check_url(self.nmroads_json_url, "WEATHER_NMROADS_JSON_URL")
        _check_url(self.nmroads_rss_url, "WEATHER_NMROADS_RSS_URL")
        _check_url(lsr_feed_url(self.lsr_url, self.lsr_hours), "WEATHER_LSR_URL")

    @property
    def lsr_feed_url(self) -> str:
        """``lsr_url`` with ``{hours}`` = ``lsr_hours``: the storm-report request."""
        return lsr_feed_url(self.lsr_url, self.lsr_hours)

    @property
    def frame_bboxes(self) -> List[Tuple[float, float, float, float]]:
        """Every site's map as (lonmin, latmin, lonmax, latmax), in site order: exactly the
        frame the renderer draws, ``geo.MapFrame(lat, lon, map_km, map_px, tile_zoom)``."""
        return [geo.MapFrame(s["lat"], s["lon"], self.map_km, self.map_px, self.tile_zoom).bbox
                for s in self.sites]

    @property
    def alert_area_codes(self) -> List[str]:
        """The ``?area=`` codes of the NWS alert query: for ``auto`` every state, territory
        and marine area whose bounding box touches a site's map (``states.areas_touching``),
        followed by any codes listed with it (``auto,LM``); otherwise the listed codes.
        ValueError when the setting is malformed, or when ``auto`` finds nothing (no site's
        map reaches the US or its territories)."""
        auto, codes = parse_alert_areas(self.alert_areas)
        if not auto:
            return codes
        found = states.areas_touching(self.frame_bboxes)
        if not found:
            raise ValueError("WEATHER_ALERT_AREAS=auto: no US state or territory lies on "
                             "any site's map (NWS alerts cover the US only); set "
                             "WEATHER_ALERT_AREAS to the area codes to query")
        return found + [c for c in codes if c not in found]

    @property
    def sites_outside_alert_areas(self) -> List[Tuple[dict, List[str]]]:
        """For a ``WEATHER_ALERT_AREAS`` list without ``auto``: the sites that lie in none of
        the listed states and territories, each with the codes whose box holds the site
        (``states.states_at``). Their alerts are never fetched, typically because the site
        list changed while the area list stayed. [] for ``auto``, and for a site in no
        state's box. ValueError when the setting is malformed."""
        auto, codes = parse_alert_areas(self.alert_areas)
        if auto:
            return []
        out = []
        for s in self.sites:
            here = states.states_at(s["lon"], s["lat"])
            if here and not set(here) & set(codes):
                out.append((s, here))
        return out

    @property
    def page_title(self) -> str:
        """The page title: ``WEATHER_TITLE`` when set, else derived from the site names
        (``default_title``), so it follows the configured sites."""
        title = (self.title or "").strip()
        return title or default_title(self.sites)

    @property
    def alert_color_overrides(self) -> Dict[str, str]:
        """``alert_colors`` parsed: {lowercased event name: "#RRGGBB"} (a fresh dict)."""
        return parse_alert_colors(self.alert_colors)

    @property
    def map_km(self) -> float:
        return self.map_miles * 1.609344

    @property
    def themes(self):
        """(theme name, tile url template) pairs rendered for every site."""
        return (("dark", self.tile_url_dark), ("light", self.tile_url_light))

    def map_basename(self, slug: str, theme: str) -> str:
        return "radar_%s_%s.png" % (slug, theme)
