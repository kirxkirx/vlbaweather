# vlbaweather

The weather at the ten VLBA antenna sites on one page, regenerated every 5 minutes, for
people: a GOOD / BAD verdict per antenna with every reason, whether the sun is up, a radar
map with the active NWS alert areas and the lightning of the last minutes, the current
conditions, the alerts, the hourly and 7-day forecasts. No local sensors, no road closures,
no safety-monitor output.

It is built on the `weather` package of
[local-weather-awareness](https://github.com/kirxkirx/local-weather-awareness) (NWS products
and alerts, MRMS radar maps, sun times, the HTTP cache, the page style), copied into
`weather/`, and on the weather cuts of [ttustatus](https://github.com/kirxkirx/ttustatus),
the TTU Alpaca SafetyMonitor, which draw the line between GOOD and BAD. Below, "the parent"
is local-weather-awareness.

```
BR  Brewster, WA        GOOD   all checks clear                       ☀ Sun up 20.0°   09:21 PDT
...
SC  St. Croix, VI       BAD    precipitation probability 50 % > 20 % (this hour); ...
```

## What the page shows

At the top, the **summary table**: every antenna with its verdict badge, the reasons in a
few words (what is happening at the site now first, the forecast after it; for a GOOD
verdict an unconfirmed radar echo, a warning scheduled to start or missing inputs, and
"all checks clear" only when there is none of these), "Sun up" or "Sun down" with the
sun's altitude (information only, so in a neutral chip), and the local time. Then the sticky navigation (one pill per antenna, its
dot in the verdict colour, amber for a GOOD that deserves a look), the NWS alerts banner
across all sites, and one section per antenna:

- **Verdict card**: GOOD (green), BAD (red, with the reasons) or NO DATA (grey, nothing
  could be checked). A GOOD verdict with some check lacking data this run reads **GOOD?**
  (amber) and names the missing inputs: unknown is never counted as clear. A check that
  cannot apply to the antenna at all (a field its NWS office does not produce, an
  observation where no station is near enough) is *n/a*, not missing. Below it, every
  check with its state (clear / BAD / watch / scheduled / no data / off / n/a; the dot green,
  red, amber for watch, scheduled and no data, grey for off and n/a), the value
  now, the threshold and the detail.
- **Radar map**, 100 × 100 statute miles centred on the antenna: OpenStreetMap basemap, the
  radar (NOAA MRMS seamless hybrid-scan reflectivity, the lowest unblocked radar tilt, via the
  Iowa Environmental Mesonet inside the contiguous US; the NWS radar composite of
  radar.weather.gov for Mauna Kea and St. Croix),
  the active NWS alert areas shaded in the NWS colours (flood products in reds), the
  **30 km radar ring** (solid) and the **50 km lightning ring** (dashed) of the verdict, the
  **GLM flashes** of the last 6 minutes as yellow diamonds, the frame time, 10 mi and 10 km
  scale bars and the dBZ bar. Dark and light maps; the page shows the one matching its palette.
- **Now**: the newest report of the nearest NWS station that suits the antenna (see the
  observation checks below; the nearest station with a report where none does), and a
  line saying how far from and how much lower than the antenna the station is and whether
  its report is used for the verdict.
- **Lightning (GOES GLM)**: flashes within 50 km in the last 6 minutes (a flash both
  satellites detected counts once), the nearest flash (distance, bearing, time), the
  satellites that see the site; "partial" when a satellite's data is incomplete.
