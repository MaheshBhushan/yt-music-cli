"""The active credential record: one atomic, permission-restricted file.

A record says how ytm authenticates (a captured browser session, a legacy
OAuth token file, or a logged-out tombstone) and carries only what a client
needs. It is versioned and revisioned: every deliberate login or logout
creates a new revision, and a commit made against an older revision is
rejected, so a login that finishes after a logout cannot resurrect a
session.

The file backend is permission-restricted, not encrypted: a private
per-user application directory plus 0600 files where the platform supports
them. POSIX mode bits are not encryption, and Windows does not honour
``chmod``; the file is never described as encrypted.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import platformdirs
from ytmusicapi.auth.oauth.token import OAuthToken

from ytm.authentication import session as session_mod
from ytm.authentication.errors import AuthInvalidFormat, AuthStorageError

SCHEMA_VERSION = 1
METHODS = ("browser", "oauth", "none")

#: how long to wait for another process's commit before giving up
LOCK_TIMEOUT = 10.0
#: retained compatibility parameter; OS locks are never stolen by age
LOCK_STALE = 300.0


def default_session_path():
    """The per-user location of the active record (platformdirs rules)."""
    return Path(platformdirs.user_config_path("ytm", appauthor=False)) / "session.json"


def _timestamp(clock):
    return datetime.fromtimestamp(clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_revision():
    return uuid.uuid4().hex


@dataclass(frozen=True)
class StoredRecord:
    """One version of the active credential.

    Secret-bearing fields are excluded from ``repr``; ``headers`` is copied
    by ``to_payload``/constructors and must be copied again before it is
    handed to ytmusicapi, which mutates header state.
    """

    revision: str
    method: str
    created_at: str
    source: str = ""
    verified_at: str | None = None
    headers: dict | None = field(default=None, repr=False)
    user: str | None = None
    token_path: str | None = None
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def browser(cls, headers, *, user=None, source="interactive_browser", clock=time.time, verified_at=None):
        session = session_mod.build_session(headers, user=user, source=source)
        return cls(
            revision=_new_revision(),
            method="browser",
            created_at=_timestamp(clock),
            source=source,
            verified_at=verified_at or _timestamp(clock),
            headers=dict(session.headers),
            user=session.user,
        )

    @classmethod
    def oauth(cls, token_path, *, source="oauth", clock=time.time, verified_at=None):
        return cls(
            revision=_new_revision(),
            method="oauth",
            created_at=_timestamp(clock),
            source=source,
            verified_at=verified_at or _timestamp(clock),
            token_path=str(token_path),
        )

    @classmethod
    def tombstone(cls, *, clock=time.time):
        return cls(
            revision=_new_revision(),
            method="none",
            created_at=_timestamp(clock),
            source="logout",
        )

    def to_payload(self):
        payload = {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "method": self.method,
            "source": self.source,
            "created_at": self.created_at,
        }
        if self.verified_at:
            payload["verified_at"] = self.verified_at
        if self.method == "browser":
            payload["headers"] = dict(self.headers or {})
            payload["user"] = self.user
        elif self.method == "oauth":
            payload["token_path"] = self.token_path
        return payload


def record_from_payload(payload):
    """Parse and validate one stored record; raises AuthInvalidFormat.

    A malformed preferred record is an error, never a reason to silently
    fall through to another account.
    """
    if not isinstance(payload, dict):
        raise AuthInvalidFormat("The stored credential record is not an object.")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise AuthInvalidFormat(
            "The stored credential record uses an unsupported schema version; "
            "run 'ytm login' again."
        )
    revision = payload.get("revision")
    if not isinstance(revision, str) or not revision:
        raise AuthInvalidFormat("The stored credential record has no revision.")
    method = payload.get("method")
    if method not in METHODS:
        raise AuthInvalidFormat("The stored credential record has an unknown method.")
    created_at = payload.get("created_at")
    source = payload.get("source")
    verified_at = payload.get("verified_at")
    if method == "browser":
        headers = payload.get("headers")
        if not isinstance(headers, dict):
            raise AuthInvalidFormat("The stored browser session has no headers.")
        user = payload.get("user")
        if user is not None and not isinstance(user, str):
            raise AuthInvalidFormat("The stored browser session has an invalid user.")
        session = session_mod.build_session(headers, user=user, source=source or "stored")
        return StoredRecord(
            revision=revision,
            method="browser",
            created_at=created_at if isinstance(created_at, str) else "",
            source=source if isinstance(source, str) else "",
            verified_at=verified_at if isinstance(verified_at, str) else None,
            headers=dict(session.headers),
            user=session.user,
        )
    if method == "oauth":
        token_path = payload.get("token_path")
        if not isinstance(token_path, str) or not token_path:
            raise AuthInvalidFormat("The stored OAuth record has no token path.")
        return StoredRecord(
            revision=revision,
            method="oauth",
            created_at=created_at if isinstance(created_at, str) else "",
            source=source if isinstance(source, str) else "",
            verified_at=verified_at if isinstance(verified_at, str) else None,
            token_path=token_path,
        )
    return StoredRecord(
        revision=revision,
        method="none",
        created_at=created_at if isinstance(created_at, str) else "",
        source=source if isinstance(source, str) else "",
        verified_at=verified_at if isinstance(verified_at, str) else None,
    )


class SessionStore:
    """Reads and atomically replaces the active record at ``path``.

    ``legacy_path`` is the historical ``auth.json``; this class can read it
    for inventory and rollback but never rewrites it during ordinary reads.
    """

    def __init__(
        self,
        path=None,
        legacy_path=None,
        *,
        clock=time.time,
        sleep=time.sleep,
        lock_timeout=LOCK_TIMEOUT,
        stale_lock=LOCK_STALE,
    ):
        self.path = Path(path) if path is not None else default_session_path()
        self.legacy_path = Path(legacy_path) if legacy_path is not None else None
        self.clock = clock
        self.sleep = sleep
        self.lock_timeout = lock_timeout
        self.stale_lock = stale_lock  # retained constructor compatibility
        import threading
        self._thread_lock = threading.RLock()
        self._lock_depth = 0

    # -- reading ---------------------------------------------------------------

    def load(self):
        """The active record, or None when no record exists yet.

        A record that exists but cannot be understood raises
        AuthInvalidFormat; only a missing file means "no record".
        """
        if self.path.is_symlink():
            raise AuthStorageError(
                f"Refusing to read {self.path}: it is a symlink, not a managed credential file."
            )
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except UnicodeError as exc:
            raise AuthInvalidFormat("Stored credentials are not valid UTF-8; run 'ytm login' again.") from exc
        except OSError as exc:
            raise AuthStorageError(f"Could not read the stored credentials at {self.path}.") from exc
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise AuthInvalidFormat(
                "The stored credential record is not valid JSON; run 'ytm login' again."
            ) from exc
        return record_from_payload(payload)

    def read_legacy(self):
        """The legacy ``auth.json`` as raw headers/token, or None when absent.

        A file that exists but cannot be read as a JSON object raises: it may
        not silently read as "logged out" and invite a different account to
        take over.
        """
        if self.legacy_path is None:
            return None
        try:
            with open(self.legacy_path, encoding="utf-8") as file:
                payload = json.load(file)
        except FileNotFoundError:
            return None
        except ValueError as exc:
            raise AuthInvalidFormat(
                "The stored credentials are not valid JSON; run 'ytm login' to replace them."
            ) from exc
        except OSError as exc:
            raise AuthStorageError("Could not read the stored credentials.") from exc
        if not isinstance(payload, dict):
            raise AuthInvalidFormat(
                "The stored credentials have the wrong shape; run 'ytm login' to replace them."
            )
        return payload

    def effective(self):
        """The credential that is actually in force: a record or the legacy file.

        The legacy file is translated in memory and never rewritten; its
        revision is derived from (mtime, size) so a refreshed browser import
        or an old ``ytm auth`` still changes the stamp.
        """
        record = self.load()
        if record is not None:
            return record
        return self._legacy_record()

    def _legacy_record(self):
        legacy = self.read_legacy()
        if legacy is None:
            return None
        try:
            stat = os.stat(self.legacy_path)
            revision = f"legacy:{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            revision = "legacy"
        if OAuthToken.is_oauth(legacy):
            return StoredRecord(
                revision=revision,
                method="oauth",
                created_at="",
                source="legacy",
                token_path=str(self.legacy_path),
            )
        session = session_mod.build_session(legacy, source="legacy")
        return StoredRecord(
            revision=revision,
            method="browser",
            created_at="",
            source="legacy",
            headers=dict(session.headers),
            user=session.user,
        )

    def remove_managed_credentials(self):
        """Delete every managed credential file except the active record.

        Returns ``(removed_paths, failures)`` where each failure is a
        ``(path, reason)`` pair. The tombstone record itself is kept: it is
        what stops an older copy from coming back.
        """
        removed = []
        failed = []
        for path in self.managed_credential_paths():
            if path == self.path:
                continue
            try:
                if path.is_symlink() or path.exists():
                    path.unlink()
                    removed.append(str(path))
            except OSError as exc:
                failed.append((str(path), str(exc)))
        root = self.path.parent / "oauth"
        if root.is_dir() and not root.is_symlink():
            import re
            import shutil
            try:
                directories = list(root.iterdir())
            except OSError:
                failed.append((str(root), "could not enumerate OAuth credentials"))
                directories = []
            for directory in directories:
                if re.fullmatch(r"[0-9a-f]{32}", directory.name):
                    try:
                        if directory.is_symlink():
                            directory.unlink()
                        else:
                            shutil.rmtree(directory)
                        removed.append(str(directory))
                    except OSError:
                        failed.append((str(directory), "could not remove OAuth credential generation"))
        return removed, failed

    def managed_credential_paths(self):
        """Every file ytm can have written while holding this user's session.

        Used by logout to remove the whole inventory; failures are reported,
        not ignored. The packaged/default OAuth clients are not here: those
        are app credentials, not a session.
        """
        paths = [self.path, self.path.parent / "cookies.txt"]
        if self.legacy_path is not None:
            parent = self.legacy_path.parent
            paths += [
                self.legacy_path,
                parent / (self.legacy_path.stem + ".source.json"),
                parent / "cookies.txt",
                parent / "oauth_client.json",
                parent / "oauth_desktop_client.json",
            ]
        return list(dict.fromkeys(paths))

    # -- writing ---------------------------------------------------------------

    def save(self, record, *, expected_revision):
        """Atomically replace the active record, refusing a stale commit.

        ``expected_revision`` is the revision observed before the work that
        produced ``record`` began; None means "no record was expected".
        """
        if not isinstance(record, StoredRecord) or not record.revision:
            raise AuthStorageError("Refusing to store an invalid credential record.")
        payload = record.to_payload()
        record_from_payload(payload)
        self._ensure_directory()
        with self._lock():
            try:
                current = self.load()
            except AuthInvalidFormat:
                if expected_revision is not None:
                    # it was readable when this work began: refuse to
                    # overwrite a record that changed underneath it
                    raise AuthStorageError(
                        "The stored credentials changed while this login was in progress; "
                        "nothing was replaced. Run 'ytm login' again."
                    ) from None
                current = None  # an explicit new login replaces an unreadable record
            current_revision = current.revision if current is not None else None
            if current_revision != expected_revision:
                raise AuthStorageError(
                    "The stored credentials changed while this login was in progress; "
                    "nothing was replaced. Run 'ytm login' again."
                )
            self._write_atomic(payload)
            stored = self.load()
            if stored is None or stored.revision != record.revision:
                raise AuthStorageError("The new credentials could not be read back.")
            return stored

    def logout(self, *, expected_revision):
        """Replace the active record with a credential-free tombstone."""
        return self.save(StoredRecord.tombstone(clock=self.clock), expected_revision=expected_revision)

    def logout_and_cleanup(self, *, expected_revision):
        self._ensure_directory()
        with self._lock():
            record = self.logout(expected_revision=expected_revision)
            removed, failed = self.remove_managed_credentials()
            return record, removed, failed

    def _ensure_directory(self):
        parent = self.path.parent
        created = not parent.exists()
        try:
            if parent.is_symlink():
                raise AuthStorageError("Credential directory must not be a symlink.")
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise AuthStorageError(f"Could not create {parent} for credential storage.") from exc
        if created and os.name != "nt":
            try:
                os.chmod(parent, 0o700)
            except OSError as exc:
                raise AuthStorageError("Could not protect the credential directory.") from exc

    def _write_atomic(self, payload):
        parent = self.path.parent
        if self.path.is_symlink():
            raise AuthStorageError(
                f"Refusing to replace {self.path}: it is a symlink, not a managed credential file."
            )
        tmp = parent / f".{self.path.name}.tmp{os.getpid()}.{uuid.uuid4().hex[:8]}"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            raise AuthStorageError(f"Could not create a temporary credential file in {parent}.") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(payload, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, self.path)
        except (OSError, TypeError, ValueError) as exc:
            raise AuthStorageError("Could not write the credential record.") from exc
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
        self._sync_directory(parent)

    def _sync_directory(self, parent):
        if os.name == "nt":  # no directory handles in the Windows CRT
            return
        with contextlib.suppress(OSError):
            fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    # -- cross-process lock -----------------------------------------------------

    @contextlib.contextmanager
    def transaction(self):
        """Hold the credential lock for a short derived-state read/write.

        The same lock login and logout use, so an export cannot interleave
        with a credential change. Callers must not perform network I/O
        inside; the scope is a local read plus an atomic file write.
        """
        self._ensure_directory()
        with self._lock():
            yield self

    @contextlib.contextmanager
    def _lock(self):
        """OS advisory lock: released on process death, never stolen by age."""
        with self._thread_lock:
            if self._lock_depth:
                self._lock_depth += 1
                try:
                    yield
                finally:
                    self._lock_depth -= 1
                return
            lock_path = self.path.with_name(self.path.name + ".lock")
            try:
                if lock_path.is_symlink():
                    raise AuthStorageError("Credential lock must not be a symlink.")
                fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            except OSError as exc:
                raise AuthStorageError("Could not open credential lock.") from exc
            locked = False
            try:
                deadline = time.monotonic() + self.lock_timeout
                while not locked:
                    try:
                        self._os_lock(fd, True)
                        locked = True
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise AuthStorageError("Another ytm process is changing credentials; try again.") from None
                        self.sleep(0.05)
                self._lock_depth = 1
                yield
            finally:
                self._lock_depth = 0
                try:
                    if locked:
                        self._os_lock(fd, False)
                finally:
                    os.close(fd)

    @staticmethod
    def _os_lock(fd, acquire):
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN)
