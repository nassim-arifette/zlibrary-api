"""Validated search filters and pagination shared by client adapters."""

from dataclasses import dataclass

from .client import SUPPORTED_FORMATS
from .pagination import BookPage as SearchPage, parse_page, validate_page


SEARCH_ORDERS = (
    "bestmatch", "popular", "date", "titleA", "title", "year", "filesize", "filesizeA",
)


def validate_search(query: str, page: int, limit: int) -> str:
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
        raise ValueError("Query must contain 1 to 500 characters.")
    validate_page(page, limit)
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
            raise ValueError("Filter values must contain 1 to 64 printable characters.")
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
