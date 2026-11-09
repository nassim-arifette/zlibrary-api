import io
import json
import unittest
from unittest.mock import patch

from zlib_connector import (
    AuthenticationError, BooklistSettings, Client, FormatChangedError,
    MutationUncertainError, NetworkError,
)
from zlib_connector.cli import main
from zlib_connector.credentials import Credentials
from zlib_connector.transport import Transport
from test_eapi import ORIGIN, QueueTransport
from test_library import LIST, transport_for
from test_transport import Connection, Reply


SETTINGS = {**LIST, "color": 3, "public": False, "description": "First line\n  Second line\t"}


class BooklistSettingsTests(unittest.TestCase):
    def test_create_defaults_to_private_and_serializes_browser_fields(self):
        transport = transport_for({"success": 1, "readlist": {**SETTINGS, "color": 0, "title": "New", "description": "A\nB"}})
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            created = client.create_booklist("New", description="A\nB")
        self.assertFalse(created.is_public)
        self.assertEqual(created.booklist_id, "42")
        self.assertEqual(transport.calls[2][0], ORIGIN + "papi/booklist/create")
        self.assertIsNone(transport.calls[2][1])
        self.assertEqual(transport.json_bodies[2], {"title": "New", "description": "A\nB", "private": "on", "color": "0"})

    def test_public_creation_requires_an_explicit_boolean(self):
        transport = transport_for({"success": 1, "readlist": {**SETTINGS, "public": True, "title": "Public", "description": "", "color": 5}})
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            self.assertTrue(client.create_booklist("Public", is_public=True, color=5).is_public)
        self.assertNotIn("private", transport.json_bodies[2])
        self.assertEqual(transport.json_bodies[2]["color"], "5")

    def test_rename_preserves_exact_description_color_and_private_state(self):
        transport = transport_for({"success": 1, "list": [SETTINGS]},
                                  {"success": 1, "readlist": {**SETTINGS, "title": "New title"}})
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            updated = client.update_booklist(42, title="New title")
        self.assertEqual(updated.title, "New title")
        self.assertEqual(transport.calls[2][0], ORIGIN + "papi/booklist/current-user/0")
        self.assertEqual(transport.calls[3][0], ORIGIN + "papi/booklist/42/edit")
        self.assertEqual(transport.json_bodies[3], {
            "title": "New title", "description": SETTINGS["description"], "color": "3", "private": "on",
        })

    def test_edit_can_clear_a_description_and_explicitly_change_visibility(self):
        for public in (True, False):
            current = {**SETTINGS, "public": not public}
            changed = {**SETTINGS, "public": public, "description": "", "color": 0}
            transport = transport_for({"success": 1, "list": [current]}, {"success": 1, "readlist": changed})
            with self.subTest(public=public), Client(ORIGIN, transport=transport) as client:
                client.login("synthetic", "synthetic")
                result = client.update_booklist(42, description="", is_public=public, color=0)
            body = transport.json_bodies[3]
            self.assertEqual(body["description"], "")
            self.assertEqual(body["title"], SETTINGS["title"])
            self.assertEqual(body["color"], "0")
            self.assertEqual("private" in body, not public)
            self.assertEqual(result.is_public, public)

    def test_updates_require_login(self):
        transport = QueueTransport([])
        with Client(ORIGIN, transport=transport) as client:
            for method, args in ((client.create_booklist, ("New",)), (client.update_booklist, (42,)),
                                 (client.delete_booklist, (42,))):
                with self.subTest(method=method.__name__), self.assertRaises(AuthenticationError):
                    method(*args)
        self.assertEqual(transport.calls, [])

    def test_invalid_settings_fail_before_any_read_or_write(self):
        invalid = ({"title": ""}, {"title": " "}, {"title": "x" * 121}, {"title": "hidden\x1b"},
                   {"description": "x" * 1001}, {"description": 12}, {"is_public": 1},
                   {"is_public": "false"}, {"color": True}, {"color": 6}, {})
        transport = transport_for()
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            for changes in invalid:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    client.update_booklist(42, **changes)
            with self.assertRaises(ValueError):
                client.create_booklist(" ")
            with self.assertRaises(ValueError):
                client.delete_booklist("../delete")
        self.assertEqual(len(transport.calls), 2)

    def test_missing_current_settings_or_list_never_trigger_a_write(self):
        cases = [([], ValueError)]
        for field in ("title", "description", "color", "public"):
            incomplete = {key: value for key, value in SETTINGS.items() if key != field}
            cases.append(([incomplete], FormatChangedError))
        for rows, error in cases:
            transport = transport_for({"success": 1, "list": rows})
            with self.subTest(rows=rows), Client(ORIGIN, transport=transport) as client:
                client.login("synthetic", "synthetic")
                # Color is intentionally not supplied so every other preserved setting must be known.
                changes = {"title": "New"} if not rows or "title" in rows[0] else {"description": "New"}
                with self.assertRaises(error):
                    client.update_booklist(42, **changes)
            self.assertEqual(len(transport.calls), 3)
            self.assertTrue(all(body is None for body in transport.json_bodies))

    def test_changed_or_unreadable_results_are_uncertain_not_success(self):
        expected = {**SETTINGS, "title": "New", "description": "", "color": 0}
        bad = (None, {}, {**expected, "id": "invalid"}, {**expected, "public": True},
               {**expected, "public": None}, {**expected, "color": "unknown"},
               {**expected, "title": "Old"}, {**expected, "description": "Unrequested"}, {**expected, "color": 2})
        for row in bad:
            transport = transport_for({"success": 1, "readlist": row})
            with self.subTest(row=row), Client(ORIGIN, transport=transport) as client:
                client.login("synthetic", "synthetic")
                with self.assertRaises(MutationUncertainError):
                    client.create_booklist("New")
            self.assertEqual(len(transport.calls), 3)
        transport = transport_for({"success": 1, "list": [SETTINGS]},
                                  {"success": 1, "readlist": {**SETTINGS, "id": 456}})
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            with self.assertRaises(MutationUncertainError):
                client.update_booklist(42, title="New")
        self.assertEqual(len(transport.calls), 4)

    def test_delete_targets_only_the_remote_list_and_requires_confirmation_response(self):
        transport = transport_for({"success": 1})
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            result = client.delete_booklist(42)
        self.assertEqual(result.to_dict(), {"booklist_id": "42", "deleted": True})
        self.assertEqual(transport.calls[2][0], ORIGIN + "papi/booklist/42/delete")
        transport = transport_for(b'not-json')
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            with self.assertRaises(MutationUncertainError):
                client.delete_booklist(42)
        self.assertEqual(len(transport.calls), 3)


