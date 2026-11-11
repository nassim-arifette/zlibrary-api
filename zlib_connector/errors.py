"""Stable error types for CLI users and future application integrations."""
class ConnectorError(Exception):
    """Base error. Messages never contain credentials, cookies or response bodies."""

class AuthenticationError(ConnectorError):
    pass

class ChallengeRequired(ConnectorError):
    pass

class RateLimitError(ConnectorError):
    pass

class FormatChangedError(ConnectorError):
    pass

class NetworkError(ConnectorError):
    def __init__(self, message: str, *, reason: str = "network"):
        super().__init__(message)
        self.reason = reason

class DownloadError(ConnectorError):
    """``reason`` is a stable cause: unavailable, no_link, refused, format_mismatch,
    protocol, size_limit, local_write, invalid_book, save_failed or download."""

    def __init__(self, message: str = "", *, reason: str = "download"):
        super().__init__(message)
        self.reason = reason


class MutationUncertainError(ConnectorError):
    """The account may have changed; read its current state before trying again."""


class BooklistNotFoundError(ValueError):
    """An editable booklist was not found; no account update was attempted."""
