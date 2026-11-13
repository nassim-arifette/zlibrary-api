"""Batch downloads for the CLI: read a list, choose editions, confirm, download, report.

Prompts, candidates, the plan summary and progress use stderr, so stdout can be
redirected. Interactive means the input policy allows prompts (cli_terminal) and
the list is not read from stdin. Exit codes: 0 complete, 3 incomplete but only
waiting (run the same command again later), 4 incomplete and needs attention,
130 cancelled; a failure before the batch started uses the CLI error codes
(docs/cli-json.md).
"""
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading

from .batch import (MESSAGES, MISSING_FILE, MOVED_FILE, REFUSED_FILE, BatchReport, StopPlanning, check_folder,
                    defer, load_report, merge_filters, pick, plan_batch, recorded_files, report_lock, run_batch,
                    searches_needed, shown_path, write_report)
from .batch_input import (INPUT_KINDS, MAX_INPUT_BYTES, MAX_ITEMS, PLAN_KIND, REPORT_KIND, BatchInputError,
                          _text_item, parse_input)
from . import cli_terminal
from .cli_output import InputRequired
from .cli_progress import ProgressDisplay
from .cli_session import AccountSession
from .cli_views import show_books
from .cli_settings import named_credentials, with_configured_filters
from .credentials import credentials_path
from .errors import AuthenticationError, ChallengeRequired, ConnectorError
from .search import SearchFilters
from .terminal_text import terminal_text
from .transfer import CancellationToken


BATCH_OPTIONS = ("batch_file", "batch_format", "dry_run", "report", "no_report", "yes", "interactive",
                 "retry_uncertain", "redownload_missing", "max_downloads")
PLANNING_PAUSE = 0.5  # Seconds between searches without a person choosing; politeness, not a retry.
_LABELS = {"planned": "planned", "needs_choice": "needs a choice", "duplicate": "duplicate",
           "already_downloaded": "already downloaded", "not_found": "not found", "skipped": "skipped",
           "error": "error", "downloading": "downloading", "downloaded": "downloaded",
           "waiting_quota": "waiting for quota", "not_started": "not started", "cancelled": "cancelled",
           "interrupted": "interrupted"}
_PLURALS = {"duplicate": "duplicates", "error": "errors", "needs_choice": "need a choice"}
_WAITING_CODES = frozenset({"QuotaExhausted", "RateLimitError", "Unavailable", "QuotaCheckFailed", "NetworkError",
                            "MaxDownloads", "Stopped"})
_ATTENTION = frozenset({"error", "not_found", "needs_choice", "interrupted", "cancelled", "downloading"})
_GUIDANCE = {
    "QuotaExhausted": "Stopped: no downloads remain in the account quota.",
    "RateLimitError": "Stopped: the service reported a quota or request limit.",
    "Unavailable": "Stopped: the service did not offer a download link; the quota may be used up.",
    "QuotaCheckFailed": "Stopped: the quota could not be checked, so nothing more was requested.",
    "MaxDownloads": "Stopped at the --max-downloads limit.",
    "NetworkError": "Stopped: a request failed. Check the connection and the domain.",
    "AuthenticationError": "Stopped: the account session is no longer valid. Run zlib-cli login first.",
    "ChallengeRequired": "Stopped: the service requires browser verification. Try another domain (zlib-cli doctor).",
    "FormatChangedError": "Stopped: the service returned unexpected responses.",
    "DownloadError": "Stopped after repeated download failures.",
    "ProtocolError": "Stopped: the service sent a download response that this client does not accept.",
    "LocalStorageError": "Stopped: a book could not be saved. Check free space, permissions and hard-link support.",
    "ReportError": "Stopped: the report could not be updated.",
    "Stopped": "Choosing stopped before the remaining items.",
    "UnknownError": "Stopped after an unexpected error.",
}
_FIRST_INTERRUPT = (b"\nStopping after the current book; press Ctrl+C again to cancel its download"
                    b" (its quota may already be used).\n")
