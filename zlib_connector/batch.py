"""Plan and download several books, with a resumable JSON report.

plan_batch() resolves each BatchItem to one exact edition without downloading.
run_batch() downloads the planned editions one at a time and mirrors the task
queue: it reads the quota before each download, stops at a confirmed zero, and
never repeats an attempt whose quota outcome is unknown. Clients are synchronous
and not thread-safe: run_batch() must own its Client and must not share it with
a DownloadManager unless it holds that manager's client lock.
"""
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import secrets
import time
import unicodedata

from .account import DownloadQuota
from .batch_input import (BOOK_KEYS, MAX_ITEMS, PLAN_KIND, REPORT_KIND, STATUSES, BatchInputError, BatchItem,
                          book_file_name, decode_batch, load_json, parse_batch, parse_document, read_bytes,
                          record_path, too_large, uncertain)
from .client import Book, SUPPORTED_FORMATS
from .errors import (AuthenticationError, ChallengeRequired, ConnectorError, DownloadError, FormatChangedError,
                     NetworkError, RateLimitError)
from .search import SearchFilters
from .session_lock import _identity, _lock, _regular, _unlock
from .terminal_text import terminal_text
from .transfer import CancellationToken, TransferCancelled


__all__ = ["BatchEntry", "BatchInputError", "BatchItem", "BatchPlan", "BatchReport", "BatchReportError",
           "StopPlanning", "check_folder", "load_report", "parse_batch", "pick", "plan_batch", "report_lock",
           "run_batch", "write_report"]

MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_REPORT_RECORDS = 10_000
_BOOK_NAME_CHARS = 108  # A 90-character title stem, "-", 12 hex digits and ".djvu".
_RETRY_NOTE = (" It may have used quota, so it is not retried automatically: check the folder and your download"
               " history, then use --retry-uncertain to try again.")
_RETRIED_NOTE = (" It may have used quota, and --retry-uncertain would request it again: check the folder and your"
                 " download history first.")
MESSAGES = {
    "InvalidItem": "This entry is not a usable search or reference.",
    "NotFound": "No result matched this search.",
    "UnsupportedFormat": "This edition is not available as PDF, EPUB or DjVu.",
    "AuthenticationError": "The account session is no longer valid.",
    "ChallengeRequired": "The service requires browser verification.",
    "RateLimitError": "The service reported a quota or request limit.",
    "QuotaExhausted": "No downloads remain in the account quota.",
    "QuotaCheckFailed": "The quota could not be checked, so nothing more was requested.",
    "Unavailable": "The service did not offer this book for download.",
    "NetworkError": "A request could not be completed.",
    "Timeout": "The request timed out; a larger --timeout helps with large books.",
    "FormatChangedError": "The service returned an unexpected response.",
    "DownloadError": "The book could not be downloaded or validated.",
    "FormatMismatch": "The service offered this edition in another format than the one listed.",
    "TooLarge": ("The file is larger than 256 MiB, the most this client downloads, and this edition is never"
                 " requested again. To get another one, replace this item with that edition's ID:HASH or reword"
                 " its search."),
    "ProtocolError": "The service sent a download response that this client does not accept.",
    "LocalStorageError": "The book could not be saved. Check free space, permissions and hard-link support.",
    "InvalidBook": "This edition cannot be downloaded from the selected domain.",
    "Cancelled": "Cancelled.",
    "Interrupted": "An earlier attempt stopped during this download.",
    "ReportError": "The report could not be updated, so the batch stopped.",
    "MaxDownloads": "The --max-downloads limit was reached.",
    "Stopped": "Choosing stopped before the remaining items.",
    "UnknownError": "The item could not be completed.",
}
MISSING_FILE = "The file recorded earlier is not in the output folder; use --redownload-missing to download it again."
REFUSED_FILE = ("The recorded file is not a plain file name, so it was not looked up; use --redownload-missing to"
                " download it again.")
MOVED_FILE = ("The file recorded earlier is not in this output folder; the report recorded its books in another"
              " folder.")
_STOPPING = (AuthenticationError, ChallengeRequired, RateLimitError, NetworkError)
_LOCK_ADVICE = "Choose a --report file in a local folder, or add --no-report."


class BatchReportError(ConnectorError):
    """The batch report could not be locked or written; nothing was downloaded."""


class StopPlanning(Exception):
    """Raise from choose() to stop choosing; unresolved items become not_started."""


class _Pick:
    def __init__(self, number: int):
        if type(number) is not int or not 1 <= number <= 50:
            raise ValueError("Choose a result number between 1 and 50.")
        self.number = number

    def __call__(self, item, books):
        return books[self.number - 1] if self.number <= len(books) else None


def pick(number: int):
    """Choose the given 1-based result of every search; fewer results mean not_found."""
    return _Pick(number)


def merge_filters(base: SearchFilters, override: SearchFilters) -> SearchFilters:
    """Per-item values replace batch filters field by field; missing ones are inherited."""
    return SearchFilters(languages=override.languages or base.languages, formats=override.formats or base.formats,
                         year_from=base.year_from if override.year_from is None else override.year_from,
                         year_to=base.year_to if override.year_to is None else override.year_to,
                         order=override.order or base.order)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shown_path(path) -> str:
    """A path as one terminal-safe line: escape sequences and control characters removed."""
    return terminal_text(str(path), multiline=True).replace("\n", " ")


def _code(error: BaseException) -> str:
    for kind in (AuthenticationError, ChallengeRequired, RateLimitError, NetworkError, FormatChangedError,
                 DownloadError):
        if isinstance(error, kind):
            return kind.__name__
    return "UnknownError"


