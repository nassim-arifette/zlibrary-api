import io
import json
import unittest
from unittest.mock import patch

from zlib_connector import AuthenticationError, Client, DownloadError, FormatChangedError, MutationUncertainError, RateLimitError
from zlib_connector.cli import main
from zlib_connector.credentials import Credentials
from test_eapi import BOOK, ORIGIN, QueueTransport
from test_transport import Connection, Reply
from session_fixture import isolated_sessions


class FavoriteTests(unittest.TestCase):
    def test_unreadable_mutation_responses_are_uncertain_without_a_retry(self):
        replies = [Reply(headers=[("Content-Length", str(4 * 1024 * 1024 + 1))]),
                   Reply(data=b'<html><title>Just a moment...</title>cf-chl challenge</html>')]
        for reply in replies:
            login = Reply(data=b'{"response":{}}', headers=[
                ("Set-Cookie", "remix_userkey=synthetic; Secure; Path=/"),
                ("Set-Cookie", "remix_userid=123; Secure; Path=/"),
            ])
            connections = [Connection(login), Connection(Reply(data=b'{"success":1,"user":{}}')),
                           Connection(reply)]
            with self.subTest(reply=reply), patch("zlib_connector.transport.public_addresses", return_value=["8.8.8.8"]), \
                    patch("http.client.HTTPSConnection", side_effect=connections) as constructor:
                with Client(ORIGIN) as client:
                    client.login("synthetic", "synthetic")
                    with self.assertRaises(MutationUncertainError):
                        client.set_favorite("123:ab12", saved=True)
            self.assertEqual(constructor.call_count, 3)
            self.assertTrue(all(connection.closed for connection in connections))

    def test_explicit_mutation_refusals_keep_their_error_type(self):
        for reason, expected in (("login required", AuthenticationError), ("limit", RateLimitError),
                                 ("operation refused", DownloadError)):
            transport = QueueTransport([{"response": {}}, {"success": 1, "user": {}},
                                        {"success": 0, "error": reason}])
            with self.subTest(reason=reason), Client(ORIGIN, transport=transport) as client:
                client.login("synthetic", "synthetic")
                with self.assertRaises(expected):
                    client.set_favorite("123:ab12", saved=True)
            self.assertEqual(len(transport.calls), 3)

    def test_add_and_remove_are_explicit_account_updates(self):
        transport = QueueTransport([{"response": {}}, {"success": 1, "user": {}},
                                    {"success": 1}, {"success": 1}])
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            added = client.set_favorite("123:ab12", saved=True)
            removed = client.set_favorite("123:ab12", saved=False)
        self.assertEqual(added.to_dict(), {"book_id": "123", "is_favorite": True})
        self.assertFalse(removed.is_favorite)
        self.assertEqual(transport.calls[2][0], ORIGIN + "eapi/user/book/123/save")
        self.assertEqual(transport.calls[3][0], ORIGIN + "eapi/user/book/123/unsave")

    def test_invalid_state_or_reference_cannot_change_the_account(self):
        transport = QueueTransport([{"response": {}}, {"success": 1, "user": {}}])
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            for reference, state in (("123:ab12", 1), ("123:ab12", "false"), ("../account", True),
                                     ("https://other.example/book/123/ab12", True)):
                with self.subTest(reference=reference), self.assertRaises(ValueError):
                    client.set_favorite(reference, saved=state)
        self.assertEqual(len(transport.calls), 2)

    def test_mutation_redirect_is_not_followed_and_outcome_stays_unknown(self):
        for location in (ORIGIN + "eapi/user/book/123/save", "https://other.example/changed"):
            login = Reply(data=b'{"response":{}}', headers=[
                ("Set-Cookie", "remix_userkey=synthetic; Secure; Path=/"),
                ("Set-Cookie", "remix_userid=123; Secure; Path=/"),
            ])
            connections = [Connection(login), Connection(Reply(data=b'{"success":1,"user":{}}')),
                           Connection(Reply(302, headers=[("Location", location)]))]
            with self.subTest(location=location), patch("zlib_connector.transport.public_addresses", return_value=["8.8.8.8"]), \
                    patch("http.client.HTTPSConnection", side_effect=connections) as constructor:
                with Client(ORIGIN) as client:
                    client.login("synthetic", "synthetic")
                    with self.assertRaises(MutationUncertainError):
                        client.set_favorite("123:ab12", saved=True)
            self.assertEqual(constructor.call_count, 3)
            self.assertTrue(all(connection.closed for connection in connections))

    def test_malformed_update_response_is_not_reported_as_success_or_retried(self):
        transport = QueueTransport([{"response": {}}, {"success": 1, "user": {}}, b'not-json synthetic-secret'])
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            with self.assertRaises(MutationUncertainError) as caught:
                client.set_favorite("123:ab12", saved=False)
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertEqual(len(transport.calls), 3)

    def test_saved_markers_follow_the_response_and_missing_means_unknown(self):
        rows = [{**BOOK, "_isUserSavedBook": True}, {**BOOK, "id": 456, "_isUserSavedBook": "0"},
                {**BOOK, "id": 789}]
        transport = QueueTransport([{"success": 1, "books": rows},
                                    {"success": 1, "book": {**BOOK, "_isUserSavedBook": 1}}])
        with Client(ORIGIN, transport=transport) as client:
            books = client.search("Example")
            detail = client.get_book("123:ab12")
        self.assertEqual([book.is_favorite for book in books], [True, False, None])
        self.assertTrue(detail.is_favorite)

    def test_favorites_collection_marks_books_saved_even_without_flag(self):
        transport = QueueTransport([{"response": {}}, {"success": 1, "user": {}}, {"success": 1, "books": [BOOK]}])
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            self.assertTrue(client.get_favorites().books[0].is_favorite)

    def test_unrecognized_saved_state_is_a_format_error(self):
        transport = QueueTransport([{"success": 1, "books": [{**BOOK, "_isUserSavedBook": "maybe"}]}])
        with Client(ORIGIN, transport=transport) as client, self.assertRaises(FormatChangedError):
            client.search("Example")


@isolated_sessions
class FavoriteCliTests(unittest.TestCase):
    def test_non_interactive_account_update_requires_explicit_yes(self):
        with patch("sys.stdin", io.StringIO()), patch("sys.stderr", io.StringIO()), \
                patch("zlib_connector.cli.load_credentials") as load:
            self.assertEqual(main(["favorites", "add", "123:ab12"]), 2)
        load.assert_not_called()

    def test_explicit_cli_update_returns_confirmed_state(self):
        for action, state in (("add", True), ("remove", False)):
            transport = QueueTransport([{"response": {}}, {"success": 1, "user": {}}, {"success": 1}])
            output = io.StringIO()
            with self.subTest(action=action), patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                    patch("zlib_connector.cli.load_credentials", return_value=Credentials(ORIGIN, "synthetic", "synthetic")), \
                    patch("sys.stdout", output):
                self.assertEqual(main(["favorites", action, "123:ab12", "--yes", "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue()), {"book_id": "123", "is_favorite": state})

    def test_declining_a_change_does_not_contact_the_server(self):
        transport = QueueTransport([])
        input_stream = io.StringIO("n\n")
        input_stream.isatty = lambda: True
        with patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                patch("zlib_connector.cli.load_credentials", return_value=Credentials(ORIGIN, "synthetic", "synthetic")), \
                patch("sys.stdin", input_stream), patch("zlib_connector.cli_terminal.is_terminal", return_value=True), patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(["favorites", "remove", "123:ab12"]), 130)
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
