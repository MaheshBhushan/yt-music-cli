"""Confirmed Windows update handoff; installers are always faked."""
import io
from types import SimpleNamespace

import pytest

from ytm import cli, update


@pytest.fixture
def windows_cli(monkeypatch):
    monkeypatch.setattr(update.sys, "platform", "win32")
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(update, "check", lambda **kwargs: {
        "installed": "0.9.4", "latest": "0.9.5", "newer": True,
    })
    monkeypatch.setattr(update, "install_kind", lambda: "pip")
    monkeypatch.setattr(update, "upgrade", lambda **kwargs: (False, "manual PowerShell command"))
    calls = []
    monkeypatch.setattr(update, "start_windows_upgrade", lambda **kwargs: (
        calls.append(kwargs), (True, "Update window opened; exiting ytm.")
    )[1], raising=False)
    return calls


def test_windows_cli_confirms_handoff_without_claiming_success(windows_cli, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    out = io.StringIO()
    assert cli.main(["update"], out=out) == 0
    assert windows_cli == [{"kind": "pip", "target": "0.9.5"}]
    assert "Update window opened" in out.getvalue()
    assert "upgraded via" not in out.getvalue()


@pytest.mark.parametrize("answer", ["n", "no"])
def test_windows_cli_decline_does_not_spawn(windows_cli, monkeypatch, answer):
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    out = io.StringIO()
    assert cli.main(["update"], out=out) == 0
    assert not windows_cli
    assert "cancelled" in out.getvalue()
    assert "manual PowerShell command" in out.getvalue()


@pytest.mark.parametrize("argv,interactive", [(["--json", "update"], True), (["update"], False)])
def test_windows_noninteractive_update_stays_manual(windows_cli, monkeypatch, argv, interactive):
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: interactive))
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("must not prompt"))
    err = io.StringIO()
    assert cli.main(argv, err=err) == 1
    assert not windows_cli
    assert "manual PowerShell command" in err.getvalue()


def test_windows_cli_eof_and_spawn_failure_are_safe(windows_cli, monkeypatch):
    def eof(prompt):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    assert cli.main(["update"], out=io.StringIO()) == 0
    assert not windows_cli
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    monkeypatch.setattr(update, "start_windows_upgrade", lambda **kwargs: (False, "failed; manual fallback"))
    err = io.StringIO()
    assert cli.main(["update"], err=err) == 1
    assert "manual fallback" in err.getvalue()


@pytest.mark.parametrize("kind", ["pip", "pipx", "uv"])
def test_helper_is_independent_and_uses_existing_upgrade_commands(monkeypatch, tmp_path, kind):
    import json
    from pathlib import Path

    folder = tmp_path / "helper"
    folder.mkdir()
    monkeypatch.setattr(update.tempfile, "mkdtemp", lambda **kwargs: str(folder))
    monkeypatch.setattr(update.shutil, "which", lambda name: str(tmp_path / name))
    monkeypatch.setattr(update, "_has_module", lambda name: True)
    monkeypatch.setattr(update.sysconfig, "get_path", lambda name: str(tmp_path / "Scripts"))
    monkeypatch.setattr(update.sys, "argv", [str(tmp_path / "User Scripts" / "ytm.exe")])
    monkeypatch.setattr(update.subprocess, "CREATE_NEW_CONSOLE", 16, raising=False)
    started = []
    monkeypatch.setattr(update.subprocess, "Popen", lambda command, **kwargs: started.append((command, kwargs)))
    ok, message = update.start_windows_upgrade(kind=kind, target="0.9.5")
    assert ok and "new window will report" in message
    command, options = started[0]
    assert command[0] == str(tmp_path / "powershell.exe")
    assert options == {"creationflags": 16, "close_fds": True, "cwd": str(folder.parent)}
    assert Path(command[-2]).read_text() == Path(update.__file__).with_name("windows_update.ps1").read_text()
    plan = json.loads(Path(command[-1]).read_text())
    expected = update.upgrade_commands(kind, target="0.9.5")
    assert [c["arguments"] for c in plan["commands"]] == [c[1:] for c in expected]
    assert plan["parent_id"] == update.os.getpid()
    assert plan["python"] == update.sys.executable
    assert plan["target"] == "0.9.5"
    assert str(tmp_path / "User Scripts" / "ytm.exe") in plan["launchers"]
    assert str(tmp_path / "Scripts" / "yt-dlp.exe") in plan["launchers"]


def test_failed_handoff_cleans_up_and_quotes_fallback(monkeypatch, tmp_path):
    folder = tmp_path / "helper"
    folder.mkdir()
    monkeypatch.setattr(update.tempfile, "mkdtemp", lambda **kwargs: str(folder))
    monkeypatch.setattr(update.shutil, "which", lambda name: name)
    monkeypatch.setattr(update.subprocess, "CREATE_NEW_CONSOLE", 16, raising=False)
    monkeypatch.setattr(update, "upgrade_commands", lambda *args, **kwargs: [["pipx", "it's $literal; &"]])
    def fail(*args, **kwargs):
        raise OSError("cannot create window")
    monkeypatch.setattr(update.subprocess, "Popen", fail)
    ok, text = update.start_windows_upgrade(kind="pipx", target="0.9.5")
    assert not ok
    assert "cannot create window" in text
    assert "'it''s $literal; &'" in text
    assert not folder.exists()


def test_missing_powershell_returns_manual_command(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda name: None)
    ok, text = update.start_windows_upgrade(kind="pipx", target="0.9.5")
    assert not ok and "PowerShell was not found" in text
    assert "'pipx' 'upgrade'" in text


