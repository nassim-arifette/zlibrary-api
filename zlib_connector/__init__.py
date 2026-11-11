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
from .my_library import BookTagUpdate, LibraryTags
from .transfer import CancellationToken, TransferCancelled, TransferProgress
from .sessions import SessionConflictError, SessionError, SessionLockTimeoutError, SessionStore
from .jobs import DownloadManager, DownloadTask, TaskEvent, TaskSubscription, TaskPage
from .batch import (BatchEntry, BatchInputError, BatchItem, BatchPlan, BatchReport, BatchReportError, StopPlanning,
                    load_report, parse_batch, pick, plan_batch, run_batch)
from .errors import (
    AuthenticationError, BooklistNotFoundError, ChallengeRequired, ConnectorError, DownloadError,
    FormatChangedError, MutationUncertainError, NetworkError, RateLimitError,
)

__all__ = [
    "Client", "Book", "BookDetails", "BookFormat", "BookReference", "BookPage",
    "SearchFilters", "SearchPage", "AccountProfile", "DownloadQuota",
    "Booklist", "BooklistBook", "BooklistBookUpdate", "BooklistDeletion", "BooklistSettings",
    "FavoriteUpdate", "LibraryOverview", "LibraryTags", "BookTagUpdate",
    "CancellationToken", "TransferCancelled", "TransferProgress",
    "SessionStore", "SessionError", "SessionConflictError", "SessionLockTimeoutError",
    "DownloadManager", "DownloadTask", "TaskEvent", "TaskSubscription", "TaskPage",
    "BatchItem", "BatchEntry", "BatchPlan", "BatchReport", "BatchInputError", "BatchReportError", "StopPlanning",
    "parse_batch", "plan_batch", "run_batch", "load_report", "pick",
    "DomainDiagnostic", "DomainReport", "diagnose_domains",
    "ConnectorError", "AuthenticationError", "ChallengeRequired", "DownloadError",
    "FormatChangedError", "MutationUncertainError", "NetworkError", "RateLimitError", "BooklistNotFoundError",
]
