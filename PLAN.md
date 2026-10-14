# vlbaweather — plan and module contract

A static web page with the weather at the ten VLBA antenna sites, generated every 5 minutes
(no local sensors, no safety-monitor output). Built on the `weather` package of
local-weather-awareness (the parent; copied into `weather/`, its `make_weather_page.py` as
`weather/pipeline.py`), and on the weather cuts of ttustatus (the TTU Alpaca
SafetyMonitor) for the GOOD / BAD verdict.

Written 2026-10-08 before the code; the "Decisions" section records what was chosen and why.
Sections 10-12 record the changes of the following days. Until 2026-10-13 the service lived
in the `vlbaweather/` directory of the parent's checkout; section 13 records the move to a
repository of its own. Sections 3, 5, 7 and 8 describe the current layout.

## 1. Scope

Per antenna, top to bottom: the verdict (GOOD / BAD with every reason and every input the
verdict used), whether the sun is up, the radar map (100 x 100 miles, alert areas shaded,
the 30 km radar ring and the 50 km lightning ring, GLM flashes of the last minutes drawn),
current conditions from the nearest NWS station, the active NWS alerts at and near the site,
GOES GLM lightning, the sun and twilight times, the hourly forecast and the 7-day forecast.
Page-wide: a summary table at the top (every antenna with its verdict, the reasons in a few
words, sun up/down and local time), the sticky navigation, the NWS alerts banner, the
footer with the freshness of every source, credits and the disclaimer.

Not in scope (asked): road closures, local sensors, machine-readable safety output (there is
a `status.json` for monitoring the *generator*, as in the parent, with the verdicts added
for convenience; it is not a safety interface).

## 2. Sites

`vlba/sites.py` holds the ten antennas (VLBA order: BR FD HN KP LA MK NL OV PT SC), with the
NRAO coordinates (checked 2026-10-08 against the NRAO / Wikipedia tables), the elevation and
the IANA time zone (KP: America/Phoenix, no DST; SC: America/St_Thomas; MK: Pacific/Honolulu).
Slugs are the lower-case two-letter codes (`radar_mk_dark.png`, `#site-mk`). The list is
used when neither `--sites`, `WEATHER_SITES` nor `WEATHER_SITES_FILE` names other sites (a
subset for tests, or another array); entries may carry `code` and `elev_m` beyond the
parent's `slug|name|lat|lon|tz`.

Verified today with api.weather.gov: MK -> grid HFO 255,77, zone HIZ028, radar PHKM, tz
Pacific/Honolulu; SC -> grid SJU 289,69, zone VIZ002, radar TJUA, tz America/St_Thomas.
Nearest stations: MK -> PHSF Bradshaw AAF (1871 m, 11 km; the antenna is at 3763 m), SC ->
TISX (19 m, 24 km).

## 3. Data sources (all free, no key; probed 2026-10-08 from this host)

