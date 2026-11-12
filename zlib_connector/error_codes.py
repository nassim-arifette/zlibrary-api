"""Stable error codes shared by the CLI, the HTTP API and the MCP server.

This module imports no adapter. It decides a code, whether the same request may
succeed later (``retryable``, advisory only), and whether an account change or
a quota-using download may have taken effect (``outcome``). Nothing here retries.
"""
from typing import NamedTuple

from .errors import (AuthenticationError, BooklistNotFoundError, ChallengeRequired, ConnectorError, DownloadError,
                     FormatChangedError, MutationUncertainError, NetworkError, RateLimitError)
from .sessions import SessionConflictError, SessionError, SessionLockTimeoutError
from .transfer import TransferCancelled


class ErrorCode(NamedTuple):
    code: str
    retryable: bool = False
    outcome: str = "not_applied"
    quota_may_be_used: bool | None = None  # Set only for a single-book download.


class Coded:
    """Mixin for exceptions that carry their own classification (local adapters only)."""

    def classification(self) -> ErrorCode:  # pragma: no cover - implemented by subclasses
        raise NotImplementedError


# Explicit refusals: the upstream service answered and did not apply the change.
_REFUSALS = (BooklistNotFoundError, AuthenticationError, ChallengeRequired, RateLimitError, DownloadError)
_CANCELLATIONS = (TransferCancelled, KeyboardInterrupt, EOFError)


def may_have_used_quota(error: BaseException, stage: str | None) -> bool:
    """True once a download's link request was sent, unless the link request was explicitly refused."""
    if stage is None:
        return False
    if stage == "link" and (isinstance(error, (RateLimitError, AuthenticationError)) or (
            isinstance(error, DownloadError) and getattr(error, "reason", None) in ("refused", "unavailable", "no_link"))):
        return False  # An explicit refusal of the link request.
    return True


def _cancellation(error: BaseException) -> bool:
    return isinstance(error, _CANCELLATIONS)


def _read(error: BaseException, *, mutation: bool) -> ErrorCode:
    if _cancellation(error):
        return ErrorCode("cancelled", outcome="unknown" if mutation else "not_applied")
    if isinstance(error, Coded):
        return error.classification()
    if isinstance(error, BooklistNotFoundError):
        return ErrorCode("booklist_not_found")
    if isinstance(error, MutationUncertainError):
        return ErrorCode("mutation_uncertain", outcome="unknown")
    if isinstance(error, AuthenticationError):
        return ErrorCode("upstream_authentication_required")
    if isinstance(error, ChallengeRequired):
        return ErrorCode("upstream_challenge")
    if isinstance(error, RateLimitError):
        return ErrorCode("upstream_rate_limited", retryable=not mutation)
    if isinstance(error, DownloadError):
        return ErrorCode("upstream_refused")
    if mutation:
        # Mutations never claim not_applied on doubt, as the HTTP API treats the whole route.
        return ErrorCode("mutation_uncertain", outcome="unknown")
    if isinstance(error, NetworkError):
        timeout = getattr(error, "reason", None) == "timeout"
        return ErrorCode("upstream_timeout" if timeout else "upstream_network_error", retryable=True)
    if isinstance(error, FormatChangedError):
        return ErrorCode("upstream_protocol_changed")
    if isinstance(error, SessionLockTimeoutError):
        return ErrorCode("session_busy", retryable=True)
    if isinstance(error, SessionConflictError):
        return ErrorCode("session_conflict")
    if isinstance(error, SessionError):
        return ErrorCode("session_error")
    if isinstance(error, ConnectorError):
        return ErrorCode("upstream_error")
    if isinstance(error, ValueError):
        return ErrorCode("invalid_argument")
    if isinstance(error, OSError):
        return ErrorCode("local_io_error")
    return ErrorCode("internal_error")


def _download(error: BaseException) -> ErrorCode:
    used = may_have_used_quota(error, getattr(error, "download_stage", None))
    doubt = "unknown" if used else "not_applied"
    if _cancellation(error):
        return ErrorCode("cancelled", outcome=doubt, quota_may_be_used=used)
    if isinstance(error, DownloadError):
        reason = getattr(error, "reason", None)
        if reason in ("unavailable", "no_link") and not used:
            return ErrorCode("download_unavailable", retryable=True, quota_may_be_used=False)
        code = "upstream_refused" if reason == "refused" else "download_failed"
        return ErrorCode(code, outcome=doubt, quota_may_be_used=used)
    cause = _read(error, mutation=False)
    if used:
        return ErrorCode(cause.code, retryable=False, outcome="unknown", quota_may_be_used=True)
    return cause._replace(quota_may_be_used=False)


def classify(error: BaseException, *, mutation: bool = False, download: bool = False) -> ErrorCode:
    """The one mapping from an exception to a stable code.

    ``mutation``: the failing call was an account change. ``download``: it was a
    single-book ``Client.download``, whose exceptions may carry ``download_stage``.
    """
    if download:
        return _download(error)
    return _read(error, mutation=mutation)
