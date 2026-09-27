"""Real mpv decoding on Windows, isolated player and silent audio output.

This proves local media playback, not audible desktop audio or YouTube login.
"""
import sys
import time
import wave

import pytest
from ytm.lifecycle import ProcessOwner
from ytm.player import Player, find_mpv

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='native Windows smoke')


def test_real_mpv_decodes_and_plays_local_wave(tmp_path):
    mpv = find_mpv()
    if mpv is None:
        pytest.fail('Windows smoke job must install mpv')
    audio = tmp_path/'test audio é.wav'
    with wave.open(str(audio),'wb') as file:
        file.setnchannels(1); file.setsampwidth(2); file.setframerate(8000)
        file.writeframes(b'\0\0' * 8000 * 8)
    import uuid
    owner=ProcessOwner()
    player=None
    try:
        player=Player(ipc_path=rf'\\.\pipe\ytm-smoke-{uuid.uuid4().hex}', owner=owner,
                      mpv_bin=mpv, extra_args=['--no-config','--ao=null','--vo=null'])
        player.play(str(audio))
        until=time.monotonic()+15
        position=None
        while time.monotonic()<until:
            position=player.get('time-pos')
            if position is not None and position > .1:
                break
            time.sleep(.05)
        assert position is not None and position > .1
        assert player.get('duration') > 7
        player.pause()
        assert player.get('pause') is True
        player.resume()
        assert player.get('pause') is False
    finally:
        if player is not None: player.close()
        owner.close()
    assert all(p.poll() is not None for p in owner._processes)
