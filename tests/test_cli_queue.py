"""queue add submits one keyed task per exact reference; serve --json announces where it listens."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from zlib_connector.api_token import load_api_token
from zlib_connector.cli import main
from zlib_connector.errors import NetworkError
from zlib_connector.http_cli import run_server
from zlib_connector.local_api import LocalAPIError
from test_http_cli import FakeClient, FakeServer, options


def task(number, state="queued"):
    return {"id": f"{number:032x}", "reference": f"{number}:abc", "state": state}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.token_file = Path(self.folder.name) / "api-token"
        self.token = load_api_token(self.token_file)

    def invoke(self, *arguments, replies=(), stdin=None):
        api = Mock()
        api.request.side_effect = list(replies)
        output, errors = io.StringIO(), io.StringIO()
        stream = io.TextIOWrapper(io.BytesIO(stdin or b""), encoding="utf-8")
        with redirect_stdout(output), redirect_stderr(errors), patch("sys.stdin", stream), \
                patch("zlib_connector.cli_jobs.LocalAPI", return_value=api) as factory, \
                patch("zlib_connector.cli.AccountSession", side_effect=AssertionError("Account opened")):
            code = main(["queue", "--token-file", str(self.token_file), *arguments])
        return code, output.getvalue(), errors.getvalue(), api, factory

    def test_each_reference_gets_a_numbered_key_in_input_order(self):
        code, out, err, api, _ = self.invoke("add", "1:abc", "https://z-lib.by/book/2/abc/title.html", "3:abc",
                                             "--key", "reading", "--json",
                                             replies=[task(1), task(2), task(3, "running")])
        self.assertEqual(0, code)
        sent = [call.kwargs["body"] for call in api.request.call_args_list]
        self.assertEqual([{"reference": f"{n}:abc", "idempotency_key": f"reading-000{n}"} for n in (1, 2, 3)], sent)
        self.assertEqual({("POST", "/downloads")}, {call.args for call in api.request.call_args_list})
        value = json.loads(out)
        self.assertEqual({"position": 3, "reference": "3:abc", "key": "reading-0003", "task_id": f"{3:032x}",
                          "state": "running", "code": None}, value["items"][2])
        self.assertEqual({"submitted": 3, "failed": 0, "uncertain": 0, "not_started": 0}, value["summary"])
        self.assertNotIn(self.token, out + err)

    def test_search_json_on_stdin_is_accepted(self):
        books = json.dumps([{"book_id": "7", "book_hash": "abc", "title": "Dune"}]).encode()
        code, out, _, api, _ = self.invoke("add", "-", "--key", "reading", "--json", replies=[task(7)], stdin=books)
        self.assertEqual((0, "7:abc"), (code, api.request.call_args.kwargs["body"]["reference"]))

    def test_invalid_lists_and_keys_fail_before_the_server_is_contacted(self):
        for arguments in (("add", "Dune", "--key", "reading"), ("add", "1:abc", "--key", "ab"),
                          ("add", "--key", "reading"), ("add", "1:abc", "-", "--key", "reading")):
            with self.subTest(arguments=arguments):
                code, out, err, _, factory = self.invoke(*arguments, "--json")
                self.assertEqual((2, ""), (code, out))
                factory.assert_not_called()
                self.assertEqual("invalid_argument", json.loads(err)["code"])

    def test_lost_connection_stops_the_run_and_marks_the_item_uncertain(self):
        code, out, err, api, _ = self.invoke("add", "1:abc", "2:abc", "3:abc", "--key", "reading", "--json",
                                             replies=[task(1), NetworkError("Could not contact the local API.")])
        value = json.loads(out)
        self.assertEqual(4, code)
        self.assertEqual(["queued", "uncertain", "not_started"], [item["state"] for item in value["items"]])
        self.assertEqual("local_api_unavailable", value["items"][1]["code"])
        self.assertEqual(2, api.request.call_count)
        self.assertIn("never adds a second download", err)

    def test_busy_server_leaves_the_rest_not_started_and_exits_3(self):
        busy = LocalAPIError(503, code="downloads_busy", retryable=True, outcome="not_applied")
        code, out, _, _, _ = self.invoke("add", "1:abc", "2:abc", "--key", "reading", "--json",
                                         replies=[task(1), busy])
        value = json.loads(out)
        self.assertEqual(3, code)
        self.assertEqual(("not_started", "downloads_busy"), (value["items"][1]["state"], value["items"][1]["code"]))

    def test_a_conflicting_item_fails_and_the_next_one_is_still_sent(self):
        conflict = LocalAPIError(409, code="download_conflict", retryable=False, outcome="not_applied")
        code, out, _, _, _ = self.invoke("add", "1:abc", "2:abc", "--key", "reading", replies=[conflict, task(2)])
        self.assertEqual(4, code)
        self.assertIn("failed (download_conflict)", out)
        self.assertIn("Submitted 1, failed 1, uncertain 0, not started 0.", out)

    def test_list_and_resume_are_the_jobs_calls(self):
        listing = {"tasks": [], "total": 0, "limit": 50, "offset": 0, "paused": False}
        code, out, _, api, _ = self.invoke("list", "--json", replies=[listing])
        self.assertEqual((0, listing), (code, json.loads(out)))
        self.assertEqual(("GET", "/downloads"), api.request.call_args.args)
        code, out, _, api, _ = self.invoke("resume", "--json", replies=[{"resumed": 2}])
        self.assertEqual((0, ("POST", "/downloads/resume")), (code, api.request.call_args.args))


class ServeJsonTests(unittest.TestCase):
    def test_one_listening_line_on_stdout_without_the_token(self):
        events = []
        with tempfile.TemporaryDirectory() as folder:
            token_path = Path(folder) / "api-token"
            args = options("--no-session", "serve", "--anonymous", "--token-file", str(token_path))
            args.json = True
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                run_server(args, client_factory=lambda *a, **k: FakeClient(events),
                           server_factory=lambda *a, **k: FakeServer(events))
            token = token_path.read_text().strip()
        lines = output.getvalue().splitlines()
        self.assertEqual(1, len(lines))
        self.assertEqual({"event": "listening", "url": "http://127.0.0.1:43123/v1", "token_file": str(token_path),
                          "downloads": False}, json.loads(lines[0]))
        self.assertNotIn(token, output.getvalue() + errors.getvalue())
        self.assertNotIn("HTTP API:", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
