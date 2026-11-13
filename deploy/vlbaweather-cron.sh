#!/bin/sh
# vlbaweather cron wrapper: ONE run of the VLBA page generator, for hosts without systemd
# (such as Gentoo with OpenRC) or any host with a cron daemon. The installer
# deploy/install-cron.sh puts this line into the run user's crontab:
#
#   */30 * * * * <checkout>/deploy/vlbaweather-cron.sh # vlbaweather
#
# vlbaweather is a checkout of its own and runs beside local-weather-awareness's cron job
# when a host runs both: its own env files, cron line, output and cache directories and
# syslog tag, so the two never interfere.
#
# One call:
#   1. changes to the checkout: the directory above this script (symlinks resolved). The
#      generator puts its own directory, which holds the weather package it imports, on
#      sys.path itself;
#   2. loads /etc/vlbaweather.env, then ~/vlbaweather.env (a later file wins). The files
#      are PARSED, never sourced or eval'd: only lines KEY=value whose KEY matches
#      ^[A-Z_][A-Z0-9_]*$ count, and the value is exported literally, so "$(...)",
#      backquotes, ";" or "$VAR" in a value stay plain text. Blank lines, "#" comments and
#      anything else (e.g. "export X=1") are skipped. Blanks around the "=" and at both
#      ends of the value, a trailing CR and one pair of surrounding quotes are removed;
#      deploy/install-cron.sh and deploy/install.sh read the files the same way (env_lookup).
#      An empty value is ignored: it does not clear an earlier file's value. The wrapper's
#      own variables are lower-case, so no KEY in a file can change them.
#      local-weather-awareness's /etc/local-weather-awareness.env is NOT read: the two
#      services are configured apart. A file that is missing is skipped silently; one
#      that exists but cannot be read stops the run (exit 2, logged); one that is not
#      owned by root or the run user, or that it or its directory is writable by group or
#      others, is skipped with a logged line (its writers could set LD_PRELOAD or PATH);
#   3. runs   timeout 600 nice -n 10 python3 make_vlba_page.py   with umask 022 (the
#      output must be readable by Apache); without timeout(1) or nice(1) it runs without;
#   4. logs the generator's output (stdout and stderr) to syslog with "logger -t vlbaweather"
#      when logger(1) exists, /dev/log is a socket and WEATHER_LOG is unset; otherwise
#      appends it to WEATHER_LOG (default ${WEATHER_CACHE_DIR:-~/.cache/vlbaweather}/cron.log),
#      which is renamed to <log>.1 before a run once it is larger than 1 MB (one old copy
#      is kept);
#   5. exits with the generator's status (124 = stopped by the timeout, 127 = no python;
#      2 = an env file it cannot read).
#
# The generator takes its own lock (<cache>/run.lock): a run that starts while the previous
# one is still busy exits 0 at once, unless that one has held the lock for 30 min or more
# (then 1: it hangs), so no flock(1) is needed here. The generator exits 0 even when data
# sources fail; non-zero means a configuration error (2), or (1) a lock held too long or
# unusable, an unwritable output/cache directory or an unexpected failure, which cron
# cannot fix by retrying.
#
# Variables (set them in an env file; the env files win over the environment):
#   WEATHER_LOG        unset = syslog as above; a file path = append there (with the 1 MB
#                      rotation); "-" = write to stdout (for a run by hand)
#   WEATHER_PYTHON     interpreter, looked up in PATH (default python3; cron's PATH is
#                      usually /usr/bin:/bin). E.g. python3.12 when Pillow, numpy and h5py
#                      are only built for that version.
#   WEATHER_GENERATOR  script to run, relative to the vlbaweather directory (default
#                      make_vlba_page.py)
#   WEATHER_TIMEOUT    seconds before timeout(1) stops a wedged run (default 600; 0 = none;
#                      above 1800, the cron interval, or not a number: 600 and a log line)
# Everything else (WEATHER_OUT_DIR, WEATHER_CACHE_DIR, WEATHER_USER_AGENT, the VLBA_*
# settings, ...) is read by the generator itself; see vlbaweather.env.example.
# Environment only (tests): ENV_FILES = space-separated env files to load instead of
# "/etc/vlbaweather.env $HOME/vlbaweather.env"; SYSLOG_SOCKET (default /dev/log).

umask 022

# Lower-case names throughout: an env file can only set KEY names matching [A-Z_][A-Z0-9_]*,
# so it can never change these (an "UPPER=abc" line once broke the key check below).
log_max_bytes=1048576
# Explicit character lists: [A-Z] ranges depend on the locale in some shells.
upper=ABCDEFGHIJKLMNOPQRSTUVWXYZ
digits=0123456789
cr=$(printf '\r')

