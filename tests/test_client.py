import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from zlib_connector import AuthenticationError, Book, ChallengeRequired, DownloadError, FormatChangedError, NetworkError
from zlib_connector.client import Client, parse_html, validate_book
from zlib_connector.transport import Response, public_addresses, validate_url
from zlib_connector.cli import main
from zlib_connector.credentials import Credentials
from streaming_fixture import MemoryDownloadTransport
from session_fixture import isolated_sessions

ORIGIN = "https://books.example/"
CARDS = b'''<div id="searchResultBox"><div class="book-item"><z-bookcard href="/book/12/abc" extension="PDF" year="2026" language="French" filesize="2 MB"><img src="ignored"><div slot="title">A &amp; <b>B</b></div><div slot="author"><a>Author</a></div></z-bookcard></div><z-bookcard href="https://other.example/book/1" extension="pdf"><div slot="title">Foreign</div></z-bookcard><z-bookcard href="/book/3/x" extension="exe"><div slot="title">Unsupported</div></z-bookcard></div>'''

class FakeTransport(MemoryDownloadTransport):
    def __init__(self):
        self.authenticated = False
        self.calls = []
        self.responses = []
        self.login_body = b'{"response":{}}'
        self.accept_login = True

    def close(self):
        self.authenticated = False

    def request(self, url, *, form=None, download=False):
        self.calls.append((url, form, download))
        if form is not None:
            self.authenticated = self.accept_login
            return Response(self.login_body, url)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, Response) else Response(value, url)

