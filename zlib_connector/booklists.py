"""Booklist records, editable settings and protocol validation."""
from dataclasses import asdict, dataclass
import re
import unicodedata

from .client import Book, _clean_text
from .books import parse_book
from .errors import FormatChangedError


def _positive_id(value: str | int, label: str) -> str:
    if type(value) not in (str, int) or not re.fullmatch(r"[1-9][0-9]{0,19}", str(value)):
        raise ValueError(f"{label} must be a positive integer of at most 20 digits.")
    return str(value)


def booklist_id(value: str | int) -> str:
    return _positive_id(value, "Booklist ID")


def booklist_entry_id(value: str | int) -> str:
    return _positive_id(value, "Booklist entry ID")


def _validate_fields(fields: dict) -> None:
    for name, value in fields.items():
        if name in ("title", "description"):
            maximum = 120 if name == "title" else 1000
            if not isinstance(value, str) or len(value) > maximum or (name == "title" and not value.strip()):
                raise ValueError(f"Booklist {name} must be text of at most {maximum} characters; a title cannot be empty.")
            allowed = "\n\r\t" if name == "description" else ""
            if any(unicodedata.category(char).startswith("C") and char not in allowed for char in value):
                raise ValueError(f"Booklist {name} contains unsupported control characters.")
        elif name == "is_public" and type(value) is not bool:
            raise ValueError("Booklist visibility must be a boolean.")
        elif name == "color" and (type(value) is not int or not 0 <= value <= 5):
            raise ValueError("Booklist color must be an integer between 0 and 5.")


def validate_booklist_changes(*, title=None, description=None, is_public=None, color=None) -> dict:
    changes = {key: value for key, value in
               (("title", title), ("description", description), ("is_public", is_public), ("color", color))
               if value is not None}
    if not changes:
        raise ValueError("Choose at least one booklist setting to change.")
    _validate_fields(changes)
    return changes


def _visibility(value) -> bool | None:
    if type(value) in (bool, int, str) and value in (True, 1, "1"):
        return True
    if type(value) in (bool, int, str) and value in (False, 0, "0"):
        return False
    if value is not None:
        raise FormatChangedError("The API returned an invalid booklist visibility.")
    return None


def _color(value) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"[0-5]", value):
        value = int(value)
    if value is not None and (type(value) is not int or not 0 <= value <= 5):
        raise FormatChangedError("The API returned an invalid booklist color.")
    return value


@dataclass(frozen=True)
class BooklistSettings:
    title: str
    description: str = ""
    is_public: bool = False
    color: int = 0

    def __post_init__(self):
        _validate_fields(asdict(self))

    @classmethod
    def from_record(cls, row: dict, changes: dict):
        try:
            return cls(
                title=changes.get("title", row.get("title")),
                description=changes.get("description", row.get("description")),
                is_public=changes["is_public"] if "is_public" in changes else _visibility(row.get("public")),
                color=changes["color"] if "color" in changes else _color(row.get("color")),
            )
        except ValueError:
            raise FormatChangedError("The API returned incomplete or invalid booklist settings.") from None

    def to_payload(self) -> dict:
        payload = {"title": self.title, "description": self.description, "color": str(self.color)}
        if not self.is_public:
            payload["private"] = "on"
        return payload

    def matches(self, result: "Booklist") -> bool:
        return (result.title == _clean_text(self.title)
                and result.description == _clean_text(self.description)
                and result.is_public == self.is_public and result.color == self.color)


@dataclass(frozen=True)
class BooklistDeletion:
    booklist_id: str

    def to_dict(self) -> dict:
        return {"booklist_id": self.booklist_id, "deleted": True}


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
    color: int | None = None

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
    public = _visibility(row.get("public"))

    def text(key):
        value = row.get(key)
        return _clean_text(value) if isinstance(value, str) else ""

    return Booklist(identifier, text("title") or "Untitled booklist", text("description"),
                    count, public, text("date_updated"), _color(row.get("color")))
