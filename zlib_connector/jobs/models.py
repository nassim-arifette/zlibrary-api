"""Immutable task records and safe projections shared by workers and HTTP callers."""
from dataclasses import dataclass, field
import math
import re

from ..errors import ConnectorError
from ..transfer import TransferProgress


TASK_STATES = ("queued", "running", "waiting_quota", "succeeded", "failed", "cancelled", "interrupted")
TERMINAL_STATES = ("succeeded", "failed", "cancelled", "interrupted")
TASK_ERROR_CODES = ("AuthenticationError", "RateLimitError", "ChallengeRequired", "NetworkError",
                    "DownloadError", "FormatChangedError", "JobFileError", "Interrupted", "Cancelled",
                    "UnknownError", "AccountChanged", "JobError")
ERROR_CODES = TASK_ERROR_CODES
_ERROR_MESSAGES = {
    "AuthenticationError": "The account session is no longer valid.",
    "RateLimitError": "The service reported a quota or request limit. Pending downloads are paused; failed tasks are not retried.",
    "ChallengeRequired": "The service requires browser verification.",
    "NetworkError": "The transfer could not be completed; its quota outcome is unknown.",
    "DownloadError": "The book could not be downloaded or validated.",
    "FormatChangedError": "The service returned an unexpected response.",
    "JobFileError": "The downloaded file is unavailable or could not be saved safely.",
    "Interrupted": "The task was interrupted; its quota outcome is unknown. It will not be retried automatically.",
    "Cancelled": "The task was cancelled. A request already sent may have used quota.",
    "UnknownError": "The task could not be completed.",
    "AccountChanged": "The signed-in account or domain changed. Further downloads have stopped.",
    "JobError": "Task storage is unavailable. Further downloads have stopped.",
}


def task_error_message(code: str | None) -> str | None:
    """Return the fixed public message for a task error code."""
    return _ERROR_MESSAGES.get(code)


class JobError(ConnectorError):
    pass


class JobConflictError(JobError):
    pass


class JobNotFoundError(JobError):
    pass


class JobCapacityError(JobError):
    pass


class JobStateError(JobError):
    pass


class JobFileError(JobError):
    pass


class JobBusyError(JobError):
    pass


def validate_task_id(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise ValueError("A task ID must contain 32 lowercase hexadecimal characters.")
    return value


def validate_reference(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,20}:[A-Za-z0-9]{1,128}", value) is None:
        raise ValueError("A task requires a normalized ID:HASH book reference.")
    return value


def validate_idempotency_key(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9._~-]{8,128}", value) is None:
        raise ValueError("An idempotency key must contain 8–128 ASCII letters, digits or ._~- characters.")
    return value


def validate_task_page(limit: int, offset: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or not 0 <= offset <= 2**63 - 1:
        raise ValueError("Choose a task limit between 1 and 1000 and a non-negative integer offset.")


def _integer(value, maximum=2**63 - 1):
    return type(value) is int and 0 <= value <= maximum


def _timestamp(value):
    return type(value) in (int, float) and 0 <= value <= 2**63 - 1 and math.isfinite(value)


@dataclass(frozen=True, repr=False)
class FileReceipt:
    name: str
    size: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int
    dir_device: int
    dir_inode: int

    def __post_init__(self):
        if (not isinstance(self.name, str) or not 1 <= len(self.name) <= 255 or self.name in (".", "..")
                or any(ord(character) < 32 or ord(character) == 127 or 0xD800 <= ord(character) <= 0xDFFF
                       or character in '<>:"/\\|?*' for character in self.name)
                or not all(_integer(value) for value in (self.size, self.mtime_ns, self.ctime_ns))
                or not all(_integer(value, 2**128 - 1) for value in (self.device, self.inode, self.dir_device, self.dir_inode))):
            raise ValueError("The private task file receipt is invalid.")


@dataclass(frozen=True)
class DownloadTask:
    id: str
    reference: str
    state: str
    created_at: int | float
    updated_at: int | float
    progress: TransferProgress | None = None
    cancel_requested: bool = False
    error_code: str | None = None
    receipt: FileReceipt | None = field(default=None, repr=False)

    def __post_init__(self):
        validate_task_id(self.id)
        validate_reference(self.reference)
        if (self.state not in TASK_STATES or not _timestamp(self.created_at) or not _timestamp(self.updated_at)
                or self.updated_at < self.created_at or type(self.cancel_requested) is not bool
                or (self.error_code is not None and self.error_code not in TASK_ERROR_CODES)
                or (self.progress is not None and (not isinstance(self.progress, TransferProgress)
                    or not _integer(self.progress.bytes_received)
                    or (self.progress.total_bytes is not None and not _integer(self.progress.total_bytes))))
                or (self.receipt is not None and not isinstance(self.receipt, FileReceipt))
                or (self.state == "succeeded") != (self.receipt is not None)
                or (self.state == "succeeded" and self.error_code is not None)):
            raise ValueError("The download task state is invalid.")

    def to_dict(self) -> dict:
        outcome = "not_started" if self.state in ("queued", "waiting_quota") else "completed" if self.state == "succeeded" else "unknown"
        return {"id": self.id, "reference": self.reference, "state": self.state,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "progress": self.progress.to_dict() if self.progress is not None else None,
                "cancel_requested": self.cancel_requested, "error_code": self.error_code,
                "file_available": self.state == "succeeded", "error_message": task_error_message(self.error_code),
                "outcome": outcome}


@dataclass(frozen=True)
class TaskPage:
    tasks: tuple[DownloadTask, ...]
    total: int
    limit: int
    offset: int
    paused: bool = False

    def __post_init__(self):
        validate_task_page(self.limit, self.offset)
        if (not isinstance(self.tasks, tuple) or any(not isinstance(task, DownloadTask) for task in self.tasks)
                or not _integer(self.total) or len(self.tasks) > self.limit or len(self.tasks) > self.total
                or type(self.paused) is not bool):
            raise ValueError("The task page is invalid.")

    def to_dict(self) -> dict:
        return {"tasks": [task.to_dict() for task in self.tasks], "total": self.total,
                "limit": self.limit, "offset": self.offset, "paused": self.paused}
