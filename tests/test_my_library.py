import io
import json
import unittest
from unittest.mock import patch

from zlib_connector import AuthenticationError, Client, DownloadError, FormatChangedError, MutationUncertainError, NetworkError, RateLimitError
from zlib_connector.cli import main
from zlib_connector.credentials import Credentials
from zlib_connector.my_library import BookTagUpdate, LibraryTags
from test_eapi import ORIGIN, QueueTransport
from test_transport import Connection, Reply
from session_fixture import isolated_sessions


def transport_for(*responses):
    return QueueTransport([{"response": {}}, {"success": 1, "user": {}}, *responses])


class MyLibraryTests(unittest.TestCase):
    def test_native_reads_use_distinct_get_routes_and_typed_projections(self):
        transport = transport_for({"success": 1, "tags": {"Science": {"opaque": "private"}}},
                                  {"success": 1, "list": [123, "456"]})
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            tags = client.get_library_tags()
            identifiers = client.get_saved_book_ids()
        self.assertEqual(tags, LibraryTags(("Science",)))
        self.assertEqual(identifiers, ("123", "456"))
        self.assertEqual([call[0] for call in transport.calls[2:]],
                         [ORIGIN + "papi/my-library/tags/", ORIGIN + "papi/my-library/saved-book-ids"])
        self.assertEqual(transport.methods[2:], ["GET", "GET"])
        self.assertEqual(transport.json_bodies[2:], [None, None])
        self.assertNotIn("private", json.dumps(tags.to_dict()))

    def test_all_native_operations_require_login_without_network_access(self):
        transport = QueueTransport([])
        with Client(ORIGIN, transport=transport) as client:
            for method, args in ((client.get_library_tags, ()), (client.get_saved_book_ids, ()),
                                 (client.add_book_tags, ("123:ab12", ["Science"])),
                                 (client.remove_book_tags, ("123:ab12", ["Science"]))):
                with self.subTest(method=method.__name__), self.assertRaises(AuthenticationError):
                    method(*args)
        self.assertEqual(transport.calls, [])

    def test_empty_catalog_and_saved_ids_are_valid_but_missing_fields_are_errors(self):
        for name, valid, invalid, expected in (
            ("get_library_tags", {"tags": []}, {}, LibraryTags(())),
            ("get_saved_book_ids", {"list": []}, {}, ()),
        ):
            transport = transport_for({"success": 1, **valid}, {"success": 1, **invalid})
            with self.subTest(method=name), Client(ORIGIN, transport=transport) as client:
                client.login("synthetic", "synthetic")
                self.assertEqual(getattr(client, name)(), expected)
                with self.assertRaises(FormatChangedError):
                    getattr(client, name)()

    def test_mutations_send_json_with_explicit_put_delete_and_report_only_changed_tags(self):
        transport = transport_for({"success": 1}, {"success": 1})
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            added = client.add_book_tags("123:ab12", ["Science", "science", "Science"])
            removed = client.remove_book_tags("123:ab12", ["Science"])
        self.assertEqual(added, BookTagUpdate("123", "add", ("Science", "science")))
        self.assertEqual(removed, BookTagUpdate("123", "remove", ("Science",)))
        self.assertTrue(added.adds_to_favorites)
        self.assertFalse(removed.adds_to_favorites)
        self.assertEqual([call[0] for call in transport.calls[2:]], [ORIGIN + "papi/my-library/tags/123"] * 2)
        self.assertEqual(transport.methods[2:], ["PUT", "DELETE"])
        self.assertEqual(transport.json_bodies[2:], [{"tags": ["Science", "science"]}, {"tags": ["Science"]}])
        self.assertEqual([call[1] for call in transport.calls[2:]], [None, None])

    def test_invalid_references_and_tag_batches_do_not_issue_mutation_requests(self):
        transport = transport_for()
        invalid = [("../account", ["Science"]), ("https://other.example/book/123/ab12", ["Science"]),
                   ("123:ab12", []), ("123:ab12", "Science"), ("123:ab12", [" "]),
                   ("123:ab12", ["unsafe\x1b"])]
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            for method in (client.add_book_tags, client.remove_book_tags):
                for reference, tags in invalid:
                    with self.subTest(method=method.__name__, reference=reference, tags=tags), self.assertRaises(ValueError):
                        method(reference, tags)
            with self.assertRaises(ValueError):
                client.add_book_tags("123:ab12", ["x" * 31])
        self.assertEqual(len(transport.calls), 2)

    def test_unreadable_mutation_responses_are_uncertain_and_not_retried(self):
        for method in ("add_book_tags", "remove_book_tags"):
            for payload in (b"not-json synthetic-private-value", {"unexpected": "synthetic-private-value"}):
                transport = transport_for(payload)
                with self.subTest(method=method, payload=payload), Client(ORIGIN, transport=transport) as client:
                    client.login("synthetic", "synthetic")
                    with self.assertRaises(MutationUncertainError) as caught:
                        getattr(client, method)("123:ab12", ["Science"])
                self.assertEqual(len(transport.calls), 3)
                self.assertNotIn("synthetic-private-value", str(caught.exception))

    def test_transport_failure_is_not_retried_and_redirects_are_disabled(self):
        for method in ("add_book_tags", "remove_book_tags"):
            transport = transport_for()
            with self.subTest(method=method), Client(ORIGIN, transport=transport) as client:
                client.login("synthetic", "synthetic")
                with patch.object(transport, "request", side_effect=NetworkError("synthetic-private-value")) as request:
                    with self.assertRaises(MutationUncertainError) as caught:
                        getattr(client, method)("123:ab12", ["Science"])
                self.assertEqual(request.call_count, 1)
                self.assertFalse(request.call_args.kwargs["follow_redirects"])
                self.assertNotIn("synthetic-private-value", str(caught.exception))

    def test_redirect_after_mutation_is_never_followed_in_the_real_transport(self):
        for method, status in (("add_book_tags", 307), ("remove_book_tags", 302)):
            login = Reply(data=b'{"response":{}}', headers=[
                ("Set-Cookie", "remix_userkey=synthetic; Secure; Path=/"),
                ("Set-Cookie", "remix_userid=123; Secure; Path=/"),
            ])
            connections = [Connection(login), Connection(Reply(data=b'{"success":1,"user":{}}')),
                           Connection(Reply(status, headers=[("Location", ORIGIN + "papi/my-library/tags/123")]))]
            with self.subTest(method=method), patch("zlib_connector.transport.public_addresses", return_value=["8.8.8.8"]), \
                    patch("http.client.HTTPSConnection", side_effect=connections) as constructor:
                with Client(ORIGIN) as client:
                    client.login("synthetic", "synthetic")
                    with self.assertRaises(MutationUncertainError):
                        getattr(client, method)("123:ab12", ["Science"])
            self.assertEqual(constructor.call_count, 3)
            self.assertTrue(all(connection.closed for connection in connections))

    def test_explicit_refusals_preserve_error_type_and_hide_remote_details(self):
        for reason, expected in (("login required", AuthenticationError), ("limit reached", RateLimitError),
                                 ("operation refused", DownloadError)):
            transport = transport_for({"success": 0, "error": reason + " synthetic-private-value"})
            with self.subTest(reason=reason), Client(ORIGIN, transport=transport) as client:
                client.login("synthetic", "synthetic")
                with self.assertRaises(expected) as caught:
                    client.add_book_tags("123:ab12", ["Science"])
            self.assertEqual(len(transport.calls), 3)
            self.assertNotIn("synthetic-private-value", str(caught.exception))