- **Alerts** at and near the site (the parent's cards with the full text).
- **Sun & twilight**: "The sun is above / below the horizon" (information only: the verdict
  does not use it), the altitude, today's sunrise, noon and sunset and every twilight
  event of the next 24 h, in the antenna's time zone.
- **Next 24 hours** and the **7-day forecast** from NWS.

The footer lists every source's freshness (alerts, MRMS, GLM per satellite, the island
composites, each site's NWS products, the gridpoint forecasts behind the forecast checks,
the radar coverage per MRMS domain),
the credits, what the checks are (spelled out from the configuration in use), and the
disclaimer: not an official product of NOAA, NWS or NRAO, not the only source for safety
or operations decisions.
`status.json` beside the page is a machine-readable health summary of the generator (the
parent's, plus the verdicts, the GLM state, the island radar and the radar coverage) for
monitoring; it is not a safety interface. Where no station qualifies (Mauna Kea, Pie Town)
the observation is only shown, so NWS delivering nothing usable there is no problem of the run.

## The verdict

BAD when any check is bad; GOOD otherwise; NO DATA when no check could answer. The checks
are the cuts of ttustatus applied to public data (every threshold is a `VLBA_*` setting,
see `vlbaweather.env.example`):

| Check | BAD when | ttustatus origin |
|---|---|---|
| NWS warning over the site | an alert AT the site (its polygon contains the antenna, or a zone product lists the antenna's forecast / county / fire zone) whose product is one of `VLBA_VETO_EVENTS`: Tornado, Dust Storm, High Wind, Extreme Wind, Hurricane, Typhoon, Tropical Storm, Blizzard, Winter Storm, Ice Storm, Snow Squall and Lake Effect Snow Warning (`VLBA_VETO_EVENTS=none` switches the check off). A warning issued ahead of its onset counts from 15 min before the onset and is shown as *scheduled* until then. Without the site's NWS metadata its zones are unknown and the check has no data | the three-event veto (Tornado, Dust Storm, High Wind Warning) with the same onset rule; the wind and snowstorm products added here |
| Rain on radar | an echo of 20 dBZ or more within 30 km in **two consecutive radar frames** (the first is shown as *watch: unconfirmed*), then held for 15 min after the last confirmed echo. Each frame counts once: a frame an earlier run already saw, or the last good one reused after a failed fetch, is no new evidence, and frames either side of an outage are not consecutive. At Mauna Kea and St. Croix a pixel of a colour the decoder does not recognise within the ring makes the check "no data", never "no echo". No echo is clear only where radar covers the ring: MRMS's own data of the frame must show radar coverage over at least 90 % of it (`VLBA_RADAR_MIN_COVERAGE`), else the check is "no data" (a radar outage, or coverage that could not be read); an echo counts either way, and a new frame without an echo restarts the count whatever its coverage | 30 km / 20 dBZ ring, `RADAR_TRIGGER_AFTER=2` (a failed fetch never counts there either), 15-min freeze |
| Lightning | a GOES GLM flash within 50 km in the last 6 minutes (the 5-minute window plus 1 minute of the previous run's, and back to the newest granule the previous run examined when that run was late: no granule between two runs goes unexamined; one that does not arrive is fetched again, first of all, by the next run, once, and a run that misses one is marked partial), bad while a flash is in that window and for 30 min after the last one. A flash counts even when a satellite's data is incomplete; "clear" needs one covering satellite's complete window | 50 km ring, 30-min cool-off; day and night here |
| Precipitation probability | above 20 % this hour or next | `NWS_PRECIP_MAX` |
| Thunder probability | above 15 % this hour or next (the Honolulu and San Juan offices do not produce it: *n/a* at Mauna Kea and St. Croix, where radar, GLM and the precipitation probability remain) | `NWS_THUNDER_MAX` |
| Cloud cover | above 60 % this hour or next (an optical-astronomy cut, kept on purpose: the user confirmed it on 2026-10-08; `VLBA_NWS_CLOUD_MAX=-1` switches it off) | `NWS_CLOUD_MAX` |
| Snow or ice | snowfall or ice accumulation above zero this hour or next (NWS gives amounts as totals over periods of often 6 h; the reason names the period), or snow / sleet / freezing rain or drizzle, or icing (freezing fog or spray, ice fog, ice crystals, blowing snow), forecast with a coverage of likely or more (likely, definite, numerous, widespread, occasional, frequent, periods, areas, intermittent). San Juan's grid has no snowfall or ice series: St. Croix's check rests on the weather entries | added: "include snowstorms in bad" |
| Precipitation observed | the station reports precipitation now (present weather, the text, or precipitation in the last hour; fog, mist, haze, squalls and snow drifting along the ground are no precipitation, nor is anything "in the vicinity"). The report it reads: the station's latest report when it says anything about the weather (a description, present weather, clouds, a precipitation amount, or a raw METAR, whose weather groups are read when NWS did not decode them) and is at most 90 min old; otherwise the newest report with weather of the last 90 min, from the station's recent reports or the latest and recent reports of the next three qualifying stations, the nearer station on a tie (Brewster: CJDW has no present-weather sensor, KOMK's report answers; a METAR station's blank record: St. Croix's TISX leaves about a third of its records blank). A qualifying station's report without a temperature (NWS's quality control drops it now and then, at St. Croix's TISX for one) counts like any other: the verdict never reads the temperature. Another station's report moves only this check: the wind and humidity stay the station's. "No precipitation" in a report is no answer while a station's recent reports, maybe newer, could not be read (rain in it still counts). A METAR whose present-weather sensor is out of service (PWINO) says nothing unless it shows precipitation (mist, haze or a thunderstorm in the vicinity do not count). *n/a* only when the station and the next qualifying ones checked never report weather (judged by their last 12 reports, at least 3 distinct: Kitt Peak's QSBA3); "no data" in every other case | the WU rain latch |
| Humidity observed | **off** (`VLBA_OBS_HUMIDITY_MAX=-1`): ttustatus's 95 % protects an optical dome from condensation, which does not concern a radio antenna; `VLBA_OBS_HUMIDITY_MAX=95` restores it | `HUMIDITY_UNSAFE_ABOVE` |
| Wind | observed wind or gust, or the forecast wind or gust this hour or next, at or above **45 mph** (`VLBA_WIND_MPH`; 0 switches it off); a report without a wind value is named in the detail, the forecast then decides alone | none: the VLBA antennas stow in high wind, added here |

The observation checks (precipitation, humidity, the observed half of the wind check) use
the nearest station of NWS's list for the antenna's grid cell that lies within 50 km and
1000 m of elevation of the antenna (`VLBA_OBS_MAX_KM`, `VLBA_OBS_MAX_DELEV_M`) and has a
recent report: at Kitt Peak that is QSBA3 (33 km, 835 m lower), because the nearest
station, QSEA3, lies 1213 m below the antenna (QSBA3 reports no weather, so Kitt Peak's
precipitation check is *n/a*); at Brewster, CJDW, whose reports carry no weather either,
so the precipitation check reads KOMK's (39 km). Where no station of the list
qualifies (Mauna Kea at 3763 m, Pie Town with the nearest station about 100 km away) those
checks do not apply:
they read *n/a*, the nearest report is shown marked "not used for the verdict", and the
verdict rests on the forecast, the radar, GLM and the warnings. A qualifying station
without a recent report is "no data" (GOOD?). The
forecast checks read the raw NWS gridpoint for the antenna's own grid cell; NWS offices
update it a few times a day (2 to 6 h apart, measured), so a forecast is "no data" only
when its update is older than 12 h (`VLBA_NWS_STALE_MIN`; ttustatus uses 150 min for a
single office). A malformed gridpoint series is "no data" too (never *n/a*, which is for a
series the office does not produce). A radar frame older than 20 min (a frame of unknown
time counts as old as the cached copy it came from), a failed GLM poll or an observation
older than 3 h are "no data" as well. A source switched off on purpose (`WEATHER_RADAR=0`,
`VLBA_ISLAND_RADAR=0`, `VLBA_GLM=0`) makes its check *off*, not missing.

The holds (radar 15 min, lightning 30 min), the radar streak, the last radar frame counted,
the newest GLM granule examined, the GLM granules to fetch again and those examined are kept in
`<cache>/vlba_state.json`, rewritten
atomically once per run; a hold stamp from the future (a clock step, a hand edit) is
clamped, so a hold can never outlast one period. A state file that cannot be written is a
run error (status.json: degraded), because without it the radar confirmation and the holds
do not carry over between runs.

## Data sources (free, no keys)

| What | Source |
|---|---|
| Point metadata, observations, 7-day and hourly forecasts, alerts and zone outlines | api.weather.gov, through `weather.nws` / `weather.alerts` |
| Raw gridpoint forecast (cloud cover, thunder probability, snowfall, ice, gusts, weather types) | `https://api.weather.gov/gridpoints/{office}/{x},{y}` (`vlba/gridpoint.py`) |
| Radar, contiguous US | NOAA/NSSL MRMS Seamless Hybrid Scan Reflectivity (the lowest unblocked tilt; IEM's `lcref` frames, which IEM labels "lowest elevation composite reflectivity", match MRMS's `SeamlessHSR` cell for cell) via the Iowa Environmental Mesonet (`weather.radar`); the ring check reads the palette indices of the same frame. These frames write "no radar coverage" like "no echo"; the radar coverage (below) tells them apart |
| Radar, Hawaii and the Virgin Islands | the NWS radar composites of radar.weather.gov on `https://opengeo.ncep.noaa.gov/geoserver/{hawaii,carib}/ows` (WMS, layers `hawaii_bref_qcd` and `carib_bref_qcd`, MRMS-based, a frame every 2 min). The server only serves coloured pixels (no values, no custom styles, WCS gives the same four bands), so `vlba/nwsradar.py` decodes the colours back to dBZ with the RIDGE II colour scale (`PALETTE`: the stops were derived by matching the MRMS source grids pixel for pixel with the served images, since no table is published; the nearest point on the colour ramp) and re-colours the result in the parent's dBZ palette, so every map reads the same. The frame time comes from the workspace's GetCapabilities. Pixels of unknown colour are left empty and counted in the log |
| Radar coverage | MRMS's own GRIB2 files of the frames' times on NOAA Open Data (`noaa-mrms-pds` on AWS): `CONUS/SeamlessHSR_00.00` (about 1.3 MB, every 2 min) and `HAWAII` / `CARIB` `MergedBaseReflectivityQC_00.50` (20-60 KB), the products behind the frames, read with Pillow (`vlba/mrms.py`): -999 marks a cell no radar covers, -99 a covered cell without echo. A file missing at the frame's time is replaced by the nearest one within 5 min |
| Lightning | GOES-East (GOES-19, 75.2° W) and GOES-West (GOES-18, 137.0° W) `GLM-L2-LCFA` granules on AWS Open Data (`noaa-goes19`, `noaa-goes18`; 20-s granules of ~200 KB, parsed in RAM with numpy + h5py, or netCDF4). Each antenna uses the satellites within 70° of arc of its sub-satellite point: Mauna Kea GOES-West only, St. Croix and Hancock GOES-East only, the others both (a flash both detect counts once: same 1 s, within 20 km). Each run fetches the last 5 minutes plus 1 minute of overlap, because a granule is published 10-30 s after it ends and would otherwise fall between two runs: about 7 MB per run with both satellites. `VLBA_GLM_STRIDE=2` halves that, but then half of each interval goes unexamined, as it does with `VLBA_GLM_WINDOW_MIN` below the 5-minute cron interval |
| Sun | `weather.sun` (NOAA solar equations); "sun up" means the altitude is above −0.833° (the upper limb is visible) |
| Basemap | OpenStreetMap standard tiles, fetched once per site and theme and cached for good |

Sites: `vlba/sites.py`, the NRAO VLBA Observational Status Summary table (WGS84), with each
antenna's elevation and time zone (Kitt Peak has no daylight saving time; St. Croix is AST
all year). `--sites`, `WEATHER_SITES` and `WEATHER_SITES_FILE` work as in the parent (a
subset for tests, or another array); without them the ten antennas are used.

## Layout

```
make_vlba_page.py        entry point: one run (lock, budget, alerts, MRMS frame, GLM, per
                         antenna: NWS products, gridpoint, station, sun, radar ring, GLM
                         view, verdict, maps; state file; index.html + status.json)
vlba/
  config.py              VlbaConfig: the parent's WEATHER_* settings (some defaults changed)
                         plus every VLBA_* setting, validated at startup
  sites.py               the ten antennas
  gridpoint.py           the raw NWS gridpoint forecast, expanded to hourly rows
  glm.py                 GOES GLM flashes of the window, per-antenna view
  nwsradar.py            the NWS composites for sites outside the MRMS grid, decoded to dBZ
  mrms.py                MRMS's own GRIB2 files: the radar coverage of each ring
  radarcheck.py          the ring scans (MRMS cells; frame-space index images)
  decision.py            the checks, the verdict, the state file
  maps.py                the parent's map renderer plus rings, flashes and the caption label
  page.py                the page and status.json (the parent's blocks in the VLBA layout)
  tests/                 pytest, network blocked (FakeHttp, from the parent's suite)
weather/                 the parent's library, copied: NWS products and alerts, MRMS frames
                         and maps, sun times, the HTTP cache and budget, the page blocks
  pipeline.py            the parent's make_weather_page.py: the run steps reused here
deploy/
  vlbaweather-cron.sh    cron wrapper (POSIX sh): env files, nice/timeout, syslog or log file
  install-cron.sh        idempotent cron installer (Gentoo/OpenRC or any cron host)
  install.sh             idempotent systemd installer: dirs, SELinux label, unit + timer
  vlbaweather.service    oneshot generator (placeholders filled by install.sh)
  vlbaweather.timer      every 5 min
  apache-vlbaweather.conf  Alias /vlbaweather → output dir, no listings, short Cache-Control
vlbaweather.env.example  every WEATHER_* and VLBA_* variable with its default
run_local.sh             local preview: regenerate every 5 min and serve on port 8080
PLAN.md                  the design and the decisions
.github/workflows/ci.yml lint, tests and shell syntax on Python 3.9 and 3.12
```

`weather/` is the parent's library at its commit c3a13b4 with two hooks added for this
service in `weather.radar.SiteMap`: the caption label (`radar["label"]`, default "MRMS") and
the scale bars (`SCALE_BARS`, default 25 mi and 10 mi; the antennas' maps draw 10 mi and
10 km). All of its modules are needed (the page and the maps import the road layer too,
which stays off here). To take in a later version, copy the parent's `weather/*.py` and its
`make_weather_page.py` (as `weather/pipeline.py`) over these, keep the two hooks, and run the
tests; the parent's own tests of its library stay in its repository.

## Run locally

```bash
git clone https://github.com/kirxkirx/vlbaweather.git
cd vlbaweather
python3 make_vlba_page.py --out ./out --cache ./.cache-dev --verbose   # one run
python3 -m http.server -d out 8000 --bind 127.0.0.1                    # then open http://localhost:8000/
./run_local.sh                                                         # or: a 5-min loop + server on 8080
```

The default User-Agent names the project only. To add a contact, set `WEATHER_USER_AGENT`
to `vlbaweather (+https://github.com/kirxkirx/vlbaweather; ` + a real address + `)`: the
generator refuses placeholder contacts such as `you@example.org`, which OpenStreetMap blocks.
`run_local.sh` listens on every interface; `BIND=127.0.0.1` keeps it on the machine. Needs
Python ≥ 3.9 and Pillow (maps), and
numpy with h5py or netCDF4 for the lightning layer; without the latter the page says
lightning is unavailable and everything else works. The first run fetches the basemap tiles
of every antenna and theme and the NWS station and zone lookups: a few minutes (3.5 min on
2026-10-12), during which `run_local.sh` serves a "no page yet" placeholder that reloads
itself. Later runs take 10–80 s, most of it the GLM granules. Options: `--skip-radar`, `--skip-glm`, `--sites 'slug|Name|lat|lon[|tz];…'`.

## Tests

```bash
python3 -m pytest vlba/tests -q
python3 -m flake8 vlba make_vlba_page.py weather          # settings in .flake8
sh -n deploy/vlbaweather-cron.sh; for f in deploy/install-cron.sh deploy/install.sh run_local.sh; do bash -n "$f"; done
```

The suite blocks the network: every fetch goes through `weather.http`, replaced by the
in-memory `FakeHttp` (`vlba/tests/fakehttp.py`); the GLM module's own transport (`vlba.glm._get`) is
monkeypatched. It covers the configuration, the site table, the gridpoint expansion, GLM
(synthetic HDF5 granules written with h5py; skipped without it), the island radar (synthetic
composites in the scale's colours, the time from a capabilities snippet, the stale
fallback), the ring scans, the radar coverage (synthetic MRMS GRIB2 files: the reader, the
ring count, the file lookup), every check of the verdict with its holds and streak, the maps,
the page, the whole run with a CONUS and an island antenna and with every source dead, the
CLI exit codes, and the deploy files (the env example against the real defaults, the cron
wrapper against a stub generator, DRY_RUN renders of both installers). CI
(`.github/workflows/ci.yml`) runs the lint, the suite and the shell syntax checks on Python
3.9 and 3.12. The publish check reads private names from the git-ignored file
`.private-patterns` at the top of the checkout (or the file named by `LWA_PRIVATE_PATTERNS`);
the installers' shared functions are compared with the parent's when `LWA_CHECKOUT` names a
checkout of it.

## Deploy on tau.kirx.net (Gentoo/OpenRC, cron, Apache)

From its own checkout, beside the parent's page, run as `apache` like the parent. The
checkout belongs to the admin account (`kirx`), which also runs every later `git pull`;
`apache` only reads it, so it must be able to enter the home directory: if `ls -ld
/home/kirx` shows `drwx------`, `chmod 711 /home/kirx` (the parent's deployment already
needs this). As kirx:

```bash
git clone https://github.com/kirxkirx/vlbaweather.git ~/vlbaweather
```

The rest in a root shell:

```bash
# packages: Pillow for the maps, numpy and h5py for the GLM lightning layer
emerge --ask --noreplace dev-python/pillow dev-python/numpy dev-python/h5py

# configuration: its own env file (the parent's /etc/local-weather-awareness.env is not read)
cat > /etc/vlbaweather.env <<'EOT'
WEATHER_OUT_DIR=/var/www/localhost/htdocs/vlbaweather
WEATHER_PAGE_URL=https://tau.kirx.net/vlbaweather/
WEATHER_USER_AGENT=vlbaweather (+https://github.com/kirxkirx/vlbaweather; you@example.org)
EOT
chmod 0644 /etc/vlbaweather.env     # and put YOUR REAL e-mail address in the User-Agent

# the cron job (every 5 min), the directories, the Apache snippet, one first run
RUN_USER=apache /home/kirx/vlbaweather/deploy/install-cron.sh
# preview only: DRY_RUN=1 RUN_USER=apache /home/kirx/vlbaweather/deploy/install-cron.sh

# Apache
cp /etc/vlbaweather/apache-vlbaweather.conf /etc/apache2/vhosts.d/vlbaweather.conf
/etc/init.d/apache2 configtest && rc-service apache2 reload
```

The crontab line is `*/5 * * * * <checkout>/deploy/vlbaweather-cron.sh # vlbaweather`; the
installer leaves every other line alone, the parent's `# local-weather-awareness` line
included. Logs go to syslog with the tag `vlbaweather`. Cache: `~apache/.cache/vlbaweather`.
The installer warns when numpy / h5py are missing for the run user's Python (the page then
says lightning is unavailable). `git pull` in the checkout (as kirx) updates the service;
rerun the installer when files under `deploy/` changed. On a systemd host,
`deploy/install.sh` installs the service and timer instead.

Packages: Pillow must be built for the Python version the run user's `python3` runs. On
Gentoo, the `PYTHON_TARGETS` of `dev-python/pillow` must include that version (or set
`WEATHER_PYTHON=python3.X` in `/etc/vlbaweather.env` to a version Pillow is built for), with
`USE="truetype zlib"` for the map labels and PNG output, and a TrueType font such as
`media-fonts/dejavu`. Debian/Ubuntu: `apt install python3-pil python3-numpy python3-h5py
fonts-dejavu-core`. RHEL, Alma, Rocky: `dnf install python3-pillow python3-numpy python3-h5py
dejavu-sans-fonts` (python3-pillow comes from EPEL).

Every setting is documented in `vlbaweather.env.example`; the generic ones keep their
`WEATHER_*` names (the generator is built on the parent's library), the VLBA page's own are `VLBA_*`.
Network per run at the defaults: a few dozen small NWS requests (most cached for 30 min), one
MRMS frame (~0.6 MB) and MRMS's own file of it for the radar coverage (~1.3 MB), two
capabilities documents, two composites and two small MRMS files for the islands, and the GLM
granules (~7 MB); about 2.6 GB a day, most of it GLM. `VLBA_GLM_STRIDE=2` or a
shorter `VLBA_GLM_WINDOW_MIN` cut that down, at the price of unexamined minutes.

## Credits and disclaimer

Map data © OpenStreetMap contributors. Radar: NOAA/NSSL MRMS via the Iowa Environmental
Mesonet, and NWS radar composites from radar.weather.gov. Lightning: NOAA GOES-East and
GOES-West Geostationary Lightning Mapper via the NOAA Open Data Dissemination program on
AWS. Forecasts and alerts: NOAA/NWS. Antenna positions: NRAO. The verdict logic follows the
ttustatus SafetyMonitor.

Not an official product: this page is not from, or endorsed by, NOAA, the National Weather
Service, NRAO or any other agency, and it must not be the only source for safety decisions.
It can be late, incomplete or wrong whenever a source is. For official forecasts, watches
and warnings use weather.gov.

## License

No license has been chosen for this code yet. Until the owner adds a `LICENSE` file, the
usual copyright rules apply: you may read the code here, but no permission to copy, modify
or redistribute it is granted. The data shown on the page are not covered by this: they come
from public sources under their own terms (see the credits above).
