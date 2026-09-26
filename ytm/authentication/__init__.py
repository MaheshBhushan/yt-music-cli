"""Authentication: session models, storage, the manager, and browser login.

``ytm.auth`` re-exports the error types and delegates client construction
here; code that only needs a client should keep importing :mod:`ytm.auth`.
"""

from ytm.authentication import browser_login, session, storage
from ytm.authentication.errors import (
    AccountSelectionError,
    AuthError,
    AuthExpired,
    AuthInvalidFormat,
    AuthMissing,
    AuthStorageError,
    BrowserUnavailable,
    LoginCancelled,
    LoginTimedOut,
    SessionVerificationUnavailable,
)
from ytm.authentication.manager import AuthManager, LogoutResult, SessionStatus
from ytm.authentication.session import BrowserSession, VerifiedSession
from ytm.authentication.storage import SessionStore, StoredRecord, default_session_path

__all__ = [
    "AccountSelectionError",
    "AuthError",
    "AuthExpired",
    "AuthInvalidFormat",
    "AuthManager",
    "AuthMissing",
    "AuthStorageError",
    "BrowserSession",
    "BrowserUnavailable",
    "LoginCancelled",
    "LoginTimedOut",
    "LogoutResult",
    "SessionStatus",
    "SessionStore",
    "SessionVerificationUnavailable",
    "StoredRecord",
    "VerifiedSession",
    "browser_login",
    "default_session_path",
    "session",
    "storage",
]
