"""Core Audio behavior without changing the developer's actual volume."""
from contextlib import contextmanager
import threading

from ytm.volume import SystemVolume
from ytm.windows_volume import WindowsVolume


class Endpoint:
    def __init__(self, level):
        self.level = level
        self.writes = []

    def GetMasterVolumeLevelScalar(self):
        return self.level

    def SetMasterVolumeLevelScalar(self, level, context):
        self.writes.append(level)
        self.level = level


def test_windows_endpoint_is_reacquired_and_thread_local():
    devices = [Endpoint(.3), Endpoint(.8), Endpoint(.4)]
    entered = []
    @contextmanager
    def factory():
        entered.append(threading.get_ident())
        yield devices.pop(0)
    mixer = WindowsVolume(factory)
    assert mixer.get() == 30
    assert mixer.get() == 80  # changed default output
    endpoint = devices[0]
    assert mixer.set(150) == 100
    assert endpoint.writes == [1.0]
    assert len(entered) == 3


def test_device_loss_returns_no_value():
    @contextmanager
    def absent():
        raise OSError('no endpoint')
        yield
    mixer = WindowsVolume(absent)
    assert mixer.get() is None
    assert mixer.set(40) is None


def test_detection_uses_windows_backend_and_falls_back(monkeypatch):
    monkeypatch.setattr(WindowsVolume, 'get', lambda self: 45)
    assert isinstance(SystemVolume.detect(platform='win32'), WindowsVolume)
    monkeypatch.setattr(WindowsVolume, 'get', lambda self: None)
    assert SystemVolume.detect(platform='win32') is None


def test_watcher_observes_external_change_and_stops():
    stop = threading.Event()
    values = iter([20, 20, None, 60])
    mixer = WindowsVolume(poll_interval=.001)
    mixer.get = lambda: next(values)
    seen = []
    def changed(value):
        seen.append(value)
        stop.set()
    mixer.watch(changed, stop)
    assert seen == [60]
