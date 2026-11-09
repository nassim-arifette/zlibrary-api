"""Optional transfer progress on stderr; successful output remains scriptable."""
import math
import sys
import time

from .transfer import TransferProgress


def add_progress_options(command):
    options = command.add_mutually_exclusive_group()
    options.add_argument("--progress", dest="progress", action="store_true", default=None,
                         help="show received bytes, speed and estimated time on stderr (automatic in a terminal)")
    options.add_argument("--no-progress", dest="progress", action="store_false", help="hide transfer progress")


def _bytes(value):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024


class ProgressDisplay:
    def __init__(self, enabled=None, *, stream=None):
        self.stream = sys.stderr if stream is None else stream
        try:
            self.terminal = self.stream.isatty()
            self.enabled = self.terminal if enabled is None else enabled
        except (OSError, ValueError):
            self.terminal = self.enabled = False
        self._last = None
        self._latest = None
        self._rendered = None
        self._width = 0

    def __enter__(self):
        return self if self.enabled else None

    def __exit__(self, *_):
        try:
            # Unknown-length transfers have no final-size callback to bypass
            # throttling. Report the last received bytes even on cancellation.
            if self.enabled and self._latest is not None and self._latest != self._rendered:
                self._render(self._latest)
            if self.enabled and self.terminal and self._rendered is not None:
                self.stream.write("\n")
                self.stream.flush()
        except (Exception, KeyboardInterrupt):
            # Closing an observer must neither fail a published download nor
            # replace the exception that interrupted an unfinished transfer.
            self.enabled = False

    def __call__(self, value: TransferProgress):
        if not self.enabled:
            return
        self._latest = value
        now = time.monotonic()
        final_bytes = value.total_bytes is not None and value.bytes_received == value.total_bytes
        interval = 0.2 if self.terminal else 1.0
        if self._last is not None and now - self._last < interval and not final_bytes:
            return
        if self._render(value):
            self._last = now

    def _render(self, value: TransferProgress) -> bool:
        text = f"Received {_bytes(value.bytes_received)}"
        if value.total_bytes is not None:
            text += f" / {_bytes(value.total_bytes)}"
        if value.fraction is not None:
            text += f" ({value.fraction * 100:.0f}%)"
        if value.bytes_per_second is not None:
            text += f" | {_bytes(value.bytes_per_second)}/s"
        if value.eta_seconds is not None:
            text += f" | about {math.ceil(value.eta_seconds)}s left"
        try:
            self.stream.write(("\r" + text.ljust(self._width)) if self.terminal else text + "\n")
            self.stream.flush()
        except (OSError, ValueError):
            self.enabled = False
            return False
        self._rendered = value
        self._width = len(text)
        return True
