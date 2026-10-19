"""Offline player and catalogue shared by HTTP and browser integration tests."""

import argparse
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests.test_cli_core import FakePlayer, track
from ytm import cache, music, state
from ytm.panel import create_server
from ytm.tui.backend import Backend, BackendError

CATALOGUE = [
    track("fixture0001", "Blue Hour", "Paper Satellites", "Evening", 180),
    track("fixture0002", "Night Drive", "Paper Satellites", "Evening", 240),
    track("fixture0003", "Morning Tide", "Coastlines", "Daybreak", 210),
]


def search(query, limit=20):
    if query == "offline":
        raise BackendError("Fixture catalogue is offline")
    if query == "empty":
        return []
    return CATALOGUE[:limit]


class PanelPlayer(FakePlayer):
    """Model the mpv operations used by the panel without starting audio."""

    def play(self, url, title=None):
        self.entries.clear()  # mpv loadfile replaces the playlist
        self.pos = 0
        self.paused = False
        super().play(url, title)

    def play_index(self, index):
        for i, entry in enumerate(self.entries):
            entry["current"] = i == index
        self.paused = False
        self.pos = 0

    def next(self):
        index = self.status()["index"] + 1
        if index < len(self.entries):
            self.play_index(index)

    def prev(self):
        index = self.status()["index"] - 1
        if index >= 0:
            self.play_index(index)

    def get(self, name, default=None):
        return self.status()["index"] if name == "playlist-pos" else default

    def seek(self, seconds, absolute=False):
        self.pos = max(0, seconds if absolute else self.pos + seconds)

    def remove(self, index):
        entry = self.entries.pop(index)
        if entry["current"] and self.entries:
            self.play_index(min(index, len(self.entries) - 1))

    def move(self, from_index, to_index):
        self.entries.insert(to_index, self.entries.pop(from_index))

    def clear(self):
        self.entries = [entry for entry in self.entries if entry["current"]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    with (tempfile.TemporaryDirectory(prefix="ytm-panel-test-") as tmp,
          patch.object(state, "STATE_PATH", Path(tmp) / "state.json"),
          patch.object(cache, "DEFAULT_CACHE_DIR", Path(tmp) / "audio"),
          patch.object(music, "search", search)):
        backend = Backend(player_factory=lambda **kwargs: PanelPlayer())
        try:
            with create_server(backend, Path(__file__).resolve().parents[1] / "control-panel/dist", args.port) as server:
                print(f"Offline panel fixture ready on {server.server_port}", flush=True)
                server.serve_forever()
        finally:
            backend.close()


if __name__ == "__main__":
    main()
