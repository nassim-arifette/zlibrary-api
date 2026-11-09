"""Bounded, coalesced observations of the latest download task state."""

from dataclasses import dataclass
import math
import re
import time

from .models import DownloadTask, TERMINAL_STATES


def validate_event_id(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}:[0-9]{1,20}", value) is None:
        raise ValueError("The task event cursor is invalid.")
    return value


@dataclass(frozen=True)
class TaskEvent:
    id: str
    task: DownloadTask
    paused: bool

    def __post_init__(self):
        validate_event_id(self.id)
        if not isinstance(self.task, DownloadTask) or type(self.paused) is not bool:
            raise ValueError("The task observation is invalid.")

    def to_dict(self) -> dict:
        return {"id": self.id, "task": self.task.to_dict(), "paused": self.paused}


class TaskSubscription:
    """Read the latest complete observation; intermediate states may coalesce.

    Obtain subscriptions through DownloadManager.watch(). Each subscription
    retains only its last cursor, rather than a queue of progress events.
    """

    def __init__(self, manager, task_id, after):
        self._manager = manager
        self._task_id = task_id
        self._after = after
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @property
    def closed(self):
        with self._manager._condition:
            return self._closed

    def _close_locked(self):
        self._closed = True
        self._manager._subscriptions.discard(self)
        self._manager._condition.notify_all()

    def close(self):
        with self._manager._condition:
            self._close_locked()

    def next(self, timeout=5) -> TaskEvent | None:
        if type(timeout) not in (int, float) or not 0 <= timeout <= 60 or not math.isfinite(timeout):
            raise ValueError("Choose a task observation timeout between 0 and 60 seconds.")
        deadline = time.monotonic() + timeout
        with self._manager._condition:
            while not self._closed:
                try:
                    event = self._manager._event(self._task_id)
                except Exception:
                    self._close_locked()
                    raise
                terminal = event.task.state in TERMINAL_STATES
                if terminal or event.id != self._after:
                    self._after = event.id
                    if terminal:
                        self._close_locked()
                    return event
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._manager._condition.wait(remaining)
            return None
