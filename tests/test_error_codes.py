"""classify() is the one mapping from an exception to a stable code, shared by the CLI, HTTP and MCP."""
import unittest

from zlib_connector import batch
from zlib_connector.error_codes import ErrorCode, classify, may_have_used_quota
from zlib_connector.errors import (AuthenticationError, BooklistNotFoundError, ChallengeRequired, ConnectorError,
                                   DownloadError, FormatChangedError, MutationUncertainError, NetworkError,
                                   RateLimitError)
from zlib_connector.http_api.contract import RequestError
from zlib_connector.http_api.server import upstream_error
from zlib_connector.sessions import SessionConflictError, SessionError, SessionLockTimeoutError
from zlib_connector.transfer import TransferCancelled


NETWORK_REASONS = ("dns", "blocked_address", "certificate", "tls", "timeout", "redirect", "http", "network")
DOWNLOAD_REASONS = ("unavailable", "no_link", "refused", "format_mismatch", "protocol", "size_limit", "local_write",
                    "invalid_book", "save_failed", "download")


def staged(error, stage):
    if stage is not None:
        error.download_stage = stage
    return error


def reference_upstream_error(error, *, mutation):
    """The HTTP mapping before error_codes existed, kept verbatim to prove the behavior did not change."""
    if isinstance(error, BooklistNotFoundError):
        return RequestError(404, "booklist_not_found", "The booklist was not found in the account's editable lists.")
    if isinstance(error, MutationUncertainError):
        return RequestError(409, "mutation_uncertain", "The server may have applied this change. Check the account before trying again.", outcome="unknown")
    if isinstance(error, AuthenticationError):
        return RequestError(424, "upstream_authentication_required", "The Z-Library session is missing or expired.")
    if isinstance(error, ChallengeRequired):
        return RequestError(502, "upstream_challenge", "Z-Library requires browser verification.")
    if isinstance(error, RateLimitError):
        return RequestError(429, "upstream_rate_limited", "Z-Library rejected the request because of a limit.", retryable=not mutation)
    if isinstance(error, NetworkError):
        if mutation:
            return reference_upstream_error(MutationUncertainError(), mutation=True)
        timeout = error.reason == "timeout"
        return RequestError(504 if timeout else 502, "upstream_timeout" if timeout else "upstream_network_error",
                            "The Z-Library request could not be completed.", retryable=True)
    if isinstance(error, FormatChangedError):
        if mutation:
            return reference_upstream_error(MutationUncertainError(), mutation=True)
        return RequestError(502, "upstream_protocol_changed", "Z-Library returned an unsupported response.")
    if isinstance(error, DownloadError):
        return RequestError(422, "upstream_refused", "Z-Library refused the operation.")
    if mutation:
        return reference_upstream_error(MutationUncertainError(), mutation=True)
    return RequestError(502 if isinstance(error, ConnectorError) else 500,
                        "upstream_error" if isinstance(error, ConnectorError) else "internal_error",
                        "The request could not be completed.")


def every_error():
    yield BooklistNotFoundError("synthetic")
    yield MutationUncertainError("synthetic")
    yield AuthenticationError("synthetic")
    yield ChallengeRequired("synthetic")
    yield RateLimitError("synthetic")
    for reason in NETWORK_REASONS:
        yield NetworkError("synthetic", reason=reason)
    yield FormatChangedError("synthetic")
    for reason in DOWNLOAD_REASONS:
        yield DownloadError("synthetic", reason=reason)
    yield SessionLockTimeoutError("synthetic")
    yield SessionConflictError("synthetic")
    yield SessionError("synthetic")
    yield TransferCancelled()
    yield ConnectorError("synthetic")
    yield ValueError("synthetic")
    yield OSError("synthetic")
    yield RuntimeError("synthetic")


class ReadContextTests(unittest.TestCase):
    def test_every_read_error_has_its_code(self):
        expected = [
            (BooklistNotFoundError(), ErrorCode("booklist_not_found")),
            (MutationUncertainError(), ErrorCode("mutation_uncertain", outcome="unknown")),
            (AuthenticationError(), ErrorCode("upstream_authentication_required")),
            (ChallengeRequired(), ErrorCode("upstream_challenge")),
            (RateLimitError(), ErrorCode("upstream_rate_limited", retryable=True)),
            (NetworkError("x", reason="timeout"), ErrorCode("upstream_timeout", retryable=True)),
            (FormatChangedError(), ErrorCode("upstream_protocol_changed")),
            (DownloadError("x", reason="refused"), ErrorCode("upstream_refused")),
            (SessionLockTimeoutError(), ErrorCode("session_busy", retryable=True)),
            (SessionConflictError(), ErrorCode("session_conflict")),
            (SessionError(), ErrorCode("session_error")),
            (ConnectorError(), ErrorCode("upstream_error")),
            (ValueError(), ErrorCode("invalid_argument")),
            (OSError(), ErrorCode("local_io_error")),
            (RuntimeError(), ErrorCode("internal_error")),
            (KeyboardInterrupt(), ErrorCode("cancelled")),
            (EOFError(), ErrorCode("cancelled")),
            (TransferCancelled(), ErrorCode("cancelled")),
        ]
        for error, code in expected:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(code, classify(error))
        for reason in NETWORK_REASONS:
            if reason != "timeout":
                with self.subTest(reason=reason):
                    self.assertEqual(ErrorCode("upstream_network_error", retryable=True),
                                     classify(NetworkError("x", reason=reason)))
        for reason in DOWNLOAD_REASONS:
            with self.subTest(reason=reason):
                self.assertEqual(ErrorCode("upstream_refused"), classify(DownloadError("x", reason=reason)))


