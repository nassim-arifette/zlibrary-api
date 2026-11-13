"""The CLI error contract: one error object, stable codes, documented exit codes (docs/cli-json.md)."""
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from zlib_connector import cli_output
from zlib_connector.cli import main
from zlib_connector.cli_output import EXIT_CODES, QUOTA_HINT, describe, exit_code_for, print_json
from zlib_connector.credentials import Credentials
from zlib_connector.errors import (AuthenticationError, BooklistNotFoundError, ChallengeRequired, ConnectorError,
                                   DownloadError, FormatChangedError, NetworkError, RateLimitError)
from zlib_connector.sessions import SessionConflictError, SessionLockTimeoutError
from zlib_connector.transfer import TransferCancelled


ORIGIN = "https://books.example/"
CREDENTIALS = Credentials(ORIGIN, "synthetic@example.com", "synthetic-password")
KEYS = {"error", "message", "code", "exit_code", "retryable", "outcome"}


def staged(error, stage):
    error.download_stage = stage
    return error


class FakeClient:
    """Signs in without a network; the chosen method raises the chosen exception."""

    domain = ORIGIN

    def __init__(self, failures=None):
        self.failures = failures or {}
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def login(self, email, password, **_):
        self.calls.append("login")

    def restore_session(self, store):
        self.calls.append("restore")

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append(name)
            if name in self.failures:
                raise self.failures[name]
            raise AssertionError(f"unexpected call {name}")
        return call


class Searching(FakeClient):
    def __init__(self, books, failures=None):
        super().__init__(failures)
        self.books = books

    def search(self, *args, **kwargs):
        self.calls.append("search")
        return self.books


class ContractTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name

    def run_cli(self, argv, client=None, *, terminal=False, answers=None, environ=None):
        client = FakeClient() if client is None else client
        stdout, stderr = io.StringIO(), io.StringIO()
        environment = {"ZLIB_DEBUG": "", "ZLIB_NO_INPUT": ""}
        environment.update(environ or {})
        answers = list(answers or [])

        def prompt(label):
            if not answers:
                raise AssertionError("unexpected prompt")
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        with patch("zlib_connector.cli.Client", return_value=client), \
                patch("zlib_connector.cli.load_credentials", return_value=CREDENTIALS), \
                patch("zlib_connector.cli.prompt", side_effect=prompt), \
                patch("zlib_connector.cli.getpass.getpass", side_effect=AssertionError("password prompt")), \
                patch("http.client.HTTPSConnection", side_effect=AssertionError("network")), \
                patch("zlib_connector.cli_terminal.is_terminal", return_value=terminal), \
                patch.dict(os.environ, environment), \
                patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main(argv)
        for secret in (CREDENTIALS.email, CREDENTIALS.password):
            self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())
        return code, stdout.getvalue(), stderr.getvalue()

    def error_object(self, argv, client=None, **options):
        code, out, err = self.run_cli(argv, client, **options)
        self.assertEqual("", out)
        lines = err.splitlines()
        self.assertEqual(1, len(lines), err)
        value = json.loads(lines[0])
        self.assertTrue(KEYS <= set(value), value)
        self.assertEqual(code, value["exit_code"])
        self.assertTrue(lines[0].isascii())
        return value