@dataclass(eq=False)
class BatchEntry:
    """Progress of one item. ``book`` is the chosen edition; ``verified`` means the
    API supplied it in this run. ``quota_may_be_used`` flags an uncertain attempt;
    ``retried`` marks one that ``retry_uncertain`` planned again in this run."""

    index: int
    item: BatchItem
    status: str = "not_started"
    filters: SearchFilters | None = None
    book: Book | None = None
    verified: bool = False
    path: Path | None = None
    duplicate_of: int | None = None
    error_code: str | None = None
    message: str | None = None
    quota_may_be_used: bool = False
    candidates: tuple[Book, ...] = ()
    retried: bool = False

    @property
    def reference(self) -> str | None:
        return None if self.book is None else f"{self.book.book_id}:{self.book.book_hash}"

    @property
    def key(self) -> tuple | None:
        """Identity used for duplicates and resume: the reference, or query plus filters."""
        item = self.item
        if item.error is not None:
            return None
        if item.reference is not None:
            return ("reference", item.reference)
        filters = self.filters or item.filters
        query = unicodedata.normalize("NFC", " ".join(item.query.split())).casefold()
        return ("query", query, tuple(sorted(filters.formats)), tuple(sorted(filters.languages)),
                filters.year_from, filters.year_to, filters.order)

    def fail(self, code: str, *, status: str = "error", used: bool = False, message: str | None = None,
             note: str = _RETRY_NOTE) -> None:
        self.status, self.error_code, self.quota_may_be_used = status, code, used
        self.message = (message or MESSAGES[code]) + (note if used else "")

    def to_dict(self) -> dict:
        """The report item record; ``path`` is the bare file name."""
        return _entry_record(self, None)


def _book_record(book: Book) -> dict:
    return {"reference": f"{book.book_id}:{book.book_hash}",
            **{name: str(getattr(book, name))[:1000] for name in BOOK_KEYS}}


def _path_record(path: Path | None, folder: Path | None) -> str | None:
    """Only a plain file name is written; the document's ``folder`` holds the location."""
    if path is None:
        return None
    if book_file_name(str(path)):
        return str(path)
    if path.is_absolute() and book_file_name(path.name) and (folder is None or path.parent == folder):
        return path.name
    return None


def _entry_record(entry: BatchEntry, folder: Path | None) -> dict:
    item = entry.item
    if item.error is not None:
        source = None
    elif item.reference is not None:
        source = {"reference": item.reference}
    else:
        filters = entry.filters or item.filters
        source = {"query": item.query, "filters": {
            "formats": list(filters.formats), "languages": list(filters.languages),
            "year_from": filters.year_from, "year_to": filters.year_to, "order": filters.order}}
    return {"index": entry.index, "position": item.position, "source": source, "status": entry.status,
            "reference": entry.reference, "book": None if entry.book is None else _book_record(entry.book),
            "path": _path_record(entry.path, folder), "duplicate_of": entry.duplicate_of,
            "error_code": entry.error_code, "message": entry.message,
            "quota_may_be_used": entry.quota_may_be_used,
            "candidates": [_book_record(book) for book in entry.candidates]}


@dataclass(eq=False)
class BatchPlan:
    """Resolved items in input order. ``previous`` keeps report entries of earlier
    runs that match no current item; their downloads still count as known.
    ``retry_uncertain`` records that the plan released uncertain attempts."""

    entries: list[BatchEntry]
    quota: DownloadQuota | None = None
    stopped: tuple[str, str] | None = None
    cancelled: bool = False
    previous: list[BatchEntry] = field(default_factory=list)
    created_at: str | None = None
    folder: Path | None = None
    report_path: Path | None = None
    retry_uncertain: bool = False
    _lookup: Path | None = field(default=None, repr=False)  # Where recorded files were looked up, if any.
    # The folder run_batch() records until a book is published in its own folder (the CLI keeps the one that
    # holds the report's books); None records run_batch()'s folder from the start.
    _report_folder: Path | None = field(default=None, repr=False)
    kind = PLAN_KIND

    @property
    def summary(self) -> dict[str, int]:
        counts = {}
        for entry in self.entries:
            counts[entry.status] = counts.get(entry.status, 0) + 1
        return {status: counts[status] for status in STATUSES if status in counts}

    def to_dict(self) -> dict:
        """JSON-ready document; it never contains tokens, cookies, links or credentials.

        File paths are bare names of files in ``folder``.
        """
        now = _now()
        document = {
            "kind": self.kind, "version": 1,
            "created_at": (self.created_at or now) if self.kind == REPORT_KIND else now, "updated_at": now,
            "folder": None if self.folder is None else str(self.folder),
            "quota": None if self.quota is None else self.quota.to_dict(),
            "stopped": None if self.stopped is None else {"error_code": self.stopped[0], "message": self.stopped[1]},
            "summary": self.summary,
            "items": [_entry_record(entry, self.folder) for entry in self.entries],
        }
        if self.kind == REPORT_KIND:
            room = max(0, MAX_REPORT_RECORDS - len(self.entries))
            document["previous"] = [_entry_record(entry, self.folder) for entry in self.previous[:room]]
        return document


@dataclass(eq=False)
class BatchReport(BatchPlan):
    """Download outcome; ``saved`` is False when the last report write failed, and
    ``recorded`` holds the indices of the books the report file lists as downloaded."""

    saved: bool = True
    recorded: frozenset = frozenset()
    kind = REPORT_KIND

    @classmethod
    def from_plan(cls, plan: BatchPlan, folder: Path | None = None) -> "BatchReport":
        return cls(plan.entries, quota=plan.quota, stopped=plan.stopped, cancelled=plan.cancelled,
                   previous=plan.previous, created_at=plan.created_at, folder=folder or plan.folder,
                   report_path=plan.report_path, retry_uncertain=plan.retry_uncertain)


