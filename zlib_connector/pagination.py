"""Book pagination shared by searches and account collections."""

from dataclasses import dataclass
import re

from .client import Book
from .errors import FormatChangedError


def validate_page(page: int, limit: int) -> None:
    if type(page) is not int or not 1 <= page <= 1000:
        raise ValueError("Page must be an integer between 1 and 1000.")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Result limit must be an integer between 1 and 50.")


@dataclass(frozen=True)
class BookPage:
    books: tuple[Book, ...]
    page: int
    limit: int
    total_items: int | None = None
    total_pages: int | None = None
    next_page: int | None = None
    previous_page: int | None = None

    def to_dict(self) -> dict:
        return {
            "books": [book.to_dict() for book in self.books],
            "pagination": {
                "page": self.page, "limit": self.limit,
                "total_items": self.total_items, "total_pages": self.total_pages,
                "next_page": self.next_page, "previous_page": self.previous_page,
            },
        }


def parse_page(payload: dict, books: list[Book], page: int, limit: int) -> BookPage:
    pagination = payload.get("pagination")
    if pagination is None:
        return BookPage(tuple(books), page, limit)
    if not isinstance(pagination, dict):
        raise FormatChangedError("The API returned invalid pagination.")

    def number(key, *, minimum=0):
        value = pagination.get(key)
        # The live API uses false, as well as null/0, for absent navigation.
        if key in ("next", "before") and value is False:
            return None
        if value is None:
            return None
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value):
            value = int(value)
        if type(value) is not int or not minimum <= value <= 10**12:
            raise FormatChangedError("The API returned invalid pagination.")
        return value

    current = number("current", minimum=1)
    current = page if current is None else current
    page_limit = number("limit", minimum=1)
    page_limit = limit if page_limit is None else page_limit
    total_items, total_pages = number("total_items"), number("total_pages")
    next_page, previous_page = number("next") or None, number("before") or None
    if "next" not in pagination and total_pages is not None and current < total_pages:
        next_page = current + 1
    if "before" not in pagination and current > 1:
        previous_page = current - 1
    if (next_page is not None and next_page <= current) or (
        previous_page is not None and previous_page >= current
    ):
        raise FormatChangedError("The API returned non-progressing pagination.")
    return BookPage(tuple(books), current, page_limit, total_items, total_pages, next_page, previous_page)
