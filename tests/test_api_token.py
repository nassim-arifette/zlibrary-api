import concurrent.futures
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from zlib_connector.api_token import load_api_token


class ApiTokenTests(unittest.TestCase):
    def test_token_is_persistent_and_never_replaces_existing_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "api-token"
            first = load_api_token(path)
            contents = path.read_bytes()
            self.assertRegex(first, r"^[A-Za-z0-9_-]{43}$")
            self.assertEqual(load_api_token(path), first)
            self.assertEqual(path.read_bytes(), contents)
            self.assertEqual(list(path.parent.iterdir()), [path])
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_concurrent_creation_publishes_one_complete_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api-token"
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                values = list(pool.map(lambda _: load_api_token(path), range(6)))
            self.assertEqual(len(set(values)), 1)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_invalid_token_errors_do_not_echo_contents_or_rewrite_the_file(self):
        for value in (b"synthetic-secret", b"secret " * 20, b"\xffsynthetic-secret", b"x" * 2000,
                      b"x" * 43 + b"\n" * 2000 + b"synthetic-secret"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "api-token"
                path.write_bytes(value)
                path.chmod(0o600)
                with self.assertRaises(ValueError) as caught:
                    load_api_token(path)
                self.assertNotIn("synthetic-secret", str(caught.exception))
                self.assertEqual(path.read_bytes(), value)

    def test_directory_cannot_be_a_token_file(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            load_api_token(Path(directory))

    @unittest.skipIf(os.name == "nt", "POSIX file permissions")
    def test_world_readable_token_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api-token"
            path.write_text("a" * 43)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                load_api_token(path)

    def test_symlink_token_is_rejected_when_platform_supports_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            actual = Path(directory) / "actual"
            actual.write_text("a" * 43)
            path = Path(directory) / "api-token"
            try:
                path.symlink_to(actual)
            except OSError:
                self.skipTest("Symlink creation is unavailable for this account")
            with self.assertRaises(ValueError):
                load_api_token(path)


if __name__ == "__main__":
    unittest.main()
