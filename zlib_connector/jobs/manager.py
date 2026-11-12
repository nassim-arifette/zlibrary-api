"""One durable download queue, with responsive state reads and cancellation."""

from contextlib import contextmanager
from dataclasses import replace
import threading
import time
import uuid

from ..books import BookReference
from ..errors import (AuthenticationError, ChallengeRequired, DownloadError, FormatChangedError,
                      NetworkError, RateLimitError)
from ..transfer import CancellationToken, TransferCancelled, TransferProgress
from .events import TaskEvent, TaskSubscription, validate_event_id
from .files import TaskFiles
from .models import (JobBusyError, JobCapacityError, JobConflictError, JobError, JobFileError,
                     JobStateError, TaskPage, TERMINAL_STATES, validate_task_id, validate_task_page)
from .store import TaskStore, load_scope_key


_TERMINAL = frozenset(TERMINAL_STATES)


class _AccountChanged(Exception):
    pass


class _StorageFailure(JobError):
    pass


def _stamp(task):
    return max(task.updated_at, time.time())


def _error_code(error):
    for kind in (AuthenticationError, ChallengeRequired, RateLimitError, NetworkError,
                 DownloadError, FormatChangedError, JobFileError):
        if isinstance(error, kind):
            return kind.__name__
    return "UnknownError"


