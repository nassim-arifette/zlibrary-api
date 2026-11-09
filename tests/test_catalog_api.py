"""Read-only public catalog parsing and cancellation over synthetic local HTTP."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from zlib_connector.api_token import load_api_token
from zlib_connector.booklists import Booklist, BooklistBook
from zlib_connector.books import BookDetails
from zlib_connector.catalog_api import CatalogAPI, CatalogAPIError
from zlib_connector.client import Book
from zlib_connector.errors import NetworkError
from zlib_connector.local_api import JSON_LIMIT
from zlib_connector.pagination import BookPage
from zlib_connector.transfer import CancellationToken, TransferCancelled
from test_cli_jobs import server
from test_http_api import FakeClient, running


BOOK = Book("A title", "An author", "epub", "https://books.example/book/12/abc", book_id="12", book_hash="abc")
DETAILS = BookDetails(**BOOK.to_dict(), description="A description", categories=("Science",))
BOOKLIST = Booklist("5", "Reading", book_count=None, is_public=None)


def page_payload(*, books=None, page=1, limit=20, **pagination):
    value = BookPage(tuple(books if books is not None else [BOOK]), page, limit).to_dict()
    value["pagination"].update(pagination)
    return value


class CatalogClient(FakeClient):
    def get_favorites(self, **kwargs):
        self._call("favorites", **kwargs)
        return BookPage((BOOK,), kwargs["page"], kwargs["limit"], 60, 3, 3, 1)

    def get_history(self, **kwargs):
        self._call("history", **kwargs)
        return BookPage((BOOK,), kwargs["page"], kwargs["limit"])

    def get_booklists(self):
        self._call("booklists")
        return [BOOKLIST]

    def get_booklist_books(self, identifier, **kwargs):
        self._call("booklist_books", identifier, **kwargs)
        return BookPage((BooklistBook(**BOOK.to_dict(), booklist_id=identifier, entry_id="99"),), kwargs["page"], 20)

    def get_book(self, reference):
        self._call("book", reference)
        return DETAILS


@contextmanager
def stalled_server(prefix):
    arrived, release = threading.Event(), threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(4096)
            self.request.sendall(prefix)
            arrived.set()
            release.wait(3)

    instance = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=instance.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{instance.server_address[1]}", arrived
    finally:
        release.set()
        instance.shutdown()
        instance.server_close()
        thread.join(2)


class CatalogAPITests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.token_file = Path(folder.name) / "api-token"
        self.token = load_api_token(self.token_file)

    def fetch(self, value, method="favorites", *args, **kwargs):
        with server(body=json.dumps(value).encode("utf-8")) as (url, _):
            return getattr(CatalogAPI(url, self.token_file), method)(*args, **kwargs)

    def test_all_collection_methods_use_the_real_http_contract_and_existing_models(self):
        client = CatalogClient()
        with running(client) as (service, _):
            # The synthetic real server uses its own fixed bearer.
            with patch("zlib_connector.local_api.read_api_token", return_value=service._token):
                api = CatalogAPI(f"http://127.0.0.1:{service.server_port}/v1", self.token_file)
            cancel = CancellationToken()
            favorites = api.favorites(page=2, limit=20, cancel=cancel)
            self.assertEqual((2, 3, 1), (favorites.page, favorites.next_page, favorites.previous_page))
            self.assertIsInstance(favorites, BookPage)
            self.assertEqual(BOOK, favorites.books[0])
            self.assertIsNone(api.history(cancel=cancel).next_page)
            self.assertEqual((BOOKLIST,), api.booklists(cancel=cancel))
            members = api.booklist_books("5", cancel=cancel)
            self.assertIsInstance(members.books[0], BooklistBook)
            self.assertEqual(("5", "99"), (members.books[0].booklist_id, members.books[0].entry_id))
            self.assertEqual(DETAILS, api.book("12:abc", cancel=cancel))
            with self.assertRaises(FrozenInstanceError):
                favorites.page = 3
        self.assertEqual(["favorites", "history", "booklists", "booklist_books", "book"], [item[0] for item in client.calls])

    def test_transport_pins_loopback_and_preserves_auth_without_proxy_or_redirect(self):
        with server(body=json.dumps(page_payload()).encode()) as (url, calls), patch.dict("os.environ", {"HTTP_PROXY": "http://unreachable.invalid:9"}):
            CatalogAPI(url.replace("127.0.0.1", "localhost"), self.token_file).favorites(page=1, limit=20)
        self.assertEqual(1, len(calls))
        self.assertEqual(("GET", "/v1/favorites?page=1&limit=20"), calls[0][:2])
        self.assertEqual("Bearer " + self.token, calls[0][2]["Authorization"])
        self.assertEqual(b"", calls[0][3])
        with server(status=302, headers=(("Location", "http://example.org/private"),)) as (url, calls):
            with self.assertRaises(CatalogAPIError):
                CatalogAPI(url, self.token_file).booklists()
        self.assertEqual(1, len(calls))

    def test_invalid_arguments_and_cancelled_requests_do_not_open_connections(self):
        api = CatalogAPI("http://localhost:8000", self.token_file)
        with patch("zlib_connector.local_api.http.client.HTTPConnection") as connect:
            for operation in (lambda: api.favorites(page=True), lambda: api.history(limit=51),
                              lambda: api.booklist_books("../5"), lambda: api.booklist_books("5", page=0),
                              lambda: api.book("https://books.example/book/12/abc"), lambda: api.book("12:abc?secret")):
                with self.assertRaises(ValueError):
                    operation()
            cancel = CancellationToken()
            cancel.cancel()
            for operation in (api.favorites, api.history, api.booklists,
                              lambda **kwargs: api.booklist_books("5", **kwargs), lambda **kwargs: api.book("12:abc", **kwargs)):
                with self.assertRaises(TransferCancelled):
                    operation(cancel=cancel)
            connect.assert_not_called()
        missing = self.token_file.with_name("missing")
        for url in ("https://127.0.0.1", "http://example.org", "http://user@localhost", "http://localhost/other"):
            with self.assertRaises(ValueError):
                CatalogAPI(url, missing)
        with self.assertRaises(FileNotFoundError):
            CatalogAPI("http://localhost:8000", missing)
        self.assertFalse(missing.exists())

    def test_unknown_fields_are_dropped_and_display_text_cannot_execute_terminal_escapes(self):
        value = DETAILS.to_dict()
        value.update(title="\x1b[31m[bold]Title[/bold]\x1b[0m\u202e\x00",
                     authors="\x1b]8;;https://private.example\x1b\\Author\x1b]8;;\x1b\\",
                     description="First\r\nSecond\tline\u2066\x07", secret="private-account-data")
        value["categories"] = ["\x1b[32mScience\x1b[0m"]
        details = self.fetch(value, "book", "12:abc")
        self.assertEqual("[bold]Title[/bold]", details.title)
        self.assertEqual("Author", details.authors)
        self.assertEqual("First\nSecond line", details.description)
        self.assertEqual(("Science",), details.categories)
        self.assertNotIn("private-account-data", repr(details))
        self.assertFalse(hasattr(details, "secret"))

    def test_identity_mismatches_and_invalid_membership_are_rejected(self):
        for patch_fields in ({"book_id": "13"}, {"book_hash": "def"}, {"book_id": 12}, {"title": 123}, {"is_favorite": 1}):
            with self.subTest(fields=patch_fields), self.assertRaises(NetworkError):
                self.fetch({**DETAILS.to_dict(), **patch_fields}, "book", "12:abc")
        member = BooklistBook(**BOOK.to_dict(), booklist_id="5", entry_id="99").to_dict()
        for patch_fields in ({"booklist_id": "6"}, {"entry_id": "0"}, {"entry_id": 99}, {"booklist_id": None}):
            payload = page_payload()
            payload["books"] = [{**member, **patch_fields}]
            with self.subTest(fields=patch_fields), self.assertRaises(NetworkError):
                self.fetch(payload, "booklist_books", "5")
        payload["books"] = [member, member]
        with self.assertRaises(NetworkError):
            self.fetch(payload, "booklist_books", "5")
        payload["books"] = [{**member, "entry_id": None}]
        self.assertIsNone(self.fetch(payload, "booklist_books", "5").books[0].entry_id)

    def test_pagination_preserves_unknown_navigation_and_rejects_impossible_directions(self):
        page = self.fetch(page_payload(books=[BOOK] * 20))
        self.assertIsNone(page.next_page)
        self.assertIsNone(page.total_items)
        payload = page_payload()
        payload["pagination"].pop("next_page")
        self.assertIsNone(self.fetch(payload).next_page)
        for fields in ({"page": 2}, {"page": True}, {"limit": 51}, {"limit": "20"}, {"next_page": 1},
                       {"previous_page": 1}, {"next_page": 1001}, {"total_items": 0}, {"total_pages": 0},
                       {"total_pages": 1, "next_page": 2}, {"total_items": 10**13}):
            with self.subTest(fields=fields), self.assertRaises(NetworkError):
                self.fetch(page_payload(**fields))
        self.assertEqual((), self.fetch(page_payload(books=[], total_items=0, total_pages=0)).books)

    def test_lists_require_unique_ids_and_preserve_unknown_metadata(self):
        lists = self.fetch([BOOKLIST.to_dict()], "booklists")
        self.assertIsNone(lists[0].book_count)
        self.assertIsNone(lists[0].is_public)
        for value in ([{**BOOKLIST.to_dict(), "booklist_id": 5}], [BOOKLIST.to_dict()] * 2,
                      [{**BOOKLIST.to_dict(), "book_count": True}], [{**BOOKLIST.to_dict(), "is_public": "true"}],
                      [{**BOOKLIST.to_dict(), "color": 6}], {"booklists": []}):
            with self.subTest(value=value), self.assertRaises(NetworkError):
                self.fetch(value, "booklists")
        self.assertEqual((), self.fetch([], "booklists"))

    def test_rows_text_categories_and_json_are_bounded(self):
        cases = [(page_payload(books=[BOOK] * 21), "favorites", ()),
                 ([{**BOOKLIST.to_dict(), "booklist_id": str(index + 1)} for index in range(1001)], "booklists", ()),
                 ({**DETAILS.to_dict(), "description": "x" * 65537}, "book", ("12:abc",)),
                 ({**DETAILS.to_dict(), "categories": ["science"] * 101}, "book", ("12:abc",)),
                 ({**DETAILS.to_dict(), "categories": [1]}, "book", ("12:abc",))]
        for value, method, args in cases:
            with self.subTest(method=method), self.assertRaises(NetworkError):
                self.fetch(value, method, *args)
        for options in ({"declared": JSON_LIMIT + 1}, {"body": b'{"books": [], "books": []}'},
                        {"body": b'{"private": NaN}'}, {"body": b"{}", "declared": 10},
                        {"headers": (("Content-Length", "2"),)}, {"headers": (("Content-Type", "application/json"),)},
                        {"headers": (("Content-Encoding", "identity"), ("Content-Encoding", "gzip"))}):
            with self.subTest(options=options), server(**options) as (url, _), self.assertRaises(NetworkError):
                CatalogAPI(url, self.token_file).favorites()

    def test_http_failures_are_safe_catalog_messages_and_never_body_text(self):
        for status in (401, 403, 404, 424, 429, 500, 503):
            with self.subTest(status=status), server(status=status, body=b"private-account-data") as (url, calls):
                with self.assertRaises(CatalogAPIError) as caught:
                    CatalogAPI(url, self.token_file).favorites()
                self.assertEqual(status, caught.exception.status)
                self.assertNotIn("private-account-data", str(caught.exception))
                self.assertNotIn("download task", str(caught.exception))
                self.assertEqual(1, len(calls))

    def test_cancellation_interrupts_stalled_headers_and_body_without_retry(self):
        for prefix in (b"HTTP/1.1 200 OK\r\nX-Idle: ",
                       b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n{"):
            with self.subTest(stage="body" if prefix.endswith(b"{") else "headers"), stalled_server(prefix) as (url, arrived), ThreadPoolExecutor(max_workers=1) as pool:
                cancel = CancellationToken()
                api = CatalogAPI(url, self.token_file, timeout=2)
                future = pool.submit(api.favorites, cancel=cancel)
                self.assertTrue(arrived.wait(1))
                cancel.cancel()
                with self.assertRaises(TransferCancelled):
                    future.result(timeout=1.5)

    def test_cancellable_read_keeps_socket_ownership_after_close_headers(self):
        value = {**DETAILS.to_dict(), "description": "a" * 40000}
        with server(body=json.dumps(value).encode()) as (url, calls):
            result = CatalogAPI(url, self.token_file).book("12:abc", cancel=CancellationToken())
        self.assertEqual(value["description"], result.description)
        self.assertEqual(1, len(calls))

    def test_cancellable_read_preserves_absolute_deadline_for_headers_and_body(self):
        for prefix in (b"HTTP/1.1 200 OK\r\nX-Idle: ",
                       b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n{"):
            with stalled_server(prefix) as (url, _):
                with self.assertRaises(NetworkError) as caught:
                    CatalogAPI(url, self.token_file, timeout=0.1).favorites(cancel=CancellationToken())
                self.assertEqual("timeout", caught.exception.reason)

    def test_catalog_import_does_not_require_textual(self):
        result = subprocess.run([sys.executable, "-c", "import sys; import zlib_connector.catalog_api; assert 'textual' not in sys.modules"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
