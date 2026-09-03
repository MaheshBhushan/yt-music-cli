"""Tests for personal daily mixes (Supermix, Discover Mix, ...) from the
home feed, using a real captured `get_home` fixture."""

import json
from pathlib import Path

from ytm import music

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "home_mixes.json").read_text())


class FakeYT:
    def __init__(self, shelves):
        self.shelves = shelves

    def get_home(self, limit=40):
        return self.shelves


def test_mixes_dedupes_and_skips_none_and_non_mix_ids():
    result = music.mixes(yt=FakeYT(FIXTURE))
    ids = [p.playlist_id for p in result]
    names = [p.title for p in result]
    # every id must be a mix id, appear once, and in feed order
    assert all(pid.startswith("RDTMAK") for pid in ids)
    assert len(ids) == len(set(ids))
    assert "Archive Mix" in names
    assert "Discover Mix" in names
    assert "New Release Mix" in names
    # non-mix contents (Liked Music, plain songs, and None entries) are skipped
    assert "Liked Music" not in names
    assert "My Band" not in names
    # feed order: shelf order, then item order within a shelf
    assert names.index("Archive Mix") < names.index("Discover Mix")
    # track count is unknown for a mix until it is fetched
    assert all(p.track_count is None for p in result)


def test_mixes_tolerates_none_shelf_and_content_entries():
    shelves = [None, {"title": "x", "contents": [None, {"playlistId": "RDTMAKfoo", "title": "Foo Mix"}]}]
    result = music.mixes(yt=FakeYT(shelves))
    assert [p.title for p in result] == ["Foo Mix"]
