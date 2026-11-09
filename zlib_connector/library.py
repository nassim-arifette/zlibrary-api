"""Account collections, kept distinct from local files and download queues."""

from dataclasses import asdict, dataclass
import re

from .client import _clean_text
from .errors import FormatChangedError
from .pagination import BookPage


FAVORITE_ORDERS = ("saved_date", "saved_dateA", "titleA", "title", "year", "filesize", "filesizeA")
HISTORY_ORDERS = ("downloaded_date", "downloaded_dateA", "titleA", "title", "year", "filesize", "filesizeA")


def booklist_id(value: str | int) -> str:
    if type(value) not in (str, int) or not re.fullmatch(r"[1-9][0-9]{0,19}", str(value)):
        raise ValueError("Booklist ID must be a positive integer of at most 20 digits.")
    return str(value)


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