_SECOND_INTERRUPT = b"\nCancelling the current download; press Ctrl+C again to stop at once.\n"


def add_batch_options(command) -> None:
    command.add_argument("-a", "--batch-file", metavar="FILE",
                         help="read titles, ISBNs or references from a TXT, CSV or JSON list; - reads standard input")
    command.add_argument("--batch-format", choices=INPUT_KINDS,
                         help="list format when the file name does not tell (standard input, other suffixes)")
    command.add_argument("--dry-run", action="store_true", help="choose editions and print the plan without downloading")
    reports = command.add_mutually_exclusive_group()
    reports.add_argument("--report", type=Path, metavar="FILE",
                         help="progress file for finishing a list over several runs (default with -a FILE: "
                              "LIST.progress.json beside the list)")
    reports.add_argument("--no-report", action="store_true", help="do not read or write a progress file")
    command.add_argument("--yes", action="store_true", help="download a list without confirmation (required without a terminal)")
    command.add_argument("--retry-uncertain", action="store_true",
                         help="try again items whose earlier attempt may have used quota")
    command.add_argument("--redownload-missing", action="store_true",
                         help="download again books recorded as downloaded whose files are missing")
    command.add_argument("--max-downloads", type=int, metavar="N", help="download at most N books in this run")


def is_batch(args) -> bool:
    """A single query with no batch option keeps the original single-book flow."""
    return len(args.query) != 1 or any(getattr(args, name) is not None and getattr(args, name) is not False
                                       for name in BATCH_OPTIONS)


def _write(stream, text: str) -> None:
    # Windows pipes may use a legacy code page: escape instead of failing mid-batch.
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        stream.write((text + "\n").encode(encoding, "backslashreplace").decode(encoding, "replace"))
    stream.flush()


def _note(text: str) -> None:
    _write(sys.stderr, text)


def _tty(stream) -> bool:
    return cli_terminal.is_terminal(stream)


def _short(value: str, limit: int = 80) -> str:
    value = terminal_text(value)
    return value if len(value) <= limit else value[:limit - 3] + "..."


def _book_text(book) -> str:
    parts = (_short(book.title), _short(book.authors, 60), _short(book.year, 10),
             _short(book.extension, 10).upper(), _short(book.size, 20))
    return " | ".join(part for part in parts if part) or f"{book.book_id}:{book.book_hash}"


def _source_text(item) -> str:
    if item.reference is not None:
        return f"{item.position} {item.reference}"
    if item.query is not None:
        return f'{item.position} "{_short(item.query, 60)}"'
    return item.position


def _path_text(path: Path, folder: Path | None) -> str:
    if folder is not None:
        try:
            return shown_path(path.relative_to(folder))
        except ValueError:
            pass
    return shown_path(path)


def entry_line(entry, total: int, folder: Path | None) -> str:
    """One ASCII-structured line per item: input, matched edition, then file or reason."""
    parts = [f"[{entry.index}/{total}] {_LABELS[entry.status]}  {_source_text(entry.item)}"]
    if entry.book is not None and entry.status != "duplicate":
        parts.append("-> " + _book_text(entry.book))
    if entry.path is not None and entry.status in ("downloaded", "already_downloaded"):
        parts.append("| " + _path_text(entry.path, folder))
    if entry.duplicate_of is not None:
        parts.append(f"- same as [{entry.duplicate_of}/{total}]")
    if entry.status == "needs_choice":
        parts.append(f"- {len(entry.candidates)} candidates; use --pick, or choose in the JSON plan")
    elif entry.message:
        parts.append("- " + _short(entry.message, 400))
    return " ".join(parts)


def summary_text(batch) -> str:
    counts = batch.summary
    if not counts:
        return "No items."
    return ", ".join(f"{count} {_PLURALS.get(status, _LABELS[status]) if count != 1 else _LABELS[status]}"
                     for status, count in counts.items()) + "."


