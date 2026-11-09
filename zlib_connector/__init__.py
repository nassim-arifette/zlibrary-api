"""Public API for the independent Z-Library connector."""
from .client import Book
from .eapi import Client
from .search import SearchFilters, SearchPage
from .books import BookDetails, BookFormat, BookReference
from .account import AccountProfile, DownloadQuota
from .diagnostics import DomainDiagnostic, DomainReport, diagnose_domains
from .library import Booklist, FavoriteUpdate, LibraryOverview
from .pagination import BookPage
from .errors import AuthenticationError, ChallengeRequired, ConnectorError, DownloadError, FormatChangedError, MutationUncertainError, NetworkError, RateLimitError

__all__ = ["AccountProfile", "DownloadQuota", "DomainDiagnostic", "DomainReport", "diagnose_domains", "Book", "BookDetails", "BookFormat", "BookReference", "BookPage", "Booklist", "FavoriteUpdate", "LibraryOverview", "Client", "SearchFilters", "SearchPage", "ConnectorError", "AuthenticationError", "ChallengeRequired", "DownloadError", "FormatChangedError", "MutationUncertainError", "NetworkError", "RateLimitError"]
