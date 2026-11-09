import io
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from zlib_connector import AuthenticationError, BookPage, Client, FormatChangedError, SearchPage
from zlib_connector.cli import main
from zlib_connector.credentials import Credentials
from test_eapi import BOOK, ORIGIN, QueueTransport
from session_fixture import isolated_sessions


LIST = {"id": 42, "title": "Reading list", "books_count": 1, "public": 0,
        "description": "For later", "date_updated": "2026-11-09", "user_id": "synthetic-private-owner"}


def transport_for(*responses):
    return QueueTransport([{"response": {}}, {"success": 1, "user": {}}, *responses])


class LibraryTests(unittest.TestCase):
    def test_collection_reads_require_login(self):
        transport = QueueTransport([])
        with Client(ORIGIN, transport=transport) as client:
            for method, args in ((client.get_favorites, ()), (client.get_history, ()),
                                 (client.get_booklists, ()), (client.get_booklist_books, (42,)),
                                 (client.get_library, ())):
                with self.subTest(method=method.__name__), self.assertRaises(AuthenticationError):
                    method(*args)
        self.assertEqual(transport.calls, [])

    def test_favorites_and_history_share_pagination_but_have_distinct_orders(self):
        page = {"success": 1, "books": [BOOK], "pagination": {"current": 2, "next": 3, "before": 1}}
        transport = transport_for(page, page)
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            favorites = client.get_favorites(page=2, limit=3, order="saved_dateA")
            history = client.get_history(page=2, limit=3, order="downloaded_date")
        self.assertIsInstance(favorites, BookPage)
        self.assertIsInstance(favorites, SearchPage)
        self.assertEqual(history.next_page, 3)
        self.assertEqual(parse_qs(urlsplit(transport.calls[2][0]).query)["order"], ["saved_dateA"])
        self.assertEqual(parse_qs(urlsplit(transport.calls[3][0]).query)["order"], ["downloaded_date"])
        self.assertTrue(urlsplit(transport.calls[2][0]).path.endswith("/saved"))
        self.assertTrue(urlsplit(transport.calls[3][0]).path.endswith("/downloaded"))
        self.assertIsNone(transport.calls[2][1])

    def test_invalid_collection_arguments_do_not_issue_requests(self):
        transport = transport_for()
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            for method, kwargs in ((client.get_favorites, {"order": "downloaded_date"}),
                                   (client.get_history, {"order": "saved_date"}),
                                   (client.get_favorites, {"page": 0}), (client.get_library, {"limit": 51})):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    method(**kwargs)
            for identifier in ("../account", True, 0, -1, "9" * 21):
                with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                    client.get_booklist_books(identifier)
        self.assertEqual(len(transport.calls), 2)

    def test_sparse_or_unsupported_format_records_remain_visible(self):
        sparse = {"id": 456, "hash": "cd34", "title": "A sparse record"}
        transport = transport_for({"success": 1, "books": [sparse, {**BOOK, "extension": "mobi"}]})
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            result = client.get_history()
        self.assertEqual(len(result.books), 2)
        self.assertEqual(result.books[0].extension, "")
        self.assertEqual(result.books[1].extension, "mobi")

    def test_booklist_metadata_is_typed_sanitized_and_does_not_expose_owner_data(self):
        transport = transport_for({"success": 1, "list": [{**LIST, "title": "Reading\x1b list", "books_count": "1"}]})
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            lists = client.get_booklists()
        self.assertEqual(lists[0].booklist_id, "42")
        self.assertEqual(lists[0].book_count, 1)
        self.assertFalse(lists[0].is_public)
        self.assertNotIn("\x1b", lists[0].title)
        self.assertNotIn("synthetic-private-owner", json.dumps(lists[0].to_dict()))

    def test_missing_optional_booklist_fields_remain_unknown(self):
        transport = transport_for({"success": 1, "list": [{"id": 42}]})
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            item = client.get_booklists()[0]
        self.assertIsNone(item.is_public)
        self.assertIsNone(item.book_count)

    def test_booklist_content_uses_empty_post_and_unwraps_books(self):
        payload = {"success": 1, "books": [{"book": BOOK, "note": "synthetic-private-note"}],
                   "pagination": {"current": 2, "limit": 20, "next": False, "before": 1}}
        transport = transport_for(payload)
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            page = client.get_booklist_books("42", page=2)
        self.assertEqual(page.books[0].book_id, "123")
        self.assertIsNone(page.next_page)
        self.assertEqual(transport.calls[-1][0], ORIGIN + "papi/booklist/42/get-books/2")
        self.assertEqual(transport.calls[-1][1], {})
        self.assertNotIn("synthetic-private-note", json.dumps(page.to_dict()))

    def test_library_keeps_favorites_history_and_booklists_separate(self):
        transport = transport_for({"success": 1, "books": [], "pagination": {"total_items": 0}},
                                  {"success": 1, "books": [BOOK]}, {"success": 1, "list": [LIST]})
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            result = client.get_library(limit=1)
        self.assertEqual(result.favorites.books, ())
        self.assertEqual(result.favorites.total_items, 0)
        self.assertEqual(len(result.history.books), 1)
        self.assertEqual(len(result.booklists), 1)
        self.assertEqual(set(result.to_dict()), {"favorites", "history", "booklists"})
        self.assertEqual(len(transport.calls), 5)

    def test_missing_or_invalid_collection_payloads_are_explicit_errors(self):
        for method, args, payload in (
            ("get_favorites", (), {"success": 1}),
            ("get_history", (), {"success": 1, "books": [None]}),
            ("get_booklists", (), {"success": 1}),
            ("get_booklists", (), {"success": 1, "list": [{**LIST, "public": "unknown"}]}),
            ("get_booklists", (), {"success": 1, "list": [{**LIST, "books_count": -1}]}),
            ("get_booklist_books", (42,), {"success": 1, "books": [{"book": None}]}),
        ):
            transport = transport_for(payload)
            with self.subTest(method=method), Client(ORIGIN, transport=transport) as client:
                client.login("synthetic", "synthetic")
                with self.assertRaises(FormatChangedError):
                    getattr(client, method)(*args)


@isolated_sessions
class LibraryCliTests(unittest.TestCase):
    def test_cli_account_views_return_serializable_results(self):
        for command, payload in ((["favorites", "--json"], {"success": 1, "books": [BOOK]}),
                                 (["history", "--json"], {"success": 1, "books": [BOOK]}),
                                 (["booklists", "--json"], {"success": 1, "list": [LIST]}),
                                 (["booklists", "list", "--json"], {"success": 1, "list": [LIST]}),
                                 (["booklists", "show", "42", "--json"], {"success": 1, "books": [{"book": BOOK}]})):
            transport = transport_for(payload)
            output = io.StringIO()
            with self.subTest(command=command), patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                    patch("zlib_connector.cli.load_credentials", return_value=Credentials(ORIGIN, "synthetic", "synthetic")), \
                    patch("sys.stdout", output):
                self.assertEqual(main(command), 0)
                self.assertTrue(json.loads(output.getvalue()))

    def test_invalid_cli_arguments_do_not_load_credentials(self):
        for command in (["favorites", "--page", "0"], ["library", "--limit", "51"],
                        ["booklists", "show", "../private"]):
            with self.subTest(command=command), patch("zlib_connector.cli.load_credentials") as load, \
                    patch("sys.stderr", io.StringIO()):
                self.assertEqual(main(command), 1)
            load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
