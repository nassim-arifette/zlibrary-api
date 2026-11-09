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
    pass

class DownloadError(ConnectorError):
    pass
