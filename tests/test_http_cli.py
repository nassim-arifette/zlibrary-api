"""Serve lifecycle and secret handling with local temporary token files."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from zlib_connector.credentials import Credentials
from zlib_connector.errors import AuthenticationError
from zlib_connector.http_cli import add_serve_parser, run_server
from zlib_connector.session_data import SavedSession, SessionCookie
from zlib_connector.sessions import SessionStore
from session_fixture import isolated_sessions


def options(*values):
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain")
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--timeout", type=float, default=30)
    sessions = parser.add_mutually_exclusive_group()
    sessions.add_argument("--session-file", type=Path)
    sessions.add_argument("--no-session", action="store_true")
    add_serve_parser(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(values)


class FakeClient:
    def __init__(self, events, *, login_error=None, restore_error=None):
        self.events = events
        self.login_error = login_error
        self.restore_error = restore_error
        self.logins = []
        self.restores = []

    def __enter__(self):
        self.events.append("client_enter")
        return self

    def __exit__(self, *_):
        self.events.append("client_close")

    def login(self, email, password):
        self.events.append("login")
        self.logins.append((email, password))
        if self.login_error:
            raise self.login_error

    def restore_session(self, store):
        self.events.append("restore")
        self.restores.append(store)
        if self.restore_error:
            raise self.restore_error


class FakeServer:
    server_address = ("127.0.0.1", 43123)

    def __init__(self, events, *, serve_error=None):
        self.events = events
        self.serve_error = serve_error

    def __enter__(self):
        self.events.append("server_enter")
        return self

    def __exit__(self, *_):
        self.events.append("server_close")

    def serve_forever(self):
        self.events.append("serve")
        if self.serve_error:
            raise self.serve_error

    def shutdown(self):
        raise AssertionError("Never call shutdown from the serve_forever thread.")


@isolated_sessions
class HttpCliTests(unittest.TestCase):
    def save_session(self):
        session = SavedSession("https://saved.example/", (
            SessionCookie("remix_userid", "123"), SessionCookie("remix_userkey", "synthetic-secret")),
            saved_at=int(time.time()))
        return SessionStore().save(session, expected_generation=None)

    def test_saved_session_verification_happens_after_bind_without_credentials_or_prompts(self):
        snapshot = self.save_session()
        events = []
        client = FakeClient(events)
        factory = Mock(return_value=client)
        loader, prompt, password = (Mock(side_effect=AssertionError) for _ in range(3))

        def bind(*args, **kwargs):
            events.append("bind")
            return FakeServer(events)

        args = options("serve")
        with redirect_stderr(io.StringIO()):
            run_server(args, client_factory=factory, credentials_loader=loader, prompt=prompt,
                       password_prompt=password, server_factory=bind)
        factory.assert_called_once_with("https://saved.example/", timeout=30)
        self.assertEqual(events, ["client_enter", "bind", "server_enter", "restore", "serve", "server_close", "client_close"])
        self.assertEqual(client.logins, [])
        self.assertEqual(client.restores[0].read(), snapshot)
        for callback in (loader, prompt, password):
            callback.assert_not_called()

    def test_bind_failure_does_not_verify_or_log_in_even_when_a_session_exists(self):
        self.save_session()
        events = []
        client = FakeClient(events)
        loader = Mock(side_effect=AssertionError)
        with self.assertRaises(OSError):
            run_server(options("serve"), client_factory=lambda *args, **kwargs: client, credentials_loader=loader,
                       server_factory=Mock(side_effect=OSError("address already in use")))
        self.assertEqual(events, ["client_enter", "client_close"])
        self.assertEqual(client.restores, [])
        self.assertEqual(client.logins, [])
        loader.assert_not_called()

    def test_rejected_saved_session_closes_server_without_fallback_login_or_serve(self):
        self.save_session()
        events = []
        client = FakeClient(events, restore_error=AuthenticationError("Synthetic expired session"))
        loader, prompt, password = (Mock(side_effect=AssertionError) for _ in range(3))
        with self.assertRaises(AuthenticationError):
            run_server(options("serve"), client_factory=lambda *args, **kwargs: client, credentials_loader=loader,
                       prompt=prompt, password_prompt=password,
                       server_factory=lambda *args, **kwargs: FakeServer(events))
        self.assertEqual(events, ["client_enter", "server_enter", "restore", "server_close", "client_close"])
        self.assertEqual(client.logins, [])
        for callback in (loader, prompt, password):
            callback.assert_not_called()

    def test_anonymous_and_no_session_launcher_do_not_open_the_session_store(self):
        for flags in (("serve", "--anonymous"), ("--no-session", "serve")):
            events = []
            loader = Mock(return_value=Credentials(email="synthetic", password="synthetic"))
            with self.subTest(flags=flags), redirect_stderr(io.StringIO()), \
                    patch("zlib_connector.cli_session.SessionStore", side_effect=AssertionError("Session store was opened")):
                run_server(options(*flags), client_factory=lambda *args, **kwargs: FakeClient(events),
                           credentials_loader=loader, server_factory=lambda *args, **kwargs: FakeServer(events))
            self.assertNotIn("restore", events)
            if "--anonymous" in flags:
                loader.assert_not_called()
                self.assertNotIn("login", events)
            else:
                loader.assert_called_once_with(None)
                self.assertIn("login", events)

    def test_parser_defaults_and_explicit_options(self):
        args = options("serve")
        self.assertEqual(("127.0.0.1", 8000, [], False), (args.host, args.port, args.allow_origin, args.anonymous))
        args = options("serve", "--host", "localhost", "--port", "0", "--allow-origin", "http://localhost:9000", "--token-file", "private-token", "--anonymous")
        self.assertEqual(("localhost", 0, ["http://localhost:9000"], Path("private-token"), True),
                         (args.host, args.port, args.allow_origin, args.token_file, args.anonymous))

    def test_authenticated_lifecycle_and_no_secret_output(self):
        events = []
        client = FakeClient(events)
        server = FakeServer(events)

        def bind(*args, **kwargs):
            events.append("bind")
            return server

        factory = Mock(return_value=client)
        server_factory = Mock(side_effect=bind)
        credentials = Credentials("https://example.org/", "reader@example.org", "private-password")
        loader = Mock(return_value=credentials)
        with tempfile.TemporaryDirectory() as folder:
            token_path = Path(folder) / "api-token"
            args = options("--credentials", str(Path(folder) / "account.toml"), "serve", "--token-file", str(token_path))
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                run_server(args, client_factory=factory, server_factory=server_factory, credentials_loader=loader)
            token = token_path.read_text().strip()
            self.assertGreaterEqual(len(token), 32)
            self.assertEqual(token, server_factory.call_args.kwargs["token"])
            self.assertNotIn(token, output.getvalue() + errors.getvalue())
            self.assertNotIn(credentials.email, output.getvalue() + errors.getvalue())
            self.assertNotIn(credentials.password, output.getvalue() + errors.getvalue())
            self.assertIn(str(token_path), errors.getvalue())
            self.assertIn("http://127.0.0.1:43123/v1", errors.getvalue())
            self.assertEqual("", output.getvalue())
            self.assertEqual([("reader@example.org", "private-password")], client.logins)
            factory.assert_called_once_with("https://example.org/", timeout=30)
            self.assertEqual(["client_enter", "bind", "server_enter", "login", "serve", "server_close", "client_close"], events)

    def test_anonymous_does_not_load_or_prompt_for_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            events = []
            factory = Mock(return_value=FakeClient(events))
            loader, prompt, password = Mock(side_effect=AssertionError), Mock(side_effect=AssertionError), Mock(side_effect=AssertionError)
            args = options("--domain", "https://anonymous.example/", "serve", "--anonymous", "--token-file", str(Path(folder) / "token"))
            with redirect_stderr(io.StringIO()):
                run_server(args, client_factory=factory, credentials_loader=loader, prompt=prompt,
                           password_prompt=password, server_factory=lambda *args, **kwargs: FakeServer(events))
            factory.assert_called_once_with("https://anonymous.example/", timeout=30)
            self.assertNotIn("login", events)
            for callback in (loader, prompt, password):
                callback.assert_not_called()

    def test_prompting_happens_after_bind_and_honors_domain_override(self):
        with tempfile.TemporaryDirectory() as folder:
            events = []
            client = FakeClient(events)
            factory = Mock(return_value=client)

            def prompt(label):
                self.assertIn("server_enter", events)
                return "reader@example.org"

            def password(label):
                self.assertIn("server_enter", events)
                return "test-password"

            args = options("--domain", "https://override.example/", "serve", "--token-file", str(Path(folder) / "token"))
            with redirect_stderr(io.StringIO()):
                run_server(args, client_factory=factory, credentials_loader=lambda path: Credentials("https://configured.example/"),
                           prompt=prompt, password_prompt=password, server_factory=lambda *args, **kwargs: FakeServer(events))
            factory.assert_called_once_with("https://override.example/", timeout=30)
            self.assertEqual([("reader@example.org", "test-password")], client.logins)

    def test_invalid_configuration_does_not_read_credentials_or_create_token(self):
        with tempfile.TemporaryDirectory() as folder:
            token_path = Path(folder) / "token"
            for flags in (("--host", "0.0.0.0"), ("--host", "::1"), ("--port", "65536"), ("--allow-origin", "*")):
                with self.subTest(flags=flags):
                    args = options("serve", "--token-file", str(token_path), *flags)
                    loader = Mock(side_effect=AssertionError)
                    with self.assertRaises(ValueError):
                        run_server(args, credentials_loader=loader)
                    loader.assert_not_called()
                    self.assertFalse(token_path.exists())

    def test_bind_failure_closes_client_without_prompt_or_login(self):
        with tempfile.TemporaryDirectory() as folder:
            events = []
            client = FakeClient(events)
            args = options("serve", "--token-file", str(Path(folder) / "token"))
            prompt, password = Mock(side_effect=AssertionError), Mock(side_effect=AssertionError)
            with self.assertRaises(OSError):
                run_server(args, client_factory=lambda *args, **kwargs: client,
                           credentials_loader=lambda path: Credentials(), prompt=prompt, password_prompt=password,
                           server_factory=Mock(side_effect=OSError("address already in use")))
            self.assertEqual(["client_enter", "client_close"], events)
            self.assertEqual([], client.logins)
            prompt.assert_not_called()
            password.assert_not_called()

    def test_login_error_and_interrupt_close_server_before_client(self):
        with tempfile.TemporaryDirectory() as folder:
            args = options("serve", "--token-file", str(Path(folder) / "token"))
            for login_error, serve_error, expected in ((AuthenticationError("login failed"), None, AuthenticationError),
                                                       (None, KeyboardInterrupt(), KeyboardInterrupt)):
                events = []
                client = FakeClient(events, login_error=login_error)
                server = FakeServer(events, serve_error=serve_error)
                with self.subTest(expected=expected), redirect_stderr(io.StringIO()), self.assertRaises(expected):
                    run_server(args, client_factory=lambda *args, **kwargs: client,
                               credentials_loader=lambda path: Credentials(email="reader@example.org", password="test-password"),
                               server_factory=lambda *args, **kwargs: server)
                self.assertEqual(["server_close", "client_close"], events[-2:])
                if login_error:
                    self.assertNotIn("serve", events)

    def test_default_state_token_is_reused(self):
        with tempfile.TemporaryDirectory() as folder, patch("zlib_connector.http_cli.state_directory", return_value=Path(folder)):
            args = options("serve", "--anonymous")
            tokens = []

            def bind(*args, **kwargs):
                tokens.append(kwargs["token"])
                return FakeServer([])

            with redirect_stderr(io.StringIO()):
                for _ in range(2):
                    run_server(args, client_factory=lambda *args, **kwargs: FakeClient([]), server_factory=bind)
            self.assertTrue((Path(folder) / "api-token").is_file())
            self.assertEqual(tokens[0], tokens[1])

    def test_default_prompts_write_to_stderr(self):
        with tempfile.TemporaryDirectory() as folder:
            args = options("serve", "--token-file", str(Path(folder) / "token"))
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors), patch("sys.stdin", io.StringIO("reader@example.org\n")), \
                    patch("zlib_connector.http_cli.getpass.getpass", return_value="test-password") as password:
                run_server(args, client_factory=lambda *args, **kwargs: FakeClient([]), credentials_loader=lambda path: Credentials(),
                           server_factory=lambda *args, **kwargs: FakeServer([]))
                self.assertIs(errors, password.call_args.kwargs["stream"])
            self.assertIn("Email: ", errors.getvalue())
            self.assertNotIn("reader@example.org", errors.getvalue())
            self.assertEqual("", output.getvalue())


if __name__ == "__main__":
    unittest.main()