def defer(entry: BatchEntry, status: str) -> None:
    """Leave a planned entry unattempted; a retried uncertain one keeps its doubt."""
    if entry.retried:
        entry.fail("Interrupted", status="interrupted", used=True)
    else:
        entry.status = status


class _History:
    """Earlier report entries keyed by identity; a later record of a key wins."""

    def __init__(self, report: BatchReport | None):
        self.records, self.downloads, self.doubts, self.order, self.used = {}, {}, {}, [], set()
        self.too_large, self.folder = {}, None
        if report is None:
            return
        self.folder = report.folder
        for old in (*report.entries, *report.previous):
            key = old.key
            if key is None or key in self.records:
                continue
            self.records[key] = old
            self.order.append(old)
            reference = old.reference
            if reference is not None and old.status in ("downloaded", "already_downloaded"):
                self.downloads.setdefault(reference, old)
            if reference is not None and _uncertain_entry(old):
                self.doubts.setdefault(reference, old)
            if reference is not None and too_large(old):
                self.too_large.setdefault(reference, old)

    def match(self, key):
        old = self.records.get(key)
        if old is not None:
            self.used.add(id(old))
        return old

    def unmatched(self) -> list[BatchEntry]:
        return [old for old in self.order if id(old) not in self.used]


def _uncertain_entry(entry: BatchEntry) -> bool:
    # A file over the size limit fails the same way every time, so it is never released for a retry.
    return not too_large(entry) and (entry.status in ("interrupted", "downloading") or (
        entry.status in ("error", "cancelled") and entry.quota_may_be_used))


def _recorded_file(path: Path | None, folder: Path) -> tuple[Path | None, bool]:
    """The recorded file inside ``folder``; True when its name was refused unopened."""
    if path is None:
        return None, False
    if book_file_name(str(path)):
        return folder / str(path), False
    if path.is_absolute() and path.parent == folder and book_file_name(path.name):
        return path, False  # A path this process produced in the same folder.
    return None, True


def recorded_files(report: "BatchReport | None", folder) -> tuple[int, int]:
    """Count the report's recorded downloads whose files are, and are not, in ``folder``.

    Only plain file names are looked up, inside ``folder``; the report's own
    ``folder`` and any other recorded path are never opened.
    """
    found = missing = 0
    if report is None:
        return found, missing
    output = Path(folder).resolve()
    for entry in (*report.entries, *report.previous):
        if entry.status in ("downloaded", "already_downloaded") and entry.book is not None:
            target, _ = _recorded_file(entry.path, output)
            if target is not None and os.path.isfile(target):
                found += 1
            elif target is not None:
                missing += 1
    return found, missing


class _LookupRejected(Exception):
    pass


