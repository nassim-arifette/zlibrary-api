import io
import json
import unittest
from unittest.mock import patch

from zlib_connector import Client, FormatChangedError, SearchFilters
from zlib_connector.cli import main, shell
from test_eapi import BOOK, ORIGIN, QueueTransport
from session_fixture import isolated_sessions


class SearchTests(unittest.TestCase):
    def test_filters_select_multiple_languages_formats_years_and_order(self):
        transport = QueueTransport([{"success": 1, "books": [BOOK]}])
        filters = SearchFilters(languages=("French", "english", "French"),
                                formats=("PDF", "epub"), year_from=1850, year_to=1950, order="titleA")
        with Client(ORIGIN, transport=transport) as client:
            books = client.search(" Example ", filters=filters)
        self.assertEqual(books[0].book_id, "123")
        self.assertEqual(transport.calls[0][1], {
            "message": "Example", "page": "1", "limit": "20", "extensions[]": ["pdf", "epub"],
            "languages[]": ["french", "english"], "yearFrom": "1850", "yearTo": "1950", "order": "titleA",
        })

    def test_legacy_extension_and_list_return_are_preserved(self):
        transport = QueueTransport([{"success": 1, "books": [BOOK]}])
        with Client(ORIGIN, transport=transport) as client:
            result = client.search("Example", extension="pdf")
        self.assertIsInstance(result, list)
        self.assertEqual(transport.calls[0][1]["extensions[]"], "pdf")
        self.assertNotIn("order", transport.calls[0][1])

    def test_filters_are_validated_before_network_access(self):
        for arguments in (
            {"languages": "french"}, {"languages": ("\x1b[31m",)}, {"formats": ("exe",)},
            {"year_from": True}, {"year_from": 2000, "year_to": 1900},
            {"year_to": "2000"}, {"year_to": 10000}, {"order": "date_created"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                SearchFilters(**arguments)
        transport = QueueTransport([])
        with Client(ORIGIN, transport=transport) as client:
            for arguments in ({"page": True}, {"limit": 0}, {"page": 1.5},
                              {"extension": "pdf", "filters": SearchFilters(formats=("epub",))}):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    client.search("Example", **arguments)
        self.assertEqual(transport.calls, [])

    def test_structured_page_preserves_remote_totals_and_navigation(self):
        transport = QueueTransport([{"success": 1, "books": [BOOK], "exactBooksCount": 1,
                                    "pagination": {"current": "2", "limit": 3, "total_items": "10",
                                                   "total_pages": 4, "before": 1, "next": 3}}])
        with Client(ORIGIN, transport=transport) as client:
            result = client.search_page("Example", page=2, limit=3)
        self.assertEqual(result.to_dict()["pagination"], {
            "page": 2, "limit": 3, "total_items": 10, "total_pages": 4,
            "next_page": 3, "previous_page": 1,
        })
        self.assertEqual(result.to_dict()["books"][0]["book_id"], "123")

    def test_missing_pagination_stays_unknown_even_with_a_full_page(self):
        transport = QueueTransport([{"success": 1, "books": [BOOK]}])
        with Client(ORIGIN, transport=transport) as client:
            result = client.search_page("Example", limit=1)
        self.assertIsNone(result.total_items)
        self.assertIsNone(result.next_page)

    def test_empty_last_page_is_valid_and_infers_only_known_navigation(self):
        transport = QueueTransport([{"success": 1, "books": [],
                                    "pagination": {"current": 2, "total_pages": 2, "total_items": 1}}])
        with Client(ORIGIN, transport=transport) as client:
            result = client.search_page("Example", page=2, limit=1)
        self.assertEqual(result.books, ())
        self.assertIsNone(result.next_page)
        self.assertEqual(result.previous_page, 1)

    def test_live_api_false_navigation_means_no_previous_or_next_page(self):
        transport = QueueTransport([{"success": 1, "books": [BOOK], "pagination": {
            "current": 1, "limit": 3, "total_items": 1, "total_pages": 1, "before": False, "next": False,
        }}])
        with Client(ORIGIN, transport=transport) as client:
            result = client.search_page("Example", limit=3)
        self.assertIsNone(result.previous_page)
        self.assertIsNone(result.next_page)

    def test_iterator_is_lazy_and_keeps_filters_across_pages(self):
        transport = QueueTransport([
            {"success": 1, "books": [BOOK], "pagination": {"current": 1, "next": 2}},
            {"success": 1, "books": [{**BOOK, "id": 456}], "pagination": {"current": 2, "next": 0}},
        ])
        with Client(ORIGIN, transport=transport) as client:
            books = client.iter_search("Example", filters=SearchFilters(languages=("english",)), limit=1)
            self.assertEqual(transport.calls, [])
            self.assertEqual(next(books).book_id, "123")
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual([book.book_id for book in books], ["456"])
        self.assertEqual(transport.calls[1][1]["page"], "2")
        self.assertEqual(transport.calls[1][1]["languages[]"], ["english"])

    def test_invalid_or_cyclic_pagination_is_rejected(self):
        for pagination in ([], {"next": 1}, {"before": 2}, {"next": "https://example.com"},
                           {"total_items": -1}, {"total_items": True}, {"next": True}, {"limit": 0}):
            with self.subTest(pagination=pagination):
                transport = QueueTransport([{"success": 1, "books": [], "pagination": pagination}])
                with Client(ORIGIN, transport=transport) as client, self.assertRaises(FormatChangedError):
                    client.search_page("Example")

    def test_unselected_formats_are_not_returned(self):
        transport = QueueTransport([{"success": 1, "books": [BOOK, {**BOOK, "id": 456, "extension": "epub"}]}])
        with Client(ORIGIN, transport=transport) as client:
            result = client.search("Example", filters=SearchFilters(formats=("epub",)))
        self.assertEqual([book.book_id for book in result], ["456"])

    def test_remote_metadata_cannot_inject_terminal_escape_sequences(self):
        transport = QueueTransport([{"success": 1, "books": [{**BOOK, "year": "\x1b[31m1900", "filesizeString": "\x07size"}]}])
        with Client(ORIGIN, transport=transport) as client:
            book = client.search("Example")[0]
        self.assertNotIn("\x1b", book.year)
        self.assertNotIn("\x07", book.size)


@isolated_sessions
class SearchCliTests(unittest.TestCase):
    def test_json_remains_an_array_unless_pagination_is_requested(self):
        for extra, expected_type in (([], list), (["--with-pagination"], dict)):
            transport = QueueTransport([{"success": 1, "books": [BOOK]}])
            output = io.StringIO()
            with patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                    patch("sys.stdout", output), patch("zlib_connector.cli.load_credentials") as credentials:
                status = main(["search", "Example", "--anonymous", "--json", "--language", "english",
                               "--format", "pdf", "--format", "epub", "--year-from", "1800", "--order", "year", *extra])
            self.assertEqual(status, 0)
            self.assertIsInstance(json.loads(output.getvalue()), expected_type)
            credentials.assert_not_called()
            self.assertEqual(transport.calls[0][1]["yearFrom"], "1800")

    def test_invalid_filters_do_not_prompt_or_connect(self):
        with patch("zlib_connector.cli.Client") as constructor, \
                patch("zlib_connector.cli.load_credentials") as credentials, patch("sys.stderr", io.StringIO()):
            status = main(["search", "Example", "--year-from", "2020", "--year-to", "1900"])
        self.assertEqual(status, 2)
        credentials.assert_not_called()
        constructor.assert_not_called()

    def test_shell_navigates_pages_with_original_filters(self):
        first = {"success": 1, "books": [BOOK], "pagination": {"current": 1, "next": 2}}
        second = {"success": 1, "books": [{**BOOK, "id": 456}], "pagination": {"current": 2, "before": 1}}
        transport = QueueTransport([first, second, first])
        input_text = 'search "Example" --language english --format pdf\nnext\nprev\nquit\n'
        with Client(ORIGIN, transport=transport) as client, patch("sys.stdin", io.StringIO(input_text)), \
                patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            shell(client)
        self.assertEqual([call[1]["page"] for call in transport.calls], ["1", "2", "1"])
        self.assertTrue(all(call[1]["languages[]"] == ["english"] for call in transport.calls))


if __name__ == "__main__":
    unittest.main()
