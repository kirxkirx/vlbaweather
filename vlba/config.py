"""Configuration of the VLBA page: the parent's ``weather.config.Config`` (every ``WEATHER_*``
variable keeps its meaning; see ``vlbaweather.env.example``) with other defaults where the VLBA
page differs, plus the ``VLBA_*`` settings of the verdict, the GLM layer and the island radar
(see ``vlbaweather.env.example``).

Sites: ``--sites`` / ``WEATHER_SITES`` / ``WEATHER_SITES_FILE`` work as in the parent (a subset
for tests, or another array); with none of them the built-in list (``vlba.sites``: the ten
VLBA antennas, the VLA and the GBT) is used, which is the normal deployment.

Everything is validated at startup: a malformed value is a ValueError (exit status 2 in the
generator), never a silent default, because a verdict built on a mis-set threshold would be
wrong without anyone noticing.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, fields
from typing import List, Tuple

from weather import alerts as _alerts
from weather.config import Config, _env_float, _env_int, _env_str

from . import sites as _sites

# NWS products that make an antenna's weather BAD while one is in effect over the site.
# ttustatus's three (Tornado / Dust Storm / High Wind Warning) plus the other damaging-wind
# products (Extreme Wind, Hurricane, Typhoon, Tropical Storm Warning) and the snowstorm
# products (asked for: Blizzard, Winter Storm, Ice Storm, Snow Squall, Lake Effect Snow
# Warning). Everything else NWS issues is shown, never part of the verdict.
DEFAULT_VETO_EVENTS = (
    "Tornado Warning,Dust Storm Warning,High Wind Warning,Extreme Wind Warning,"
    "Hurricane Warning,Typhoon Warning,Tropical Storm Warning,Blizzard Warning,"
    "Winter Storm Warning,Ice Storm Warning,Snow Squall Warning,Lake Effect Snow Warning")

# GOES-East (GOES-19, 75.2 W) and GOES-West (GOES-18, 137.0 W), the NOAA Open Data buckets
# on AWS. Positions checked 2026-10-08 (NOAA OSPO status page).
DEFAULT_GLM_BUCKETS = "noaa-goes19=-75.2;noaa-goes18=-137.0"

_KNOWN_EVENTS = {k.lower() for k in _alerts.NWS_COLORS} | {k.lower() for k in _alerts.EVENT_COLORS}

# VLBA_VETO_EVENTS values that mean "no warning check". "none" is the one to use: the cron
# wrapper skips empty values in env files (so that an empty line cannot clear an earlier
# file's setting), which makes an empty VLBA_VETO_EVENTS= work under systemd only.
VETO_OFF = ("", "none")


def _env_events(name: str, default: str) -> str:
    """VLBA_VETO_EVENTS: unset = ``default``; "none", or set but empty, = "" (no warning
    check); anything else is the comma list as given."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return "" if raw.strip().lower() in VETO_OFF else raw


def _env_flag(name: str, default: bool) -> bool:
    """A VLBA_* switch: 1/0, true/false, yes/no, on/off; unset or empty = ``default``.
    Anything else is a ValueError (the parent's lenient reading would take "n" or
    "disabled" for "on", and a check meant to be off would silently stay on)."""
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    s = v.strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise ValueError("%s must be 1 or 0 (true/false, yes/no, on/off), not %r" % (name, v))


def parse_event_names(raw: str) -> Tuple[List[str], List[str]]:
    """``"tornado warning, Dust  Storm Warning"`` -> (``["Tornado Warning", "Dust Storm
    Warning"]``, names NWS does not seem to use). Matching is case-insensitive everywhere;
    the names are kept in NWS's Title Case so the page shows them as NWS spells them.
    Duplicates are dropped. A name the colour table does not know is KEPT (NWS renames
    products now and then) and returned in the second list so the generator can warn: a typo
    would otherwise silently never count."""
    out: List[str] = []
    unknown: List[str] = []
    seen = set()
    for part in (raw or "").split(","):
        words = part.split()
        if not words:
            continue
        canon = " ".join(w[:1].upper() + w[1:].lower() for w in words)
        if canon.lower() in seen:
            continue
        seen.add(canon.lower())
        if canon.lower() not in _KNOWN_EVENTS:
            unknown.append(canon)
        out.append(canon)
    return out, unknown


