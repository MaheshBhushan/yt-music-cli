"""AuthManager: verify a candidate before committing it, then hand out clients.

Nothing here talks to a browser or to the CLI; the browser runner, the
YTMusic factory, the account probe and the clock are injectable so tests
can drive the whole flow with fakes. A candidate is only written to the
store by ``save_verified``, and only with the revision observed when the
login began -- a commit that loses a race against a logout is rejected.
"""

from __future__ import annotations

import time
import re
from dataclasses import dataclass

import requests
import ytmusicapi
from ytmusicapi.exceptions import YTMusicError

from ytm.authentication import session as session_mod
from ytm.authentication.errors import (
    AccountSelectionError,
    AuthExpired,
    AuthInvalidFormat,
    AuthMissing,
    SessionVerificationUnavailable,
)
from ytm.authentication.storage import StoredRecord

MISSING_HINT = "No YouTube Music account is signed in. Run 'ytm login'."


def default_client_factory(headers, user=None):
    """The account client for browser headers; a fresh dict every time."""
    return ytmusicapi.YTMusic(dict(headers), user=user)


def default_probe(client):
    """The supported read-only account probe: account/account_menu."""
    return client.get_account_info()


def _rejected_as_unauthenticated(exc):
    return re.match(r"Server returned HTTP 401(?:\D|$)", str(exc)) is not None