class BooklistSettingsCliTests(unittest.TestCase):
    def test_create_edit_and_delete_return_json(self):
        cases = ((["create", "New"], [{"success": 1, "readlist": {**SETTINGS, "title": "New", "description": "", "color": 0}}]),
                 (["edit", "42", "--title", "New"], [{"success": 1, "list": [SETTINGS]}, {"success": 1, "readlist": {**SETTINGS, "title": "New"}}]),
                 (["delete", "42"], [{"success": 1}]))
        for arguments, responses in cases:
            transport = transport_for(*responses)
            with self.subTest(arguments=arguments), patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                    patch("zlib_connector.cli.load_credentials", return_value=Credentials(ORIGIN, "synthetic", "synthetic")), \
                    patch("sys.stdout", io.StringIO()) as output:
                self.assertEqual(main(["booklists", *arguments, "--yes", "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue())["booklist_id"], "42")

    def test_invalid_or_noninteractive_commands_fail_before_credentials(self):
        cases = (["create", "New"], ["edit", "42", "--title", "New"], ["delete", "42"],
                 ["edit", "42", "--yes"], ["create", "", "--yes"], ["delete", "../delete", "--yes"])
        for arguments in cases:
            with self.subTest(arguments=arguments), patch("sys.stdin", io.StringIO()), patch("sys.stderr", io.StringIO()), \
                    patch("zlib_connector.cli.load_credentials") as load:
                self.assertEqual(main(["booklists", *arguments]), 1)
            load.assert_not_called()

    def test_declining_each_change_prevents_login(self):
        cases = (["create", "New", "--public"], ["edit", "42", "--description", "A\nB"], ["delete", "42"])
        for arguments in cases:
            transport = QueueTransport([])
            input_stream = io.StringIO("n\n")
            input_stream.isatty = lambda: True
            with self.subTest(arguments=arguments), patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                    patch("zlib_connector.cli.load_credentials", return_value=Credentials(ORIGIN, "synthetic", "synthetic")), \
                    patch("sys.stdin", input_stream), patch("sys.stderr", io.StringIO()) as output:
                self.assertEqual(main(["booklists", *arguments]), 130)
            self.assertEqual(transport.calls, [])
            if arguments[0] == "create":
                self.assertIn('"is_public": true', output.getvalue())
            elif arguments[0] == "edit":
                self.assertIn('"description": "A\\nB"', output.getvalue())
            else:
                self.assertIn("Permanently delete booklist 42", output.getvalue())


class JsonRequestTests(unittest.TestCase):
    def test_json_request_uses_the_browser_encoding_and_keeps_unicode(self):
        connection = Connection(Reply())
        with patch("zlib_connector.transport.public_addresses", return_value=["8.8.8.8"]), \
                patch("http.client.HTTPSConnection", return_value=connection):
            Transport().request(ORIGIN + "papi/booklist/create", json_body={"title": "Été", "private": "on"}, follow_redirects=False)
        method, _, body, headers = connection.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(json.loads(body), {"title": "Été", "private": "on"})
        self.assertIn("Été".encode(), body)
        self.assertEqual({key.lower(): value for key, value in headers.items()}["content-type"], "text/plain;charset=UTF-8")

    def test_json_mutation_redirect_is_never_followed(self):
        login = Reply(data=b'{"response":{}}', headers=[
            ("Set-Cookie", "remix_userkey=synthetic; Secure; Path=/"),
            ("Set-Cookie", "remix_userid=123; Secure; Path=/"),
        ])
        connections = [Connection(login), Connection(Reply(data=b'{"success":1,"user":{}}')),
                       Connection(Reply(307, headers=[("Location", ORIGIN + "again")]))]
        with patch("zlib_connector.transport.public_addresses", return_value=["8.8.8.8"]), \
                patch("http.client.HTTPSConnection", side_effect=connections) as constructor:
            with Client(ORIGIN) as client:
                client.login("synthetic", "synthetic")
                with self.assertRaises(MutationUncertainError):
                    client.create_booklist("New")
        self.assertEqual(constructor.call_count, 3)
        self.assertTrue(all(connection.closed for connection in connections))

    def test_json_body_cannot_cross_an_origin_even_when_redirects_are_allowed(self):
        connection = Connection(Reply(307, headers=[("Location", "https://other.example/create")]))
        with patch("zlib_connector.transport.public_addresses", return_value=["8.8.8.8"]), \
                patch("http.client.HTTPSConnection", return_value=connection) as constructor, self.assertRaises(NetworkError):
            Transport().request(ORIGIN + "papi/booklist/create", json_body={"title": "Synthetic"})
        constructor.assert_called_once()

    def test_conflicting_body_types_fail_before_network_access(self):
        with patch("zlib_connector.transport.public_addresses") as resolve, self.assertRaises(ValueError):
            Transport().request(ORIGIN, form={}, json_body={})
        resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