# PowerShell itself is available on Linux too. Exercise the helper's control
# flow with process/lock checks stubbed; actual Windows locks need manual testing.
def run_powershell(tmp_path, body):
    import shutil
    import subprocess
    from pathlib import Path

    shell = shutil.which("pwsh") or shutil.which("powershell.exe")
    if not shell:
        pytest.skip("PowerShell is not installed")
    source = str(Path(update.__file__).with_name("windows_update.ps1")).replace("'", "''")
    script = tmp_path / "test.ps1"
    script.write_text(f". '{source}'\n" + body, encoding="utf-8-sig")
    return subprocess.run([shell, "-NoProfile", "-File", str(script)], capture_output=True, text=True, timeout=15, check=False)


@pytest.mark.parametrize("failure", ["none", "parent", "lock", "installer", "verify"])
def test_powershell_only_installs_after_exit_and_unlock_and_verifies(tmp_path, failure):
    result = run_powershell(tmp_path, '''
$script:events = [System.Collections.Generic.List[string]]::new()
function Wait-YtmExit($ParentId) {
    $script:events.Add('wait')
    if ('FAILURE' -eq 'parent') { throw 'parent still running' }
}
function Wait-YtmLaunchers($Paths) {
    $script:events.Add('unlock')
    if ('FAILURE' -eq 'lock') { throw 'other instance locked' }
}
function Invoke-YtmCommand($Command) {
    $script:events.Add($Command.executable)
    if ('FAILURE' -eq $Command.executable) { throw 'failed' }
}
$plan = @{parent_id = 42; launchers = @('ytm.exe'); target = '0.9.5'; python = 'verify';
    commands = @(@{executable = 'installer'; arguments = @()})}
try { Invoke-YtmUpdate $plan } catch { Write-Host "Caught: $_" }
Write-Host ('EVENTS:' + ($script:events -join ','))
'''.replace('FAILURE', failure))
    assert result.returncode == 0, result.stderr
    expected = {
        "none": "wait,unlock,installer,verify", "parent": "wait", "lock": "wait,unlock",
        "installer": "wait,unlock,installer", "verify": "wait,unlock,installer,verify",
    }
    assert f"EVENTS:{expected[failure]}" in result.stdout
    assert ("Successfully updated" in result.stdout) == (failure == "none")


def test_powershell_native_command_preserves_arguments_and_stops_on_failure(tmp_path):
    import sys
    executable = sys.executable.replace("'", "''")
    result = run_powershell(tmp_path, f'''
Invoke-YtmCommand @{{executable = '{executable}'; arguments = @('-c', 'import sys; print(sys.argv[1])', 'space ''quote $literal; &')}}
try {{
    Invoke-YtmCommand @{{executable = '{executable}'; arguments = @('-c', 'import sys; sys.exit(7)')}}
    throw 'missed failure'
}} catch {{
    if ($_.Exception.Message -notlike '*exit 7*') {{ throw }}
    Write-Host 'Stopped on exit 7'
}}
''')
    assert result.returncode == 0, result.stderr
    assert "space 'quote $literal; &" in result.stdout
    assert "Stopped on exit 7" in result.stdout


@pytest.mark.parametrize("target,success", [("0", True), ("9999.0.0", False)])
def test_powershell_verifies_using_a_fresh_python_process(tmp_path, target, success):
    import sys
    executable = sys.executable.replace("'", "''")
    result = run_powershell(tmp_path, f'''
function Wait-YtmExit($ParentId) {{}}
function Wait-YtmLaunchers($Paths) {{}}
Invoke-YtmUpdate @{{parent_id = 42; launchers = @(); commands = @(); python = '{executable}'; target = '{target}'}}
''')
    assert (result.returncode == 0) == success, result.stderr
    assert ("Successfully updated" in result.stdout) == success
    if not success:
        assert "Expected at least ytm 9999.0.0" in result.stderr


def test_powershell_entrypoint_shows_fallback_and_cleans_up(tmp_path):
    import json
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    shell = shutil.which("pwsh") or shutil.which("powershell.exe")
    if not shell:
        pytest.skip("PowerShell is not installed")
    folder = tmp_path / "helper"
    folder.mkdir()
    script = folder / "update.ps1"
    shutil.copyfile(Path(update.__file__).with_name("windows_update.ps1"), script)
    plan_path = folder / "plan.json"
    plan_path.write_text(json.dumps({
        "parent_id": 2147483647, "launchers": [], "python": sys.executable, "target": "0",
        "commands": [{"executable": sys.executable, "arguments": ["-c", "import sys; sys.exit(7)"]}],
        "fallback": "MANUAL-COMMAND",
    }))
    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script), str(plan_path)],
        cwd=tmp_path, input="\n", capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 1, result.stderr
    assert "Update failed" in result.stdout and "MANUAL-COMMAND" in result.stdout
    assert "Successfully updated" not in result.stdout
    assert not folder.exists()


def test_powershell_launcher_probe_detects_locks_without_changing_files(tmp_path):
    path = str(tmp_path / "ytm.exe").replace("'", "''")
    result = run_powershell(tmp_path, f'''
[System.IO.File]::WriteAllText('{path}', 'unchanged')
Test-YtmLaunchers @('{path}')
$held = [System.IO.File]::Open('{path}', 'Open', 'ReadWrite', 'None')
try {{
    try {{
        Test-YtmLaunchers @('{path}')
        throw 'missed lock'
    }} catch [System.IO.IOException] {{ Write-Host 'Detected lock' }}
}} finally {{ $held.Dispose() }}
if ([System.IO.File]::ReadAllText('{path}') -ne 'unchanged') {{ throw 'file changed' }}
''')
    assert result.returncode == 0, result.stderr
    assert "Detected lock" in result.stdout
