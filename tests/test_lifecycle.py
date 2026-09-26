"""Real process ownership and IPC teardown, without network or audio devices."""

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ytm.lifecycle import ProcessOwner
from ytm.player import Player, PlayerError

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux process lifecycle')


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError('condition did not become true')


def test_owned_descendants_are_killed_and_reaped_but_unrelated_process_survives(tmp_path):
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    owner = ProcessOwner()
    child_file = tmp_path / 'child'
    code = '''
import subprocess, sys, time, signal
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
p = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'], start_new_session=True)
Path(sys.argv[1]).write_text(str(p.pid))
time.sleep(60)
'''
    try:
        process = owner.spawn([sys.executable, '-c', code, str(child_file)])
        wait_for(child_file.exists)
        child_pid = int(child_file.read_text())
        owner.close()
        owner.close()
        assert process.returncode is not None
        assert not Path(f'/proc/{process.pid}').exists()
        assert not Path(f'/proc/{child_pid}').exists()  # no zombie either
        assert unrelated.poll() is None
        with pytest.raises(OSError, match='shutting down'):
            owner.spawn([sys.executable, '-c', 'pass'])
    finally:
        owner.close()
        unrelated.terminate()
        unrelated.wait()


@pytest.mark.skipif(not shutil.which('mpv'), reason='mpv not installed')
def test_real_mpv_observer_unblocks_and_owned_player_exits(tmp_path):
    owner = ProcessOwner()
    endpoint = str(tmp_path / 'mpv.sock')
    player = Player(ipc_path=endpoint, owner=owner, extra_args=['--ao=null'])
    process = owner._processes[0]
    observer = Player(ipc_path=endpoint, spawn=False, timeout=None)
    entered = threading.Event()
    def listen():
        try:
            for _ in observer.observe('pause'):
                entered.set()
        except PlayerError:
            pass
    thread = threading.Thread(target=listen)
    thread.start()
    try:
        assert entered.wait(3)
        observer.close()
        thread.join(2)
        assert not thread.is_alive()
        player.close()
        owner.close()
        assert process.returncode is not None
        assert not Path(f'/proc/{process.pid}').exists()
    finally:
        observer.close()
        player.close()
        owner.close()
        thread.join(2)


def test_mpv_startup_timeout_reaps_spawned_process(tmp_path, monkeypatch):
    from ytm import player as player_mod
    owner = ProcessOwner()
    monkeypatch.setattr(player_mod, 'SPAWN_TIMEOUT', 0.05)
    def spawner(args):
        return owner.spawn([sys.executable, '-c', 'import time; time.sleep(60)'])
    with pytest.raises(PlayerError):
        Player(ipc_path=str(tmp_path / 'missing.sock'), owner=owner, spawner=spawner)
    assert owner._processes[0].returncode is not None