@isolated_sessions
class ClientTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.client = Client(ORIGIN, transport=self.transport)

    def login(self):
        self.client.login("synthetic@example.com", "synthetic-password")

    def test_requires_login(self):
        with self.assertRaises(AuthenticationError):
            self.client.search("book")

    def test_failed_login_clears_session_and_redacts_site_error(self):
        self.transport.login_body = b'{"response":{"validationError":"secret-cookie-do-not-show"}}'
        with self.assertRaises(AuthenticationError) as error:
            self.login()
        self.assertNotIn("secret-cookie", str(error.exception))
        self.assertFalse(self.transport.authenticated)

    def test_missing_cookie_is_not_success(self):
        self.transport.accept_login = False
        with self.assertRaises(AuthenticationError):
            self.login()

    def test_login_format_change_and_challenge_are_distinct(self):
        self.transport.login_body = b'[]'
        with self.assertRaises(FormatChangedError):
            self.login()
        self.transport.login_body = b'<title>Just a moment...</title>'
        with self.assertRaises(ChallengeRequired):
            self.login()

    def test_search_parses_nested_slots_and_encodes_query(self):
        self.login()
        self.transport.responses.append(CARDS)
        books = self.client.search("author / title?", extension="pdf", page=2)
        self.assertEqual(len(books), 1)
        self.assertEqual(books[0].title, "A & B")
        self.assertEqual(books[0].authors, "Author")
        self.assertEqual(books[0].extension, "pdf")
        url = self.transport.calls[-1][0]
        self.assertIn("author%20%2F%20title%3F", url)
        self.assertIn("page=2", url)
        self.assertNotIn("synthetic", url)

    def test_empty_results_and_changed_format(self):
        self.login()
        self.transport.responses.extend([b'<div class="notFound">Nothing</div>', b'<form>Sign in</form>'])
        self.assertEqual(self.client.search("unknown"), [])
        with self.assertRaises(FormatChangedError):
            self.client.search("unknown")

    def test_remote_metadata_cannot_inject_terminal_controls(self):
        page = b'<z-bookcard href="/book/1/x" extension="pdf"><div slot="title">A\x1b[31m\nB</div></z-bookcard>'
        book = parse_html(page, ORIGIN).books[0]
        self.assertNotIn('\x1b', book.title)
        self.assertNotIn('\n', book.title)

    def test_search_cross_origin_redirect_is_not_accepted(self):
        self.login()
        self.transport.responses.append(Response(CARDS, "https://other.example/"))
        with self.assertRaises(FormatChangedError):
            self.client.search("book")

    def test_download_validates_content_and_never_overwrites(self):
        self.login()
        book = Book('../../CON: bad?', "Author", "pdf", ORIGIN + "book/12/abc")
        self.transport.responses.extend([b'<a class="btn addDownloadedBook" href="/dl/token">Download</a>', b'%PDF-1.7\nsynthetic'])
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path(tmp)/"existing.pdf"
            previous.write_bytes(b"keep")
            target = self.client.download(book, tmp)
            self.assertEqual(target.parent, Path(tmp).resolve())
            self.assertNotIn(":", target.name)
            self.assertTrue(target.read_bytes().startswith(b"%PDF-"))
            self.assertEqual(previous.read_bytes(), b"keep")
            self.assertEqual(len(list(Path(tmp).glob("*.part"))), 0)

    def test_html_instead_of_book_creates_no_file(self):
        self.login()
        self.transport.responses.extend([b'<a class="addDownloadedBook" href="/dl/1">Download</a>', b'<html>Quota</html>'])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DownloadError):
                self.client.download(Book("Book", "", "pdf", ORIGIN+"book/1/x"), tmp)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_write_failure_cleans_partial_file(self):
        self.login()
        self.transport.responses.extend([b'<a class="addDownloadedBook" href="/dl/1">Download</a>', b'%PDF-1.7\n'])
        with tempfile.TemporaryDirectory() as tmp, patch("zlib_connector.book_files.os.link", side_effect=OSError):
            with self.assertRaises(DownloadError):
                self.client.download(Book("Book", "", "pdf", ORIGIN+"book/1/x"), tmp)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_quota_or_missing_link_fails_cleanly(self):
        self.login()
        self.transport.responses.append(b'<html>Download limit exceeded</html>')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DownloadError):
                self.client.download(Book("Book", "", "pdf", ORIGIN+"book/1/x"), tmp)

    def test_close_discards_session(self):
        self.login()
        self.client.close()
        self.assertFalse(self.transport.authenticated)
        with self.assertRaises(AuthenticationError):
            self.client.search("book")

    def test_foreign_book_is_rejected_before_request(self):
        self.login()
        before = len(self.transport.calls)
        with self.assertRaises(ValueError):
            self.client.download(Book("Book", "", "pdf", "https://other.example/book/1"))
        self.assertEqual(before, len(self.transport.calls))

    def test_epub_and_djvu_validation(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
        validate_book(data.getvalue(), "epub")
        validate_book(b"AT&TFORM\x00\x00\x00\x00DJVU", "djvu")
        with self.assertRaises(DownloadError):
            validate_book(b"PKnot-an-epub", "epub")

    def test_private_dns_cleartext_and_credential_urls_blocked(self):
        for url in ("http://books.example/", "https://user:pass@books.example/", "https://books.example:444/"):
            with self.assertRaises(NetworkError):
                validate_url(url)
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "::ffff:127.0.0.1", "::1"):
            with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", (ip, 443))]), self.assertRaises(NetworkError):
                public_addresses("books.example")

    def test_cli_json_download_uses_selected_result(self):
        book = Book("Book", "Author", "pdf", ORIGIN+"book/1/x")
        stdout = io.StringIO()
        with patch("zlib_connector.cli.load_credentials", return_value=Credentials()), patch("zlib_connector.cli.Client") as constructor, patch("zlib_connector.cli.prompt", return_value="synthetic@example.com"), patch("getpass.getpass", return_value="synthetic-password"), patch("sys.stdout", stdout):
            client = constructor.return_value.__enter__.return_value
            client.search.return_value = [book]
            client.download.return_value = Path("book.pdf")
            self.assertEqual(main(["download", "Book", "--pick", "1", "--json"]), 0)
            client.download.assert_called_once()
        self.assertEqual(stdout.getvalue().strip(), '{"path": "book.pdf"}')

    def test_cli_invalid_pick_rejected_before_prompt(self):
        with patch("zlib_connector.cli.prompt") as prompt, patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(["download", "Book", "--pick", "0"]), 1)
            prompt.assert_not_called()

if __name__ == "__main__":
    unittest.main()