class DownloadManager:
    """Own one worker and journal lease; do not share its Client without its lock."""

    def __init__(self, client, directory, *, client_lock=None, max_tasks=1000):
        self._files = TaskFiles(directory)
        self._client = client
        self._client_lock = client_lock if client_lock is not None else threading.Lock()
        self._condition = threading.Condition(threading.RLock())
        self._close_lock = threading.Lock()
        self._closing = self._closed = self._blocked = False
        self._active = None
        self._progress = {}
        self._subscriptions = set()
        self._event_epoch = uuid.uuid4().hex
        self._revision = self._queue_revision = 0
        self._scope_key = load_scope_key(self._files.directory)
        with self._client_lock:
            self._scope = client.account_fingerprint(self._scope_key)
            self._domain = client.domain
        self._store = TaskStore(self._files.directory, self._scope, max_tasks=max_tasks)
        try:
            tasks = self._store.all()
            self._task_revisions = {task.id: 0 for task in tasks}
            self._quota_paused = self._store.paused or any(task.state == "waiting_quota" for task in tasks)
            self._thread = threading.Thread(target=self._work, name="zlib-download-worker")
            self._thread.start()
        except BaseException:
            self._store.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _available(self, *, writing=False):
        if self._closing or self._closed:
            raise JobBusyError("The download manager is closing.")
        if writing and self._blocked:
            raise JobBusyError("Download dispatch stopped after a state or account failure.")

    def _view(self, task):
        progress = self._progress.get(task.id)
        return replace(task, progress=progress) if progress is not None else task

    def _changed(self, task_id=None):
        self._revision += 1
        if task_id is None:
            self._queue_revision = self._revision
        else:
            self._task_revisions[task_id] = self._revision
        self._condition.notify_all()

    def _event(self, task_id):
        task = self._view(self._store.get(task_id))
        revision = max(self._task_revisions[task_id], self._queue_revision)
        return TaskEvent(f"{self._event_epoch}:{revision}", task, self._quota_paused)

    def watch(self, task_id, *, after=None) -> TaskSubscription:
        validate_task_id(task_id)
        if after is not None:
            validate_event_id(after)
        with self._condition:
            self._available()
            self._store.get(task_id)
            if len(self._subscriptions) >= 32:
                raise JobBusyError("The download manager has too many active task subscriptions.")
            subscription = TaskSubscription(self, task_id, after)
            self._subscriptions.add(subscription)
            return subscription

    def _update(self, task):
        try:
            previous = self._view(self._store.get(task.id))
            updated = self._store.update(task)
        except Exception:
            self._blocked = True
            self._condition.notify_all()
            raise _StorageFailure("Task state could not be recorded. Further downloads have stopped.") from None
        if previous != self._view(updated):
            self._changed(task.id)
        return updated

    def _set_paused(self, value):
        try:
            self._store.set_paused(value)
        except Exception:
            self._blocked = True
            self._condition.notify_all()
            raise _StorageFailure("Queue state could not be recorded. Further downloads have stopped.") from None
        if self._quota_paused != value:
            self._quota_paused = value
            self._changed()

    def submit(self, reference, *, idempotency_key):
        reference = str(BookReference.parse(reference, self._domain))
        with self._condition:
            self._available(writing=True)
            try:
                task = self._store.create(reference, idempotency_key)
            except (JobConflictError, JobCapacityError, ValueError):
                raise
            except Exception:
                self._blocked = True
                self._condition.notify_all()
                raise
            if task.id not in self._task_revisions:
                self._changed(task.id)
            self._condition.notify_all()
            return self._view(task)

    def find(self, idempotency_key):
        """Look up the task of an idempotency key; never creates a task or contacts the service."""
        with self._condition:
            self._available()
            task = self._store.find(idempotency_key)
            return None if task is None else self._view(task)

    def get(self, task_id):
        with self._condition:
            self._available()
            return self._view(self._store.get(task_id))

    def list(self, limit=50, offset=0):
        validate_task_page(limit, offset)
        with self._condition:
            self._available()
            tasks = self._store.all()
            return TaskPage(tuple(self._view(task) for task in tasks[offset:offset + limit]), len(tasks), limit, offset,
                            paused=self._quota_paused)

    def cancel(self, task_id):
        with self._condition:
            self._available()
            task = self._view(self._store.get(task_id))
            if task.state in _TERMINAL or task.cancel_requested:
                return task
            if task.state in ("queued", "waiting_quota"):
                task = replace(task, state="cancelled", cancel_requested=True,
                               error_code="Cancelled", updated_at=_stamp(task))
            else:
                task = replace(task, cancel_requested=True, updated_at=_stamp(task))
            task = self._update(task)
            # The durable request must precede signalling the active transfer.
            if self._active is not None and self._active[0] == task_id:
                self._active[1].cancel()
            self._condition.notify_all()
            return task

    def resume(self):
        with self._condition:
            self._available(writing=True)
            resumed = 0
            for task in self._store.all():
                if task.state == "waiting_quota":
                    self._update(replace(task, state="queued", error_code=None, updated_at=_stamp(task)))
                    resumed += 1
            self._set_paused(False)
            self._condition.notify_all()
            return {"resumed": resumed}

    @contextmanager
    def open_file(self, task_id):
        with self._condition:
            self._available()
            task = self._store.get(task_id)
            if task.state != "succeeded" or task.receipt is None:
                raise JobStateError("The task has no completed file.")
            receipt = task.receipt
        with self._files.open(task_id, receipt) as file:
            yield file

    def file_path(self, task_id):
        """Return the absolute path of a succeeded task's file after the identity checks of open_file.

        Raises JobStateError when the task has no completed file and JobFileError
        when the file was removed, replaced or changed.
        """
        with self._condition:
            self._available()
            task = self._store.get(task_id)
            if task.state != "succeeded" or task.receipt is None:
                raise JobStateError("The task has no completed file.")
            receipt = task.receipt
        with self._files.open(task_id, receipt):
            return self._files.directory / task_id / receipt.name

    def close(self):
        if threading.current_thread() is getattr(self, "_thread", None):
            raise JobBusyError("A download worker cannot close its own manager.")
        with self._close_lock:
            with self._condition:
                if self._closed:
                    return
                self._closing = True
                for subscription in tuple(self._subscriptions):
                    subscription._close_locked()
                if self._active is not None:
                    self._active[1].cancel()
                self._condition.notify_all()
            self._thread.join()
            try:
                self._store.close()
            finally:
                with self._condition:
                    self._closed = True

    def _work(self):
        while True:
            with self._condition:
                while True:
                    if self._closing:
                        return
                    task = None
                    if not self._blocked and not self._quota_paused:
                        try:
                            task = next((item for item in self._store.all() if item.state == "queued"), None)
                        except Exception:
                            self._blocked = True
                    if task is not None:
                        break
                    self._condition.wait()
                token = CancellationToken()
                try:
                    task = self._update(replace(task, state="running", updated_at=_stamp(task)))
                except Exception:
                    continue
                self._active = task.id, token
            try:
                self._execute(task, token)
            except TransferCancelled:
                self._finish_interruption(task.id)
            except _AccountChanged:
                self._finish_failure(task.id, "AccountChanged", block=True)
            except _StorageFailure:
                pass  # Preserve the last durable state; never retry an unrecorded result.
            except RateLimitError:
                self._finish_failure(task.id, "RateLimitError", pause=True)
            except KeyboardInterrupt:
                self._finish_interruption(task.id)
            except Exception as error:
                self._finish_failure(task.id, _error_code(error))
            finally:
                with self._condition:
                    self._active = None
                    progress = self._progress.pop(task.id, None)
                    if progress is not None:
                        try:
                            if progress != self._store.get(task.id).progress:
                                self._changed(task.id)
                        except Exception:
                            self._blocked = True
                    self._condition.notify_all()

    @contextmanager
    def _access_client(self, token):
        while True:
            token.check()
            if self._client_lock.acquire(timeout=0.1):
                break
        try:
            token.check()
            yield self._client
        finally:
            self._client_lock.release()

    def _execute(self, task, token):
        with self._access_client(token) as client:
            if client.account_fingerprint(self._scope_key) != self._scope:
                raise _AccountChanged()
            token.check()
            quota = client.get_quota()
            if client.account_fingerprint(self._scope_key) != self._scope:
                raise _AccountChanged()
            token.check()
            if quota.remaining == 0:
                with self._condition:
                    token.check()
                    current = self._view(self._store.get(task.id))
                    self._set_paused(True)
                    self._update(replace(current, state="waiting_quota", error_code="RateLimitError", updated_at=_stamp(current)))
                    self._condition.notify_all()
                return
            owned = self._files.prepare(task.id)
            token.check()

            def progress(value):
                if not isinstance(value, TransferProgress):
                    raise ValueError("A transfer supplied invalid progress.")
                with self._condition:
                    if self._progress.get(task.id) != value:
                        self._progress[task.id] = value
                        self._changed(task.id)

            result = client.download(task.reference, owned.path, progress=progress, cancel=token)
            if client.account_fingerprint(self._scope_key) != self._scope:
                raise _AccountChanged()
            # A successfully returned publication wins over a late cancellation.
            receipt = self._files.capture(owned, result)
            with self._condition:
                current = self._view(self._store.get(task.id))
                self._update(replace(current, state="succeeded", receipt=receipt, error_code=None, updated_at=_stamp(current)))
                self._condition.notify_all()
                closing = self._closing
            if not closing:
                try:
                    client.get_quota()
                except RateLimitError:
                    with self._condition:
                        self._set_paused(True)
                        self._condition.notify_all()
                except Exception:
                    pass  # Quota refresh cannot undo a published, recorded file.

    def _finish_interruption(self, task_id):
        with self._condition:
            try:
                task = self._view(self._store.get(task_id))
                if task.state in _TERMINAL:
                    return
                state = "cancelled" if task.cancel_requested else "interrupted"
                self._update(replace(task, state=state, error_code="Cancelled" if task.cancel_requested else "Interrupted",
                                     updated_at=_stamp(task)))
            except Exception:
                self._blocked = True

    def _finish_failure(self, task_id, error_code, *, block=False, pause=False):
        with self._condition:
            self._blocked |= block
            try:
                if pause:
                    self._set_paused(True)
                task = self._view(self._store.get(task_id))
                if task.state in _TERMINAL:
                    return
                state = "cancelled" if task.cancel_requested else "interrupted" if self._closing else "failed"
                code = "Cancelled" if task.cancel_requested else "Interrupted" if self._closing else error_code
                self._update(replace(task, state=state, error_code=code, updated_at=_stamp(task)))
            except Exception:
                self._blocked = True