class ReadErrorTests(ContractTestCase):
    def test_each_read_error_has_its_code_exit_and_hint(self):
        cases = [
            (AuthenticationError("Login failed."), "upstream_authentication_required", 5, False, "Run: zlib-cli login"),
            (ChallengeRequired("Verify."), "upstream_challenge", 1, False, "Run: zlib-cli doctor"),
            (RateLimitError("Limit."), "upstream_rate_limited", 3, True, "Check: zlib-cli quota"),
            (NetworkError("The request timed out.", reason="timeout"), "upstream_timeout", 3, True, None),
            (NetworkError("No route.", reason="dns"), "upstream_network_error", 3, True, "Run: zlib-cli doctor"),
            (FormatChangedError("Changed."), "upstream_protocol_changed", 1, False, None),
            (DownloadError("Refused.", reason="refused"), "upstream_refused", 1, False, None),
            (ConnectorError("Failed."), "upstream_error", 1, False, None),
            (SessionLockTimeoutError("Busy."), "session_busy", 3, True, "Try again in a moment."),
            (SessionConflictError("Changed."), "session_conflict", 4, False, "Run: zlib-cli status"),
            (BooklistNotFoundError("Missing."), "booklist_not_found", 1, False, "Run: zlib-cli booklists"),
        ]
        for error, code, exit_code, retryable, hint in cases:
            with self.subTest(error=type(error).__name__, code=code):
                value = self.error_object(["quota", "--json", "--no-session"],
                                          FakeClient({"get_profile": error}))
                self.assertEqual((type(error).__name__, str(error), code, exit_code, retryable, "not_applied"),
                                 (value["error"], value["message"], value["code"], value["exit_code"],
                                  value["retryable"], value["outcome"]))
                self.assertEqual(hint, value.get("hint"))
                self.assertNotIn("quota_may_be_used", value)
                if isinstance(error, (NetworkError, DownloadError)):
                    self.assertEqual(error.reason, value["reason"])
                else:
                    self.assertNotIn("reason", value)

    def test_text_mode_keeps_the_first_line_and_adds_a_hint(self):
        code, out, err = self.run_cli(["quota", "--no-session"], FakeClient({"get_profile": AuthenticationError("Login failed.")}))
        self.assertEqual((5, ""), (code, out))
        self.assertEqual(["AuthenticationError: Login failed.", "Hint: Run: zlib-cli login"], err.splitlines())

    def test_local_io_and_unexpected_errors(self):
        value = self.error_object(["quota", "--json", "--no-session"], FakeClient({"get_profile": OSError("C:/private")}))
        self.assertEqual(("OSError", "Local I/O failed. Check the destination and permissions.", "local_io_error", 1),
                         (value["error"], value["message"], value["code"], value["exit_code"]))
        code, out, err = self.run_cli(["quota", "--no-session"], FakeClient({"get_profile": OSError("C:/private")}))
        self.assertEqual("Local I/O failed. Check the destination and permissions.\n", err)
        value = self.error_object(["quota", "--json", "--no-session"],
                                  FakeClient({"get_profile": RuntimeError("private detail")}))
        self.assertEqual(("InternalError", "internal_error", 1), (value["error"], value["code"], value["exit_code"]))
        self.assertNotIn("private detail", json.dumps(value))
        code, _, err = self.run_cli(["quota", "--no-session"], FakeClient({"get_profile": RuntimeError("private detail")}))
        self.assertEqual(1, code)
        self.assertNotIn("Traceback", err)
        self.assertEqual("InternalError: Unexpected error. Set ZLIB_DEBUG=1 to show details.\n", err)
        code, _, err = self.run_cli(["quota", "--no-session"], FakeClient({"get_profile": RuntimeError("detail")}),
                                    environ={"ZLIB_DEBUG": "1"})
        self.assertEqual(1, code)
        self.assertIn("Traceback", err)

    def test_validation_and_not_found(self):
        value = self.error_object(["search", "dune", "--limit", "0", "--json", "--no-session"])
        self.assertEqual(("ValueError", "invalid_argument", 2, "Run: zlib-cli search --help"),
                         (value["error"], value["code"], value["exit_code"], value["hint"]))
        value = self.error_object(["download", "dune", "--pick", "1", "--json", "--no-session", "--output", self.folder],
                                  Searching([]))
        self.assertEqual(("ValueError", "not_found", 1), (value["error"], value["code"], value["exit_code"]))


