"""No prompt reads stdin, and getpass never runs, unless the input policy allows it."""
import io
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from zlib_connector import cli_terminal
from zlib_connector.cli import main
from zlib_connector.cli_parser import parser
from zlib_connector.cli_terminal import InputPolicy, is_terminal
from zlib_connector.client import Book
from zlib_connector.credentials import Credentials
from zlib_connector.http_cli import run_server
from batch_fixture import BatchClient, book
from session_fixture import isolated_sessions


ORIGIN = "https://books.example/"
FULL = Credentials(ORIGIN, "synthetic@example.com", "synthetic-password")
EMPTY = Credentials(ORIGIN)
EMAIL_ONLY = Credentials(ORIGIN, "synthetic@example.com")
PASSWORD_ONLY = Credentials(ORIGIN, None, "synthetic-password")
BOOKS = [Book("First", "Author", "epub", ORIGIN + "book/1/a1", book_id="1", book_hash="a1"),
         Book("Second", "Author", "epub", ORIGIN + "book/2/b2", book_id="2", book_hash="b2")]


class UnreadableInput(io.StringIO):
    """A stdin that fails the test if anything reads it."""

    def readline(self, *args):
        raise AssertionError("stdin was read")

    read = readlines = readline


class Client:
    domain = ORIGIN

    def __init__(self, *args, **kwargs):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def login(self, email, password, **_):
        self.calls.append(("login", email))

    def restore_session(self, store):
        self.calls.append(("restore",))

    def search(self, *args, **kwargs):
        self.calls.append(("search",))
        return BOOKS

    def download(self, book, folder, **_):
        self.calls.append(("download", book.book_id))
        return folder

    def get_book(self, reference):
        self.calls.append(("get_book", reference))
        return Mock(title="Example")

    def set_favorite(self, reference, saved):
        self.calls.append(("set_favorite", reference))
        return Mock(to_dict=lambda: {"book_id": "123", "is_favorite": saved}, is_favorite=saved, book_id="123")

    def get_profile(self):
        raise AssertionError("signed in without credentials")


# (name, extra argv, environment, terminal): prompts allowed?
POLICIES = (
    ("no terminal", [], {}, False, False),
    ("--no-input", ["--no-input"], {}, True, False),
    ("ZLIB_NO_INPUT=1", [], {"ZLIB_NO_INPUT": "1"}, True, False),
    ("--interactive", ["--interactive"], {}, False, True),
    ("terminal", [], {}, True, True),
)


