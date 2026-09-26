"""Typed authentication failures.

Safe to show: every message is a sentence written for the user, never an
interpolated exception, header, or response body. Existing code imports
``AuthError``/``AuthMissing``/``AuthExpired`` from :mod:`ytm.auth`, which
re-exports these same classes, so identities stay stable.
"""


class AuthError(Exception):
    """Base class for authentication problems."""


class AuthMissing(AuthError):
    """No credentials have been stored yet."""


class AuthExpired(AuthError):
    """Stored credentials are present but no longer accepted by YouTube Music."""


class AuthInvalidFormat(AuthError):
    """A stored or captured credential is malformed."""


class AuthStorageError(AuthError):
    """Credentials could not be read or written safely."""


class BrowserUnavailable(AuthError):
    """No supported browser could be launched for interactive login."""


class LoginCancelled(AuthError):
    """The person closed the browser or declined the account selection."""


class LoginTimedOut(AuthError):
    """The interactive login did not complete within its deadline."""


class AccountSelectionError(AuthError):
    """The selected account could not be verified as the captured identity."""


class SessionVerificationUnavailable(AuthError):
    """The session could not be checked because the provider misbehaved."""
