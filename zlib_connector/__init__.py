"""Public API for the independent Z-Library connector."""
from .client import Book
from .eapi import Client
from .search import SearchFilters, SearchPage
from .books import BookDetails, BookFormat, BookReference
from .account import AccountProfile, DownloadQuota
from .diagnostics import DomainDiagnostic, DomainReport, diagnose_domains
from .library import FavoriteUpdate, LibraryOverview
from .booklists import Booklist, BooklistBook, BooklistBookUpdate, BooklistDeletion, BooklistSettings
from .pagination import BookPage
from .errors import (
    AuthenticationError, ChallengeRequired, ConnectorError, DownloadError,
    FormatChangedError, MutationUncertainError, NetworkError, RateLimitError,
)

__all__ = [
    "Client", "Book", "BookDetails", "BookFormat", "BookReference", "BookPage",
    "SearchFilters", "SearchPage", "AccountProfile", "DownloadQuota",
    "Booklist", "BooklistBook", "BooklistBookUpdate", "BooklistDeletion", "BooklistSettings",
    "FavoriteUpdate", "LibraryOverview",
    "DomainDiagnostic", "DomainReport", "diagnose_domains",
    "ConnectorError", "AuthenticationError", "ChallengeRequired", "DownloadError",
    "FormatChangedError", "MutationUncertainError", "NetworkError", "RateLimitError",
]
