"""Synthetic response streams and sinks; no external network or book downloads."""

from contextlib import contextmanager
from email.message import Message
import http.client
import io
import socket
import ssl
import threading
import time
import tracemalloc
import unittest
from unittest.mock import patch

from zlib_connector.errors import DownloadError, NetworkError
from zlib_connector.transfer import CancellationToken, TransferCancelled
from zlib_connector.transport import CHUNK_SIZE, Transport
from test_transport import Connection, Reply


URL = "https://books.example/file.epub"


class GeneratedReply:
    status = 200

    def __init__(self, size, *, known=True):
        self.remaining = size
        self.headers = Message()
        if known:
            self.headers["Content-Length"] = str(size)
        self.read_sizes = []
        self.closed = False

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def info(self):
        return self.headers

    def read(self, count):
        if not 0 < count <= CHUNK_SIZE:
            raise AssertionError("Unbounded read requested.")
        self.read_sizes.append(count)
        size = min(count, self.remaining)
        self.remaining -= size
        return b"x" * size

    def close(self):
        self.closed = True


class CountingSink:
    def __init__(self, partial=None):
        self.count = 0
        self.maximum = 0
        self.partial = partial

    def write(self, data):
        self.maximum = max(self.maximum, len(data))
        written = min(len(data), self.partial) if self.partial else len(data)
        self.count += written
        return written


class BufferedSocket:
    """Socket-shaped source for the actual stdlib HTTP framing parser."""

    def __init__(self, wire):
        self.stream = io.BytesIO(wire)
        self.timeouts = []

    def makefile(self, mode):
        return self.stream

    def settimeout(self, timeout):
        if self.stream.closed:
            raise OSError("The parsed response socket has already closed.")
        self.timeouts.append(timeout)


class DetachedConnection(Connection):
    def __init__(self, wire):
        self.source = BufferedSocket(wire)
        response = http.client.HTTPResponse(self.source)
        response.begin()
        super().__init__(response)
        self.sock = self.source

    def getresponse(self):
        # Like HTTPConnection.getresponse for a Connection: close response.
        self.sock = None
        return self.reply


class SocketPairConnection(Connection):
    """Real socket reads and the stdlib HTTP parser, with no DNS/TLS endpoint."""

    def __init__(self, source):
        super().__init__(None)
        self.sock = source

    def getresponse(self):
        response = http.client.HTTPResponse(self.sock)
        try:
            response.begin()
        except BaseException:
            response.close()
            raise
        self.reply = response
        self.sock.close()  # The response file keeps the underlying socket alive.
        self.sock = None
        return response

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        super().close()


@contextmanager
def trickling_response(prefix, slow, suffix=b""):
    source, peer = socket.socketpair()
    connection = SocketPairConnection(source)
    stop = threading.Event()
    sent = []

    def send():
        try:
            peer.settimeout(1)
            peer.sendall(prefix)
            for value in slow:
                if stop.wait(0.025):
                    return
                peer.sendall(bytes((value,)))
                sent.append(value)
            peer.sendall(suffix)
        except OSError:
            pass
        finally:
            peer.close()

    writer = threading.Thread(target=send, name="synthetic-trickle")
    writer.start()
    try:
        yield connection, sent
    finally:
        stop.set()
        peer.close()
        source.close()
        writer.join(2)
        if writer.is_alive():
            raise AssertionError("The synthetic response writer did not stop.")


@contextmanager
def upstream(*connections):
    with patch("zlib_connector.transport.public_addresses", return_value=["8.8.8.8"]) as resolve, \
            patch("http.client.HTTPSConnection", side_effect=connections) as construct:
        yield resolve, construct


