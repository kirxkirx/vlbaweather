#!/bin/bash
# Install vlbaweather (the VLBA antenna weather page) under systemd: a oneshot service + a
# 30-minute timer that regenerate the static page into OUT_DIR, which Apache serves as
# https://<host>/vlbaweather/.
#
# vlbaweather is a checkout of its own (https://github.com/kirxkirx/vlbaweather; the weather
# package it is built on is copied in) and installs beside local-weather-awareness's units
# when a host runs both: its own env files, units, output and cache directories.
#
# Usage:  sudo ./deploy/install.sh [OUT_DIR] [RUN_USER]         (in the checkout)
#    or:  sudo OUT_DIR=/srv/www/vlbaweather RUN_USER=weather ./deploy/install.sh
#   OUT_DIR   where index.html + PNGs go     (default: WEATHER_OUT_DIR from an existing
#             /etc/vlbaweather.env, else /var/www/html/vlbaweather)
#   CACHE_DIR (environment only) the generator's cache (default: WEATHER_CACHE_DIR from
#             that env file, else ~RUN_USER/.cache/vlbaweather)
#   RUN_USER  unprivileged account that runs the generator (default: the sudo caller, else
#             the owner of this checkout; root is refused)
#   DRY_RUN=1 print what would be done (renders into a temp dir); no root needed.
#
# At run time the env file wins over the unit's own Environment= lines, so an OUT_DIR or
# CACHE_DIR given here that differs from WEATHER_OUT_DIR / WEATHER_CACHE_DIR in it is
# refused (the service would write somewhere this installer never prepared): edit
# the env file instead, or drop the argument.
#
# Idempotent: rerun after a git pull / rsync to pick up unit-file changes, or after editing
# WEATHER_OUT_DIR / WEATHER_CACHE_DIR in an env file. It never touches the Apache config:
# it renders the snippet and tells you where to copy it.
#
# Hosts without systemd (such as Gentoo with OpenRC) use deploy/install-cron.sh, which
# installs a crontab line for deploy/vlbaweather-cron.sh instead. Use one or the other.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"             # the vlbaweather checkout
DRY_RUN="${DRY_RUN:-}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"         # overridable for tests
ETC_DIR="${ETC_DIR:-/etc/vlbaweather}"              # rendered Apache snippet lives here
REQ_OUT_DIR="${1:-${OUT_DIR:-}}"                    # asked for explicitly (argument or env)
REQ_CACHE_DIR="${CACHE_DIR:-}"
SYSTEM_ENV_FILE="/etc/vlbaweather.env"
DEFAULT_OUT_DIR="/var/www/html/vlbaweather"

die() { echo "error: $*" >&2; exit 1; }
run() {  # run CMD...: execute, or just print it in dry-run mode
    if [ -n "$DRY_RUN" ]; then echo "+ $*"; else "$@"; fi
}
printable() {  # printable TEXT: TEXT without control bytes, for printing a value read from an
               # env file or a link (an escape sequence could hide or forge the lines around it)
    printf '%s' "$1" | LC_ALL=C tr -d '\000-\037\177'
}

# The generator imports its copy of the weather package from the checkout itself.
[ -f "$REPO/weather/__init__.py" ] \
    || die "$REPO/weather/__init__.py not found: an incomplete vlbaweather checkout (the
       generator imports the weather package from it). Clone the whole repository and run
       <checkout>/deploy/install.sh from there."

if [ -z "$DRY_RUN" ] && [ "$(id -u)" -ne 0 ]; then
    die "run me with sudo:  sudo $0 [OUT_DIR] [RUN_USER]   (or DRY_RUN=1 $0 to preview)"
fi
if [ -z "$DRY_RUN" ] && ! command -v systemctl >/dev/null 2>&1; then
    die "no systemd here (systemctl not found). On a cron host (e.g. Gentoo with OpenRC) use:
       sudo RUN_USER=<user> $(dirname "$0")/install-cron.sh"
fi
if [ -n "$DRY_RUN" ]; then
    UNIT_DIR="$(mktemp -d)"; ETC_DIR="$UNIT_DIR/vlbaweather"
    echo "DRY RUN: nothing is changed; rendered files go to $UNIT_DIR"
fi

