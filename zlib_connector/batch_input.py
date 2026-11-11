"""Batch lists from text, CSV or JSON, parsed without any network access.

Each line, row or array entry becomes a BatchItem: a free-text search, an exact
ID:HASH reference, or an item error that names its position. Only structural
problems (encoding, size, CSV header, JSON syntax, item count) reject the whole
list. Messages name positions and never echo the file contents.
"""
import codecs
import csv
from dataclasses import dataclass, field
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata
from urllib.parse import urlsplit

from .books import BookReference, _isbn
from .client import Book, SUPPORTED_FORMATS
from .search import SearchFilters
from .terminal_text import terminal_text


MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_ITEMS = 1000
MAX_QUERY_CHARS = 500
PLAN_KIND = "zlib-connector-batch-plan"
REPORT_KIND = "zlib-connector-batch-report"
INPUT_KINDS = ("txt", "csv", "json")
STATUSES = ("downloaded", "already_downloaded", "duplicate", "skipped", "planned", "needs_choice", "not_found",
            "error", "waiting_quota", "not_started", "cancelled", "interrupted", "downloading")
DOCUMENT_KEYS = frozenset({"kind", "version", "created_at", "updated_at", "folder", "quota", "stopped",
                           "summary", "items", "previous"})
RECORD_KEYS = frozenset({"index", "position", "source", "status", "reference", "book", "path", "duplicate_of",
                         "error_code", "message", "quota_may_be_used", "candidates"})
BOOK_KEYS = ("title", "authors", "extension", "year", "language", "size")
FILTER_KEYS = ("formats", "languages", "year_from", "year_to", "order")
_SUFFIXES = {".txt": "txt", ".csv": "csv", ".tsv": "csv", ".json": "json"}
_LINE_BREAKS = re.compile(r"\r\n|\r|\n")
_REFERENCE_LIKE = re.compile(r"[0-9]+:\S*")
_BOOK_PATH = re.compile(r"/book/([0-9]+)/([A-Za-z0-9]+)(?:/[^?#]*)?")
_SERIES = re.compile(r"\s*\([^()]*#\s*[0-9]+(?:\.[0-9]+)?\)\s*$")
_SCIENTIFIC = re.compile(r"[0-9]+(?:[.,][0-9]+)?E\+?[0-9]+")
_JOINERS = frozenset("\u200c\u200d")
_COLUMNS = {
    "query": "query", "title": "title", "author": "author", "authors": "author",
    "isbn": "isbn", "isbn13": "isbn13", "isbn10": "isbn10", "isbn/uid": "isbn_uid",
    "reference": "reference", "url": "url", "book_id": "book_id", "book_hash": "book_hash",
    "extension": "extension", "format": "format", "language": "language",
}
_USABLE = frozenset({"query", "title", "isbn", "isbn13", "isbn10", "isbn_uid", "reference", "url", "book_id"})
_ISBN_FIELDS = ("isbn13", "isbn", "isbn10", "isbn_uid")
_HEADER_WORDS = frozenset({"query", "title", "isbn", "isbn13", "isbn10", "isbn_uid", "reference", "url"})
_ONE_COLUMN = "\x1f"  # A header without a delimiter is one column; _clean() refuses this character in a value.
_CREDENTIAL_SETTING = re.compile(r"[\"']?(?:email|password)[\"']?\s*=", re.IGNORECASE)
_BOOK_SUFFIXES = (".pdf", ".epub", ".djvu")
_NAME_FORBIDDEN = frozenset('/\\:<>"|?*')
_RESERVED_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                             *(prefix + digit for prefix in ("COM", "LPT") for digit in "123456789\xb9\xb2\xb3")})
_OUTCOMES = frozenset({"downloaded", "already_downloaded", "interrupted", "downloading"})


class BatchInputError(ValueError):
    """A list, plan or report cannot be used. Messages never echo file contents."""


class _Problem(Exception):
    """An item-level problem: the entry becomes an error, the list stays usable."""