class StreamingTransportTests(unittest.TestCase):
    def test_large_response_streams_with_bounded_memory_reads_and_writes(self):
        size = 8 * 1024 * 1024 + 17
        reply = GeneratedReply(size)
        connection, target = Connection(reply), CountingSink()
        last = []

        def progress(event):
            if last:
                self.assertGreater(event.bytes_received, last[0].bytes_received)
            last[:] = [event]

        with upstream(connection):
            tracemalloc.start()
            try:
                received = Transport().download_to(URL, target, progress=progress)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertEqual((size, size, size), (received, target.count, last[0].bytes_received))
        self.assertEqual(size, last[0].total_bytes)
        self.assertGreaterEqual(last[0].elapsed_seconds, 0)
        self.assertLess(peak, 2 * 1024 * 1024)
        self.assertLessEqual(max(reply.read_sizes), CHUNK_SIZE)
        self.assertLessEqual(target.maximum, CHUNK_SIZE)
        self.assertTrue(reply.closed and connection.closed)
        self.assertEqual("close", connection.calls[0][3]["Connection"])

    def test_unknown_length_stays_unknown_and_short_sink_writes_are_supported(self):
        reply = GeneratedReply(CHUNK_SIZE + 5, known=False)
        target, events = CountingSink(partial=7), []
        with upstream(Connection(reply)):
            result = Transport().download_to(URL, target, progress=events.append)
        self.assertEqual(CHUNK_SIZE + 5, result)
        self.assertEqual(result, target.count)
        self.assertEqual([0, CHUNK_SIZE, CHUNK_SIZE + 5], [event.bytes_received for event in events])
        self.assertTrue(all(event.total_bytes is None for event in events))

    def test_zero_length_produces_a_zero_event_without_body_reads(self):
        reply, events = GeneratedReply(0), []
        with upstream(Connection(reply)):
            self.assertEqual(0, Transport().download_to(URL, CountingSink(), progress=events.append))
        self.assertEqual([], reply.read_sizes)
        self.assertEqual((0, 0), (events[0].bytes_received, events[0].total_bytes))

    def test_actual_http_framing_known_chunked_and_eof(self):
        for headers, body, expected, total in (
                (b"Content-Length: 5\r\n", b"helloEXTRA", b"hello", 5),
                (b"Transfer-Encoding: chunked\r\n", b"5\r\nhello\r\n0\r\n\r\n", b"hello", None),
                (b"", b"hello", b"hello", None),
                (b"Content-Length: 0\r\n", b"", b"", 0)):
            connection = DetachedConnection(b"HTTP/1.1 200 OK\r\nConnection: close\r\n" + headers + b"\r\n" + body)
            target, events = io.BytesIO(), []
            with self.subTest(headers=headers), upstream(connection):
                self.assertEqual(len(expected), Transport().download_to(URL, target, progress=events.append))
            self.assertEqual(expected, target.getvalue())
            self.assertEqual(total, events[-1].total_bytes)
            self.assertTrue(connection.source.stream.closed)
            self.assertGreaterEqual(len(connection.source.timeouts), 1)

    def test_physical_truncation_is_rejected_by_length_and_chunk_parser(self):
        for wire in (b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\nhello",
                     b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n8\r\nhello"):
            connection = DetachedConnection(wire)
            with self.subTest(wire=wire), upstream(connection), self.assertRaises(NetworkError):
                Transport().download_to(URL, CountingSink())
            self.assertTrue(connection.closed and connection.source.stream.closed)

    def test_limits_headers_and_unexpected_encodings_fail_without_writing(self):
        for headers in (
                [("Content-Length", "6")], [("Content-Length", "-1")], [("Content-Length", "invalid")],
                [("Content-Length", "2"), ("Content-Length", "2")], [("Content-Encoding", "gzip")],
                [("Content-Encoding", "br")], [("Transfer-Encoding", "gzip")],
                [("Transfer-Encoding", "chunked"), ("Content-Length", "2")], [("Content-Range", "bytes 0-1/8")]):
            connection, sink = Connection(Reply(headers=headers)), CountingSink()
            with self.subTest(headers=headers), upstream(connection), self.assertRaises(DownloadError):
                Transport(max_download_bytes=5).download_to(URL, sink)
            self.assertEqual(0, sink.count)
            self.assertTrue(connection.closed)
        reply = GeneratedReply(6, known=False)
        with upstream(Connection(reply)), self.assertRaises(DownloadError):
            Transport(max_download_bytes=5).download_to(URL, CountingSink())
        with upstream(Connection(Reply(status=206))), self.assertRaises(DownloadError):
            Transport().download_to(URL, CountingSink())

    def test_cancellation_before_network_does_not_resolve_or_open(self):
        token = CancellationToken()
        token.cancel()
        with upstream() as (resolve, construct), self.assertRaises(TransferCancelled):
            Transport().download_to(URL, CountingSink(), cancel=token)
        resolve.assert_not_called()
        construct.assert_not_called()

    def test_cancellation_after_progress_stops_before_another_chunk(self):
        for threshold in (0, CHUNK_SIZE):
            token, events = CancellationToken(), []
            reply, sink = GeneratedReply(CHUNK_SIZE * 3), CountingSink()
            connection = Connection(reply)

            def progress(event):
                events.append(event)
                if event.bytes_received >= threshold:
                    token.cancel()

            with self.subTest(threshold=threshold), upstream(connection), self.assertRaises(TransferCancelled):
                Transport().download_to(URL, sink, progress=progress, cancel=token)
            self.assertEqual(threshold, sink.count)
            self.assertEqual(threshold // CHUNK_SIZE, len(reply.read_sizes))
            self.assertTrue(reply.closed and connection.closed)

    def test_cancellation_during_a_blocked_read_wins_over_network_error(self):
        token = CancellationToken()

        class CancellingReply(GeneratedReply):
            def read(self, count):
                token.cancel()
                raise TimeoutError("synthetic read timeout")

        reply = CancellingReply(4)
        connection = Connection(reply)
        with upstream(connection), self.assertRaises(TransferCancelled):
            Transport().download_to(URL, CountingSink(), cancel=token)
        self.assertTrue(reply.closed and connection.closed)

    def test_cancellation_between_redirect_hops_prevents_followup(self):
        token = CancellationToken()
        connection = Connection(Reply(302, headers=[("Location", "https://cdn.example/book")]))

        def getresponse():
            token.cancel()
            return connection.reply

        connection.getresponse = getresponse
        with upstream(connection) as (resolve, construct), self.assertRaises(TransferCancelled):
            Transport().download_to(URL, CountingSink(), cancel=token)
        self.assertEqual(1, resolve.call_count)
        self.assertEqual(1, construct.call_count)
        self.assertTrue(connection.closed)

    def test_callback_exceptions_are_not_wrapped_as_network_failures(self):
        for error in (OSError("callback failed"), ValueError("callback failed"), TimeoutError("callback failed"),
                      http.client.HTTPException("callback failed")):
            reply = GeneratedReply(CHUNK_SIZE * 2)
            connection = Connection(reply)

            def progress(event):
                if event.bytes_received:
                    raise error

            with self.subTest(error=type(error).__name__), upstream(connection), self.assertRaises(type(error)) as raised:
                Transport().download_to(URL, CountingSink(), progress=progress)
            self.assertIs(error, raised.exception)
            self.assertEqual(1, len(reply.read_sizes))
            self.assertTrue(reply.closed and connection.closed)

    def test_sink_errors_are_local_and_do_not_expose_os_messages(self):
        class FailingSink:
            def write(self, value):
                raise OSError("private path or device details")

        for sink in (FailingSink(), type("ZeroSink", (), {"write": lambda self, data: 0})()):
            reply, connection = GeneratedReply(3), None
            connection = Connection(reply)
            with upstream(connection), self.assertRaises(DownloadError) as raised:
                Transport().download_to(URL, sink)
            self.assertNotIn("private", str(raised.exception))
            self.assertTrue(reply.closed and connection.closed)

    def test_global_deadline_does_not_restart_after_each_read(self):
        now = [10.0]

        class SlowReply(GeneratedReply):
            def read(self, count):
                now[0] += 0.6
                return super().read(count)

        reply, sink = SlowReply(CHUNK_SIZE * 3), CountingSink()
        connection = Connection(reply)
        with upstream(connection), patch("zlib_connector.transport.time.monotonic", side_effect=lambda: now[0]), self.assertRaises(NetworkError) as raised:
            Transport(timeout=1).download_to(URL, sink)
        self.assertEqual("timeout", raised.exception.reason)
        self.assertEqual(CHUNK_SIZE, sink.count)
        self.assertEqual(2, len(reply.read_sizes))
        self.assertTrue(reply.closed and connection.closed)

    def test_real_socket_trickling_cannot_extend_the_response_deadline(self):
        slow = b"x" * 100  # A completed stream takes at least 2.5 seconds.
        cases = (
            (b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n", slow, b""),
            (b"HTTP/1.1 200 OK\r\nX-Slow: ", slow, b"\r\nContent-Length: 1\r\n\r\nx"),
            (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n", b"0" * 100, b"1\r\nx\r\n0\r\n\r\n"),
            (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\nx\r\n0\r\nX-Slow: ", slow, b"\r\n\r\n"),
        )
        for prefix, delayed, suffix in cases:
            with self.subTest(prefix=prefix), trickling_response(prefix, delayed, suffix) as (connection, sent), upstream(connection):
                started = time.monotonic()
                with self.assertRaises(NetworkError) as raised:
                    Transport(timeout=0.15).download_to(URL, CountingSink())
                elapsed = time.monotonic() - started
                self.assertEqual("timeout", raised.exception.reason)
                self.assertLess(len(sent), len(delayed))
                # Allow a wide scheduler margin on loaded Windows CI while
                # still proving we did not wait for the trickling stream to end.
                self.assertLess(elapsed, 1.0)
                self.assertTrue(connection.closed)
                self.assertFalse(any(thread.name == "zlib-transfer-deadline" for thread in threading.enumerate()))

    def test_cancellation_interrupts_real_header_reads_and_joins_guard(self):
        token = CancellationToken()
        slow = b"x" * 100
        with trickling_response(b"HTTP/1.1 200 OK\r\nX-Slow: ", slow,
                                b"\r\nContent-Length: 1\r\n\r\nx") as (connection, sent), upstream(connection):
            timer = threading.Timer(0.08, token.cancel)
            started = time.monotonic()
            timer.start()
            try:
                with self.assertRaises(TransferCancelled):
                    Transport(timeout=5).download_to(URL, CountingSink(), cancel=token)
            finally:
                timer.cancel()
                timer.join()
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertLess(len(sent), len(slow))
            self.assertTrue(connection.closed)
            self.assertFalse(any(thread.name == "zlib-transfer-deadline" for thread in threading.enumerate()))

    def test_credentials_do_not_follow_download_redirect_to_cdn(self):
        transport = Transport()
        seed = Connection(Reply(headers=[("Set-Cookie", "remix_userkey=synthetic; Secure; Path=/"),
                                         ("Set-Cookie", "remix_userid=123; Secure; Path=/")]))
        redirect = Connection(Reply(302, headers=[("Location", "https://cdn.example/eapi/file")]))
        final = Connection(GeneratedReply(10))
        with upstream(seed, redirect, final):
            transport.request("https://books.example/")
            transport.download_to("https://books.example/eapi/file", CountingSink())
        self.assertEqual("synthetic", redirect.calls[0][3]["Remix-userkey"])
        self.assertNotIn("Cookie", final.calls[0][3])
        self.assertNotIn("Remix-userkey", final.calls[0][3])
        self.assertNotIn("Remix-userid", final.calls[0][3])
        self.assertTrue(all(connection.closed for connection in (seed, redirect, final)))

    def test_unsafe_destinations_and_tls_errors_are_not_retried(self):
        for destination in ("http://books.example/file", "https://user:password@books.example/file"):
            connection = Connection(Reply(302, headers=[("Location", destination)]))
            with self.subTest(destination=destination), upstream(connection) as (_, construct), self.assertRaises(NetworkError):
                Transport().download_to(URL, CountingSink())
            self.assertEqual(1, construct.call_count)
            self.assertTrue(connection.closed)
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]), \
                patch("http.client.HTTPSConnection") as construct, self.assertRaises(NetworkError) as raised:
            Transport().download_to(URL, CountingSink())
        self.assertEqual("blocked_address", raised.exception.reason)
        construct.assert_not_called()
        for error, reason in ((ssl.SSLCertVerificationError("synthetic"), "certificate"), (ssl.SSLError("synthetic"), "tls")):
            connection = Connection(Reply())
            connection.request = lambda *args: (_ for _ in ()).throw(error)
            with self.subTest(reason=reason), upstream(connection) as (_, construct), self.assertRaises(NetworkError) as raised:
                Transport().download_to(URL, CountingSink())
            self.assertEqual(reason, raised.exception.reason)
            self.assertEqual(1, construct.call_count)
            self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
