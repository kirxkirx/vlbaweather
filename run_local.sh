#!/bin/bash
# Local preview of the VLBA page: regenerate it every INTERVAL seconds in a background loop
# and serve the output directory with Python's built-in HTTP server. Ctrl-C (or
# SIGTERM/SIGHUP) stops both, including a generator run that is in progress. It cd's to the
# checkout itself (the generator imports the weather package copied there). Until the first run
# has written the page (a few minutes with an empty cache), the server shows a placeholder
# page that says so and reloads itself.
# Usage:  ./run_local.sh [port] [out_dir]        (defaults: 8080, ./out)
# Env:    INTERVAL=300        seconds between generator runs (the deployment's 5 minutes)
#         BIND=0.0.0.0        address the preview server listens on (0.0.0.0 = every
#                             interface, so the page can be opened from other machines;
#                             BIND=127.0.0.1 keeps it on this machine)
#         WEATHER_* and VLBA_* variables are passed through to the generator.
# Exit status: the server's own status when it stops by itself (e.g. 1 when the port is
# already in use), 128+signal when the script is interrupted (130 for Ctrl-C).
cd "$(dirname "$0")" || exit 1

PORT="${1:-8080}"
OUT="${2:-./out}"
INTERVAL="${INTERVAL:-300}"
BIND="${BIND:-0.0.0.0}"
export WEATHER_OUT_DIR="$OUT"
export WEATHER_CACHE_DIR="${WEATHER_CACHE_DIR:-./.cache-dev}"
DEV_USER_AGENT="vlbaweather-dev (+https://github.com/kirxkirx/vlbaweather)"
export WEATHER_USER_AGENT="${WEATHER_USER_AGENT:-$DEV_USER_AGENT}"

mkdir -p "$OUT" || exit 1

# The generator writes index.html at the end of a run, and a first run with an empty cache
# takes a few minutes (map tiles, NWS lookups): until then the server showed a bare
# directory listing. A placeholder says so and reloads itself until the generator's page
# replaces it (by rename). noclobber creates it only where there is no index.html yet, so
# an earlier run's page is kept, and atomically (O_EXCL), so it never overwrites one.
STARTED=$(date '+%H:%M:%S %Z')
WAITING=""
if ( set -o noclobber; cat > "$OUT/index.html" ) 2>/dev/null <<EOT
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta http-equiv="refresh" content="10">
<meta name="color-scheme" content="light dark"><title>VLBA weather: no page yet</title></head>
<body style="font: 16px/1.5 sans-serif; max-width: 40em; margin: 3em auto; padding: 0 1em">
<h1>VLBA weather: no page yet</h1>
<p>The local preview started at $STARTED, and its first generator run writes the page.
With an empty cache that run takes a few minutes (map tiles, NWS lookups). This page
reloads every 10 seconds.</p>
<p>If it stays here, the generator's log lines in the terminal running run_local.sh say why.</p>
</body></html>
EOT
then
    WAITING=1
fi

loop() {
    while true; do
        start=$(date +%s)
        python3 ./make_vlba_page.py
        elapsed=$(( $(date +%s) - start ))
        sleep_time=$(( INTERVAL - elapsed ))
        [ "$sleep_time" -gt 0 ] && sleep "$sleep_time"
    done
}

LOOP_PID=""
SERVER_PID=""

# Stop both background jobs. Each runs in its own process group (set -m below), so killing
# the group also stops what the job started: the generator run and the loop's sleep, not
# just the loop's subshell (which would leave a running generator orphaned).
stop_jobs() {
    trap - INT TERM HUP EXIT
    local pid
    for pid in $SERVER_PID $LOOP_PID; do
        kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
    done
    for pid in $SERVER_PID $LOOP_PID; do
        wait "$pid" 2>/dev/null
    done
}
trap 'stop_jobs; exit 130' INT
trap 'stop_jobs; exit 143' TERM
trap 'stop_jobs; exit 129' HUP
trap 'stop_jobs' EXIT

# Job control: every background job gets its own process group (see stop_jobs). It also
# keeps Ctrl-C away from the jobs themselves, so only this script reacts to it, via the trap.
set -m

loop &
LOOP_PID=$!

case "$BIND" in
    0.0.0.0|::) where="http://$(hostname):$PORT/  (also http://localhost:$PORT/)" ;;
    *:*)        where="http://[$BIND]:$PORT/" ;;
    *)          where="http://$BIND:$PORT/" ;;
esac
echo "vlbaweather local preview: $where"
echo "regenerating every ${INTERVAL}s into $OUT; log lines from the generator follow"
if [ -n "$WAITING" ]; then
    echo "until the first run has written the page (a few minutes with an empty cache), it says \"no page yet\""
fi

python3 -m http.server "$PORT" --bind "$BIND" --directory "$OUT" &
SERVER_PID=$!

# `wait` returns as soon as the server exits, or at once when a trapped signal arrives
# (the trap then stops both jobs and exits 128+signal).
wait "$SERVER_PID"
rc=$?
if [ "$rc" -ne 0 ]; then
    echo "run_local.sh: preview server exited with status $rc; stopping the generator loop" >&2
fi
stop_jobs
exit "$rc"
