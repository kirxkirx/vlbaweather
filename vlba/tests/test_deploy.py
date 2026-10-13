"""Consistency checks for the operational files of vlbaweather (deploy/,
vlbaweather.env.example, run_local.sh).

No network, no root: these read the checkout, run the installers only with DRY_RUN=1 and
run the cron wrapper and run_local.sh against a stub generator in a temp dir (the preview
server of run_local.sh is the real one, on 127.0.0.1; make_vlba_page.py itself is never
run). They exist so that a new WEATHER_* / VLBA_* variable or a renamed placeholder
cannot silently drift out of the example env file or the installers, so that the wrapper
never evaluates what an env file contains, and so that the vlbaweather deployment keeps
its own names (env files, cron tag, units, Alias, directories) beside local-weather-awareness's
on a host that runs both. Adapted from local-weather-awareness's weather/tests/test_deploy.py.
"""
from __future__ import annotations

import http.client
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time

import pytest

from vlba.config import VlbaConfig

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))              # the vlbaweather checkout
DEPLOY = os.path.join(ROOT, "deploy")
WRAPPER = os.path.join(DEPLOY, "vlbaweather-cron.sh")
CRON_INSTALLER = os.path.join(DEPLOY, "install-cron.sh")
INSTALLER = os.path.join(DEPLOY, "install.sh")
CRON_LINE = "*/5 * * * * %s # vlbaweather" % WRAPPER
GENTOO_CRON_FIX = ("emerge --ask sys-process/cronie && rc-update add cronie default"
                   " && rc-service cronie start")
GLM_WARNING = "the GLM lightning layer needs python3-numpy and python3-h5py"
IS_ROOT = os.geteuid() == 0
ME = pwd.getpwuid(os.getuid()).pw_name


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


# ---- vlbaweather.env.example vs the two config modules ----------------------------------------
def _road_variables():
    """The library's road-closure variables (the fields roads_*, lsr_*, nmroads_*): the one
    group of WEATHER_* settings the VLBA page leaves out of its example."""
    roads = set(re.findall(r'c\.(?:roads|lsr|nmroads)_[a-z_]+ = _env_[a-z]+\("(WEATHER_[A-Z0-9_]+)"',
                           _read("weather", "config.py")))
    assert {"WEATHER_ROADS", "WEATHER_LSR", "WEATHER_LSR_URL"} <= roads
    return roads


def _vlba_fields():
    """{VLBA_* name: VlbaConfig field} from the ``c.<field> = _env_*("VLBA_...", c.<field>)``
    lines of vlba/config.py (the authoritative list of what the config reads)."""
    pairs = re.findall(r'c\.([a-z_]+) = _env_[a-z]+\("(VLBA_[A-Z0-9_]+)", c\.\1\)',
                       _read("vlba", "config.py"))
    assert len(pairs) >= 30, pairs
    return dict((var, field) for field, var in pairs)


def test_env_example_lists_every_config_variable():
    """Every WEATHER_* weather/config.py reads, except the road-closure ones, and every
    VLBA_* vlba/config.py reads appear in the example (commented or not), and the example
    mentions nothing the two config modules do not read."""
    in_parent = set(re.findall(r'"(WEATHER_[A-Z0-9_]+)"', _read("weather", "config.py")))
    assert in_parent, "no WEATHER_* variables found in weather/config.py"
    wanted = in_parent - _road_variables()
    example = _read("vlbaweather.env.example")
    in_example = set(re.findall(r"^#?\s*(WEATHER_[A-Z0-9_]+)=", example, re.M))
    assert wanted - in_example == set(), "missing from vlbaweather.env.example"
    assert in_example - wanted == set(), "unknown or road-closure variables in the example"

    fields = _vlba_fields()
    quoted = set(re.findall(r'"(VLBA_[A-Z0-9_]+)"', _read("vlba", "config.py")))
    assert quoted == set(fields), "a VLBA_* name is read some other way than c.x = _env_*()"
    in_example_vlba = set(re.findall(r"^#?\s*(VLBA_[A-Z0-9_]+)=", example, re.M))
    assert set(fields) - in_example_vlba == set(), "VLBA_* missing from vlbaweather.env.example"
    assert in_example_vlba - set(fields) == set(), "unknown VLBA_* in vlbaweather.env.example"


def test_env_example_is_valid_environmentfile():
    """Nothing is active: copied to /etc/vlbaweather.env as the header says, the example must
    not override the installer's own choice of output directory. The commented lines are
    plain KEY=value (systemd format: no export, no quotes)."""
    text = _read("vlbaweather.env.example")
    active = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert active == []
    assert "# WEATHER_OUT_DIR=/var/www/localhost/htdocs/vlbaweather" in text
    for m in re.finditer(r"^# ((?:WEATHER|VLBA)_[A-Z0-9_]+=.*)$", text, re.M):
        assert re.match(r"^[A-Z_][A-Z0-9_]*=[^\"']*$", m.group(1)), m.group(1)


# Commented lines in the example that deliberately show a non-default value.
EXAMPLE_NOT_DEFAULT = {"WEATHER_OUT_DIR", "WEATHER_ALERT_COLORS"}


def test_env_example_defaults_match_config(monkeypatch):
    """Every commented ``# WEATHER_X=value`` / ``# VLBA_X=value`` line shows the value
    VlbaConfig actually defaults to: the library's defaults the VLBA page changes (refresh,
    budget, title, cache, User-Agent) as well as its own settings."""
    example = _read("vlbaweather.env.example")
    shown = []
    for m in re.finditer(r"^#\s*((?:WEATHER|VLBA)_[A-Z0-9_]+)=(.*)$", example, re.M):
        shown.append((m.group(1), m.group(2).strip()))
    field_of = dict((v, f) for f, v in re.findall(
        r'c\.([a-z_]+) = [^\n]*?_env_[a-z]+\("(WEATHER_[A-Z0-9_]+)"',
        _read("weather", "config.py")))
    field_of["WEATHER_SITES"] = "sites"         # read through parse_sites(), not c.x = _env_*
    field_of.update(_vlba_fields())
    for k in list(os.environ):
        if k.startswith(("WEATHER_", "VLBA_")):
            monkeypatch.delenv(k)
    default = VlbaConfig.from_env()
    checked = []
    for var, value in shown:
        if var in EXAMPLE_NOT_DEFAULT:
            continue
        assert var in field_of, "%s: no VlbaConfig field reads it" % var
        monkeypatch.setenv(var, value)
        try:
            got = getattr(VlbaConfig.from_env(), field_of[var])
        finally:
            monkeypatch.delenv(var)
        assert got == getattr(default, field_of[var]), "%s=%s is not the default" % (var, value)
        checked.append(var)
    assert {"WEATHER_REFRESH_SECONDS", "WEATHER_RUN_BUDGET", "WEATHER_TITLE", "WEATHER_USER_AGENT",
            "WEATHER_CACHE_DIR", "WEATHER_SITES", "VLBA_VETO_EVENTS", "VLBA_GLM_BUCKETS",
            "VLBA_RADAR_KM", "VLBA_STATE_FILE", "VLBA_NWSRADAR_URL"} <= set(checked)
    # the VLBA page's own defaults, spelled out
    assert "# WEATHER_REFRESH_SECONDS=300" in example and "# WEATHER_RUN_BUDGET=240" in example
    assert "# WEATHER_CACHE_DIR=~/.cache/vlbaweather" in example
    assert "# WEATHER_USER_AGENT=vlbaweather (+https://github.com/kirxkirx/vlbaweather)" in example


def test_env_example_explains_the_two_services(cfg):
    """The header says what a deployer must know: the generic settings keep their WEATHER_*
    names because the generator is built on local-weather-awareness's library, the env files of a host running both are
    separate, the User-Agent needs a real address, road closures are off, and the VLBA_*
    settings come in their three sections."""
    text = _read("vlbaweather.env.example")
    head = text[:4000]
    assert "/etc/vlbaweather.env" in head and "~<run user>/vlbaweather.env" in head
    assert "/etc/local-weather-awareness.env" in head and "SEPARATE" in head
    assert "WEATHER_*" in head and "VLBA_*" in head
    assert "ABSOLUTELY CRUCIAL" in head and "REAL E-MAIL ADDRESS" in head
    assert "you@example.org" in text and "REPLACED BY YOUR REAL E-MAIL ADDRESS" in text
    assert "deploy/vlbaweather-cron.sh" in head
    assert re.search(r"^# .*road closures are off \(WEATHER_ROADS stays off", text, re.M)
    assert not re.search(r"^#?\s*WEATHER_ROADS", text, re.M), "no WEATHER_ROADS line"
    for section in ("# ---- VLBA: the verdict", "# ---- VLBA: GLM lightning",
                    "# ---- VLBA: radar outside the MRMS grid"):
        assert section in text, section
    verdict = text.index("# ---- VLBA: the verdict")
    glm = text.index("# ---- VLBA: GLM lightning")
    island = text.index("# ---- VLBA: radar outside the MRMS grid")
    assert verdict < glm < island
    assert "# VLBA_WIND_MPH=45" in text[verdict:glm] and "# VLBA_GLM_KM=50" in text[glm:island]
    assert "# VLBA_ISLAND_RADAR=1" in text[island:]
    # the fixture's config agrees with what the header claims about the VLBA page
    assert cfg.roads_enabled is False and cfg.refresh_seconds == 300


# ---- systemd units ----------------------------------------------------------------------
def _unit(text):
    """Parse a unit file into {section: {key: [values]}} (comments and blanks ignored)."""
    out, section = {}, None
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if ln.startswith("[") and ln.endswith("]"):
            section = ln[1:-1]
            out.setdefault(section, {})
            continue
        assert section is not None and "=" in ln, "stray line: %r" % ln
        k, v = ln.split("=", 1)
        out[section].setdefault(k.strip(), []).append(v.strip())
    return out


