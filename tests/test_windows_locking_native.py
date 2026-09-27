"""Native cross-process locking, never the user's credential store."""
import subprocess
import sys
import time

import pytest
from ytm.authentication.storage import SessionStore, StoredRecord

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='native Windows locking')


def test_msvcrt_blocks_competing_credential_commit(tmp_path):
    path = tmp_path/'session.json'
    store = SessionStore(path)
    record = store.save(StoredRecord.tombstone(), expected_revision=None)
    ready = tmp_path/'ready'
    child = None
    with store._lock():
        child = subprocess.Popen([sys.executable, '-c', '''
import sys
from pathlib import Path
from ytm.authentication.storage import SessionStore, StoredRecord
Path(sys.argv[3]).write_text('ready')
SessionStore(sys.argv[1]).save(StoredRecord.tombstone(), expected_revision=sys.argv[2])
''', str(path), record.revision, str(ready)])
        try:
            until = time.monotonic()+10
            while not ready.exists() and time.monotonic()<until:
                time.sleep(.02)
            assert ready.exists()
            time.sleep(.15)
            assert child.poll() is None
            assert store.load().revision == record.revision
        except BaseException:
            child.kill(); child.wait()
            raise
    try:
        assert child.wait(timeout=10) == 0
        assert store.load().revision != record.revision
    finally:
        if child.poll() is None:
            child.kill(); child.wait()
