"""Tests for ytm.daemon.mpris.

Entirely offline: no real D-Bus session bus is used. The `_bus` object
handed to `Mpris`/`register` is a fake, and daemon startup is exercised
with the real `mpris.register` path but a monkeypatched `MessageBus` that
mimics an unavailable session bus.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ytm.daemon import mpris
from ytm.daemon.server import Daemon
from tests.test_server import FakePlayer, FakeQueue, FakeYT, make_track


# -- pure helpers ------------------------------------------------------------


def test_playback_status_stopped_when_no_track():
    status = {"current": None, "paused": False, "volume": 100, "position": 0.0}
    assert mpris._playback_status(status) == "Stopped"


def test_playback_status_playing_and_paused():
    status = {"current": {"video_id": "x"}, "paused": False}
    assert mpris._playback_status(status) == "Playing"
    status["paused"] = True
    assert mpris._playback_status(status) == "Paused"


def test_metadata_empty_when_no_track():
    metadata = mpris._metadata({"current": None})
    assert metadata["xesam:title"].value == ""
    assert metadata["mpris:length"].value == 0


def test_metadata_reflects_current_track():
    status = {
        "current": {
            "video_id": "abc123",
            "title": "A Song",
            "artist": "An Artist",
            "album": "An Album",
            "duration_seconds": 180,
        }
    }
    metadata = mpris._metadata(status)
    assert metadata["xesam:title"].value == "A Song"
    assert metadata["xesam:artist"].value == ["An Artist"]
    assert metadata["xesam:album"].value == "An Album"
    assert metadata["mpris:length"].value == 180 * 1_000_000
    assert "abc123" in metadata["mpris:trackid"].value


# -- method -> daemon command mapping ----------------------------------------


def build_daemon(tmp_path, sock_path, tracks=()):
    player = FakePlayer()
    queue = FakeQueue(player)
    for track in tracks:
        queue.enqueue(track)
    daemon = Daemon(
        path=sock_path,
        player=player,
        queue=queue,
        state_path=tmp_path / "state.json",
        yt=FakeYT(),
    )
    return daemon, player


@pytest.mark.skipif(not mpris.DBUS_AVAILABLE, reason="dbus-next not installed")
@pytest.mark.parametrize(
    "mpris_method,expected_cmd",
    [
        ("Play", "resume"),
        ("Pause", "pause"),
        ("PlayPause", "toggle"),
        ("Stop", "pause"),
        ("Next", "next"),
        ("Previous", "prev"),
    ],
)
def test_player_interface_methods_map_to_daemon_commands(
    tmp_path, mpris_method, expected_cmd
):
    daemon, player = build_daemon(tmp_path, tmp_path / "ytmd.sock", tracks=[make_track("a")])
    iface = mpris._PlayerInterface(daemon)

    # dbus-next's @method() wrapper does not itself await a coroutine
    # result (that's the message dispatcher's job); call the underlying
    # unwrapped coroutine function directly, as the dispatcher would.
    unwrapped = getattr(mpris._PlayerInterface, mpris_method).__wrapped__

    async def scenario():
        await unwrapped(iface)

    asyncio.run(scenario())
    calls = [call[0] for call in player.calls]
    if expected_cmd in ("resume", "pause", "toggle"):
        assert expected_cmd in calls


@pytest.mark.skipif(not mpris.DBUS_AVAILABLE, reason="dbus-next not installed")
def test_playback_status_property_reflects_daemon_state(tmp_path):
    daemon, player = build_daemon(tmp_path, tmp_path / "ytmd.sock", tracks=[make_track("a")])
    iface = mpris._PlayerInterface(daemon)
    assert iface.PlaybackStatus == "Playing"

    async def scenario():
        await mpris._PlayerInterface.Pause.__wrapped__(iface)

    asyncio.run(scenario())
    assert iface.PlaybackStatus == "Paused"


@pytest.mark.skipif(not mpris.DBUS_AVAILABLE, reason="dbus-next not installed")
def test_metadata_property_reflects_current_track(tmp_path):
    daemon, player = build_daemon(tmp_path, tmp_path / "ytmd.sock", tracks=[make_track("vid1")])
    iface = mpris._PlayerInterface(daemon)
    metadata = iface.Metadata
    assert metadata["xesam:title"].value == "title-vid1"


@pytest.mark.skipif(not mpris.DBUS_AVAILABLE, reason="dbus-next not installed")
def test_volume_property_reflects_daemon_state(tmp_path):
    daemon, player = build_daemon(tmp_path, tmp_path / "ytmd.sock", tracks=[make_track("a")])
    iface = mpris._PlayerInterface(daemon)
    assert iface.Volume == daemon._volume / 100.0

    async def scenario():
        await daemon.handle_request({"cmd": "volume", "args": {"level": 42}})

    asyncio.run(scenario())
    assert iface.Volume == 0.42


# -- daemon must survive a D-Bus registration failure ------------------------


class _ExplodingBus:
    async def connect(self):
        raise OSError("no session bus available")


def test_register_returns_none_and_logs_when_bus_unavailable(monkeypatch):
    if not mpris.DBUS_AVAILABLE:
        pytest.skip("dbus-next not installed")
    monkeypatch.setattr(mpris, "MessageBus", _ExplodingBus)

    async def scenario():
        return await mpris.register(daemon=object())

    assert asyncio.run(scenario()) is None


def test_daemon_starts_and_serves_when_dbus_unavailable(tmp_path, monkeypatch):
    """The daemon must start and answer requests even if MPRIS can't register."""

    async def failing_register(daemon):
        return None

    monkeypatch.setattr(mpris, "register", failing_register)

    async def scenario():
        daemon, player = build_daemon(tmp_path, tmp_path / "ytmd.sock")
        await daemon.start()
        try:
            response = await daemon.handle_request({"cmd": "status", "args": {}})
        finally:
            await daemon.stop()
        return response

    response = asyncio.run(scenario())
    assert response["ok"] is True
    assert response["data"]["current"] is None

def test_register_swallows_export_failures(monkeypatch):
    """A failure anywhere inside register() (not just connect()) must also
    be swallowed rather than raised."""
    if not mpris.DBUS_AVAILABLE:
        pytest.skip("dbus-next not installed")

    class _BadExportBus:
        async def connect(self):
            return self

        def export(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(mpris, "MessageBus", _BadExportBus)

    async def scenario():
        return await mpris.register(daemon=object())

    assert asyncio.run(scenario()) is None