@isolated_sessions
class MyLibraryCliTests(unittest.TestCase):
    def test_existing_long_label_can_be_removed_without_changing_its_identity(self):
        label = "A legacy tag longer than thirty characters"
        transport = transport_for({"success": 1})
        output = io.StringIO()
        with patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                patch("zlib_connector.cli.load_credentials", return_value=Credentials(ORIGIN, "synthetic", "synthetic")), \
                patch("sys.stdout", output):
            self.assertEqual(main(["tags", "remove", "123:ab12", label, "--yes", "--json"]), 0)
        self.assertEqual(transport.json_bodies[-1], {"tags": [label]})
        self.assertEqual(json.loads(output.getvalue())["tags"], [label])

    def test_catalog_alias_and_saved_ids_have_stable_json_shapes(self):
        for command, payload, expected in (
            (["tags", "--json"], {"tags": []}, {"tags": []}),
            (["tags", "list", "--json"], {"tags": {"Science": 2}}, {"tags": ["Science"]}),
            (["tags", "saved-ids", "--json"], {"list": [123, "456"]}, {"book_ids": ["123", "456"]}),
        ):
            transport = transport_for({"success": 1, **payload})
            output = io.StringIO()
            with self.subTest(command=command), patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                    patch("zlib_connector.cli.load_credentials", return_value=Credentials(ORIGIN, "synthetic", "synthetic")), \
                    patch("sys.stdout", output):
                self.assertEqual(main(command), 0)
            self.assertEqual(json.loads(output.getvalue()), expected)

    def test_explicit_updates_return_confirmed_mutation_json(self):
        for action in ("add", "remove"):
            transport = transport_for({"success": 1})
            output = io.StringIO()
            with self.subTest(action=action), patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                    patch("zlib_connector.cli.load_credentials", return_value=Credentials(ORIGIN, "synthetic", "synthetic")), \
                    patch("sys.stdout", output):
                self.assertEqual(main(["tags", action, "123:ab12", "Science", "History", "--yes", "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue()),
                             BookTagUpdate("123", action, ("Science", "History")).to_dict())

    def test_noninteractive_updates_require_yes_before_loading_credentials(self):
        for action in ("add", "remove"):
            with self.subTest(action=action), patch("sys.stdin", io.StringIO()), patch("sys.stderr", io.StringIO()), \
                    patch("zlib_connector.cli.load_credentials") as load, patch("zlib_connector.cli.Client") as constructor:
                self.assertEqual(main(["tags", action, "123:ab12", "Science"]), 2)
            load.assert_not_called()
            constructor.assert_not_called()

    def test_declining_update_sends_no_change_and_add_confirmation_mentions_favorites(self):
        for action in ("add", "remove"):
            transport = QueueTransport([{"response": {}}, {"success": 1, "user": {}},
                                        {"success": 1, "book": {"id": 123, "hash": "ab12", "title": "Example"}}])
            input_stream = io.StringIO("n\n")
            input_stream.isatty = lambda: True
            output = io.StringIO()
            with self.subTest(action=action), patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                    patch("zlib_connector.cli.load_credentials", return_value=Credentials(ORIGIN, "synthetic", "synthetic")), \
                    patch("sys.stdin", input_stream), patch("zlib_connector.cli_terminal.is_terminal", return_value=True), patch("sys.stderr", output):
                self.assertEqual(main(["tags", action, "123:ab12", "Science"]), 130)
            self.assertEqual(transport.responses, [])
            self.assertFalse(any("tags" in url for url, _, _ in transport.calls))
            if action == "add":
                self.assertIn("favorite", output.getvalue().lower())

    def test_invalid_tag_mutations_are_rejected_before_loading_credentials(self):
        for reference, tag in (("../account", "Science"), ("123:ab12", ""), ("123:ab12", "x" * 31),
                               ("123:ab12", "unsafe\x1b")):
            with self.subTest(reference=reference, tag=tag), patch("sys.stderr", io.StringIO()), \
                    patch("zlib_connector.cli.load_credentials") as load, patch("zlib_connector.cli.Client") as constructor:
                self.assertEqual(main(["tags", "add", reference, tag, "--yes"]), 2)
            load.assert_not_called()
            constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
