"""Clients, locks, the restore-only session and the lazy download manager behind the MCP tools."""

from contextlib import contextmanager
from pathlib import Path
import threading
import time

from ..errors import AuthenticationError
from ..http_api.contract import RequestError
from ..session_data import canonical_origin
from ..sessions import SessionError


LOCK_WAIT = 10.0
PUBLIC_WAIT = 0.5
QUOTA_WAIT = 1.0
_SLICE = 0.1


class ToolError(Exception):
    """A tool failure carrying the public error envelope."""

    def __init__(self, code, *, retryable=False, outcome="not_applied", message=None):
        super().__init__(code)
        self.code, self.retryable, self.outcome, self.message = code, retryable, outcome, message

    @classmethod
    def from_request(cls, error: RequestError):
        return cls(error.code, retryable=error.retryable, outcome=error.outcome, message=error.message)


def _new_manager(client, directory, *, client_lock, max_tasks):
    from ..jobs.manager import DownloadManager
    return DownloadManager(client, directory, client_lock=client_lock, max_tasks=max_tasks)


class Backend:
    """Own the account Client (one lock), a lazy anonymous public Client and the download manager.

    The account Client is never used without ``account_lock``. The manager's
    worker shares that lock for a whole task. Nothing here prompts or reads
    credentials: the only sign-in path is restoring the saved session.
    """

    def __init__(self, *, domain, timeout, client_factory, store=None, anonymous=False,
                 download_dir=None, max_tasks=1000, logger=None):
        self.domain = domain
        self.timeout = timeout
        self.anonymous = anonymous
        self.download_dir = Path(download_dir) if download_dir is not None else None
        self.max_tasks = max_tasks
        self._factory = client_factory
        self._store = store
        self._log = logger
        self.lock_wait, self.public_wait, self.quota_wait = LOCK_WAIT, PUBLIC_WAIT, QUOTA_WAIT
        self.account_lock = threading.Lock()
        self._public_lock = threading.Lock()
        self._public_create = threading.Lock()
        self._manager_lock = threading.Lock()
        self._public = None
        self._manager = None
        self._closed = False
        self._signed_in = False
        self._failed_generation = _NO_GENERATION
        self.account = None if anonymous else client_factory(domain, timeout=timeout)

    # Locks ------------------------------------------------------------------

    @staticmethod
    def _acquire(lock, seconds, context):
        deadline = time.monotonic() + seconds
        while True:
            if context is not None and context.is_cancelled:
                return False
            remaining = deadline - time.monotonic()
            if lock.acquire(timeout=max(0.0, min(_SLICE, remaining))):
                return True
            if remaining <= 0:
                return False

    def _public_client(self):
        with self._public_create:
            if self._public is None:
                self._public = self._factory(self.domain, timeout=self.timeout)
            return self._public

    # Session ----------------------------------------------------------------

    def _snapshot(self):
        try:
            return self._store.read()
        except SessionError:
            return None

    def session_status(self) -> dict:
        if self.anonymous:
            return {"state": "none", "expires_at": None}
        snapshot = self._snapshot()
        session = snapshot.session if snapshot is not None else None
        expires = session.expires_at if session is not None else None
        if self._signed_in:
            return {"state": "signed_in", "expires_at": expires}
        if snapshot is None:
            return {"state": "rejected", "expires_at": None}
        if session is None:
            return {"state": "empty", "expires_at": None}
        if session.expired:
            return {"state": "expired", "expires_at": expires}
        if session.origin != canonical_origin(self.domain) or snapshot.generation == self._failed_generation:
            return {"state": "rejected", "expires_at": expires}
        return {"state": "stored", "expires_at": expires}

    def _ensure_session(self) -> bool:
        """Restore the saved session at most once per generation; call with account_lock held."""
        if self._signed_in:
            return True
        snapshot = self._snapshot()
        if snapshot is None or snapshot.session is None or snapshot.session.expired:
            return False
        if snapshot.session.origin != canonical_origin(self.domain):
            return False
        if snapshot.generation == self._failed_generation:
            return False
        try:
            self.account.restore_session(self._store)
        except AuthenticationError:
            self._failed_generation = snapshot.generation
            return False
        self._signed_in = True
        return True

    # Client access ----------------------------------------------------------

    @contextmanager
    def public(self, context):
        """Yield (client, signed_in). Prefer the account Client; fall back to the anonymous one while it is busy."""
        if self.anonymous:
            client, lock, signed_in = self._public_client(), self._public_lock, False
        elif self._acquire(self.account_lock, self.public_wait, context):
            try:
                signed_in = self._ensure_session()
            except BaseException:
                self.account_lock.release()
                raise
            try:
                yield self.account, signed_in
            finally:
                self.account_lock.release()
            return
        else:
            client, lock, signed_in = self._public_client(), self._public_lock, False
        if not self._acquire(lock, self.lock_wait, context):
            raise ToolError("client_busy", retryable=True)
        try:
            yield client, signed_in
        finally:
            lock.release()

    @contextmanager
    def account_client(self, context, *, wait=None):
        """Yield the signed-in account Client, or fail with client_busy / upstream_authentication_required."""
        if self.anonymous:
            raise ToolError("upstream_authentication_required")
        if not self._acquire(self.account_lock, self.lock_wait if wait is None else wait, context):
            raise ToolError("client_busy", retryable=True)
        try:
            if not self._ensure_session():
                raise ToolError("upstream_authentication_required")
            yield self.account
        finally:
            self.account_lock.release()

    def try_account(self, context, seconds):
        """Return True with account_lock held if it was free within seconds (caller releases)."""
        return self._acquire(self.account_lock, seconds, context)

    # Downloads --------------------------------------------------------------

    def manager(self, context):
        from ..http_api.downloads import job_error
        from ..jobs.models import JobBusyError, JobError
        if self.download_dir is None:
            raise ToolError("downloads_unavailable")
        with self._manager_lock:
            if self._closed:
                raise ToolError.from_request(job_error(JobBusyError()))
            if self._manager is not None:
                return self._manager
            with self.account_client(context):
                pass
            try:
                # The constructor takes account_lock itself; it must not be held here.
                self._manager = _new_manager(self.account, self.download_dir, client_lock=self.account_lock,
                                             max_tasks=self.max_tasks)
            except JobError as error:
                raise ToolError.from_request(job_error(error)) from None
            return self._manager

    def close(self):
        with self._manager_lock:
            self._closed = True
            manager, self._manager = self._manager, None
        if manager is not None:
            try:
                manager.close()
            except Exception:
                if self._log is not None:
                    self._log.warning("the download manager did not close cleanly")
        for client in (self.account, self._public):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass


class _NoGeneration:
    def __eq__(self, other):
        return False

    __hash__ = object.__hash__


_NO_GENERATION = _NoGeneration()
