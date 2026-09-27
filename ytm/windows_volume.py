"""Windows default-render-endpoint volume via Core Audio.

COM objects are acquired and released on the calling thread. Resolving the
endpoint on every operation follows device switches without sharing COM
pointers between the command and mixer watcher threads. Polling observes
external volume changes; it never changes the user's mute state.
"""
from contextlib import contextmanager


@contextmanager
def core_audio_endpoint():
    import comtypes
    from pycaw.pycaw import AudioUtilities

    comtypes.CoInitialize()
    device = endpoint = None
    try:
        device = AudioUtilities.GetSpeakers()
        if device is None:
            raise OSError("No default Windows audio output")
        endpoint = device.EndpointVolume
        yield endpoint
    finally:
        endpoint = device = None
        comtypes.CoUninitialize()


class WindowsVolume:
    owner = None  # matches the mixer protocol; no subprocess ownership needed

    def __init__(self, endpoint_factory=core_audio_endpoint, poll_interval=1.0):
        self._endpoint_factory = endpoint_factory
        self._poll_interval = poll_interval

    def get(self):
        try:
            with self._endpoint_factory() as endpoint:
                return round(endpoint.GetMasterVolumeLevelScalar() * 100)
        except Exception:  # COM/device loss is an expected fallback, never a traceback
            return None

    def set(self, level):
        level = max(0, min(100, round(level)))
        try:
            with self._endpoint_factory() as endpoint:
                endpoint.SetMasterVolumeLevelScalar(level / 100, None)
                return round(endpoint.GetMasterVolumeLevelScalar() * 100)
        except Exception:
            return None

    def watch(self, callback, stop):
        last = self.get()
        while not stop.wait(self._poll_interval):
            current = self.get()
            if current is not None and current != last:
                last = current
                callback(current)