def exit_status(batch, *, dry_run: bool) -> int:
    """0 complete, 3 only waiting (run again later), 4 needs attention, 130 cancelled."""
    if batch.cancelled:
        return 130
    statuses = {entry.status for entry in batch.entries}
    stop = batch.stopped[0] if batch.stopped else None
    if statuses & _ATTENTION or (stop is not None and stop not in _WAITING_CODES) or not getattr(batch, "saved", True):
        return 4
    pending = {"not_started", "waiting_quota"} | (set() if dry_run else {"planned"})
    return 3 if statuses & pending else 0


def _positions(entries, limit=20) -> str:
    labels = [entry.item.position for entry in entries]
    shown = ", ".join(labels[:limit])
    return shown + (f" and {len(labels) - limit} more" if len(labels) > limit else "")


def guidance(batch, *, report_path: Path | None, hint: bool = True, moved: Path | None = None) -> list[str]:
    """Why the batch stopped, how to continue, and which positions need attention.

    ``moved`` is the folder an earlier report recorded, when it is not the output folder.
    """
    lines = []
    stop = batch.stopped[0] if batch.stopped else None
    if stop in _GUIDANCE:
        lines.append(_GUIDANCE[stop])
    entries = batch.entries
    waiting = [entry for entry in entries if entry.status in ("not_started", "waiting_quota", "planned")]
    if hint and waiting:
        if report_path is None and any(entry.status == "downloaded" or entry.quota_may_be_used for entry in entries):
            # Nothing records this run's downloads, so only the items still waiting can be run again safely.
            lines.append(f"No report was saved: to finish, run again with only {_positions(waiting)}; the same"
                         " command would download the finished books a second time. Add --report FILE from the"
                         " start to continue a list over several runs.")
        elif report_path is None:
            lines.append("No report was saved; run the same command again to continue.")
        elif not getattr(batch, "saved", True):
            lines.append(f"{shown_path(report_path)} keeps the progress of its last update; run the same command"
                         " again once that file can be written.")
        elif getattr(batch, "retry_uncertain", False) and any(entry.quota_may_be_used for entry in entries):
            # The same command would request again the links that this run left uncertain.
            lines.append("Run the command again without --retry-uncertain to continue; progress is saved in"
                         f" {shown_path(report_path)}.")
        else:
            lines.append(f"Run the same command again to continue; progress is saved in {shown_path(report_path)}.")
    for label, test in (("Not found", lambda entry: entry.status == "not_found"),
                        ("Errors", lambda entry: entry.status == "error" and not entry.quota_may_be_used
                         and entry.error_code != "TooLarge"),
                        ("Need a choice", lambda entry: entry.status == "needs_choice")):
        matched = [entry for entry in entries if test(entry)]
        if matched:
            lines.append(f"{label}: {_positions(matched)}.")
    large = [entry for entry in entries if entry.error_code == "TooLarge"]
    if large:
        # Running the list again keeps these editions: only a changed item is resolved again.
        lines.append(f"Larger than 256 MiB: {_positions(large)}. These editions are never requested again. To get"
                     " another edition, replace the item with that edition's ID:HASH (zlib-cli search lists them),"
                     " or reword its search, for example by adding the author.")
    doubtful = [entry for entry in entries if entry.quota_may_be_used and entry.error_code != "TooLarge"]
    if doubtful:
        lines.append(f"May have used quota: {_positions(doubtful)}. Check the folder and your download history;"
                     " --retry-uncertain tries them again.")
    missing = [entry for entry in entries if entry.message in (MISSING_FILE, REFUSED_FILE, MOVED_FILE)]
    if missing and moved is not None and any(entry.message == MOVED_FILE for entry in missing):
        # Downloading them again would spend quota on books that are probably in the recorded folder.
        lines.append(f"Missing files: {_positions(missing)}. The report recorded them in {shown_path(moved)};"
                     " pass --output with that folder to find them.")
    elif missing:
        lines.append(f"Missing files: {_positions(missing)}. --redownload-missing downloads them again.")
    return lines