class _Planner:
    def __init__(self, client, entries, *, base, candidates, choose, history, retry_uncertain,
                 redownload_missing, pause, folder):
        self.client, self.entries, self.base, self.candidates = client, entries, base, candidates
        self.choose, self.history, self.pause, self.folder = choose, history, pause, folder
        self.retry_uncertain, self.redownload_missing = retry_uncertain, redownload_missing
        self.keys, self.chosen = {}, {}
        self.consecutive = 0
        self.finished = None
        self.looked_up = False
        # Compared, never opened: the report's folder is only a hint for the person running the list.
        self.moved = history.folder is not None and history.folder != folder

    def lookup(self, call):
        # Space requests from the end of the previous one; time spent choosing counts toward the pause.
        if self.pause and self.finished is not None:
            wait = self.pause - (time.monotonic() - self.finished)
            if wait > 0:
                time.sleep(wait)
        try:
            result = call()
        except ValueError:
            raise _LookupRejected from None
        finally:
            self.finished = time.monotonic()
        self.consecutive = 0
        return result

    def resolve(self, entry: BatchEntry, mode: str) -> None:
        """Resolve one entry; mode is normal, no_choice (after q) or offline (after a stop)."""
        item = entry.item
        if item.error is not None:
            entry.fail("InvalidItem", message=item.error)
            return
        key = entry.key
        if key in self.keys:
            self.duplicate(entry, self.keys[key])
            return
        self.keys[key] = entry.index
        record = self.history.match(key)
        if record is not None and self.recorded_outcome(entry, record):
            return
        if self.carried_outcome(entry):
            return
        book = item.choice
        if book is None and (item.skipped or (record is not None and record.status == "skipped")):
            entry.status = "skipped"
            return
        if book is None and record is not None:
            book = record.book
        if book is None:
            if mode == "offline":
                entry.status = "not_started"
                return
            book = self.find(entry, mode)
            if book is None:
                return
        entry.book = book
        self.finish(entry, mode)

    def recorded_outcome(self, entry: BatchEntry, record: BatchEntry) -> bool:
        if record.status in ("downloaded", "already_downloaded") and record.book is not None:
            return self.downloaded(entry, record.book, record.path)
        if too_large(record) and record.book is not None:
            self.keep_too_large(entry, record.book, record.quota_may_be_used)
            return True
        if _uncertain_entry(record) and not self.retry_uncertain:
            self.keep_uncertain(entry, record.book, record.error_code,
                                record.status if record.status in ("error", "cancelled") else "interrupted")
            return True
        return False

    def carried_outcome(self, entry: BatchEntry) -> bool:
        item = entry.item
        if (item.path is not None or item.recorded) and item.choice is not None:
            return self.downloaded(entry, item.choice, item.path)
        if item.too_large and item.choice is not None:
            self.keep_too_large(entry, item.choice, True)
            return True
        if item.uncertain and item.choice is not None and not self.retry_uncertain:
            self.keep_uncertain(entry, item.choice, None, "interrupted")
            return True
        return False

    def claim(self, entry: BatchEntry) -> None:
        # Another line that resolves to this edition is then a duplicate, not a download.
        if entry.reference is not None:
            self.chosen.setdefault(entry.reference, entry.index)

    def downloaded(self, entry: BatchEntry, book: Book, path: Path | None) -> bool:
        target, refused = _recorded_file(path, self.folder)
        if target is not None:
            self.looked_up = True
            if os.path.isfile(target):
                entry.status, entry.book, entry.path = "already_downloaded", book, target
                self.claim(entry)
                return True
        if self.redownload_missing:
            return False
        # Keep the recorded name, not where it was looked for: a later run in the right folder finds it.
        entry.status, entry.book = "already_downloaded", book
        entry.path = None if target is None else Path(target.name)
        entry.message = REFUSED_FILE if refused else MOVED_FILE if self.moved else MISSING_FILE
        self.claim(entry)
        return True

    def keep_uncertain(self, entry: BatchEntry, book: Book | None, code: str | None, status: str) -> None:
        entry.book = book
        entry.fail(code or "Interrupted", status=status, used=True,
                   message=MESSAGES["Interrupted"] if code is None else MESSAGES.get(code, MESSAGES["UnknownError"]))
        self.claim(entry)

    def keep_too_large(self, entry: BatchEntry, book: Book, used: bool) -> None:
        entry.book = book
        entry.fail("TooLarge", used=used, note="")  # Final: any retry would fail the same way.
        self.claim(entry)

    def find(self, entry: BatchEntry, mode: str) -> Book | None:
        item = entry.item
        if item.reference is not None:
            details = self.lookup(lambda: self.client.get_book(item.reference))
            if details.extension not in SUPPORTED_FORMATS:
                entry.fail("UnsupportedFormat")
                return None
            entry.verified = True
            if self.base.formats and details.extension not in self.base.formats:
                entry.message = f"Search filters apply only to searches; this edition is {details.extension.upper()}."
            return details
        if mode == "no_choice":
            entry.status = "not_started"
            return None
        page = self.lookup(lambda: self.client.search_page(item.query, filters=entry.filters, page=1,
                                                           limit=self.candidates))
        books = tuple(page.books)
        if not books:
            entry.fail("NotFound", status="not_found")
            return None
        if self.choose is None:
            entry.status, entry.candidates = "needs_choice", books
            return None
        if isinstance(self.choose, _Pick):
            if self.choose.number > len(books):
                found = f"{len(books)} result{'s' if len(books) != 1 else ''}"
                entry.fail("NotFound", status="not_found",
                           message=f"Only {found} matched; result {self.choose.number} was requested.")
                return None
            book = books[self.choose.number - 1]
        else:
            book = self.choose(item, books)
            if book is None:
                entry.status = "skipped"
                return None
            if not any(book is candidate for candidate in books):
                raise ValueError("choose() must return one of the offered books or None.")
        entry.verified = True
        return book

    def finish(self, entry: BatchEntry, mode: str) -> None:
        reference = entry.reference
        if reference in self.chosen:
            self.duplicate(entry, self.chosen[reference])
            return
        self.chosen[reference] = entry.index
        known = self.history.downloads.get(reference)
        if known is not None and self.downloaded(entry, entry.book, known.path):
            return
        if reference in self.history.too_large:
            self.keep_too_large(entry, entry.book, False)  # This item never requested it.
            return
        doubt = self.history.doubts.get(reference)
        if doubt is not None and not self.retry_uncertain:
            self.keep_uncertain(entry, entry.book, doubt.error_code, "interrupted")
            return
        choice = entry.item.choice
        if doubt is not None or (entry.item.uncertain and choice is not None
                                 and f"{choice.book_id}:{choice.book_hash}" == reference):
            # Only retry_uncertain reaches this point with an uncertain edition.
            if mode == "offline":  # Not attempted in this run, so the doubt stays.
                self.keep_uncertain(entry, entry.book, None if doubt is None else doubt.error_code, "interrupted")
                return
            entry.retried = True
        entry.status = "not_started" if mode == "offline" else "planned"

    def duplicate(self, entry: BatchEntry, first: int) -> None:
        entry.status, entry.duplicate_of = "duplicate", first
        entry.book = self.entries[first - 1].book


def _reset(entry: BatchEntry) -> None:
    entry.status, entry.book, entry.verified, entry.candidates = "not_started", None, False, ()
    entry.error_code = entry.message = None


