"""A synthetic batch client: scripted searches, details, quota and downloads only."""
from pathlib import Path

from zlib_connector import Book, BookDetails
from zlib_connector.account import DownloadQuota
from zlib_connector.search import SearchPage
from zlib_connector.transfer import TransferCancelled


ORIGIN = "https://books.example/"
SECRET = "synthetic-private-token"


def book(number, extension="epub", *, title=None, authors="Author"):
    return Book(title or f"Book {number}", authors, extension, f"{ORIGIN}book/{number}/h{number}", "2001",
                "english", "1 MB", str(number), f"h{number}")


def details(number, extension="epub", *, title=None):
    return BookDetails(**book(number, extension, title=title).to_dict())


class BatchClient:
    """Records every call; failures are queued per method or per reference."""

    domain = ORIGIN

    def __init__(self, results=None, *, remaining=10, limit=10):
        self.results = {key.casefold(): list(value) for key, value in (results or {}).items()}
        self.remaining, self.limit = remaining, limit
        self.calls = []
        self.search_failures, self.book_failures, self.quota_failures = [], {}, []
        self.download_failures = {}
        self.extensions = {}
        self.during_download = None
        self.logged_in = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.logged_in = False

    def login(self, email, password, **_):
        self.logged_in = True

    def restore_session(self, store):
        self.logged_in = True
        return {"authenticated": True}

    def search_page(self, query, *, filters=None, page=1, limit=20):
        self.calls.append(("search", query, filters, limit))
        if self.search_failures:
            failure = self.search_failures.pop(0)
            if failure is not None:
                raise failure
        return SearchPage(tuple(self.results.get(query.casefold(), ())[:limit]), page, limit)

    def search(self, query, *, filters=None, page=1, limit=20):
        return list(self.search_page(query, filters=filters, page=page, limit=limit).books)

    def get_book(self, reference):
        self.calls.append(("book", reference))
        failure = self.book_failures.pop(reference, None)
        if failure is not None:
            raise failure
        number = int(reference.split(":")[0])
        return details(number, self.extensions.get(reference, "epub"))

    def get_quota(self):
        self.calls.append(("quota",))
        if self.quota_failures:
            failure = self.quota_failures.pop(0)
            if failure is not None:
                raise failure
        if self.remaining is None:
            return DownloadQuota(None, None, None)
        return DownloadQuota(self.limit, self.limit - self.remaining, self.remaining)

    def download(self, value, destination, *, progress=None, cancel=None):
        reference = f"{value.book_id}:{value.book_hash}"
        self.calls.append(("download", reference))
        if self.during_download is not None:
            self.during_download(reference)
        if cancel is not None:
            try:
                cancel.check()
            except TransferCancelled as error:
                error.download_stage = "transfer"  # As the real client marks it once a link was issued.
                raise
        failure = self.download_failures.pop(reference, None)
        if failure is not None:
            raise failure
        downloads = sum(call[0] == "download" for call in self.calls)
        path = Path(destination).resolve() / f"{value.title}-{downloads:012x}.{value.extension}"
        path.write_bytes(b"synthetic book")
        if self.remaining is not None:
            self.remaining -= 1
        return path

    def kinds(self, kind):
        return [call[1] for call in self.calls if call[0] == kind]


def staged(error, stage):
    error.download_stage = stage
    return error