say() {  # say TEXT: a line from this wrapper, timestamped like the generator's own lines
    printf '%s vlbaweather-cron: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

if [ "$(id -u)" = 0 ]; then
    say "refusing to run as root (files in the cache would become root-owned);" \
        "run it from the crontab of the unprivileged run user" >&2
    exit 1
fi

# ---- 1. the vlbaweather directory ---------------------------------------------------------
self=$0
if command -v readlink >/dev/null 2>&1; then
    resolved=$(readlink -f "$0" 2>/dev/null) && [ -n "$resolved" ] && self=$resolved
fi
repo=$(cd -P "$(dirname "$self")/.." && pwd -P) || {
    say "cannot find the vlbaweather directory from $0" >&2
    exit 1
}
cd "$repo" || exit 1

# ---- 2. env files: parsed, exported literally ---------------------------------------------
# What happened to the env files is logged once the log is set up (an env file may say
# where it goes): "notes" collects those lines; env_error stops the run (exit 2).
notes=""
env_error=""
note() {  # note TEXT: one line for the log, emitted after the log is set up
    notes="$notes$*
"
}
me=$(id -u)
safe_path() {  # safe_path PATH: owned by root or by this user, writable by nobody else
    # ls -n is POSIX (stat(1) is not); -L judges what a link points to
    ls_out=$(LC_ALL=C ls -ldnL -- "$1" 2>/dev/null) || return 1
    set -f
    # shellcheck disable=SC2086  # split into fields on purpose
    set -- $ls_out
    set +f
    [ "$3" = 0 ] || [ "$3" = "$me" ] || return 1
    case $1 in
        ?????w* | ????????w*) return 1 ;;          # group- or other-writable
    esac
    return 0
}
env_file_trusted() {  # env_file_trusted FILE: the file, what it links to, and their directories
    target=$1
    if command -v readlink >/dev/null 2>&1; then
        target=$(readlink -f -- "$1" 2>/dev/null) && [ -n "$target" ] || target=$1
    fi
    for p in "$1" "$(dirname -- "$1")" "$target" "$(dirname -- "$target")"; do
        safe_path "$p" || return 1
    done
    return 0
}
load_env_file() {  # load_env_file FILE
    [ -e "$1" ] || return 0                     # not there: nothing to load, no message
    if [ ! -f "$1" ] || [ ! -r "$1" ]; then
        # exists but cannot be read: running on defaults would write the page elsewhere
        # (WEATHER_OUT_DIR is in it) with the default limits, and nobody would notice
        note "cannot read the env file $1 (not a readable file for uid $me); not running" \
             "the generator on defaults. Fix: chgrp <run user's group> $1 && chmod 0640 $1"
        env_error=1
        return 0
    fi
    if ! env_file_trusted "$1"; then
        # whoever else can write it could set LD_PRELOAD, PATH or WEATHER_PYTHON for this run
        note "skipped the env file $1: it, its target or its directory is not owned by root or" \
             "uid $me, or is writable by group or others"
        return 0
    fi
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%"$cr"}
        line=${line#"${line%%[![:space:]]*}"}           # leading blanks
        case $line in
            '' | '#'*) continue ;;
            *=*) ;;
            *) continue ;;
        esac
        key=${line%%=*}
        key=${key%"${key##*[![:space:]]}"}               # blanks before the "="
        case $key in
            '' | [!${upper}_]* | *[!${upper}${digits}_]*) continue ;;
            # names the shell itself owns: never taken from a file
            IFS | PWD | OLDPWD | PPID | UID | EUID | ENV | BASH_ENV | SHELLOPTS | BASHOPTS | CDPATH | OPTIND | OPTARG | LINENO) continue ;;
        esac
        val=${line#*=}
        val=${val#"${val%%[![:space:]]*}"}              # blanks after the "="
        val=${val%"${val##*[![:space:]]}"}              # trailing blanks
        case $val in
            \"*\") val=${val#\"}; val=${val%\"} ;;
            \'*\') val=${val#\'}; val=${val%\'} ;;
        esac
        [ -n "$val" ] || continue
        # `command` keeps a refused assignment (e.g. a readonly name) from ending the script
        command export "$key=$val" 2>/dev/null || true
    done < "$1"
}

if [ "${ENV_FILES+set}" = set ]; then
    env_files=$ENV_FILES
else
    env_files=/etc/vlbaweather.env
    [ -n "${HOME:-}" ] && env_files="$env_files $HOME/vlbaweather.env"
fi
set -f                                   # split the list on blanks, but never glob it
for f in $env_files; do
    load_env_file "$f"
done
set +f

# ---- 4. where the output goes -------------------------------------------------------------
expand_tilde() {  # expand_tilde PATH: a leading ~ or ~/ becomes $HOME (as config.py does)
    # shellcheck disable=SC2088  # the quoted ~ is the literal text being matched
    case $1 in
        "~") printf '%s\n' "${HOME:-}" ;;
        "~/"*) printf '%s/%s\n' "${HOME:-}" "${1#"~/"}" ;;
        *) printf '%s\n' "$1" ;;
    esac
}