def _plan(client, items, *, filters, candidates, choose, previous, retry_uncertain, redownload_missing,
          on_item, pause, mode, folder) -> BatchPlan:
    items = tuple(items)
    if len(items) > MAX_ITEMS or any(not isinstance(item, BatchItem) for item in items):
        raise ValueError("Pass at most 1000 BatchItem values.")
    if type(candidates) is not int or not 1 <= candidates <= 50:
        raise ValueError("Choose between 1 and 50 candidates per search.")
    base = SearchFilters() if filters is None else filters
    if not isinstance(base, SearchFilters):
        raise ValueError("Batch filters must be SearchFilters.")
    if (choose is not None and not callable(choose)) or (on_item is not None and not callable(on_item)):
        raise ValueError("choose and on_item must be callable or None.")
    if previous is not None and not isinstance(previous, BatchReport):
        raise ValueError("previous must be a BatchReport from load_report().")
    if type(pause) not in (int, float) or not 0 <= pause <= 60:
        raise ValueError("Choose a pause between 0 and 60 seconds.")
    try:
        output = (Path.cwd() if folder is None else Path(folder)).resolve()
    except (OSError, RuntimeError, TypeError):
        raise ValueError("Choose an output folder path.") from None
    entries = []
    for index, item in enumerate(items, 1):
        entry = BatchEntry(index, item)
        if item.query is not None and item.error is None:
            try:
                entry.filters = merge_filters(base, item.filters)
            except ValueError:
                entry.item = BatchItem(item.position, error=f"{item.position.capitalize()} has conflicting search filters.")
        entries.append(entry)
    history = _History(previous)
    plan = BatchPlan(entries, created_at=None if previous is None else previous.created_at,
                     folder=None if folder is None else output,
                     report_path=None if previous is None else previous.report_path,
                     retry_uncertain=bool(retry_uncertain))
    planner = _Planner(client, entries, base=base, candidates=candidates, choose=choose, history=history,
                       retry_uncertain=retry_uncertain, redownload_missing=redownload_missing, pause=pause,
                       folder=output)
    for entry in entries:
        resolved = False
        try:
            try:
                planner.resolve(entry, mode)
            except _LookupRejected:
                entry.fail("InvalidItem")
            except (FormatChangedError, DownloadError) as error:
                entry.fail(_code(error))
                planner.consecutive += 1
                if planner.consecutive >= 2:
                    plan.stopped, mode = (_code(error), MESSAGES[_code(error)]), "offline"
            resolved = True
            if on_item is not None:
                on_item(entry)
        except StopPlanning:
            if not resolved:
                _reset(entry)
            plan.stopped, mode = ("Stopped", MESSAGES["Stopped"]), "no_choice"
        except (TransferCancelled, KeyboardInterrupt):
            if not resolved:
                _reset(entry)
            plan.cancelled, plan.stopped, mode = True, ("Cancelled", MESSAGES["Cancelled"]), "offline"
        except _STOPPING as error:
            if not resolved:
                _reset(entry)
            plan.stopped, mode = (_code(error), MESSAGES[_code(error)]), "offline"
        except ConnectorError:
            if not resolved:
                _reset(entry)
            plan.stopped, mode = ("UnknownError", MESSAGES["UnknownError"]), "offline"
    plan.previous = history.unmatched()
    plan._lookup = output if planner.looked_up else None
    return plan


def plan_batch(client, items, *, filters: SearchFilters | None = None, candidates: int = 5, choose=None,
               previous: "BatchReport | None" = None, retry_uncertain: bool = False,
               redownload_missing: bool = False, on_item=None, pause: float = 0.0, folder=None) -> BatchPlan:
    """Resolve items to exact editions without downloading anything.

    Each search reads page 1 with ``candidates`` results (1-50) using the batch
    ``filters`` merged with the item's own. ``choose(item, books)`` returns one of
    ``books`` or None to skip; raising StopPlanning stops choosing while items that
    need no choice are still resolved. Without ``choose``, such items become
    ``needs_choice`` with their candidates; ``pick(n)`` takes the n-th result.
    References are confirmed with get_book(). Identical requests and editions are
    ``duplicate``. ``previous`` (from load_report) reuses recorded downloads, skips
    and choices without requests and keeps uncertain attempts unless
    ``retry_uncertain``. Recorded files are looked up by plain file name in
    ``folder``, the output folder (default: the current folder); other recorded
    paths are never opened, and run_batch() refuses a plan that looked up
    recorded files in another folder than its own. An edition recorded as too
    large is never requested again. Authentication, challenge, rate-limit and network
    errors stop resolution (two format errors in a row too); Ctrl+C or
    TransferCancelled return the partial plan with ``cancelled`` set. ``pause``
    is the minimum time in seconds between the end of one lookup and the next.
    """
    return _plan(client, items, filters=filters, candidates=candidates, choose=choose, previous=previous,
                 retry_uncertain=retry_uncertain, redownload_missing=redownload_missing, on_item=on_item,
                 pause=pause, mode="normal", folder=folder)


def searches_needed(items, *, filters: SearchFilters | None = None, previous: "BatchReport | None" = None,
                    retry_uncertain: bool = False, redownload_missing: bool = False,
                    folder=None) -> list[BatchItem]:
    """Items that would need a search result choice, found without any request."""
    plan = _plan(None, items, filters=filters, candidates=1, choose=None, previous=previous,
                 retry_uncertain=retry_uncertain, redownload_missing=redownload_missing, on_item=None,
                 pause=0, mode="offline", folder=folder)
    return [entry.item for entry in plan.entries
            if entry.status == "not_started" and entry.book is None and entry.item.query is not None]


def _may_have_used_quota(error: BaseException, stage: str | None) -> bool:
    if stage is None:
        return False
    if stage == "link" and (isinstance(error, (RateLimitError, AuthenticationError)) or (
            isinstance(error, DownloadError) and getattr(error, "reason", None) in ("refused", "unavailable", "no_link"))):
        return False  # An explicit refusal of the link request.
    return True


def _plain_title(book: Book) -> Book:
    # The title names the file; a control character in it would make the recorded name unusable.
    title = "".join("_" if unicodedata.category(character) in ("Cc", "Cs") else character for character in book.title)
    return book if title == book.title else replace(book, title=title)


