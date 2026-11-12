"""A legacy code page on redirected stdout (cp1252 on Windows) never crashes a listing."""
import io
import json
import unittest
from unittest.mock import patch

from zlib_connector.cli import main
from zlib_connector.client import Book


ORIGIN = "https://books.example/"
TITLE = "Война и мир"  # Cyrillic, absent from cp1252.
BOOK = Book(TITLE, "Лев Толстой", "epub", ORIGIN + "book/1/a1",
            book_id="1", book_hash="a1")


class Client:
    domain = ORIGIN

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def search(self, *args, **kwargs):
        return [BOOK]

    def get_popular(self):
        return [BOOK]


def legacy_stream():
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline="\n")


class LegacyCodePageTests(unittest.TestCase):
    def run_cli(self, argv):
        stdout, stderr = legacy_stream(), legacy_stream()
        with patch("zlib_connector.cli.Client", Client), \
                patch("http.client.HTTPSConnection", side_effect=AssertionError("network")), \
                patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main(argv)
        stdout.flush()
        stderr.flush()
        return code, stdout.buffer.getvalue().decode("cp1252"), stderr.buffer.getvalue().decode("cp1252")

    def test_json_round_trips_the_title(self):
        for argv in (["search", "war", "--anonymous", "--json"], ["popular", "--anonymous", "--json"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(argv)
                self.assertEqual((0, ""), (code, err))
                self.assertTrue(out.isascii())
                self.assertEqual(TITLE, json.loads(out)[0]["title"])

    def test_text_escapes_what_the_code_page_lacks(self):
        code, out, err = self.run_cli(["search", "war", "--anonymous"])
        self.assertEqual((0, ""), (code, err))
        self.assertIn("\\u0412", out)
        self.assertIn("1:a1", out)

    def test_errors_never_crash_either(self):
        code, out, err = self.run_cli(["search", "В" * 600, "--anonymous", "--json"])
        self.assertEqual((2, ""), (code, out))
        self.assertEqual("invalid_argument", json.loads(err)["code"])


if __name__ == "__main__":
    unittest.main()