mode=tofile
log=${WEATHER_LOG:-}
if [ "$log" = "-" ]; then
    mode=stdout
elif [ -n "$log" ]; then
    log=$(expand_tilde "$log")
elif command -v logger >/dev/null 2>&1 && [ -S "${SYSLOG_SOCKET:-/dev/log}" ]; then
    mode=syslog
else
    log="$(expand_tilde "${WEATHER_CACHE_DIR:-${HOME:-}/.cache/vlbaweather}")/cron.log"
fi

if [ "$mode" = tofile ]; then
    mkdir -p "$(dirname "$log")" 2>/dev/null
    if [ -f "$log" ]; then
        size=$(wc -c < "$log" 2>/dev/null | tr -d ' \t')
        case $size in '' | *[!0-9]*) size=0 ;; esac
        if [ "$size" -gt "$log_max_bytes" ]; then
            mv -f "$log" "$log.1" 2>/dev/null
        fi
    fi
    if ! ( : >> "$log" ) 2>/dev/null; then
        say "cannot write the log file $log; writing to stdout instead (cron mails it)" >&2
        mode=stdout
    fi
fi

emit() {  # emit TEXT: a line from this wrapper, to wherever the run's output goes
    case $mode in
        syslog) logger -t vlbaweather "vlbaweather-cron: $*" 2>/dev/null \
                    || say "$*" >&2 ;;
        tofile) say "$*" >> "$log" ;;
        *) say "$*" ;;
    esac
}

# the env-file lines collected above
while IFS= read -r line; do
    [ -n "$line" ] && emit "$line"
done <<EOF_NOTES
$notes
EOF_NOTES
if [ -n "$env_error" ]; then
    [ "$mode" = syslog ] && say "cannot read an env file; see syslog (tag vlbaweather)" >&2
    exit 2
fi

# ---- 3. the command -----------------------------------------------------------------------
py=${WEATHER_PYTHON:-python3}
gen=${WEATHER_GENERATOR:-make_vlba_page.py}
tmo=${WEATHER_TIMEOUT:-600}
case $tmo in
    '' | *[!0-9]*)
        emit "WEATHER_TIMEOUT=$tmo is not a whole number of seconds; using 600"
        tmo=600 ;;
    *)
        # leading zeros off; then at most 1800 s (the cron interval: a run allowed longer
        # only holds the lock against the next one). A longer number would also overflow
        # the shell's arithmetic, and the test below would then drop the timeout silently.
        t=${tmo#"${tmo%%[!0]*}"}
        if [ ${#t} -gt 4 ] || [ "${t:-0}" -gt 1800 ]; then
            emit "WEATHER_TIMEOUT=$tmo is longer than the 1800 s cron interval; using 600"
            tmo=600
        else
            tmo=${t:-0}
        fi ;;
esac
if ! command -v "$py" >/dev/null 2>&1; then
    emit "python interpreter not found: $py (PATH=$PATH); set WEATHER_PYTHON or PATH in" \
         "/etc/vlbaweather.env"
    exit 127
fi

set -- "$py" "$gen"
if command -v nice >/dev/null 2>&1; then
    set -- nice -n 10 "$@"
fi
if [ "$tmo" -gt 0 ] && command -v timeout >/dev/null 2>&1; then
    set -- timeout "$tmo" "$@"
fi

# ---- run it -----------------------------------------------------------------------------
case $mode in
    syslog)
        # POSIX sh has no pipefail: the generator's status travels back on fd 4, while
        # logger's own stdout (normally nothing) goes to the original stdout on fd 3.
        exec 3>&1
        rc=$( { { "$@" 2>&1 3>&- 4>&-; echo "$?" >&4; } \
                | logger -t vlbaweather >&3 3>&- 4>&-; } 4>&1 )
        exec 3>&-
        ;;
    tofile)
        "$@" >> "$log" 2>&1
        rc=$?
        ;;
    *)
        "$@" 2>&1
        rc=$?
        ;;
esac
case $rc in '' | *[!0-9]*) rc=1 ;; esac

if [ "$rc" -eq 124 ] && [ "$1" = timeout ]; then
    emit "generator stopped by the timeout after $tmo s (exit status 124)"
elif [ "$rc" -ne 0 ]; then
    emit "generator exited with status $rc"
fi
exit "$rc"
