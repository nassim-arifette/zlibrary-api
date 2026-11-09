"""Validated search filters and pagination shared by client adapters."""

from dataclasses import dataclass
import re

from .client import Book, SUPPORTED_FORMATS
from .errors import FormatChangedError


SEARCH_ORDERS = (
    "bestmatch", "popular", "date", "titleA", "title", "year", "filesize", "filesizeA",
)


def validate_search(query: str, page: int, limit: int) -> str:
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
        raise ValueError("Query must contain 1–500 characters.")
    if type(page) is not int or not 1 <= page <= 1000:
        raise ValueError("Page must be an integer between 1 and 1000.")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Result limit must be an integer between 1 and 50.")
    return query.strip()


def _choices(values, *, formats=False) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValueError("Pass filter values as a sequence, not a single string.")
    try:
        values = tuple(values)
    except TypeError:
        raise ValueError("Filter values must be a sequence of strings.") from None
    if len(values) > 50:
        raise ValueError("Use at most 50 values per filter.")
    normalized = []
    for value in values:
        if not isinstance(value, str) or not value.isprintable() or not 1 <= len(value.strip()) <= 64:
            raise ValueError("Filter values must contain 1–64 printable characters.")
        value = value.strip().lower()
        if formats and value not in SUPPORTED_FORMATS:
            raise ValueError("Supported formats: pdf, epub, djvu.")
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)


@dataclass(frozen=True)
class SearchFilters:
    """An empty filter keeps the upstream defaults and supported download formats."""

    languages: tuple[str, ...] = ()
    formats: tuple[str, ...] = ()
    year_from: int | None = None
    year_to: int | None = None
    order: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "languages", _choices(self.languages))
        object.__setattr__(self, "formats", _choices(self.formats, formats=True))
        for value in (self.year_from, self.year_to):
            if value is not None and (type(value) is not int or not 1 <= value <= 9999):
                raise ValueError("Years must be integers between 1 and 9999.")
        if self.year_from is not None and self.year_to is not None and self.year_from > self.year_to:
            raise ValueError("The starting year must not exceed the ending year.")
        if self.order is not None and self.order not in SEARCH_ORDERS:
            raise ValueError("Unsupported search order.")

    def to_form(self, extension: str | None = None) -> dict:
        if extension is not None and extension not in SUPPORTED_FORMATS:
            raise ValueError("Supported formats: pdf, epub, djvu.")
        if extension is not None and self.formats:
            raise ValueError("Use either extension or filters.formats, not both.")
        form = {"extensions[]": extension if extension else list(self.formats or SUPPORTED_FORMATS)}
        if self.languages:
            form["languages[]"] = list(self.languages)
        if self.year_from is not None:
            form["yearFrom"] = str(self.year_from)
        if self.year_to is not None:
            form["yearTo"] = str(self.year_to)
        if self.order is not None:
            form["order"] = self.order
        return form


@dataclass(frozen=True)
class SearchPage:
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


def parse_page(payload: dict, books: list[Book], page: int, limit: int) -> SearchPage:
    pagination = payload.get("pagination")
    if pagination is None:
        return SearchPage(tuple(books), page, limit)
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
    return SearchPage(tuple(books), current, page_limit, total_items, total_pages, next_page, previous_page)