@isolated_sessions
class PromptSiteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name

    def run_cli(self, argv, *, credentials=FULL, terminal=False, environ=None, answers=None, stdin=None):
        constructor = Mock(side_effect=lambda *args, **kwargs: self.client)
        self.client = Client()
        answers = list(answers or [])

        def prompt(label):
            self.prompts.append(label)
            if not answers:
                raise AssertionError("unexpected prompt " + label)
            return answers.pop(0)

        self.prompts = []
        environment = {"ZLIB_NO_INPUT": "", "ZLIB_DEBUG": ""}
        environment.update(environ or {})
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("zlib_connector.cli.Client", constructor), \
                patch("zlib_connector.cli.load_credentials", return_value=credentials), \
                patch("zlib_connector.cli.prompt", side_effect=prompt), \
                patch("zlib_connector.cli.getpass.getpass", return_value="typed-password") as password, \
                patch("http.client.HTTPSConnection", side_effect=AssertionError("network")), \
                patch("zlib_connector.cli_terminal.is_terminal", return_value=terminal), \
                patch.dict(os.environ, environment), \
                patch("sys.stdin", stdin if stdin is not None else UnreadableInput()), \
                patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue(), constructor, password

    def refused(self, argv, *, name, code, exit_code, message, **options):
        status, out, err, constructor, password = self.run_cli([*argv, "--json"], **options)
        value = json.loads(err)
        self.assertEqual((exit_code, "", name, code, message),
                         (status, out, value["error"], value["code"], value["message"]))
        constructor.assert_not_called()
        password.assert_not_called()
        self.assertEqual([], self.prompts)
        return value

    def test_account_confirmation(self):
        for label, extra, environ, terminal, allowed in POLICIES:
            with self.subTest(policy=label):
                argv = [*extra, "favorites", "add", "123:ab12", "--no-session"]
                if allowed:
                    code, *_ = self.run_cli(argv, terminal=terminal, environ=environ, answers=["y"])
                    self.assertEqual(0, code)
                    self.assertIn(("set_favorite", "123:ab12"), self.client.calls)
                else:
                    self.refused(argv, name="ValueError", code="input_required", exit_code=2,
                                 message="Use --yes to apply an account update in non-interactive mode.",
                                 terminal=terminal, environ=environ)

    def test_result_choice(self):
        message = "Choosing a search result needs a terminal. Add --pick N, or download an ID:HASH reference."
        for label, extra, environ, terminal, allowed in POLICIES:
            if label == "--interactive":
                continue  # On download, --interactive selects the list flow (test_piped_answers_need_interactive).
            with self.subTest(policy=label):
                argv = [*extra, "download", "dune", "--no-session", "--output", self.folder]
                if allowed:
                    code, *_ = self.run_cli(argv, terminal=terminal, environ=environ, answers=["2"])
                    self.assertEqual(0, code)
                    self.assertEqual(("download", "2"), self.client.calls[-1])
                    self.assertEqual(["Choose 1-2, or q to cancel: "], self.prompts)
                else:
                    self.refused(argv, name="InputRequired", code="input_required", exit_code=2, message=message,
                                 terminal=terminal, environ=environ)

    def test_choice_loop_asks_again_and_q_or_eof_cancel(self):
        argv = ["download", "dune", "--no-session", "--output", self.folder]
        code, _, err, *_ = self.run_cli(argv, terminal=True, answers=["abc", "0", "2"])
        self.assertEqual(0, code)
        self.assertEqual(2, err.count("Type a number from 1 to 2, or q."))
        self.assertEqual(("download", "2"), self.client.calls[-1])
        code, _, err, *_ = self.run_cli(argv, terminal=True, answers=["q"])
        self.assertEqual(130, code)
        self.assertTrue(err.endswith("Cancelled.\n"), err)
        self.assertNotIn(("download", "1"), self.client.calls)
        piped = io.StringIO("")  # EOF at the prompt, read through the real line prompt.
        with patch("zlib_connector.cli_terminal.is_terminal", return_value=True), \
                patch("zlib_connector.cli.Client", return_value=Client()), \
                patch("zlib_connector.cli.load_credentials", return_value=FULL), \
                patch("http.client.HTTPSConnection", side_effect=AssertionError("network")), \
                patch("sys.stdin", piped), patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            self.assertEqual(130, main(argv))

    def test_piped_answers_need_interactive(self):
        argv = ["download", "dune", "--no-session", "--output", self.folder]
        piped = io.StringIO("1\ny\n")
        client = BatchClient({"dune": [book(1)]})
        with patch("zlib_connector.cli.Client", return_value=client), \
                patch("zlib_connector.cli.load_credentials", return_value=FULL), \
                patch("zlib_connector.cli.getpass.getpass", side_effect=AssertionError("password prompt")), \
                patch("http.client.HTTPSConnection", side_effect=AssertionError("network")), \
                patch.dict(os.environ, {"ZLIB_NO_INPUT": ""}), \
                patch("zlib_connector.cli_batch.PLANNING_PAUSE", 0), \
                patch("zlib_connector.cli_terminal.is_terminal", return_value=False), \
                patch("sys.stdin", piped), patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            self.assertEqual(2, main(argv))
            self.assertEqual("1\ny\n", piped.read())  # Nothing was read.
            piped.seek(0)
            # --interactive keeps piped answers; on download it runs the title as a one-book list.
            self.assertEqual(0, main([*argv, "--interactive", "--no-report"]))
        self.assertEqual(["1:h1"], client.kinds("download"))

    def test_signing_in_for_login(self):
        message = "Signing in needs a terminal to ask for the email and password, or both in the credentials file."
        for label, extra, environ, terminal, allowed in POLICIES:
            with self.subTest(policy=label):
                argv = [*extra, "login"]
                if allowed and terminal:
                    code, *_ = self.run_cli(argv, credentials=EMPTY, terminal=terminal, environ=environ,
                                            answers=["typed@example.com"])
                    self.assertEqual(0, code)
                    self.assertEqual(("login", "typed@example.com"), self.client.calls[-1])
                else:
                    # A password is never read from a pipe, even with --interactive.
                    self.refused(argv, name="InputRequired", code="input_required", exit_code=2, message=message,
                                 credentials=EMPTY, terminal=terminal, environ=environ)

    def test_interactive_reads_only_the_email_from_a_pipe(self):
        code, *_, password = self.run_cli(["--interactive", "auth-test"], credentials=PASSWORD_ONLY,
                                          answers=["piped@example.com"])
        self.assertEqual(0, code)
        self.assertEqual(("login", "piped@example.com"), self.client.calls[-1])
        password.assert_not_called()
        self.refused(["--interactive", "auth-test"], name="InputRequired", code="input_required", exit_code=2,
                     message="Signing in needs a terminal to ask for the email and password, or both in the"
                             " credentials file.", credentials=EMAIL_ONLY)

    def test_signing_in_for_other_account_commands(self):
        for argv in (["quota"], ["favorites"], ["download", "123:ab12", "--output", self.folder]):
            for credentials in (EMPTY, EMAIL_ONLY):
                with self.subTest(argv=argv, credentials=credentials):
                    value = self.refused([*argv, "--no-session"], name="AuthenticationError",
                                         code="upstream_authentication_required", exit_code=5,
                                         message="No saved session, and no terminal to sign in.",
                                         credentials=credentials)
                    self.assertEqual("Run zlib-cli login in a terminal first.", value["hint"])

    def test_shell(self):
        message = ("The shell needs a terminal. Use search and download in scripts, or add --interactive to read"
                   " commands from standard input.")
        for label, extra, environ, terminal, allowed in POLICIES:
            if allowed:
                continue
            with self.subTest(policy=label):
                self.refused([*extra, "shell"], name="InputRequired", code="input_required", exit_code=2,
                             message=message, terminal=terminal, environ=environ)
        code, out, *_ = self.run_cli(["--interactive", "shell", "--no-session"], stdin=io.StringIO(),
                                     answers=["quit"])
        self.assertEqual(0, code)
        self.assertIn("Commands:", out)

    def test_serve_refuses_before_binding(self):
        args = parser().parse_args(["serve", "--no-session", "--token-file", os.path.join(self.folder, "token")])
        server_factory = Mock(side_effect=AssertionError("port bound"))
        client_factory = Mock(side_effect=AssertionError("client opened"))
        password = Mock(side_effect=AssertionError("password prompt"))
        with patch("zlib_connector.cli_terminal.is_terminal", return_value=False), \
                patch.dict(os.environ, {"ZLIB_NO_INPUT": ""}), patch("sys.stdin", UnreadableInput()):
            with self.assertRaises(Exception) as caught:
                run_server(args, client_factory=client_factory, credentials_loader=lambda path: EMPTY,
                           prompt=Mock(side_effect=AssertionError("prompt")), password_prompt=password,
                           server_factory=server_factory)
        self.assertEqual("upstream_authentication_required", caught.exception.code)
        server_factory.assert_not_called()
        client_factory.assert_not_called()
        password.assert_not_called()


