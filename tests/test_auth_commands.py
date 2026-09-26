"""`ytm login`, `ytm auth --from-browser`, and their honest output."""

import io
import json

import pytest

from ytm import cli
from ytm.authentication import session as session_mod
from ytm.authentication.errors import AuthInvalidFormat, BrowserUnavailable
from ytm.authentication.storage import StoredRecord


def headers(**extra):
    base = {
        "cookie": "SID=x; __Secure-3PAPISID=secret",
        "x-goog-authuser": "0",
        "authorization": "SAPISIDHASH 0_0",
        "origin": "https://music.youtube.com",
    }
    base.update(extra)
    return base


def run(*argv):
    """Run the CLI, capturing stdout and the progress/errors stream.

    `ytm login` writes progress to stderr as it happens, so stderr is
    redirected for the duration of the call -- exactly as a user's terminal
    would receive it.
    """
    out, err = io.StringIO(), io.StringIO()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli.sys, "stderr", err)
        code = cli.main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def verified():
    session = session_mod.build_session(headers())
    return session_mod.VerifiedSession(session=session, account_name="Example Listener", verified_at="now")


# -- interactive login --------------------------------------------------------


def test_interactive_login_prints_progress_to_stderr_and_the_result_to_stdout(monkeypatch, tmp_path):
    from ytm.authentication import browser_login

    calls = []

    def fake_interactive(manager, *, browser=None, timeout=600, confirm=None):
        calls.append(timeout)
        assert confirm is not None and confirm(verified())
        return StoredRecord.browser(headers())

    monkeypatch.setattr(browser_login, "interactive_login", fake_interactive)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    code, out, err = run("login", "--timeout", "42")
    assert code == 0
    assert calls == [42]
    assert out.strip() == "Login successful. Credentials stored locally."
    assert err.splitlines() == [
        "Opening YouTube Music login...",
        "Sign in using the browser window.",
        "Waiting for YouTube Music authentication...",
        "YouTube Music account verified.",
        "Use this account: Example Listener? [Y/n] ",
    ]


def test_login_json_payload_has_only_safe_fields(monkeypatch):
    from ytm.authentication import browser_login

    monkeypatch.setattr(
        browser_login, "interactive_login",
        lambda manager, **kw: StoredRecord.browser(headers()),
    )
    code, out, err = run("--json", "login", "--yes")
    assert json.loads(out) == {"authenticated": True, "method": "browser", "status": "valid"}
    assert "cookie" not in out and "SAPISID" not in out and "session.json" not in out
    assert "Opening YouTube Music login..." in err  # progress never pollutes JSON stdout


def test_yes_skips_the_confirmation_prompt(monkeypatch):
    from ytm.authentication import browser_login

    def fake_interactive(manager, *, browser=None, timeout=600, confirm=None):
        assert confirm is not None and confirm(verified())
        return StoredRecord.browser(headers())

    monkeypatch.setattr(browser_login, "interactive_login", fake_interactive)
    monkeypatch.setattr(cli, "_confirm_account", lambda name: pytest.fail("must not prompt"))
    assert run("login", "--yes")[0] == 0


def test_declining_the_account_keeps_the_old_credentials(monkeypatch):
    from ytm.authentication import browser_login
    from ytm.authentication.errors import LoginCancelled

    def fake_interactive(manager, *, browser=None, timeout=600, confirm=None):
        assert confirm is not None and not confirm(verified())
        raise LoginCancelled("Login cancelled; the previous credentials were kept.")

    monkeypatch.setattr(browser_login, "interactive_login", fake_interactive)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    code, out, err = run("login")
    assert code == 1
    assert "cancelled" in err.lower()


def test_ctrl_c_exits_130(monkeypatch):
    from ytm.authentication import browser_login

    def interrupted(manager, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(browser_login, "interactive_login", interrupted)
    code, out, err = run("login")
    assert code == 130
    assert "cancelled" in err


def test_a_browser_failure_is_a_one_line_error(monkeypatch):
    from ytm.authentication import browser_login

    def unavailable(manager, **kw):
        raise BrowserUnavailable("Install Playwright with 'ytm login --install-browser'.")

    monkeypatch.setattr(browser_login, "interactive_login", unavailable)
    code, out, err = run("login")
    assert code == 1
    assert "ytm login --install-browser" in err and "Traceback" not in err


def test_the_launch_channel_reaches_the_browser(monkeypatch):
    from ytm.authentication import browser_login

    seen = {}

    class FakePlaywright:
        def __init__(self, *, channel=None):
            seen["channel"] = channel

    def fake_interactive(manager, *, browser=None, timeout=600, confirm=None):
        seen["browser"] = browser
        return StoredRecord.browser(headers())

    monkeypatch.setattr(browser_login, "PlaywrightBrowser", FakePlaywright)
    monkeypatch.setattr(browser_login, "interactive_login", fake_interactive)
    assert run("login", "--method", "playwright", "--browser", "chrome", "--yes")[0] == 0
    assert seen["channel"] == "chrome"
    assert isinstance(seen["browser"], FakePlaywright)


# -- the confirmation prompt --------------------------------------------------


def test_confirm_account_defaults_to_yes_and_accepts_no():
    answers = iter(["", "y", "yes", "n", "no", "maybe"])
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
        assert cli._confirm_account("Example Listener") is True
        assert cli._confirm_account("Example Listener") is True
        assert cli._confirm_account("Example Listener") is True
        assert cli._confirm_account("Example Listener") is False
        assert cli._confirm_account("Example Listener") is False
        assert cli._confirm_account("Example Listener") is False
    finally:
        monkeypatch.undo()


def test_confirm_account_treats_eof_as_no(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError))
    assert cli._confirm_account("Example") is False
    assert cli._confirm_account(None) is False