def parse_glm_buckets(raw: str) -> List[Tuple[str, float]]:
    """``"noaa-goes19=-75.2;noaa-goes18=-137.0"`` -> ``[("noaa-goes19", -75.2), ...]``: the
    S3 bucket of each satellite's GLM-L2-LCFA granules and its sub-satellite longitude (east
    positive). ValueError on a malformed entry or a repeated bucket; an empty value gives []
    (no satellite: the GLM layer is then off)."""
    out: List[Tuple[str, float]] = []
    for part in (raw or "").split(";"):
        item = part.strip()
        if not item:
            continue
        name, sep, lon = item.partition("=")
        name = name.strip()
        if not sep or not name or not all(c.isalnum() or c in ".-" for c in name):
            raise ValueError("VLBA_GLM_BUCKETS entry must be bucket=longitude: %r" % item)
        try:
            lonf = float(lon.strip())
        except ValueError:
            raise ValueError("VLBA_GLM_BUCKETS: longitude must be a number: %r" % item)
        if not (-180.0 <= lonf <= 180.0):
            raise ValueError("VLBA_GLM_BUCKETS: longitude out of range: %r" % item)
        if any(name == n for n, _ in out):
            raise ValueError("VLBA_GLM_BUCKETS: bucket %r listed twice" % name)
        out.append((name, lonf))
    return out