class _Runner:
    def __init__(self, client, report, folder, *, token, stop, path, progress_factory, on_item, max_downloads):
        self.client, self.report, self.token, self.stop, self.path = client, report, token, stop, path
        self.progress_factory, self.on_item, self.max_downloads = progress_factory, on_item, max_downloads
        self.folder = folder  # Books go here; the report may name another folder until one does.
        self.note = _RETRIED_NOTE if report.retry_uncertain else _RETRY_NOTE
        self.rest = None
        self.attempted = self.consecutive = 0
        self.quota_stale = False  # A download may have used quota since report.quota was read.

    def stopped(self, code: str, rest: str) -> None:
        if self.rest is None:
            self.rest = rest
            self.report.stopped = (code, MESSAGES[code])

    def stop_requested(self) -> None:
        if self.stop is not None and self.stop.is_cancelled:
            raise TransferCancelled()

    def save(self) -> bool:
        if self.path is None:
            return True
        try:
            write_report(self.report, self.path)
        except (OSError, ValueError):
            self.report.saved = False
            return False
        self.report.saved = True
        self.report.recorded = frozenset(entry.index for entry in self.report.entries if entry.status == "downloaded")
        return True

    def quota_allows(self) -> bool:
        try:
            quota = self.client.get_quota()
        except RateLimitError:
            self.stopped("RateLimitError", "waiting_quota")
            return False
        except (AuthenticationError, ChallengeRequired) as error:
            self.stopped(_code(error), "not_started")
            return False
        except (ConnectorError, ValueError):
            self.stopped("QuotaCheckFailed", "not_started")
            return False
        self.report.quota, self.quota_stale = quota, False
        if quota.remaining == 0:
            self.stopped("QuotaExhausted", "waiting_quota")
            return False
        return True

    def count_failure(self, code: str) -> None:
        self.consecutive += 1
        if self.consecutive >= 2:
            self.stopped(code, "not_started")

    def verify(self, entry: BatchEntry) -> bool | None:
        """Confirm a recorded edition before its download; None stops the batch."""
        try:
            details = self.client.get_book(entry.reference)
        except RateLimitError:
            self.stopped("RateLimitError", "waiting_quota")
            return None
        except (AuthenticationError, ChallengeRequired, NetworkError) as error:
            self.stopped(_code(error), "not_started")
            return None
        except (FormatChangedError, DownloadError, ValueError) as error:
            code = "InvalidBook" if isinstance(error, ValueError) else _code(error)
            entry.fail(code)
            self.count_failure(code)
            return False
        if details.extension not in SUPPORTED_FORMATS:
            entry.fail("UnsupportedFormat")
            return False
        entry.book, entry.verified = details, True
        return True

    def failed(self, entry: BatchEntry, error: BaseException) -> None:
        stage = getattr(error, "download_stage", None)
        used = _may_have_used_quota(error, stage)
        self.quota_stale = used  # A link never requested, or refused, left the quota read before it current.
        reason = getattr(error, "reason", None)
        if isinstance(error, (TransferCancelled, KeyboardInterrupt)):
            entry.fail("Cancelled", status="cancelled", used=used, note=self.note)
            raise error
        if isinstance(error, DownloadError) and reason in ("save_failed", "local_write"):
            code, rest = "LocalStorageError", "not_started"
        elif isinstance(error, DownloadError) and reason == "size_limit":
            # The size cap is fixed and the file is the same next time: final, and no reason to stop the batch.
            entry.fail("TooLarge", used=used, note="")
            return
        elif isinstance(error, DownloadError) and reason == "format_mismatch":
            # A retry checks the edition again, so the format the service lists may then match.
            entry.fail("FormatMismatch", used=used, note=self.note)
            self.count_failure("DownloadError")
            return
        elif isinstance(error, DownloadError) and not used:
            quota = None
            try:
                quota = self.client.get_quota()
            except (ConnectorError, ValueError):
                pass
            if quota is not None and quota.remaining:
                self.report.quota = quota
                entry.fail("Unavailable")
                return
            code, rest = "Unavailable", "waiting_quota"
        elif isinstance(error, DownloadError) and reason == "protocol":
            # Header and encoding anomalies come from the server or a proxy and would repeat for every book.
            code, rest = "ProtocolError", "not_started"
        elif isinstance(error, DownloadError):
            entry.fail("DownloadError", used=True, note=self.note)
            self.count_failure("DownloadError")
            return
        elif isinstance(error, OSError):
            code, rest = "LocalStorageError", "not_started"
        elif isinstance(error, ValueError):
            entry.fail("InvalidBook", used=used, note=self.note)
            return
        elif isinstance(error, ConnectorError):
            code = _code(error)
            rest = "waiting_quota" if code == "RateLimitError" else "not_started"
        else:
            if used:
                entry.fail("Interrupted", status="interrupted", used=True, note=self.note)
            else:
                entry.status = "planned"
            raise error
        if used:
            timeout = isinstance(error, NetworkError) and reason == "timeout"
            entry.fail(code, used=True, message=MESSAGES["Timeout"] if timeout else None, note=self.note)
        else:
            entry.status = "planned"
        self.stopped(code, rest)

    def download(self, entry: BatchEntry) -> None:
        if self.max_downloads is not None and self.attempted >= self.max_downloads:
            self.stopped("MaxDownloads", "not_started")
            return
        if not self.quota_allows():
            return
        if not entry.verified:
            verified = self.verify(entry)
            if verified is None:
                return
        if entry.status == "planned":
            self.stop_requested()  # A first Ctrl+C during the quota read or the edition check costs nothing.
            entry.status = "downloading"
            try:
                saved = self.save()
            except BaseException:
                entry.status = "planned"  # Interrupted before any request: nothing is uncertain.
                raise
            if not saved:
                entry.status = "planned"
                self.stopped("ReportError", "not_started")
                return
            self.attempted += 1
            self.quota_stale = True
            try:
                context = nullcontext() if self.progress_factory is None else self.progress_factory(entry)
                with context as callback:
                    path = self.client.download(_plain_title(entry.book), self.folder, progress=callback,
                                                cancel=self.token)
            except BaseException as error:
                self.failed(entry, error)
            else:
                entry.status, entry.path, entry.error_code, entry.message = "downloaded", Path(path), None, None
                entry.quota_may_be_used = False
                # A book is in this folder now: the report records it there from this save on, so that
                # the book keeps its file name (names are kept only for files in the report's folder).
                self.report.folder = self.folder
                self.consecutive = 0
        if not self.save():
            self.stopped("ReportError", "not_started")
        if self.on_item is not None:
            self.on_item(entry)

    def finish(self) -> None:
        rest = self.rest or "not_started"
        for entry in self.report.entries:
            if entry.status == "planned":
                defer(entry, rest)
            elif entry.status == "downloading":
                entry.fail("Interrupted", status="interrupted", used=True, note=self.note)

    def run(self) -> BatchReport:
        pending = [entry for entry in self.report.entries if entry.status == "planned"]
        if not self.save():
            raise BatchReportError("The report could not be written; nothing was downloaded.")
        try:
            for entry in pending:
                if self.rest is not None:
                    break
                self.token.check()
                self.stop_requested()
                self.download(entry)
        except (TransferCancelled, KeyboardInterrupt):
            self.report.cancelled = True
            self.stopped("Cancelled", "not_started")
        except BaseException:
            self.finish()
            self.forget_stale_quota()
            self.save()
            raise
        self.finish()
        refused = self.report.stopped is not None and self.report.stopped[0] in ("AuthenticationError", "ChallengeRequired")
        if self.attempted and not self.report.cancelled and not refused:
            try:
                self.report.quota = self.client.get_quota()
            except (ConnectorError, ValueError):
                self.report.quota = None
        else:
            self.forget_stale_quota()
        self.save()
        return self.report

    def forget_stale_quota(self) -> None:
        # No request follows a stop, so a figure read before the last download is no longer known.
        if self.quota_stale:
            self.report.quota = None


