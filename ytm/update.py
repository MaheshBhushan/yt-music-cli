"""Keeping ytm (and yt-dlp, which goes stale fastest) current.

Two halves:

* ``check()`` asks PyPI for the latest ytm version at most once a day and
  says whether it is newer than what is running. It never raises: no
  network means "unknown", not a crash at startup.
* ``upgrade()`` re-installs through whatever installed ytm in the first
  place (pipx, ``uv tool`` or plain pip), so the upgrade lands in the same
  environment the ``ytm`` command runs from.

The TUI calls ``check()`` in the background when it opens and shows a toast
when a newer release exists; with ``[update] auto = true`` it runs
``upgrade()`` too. ``ytm update`` does the same from the shell.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from importlib import metadata
from pathlib import Path

PACKAGE = "ytm"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
#: where the last check result is remembered, so a day's worth of TUI
#: launches costs one HTTP request
CHECK_PATH = Path(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
) / "ytm" / "update-check.json"
CHECK_INTERVAL = 24 * 60 * 60  # seconds


def _editable_source(dist=None):
    """The checkout an editable install points at, or None for a normal install."""
    try:
        dist = dist or metadata.distribution(PACKAGE)
        direct = json.loads(dist.read_text("direct_url.json") or "null")
        if not direct.get("dir_info", {}).get("editable"):
            return None
        return Path(urllib.request.url2pathname(urllib.parse.urlparse(direct["url"]).path))
    except Exception:
        return None


def installed_version():
    """The version that is actually running.

    An editable install's dist-info records the version at `pip install -e`
    time and never changes, so after a `git pull` it says the old number
    and every check finds PyPI "newer" than a checkout that is already
    ahead of it. For an editable install the checkout's pyproject.toml is
    the truth.
    """
    source = _editable_source()
    if source is not None:
        try:
            import tomllib

            with open(source / "pyproject.toml", "rb") as file:
                return tomllib.load(file)["project"]["version"]
        except Exception:
            pass
    try:
        return metadata.version(PACKAGE)
    except metadata.PackageNotFoundError:
        return "0"


def latest_version(timeout=3.0, opener=None):
    """The newest release on PyPI, or None when it cannot be reached."""
    opener = opener or urllib.request.urlopen
    try:
        with opener(PYPI_URL, timeout=timeout) as response:
            payload = json.load(response)
        return payload["info"]["version"]
    except Exception:
        return None


def _parts(version):
    return tuple(int(p) if p.isdigit() else -1 for p in re.split(r"[.+-]", version))


def is_newer(latest, installed):
    """True when `latest` is a strictly higher version than `installed`."""
    if not latest or not installed:
        return False
    return _parts(latest) > _parts(installed)


def check(force=False, path=None, fetch=None, now=None):
    """Compare the running version with PyPI, at most once per CHECK_INTERVAL.

    Returns ``{"installed", "latest", "newer", "checked_at", "cached"}``.
    `latest` is None when PyPI could not be reached and nothing was cached.
    """
    path = Path(path) if path is not None else CHECK_PATH
    now = time.time() if now is None else now
    fetch = fetch or latest_version  # looked up at call time so tests can stub it
    installed = installed_version()
    cached = None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = None
    fresh = (
        cached is not None
        and isinstance(cached.get("checked_at"), (int, float))
        and now - cached["checked_at"] < CHECK_INTERVAL
    )
    if fresh and not force:
        latest = cached.get("latest")
        checked_at = cached["checked_at"]
        used_cache = True
    else:
        latest = fetch()
        checked_at = now
        used_cache = False
        if latest is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"latest": latest, "checked_at": now}), encoding="utf-8")
            except OSError:
                pass
        elif cached is not None:
            latest = cached.get("latest")  # stale is better than nothing
    return {
        "installed": installed,
        "latest": latest,
        "newer": is_newer(latest, installed),
        "checked_at": checked_at,
        "cached": used_cache,
    }


def install_kind(prefix=None):
    """How ytm was installed: "pipx", "uv", "editable" or "pip".

    Decides which upgrade command reaches the right environment.
    """
    prefix = (prefix if prefix is not None else sys.prefix).replace("\\", "/")
    if "/pipx/venvs/" in prefix or "/pipx/venvs" in prefix.rstrip("/"):
        return "pipx"
    if "/uv/tools/" in prefix:
        return "uv"
    try:
        dist = metadata.distribution(PACKAGE)
        direct = dist.read_text("direct_url.json")
        if direct and json.loads(direct).get("dir_info", {}).get("editable"):
            return "editable"
    except Exception:
        pass
    return "pip"


def _has_module(name):
    return importlib.util.find_spec(name) is not None


def upgrade_commands(kind, yt_dlp=True, has_pip=None, has_uv=None, target=None):
    """The shell commands that upgrade ytm (and yt-dlp) for `kind`.

    A plain venv normally has pip; one made by `uv venv` does not, so that
    case goes through `uv pip` aimed at this interpreter instead.

    Every installer is told not to trust what it cached: right after a
    release pip's HTTP cache (and the index CDN behind it) can still say
    the old version is the newest, and then `pip install -U` exits 0 having
    installed nothing. With `target` set, pip is asked for that exact
    version, so a stale index is an error rather than a silent no-op.
    """
    spec = f"{PACKAGE}=={target}" if target else PACKAGE
    if kind == "pipx":
        commands = [["pipx", "upgrade", "--pip-args=--no-cache-dir", PACKAGE]]
        if yt_dlp:
            commands.append(["pipx", "runpip", PACKAGE, "install", "-U", "--no-cache-dir", "yt-dlp"])
        return commands
    if kind == "uv":
        # `uv tool upgrade` refreshes the tool and its dependencies together
        return [["uv", "tool", "upgrade", "--refresh", PACKAGE]]
    if kind == "editable":
        return []  # a checkout: `git pull` is the upgrade
    packages = [spec, "yt-dlp"] if yt_dlp else [spec]
    has_pip = _has_module("pip") if has_pip is None else has_pip
    if has_pip:
        return [[sys.executable, "-m", "pip", "install", "-U", "--no-cache-dir", *packages]]
    has_uv = shutil.which("uv") is not None if has_uv is None else has_uv
    if has_uv:
        return [["uv", "pip", "install", "-U", "--refresh", "--python", sys.executable, *packages]]
    return []


def installed_version_now(run=subprocess.run):
    """What a fresh process of this interpreter would report as ytm's version.

    The running process's metadata is what was on disk when it started; only
    a new process sees the dist-info an upgrade just wrote.
    """
    try:
        result = run(
            [sys.executable, "-c", f"from importlib import metadata; print(metadata.version({PACKAGE!r}))"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def upgrade(kind=None, yt_dlp=True, run=subprocess.run, target=None, verify=None):
    """Upgrade in place. Returns (ok, text) where text is what to show.

    With `target` (the version PyPI reported) the result is checked: the
    upgrade only counts as done when a fresh interpreter reports that
    version, so a silent no-op from a stale index is reported as such
    instead of as "upgraded, restart ytm".

    Windows launchers stay locked while running. Return an external update
    command there instead of letting an installer partially uninstall ytm.
    """
    kind = kind or install_kind()
    commands = upgrade_commands(kind, yt_dlp=yt_dlp, target=target)
    if not commands:
        if kind == "editable":
            return False, "ytm runs from a source checkout; update it with git pull"
        return False, (
            f"this environment has neither pip nor uv; run: "
            f"uv pip install -U --python {sys.executable} {PACKAGE} yt-dlp"
        )
    if sys.platform == "win32":
        # PowerShell needs the call operator for quoted executable paths.
        # Single-quoted arguments also keep spaces and metacharacters literal.
        external = "; ".join(
            "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in command)
            for command in commands
        )
        return False, (
            "Close all ytm instances, then run this in PowerShell to update "
            f"without locking ytm.exe: {external}"
        )
    output = []
    for command in commands:
        try:
            result = run(command, capture_output=True, text=True)
        except OSError as exc:  # pipx/uv not on PATH
            return False, f"could not run {command[0]}: {exc}"
        output.append((result.stdout or "") + (result.stderr or ""))
        if result.returncode != 0:
            return False, f"{' '.join(command)} failed:\n{output[-1].strip()}"
    text = "\n".join(part.strip() for part in output if part.strip())
    if target:
        verify = verify or installed_version_now
        now = verify()
        if now is not None and not is_newer(now, target) and now != target:
            return False, (
                f"{PACKAGE} {now} is still installed after the upgrade; the package index "
                f"has not picked up {target} yet. Try again in a few minutes.\n{text}".rstrip()
            )
    return True, text