| What | Source | Notes |
|---|---|---|
| NWS point metadata, observations, 7-day and hourly forecasts, alerts, zone outlines | api.weather.gov via `weather.nws` / `weather.alerts` | unchanged |
| NWS raw gridpoint (skyCover, probabilityOfThunder, probabilityOfPrecipitation, snowfallAmount, iceAccumulation, windSpeed, windGust, weather) | `https://api.weather.gov/gridpoints/{grid}/{x},{y}` | `vlba/gridpoint.py`; HFO and SJU have no probabilityOfThunder (empty series), SJU no snowfallAmount / iceAccumulation either, TWC no iceAccumulation |
| Radar, contiguous US | NOAA MRMS composite via IEM, `weather.radar` | the 8 CONUS sites; ring check on the palette indices |
| Radar, Hawaii and the Virgin Islands | NWS radar.weather.gov composites on `https://opengeo.ncep.noaa.gov/geoserver/{hawaii,carib}/ows` (WMS 1.3.0, layers `hawaii_bref_qcd`, `carib_bref_qcd`: "Quality Controlled 1km x 1km ... Radar Base Reflectivity ... MRMS algorithm", a frame every 2 min, TIME dimension with the latest as default) | `vlba/nwsradar.py`. The server returns pre-coloured RGBA (GetFeatureInfo gives RED/GREEN/BLUE/ALPHA bands; `SLD_BODY` is "forbidden"; WCS is the same 4 bands), so dBZ is decoded from the colours with the radar.weather.gov colour scale. Per-workspace GetCapabilities (18 KB) gives the latest frame time. IEM's `ridge::PHKM-N0B-0` tiles are the fallback option (not implemented) |
| MRMS Hawaii/Caribbean GRIB2 on mrms.ncep.noaa.gov | NOT usable: 403 for every product directory and file from here |
| MRMS GRIB2 on NOAA Open Data (AWS `noaa-mrms-pds`, anonymous HTTPS; added 2026-10-12) | `CONUS/SeamlessHSR_00.00` (behind IEM's lcref, ~1.3 MB every 2 min) and `HAWAII` / `CARIB` `MergedBaseReflectivityQC_00.50` (behind the NWS composites, 20-60 KB): read for the radar coverage only (-999 no radar, -99 no echo), `vlba/mrms.py` |
| GLM lightning | GOES-19 (East, 75.2 W) and GOES-18 (West, 137.0 W) `GLM-L2-LCFA` granules on AWS Open Data (`noaa-goes19`, `noaa-goes18` buckets, anonymous HTTPS; 20-s granules, ~240 KB and ~175 KB each) | `vlba/glm.py`, parsed in RAM with numpy + h5py (netCDF4 also accepted). Each site uses the satellites whose sub-satellite point is within 70 deg of arc (MK: West only; SC and HN: East only; the other seven both). ~6 MB per run with both satellites and a 5-min window |
| Sun | `weather.sun` (NOAA equations) | sun up = altitude above -0.833 deg (upper limb visible) |
| Basemap | OpenStreetMap tiles via `weather.radar` | fetched once per site and theme, cached |

## 4. The verdict (ttustatus cuts, adapted to "no local sensors")

`vlba/decision.py`, pure (inputs: the site entry, the persisted per-site state, `now`).
BAD when any check is bad; GOOD otherwise; `incomplete` when an enabled check had no usable
data this run (the page shows an amber "GOOD?" naming the missing inputs, never a silent
green); a check that cannot apply to the antenna at all is "not provided" (n/a) and does not
count as missing. Checks, with the ttustatus defaults:

| Check | Bad when | Source | ttustatus origin |
|---|---|---|---|
| NWS warning over the site | an alert AT the site (polygon contains it, or a zone product lists its zones) whose event is in `VLBA_VETO_EVENTS`, from 15 min before its onset to its end | `alerts_at` | `TTU_SAFETY_HAZARD_VETO_EVENTS` (Tornado, Dust Storm, High Wind Warning); here plus Extreme Wind, Hurricane, Typhoon, Tropical Storm, and the snowstorm products Blizzard, Winter Storm, Ice Storm, Snow Squall, Lake Effect Snow Warning |
| Radar | an echo >= 20 dBZ within 30 km in 2 consecutive radar frames, each frame counted once (the first is shown as "unconfirmed"); held 15 min after the last confirmed hit; no echo is clear only where radar covers >= 90 % of the ring (MRMS's own file of the frame), else "no data" | MRMS (CONUS) or the NWS composite (islands) | `RADAR_KM`, `RADAR_DBZ`, `RADAR_TRIGGER_AFTER`, `RADAR_LATCH_SEC` |
| Lightning | a GLM flash within 50 km in the fetch window (5 min plus 1 min of overlap, longer after a late run); bad while one is in the window and 30 min after the last one; a flash in partial data counts | GLM | `GLM_TRIGGER_KM`, `GLM_COOLOFF_HOURS`; here day and night |
| Forecast | this hour or next: cloud cover > 60 %, precipitation probability > 20 %, thunder probability > 15 % | raw gridpoint | `NWS_CLOUD_MAX` / `_PRECIP_MAX` / `_THUNDER_MAX` |
| Winter precipitation | this hour or next: snowfall or ice accumulation > 0, or a weather entry of snow / sleet / freezing precipitation / ice with a coverage of likely or more | raw gridpoint | added: "include snowstorms in bad" |
| Precipitation observed | the station reports precipitation now (present weather, text, METAR weather groups, or precipitation in the last hour); the report read is the station's latest with weather of the last 90 min, else the newest such report of the station and the next three qualifying ones (the nearer on a tie; no temperature needed); "no precipitation" while a possibly newer report could not be read is unknown | observation | the WU `precipRate > 0` rain latch (1 h); here the station report itself lasts about an hour |
| Humidity observed (off by default) | relative humidity > `VLBA_OBS_HUMIDITY_MAX` | observation | `HUMIDITY_UNSAFE_ABOVE` (95 %); off here (asked 2026-10-08) |
| Wind | observed wind or gust, or forecast wind or gust this/next hour, >= 45 mph (`VLBA_WIND_MPH`) | observation + gridpoint | none; on at 45 mph because VLBA antennas stow in wind (asked 2026-10-08) |

The observation checks use the nearest station of NWS's list for the grid cell that lies
within `VLBA_OBS_MAX_KM` (50 km) and `VLBA_OBS_MAX_DELEV_M` (1000 m of elevation) of the
antenna and has a recent report; the parent's choice (the nearest with a report) is
replaced by it when it does not qualify (Kitt Peak: QSEA3 1213 m lower, QSBA3 qualifies).
Where no station of the list qualifies (MK, PT) the checks are "not provided" (n/a), like
a forecast field an office does not produce, and the verdict rests on the forecast, radar,
GLM and warnings; a qualifying station without a recent report is "unknown".
Sun altitude is NOT a check (asked: information only).

Freshness: a forecast whose NWS update is older than 12 h (ttustatus uses 150 min, but NWS
offices update their grids only a few times a day, measured 2 to 6 h apart on 2026-10-08), a
malformed gridpoint series, a radar frame older than 20 min, a ring radar covers less than
90 % of or whose coverage could not be read, a GLM poll that failed, an observation older
than 3 h count as "no data" (unknown), never as "clear". Holds (radar
15 min, GLM 30 min), the radar streak and the last radar frame counted live in
`<cache>/vlba_state.json`, written atomically once per run.

## 5. Modules and contracts

```
vlbaweather/              the checkout
  make_vlba_page.py       entry point: one run (lock, budget, alerts, MRMS frame, GLM, per
                          site: NWS products, gridpoint, sun, radar check, GLM view,
                          verdict, maps; state file; index.html + status.json)
  vlba/config.py          VlbaConfig(weather.config.Config): WEATHER_* as in the parent
                          (defaults changed: refresh 300 s, roads off, run budget 240 s,
                          cache ~/.cache/vlbaweather, title "VLBA weather", UA
                          "vlbaweather (+https://github.com/kirxkirx/vlbaweather)")
                          plus the VLBA_* settings below
  vlba/sites.py           VLBA_SITES
  vlba/gridpoint.py       fetch(cfg, cache, site, meta) -> hourly rows of the raw gridpoint
  vlba/glm.py             fetch(cfg, now) -> flashes of the window; site_view(res, site, cfg)
  vlba/nwsradar.py        region_for(frame_bbox); fetch(cfg, cache, site, frame) -> decoded
                          frame (RGBA layer + dBZ index image + time) for sites outside MRMS
  vlba/radarcheck.py      mrms_ring(img, lat, lon, km, dbz) / index_ring(idx, frame, ...)
  vlba/mrms.py            coverage(cfg, domain, frame_ts, sites, km) -> the radar coverage
                          of each ring from MRMS's own GRIB2 file; site_coverage(res, slug)
  vlba/decision.py        evaluate(cfg, entry, state, now) -> verdict; load/save_state
  vlba/maps.py            VlbaSiteMap(weather.radar.SiteMap): island radar layer, rings
                          and GLM flashes drawn, caption label
  vlba/page.py            render_html / write_page / write_status_json (reuses weather.page)
  vlba/tests/             pytest, network blocked (FakeHttp, from the parent's suite)
  weather/                the parent's library, copied (weather/pipeline.py: its
                          make_weather_page.py, the run steps the generator reuses)
  deploy/                 vlbaweather-cron.sh, install-cron.sh, apache-vlbaweather.conf
  vlbaweather.env.example every variable with its default
  run_local.sh            local preview (5-min loop + http.server)
  README.md               what it shows, the cuts, sources, deployment
```

### VLBA_* settings (`vlba/config.py`, every one overridable, validated at startup)

| Variable | Default | Meaning |
|---|---|---|
| VLBA_VETO_EVENTS | the 12 events of section 4 | comma list, case-insensitive; "none" (or empty, systemd only) = no warning check |
| VLBA_VETO_ONSET_LEAD | 900 | s before a warning's onset from which it counts |
| VLBA_RADAR_KM / VLBA_RADAR_DBZ | 30 / 20 | ring radius, echo threshold |
| VLBA_RADAR_TRIGGER_AFTER | 2 | consecutive radar frames (each counted once) with an in-ring echo before it counts |
| VLBA_RADAR_HOLD | 900 | s the radar check stays bad after the last confirmed hit |
| VLBA_RADAR_STALE | 1200 | s: an older frame is "no data" for the verdict |
| VLBA_RADAR_COVERAGE | 1 | read MRMS's own file of each frame for the radar coverage of the rings |
| VLBA_RADAR_MIN_COVERAGE | 90 | % of the ring radar must cover for "no echo" to be clear |
| VLBA_MRMS_URL | https://noaa-mrms-pds.s3.amazonaws.com | base of the MRMS bucket |
| VLBA_ISLAND_RADAR | 1 | NWS composites for sites outside the MRMS grid |
| VLBA_NWSRADAR_URL | https://opengeo.ncep.noaa.gov/geoserver | base of the WMS |
| VLBA_NWSRADAR_MAX_STALE | 1800 | s: reuse the last good composite (labelled stale) |
| VLBA_GLM | 1 | GLM layer on/off |
| VLBA_GLM_KM / VLBA_GLM_HOLD | 50 / 1800 | ring radius, s the check stays bad after the last flash |
| VLBA_GLM_WINDOW_MIN | 5 | minutes of granules fetched per run |
| VLBA_GLM_STRIDE | 1 | use every Nth granule (2 halves the traffic) |
| VLBA_GLM_BUCKETS | noaa-goes19=-75.2;noaa-goes18=-137.0 | bucket=sub-satellite longitude |
| VLBA_GLM_MAX_ARC | 70 | deg of arc from the sub-satellite point within which a site is covered |
| VLBA_GLM_TIMEOUT | 45 | s cap on the whole GLM fetch of a run |
| VLBA_GLM_WORKERS | 4 | parallel granule downloads |
| VLBA_NWS_CLOUD_MAX / _PRECIP_MAX / _THUNDER_MAX | 60 / 20 / 15 | %, bad when exceeded this or next hour; a negative value switches the check off |
| VLBA_NWS_WINTER | 1 | snow / ice check |
| VLBA_NWS_STALE_MIN | 720 | min since the NWS update beyond which the forecast is "no data" |
| VLBA_GRIDPOINT_TTL / _MAX_STALE | 1800 / 7200 | s, like the other NWS products |
| VLBA_OBS_HUMIDITY_MAX | -1 | %; negative = off (the default since 2026-10-08) |
| VLBA_OBS_PRECIP | 1 | precipitation-observed check |
| VLBA_OBS_MAX_KM / VLBA_OBS_MAX_DELEV_M | 50 / 1000 | station must be this close / this level with the antenna |
| VLBA_WIND_MPH | 45 | wind check threshold; 0 = off |
| VLBA_STATE_FILE | <cache>/vlba_state.json | holds and streaks |

### Run dict (the parent's, plus)

```
run["glm"]      = glm.fetch result without "flashes" (ok, stale, error, age_s, window_start,
                  window_end, sats {G19: {bucket, sub_lon, ok, error, listed, selected,
                  fetched, failed, dropped (given up: failed twice, or gone, or too old),
                  refetch_failed (overlap granules the last run examined), flashes}},
                  deps, elapsed_s, skipped, partial, newest {G19: epoch s of the newest
                  granule of the window}, retry {G19: [epoch s of the granules to fetch
                  again, first]}, seen {G19: [epoch s of the granules examined or given
                  up]}); the state file keeps them as glm_newest / glm_retry / glm_seen
run["radar_coverage"] = {domain: mrms.coverage result (ok, error, domain, ts, url, sites:
                  {slug: {cells, uncovered, covered_pct}})}: CONUS once per run, an
                  island domain per site (merged)
run["summary"]  = {"good": n, "bad": n, "incomplete": n}
site entry +=
  "gridpoint":    gridpoint.fetch result (ok, stale, error, age_s, updated, update_age_min,
                  elevation_m, missing: [field] (not produced), unusable: [field]
                  (malformed this run), hours: [row], now: row|None, next: row|None)
                  row = {start (aware UTC), local, sky_cover_pct, pop_pct, thunder_pct,
                         qpf_mm, snow_mm, ice_mm, wind_kmh, gust_kmh, temp_c, rh_pct,
                         weather: [{coverage, weather, intensity}],
                         periods: {qpf_mm|snow_mm|ice_mm: {start, hours}}}  (an amount
                         is the total of its NWS period, repeated in each of its hours)
  "obs_station":  {"id" (None without an observation), "name", "lat", "lon", "elev_m",
                   "dist_km", "delev_m", "usable", "why", "qualifying": [station ids
                   of the list within both limits, nearest first] | None (unknown),
                   "weather": "reported" | "none" (neither its recent reports nor those
                   of the next qualifying stations ever carry weather) | "unknown" (no
                   answer this run), "weather_why"} | None
  "obs_raw":      + "reports_weather", "metar"; and "report_station", "report_age_min",
                   "report_stale", "report_passed" (why the latest was not read),
                   "report_doubt" (a possibly newer report that could not be read) when
                   the precipitation check reads another report than the station's latest:
                   its own newest with weather, or another qualifying station's
  "obs_raw":      {"present_weather": [str], "precip_last_hour_mm": float|None, "text": str}
  "radar_check":  {"available", "stale", "source": "MRMS"|"NWS Hawaii composite"|...,
                   "frame_ts", "age_s" (islands: the copy's age), "in_ring", "nearest_km",
                   "count", "max_dbz", "error", "off" (switched off on purpose),
                   "coverage": mrms.site_coverage result (ok, error, ts, cells,
                   uncovered, covered_pct) | {"off": True} (with a ring scan only)}
  "island_radar": nwsradar.fetch result without images (only sites outside the MRMS grid;
                  {"not_applicable": True, ...} when VLBA_ISLAND_RADAR=0)
  "glm":          glm.site_view result (covered, sats, available (one covering satellite's
                  window complete), partial, error, count_window (a flash both satellites
                  saw once), count_by_sat, nearest_km, nearest_bearing, nearest_t,
                  nearest_sat, flashes_on_map: [(lat, lon)], window_min)
  "verdict":      decision result (status GOOD|BAD|NO DATA, incomplete, reasons (in
                  decision.REASON_ORDER), missing, checks (each with its reasons, a
                  "watch" one with a short note), scheduled, watch)
  "sun_up":       bool | None
  "obs":          + "display_only": True when no station qualifies and NWS sent nothing
                   usable (or a stale copy): only shown, no problem of the run
```

### Status keys

Every fetch result carries the parent's `ok / stale / error / age_s`.

## 6. Page

Same visual family as the parent (its CSS, JS, tiles, pills, day/night toggle; reload every
300 s). Verdict colours: GOOD = the template's `--good` green, BAD = `#b42318` red (the
ttustatus UNSAFE red), GOOD? (inputs missing) = `#b54708` amber, NO DATA = grey; the nav dot
is amber too for a GOOD with an unconfirmed echo or a scheduled warning, whose summary text
is amber. The summary table is the first thing on the page; below 600 px it stacks.
Masthead clock: UTC, with each antenna's local time in its row and section heading.

## 7. Deployment

A Gentoo/OpenRC host with cron and Apache, beside the parent when the host runs it: its own
env files `/etc/vlbaweather.env` and `~<run user>/vlbaweather.env`, cron line `*/5 * * * *
<checkout>/deploy/vlbaweather-cron.sh # vlbaweather` (its own checkout), output
`<DocumentRoot>/vlbaweather` (the live page: https://tau.kirx.net/vlbaweather/), cache
`~apache/.cache/vlbaweather`, syslog tag `vlbaweather`. The generic settings keep their
`WEATHER_*` names (the generator is built on the parent's library); the VLBA settings are `VLBA_*`.
GLM needs `dev-python/numpy` and `dev-python/h5py` (or netcdf4) for the run user's Python;
without them the lightning layer says so and the rest works.

## 8. Tests

`vlba/tests`, pytest with the network blocked (`FakeHttp`, from the parent's suite), on Python 3.9 and
3.12: config and sites, gridpoint expansion, GLM (synthetic HDF5 granules written with h5py
in the test; skipped without h5py; FOV; the per-site view), the island radar (synthetic WMS
PNGs in the scale's colours; the time from a GetCapabilities snippet; stale fallback),
the ring scans, every check of the decision with holds and streaks, the page (escaping,
summary, verdict cards, older run dicts), the whole run with a CONUS and an island site and
with every source dead, and the deploy files (env example vs config, DRY_RUN installer,
wrapper syntax), run by CI on every push.

## 9. Decisions

- One new package beside the parent (`vlbaweather/vlba`), importing `weather`: the parent
  stays a generic multi-site generator; the only parent change is a caption label hook in
  `weather.radar.SiteMap._decorate` (`radar["label"]`, default "MRMS").
- Sites outside the MRMS grid get the NWS radar composites from opengeo.ncep.noaa.gov
  (radar.weather.gov's own data, MRMS-derived, 2-min cadence). Colours are decoded to dBZ
  because the server will not serve values; the result is re-coloured in the parent's dBZ
  palette so every map reads the same.
- GLM day and night (asked), both satellites, their flashes combined per site (a flash both
  detect counted once).
- The cloud-cover cut is kept (asked: "the ttustatus cuts as the boundary", and confirmed by
  the user on 2026-10-08) although it is an optical-astronomy criterion;
  `VLBA_NWS_CLOUD_MAX=-1` switches it off. The humidity cut is off (the user, 2026-10-08:
  condensation is an optical-dome concern). The wind cut is on at 45 mph (the user,
  2026-10-08: the antennas stow in high wind). The twelve warning products stay (the
  user, 2026-10-08).
- Cadence 5 min (asked); page reload 300 s. The radar confirmation (2 runs) therefore costs
  up to 10 min on real rain; the first frame is shown as "unconfirmed" so a viewer sees it.

## 10. Changes after the first live runs (2026-10-08)

- Forecast staleness: NWS offices update their gridpoint forecast a few times a day (2 to
  6 h apart measured across the ten offices), so ttustatus's 150-min limit blanked every
  forecast check. `VLBA_NWS_STALE_MIN` now defaults to 720 min, and the check detail shows
  the age of the NWS update ("NWS update 5 h 25 min ago").
- Run budget: a cold first run (basemap tiles for every site and theme, 61 alerts' zone
  outlines, the GLM granules) took 151 s and ran out of the 150-s budget before the last
  antenna; the default is 240 s. A warm run takes 40-80 s, most of it the GLM granules.
- A third verdict state, NO DATA, for a run in which no check could answer (every source
  dead): a green "GOOD?" on nothing would have been a lie.
- The RIDGE II colour scale of the NWS composites was derived exactly: the GeoServer style
  is an empty RasterSymbolizer and no table is published, so the MRMS source grids
  (PNG-packed GRIB2 on mrms.ncep.noaa.gov, which answers the research host but refuses
  this one) were matched pixel for pixel with the RGBA GeoTIFFs the server redistributes.
  The stops (-30 .. 80 dBZ, linear in between; verified -19.5 .. 59) are in
  `vlba/nwsradar.py`; the live Hawaii and Caribbean composites then decode with no unknown
  colour. Those PNG-packed GRIB2 files would be an exact-values alternative to the colour
  decoding, should the server ever allow this host.
- North Liberty is 58 deg of arc from GOES-West, so it uses both satellites; only Mauna Kea
  (West) and St. Croix and Hancock (East) have a single one.
- The map key (rings, flashes) moved to the top-right corner: bottom-left it collided with
  the parent's scale bars.

## 11. Second session (2026-10-08): independent review, the user's answers

An independent review (six reviewers, one skeptic per area) of the first session's code,
and the user's answers to the open questions, changed:

- The user's answers: cloud cut kept, humidity cut off (`VLBA_OBS_HUMIDITY_MAX=-1`), wind
  cut on at 45 mph (`VLBA_WIND_MPH=45`; the forecast's sustained wind counts beside its
  gust), the twelve warning products kept. The footer's description of the checks is now
  spelled out from the configuration, so it never describes a cut that is off.
- Radar: the streak counts frames, each once (`radar_last_frame_ts` in the state file). A
  frame seen again by a later run, or the last good frame reused after a failed fetch
  (`stale`), used to count again, so a single echo frame confirmed itself; ttustatus
  never counts a failed fetch.
- GLM: a satellite failing (even one granule of it) made every antenna it covers
  "unavailable", and a flash the other satellite saw was ignored (GOOD? instead of BAD).
  Now a covering satellite's complete window makes the view available, and any flash in
  the ring counts, even from partial data. Each window reaches 60 s into the previous one:
  the newest granules of a window are not published yet when a run lists them, and no run
  fetched them (13 % of the time blind, simulated). A flash both satellites detect (1 s,
  20 km) counts once.
- Observation: the station is chosen by the antenna's rule (the nearest qualifying one with
  a recent report), not the parent's nearest-with-a-report. Where no station of NWS's list
  qualifies (MK, PT) the observation checks are "not provided" instead of "unknown", so
  those antennas no longer read GOOD? on every run.
- Gridpoint: a malformed series is "unusable" (unknown), never "missing" (not provided); one
  bad entry is skipped instead of discarding its series; amounts carry their NWS period
  and the reasons name it ("snowfall 12.7 mm forecast for 06:00-12:00 MDT").
- Page: the reasons come one per check (both hours in one) and in REASON_ORDER (the site
  now first, the forecast after); for a GOOD verdict the summary names an unconfirmed echo,
  a scheduled warning or missing inputs instead of "all checks clear", with an amber badge
  (GOOD?) or nav dot; the layout works at phone width; the footer has a pill for the
  gridpoint forecasts and `status.json` their state; the disclaimer and credits name NRAO.
- Later in the same review: frames either side of an outage do not form a streak; a frame
  of unknown time ages with its cached copy; a pixel of an unrecognised colour inside an
  island ring makes the radar check "no data" (a changed NWS colour scale must not read as
  "no echo"); a stale composite is reused only for the identical map request; VLBA_RADAR_KM
  must fit in half the map while the island composites are on; a source switched off on
  purpose (WEATHER_RADAR=0, VLBA_ISLAND_RADAR=0) makes its check "off", not missing.
- GLM: the state file keeps the newest granule each satellite delivered (`glm_newest`); a
  late run reaches back to just after it (at most 15 min), because the GLM step starts 6 to
  80 s into a run and a fixed overlap does not cover that drift.
- Observation: a station that reports no weather (mesonet / RAWS: no description, raw
  message, present weather or precipitation amount; Brewster's CJDW, Kitt Peak's QSBA3)
  cannot answer "precipitation observed": a qualifying station that reports weather is
  preferred (Brewster: KOMK), and without one the check is n/a ("unknown" while one could
  not be checked this run). Fog, mist, haze, squalls and low drifting snow are no
  precipitation; a joined description is read part by part.
- Warnings: without the site's point metadata its zones are unknown and the warning check is
  "unknown" instead of "none in effect".
- Configuration: VLBA_VETO_EVENTS=none switches the warning check off (the cron wrapper drops
  empty values, so an empty value only worked under systemd); the VLBA_* switches accept
  1/0, true/false, yes/no, on/off and nothing else; NaN or infinite thresholds are startup
  errors; VLBA_STATE_FILE is ~-expanded, and a state file that cannot be written is a run
  error; a VLBA antenna named through --sites / WEATHER_SITES keeps its code and height, and
  any other site uses its NWS grid cell's elevation for the station rule.
- CI installs numpy and h5py (the GLM parser tests ran nowhere) and checks the syntax of
  both cron wrappers (`sh -n a b` checked only `a`).
- A second independent review, of these changes, found and the session fixed: a confirmed
  echo on a repeated frame turned into an amber "watch" once the hold ran out (now BAD while
  the frame is usable and the streak complete, as ttustatus); the streak also keeps the run
  time it last counted (`radar_last_count_ts`), so an outage resets it without frame times
  and an old state file confirms nothing; one blank METAR record made "precipitation
  observed" n/a (now a station is judged by its last 12 reports; a blank record is replaced
  by the newest report with weather of the last 90 min, else no data); the GLM dedup kept
  the farther copy of a flash (now nearest first, one to one), the catch-up skipped a
  `since` older than 15 min (now clamped) and moved past failed granules (now retried), an
  int too big for a float in the state file broke the fetch; an unreadable winter or wind
  series still read clear; a station list entry without a position made "qualifying" empty
  (now unknown) and one odd entry dropped the whole list; the summary's verdict cell took
  the parent's .badge style; the gridpoint pill and status.json called a forecast the
  verdict rejects (too old, unreadable) fresh; the radar ring was drawn where the radar
  check is off; infinite distance limits passed validation.
- A third review, of those fixes, found: a blank latest record of the nearest station made
  its older report win over another station's CURRENT report with weather (a regression);
  records with only a raw METAR counted as "with weather" although nothing read their
  weather groups; another station's blank latest record was not replaced by its recent
  report; an unknown station list could still end in n/a; a stored streak confirmed a
  reused copy of a frame it was never counted on; a report dated a little ahead of this
  host's clock hid the valid ones, and copies of one blank record counted as three. Now
  `make_vlba_page._precip_report` takes, in order, the station's latest report with
  weather, another qualifying station's latest with weather, the newest report with weather
  of the last 90 min (its own first); `decision.metar_precipitation` reads the METAR's
  present-weather groups (VC, DR and remarks excluded); "none" (n/a) needs a known station
  list whose every station's last 12 distinct reports carry no weather; the stored streak
  confirms only the frame it was counted on.
- The third review's GLM and page findings, fixed: a granule of the catch-up part (older
  than the regular window) that does not arrive is neither retried nor a failure of the run
  (a slow store had held every run at the 15-min window, partial, GOOD? everywhere); the
  hold runs from the time of the newest flash in the ring (`last_in_ring_t`), not from the
  run that found it; the 15-min clamp never shrinks a VLBA_GLM_WINDOW_MIN longer than that;
  a run that does not fetch GLM keeps `glm_newest`; `decision.forecast_fitness` is the one
  rule for a usable gridpoint forecast (the page and status.json use it: a stale copy is
  usable, `gridpoint_ok` true, "stale" listed; problem strings stay stable).
- A fourth review, of those fixes, found and the session fixed: a catch-up granule that did
  not arrive was dropped although no run had examined it, and GLM read complete over the
  hole (now any failed granule leaves the run partial and is fetched again by the next run;
  failing a second time it is dropped, that run still partial, so a store that never
  delivers it cannot hold every run at the 15-min window: `glm.fetch(retried=)`,
  `state["glm_retry"]`); a flash in the fetch window older than VLBA_GLM_HOLD (a long
  catch-up window) read "no flash" (now bad while a flash is in the window); the gridpoint
  pill and `gridpoint_ok` counted an unreadable series the verdict does not read
  (temperature, the amount of rain, a switched-off check's series) as a gap
  (`decision.forecast_unreadable`). The precipitation report: another station's latest
  report won whatever its age, also over the station's own newer report with rain, and it
  replaced the whole observation, so the wind and humidity checks and the page moved to the
  farther station on every blank record; a blank copy of a repeated report hid the copy
  with weather; a report without a temperature (NWS QC) was fetched and then ignored; a
  METAR whose present-weather sensor is out (PWINO) read as "no precipitation". Now the
  report is the station's latest with weather of the last 90 min, else the newest such
  report of the station and the next three qualifying stations (nearer on a tie; no
  temperature needed), and only the precipitation check reads it (`obs_raw.report_station`);
  `_qualifying_observation` takes the nearest qualifying station with a recent, complete
  report; a repeated report keeps its copy with weather; PWINO counts as no weather
  information ("unknown", never n/a). The n/a rule is documented as it is coded: the
  station and up to three more qualifying stations, judged by at least 3 distinct of
  their last 12 reports.
- A fifth review, of those fixes, found and the session fixed: holding the GLM marker just
  before a failed granule lost the retry when an older failure lay in the overlap with the
  last run (the hold was skipped), and with a slow store it pinned every run at the 15-min
  window again (re-downloads of granules already delivered failed as "first failures");
  the granule to fetch again, queued last (newest first), was cut by the deadline and
  dropped without a request; a listing failure or an exception forgot what was to be
  fetched again. Now the marker never moves back: the next run fetches the granules of
  `glm_retry` first, explicitly, wherever they lie (their hour is listed too); a failure of
  a granule the last run examined (`glm_seen`) is no gap, one published too late for it is;
  a granule never asked for before the deadline stays to be fetched; one older than 15 min
  or gone from the store is given up (the run partial). The precipitation report: a PWINO
  METAR with mist, haze or a thunderstorm in the vicinity read as "no precipitation" (now
  only precipitation counts); a station's newer history that could not be read let an
  older report of another station say "clear" (now unknown; rain in it still counts); the
  only qualifying station's report without a temperature was never read when the parent
  took a farther station (now a qualifying station's recent report counts with or without
  a temperature: `_latest_report` drops that rule, so St. Croix's TISX is the observation,
  its wind included, and the precipitation check reads the qualifying stations even when
  none can be the observation); a tie went to the parent's station, not the nearer one,
  and ages taken a moment apart decided it (now report times, then distance); Brewster
  fetched three histories that could not win, also with the check off (now a station's
  recent reports only when its latest is newer than the best report found, and nothing
  when the check is off); the detail called a latest report that is merely too old
  "without weather"; the stale mark came from the station's latest, not from the report
  read.


## 12. The user's first local run (2026-10-12)

- `run_local.sh` served a bare directory listing for the whole first run (3.5 min with an
  empty cache; the generator writes index.html at the end of a run). It now writes a
  placeholder index.html first, only where none exists (noclobber, O_EXCL: an earlier
  run's page is kept), which says so and reloads every 10 s until the generator's page
  replaces it; the terminal says so too. Tests run the script with a stub generator and
  its real preview server: the placeholder and its replacement, an earlier page kept,
  Ctrl-C, SIGTERM, a busy port. The README and the env example give the measured
  first-run time instead of "about 150 s".
- The maps' scale bars are 10 mi and 10 km (the user's request) instead of the parent's
  25 mi and 10 mi: the rings and the page's distances are in km. The parent's `SiteMap`
  takes them from a class attribute (`SCALE_BARS`, its own default unchanged), which
  `VlbaSiteMap` overrides.
- A check of the live preview (five independent checkers and a skeptic per finding)
  re-derived the 17:11:28Z run from upstream data: every forecast value (raw gridpoints,
  DWML), every station choice, wind, precipitation report, warning and alert, the radar
  (the MRMS frames decoded anew, IEM's NEXRAD mosaic, KEMX Level III with its hydrometeor
  class: Kitt Peak's BAD was rain), the lightning (the GLM granules parsed anew: every
  nearest flash to 0.1 km), the sun (within 0.07 deg), the times, the page against
  status.json and the loop's health. No verdict was wrong. Fixed: the wind and
  precipitation details doubled the parent's "no usable observation:" prefix; at phone
  width the header still promised local times in each row; the lightning panel wrote
  "912.0 km" where the check row writes "912 km"; the README called the radar product
  composite reflectivity (IEM's lcref is MRMS SeamlessHSR, the lowest unblocked tilt) and
  now says that these frames write "no coverage" like "no echo".
- The user's choices from that check, all four done:
  - Radar coverage (`vlba/mrms.py`): each run reads MRMS's own GRIB2 file of the frame's
    time from NOAA Open Data on AWS (CONUS `SeamlessHSR_00.00`, the product behind IEM's
    lcref, ~1.3 MB; HAWAII / CARIB `MergedBaseReflectivityQC_00.50`, behind the NWS
    composites, 20-60 KB, keyed to the second), the nearest file within 5 min when that one
    is missing, and counts the ring cells marked -999 (no radar; -99 is a covered cell
    without echo), a part of the ring beyond the grid included. Grid template 3.0 and PNG
    packing (5.41) only, read with Pillow. No echo is "clear" only while radar covers at
    least `VLBA_RADAR_MIN_COVERAGE` (90) % of the ring; less, or coverage that could not be
    read, is "no data" (an echo, a hold count as before). `VLBA_RADAR_COVERAGE=0` keeps the
    old "clear", the detail saying so. A failed read is a run error; a ring below the
    minimum a problem in status.json (`radar_coverage` block); a footer pill per domain.
  - An observation that is only shown (no station qualifies: Mauna Kea, Pie Town) is no
    problem of the run when NWS delivers nothing usable: its status is noted once the
    station step knows whether a check reads it (the observation the checks use, which
    can be a qualifying station's, not the first one tried), and status.json leaves out
    the site's "observation: unavailable".
  - A report without a wind value is named in the wind check's detail.
  - The sun chips are neutral (information only); a check without data has the amber dot.
- Left open: St. Croix's "Now" shows dashes when NWS's decoded record is empty while the
  METAR has the values (a METAR fallback would be a parent change); two runs' frames can
  carry the same radar volume scan (KEMX scans every ~7 min, MRMS frames come every 2).

## 13. A repository of its own (2026-10-13)

- The user asked for an independent package in its own public repository,
  https://github.com/kirxkirx/vlbaweather. The parent's library is copied into `weather/`:
  every module of it, since the page and the maps import the road layer too (it stays
  off), at the parent's commit c3a13b4 plus the two hooks this service added
  (`radar["label"]`, `SiteMap.SCALE_BARS`). The parent's `make_weather_page.py` is
  `weather/pipeline.py` (the run steps the generator reuses), and the tests' `FakeHttp`
  moved to `vlba/tests/fakehttp.py`; the parent's own tests stay in its repository.
- The checkout holds everything: `make_vlba_page.py` imports `weather` from its own
  directory, the installers refuse a checkout without `weather/__init__.py` (an incomplete
  copy) instead of looking one directory up, the font probe imports from the checkout, the
  cron wrapper and the unit are unchanged in function. The User-Agent and the unit's
  Documentation= point to the new repository.
- Own `.gitignore` (outputs, caches, real env files, `.private-patterns`, NFS leftovers),
  `.flake8` and CI (Python 3.9 and 3.12: flake8 over `vlba`, `make_vlba_page.py` and
  `weather`, the suite, the shell syntax; shellcheck runs inside the suite). The deploy
  tests read the copied `weather/config.py` and the checkout's own `.gitignore`; the
  comparison of the installers' shared functions with the parent's runs when
  `LWA_CHECKOUT` names a checkout of it.
