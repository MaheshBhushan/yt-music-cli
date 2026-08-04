"""Tests for ytm.cache, with yt-dlp fully mocked (no network)."""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ytm import cache


class FakeYDL:
    """Writes a fake audio file to the outtmpl location on download."""

    content = b"fake-audio-bytes"

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download=False):
        outtmpl = self.opts["outtmpl"]
        path = outtmpl.replace("%(ext)s", "m4a")
        with open(path, "wb") as f:
            f.write(self.content)
        return {"url": url}


class ExplodingYDL(FakeYDL):
    """Writes a partial file then raises, simulating an interrupted download."""

    def extract_info(self, url, download=False):
        outtmpl = self.opts["outtmpl"]
        path = outtmpl.replace("%(ext)s", "m4a.part")
        with open(path, "wb") as f:
            f.write(b"only-half-the-a")
        raise RuntimeError("network died mid-download")


@pytest.fixture(autouse=True)
def no_cookies(monkeypatch):
    from ytm import auth

    monkeypatch.setattr(
        auth, "load_cookies", lambda: (_ for _ in ()).throw(auth.AuthMissing("none"))
    )


def test_uncached_video_reports_not_cached(tmp_path):
    assert cache.is_cached("abc123", cache_dir=tmp_path) is False
    assert cache.get_cached_path("abc123", cache_dir=tmp_path) is None


def test_download_produces_a_cached_file(tmp_path):
    path = cache.download("abc123", cache_dir=tmp_path, ydl_class=FakeYDL)
    assert path.exists()
    assert path.read_bytes() == FakeYDL.content
    assert cache.is_cached("abc123", cache_dir=tmp_path)
    assert cache.get_cached_path("abc123", cache_dir=tmp_path) == path


def test_interrupted_download_leaves_no_cache_entry(tmp_path):
    with pytest.raises(RuntimeError):
        cache.download("abc123", cache_dir=tmp_path, ydl_class=ExplodingYDL)
    assert cache.is_cached("abc123", cache_dir=tmp_path) is False
    assert cache.get_cached_path("abc123", cache_dir=tmp_path) is None
    # no partial file anywhere under the cache dir
    leftovers = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert leftovers == []


def test_cache_aware_resolver_skips_fallback_when_cached(tmp_path):
    cache.download("cached-id", cache_dir=tmp_path, ydl_class=FakeYDL)

    calls = []

    def fallback(video_id):
        calls.append(video_id)
        return f"https://stream.test/{video_id}"

    resolver = cache.cache_aware_resolver(fallback=fallback, cache_dir=tmp_path)
    result = resolver("cached-id")

    assert result == str(cache.get_cached_path("cached-id", cache_dir=tmp_path, touch=False))
    assert calls == []


def test_cache_aware_resolver_falls_through_when_uncached(tmp_path):
    calls = []

    def fallback(video_id):
        calls.append(video_id)
        return f"https://stream.test/{video_id}"

    resolver = cache.cache_aware_resolver(fallback=fallback, cache_dir=tmp_path)
    result = resolver("not-cached-id")

    assert result == "https://stream.test/not-cached-id"
    assert calls == ["not-cached-id"]


def test_lru_eviction_drops_least_recently_used(tmp_path):
    path_a = cache.download("a", cache_dir=tmp_path, ydl_class=FakeYDL)
    time.sleep(0.01)
    path_b = cache.download("b", cache_dir=tmp_path, ydl_class=FakeYDL)

    # touch "a" (mark it recently used) so "b" becomes the LRU entry
    time.sleep(0.01)
    cache.get_cached_path("a", cache_dir=tmp_path)

    size_each = path_a.stat().st_size
    cap = size_each + (size_each // 2)  # room for only one entry
    evicted = cache.enforce_cap(cap, cache_dir=tmp_path)

    assert evicted == ["b"]
    assert cache.is_cached("a", cache_dir=tmp_path)
    assert not cache.is_cached("b", cache_dir=tmp_path)


def test_download_enforces_cap_immediately(tmp_path):
    path_a = cache.download("a", cache_dir=tmp_path, ydl_class=FakeYDL)
    size_each = path_a.stat().st_size
    time.sleep(0.01)

    cache.download("b", cache_dir=tmp_path, ydl_class=FakeYDL, cap_bytes=size_each + 1)

    assert cache.is_cached("b", cache_dir=tmp_path)
    assert not cache.is_cached("a", cache_dir=tmp_path)


def test_remove_deletes_entry(tmp_path):
    cache.download("abc123", cache_dir=tmp_path, ydl_class=FakeYDL)
    assert cache.remove("abc123", cache_dir=tmp_path) is True
    assert cache.is_cached("abc123", cache_dir=tmp_path) is False
    assert cache.remove("abc123", cache_dir=tmp_path) is False


def test_list_cached_reports_entries(tmp_path):
    cache.download("a", cache_dir=tmp_path, ydl_class=FakeYDL)
    cache.download("b", cache_dir=tmp_path, ydl_class=FakeYDL)
    entries = cache.list_cached(cache_dir=tmp_path)
    video_ids = {e["video_id"] for e in entries}
    assert video_ids == {"a", "b"}
    for e in entries:
        assert e["size"] == len(FakeYDL.content)
