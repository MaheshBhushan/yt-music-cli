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


def test_real_updater_upgrades_disposable_installation(tmp_path):
    """Real pip installs, real PowerShell helper, no live installation touched."""
    import json
    import shutil
    import subprocess
    from importlib.metadata import version
    from pathlib import Path
    from ytm import update

    def run(argv, **kwargs):
        return subprocess.run(argv, check=True, capture_output=True, text=True,
                              timeout=180, **kwargs)

    env = tmp_path/'disposable env'
    run([sys.executable, '-m', 'venv', '--system-site-packages', str(env)])
    python = env/'Scripts/python.exe'
    # Seed a genuinely older release into this venv only.
    run([str(python), '-m', 'pip', 'install', '--ignore-installed', '--no-deps', 'ytm==0.10.0'])
    assert run([str(python), '-c', 'from importlib.metadata import version; print(version("ytm"))']).stdout.strip() == '0.10.0'
    wheels=tmp_path/'wheels'
    run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '-w', str(wheels), str(Path(__file__).resolve().parents[1])])
    wheel=next(wheels.glob('ytm-*.whl'))
    helper=tmp_path/'helper'
    helper.mkdir()
    script=helper/'update.ps1'
    shutil.copyfile(Path(update.__file__).with_name('windows_update.ps1'),script)
    plan=helper/'plan.json'
    plan.write_text(json.dumps({
        'parent_id':2147483647,
        'launchers':[str(env/'Scripts/ytm.exe')],
        'python':str(python), 'target':version('ytm'),
        'commands':[{'executable':str(python),'arguments':['-m','pip','install','--no-deps','--force-reinstall',str(wheel)]}],
        'fallback':'Disposable installation only',
    }),encoding='utf-8')
    shell=shutil.which('powershell.exe')
    assert shell, 'Native smoke requires Windows PowerShell 5.1'
    result=run([shell,'-NoProfile','-File',str(script),str(plan)],input='\n',cwd=tmp_path)
    assert 'Successfully updated' in result.stdout
    assert run([str(python),'-c','from importlib.metadata import version; print(version("ytm"))']).stdout.strip() == version('ytm')
    assert not helper.exists()