@dataclass(frozen=True)
class BatchItem:
    """One requested book: a search ``query`` or an exact ``reference`` ("ID:HASH").

    ``filters`` replaces the batch search filters field by field for this query.
    ``choice`` is an edition already chosen in a plan or report; its metadata is
    for display until the API confirms it. ``recorded`` marks a download that a
    plan or report recorded, and ``path`` its file name (None when no usable name
    was recorded): planning looks it up in the output folder only, and never
    opens it unless it is a plain book file name. ``uncertain`` marks an earlier
    attempt that may have used quota, ``too_large`` an edition recorded as larger
    than this client downloads, ``skipped`` a recorded skip. ``error`` marks an
    unusable entry.
    """

    position: str
    query: str | None = None
    reference: str | None = None
    filters: SearchFilters = field(default_factory=SearchFilters)
    choice: Book | None = None
    skipped: bool = False
    path: Path | None = None
    uncertain: bool = False
    error: str | None = None
    recorded: bool = False
    too_large: bool = False

    def __post_init__(self):
        if not isinstance(self.position, str) or not 1 <= len(self.position) <= 64:
            raise ValueError("A batch item needs a position label of 1 to 64 characters.")
        if self.error is not None:
            if not isinstance(self.error, str) or self.query is not None or self.reference is not None:
                raise ValueError("An item error carries a message instead of a query or reference.")
        elif (self.query is None) == (self.reference is None):
            raise ValueError("A batch item needs either a query or a reference.")
        if self.query is not None and (not isinstance(self.query, str)
                                       or not 1 <= len(self.query.strip()) <= MAX_QUERY_CHARS):
            raise ValueError("A batch query must contain 1 to 500 characters.")
        if self.reference is not None and (not isinstance(self.reference, str)
                                           or str(BookReference.parse(self.reference)) != self.reference):
            raise ValueError("A batch reference must be a normalized ID:HASH value.")
        if (not isinstance(self.filters, SearchFilters) or type(self.skipped) is not bool
                or type(self.uncertain) is not bool or type(self.recorded) is not bool
                or type(self.too_large) is not bool or (self.path is not None and not isinstance(self.path, Path))):
            raise ValueError("Batch item filters, flags or paths have invalid types.")
        if self.choice is not None:
            if not isinstance(self.choice, Book):
                raise ValueError("A batch choice must be a Book.")
            BookReference(self.choice.book_id, self.choice.book_hash)


@dataclass(frozen=True)
class _Record:
    """A validated plan or report entry; metadata is display-only text."""

    index: int
    position: str
    query: str | None
    reference: str | None
    filters: SearchFilters | None
    status: str
    book: Book | None
    path: str | None
    duplicate_of: int | None
    error_code: str | None
    message: str | None
    quota_may_be_used: bool
    candidates: tuple[Book, ...]
    pick: int | None


def _label(position: str) -> str:
    return position[:1].upper() + position[1:]


def _controls(value: str) -> bool:
    return any(unicodedata.category(character) in ("Cc", "Cs") for character in value)


def book_file_name(name) -> bool:
    """True for one plain book file name, checked without touching the filesystem.

    A folder, drive, UNC or device prefix, ``.`` or ``..``, control characters and
    suffixes other than .pdf, .epub and .djvu are refused, so a name from a document
    can only mean a file directly inside the output folder.
    """
    if not isinstance(name, str) or not 1 <= len(name) <= 255 or name != name.strip() or name in (".", ".."):
        return False
    if any(character in _NAME_FORBIDDEN for character in name) or _controls(name):
        return False
    if name.split(".", 1)[0].rstrip(" ").upper() in _RESERVED_NAMES:
        return False
    return PurePosixPath(name).suffix.lower() in _BOOK_SUFFIXES


def _clean(value: str, position: str) -> str:
    """Normalize whitespace and NFC; drop invisible format marks; refuse controls."""
    characters = []
    for character in value:
        category = unicodedata.category(character)
        if character in "\t\n\r":
            characters.append(" ")
        elif category == "Cf":
            if character in _JOINERS:
                characters.append(character)
        elif category in ("Cc", "Cs"):
            raise _Problem(f"{_label(position)} contains control characters.")
        else:
            characters.append(character)
    return unicodedata.normalize("NFC", " ".join("".join(characters).split()))


def _looks_like_reference(text: str) -> bool:
    return "://" in text or _REFERENCE_LIKE.fullmatch(text) is not None


def _reference(text: str, position: str) -> str:
    """Accept ID:HASH or an http(s) /book/ID/HASH URL from any mirror; keep ID:HASH only."""
    try:
        if "://" not in text:
            return str(BookReference.parse(text))
        parsed = urlsplit(text)
        match = _BOOK_PATH.fullmatch(parsed.path)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or match is None:
            raise ValueError
        return str(BookReference(*match.groups()))
    except ValueError:
        raise _Problem(f"{_label(position)} is not a valid ID:HASH reference or book URL.") from None


def _query(text: str, position: str) -> str:
    if not 1 <= len(text) <= MAX_QUERY_CHARS:
        raise _Problem(f"{_label(position)} must contain 1 to 500 characters.")
    return text