def test_service_unit_shape():
    u = _unit(_read("deploy", "vlbaweather.service"))
    s = u["Service"]
    assert s["Type"] == ["oneshot"]
    assert s["User"] == ["__USER__"] and s["Group"] == ["__GROUP__"]
    assert s["WorkingDirectory"] == ["__REPO__"]
    assert s["ExecStart"] == ["/usr/bin/python3 __REPO__/make_vlba_page.py"]
    assert s["SyslogIdentifier"] == ["vlbaweather"]
    assert s["EnvironmentFile"] == ["-/etc/vlbaweather.env", "-__HOME__/vlbaweather.env"]
    assert s["NoNewPrivileges"] == ["true"] and s["PrivateTmp"] == ["true"]
    assert s["ProtectSystem"] == ["full"]
    rw = " ".join(s["ReadWritePaths"])
    assert "__OUT_DIR__" in rw and "__CACHE_DIR__" in rw
    assert "WEATHER_OUT_DIR=__OUT_DIR__" in s["Environment"]
    assert u["Install"]["WantedBy"] == ["multi-user.target"]
    assert "network-online.target" in u["Unit"]["After"]
    assert "local-weather-awareness" not in " ".join(sum(s.values(), [])), \
        "the unit must not read local-weather-awareness's env files or run its generator"


def test_service_unit_recreates_dirs_and_outlasts_the_budget():
    """A deleted cache/output dir is recreated before the sandboxed run (under
    ProtectHome=read-only the generator cannot) BY THE RUN USER, outside the sandbox but
    never as root, without a failure there blocking the run; and the start timeout
    comfortably exceeds the VLBA page's larger network budget plus rendering."""
    s = _unit(_read("deploy", "vlbaweather.service"))["Service"]
    pre = s["ExecStartPre"]
    assert len(pre) == 1
    words = pre[0].split()
    assert words[0] in ("-+/usr/bin/setpriv", "+-/usr/bin/setpriv")
    assert words[1:] == ["--reuid=__USER__", "--regid=__GROUP__", "--init-groups",
                         "/bin/mkdir", "-p", "__OUT_DIR__", "__CACHE_DIR__"]
    assert "install -d" not in " ".join(pre) and "chown" not in " ".join(pre)
    m = re.match(r"^(\d+)(min|s)$", s["TimeoutStartSec"][0])
    assert m, s["TimeoutStartSec"]
    timeout_s = int(m.group(1)) * (60 if m.group(2) == "min" else 1)
    assert timeout_s >= VlbaConfig().run_budget_s + 180
    assert timeout_s <= 600, "a wedged run must be gone before the following 5-minute ticks pile up"


