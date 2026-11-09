"""Public API for the independent Z-Library connector."""
from .client import Book
from .eapi import Client
from .errors import AuthenticationError, ChallengeRequired, ConnectorError, DownloadError, FormatChangedError, NetworkError, RateLimitError

__all__ = ["Book", "Client", "ConnectorError", "AuthenticationError", "ChallengeRequired", "DownloadError", "FormatChangedError", "NetworkError", "RateLimitError"]