class UsageErrorTests(ContractTestCase):
    def test_usage_errors_are_json_in_json_mode(self):
        value = self.error_object(["domains", "--bogus", "--json"])
        self.assertEqual(("UsageError", "unrecognized arguments: --bogus", "invalid_argument", 2, False, "not_applied",
                          "Run: zlib-cli domains --help"),
                         (value["error"], value["message"], value["code"], value["exit_code"], value["retryable"],
                          value["outcome"], value["hint"]))
        value = self.error_object(["--json"])
        self.assertEqual(("UsageError", 2, "Run: zlib-cli --help"), (value["error"], value["exit_code"], value["hint"]))
        code, out, err = self.run_cli(["domains", "--bogus"])
        self.assertEqual((2, ""), (code, out))
        self.assertTrue(err.startswith("UsageError: unrecognized arguments: --bogus\n"))
        code, _, err = self.run_cli(["domains", "--", "--json"])  # After --, the token is an argument.
        self.assertEqual(2, code)
        self.assertTrue(err.startswith("UsageError: "))

    def test_conflicts_at_any_position(self):
        for argv in (["--no-input", "domains", "--interactive", "--json"],
                     ["domains", "--no-input", "--interactive", "--json"],
                     ["--session-file", "x.json", "status", "--no-session", "--json"]):
            with self.subTest(argv=argv):
                value = self.error_object(argv)
                self.assertEqual(("UsageError", "invalid_argument", 2), (value["error"], value["code"], value["exit_code"]))
                self.assertIn("not allowed with argument", value["message"])

    def test_version(self):
        for version, expected in (("1.2.3", "zlib-cli 1.2.3\n"), (None, "zlib-cli unknown\n")):
            from importlib.metadata import PackageNotFoundError
            effect = PackageNotFoundError("zlib-connector") if version is None else None
            stdout = io.StringIO()
            with self.subTest(version=version), patch("importlib.metadata.version", return_value=version,
                                                      side_effect=effect), \
                    patch("sys.stdout", stdout), self.assertRaises(SystemExit) as exited:
                main(["--version"])
            self.assertEqual(0, exited.exception.code)
            self.assertEqual(expected, stdout.getvalue())


class AccountChangeErrorTests(ContractTestCase):
    def test_uncertain_changes_exit_4_with_the_read_that_shows_the_state(self):
        cases = [
            (["favorites", "add", "123:ab12"], "set_favorite", "Check with: zlib-cli favorites"),
            (["tags", "add", "123:ab12", "Science"], "add_book_tags", "Check with: zlib-cli tags saved-ids"),
            (["booklists", "add", "42", "123:ab12"], "add_booklist_book", "Check with: zlib-cli booklists show 42"),
            (["booklists", "remove", "42", "987"], "remove_booklist_book", "Check with: zlib-cli booklists show 42"),
            (["booklists", "create", "New"], "create_booklist", "Check with: zlib-cli booklists"),
            (["booklists", "delete", "42"], "delete_booklist", "Check with: zlib-cli booklists"),
        ]
        for argv, method, hint in cases:
            with self.subTest(argv=argv):
                value = self.error_object([*argv, "--yes", "--json", "--no-session"],
                                          FakeClient({method: NetworkError("Lost.", reason="network")}))
                self.assertEqual(("NetworkError", "mutation_uncertain", 4, False, "unknown", hint, "network"),
                                 (value["error"], value["code"], value["exit_code"], value["retryable"],
                                  value["outcome"], value["hint"], value["reason"]))

    def test_rate_limit_is_not_retryable_and_ctrl_c_is_unknown(self):
        value = self.error_object(["favorites", "add", "123:ab12", "--yes", "--json", "--no-session"],
                                  FakeClient({"set_favorite": RateLimitError("Limit.")}))
        self.assertEqual(("upstream_rate_limited", 3, False, "not_applied"),
                         (value["code"], value["exit_code"], value["retryable"], value["outcome"]))
        value = self.error_object(["favorites", "add", "123:ab12", "--yes", "--json", "--no-session"],
                                  FakeClient({"set_favorite": KeyboardInterrupt()}))
        self.assertEqual(("Cancelled", "cancelled", 130, "unknown", "Check with: zlib-cli favorites"),
                         (value["error"], value["code"], value["exit_code"], value["outcome"], value["hint"]))

    def test_declined_eof_and_ctrl_c_at_the_confirmation(self):
        for answer, message in (("n", "Cancelled. Nothing was changed."), (EOFError(), "Cancelled. Nothing was changed."),
                                (KeyboardInterrupt(), "Cancelled.")):
            with self.subTest(answer=answer):
                client = FakeClient({"get_book": FormatChangedError("synthetic")})  # The title stays unknown.
                value = self.error_object(["favorites", "add", "123:ab12", "--json", "--no-session"], client,
                                          terminal=True, answers=[answer])
                self.assertEqual(("Cancelled", message, "cancelled", 130, "not_applied"),
                                 (value["error"], value["message"], value["code"], value["exit_code"],
                                  value["outcome"]))
                self.assertEqual(["login", "get_book"], client.calls)
        code, out, err = self.run_cli(["favorites", "add", "123:ab12", "--no-session"],
                                      FakeClient({"get_book": FormatChangedError("synthetic")}), terminal=True,
                                      answers=["no"])
        self.assertEqual((130, "", "Cancelled. Nothing was changed.\n"), (code, out, err))


