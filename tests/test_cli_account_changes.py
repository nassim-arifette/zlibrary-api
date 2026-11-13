"""Described account changes, dry runs and lists of exact references, with synthetic replies only."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from zlib_connector import Client, NetworkError, RateLimitError
from zlib_connector.cli import main
from zlib_connector.credentials import Credentials
from test_eapi import BOOK, ORIGIN, QueueTransport
from session_fixture import isolated_sessions

LOGIN = [{"response": {}}, {"success": 1, "user": {}}]
LISTS = {"success": 1, "list": [{"id": 42, "title": "To read", "books_count": 12}]}
MUTATING = ("/favorites", "/tags", "add-book", "remove-book", "/delete", "/update", "/create")


class StdinBytes(io.StringIO):
    def __init__(self, data: bytes):
        super().__init__("")
        self.buffer = io.BytesIO(data)


@isolated_sessions
class AccountChangeCliTests(unittest.TestCase):
    def run_cli(self, args, *replies, stdin=None, terminal=False):
        transport = QueueTransport([*LOGIN, *replies])
        stdout, stderr = io.StringIO(), io.StringIO()
        stream = stdin if stdin is not None else io.StringIO()
        with patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                patch("zlib_connector.cli.load_credentials", return_value=Credentials(ORIGIN, "synthetic", "synthetic")), \
                patch("sys.stdin", stream), patch("zlib_connector.cli_terminal.is_terminal", return_value=terminal), \
                patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main(args)
        return code, stdout.getvalue(), stderr.getvalue(), transport

    def assert_nothing_changed(self, transport):
        self.assertEqual(transport.responses, [])
        self.assertFalse([url for url, _, _ in transport.calls if any(part in url for part in MUTATING)])

    def test_dry_run_describes_a_single_change_without_prompting_or_sending_it(self):
        code, out, err, transport = self.run_cli(["favorites", "add", "123:ab12", "--dry-run", "--json"],
                                                 {"success": 1, "book": BOOK})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"dry_run": True, "action": "favorite_add", "domain": ORIGIN,
                                           "book_id": "123", "title": "Example"})
        self.assertEqual(err, "")
        self.assert_nothing_changed(transport)

    def test_every_change_accepts_dry_run(self):
        entries = {"success": 1, "books": [{"book": BOOK, "readlist_books_id": 987}]}
        for args, replies, expected in (
            (["favorites", "remove", "123:ab12"], [{"success": 1, "book": BOOK}],
             'Remove "Example" (book 123) from favorites'),
            (["tags", "add", "123:ab12", "Science"], [{"success": 1, "book": BOOK}],
             'Add tags ["Science"] to "Example" (book 123) on ' + ORIGIN + '. This also adds the book to favorites.'),
            (["tags", "remove", "123:ab12", "Science"], [{"success": 1, "book": BOOK}], 'Remove tags ["Science"]'),
            (["booklists", "add", "42", "123:ab12"], [{"success": 1, "book": BOOK}, LISTS],
             'Add "Example" (book 123) to booklist 42 "To read" (12 books)'),
            (["booklists", "remove", "42", "987"], [LISTS, entries],
             'Remove entry 987 ("Example", book 123) from booklist 42'),
            (["booklists", "create", "Novels", "--public"], [], 'Create booklist "Novels" on ' + ORIGIN + " (public"),
            (["booklists", "edit", "42", "--title", "Later"], [LISTS],
             'Edit booklist 42 "To read" (12 books) on ' + ORIGIN + ': title "Later"'),
            (["booklists", "delete", "42"], [LISTS],
             f'Permanently delete booklist 42 "To read" (12 books) on {ORIGIN}. This cannot be undone.'),
        ):
            with self.subTest(args=args):
                code, out, err, transport = self.run_cli([*args, "--dry-run"], *replies)
                self.assertEqual(code, 0, err)
                self.assertTrue(out.startswith("Dry run, nothing sent: "), out)
                self.assertIn(expected, out)
                self.assert_nothing_changed(transport)

    def test_dry_run_of_an_unknown_booklist_edit_is_an_error(self):
        code, _, err, _ = self.run_cli(["booklists", "edit", "7", "--title", "x", "--dry-run", "--json"], LISTS)
        self.assertEqual((code, json.loads(err)["code"]), (1, "booklist_not_found"))

    def test_yes_and_dry_run_together_are_refused_before_credentials(self):
        with patch("zlib_connector.cli.load_credentials") as load, patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(["favorites", "add", "123:ab12", "--yes", "--dry-run"]), 2)
        load.assert_not_called()

    def test_a_list_from_stdin_applies_each_reference_once_in_order(self):
        stdin = StdinBytes(b"123:ab12\nhttps://books.example/book/456/cd34/x.html\n789:ef56\n")
        code, out, err, transport = self.run_cli(["favorites", "add", "-", "--yes", "--json"],
                                                 {"success": 1}, NetworkError("synthetic"), {"success": 1}, stdin=stdin)
        self.assertEqual(code, 4)
        self.assertIn(f"Add 3 books to favorites on {ORIGIN}.", err)
        self.assertEqual(json.loads(out), {
            "items": [{"position": 1, "reference": "123:ab12", "status": "applied", "code": None},
                      {"position": 2, "reference": "456:cd34", "status": "uncertain", "code": "mutation_uncertain"},
                      {"position": 3, "reference": "789:ef56", "status": "applied", "code": None}],
            "summary": {"applied": 2, "failed": 0, "uncertain": 1, "not_started": 0}})
        self.assertEqual(len(transport.calls), 5)

    def test_a_rate_limit_stops_the_list_and_leaves_the_rest_not_started(self):
        stdin = StdinBytes(b"123:ab12\n456:cd34\n789:ef56\n")
        code, out, _, transport = self.run_cli(["tags", "remove", "-", "Science", "--yes", "--json"],
                                               {"success": 1}, RateLimitError("slow down"), stdin=stdin)
        self.assertEqual(code, 3)
        result = json.loads(out)
        self.assertEqual([item["status"] for item in result["items"]], ["applied", "not_started", "not_started"])
        self.assertEqual(result["items"][1]["code"], "upstream_rate_limited")
        self.assertEqual(result["summary"], {"applied": 1, "failed": 0, "uncertain": 0, "not_started": 2})
        self.assertEqual(transport.responses, [])

    def test_search_json_output_works_as_a_file_list(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "books.json"
            path.write_text(json.dumps([{"book_id": "123", "book_hash": "ab12", "title": "Example"},
                                        {"book_id": "456", "book_hash": "cd34", "title": "Other"}]))
            code, out, _, transport = self.run_cli(["tags", "add", "-a", str(path), "Science", "--yes", "--json"],
                                                   {"success": 1}, {"success": 1})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["summary"], {"applied": 2, "failed": 0, "uncertain": 0, "not_started": 0})
        self.assertEqual(transport.json_bodies[-2:], [{"tags": ["Science"]}, {"tags": ["Science"]}])

    def test_a_list_dry_run_shows_the_references_without_sending(self):
        stdin = StdinBytes(json.dumps({"books": [{"book_id": "123", "book_hash": "ab12"}]}).encode())
        code, out, _, transport = self.run_cli(["booklists", "add", "42", "-", "--dry-run", "--json"], LISTS, stdin=stdin)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"dry_run": True, "action": "booklist_add", "domain": ORIGIN, "count": 1,
                                           "references": ["123:ab12"], "booklist_id": "42"})
        self.assert_nothing_changed(transport)

    def test_lists_refuse_titles_missing_yes_and_mixed_sources_before_credentials(self):
        for args, data, expected in (
            (["favorites", "add", "-", "--yes"], b"123:ab12\nDune\n", "download -a LIST --dry-run --json"),
            (["favorites", "remove", "-"], b"123:ab12\n", "needs --yes"),
            (["favorites", "add", "123:ab12", "-a", "list.txt", "--yes"], b"", "not several"),
            (["favorites", "add", "--yes"], b"", "Give a reference"),
            (["booklists", "add", "42", "-", "--yes"], b"", "no references"),
        ):
            with self.subTest(args=args), patch("zlib_connector.cli.load_credentials") as load, \
                    patch("sys.stdin", StdinBytes(data)), patch("sys.stderr", io.StringIO()) as err:
                self.assertEqual(main(args), 2)
            self.assertIn(expected, err.getvalue())
            load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