@contextmanager
def cooperative_interrupts(stop: CancellationToken, transfer: CancellationToken):
    """The first Ctrl+C lets the current book finish, then stops; a second one cancels
    the current download at its next check; a third one stops at once."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    presses = 0

    def handle(signum, frame):
        nonlocal presses
        presses += 1
        if presses == 1:
            stop.cancel()
            message = _FIRST_INTERRUPT
        elif presses == 2:
            transfer.cancel()
            message = _SECOND_INTERRUPT
        else:
            raise KeyboardInterrupt
        try:
            os.write(2, message)
        except OSError:
            pass

    try:
        previous = signal.signal(signal.SIGINT, handle)
    except (ValueError, OSError):
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous if previous is not None else signal.default_int_handler)


def _validate(args) -> tuple[int, SearchFilters]:
    if args.batch_file is not None and args.query:
        raise ValueError("Give titles on the command line or a list with -a, not both.")
    if args.batch_file is None and not args.query:
        raise ValueError("Give a title, ISBN or reference, or a list with -a FILE.")
    if args.batch_format is not None and args.batch_file is None:
        raise ValueError("--batch-format applies to a list given with -a.")
    if args.page is not None:
        raise ValueError("--page cannot be used with several books; each search uses its first page.")
    limit = 5 if args.limit is None else args.limit
    if not 1 <= limit <= 50:
        raise ValueError("Choose --limit between 1 and 50 candidates per search.")
    if args.pick is not None and not 1 <= args.pick <= limit:
        raise ValueError("Choose --pick between 1 and the --limit number of candidates.")
    if args.yes and args.dry_run:
        raise ValueError("--yes cannot be combined with --dry-run, which never downloads.")
    if args.interactive and args.batch_file == "-":
        raise ValueError("--interactive needs the list in a file, because standard input holds the list.")
    if args.max_downloads is not None and args.max_downloads < 1:
        raise ValueError("Choose a positive --max-downloads number.")
    if len(args.query) > MAX_ITEMS:
        raise BatchInputError("The list has more than 1000 items; split it into smaller lists.")
    if not args.output.is_dir():
        raise ValueError("Choose an existing output directory.")
    filters = SearchFilters(languages=args.language or (), formats=args.format or (), year_from=args.year_from,
                            year_to=args.year_to, order=args.order)
    return limit, filters


def _read_items(args):
    """The items, the list file (None for titles and standard input), and the kind of a plan or
    report read as JSON (None for any other list)."""
    if args.batch_file is None:
        items = tuple(_text_item(value, f"argument {number}") for number, value in enumerate(args.query, 1))
        return items, None, None
    if args.batch_file == "-":
        stream = getattr(sys.stdin, "buffer", None)
        data = stream.read(MAX_INPUT_BYTES + 1) if stream is not None else sys.stdin.read(MAX_INPUT_BYTES + 1)
        items, kind = parse_input(data, kind=args.batch_format, warn=_note)
        return items, None, kind
    path = Path(args.batch_file)
    items, kind = parse_input(path, kind=args.batch_format, warn=_note)
    return items, path, kind


def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return first.absolute() == second.absolute()


def _report_path(args, list_path: Path | None, kind: str | None) -> Path | None:
    """--report, else a report given with -a, else LIST.progress.json; a plan has no default."""
    if args.no_report:
        return None
    if args.report is not None:
        path = args.report
    elif list_path is None or kind == PLAN_KIND:
        return None
    elif kind == REPORT_KIND:
        path = list_path
    else:
        path = list_path.with_name(list_path.stem + ".progress.json")
    path = Path(path).absolute()
    if path.is_dir() or not path.parent.is_dir():
        raise ValueError("The report must be a file in an existing folder.")
    if list_path is not None and kind != REPORT_KIND and path.exists() and _same_file(path, list_path):
        raise ValueError("The report would replace the list itself; choose another --report file.")
    return path


def _interactive(args, policy) -> bool:
    return policy.allowed and args.batch_file != "-"


def run_batch_command(args, *, client_factory, credentials_loader, prompt, password_prompt, policy=None) -> int:
    """Validate everything local first; only then sign in, plan and download."""
    policy = cli_terminal.InputPolicy.from_args(args) if policy is None else policy
    limit, filters = _validate(args)
    interactive = _interactive(args, policy)
    if not args.dry_run and not interactive and not args.yes:
        # Known before anything is read, so a refused run consumes no standard input.
        raise InputRequired("Use --yes to download a list without a terminal, or --dry-run to review it first.",
                            name="ValueError")
    if args.batch_file not in (None, "-"):
        credentials = credentials_path(named_credentials(args))  # The file a sign-in would read.
        if credentials is not None and _same_file(Path(args.batch_file), Path(credentials)):
            raise ValueError("This is the credentials file, not a list of books.")
    account = None
    if args.batch_file == "-":
        # The list occupies standard input, so an email prompt could not be answered.
        account = AccountSession(args, credentials_loader=credentials_loader)
        if not account.anonymous and not account.restore and not (account.credentials and account.credentials.email):
            raise InputRequired("Reading the list from standard input needs a saved session or an email in the"
                                " credentials file. Run zlib-cli login first, or pass the list as a file.",
                                name="ValueError")
    items, list_path, kind = _read_items(args)
    if not items:
        raise ValueError("The list contains no items.")
    cli_filters = filters
    if any(item.query is not None for item in items):
        # Configured formats and languages apply to searched items only, never to references.
        filters = with_configured_filters(args, filters, announce=False)
    report_path = _report_path(args, list_path, kind)
    if kind is not None and report_path is None and not args.no_report and not args.dry_run:
        # With -a FILE or -a -: downloads that no report records would be repeated when the list runs again.
        if kind == PLAN_KIND:
            raise ValueError("Downloading a plan needs --report FILE or --no-report. Use the list's .progress.json"
                             " file so that the list knows about these downloads.")
        raise ValueError("Downloading a report from standard input needs --report FILE or --no-report; give the"
                         " report with -a FILE to continue it.")
    command = _BatchCommand(args, items, limit=limit, filters=filters, report_path=report_path,
                            interactive=interactive, prompt=prompt, list_path=list_path, kind=kind)
    lock = report_lock(report_path) if report_path is not None and not args.dry_run else nullcontext()
    with lock:
        previous = load_report(report_path) if report_path is not None else None
        command.locate(previous)
        if not args.dry_run:
            if not command.interactive and args.pick is None:
                waiting = searches_needed(items, filters=filters, previous=previous,
                                          retry_uncertain=args.retry_uncertain,
                                          redownload_missing=args.redownload_missing, folder=command.folder)
                if waiting:
                    raise InputRequired(f"{len(waiting)} item{'s need' if len(waiting) != 1 else ' needs'} a search"
                                        " result choice. Use --pick N without a terminal, or --dry-run to see the"
                                        " candidates.", name="ValueError")
            check_folder(args.output)
        if account is None:
            account = AccountSession(args, credentials_loader=credentials_loader)
        account.require_input(policy, explicit=False)
        if filters is not cli_filters:
            with_configured_filters(args, cli_filters)  # The notice, once, before any request.
        with client_factory(account.domain, timeout=args.timeout) as client:
            account.authenticate(client, prompt=prompt, password_prompt=password_prompt)
            return command.run(client, previous)


class _Ticker:
    """Overwrite one stderr line while a list is checked without prompts."""

    def __init__(self, total: int, enabled: bool):
        self.total, self.enabled, self.used = total, enabled, False

    def __call__(self, entry) -> None:
        if self.enabled:
            try:
                sys.stderr.write(f"\rChecked {entry.index} of {self.total}")
                sys.stderr.flush()
                self.used = True
            except (OSError, ValueError):
                self.enabled = False

    def close(self) -> None:
        if self.used:
            _note("")
            self.used = False


class _BatchCommand:
    def __init__(self, args, items, *, limit, filters, report_path, interactive, prompt, list_path=None, kind=None):
        self.args, self.items, self.limit, self.filters = args, items, limit, filters
        self.report_path, self.interactive, self.prompt = report_path, interactive, prompt
        self.list_path, self.kind = list_path, kind
        self.folder = args.output.resolve()
        self.total = len(items)
        self.entries, self.cursor = [], 0
        self.indices = {id(item): number for number, item in enumerate(items, 1)}
        self.moved, self.elsewhere, self.hint = None, False, None

    def locate(self, previous) -> None:
        """Compare the report's folder with the output folder, before any request.

        Recorded books are looked up only in the output folder, never in the folder a
        document names. When the report recorded another folder and none of its books
        is here, a run without a terminal stops: it could only mix the two folders.
        """
        folder = None if previous is None else previous.folder
        if folder is None or folder == self.folder:
            return
        self.moved = folder
        found, missing = recorded_files(previous, self.folder)
        if not missing:
            return
        self.elsewhere = not found
        where, here = shown_path(folder), shown_path(self.folder)
        if self.elsewhere and not self.args.dry_run and not self.interactive:
            raise ValueError(f"The report recorded its books in {where}, and none of them is in {here}. Pass"
                             " --output with that folder to continue this list there, or run the command in a"
                             " terminal to use this folder anyway.")
        state = ("none of them is in" if self.elsewhere
                 else f"{missing} of them {'is' if missing == 1 else 'are'} not in")
        self.hint = (f"The report recorded its books in {where}, and {state} {here}; pass --output with that"
                     " folder to find them.")
        _note(self.hint)

    @property
    def report_folder(self) -> Path:
        # Until a book is found or downloaded here, the report keeps the folder that holds its books.
        return self.moved if self.elsewhere else self.folder

    def advance(self, _entry=None, *, everything: bool = False) -> None:
        """Print item lines in input order, as soon as each outcome is final."""
        while self.cursor < len(self.entries):
            entry = self.entries[self.cursor]
            if not everything and entry.status in ("planned", "downloading"):
                return
            self.cursor += 1
            if not self.args.json:
                _write(sys.stdout, entry_line(entry, self.total, self.folder))

    def choose(self, item, books):
        effective = merge_filters(self.filters, item.filters)
        details = ", ".join([*effective.formats, *effective.languages])
        _note(f"[{self.indices.get(id(item), '?')}/{self.total}] {_source_text(item)}"
              + (f" ({details})" if details else ""))
        show_books(books, file=sys.stderr)
        while True:
            try:
                answer = self.prompt(f"Choose 1-{len(books)}, s to skip, q to stop: ").strip().lower()
            except EOFError:
                raise StopPlanning from None
            if answer == "s":
                return None
            if answer == "q":
                raise StopPlanning
            if re.fullmatch(r"[0-9]{1,3}", answer) and 1 <= int(answer) <= len(books):
                return books[int(answer) - 1]
            _note(f"Type a number from 1 to {len(books)}, s or q.")

    def early_quota(self, client):
        # Informational: run_batch() reads the quota again before every download.
        try:
            return client.get_quota()
        except (AuthenticationError, ChallengeRequired):
            raise
        except (ConnectorError, ValueError):
            return None

    def save(self, report) -> None:
        if self.report_path is None:
            return
        try:
            write_report(report, self.report_path)
        except (OSError, ValueError):
            report.saved = False
        else:
            report.recorded = frozenset(entry.index for entry in report.entries if entry.status == "downloaded")

    def progress(self, entry):
        display = ProgressDisplay(self.args.progress)
        if display.enabled:
            _note(f"[{entry.index}/{self.total}] downloading {_book_text(entry.book)}")
        return display

    def confirmed(self, report, quota, planned: int) -> bool:
        if self.args.yes or not self.interactive:
            return True
        _note(f"Plan: {summary_text(report)}")
        for line in guidance(report, report_path=None, hint=False, moved=self.moved):
            _note(line)
        retried = [entry for entry in report.entries if entry.status == "planned" and entry.retried]
        if retried:
            _note(f"Trying again downloads that may have used quota: {_positions(retried)}.")
        if self.hint is not None and self.args.redownload_missing:
            _note(self.hint)  # The books about to be downloaded again may be in that folder.
        cap = self.args.max_downloads
        count = planned if cap is None else min(planned, cap)
        remaining = None if quota is None else quota.remaining
        quota_text = ""
        if remaining is not None:
            limit = "unknown" if quota.limit is None else quota.limit
            quota_text = f" Quota: {remaining} of {limit} remaining."
            if remaining < count:
                quota_text += f" Only {remaining} can be downloaded now; the rest will wait."
        books = f"{count} of {planned} books" if count < planned else f"{planned} book{'s' if planned != 1 else ''}"
        try:
            answer = self.prompt(f"Download {books} into {shown_path(self.folder)}?{quota_text} [y/N] ")
        except EOFError:
            answer = ""
        return answer.strip().lower() in ("y", "yes")

    def plan_name(self) -> str:
        """Where to save a plan: never the list being read, which a shell redirect empties first."""
        name = "plan.json"
        if self.list_path is not None and (self.list_path.name.casefold() == name
                                           or _same_file(Path(name), self.list_path)):
            name = "reviewed-plan.json"
        return name

    def dry_run_lines(self, batch) -> list[str]:
        """A dry run writes nothing: say so, and how to keep the choices it made."""
        report = None if self.report_path is None else shown_path(self.report_path)
        if report is not None and self.report_path.exists():
            lines = [f"Dry run: nothing was downloaded and {report} was not changed."]
        else:
            lines = ["Dry run: nothing was downloaded and no report was written."]
        if any(entry.status in ("planned", "skipped") for entry in batch.entries):
            # Choices this run made by searching, as opposed to those read from a plan.
            chosen = any((entry.status == "planned" and entry.verified and entry.item.query is not None)
                         or (entry.status == "skipped" and not entry.item.skipped) for entry in batch.entries)
            if self.kind == PLAN_KIND and not chosen:
                target = (f"--report {report}" if report is not None
                          else "--report with the list's .progress.json file, or --no-report")
                source = "-" if self.list_path is None else shown_path(self.list_path)
                lines.append(f"The plan already holds these choices: download it with -a {source} {target}.")
            else:
                name = self.plan_name()
                target = f"--report {report}" if report is not None else "--report FILE or --no-report"
                lines.append(f"To keep these choices, save them with --dry-run --json > {name}, then download"
                             f" -a {name} {target}.")
        return lines

    def finish(self, batch, *, dry_run: bool = False) -> int:
        if self.args.json:
            _write(sys.stdout, json.dumps(batch.to_dict(), ensure_ascii=True, allow_nan=False, indent=2))
            if dry_run and self.report_path is not None:
                _note(f"Download this plan with --report {shown_path(self.report_path)} to keep its progress"
                      " with this list.")
        else:
            self.entries = batch.entries
            self.advance(everything=True)
            _write(sys.stdout, summary_text(batch))
            for line in guidance(batch, report_path=self.report_path, hint=not dry_run, moved=self.moved):
                _write(sys.stdout, line)
            quota = batch.quota
            if quota is not None and quota.remaining is not None:
                limit = "unknown" if quota.limit is None else quota.limit
                _write(sys.stdout, f"Quota: {quota.remaining} of {limit} downloads remaining.")
            if dry_run:
                for line in self.dry_run_lines(batch):
                    _write(sys.stdout, line)
        if not getattr(batch, "saved", True):
            _note(f"The report could not be updated: {shown_path(self.report_path)}.")
            recorded = getattr(batch, "recorded", frozenset())
            for entry in batch.entries:
                if entry.status == "downloaded" and entry.path is not None and entry.index not in recorded:
                    _note(f"Downloaded but not recorded: {shown_path(entry.path)}")
        if batch.cancelled:
            _note("Cancelled.")
        return exit_status(batch, dry_run=dry_run)

    def run(self, client, previous) -> int:
        args = self.args
        quota = self.early_quota(client)
        if self.interactive and quota is not None and quota.remaining is not None:
            limit = "unknown" if quota.limit is None else quota.limit
            _note(f"Quota: {quota.remaining} of {limit} downloads remaining; choosing editions uses none.")
        if args.pick is not None:
            choose = pick(args.pick)
        elif self.interactive:
            choose = self.choose
            if args.dry_run and not args.json:
                _note(f"Dry run: choices are not saved; add --json > {self.plan_name()} to keep them.")
        else:
            choose = None
        unattended = args.pick is not None or not self.interactive
        ticker = _Ticker(self.total, unattended and args.progress is not False and _tty(sys.stderr))
        try:
            plan = plan_batch(client, self.items, filters=self.filters, candidates=self.limit, choose=choose,
                              previous=previous, retry_uncertain=args.retry_uncertain,
                              redownload_missing=args.redownload_missing, on_item=ticker,
                              pause=PLANNING_PAUSE, folder=self.folder)
        finally:
            ticker.close()
        plan.quota, plan.folder = quota, self.folder
        if any(entry.status == "already_downloaded" and entry.path is not None and entry.path.is_absolute()
               for entry in plan.entries):
            self.elsewhere = False  # A recorded book was found here after all: this folder becomes the report's.
        if args.dry_run:
            return self.finish(plan, dry_run=True)
        report = BatchReport.from_plan(plan, self.report_folder)
        if plan.cancelled or (plan.stopped is not None and plan.stopped[0] != "Stopped"):
            for entry in report.entries:
                if entry.status == "planned":
                    defer(entry, "planned")  # A retried uncertain item keeps its doubt.
            self.save(report)  # Keep the choices made so far; nothing was downloaded.
            return self.finish(report)
        planned = sum(entry.status == "planned" for entry in report.entries)
        if planned and quota is not None and quota.remaining == 0:
            for entry in report.entries:
                if entry.status == "planned":
                    defer(entry, "waiting_quota")
            report.stopped = ("QuotaExhausted", MESSAGES["QuotaExhausted"])
            self.save(report)
            return self.finish(report)
        remaining = None if quota is None else quota.remaining
        capped = args.max_downloads is not None and args.max_downloads < planned  # A certain early stop.
        unrecorded = (planned and self.report_path is None and not args.no_report
                      and (remaining is None or remaining < planned or capped))
        if unrecorded and not self.interactive and capped:
            raise ValueError(f"--max-downloads {args.max_downloads} is lower than the {planned} books planned."
                             " Without --report, a later run would download the finished books again: add"
                             f" --report FILE to continue later, or --no-report to download only {args.max_downloads}.")
        if unrecorded and not self.interactive:
            known = "unknown" if remaining is None else f"{remaining}, lower than the {planned} books planned"
            raise ValueError(f"The remaining quota is {known}. Without --report, a later run would download the"
                             " finished books again: add --report FILE to continue later, or --no-report to"
                             " download what the quota allows.")
        if unrecorded:
            _note("Without --report, a later run will download these books again.")
        if planned and not self.confirmed(report, quota, planned):
            report.cancelled, report.stopped = True, ("Cancelled", "Cancelled before downloading.")
            if args.json:
                _write(sys.stdout, json.dumps(report.to_dict(), ensure_ascii=True, allow_nan=False, indent=2))
            _note("Cancelled.")
            return 130
        if not planned:
            self.save(report)
            return self.finish(report)
        leftovers = sum(1 for _ in self.folder.glob(".zlib-*.part"))
        if leftovers:
            _note(f"The output folder holds {leftovers} temporary .zlib-*.part file{'s' if leftovers != 1 else ''}"
                  " from an interrupted transfer; delete them when no download is running.")
        self.entries = report.entries
        self.advance()
        plan._report_folder = self.report_folder  # Until a book is downloaded here, as for the reports above.
        stop, transfer = CancellationToken(), CancellationToken()
        with cooperative_interrupts(stop, transfer):
            report = run_batch(client, plan, self.folder, report=self.report_path, progress_factory=self.progress,
                               cancel=transfer, stop=stop, on_item=self.advance, max_downloads=args.max_downloads)
        return self.finish(report)
