"""CLI session lifecycle using synthetic cookies and mocked HTTPS connections."""

import io
import json
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

from zlib_connector.cli import main
from zlib_connector.credentials import Credentials
from zlib_connector.private_files import create_private_file
from zlib_connector.session_data import SavedSession, SessionCookie
from zlib_connector.sessions import SessionStore
from session_fixture import isolated_sessions
from test_transport import Connection, Reply


ORIGIN = "https://books.example/"
SECRET = "synthetic-cookie-secret"
CREDENTIALS = Credentials(ORIGIN, "synthetic@example.com", "synthetic-password")


def profile_reply():
    return Reply(data=b'{"success":1,"user":{}}')


def login_reply():
    return Reply(data=b'{"response":{}}', headers=[
        ("Set-Cookie", "remix_userid=123; Secure; Path=/"),
        ("Set-Cookie", "remix_userkey=" + SECRET + "; Secure; Path=/"),
    ])


@isolated_sessions
class CliSessionTests(unittest.TestCase):
    def store(self):
        return SessionStore()

    def saved(self, *, expired=False):
        now = int(time.time())
        expiry = now - 1 if expired else now + 3600
        session = SavedSession(ORIGIN, (SessionCookie("remix_userid", "123", expires=expiry),
                                       SessionCookie("remix_userkey", SECRET, expires=expiry)), saved_at=now)
        return self.store().save(session, expected_generation=None)

    def run_cli(self, args, *, replies=(), credentials=False, input_stream=None):
        connections = [Connection(reply) for reply in replies]
        supplied = CREDENTIALS if credentials is True else credentials
        loader = Mock(return_value=supplied) if supplied else Mock(side_effect=AssertionError("Credentials were read"))
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("zlib_connector.cli.load_credentials", loader), \
                patch("zlib_connector.cli.prompt", return_value="n") as prompt, \
                patch("zlib_connector.cli.getpass.getpass", side_effect=AssertionError("Password was prompted")), \
                patch("zlib_connector.transport.public_addresses", return_value=["8.8.8.8"]), \
                patch("http.client.HTTPSConnection", side_effect=connections) as network, \
                patch("sys.stdin", input_stream or io.StringIO()), \
                patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main(args)
        self.assertEqual(network.call_count, len(connections))
        self.assertTrue(all(connection.closed for connection in connections))
        for secret in (SECRET, CREDENTIALS.email, CREDENTIALS.password):
            self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())
        return code, stdout.getvalue(), stderr.getvalue(), connections, loader, prompt

    def test_status_without_session_does_not_create_a_directory_or_load_credentials(self):
        code, output, errors, _, _, _ = self.run_cli(["status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertEqual(json.loads(output), {"state": "empty", "domain": None, "saved_at": None,
                                              "expires_at": None, "verified": False})
        self.assertFalse(self._isolated_session_state.exists())

    def test_local_status_and_logout_do_not_connect_or_disclose_the_saved_cookies(self):
        snapshot = self.saved()
        code, output, _, _, _, _ = self.run_cli(["status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), snapshot.status())
        code, output, _, _, _, _ = self.run_cli(["logout", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["state"], "empty")
        cleared = self.store().read()
        self.assertIsNone(cleared.session)
        self.assertNotEqual(cleared.generation, snapshot.generation)

    def test_verify_confirms_the_saved_session_remotely_without_password_login_or_writing(self):
        snapshot = self.saved()
        code, output, errors, connections, _, _ = self.run_cli(
            ["status", "--verify", "--json"], replies=[profile_reply()])
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertEqual(json.loads(output), {**snapshot.status(), "verified": True, "authenticated": True})
        self.assertEqual(connections[0].calls[0][1], "/eapi/user/profile")
        self.assertEqual(self.store().read(), snapshot)

    def test_normal_command_restores_then_reads_account_history_without_loading_credentials(self):
        snapshot = self.saved()
        code, output, errors, connections, _, _ = self.run_cli(
            ["history", "--json"], replies=[profile_reply(), Reply(data=b'{"success":1,"books":[]}')])
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertEqual(json.loads(output)["books"], [])
        self.assertEqual(connections[0].calls[0][1], "/eapi/user/profile")
        self.assertTrue(connections[1].calls[0][1].startswith("/eapi/user/book/downloaded?"))
        self.assertEqual(self.store().read(), snapshot)

    def test_expired_session_is_reported_locally_and_never_falls_back_to_credentials(self):
        snapshot = self.saved(expired=True)
        code, output, _, _, _, _ = self.run_cli(["status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["state"], "expired")
        for args in (["status", "--verify", "--json"], ["history", "--json"]):
            with self.subTest(args=args):
                code, output, errors, _, _, prompt = self.run_cli(args)
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertEqual(json.loads(errors)["error"], "AuthenticationError")
                prompt.assert_not_called()
        self.assertEqual(self.store().read(), snapshot)

    def test_server_rejection_does_not_retry_or_fall_back_to_password_authentication(self):
        self.saved()
        code, output, errors, connections, _, prompt = self.run_cli(
            ["history", "--json"], replies=[Reply(data=b'{"success":0,"error":"authorization synthetic-cookie-secret"}')])
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertEqual(json.loads(errors)["error"], "AuthenticationError")
        self.assertEqual(len(connections), 1)
        prompt.assert_not_called()

    def test_corrupt_session_is_an_explicit_error_without_password_fallback(self):
        self._isolated_session_state.mkdir()
        with create_private_file(self._isolated_session_state / "session.json") as target:
            target.write(b'{invalid synthetic-cookie-secret')
        code, output, errors, _, _, prompt = self.run_cli(["history", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertTrue(json.loads(errors)["error"])
        prompt.assert_not_called()

    def test_domain_mismatch_does_not_send_cookies_or_forget_another_domains_session(self):
        snapshot = self.saved()
        for command in ("history", "status", "logout"):
            with self.subTest(command=command):
                code, output, errors, _, _, _ = self.run_cli(
                    ["--domain", "https://other.example/", command, "--json"])
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertEqual(json.loads(errors)["error"], "ValueError")
        self.assertEqual(self.store().read(), snapshot)

    def test_explicit_credentials_domain_takes_precedence_and_explicit_domain_can_override_it(self):
        snapshot = self.saved()
        other = Credentials("https://configured.example/", "synthetic", "synthetic")
        code, _, errors, _, loader, _ = self.run_cli(
            ["--credentials", "synthetic.toml", "history", "--json"], credentials=other)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(errors)["error"], "ValueError")
        loader.assert_called_once_with(Path("synthetic.toml"))
        code, output, errors, connections, loader, _ = self.run_cli(
            ["--credentials", "synthetic.toml", "--domain", ORIGIN, "history", "--json"], credentials=other,
            replies=[profile_reply(), Reply(data=b'{"success":1,"books":[]}')])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["books"], [])
        self.assertEqual(errors, "")
        self.assertEqual(connections[0].calls[0][1], "/eapi/user/profile")
        self.assertEqual(self.store().read(), snapshot)

    def test_explicit_session_file_is_used_without_reading_or_creating_the_default_path(self):
        session = SavedSession(ORIGIN, (SessionCookie("remix_userid", "123"),
                                       SessionCookie("remix_userkey", SECRET)), saved_at=int(time.time()))
        custom = self._isolated_session_state.parent / "custom-session.json"
        snapshot = SessionStore(custom).save(session, expected_generation=None)
        code, output, errors, _, _, _ = self.run_cli(["--session-file", str(custom), "status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), snapshot.status())
        self.assertEqual(errors, "")
        self.assertFalse(self._isolated_session_state.exists())

    def test_explicit_login_persists_a_confirmed_session_and_returns_only_safe_json(self):
        code, output, errors, connections, loader, _ = self.run_cli(
            ["login", "--json"], replies=[login_reply(), profile_reply()], credentials=True)
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertEqual(json.loads(output), {"authenticated": True, "stored": True, "domain": ORIGIN})
        self.assertEqual([connection.calls[0][1] for connection in connections], ["/rpc.php", "/eapi/user/profile"])
        loader.assert_called_once_with(None)
        snapshot = self.store().read()
        self.assertEqual(snapshot.session.origin, ORIGIN)
        self.assertEqual({cookie.name: cookie.value for cookie in snapshot.session.cookies},
                         {"remix_userid": "123", "remix_userkey": SECRET})
        contents = (self._isolated_session_state / "session.json").read_text()
        self.assertNotIn(CREDENTIALS.email, contents)
        self.assertNotIn(CREDENTIALS.password, contents)

    def test_explicit_login_replaces_saved_session_using_credentials_instead_of_restoring(self):
        previous = self.saved()
        code, _, _, connections, loader, _ = self.run_cli(
            ["login", "--json"], replies=[login_reply(), profile_reply()], credentials=True)
        self.assertEqual(code, 0)
        self.assertEqual(connections[0].calls[0][1], "/rpc.php")
        self.assertNotEqual(self.store().read().generation, previous.generation)
        loader.assert_called_once_with(None)

    def test_no_session_and_auth_test_bypass_all_session_reads_and_writes(self):
        for args, replies in (
            (["--no-session", "history", "--json"], [login_reply(), profile_reply(), Reply(data=b'{"success":1,"books":[]}')]),
            (["auth-test"], [login_reply(), profile_reply()]),
        ):
            with self.subTest(args=args), patch("zlib_connector.cli_session.SessionStore", side_effect=AssertionError("Session store was used")):
                code, _, errors, connections, loader, _ = self.run_cli(args, replies=replies, credentials=True)
            self.assertEqual(code, 0)
            self.assertEqual(errors, "")
            self.assertEqual(connections[0].calls[0][1], "/rpc.php")
            loader.assert_called_once_with(None)
            self.assertFalse(self._isolated_session_state.exists())

    def test_anonymous_search_bypasses_credentials_and_session_state(self):
        with patch("zlib_connector.cli_session.SessionStore", side_effect=AssertionError("Session store was used")):
            code, output, errors, _, _, _ = self.run_cli(
                ["--domain", ORIGIN, "search", "Example", "--anonymous", "--json"],
                replies=[Reply(data=b'{"success":1,"books":[]}')])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), [])
        self.assertEqual(errors, "")
        self.assertFalse(self._isolated_session_state.exists())

    def test_no_session_is_rejected_for_commands_that_manage_persistent_sessions(self):
        for command in ("login", "logout", "status"):
            with self.subTest(command=command):
                code, output, errors, _, _, _ = self.run_cli(["--no-session", command, "--json"])
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertEqual(json.loads(errors)["error"], "ValueError")
        self.assertFalse(self._isolated_session_state.exists())

    def test_declined_account_mutation_does_not_even_verify_a_saved_session(self):
        snapshot = self.saved()
        input_stream = io.StringIO()
        input_stream.isatty = lambda: True
        code, output, _, _, _, prompt = self.run_cli(
            ["favorites", "remove", "123:ab12"], input_stream=input_stream)
        self.assertEqual(code, 130)
        self.assertEqual(output, "")
        prompt.assert_called_once()
        self.assertEqual(self.store().read(), snapshot)


if __name__ == "__main__":
    unittest.main()
