"""Local CLI uses only the authenticated loopback server, with bounded file copies."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import socketserver
import tempfile
import threading
import time
import unittest
import unittest.mock
from unittest.mock import patch

from zlib_connector.api_token import load_api_token
from zlib_connector.cli import main
from zlib_connector.errors import DownloadError, NetworkError
from zlib_connector.local_api import JSON_LIMIT, LocalAPI, LocalAPIError


IDENTIFIER = "a" * 32
TASK = {"id": IDENTIFIER, "reference": "123:abc", "state": "queued"}
PDF = b"%PDF-1.7\nSynthetic test book\n"


@contextmanager
def server(*, status=200, body=None, content_type="application/json", declared=None, headers=()):
    body = json.dumps(TASK).encode() if body is None else body
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append((self.command, self.path, self.headers, data))
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body) if declared is None else declared))
            self.send_header("Connection", "close")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

        def log_message(self, *_):
            pass

    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=instance.serve_forever, kwargs={"poll_interval": .01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{instance.server_port}/v1", calls
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(2)


class LocalAPITests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)
        self.token_file = self.path / "api-token"
        self.token = load_api_token(self.token_file)

    def test_requests_authenticate_once_without_proxy_or_redirect(self):
        with server() as (url, calls), patch.dict("os.environ", {"HTTP_PROXY": "http://unreachable.invalid:9"}):
            result = LocalAPI(url, self.token_file).request("POST", "/downloads", body={"reference": "123:abc", "idempotency_key": "test-key-1"})
        self.assertEqual(TASK, result)
        self.assertEqual(1, len(calls))
        method, path, headers, data = calls[0]
        self.assertEqual(("POST", "/v1/downloads"), (method, path))
        self.assertEqual("Bearer " + self.token, headers["Authorization"])
        self.assertEqual("test-key-1", json.loads(data)["idempotency_key"])
        with server(status=302, headers=(("Location", "http://example.org/leak"),)) as (url, calls):
            with self.assertRaises(LocalAPIError):
                LocalAPI(url, self.token_file).request("GET", "/downloads")
        self.assertEqual(1, len(calls))

    def test_url_validation_precedes_token_read_and_missing_token_is_not_created(self):
        missing = self.path / "missing"
        for url in ("https://127.0.0.1", "http://example.org", "http://192.168.0.1", "http://127.0.0.1:0", "http://127.0.0.1/?x", "http://user@localhost", "http://localhost:8000/other"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                LocalAPI(url, missing)
        with self.assertRaises(FileNotFoundError):
            LocalAPI("http://localhost:8000", missing)
        self.assertFalse(missing.exists())

    def test_response_errors_are_safe_and_bounded(self):
        for options in ({"status": 503, "body": b"private-account-data"}, {"declared": JSON_LIMIT + 1},
                        {"headers": (("Content-Length", "1"),)}, {"body": b"not-json"},
                        {"body": b"{}", "declared": 10}, {"body": b"[]"}):
            with self.subTest(options=options), server(**options) as (url, _):
                with self.assertRaises((NetworkError, LocalAPIError)) as error:
                    LocalAPI(url, self.token_file).request("GET", "/downloads")
                self.assertNotIn("private-account-data", str(error.exception))

    def test_file_copy_validates_and_never_overwrites(self):
        with server(body=PDF, content_type="application/pdf") as (url, calls):
            api = LocalAPI(url, self.token_file)
            first = api.file(IDENTIFIER, self.path)
            second = api.file(IDENTIFIER, self.path)
        self.assertNotEqual(first, second)
        self.assertEqual(PDF, first.read_bytes())
        self.assertEqual(PDF, second.read_bytes())
        self.assertTrue(all(call[1] == f"/v1/downloads/{IDENTIFIER}/file" for call in calls))
        self.assertEqual([], list(self.path.glob("*.part")))

    def test_invalid_and_incomplete_files_leave_no_output(self):
        for options in ({"body": b"<html>login</html>"}, {"body": PDF, "declared": len(PDF) + 1}):
            with self.subTest(options=options), server(content_type="application/pdf", **options) as (url, _):
                with self.assertRaises((DownloadError, NetworkError)):
                    LocalAPI(url, self.token_file).file(IDENTIFIER, self.path)
            self.assertEqual([self.token_file], list(self.path.iterdir()))

    def test_deadline_interrupts_slow_headers(self):
        class SlowHeaders(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(4096)
                try:
                    self.request.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                    for _ in range(15):
                        self.request.sendall(b"x")
                        time.sleep(.03)
                except OSError:
                    pass

        with socketserver.TCPServer(("127.0.0.1", 0), SlowHeaders) as instance:
            thread = threading.Thread(target=instance.handle_request)
            thread.start()
            started = time.monotonic()
            try:
                with self.assertRaises(NetworkError):
                    LocalAPI(f"http://127.0.0.1:{instance.server_address[1]}", self.token_file, timeout=.1).request("GET", "/downloads")
                self.assertLess(time.monotonic() - started, .4)
            finally:
                thread.join(2)

    def test_cli_jobs_skip_account_files_and_produce_json(self):
        cases = [(["add", "123:abc", "--key", "test-key-1"], "POST", "/v1/downloads"),
                 (["show", IDENTIFIER], "GET", f"/v1/downloads/{IDENTIFIER}"),
                 (["cancel", IDENTIFIER], "POST", f"/v1/downloads/{IDENTIFIER}/cancel"),
                 (["resume"], "POST", "/v1/downloads/resume"),
                 (["list", "--limit", "2", "--offset", "1"], "GET", "/v1/downloads?limit=2&offset=1")]
        for arguments, method, path in cases:
            with self.subTest(arguments=arguments), server() as (url, calls), \
                    patch("zlib_connector.cli.AccountSession", side_effect=AssertionError("Account opened")), \
                    patch("zlib_connector.cli.Client", side_effect=AssertionError("Upstream client opened")):
                output = io.StringIO()
                with redirect_stdout(output), redirect_stderr(io.StringIO()):
                    code = main(["jobs", "--api-url", url, "--token-file", str(self.token_file), *arguments, "--json"])
                self.assertEqual(0, code)
                self.assertEqual(TASK, json.loads(output.getvalue()))
                self.assertEqual((method, path), calls[0][:2])
                self.assertNotIn(self.token, output.getvalue())

    def test_cli_key_and_page_errors_precede_network(self):
        for args in (("add", "123:abc", "--key", "short"), ("list", "--offset", "10001")):
            with self.subTest(args=args), patch("zlib_connector.cli_jobs.LocalAPI", side_effect=AssertionError("Client opened")), \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(2, main(["jobs", *args]))


def watch_event(state, cursor):
    task = {"id": IDENTIFIER, "reference": "123:abc", "state": state, "progress": None}
    return {"id": cursor, "task": task, "paused": False}


class JobsContractTests(unittest.TestCase):
    """Exit codes and error codes of jobs, including the server's codes (docs/cli-json.md)."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.token_file = Path(self.folder.name) / "api-token"
        load_api_token(self.token_file)

    def invoke(self, url, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors), \
                patch("zlib_connector.cli.AccountSession", side_effect=AssertionError("Account opened")):
            code = main(["jobs", "--api-url", url, "--token-file", str(self.token_file), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_server_codes_pass_through_without_the_server_message(self):
        cases = [
            (409, {"code": "download_conflict", "retryable": False, "outcome": "not_applied"}, "download_conflict", 1,
             "Use a new --key for a different book."),
            (401, {"code": "unauthorized", "retryable": False, "outcome": "not_applied"}, "unauthorized", 1,
             "Use the token file of the running server (--token-file)."),
            (503, {"code": "downloads_unavailable", "retryable": False, "outcome": "not_applied"},
             "downloads_unavailable", 1, "Start the server with --download-dir DIR."),
            (503, {"code": "downloads_busy", "retryable": True, "outcome": "not_applied"}, "downloads_busy", 3, None),
            (404, {"code": "download_not_found", "retryable": False, "outcome": "not_applied"}, "download_not_found",
             1, None),
            (503, {"code": "download_storage_error", "retryable": False, "outcome": "unknown"},
             "download_storage_error", 4, None),
        ]
        for status, details, code, exit_code, hint in cases:
            body = json.dumps({"error": {**details, "message": "private-server-text"}}).encode()
            with self.subTest(code=code), server(status=status, body=body) as (url, _):
                result, out, err = self.invoke(url, "add", "123:abc", "--key", "test-key-1", "--json")
            value = json.loads(err)
            self.assertEqual((exit_code, "", "LocalAPIError", code, exit_code, details["outcome"]),
                             (result, out, value["error"], value["code"], value["exit_code"], value["outcome"]))
            self.assertEqual(hint, value.get("hint"))
            self.assertNotIn("private-server-text", err)

    def test_bodies_that_are_too_large_invalid_or_badly_coded_give_local_api_error(self):
        padding = " " * (64 * 1024)
        for body in (('{"error": {"code": "download_conflict"}}' + padding).encode(), b"not-json",
                     b'{"error": {"code": "Bad-Code", "retryable": "yes", "outcome": "maybe"}}',
                     b'{"error": "download_conflict"}', b"[]"):
            with self.subTest(body=body[:40]), server(status=409, body=body) as (url, _):
                result, out, err = self.invoke(url, "show", IDENTIFIER, "--json")
            value = json.loads(err)
            self.assertEqual((1, "", "local_api_error", False, "not_applied"),
                             (result, out, value["code"], value["retryable"], value["outcome"]))

    def test_network_failures_name_what_to_check(self):
        cases = [(["add", "123:abc", "--key", "test-key-1"], "unknown", 4,
                  "Run the same command again with the same --key: the server replays it and never adds a second"
                  " download."),
                 (["cancel", IDENTIFIER], "unknown", 4, f"Check with: zlib-cli jobs show {IDENTIFIER}"),
                 (["resume"], "unknown", 4, "Check with: zlib-cli jobs list"),
                 (["list"], "not_applied", 1, "Start the server: zlib-cli serve --download-dir DIR"),
                 (["show", IDENTIFIER], "not_applied", 1, "Start the server: zlib-cli serve --download-dir DIR")]
        api = unittest.mock.Mock()
        api.request.side_effect = NetworkError("Could not contact the local API.")
        for arguments, outcome, exit_code, hint in cases:
            with self.subTest(arguments=arguments), patch("zlib_connector.cli_jobs.LocalAPI", return_value=api):
                result, out, err = self.invoke("http://127.0.0.1:9/v1", *arguments, "--json")
            value = json.loads(err)
            self.assertEqual((exit_code, "", "NetworkError", "local_api_unavailable", outcome, hint, "network"),
                             (result, out, value["error"], value["code"], value["outcome"], value["hint"],
                              value["reason"]))

    def test_watch_exits_by_the_last_state(self):
        for states, exit_code, note in ((["running", "succeeded"], 0, None),
                                        (["running", "failed"], 4, "The task ended in state failed."),
                                        (["interrupted"], 4, "The task ended in state interrupted."),
                                        (["cancelled"], 4, "The task ended in state cancelled.")):
            class API:
                def _events(self, identifier, **options):
                    for number, state in enumerate(states):
                        yield watch_event(state, f"{number:032x}")

            for flag in ([], ["--json"]):
                with self.subTest(states=states, flag=flag), patch("zlib_connector.cli_jobs.LocalAPI",
                                                                   return_value=API()):
                    result, out, err = self.invoke("http://127.0.0.1:9/v1", "watch", IDENTIFIER, *flag)
                self.assertEqual(exit_code, result)
                if flag:
                    self.assertEqual(states, [json.loads(line)["task"]["state"] for line in out.splitlines()])
                    self.assertEqual("", err)
                else:
                    self.assertEqual((note + "\n") if note else "", err)