def _text_item(value: str, position: str) -> BatchItem:
    """Classify one TXT line, argument or JSON string; references fail closed."""
    try:
        text = _clean(value, position)
        if not text:
            raise _Problem(f"{_label(position)} is empty.")
        if _looks_like_reference(text):
            return BatchItem(position, reference=_reference(text, position))
        return BatchItem(position, query=_query(text, position))
    except _Problem as problem:
        return BatchItem(position, error=str(problem))


def _unwrap(value: str) -> str:
    # Spreadsheet exports such as Goodreads write ISBNs as ="0441172717".
    stripped = value.strip()
    if len(stripped) >= 3 and stripped.startswith('="') and stripped.endswith('"'):
        return stripped[2:-1]
    return value


def _field_text(fields: dict, name: str, position: str) -> str | None:
    value = fields.get(name)
    if value is None:
        return None
    if type(value) is int and name in ("book_id", *_ISBN_FIELDS):
        value = str(value)
    if not isinstance(value, str):
        raise _Problem(f"{_label(position)}: {name} must be text.")
    return _clean(_unwrap(value), position) or None


def _field_values(fields: dict, name: str, position: str) -> tuple[str, ...]:
    value = fields.get(name)
    if value is None:
        return ()
    values = value if isinstance(value, list) else [value]
    result = []
    for entry in values:
        if not isinstance(entry, str):
            raise _Problem(f"{_label(position)}: {name} must be text.")
        for part in re.split(r"[,;|]", _unwrap(entry)):
            part = _clean(part, position)
            if part:
                result.append(part)
    return tuple(result)


def _isbn_query(fields: dict, position: str) -> str | None:
    problem = None
    for name in _ISBN_FIELDS:
        value = _field_text(fields, name, position)
        if value is None:
            continue
        compact = re.sub(r"[\s-]", "", value).upper()
        if _SCIENTIFIC.fullmatch(compact):
            problem = problem or _Problem(
                f"{_label(position)} has an ISBN changed into a number; format the spreadsheet column as text.")
            continue
        if compact and compact in _isbn(compact):
            return compact
        problem = problem or _Problem(f"{_label(position)} has an invalid ISBN.")
    if problem is not None:
        raise problem
    return None


def _filters(fields: dict, position: str, notes: set) -> SearchFilters:
    formats = _field_values(fields, "extension", position)
    loose = _field_values(fields, "format", position)
    if loose:
        # StoryGraph's Format column holds paperback/hardcover: use only file formats.
        if all(value.lower() in SUPPORTED_FORMATS for value in loose):
            formats = formats or loose
        else:
            notes.add("format")
    try:
        return SearchFilters(languages=_field_values(fields, "language", position), formats=formats)
    except ValueError:
        raise _Problem(f"{_label(position)} has an unsupported format or an invalid language.") from None


def _fields_item(fields: dict, position: str, notes: set) -> BatchItem:
    """Build an item from CSV cells or JSON fields; a reference wins over search fields."""
    try:
        reference = _field_text(fields, "reference", position)
        if reference is not None:
            return BatchItem(position, reference=_reference(reference, position))
        book_id, book_hash = _field_text(fields, "book_id", position), _field_text(fields, "book_hash", position)
        if book_id is not None or book_hash is not None:
            try:
                return BatchItem(position, reference=str(BookReference(book_id, book_hash)))
            except ValueError:
                raise _Problem(f"{_label(position)} needs a valid book_id and book_hash.") from None
        url = _field_text(fields, "url", position)
        if url is not None:
            try:
                return BatchItem(position, reference=_reference(url, position))
            except _Problem:
                pass  # Other links, such as a catalog page, are not book references.
        query = _field_text(fields, "query", position)
        if query is None:
            title = _field_text(fields, "title", position)
            if title is not None:
                title = _SERIES.sub("", title).strip() or title
                author = _field_text(fields, "author", position)
                combined = f"{title} {author}" if author else title
                query = combined if len(combined) <= MAX_QUERY_CHARS else title
            else:
                query = _isbn_query(fields, position)
        if query is None:
            raise _Problem(f"{_label(position)} has no query, title, ISBN or reference.")
        return BatchItem(position, query=_query(query, position), filters=_filters(fields, position, notes))
    except _Problem as problem:
        return BatchItem(position, error=str(problem))


def _too_many() -> BatchInputError:
    return BatchInputError("The list has more than 1000 items; split it into smaller lists.")


def _column(name: str) -> str | None:
    return _COLUMNS.get(" ".join(name.replace("\ufeff", "").split()).casefold())


