"""Synthetic local streams: bounded parsing, safe projections and observation-only CLI."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from email.message import Message
import io
import json
from pathlib import Path
import socketserver
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from zlib_connector.api_token import load_api_token
from zlib_connector.cli import main
from zlib_connector.errors import NetworkError
from zlib_connector.http_api.contract import Field
from zlib_connector.jobs.models import DownloadTask, FileReceipt
from zlib_connector.local_api import LocalAPI, LocalAPIError
from zlib_connector.sse import FRAME_LIMIT, SSEDecoder
from zlib_connector.transfer import TransferProgress


IDENTIFIER = "a" * 32
CURSOR = "b" * 32 + ":1"
NEXT_CURSOR = "b" * 32 + ":2"


def observation(state="running", *, cursor=CURSOR, paused=False):
    receipt = FileReceipt("synthetic.pdf", 20, 1, 2, 3, 4, 5, 6) if state == "succeeded" else None
    task = DownloadTask(IDENTIFIER, "123:abc", state, 1, 2, TransferProgress(10, 20, 2), receipt=receipt)
    return {"id": cursor, "task": task.to_dict(), "paused": paused}


def frame(value=None, *, ending="\n", name="task", cursor=CURSOR):
    value = observation() if value is None else value
    fields = ["event: " + name]
    if cursor is not None:
        fields.append("id: " + cursor)
    fields.append("data: " + json.dumps(value, separators=(",", ":")))
    return (ending.join(fields) + ending * 2).encode()


def end(reason="duration"):
    return frame({"reason": reason}, name="end", cursor=None)


class Response:
    def __init__(self, data, headers, *, chunksize=11, status=200):
        self.headers = Message()
        for name, value in headers:
            self.headers[name] = value
        self.status = status
        self.source = io.BytesIO(data)
        self.chunksize = chunksize
        self.closed = False
        self.reads = 0

    def read1(self, size):
        self.reads += 1
        return self.source.read(min(size, self.chunksize))

    def close(self):
        self.closed = True
        self.source.close()


@contextmanager
def response(data, *, headers=None, chunksize=11, status=200):
    headers = [("Content-Type", "text/event-stream; charset=utf-8"), ("Connection", "close")] if headers is None else headers
    reply = Response(data, headers, chunksize=chunksize, status=status)
    requests = []

    class Connection:
        sock = None
        closed = False

        def __init__(self, host, port, *, timeout):
            self.host, self.port, self.timeout = host, port, timeout

        def connect(self):
            pass

        def request(self, method, target, *, body, headers):
            requests.append((method, target, headers))

        def getresponse(self):
            return reply

        def close(self):
            self.closed = True

    connection = Connection("127.0.0.1", 8000, timeout=1)
    with patch("zlib_connector.local_api.http.client.HTTPConnection", return_value=connection) as factory:
        yield reply, connection, requests, factory


class DecoderTests(unittest.TestCase):
    def test_incremental_bom_comments_and_all_line_endings(self):
        for ending in ("\n", "\r", "\r\n"):
            payload = ("\ufeff: keepalive" + ending + "unknown: ignored" + ending
                       + "event: task" + ending + "id: " + CURSOR + ending
                       + 'data: {"first": 1,' + ending + 'data: "second": "é"}' + ending * 2).encode()
            decoder, result = SSEDecoder(), []
            for byte in payload:
                result.extend(decoder.feed(bytes([byte])))
            self.assertEqual([("task", CURSOR, '{"first": 1,\n"second": "é"}')], result)

    def test_limits_include_comments_and_ignored_fields(self):
        cases = [b"data: " + b"x" * FRAME_LIMIT,
                 (b": " + b"x" * 200 + b"\n") * 330,
                 (b"unknown: ignored\r\n") * 4000]
        for data in cases:
            with self.subTest(size=len(data)), self.assertRaises(NetworkError):
                list(SSEDecoder().feed(data))

    def test_malformed_utf8_duplicate_fields_and_nul_ids_are_rejected(self):
        for data in (b": \xff\n", b"event: task\nevent: end\n", b"id: first\nid: second\n",
                     b"id: bad\x00id\n", b"event: task\n\n"):
            with self.subTest(data=data), self.assertRaises(NetworkError):
                list(SSEDecoder().feed(data))


class LocalEventsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.token_file = Path(folder.name) / "api-token"
        self.token = load_api_token(self.token_file)
        self.api = LocalAPI("http://localhost:8000/v1", self.token_file, timeout=.2)

    def test_one_authenticated_stream_resumes_cursor_and_finishes_explicitly(self):
        values = [observation(), observation(cursor=NEXT_CURSOR, paused=True)]
        data = frame(values[0]) + b": heartbeat\n\n" + frame(values[1], cursor=NEXT_CURSOR) + end()
        with response(data, chunksize=1) as (reply, connection, calls, _), \
                patch.dict("os.environ", {"HTTP_PROXY": "http://unreachable.invalid:9"}):
            self.assertEqual(values, list(self.api.events(IDENTIFIER, after=CURSOR, duration=1.5)))
        self.assertTrue(reply.closed)
        self.assertTrue(connection.closed)
        self.assertEqual(1, len(calls))
        method, target, headers = calls[0]
        self.assertEqual("GET", method)
        self.assertEqual(f"/v1/downloads/{IDENTIFIER}/events?duration=1.5&after=" + CURSOR.replace(":", "%3A"), target)
        self.assertEqual("Bearer " + self.token, headers["Authorization"])
        self.assertEqual("text/event-stream", headers["Accept"])
        self.assertEqual("identity", headers["Accept-Encoding"])

    def test_terminal_observation_finishes_without_needing_an_end_frame(self):
        for state in ("succeeded", "failed", "cancelled", "interrupted"):
            value = observation(state)
            with self.subTest(state=state), response(frame(value)) as (reply, connection, _, _):
                self.assertEqual([value], list(self.api.events(IDENTIFIER)))
                self.assertTrue(reply.closed and connection.closed)

    def test_fractional_clock_duration_uses_the_servers_bounded_decimal_grammar(self):
        duration = 4.999998100101948
        field = Field("number", minimum=1, maximum=60)
        with self.assertRaises(ValueError):
            field.parse(str(duration), query=True)
        with response(end()) as (_, _, calls, _), patch("zlib_connector.local_api.ReadDeadline") as guard, \
                patch("zlib_connector.local_api.time.monotonic", return_value=10):
            self.assertEqual([], list(self.api.events(IDENTIFIER, duration=duration)))
        encoded = parse_qs(urlsplit(calls[0][1]).query)["duration"][0]
        self.assertEqual("4.9999981", encoded)
        self.assertAlmostEqual(duration, field.parse(encoded, query=True), places=9)
        self.assertEqual(10 + self.api._timeout + duration, guard.call_args.args[1])

    def test_iterator_close_releases_response_connection_and_deadline_guard(self):
        with response(frame()) as (reply, connection, _, _), patch("zlib_connector.local_api.ReadDeadline") as guard:
            stream = self.api.events(IDENTIFIER)
            self.assertEqual(observation(), next(stream))
            self.assertFalse(reply.closed)
            stream.close()
            self.assertTrue(reply.closed and connection.closed)
            guard.return_value.close.assert_called_once()

    def test_empty_explicit_duration_is_valid_but_eof_shutdown_and_false_terminal_are_not(self):
        with response(end()):
            self.assertEqual([], list(self.api.events(IDENTIFIER)))
        for data in (b"", b": heartbeat\n\n", frame(), frame()[:-1], end("shutdown"), end("terminal"), end("unknown")):
            with self.subTest(data=data[-50:]), response(data) as (reply, connection, calls, _):
                with self.assertRaises(NetworkError):
                    list(self.api.events(IDENTIFIER))
                self.assertEqual(1, len(calls))
                self.assertTrue(reply.closed and connection.closed)

    def test_invalid_arguments_precede_connection(self):
        cases = [("secret", {}), (IDENTIFIER, {"after": "secret"})]
        cases += [(IDENTIFIER, {"duration": value}) for value in (True, 0, 61, 10**1000, float("nan"), float("inf"), "30")]
        with response(end()) as (_, _, _, factory):
            for identifier, options in cases:
                with self.subTest(options=options), self.assertRaises(ValueError):
                    list(self.api.events(identifier, **options))
            factory.assert_not_called()

    def test_redirects_and_errors_are_not_followed_or_reflected(self):
        for status in (302, 401, 503):
            with self.subTest(status=status), response(b"private-account-secret", status=status) as (reply, _, calls, _):
                with self.assertRaises(LocalAPIError) as error:
                    list(self.api.events(IDENTIFIER))
                self.assertNotIn("private-account-secret", str(error.exception))
                self.assertEqual(0, reply.reads)
                self.assertEqual(1, len(calls))

    def test_framing_conflicts_and_non_utf8_compressed_streams_are_rejected(self):
        valid = [("Content-Type", "text/event-stream; charset=utf-8"), ("Connection", "close")]
        cases = [valid + [extra] for extra in (("Content-Length", "0"), ("Transfer-Encoding", "chunked"),
                 ("Content-Encoding", "gzip"), ("Content-Type", "text/event-stream"), ("Connection", "keep-alive"))]
        cases += [[("Content-Type", value), ("Connection", "close")] for value in ("text/event-stream", "application/json", "text/event-stream; charset=latin1")]
        cases += [[("Content-Type", "text/event-stream; charset=utf-8")]]
        for headers in cases:
            with self.subTest(headers=headers), response(end(), headers=headers) as (reply, _, _, _):
                with self.assertRaises(NetworkError):
                    list(self.api.events(IDENTIFIER))
                self.assertEqual(0, reply.reads)

    def test_public_projection_rejects_private_fields_mismatches_and_unsafe_messages(self):
        mutations = [lambda value: value.update(receipt="private-account-secret"),
                     lambda value: value.update(paused=1),
                     lambda value: value.update(id=NEXT_CURSOR),
                     lambda value: value["task"].update(id="c" * 32),
                     lambda value: value["task"].update(path="private-account-secret"),
                     lambda value: value["task"].update(error_message="private-account-secret"),
                     lambda value: value["task"].update(state="private-account-secret"),
                     lambda value: value["task"].update(outcome="completed"),
                     lambda value: value["task"].update(file_available=1),
                     lambda value: value["task"].update(created_at=True),
                     lambda value: value["task"].update(error_code=[]),
                     lambda value: value["task"]["progress"].update(bytes_per_second=123),
                     lambda value: value["task"]["progress"].update(fraction=True),
                     lambda value: value["task"]["progress"].update(bytes_received=True)]
        for mutate in mutations:
            value = observation()
            mutate(value)
            with self.subTest(value=value), response(frame(value) + end()):
                with self.assertRaises(NetworkError) as error:
                    list(self.api.events(IDENTIFIER))
                self.assertNotIn("private-account-secret", str(error.exception))

    def test_duplicate_json_nonfinite_data_and_frame_mismatch_fail_safely(self):
        cases = [b'event: end\ndata: {"reason":"duration","reason":"shutdown"}\n\n',
                 b'event: end\ndata: {"reason":NaN}\n\n', frame(name="unknown"),
                 frame(cursor=NEXT_CURSOR), frame(cursor=None),
                 frame({"reason": "duration", "path": "private-account-secret"}, name="end", cursor=None)]
        for data in cases:
            with self.subTest(data=data[:50]), response(data), self.assertRaises(NetworkError):
                list(self.api.events(IDENTIFIER))

    def test_absolute_deadline_interrupts_a_drip_stream(self):
        class Drip(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(4096)
                try:
                    self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\nConnection: close\r\n\r\n")
                    for _ in range(160):
                        self.request.sendall(b": heartbeat\n\n")
                        time.sleep(.01)
                except OSError:
                    pass

        with socketserver.TCPServer(("127.0.0.1", 0), Drip) as server:
            thread = threading.Thread(target=server.handle_request)
            thread.start()
            started = time.monotonic()
            try:
                api = LocalAPI(f"http://127.0.0.1:{server.server_address[1]}", self.token_file, timeout=.15)
                with self.assertRaises(NetworkError) as error:
                    list(api.events(IDENTIFIER, duration=1))
                self.assertEqual("timeout", error.exception.reason)
                self.assertLess(time.monotonic() - started, 1.5)
            finally:
                thread.join(2)

    def test_valid_silent_stream_outlives_ordinary_request_timeout(self):
        class Silent(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(4096)
                self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\nConnection: close\r\n\r\n")
                time.sleep(.35)
                try:
                    self.request.sendall(end())
                except OSError:
                    pass

        with socketserver.TCPServer(("127.0.0.1", 0), Silent) as server:
            thread = threading.Thread(target=server.handle_request)
            thread.start()
            started = time.monotonic()
            try:
                api = LocalAPI(f"http://127.0.0.1:{server.server_address[1]}", self.token_file, timeout=.1)
                self.assertEqual([], list(api.events(IDENTIFIER, after=CURSOR, duration=1)))
                self.assertGreater(time.monotonic() - started, .25)
            finally:
                thread.join(2)


class WatchTests(unittest.TestCase):
    def invoke(self, api, *options):
        output, errors = io.StringIO(), io.StringIO()
        with patch("zlib_connector.cli_jobs.LocalAPI", return_value=api), \
                patch("zlib_connector.cli.AccountSession", side_effect=AssertionError("Account opened")), \
                patch("zlib_connector.cli.Client", side_effect=AssertionError("Upstream client opened")), \
                redirect_stdout(output), redirect_stderr(errors):
            code = main(["jobs", "watch", IDENTIFIER, *options])
        return code, output.getvalue(), errors.getvalue()

    def test_watch_reconnects_explicit_duration_using_last_cursor_and_ndjson(self):
        values = [observation(), observation("succeeded", cursor=NEXT_CURSOR)]
        calls, closed = [], []

        class API:
            def _events(self, identifier, **options):
                index = len(calls)
                calls.append((identifier, options))
                try:
                    yield values[index]
                finally:
                    closed.append(index)

        with patch("zlib_connector.cli_jobs.time.monotonic", side_effect=[0, 0, 30]):
            code, output, errors = self.invoke(API(), "--json", "--duration", "45", "--after", CURSOR)
        self.assertEqual(0, code)
        self.assertEqual(values, [json.loads(line) for line in output.splitlines()])
        self.assertEqual("", errors)
        self.assertEqual([0, 1], closed)
        self.assertEqual([30, 15], [call[1]["duration"] for call in calls])
        self.assertEqual([CURSOR, CURSOR], [call[1]["after"] for call in calls])
        self.assertEqual([45, 45], [call[1]["stop_at"] for call in calls])

    def test_watch_caller_deadline_stops_without_reconnecting_or_cancelling(self):
        calls = []

        class API:
            def _events(self, identifier, **options):
                calls.append(options)
                yield observation()
                raise NetworkError("Deadline reached.", reason="timeout")

        with patch("zlib_connector.cli_jobs.time.monotonic", side_effect=[0, 0, 2]):
            code, output, errors = self.invoke(API(), "--duration", "2")
        self.assertEqual(3, code)
        self.assertIn("10/20 bytes", output)
        self.assertEqual("Stopped watching before the task finished. Run the same command to keep watching.\n", errors)
        self.assertEqual(1, len(calls))

    def test_watch_never_retries_protocol_network_shutdown_or_early_timeout(self):
        for reason in ("network", "timeout"):
            calls = []

            class API:
                def _events(self, identifier, **options):
                    calls.append(options)
                    raise NetworkError("Synthetic stream failure.", reason=reason)
                    yield

            with self.subTest(reason=reason), patch("zlib_connector.cli_jobs.time.monotonic", return_value=0):
                code, _, errors = self.invoke(API(), "--json")
            self.assertEqual(1, code)
            self.assertEqual(1, len(calls))
            self.assertEqual("NetworkError", json.loads(errors)["error"])

    def test_watch_ctrl_c_closes_only_observation(self):
        closed = []

        class API:
            def _events(self, identifier, **options):
                try:
                    yield observation(paused=True)
                    raise KeyboardInterrupt
                finally:
                    closed.append(True)

        code, output, _ = self.invoke(API())
        self.assertEqual(130, code)
        self.assertIn("queue paused", output)
        self.assertEqual([True], closed)

    def test_watch_argument_errors_precede_token_loading(self):
        for options in (("--duration", "0"), ("--duration", "3601"), ("--duration", "nan"), ("--after", "secret")):
            with self.subTest(options=options), patch("zlib_connector.cli_jobs.LocalAPI", side_effect=AssertionError("Client opened")), \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(2, main(["jobs", "watch", IDENTIFIER, *options]))

    def test_last_fractional_second_stops_locally(self):
        calls = []

        class API:
            def _events(self, *args, **kwargs):
                calls.append(kwargs)
                yield observation()

        with patch("zlib_connector.cli_jobs.time.monotonic", side_effect=[0, 0, 1.5]):
            code, output, _ = self.invoke(API(), "--duration", "2")
        self.assertEqual(3, code)
        self.assertIn("running", output)
        self.assertEqual(1, len(calls))

    def test_one_second_budget_still_opens_first_stream_and_receives_immediate_state(self):
        calls = []
        value = observation("succeeded")

        class API:
            def _events(self, identifier, **options):
                calls.append(options)
                yield value

        with patch("zlib_connector.cli_jobs.time.monotonic", side_effect=[0, .01]):
            code, output, errors = self.invoke(API(), "--duration", "1", "--json")
        self.assertEqual(0, code)
        self.assertEqual(value, json.loads(output))
        self.assertEqual("", errors)
        self.assertEqual(1, calls[0]["duration"])
        self.assertEqual(1, calls[0]["stop_at"])