def run_batch(client, plan: BatchPlan, folder, *, report=None, progress_factory=None,
              cancel: CancellationToken | None = None, on_item=None, max_downloads: int | None = None,
              stop: CancellationToken | None = None) -> BatchReport:
    """Download the planned editions one at a time with the existing streaming flow.

    The quota is read before each download: a confirmed zero stops the batch with
    the rest ``waiting_quota``; missing counters are unknown and do not block, but
    a failed read stops it with nothing requested. It is read again at the end,
    except after a cancellation or a refused session: then the report's ``quota``
    is None if a download was requested since the last read. A recorded edition is confirmed
    with get_book() first. Account, network, protocol and local-storage failures
    stop the batch (the rest ``not_started``); a failure that may have used quota is
    marked ``quota_may_be_used`` and never retried. A file over the size limit is
    ``TooLarge`` and never requested again. ``folder`` must be the folder in which
    plan_batch() looked up recorded files, if it did. ``report`` is a JSON file
    written atomically before the first download, before and after each attempt and
    at the end; an existing one must have been read with load_report() for this
    plan. ``progress_factory(entry)`` returns a context manager yielding a progress
    callback or None. ``on_item(entry)`` runs after each attempted item is
    recorded. A cancelled ``stop`` token lets the current book finish and stops
    before the next one; a cancelled ``cancel`` token, or Ctrl+C, also cancels the
    current transfer. Both return the report with ``cancelled`` set.
    """
    if not isinstance(plan, BatchPlan) or isinstance(plan, BatchReport):
        raise ValueError("run_batch() needs a BatchPlan from plan_batch().")
    if plan.cancelled:
        raise ValueError("A cancelled plan cannot be downloaded.")
    if any(token is not None and not isinstance(token, CancellationToken) for token in (cancel, stop)):
        raise ValueError("Batch cancellation requires a CancellationToken.")
    if (progress_factory is not None and not callable(progress_factory)) or (on_item is not None and not callable(on_item)):
        raise ValueError("progress_factory and on_item must be callable or None.")
    if max_downloads is not None and (type(max_downloads) is not int or max_downloads < 1):
        raise ValueError("max_downloads must be a positive integer or None.")
    folder = check_folder(folder)
    if plan._lookup is not None and plan._lookup != folder:
        # Its recorded files were judged present or missing in another folder than the one written to the report.
        raise ValueError("The plan looked up recorded files in another folder; pass this folder to"
                         " plan_batch(folder=...) too.")
    path = None
    if report is not None:
        path = Path(report).absolute()
        if path.is_dir() or not path.parent.is_dir():
            raise ValueError("The report must be a file in an existing folder.")
        if path.exists() and (plan.report_path is None or not _same_file(path, plan.report_path)):
            raise BatchInputError("This report already exists; continue it with plan_batch(previous=load_report(path)).")
    result = BatchReport.from_plan(plan, plan._report_folder or folder)
    result.created_at = result.created_at or _now()
    runner = _Runner(client, result, folder, token=cancel or CancellationToken(), stop=stop, path=path,
                     progress_factory=progress_factory, on_item=on_item, max_downloads=max_downloads)
    return runner.run()


def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return first.absolute() == Path(second).absolute()


