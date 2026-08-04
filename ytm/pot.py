"""PO token provider plumbing for yt-dlp.

YouTube puts some accounts into a SABR-only experiment: when yt-dlp sends
account cookies, InnerTube answers with media formats that carry no URL
unless the request is accompanied by a *PO token* (a BotGuard attestation).
The token is minted by the maintained ``bgutil-ytdlp-pot-provider`` yt-dlp
plugin, which asks a small HTTP service for it. Nothing in this module
implements attestation itself -- it only makes sure the service is running
and tells the plugin where to find it.

The service runs as a Docker container (``--restart unless-stopped``, so it
comes back by itself after a reboot once created). :func:`ensure_provider`
is what the daemon calls at startup: it pings the service and, only if that
fails, starts (or creates) the container. It never raises and never blocks
playback -- if the provider cannot be brought up, resolution simply falls
back to :mod:`ytm.resolve`'s existing cookies-then-no-cookies chain.
"""

import json
import subprocess
import time
import urllib.error
import urllib.request

from ytm import config as config_mod

#: name of the Docker container holding the token service
CONTAINER_NAME = "bgutil-provider"

#: image the container is created from; deliberately unpinned beyond the
#: plugin's own major series, so it tracks the plugin rather than freezing it
IMAGE = "brainicism/bgutil-ytdlp-pot-provider"

#: how long to wait for the service to answer /ping
PING_TIMEOUT = 2.0

#: how long to wait for docker to do anything
DOCKER_TIMEOUT = 30

#: how long a just-started container is given to answer /ping, and how often
#: it is asked -- the service takes a couple of seconds to come up
STARTUP_TIMEOUT = 20.0
STARTUP_POLL = 0.5


def _pot_config(config=None):
    config = config if config is not None else config_mod.load()
    return config["pot"]


def is_reachable(base_url, timeout=PING_TIMEOUT, opener=urllib.request.urlopen):
    """Whether the token service answers ``GET /ping`` at `base_url`."""
    try:
        with opener(f"{base_url.rstrip('/')}/ping", timeout=timeout) as response:
            json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError):
        return False
    return True


def _docker(*args, runner=subprocess.run):
    """Run docker with `args`, returning whether it succeeded."""
    try:
        completed = runner(
            ["docker", *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=DOCKER_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _port(base_url):
    """The port `base_url` points at, defaulting to the service's own."""
    _, _, tail = base_url.rpartition(":")
    port = tail.split("/")[0]
    return port if port.isdigit() else "4416"


def ensure_provider(config=None, runner=subprocess.run, opener=urllib.request.urlopen):
    """Make the token service reachable, and report whether it is.

    Pings first, so the common case (the container is already up, because
    Docker restarted it after the last reboot) costs one local request. Only
    if that fails does it start the existing container, and only if there is
    no container does it create one. Never raises: a False return means
    resolution will fall back to working without a PO token.
    """
    pot = _pot_config(config)
    if not pot["enabled"]:
        return False
    base_url = pot["base_url"]
    if is_reachable(base_url, opener=opener):
        return True
    port = _port(base_url)
    started = _docker("start", CONTAINER_NAME, runner=runner) or _docker(
        "run",
        "--name",
        CONTAINER_NAME,
        "--detach",
        "--init",
        "--restart",
        "unless-stopped",
        "--publish",
        f"{port}:4416",
        IMAGE,
        runner=runner,
    )
    if not started:
        return False
    # the service needs a moment to listen; docker returning is not enough
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while True:
        if is_reachable(base_url, opener=opener):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(STARTUP_POLL)


def ydl_opts(config=None):
    """yt-dlp options that let the plugin reach the service, if enabled.

    Returns an empty mapping when the provider is disabled, so the caller's
    options are untouched.
    """
    pot = _pot_config(config)
    if not pot["enabled"]:
        return {}
    return {
        "extractor_args": {
            "youtubepot-bgutilhttp": {"base_url": [pot["base_url"]]},
        },
    }