# ---- who runs it ---------------------------------------------------------------------
RUN_USER="${2:-${RUN_USER:-${SUDO_USER:-$(stat -c %U "$REPO")}}}"
[ "$RUN_USER" != "root" ] || die "refusing to run the generator as root; pass RUN_USER=<user>" \
    "(local-weather-awareness's run user does, or e.g. sudo useradd --system --create-home" \
    "--home-dir /var/lib/vlbaweather --shell /usr/sbin/nologin weather)"
id "$RUN_USER" >/dev/null 2>&1 || die "no such user: $RUN_USER"
RUN_GROUP="$(id -gn "$RUN_USER")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
[ -n "$RUN_HOME" ] || die "cannot determine the home directory of $RUN_USER"
# Root runs this script and installs files from the checkout, so whoever can change the
# checkout chooses what root runs at the next rerun. A warning only (a check inside a script
# that the same account can edit proves nothing), but it names the risk and the fix.
checkout_writers() {  # prints the checkout paths the run user or others could change
    local admin p owner mode
    admin="${SUDO_USER:-$(id -un)}"
    for p in "$REPO" "$REPO/deploy" "$REPO"/deploy/*; do
        [ -e "$p" ] || continue
        owner="$(stat -L -c %U -- "$p" 2>/dev/null)" || continue
        mode="$(stat -L -c %a -- "$p" 2>/dev/null)" || continue
        if { [ "$owner" = "$RUN_USER" ] && [ "$owner" != "$admin" ]; } \
                || [ $((0$mode & 022)) -ne 0 ]; then
            # printable: a file name the run user chose must not erase this warning
            printf '           %s (owner %s, mode %s)\n' "$(printable "$p")" "$owner" "$mode"
        fi
    done
}
WRITERS="$(checkout_writers)"
if [ -n "$WRITERS" ]; then
    cat >&2 <<MSG
WARNING: root runs this installer and installs files from the checkout, but $RUN_USER (the
         account that runs the generator) or other accounts can change these:
$WRITERS
         Code running as $RUN_USER could then make the next installer run do anything as
         root. Let root or your own admin account own the checkout, writable by nobody
         else: chown -R root:root '$REPO' && chmod -R go-w '$REPO'
MSG
fi

# ---- paths: argument / environment, else the env files, else the default ------------------
# The unit's EnvironmentFile= list, in systemd's order (later wins); overridable for tests.
# Only the root-owned /etc/vlbaweather.env: the unit has a command that starts as root (the
# "+" line), and systemd applies the environment files to every command of the unit, so a
# file the run user can write (LD_PRELOAD=...) would run its code as root.
ENV_FILES="${ENV_FILES:-$SYSTEM_ENV_FILE}"
if [ -e "$RUN_HOME/vlbaweather.env" ]; then
    cat >&2 <<MSG
WARNING: $RUN_HOME/vlbaweather.env exists, but it is no longer read under systemd: the
         unit reads only $SYSTEM_ENV_FILE. Move its settings to $SYSTEM_ENV_FILE
         (owned by root, mode 0640, group $RUN_GROUP) and delete it.
MSG
fi
env_lookup() {  # env_lookup KEY: last non-empty KEY=value across the env files, read the way
                # the cron wrapper reads them (blanks around '=' and the value, a CR and one
                # pair of surrounding quotes removed)
    local key="$1" val="" f v
    for f in $ENV_FILES; do
        [ -r "$f" ] || continue
        v="$(grep -E "^[[:space:]]*${key}[[:space:]]*=[[:space:]]*[^[:space:]]" "$f" | tail -n 1 \
             | sed -e 's/\r$//' -e "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//" \
                   -e 's/[[:space:]]*$//' -e "s/^\"\(.*\)\"$/\1/" -e "s/^'\(.*\)'$/\1/")" || true
        [ -n "$v" ] && val="$v"
    done
    printf '%s' "$val"
}
env_source() {  # env_source KEY: the env file whose KEY= is the one env_lookup returns
    local key="$1" src="" f
    for f in $ENV_FILES; do
        [ -r "$f" ] || continue
        grep -qE "^[[:space:]]*${key}[[:space:]]*=[[:space:]]*[^[:space:]]" "$f" && src="$f"
    done
    printf '%s' "$src"
}
file_trusted() {  # file_trusted FILE: owned by root or by whoever runs this script, and neither
                  # it nor its directory writable by anybody else (so ~RUN_USER/vlbaweather.env
                  # is NOT trusted when root runs the installer for RUN_USER)
    local f="$1" me d uid mode
    me="$(id -u)"
    for d in "$f" "$(dirname "$f")"; do
        uid="$(stat -L -c %u "$d" 2>/dev/null)" || return 1
        mode="$(stat -L -c %a "$d" 2>/dev/null)" || return 1
        [ "$uid" = 0 ] || [ "$uid" = "$me" ] || return 1
        [ $((0$mode & 022)) -eq 0 ] || return 1          # group- or other-writable
    done
    return 0
}
path_from_trusted_file() {  # path_from_trusted_file KEY: stop when the env file that sets KEY
                            # could be changed by somebody other than the user running this
    local key="$1" src
    src="$(env_source "$key")"
    [ -n "$src" ] || return 0
    file_trusted "$src" && return 0
    die "$src sets $key, but that file or its directory belongs to another user or is
       writable by others (file: owner $(stat -L -c %U "$src" 2>/dev/null || echo ?), mode
       $(stat -L -c %a "$src" 2>/dev/null || echo ?)). This installer runs as $(id -un), creates
       that directory and hands it to $RUN_USER, so it takes the path only from a file nobody
       else can change. Move the $key line to $SYSTEM_ENV_FILE (owned by root, mode 0640,
       group $RUN_GROUP) and rerun."
}
canon() {  # canon PATH: ~ expanded (cache dir only; config.py does the same), no trailing
           # slash, // and /./ collapsed; symlinks are NOT resolved
    local p="$1"
    [ "${2:-}" = "tilde" ] && p="${p/#\~/$RUN_HOME}"
    case "$p" in
        /*) realpath -m -s -- "$p" 2>/dev/null || printf '%s' "${p%/}" ;;
        *)  printf '%s' "${p%/}" ;;
    esac
}
# The paths decide what root creates and hands to RUN_USER: never from a file RUN_USER
# (or anybody but root) can write.
path_from_trusted_file WEATHER_OUT_DIR
path_from_trusted_file WEATHER_CACHE_DIR
ENV_OUT_DIR="$(env_lookup WEATHER_OUT_DIR)"
ENV_CACHE_DIR="$(env_lookup WEATHER_CACHE_DIR)"
[ -z "$ENV_OUT_DIR" ] || ENV_OUT_DIR="$(canon "$ENV_OUT_DIR")"
[ -z "$ENV_CACHE_DIR" ] || ENV_CACHE_DIR="$(canon "$ENV_CACHE_DIR" tilde)"
[ -z "$REQ_OUT_DIR" ] || REQ_OUT_DIR="$(canon "$REQ_OUT_DIR")"
[ -z "$REQ_CACHE_DIR" ] || REQ_CACHE_DIR="$(canon "$REQ_CACHE_DIR" tilde)"

# The env files override the unit's Environment= at run time: a requested path that differs
# would be created, labelled and opened in ReadWritePaths= here, and then never used.
env_conflict() {  # env_conflict NAME KEY REQUESTED FROM_ENV_FILE
    local name="$1" key="$2" req="$3" have="$4" src
    [ -n "$req" ] && [ -n "$have" ] && [ "$req" != "$have" ] || return 0
    src="$(env_source "$key")"
    die "$name=$req was requested, but ${src:-an env file} sets $key=$have.
       The env file wins at run time (EnvironmentFile= overrides the unit's Environment=),
       so the service would write to $have, which this installer would not have created,
       labelled or opened in ReadWritePaths=. Either set $key=$req in ${src:-that file}
       and rerun, or rerun without $name to use $have."
}
env_conflict OUT_DIR WEATHER_OUT_DIR "$REQ_OUT_DIR" "$ENV_OUT_DIR"
env_conflict CACHE_DIR WEATHER_CACHE_DIR "$REQ_CACHE_DIR" "$ENV_CACHE_DIR"

OUT_DIR="${REQ_OUT_DIR:-${ENV_OUT_DIR:-$DEFAULT_OUT_DIR}}"
CACHE_DIR="${REQ_CACHE_DIR:-${ENV_CACHE_DIR:-$RUN_HOME/.cache/vlbaweather}}"
# These paths end up in the unit (where systemd expands % and $), in sed replacements and in
# su command lines: allow only plain path characters.
for p in "$OUT_DIR" "$CACHE_DIR" "$REPO" "$RUN_HOME"; do
    case "$p" in
        /*) ;;
        *) die "path must be absolute: $p" ;;
    esac
    case "$p" in
        *[!A-Za-z0-9._/+@,:=-]*) die "path may only contain letters, digits and . _ / + @ , : = -: $(printable "$p")" ;;
    esac
done
# The output directory is public: Apache serves every file in it. So the cache (cached
# replies, and by default the state file, the lock and the cron log) must not lie in it, nor
# the output directory in the cache, and neither may a state, lock or log file set apart.
inside() {  # inside A B: path A equals B or lies below it
    case "$1/" in "${2%/}"/*) return 0 ;; esac
    return 1
}
OUT_REAL="$(realpath -m -- "$OUT_DIR")"
CACHE_REAL="$(realpath -m -- "$CACHE_DIR")"
if inside "$CACHE_DIR" "$OUT_DIR" || inside "$OUT_DIR" "$CACHE_DIR" \
        || inside "$CACHE_REAL" "$OUT_REAL" || inside "$OUT_REAL" "$CACHE_REAL"; then
    die "the cache directory $CACHE_DIR and the output directory $OUT_DIR overlap.
       Apache serves everything in the output directory, so the cached replies, the state
       file, the lock and the log would be public. Give the cache a directory of its own
       outside it (the default is ~$RUN_USER/.cache/vlbaweather) and rerun."
fi
not_public() {  # not_public KEY: stop when the file KEY names in the env files is in the output dir
    local key="$1" f
    f="$(env_lookup "$key")"
    case "$f" in "" | -) return 0 ;; esac
    f="$(canon "$f" tilde)"
    case "$f" in /*) ;; *) f="$REPO/$f" ;; esac       # relative: to the checkout, the run's cwd
    if inside "$f" "$OUT_DIR" || inside "$(realpath -m -- "$f")" "$OUT_REAL"; then
        die "$key=$(printable "$f") lies in the output directory $OUT_DIR, which Apache
       serves to anyone. Leave $key unset (it then lives in the cache directory) or
       point it outside $OUT_DIR, and rerun."
    fi
}
not_public WEATHER_LOCK_FILE
not_public VLBA_STATE_FILE

echo "repo:      $REPO"
echo "user:      $RUN_USER ($RUN_GROUP), home $RUN_HOME"
echo "output:    $OUT_DIR"
echo "cache:     $CACHE_DIR"
# (inside the output directory they were refused above, see not_public)
lock="$(env_lookup WEATHER_LOCK_FILE)"
case "$lock" in
    ""|"$CACHE_DIR"/*) ;;
    *) echo "WARNING: WEATHER_LOCK_FILE=$(printable "$lock") is outside the cache directory;" \
            "under the unit's sandboxing (ProtectSystem=strict, ProtectHome=read-only, PrivateTmp)" \
            "it may be unwritable or private to each run. Leave it unset (<cache>/run.lock)." >&2 ;;
esac
# the state file carries the radar confirmation and the holds from one run to the next
state="$(env_lookup VLBA_STATE_FILE)"
case "$state" in
    ""|"$CACHE_DIR"/*) ;;
    *) echo "WARNING: VLBA_STATE_FILE=$(printable "$state") is outside the cache directory;" \
            "under the unit's sandboxing it may be unwritable or private to each run, and then" \
            "the radar confirmation and the holds never carry over. Leave it unset" \
            "(<cache>/vlba_state.json)." >&2 ;;
esac

# ---- sanity: python + Pillow (+ numpy/h5py for GLM), as the unit will run them -------------
# Code of the run user (WEATHER_PYTHON may come from its own env file) never gets root's
# terminal: stdin is /dev/null and, where setsid(1) can wait for its child (util-linux), it
# runs in a session of its own, so a process it leaves behind cannot read what root types
# next or forge a password prompt from the controlling terminal. Output passes unchanged.
SETSID=""
if command -v setsid >/dev/null 2>&1 && setsid -w true </dev/null >/dev/null 2>&1; then
    SETSID=1
fi
detached() {  # detached CMD...: CMD with stdin from /dev/null, in a new session when possible
    local pid rc=0
    if [ -z "$SETSID" ]; then "$@" </dev/null; return; fi
    # In its own session Ctrl-C at this terminal no longer reaches CMD: pass it on.
    setsid -w "$@" </dev/null &
    pid=$!
    trap 'kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null; wait "$pid"; exit 130' INT TERM
    wait "$pid" || rc=$?
    trap - INT TERM
    return "$rc"
}
as_run_user() {  # as_run_user CMD: run a shell command as RUN_USER with a bare environment
    # no exec: in "mkdir X && chmod 0755 X && test -w X" all three must run
    local envcmd="cd / && env -i HOME='$RUN_HOME' LOGNAME='$RUN_USER' USER='$RUN_USER' SHELL=/bin/sh PATH=/usr/bin:/bin"
    if [ "$(id -u)" -eq 0 ]; then
        detached su -s /bin/sh "$RUN_USER" -c "$envcmd $1"
    else                          # dry run without root: as this user, with the same env
        detached sh -c "$envcmd $1"
    fi
}
as_run_user "/usr/bin/python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'" 2>/dev/null \
    || die "/usr/bin/python3 >= 3.9 is required (found: $(as_run_user '/usr/bin/python3 --version' 2>&1 || echo none))"
if ! as_run_user "/usr/bin/python3 -c 'import PIL'" 2>/dev/null; then
    echo "WARNING: Pillow is not importable by /usr/bin/python3 as $RUN_USER - the page will be" >&2
    echo "         generated WITHOUT radar maps. Install it:  apt install python3-pil" >&2
    echo "         RHEL/Alma/Rocky: dnf install python3-pillow, which comes from EPEL (see README)" >&2
else
    # The map labels want a TrueType font; without one they fall back to Pillow's default
    # font (fixed ~11 px bitmap before Pillow 10.1). Ask Pillow about the renderer's own list.
    # The weather package is imported from the checkout, as the generator does.
    rc=0
    as_run_user "/usr/bin/python3 -B -c '
import sys
sys.path.insert(0, \"$REPO\")
try:
    from PIL import ImageFont
    from weather.radar import FONT_PATHS
except Exception:
    sys.exit(2)
for p in FONT_PATHS:
    try:
        ImageFont.truetype(p, 12)
    except Exception:
        continue
    sys.exit(0)
sys.exit(1)'" 2>/dev/null || rc=$?
    if [ "$rc" -eq 1 ]; then
        echo "WARNING: no TrueType font for the map labels (DejaVu Sans, Liberation Sans or" >&2
        echo "         FreeSans); they fall back to Pillow's small default font. Install one:" >&2
        echo "         apt install fonts-dejavu-core  |  dnf install dejavu-sans-fonts" >&2
    fi
fi
# The GLM lightning layer (vlba/glm.py) parses the GOES GLM granules with numpy + h5py
# (netCDF4 also works). Without them the page says lightning is unavailable and the rest
# works, so this is a warning, never fatal.
if ! as_run_user "/usr/bin/python3 -c 'import numpy, h5py'" 2>/dev/null \
        && ! as_run_user "/usr/bin/python3 -c 'import numpy, netCDF4'" 2>/dev/null; then
    cat >&2 <<MSG
WARNING: the GLM lightning layer needs python3-numpy and python3-h5py (or python3-netcdf4)
         for /usr/bin/python3 (as $RUN_USER); without them the page says lightning is unavailable.
           Gentoo:  emerge --ask dev-python/numpy dev-python/h5py   (PYTHON_TARGETS as for Pillow)
           Debian/Ubuntu: apt install python3-numpy python3-h5py
           RHEL family:   dnf install python3-numpy python3-h5py
MSG
fi
[ -f "$REPO/make_vlba_page.py" ] || echo "WARNING: $REPO/make_vlba_page.py not found" >&2

# ---- directories ---------------------------------------------------------------------
# prepare_dir and root_safe are the same in deploy/install-cron.sh (a test keeps them identical).
UNSAFE_PARENT=""
root_safe() {  # root_safe DIR: every existing ancestor of DIR is owned by root and writable by
               # nobody else, so no other user can swap a path component under root's feet
    local d="$1" uid mode
    while [ "$d" != / ]; do
        d="$(dirname "$d")"
        [ -e "$d" ] || continue
        uid="$(stat -c %u "$d")" && mode="$(stat -c %a "$d")" || return 1
        if [ "$uid" != 0 ] || [ $((0$mode & 022)) -ne 0 ]; then
            UNSAFE_PARENT="$d (owner $(stat -c %U "$d"), mode $mode)"
            return 1
        fi
    done
    return 0
}
prepare_dir() {  # prepare_dir WHAT DIR DEFAULT: DIR (mode 0755) owned by RUN_USER, safely
    local what="$1" dir="$2" default="$3" real owner p link="" why
    # Root decides on DIR as given (canon resolves no link) and acts on it only when no
    # component is a symbolic link: through a link, root would create or hand over whatever
    # the link points to, and whoever owns the link chooses that (the run user can put one
    # at the default cache path in its home). The resolved path is checked too.
    real="$(realpath -m -- "$dir")"
    case "$real" in
        *[!A-Za-z0-9._/+@,:=-]*)
            die "refusing $what $dir: a symbolic link on the way leads to a path with characters
       other than letters, digits and . _ / + @ , : = - ($(printable "$real"))" ;;
    esac
    for p in "$dir" "$real"; do
        case "$p/" in
            /bin/* | /boot/* | /dev/* | /etc/* | /lib/* | /lib32/* | /lib64/* | /libx32/* | /proc/* | \
            /root/* | /run/* | /sbin/* | /sys/* | /usr/* | /var/db/* | /var/log/* | /var/spool/*)
                die "refusing $what $dir ($real): a system location is never handed to $RUN_USER" ;;
        esac
        case "$p" in
            / | /home | /opt | /srv | /tmp | /var | /var/cache | /var/lib | /var/tmp | /var/www | \
            /var/www/html | /var/www/localhost | /var/www/localhost/htdocs | "$RUN_HOME")
                die "refusing $what $dir: use a directory of its own below it, e.g. $dir/vlbaweather" ;;
        esac
    done
    if [ "$real" != "$dir" ]; then
        p="$dir"                                     # name the outermost link on the way
        while [ "$p" != / ]; do
            [ -L "$p" ] && link="$p"
            p="$(dirname "$p")"
        done
        link="${link:-$dir}"
    fi
    if [ -z "$link" ] && root_safe "$dir"; then
        if [ -e "$dir" ]; then
            [ -d "$dir" ] || die "$what $dir exists and is not a directory"
            owner="$(stat -c %U -- "$dir")"
            # the default is compared as written: a link there must not make its target "the default"
            if [ "$owner" != "$RUN_USER" ] && [ "$dir" != "$(realpath -m -s -- "$default")" ]; then
                die "$what $dir already exists and belongs to $owner. This installer takes over
       only the default directory ($default) or one that already belongs to $RUN_USER.
       If $dir is meant for vlbaweather, hand it over yourself and rerun:
         chown $RUN_USER:$RUN_GROUP '$dir' && chmod 0755 '$dir'"
            fi
        fi
        run install -d -m 0755 -o "$RUN_USER" -g "$RUN_GROUP" "$dir"
    else
        # inside space that RUN_USER (or another user) controls, e.g. ~RUN_USER/.cache, or
        # reached through a link: RUN_USER creates it, so no root action ever follows a path
        # somebody else can change
        if [ -n "$link" ]; then
            why="$link is a symbolic link (to $(printable "$(readlink -- "$link" 2>/dev/null || echo '?')")),
       and root never creates or hands over a directory through a link. Remove the link, or,
       if $real is meant for vlbaweather, give that path instead of $dir
       (the argument, or the setting in $SYSTEM_ENV_FILE) and rerun."
        else
            why="Its parent $UNSAFE_PARENT is not owned by root alone, so this
       installer will not create the directory as root. Create it yourself, hand it to
       $RUN_USER, and rerun the installer:
         mkdir -p '$dir' && chown $RUN_USER:$RUN_GROUP '$dir' && chmod 0755 '$dir'"
        fi
        if [ -n "$DRY_RUN" ]; then
            echo "+ su -s /bin/sh $RUN_USER -c \"mkdir -p '$dir' && chmod 0755 '$dir' && test -w '$dir'\""
        elif ! as_run_user "mkdir -p '$dir' && chmod 0755 '$dir' && test -w '$dir'" 2>/dev/null; then
            die "$RUN_USER cannot create or write $what $dir.
       $why"
        fi
    fi
    echo "$(printf '%-10s' "$what") $dir owned by $RUN_USER"
}
prepare_dir "output dir" "$OUT_DIR" "$DEFAULT_OUT_DIR"
prepare_dir "cache dir" "$CACHE_DIR" "$RUN_HOME/.cache/vlbaweather"

# ---- SELinux: Apache may only read httpd_sys_content_t --------------------------------
selinux_label() {
    local mode spec
    command -v getenforce >/dev/null 2>&1 || { echo "SELinux: getenforce not found, nothing to label"; return; }
    mode="$(getenforce 2>/dev/null || echo Disabled)"
    if [ "$mode" = "Disabled" ]; then echo "SELinux: disabled, nothing to label"; return; fi
    spec="${OUT_DIR}(/.*)?"
    if command -v semanage >/dev/null 2>&1 && command -v restorecon >/dev/null 2>&1; then
        if command -v matchpathcon >/dev/null 2>&1 \
                && matchpathcon -n "$OUT_DIR" 2>/dev/null | grep -q httpd_sys_content_t; then
            echo "SELinux ($mode): policy already labels $OUT_DIR httpd_sys_content_t (no fcontext rule needed)"
        elif [ -z "$DRY_RUN" ] && semanage fcontext -l 2>/dev/null | awk -v s="$spec" '$1 == s {f=1} END {exit !f}'; then
            echo "SELinux ($mode): fcontext rule for $spec already present"
        else
            run semanage fcontext -a -t httpd_sys_content_t "$spec"
            echo "SELinux ($mode): added persistent fcontext rule httpd_sys_content_t for $spec"
        fi
        run restorecon -R "$OUT_DIR"
        echo "SELinux ($mode): restorecon -R $OUT_DIR"
    else
        run chcon -R -t httpd_sys_content_t "$OUT_DIR"
        echo "SELinux ($mode): chcon -R -t httpd_sys_content_t $OUT_DIR"
        echo "         (semanage not installed, so this label does not survive a relabel;" \
             "dnf install policycoreutils-python-utils for a permanent rule)"
    fi
}
selinux_label

# ---- render the templates ------------------------------------------------------------
sed_escape() { printf '%s' "$1" | sed -e 's/[|&\\]/\\&/g'; }
render() {  # render SRC DEST MODE: fill placeholders, install only if changed
    local src="$1" dest="$2" mode="$3" tmp
    tmp="$(mktemp)"
    sed -e "s|__REPO__|$(sed_escape "$REPO")|g" \
        -e "s|__USER__|$(sed_escape "$RUN_USER")|g" \
        -e "s|__GROUP__|$(sed_escape "$RUN_GROUP")|g" \
        -e "s|__HOME__|$(sed_escape "$RUN_HOME")|g" \
        -e "s|__OUT_DIR__|$(sed_escape "$OUT_DIR")|g" \
        -e "s|__CACHE_DIR__|$(sed_escape "$CACHE_DIR")|g" \
        "$src" > "$tmp"
    if grep -v '^[[:space:]]*#' "$tmp" | grep -q '__[A-Z_]*__'; then
        left="$(grep -v '^[[:space:]]*#' "$tmp" | grep -o '__[A-Z_]*__' | sort -u | tr '\n' ' ')"
        rm -f "$tmp"; die "unrendered placeholder in $src: $left"
    fi
    if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then
        echo "unchanged  $dest"
    else
        install -D -m "$mode" "$tmp" "$dest"
        echo "installed  $dest"
    fi
    rm -f "$tmp"
}
render "$REPO/deploy/vlbaweather.service" "$UNIT_DIR/vlbaweather.service" 0644
render "$REPO/deploy/vlbaweather.timer" "$UNIT_DIR/vlbaweather.timer" 0644
render "$REPO/deploy/apache-vlbaweather.conf" "$ETC_DIR/apache-vlbaweather.conf" 0644

# ---- systemd -------------------------------------------------------------------------
run systemctl daemon-reload
# The timer first: on a machine booted more than 2 min ago OnBootSec= elapses at once and
# triggers the service; the explicit start below then just joins that run (one run, not
# two) and blocks until it finishes so the first result is visible here.
run systemctl enable --now vlbaweather.timer
echo "running the generator once (fetches basemap tiles for all twelve sites: allow a few minutes)"
if ! run systemctl start vlbaweather.service; then
    echo "WARNING: the first run failed - see: journalctl -u vlbaweather -n 50" >&2
fi
if [ -z "$DRY_RUN" ]; then
    systemctl --no-pager --lines=0 status vlbaweather.service || true
    systemctl --no-pager list-timers 'vlbaweather*' || true
fi

# ---- next steps ----------------------------------------------------------------------
snippet="$ETC_DIR/apache-vlbaweather.conf"
cat <<MSG

Done. The timer regenerates $OUT_DIR every 30 minutes.
  timers:   systemctl list-timers 'vlbaweather*'
  logs:     journalctl -u vlbaweather -n 50
            (follow: journalctl -u vlbaweather -f)
  run now:  sudo systemctl start vlbaweather.service
  config:   /etc/vlbaweather.env (the only env file the unit reads)
            (see vlbaweather.env.example; local-weather-awareness's
            /etc/local-weather-awareness.env is not read); rerun this installer after
            changing WEATHER_OUT_DIR / WEATHER_CACHE_DIR.

Apache: rendered snippet at $snippet
MSG
if [ -d /etc/httpd/conf.d ]; then
    cat <<MSG
  sudo cp $snippet /etc/httpd/conf.d/vlbaweather.conf
  sudo apachectl configtest && sudo systemctl reload httpd
MSG
elif [ -d /etc/apache2/conf-available ]; then
    cat <<MSG
  sudo a2enmod headers
  sudo cp $snippet /etc/apache2/conf-available/vlbaweather.conf
  sudo a2enconf vlbaweather && sudo apache2ctl configtest && sudo systemctl reload apache2
MSG
else
    echo "  (no /etc/httpd/conf.d or /etc/apache2 found here - copy it into your Apache conf dir)"
fi
case "$OUT_DIR" in
    /var/www/html/*) echo "  If /var/www/html is the DocumentRoot the Alias is not needed; the snippet still" \
                          "adds -Indexes and the cache headers." ;;
    *) echo "  $OUT_DIR is outside /var/www/html: the Alias in the snippet is required." ;;
esac
# The page's address: WEATHER_PAGE_URL from the env files, else a guess from this host's name.
# Values printed into the commands below come from the env files, which root may be about to
# paste into its shell: they are shown only from a file nobody but root (or whoever runs
# this) can change, and only when they are a plain URL or path.
shown_value() {  # shown_value KEY url|path: KEY from the env files, or nothing (with a note)
    local key="$1" kind="$2" val src re
    val="$(env_lookup "$key")"
    [ -n "$val" ] || return 0
    src="$(env_source "$key")"
    if ! file_trusted "$src"; then
        echo "note: $key from $(printable "$src") is not shown: another user can change that file" >&2
        return 0
    fi
    if [ "$kind" = url ]; then
        re='^https?://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~%/+-]*)?$'
    else
        re='^(-|~?/[A-Za-z0-9._/+@,:=-]*)$'
    fi
    if ! [[ "$val" =~ $re ]]; then
        echo "note: $key=$(printable "$val") is not shown: only plain $kind characters are" >&2
        return 0
    fi
    printf '%s' "$val"
}
PAGE_URL="$(shown_value WEATHER_PAGE_URL url)"
if [ -n "$PAGE_URL" ]; then
    PAGE_BASE="${PAGE_URL%/}"
else
    PAGE_BASE="https://$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo '<your host>')/vlbaweather"
fi
echo "  then:   curl -I $PAGE_BASE/"
echo "  page:   $PAGE_BASE/"
if [ -z "$(env_lookup WEATHER_PAGE_URL)" ]; then
    echo "  (WEATHER_PAGE_URL is not set, so the page footer shows no address: add"
    echo "   WEATHER_PAGE_URL=$PAGE_BASE/ (your public address) to $SYSTEM_ENV_FILE)"
fi
