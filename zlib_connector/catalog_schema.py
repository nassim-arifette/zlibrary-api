"""Parse the local API's public catalog records into existing immutable models."""

from .booklists import Booklist, BooklistBook, booklist_id, booklist_entry_id
from .books import BookDetails, BookReference
from .client import Book
from .errors import NetworkError
from .pagination import BookPage
from .terminal_text import terminal_text


MAX_ROWS = 1000


def invalid_catalog():
    raise NetworkError("The local API returned invalid catalog data.") from None


def _record(value):
    if not isinstance(value, dict):
        invalid_catalog()
    return value


def _text(value, *, maximum=512, required=False, multiline=False):
    if not isinstance(value, str) or len(value) > maximum:
        invalid_catalog()
    value = terminal_text(value, multiline=multiline)
    if required and not value:
        invalid_catalog()
    return value


def _number(value, *, minimum=0, maximum=10**12, nullable=True):
    if value is None and nullable:
        return None
    if type(value) is not int or not minimum <= value <= maximum:
        invalid_catalog()
    return value


def _boolean(value):
    if value is not None and type(value) is not bool:
        invalid_catalog()
    return value


def _identity(value, validator):
    if not isinstance(value, str):
        invalid_catalog()
    try:
        return validator(value)
    except ValueError:
        invalid_catalog()


def _book_fields(value):
    value = _record(value)
    try:
        reference = BookReference(value.get("book_id"), value.get("book_hash"))
    except ValueError:
        invalid_catalog()
    fields = {name: _text(value.get(name, ""), maximum=maximum, required=name == "title")
              for name, maximum in (("title", 4096), ("authors", 4096), ("extension", 32), ("url", 2048),
                                     ("year", 64), ("language", 256), ("size", 256))}
    return {**fields, "book_id": reference.book_id, "book_hash": reference.book_hash,
            "is_favorite": _boolean(value.get("is_favorite"))}


def book_page(value, *, page, limit=None, identifier=None):
    value = _record(value)
    rows, pagination = value.get("books"), _record(value.get("pagination"))
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        invalid_catalog()
    current = _number(pagination.get("page"), minimum=1, maximum=1000, nullable=False)
    page_limit = _number(pagination.get("limit"), minimum=1, maximum=limit or MAX_ROWS, nullable=False)
    if current != page or len(rows) > page_limit:
        invalid_catalog()
    total_items = _number(pagination.get("total_items"))
    total_pages = _number(pagination.get("total_pages"))
    next_page = _number(pagination.get("next_page"), minimum=1, maximum=1000)
    previous_page = _number(pagination.get("previous_page"), minimum=1, maximum=1000)
    if (total_items is not None and total_items < len(rows)
            or total_pages == 0 and (rows or next_page is not None or previous_page is not None)
            or total_pages is not None and total_pages > 0 and current > total_pages
            or next_page is not None and (next_page <= current or total_pages is not None and next_page > total_pages)
            or previous_page is not None and previous_page >= current):
        invalid_catalog()
    books, entry_ids = [], set()
    for row in rows:
        fields = _book_fields(row)
        if identifier is None:
            books.append(Book(**fields))
            continue
        returned_id = _identity(row.get("booklist_id"), booklist_id)
        entry_id = row.get("entry_id")
        if entry_id is not None:
            entry_id = _identity(entry_id, booklist_entry_id)
            if entry_id in entry_ids:
                invalid_catalog()
            entry_ids.add(entry_id)
        if returned_id != identifier:
            invalid_catalog()
        books.append(BooklistBook(**fields, booklist_id=returned_id, entry_id=entry_id))
    return BookPage(tuple(books), current, page_limit, total_items, total_pages, next_page, previous_page)


def booklists(value):
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        invalid_catalog()
    result, identifiers = [], set()
    for row in value:
        row = _record(row)
        identifier = _identity(row.get("booklist_id"), booklist_id)
        if identifier in identifiers:
            invalid_catalog()
        identifiers.add(identifier)
        result.append(Booklist(identifier, _text(row.get("title", ""), maximum=4096, required=True),
            _text(row.get("description", ""), maximum=65536, multiline=True),
            _number(row.get("book_count")), _boolean(row.get("is_public")),
            _text(row.get("updated_at", "")), _number(row.get("color"), maximum=5)))
    return tuple(result)


def book_details(value, *, reference):
    fields = _book_fields(value)
    if (fields["book_id"], fields["book_hash"]) != (reference.book_id, reference.book_hash):
        invalid_catalog()
    details = {name: _text(value.get(name, ""), maximum=maximum, multiline=name == "description")
               for name, maximum in (("description", 65536), ("cover_url", 2048), ("identifier", 2048),
                                     ("isbn10", 256), ("isbn13", 256), ("publisher", 4096), ("edition", 512), ("pages", 512))}
    categories = value.get("categories", [])
    if not isinstance(categories, list) or len(categories) > 100:
        invalid_catalog()
    return BookDetails(**fields, **details, categories=tuple(_text(item, maximum=256) for item in categories))
