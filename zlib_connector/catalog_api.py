"""Read-only catalog access through an existing authenticated loopback server."""

import re
import unicodedata
from urllib.parse import urlencode

from . import catalog_schema
from .booklists import Booklist, booklist_id
from .books import BookDetails, BookReference
from .client import Book
from .errors import ConnectorError
from .local_api import MAX_QUERY_LENGTH, LocalAPI, LocalAPIError
from .pagination import BookPage, validate_page
from .search import SearchFilters, validate_search
from .transfer import CancellationToken


_STATUS_MESSAGES = {
    400: "The local API rejected this catalog request. Check its arguments.",
    401: "The local API rejected the bearer token. Use the token file from zlib-cli serve.",
    403: "The local API refused this client.",
    404: "The requested book or collection was not found.",
    424: "The server's Z-Library session is missing or expired. Restart zlib-cli serve with a signed-in account.",
    429: "Z-Library reported a request limit. Try again later.",
    502: "Z-Library could not complete the request. Try again later.",
    503: "The local API is busy or unavailable. Refresh when it is ready.",
    504: "Z-Library did not answer in time. Try again later.",
}


def search_parameters(query, *, page=1, limit=20, filters: SearchFilters | None = None) -> tuple[tuple[str, str], ...]:
    """Validate a search as the local API does and return its ordered query pairs.

    Failures raise ValueError with a presentable message before any connection.
    """
    query = validate_search(query, page, limit)
    if any(unicodedata.category(character).startswith("C") for character in query):
        raise ValueError("Search text cannot contain control or formatting characters.")
    if filters is None:
        filters = SearchFilters()
    elif not isinstance(filters, SearchFilters):
        raise ValueError("Pass search filters as SearchFilters.")
    pairs = [("q", query), ("page", str(page)), ("limit", str(limit))]
    pairs += [("language", value) for value in filters.languages]
    pairs += [("format", value) for value in filters.formats]
    for name, value in (("year_from", filters.year_from), ("year_to", filters.year_to), ("order", filters.order)):
        if value is not None:
            pairs.append((name, str(value)))
    if len(urlencode(pairs)) > MAX_QUERY_LENGTH:
        raise ValueError("Shorten the search text or remove some languages.")
    return tuple(pairs)


def _reference(value) -> BookReference:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,20}:[A-Za-z0-9]{1,128}", value) is None:
        raise ValueError("Use a complete book reference: ID:HASH.")
    return BookReference(*value.split(":"))


class CatalogAPIError(LocalAPIError):
    """Safe HTTP status information without upstream response text."""

    def __init__(self, status):
        self.status = status
        ConnectorError.__init__(self, _STATUS_MESSAGES.get(status, "The local API could not load the requested catalog data."))


class _CatalogHTTP(LocalAPI):
    @staticmethod
    def _validate_operation(method, path):
        if method != "GET" or re.fullmatch(r"/(?:favorites|history|search|discovery/(?:popular|recommended)"
                                           r"|booklists(?:/[1-9][0-9]{0,19}/books)?"
                                           r"|books/[0-9]{1,20}/[A-Za-z0-9]{1,128}(?:/similar)?)", path) is None:
            raise ValueError("Unsupported local catalog operation.")

    @staticmethod
    def _status_error(status):
        return CatalogAPIError(status)


class CatalogAPI:
    """Read-only collection, search and discovery requests, using the same bounded transport as task commands.

    Invalid arguments raise ValueError before connecting. The caller can cancel
    a local read. An upstream read already accepted by the server may continue;
    cancellation does not change the account or its tasks.
    """

    def __init__(self, url, token_file, *, timeout=30):
        self._http = _CatalogHTTP(url, token_file, timeout=timeout)

    def favorites(self, *, page=1, limit=20, cancel: CancellationToken | None = None) -> BookPage:
        return self._page("/favorites", page=page, limit=limit, cancel=cancel)

    def history(self, *, page=1, limit=20, cancel: CancellationToken | None = None) -> BookPage:
        return self._page("/history", page=page, limit=limit, cancel=cancel)

    def _page(self, path, *, page, limit, cancel):
        validate_page(page, limit)
        value = self._http._request_json("GET", path, query={"page": page, "limit": limit}, cancel=cancel)
        result = catalog_schema.book_page(value, page=page, limit=limit)
        if cancel is not None:
            cancel.check()
        return result

    def booklists(self, *, cancel: CancellationToken | None = None) -> tuple[Booklist, ...]:
        result = catalog_schema.booklists(self._http._request_json("GET", "/booklists", cancel=cancel))
        if cancel is not None:
            cancel.check()
        return result

    def booklist_books(self, identifier, *, page=1, cancel: CancellationToken | None = None) -> BookPage:
        identifier = booklist_id(identifier)
        validate_page(page, 20)
        value = self._http._request_json("GET", f"/booklists/{identifier}/books", query={"page": page}, cancel=cancel)
        result = catalog_schema.book_page(value, page=page, identifier=identifier)
        if cancel is not None:
            cancel.check()
        return result

    def book(self, reference, *, cancel: CancellationToken | None = None) -> BookDetails:
        reference = _reference(reference)
        value = self._http._request_json("GET", f"/books/{reference.book_id}/{reference.book_hash}", cancel=cancel)
        result = catalog_schema.book_details(value, reference=reference)
        if cancel is not None:
            cancel.check()
        return result

    def search(self, query, *, page=1, limit=20, filters: SearchFilters | None = None,
               cancel: CancellationToken | None = None) -> BookPage:
        """Read one search page; the server applies the filters and reports navigation."""
        pairs = search_parameters(query, page=page, limit=limit, filters=filters)
        value = self._http._request_json("GET", "/search", query=pairs, cancel=cancel)
        result = catalog_schema.book_page(value, page=page, limit=limit)
        if cancel is not None:
            cancel.check()
        return result

    def popular(self, *, cancel: CancellationToken | None = None) -> tuple[Book, ...]:
        return self._books("/discovery/popular", cancel=cancel)

    def recommended(self, *, cancel: CancellationToken | None = None) -> tuple[Book, ...]:
        """Account recommendations; a server without a signed-in session answers 424."""
        return self._books("/discovery/recommended", cancel=cancel)

    def similar(self, reference, *, cancel: CancellationToken | None = None) -> tuple[Book, ...]:
        reference = _reference(reference)
        return self._books(f"/books/{reference.book_id}/{reference.book_hash}/similar", cancel=cancel)

    def _books(self, path, *, cancel):
        result = catalog_schema.books(self._http._request_json("GET", path, cancel=cancel))
        if cancel is not None:
            cancel.check()
        return result
