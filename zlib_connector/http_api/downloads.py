"""Optional local task routes; no account-network lock while polling or serving files."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import threading
import unicodedata
from urllib.parse import quote

from ..books import BookReference
from ..jobs.models import (JobError, JobConflictError, JobNotFoundError, JobCapacityError,
                           JobStateError, JobFileError, JobBusyError)
from ..transport import BOOK_LIMIT
from .contract import RequestError


FILE_CHUNK_SIZE = 64 * 1024
FILE_RESPONSE_TIMEOUT = 30


def _new_manager(client, directory, *, client_lock, max_tasks):
    from ..jobs.manager import DownloadManager
    return DownloadManager(client, directory, client_lock=client_lock, max_tasks=max_tasks)


def _job_error(error, *, mutation=False):
    if isinstance(error, JobNotFoundError):
        return RequestError(404, "download_not_found", "The download task does not exist.")
    if isinstance(error, JobConflictError):
        return RequestError(409, "download_conflict", "The request conflicts with existing download state.")
    if isinstance(error, JobStateError):
        return RequestError(409, "download_not_ready", "The task has no completed file available yet.")
    if isinstance(error, JobFileError):
        return RequestError(410, "download_file_unavailable", "The task file was removed, changed or cannot be served safely.")
    if isinstance(error, JobCapacityError):
        return RequestError(503, "download_capacity", "The download task registry has reached its capacity.")
    if isinstance(error, JobBusyError):
        return RequestError(503, "downloads_busy", "The download manager is unavailable or stopping.", retryable=True)
    return RequestError(503, "download_storage_error", "The download task state could not be accessed safely.",
                        outcome="unknown" if mutation else "not_applied")


@dataclass(frozen=True)
class FileResponse:
    manager: object = field(repr=False)
    task_id: str

    @contextmanager
    def open(self):
        try:
            with self.manager.open_file(self.task_id) as download:
                yield download
        except JobError as error:
            raise _job_error(error) from None


@dataclass(frozen=True)
class EventResponse:
    manager: object = field(repr=False)
    task_id: str
    duration: float
    after: str | None = None

    @contextmanager
    def open(self):
        try:
            with self.manager.watch(self.task_id, after=self.after) as subscription:
                yield subscription
        except JobError as error:
            raise _job_error(error) from None


def file_headers(download):
    """Build attachment headers from public, bounded metadata only."""
    name = download.name
    if (not isinstance(name, str) or not name.strip() or len(name) > 255
            or any(unicodedata.category(char).startswith("C") for char in name)
            or "/" in name or "\\" in name or name in (".", "..")
            or type(download.size) is not int or not 0 <= download.size <= BOOK_LIMIT
            or not callable(getattr(download.stream, "read", None))):
        raise _job_error(JobFileError())
    content_type = download.content_type
    if content_type not in ("application/pdf", "application/epub+zip", "image/vnd.djvu", "application/octet-stream"):
        content_type = "application/octet-stream"
    return {"Content-Type": content_type, "Content-Length": str(download.size), "Accept-Ranges": "none",
            "Content-Disposition": "attachment; filename=\"download\"; filename*=UTF-8''" + quote(name, safe=""),
            "Access-Control-Expose-Headers": "Content-Disposition, Content-Length"}


class DownloadRoutes:
    """Own the optional manager, while the server's caller owns its Client."""

    def __init__(self, client, client_lock):
        self._client, self._client_lock = client, client_lock
        self._state_lock = threading.Lock()
        self._manager = None
        self._closed = False

    @property
    def enabled(self):
        with self._state_lock:
            return self._manager is not None

    def enable(self, directory, *, max_tasks=1000):
        if type(max_tasks) is not int or not 1 <= max_tasks <= 10000:
            raise ValueError("The download task capacity must be between 1 and 10000.")
        with self._state_lock:
            if self._closed:
                raise ValueError("The HTTP server has already closed.")
            if self._manager is not None:
                raise ValueError("Downloads are already enabled for this server.")
            self._manager = _new_manager(self._client, directory, client_lock=self._client_lock, max_tasks=max_tasks)

    def close(self):
        with self._state_lock:
            self._closed = True
            manager, self._manager = self._manager, None
        if manager is not None:
            manager.close()

    def execute(self, operation, path, query, body):
        with self._state_lock:
            manager = self._manager
            if manager is None:
                if self._closed:
                    raise _job_error(JobBusyError())
                raise RequestError(503, "downloads_unavailable", "Downloads are not enabled on this server.")
        mutation = operation in ("download_submit", "download_cancel", "download_resume")
        try:
            if operation == "download_submit":
                try:
                    reference = str(BookReference.parse(body["reference"], self._client.domain))
                except ValueError:
                    raise RequestError(400, "invalid_argument", "Use ID:HASH or a book URL on the configured domain.") from None
                return manager.submit(reference, idempotency_key=body["idempotency_key"]).to_dict()
            if operation == "download_list":
                return manager.list(**query).to_dict()
            if operation == "download_get":
                return manager.get(path["task_id"]).to_dict()
            if operation == "download_cancel":
                return manager.cancel(path["task_id"]).to_dict()
            if operation == "download_resume":
                return manager.resume()
            if operation == "download_file":
                return FileResponse(manager, path["task_id"])
            if operation == "download_events":
                return EventResponse(manager, path["task_id"], **query)
            raise RuntimeError("Unimplemented download route.")
        except RequestError:
            raise
        except JobError as error:
            raise _job_error(error, mutation=mutation) from None
        except Exception:
            raise RequestError(500, "download_error", "The download task request could not be completed.",
                               outcome="unknown" if mutation else "not_applied") from None
