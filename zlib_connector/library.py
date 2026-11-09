"""Account collections, kept distinct from local files and download queues."""

from dataclasses import asdict, dataclass
import re

from .client import Book, _clean_text
from .books import parse_book
from .errors import FormatChangedError
from .pagination import BookPage


FAVORITE_ORDERS = ("saved_date", "saved_dateA", "titleA", "title", "year", "filesize", "filesizeA")
HISTORY_ORDERS = ("downloaded_date", "downloaded_dateA", "titleA", "title", "year", "filesize", "filesizeA")


def _positive_id(value: str | int, label: str) -> str:
    if type(value) not in (str, int) or not re.fullmatch(r"[1-9][0-9]{0,19}", str(value)):
        raise ValueError(f"{label} must be a positive integer of at most 20 digits.")
    return str(value)


def booklist_id(value: str | int) -> str:
    return _positive_id(value, "Booklist ID")


def booklist_entry_id(value: str | int) -> str:
    return _positive_id(value, "Booklist entry ID")


@dataclass(frozen=True)
class BooklistBook(Book):
    booklist_id: str = ""
    entry_id: str | None = None


@dataclass(frozen=True)
class BooklistBookUpdate:
    booklist_id: str
    action: str
    book_id: str | None = None
    entry_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def parse_booklist_books(rows, domain: str, identifier: str) -> list[BooklistBook]:
    if not isinstance(rows, list):
        raise FormatChangedError("The API response has no collection books list.")
    books = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("book"), dict):
            raise FormatChangedError("The API returned an unavailable or invalid booklist entry.")
        entry_id = row.get("readlist_books_id")
        if entry_id is not None:
            try:
                entry_id = booklist_entry_id(entry_id)
            except ValueError:
                raise FormatChangedError("The API returned an invalid booklist entry ID.") from None
        record = row["book"]
        if "_isUserSavedBook" not in record and "_isUserSavedBook" in row:
            record = {**record, "_isUserSavedBook": row["_isUserSavedBook"]}
        book = parse_book(record, domain)
        books.append(BooklistBook(**book.to_dict(), booklist_id=identifier, entry_id=entry_id))
    return books


@dataclass(frozen=True)
class Booklist:
    booklist_id: str
    title: str
    description: str = ""
    book_count: int | None = None
    is_public: bool | None = None
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LibraryOverview:
    favorites: BookPage
    history: BookPage
    booklists: tuple[Booklist, ...]

    def to_dict(self) -> dict:
        return {"favorites": self.favorites.to_dict(), "history": self.history.to_dict(),
                "booklists": [item.to_dict() for item in self.booklists]}


@dataclass(frozen=True)
class FavoriteUpdate:
    book_id: str
    is_favorite: bool

    def to_dict(self) -> dict:
        return asdict(self)


def parse_booklist(row: dict) -> Booklist:
    if not isinstance(row, dict):
        raise FormatChangedError("The API returned an invalid booklist.")
    try:
        identifier = booklist_id(row.get("id"))
    except ValueError:
        raise FormatChangedError("The API returned an invalid booklist reference.") from None
    count = row.get("books_count")
    if isinstance(count, str) and re.fullmatch(r"[0-9]{1,12}", count):
        count = int(count)
    if count is not None and (type(count) is not int or not 0 <= count <= 10**12):
        raise FormatChangedError("The API returned an invalid booklist count.")
    public = row.get("public")
    if type(public) in (bool, int, str) and public in (True, 1, "1"):
        public = True
    elif type(public) in (bool, int, str) and public in (False, 0, "0"):
        public = False
    elif public is not None:
        raise FormatChangedError("The API returned an invalid booklist visibility.")

    def text(key):
        value = row.get(key)
        return _clean_text(value) if isinstance(value, str) else ""

    return Booklist(identifier, text("title") or "Untitled booklist", text("description"),
                    count, public, text("date_updated"))
