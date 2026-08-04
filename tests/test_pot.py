"""Tests for ytm.pot: reachability, container startup, graceful degradation.

Fully offline: no HTTP request leaves the process, docker is never invoked,
and the real ~/.config/ytm/config.toml and auth file are never read.
"""

import io
import json
import subprocess

from ytm import config as config_mod, pot, resolve

BASE_URL = "http://127.0.0.1:4416"


def _config(enabled=True, base_url=BASE_URL):
    config = config_mod.load("/nonexistent/ytm-test-config.toml")
    config["pot"] = {"enabled": enabled, "base_url": base_url}
    return config


class FakeOpener:
    """Stands in for urllib.request.urlopen; answers /ping while `up`."""

    def __init__(self, up=True, body=None):
        self.up = up
        self.body = body if body is not None else json.dumps({"version": "1.3.1"})
        self.urls = []

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        if not self.up:
            raise OSError("connection refused")
        return io.BytesIO(self.body.encode("utf-8"))


class FakeRunner:
    """Records docker invocations; `start_succeeds` drives the return code."""

    def __init__(self, start_succeeds=False, opener=None):
        self.start_succeeds = start_succeeds
        self.opener = opener
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        ok = argv[1] != "start" or self.start_succeeds
        if ok and self.opener is not None:
            self.opener.up = True
        return subprocess.CompletedProcess(argv, 0 if ok else 1)


# -- reachability -----------------------------------------------------------


def test_is_reachable_when_service_answers_ping():
    opener = FakeOpener()
    assert pot.is_reachable(BASE_URL, opener=opener) is True
    assert opener.urls == [f"{BASE_URL}/ping"]


def test_is_reachable_false_when_connection_refused():
    assert pot.is_reachable(BASE_URL, opener=FakeOpener(up=False)) is False


def test_is_reachable_false_on_garbage_response():
    assert pot.is_reachable(BASE_URL, opener=FakeOpener(body="not json")) is False


# -- ensure_provider --------------------------------------------------------


def test_ensure_provider_short_circuits_when_already_up():
    runner = FakeRunner()
    assert pot.ensure_provider(_config(), runner=runner, opener=FakeOpener()) is True
    assert runner.calls == []


def test_ensure_provider_starts_existing_container():
    opener = FakeOpener(up=False)
    runner = FakeRunner(start_succeeds=True, opener=opener)
    assert pot.ensure_provider(_config(), runner=runner, opener=opener) is True
    assert runner.calls == [["docker", "start", pot.CONTAINER_NAME]]


def test_ensure_provider_creates_container_when_none_exists():
    opener = FakeOpener(up=False)
    runner = FakeRunner(start_succeeds=False, opener=opener)
    assert pot.ensure_provider(_config(), runner=runner, opener=opener) is True
    assert [call[1] for call in runner.calls] == ["start", "run"]
    assert pot.IMAGE in runner.calls[1]
    assert "4416:4416" in runner.calls[1]


def test_ensure_provider_publishes_the_configured_port():
    opener = FakeOpener(up=False)
    runner = FakeRunner(opener=opener)
    pot.ensure_provider(
        _config(base_url="http://127.0.0.1:9999"), runner=runner, opener=opener
    )
    assert "9999:4416" in runner.calls[1]


def test_ensure_provider_reports_failure_without_raising():
    """Docker missing entirely must degrade, not raise."""

    def missing_docker(argv, **kwargs):
        raise FileNotFoundError("docker")

    assert (
        pot.ensure_provider(
            _config(), runner=missing_docker, opener=FakeOpener(up=False)
        )
        is False
    )


def test_ensure_provider_gives_up_when_started_container_never_answers(monkeypatch):
    """A container that starts but never listens must not hang the daemon."""
    monkeypatch.setattr(pot, "STARTUP_TIMEOUT", 0.0)
    opener = FakeOpener(up=False)
    runner = FakeRunner(start_succeeds=True)
    assert pot.ensure_provider(_config(), runner=runner, opener=opener) is False


def test_ensure_provider_does_nothing_when_disabled():
    runner = FakeRunner()
    opener = FakeOpener()
    assert (
        pot.ensure_provider(_config(enabled=False), runner=runner, opener=opener)
        is False
    )
    assert runner.calls == []
    assert opener.urls == []


# -- yt-dlp options ---------------------------------------------------------


def test_ydl_opts_points_the_plugin_at_the_configured_base_url():
    opts = pot.ydl_opts(_config(base_url="http://127.0.0.1:9999"))
    assert opts["extractor_args"]["youtubepot-bgutilhttp"]["base_url"] == [
        "http://127.0.0.1:9999"
    ]


def test_ydl_opts_empty_when_disabled():
    assert pot.ydl_opts(_config(enabled=False)) == {}


def test_resolve_ydl_opts_carry_the_provider_config(monkeypatch):
    monkeypatch.setattr(pot, "_pot_config", lambda config=None: _config()["pot"])
    opts = resolve.ydl_opts("SID=fake")
    assert opts["format"] == "bestaudio"
    assert opts["http_headers"]["Cookie"] == "SID=fake"
    assert opts["extractor_args"]["youtubepot-bgutilhttp"]["base_url"] == [BASE_URL]


def test_resolve_ydl_opts_unaffected_when_provider_disabled(monkeypatch):
    monkeypatch.setattr(
        pot, "_pot_config", lambda config=None: _config(enabled=False)["pot"]
    )
    opts = resolve.ydl_opts(None)
    assert "extractor_args" not in opts


def test_resolution_still_falls_back_when_provider_is_down(monkeypatch):
    """The provider being unreachable must not change the fallback chain."""
    monkeypatch.setattr(pot, "_pot_config", lambda config=None: _config()["pot"])
    monkeypatch.setattr(pot, "is_reachable", lambda *a, **k: False)
    from ytm import auth

    monkeypatch.setattr(auth, "load_cookies", lambda: "SID=fake")
    assert resolve.cookie_attempts() == ["SID=fake", None]