class TerminalTests(unittest.TestCase):
    def test_null_device_is_not_a_terminal(self):
        for mode in ("r", "w"):
            with open(os.devnull, mode) as stream:  # On Windows, isatty() is true for NUL.
                self.assertFalse(is_terminal(stream))

    def test_a_windows_stream_without_a_console_mode_is_not_a_terminal(self):
        stream = io.StringIO()
        stream.isatty = lambda: True
        with patch("zlib_connector.cli_terminal._console", return_value=False):
            self.assertFalse(is_terminal(stream))

    def test_policy_rules(self):
        args = parser().parse_args(["domains"])
        with patch("zlib_connector.cli_terminal.is_terminal", return_value=True):
            self.assertEqual(InputPolicy(True, True), InputPolicy.from_args(args, environ={}))
            self.assertEqual(InputPolicy(True, True), InputPolicy.from_args(args, environ={"ZLIB_NO_INPUT": "0"}))
            self.assertEqual(InputPolicy(False, False), InputPolicy.from_args(args, environ={"ZLIB_NO_INPUT": "yes"}))
        with patch("zlib_connector.cli_terminal.is_terminal", side_effect=lambda stream: stream is not None and
                   getattr(stream, "name", "") == "stdin"):
            stdin, stderr = Mock(), Mock()
            stdin.name, stderr.name = "stdin", "stderr"
            self.assertEqual(InputPolicy(False, False), InputPolicy.from_args(args, environ={}, stdin=stdin,
                                                                              stderr=stderr))
        interactive = parser().parse_args(["--interactive", "domains"])
        with patch("zlib_connector.cli_terminal.is_terminal", return_value=False):
            self.assertEqual(InputPolicy(True, False), InputPolicy.from_args(interactive,
                                                                             environ={"ZLIB_NO_INPUT": "1"}))

    def test_helpers_refuse_without_reading(self):
        refused = InputPolicy(False, False)
        read = Mock(side_effect=AssertionError("read"))
        for helper in (lambda: cli_terminal.ask("x", policy=refused, read=read),
                       lambda: cli_terminal.password("x", policy=InputPolicy(True, False), read=read),
                       lambda: cli_terminal.confirm("x", policy=refused, read=read),
                       lambda: cli_terminal.choose(3, policy=refused, read=read)):
            with self.assertRaises(Exception) as caught:
                helper()
            self.assertEqual("input_required", caught.exception.code)
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
