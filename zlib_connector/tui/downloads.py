"""Download one book at a time through the server's download tasks, after an explicit confirmation."""
from dataclasses import dataclass
from pathlib import Path
import secrets
import threading
from typing import Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Static

from ..jobs.models import TERMINAL_STATES
from .views import format_label, safe_text
from .widgets import ActionButton


POLL_SECONDS = 0.5


@dataclass(frozen=True)
class DownloadTarget:
    reference: str
    title: str
    extension: str = ""
    size: str = ""


@dataclass(frozen=True)
class DownloadUpdate:
    """One line for the header: text, a style and whether the download is over."""
    text: str
    style: str = "$text-accent"
    done: bool = False


class DownloadDialog(ModalScreen[bool]):
    """Confirm one download: the book, its format and size, the folder and the quota it uses."""

    AUTO_FOCUS = "#download-confirm"
    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel", priority=True),
                Binding("right,tab", "app.focus_next", show=False),
                Binding("left,shift+tab", "app.focus_previous", show=False)]
    DEFAULT_CSS = """
    DownloadDialog { align: center middle; background: $background 60%; }
    #download-box { width: 64; max-width: 100%; height: auto; background: $surface; padding: 1 2; }
    #download-heading { height: 1; margin-bottom: 1; color: $text-accent; text-style: bold; }
    #download-text { height: auto; }
    #download-buttons { height: 1; margin-top: 1; }
    #download-buttons ActionButton { margin-right: 2; }
    """

    def __init__(self, target: DownloadTarget, folder: Path):
        super().__init__()
        self.target, self.folder = target, folder

    def compose(self) -> ComposeResult:
        target = self.target
        details = " · ".join(part for part in (format_label(target.extension), safe_text(target.size)) if part)
        lines = [Content.styled(safe_text(target.title) or "Untitled book", "bold")]
        if details:
            lines.append(Content.styled(details, "$foreground-muted"))
        lines += [Content(""), Content.assemble(("Save in  ", "$foreground-muted"), safe_text(str(self.folder))),
                  Content.styled("This uses one download from your daily allowance.", "$foreground-muted")]
        with Vertical(id="download-box"):
            yield Static("Download this book?", id="download-heading", markup=False)
            yield Static(Content("\n").join(lines), id="download-text", markup=False)
            with Horizontal(id="download-buttons"):
                yield ActionButton("Download", id="download-confirm")
                yield ActionButton("Cancel", id="download-cancel")

    def on_action_button_pressed(self, message: ActionButton.Pressed) -> None:
        self.dismiss(message.button.id == "download-confirm")


def _size(value) -> str:
    if type(value) is not int or value < 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def progress_text(task: dict, title: str) -> str:
    state, progress = task.get("state"), task.get("progress") or {}
    if state == "queued":
        return f"↓ Waiting to start · {title}"
    received, total = progress.get("bytes_received"), progress.get("total_bytes")
    if type(received) is int and type(total) is int and total > 0:
        return f"↓ {min(100, received * 100 // total)}% · {_size(received)} of {_size(total)} · {title}"
    if type(received) is int and received > 0:
        return f"↓ {_size(received)} · {title}"
    return f"↓ Starting · {title}"


def failure_text(error: Exception) -> str:
    status, code = getattr(error, "status", None), getattr(error, "code", None)
    if code == "downloads_unavailable":
        return ("✗ Downloads are off on this server. Restart zlib-cli serve without --no-downloads, or stop it"
                " and let zlib-cli tui start its own.")
    if status == 503:
        return "✗ The server cannot take a download now. Try again later."
    if status == 400:
        return "✗ The server refused this book reference."
    if status in (401, 403):
        return "✗ Access denied by the local server."
    if isinstance(error, OSError):
        return "✗ The file could not be saved in the download folder."
    return "✗ The download failed. Check the server, then try again."


class DownloadRunner:
    """One download in a daemon thread: submit, follow, then save the file. Never retried on its own.

    Every submission gets a new request key, so only an explicit new confirmation downloads again.
    ``report`` must be thread-safe.
    """

    def __init__(self, api, folder: Path, report: Callable[[DownloadUpdate], None]):
        self._api, self._folder, self._report = api, Path(folder), report
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._task_id: str | None = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, target: DownloadTarget) -> None:
        if self.active:
            raise RuntimeError("A download is already running.")
        self._stop.clear()
        self._task_id = None
        self._thread = threading.Thread(target=self._run, args=(target,), name="zlib-tui-download", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        """Ask the server to cancel; the thread reports the final state."""
        self._stop.set()
        identifier = self._task_id
        if identifier is not None:
            try:
                self._api.cancel_download(identifier)
            except Exception:
                pass  # The server may already have finished or stopped.

    def close(self, timeout: float = .5) -> None:
        if self.active:
            self.cancel()
            self._thread.join(timeout)

    def _run(self, target: DownloadTarget) -> None:
        title = safe_text(target.title) or "Untitled book"
        try:
            self._report(DownloadUpdate(f"↓ Starting · {title}"))
            task = self._api.submit_download(target.reference, key="tui-" + secrets.token_hex(12))
            self._task_id = task.get("id")
            while task.get("state") not in TERMINAL_STATES:
                if task.get("state") == "waiting_quota":
                    self.cancel()
                    self._report(DownloadUpdate("✗ Your daily download allowance is used up. Try again tomorrow.",
                                                "$text-error", True))
                    return
                self._report(DownloadUpdate(progress_text(task, title)))
                self._stop.wait(POLL_SECONDS)
                task = self._api.download_task(self._task_id)
            if task.get("state") == "cancelled":
                self._report(DownloadUpdate(f"Download cancelled · {title}", "$foreground-muted", True))
                return
            if task.get("state") != "succeeded":
                message = task.get("error_message") if isinstance(task.get("error_message"), str) else ""
                self._report(DownloadUpdate("✗ " + (safe_text(message) or "The download did not complete."),
                                            "$text-error", True))
                return
            self._report(DownloadUpdate(f"↓ Saving · {title}"))
            path = self._api.save_download(self._task_id, self._folder, title=target.title)
            self._report(DownloadUpdate(f"✓ Saved {safe_text(Path(path).name)} in {safe_text(str(self._folder))}",
                                        "$text-success", True))
        except Exception as error:
            self._report(DownloadUpdate(failure_text(error), "$text-error", True))