@dataclass
class VlbaConfig(Config):
    # ---- the parent's fields with other defaults ---------------------------------------
    sites_source: str = "the built-in list (vlba/sites.py)"
    cache_dir: str = os.path.expanduser("~/.cache/vlbaweather")
    title: str = "VLBA, VLA and GBT weather"
    # The browser reload. The generator runs every 30 min (cron / timer, asked 2026-10-14);
    # reloading every 5 min shows a new run's page within 5 min of its writing.
    refresh_seconds: int = 300
    user_agent: str = "vlbaweather (+https://github.com/kirxkirx/vlbaweather)"
    # A run fetches twelve sites, one radar frame per source (more where a ring shows an echo)
    # and ~185 GLM granules (the 30-minute window, 25-30 s, measured 2026-10-14); the first run
    # of a fresh cache also
    # downloads ~16 basemap tiles per site and theme. Measured 2026-10-08 with ten sites and a
    # 5-minute window: a cold run needs about 150 s, a warm one about 40 s. 240 s is well
    # inside the wrapper's 10-minute timeout and the 30-minute run interval.
    run_budget_s: int = 240
    # The runs are 30 min apart (2026-10-14), so the parent's timings meant for runs 2 min
    # apart would expire between two runs: the forecasts' cached copies are re-fetched by
    # every scheduled run (25 min < 30), and the alert feed of the run before is reused for
    # one failed run (40 min): a failed fetch must not drop a warning in effect.
    forecast_ttl: int = 1500
    hourly_ttl: int = 1500
    alerts_max_stale: int = 2400
    # likewise the last good MRMS frame (the map shows it labelled stale; the verdict never
    # counts a frame that old)
    radar_max_stale: int = 2400
    roads_enabled: bool = False     # no road closures (asked)
    lsr_enabled: bool = False
    attribution: str = ("© OpenStreetMap contributors · Radar: NOAA MRMS via IEM / NWS · "
                        "Lightning: NOAA GOES GLM · Forecast & alerts: NOAA/NWS")

    # ---- the verdict ---------------------------------------------------------------------
    veto_events: str = DEFAULT_VETO_EVENTS      # VLBA_VETO_EVENTS
    # ttustatus counts a warning from 15 min before its onset; runs 30 min apart need the
    # interval on top, so the page is BAD at least 15 min before every onset (45 min).
    veto_onset_lead_s: int = 2700               # VLBA_VETO_ONSET_LEAD
    radar_km: float = 30.0                      # VLBA_RADAR_KM
    radar_dbz: float = 20.0                     # VLBA_RADAR_DBZ
    radar_trigger_after: int = 2                # VLBA_RADAR_TRIGGER_AFTER
    radar_hold_s: int = 900                     # VLBA_RADAR_HOLD
    radar_stale_s: int = 1200                   # VLBA_RADAR_STALE
    # The radar frames write "no coverage" like "no echo" (asked 2026-10-12: a radar outage
    # must read "no data", never "clear"): MRMS's own files of the frame tell them apart.
    radar_coverage: bool = True                 # VLBA_RADAR_COVERAGE
    radar_min_coverage: float = 90.0            # VLBA_RADAR_MIN_COVERAGE (% of the ring)
    mrms_url: str = "https://noaa-mrms-pds.s3.amazonaws.com"    # VLBA_MRMS_URL
    nws_cloud_max: float = 60.0                 # VLBA_NWS_CLOUD_MAX (negative = off)
    nws_precip_max: float = 20.0                # VLBA_NWS_PRECIP_MAX (negative = off)
    nws_thunder_max: float = 15.0               # VLBA_NWS_THUNDER_MAX (negative = off)
    nws_winter: bool = True                     # VLBA_NWS_WINTER
    # NWS offices update their gridded forecast a few times a day (measured 2026-10-08: 2 to
    # 6 h between updates), so ttustatus's 150 min would blank the forecast checks most of
    # the time; an update older than 12 h means the office or the feed has a problem.
    nws_stale_min: int = 720                    # VLBA_NWS_STALE_MIN
    gridpoint_ttl: int = 1500                   # VLBA_GRIDPOINT_TTL (every 30-min run re-fetches)
    gridpoint_max_stale: int = 7200             # VLBA_GRIDPOINT_MAX_STALE
    # Off (asked 2026-10-08): ttustatus's 95 % protects an optical dome from condensation,
    # which does not concern a radio antenna. A value 0..100 switches it back on.
    obs_humidity_max: float = -1.0              # VLBA_OBS_HUMIDITY_MAX (negative = off)
    obs_precip: bool = True                     # VLBA_OBS_PRECIP
    obs_max_km: float = 50.0                    # VLBA_OBS_MAX_KM
    obs_max_delev_m: float = 1000.0             # VLBA_OBS_MAX_DELEV_M
    # The VLBA antennas stow in high wind; 45 mph asked for on 2026-10-08 (ttustatus has no
    # wind cut).
    wind_mph: float = 45.0                      # VLBA_WIND_MPH (0 = off)
    state_file: str = ""                        # VLBA_STATE_FILE; default <cache>/vlba_state.json

    # ---- GLM lightning -------------------------------------------------------------------
    glm_enabled: bool = True                    # VLBA_GLM
    glm_km: float = 50.0                        # VLBA_GLM_KM
    glm_hold_s: int = 1800                      # VLBA_GLM_HOLD
    # The run interval (30 min since 2026-10-14): the window plus its minute of overlap covers
    # the 30-minute hold above, so no flash that could still count goes unexamined; a flash of
    # the window older than the hold no longer counts.
    glm_window_min: int = 30                    # VLBA_GLM_WINDOW_MIN
    glm_stride: int = 1                         # VLBA_GLM_STRIDE
    glm_buckets: str = DEFAULT_GLM_BUCKETS      # VLBA_GLM_BUCKETS
    # GLM's field of view ends about 62 deg of arc from the sub-satellite point diagonally
    # (measured 2026-10-14 in the granules: GOES-18 sees nothing at the GBT, 64.9 deg away)
    glm_max_arc_deg: float = 62.0               # VLBA_GLM_MAX_ARC
    glm_timeout_s: int = 90                     # VLBA_GLM_TIMEOUT (184 granules: ~25 s measured)
    glm_workers: int = 4                        # VLBA_GLM_WORKERS

    # ---- radar for sites outside the MRMS grid ------------------------------------------
    island_radar: bool = True                   # VLBA_ISLAND_RADAR
    nwsradar_url: str = "https://opengeo.ncep.noaa.gov/geoserver"   # VLBA_NWSRADAR_URL
    nwsradar_max_stale: int = 2400              # VLBA_NWSRADAR_MAX_STALE (one failed 30-min run)

    @classmethod
    def from_env(cls, **overrides) -> "VlbaConfig":
        """Defaults <- WEATHER_* and VLBA_* environment <- keyword overrides, then validation.
        The VLBA site list is used unless ``--sites`` (the ``sites`` override), ``WEATHER_SITES``
        or ``WEATHER_SITES_FILE`` names other sites."""
        builtin = (overrides.get("sites") is None and not _env_str("WEATHER_SITES", "")
                   and not _env_str("WEATHER_SITES_FILE", ""))
        if builtin:
            overrides = dict(overrides, sites=[dict(s) for s in _sites.SITES])
        c = super().from_env(**overrides)
        if builtin:
            c.sites_source = "the built-in list (vlba/sites.py)"
            c._site_where = {s["slug"]: "vlba/sites.py entry %d" % n
                             for n, s in enumerate(c.sites, 1)}
        else:
            # a built-in site named through --sites / WEATHER_SITES keeps its code and height
            # (the parent's format has neither; the station elevation rule needs the height)
            known = {s["slug"]: s for s in _sites.SITES}
            for s in c.sites:
                k = known.get(s.get("slug"))
                try:
                    near = k is not None and abs(float(s["lat"]) - k["lat"]) < 0.05 \
                        and abs(float(s["lon"]) - k["lon"]) < 0.05
                except (KeyError, TypeError, ValueError):
                    near = False
                if near:
                    s.setdefault("code", k["code"])
                    s.setdefault("elev_m", k["elev_m"])
        c.veto_events = _env_events("VLBA_VETO_EVENTS", c.veto_events)    # "none": no warning check
        c.veto_onset_lead_s = _env_int("VLBA_VETO_ONSET_LEAD", c.veto_onset_lead_s)
        c.radar_km = _env_float("VLBA_RADAR_KM", c.radar_km)
        c.radar_dbz = _env_float("VLBA_RADAR_DBZ", c.radar_dbz)
        c.radar_trigger_after = _env_int("VLBA_RADAR_TRIGGER_AFTER", c.radar_trigger_after)
        c.radar_hold_s = _env_int("VLBA_RADAR_HOLD", c.radar_hold_s)
        c.radar_stale_s = _env_int("VLBA_RADAR_STALE", c.radar_stale_s)
        c.radar_coverage = _env_flag("VLBA_RADAR_COVERAGE", c.radar_coverage)
        c.radar_min_coverage = _env_float("VLBA_RADAR_MIN_COVERAGE", c.radar_min_coverage)
        c.mrms_url = _env_str("VLBA_MRMS_URL", c.mrms_url)
        c.nws_cloud_max = _env_float("VLBA_NWS_CLOUD_MAX", c.nws_cloud_max)
        c.nws_precip_max = _env_float("VLBA_NWS_PRECIP_MAX", c.nws_precip_max)
        c.nws_thunder_max = _env_float("VLBA_NWS_THUNDER_MAX", c.nws_thunder_max)
        c.nws_winter = _env_flag("VLBA_NWS_WINTER", c.nws_winter)
        c.nws_stale_min = _env_int("VLBA_NWS_STALE_MIN", c.nws_stale_min)
        c.gridpoint_ttl = _env_int("VLBA_GRIDPOINT_TTL", c.gridpoint_ttl)
        c.gridpoint_max_stale = _env_int("VLBA_GRIDPOINT_MAX_STALE", c.gridpoint_max_stale)
        c.obs_humidity_max = _env_float("VLBA_OBS_HUMIDITY_MAX", c.obs_humidity_max)
        c.obs_precip = _env_flag("VLBA_OBS_PRECIP", c.obs_precip)
        c.obs_max_km = _env_float("VLBA_OBS_MAX_KM", c.obs_max_km)
        c.obs_max_delev_m = _env_float("VLBA_OBS_MAX_DELEV_M", c.obs_max_delev_m)
        c.wind_mph = _env_float("VLBA_WIND_MPH", c.wind_mph)
        c.state_file = _env_str("VLBA_STATE_FILE", c.state_file)
        c.glm_enabled = _env_flag("VLBA_GLM", c.glm_enabled)
        c.glm_km = _env_float("VLBA_GLM_KM", c.glm_km)
        c.glm_hold_s = _env_int("VLBA_GLM_HOLD", c.glm_hold_s)
        c.glm_window_min = _env_int("VLBA_GLM_WINDOW_MIN", c.glm_window_min)
        c.glm_stride = _env_int("VLBA_GLM_STRIDE", c.glm_stride)
        c.glm_buckets = _env_str("VLBA_GLM_BUCKETS", c.glm_buckets)
        c.glm_max_arc_deg = _env_float("VLBA_GLM_MAX_ARC", c.glm_max_arc_deg)
        c.glm_timeout_s = _env_int("VLBA_GLM_TIMEOUT", c.glm_timeout_s)
        c.glm_workers = _env_int("VLBA_GLM_WORKERS", c.glm_workers)
        c.island_radar = _env_flag("VLBA_ISLAND_RADAR", c.island_radar)
        c.nwsradar_url = _env_str("VLBA_NWSRADAR_URL", c.nwsradar_url)
        c.nwsradar_max_stale = _env_int("VLBA_NWSRADAR_MAX_STALE", c.nwsradar_max_stale)
        for k, v in overrides.items():          # keyword overrides win over the environment
            if v is None or k == "sites":
                continue
            if k not in {f.name for f in fields(cls)}:
                raise TypeError("unknown config override %r" % k)
            setattr(c, k, v)
        c.validate_vlba()
        return c

    def validate_vlba(self) -> None:
        """The VLBA_* values (the parent's ``validate`` has already run)."""
        def positive(name: str, value, what: str) -> None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or not value > 0:
                raise ValueError("%s must be a positive number%s" % (name, what))

        parse_event_names(self.veto_events)
        if isinstance(self.veto_onset_lead_s, bool) or not isinstance(self.veto_onset_lead_s, int) \
                or self.veto_onset_lead_s < 0:
            raise ValueError("VLBA_VETO_ONSET_LEAD must be a whole number of seconds >= 0")
        positive("VLBA_RADAR_KM", self.radar_km, " (km)")
        if self.radar_km > 300:
            raise ValueError("VLBA_RADAR_KM must be <= 300 (km)")
        if self.island_radar and self.radar_km > self.map_km / 2.0:
            # the NWS composites are fetched for the map frame only: a wider ring is partly unseen
            raise ValueError("VLBA_RADAR_KM must be <= %.0f km, half the map width (WEATHER_MAP_MILES), while "
                             "VLBA_ISLAND_RADAR=1: the island composites cover the map only" % (self.map_km / 2.0))
        if not isinstance(self.radar_dbz, (int, float)) or isinstance(self.radar_dbz, bool) \
                or not (-30 <= self.radar_dbz <= 80):
            raise ValueError("VLBA_RADAR_DBZ must be a reflectivity between -30 and 80 (dBZ)")
        # each run fetches the frames before the newest one that a confirmation needs
        # (make_vlba_page._earlier_mrms and _earlier_island_checks): more per extra frame, so the
        # count is bounded
        if isinstance(self.radar_trigger_after, bool) or not isinstance(self.radar_trigger_after, int) \
                or not (1 <= self.radar_trigger_after <= 6):
            raise ValueError("VLBA_RADAR_TRIGGER_AFTER must be a whole number, 1..6")
        if isinstance(self.radar_min_coverage, bool) or not isinstance(self.radar_min_coverage, (int, float)) \
                or not math.isfinite(self.radar_min_coverage) or not (0 <= self.radar_min_coverage <= 100):
            raise ValueError("VLBA_RADAR_MIN_COVERAGE must be a percentage, 0..100")
        if not isinstance(self.mrms_url, str) or not self.mrms_url.startswith(("http://", "https://")):
            raise ValueError("VLBA_MRMS_URL must be an http:// or https:// URL")
        self.mrms_url = self.mrms_url.rstrip("/")
        if not isinstance(self.glm_hold_s, bool) and isinstance(self.glm_hold_s, int) and 0 <= self.glm_hold_s < 60:
            # a flash counts only while it is younger than the hold: a shorter one ignores flashes
            raise ValueError("VLBA_GLM_HOLD must be >= 60 s (a shorter hold would ignore the flashes; "
                             "VLBA_GLM=0 switches lightning off)")
        for name, value in (("VLBA_RADAR_HOLD", self.radar_hold_s),
                            ("VLBA_RADAR_STALE", self.radar_stale_s),
                            ("VLBA_GLM_HOLD", self.glm_hold_s),
                            ("VLBA_GRIDPOINT_TTL", self.gridpoint_ttl),
                            ("VLBA_GRIDPOINT_MAX_STALE", self.gridpoint_max_stale),
                            ("VLBA_NWSRADAR_MAX_STALE", self.nwsradar_max_stale)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("%s must be a whole number of seconds >= 0" % name)
        for name, value in (("VLBA_NWS_CLOUD_MAX", self.nws_cloud_max),
                            ("VLBA_NWS_PRECIP_MAX", self.nws_precip_max),
                            ("VLBA_NWS_THUNDER_MAX", self.nws_thunder_max),
                            ("VLBA_OBS_HUMIDITY_MAX", self.obs_humidity_max)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or value > 100:
                raise ValueError("%s must be a percentage (<= 100; negative switches the check off)"
                                 % name)
        if isinstance(self.nws_stale_min, bool) or not isinstance(self.nws_stale_min, int) \
                or self.nws_stale_min < 1:
            raise ValueError("VLBA_NWS_STALE_MIN must be a whole number of minutes >= 1")
        positive("VLBA_OBS_MAX_KM", self.obs_max_km, " (km)")
        positive("VLBA_OBS_MAX_DELEV_M", self.obs_max_delev_m, " (m)")
        if isinstance(self.wind_mph, bool) or not isinstance(self.wind_mph, (int, float)) \
                or not math.isfinite(self.wind_mph) or self.wind_mph < 0:
            raise ValueError("VLBA_WIND_MPH must be a speed >= 0 (0 = no wind check)")
        positive("VLBA_GLM_KM", self.glm_km, " (km)")
        if self.glm_km > 500:
            raise ValueError("VLBA_GLM_KM must be <= 500 (km)")
        if isinstance(self.glm_window_min, bool) or not isinstance(self.glm_window_min, int) \
                or not (1 <= self.glm_window_min <= 60):
            raise ValueError("VLBA_GLM_WINDOW_MIN must be a whole number of minutes, 1..60")
        if isinstance(self.glm_stride, bool) or not isinstance(self.glm_stride, int) \
                or not (1 <= self.glm_stride <= 30):
            raise ValueError("VLBA_GLM_STRIDE must be a whole number, 1..30")
        parse_glm_buckets(self.glm_buckets)
        if not isinstance(self.glm_max_arc_deg, (int, float)) or isinstance(self.glm_max_arc_deg, bool) \
                or not (0 < self.glm_max_arc_deg <= 90):
            raise ValueError("VLBA_GLM_MAX_ARC must be between 0 and 90 (degrees of arc)")
        if isinstance(self.glm_timeout_s, bool) or not isinstance(self.glm_timeout_s, int) \
                or self.glm_timeout_s < 5:
            raise ValueError("VLBA_GLM_TIMEOUT must be a whole number of seconds >= 5")
        if isinstance(self.glm_workers, bool) or not isinstance(self.glm_workers, int) \
                or not (1 <= self.glm_workers <= 16):
            raise ValueError("VLBA_GLM_WORKERS must be a whole number, 1..16")
        if not isinstance(self.nwsradar_url, str) or not self.nwsradar_url.startswith(("http://", "https://")):
            raise ValueError("VLBA_NWSRADAR_URL must be an http:// or https:// URL")
        self.nwsradar_url = self.nwsradar_url.rstrip("/")
        if not self.state_file:
            self.state_file = os.path.join(self.cache_dir, "vlba_state.json")
        # "~/..." like WEATHER_CACHE_DIR; a relative path would land in the cron job's cwd
        self.state_file = os.path.abspath(os.path.expanduser(self.state_file))

    # ---- derived -----------------------------------------------------------------------
    @property
    def veto_event_names(self) -> List[str]:
        """The warning products that count, in NWS Title Case (``parse_event_names``)."""
        return parse_event_names(self.veto_events)[0]

    @property
    def glm_satellites(self) -> List[Tuple[str, float]]:
        """``[(bucket, sub-satellite longitude), ...]`` (``parse_glm_buckets``)."""
        return parse_glm_buckets(self.glm_buckets)

    def check_enabled(self, key: str) -> bool:
        """Whether a verdict check is switched on in this configuration (``decision`` keys:
        warning, radar, glm, forecast_cloud, forecast_precip, forecast_thunder,
        forecast_winter, obs_precip, obs_humidity, wind)."""
        return {
            "warning": bool(self.veto_event_names),
            "radar": bool(self.radar_enabled or self.island_radar),
            "glm": bool(self.glm_enabled and self.glm_satellites),
            "forecast_cloud": self.nws_cloud_max >= 0,
            "forecast_precip": self.nws_precip_max >= 0,
            "forecast_thunder": self.nws_thunder_max >= 0,
            "forecast_winter": bool(self.nws_winter),
            "obs_precip": bool(self.obs_precip),
            "obs_humidity": self.obs_humidity_max >= 0,
            "wind": self.wind_mph > 0,
        }.get(key, False)