class DownloadErrorTests(ContractTestCase):
    def download(self, error):
        return self.error_object(["download", "123:ab12", "--json", "--no-session", "--output", self.folder],
                                 FakeClient({"download": error}))

    def test_quota_may_be_used_after_the_link_request(self):
        value = self.download(staged(NetworkError("The request timed out.", reason="timeout"), "transfer"))
        self.assertEqual(("NetworkError", "upstream_timeout", 4, False, "unknown", True, QUOTA_HINT),
                         (value["error"], value["code"], value["exit_code"], value["retryable"], value["outcome"],
                          value["quota_may_be_used"], value["hint"]))
        value = self.download(staged(DownloadError("Bad file.", reason="protocol"), "transfer"))
        self.assertEqual(("download_failed", 4, "unknown", True, "protocol"),
                         (value["code"], value["exit_code"], value["outcome"], value["quota_may_be_used"],
                          value["reason"]))
        value = self.download(staged(TransferCancelled(), "transfer"))
        self.assertEqual(("TransferCancelled", "Cancelled.", "cancelled", 130, "unknown", True),
                         (value["error"], value["message"], value["code"], value["exit_code"], value["outcome"],
                          value["quota_may_be_used"]))

    def test_explicit_refusals_used_no_quota(self):
        value = self.download(staged(DownloadError("No link.", reason="unavailable"), "link"))
        self.assertEqual(("download_unavailable", 3, True, "not_applied", False, "Check: zlib-cli quota"),
                         (value["code"], value["exit_code"], value["retryable"], value["outcome"],
                          value["quota_may_be_used"], value["hint"]))
        value = self.download(staged(DownloadError("Refused.", reason="refused"), "link"))
        self.assertEqual(("upstream_refused", 1, False), (value["code"], value["exit_code"], value["quota_may_be_used"]))
        value = self.download(staged(RateLimitError("Limit."), "link"))
        self.assertEqual(("upstream_rate_limited", 3, False), (value["code"], value["exit_code"],
                                                               value["quota_may_be_used"]))
        value = self.download(TransferCancelled())
        self.assertEqual(("cancelled", 130, "not_applied", False),
                         (value["code"], value["exit_code"], value["outcome"], value["quota_may_be_used"]))


class ContractTableTests(unittest.TestCase):
    def test_exit_code_derivation(self):
        self.assertEqual(130, exit_code_for("cancelled", "unknown", True, True))
        self.assertEqual(4, exit_code_for("upstream_timeout", "unknown", True, True))
        self.assertEqual(4, exit_code_for("download_failed", "not_applied", True, False))
        for code, expected in (("invalid_argument", 2), ("input_required", 2), ("session_domain_mismatch", 2),
                               ("upstream_authentication_required", 5), ("upstream_rate_limited", 3),
                               ("download_not_ready", 3), ("downloads_busy", 3), ("session_conflict", 4),
                               ("mutation_uncertain", 4), ("upstream_error", 1), ("download_not_found", 1)):
            with self.subTest(code=code):
                self.assertEqual(expected, exit_code_for(code))
        self.assertEqual(3, exit_code_for("something_new", retryable=True))

    def test_serialization_failure_is_internal(self):
        with self.assertRaises(Exception) as caught:
            print_json({"value": float("nan")}, file=io.StringIO())
        self.assertEqual("internal_error", describe(caught.exception)["code"])

    def test_cli_text_is_ascii(self):
        texts = [text for _, text in EXIT_CODES] + list(cli_output._HINTS.values()) + [QUOTA_HINT]
        for text in texts:
            self.assertTrue(text.isascii(), text)
        self.assertTrue(cli_output.exit_codes_text().isascii())

    def test_root_help_lists_exit_codes(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout), self.assertRaises(SystemExit):
            main(["--help"])
        text = stdout.getvalue()
        for number, _ in EXIT_CODES:
            self.assertIn(f"  {number} ", text)
        self.assertIn("docs/cli-json.md", text)


if __name__ == "__main__":
    unittest.main()
