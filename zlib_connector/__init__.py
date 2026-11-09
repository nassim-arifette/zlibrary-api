"""Public API for the independent Z-Library connector."""
from .client import Book
from .eapi import Client
from .search import SearchFilters, SearchPage
from .books import BookDetails, BookFormat, BookReference
from .account import AccountProfile, DownloadQuota
from .diagnostics import DomainDiagnostic, DomainReport, diagnose_domains
from .errors import AuthenticationError, ChallengeRequired, ConnectorError, DownloadError, FormatChangedError, NetworkError, RateLimitError

__all__ = ["AccountProfile", "DownloadQuota", "DomainDiagnostic", "DomainReport", "diagnose_domains", "Book", "BookDetails", "BookFormat", "BookReference", "Client", "SearchFilters", "SearchPage", "ConnectorError", "AuthenticationError", "ChallengeRequired", "DownloadError", "FormatChangedError", "NetworkError", "RateLimitError"]