# -- importing an existing browser -------------------------------------------


def test_from_browser_imports_through_the_verified_path(monkeypatch, tmp_path):
    from ytm import auth

    calls = []
    monkeypatch.setattr(
        auth, "import_from_browser",
        lambda browser, **kw: (calls.append((browser, kw)), StoredRecord.browser(headers()))[1],
    )
    code, out, err = run("login", "--from-browser", "helium", "--profile", "Profile 1",
                         "--authuser", "2", "--yes")
    assert code == 0
    assert out.strip() == "Login successful. Credentials stored locally."
    assert calls[0][0] == "helium"
    assert calls[0][1]["profile"] == "Profile 1"
    assert calls[0][1]["authuser"] == "2"
    assert calls[0][1]["confirm"](verified()) is True  # --yes short-circuits the prompt
    assert "Importing the browser's YouTube Music session..." in err


def test_from_browser_failure_is_reported_not_traced(monkeypatch):
    from ytm import auth

    def broken(browser, **kw):
        raise AuthInvalidFormat("The captured session has no __Secure-3PAPISID cookie.")

    monkeypatch.setattr(auth, "import_from_browser", broken)
    code, out, err = run("login", "--from-browser")
    assert code == 1
    assert "__Secure-3PAPISID" in err and "Traceback" not in err


def test_ytm_auth_from_browser_uses_the_same_verified_path(monkeypatch, tmp_path):
    from ytm import auth

    calls = []
    monkeypatch.setattr(auth, "import_from_browser",
                        lambda browser, **kw: calls.append((browser, kw)) or StoredRecord.browser(headers()))
    monkeypatch.setattr(auth, "cookies_file", lambda: None)
    monkeypatch.setattr(auth, "SESSION_PATH", tmp_path / "session.json")
    assert run("auth", "--from-browser", "chrome")[0] == 0
    assert calls == [("chrome", {"profile": None, "authuser": None})]


# -- oauth fallback ----------------------------------------------------------


def test_login_method_oauth_activates_the_verified_token(monkeypatch):
    from ytm import auth

    calls = []
    monkeypatch.setattr(
        auth, "oauth_login",
        lambda **kw: (calls.append(kw), StoredRecord.oauth("/tmp/token.json"))[1],
    )
    code, out, err = run("--json", "login", "--method", "oauth", "--client-file", "c.json")
    assert json.loads(out) == {"authenticated": True, "method": "oauth", "status": "valid"}
    assert calls == [{"client_id": None, "client_secret": None, "client_file": "c.json"}]


# -- account ------------------------------------------------------------------


class StubManager:
    def __init__(self, *, status=None, logout=None):
        self._status = status
        self._logout = logout
        self.validated = None
        self.logged_out = 0

    def status(self, *, validate=True):
        self.validated = validate
        return self._status

    def logout(self):
        self.logged_out += 1
        return self._logout


def status(**fields):
    from ytm.authentication.manager import SessionStatus

    base = {"logged_in": False, "method": None, "state": "logged_out"}
    base.update(fields)
    return SessionStatus(**base)


def test_account_reports_a_validated_session(monkeypatch):
    from ytm import auth

    stub = StubManager(status=status(
        logged_in=True, method="browser", state="valid", account_name="Example Listener"
    ))
    monkeypatch.setattr(auth, "auth_manager", lambda: stub)
    code, out, err = run("account")
    assert code == 0
    assert stub.validated is True
    assert out.splitlines() == [
        "Logged in: Yes",
        "Authentication: Browser session",
        "Session status: Valid",
        "Account: Example Listener",
    ]
    assert "cookie" not in out.lower() and "SAPISID" not in out


