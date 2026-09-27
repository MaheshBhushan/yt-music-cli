"""M1: a bounded, safe diagnostic history that survives a restart."""

import os
import time

from ytm import diagnostics


def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv("YTM_TUI_LOG", raising=False)
    monkeypatch.setattr(diagnostics, "DEFAULT_DIR", tmp_path / "logs")
    diagnostics.reset()


def test_two_sequential_starts_keep_the_first_run(tmp_path, monkeypatch):
    _clean(monkeypatch, tmp_path)
    first = diagnostics.start_run(now=1_000_000, pid=1)
    diagnostics.write("first run: started", path=first)
    diagnostics.write("error 'Could not play this track. Try another track or retry playback.'", path=first)

    second = diagnostics.start_run(now=2_000_000, pid=2)
    diagnostics.write("second run: started", path=second)

    assert first != second
    assert "first run: started" in first.read_text()
    assert "Could not play this track" in first.read_text()  # the earlier failure is still diagnosable
    assert "second run: started" in second.read_text()
    assert "first run: started" not in second.read_text()


def test_concurrent_starts_in_one_second_do_not_clobber(tmp_path, monkeypatch):
    _clean(monkeypatch, tmp_path)
    first = diagnostics.start_run(now=1_000_000, pid=101)
    second = diagnostics.start_run(now=1_000_000, pid=202)
    assert first != second
    diagnostics.write("from one", path=first)
    diagnostics.write("from two", path=second)
    assert "from one" in first.read_text()
    assert "from two" in second.read_text()


def test_retention_removes_only_the_oldest_runs(tmp_path, monkeypatch):
    _clean(monkeypatch, tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    now = time.time()
    runs = []
    for index in range(diagnostics.KEEP_RUNS + 3):
        path = logs / f"tui-20260101-0000{index:02d}-{index}.log"
        path.write_text("x" * 32)
        stamp = now - index * 60
        os.utime(path, (stamp, stamp))
        runs.append(path)

    diagnostics.prune(now=now)
    surviving = sorted(path.name for path in logs.glob("tui-*.log"))
    assert len(surviving) == diagnostics.KEEP_RUNS
    for kept in runs[: diagnostics.KEEP_RUNS]:
        assert kept.exists()
    for removed in runs[diagnostics.KEEP_RUNS:]:
        assert not removed.exists()


def test_start_run_prunes_old_history(tmp_path, monkeypatch):
    _clean(monkeypatch, tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    now = time.time()
    for index in range(diagnostics.KEEP_RUNS + 3):
        path = logs / f"tui-20260101-0000{index:02d}-{index}.log"
        path.write_text("x")
        stamp = now - (index + 1) * 60
        os.utime(path, (stamp, stamp))

    fresh = diagnostics.start_run(now=now, pid=999)
    assert fresh is not None and fresh.exists()
    assert len(list(logs.glob("tui-*.log"))) == diagnostics.KEEP_RUNS


def test_retention_does_not_touch_a_live_run(tmp_path, monkeypatch):
    _clean(monkeypatch, tmp_path)
    now = time.time()
    live = diagnostics.start_run(now=now, pid=1)  # newest, still being written
    diagnostics.prune(now=now)
    assert live.exists()


def test_an_unwritable_log_directory_never_breaks_work(tmp_path, monkeypatch):
    blocked = tmp_path / "logs"
    blocked.write_text("not a directory")
    monkeypatch.setattr(diagnostics, "DEFAULT_DIR", blocked)
    monkeypatch.delenv("YTM_TUI_LOG", raising=False)
    diagnostics.reset()

    assert diagnostics.start_run(now=1_000_000, pid=1) is None
    diagnostics.write("anything")  # must not raise
    diagnostics.reset()


def test_an_explicit_log_path_is_used_and_never_rotated(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.log"
    monkeypatch.setenv("YTM_TUI_LOG", str(explicit))
    monkeypatch.setattr(diagnostics, "DEFAULT_DIR", tmp_path / "logs")
    diagnostics.reset()

    assert diagnostics.run_path() == explicit
    diagnostics.write("hello")
    diagnostics.write("again", mode="a")
    assert "hello" in explicit.read_text()
    diagnostics.prune()
    assert explicit.exists()
    diagnostics.reset()


def test_redaction_removes_synthetic_secrets():
    line = (
        "error 'cookie: SID=SECRETSID; __Secure-3PAPISID=SECRETPAPISID "
        "authorization=SAPISIDHASH 123_SECRET "
        "refresh_token=SECRETTOKEN https://media.example/stream?token=SECRETURL&x=1'"
    )
    cleaned = diagnostics.redact(line)
    for secret in ("SECRETSID", "SECRETPAPISID", "123_SECRET", "SECRETTOKEN", "SECRETURL"):
        assert secret not in cleaned
    assert "<redacted>" in cleaned or "<signed-url>" in cleaned


def test_write_redacts_before_persisting(tmp_path, monkeypatch):
    _clean(monkeypatch, tmp_path)
    path = tmp_path / "run.log"
    diagnostics.write("cookie: SID=SECRET", path=path)
    assert "SECRET" not in path.read_text()
