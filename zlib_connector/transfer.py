"""Transfer-only progress and cooperative cancellation, without rendering or I/O.

A fraction of 1 describes received bytes. File validation and publication may
still be pending; observers must not treat it as a successful download result.
"""

from dataclasses import dataclass
import math
from threading import Event
from typing import Callable

from .errors import ConnectorError


class TransferCancelled(ConnectorError):
    """A caller explicitly cancelled a transfer."""

    def __init__(self):
        super().__init__("Transfer cancelled.")


class CancellationToken:
    """A thread-safe, one-way cancellation signal checked by the transfer loop."""

    def __init__(self):
        self._cancelled = Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def check(self) -> None:
        if self.is_cancelled:
            raise TransferCancelled()


def _finite_ratio(numerator: int, denominator: float) -> float | None:
    try:
        value = numerator / denominator
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


@dataclass(frozen=True)
class TransferProgress:
    bytes_received: int
    total_bytes: int | None
    elapsed_seconds: float

    def __post_init__(self):
        if type(self.bytes_received) is not int or self.bytes_received < 0:
            raise ValueError("Received bytes must be a non-negative integer.")
        if self.total_bytes is not None and (
            type(self.total_bytes) is not int or self.total_bytes < self.bytes_received
        ):
            raise ValueError("Total bytes must be unknown or an integer at least as large as received bytes.")
        if type(self.elapsed_seconds) not in (int, float):
            raise ValueError("Elapsed seconds must be a finite, non-negative number.")
        try:
            elapsed = float(self.elapsed_seconds)
        except OverflowError:
            raise ValueError("Elapsed seconds must be a finite, non-negative number.") from None
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("Elapsed seconds must be a finite, non-negative number.")
        object.__setattr__(self, "elapsed_seconds", elapsed)

    @property
    def bytes_per_second(self) -> float | None:
        if not self.bytes_received or not self.elapsed_seconds:
            return None
        return _finite_ratio(self.bytes_received, self.elapsed_seconds)

    @property
    def eta_seconds(self) -> float | None:
        speed = self.bytes_per_second
        if self.total_bytes is None or speed is None or speed <= 0:
            return None
        return _finite_ratio(self.total_bytes - self.bytes_received, speed)

    @property
    def fraction(self) -> float | None:
        if not self.total_bytes:
            return None
        return self.bytes_received / self.total_bytes

    def to_dict(self) -> dict:
        return {"bytes_received": self.bytes_received, "total_bytes": self.total_bytes,
                "elapsed_seconds": self.elapsed_seconds, "bytes_per_second": self.bytes_per_second,
                "eta_seconds": self.eta_seconds, "fraction": self.fraction}


ProgressCallback = Callable[[TransferProgress], None]