def check_folder(folder) -> Path:
    """Fail before any request unless the folder can publish book files by hard link.

    A probe named like the longest book file (108 characters) is created and
    linked, then removed: FAT32/exFAT drives, and on Windows folder paths too long
    for book names, fail here, not after quota. Elsewhere save_book() shortens
    names that would exceed the folder's name limit in bytes.
    """
    try:
        folder = Path(folder).resolve(strict=True)
    except (OSError, RuntimeError, TypeError):
        raise ValueError("Choose an existing output directory.") from None
    if not folder.is_dir():
        raise ValueError("Choose an existing output directory.")
    name = ".zlib-probe-" + secrets.token_hex(8)
    first, second = folder / (name + "-" * (_BOOK_NAME_CHARS - len(name))), folder / (name + ".link")
    created = linked = False
    try:
        try:
            descriptor = os.open(first, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            os.close(descriptor)
            created = True
        except OSError:
            raise ValueError("Cannot create book files in the output folder. Check its permissions,"
                             " or choose a folder with a shorter path.") from None
        try:
            os.link(first, second)
            linked = True
        except OSError:
            raise ValueError("The output folder must support hard links; FAT32 and exFAT drives are not supported.") from None
    finally:
        for probe, exists in ((second, linked), (first, created)):
            if exists:
                try:
                    probe.unlink()
                except OSError:
                    pass
    return folder


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass  # Some filesystems cannot sync directories; the file itself was synced.
    finally:
        os.close(descriptor)


def _sharing_violation(error: OSError) -> bool:
    return os.name == "nt" and getattr(error, "winerror", None) in (5, 32)


def write_report(report: BatchReport, path) -> None:
    """Replace ``path`` atomically with the report: a synced temporary file, then os.replace.

    Text is UTF-8 JSON with ASCII escapes. Book files are stored by bare name, with
    the output folder at the top level. A Windows sharing violation is retried for
    two seconds.
    """
    path = Path(path).absolute()
    folder = path.parent
    data = (json.dumps(report.to_dict(), ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode("ascii")
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("The report would exceed 32 MiB.")
    temporary = folder / f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        with open(temporary, "xb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        deadline = time.monotonic() + 2.0
        while True:
            try:
                os.replace(temporary, path)
                break
            except OSError as error:
                if not _sharing_violation(error) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)  # Antivirus, indexers or editors may hold the file briefly.
        temporary = None
        _sync_directory(folder)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _loaded_entry(record) -> BatchEntry:
    if record.query is None and record.reference is None:
        item = BatchItem(record.position, error="This entry was not usable in the original list.")
    else:
        item = BatchItem(record.position, query=record.query, reference=record.reference,
                         filters=record.filters or SearchFilters())
    entry = BatchEntry(record.index, item, record.status, filters=record.filters if record.query else None,
                       book=record.book, path=record_path(record), duplicate_of=record.duplicate_of,
                       error_code=record.error_code, message=record.message,
                       quota_may_be_used=record.quota_may_be_used, candidates=record.candidates)
    if record.status == "downloading" or (uncertain(record) and record.status == "interrupted"):
        entry.fail(record.error_code if record.status == "interrupted" and record.error_code in MESSAGES else "Interrupted",
                   status="interrupted", used=True,
                   message=MESSAGES["Interrupted"])
    return entry


def load_report(path) -> BatchReport | None:
    """Read a report written by run_batch(); None when the file does not exist.

    The file is bounded (32 MiB, 10,000 entries) and validated strictly; a plan or
    any other JSON is refused and left unchanged. An entry still ``downloading``
    becomes ``interrupted``: that attempt may have used quota.
    """
    path = Path(path).absolute()
    data = read_bytes(path, limit=MAX_REPORT_BYTES, what="report")
    if data is None:
        return None
    document = load_json(decode_batch(data, limit=MAX_REPORT_BYTES), "report")
    kind = document.get("kind") if isinstance(document, dict) else None
    if kind == PLAN_KIND:
        raise BatchInputError("This file is a batch plan, not a report; read it as a list with -a.")
    if kind != REPORT_KIND:
        raise BatchInputError("This file is not a batch report; it was left unchanged.")
    current, earlier, metadata = parse_document(document, label="report")
    quota = metadata.get("quota")
    stopped = metadata.get("stopped")
    folder = metadata.get("folder")
    return BatchReport([_loaded_entry(record) for record in current],
                       quota=None if quota is None else DownloadQuota(**quota),
                       stopped=None if stopped is None else (stopped["error_code"], stopped["message"]),
                       previous=[_loaded_entry(record) for record in earlier],
                       created_at=metadata.get("created_at"), folder=None if folder is None else Path(folder),
                       report_path=path)


class _LockRefused(Exception):
    pass


def _open_report_lock(path: Path, deadline: float) -> int:
    """Open or create an empty regular lock file without following links; it is never written."""
    flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_BINARY", 0))
    while True:
        try:
            before = os.lstat(path)
        except FileNotFoundError:
            before = None
        if before is not None and (not _regular(before) or before.st_size != 0):
            raise _LockRefused
        try:
            descriptor = os.open(path, flags, 0o600)
            break
        except OSError as error:
            # Antivirus or indexers may hold a new file briefly on Windows.
            if os.name != "nt" or getattr(error, "winerror", None) != 32 or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)
    try:
        opened, current = os.fstat(descriptor), os.lstat(path)
        if (not _regular(opened) or opened.st_size != 0 or not _regular(current)
                or _identity(current) != _identity(opened)):
            raise _LockRefused
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def report_lock(path):
    """Hold an exclusive OS lock on ``NAME.lock`` beside the report for a whole run.

    The lock file stays empty and is never removed. It needs no private permissions,
    because the report itself is not private: copied, synced or shared folders work.
    """
    path = Path(path).absolute()
    lock = path.with_name(path.name + ".lock")
    refused = BatchReportError(f"Cannot lock {shown_path(lock)}. {_LOCK_ADVICE}")
    deadline = time.monotonic() + 0.2
    try:
        descriptor = _open_report_lock(lock, deadline)
    except (_LockRefused, OSError, ValueError):
        raise refused from None
    acquired = False
    try:
        while not acquired:
            try:
                _lock(descriptor)
                acquired = True
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise refused from None
                if time.monotonic() >= deadline:
                    raise BatchReportError("Another batch is using this report; wait for it to finish.") from None
                time.sleep(0.02)
        yield
    finally:
        if acquired:
            try:
                _unlock(descriptor)
            except OSError:
                pass  # Closing the descriptor also releases the OS lock.
        os.close(descriptor)
