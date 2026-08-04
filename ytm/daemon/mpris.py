"""MPRIS D-Bus interface for the daemon.

Exposes the daemon on the session bus as ``org.mpris.MediaPlayer2.ytm`` so
the desktop's media keys and tools like ``playerctl`` can control playback
without knowing anything about the daemon's own socket protocol.

Deliberately minimal: only the ``org.mpris.MediaPlayer2`` and
``org.mpris.MediaPlayer2.Player`` interfaces, and only the members actually
needed for playback control (no TrackList, no Playlists, no LoopStatus,
Shuffle or Rate).

D-Bus is never a hard dependency: if the ``dbus-next`` package is missing,
if there is no session bus (headless/CI) or if registration otherwise fails,
:func:`register` logs a warning and returns ``None`` -- the daemon must
start and serve normally either way.
"""

import contextlib
import logging

logger = logging.getLogger(__name__)

BUS_NAME = "org.mpris.MediaPlayer2.ytm"
OBJECT_PATH = "/org/mpris/MediaPlayer2"

try:
    from dbus_next import Variant
    from dbus_next.aio import MessageBus
    from dbus_next.service import ServiceInterface, method, dbus_property, PropertyAccess

    DBUS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the "no dbus-next" test
    DBUS_AVAILABLE = False


def _status(daemon):
    """The daemon's status snapshot, as used for the MPRIS properties."""
    return daemon._status_data()


def _playback_status(status):
    if status["current"] is None:
        return "Stopped"
    return "Paused" if status["paused"] else "Playing"


def _metadata(status):
    track = status["current"]
    metadata = {
        "mpris:trackid": Variant("s", "/org/mpris/MediaPlayer2/Track/0"),
        "mpris:length": Variant("x", 0),
        "xesam:title": Variant("s", ""),
        "xesam:artist": Variant("as", []),
        "xesam:album": Variant("s", ""),
    }
    if track is not None:
        metadata["mpris:trackid"] = Variant(
            "s", f"/org/mpris/MediaPlayer2/Track/{track['video_id']}"
        )
        metadata["mpris:length"] = Variant(
            "x", int((track.get("duration_seconds") or 0) * 1_000_000)
        )
        metadata["xesam:title"] = Variant("s", track.get("title") or "")
        artist = track.get("artist") or ""
        metadata["xesam:artist"] = Variant("as", [artist] if artist else [])
        metadata["xesam:album"] = Variant("s", track.get("album") or "")
    return metadata


if DBUS_AVAILABLE:

    class _RootInterface(ServiceInterface):
        """``org.mpris.MediaPlayer2`` -- the app-identity half of the spec."""

        def __init__(self):
            super().__init__("org.mpris.MediaPlayer2")

        @method()
        def Raise(self):
            pass

        @method()
        def Quit(self):
            pass

        @dbus_property(access=PropertyAccess.READ)
        def CanQuit(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def CanRaise(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def HasTrackList(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def Identity(self) -> "s":
            return "ytm"

        @dbus_property(access=PropertyAccess.READ)
        def DesktopEntry(self) -> "s":
            return "ytm"

        @dbus_property(access=PropertyAccess.READ)
        def SupportedUriSchemes(self) -> "as":
            return []

        @dbus_property(access=PropertyAccess.READ)
        def SupportedMimeTypes(self) -> "as":
            return []

    class _PlayerInterface(ServiceInterface):
        """``org.mpris.MediaPlayer2.Player`` -- delegates to the daemon."""

        def __init__(self, daemon):
            super().__init__("org.mpris.MediaPlayer2.Player")
            self._daemon = daemon

        async def _run(self, cmd):
            await self._daemon.handle_request({"cmd": cmd, "args": {}})

        @method()
        async def Play(self):
            await self._run("resume")

        @method()
        async def Pause(self):
            await self._run("pause")

        @method()
        async def PlayPause(self):
            await self._run("toggle")

        @method()
        async def Stop(self):
            await self._run("pause")

        @method()
        async def Next(self):
            await self._run("next")

        @method()
        async def Previous(self):
            await self._run("prev")

        @dbus_property(access=PropertyAccess.READ)
        def PlaybackStatus(self) -> "s":
            return _playback_status(_status(self._daemon))

        @dbus_property(access=PropertyAccess.READ)
        def Metadata(self) -> "a{sv}":
            return _metadata(_status(self._daemon))

        @dbus_property(access=PropertyAccess.READ)
        def Volume(self) -> "d":
            return _status(self._daemon)["volume"] / 100.0

        @dbus_property(access=PropertyAccess.READ)
        def Position(self) -> "x":
            return int(_status(self._daemon)["position"] * 1_000_000)

        @dbus_property(access=PropertyAccess.READ)
        def CanPlay(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanPause(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanGoNext(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanGoPrevious(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanSeek(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def CanControl(self) -> "b":
            return True


class Mpris:
    """Owns the D-Bus connection and keeps its properties in sync.

    Constructed only on a successful :func:`register`; every method here
    assumes ``dbus-next`` is importable and a bus connection was made.
    """

    def __init__(self, bus, root, player):
        self._bus = bus
        self._root = root
        self._player = player

    def on_daemon_event(self, event, data):
        """Reflect a daemon event (`track_changed`, `state_changed`,
        `position`) as an MPRIS PropertiesChanged signal."""
        try:
            if event == "track_changed":
                self._player.emit_properties_changed(
                    {
                        "Metadata": _metadata(_status(self._player._daemon)),
                        "PlaybackStatus": _playback_status(
                            _status(self._player._daemon)
                        ),
                    }
                )
            elif event == "state_changed":
                self._player.emit_properties_changed(
                    {
                        "PlaybackStatus": _playback_status(
                            _status(self._player._daemon)
                        ),
                        "Volume": _status(self._player._daemon)["volume"] / 100.0,
                    }
                )
            elif event == "position":
                self._player.emit_properties_changed(
                    {"Position": int((data.get("position") or 0) * 1_000_000)}
                )
        except Exception:  # never let a D-Bus hiccup break the daemon
            logger.warning("mpris: failed to publish %s", event, exc_info=True)

    async def close(self):
        """Release the bus name and disconnect."""
        with contextlib.suppress(Exception):
            await self._bus.release_name(BUS_NAME)
        with contextlib.suppress(Exception):
            self._bus.disconnect()


async def register(daemon):
    """Publish `daemon` on the session bus; ``None`` if that isn't possible.

    Never raises: any failure (missing dbus-next, no session bus, name
    already taken, ...) is logged as a warning and the daemon carries on
    without MPRIS support.
    """
    if not DBUS_AVAILABLE:
        logger.warning("mpris: dbus-next is not installed, skipping MPRIS")
        return None
    try:
        bus = await MessageBus().connect()
        root = _RootInterface()
        player = _PlayerInterface(daemon)
        bus.export(OBJECT_PATH, root)
        bus.export(OBJECT_PATH, player)
        await bus.request_name(BUS_NAME)
    except Exception:
        logger.warning("mpris: could not register on the session bus", exc_info=True)
        return None
    return Mpris(bus, root, player)