def _display_names(names) -> str:
    shown = [terminal_text(name)[:40] or "(blank)" for name in names[:12]]
    if len(names) > 12:
        shown.append(f"and {len(names) - 12} more")
    return ", ".join(shown)


def _split_header(line: str, delimiter: str) -> list[str]:
    try:
        return next(csv.reader([line], delimiter=delimiter), [])
    except csv.Error:
        return []


def _delimiter(line: str) -> tuple[str, int]:
    best, score = ",", 0
    for delimiter in (",", ";", "\t"):
        count = sum(_column(name) is not None for name in _split_header(line, delimiter))
        if count > score:
            best, score = delimiter, count
    return best, score


def _spans_rows(fields: dict, delimiter: str) -> bool:
    # A used value whose later lines hold the delimiter swallowed rows after a missing closing quote;
    # in a one-column list any line break in a used value does.
    return any(delimiter == _ONE_COLUMN or delimiter in line
               for value in fields.values() for line in _LINE_BREAKS.split(value)[1:])


def _unclosed(text: str, row: list[str]) -> bool:
    # Without strict mode, a quote that is never closed makes the last cell run to the end of the text.
    if not row:
        return False
    cell = row[-1]
    body = cell.rstrip("\r\n")
    return text.endswith(cell) and ("\n" in body or "\r" in body)


def _parse_csv(text: str, notes: set, *, delimiter: str | None = None) -> list[BatchItem]:
    offset = 0
    if text.startswith("#TYPE "):
        # Windows PowerShell 5.1 Export-Csv writes a type line before the header.
        first = _LINE_BREAKS.search(text)
        text = "" if first is None else text[first.end():]
        offset = 1
    header_line = next((line for line in _LINE_BREAKS.split(text) if line.strip()), "")
    if delimiter is None:
        if any(mark in header_line for mark in (",", ";", "\t")):
            delimiter, _ = _delimiter(header_line)
        else:
            delimiter = _ONE_COLUMN  # One column: commas and semicolons stay inside the values.
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    items, columns, previous_line, last = [], None, 0, None
    try:
        for row in reader:
            start, previous_line = previous_line + 1 + offset, reader.line_num
            last = (start, row)
            if not any(cell.strip() for cell in row):
                continue
            if columns is None:
                columns, seen, ignored = [], set(), []
                for name in row:
                    column = _column(name)
                    if column is None or column in seen:
                        ignored.append(name.strip())
                        column = None
                    else:
                        seen.add(column)
                    columns.append(column)
                if not seen & _USABLE:
                    if _unclosed(text, row):
                        raise BatchInputError(f"Line {start} opens a quote that is never closed; close or remove it.")
                    raise BatchInputError(
                        f"The CSV header has no usable column (found: {_display_names([name.strip() for name in row])}). "
                        "Use a title, query, isbn or reference column.")
                if ignored:
                    notes.add(("columns", tuple(ignored)))
                continue
            position = f"line {start}"
            fields = {column: cell for column, cell in zip(columns, row) if column is not None and cell.strip()}
            if any(cell.strip() for cell in row[len(columns):]):
                item = BatchItem(position, error=f"Line {start} has more cells than the header; put values that"
                                                 " contain the delimiter in double quotes.")
            elif _spans_rows(fields, delimiter):
                item = BatchItem(position, error=f"Line {start} has a value that spans several rows; check for a"
                                                 " missing closing quote.")
            elif fields:
                item = _fields_item(fields, position, notes)
            else:
                item = BatchItem(position, error=f"Line {start} has no query, title, ISBN or reference.")
            items.append(item)
            if len(items) > MAX_ITEMS:
                raise _too_many()
    except csv.Error:
        raise BatchInputError(f"The CSV list could not be read near line {reader.line_num + offset}.") from None
    if last is not None and _unclosed(text, last[1]):
        raise BatchInputError(f"Line {last[0]} opens a quote that is never closed; close or remove it.")
    if columns is None:
        raise BatchInputError("The CSV list has no header row.")
    return items


def _looks_like_csv_header(line: str) -> bool:
    for delimiter in (",", ";", "\t"):
        if delimiter in line:
            names = _split_header(line, delimiter)
            if len(names) >= 2 and sum(_column(name) is not None for name in names) >= 2:
                return True
    return False


def _header_word(line: str) -> str | None:
    """The column a one-cell first line names (quotes removed), such as Title or "ISBN"."""
    cells = _split_header(line, ",")
    return _column(cells[0]) if len(cells) == 1 else None


