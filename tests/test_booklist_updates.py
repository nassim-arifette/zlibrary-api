import io
import json
import unittest
from unittest.mock import patch

from zlib_connector import AuthenticationError, BooklistBook, Client, FormatChangedError, MutationUncertainError
from zlib_connector.cli import main
from zlib_connector.credentials import Credentials
from test_eapi import BOOK, ORIGIN, QueueTransport
from test_library import transport_for
from test_transport import Connection, Reply


ENTRY = {"book": BOOK, "readlist_books_id": 987, "_isUserSavedBook": True}


class BooklistUpdateTests(unittest.TestCase):
    def test_entries_preserve_their_own_id_and_wrapped_favorite_state(self):
        transport = transport_for({"success": 1, "books": [ENTRY], "pagination": {"current": 1, "next": 2}})
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            page = client.get_booklist_books(42)
        book = page.books[0]
        self.assertIsInstance(book, BooklistBook)
        self.assertEqual(book.booklist_id, "42")
        self.assertEqual(book.entry_id, "987")
        self.assertEqual(book.book_id, "123")
        self.assertTrue(book.is_favorite)
        self.assertEqual(page.next_page, 2)
        self.assertEqual(page.to_dict()["books"][0]["entry_id"], "987")

    def test_missing_entry_id_is_unknown_and_malformed_entry_ids_are_rejected(self):
        transport = transport_for({"success": 1, "books": [{"book": BOOK}]})
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            self.assertIsNone(client.get_booklist_books(42).books[0].entry_id)
        for value in (True, 0, -1, [], "../delete", "9" * 21):
            transport = transport_for({"success": 1, "books": [{**ENTRY, "readlist_books_id": value}]})
            with self.subTest(value=value), Client(ORIGIN, transport=transport) as client:
                client.login("synthetic", "synthetic")
                with self.assertRaises(FormatChangedError):
                    client.get_booklist_books(42)

    def test_add_uses_book_id_and_remove_uses_list_entry_id(self):
        transport = transport_for({"success": 1}, {"success": 1})
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            added = client.add_booklist_book(42, "123:ab12")
            removed = client.remove_booklist_book("42", "987")
        self.assertEqual(added.to_dict(), {"booklist_id": "42", "action": "add", "book_id": "123", "entry_id": None})
        self.assertEqual(removed.to_dict(), {"booklist_id": "42", "action": "remove", "book_id": None, "entry_id": "987"})
        self.assertEqual(transport.calls[2][0], ORIGIN + "papi/booklist/42/add-book/123")
        self.assertEqual(transport.calls[3][0], ORIGIN + "papi/booklist/42/remove-book/987")
        self.assertIsNone(transport.calls[2][1])
        self.assertIsNone(transport.calls[3][1])

    def test_updates_require_a_session_and_valid_references(self):
        transport = QueueTransport([])
        with Client(ORIGIN, transport=transport) as client:
            for method, argument in ((client.add_booklist_book, "123:ab12"), (client.remove_booklist_book, 987)):
                with self.assertRaises(AuthenticationError):
                    method(42, argument)
        self.assertEqual(transport.calls, [])
        transport = transport_for()
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            invalid = ((client.add_booklist_book, "../delete", "123:ab12"),
                       (client.add_booklist_book, 42, "https://other.example/book/123/ab12"),
                       (client.remove_booklist_book, True, 987), (client.remove_booklist_book, 42, None),
                       (client.remove_booklist_book, 42, "123:ab12"))
            for method, identifier, reference in invalid:
                with self.subTest(method=method.__name__), self.assertRaises(ValueError):
                    method(identifier, reference)
        self.assertEqual(len(transport.calls), 2)

    def test_redirected_booklist_updates_are_not_replayed(self):
        for method, argument in (("add_booklist_book", "123:ab12"), ("remove_booklist_book", 987)):
            login = Reply(data=b'{"response":{}}', headers=[
                ("Set-Cookie", "remix_userkey=synthetic; Secure; Path=/"),
                ("Set-Cookie", "remix_userid=123; Secure; Path=/"),
            ])
            connections = [Connection(login), Connection(Reply(data=b'{"success":1,"user":{}}')),
                           Connection(Reply(307, headers=[("Location", ORIGIN + "unexpected")]))]
            with self.subTest(method=method), patch("zlib_connector.transport.public_addresses", return_value=["8.8.8.8"]), \
                    patch("http.client.HTTPSConnection", side_effect=connections) as constructor:
                with Client(ORIGIN) as client:
                    client.login("synthetic", "synthetic")
                    with self.assertRaises(MutationUncertainError):
                        getattr(client, method)(42, argument)
            self.assertEqual(constructor.call_count, 3)
            self.assertTrue(all(connection.closed for connection in connections))

    def test_unreadable_update_results_are_uncertain(self):
        for method, argument in (("add_booklist_book", "123:ab12"), ("remove_booklist_book", 987)):
            transport = transport_for(b'not-json synthetic-private')
            with self.subTest(method=method), Client(ORIGIN, transport=transport) as client:
                client.login("synthetic", "synthetic")
                with self.assertRaises(MutationUncertainError) as caught:
                    getattr(client, method)(42, argument)
            self.assertNotIn("synthetic-private", str(caught.exception))
            self.assertEqual(len(transport.calls), 3)