class MutationContextTests(unittest.TestCase):
    def test_doubt_is_unknown_and_explicit_refusals_are_not_applied(self):
        for error in (MutationUncertainError(), NetworkError("x"), NetworkError("x", reason="timeout"),
                      FormatChangedError(), ConnectorError(), SessionError(), ValueError(), RuntimeError()):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(ErrorCode("mutation_uncertain", outcome="unknown"), classify(error, mutation=True))
        self.assertEqual(ErrorCode("upstream_rate_limited"), classify(RateLimitError(), mutation=True))
        self.assertEqual(ErrorCode("upstream_authentication_required"), classify(AuthenticationError(), mutation=True))
        self.assertEqual(ErrorCode("booklist_not_found"), classify(BooklistNotFoundError(), mutation=True))
        self.assertEqual(ErrorCode("cancelled", outcome="unknown"), classify(KeyboardInterrupt(), mutation=True))


class DownloadContextTests(unittest.TestCase):
    def test_no_stage_means_no_quota_was_used(self):
        self.assertEqual(ErrorCode("upstream_timeout", retryable=True, quota_may_be_used=False),
                         classify(NetworkError("x", reason="timeout"), download=True))
        self.assertEqual(ErrorCode("upstream_rate_limited", retryable=True, quota_may_be_used=False),
                         classify(RateLimitError(), download=True))
        self.assertEqual(ErrorCode("invalid_argument", quota_may_be_used=False), classify(ValueError(), download=True))

    def test_explicit_link_refusals_used_no_quota(self):
        for reason in ("unavailable", "no_link"):
            with self.subTest(reason=reason):
                self.assertEqual(ErrorCode("download_unavailable", retryable=True, quota_may_be_used=False),
                                 classify(staged(DownloadError("x", reason=reason), "link"), download=True))
        self.assertEqual(ErrorCode("upstream_refused", quota_may_be_used=False),
                         classify(staged(DownloadError("x", reason="refused"), "link"), download=True))
        self.assertEqual(ErrorCode("upstream_rate_limited", retryable=True, quota_may_be_used=False),
                         classify(staged(RateLimitError(), "link"), download=True))
        self.assertEqual(ErrorCode("upstream_authentication_required", quota_may_be_used=False),
                         classify(staged(AuthenticationError(), "link"), download=True))

    def test_after_the_link_request_the_outcome_is_unknown(self):
        self.assertEqual(ErrorCode("upstream_timeout", outcome="unknown", quota_may_be_used=True),
                         classify(staged(NetworkError("x", reason="timeout"), "link"), download=True))
        self.assertEqual(ErrorCode("upstream_network_error", outcome="unknown", quota_may_be_used=True),
                         classify(staged(NetworkError("x"), "transfer"), download=True))
        for reason in ("invalid_book", "format_mismatch", "protocol", "size_limit", "local_write", "save_failed",
                       "download", "unavailable"):
            with self.subTest(reason=reason):
                self.assertEqual(ErrorCode("download_failed", outcome="unknown", quota_may_be_used=True),
                                 classify(staged(DownloadError("x", reason=reason), "transfer"), download=True))
                if reason != "unavailable":  # Without a stage, unavailable is an explicit refusal.
                    self.assertEqual(ErrorCode("download_failed", quota_may_be_used=False),
                                     classify(DownloadError("x", reason=reason), download=True))
        self.assertEqual(ErrorCode("upstream_refused", outcome="unknown", quota_may_be_used=True),
                         classify(staged(DownloadError("x", reason="refused"), "transfer"), download=True))

    def test_cancellation_records_whether_quota_may_be_used(self):
        self.assertEqual(ErrorCode("cancelled", quota_may_be_used=False), classify(TransferCancelled(), download=True))
        self.assertEqual(ErrorCode("cancelled", outcome="unknown", quota_may_be_used=True),
                         classify(staged(TransferCancelled(), "transfer"), download=True))
        self.assertEqual(ErrorCode("cancelled", outcome="unknown", quota_may_be_used=True),
                         classify(staged(KeyboardInterrupt(), "link"), download=True))


class QuotaRuleTests(unittest.TestCase):
    def test_batch_uses_the_shared_rule(self):
        self.assertIs(batch.may_have_used_quota, may_have_used_quota)

    def test_rule_results(self):
        cases = [
            (NetworkError("x"), None, False),
            (NetworkError("x"), "link", True),
            (NetworkError("x"), "transfer", True),
            (RateLimitError(), "link", False),
            (RateLimitError(), "transfer", True),
            (AuthenticationError(), "link", False),
            (DownloadError("x", reason="refused"), "link", False),
            (DownloadError("x", reason="unavailable"), "link", False),
            (DownloadError("x", reason="no_link"), "link", False),
            (DownloadError("x", reason="format_mismatch"), "link", True),
            (DownloadError("x", reason="no_link"), "transfer", True),
            (TransferCancelled(), "link", True),
        ]
        for error, stage, used in cases:
            with self.subTest(error=type(error).__name__, stage=stage):
                self.assertIs(used, may_have_used_quota(error, stage))


class HttpParityTests(unittest.TestCase):
    def test_http_statuses_codes_and_messages_are_unchanged(self):
        for mutation in (False, True):
            for error in every_error():
                with self.subTest(error=type(error).__name__, reason=getattr(error, "reason", None), mutation=mutation):
                    actual = upstream_error(error, mutation=mutation)
                    expected = reference_upstream_error(error, mutation=mutation)
                    self.assertEqual((expected.status, expected.code, expected.message, expected.retryable,
                                      expected.outcome),
                                     (actual.status, actual.code, actual.message, actual.retryable, actual.outcome))


if __name__ == "__main__":
    unittest.main()