@pytest.mark.skipif(not shutil.which('mpv'), reason='mpv not installed')
@pytest.mark.parametrize('mode', ['e', 'x', 'ctrl_c', 'SIGHUP', 'SIGTERM', 'SIGINT', 'terminal_close', 'normal', 'exception', 'slow_request'])
def test_tui_exit_paths_reap_real_mpv_and_restore_terminal(tmp_path, mode):
    import fcntl
    import json
    import pty
    import select
    import struct
    import termios

    ready = tmp_path / 'ready.json'
    program = '''
import copy, json, os
from pathlib import Path
from ytm import config, cli
from ytm.tui import app as module
from ytm.tui.backend import Backend
cfg = copy.deepcopy(config.DEFAULTS)
cfg['audio']['control'] = 'player'
cfg['behaviour']['autoplay_radio'] = False
cfg['behaviour']['authenticated_streams'] = False
cfg['update']['check'] = False
config.load = lambda: cfg
class LocalBackend(Backend):
    def request(self, command, args=None):
        if command == 'playlist_list':
            if os.environ['MODE'] == 'slow_request':
                import time
                time.sleep(60)
            return {'playlists': []}
        return super().request(command, args)
module.Backend = LocalBackend
app = module.YTMApp(config=cfg)
async def ready(pilot):
    Path(os.environ['READY']).write_text(json.dumps({'mpv': app.client._owner._processes[0].pid}))
    if os.environ['MODE'] in ('normal', 'exception'):
        await pilot.pause(0.3)
        if os.environ['MODE'] == 'exception':
            raise RuntimeError('simulated UI failure')
        app.exit()
app.run(auto_pilot=ready)
'''
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 110, 880, 512))
    before = termios.tcgetattr(slave)
    env = dict(os.environ, READY=str(ready), MODE=mode, TERM='xterm-256color',
               XDG_CONFIG_HOME=str(tmp_path / 'config'), XDG_STATE_HOME=str(tmp_path / 'state'),
               XDG_CACHE_HOME=str(tmp_path / 'cache'))
    process = subprocess.Popen([sys.executable, '-c', program], stdin=slave, stdout=slave,
                               stderr=slave, start_new_session=True, env=env)
    stop = threading.Event()
    output = bytearray()
    read_fd = master
    def drain():
        while not stop.is_set():
            try:
                if select.select([read_fd], [], [], 0.05)[0]:
                    data = os.read(read_fd, 65536)
                    if not data:
                        return
                    output.extend(data)
            except OSError:
                return
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    mpv_pid = None
    try:
        wait_for(lambda: ready.exists() or process.poll() is not None)
        assert ready.exists(), output.decode(errors='replace')
        mpv_pid = json.loads(ready.read_text())['mpv']
        time.sleep(1.0 if mode in ('e', 'x', 'ctrl_c') else 0.2)
        if mode in ('e', 'x'):
            os.write(master, b'\x1b')
            time.sleep(0.4)
            os.write(master, mode.encode())
        elif mode == 'ctrl_c':
            os.write(master, b'\x03')
        elif mode == 'slow_request':
            os.kill(process.pid, signal.SIGTERM)
        elif mode.startswith('SIG'):
            os.kill(process.pid, getattr(signal, mode))
        elif mode == 'terminal_close':
            os.close(master)
            master = None
        process.wait(timeout=8)
        assert not Path(f'/proc/{mpv_pid}').exists(), 'owned mpv survived or became a zombie: ' + output.decode(errors='replace')
        if mode != 'terminal_close':
            assert termios.tcgetattr(slave) == before
            assert b'\x1b[?1049l' in output  # alternate screen restored by Textual
            assert b'\x1b[?25h' in output  # cursor restored
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        stop.set()
        reader.join(1)
        if master is not None:
            os.close(master)
        os.close(slave)
        if mpv_pid is not None and Path(f'/proc/{mpv_pid}').exists():
            os.kill(mpv_pid, signal.SIGKILL)


def test_volume_subscription_is_terminated_and_waited(monkeypatch):
    from ytm import volume

    real_popen = subprocess.Popen
    started = []
    def subscribe(args, **kwargs):
        process = real_popen([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
        started.append(process)
        return process
    monkeypatch.setattr(volume.subprocess, 'Popen', subscribe)
    mixer = volume.SystemVolume(pactl='test-pactl', run=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, '50%'))
    stop = threading.Event()
    thread = threading.Thread(target=mixer.watch, args=(lambda level: None, stop))
    thread.start()
    try:
        wait_for(lambda: started)
        stop.set()
        thread.join(3)
        assert not thread.is_alive()
        assert started[0].returncode is not None
        assert not Path(f'/proc/{started[0].pid}').exists()
    finally:
        stop.set()
        for process in started:
            if process.poll() is None:
                process.kill()
                process.wait()
        thread.join(3)


def test_already_exited_orphan_is_reaped(tmp_path):
    owner = ProcessOwner()
    child_file = tmp_path / 'orphan'
    code = '''
import subprocess, sys
from pathlib import Path
p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.1)'])
Path(sys.argv[1]).write_text(str(p.pid))
'''
    child_pid = None
    try:
        parent = owner.spawn([sys.executable, '-c', code, str(child_file)])
        parent.wait(timeout=3)
        child_pid = int(child_file.read_text())
        def zombie():
            return Path(f'/proc/{child_pid}/stat').read_text().rsplit(')', 1)[1].split()[0] == 'Z'
        wait_for(zombie)
        owner.close()
        assert not Path(f'/proc/{child_pid}').exists()
    finally:
        owner.close()
        if child_pid is not None:
            try:
                os.waitpid(child_pid, os.WNOHANG)
            except ChildProcessError:
                pass
