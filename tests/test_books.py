import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from zlib_connector import BookDetails, BookReference, Client, FormatChangedError
from zlib_connector.cli import main
from zlib_connector.credentials import Credentials
from test_eapi import BOOK, ORIGIN, QueueTransport


class BookTests(unittest.TestCase):
    def test_details_include_clean_metadata_and_checksum_valid_isbns(self):
        row = {**BOOK, "description": "<p>A <b>description</b></p><script>hidden</script>\x1b",
               "identifier": "ISBN 0-306-40615-2; ISBN 978-0-306-40615-7", "publisher": "Publisher",
               "edition": "2nd", "pages": 250, "cover": "https://covers.example/123.jpg",
               "categories": ["Science", {"name": "Physics"}]}
        transport = QueueTransport([{"success": 1, "book": row}])
        with Client(ORIGIN, transport=transport) as client:
            details = client.get_book("123:ab12")
        self.assertIsInstance(details, BookDetails)
        self.assertEqual(details.description, "A description")
        self.assertEqual(details.isbn10, "0306406152")
        self.assertEqual(details.isbn13, "9780306406157")
        self.assertEqual(details.pages, "250")
        self.assertEqual(details.publisher, "Publisher")
        self.assertEqual(details.categories, ("Science", "Physics"))
        self.assertEqual(details.to_dict()["edition"], "2nd")
        self.assertEqual(transport.calls[0][0], ORIGIN + "eapi/book/123/ab12")

    def test_missing_details_and_invalid_isbns_are_not_invented(self):
        transport = QueueTransport([{"success": 1, "book": {**BOOK, "identifier": "9780306406158", "cover": "javascript:bad"}}])
        with Client(ORIGIN, transport=transport) as client:
            details = client.get_book("123:ab12")
        self.assertEqual(details.isbn13, "")
        self.assertEqual(details.isbn10, "")
        self.assertEqual(details.cover_url, "")
        self.assertEqual(details.pages, "")
        self.assertEqual(details.description, "")

    def test_reference_accepts_only_complete_ids_and_selected_domain_urls(self):
        self.assertEqual(str(BookReference.parse(ORIGIN + "book/123/ab12/title.html", ORIGIN)), "123:ab12")
        invalid = ["123", "123:../../account", "123:ab12?token=hidden", "https://other.example/book/123/ab12",
                   "http://books.example/book/123/ab12", "https://user:pass@books.example/book/123/ab12",
                   ORIGIN + "book/123/ab12?token=hidden", ORIGIN + "account/123/ab12", "9" * 21 + ":ab12"]
        transport = QueueTransport([])
        with Client(ORIGIN, transport=transport) as client:
            for reference in invalid:
                with self.subTest(reference=reference), self.assertRaises(ValueError):
                    client.get_book(reference)
        self.assertEqual(transport.calls, [])

    def test_missing_or_mismatched_book_is_rejected(self):
        for row in (None, [], {**BOOK, "id": 456}, {**BOOK, "hash": "different"}):
            transport = QueueTransport([{"success": 1, "book": row}])
            with self.subTest(row=row), Client(ORIGIN, transport=transport) as client, self.assertRaises(FormatChangedError):
                client.get_book("123:ab12")

    def test_alternative_formats_keep_their_own_references(self):
        transport = QueueTransport([{"success": 1, "books": [
            {"id": 456, "hash": "cd34", "extension": "epub", "filesizeString": "3 KB"},
            {"id": 789, "hash": "ef56", "extension": "mobi"},
        ]}])
        with Client(ORIGIN, transport=transport) as client:
            formats = client.get_formats("123:ab12")
        self.assertEqual(formats[0].book_id, "456")
        self.assertEqual(formats[0].to_dict()["download_supported"], True)
        self.assertEqual(formats[1].to_dict()["download_supported"], False)

    def test_empty_formats_are_valid_but_a_missing_list_is_not(self):
        transport = QueueTransport([{"success": 1, "books": []}, {"success": 1}])
        with Client(ORIGIN, transport=transport) as client:
            self.assertEqual(client.get_formats("123:ab12"), [])
            with self.assertRaises(FormatChangedError):
                client.get_formats("123:ab12")

    def test_direct_download_uses_details_and_file_without_searching(self):
        transport = QueueTransport([
            {"response": {}}, {"success": 1, "user": {}}, {"success": 1, "book": BOOK},
            {"success": 1, "file": {"allowDownload": True, "extension": "pdf", "downloadLink": "https://cdn.example/book.pdf"}},
            b"%PDF-1.7\nexample",
        ])
        with tempfile.TemporaryDirectory() as directory, Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            path = client.download("123:ab12", directory)
            self.assertEqual(path.read_bytes(), b"%PDF-1.7\nexample")
        self.assertFalse(any("/search" in call[0] for call in transport.calls))


class BookCliTests(unittest.TestCase):
    def test_anonymous_info_and_formats_do_not_load_credentials(self):
        for command, payload in (("info", {"success": 1, "book": BOOK}),
                                 ("formats", {"success": 1, "books": []})):
            transport = QueueTransport([payload])
            output = io.StringIO()
            with patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                    patch("zlib_connector.cli.load_credentials") as credentials, patch("sys.stdout", output):
                status = main(["--domain", ORIGIN, command, "123:ab12", "--anonymous", "--json"])
            self.assertEqual(status, 0)
            self.assertIsNotNone(json.loads(output.getvalue()))
            credentials.assert_not_called()

    def test_direct_download_cli_does_not_prompt_for_a_search_result(self):
        transport = QueueTransport([
            {"response": {}}, {"success": 1, "user": {}}, {"success": 1, "book": BOOK},
            {"success": 1, "file": {"downloadLink": "https://cdn.example/book.pdf"}}, b"%PDF-1.7\nexample",
        ])
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
                patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                patch("zlib_connector.cli.load_credentials", return_value=Credentials(ORIGIN, "synthetic", "synthetic")), \
                patch("zlib_connector.cli.prompt") as prompt, patch("sys.stdout", output):
            self.assertEqual(main(["download", "123:ab12", "--output", directory, "--json"]), 0)
            self.assertTrue(Path(json.loads(output.getvalue())["path"]).is_file())
            prompt.assert_not_called()

    def test_bad_references_or_search_options_fail_before_login(self):
        for args in (["info", "invalid"], ["download", "123:ab12", "--pick", "1"],
                     ["download", "123:ab12", "--language", "english"]):
            with self.subTest(args=args), patch("zlib_connector.cli.load_credentials") as credentials, \
                    patch("sys.stderr", io.StringIO()):
                self.assertEqual(main(args), 1)
                credentials.assert_not_called()


if __name__ == "__main__":
    unittest.main()
