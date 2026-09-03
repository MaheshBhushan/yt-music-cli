"""ytm/update.py: version check with a daily cache, and the upgrade path."""

import io
import json
import subprocess
import sys

import pytest

from ytm import cli, update


def test_is_newer_compares_numerically():
    assert update.is_newer("0.10.0", "0.9.9")
    assert update.is_newer("1.0", "0.99.99")
    assert not update.is_newer("0.2.0", "0.2.0")
    assert not update.is_newer("0.1.9", "0.2.0")
    assert not update.is_newer(None, "0.2.0")
    assert not update.is_newer("0.3.0", None)


def test_latest_version_returns_none_when_pypi_unreachable(real_latest_version):
    def opener(url, timeout):
        raise OSError("offline")
    assert real_latest_version(opener=opener) is None


def test_latest_version_reads_the_pypi_payload(real_latest_version):
    import io

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    assert real_latest_version(opener=lambda url, timeout: Resp(json.dumps({"info": {"version": "9.9.9"}}).encode())) == "9.9.9"


def test_check_fetches_once_a_day_then_uses_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda: "0.2.0")
    calls = []
    fetch = lambda: (calls.append(1), "0.3.0")[1]
    path = tmp_path / "check.json"
    first = update.check(path=path, fetch=fetch, now=1000)
    assert first["newer"] and first["latest"] == "0.3.0" and first["cached"] is False
    second = update.check(path=path, fetch=fetch, now=1000 + 3600)
    assert second["cached"] is True and second["newer"] and len(calls) == 1
    third = update.check(path=path, fetch=fetch, now=1000 + update.CHECK_INTERVAL + 1)
    assert third["cached"] is False and len(calls) == 2
    forced = update.check(path=path, fetch=fetch, now=1000 + update.CHECK_INTERVAL + 2, force=True)
    assert forced["cached"] is False and len(calls) == 3


def test_check_offline_falls_back_to_stale_cache_or_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda: "0.2.0")
    path = tmp_path / "check.json"
    unknown = update.check(path=path, fetch=lambda: None, now=0)
    assert unknown["latest"] is None and unknown["newer"] is False
    path.write_text(json.dumps({"latest": "0.5.0", "checked_at": 0}))
    stale = update.check(path=path, fetch=lambda: None, now=10 * update.CHECK_INTERVAL)
    assert stale["latest"] == "0.5.0" and stale["newer"] is True


def test_check_never_raises_on_a_corrupt_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda: "0.2.0")
    path = tmp_path / "check.json"
    path.write_text("{not json")
    assert update.check(path=path, fetch=lambda: "0.2.0", now=0)["newer"] is False


def test_install_kind_from_prefix():
    assert update.install_kind("/home/u/.local/share/pipx/venvs/ytm") == "pipx"
    assert update.install_kind("C:\\Users\\u\\pipx\\venvs\\ytm") == "pipx"
    assert update.install_kind("/home/u/.local/share/uv/tools/ytm") == "uv"


def test_install_kind_editable_checkout_is_detected(monkeypatch):
    class Dist:
        def read_text(self, name):
            return json.dumps({"url": "file:///x", "dir_info": {"editable": True}})
    monkeypatch.setattr(update.metadata, "distribution", lambda name: Dist())
    assert update.install_kind("/somewhere/.venv") == "editable"


def test_upgrade_commands_per_installer():
    assert update.upgrade_commands("pipx") == [
        ["pipx", "upgrade", "ytm"], ["pipx", "runpip", "ytm", "install", "-U", "yt-dlp"]]
    assert update.upgrade_commands("pipx", yt_dlp=False) == [["pipx", "upgrade", "ytm"]]
    assert update.upgrade_commands("uv") == [["uv", "tool", "upgrade", "ytm"]]
    assert update.upgrade_commands("pip", has_pip=True) == [[sys.executable, "-m", "pip", "install", "-U", "ytm", "yt-dlp"]]
    # a `uv venv` has no pip module: go through uv aimed at this interpreter
    assert update.upgrade_commands("pip", has_pip=False, has_uv=True) == [
        ["uv", "pip", "install", "-U", "--python", sys.executable, "ytm", "yt-dlp"]]
    assert update.upgrade_commands("pip", has_pip=False, has_uv=False) == []
    assert update.upgrade_commands("editable") == []


def test_upgrade_without_pip_or_uv_explains_itself(monkeypatch):
    monkeypatch.setattr(update, "upgrade_commands", lambda kind, yt_dlp=True, **k: [])
    ok, text = update.upgrade(kind="pip")
    assert not ok and "uv pip install" in text and sys.executable in text


def test_upgrade_runs_each_command_and_stops_on_failure():
    ran = []
    def run(cmd, capture_output, text):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")
    ok, text = update.upgrade(kind="pipx", run=run)
    assert ok and len(ran) == 2 and "ok" in text

    def failing(cmd, capture_output, text):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
    ok, text = update.upgrade(kind="pip", run=failing)
    assert not ok and "boom" in text

    def missing(cmd, capture_output, text):
        raise FileNotFoundError("pipx")
    ok, text = update.upgrade(kind="pipx", run=missing)
    assert not ok and "pipx" in text

    ok, text = update.upgrade(kind="editable")
    assert not ok and "git pull" in text


def test_cli_update_check_reports(monkeypatch, capsys):
    monkeypatch.setattr(update, "check", lambda force=False, **k: {
        "installed": "0.2.0", "latest": "0.3.0", "newer": True, "checked_at": 0, "cached": False})
    out = io.StringIO()
    assert cli.main(["update", "--check"], out=out) == 0
    assert "0.2.0" in out.getvalue() and "0.3.0 is available" in out.getvalue()


def test_cli_update_upgrades_when_newer(monkeypatch, capsys):
    monkeypatch.setattr(update, "check", lambda force=False, **k: {
        "installed": "0.2.0", "latest": "0.3.0", "newer": True, "checked_at": 0, "cached": False})
    monkeypatch.setattr(update, "install_kind", lambda: "pipx")
    monkeypatch.setattr(update, "upgrade", lambda kind=None, yt_dlp=True, run=None: (True, "done"))
    out = io.StringIO()
    assert cli.main(["update"], out=out) == 0
    assert "upgraded via pipx" in out.getvalue()


def test_cli_update_up_to_date_does_nothing_unless_forced(monkeypatch, capsys):
    monkeypatch.setattr(update, "check", lambda force=False, **k: {
        "installed": "0.2.0", "latest": "0.2.0", "newer": False, "checked_at": 0, "cached": False})
    called = []
    monkeypatch.setattr(update, "install_kind", lambda: "pip")
    monkeypatch.setattr(update, "upgrade", lambda kind=None, yt_dlp=True, run=None: (called.append(1), (True, ""))[1])
    out = io.StringIO()
    assert cli.main(["update"], out=out) == 0 and not called
    assert "up to date" in out.getvalue()
    assert cli.main(["update", "--force"], out=io.StringIO()) == 0 and called


def test_cli_update_failure_is_an_error(monkeypatch, capsys):
    monkeypatch.setattr(update, "check", lambda force=False, **k: {
        "installed": "0.2.0", "latest": "0.3.0", "newer": True, "checked_at": 0, "cached": False})
    monkeypatch.setattr(update, "install_kind", lambda: "pip")
    monkeypatch.setattr(update, "upgrade", lambda kind=None, yt_dlp=True, run=None: (False, "pip exploded"))
    err = io.StringIO()
    assert cli.main(["update"], err=err) == 1
    assert "pip exploded" in err.getvalue()


def test_cli_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("ytm ")