def test_timer_unit_shape():
    """Every 5 minutes on the wall clock, like the cron line, with the jitter of the
    parent's timer; no placeholders."""
    u = _unit(_read("deploy", "vlbaweather.timer"))
    t = u["Timer"]
    assert t["OnCalendar"] == ["*:0/5"]
    assert t["OnBootSec"] == ["2min"]
    assert "OnUnitActiveSec" not in t
    assert t["RandomizedDelaySec"] == ["15"]
    assert t["AccuracySec"] == ["15s"]
    assert t["Persistent"] == ["true"]
    assert t["Unit"] == ["vlbaweather.service"]
    assert u["Install"]["WantedBy"] == ["timers.target"]
    assert "__" not in _read("deploy", "vlbaweather.timer"), "timer must not need rendering"


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze not installed")
def test_timer_calendar_expression_is_every_five_minutes():
    r = subprocess.run(["systemd-analyze", "calendar", "*:0/5"], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("systemd-analyze calendar unusable here: %s" % r.stderr.strip())
    assert re.search(r"Normalized form:\s*\*-\*-\* \*:00/5:00", r.stdout), r.stdout


# ---- placeholders vs installer ----------------------------------------------------------
def test_installer_renders_every_placeholder():
    installer = _read("deploy", "install.sh")
    rendered = set(re.findall(r"s\|(__[A-Z_]+__)\|", installer))
    used = set()
    for name in ("vlbaweather.service", "vlbaweather.timer", "apache-vlbaweather.conf"):
        used |= set(re.findall(r"__[A-Z_]+__", _read("deploy", name)))
    assert used - rendered == set(), "placeholders the installer does not render"
    assert {"__REPO__", "__USER__", "__OUT_DIR__"} <= rendered


def test_installer_syntax_and_flags():
    installer = _read("deploy", "install.sh")
    assert installer.startswith("#!/bin/bash")
    assert "set -euo pipefail" in installer
    assert os.access(INSTALLER, os.X_OK), "install.sh must be executable"
    r = subprocess.run(["bash", "-n", INSTALLER], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not os.path.exists("/bin/bash") and not os.path.exists("/usr/bin/bash"),
                    reason="bash needed")
def test_installer_dry_run_renders(tmp_path):
    """DRY_RUN=1 must work unprivileged and produce fully rendered units with the
    requested output directory, the vlbaweather directory as the working directory and
    make_vlba_page.py as the generator."""
    env = dict(os.environ, DRY_RUN="1", OUT_DIR=str(tmp_path / "www"),
               CACHE_DIR=str(tmp_path / "cache"),
               # a real /etc/vlbaweather.env must not interfere
               ENV_FILES=str(tmp_path / "absent.env"))
    r = subprocess.run(["bash", INSTALLER], env=env, capture_output=True, text=True,
                       cwd=str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    m = re.search(r"rendered files go to (\S+)", r.stdout)
    assert m, r.stdout
    unit_dir = m.group(1)
    try:
        service = open(os.path.join(unit_dir, "vlbaweather.service"), encoding="utf-8").read()
        timer = open(os.path.join(unit_dir, "vlbaweather.timer"), encoding="utf-8").read()
        apache = open(os.path.join(unit_dir, "vlbaweather", "apache-vlbaweather.conf"),
                      encoding="utf-8").read()
    finally:
        subprocess.run(["rm", "-rf", unit_dir], check=False)
    assert "__" not in service and "__" not in apache
    assert timer == _read("deploy", "vlbaweather.timer")
    assert "WEATHER_OUT_DIR=%s" % (tmp_path / "www") in service
    assert "WorkingDirectory=%s\n" % ROOT in service
    assert "ExecStart=/usr/bin/python3 %s/make_vlba_page.py" % ROOT in service
    assert "EnvironmentFile=-/etc/vlbaweather.env" in service
    assert "EnvironmentFile=-%s/vlbaweather.env" % pwd.getpwnam(ME).pw_dir in service
    assert "Alias /vlbaweather %s" % (tmp_path / "www") in apache
    assert "systemctl enable --now vlbaweather.timer" in r.stdout
    assert "repo:      %s\n" % ROOT in r.stdout
    assert "local-weather-awareness.service" not in r.stdout


def _dry_run(tmp_path, *args, **env):
    e = dict(os.environ, DRY_RUN="1", **env)
    for k in ("OUT_DIR", "CACHE_DIR"):
        if k not in env:
            e.pop(k, None)
    r = subprocess.run(["bash", INSTALLER] + list(args), env=e, capture_output=True, text=True,
                       cwd=str(tmp_path))
    m = re.search(r"rendered files go to (\S+)", r.stdout)
    if m:
        subprocess.run(["rm", "-rf", m.group(1)], check=False)
    return r


def test_installer_refuses_a_path_the_env_file_overrides(tmp_path):
    """At run time EnvironmentFile= beats the unit's Environment=, so an OUT_DIR/CACHE_DIR
    request that differs from the env files must stop the installer, not produce a unit
    that writes somewhere else; an agreeing request (modulo a trailing slash, ~) is fine."""
    envf = tmp_path / "vlbaweather.env"
    envf.write_text("WEATHER_OUT_DIR=%s\n# WEATHER_CACHE_DIR=/ignored\n"
                    "WEATHER_CACHE_DIR=%s/\n" % (tmp_path / "www", tmp_path / "cache"))
    files = "%s %s" % (tmp_path / "absent.env", envf)

    r = _dry_run(tmp_path, str(tmp_path / "other"), ENV_FILES=files)
    assert r.returncode != 0
    assert "WEATHER_OUT_DIR=%s" % (tmp_path / "www") in r.stderr
    assert str(envf) in r.stderr and "env file wins at run time" in r.stderr

    r = _dry_run(tmp_path, ENV_FILES=files, OUT_DIR=str(tmp_path / "other"))
    assert r.returncode != 0 and "OUT_DIR=%s was requested" % (tmp_path / "other") in r.stderr

    r = _dry_run(tmp_path, ENV_FILES=files, CACHE_DIR=str(tmp_path / "c2"))
    assert r.returncode != 0 and "WEATHER_CACHE_DIR=%s" % (tmp_path / "cache") in r.stderr

    r = _dry_run(tmp_path, str(tmp_path / "www") + "/", ENV_FILES=files,
                 CACHE_DIR=str(tmp_path / "cache"))
    assert r.returncode == 0, r.stderr
    assert "output:    %s\n" % (tmp_path / "www") in r.stdout
    assert "cache:     %s\n" % (tmp_path / "cache") in r.stdout

    r = _dry_run(tmp_path, ENV_FILES=files)            # no request: the env file decides
    assert r.returncode == 0, r.stderr
    assert "output:    %s\n" % (tmp_path / "www") in r.stdout


# ---- apache snippet ---------------------------------------------------------------------
def test_apache_snippet_shape():
    conf = _read("deploy", "apache-vlbaweather.conf")
    assert "Alias /vlbaweather __OUT_DIR__" in conf
    assert "/myweather" not in "\n".join(ln for ln in conf.splitlines()
                                         if not ln.lstrip().startswith("#"))
    assert '<Directory "__OUT_DIR__">' in conf
    # the run user owns the directory: never follow a symlink it could plant there to a file
    # it does not own
    assert "Options -Indexes -FollowSymLinks +SymLinksIfOwnerMatch" in conf
    assert "+FollowSymLinks" not in conf
    assert "Require all granted" in conf and "DirectoryIndex index.html" in conf
    # the page regenerates every 5 min: 120 s for the page, its health JSON and the PNGs
    assert re.search(r'<FilesMatch "\^\(index\\\.html\|status\\\.json\)\$">\s*'
                     r'Header set Cache-Control "max-age=120"', conf)
    assert re.search(r'<FilesMatch "\\\.png\$">\s*Header set Cache-Control "max-age=120"', conf)
    assert "mod_headers" in conf and "DocumentRoot" in conf
    # every opened block is closed (comments mention the tags too, so strip them first)
    code = "\n".join(ln for ln in conf.splitlines() if not ln.lstrip().startswith("#"))
    for tag in ("Directory", "FilesMatch", "IfModule"):
        assert len(re.findall(r"<%s\b" % tag, code)) == len(re.findall(r"</%s>" % tag, code))


def test_apache_snippet_header_lines_need_no_mod_headers():
    """Every Header directive sits inside <IfModule mod_headers.c>: a build without
    mod_headers (Gentoo compiles modules from APACHE2_MODULES) must still start and serve the
    page. The comments name Gentoo's DocumentRoot and vhosts.d, and the snippet's own file."""
    conf = _read("deploy", "apache-vlbaweather.conf")
    depth = headers = 0
    for ln in conf.splitlines():
        t = ln.strip()
        if t.startswith("#"):
            continue
        if t == "<IfModule mod_headers.c>":
            depth += 1
        elif t == "</IfModule>":
            depth -= 1
        elif t.startswith("Header "):
            headers += 1
            assert depth == 1, "Header outside <IfModule mod_headers.c>: %r" % t
    assert headers == 2 and depth == 0
    assert "/var/www/localhost/htdocs" in conf and "/etc/apache2/vhosts.d/vlbaweather.conf" in conf
    assert "/etc/vlbaweather/apache-vlbaweather.conf" in conf


# ---- cron wrapper (deploy/vlbaweather-cron.sh) ---------------------------------------------
STUB_GENERATOR = r'''
import json, os, sys
print("STUB-OUT " + json.dumps({
    "cwd": os.getcwd(), "argv": sys.argv[1:], "nice": os.nice(0),
    "env": dict((k, v) for k, v in os.environ.items() if k.startswith(("WEATHER_", "VLBA_", "STUB_")))}))
sys.stdout.flush()
sys.stderr.write("STUB-ERR a line on stderr\n")
if os.environ.get("STUB_SLEEP"):
    import time
    time.sleep(float(os.environ["STUB_SLEEP"]))
sys.exit(int(os.environ.get("STUB_RC", "0")))
'''


def _code_lines(text):
    """Shell source without comment-only lines and without trailing '# ...' comments."""
    out = []
    for ln in text.splitlines():
        if ln.lstrip().startswith("#"):
            continue
        out.append(re.sub(r"\s+#\s.*$", "", ln))
    return "\n".join(out)


def _stub(bindir, name, body):
    """An executable /bin/sh stub called NAME in BINDIR."""
    bindir.mkdir(exist_ok=True)
    path = bindir / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return path


def _stub_output(text):
    """The dict the stub generator printed (the last STUB-OUT line)."""
    lines = [ln for ln in text.splitlines() if "STUB-OUT " in ln]
    assert lines, "stub generator did not run; output:\n" + text
    return json.loads(lines[-1].split("STUB-OUT ", 1)[1])


def _wrapper_env(tmp_path, **extra):
    """A cron-like environment: no inherited WEATHER_* / VLBA_* variables, HOME in tmp_path,
    no system env files (ENV_FILES), the stub generator run by this test's Python."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    gen = tmp_path / "gen.py"
    if not gen.exists():
        gen.write_text(STUB_GENERATOR)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LANG": "C",
           "ENV_FILES": "", "WEATHER_PYTHON": sys.executable, "WEATHER_GENERATOR": str(gen),
           "WEATHER_LOG": "-"}
    env.update(extra)
    return dict((k, v) for k, v in env.items() if v is not None)


def _run_wrapper(env, cwd):
    # cron runs the script through its #!/bin/sh line; LWA_TEST_SH=/path/to/dash (or
    # "busybox ash") repeats these tests under another POSIX shell.
    shell = os.environ.get("LWA_TEST_SH", "/bin/sh").split()
    return subprocess.run(shell + [WRAPPER], env=env, cwd=str(cwd),
                          capture_output=True, text=True, timeout=60)


needs_non_root = pytest.mark.skipif(IS_ROOT, reason="the wrapper refuses to run as root")


def test_cron_wrapper_is_posix_sh_and_never_evaluates_env_files():
    text = _read("deploy", "vlbaweather-cron.sh")
    assert text.startswith("#!/bin/sh\n")
    assert os.access(WRAPPER, os.X_OK), "vlbaweather-cron.sh must be executable"
    for shell in (["sh", "-n"], ["bash", "--posix", "-n"], ["bash", "-n"]):
        r = subprocess.run(shell + [WRAPPER], capture_output=True, text=True)
        assert r.returncode == 0, (shell, r.stderr)
    code = _code_lines(text)
    assert not re.search(r"\beval\b", code), "the wrapper must never eval"
    assert not re.search(r"(^|[;&|]\s*)(\.|source)\s", code, re.M), "nor source a file"
    assert "logger -t vlbaweather" in code and "nice -n 10" in code
    assert "umask 022" in code and "log_max_bytes=1048576" in code
    assert "gen=${WEATHER_GENERATOR:-make_vlba_page.py}" in code
    assert "env_files=/etc/vlbaweather.env" in code and "$HOME/vlbaweather.env" in code
    assert "local-weather-awareness" not in code, "the wrapper keeps the vlbaweather names"
    # the wrapper's own variables are lower-case: an env file KEY can never change them
    assert not re.findall(r"^\s*([A-Z][A-Z0-9_]*)=", code, re.M)


@needs_non_root
def test_cron_wrapper_env_files_are_parsed_literally(tmp_path):
    """KEY=value lines are exported as plain text: command substitutions, backquotes and
    ';' never run. Comments, 'export X=', lower-case or malformed keys are skipped; quotes
    and blanks are trimmed; a later file wins; an empty value does not clear an earlier one;
    the files win over the inherited environment; VLBA_* keys travel like WEATHER_* ones."""
    m1, m2, m3 = (tmp_path / n for n in ("pwned1", "pwned2", "pwned3"))
    first = tmp_path / "etc.env"
    first.write_bytes((
        "# WEATHER_COMMENTED=1\n"
        "   # an indented comment\n"
        "\n"
        "WEATHER_X=$(touch %s)\n"
        "WEATHER_Y=`touch %s`; touch %s\n"
        "  WEATHER_INDENT=ok   \n"
        "WEATHER_DQ=\"two words\"\n"
        "WEATHER_SQ='single $HOME'\n"
        "WEATHER_DOLLAR=$HOME\n"
        "export WEATHER_EXPORTED=1\n"
        "weather_lower=1\n"
        "WEATHER_BAD-NAME=1\n"
        "9WEATHER=1\n"
        "just some text\n"
        "IFS=x\n"
        "WEATHER_CRLF=dos\r\n"
        "WEATHER_KEEP=first\n"
        "WEATHER_WIN=first\n"
        "WEATHER_ENV=from-file\n"
        "VLBA_GLM_BUCKETS=noaa-goes19=-75.2;noaa-goes18=-137.0\n"
        "VLBA_VETO_EVENTS=Tornado Warning, High Wind Warning\n" % (m1, m2, m3)).encode())
    second = tmp_path / "home.env"
    second.write_text("WEATHER_KEEP=\nWEATHER_WIN=second\nWEATHER_LAST=no-newline")
    env = _wrapper_env(tmp_path, ENV_FILES="%s %s" % (first, second), WEATHER_ENV="inherited")
    r = _run_wrapper(env, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    for marker in (m1, m2, m3):
        assert not marker.exists(), "an env file value was executed"
    got = _stub_output(r.stdout)["env"]
    assert got["WEATHER_X"] == "$(touch %s)" % m1
    assert got["WEATHER_Y"] == "`touch %s`; touch %s" % (m2, m3)
    assert got["WEATHER_INDENT"] == "ok"
    assert got["WEATHER_DQ"] == "two words"
    assert got["WEATHER_SQ"] == "single $HOME"
    assert got["WEATHER_DOLLAR"] == "$HOME"
    assert got["WEATHER_CRLF"] == "dos"
    assert got["WEATHER_KEEP"] == "first"
    assert got["WEATHER_WIN"] == "second"
    assert got["WEATHER_LAST"] == "no-newline"
    assert got["WEATHER_ENV"] == "from-file"
    assert got["VLBA_GLM_BUCKETS"] == "noaa-goes19=-75.2;noaa-goes18=-137.0"
    assert got["VLBA_VETO_EVENTS"] == "Tornado Warning, High Wind Warning"
    for absent in ("WEATHER_COMMENTED", "WEATHER_EXPORTED", "WEATHER_BAD"):
        assert absent not in got
    assert "STUB-ERR a line on stderr" in r.stdout, "stderr must be logged too"


@needs_non_root
def test_cron_wrapper_passes_the_warning_switch_off(tmp_path, monkeypatch):
    """VLBA_VETO_EVENTS=none switches the warning check off through the cron wrapper too:
    the wrapper skips empty values (an empty line must not clear an earlier file's setting),
    so the empty form only works under systemd, and the docs give "none"."""
    f = tmp_path / "etc.env"
    f.write_text("VLBA_VETO_EVENTS=none\nVLBA_NWS_WINTER=off\n")
    r = _run_wrapper(_wrapper_env(tmp_path, ENV_FILES=str(f)), tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    got = _stub_output(r.stdout)["env"]
    for k in list(os.environ):
        if k.startswith(("WEATHER_", "VLBA_")):
            monkeypatch.delenv(k)
    for k, v in got.items():
        if k.startswith("VLBA_"):
            monkeypatch.setenv(k, v)
    c = VlbaConfig.from_env(out_dir=str(tmp_path / "o"), cache_dir=str(tmp_path / "c"))
    assert c.veto_event_names == [] and not c.check_enabled("warning") and c.nws_winter is False
    text = _read("vlbaweather.env.example")
    assert "VLBA_VETO_EVENTS=none" in text


@needs_non_root
def test_cron_wrapper_default_env_files_are_etc_then_home(tmp_path):
    """Without ENV_FILES the wrapper reads /etc/vlbaweather.env, then ~/vlbaweather.env (the
    home file sets everything this test depends on, so a real /etc file cannot interfere);
    local-weather-awareness's ~/local-weather-awareness.env is NOT read."""
    env = _wrapper_env(tmp_path, ENV_FILES=None)
    for k in ("WEATHER_PYTHON", "WEATHER_GENERATOR", "WEATHER_LOG"):
        env.pop(k)
    (tmp_path / "gen.py").write_text(STUB_GENERATOR)
    (tmp_path / "home" / "vlbaweather.env").write_text(
        "WEATHER_PYTHON=%s\nWEATHER_GENERATOR=%s\nWEATHER_LOG=-\nWEATHER_FROM_HOME=yes\n"
        % (sys.executable, tmp_path / "gen.py"))
    (tmp_path / "home" / "local-weather-awareness.env").write_text("WEATHER_FROM_PARENT=yes\n")
    r = _run_wrapper(env, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    got = _stub_output(r.stdout)["env"]
    assert got["WEATHER_FROM_HOME"] == "yes" and "WEATHER_FROM_PARENT" not in got
    assert "/etc/vlbaweather.env" in _read("deploy", "vlbaweather-cron.sh")


@needs_non_root
def test_cron_wrapper_runs_python3_in_the_vlbaweather_dir_and_passes_its_status(tmp_path):
    """Default command: `python3 make_vlba_page.py` found through PATH, run from the
    vlbaweather directory whatever the caller's directory; the wrapper exits with its status."""
    bindir = tmp_path / "bin"
    record = tmp_path / "python3.args"
    _stub(bindir, "python3", 'printf "%%s\\n" "$(pwd)" "$@" > "%s"\n'
                             'echo "stub python says hello"\nexit 7' % record)
    env = _wrapper_env(tmp_path, PATH="%s:%s" % (bindir, os.environ.get("PATH", "/usr/bin:/bin")),
                       WEATHER_PYTHON=None, WEATHER_GENERATOR=None,
                       WEATHER_LOG=str(tmp_path / "cron.log"))
    r = _run_wrapper(env, tmp_path)
    assert r.returncode == 7, r.stdout + r.stderr
    assert record.read_text().splitlines() == [ROOT, "make_vlba_page.py"]
    log = (tmp_path / "cron.log").read_text()
    assert "stub python says hello" in log
    assert "vlbaweather-cron: generator exited with status 7" in log


@needs_non_root
def test_cron_wrapper_nice_and_success(tmp_path):
    env = _wrapper_env(tmp_path)
    r = _run_wrapper(env, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    out = _stub_output(r.stdout)
    assert out["cwd"] == ROOT
    if shutil.which("nice"):
        assert out["nice"] == min(os.nice(0) + 10, 19)
    assert "exited with status" not in r.stdout


@needs_non_root
@pytest.mark.skipif(not shutil.which("timeout"), reason="timeout(1) not installed")
def test_cron_wrapper_stops_a_wedged_run(tmp_path):
    env = _wrapper_env(tmp_path, WEATHER_TIMEOUT="1", STUB_SLEEP="20")
    r = _run_wrapper(env, tmp_path)
    assert r.returncode == 124, r.stdout + r.stderr
    assert "stopped by the timeout after 1 s" in r.stdout


@needs_non_root
def test_cron_wrapper_missing_python(tmp_path):
    env = _wrapper_env(tmp_path, WEATHER_PYTHON=str(tmp_path / "no-such-python"),
                       WEATHER_LOG=str(tmp_path / "cron.log"))
    r = _run_wrapper(env, tmp_path)
    assert r.returncode == 127
    text = (tmp_path / "cron.log").read_text()
    assert "python interpreter not found" in text and "/etc/vlbaweather.env" in text


@needs_non_root
def test_cron_wrapper_log_file_rotation(tmp_path):
    """A log above 1 MB becomes <log>.1 before the run (one old copy); at exactly 1 MB it is
    kept and appended to. The log directory is created when missing."""
    log = tmp_path / "logs" / "cron.log"
    env = _wrapper_env(tmp_path, WEATHER_LOG=str(log))
    r = _run_wrapper(env, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == "" and "STUB-OUT" in log.read_text()

    log.write_bytes(b"x" * 1048576)                    # exactly 1 MB: not rotated
    assert _run_wrapper(env, tmp_path).returncode == 0
    assert not (tmp_path / "logs" / "cron.log.1").exists()
    assert log.stat().st_size > 1048576

    old = log.read_bytes()                              # now above 1 MB: rotated
    (tmp_path / "logs" / "cron.log.1").write_bytes(b"older copy\n")
    assert _run_wrapper(env, tmp_path).returncode == 0
    assert (tmp_path / "logs" / "cron.log.1").read_bytes() == old
    new = log.read_text()
    assert "x" * 100 not in new and "STUB-OUT" in new and "STUB-ERR" in new
    assert log.stat().st_size < 10000


@needs_non_root
def test_cron_wrapper_default_log_is_in_the_cache_dir_without_syslog(tmp_path):
    """No /dev/log (SYSLOG_SOCKET points nowhere) and no WEATHER_LOG: append to
    ${WEATHER_CACHE_DIR}/cron.log, with ~ expanded like config.py does; without
    WEATHER_CACHE_DIR the VLBA page's own cache, ~/.cache/vlbaweather."""
    envf = tmp_path / "etc.env"
    envf.write_text("WEATHER_CACHE_DIR=~/wxcache\n")
    env = _wrapper_env(tmp_path, ENV_FILES=str(envf), WEATHER_LOG=None, STUB_RC="2",
                       SYSLOG_SOCKET=str(tmp_path / "no-socket"))
    r = _run_wrapper(env, tmp_path)
    assert r.returncode == 2
    log = tmp_path / "home" / "wxcache" / "cron.log"
    text = log.read_text()
    assert "STUB-OUT" in text and "generator exited with status 2" in text
    assert _stub_output(text)["env"]["WEATHER_CACHE_DIR"] == "~/wxcache"   # config.py expands it

    env = _wrapper_env(tmp_path, WEATHER_LOG=None, SYSLOG_SOCKET=str(tmp_path / "no-socket"))
    assert _run_wrapper(env, tmp_path).returncode == 0
    assert "STUB-OUT" in (tmp_path / "home" / ".cache" / "vlbaweather" / "cron.log").read_text()


@needs_non_root
def test_cron_wrapper_logs_to_syslog_through_logger(tmp_path):
    """logger(1) on PATH and a /dev/log socket: the generator's stdout and stderr go to
    `logger -t vlbaweather`, the wrapper's own note too, and the exit status survives the
    pipe (POSIX sh has no pipefail)."""
    sock_path = os.path.join(str(tmp_path), "s")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.bind(sock_path)
    except OSError as e:                               # path too long on some systems
        pytest.skip("cannot create a unix socket here: %s" % e)
    try:
        bindir = tmp_path / "bin"
        args, stdin = tmp_path / "logger.args", tmp_path / "logger.stdin"
        _stub(bindir, "logger", 'printf "%%s\\n" "$*" >> "%s"\n'
                                '[ $# -gt 2 ] || cat >> "%s"' % (args, stdin))
        env = _wrapper_env(tmp_path, WEATHER_LOG=None, SYSLOG_SOCKET=sock_path, STUB_RC="3",
                           PATH="%s:%s" % (bindir, os.environ.get("PATH", "/usr/bin:/bin")))
        r = _run_wrapper(env, tmp_path)
    finally:
        sock.close()
    assert r.returncode == 3, r.stdout + r.stderr
    assert r.stdout == ""
    calls = args.read_text().splitlines()
    assert calls[0] == "-t vlbaweather"
    assert "-t vlbaweather vlbaweather-cron: generator exited with status 3" in calls
    logged = stdin.read_text()
    assert "STUB-OUT" in logged and "STUB-ERR a line on stderr" in logged


# ---- cron installer (deploy/install-cron.sh), DRY_RUN only ------------------------------------
needs_user = pytest.mark.skipif(IS_ROOT, reason="needs an unprivileged user to install for")


def _cron_dry_run(tmp_path, *args, crontab=None, pgrep_rc=0, **env):
    """DRY_RUN=1 install-cron.sh with stubs for crontab(1) (its -l prints CRONTAB, or says
    "no crontab" when None) and pgrep(1); no system env file, no Gentoo docroot unless given.
    Returns (CompletedProcess, {preview file name: text})."""
    bindir = tmp_path / "stubbin"
    fixture = tmp_path / "crontab.fixture"
    if crontab is None:
        _stub(bindir, "crontab", 'echo "no crontab for $USER" >&2; exit 1')
    else:
        fixture.write_text(crontab)
        _stub(bindir, "crontab", 'for a; do [ "$a" = -l ] && exec cat "%s"; done\n'
                                 'echo "stub crontab: refusing to write in a test" >&2; exit 9'
                                 % fixture)
    _stub(bindir, "pgrep", "exit %d" % pgrep_rc)
    e = dict(os.environ, DRY_RUN="1", RUN_USER=ME,
             PATH="%s:%s" % (bindir, os.environ.get("PATH", "/usr/bin:/bin")),
             ENV_FILES=str(tmp_path / "absent.env"),
             SYSTEM_ENV_FILE=str(tmp_path / "absent-system.env"),
             GENTOO_DOCROOT=str(tmp_path / "no-htdocs"))
    for k in ("OUT_DIR", "CACHE_DIR", "SUDO_USER", "ETC_DIR"):
        e.pop(k, None)
    e.update(env)
    r = subprocess.run(["bash", CRON_INSTALLER] + list(args), env=e, capture_output=True,
                       text=True, cwd=str(tmp_path), timeout=120)
    files = {}
    m = re.search(r"preview files go to (\S+)", r.stdout)
    if m:
        for dirpath, _dirs, names in os.walk(m.group(1)):
            for n in names:
                p = os.path.join(dirpath, n)
                with open(p, encoding="utf-8") as f:
                    files[os.path.relpath(p, m.group(1))] = f.read()
                if n == "vlbaweather.env":
                    files["vlbaweather.env mode"] = stat.S_IMODE(os.stat(p).st_mode)
        shutil.rmtree(m.group(1), ignore_errors=True)
    return r, files


def test_cron_installer_syntax_and_flags():
    text = _read("deploy", "install-cron.sh")
    assert text.startswith("#!/bin/bash")
    assert "set -euo pipefail" in text
    assert os.access(CRON_INSTALLER, os.X_OK), "install-cron.sh must be executable"
    r = subprocess.run(["bash", "-n", CRON_INSTALLER], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert GENTOO_CRON_FIX in text
    assert 'CRON_TAG="# vlbaweather"' in text
    assert "/deploy/vlbaweather-cron\\.sh" in text


def test_env_file_helpers_identical_in_both_installers():
    """install.sh and install-cron.sh must read the env files the same way (and the way the
    wrapper does): their env_lookup / env_source / canon functions are kept identical, as
    are the directory helpers."""
    def funcs(text):
        return dict((m.group(1), m.group(0)) for m in re.finditer(
            r"^(env_lookup|env_source|canon|file_trusted|path_from_trusted_file|root_safe|"
            r"prepare_dir)\(\) \{.*?^\}$", text, re.M | re.S))
    a, b = funcs(_read("deploy", "install.sh")), funcs(_read("deploy", "install-cron.sh"))
    assert set(a) == {"env_lookup", "env_source", "canon", "file_trusted",
                      "path_from_trusted_file", "root_safe", "prepare_dir"}
    assert a == b
    # ... and their code is local-weather-awareness's, apart from the service's own names
    # (comments aside), when a checkout of that project is at hand (LWA_CHECKOUT)
    lwa = os.path.join(os.environ.get("LWA_CHECKOUT") or "/nonexistent", "deploy", "install-cron.sh")
    if os.path.isfile(lwa):
        with open(lwa, encoding="utf-8") as f:
            parent = funcs(f.read())
        assert set(parent) == set(a)
        for name in a:
            theirs = _code_lines(parent[name]).replace("local-weather-awareness", "vlbaweather") \
                .replace("myweather", "vlbaweather")
            assert _code_lines(a[name]) == theirs, name


def test_cron_installer_renders_every_snippet_placeholder():
    rendered = set(re.findall(r"s\|(__[A-Z_]+__)\|", _read("deploy", "install-cron.sh")))
    used = set(re.findall(r"__[A-Z_]+__", _read("deploy", "apache-vlbaweather.conf")))
    assert used and used <= rendered


@needs_user
def test_cron_installer_dry_run_crontab_line(tmp_path):
    """The previous vlbaweather line (any checkout) is replaced, every other line is kept
    byte for byte (local-weather-awareness's line among them), and the new line
    comes last."""
    keep = ('MAILTO=""\n'
            "# my own jobs\n"
            "0 3 * * * /usr/local/bin/backup.sh\n"
            "*/2 * * * * /home/me/local-weather-awareness/deploy/local-weather-awareness-cron.sh"
            " # local-weather-awareness\n"
            "15 * * * * echo keep-me # vlbaweather-other\n")
    old = keep + ("*/5 * * * * /old/checkout/vlbaweather/deploy/vlbaweather-cron.sh"
                  " # vlbaweather\n"
                  "*/10 * * * * /srv/elsewhere/vlbaweather/deploy/vlbaweather-cron.sh\n")
    r, files = _cron_dry_run(tmp_path, crontab=old)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "cron line: %s\n" % CRON_LINE in r.stdout
    assert files["crontab"] == keep + CRON_LINE + "\n"
    assert "+ crontab -u %s " % ME in r.stdout

    r, files = _cron_dry_run(tmp_path, crontab=keep + CRON_LINE + "\n")     # idempotent
    assert r.returncode == 0, r.stdout + r.stderr
    assert "unchanged  crontab of %s" % ME in r.stdout
    assert files["crontab"] == keep + CRON_LINE + "\n"

    r, files = _cron_dry_run(tmp_path, crontab=None)                        # no crontab yet
    assert r.returncode == 0, r.stdout + r.stderr
    assert files["crontab"] == CRON_LINE + "\n"


@needs_user
def test_cron_installer_gentoo_docroot_default(tmp_path):
    htdocs = tmp_path / "htdocs"
    htdocs.mkdir()
    r, files = _cron_dry_run(tmp_path, GENTOO_DOCROOT=str(htdocs) + "/")
    assert r.returncode == 0, r.stdout + r.stderr
    out_dir = "%s/vlbaweather" % htdocs
    assert "output:    %s\n" % out_dir in r.stdout
    assert "cache:     %s/.cache/vlbaweather\n" % pwd.getpwnam(ME).pw_dir in r.stdout
    snippet = files["vlbaweather/apache-vlbaweather.conf"]
    assert "Alias /vlbaweather %s\n" % out_dir in snippet
    assert '<Directory "%s">' % out_dir in snippet
    assert "WEATHER_OUT_DIR=%s\n" % out_dir in files["vlbaweather.env"]

    r, files = _cron_dry_run(tmp_path)                  # no Gentoo docroot: Debian/RHEL path
    assert r.returncode == 0, r.stdout + r.stderr
    assert "output:    /var/www/html/vlbaweather\n" in r.stdout


@needs_user
def test_cron_installer_refuses_a_path_the_env_file_overrides(tmp_path):
    envf = tmp_path / "vlbaweather.env"
    envf.write_text("WEATHER_OUT_DIR=%s\n# WEATHER_CACHE_DIR=/ignored\n"
                    "WEATHER_CACHE_DIR=%s/\n" % (tmp_path / "www", tmp_path / "cache"))
    files = "%s %s" % (tmp_path / "absent.env", envf)

    r, _ = _cron_dry_run(tmp_path, str(tmp_path / "other"), ENV_FILES=files)
    assert r.returncode != 0
    assert "WEATHER_OUT_DIR=%s" % (tmp_path / "www") in r.stderr
    assert str(envf) in r.stderr and "env file wins at run time" in r.stderr
    assert "deploy/vlbaweather-cron.sh loads it" in r.stderr

    r, _ = _cron_dry_run(tmp_path, ENV_FILES=files, CACHE_DIR=str(tmp_path / "c2"))
    assert r.returncode != 0 and "WEATHER_CACHE_DIR=%s" % (tmp_path / "cache") in r.stderr

    r, _ = _cron_dry_run(tmp_path, str(tmp_path / "www") + "/", ENV_FILES=files,
                         CACHE_DIR=str(tmp_path / "cache"))
    assert r.returncode == 0, r.stderr
    assert "output:    %s\n" % (tmp_path / "www") in r.stdout
    assert "cache:     %s\n" % (tmp_path / "cache") in r.stdout


@needs_user
def test_cron_installer_appends_to_the_env_file_without_clobbering(tmp_path):
    """Only the missing path line is appended (after a newline the file lacked), every
    existing line stays as it was, the result is 0644, and the real file is untouched."""
    system = tmp_path / "system.env"
    original = ("# site settings\n"
                "WEATHER_USER_AGENT=vlbaweather (https://wx.example.org/vlbaweather/; ops)\n"
                "# WEATHER_CACHE_DIR=/commented/out\n"
                "WEATHER_TITLE=a $(b) `c` d\n"
                "VLBA_WIND_MPH=45\n"
                "WEATHER_OUT_DIR=%s" % (tmp_path / "www"))       # no final newline
    system.write_text(original)
    system.chmod(0o600)
    r, files = _cron_dry_run(tmp_path, SYSTEM_ENV_FILE=str(system), ENV_FILES=str(system))
    assert r.returncode == 0, r.stdout + r.stderr
    new = files["vlbaweather.env"]
    assert new.startswith(original + "\n# added by vlbaweather/deploy/install-cron.sh on ")
    added = new[len(original) + 1:].splitlines()
    assert added[1:] == ["WEATHER_CACHE_DIR=%s/.cache/vlbaweather" % pwd.getpwnam(ME).pw_dir]
    assert new.count("WEATHER_OUT_DIR=") == 1
    assert files["vlbaweather.env mode"] == 0o644
    assert "600 -> 0644" in r.stdout
    assert system.read_text() == original, "a dry run must not touch the real env file"

    complete = original + "\nWEATHER_CACHE_DIR=%s\n" % (tmp_path / "cache")
    system.write_text(complete)
    r, files = _cron_dry_run(tmp_path, SYSTEM_ENV_FILE=str(system), ENV_FILES=str(system))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "unchanged  %s" % system in r.stdout
    assert files["vlbaweather.env"] == complete

    r, files = _cron_dry_run(tmp_path, SYSTEM_ENV_FILE=str(tmp_path / "new.env"))   # absent
    assert r.returncode == 0, r.stdout + r.stderr
    lines = files["vlbaweather.env"].splitlines()
    assert lines[1:] == ["WEATHER_OUT_DIR=/var/www/html/vlbaweather",
                         "WEATHER_CACHE_DIR=%s/.cache/vlbaweather" % pwd.getpwnam(ME).pw_dir]


@needs_user
def test_cron_installer_cron_daemon_check_and_next_steps(tmp_path):
    r, _ = _cron_dry_run(tmp_path, pgrep_rc=1)          # nothing running
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no running cron daemon found" in r.stderr and GENTOO_CRON_FIX in r.stderr

    r, _ = _cron_dry_run(tmp_path, pgrep_rc=0)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "is running" in r.stdout and GENTOO_CRON_FIX not in r.stderr
    # the first run goes through the wrapper as the run user, with cron's bare environment
    assert re.search(r"^\+ su -s /bin/sh %s -c 'cd / && exec env -i HOME=\S+ .*"
                     r"WEATHER_LOG=- %s'$" % (re.escape(ME), re.escape(WRAPPER)), r.stdout, re.M)
    assert ("crontab -u %s -l | grep -v '# vlbaweather$' | crontab -u %s -" % (ME, ME)) in r.stdout
    assert "every 5 minutes" in r.stdout
    assert "vlbaweather.env.example" in r.stdout and "/etc/local-weather-awareness.env is" in r.stdout
    assert "repo:      %s\n" % ROOT in r.stdout
    assert "git pull (or rsync) in %s" % ROOT in r.stdout


@needs_user
def test_installers_print_this_deployments_address(tmp_path):
    """The next steps name the page's address: WEATHER_PAGE_URL from the env files, else a
    guess from this host's name with a hint to set it. Never the example deployment's host,
    which only the example deployment has."""
    envf = tmp_path / "site.env"
    envf.write_text("WEATHER_PAGE_URL = https://wx.example.org/vlbaweather\n")
    r, _ = _cron_dry_run(tmp_path, ENV_FILES=str(envf))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "  check:    curl -I https://wx.example.org/vlbaweather/\n" in r.stdout
    assert "            curl -s https://wx.example.org/vlbaweather/status.json\n" in r.stdout
    assert "WEATHER_PAGE_URL is not set" not in r.stdout
    r, _ = _cron_dry_run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert re.search(r"^  check:    curl -I https://\S+/vlbaweather/$", r.stdout, re.M)
    assert "WEATHER_PAGE_URL is not set, so the page footer shows no address" in r.stdout
    r2 = _dry_run(tmp_path, OUT_DIR=str(tmp_path / "www"), CACHE_DIR=str(tmp_path / "cache"),
                  ENV_FILES=str(envf))
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "  then:   curl -I https://wx.example.org/vlbaweather/\n" in r2.stdout
    assert "  page:   https://wx.example.org/vlbaweather/\n" in r2.stdout
    r2 = _dry_run(tmp_path, OUT_DIR=str(tmp_path / "www"), CACHE_DIR=str(tmp_path / "cache"),
                  ENV_FILES=str(tmp_path / "absent.env"))
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "WEATHER_PAGE_URL is not set, so the page footer shows no address" in r2.stdout
    for out in (r.stdout, r2.stdout):
        assert "tau.kirx.net" not in out and "/myweather" not in out
    # the host is named only in comments (as the example deployment), never in output or
    # in a setting; the Apache snippet is rendered on every host, so it says "example"
    for name in ("install.sh", "install-cron.sh", "apache-vlbaweather.conf",
                 "vlbaweather-cron.sh", "vlbaweather.service", "vlbaweather.timer"):
        for line in _read("deploy", name).splitlines():
            if "tau.kirx.net" in line:
                assert line.lstrip().startswith("#"), (name, line)
                if name.endswith(".conf"):
                    assert "example" in line, (name, line)
    for line in _read("run_local.sh").splitlines():
        assert "tau.kirx.net" not in line or line.lstrip().startswith("#"), line


def test_cron_installer_refuses_root_and_needs_root():
    e = dict(os.environ, RUN_USER="root", DRY_RUN="1", ENV_FILES="/nonexistent")
    e.pop("SUDO_USER", None)
    r = subprocess.run(["bash", CRON_INSTALLER], env=e, capture_output=True, text=True)
    assert r.returncode != 0 and "refusing to run the generator as root" in r.stderr
    if not IS_ROOT:
        e.pop("DRY_RUN")
        r = subprocess.run(["bash", CRON_INSTALLER], env=e, capture_output=True, text=True)
        assert r.returncode != 0 and "run me as root" in r.stderr


@pytest.mark.parametrize("script", ["install-cron.sh", "install.sh"])
def test_installers_refuse_an_incomplete_checkout(tmp_path, script):
    """The generator imports the weather package copied into the checkout: deploy/ without it
    cannot run, and the installers say so before doing anything (even in a dry run), naming
    the missing file."""
    copy = tmp_path / "vlbaweather" / "deploy"
    copy.mkdir(parents=True)
    for n in os.listdir(DEPLOY):
        shutil.copy2(os.path.join(DEPLOY, n), str(copy / n))
    e = dict(os.environ, DRY_RUN="1", RUN_USER=ME, ENV_FILES=str(tmp_path / "absent.env"))
    e.pop("SUDO_USER", None)
    r = subprocess.run(["bash", str(copy / script)], env=e, capture_output=True, text=True,
                       cwd=str(tmp_path))
    assert r.returncode != 0
    assert "%s/vlbaweather/weather/__init__.py not found" % tmp_path in r.stderr
    assert "an incomplete vlbaweather checkout" in r.stderr and "<checkout>/deploy/%s" % script in r.stderr
    assert "DRY RUN" not in r.stdout, "the layout check comes before anything else"
    assert not [n for n in os.listdir(str(tmp_path)) if n.startswith("tmp")], "no temp dir left"


def test_installers_check_the_glm_dependencies_and_say_how_to_install_them():
    """Both installers probe numpy + h5py (or netCDF4) for the run user's interpreter and
    print the same non-fatal warning with the Gentoo, Debian and RHEL packages."""
    for name in ("install-cron.sh", "install.sh"):
        code = _code_lines(_read("deploy", name))
        assert "import numpy, h5py" in code and "import numpy, netCDF4" in code, name
        assert "WARNING: the GLM lightning layer needs python3-numpy and python3-h5py" in code, name
        text = _read("deploy", name)
        for fix in ("emerge --ask dev-python/numpy dev-python/h5py",
                    "apt install python3-numpy python3-h5py",
                    "dnf install python3-numpy python3-h5py", "python3-netcdf4",
                    "the page says lightning is unavailable"):
            assert fix in text, (name, fix)
        # a warning, never a die, and only ever as the run user
        glm_block = code[code.index("import numpy, h5py") - 200:code.index("import numpy, h5py") + 800]
        assert "die " not in glm_block and "as_run_user" in glm_block


def _python_that(tmp_path, name, when, rc):
    """An interpreter stub: exit RC when WHEN is among its arguments (e.g. the module name
    of an import probe), otherwise run the real interpreter."""
    return _stub(tmp_path / "pybin", name,
                 'case "$*" in *%s*) exit %d ;; esac\nexec "%s" "$@"' % (when, rc, sys.executable))


@needs_user
def test_cron_installer_warns_without_numpy_and_h5py(tmp_path):
    """WEATHER_PYTHON that cannot import numpy: the GLM warning names it; one that can:
    no warning. Both runs succeed (the layer is optional)."""
    without = _python_that(tmp_path, "py-without-numpy", "numpy", 1)
    envf = tmp_path / "user.env"
    envf.write_text("WEATHER_PYTHON=%s\n" % without)
    r, _ = _cron_dry_run(tmp_path, ENV_FILES=str(envf))
    assert r.returncode == 0, r.stdout + r.stderr
    assert GLM_WARNING in r.stderr and "for '%s' (as %s)" % (without, ME) in r.stderr
    assert "emerge --ask dev-python/numpy dev-python/h5py" in r.stderr

    with_ = _python_that(tmp_path, "py-with-numpy", "numpy", 0)
    envf.write_text("WEATHER_PYTHON=%s\n" % with_)
    r, _ = _cron_dry_run(tmp_path, ENV_FILES=str(envf))
    assert r.returncode == 0, r.stdout + r.stderr
    assert GLM_WARNING not in r.stderr

    # h5py missing but netCDF4 present is fine too
    only_netcdf = _stub(tmp_path / "pybin", "py-netcdf",
                        'case "$*" in *h5py*) exit 1 ;; *netCDF4*) exit 0 ;; esac\nexec "%s" "$@"'
                        % sys.executable)
    envf.write_text("WEATHER_PYTHON=%s\n" % only_netcdf)
    r, _ = _cron_dry_run(tmp_path, ENV_FILES=str(envf))
    assert r.returncode == 0, r.stdout + r.stderr
    assert GLM_WARNING not in r.stderr


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed")
def test_shellcheck_clean():
    for path, dialect in ((WRAPPER, "sh"), (CRON_INSTALLER, "bash"), (INSTALLER, "bash"),
                          (os.path.join(ROOT, "run_local.sh"), "bash")):
        r = subprocess.run(["shellcheck", "-s", dialect, path], capture_output=True, text=True)
        assert r.returncode == 0, path + "\n" + r.stdout + r.stderr


def test_run_local_previews_the_vlba_page():
    text = _read("run_local.sh")
    assert text.startswith("#!/bin/bash")
    assert os.access(os.path.join(ROOT, "run_local.sh"), os.X_OK)
    r = subprocess.run(["bash", "-n", os.path.join(ROOT, "run_local.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    code = _code_lines(text)
    assert "python3 ./make_vlba_page.py" in code
    assert 'INTERVAL="${INTERVAL:-300}"' in code
    assert 'WEATHER_CACHE_DIR="${WEATHER_CACHE_DIR:-./.cache-dev}"' in code
    assert 'DEV_USER_AGENT="vlbaweather-dev (+https://github.com/kirxkirx/vlbaweather)"' in code
    assert "make_weather_page" not in text and "local-weather-awareness-dev" not in text


# ---- run_local.sh, run ------------------------------------------------------------------------
# The parent's tests run its own copy (weather/tests/test_main.py); these run this one.
RUN_LOCAL = os.path.join(ROOT, "run_local.sh")
needs_bash = pytest.mark.skipif(not shutil.which("bash") or not hasattr(os, "killpg"),
                                reason="needs bash and POSIX process groups")


@pytest.fixture
def run_local_env(tmp_path):
    """Environment for run_local.sh with a stub ``python3`` first on PATH: a generator run
    records its pid, waits until the file ``go`` exists, writes its page the generator's way
    (a temp file renamed over index.html) and then stays busy, without network; anything
    else (the preview server) execs the real interpreter."""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    stub = stub_dir / "python3"
    stub.write_text('#!/bin/bash\n'
                    'if [ "$1" = "./make_vlba_page.py" ]; then\n'
                    '    echo $$ >> "$STUB_PIDS"\n'
                    '    while [ ! -e "$STUB_GO" ]; do sleep 0.1; done\n'
                    '    echo "the generated page" > "$WEATHER_OUT_DIR/index.html.tmp"\n'
                    '    mv -f "$WEATHER_OUT_DIR/index.html.tmp" "$WEATHER_OUT_DIR/index.html"\n'
                    '    exec sleep 60\n'
                    'fi\n'
                    'exec %s "$@"\n' % shlex.quote(sys.executable))
    stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("WEATHER_", "VLBA_"))}
    env.update(PATH=str(stub_dir) + os.pathsep + env.get("PATH", ""),
               STUB_PIDS=str(tmp_path / "gen.pids"), STUB_GO=str(tmp_path / "go"),
               BIND="127.0.0.1", INTERVAL="300", WEATHER_CACHE_DIR=str(tmp_path / "cache"))
    return env, tmp_path


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _gen_pids(path):
    try:
        return [int(x) for x in path.read_text().split()]
    except (OSError, ValueError):
        return []


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"     # a zombie is dead
    except (OSError, IndexError):
        return True


def _wait_until(pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def _listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


def _get(port):
    """(status, body) of GET / from the preview server; (None, "") when it does not answer."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/")
        r = conn.getresponse()
        return r.status, r.read().decode("utf-8", "replace")
    except (OSError, http.client.HTTPException):
        return None, ""
    finally:
        conn.close()


def _start_run_local(env, port, out):
    return subprocess.Popen(["bash", RUN_LOCAL, str(port), str(out)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)


def _cleanup(proc, pids_file):
    """Stop what a failed test left running: the script first (its traps stop the loop and
    the server, each in its own process group), then the generator runs, then the rest."""
    if proc.poll() is None:
        try:
            os.kill(proc.pid, signal.SIGTERM)
            proc.communicate(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
    for pid in _gen_pids(pids_file):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait(timeout=5)


@needs_bash
def test_run_local_shows_a_placeholder_until_the_first_run_has_written_the_page(run_local_env):
    """A first run with an empty cache takes minutes, and the preview showed a bare
    directory listing meanwhile. The placeholder reloads itself until the generator's page
    replaces it; Ctrl-C then stops the server and the running generator."""
    env, tmp = run_local_env
    pids = tmp / "gen.pids"
    port = _free_port()
    proc = _start_run_local(env, port, tmp / "out")
    try:
        assert _wait_until(lambda: _listening(port) and _gen_pids(pids), 15), \
            "preview server or generator did not start"
        status, body = _get(port)
        assert status == 200 and "Directory listing" not in body
        assert "no page yet" in body and '<meta http-equiv="refresh" content="10">' in body
        (tmp / "go").touch()
        assert _wait_until(lambda: _get(port) == (200, "the generated page\n"), 15)
        gen = _gen_pids(pids)
        assert gen and all(_alive(p) for p in gen)
        os.killpg(proc.pid, signal.SIGINT)      # Ctrl-C: SIGINT to the foreground process group
        text = proc.communicate(timeout=15)[0].decode("utf-8", "replace")
        assert proc.returncode == 130, text
        assert _wait_until(lambda: not any(_alive(p) for p in gen), 5), \
            "generator run survived Ctrl-C"
        assert not _listening(port)
        assert "http://127.0.0.1:%d/" % port in text        # BIND is honoured
        assert 'it says "no page yet"' in text
    finally:
        _cleanup(proc, pids)


@needs_bash
def test_run_local_keeps_an_earlier_page(run_local_env):
    """An earlier run's page stays until the next run replaces it; SIGTERM stops it all."""
    env, tmp = run_local_env
    out, pids = tmp / "out", tmp / "gen.pids"
    out.mkdir()
    (out / "index.html").write_text("an earlier run's page\n")
    port = _free_port()
    proc = _start_run_local(env, port, out)
    try:
        assert _wait_until(lambda: _listening(port) and _gen_pids(pids), 15), \
            "preview server or generator did not start"
        assert _get(port) == (200, "an earlier run's page\n")
        gen = _gen_pids(pids)
        os.kill(proc.pid, signal.SIGTERM)
        text = proc.communicate(timeout=15)[0].decode("utf-8", "replace")
        assert proc.returncode == 143, text
        assert _wait_until(lambda: not any(_alive(p) for p in gen), 5)
        assert not _listening(port)
        assert "no page yet" not in text
    finally:
        _cleanup(proc, pids)
    assert (out / "index.html").read_text() == "an earlier run's page\n"


@needs_bash
def test_run_local_exits_with_the_server_status(run_local_env):
    """Port already taken: the server fails, and so does the script, taking the generator
    loop down with it."""
    env, tmp = run_local_env
    pids = tmp / "gen.pids"
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        proc = _start_run_local(env, port, tmp / "out")
        try:
            text = proc.communicate(timeout=20)[0].decode("utf-8", "replace")
        finally:
            _cleanup(proc, pids)
    assert proc.returncode == 1, text
    assert "Address already in use" in text
    assert _wait_until(lambda: not any(_alive(p) for p in _gen_pids(pids)), 5)


# ---- public release -------------------------------------------------------------------------
# Private names (internal hosts, domains, subnets, the author's login and paths) must never
# be published, so this repository does not list them either: they are regular expressions,
# one per line ('#' comments), in the git-ignored file .private-patterns at the top of the
# checkout (local-weather-awareness's test reads a file of the same name), or in the file named
# by LWA_PRIVATE_PATTERNS. Without either, only the generic e-mail check below runs.
PRIVATE_PATTERNS_FILE = os.environ.get("LWA_PRIVATE_PATTERNS") or os.path.join(
    ROOT, ".private-patterns")


def _private_patterns():
    try:
        with open(PRIVATE_PATTERNS_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    return [re.compile(ln.strip()) for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# Public agency addresses that appear verbatim in feed fixtures (the NWS CAP "sender") are
# data, not anybody's private address; the reserved documentation domains (RFC 2606) are
# the placeholder the docs tell deployers to replace.
_EMAIL_OK = (".gov", "@example.org", "@example.com", "@example.net")
_SKIP_DIRS = {".git", "out", "__pycache__", ".pytest_cache", "venv", ".venv"}


def _published_files():
    """Every file of the checkout that the repository publishes."""
    for dirpath, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".cache")]
        for n in names:
            if n.startswith(".nfs") or n.endswith((".pyc", ".png")) or n == ".private-patterns":
                continue
            if n.endswith(".env") and n != "vlbaweather.env.example":
                continue
            yield os.path.join(dirpath, n)


def test_published_files_have_no_private_hosts_paths_or_addresses():
    private = _private_patterns()
    bad = []
    for path in _published_files():
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except UnicodeDecodeError:
            continue
        for i, ln in enumerate(text.splitlines(), 1):
            hits = ["private pattern %d" % i for i, p in enumerate(private, 1) if p.search(ln)]
            hits += [m.group(0) for m in _EMAIL.finditer(ln)
                     if not m.group(0).endswith(_EMAIL_OK)]
            if hits:
                bad.append("%s:%d: %s" % (os.path.relpath(path, ROOT), i, hits))
    assert not bad, "\n".join(bad)


def test_gitignore_keeps_local_files_out_but_publishes_the_example():
    """run_local.sh's out/ and .cache-dev/, any real *.env and the private patterns stay out
    of git, while vlbaweather.env.example goes in."""
    ignore = _read(".gitignore").split()
    for pattern in ("out/", ".cache*/", "__pycache__/", ".pytest_cache/", "*.env",
                    ".nfs*", ".private-patterns"):
        assert pattern in ignore, pattern
    if not shutil.which("git") or not os.path.isdir(os.path.join(ROOT, ".git")):
        pytest.skip("not a git checkout, or git is not installed")
    checks = {"vlbaweather.env.example": 1, "out/index.html": 0, ".cache-dev/x": 0,
              "vlbaweather.env": 0}
    for rel, expect in checks.items():
        r = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", rel], capture_output=True, text=True)
        if r.returncode not in (0, 1):
            pytest.skip("git check-ignore unusable here: %s" % r.stderr.strip())
        assert r.returncode == expect, (rel, "ignored" if r.returncode == 0 else "not ignored")


def test_private_patterns_are_not_in_the_repository():
    """The publish check reads its private names from a git-ignored file, never from the
    code: a denylist in a public test would publish exactly what it keeps out."""
    text = _read("vlba", "tests", "test_deploy.py")
    assert not re.search(r"^_PRIVATE\s*=", text, re.M)
    assert not re.search(r'"[a-z]+" \+ "[a-z]+"', text), "no names split to hide from grep"


# ---- review findings: env-file parsing, installer trust, private patterns -----------------------
@needs_non_root
def test_cron_wrapper_blanks_around_equals_and_its_own_names(tmp_path):
    """'KEY = value' and 'KEY= value' mean KEY=value, as for the installers' env_lookup;
    upper-case names the wrapper used internally (UPPER, LOG_MAX_BYTES, REPO) are ordinary
    keys and cannot break the parser or the log rotation."""
    envf = tmp_path / "blank.env"
    envf.write_text("WEATHER_SPACED = two sides\nWEATHER_AFTER= after\nUPPER=abc\n"
                    "LOG_MAX_BYTES=abc\nREPO=/nowhere\nDIGITS=x\nCR=y\n"
                    "WEATHER_LATER=still-read\nWEATHER_TIMEOUT= 300\n")
    log = tmp_path / "cron.log"
    log.write_bytes(b"x" * 1048577)
    env = _wrapper_env(tmp_path, ENV_FILES=str(envf), WEATHER_LOG=str(log))
    r = _run_wrapper(env, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    text = log.read_text()
    got = _stub_output(text)["env"]
    assert got["WEATHER_SPACED"] == "two sides" and got["WEATHER_AFTER"] == "after"
    assert got["WEATHER_LATER"] == "still-read" and got["WEATHER_TIMEOUT"] == "300"
    assert "not a whole number" not in text
    assert (tmp_path / "cron.log.1").exists(), "the 1 MB rotation still works"
    assert _stub_output(text)["cwd"] == ROOT


def _evil_python(tmp_path):
    """A WEATHER_PYTHON that records how it was called (the environment tells whether the
    installer ran it through as_run_user's `env -i`), then runs the real interpreter."""
    log = tmp_path / "evil.log"
    evil = _stub(tmp_path / "evilbin", "evil-python",
                 'echo "DRY_RUN=${DRY_RUN:-unset} $*" >> "%s"\nexec "%s" "$@"'
                 % (log, sys.executable))
    return evil, log


@needs_user
def test_cron_installer_runs_every_interpreter_as_the_run_user(tmp_path):
    """WEATHER_PYTHON may come from the run user's own env file, so the installer (root)
    runs it only through as_run_user (su + env -i), never directly: the font/zlib probe
    and the GLM probe included."""
    evil, log = _evil_python(tmp_path)
    envf = tmp_path / "user.env"
    envf.write_text("WEATHER_PYTHON=%s\n" % evil)
    r, _ = _cron_dry_run(tmp_path, ENV_FILES=str(envf))
    assert r.returncode == 0, r.stdout + r.stderr
    calls = [c for c in log.read_text().splitlines() if c.startswith("DRY_RUN=")]
    assert any("-B -c" in c for c in calls), calls                    # the font/zlib probe
    assert any("import numpy, h5py" in c for c in calls), calls      # the GLM probe
    assert len(calls) >= 4 and all(c.startswith("DRY_RUN=unset ") for c in calls), calls
    # the probe's -c code (a multi-line argument) imports weather from the checkout
    assert 'sys.path.insert(0, "%s")' % ROOT in log.read_text(), log.read_text()
    code = _code_lines(_read("deploy", "install-cron.sh"))
    assert not re.search(r'^\s*\(?\s*(cd [^)]*&&\s*)?"\$PY"', code, re.M), \
        "every $PY call goes through as_run_user"


@needs_user
def test_installers_take_paths_only_from_files_nobody_else_can_change(tmp_path):
    """Root hands the output and cache directories to the run user, so their paths never
    come from a file the run user (or anybody else) can write: a group/world-writable file
    stands in here for ~RUN_USER/vlbaweather.env as root sees it."""
    envf = tmp_path / "user.env"
    envf.write_text("WEATHER_OUT_DIR=%s\n" % (tmp_path / "www"))
    envf.chmod(0o666)
    r, _ = _cron_dry_run(tmp_path, ENV_FILES=str(envf))
    assert r.returncode != 0
    assert "sets WEATHER_OUT_DIR, but that file or its directory belongs to another user or is" \
        in r.stderr and "writable by others" in r.stderr
    assert "Move the WEATHER_OUT_DIR line to %s (owned by root, mode 0644)" \
        % (tmp_path / "absent-system.env") in r.stderr
    r = _dry_run(tmp_path, ENV_FILES=str(envf))
    assert r.returncode != 0 and "sets WEATHER_OUT_DIR" in r.stderr
    envf.chmod(0o644)                                   # the same file, trusted
    r, _ = _cron_dry_run(tmp_path, ENV_FILES=str(envf))
    assert r.returncode == 0, r.stderr
    # WEATHER_PYTHON / PATH may come from anywhere (they only ever run as the run user), but
    # a value the wrapper could not use stops the installer instead of every cron run
    for line, msg in (("PATH=$PATH:/usr/local/bin", "are not expanded"),
                      ("WEATHER_PYTHON=python3 -u", "give an interpreter name")):
        bad = tmp_path / "bad.env"
        bad.write_text(line + "\n")
        r, _ = _cron_dry_run(tmp_path, ENV_FILES=str(bad))
        assert r.returncode != 0 and msg in r.stderr, (line, r.stderr)
    ok = tmp_path / "ok.env"
    ok.write_text("WEATHER_PYTHON = %s \nPATH= /usr/bin:/bin\n" % sys.executable)
    r, _ = _cron_dry_run(tmp_path, ENV_FILES=str(ok))
    assert r.returncode == 0 and "Python 3.9" not in r.stderr, r.stderr


@needs_user
def test_installers_never_hand_over_system_or_foreign_directories(tmp_path):
    """No system location, no directory reached through a link into one, and no existing
    directory of somebody else (except the default output directory) is chowned to the run
    user; a directory in space the run user controls is made by the run user itself."""
    link = tmp_path / "link"
    link.symlink_to("/etc")
    for out in ("/usr/local/bin", "/root", "/etc/cron.d", "/var/www/html", str(link / "wx")):
        for runner in (_cron_dry_run, _dry_run):
            r = runner(tmp_path, out, ENV_FILES=str(tmp_path / "absent.env"),
                       CACHE_DIR=str(tmp_path / "cache"))
            r = r[0] if isinstance(r, tuple) else r
            assert r.returncode != 0 and "refusing output dir" in r.stderr, (out, r.stderr)
    r, _ = _cron_dry_run(tmp_path, "/var/www/html", ENV_FILES=str(tmp_path / "absent.env"),
                         CACHE_DIR=str(tmp_path / "cache"))
    assert "e.g. /var/www/html/vlbaweather" in r.stderr
    # the run user's own space: no root install -d, the run user's mkdir instead
    r, _ = _cron_dry_run(tmp_path, str(tmp_path / "www"), CACHE_DIR=str(tmp_path / "cache"))
    assert r.returncode == 0, r.stderr
    assert "+ install -d" not in r.stdout
    assert "+ su -s /bin/sh %s -c 'mkdir -p %s && chmod 0755 %s'" % (
        ME, tmp_path / "cache", tmp_path / "cache") in r.stdout
    # an existing directory that belongs to somebody else under a root-owned parent
    foreign = None
    for parent in ("/var/lib", "/var/cache", "/opt", "/var/empty"):
        try:
            names = sorted(os.listdir(parent))
        except OSError:
            continue
        for n in names:
            p = os.path.join(parent, n)
            st = os.lstat(p)
            if stat.S_ISDIR(st.st_mode) and st.st_uid == 0 and os.stat(parent).st_uid == 0:
                foreign = p
                break
        if foreign:
            break
    if foreign is None:
        pytest.skip("no root-owned directory to try")
    r, _ = _cron_dry_run(tmp_path, foreign, CACHE_DIR=str(tmp_path / "cache"))
    assert r.returncode != 0 and "already exists and belongs to root" in r.stderr, r.stderr
    assert "meant for vlbaweather" in r.stderr


@pytest.mark.parametrize("script", ["install-cron.sh", "install.sh"])
def test_refusal_names_the_unsafe_parent_and_the_fix(tmp_path, script):
    """A web root that root does not own alone (Gentoo's htdocs often belongs to another
    account): the installer must not create the output directory as root, and when the run
    user cannot create it either, the error names the offending parent and prints the exact
    commands that hand the directory over."""
    with open(os.path.join(DEPLOY, script)) as f:
        text = f.read()
    funcs = "\n".join(m.group(0) for m in re.finditer(
        r"^(root_safe|prepare_dir)\(\) \{.*?^\}$", text, re.M | re.S))
    parent = tmp_path / "htdocs"              # owned by the test user, not by root
    parent.mkdir()
    out = parent / "vlbaweather"
    harness = "\n".join([
        'die() { echo "error: $*" >&2; exit 1; }',
        'run() { echo "+ $*"; }',
        'as_run_user() { return 1; }',        # the run user cannot write the parent
        'RUN_USER=weather RUN_GROUP=weather RUN_HOME=/home/weather DRY_RUN=""',
        'UNSAFE_PARENT=""',
        funcs,
        'prepare_dir "output dir" "%s" "/nonexistent/default"' % out,
    ])
    r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    assert r.returncode != 0
    assert "Its parent %s (owner " % parent in r.stderr, r.stderr
    assert "mkdir -p %s && chown weather:weather %s && chmod 0755 %s" % (out, out, out) \
        in r.stderr, r.stderr
    assert "+ install -d" not in r.stdout      # root never created it
