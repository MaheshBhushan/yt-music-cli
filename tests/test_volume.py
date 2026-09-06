"""Tests for ytm.volume (the system mixer) and how the Player, backend and
config use it. No wpctl/pactl is ever run: the subprocess runner is faked."""

import subprocess
import threading

import pytest

from ytm import config as config_mod
from ytm.tui.backend import Backend
from ytm.volume import SystemVolume
from tests.test_cli_core import FakePlayer


class Runner:
    """Stands in for subprocess.run: answers wpctl/pactl from a fake sink."""

    def __init__(self, level=55, wpctl=True, pactl=True, fail=False):
        self.level = level
        self.calls = []
        self.fail = fail
        self.wpctl = wpctl
        self.pactl = pactl

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if self.fail:
            return subprocess.CompletedProcess(argv, 1, "", "no sink")
        tool = argv[0]
        if tool == "wpctl":
            if argv[1] == "get-volume":
                return subprocess.CompletedProcess(argv, 0, f"Volume: {self.level / 100:.2f}\n", "")
            self.level = int(round(float(argv[-1]) * 100))
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1] == "get-sink-volume":
            return subprocess.CompletedProcess(
                argv, 0, f"Volume: front-left: 36045 / {self.level:3d}% / -15.58 dB,   front-right: 36045 / {self.level:3d}% / -15.58 dB\n", ""
            )
        self.level = int(argv[-1].rstrip("%"))
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_detect_needs_wpctl_or_pactl():
    assert SystemVolume.detect(which=lambda name: None) is None
    only_pactl = SystemVolume.detect(which=lambda name: "/usr/bin/pactl" if name == "pactl" else None)
    assert only_pactl._pactl == "/usr/bin/pactl" and only_pactl._wpctl is None


def test_wpctl_reads_and_sets_the_default_sink():
    run = Runner(level=55)
    mixer = SystemVolume(wpctl="wpctl", pactl="pactl", run=run)
    assert mixer.get() == 55
    assert mixer.set(70) == 70
    assert ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "0.70"] in run.calls
    # clamped
    assert mixer.set(140) == 100
    assert mixer.set(-5) == 0


def test_wpctl_muted_output_still_parses():
    class Muted(Runner):
        def __call__(self, argv, **kwargs):
            if argv[1] == "get-volume":
                return subprocess.CompletedProcess(argv, 0, "Volume: 0.40 [MUTED]\n", "")
            return super().__call__(argv, **kwargs)

    assert SystemVolume(wpctl="wpctl", run=Muted()).get() == 40


def test_pactl_alone_reads_and_sets_percentages():
    run = Runner(level=30)
    mixer = SystemVolume(pactl="pactl", run=run)
    assert mixer.get() == 30
    assert mixer.set(45) == 45
    assert ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "45%"] in run.calls


def test_a_failing_mixer_command_reads_as_none():
    mixer = SystemVolume(wpctl="wpctl", pactl="pactl", run=Runner(fail=True))
    assert mixer.get() is None
    # set falls back to the requested level when nothing can be read back
    assert mixer.set(33) == 33


def test_a_missing_binary_reads_as_none():
    def run(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    assert SystemVolume(wpctl="wpctl", run=run).get() is None


# -- Player with a mixer --------------------------------------------------------


class FakeMixer:
    def __init__(self, level=55):
        self.level = level
        self.sets = []

    def get(self):
        return self.level

    def set(self, level):
        self.sets.append(level)
        self.level = int(level)
        return self.level

    def watch(self, callback, stop):
        self.callback = callback
        stop.wait()


def test_player_with_mixer_uses_system_volume_and_pins_mpv_to_100(tmp_path):
    from tests.test_mpv_player import FakeMpv
    from ytm.player import Player

    mpv = FakeMpv(str(tmp_path / "mpv.sock"))
    mixer = FakeMixer(55)
    with Player(ipc_path=mpv.path, spawn=False, mixer=mixer) as p:
        assert p.volume() == 55
        assert p.status()["volume"] == 55
        assert p.volume(60) == 60
        assert mixer.sets == [60]
        # mpv started at 70 (an old config): the first set pins it to 100
        assert mpv.props["volume"] == 100
        assert p.volume() == 60


def test_player_without_mixer_keeps_mpv_volume(tmp_path):
    from tests.test_mpv_player import FakeMpv
    from ytm.player import Player

    mpv = FakeMpv(str(tmp_path / "mpv.sock"))
    with Player(ipc_path=mpv.path, spawn=False) as p:
        assert p.volume() == 70
        assert p.volume(40) == 40
        assert mpv.props["volume"] == 40


def test_player_falls_back_to_mpv_volume_when_the_mixer_is_silent(tmp_path):
    from tests.test_mpv_player import FakeMpv
    from ytm.player import Player

    class Silent(FakeMixer):
        def get(self):
            return None

    mpv = FakeMpv(str(tmp_path / "mpv.sock"))
    with Player(ipc_path=mpv.path, spawn=False, mixer=Silent()) as p:
        assert p.volume() == 70


# -- backend: system changes become TUI events -----------------------------------


def test_listen_reports_system_volume_changes_as_state_changed(monkeypatch, tmp_path):
    from ytm import state
    from ytm.player import PlayerError

    monkeypatch.setattr(state, "STATE_PATH", tmp_path / "session.json")
    fake = FakePlayer()
    fake.mixer = FakeMixer(55)
    backend = Backend(player_factory=lambda spawn=True, timeout=None: fake)
    watching = threading.Event()

    def watch(callback, stop):
        fake.mixer.callback = callback
        watching.set()
        stop.wait()

    fake.mixer.watch = watch

    class Observer:
        def observe(self, *names):
            assert watching.wait(2)
            fake.mixer.callback(60)  # the media key was pressed
            yield ("pause", False)
            backend.close()
            raise PlayerError("closed")

        def close(self):
            pass

    backend._make_player = lambda spawn=True, timeout=None: Observer()
    events = []
    backend.on_event(lambda e, d: events.append((e, d)))
    backend._closed = False
    backend.listen()
    assert events[0] == ("state_changed", {"paused": False, "volume": 60})
    # the watcher was told to stop when listen returned
    for t in threading.enumerate():
        if t.name == "ytm-mixer":
            t.join(2)
            assert not t.is_alive()


# -- config -----------------------------------------------------------------------


def test_audio_control_accepts_system_or_player(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text('[audio]\ncontrol = "player"\n')
    assert config_mod.load(path)["audio"]["control"] == "player"
    path.write_text('[audio]\ncontrol = "mpv"\n')
    assert config_mod.load(path)["audio"]["control"] == "system"
    assert "audio.control" in capsys.readouterr().err
