"""Read-only catalog access through an existing authenticated loopback server."""

import re

from . import catalog_schema
from .booklists import Booklist, booklist_id
from .books import BookDetails, BookReference
from .errors import ConnectorError
from .local_api import LocalAPI, LocalAPIError
from .pagination import BookPage, validate_page
from .transfer import CancellationToken


_STATUS_MESSAGES = {
    400: "The local API rejected the collection request. Check its arguments.",
    401: "The local API rejected the bearer token. Use the token file from zlib-cli serve.",
    403: "The local API refused this client.",
    404: "The requested book or collection was not found.",
    424: "The server's Z-Library session is missing or expired. Restart zlib-cli serve with a signed-in account.",
    429: "Z-Library reported a request limit. Refresh the collection later.",
    503: "The local API is busy or unavailable. Refresh when it is ready.",
}


class CatalogAPIError(LocalAPIError):
    """Safe HTTP status information without upstream response text."""

    def __init__(self, status):
        self.status = status
        ConnectorError.__init__(self, _STATUS_MESSAGES.get(status, "The local API could not load the requested catalog data."))


class _CatalogHTTP(LocalAPI):
    @staticmethod
    def _validate_operation(method, path):
        if method != "GET" or re.fullmatch(r"/(?:favorites|history|booklists(?:/[1-9][0-9]{0,19}/books)?|books/[0-9]{1,20}/[A-Za-z0-9]{1,128})", path) is None:
            raise ValueError("Unsupported local catalog operation.")

    @staticmethod
    def _status_error(status):
        return CatalogAPIError(status)


class CatalogAPI:
    """Five collection reads, using the same bounded transport as task commands.

    The caller can cancel a local read. An upstream read already accepted by the
    server may continue; cancellation does not change the account or its tasks.
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
        if not isinstance(reference, str) or re.fullmatch(r"[0-9]{1,20}:[A-Za-z0-9]{1,128}", reference) is None:
            raise ValueError("Use a complete book reference: ID:HASH.")
        reference = BookReference(*reference.split(":"))
        value = self._http._request_json("GET", f"/books/{reference.book_id}/{reference.book_hash}", cancel=cancel)
        result = catalog_schema.book_details(value, reference=reference)
        if cancel is not None:
            cancel.check()
        return result
