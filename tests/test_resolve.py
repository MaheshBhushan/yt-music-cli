"""Tests for ytm.resolve, with yt-dlp fully mocked (no network)."""

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ytm import auth, resolve


class FakeYDL:
    """Records the opts it was constructed with; returns a fake stream url."""

    last_opts = None
    last_url_arg = None

    def __init__(self, opts):
        FakeYDL.last_opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download=False):
        FakeYDL.last_url_arg = url
        return {"url": "https://example.com/audio.stream?sig=abc123"}


def test_resolve_returns_audio_url(monkeypatch):
    monkeypatch.setattr(auth, "load_cookies", lambda: (_ for _ in ()).throw(auth.AuthMissing("none")))
    url = resolve.resolve_stream_url("qD53-RZpTOc", ydl_class=FakeYDL)
    assert url == "https://example.com/audio.stream?sig=abc123"
    assert FakeYDL.last_opts["format"] == "bestaudio"


def test_cookies_passed_through_when_present(monkeypatch):
    monkeypatch.setattr(auth, "load_cookies", lambda: "SID=fake; HSID=fake")
    resolve.resolve_stream_url("qD53-RZpTOc", ydl_class=FakeYDL)
    assert FakeYDL.last_opts["http_headers"]["Cookie"] == "SID=fake; HSID=fake"


def test_resolution_attempted_without_cookies(monkeypatch):
    monkeypatch.setattr(auth, "load_cookies", lambda: (_ for _ in ()).throw(auth.AuthMissing("none")))
    url = resolve.resolve_stream_url("qD53-RZpTOc", ydl_class=FakeYDL)
    assert "http_headers" not in FakeYDL.last_opts
    assert url


def test_stale_version_warns_above_threshold(monkeypatch, capsys):
    monkeypatch.setattr(resolve.yt_dlp.version, "__version__", "2020.01.01")
    resolve.check_ytdlp_freshness(now=datetime(2020, 3, 1))
    assert "warning" in capsys.readouterr().err.lower()


def test_fresh_version_does_not_warn(monkeypatch, capsys):
    monkeypatch.setattr(resolve.yt_dlp.version, "__version__", "2020.01.01")
    resolve.check_ytdlp_freshness(now=datetime(2020, 1, 10))
    assert capsys.readouterr().err == ""


def test_resolved_url_never_written_to_disk(monkeypatch, tmp_path):
    """No code path in resolve.py touches the filesystem while resolving."""
    monkeypatch.setattr(auth, "load_cookies", lambda: (_ for _ in ()).throw(auth.AuthMissing("none")))
    monkeypatch.chdir(tmp_path)

    real_open = open

    def guarded_open(path, *args, **kwargs):
        raise AssertionError(f"resolve.py must not open files, tried: {path}")

    import builtins

    monkeypatch.setattr(builtins, "open", guarded_open)
    try:
        url = resolve.resolve_stream_url("qD53-RZpTOc", ydl_class=FakeYDL)
    finally:
        monkeypatch.setattr(builtins, "open", real_open)
    assert url
    assert list(tmp_path.iterdir()) == []