def _holds_json(first: str, text: str) -> bool:
    """True when text read as a list is JSON: its first line is a lone bracket, or the whole text parses."""
    if first in ("[", "{"):
        return True  # Pretty-printed JSON, also when hand-editing broke it.
    if first[:1] not in ("[", "{"):
        return False
    try:
        json.loads(text)
    except (ValueError, RecursionError):
        return False  # A title such as "[Graphic novel] Maus".
    return True


def _parse_txt(text: str, *, detect_csv: bool) -> list[BatchItem]:
    items = []
    for number, line in enumerate(_LINE_BREAKS.split(text), 1):
        # Byte-order marks, zero-width and bidi characters must not hide a comment or a blank line.
        visible = "".join(character for character in line if unicodedata.category(character) != "Cf").strip()
        if not visible or visible.startswith("#"):
            continue
        if _CREDENTIAL_SETTING.match(visible):
            raise BatchInputError(f"Line {number} looks like a credentials setting, not a book; the list was not used.")
        if not items and detect_csv:
            if _holds_json(visible, text):
                # A plan saved as plan.txt would otherwise be searched line by line, without its report rules.
                raise BatchInputError("This list holds JSON, such as a plan or a report; name it .json or use"
                                      " --batch-format json.")
            if _looks_like_csv_header(visible):
                raise BatchInputError("This list looks like CSV with a header row; read it as CSV (--batch-format csv).")
            word = _header_word(visible)
            if word in _HEADER_WORDS:
                raise BatchInputError("The first line looks like a column name. Use --batch-format csv to read a"
                                      " one-column CSV, or --batch-format txt to search it.")
            if word == "author":
                raise BatchInputError("The first line looks like a column name; remove it, or use"
                                      " --batch-format txt to search it.")
        items.append(_text_item(line, f"line {number}"))
        if len(items) > MAX_ITEMS:
            raise _too_many()
    return items


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError("non-finite number")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value


def load_json(text: str, what: str):
    """Parse strict JSON: duplicate keys and non-finite numbers are refused."""
    try:
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant,
                          parse_float=_finite_float)
    except RecursionError:
        raise BatchInputError(f"The {what} is nested too deeply.") from None
    except json.JSONDecodeError as error:
        raise BatchInputError(f"The {what} is not valid JSON (line {error.lineno}, column {error.colno}).") from None
    except ValueError:
        raise BatchInputError(f"The {what} repeats a key or contains a non-finite number.") from None