class AuthManager:
    def __init__(
        self,
        store,
        *,
        client_factory=default_client_factory,
        probe=default_probe,
        oauth_client_factory=None,
        anonymous_client_factory=ytmusicapi.YTMusic,
        clock=time.time,
    ):
        self.store = store
        self.client_factory = client_factory
        self.probe = probe
        self.oauth_client_factory = oauth_client_factory
        self.anonymous_client_factory = anonymous_client_factory
        self.clock = clock

    # -- discovery -----------------------------------------------------------

    def active_record(self):
        return self.store.load()

    def expected_revision(self):
        """The revision a commit must be based on; None when none exists.

        A record too broken to read is treated as replaceable by an explicit
        login or logout -- refusing to repair it would trap the user -- while
        a commit that expected a *readable* revision still cannot overwrite
        an unreadable one behind its back (see SessionStore.save).
        """
        try:
            record = self.store.load()
        except AuthInvalidFormat:
            return None
        return record.revision if record is not None else None

    def effective_record(self):
        """The record in force, including a translated legacy file."""
        return self.store.effective()

    def has_credentials(self):
        """A locally present usable record; not a claim about server validity."""
        try:
            record = self.store.effective()
        except AuthInvalidFormat:
            return False
        return record is not None and record.method != "none"

    # -- login ---------------------------------------------------------------

    def _read_account(self, client):
        """The account probe, with provider failures classified safely."""
        try:
            account = self.probe(client)
            name = account.get("accountName") if isinstance(account, dict) else None
            if not isinstance(name, str) or not session_mod.safe_text(name):
                raise AccountSelectionError("YouTube Music did not return a readable account identity.")
            return account
        except requests.exceptions.RequestException as exc:
            raise SessionVerificationUnavailable(
                "Could not reach YouTube Music to verify the sign-in; check connectivity "
                "and run 'ytm login' again. Existing credentials were kept."
            ) from exc
        except YTMusicError as exc:
            if _rejected_as_unauthenticated(exc):
                raise AuthExpired(
                    "YouTube Music rejected the session as signed out. "
                    "Existing credentials were kept."
                ) from exc
            raise SessionVerificationUnavailable(
                "YouTube Music answered with an unexpected response while verifying the "
                "session; existing credentials were kept. ytmusicapi may need an update."
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionVerificationUnavailable(
                "YouTube Music's account response could not be read; existing "
                "credentials were kept. ytmusicapi may need an update."
            ) from exc

    def validate_candidate(self, candidate):
        """Prove a captured session can read account details, or raise.

        Returns a VerifiedSession; nothing is written here. The candidate is
        validated with a fresh client built from a copy of its headers, so a
        rejected candidate cannot have touched stored state.
        """
        session = session_mod.build_session(
            dict(candidate.headers), user=candidate.user, source=candidate.source
        )
        try:
            client = self.client_factory(dict(session.headers), session.user)
        except (YTMusicError, requests.RequestException, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, YTMusicError) and _rejected_as_unauthenticated(exc):
                raise AuthExpired("Your YouTube Music session has expired. Run 'ytm login'.") from exc
            raise SessionVerificationUnavailable(
                "Could not prepare the YouTube Music session; existing credentials were kept."
            ) from exc
        account = self._read_account(client)
        if not isinstance(account, dict) or not account.get("accountName"):
            raise AccountSelectionError(
                "YouTube Music did not return an account for the captured session; "
                "existing credentials were kept."
            )
        return session_mod.VerifiedSession(
            session=session,
            account_name=session_mod.safe_text(account.get("accountName")),
            verified_at=_timestamp(self.clock),
        )

    def save_verified(self, verified, *, expected_revision):
        """Commit a verified browser session; stale commits are rejected."""
        session = verified.session
        record = StoredRecord.browser(
            dict(session.headers),
            user=session.user,
            source=session.source,
            clock=self.clock,
            verified_at=verified.verified_at or None,
        )
        return self.store.save(record, expected_revision=expected_revision)

    def save_oauth(self, token_path, *, expected_revision):
        """Activate an already-verified OAuth token file."""
        record = StoredRecord.oauth(token_path, clock=self.clock, verified_at=_timestamp(self.clock))
        return self.store.save(record, expected_revision=expected_revision)

    # -- clients -------------------------------------------------------------

    def get_client(self, *, require_auth=False):
        """The client for stored credentials, or an anonymous one for guests.

        With ``require_auth=True`` missing credentials raise AuthMissing,
        which is what account commands and account-only operations want.
        """
        if not require_auth:
            return self.anonymous_client_factory()
        record = self.store.effective()
        if record is None or record.method == "none":
            raise AuthMissing(MISSING_HINT)
        if record.method == "browser":
            return self.client_factory(dict(record.headers or {}), record.user)
        if record.method == "oauth":
            if self.oauth_client_factory is None:
                raise AuthMissing(MISSING_HINT)
            return self.oauth_client_factory(record)
        raise AuthInvalidFormat(
            "The stored credential record uses an unknown method; run 'ytm login' again."
        )

    # -- status --------------------------------------------------------------

    def status(self, *, validate=True):
        """A SessionStatus; only ``validate=True`` may touch the network.

        A local record is never reported as "valid" without a probe: with
        validation off the state is "not_checked", and an unreachable
        provider leaves validity "unknown" rather than claiming expiry.
        """
        try:
            record = self.store.effective()
        except AuthInvalidFormat:
            return SessionStatus(
                logged_in=False,
                method=None,
                state="invalid",
                detail="The stored credentials are unreadable; run 'ytm login' to replace them.",
            )
        if record is None or record.method == "none":
            return SessionStatus(logged_in=False, method=None, state="logged_out", detail=MISSING_HINT)
        method = record.method
        if not validate:
            return SessionStatus(logged_in=True, method=method, state="not_checked")
        try:
            client = self.get_client(require_auth=True)
            account = self._read_account(client)
        except AuthMissing as exc:
            # a record exists but cannot be turned into a client (e.g. an
            # OAuth token whose client file is gone): stored, not logged out
            return SessionStatus(
                logged_in=False, method=method, state="invalid", detail=str(exc)
            )
        except AuthExpired:
            return SessionStatus(
                logged_in=False,
                method=method,
                state="expired",
                detail="The session was rejected; run 'ytm login' again.",
            )
        except (SessionVerificationUnavailable, AuthInvalidFormat, AccountSelectionError,
                requests.RequestException, KeyError, TypeError, ValueError, YTMusicError):
            return SessionStatus(
                logged_in=True,
                method=method,
                state="unknown",
                detail="Stored credentials are present but could not be verified.",
            )
        name = account.get("accountName") if isinstance(account, dict) else None
        return SessionStatus(
            logged_in=True,
            method=method,
            state="valid",
            account_name=session_mod.safe_text(name) if name else None,
        )

    # -- logout --------------------------------------------------------------

    def logout(self):
        """Sign out locally: tombstone first, then delete managed copies.

        Idempotent, and never contacts Google. The tombstone goes down before
        any file is deleted, so a crash mid-cleanup can never leave legacy
        credentials able to come back. Deletion failures are reported, not
        hidden.
        """
        record, removed, failed = self.store.logout_and_cleanup(
            expected_revision=self.expected_revision()
        )
        return LogoutResult(revision=record.revision, removed=tuple(removed), failed=tuple(failed))


def _timestamp(clock):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class LogoutResult:
    """What a logout removed, and what it could not."""

    revision: str
    removed: tuple = ()
    failed: tuple = ()


class SessionStatus:
    """What `ytm account` reports: honest about how much is known."""

    __slots__ = ("logged_in", "method", "state", "account_name", "detail")

    def __init__(self, *, logged_in, method, state, account_name=None, detail=""):
        self.logged_in = logged_in
        self.method = method
        self.state = state
        self.account_name = account_name
        self.detail = detail

    def __repr__(self):
        return (
            f"SessionStatus(logged_in={self.logged_in!r}, method={self.method!r}, "
            f"state={self.state!r})"
        )