class BooklistUpdateCliTests(unittest.TestCase):
    def test_updates_return_json_with_an_explicit_yes(self):
        for action, argument in (("add", "123:ab12"), ("remove", "987")):
            transport = transport_for({"success": 1})
            with self.subTest(action=action), patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                    patch("zlib_connector.cli.load_credentials", return_value=Credentials(ORIGIN, "synthetic", "synthetic")), \
                    patch("sys.stdout", io.StringIO()) as output:
                self.assertEqual(main(["booklists", action, "42", argument, "--yes", "--json"]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["booklist_id"], "42")
            self.assertEqual(result["action"], action)
            self.assertEqual(len(transport.calls), 3)

    def test_noninteractive_or_invalid_updates_do_not_load_credentials(self):
        invalid = (["add", "42", "123:ab12"], ["remove", "42", "987"],
                   ["add", "0", "123:ab12", "--yes"], ["remove", "42", "123:ab12", "--yes"])
        for arguments in invalid:
            with self.subTest(arguments=arguments), patch("sys.stdin", io.StringIO()), \
                    patch("sys.stderr", io.StringIO()), patch("zlib_connector.cli.load_credentials") as load:
                self.assertEqual(main(["booklists", *arguments]), 1)
            load.assert_not_called()

    def test_declining_a_remove_does_not_log_in_or_change_the_list(self):
        transport = QueueTransport([])
        input_stream = io.StringIO("n\n")
        input_stream.isatty = lambda: True
        with patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                patch("zlib_connector.cli.load_credentials", return_value=Credentials(ORIGIN, "synthetic", "synthetic")), \
                patch("sys.stdin", input_stream), patch("sys.stderr", io.StringIO()) as output:
            self.assertEqual(main(["booklists", "remove", "42", "987"]), 130)
        self.assertIn("Remove entry 987 from booklist 42", output.getvalue())
        self.assertEqual(transport.calls, [])

    def test_show_displays_the_entry_id_needed_for_removal(self):
        transport = transport_for({"success": 1, "books": [ENTRY]})
        with patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                patch("zlib_connector.cli.load_credentials", return_value=Credentials(ORIGIN, "synthetic", "synthetic")), \
                patch("sys.stdout", io.StringIO()) as output:
            self.assertEqual(main(["booklists", "show", "42"]), 0)
        self.assertIn("Booklist entry: 987", output.getvalue())
        self.assertIn("123:ab12", output.getvalue())


if __name__ == "__main__":
    unittest.main()