def _display(value, where: str, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 2000:
        raise BatchInputError(f"{where} has an invalid {name}.")
    return terminal_text(value)


def _display_book(value, where: str) -> Book:
    if not isinstance(value, dict) or not set(value) <= {"reference", *BOOK_KEYS}:
        raise BatchInputError(f"{where} has an invalid book record.")
    reference = value.get("reference")
    try:
        parsed = BookReference.parse(reference) if isinstance(reference, str) else None
    except ValueError:
        parsed = None
    if parsed is None or str(parsed) != reference:
        raise BatchInputError(f"{where} has an invalid reference.")
    text = {name: _display(value.get(name), where, name) for name in BOOK_KEYS}
    return Book(text["title"], text["authors"], text["extension"].lower(), "", text["year"], text["language"],
                text["size"], parsed.book_id, parsed.book_hash)


def _record_filters(value, where: str) -> SearchFilters:
    if not isinstance(value, dict) or not set(value) <= set(FILTER_KEYS):
        raise BatchInputError(f"{where} has invalid search filters.")
    try:
        for name in ("formats", "languages"):
            entries = value.get(name, [])
            if not isinstance(entries, list) or any(not isinstance(entry, str) for entry in entries):
                raise ValueError
        return SearchFilters(languages=tuple(value.get("languages", [])), formats=tuple(value.get("formats", [])),
                             year_from=value.get("year_from"), year_to=value.get("year_to"),
                             order=value.get("order"))
    except ValueError:
        raise BatchInputError(f"{where} has invalid search filters.") from None


def _small_int(value, *, minimum=1) -> bool:
    return type(value) is int and minimum <= value <= 10**9


def _fixed_edition(where: str) -> BatchInputError:
    # The identity is the source, so a copy of the item with another reference would be a duplicate.
    return BatchInputError(
        f"{where} was downloaded or attempted already, so its reference cannot change. To get another edition"
        ' too, add its ID:HASH as a new line of the list, or a new item whose source is {"reference": "ID:HASH"};'
        " a copy of this item would be skipped as a duplicate.")


def parse_record(record, where: str) -> _Record:
    """Validate one entry of a plan or report written by this module."""
    if not isinstance(record, dict) or not set(record) <= RECORD_KEYS | {"pick"}:
        raise BatchInputError(f"{where} has an unknown or invalid field.")
    if "source" not in record or record.get("status") not in STATUSES:
        raise BatchInputError(f"{where} needs a source and a known status.")
    index = record.get("index", 1)
    position = record.get("position", "")
    if not _small_int(index) or not isinstance(position, str) or len(position) > 64:
        raise BatchInputError(f"{where} has an invalid index or position.")
    position = terminal_text(position)
    source, query, reference, filters = record["source"], None, None, None
    if source is not None:
        if not isinstance(source, dict):
            raise BatchInputError(f"{where} has an invalid source.")
        if set(source) == {"reference"}:
            reference = source["reference"]
            try:
                valid = isinstance(reference, str) and str(BookReference.parse(reference)) == reference
            except ValueError:
                valid = False
            if not valid:
                raise BatchInputError(f"{where} has an invalid source reference.")
        elif "query" in source and set(source) <= {"query", "filters"}:
            try:
                query = _query(_clean(source["query"], position or where), where) if isinstance(source["query"], str) else None
            except _Problem:
                query = None
            if query is None:
                raise BatchInputError(f"{where} has an invalid source query.")
            filters = _record_filters(source.get("filters", {}), where)
        else:
            raise BatchInputError(f"{where} has an invalid source.")
    duplicate_of = record.get("duplicate_of")
    error_code, message = record.get("error_code"), record.get("message")
    used = record.get("quota_may_be_used", False)
    if ((duplicate_of is not None and not _small_int(duplicate_of))
            or (error_code is not None and (not isinstance(error_code, str) or re.fullmatch(r"[A-Za-z]{1,40}", error_code) is None))
            or (message is not None and (not isinstance(message, str) or len(message) > 1000))
            or type(used) is not bool):
        raise BatchInputError(f"{where} has an invalid status detail.")
    status = record["status"]
    chosen = record.get("reference")
    try:
        parsed = BookReference.parse(chosen) if isinstance(chosen, str) else None
    except ValueError:
        parsed = None
    if chosen is not None and (parsed is None or str(parsed) != chosen):
        raise BatchInputError(f"{where} has an invalid reference.")
    # book names the edition that status, path and quota_may_be_used describe; reference is the choice.
    book_value = record.get("book")
    attempted = None if book_value is None else _display_book(book_value, where)
    attempted_reference = None if attempted is None else f"{attempted.book_id}:{attempted.book_hash}"
    fixed = status in _OUTCOMES or (status in ("error", "cancelled") and used) or (
        status == "error" and error_code == "TooLarge")
    if fixed and (attempted is None or chosen != attempted_reference):
        raise _fixed_edition(where)
    if chosen is None:
        book = None
    elif chosen == attempted_reference:
        book = attempted
    else:
        book = Book("", "", "", "", "", "", "", parsed.book_id, parsed.book_hash)  # A new choice: no metadata yet.
    candidates = record.get("candidates", [])
    if not isinstance(candidates, list) or len(candidates) > 50:
        raise BatchInputError(f"{where} has invalid candidates.")
    candidates = tuple(_display_book(candidate, where) for candidate in candidates)
    pick = record.get("pick")
    if pick is not None and (not _small_int(pick) or pick > len(candidates)):
        raise BatchInputError(f"{where} has a pick number outside its candidates.")
    if pick is not None and fixed:
        raise _fixed_edition(where)
    path = record.get("path")
    if path is not None and (not isinstance(path, str) or not 1 <= len(path) <= 4096 or _controls(path)
                             or Path(path).suffix.lower() not in _BOOK_SUFFIXES):
        raise BatchInputError(f"{where} has an invalid file path.")
    return _Record(index, position or where, query, reference, filters, status, book, path,
                   duplicate_of, error_code, None if message is None else terminal_text(message), used,
                   candidates, pick)


def parse_document(document, *, label: str) -> tuple[list[_Record], list[_Record], dict]:
    """Validate a plan or report; returns current records, earlier records and metadata."""
    if not isinstance(document, dict) or not set(document) <= DOCUMENT_KEYS:
        raise BatchInputError(f"The {label} has unknown or invalid fields.")
    if type(document.get("version")) is not int or document["version"] != 1:
        raise BatchInputError(f"The {label} uses an unsupported version.")
    for name in ("created_at", "updated_at", "folder"):
        value = document.get(name)
        if value is not None and (not isinstance(value, str) or len(value) > 4096):
            raise BatchInputError(f"The {label} has an invalid {name}.")
    quota = document.get("quota")
    if quota is not None and (not isinstance(quota, dict) or set(quota) != {"limit", "used", "remaining"}
                              or any(value is not None and not _small_int(value, minimum=0) for value in quota.values())):
        raise BatchInputError(f"The {label} has an invalid quota.")
    stopped = document.get("stopped")
    if stopped is not None and (not isinstance(stopped, dict) or set(stopped) != {"error_code", "message"}
                                or not all(isinstance(value, str) and len(value) <= 1000 for value in stopped.values())):
        raise BatchInputError(f"The {label} has an invalid stop reason.")
    summary = document.get("summary", {})
    if not isinstance(summary, dict) or any(key not in STATUSES or not _small_int(value, minimum=0)
                                            for key, value in summary.items()):
        raise BatchInputError(f"The {label} has an invalid summary.")
    items, earlier = document.get("items"), document.get("previous", [])
    if not isinstance(items, list) or not isinstance(earlier, list) or len(items) > MAX_ITEMS or len(items) + len(earlier) > 10_000:
        raise BatchInputError(f"The {label} needs an items list within the supported size.")
    current = [parse_record(record, f"Item {number} of the {label}") for number, record in enumerate(items, 1)]
    previous = [parse_record(record, f"Earlier entry {number} of the {label}")
                for number, record in enumerate(earlier, 1)]
    return current, previous, document


def record_path(record: _Record) -> Path | None:
    """The recorded file as written; planning resolves only a plain name, inside the output folder."""
    return None if record.path is None else Path(record.path)


def too_large(record) -> bool:
    """True for an edition that failed because it is larger than this client downloads; never retried."""
    return record.status == "error" and record.error_code == "TooLarge"


def uncertain(record: _Record) -> bool:
    """True when an earlier attempt may have used quota without a recorded file and may be retried."""
    return not too_large(record) and (record.status in ("interrupted", "downloading") or (
        record.status in ("error", "cancelled") and record.quota_may_be_used))


def _record_item(record: _Record) -> BatchItem:
    """Read a plan or report entry back as input: its choice wins, metadata is never searched."""
    position = record.position
    if record.query is None and record.reference is None:
        return BatchItem(position, error=f"{_label(position)} was not a usable entry in the original list.")
    choice = record.book
    if record.pick is not None:
        choice = record.candidates[record.pick - 1]
    # A recorded download stays one even without a usable file name: only --redownload-missing repeats it.
    done = record.status in ("downloaded", "already_downloaded") and record.book is not None
    return BatchItem(position, query=record.query, reference=record.reference,
                     filters=record.filters or SearchFilters(), choice=choice,
                     skipped=record.status == "skipped" and choice is None,
                     path=record_path(record) if done else None, recorded=done,
                     uncertain=uncertain(record) and choice is not None and record.pick is None,
                     too_large=too_large(record) and choice is not None)


def _parse_json(text: str, notes: set) -> tuple[list[BatchItem], str | None]:
    """The items, and the kind of a plan or report document (None for other JSON)."""
    if not text.strip():
        # Usually "command -a plan.json > plan.json": the shell empties the file before the command reads it.
        raise BatchInputError("The JSON list is empty. Redirecting a command's output into the file it reads"
                              " empties that file.")
    document = load_json(text, "JSON list")
    if isinstance(document, dict) and document.get("kind") in (PLAN_KIND, REPORT_KIND):
        label = "plan" if document["kind"] == PLAN_KIND else "report"
        current, _, _ = parse_document(document, label=label)
        return [_record_item(record) for record in current], document["kind"]
    if isinstance(document, list):
        rows = document
    elif isinstance(document, dict) and "kind" not in document and isinstance(document.get("items"), list):
        rows = document["items"]
    elif isinstance(document, dict) and "kind" not in document and isinstance(document.get("books"), list):
        rows = document["books"]
    else:
        raise BatchInputError("Use a JSON array, an object with an items or books array, or a batch plan or report.")
    if len(rows) > MAX_ITEMS:
        raise _too_many()
    items = []
    for number, row in enumerate(rows, 1):
        position = f"item {number}"
        if isinstance(row, str):
            items.append(_text_item(row, position))
        elif isinstance(row, dict):
            fields = {}
            for name, value in row.items():
                column = _column(name)
                if column is not None and column not in fields:
                    fields[column] = value
            items.append(_fields_item(fields, position, notes))
        else:
            items.append(BatchItem(position, error=f"{_label(position)} must be text or an object."))
    return items, None


def decode_batch(data: bytes, *, limit: int = MAX_INPUT_BYTES) -> str:
    """Decode UTF-8 (BOM optional) or UTF-16 with a BOM, as Windows PowerShell writes it."""
    if len(data) > limit:
        raise BatchInputError(f"The list is larger than {limit // (1024 * 1024)} MiB.")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError:
            raise BatchInputError("The list is not valid UTF-16 text. Save it as UTF-8.") from None
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            line = data[:error.start].count(b"\n") + 1
            raise BatchInputError(
                f"Line {line} is not valid UTF-8. Save the list as UTF-8 (in Excel: CSV UTF-8).") from None
    return _checked_text(text)


def _checked_text(text: str) -> str:
    text = text.lstrip("\ufeff")
    if "\x00" in text:
        raise BatchInputError("The list contains NUL characters; save it as UTF-8 text.")
    return text


def read_bytes(path, *, limit: int = MAX_INPUT_BYTES, what: str = "list") -> bytes | None:
    """Read a regular file with a size bound; a missing file returns None."""
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            raise BatchInputError(f"The {what} must be a regular file.")
        if info.st_size > limit:
            raise BatchInputError(f"The {what} is larger than {limit // (1024 * 1024)} MiB.")
        with open(path, "rb") as source:
            data = source.read(limit + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise BatchInputError(f"The {what} file could not be read.") from None
    if len(data) > limit:
        raise BatchInputError(f"The {what} is larger than {limit // (1024 * 1024)} MiB.")
    return data


def _notices(notes: set) -> list[str]:
    messages = []
    for note in sorted(notes, key=str):
        if isinstance(note, tuple) and note[0] == "columns":
            messages.append(f"Ignoring CSV columns: {_display_names(list(note[1]))}.")
        elif note == "format":
            messages.append("Ignoring format values other than pdf, epub and djvu (such as paperback).")
    return messages


def parse_batch(source, *, kind: str | None = None, warn=None) -> tuple[BatchItem, ...]:
    """Parse a batch list without network access.

    ``source`` is an ``os.PathLike`` file, a ``str`` of list text, ``bytes`` of an
    encoded list, or a list/tuple of strings (one item each). ``kind`` forces
    ``txt``, ``csv`` or ``json``; otherwise a file suffix decides, then JSON if the
    text starts with ``[`` or ``{``, else TXT. A ``.tsv`` file is read with tabs.
    Text read as TXT that holds JSON is refused unless ``kind`` is ``txt``.
    ``warn(message)`` receives notices such as ignored CSV columns. File names
    recorded in a plan or report are kept as written; planning looks them up in its
    output folder. Raises BatchInputError for structural problems; a bad entry
    becomes an item with ``error`` set.
    """
    return parse_input(source, kind=kind, warn=warn)[0]


def parse_input(source, *, kind: str | None = None, warn=None) -> tuple[tuple[BatchItem, ...], str | None]:
    """parse_batch() that also returns the kind of a plan or report read as JSON, else None."""
    if kind is not None and kind not in INPUT_KINDS:
        raise ValueError("Choose txt, csv or json as the list format.")
    if warn is not None and not callable(warn):
        raise ValueError("warn must be callable or None.")
    if isinstance(source, (list, tuple)):
        if kind is not None or any(not isinstance(value, str) for value in source):
            raise ValueError("Pass a sequence of strings without a list format.")
        if len(source) > MAX_ITEMS:
            raise _too_many()
        return tuple(_text_item(value, f"item {number}") for number, value in enumerate(source, 1)), None
    requested, delimiter = kind, None
    if isinstance(source, os.PathLike):
        data = read_bytes(source)
        if data is None:
            raise BatchInputError("The list file does not exist.")
        text = decode_batch(data)
        suffix = Path(source).suffix.lower()
        kind = kind or _SUFFIXES.get(suffix)
        delimiter = "\t" if suffix == ".tsv" else None
    elif isinstance(source, (bytes, bytearray)):
        text = decode_batch(bytes(source))
    elif isinstance(source, str):
        if len(source) > MAX_INPUT_BYTES:
            raise BatchInputError("The list is larger than 16 MiB.")
        text = _checked_text(source)
    else:
        raise ValueError("Pass a path, text, bytes or a sequence of strings.")
    if kind is None:
        kind = "json" if text.lstrip()[:1] in ("[", "{") else "txt"
    notes, document = set(), None
    if kind == "json":
        items, document = _parse_json(text, notes)
    elif kind == "csv":
        items = _parse_csv(text, notes, delimiter=delimiter)
    else:
        items = _parse_txt(text, detect_csv=requested != "txt")
    if warn is not None:
        for message in _notices(notes):
            warn(message)
    return tuple(items), document
