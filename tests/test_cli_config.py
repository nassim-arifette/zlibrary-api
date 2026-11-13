"""`zlib-cli config`: text and JSON, protected writes, notes, repair. Offline, temporary folders."""
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import tomllib
import unittest
from unittest.mock import patch

from session_fixture import isolated_sessions
from zlib_connector import local_state, settings as settings_module
from zlib_connector.cli import main
from zlib_connector.session_data import SavedSession, SessionCookie
from zlib_connector.session_lock import PrivateLockTimeout
from zlib_connector.sessions import SessionStore


@isolated_sessions
class ConfigCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.folder = self.root / "settings" / "zlibrary-api"
        self.file = self.folder / "config.toml"
        location = patch.object(local_state, "config_directory", lambda: self.folder)
        location.start()
        self.addCleanup(location.stop)
        # config never reads credentials or the API token.
        for target in ("zlib_connector.credentials.load_credentials", "zlib_connector.cli.load_credentials",
                       "zlib_connector.api_token.read_api_token", "zlib_connector.api_token.load_api_token"):
            guard = patch(target, side_effect=AssertionError("must not be read"))
            guard.start()
            self.addCleanup(guard.stop)
        self.books = self.root / "Books"
        self.books.mkdir()

    def run_cli(self, *argv, environ=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, environ or {}), patch("sys.stdout", stdout), patch("sys.stderr", stderr), \
                patch("http.client.HTTPSConnection", side_effect=AssertionError("network")), \
                patch("http.client.HTTPConnection", side_effect=AssertionError("network")):
            code = main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_path_before_and_after_creation(self):
        code, out, err = self.run_cli("config", "path")
        self.assertEqual((0, str(self.file)), (code, out.strip()))
        self.assertIn("not created yet", err)
        self.assertFalse(self.folder.exists())
        code, out, _ = self.run_cli("config", "path", "--json")
        value = json.loads(out)
        self.assertEqual({"path": str(self.file), "exists": False, "source": "default",
                          "credentials_path": str(self.folder / "credentials.toml"), "credentials_exists": False},
                         value)
        other = self.root / "other.toml"
        code, out, _ = self.run_cli("--config", str(other), "config", "path", "--json")
        self.assertEqual(("cli", str(other)), (json.loads(out)["source"], json.loads(out)["path"]))
        code, out, _ = self.run_cli("config", "path", "--json", environ={"ZLIB_CONFIG": str(other)})
        self.assertEqual("env", json.loads(out)["source"])

    def test_set_get_list_unset_in_text_and_json(self):
        code, out, err = self.run_cli("config", "set", "formats", "EPUB,pdf")
        self.assertEqual(0, code)
        self.assertEqual(f"Set formats = epub, pdf in {self.file}.", out.strip())
        self.assertEqual("", err)
        code, out, _ = self.run_cli("config", "set", "timeout", "60", "--json")
        self.assertEqual({"key": "timeout", "value": 60, "previous": None, "path": str(self.file)}, json.loads(out))
        code, out, _ = self.run_cli("config", "set", "timeout", "2.5", "--json")
        self.assertEqual((2.5, 60), (json.loads(out)["value"], json.loads(out)["previous"]))
        self.run_cli("config", "set", "download_dir", str(self.books))
        self.run_cli("config", "set", "domain", "https://Z-LIB.sk")
        self.run_cli("config", "set", "languages", "English", "french", "english")
        document = tomllib.loads(self.file.read_text(encoding="ascii"))
        self.assertEqual({"domain": "https://z-lib.sk/", "timeout": 2.5, "download_dir": str(self.books),
                          "languages": ["english", "french"], "formats": ["epub", "pdf"]}, document)
        self.assertEqual(["domain", "timeout", "download_dir", "languages", "formats"],
                         [line.split(" = ")[0] for line in self.file.read_text().splitlines()[2:]])

        code, out, _ = self.run_cli("config", "get", "formats")
        self.assertEqual("epub\npdf\n", out)
        code, out, _ = self.run_cli("config", "get", "timeout")
        self.assertEqual("2.5\n", out)
        code, out, _ = self.run_cli("config", "get", "api_url", "--json")
        self.assertEqual({"key": "api_url", "value": "http://127.0.0.1:8000/v1", "origin": "default"}, json.loads(out))
        code, out, _ = self.run_cli("config", "get", "credentials_file")
        self.assertEqual((0, ""), (code, out))
        code, out, _ = self.run_cli("config", "get", "languages", "--json", environ={"ZLIB_LANGUAGES": "german"})
        self.assertEqual({"key": "languages", "value": ["german"], "origin": "env", "variable": "ZLIB_LANGUAGES"},
                         json.loads(out))

        code, out, _ = self.run_cli("config", "list")
        rows = [line.split(maxsplit=1) for line in out.splitlines()]
        self.assertEqual(["domain", "https://z-lib.sk/"], rows[0])
        self.assertEqual([["credentials_file", "(not set)"], ["progress", "auto"]], rows[-2:])
        code, out, _ = self.run_cli("--timeout", "7", "config", "list", "--show-origin",
                                    environ={"ZLIB_LANGUAGES": "german"})
        lines = {line.split()[0]: line for line in out.splitlines()}
        self.assertTrue(lines["timeout"].endswith("cli --timeout"))
        self.assertTrue(lines["languages"].endswith("env ZLIB_LANGUAGES"))
        self.assertTrue(lines["formats"].endswith("config"))
        self.assertTrue(lines["api_url"].endswith("default"))
        code, out, _ = self.run_cli("config", "list", "--json")
        value = json.loads(out)
        self.assertEqual((str(self.file), True), (value["path"], value["exists"]))
        self.assertEqual({"value": ["epub", "pdf"], "origin": "config"}, value["settings"]["formats"])
        self.assertEqual({"value": None, "origin": "default"}, value["settings"]["credentials_file"])

        code, out, _ = self.run_cli("config", "unset", "formats", "--json")
        self.assertEqual({"key": "formats", "removed": True, "previous": ["epub", "pdf"], "path": str(self.file)},
                         json.loads(out))
        code, out, _ = self.run_cli("config", "unset", "formats")
        self.assertEqual((0, f"formats was not set in {self.file}."), (code, out.strip()))
        code, out, _ = self.run_cli("config", "unset", "timeout")
        self.assertEqual(f"Removed timeout from {self.file}.", out.strip())

    def test_folder_is_private_and_file_private_after_set(self):
        self.run_cli("config", "set", "timeout", "10")
        self.assertTrue(self.file.is_file())
        if os.name != "nt":
            self.assertEqual(0o700, stat.S_IMODE(self.folder.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(self.file.stat().st_mode))
        self.assertEqual([], [name for name in os.listdir(self.folder) if name.startswith(".config-")])

    def test_usage_errors(self):
        cases = [(("config", "set", "password", "x"), "never stored"), (("config", "get", "proxy"), "Unknown setting"),
                 (("config", "set", "timeout", "1", "2"), "exactly one value"),
                 (("config", "set", "timeout", "abc"), "Invalid value for timeout"),
                 (("config", "set", "download_dir", str(self.root / "missing")), "existing folder"),
                 (("config", "set", "formats", ","), "at least one value"),
                 (("config", "list", "--no-config"), "omit --no-config"),
                 (("--config", "a.toml", "--no-config", "config", "path"), "not allowed with")]
        for argv, text in cases:
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv, "--json")
                error = json.loads(err)
                self.assertEqual((2, ""), (code, out))
                self.assertIn(error["code"], ("invalid_argument",))
                self.assertIn(text, error["message"])
        self.assertFalse(self.file.exists())

    def test_explicit_file_parent_must_exist_and_missing_file_is_empty(self):
        missing = self.root / "nowhere" / "config.toml"
        code, _, err = self.run_cli("--config", str(missing), "config", "set", "timeout", "5", "--json")
        self.assertEqual((2, "config_invalid"), (code, json.loads(err)["code"]))
        explicit = self.root / "mine.toml"
        code, out, _ = self.run_cli("--config", str(explicit), "config", "get", "timeout")
        self.assertEqual((0, "30\n"), (code, out))
        code, _, _ = self.run_cli("config", "set", "timeout", "5", environ={"ZLIB_CONFIG": str(explicit)})
        self.assertEqual(0, code)
        self.assertEqual({"timeout": 5}, tomllib.loads(explicit.read_text()))
        self.assertFalse(self.folder.exists())  # Only the default folder is ever created.

    def test_comment_environment_and_session_notes(self):
        self.folder.mkdir(parents=True)
        self.file.write_text("# mine\ntimeout = 5 # short\n", encoding="utf-8")
        code, _, err = self.run_cli("config", "set", "formats", "epub", environ={"ZLIB_FORMATS": "pdf"})
        self.assertEqual(0, code)
        self.assertIn("Note: comments in the config file were not kept.", err)
        self.assertIn("Note: ZLIB_FORMATS is set and overrides this value in this environment.", err)
        self.assertEqual({"timeout": 5, "formats": ["epub"]}, tomllib.loads(self.file.read_text()))
        SessionStore().save(SavedSession("https://z-lib.by/", (SessionCookie("remix_userid", "1"),
                                                               SessionCookie("remix_userkey", "k")), int(time.time())),
                            expected_generation=None)
        code, _, err = self.run_cli("config", "set", "domain", "https://z-lib.sk/")
        self.assertIn("Note: the saved session belongs to https://z-lib.by/. Account commands will stop", err)
        code, _, err = self.run_cli("config", "set", "domain", "https://z-lib.by/")
        self.assertNotIn("saved session", err)

    def test_repair_a_bad_value_of_the_same_key_only(self):
        self.folder.mkdir(parents=True)
        self.file.write_text('timeout = "soon"\nformats = ["epub"]\n', encoding="utf-8")
        code, _, err = self.run_cli("search", "dune", "--json")
        error = json.loads(err)
        self.assertEqual((2, "config_invalid", "timeout", "config"), (code, error["code"], error["key"],
                                                                      error["source"]))
        self.assertEqual("Run: zlib-cli config unset timeout", error["hint"])
        code, _, err = self.run_cli("config", "set", "formats", "pdf", "--json")
        self.assertEqual(2, code)
        self.assertIn("Fix or remove the config file first", json.loads(err)["message"])
        code, out, _ = self.run_cli("config", "unset", "timeout", "--json")
        self.assertEqual({"key": "timeout", "removed": True, "previous": None, "path": str(self.file)}, json.loads(out))
        self.assertEqual({"formats": ["epub"]}, tomllib.loads(self.file.read_text()))
        self.file.write_text('timeout = "soon"\n', encoding="utf-8")
        self.assertEqual(0, self.run_cli("config", "set", "timeout", "9")[0])
        self.file.write_text('timeout = "unterminated\n', encoding="utf-8")
        code, _, err = self.run_cli("config", "set", "timeout", "9", "--json")
        self.assertIn("Fix or remove the config file first", json.loads(err)["message"])

    def test_linked_config_file_is_refused_for_writing(self):
        self.folder.mkdir(parents=True)
        target = self.root / "real.toml"
        target.write_text("timeout = 5\n", encoding="utf-8")
        try:
            os.symlink(target, self.file)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are not available")
        code, out, _ = self.run_cli("config", "get", "timeout")
        self.assertEqual("5\n", out)  # Reading follows links.
        code, _, err = self.run_cli("config", "set", "timeout", "6", "--json")
        self.assertEqual(2, code)
        self.assertIn("refuses a linked config file", json.loads(err)["message"])
        self.assertEqual("timeout = 5\n", target.read_text())

    def test_conflict_when_the_file_changes_before_replace(self):
        self.run_cli("config", "set", "timeout", "5")
        original = settings_module.ConfigStore._read
        calls = []

        def changing(store):
            calls.append(1)
            if len(calls) == 2:
                self.file.write_text("timeout = 77\n", encoding="utf-8")  # A writer that ignores the lock.
            return original(store)

        with patch.object(settings_module.ConfigStore, "_read", changing):
            code, _, err = self.run_cli("config", "set", "timeout", "6", "--json")
        error = json.loads(err)
        self.assertEqual((3, "config_conflict", True), (code, error["code"], error["retryable"]))
        self.assertEqual("timeout = 77\n", self.file.read_text())
        self.assertEqual([], [name for name in os.listdir(self.folder) if name.startswith(".config-")])

    def test_busy_lock(self):
        with patch("zlib_connector.settings.private_lock", side_effect=PrivateLockTimeout):
            code, _, err = self.run_cli("config", "set", "timeout", "6", "--json")
        error = json.loads(err)
        self.assertEqual((3, "config_busy", True), (code, error["code"], error["retryable"]))

    def test_config_never_touches_the_network_or_reads_credentials(self):
        # The guards of setUp raise if credentials or the token are read; the patched connections refuse network.
        for argv in (("config", "path"), ("config", "list"), ("config", "get", "domain"),
                     ("config", "set", "credentials_file", str(self.root / "c.toml")),
                     ("config", "unset", "credentials_file")):
            with self.subTest(argv=argv):
                self.assertEqual(0, self.run_cli(*argv)[0])


if __name__ == "__main__":
    unittest.main()
