"""One active read and one replaceable pending read, with cooperative cancellation."""
from dataclasses import dataclass, field
import threading
from typing import Callable

from ..errors import NetworkError
from ..transfer import CancellationToken, TransferCancelled


@dataclass(frozen=True)
class LoadResult:
    generation: int
    value: object = field(default=None, repr=False)
    error: str | None = None


@dataclass(frozen=True)
class _Request:
    generation: int
    read: Callable[[CancellationToken], object] = field(repr=False)
    cancel: CancellationToken = field(default_factory=CancellationToken, repr=False)


def _failure(error: Exception) -> str:
    status = getattr(error, "status", None)
    if status in (401, 403):
        return "Access denied. Use the running server's token file, then refresh."
    if status == 503:
        return "The server is busy or unavailable. Wait, then refresh."
    if status == 424:
        return "The account session is unavailable. Run zlib-cli login, restart zlib-cli serve, then refresh."
    if status == 429:
        return "The service is limiting requests. Wait before refreshing."
    if status in (404, 410):
        return "This collection or book is unavailable. Go back or refresh."
    if isinstance(error, NetworkError):
        return "Could not reach the server. Start zlib-cli serve and check its address, then refresh."
    return "Could not load this view. Refresh to try again."


class BoundedLoader:
    """Own a single lazy daemon thread; the delivery callback must be thread-safe.

    close() signals both tokens and joins for at most 0.5 seconds by default.
    The real adapter interrupts connected reads through its deadline guard. If
    another adapter ignores cancellation, its sole daemon thread may finish
    later, but cannot start pending work or deliver another result after close.
    """

    def __init__(self, deliver: Callable[[LoadResult], None]):
        self._deliver = deliver
        self._condition = threading.Condition()
        self._active: _Request | None = None
        self._pending: _Request | None = None
        self._closed = False
        self._thread: threading.Thread | None = None

    def submit(self, generation: int, read: Callable[[CancellationToken], object]) -> None:
        with self._condition:
            if self._closed:
                raise RuntimeError("The collection loader is closed.")
            self._cancel_locked()
            self._pending = _Request(generation, read)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="zlib-collections", daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def _cancel_locked(self):
        if self._active is not None:
            self._active.cancel.cancel()
        if self._pending is not None:
            self._pending.cancel.cancel()
        self._pending = None

    def cancel(self) -> None:
        with self._condition:
            self._cancel_locked()
            self._condition.notify_all()

    def close(self, timeout: float = .5) -> None:
        with self._condition:
            self._closed = True
            self._cancel_locked()
            thread = self._thread
            self._condition.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0, min(timeout, 5)))

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._closed:
                    return
                request = self._pending
                self._pending = None
                self._active = request
            try:
                request.cancel.check()
                value = request.read(request.cancel)
                request.cancel.check()
                result = LoadResult(request.generation, value)
            except TransferCancelled:
                result = None
            except Exception as error:
                result = LoadResult(request.generation, error=_failure(error))
            with self._condition:
                self._active = None
                deliver = not self._closed and not request.cancel.is_cancelled and result is not None
            if deliver:
                self._deliver(result)
