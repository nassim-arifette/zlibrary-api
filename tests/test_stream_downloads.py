"""Streaming client and CLI integration with synthetic in-memory network replies."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from zlib_connector import Book, Client, DownloadError
from zlib_connector.client import Client as HtmlClient
from zlib_connector.cli import main
from zlib_connector.credentials import Credentials
from zlib_connector.transfer import CancellationToken, TransferCancelled, TransferProgress
from test_eapi import ORIGIN, QueueTransport
from session_fixture import isolated_sessions


BOOK = Book("Example", "Author", "pdf", ORIGIN + "book/123/ab12", book_id="123", book_hash="ab12")
DETAILS = {"success": 1, "book": {"id": 123, "hash": "ab12", "title": "Example", "author": "Author", "extension": "pdf"}}
LINK = "https://cdn.example/file?token=synthetic-private-token"
FILE = {"success": 1, "file": {"allowDownload": True, "extension": "pdf", "downloadLink": LINK}}
HTML = b'<a class="addDownloadedBook" href="/dl/synthetic-private-token">Download</a>'
CONTENT = b"%PDF-1.7\n" + b"x" * (2 * 65536 + 17)


def make_client(*responses, html=False):
    login = [{"response": {}}] if html else [{"response": {}}, {"success": 1, "user": {}}]
    transport = QueueTransport([*login, *responses])
    client = (HtmlClient if html else Client)(ORIGIN, transport=transport)
    return client, transport


class StreamDownloadTests(unittest.TestCase):
    def test_reference_stream_reports_exact_bytes_then_returns_a_valid_published_path(self):
        client, transport = make_client(DETAILS, FILE, CONTENT)
        snapshots = []
        with tempfile.TemporaryDirectory() as directory, client:
            folder = Path(directory).resolve()

            def progress(value):
                self.assertIsInstance(value, TransferProgress)
                self.assertEqual(list(folder.glob("*.pdf")), [])
                snapshots.append(value)

            client.login("synthetic", "synthetic")
            path = client.download("123:ab12", folder, progress=progress)
            self.assertIsInstance(path, Path)
            self.assertEqual(path.parent, folder.resolve())
            self.assertEqual(path.read_bytes(), CONTENT)
            self.assertEqual(list(folder.iterdir()), [path])
        self.assertEqual([snapshot.bytes_received for snapshot in snapshots], [0, 65536, 131072, len(CONTENT)])
        self.assertTrue(all(snapshot.total_bytes == len(CONTENT) for snapshot in snapshots))
        self.assertEqual(snapshots[-1].fraction, 1.0)
        self.assertEqual([call[0] for call in transport.calls[2:]],
                         [ORIGIN + "eapi/book/123/ab12", ORIGIN + "eapi/book/123/ab12/file", LINK])
        self.assertTrue(transport.calls[-1][2])
        serialized = json.dumps([snapshot.to_dict() for snapshot in snapshots], allow_nan=False)
        self.assertNotIn("http", serialized)
        self.assertNotIn("synthetic-private-token", serialized)

    def test_complete_book_downloads_without_reloading_metadata_and_html_client_streams_too(self):
        for html in (False, True):
            client, transport = make_client(HTML if html else FILE, CONTENT, html=html)
            snapshots = []
            with self.subTest(html=html), tempfile.TemporaryDirectory() as directory, client:
                client.login("synthetic", "synthetic")
                path = client.download(BOOK, directory, progress=snapshots.append)
                self.assertEqual(path.read_bytes(), CONTENT)
                self.assertEqual(list(Path(directory).resolve().iterdir()), [path])
            self.assertEqual(snapshots[-1].bytes_received, len(CONTENT))
            self.assertEqual(len(transport.calls), 3 if html else 4)
            if not html:
                self.assertEqual(transport.calls[-2][0], ORIGIN + "eapi/book/123/ab12/file")

    def test_pre_cancelled_download_does_not_request_metadata_or_consume_a_link(self):
        for html in (False, True):
            client, transport = make_client(html=html)
            token = CancellationToken()
            token.cancel()
            with self.subTest(html=html), tempfile.TemporaryDirectory() as directory, client:
                client.login("synthetic", "synthetic")
                with self.assertRaises(TransferCancelled):
                    client.download(BOOK if html else "123:ab12", directory, cancel=token)
                self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertEqual(len(transport.calls), 1 if html else 2)

    def test_callback_cancellation_after_a_chunk_removes_partial_and_final_files(self):
        for html in (False, True):
            client, transport = make_client(HTML if html else FILE, CONTENT, html=html)
            token = CancellationToken()
            received = []

            def progress(value):
                received.append(value.bytes_received)
                if value.bytes_received:
                    token.cancel()

            with self.subTest(html=html), tempfile.TemporaryDirectory() as directory, client:
                client.login("synthetic", "synthetic")
                with self.assertRaises(TransferCancelled):
                    client.download(BOOK, directory, progress=progress, cancel=token)
                self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertEqual(received, [0, 65536])
            self.assertEqual(transport.responses, [])

    def test_callback_exception_propagates_unchanged_and_cleans_partial_files(self):
        for html in (False, True):
            client, _ = make_client(HTML if html else FILE, CONTENT, html=html)
            failure = RuntimeError("Synthetic observer failure")

            def progress(value):
                if value.bytes_received:
                    raise failure

            with self.subTest(html=html), tempfile.TemporaryDirectory() as directory, client:
                client.login("synthetic", "synthetic")
                with self.assertRaises(RuntimeError) as caught:
                    client.download(BOOK, directory, progress=progress)
                self.assertIs(caught.exception, failure)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_invalid_options_fail_before_metadata_requests_or_file_creation(self):
        for html in (False, True):
            client, transport = make_client(html=html)
            with self.subTest(html=html), tempfile.TemporaryDirectory() as directory, client:
                client.login("synthetic", "synthetic")
                for options in ({"progress": False}, {"progress": []}, {"cancel": True}, {"cancel": object()}):
                    with self.subTest(options=options), self.assertRaises(ValueError):
                        client.download(BOOK if html else "123:ab12", directory, **options)
                self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertEqual(len(transport.calls), 1 if html else 2)

    def test_unwritable_destination_fails_before_quota_consuming_link_request(self):
        for html in (False, True):
            client, transport = make_client(*(() if html else (DETAILS,)), html=html)
            with self.subTest(html=html), tempfile.TemporaryDirectory() as directory, client:
                client.login("synthetic", "synthetic")
                with patch("zlib_connector.book_files.tempfile.NamedTemporaryFile", side_effect=OSError("synthetic-disk-detail")):
                    with self.assertRaises(DownloadError) as caught:
                        client.download(BOOK if html else "123:ab12", directory)
                self.assertEqual(list(Path(directory).iterdir()), [])
                self.assertNotIn("synthetic-disk-detail", str(caught.exception))
            self.assertEqual(len(transport.calls), 1 if html else 3)
            self.assertFalse(any(call[0].endswith("/file") or call[2] for call in transport.calls))

    def test_receiving_all_bytes_does_not_publish_an_invalid_book(self):
        invalid = b"<html>Not a book</html>"
        client, _ = make_client(FILE, invalid)
        snapshots = []
        with tempfile.TemporaryDirectory() as directory, client:
            client.login("synthetic", "synthetic")
            with self.assertRaises(DownloadError):
                client.download(BOOK, directory, progress=snapshots.append)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(snapshots[-1].bytes_received, len(invalid))
        self.assertEqual(snapshots[-1].fraction, 1.0)


@isolated_sessions
class StreamDownloadCliTests(unittest.TestCase):
    def test_progress_stays_on_stderr_and_no_progress_preserves_path_only_json(self):
        for flag in ("--progress", "--no-progress"):
            client, _ = make_client(DETAILS, FILE, CONTENT)
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as directory, \
                    patch("zlib_connector.cli.Client", return_value=client), \
                    patch("zlib_connector.cli.load_credentials", return_value=Credentials(ORIGIN, "synthetic", "synthetic")), \
                    patch("zlib_connector.cli.prompt", side_effect=AssertionError("Unexpected prompt")), \
                    patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                self.assertEqual(main(["download", "123:ab12", "--output", directory, flag, "--json"]), 0)
                result = json.loads(stdout.getvalue())
                self.assertEqual(set(result), {"path"})
                path = Path(result["path"])
                self.assertEqual(path.read_bytes(), CONTENT)
                self.assertEqual(list(Path(directory).resolve().iterdir()), [path])
            if flag == "--progress":
                self.assertIn("Received", stderr.getvalue())
                self.assertIn("100%", stderr.getvalue())
            else:
                self.assertEqual(stderr.getvalue(), "")
            self.assertNotIn("synthetic-private-token", stderr.getvalue())

    def test_cancelled_cli_returns_130_and_json_error_without_a_published_file(self):
        client, transport = make_client(DETAILS, FILE, TransferCancelled())
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch("zlib_connector.cli.Client", return_value=client), \
                patch("zlib_connector.cli.load_credentials", return_value=Credentials(ORIGIN, "synthetic", "synthetic")), \
                patch("zlib_connector.cli.prompt", side_effect=AssertionError("Unexpected prompt")), \
                patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            self.assertEqual(main(["download", "123:ab12", "--output", directory, "--no-progress", "--json"]), 130)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(stdout.getvalue(), "")
        error = json.loads(stderr.getvalue())
        self.assertEqual({key: error[key] for key in ("error", "message", "code", "exit_code", "quota_may_be_used")},
                         {"error": "TransferCancelled", "message": "Cancelled.", "code": "cancelled", "exit_code": 130,
                          "quota_may_be_used": True})
        self.assertEqual(transport.responses, [])
        self.assertFalse(transport.authenticated)


if __name__ == "__main__":
    unittest.main()
