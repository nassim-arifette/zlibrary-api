import io
import json
import tempfile
import unittest
from unittest.mock import patch

from zlib_connector import AuthenticationError, Book, Client, FormatChangedError
from zlib_connector.cli import main
from zlib_connector.credentials import Credentials
from test_eapi import BOOK, ORIGIN, QueueTransport
from session_fixture import isolated_sessions

SPARSE_BOOK = {"id": 123, "hash": "ab12", "title": "Example", "author": "Author", "cover": "https://covers.example/book.jpg"}


class DiscoveryTests(unittest.TestCase):
    def test_similar_and_popular_keep_sparse_records_without_adding_requests(self):
        transport = QueueTransport([{"success": 1, "books": [SPARSE_BOOK]}, {"success": 1, "books": [SPARSE_BOOK]}])
        with Client(ORIGIN, transport=transport) as client:
            similar = client.get_similar("123:ab12")
            popular = client.get_popular()
        self.assertEqual(similar[0].book_id, "123")
        self.assertEqual(similar[0].extension, "")
        self.assertEqual(popular[0].url, ORIGIN + "book/123/ab12")
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(transport.calls[0][0].endswith("/123/ab12/similar"))
        self.assertTrue(transport.calls[1][0].endswith("/most-popular"))

    def test_recommendations_require_a_session(self):
        transport = QueueTransport([{"response": {}}, {"success": 1, "user": {}}, {"success": 1, "books": [SPARSE_BOOK]}])
        with Client(ORIGIN, transport=transport) as client:
            with self.assertRaises(AuthenticationError):
                client.get_recommendations()
            self.assertEqual(transport.calls, [])
            client.login("synthetic", "synthetic")
            self.assertEqual(client.get_recommendations()[0].title, "Example")
        self.assertTrue(transport.calls[-1][0].endswith("/user/book/recommended"))

    def test_missing_collection_is_not_reported_as_empty(self):
        transport = QueueTransport([{"success": 1, "books": []}, {"success": 1},
                                    {"success": 1, "books": [{"title": "missing reference"}]}])
        with Client(ORIGIN, transport=transport) as client:
            self.assertEqual(client.get_popular(), [])
            with self.assertRaises(FormatChangedError):
                client.get_popular()
            with self.assertRaises(FormatChangedError):
                client.get_similar("123:ab12")

    def test_similar_rejects_foreign_or_incomplete_references_before_requesting(self):
        transport = QueueTransport([])
        with Client(ORIGIN, transport=transport) as client:
            for reference in ("123", "123:../private", "https://other.example/book/123/ab12"):
                with self.subTest(reference=reference), self.assertRaises(ValueError):
                    client.get_similar(reference)
        self.assertEqual(transport.calls, [])

    def test_downloading_a_sparse_book_fetches_its_format_before_the_file(self):
        transport = QueueTransport([{"response": {}}, {"success": 1, "user": {}}, {"success": 1, "books": [SPARSE_BOOK]},
                                    {"success": 1, "book": BOOK}, {"success": 1, "file": {"downloadLink": "https://cdn.example/book.pdf"}},
                                    b"%PDF-1.7\nexample"])
        with tempfile.TemporaryDirectory() as directory, Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            book = client.get_popular()[0]
            path = client.download(book, directory)
            self.assertEqual(path.suffix, ".pdf")
            self.assertEqual(path.read_bytes(), b"%PDF-1.7\nexample")
        self.assertTrue(transport.calls[3][0].endswith("/book/123/ab12"))
        self.assertTrue(transport.calls[4][0].endswith("/book/123/ab12/file"))

    def test_foreign_sparse_book_cannot_trigger_a_detail_request(self):
        transport = QueueTransport([{"response": {}}, {"success": 1, "user": {}}])
        with tempfile.TemporaryDirectory() as directory, Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            book = Book("Example", "", "", "https://other.example/book/123/ab12", book_id="123", book_hash="ab12")
            with self.assertRaises(ValueError):
                client.download(book, directory)
        self.assertEqual(len(transport.calls), 2)


@isolated_sessions
class DiscoveryCliTests(unittest.TestCase):
    def test_anonymous_discovery_does_not_load_credentials(self):
        for args in (["popular", "--anonymous"], ["similar", "123:ab12", "--anonymous"]):
            transport = QueueTransport([{"success": 1, "books": [SPARSE_BOOK, {**SPARSE_BOOK, "id": 456}]}])
            output = io.StringIO()
            with self.subTest(args=args), patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                    patch("zlib_connector.cli.load_credentials") as load, patch("sys.stdout", output):
                self.assertEqual(main([*args, "--limit", "1", "--json"]), 0)
            self.assertEqual(len(json.loads(output.getvalue())), 1)
            load.assert_not_called()

    def test_recommended_cli_uses_the_signed_in_account(self):
        transport = QueueTransport([{"response": {}}, {"success": 1, "user": {}}, {"success": 1, "books": [SPARSE_BOOK]}])
        output = io.StringIO()
        with patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                patch("zlib_connector.cli.load_credentials", return_value=Credentials(ORIGIN, "synthetic", "synthetic")), \
                patch("sys.stdout", output):
            self.assertEqual(main(["recommended", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())[0]["book_id"], "123")

    def test_bad_limits_fail_before_credentials_or_network(self):
        with patch("zlib_connector.cli.load_credentials") as load, patch("zlib_connector.cli.Client") as constructor, \
                patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(["recommended", "--limit", "0"]), 1)
        load.assert_not_called()
        constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
