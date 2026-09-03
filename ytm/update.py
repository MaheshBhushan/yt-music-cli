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


def installed_version():
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


def upgrade_commands(kind, yt_dlp=True, has_pip=None, has_uv=None):
    """The shell commands that upgrade ytm (and yt-dlp) for `kind`.

    A plain venv normally has pip; one made by `uv venv` does not, so that
    case goes through `uv pip` aimed at this interpreter instead.
    """
    if kind == "pipx":
        commands = [["pipx", "upgrade", PACKAGE]]
        if yt_dlp:
            commands.append(["pipx", "runpip", PACKAGE, "install", "-U", "yt-dlp"])
        return commands
    if kind == "uv":
        # `uv tool upgrade` refreshes the tool and its dependencies together
        return [["uv", "tool", "upgrade", PACKAGE]]
    if kind == "editable":
        return []  # a checkout: `git pull` is the upgrade
    packages = [PACKAGE, "yt-dlp"] if yt_dlp else [PACKAGE]
    has_pip = _has_module("pip") if has_pip is None else has_pip
    if has_pip:
        return [[sys.executable, "-m", "pip", "install", "-U", *packages]]
    has_uv = shutil.which("uv") is not None if has_uv is None else has_uv
    if has_uv:
        return [["uv", "pip", "install", "-U", "--python", sys.executable, *packages]]
    return []


def upgrade(kind=None, yt_dlp=True, run=subprocess.run):
    """Upgrade in place. Returns (ok, text) where text is what to show."""
    kind = kind or install_kind()
    commands = upgrade_commands(kind, yt_dlp=yt_dlp)
    if not commands:
        if kind == "editable":
            return False, "ytm runs from a source checkout; update it with git pull"
        return False, (
            f"this environment has neither pip nor uv; run: "
            f"uv pip install -U --python {sys.executable} {PACKAGE} yt-dlp"
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
    return True, "\n".join(part.strip() for part in output if part.strip())
