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
from urllib.parse import urlencode

from zlib_connector import catalog_schema
from zlib_connector.api_token import load_api_token
from zlib_connector.booklists import Booklist, BooklistBook
from zlib_connector.books import BookDetails
from zlib_connector.catalog_api import CatalogAPI, CatalogAPIError, search_parameters
from zlib_connector.client import Book
from zlib_connector.errors import AuthenticationError, NetworkError, RateLimitError
from zlib_connector.local_api import JSON_LIMIT, MAX_QUERY_LENGTH, LocalAPI
from zlib_connector.pagination import BookPage
from zlib_connector.search import SearchFilters
from zlib_connector.transfer import CancellationToken, TransferCancelled
from test_cli_jobs import server
from test_http_api import FakeClient, running


BOOK = Book("A title", "An author", "epub", "https://books.example/book/12/abc", book_id="12", book_hash="abc")
SPARSE = Book("Sparse", "", "", "", book_id="13", book_hash="def")
DETAILS = BookDetails(**BOOK.to_dict(), description="A description", categories=("Science",))
BOOKLIST = Booklist("5", "Reading", book_count=None, is_public=None)
STALLED = (b"HTTP/1.1 200 OK\r\nX-Idle: ",
           b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n{")


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

    def search_page(self, query, **kwargs):
        self._call("search", query, **kwargs)
        return BookPage((BOOK, SPARSE), kwargs["page"], kwargs["limit"], 41, 3, 3, 1)

    def get_popular(self):
        self._call("popular")
        return [BOOK, SPARSE, BOOK]

    def get_recommendations(self):
        self._call("recommendations")
        return [SPARSE]

    def get_similar(self, reference):
        self._call("similar", reference)
        return [SPARSE, BOOK]


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
                       {"previous_page": 1}, {"total_items": 0}, {"total_pages": 0},
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
        reads = (("favorites", ()), ("search", ("history",)), ("popular", ()), ("recommended", ()), ("similar", ("12:abc",)))
        for status in (400, 401, 403, 404, 424, 429, 500, 502, 503, 504):
            with self.subTest(status=status), server(status=status, body=b"private-account-data") as (url, calls):
                api = CatalogAPI(url, self.token_file)
                for method, args in reads:
                    with self.assertRaises(CatalogAPIError) as caught:
                        getattr(api, method)(*args)
                    self.assertEqual(status, caught.exception.status)
                    self.assertNotIn("private-account-data", str(caught.exception))
                    self.assertNotIn("download task", str(caught.exception))
                    if status != 404:
                        self.assertNotIn("collection", str(caught.exception))
                # Exactly one request per read: failures are never retried.
                self.assertEqual(len(reads), len(calls))
        self.assertEqual("Z-Library could not complete the request. Try again later.", str(CatalogAPIError(502)))
        self.assertEqual("Z-Library did not answer in time. Try again later.", str(CatalogAPIError(504)))

    def test_cancellation_interrupts_stalled_headers_and_body_without_retry(self):
        reads = {"favorites": lambda api, cancel: api.favorites(cancel=cancel),
                 "search": lambda api, cancel: api.search("history", filters=SearchFilters(formats=("epub",)), cancel=cancel),
                 "popular": lambda api, cancel: api.popular(cancel=cancel),
                 "recommended": lambda api, cancel: api.recommended(cancel=cancel),
                 "similar": lambda api, cancel: api.similar("12:abc", cancel=cancel)}
        for prefix in STALLED:
            for name, read in reads.items():
                with self.subTest(stage="body" if prefix.endswith(b"{") else "headers", read=name):
                    with stalled_server(prefix) as (url, arrived), ThreadPoolExecutor(max_workers=1) as pool:
                        cancel = CancellationToken()
                        future = pool.submit(read, CatalogAPI(url, self.token_file, timeout=2), cancel)
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
        for prefix in STALLED:
            with stalled_server(prefix) as (url, _):
                with self.assertRaises(NetworkError) as caught:
                    CatalogAPI(url, self.token_file, timeout=0.1).favorites(cancel=CancellationToken())
                self.assertEqual("timeout", caught.exception.reason)

    def test_search_parameters_follow_the_http_contract_in_a_stable_order(self):
        filters = SearchFilters(languages=(" French ", "english", "french"), formats=("epub", "pdf"),
                                year_from=2000, year_to=2010, order="year")
        self.assertEqual((("q", "history"), ("page", "2"), ("limit", "7"), ("language", "french"),
                          ("language", "english"), ("format", "epub"), ("format", "pdf"),
                          ("year_from", "2000"), ("year_to", "2010"), ("order", "year")),
                         search_parameters("  history ", page=2, limit=7, filters=filters))
        self.assertEqual((("q", "[bold] ISBN 978"), ("page", "1"), ("limit", "20")),
                         search_parameters("[bold] ISBN 978"))
        self.assertEqual((("q", "a b"), ("page", "1"), ("limit", "20")), search_parameters("a b"))

    def test_search_parameters_reject_what_the_local_api_would_refuse(self):
        cases = ("", "   ", "x" * 501, "tab\tinside", "escape\x1b[31m", "joiner\u200dtext", "bidi\u202etext",
                 "nul\x00")
        for query in cases:
            with self.subTest(query=query), self.assertRaises(ValueError):
                search_parameters(query)
        for options in ({"page": 0}, {"page": 1001}, {"limit": 0}, {"limit": 51}, {"page": True},
                        {"filters": {"languages": ("french",)}}, {"filters": ("french",)}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                search_parameters("history", **options)
        crowded = SearchFilters(languages=tuple(f"{index:02d}" + "é" * 62 for index in range(50)))
        with self.assertRaises(ValueError) as caught:
            search_parameters("x" * 500, filters=crowded)
        self.assertIn("Shorten", str(caught.exception))
        self.assertLessEqual(len(urlencode(search_parameters("é" * 500))), MAX_QUERY_LENGTH)

    def test_oversized_query_strings_fail_before_connecting(self):
        api = LocalAPI("http://localhost:8000", self.token_file)
        with patch("zlib_connector.local_api.http.client.HTTPConnection") as connect:
            with self.assertRaises(ValueError):
                api._request_json("GET", "/downloads", query=(("q", "x" * MAX_QUERY_LENGTH),))
            connect.assert_not_called()

    def test_search_and_discovery_use_the_real_http_contract_and_plain_books(self):
        client = CatalogClient()
        filters = SearchFilters(languages=(" French ", "english"), formats=("epub", "djvu"), year_from=2000, year_to=2010, order="year")
        with running(client) as (service, _):
            # The synthetic real server uses its own fixed bearer.
            with patch("zlib_connector.local_api.read_api_token", return_value=service._token):
                api = CatalogAPI(f"http://127.0.0.1:{service.server_port}/v1", self.token_file)
            cancel = CancellationToken()
            found = api.search("  history ", page=2, limit=15, filters=filters, cancel=cancel)
            self.assertEqual(BookPage((BOOK, SPARSE), 2, 15, 41, 3, 3, 1), found)
            self.assertEqual(BookPage((BOOK, SPARSE), 2, 20, 41, 3, 3, 1), api.search("[bold]history", page=2))
            popular = api.popular(cancel=cancel)
            self.assertEqual((BOOK, SPARSE, BOOK), popular)
            self.assertEqual((SPARSE,), api.recommended(cancel=cancel))
            self.assertEqual((SPARSE, BOOK), api.similar("12:abc", cancel=cancel))
            self.assertTrue(all(type(book) is Book for book in found.books + popular))
            for error, status in ((AuthenticationError("private-account-data"), 424), (RateLimitError("private-account-data"), 429),
                                  (NetworkError("private-account-data"), 502),
                                  (NetworkError("private-account-data", reason="timeout"), 504)):
                client.error = error
                with self.subTest(status=status):
                    with self.assertRaises(CatalogAPIError) as caught:
                        api.recommended()
                    self.assertEqual(status, caught.exception.status)
                    self.assertNotIn("private-account-data", str(caught.exception))
        self.assertEqual([("search", ("history",), {"page": 2, "limit": 15, "filters": filters}),
                          ("search", ("[bold]history",), {"page": 2, "limit": 20, "filters": SearchFilters()}),
                          ("popular", (), {}), ("recommendations", (), {}), ("similar", ("12:abc",), {})], client.calls[:5])
        self.assertEqual([("recommendations", (), {})] * 4, client.calls[5:])

    def test_search_and_discovery_send_exact_read_only_requests(self):
        filters = SearchFilters(languages=("French", "english"), formats=("epub",), year_from=2000, year_to=2010, order="year")
        with server(body=json.dumps(page_payload()).encode()) as (url, calls):
            api = CatalogAPI(url, self.token_file)
            api.search(" history ", filters=filters)
            api.search("C++ & Rust/2024 é", cancel=CancellationToken())
        with server(body=json.dumps([BOOK.to_dict()]).encode()) as (url, lists):
            api = CatalogAPI(url, self.token_file)
            self.assertEqual((BOOK,), api.popular())
            self.assertEqual((BOOK,), api.recommended(cancel=CancellationToken()))
            self.assertEqual((BOOK,), api.similar("12:abc"))
        calls += lists
        self.assertEqual(["/v1/search?q=history&page=1&limit=20&language=french&language=english&format=epub"
                          "&year_from=2000&year_to=2010&order=year",
                          "/v1/search?q=C%2B%2B+%26+Rust%2F2024+%C3%A9&page=1&limit=20",
                          "/v1/discovery/popular", "/v1/discovery/recommended", "/v1/books/12/abc/similar"],
                         [call[1] for call in calls])
        for method, _, headers, body in calls:
            self.assertEqual(("GET", b""), (method, body))
            self.assertEqual("Bearer " + self.token, headers["Authorization"])

    def test_invalid_search_and_discovery_arguments_and_cancelled_reads_do_not_connect(self):
        api = CatalogAPI("http://localhost:8000", self.token_file)
        crowded = SearchFilters(languages=tuple(f"{index:02d}" + "é" * 62 for index in range(50)))
        invalid = [lambda query=query: api.search(query)
                   for query in ("", "   ", None, b"history", "x" * 501, "tab\tinside", "escape\x1b[31m", "bidi\u202etext")]
        invalid += [lambda options=options: api.search("history", **options)
                    for options in ({"page": 0}, {"page": 1001}, {"page": True}, {"page": "2"}, {"limit": 0}, {"limit": 51},
                                    {"filters": {"languages": ("french",)}}, {"filters": ("french",)}, {"filters": "epub"})]
        invalid.append(lambda: api.search("x" * 500, filters=crowded))
        invalid += [lambda reference=reference: api.similar(reference)
                    for reference in ("12", "12:", ":abc", 12, None, " 12:abc", "12:abc\n", "12:abc/formats", "12:abc?secret",
                                      "12:a-b", "1" * 21 + ":abc", "12:" + "a" * 129, "١٢:abc",
                                      "https://books.example/book/12/abc")]
        with patch("zlib_connector.local_api.http.client.HTTPConnection") as connect:
            for index, operation in enumerate(invalid):
                with self.subTest(case=index), self.assertRaises(ValueError):
                    operation()
            with self.assertRaises(ValueError) as caught:
                api.similar("12")
            self.assertEqual("Use a complete book reference: ID:HASH.", str(caught.exception))
            cancel = CancellationToken()
            cancel.cancel()
            for operation in (lambda **kwargs: api.search("history", filters=SearchFilters(formats=("pdf",)), **kwargs),
                              api.popular, api.recommended, lambda **kwargs: api.similar("12:abc", **kwargs)):
                with self.assertRaises(TransferCancelled):
                    operation(cancel=cancel)
            connect.assert_not_called()

    def test_book_lists_keep_server_order_sparse_metadata_and_literal_text(self):
        sparse = {"title": "Sparse", "book_id": "13", "book_hash": "def"}
        hostile = {**BOOK.to_dict(), "title": "\x1b[31m[bold]Title[/bold]\x1b[0m\u202e\x00",
                   "authors": "\x1b]8;;https://private.example\x1b\\Author\x1b]8;;\x1b\\", "year": "\u20662024\u2069",
                   "is_favorite": True, "secret": "private-account-data", "description": "private-account-data",
                   "booklist_id": "5", "entry_id": "99"}
        result = self.fetch([BOOK.to_dict(), sparse, hostile, {**sparse, "is_favorite": False}, BOOK.to_dict()], "popular")
        self.assertEqual((BOOK, SPARSE, Book("[bold]Title[/bold]", "Author", "epub", BOOK.url, year="2024", book_id="12",
                                             book_hash="abc", is_favorite=True),
                          Book("Sparse", "", "", "", book_id="13", book_hash="def", is_favorite=False), BOOK), result)
        self.assertTrue(all(type(book) is Book for book in result))
        self.assertNotIn("private-account-data", repr(result))
        self.assertEqual((), self.fetch([], "recommended"))
        self.assertEqual((SPARSE,), self.fetch([sparse], "similar", "12:abc"))
        self.assertEqual(1000, len(self.fetch([sparse] * 1000, "popular")))

    def test_book_lists_reject_invalid_records_and_unbounded_rows(self):
        row = BOOK.to_dict()
        untitled = {name: value for name, value in row.items() if name != "title"}
        for value in ({"books": [row]}, None, "books", [row] * 1001, [None], [[row]], ["12:abc"], [untitled],
                      [{**row, "title": ""}], [{**row, "title": "\x1b[0m\u202e"}], [{**row, "title": 123}],
                      [{**row, "title": "x" * 4097}], [{**row, "authors": None}], [{**row, "extension": "x" * 33}],
                      [{**row, "year": 2024}], [{**row, "book_id": 12}], [{**row, "book_id": "x"}], [{**row, "book_id": ""}],
                      [{**row, "book_hash": "a-b"}], [{**row, "book_hash": None}], [{**row, "is_favorite": 1}],
                      [{**row, "is_favorite": "true"}], [row, {**row, "is_favorite": 0}]):
            with self.subTest(value=repr(value)[:80]):
                with self.assertRaises(NetworkError) as caught:
                    catalog_schema.books(value)
                self.assertEqual("The local API returned invalid catalog data.", str(caught.exception))
        for method, args in (("popular", ()), ("recommended", ()), ("similar", ("12:abc",))):
            for value in ([row] * 1001, [{**row, "book_hash": "a-b"}], page_payload()):
                with self.subTest(method=method, value=repr(value)[:80]), self.assertRaises(NetworkError):
                    self.fetch(value, method, *args)

    def test_search_pages_follow_collection_page_rules(self):
        payload = page_payload(books=[BOOK, BOOK], page=2, limit=3, total_items=8, total_pages=3, next_page=3, previous_page=1)
        self.assertEqual(BookPage((BOOK, BOOK), 2, 3, 8, 3, 3, 1), self.fetch(payload, "search", "history", page=2, limit=3))
        payload = page_payload(books=[])
        payload["books"] = [{**SPARSE.to_dict(), "booklist_id": "5", "entry_id": "99"}]
        self.assertIs(Book, type(self.fetch(payload, "search", "history").books[0]))
        self.assertEqual((), self.fetch(page_payload(books=[], total_items=0, total_pages=0), "search", "history").books)
        # The server may echo a larger upstream page size; rows must still fit the request.
        echoed = self.fetch(page_payload(books=[BOOK] * 3, limit=50), "search", "history", limit=3)
        self.assertEqual((3, 3), (len(echoed.books), echoed.limit))
        self.assertIsNone(self.fetch(page_payload(page=1000, next_page=1001), "search", "history", page=1000).next_page)
        for fields, options in (({"page": 2}, {}), ({"page": "1"}, {}), ({"limit": 51}, {}), ({"limit": 51}, {"limit": 3}),
                                ({"books": [BOOK] * 4, "limit": 3}, {"limit": 3}), ({"books": [BOOK] * 4, "limit": 50}, {"limit": 3}),
                                ({"next_page": 1}, {}), ({"previous_page": 1}, {}), ({"total_pages": 1, "next_page": 2}, {}),
                                ({"total_items": 0}, {}), ({"total_pages": 0}, {})):
            with self.subTest(fields=fields), self.assertRaises(NetworkError):
                self.fetch(page_payload(**fields), "search", "history", **options)
        with self.assertRaises(NetworkError):
            self.fetch([BOOK.to_dict()], "search", "history")

    def test_catalog_transport_allows_only_whitelisted_reads(self):
        api = CatalogAPI("http://localhost:8000", self.token_file)
        for path in ("/favorites", "/history", "/booklists", "/booklists/5/books", "/books/12/abc", "/search",
                     "/discovery/popular", "/discovery/recommended", "/books/12/abc/similar"):
            with self.subTest(path=path):
                api._http._validate_operation("GET", path)
        for method, path in (("POST", "/search"), ("HEAD", "/search"), ("get", "/search"), ("GET", "/search/x"),
                             ("GET", "/search?q=x"), ("GET", "/discovery"), ("GET", "/discovery/recently"),
                             ("GET", "/discovery/popular/"), ("POST", "/discovery/recommended"), ("GET", "/books/1/a/formats"),
                             ("GET", "/books/1/a/similar/x"), ("GET", "/books/1/a-b/similar"), ("GET", "/books/x/a/similar"),
                             ("GET", "/books/12/abc/similar\n"), ("DELETE", "/books/12/abc/similar"),
                             ("GET", "/books/" + "1" * 21 + "/a/similar"), ("GET", "/downloads"), ("GET", "/profile"),
                             ("GET", "/library"), ("GET", "/my-library/tags"), ("PUT", "/favorites/12/abc")):
            with self.subTest(method=method, path=path):
                with self.assertRaises(ValueError) as caught:
                    api._http._validate_operation(method, path)
                self.assertEqual("Unsupported local catalog operation.", str(caught.exception))
        with patch("zlib_connector.local_api.http.client.HTTPConnection") as connect:
            for path in ("/discovery/recently", "/books/12/abc/formats", "/downloads"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    api._http._request_json("GET", path)
            connect.assert_not_called()

    def test_catalog_import_does_not_require_textual(self):
        result = subprocess.run([sys.executable, "-c", "import sys; import zlib_connector.catalog_api; assert 'textual' not in sys.modules"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