def test_account_json_has_only_safe_fields(monkeypatch):
    from ytm import auth

    stub = StubManager(status=status(
        logged_in=True, method="oauth", state="valid", account_name="Example Listener"
    ))
    monkeypatch.setattr(auth, "auth_manager", lambda: stub)
    code, out, err = run("--json", "account")
    assert json.loads(out) == {
        "logged_in": True, "method": "oauth",
        "session_status": "valid", "account": "Example Listener",
    }


def test_account_no_check_never_validates(monkeypatch):
    from ytm import auth

    stub = StubManager(status=status(logged_in=True, method="browser", state="not_checked"))
    monkeypatch.setattr(auth, "auth_manager", lambda: stub)
    code, out, err = run("account", "--no-check")
    assert code == 0
    assert stub.validated is False
    assert out.splitlines() == ["Credentials stored: Yes", "Session status: Not checked"]


@pytest.mark.parametrize("state,expected", [
    ("expired", ["Logged in: No", "Authentication: Browser session", "Session status: Expired"]),
    ("unknown", ["Credentials stored: Yes", "Session status: Unknown (could not verify)"]),
    ("invalid", ["Credentials stored: Yes", "Session status: Invalid"]),
    ("logged_out", ["Logged in: No"]),
])
def test_account_states_are_honest(monkeypatch, state, expected):
    from ytm import auth

    method = None if state in ("logged_out", "invalid") else "browser"
    stub = StubManager(status=status(logged_in=state not in ("expired", "logged_out"), method=method, state=state))
    monkeypatch.setattr(auth, "auth_manager", lambda: stub)
    code, out, err = run("account")
    assert code == 0
    for line in expected:
        assert line in out.splitlines()
    if state in ("expired", "invalid", "logged_out"):
        assert "ytm login" in out


# -- logout -------------------------------------------------------------------


def test_logout_reports_cleanup_and_invalidates_clients(monkeypatch):
    from ytm import auth
    from ytm.authentication.manager import LogoutResult

    stub = StubManager(logout=LogoutResult(revision="r2", removed=("/home/u/.config/ytm/auth.json",)))
    monkeypatch.setattr(auth, "auth_manager", lambda: stub)
    invalidated = []
    monkeypatch.setattr(cli, "_reset_account_clients", lambda: invalidated.append(1))
    code, out, err = run("--json", "logout")
    assert code == 0
    assert stub.logged_out == 1
    assert invalidated == [1]
    data = json.loads(out)
    assert data["signed_out"] is True
    assert data["cleanup_failures"] == []
    assert data["cleaned"] == ["/home/u/.config/ytm/auth.json"]


def test_logout_text_is_the_documented_sentence(monkeypatch):
    from ytm import auth
    from ytm.authentication.manager import LogoutResult

    monkeypatch.setattr(auth, "auth_manager", lambda: StubManager(logout=LogoutResult(revision="r")))
    assert run("logout")[1].strip() == "Signed out of YTM on this computer."


def test_logout_reports_incomplete_cleanup_as_a_failure(monkeypatch):
    from ytm import auth
    from ytm.authentication.manager import LogoutResult

    stub = StubManager(logout=LogoutResult(
        revision="r", failed=(("/home/u/.config/ytm/auth.json", "permission denied"),)
    ))
    monkeypatch.setattr(auth, "auth_manager", lambda: stub)
    code, out, err = run("logout")
    assert code == 1
    assert "Signed out" in err and "auth.json" in err and "permission denied" in err


# -- invalid combinations fail before anything opens --------------------------


@pytest.mark.parametrize("argv", [
    ["login", "--method", "playwright", "--profile", "Default"],
    ["login", "--method", "playwright", "--authuser", "1"],
    ["login", "--client-id", "x"],
    ["login", "--from-browser", "chrome", "--method", "oauth"],
    ["login", "--browser", "chrome", "--from-browser"],
    ["login", "--timeout", "0"],
    ["login", "--timeout", "-5"],
])
def test_invalid_login_combinations_are_usage_errors(argv):
    with pytest.raises(SystemExit) as excinfo:
        run(*argv)
    assert excinfo.value.code == 2


# -- installing the browser binary -------------------------------------------


def test_install_browser_without_playwright_says_how(monkeypatch):
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: None)
    code, out, err = run("login", "--install-browser")
    assert code == 1
    assert "pip install \"ytm[login]\"" in err


def test_install_browser_runs_the_playwright_installer(monkeypatch):
    ran = []
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(cli.subprocess, "call", lambda command: (ran.append(command), 0)[1])
    code, out, err = run("login", "--install-browser", "--browser", "chrome")
    assert code == 0
    assert ran == [[cli.sys.executable, "-m", "playwright", "install", "chrome"]]
    assert "chrome installed" in out


def test_install_browser_reports_a_failing_installer(monkeypatch):
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(cli.subprocess, "call", lambda command: 3)
    code, out, err = run("login", "--install-browser")
    assert code == 1 and "exit 3" in err
