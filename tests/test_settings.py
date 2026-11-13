"""The settings resolver: locations, precedence, strict reading and the TOML writer. Offline, temporary files."""
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from session_fixture import SETTINGS_ROOT  # noqa: F401  (isolates the real config folder)
from zlib_connector import Settings, SettingsError, load_settings
from zlib_connector.local_state import default_config_directory
from zlib_connector.settings import KEYS, dumps_config, has_comments, parse_text


WINDOWS = sys.platform == "win32"


class LocationTests(unittest.TestCase):
    def test_windows_uses_appdata_then_the_roaming_folder_of_the_profile(self):
        expected = PureWindowsPath(r"C:\Users\me\AppData\Roaming\zlibrary-api")
        self.assertEqual(expected, PureWindowsPath(default_config_directory(
            {"APPDATA": r"C:\Users\me\AppData\Roaming"}, platform="win32")))
        self.assertEqual(expected, PureWindowsPath(default_config_directory(
            {"USERPROFILE": r"C:\Users\me"}, platform="win32")))
        # A relative APPDATA is ignored.
        self.assertEqual(expected, PureWindowsPath(default_config_directory(
            {"APPDATA": "relative", "USERPROFILE": r"C:\Users\me"}, platform="win32")))

    def test_posix_uses_an_absolute_xdg_config_home_else_home_config(self):
        self.assertEqual(PurePosixPath("/xdg/zlibrary-api"),
                         PurePosixPath(default_config_directory({"XDG_CONFIG_HOME": "/xdg", "HOME": "/home/u"},
                                                                platform="linux")))
        for environ in ({"XDG_CONFIG_HOME": "relative/x", "HOME": "/home/u"}, {"HOME": "/home/u"},
                        {"XDG_CONFIG_HOME": "  ", "HOME": "/home/u"}):
            with self.subTest(environ=environ):
                self.assertEqual(PurePosixPath("/home/u/.config/zlibrary-api"),
                                 PurePosixPath(default_config_directory(environ, platform="linux")))

    def test_minimal_mcp_environment_finds_the_default_file(self):
        native = {"APPDATA": r"C:\Users\me\AppData\Roaming"} if WINDOWS else {"HOME": "/home/u"}
        folder = default_config_directory(native)
        self.assertTrue(str(folder).endswith("zlibrary-api"))
        self.assertTrue(Path(folder).is_absolute())


class Folder(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.file = self.root / "config.toml"

    def write(self, text, *, encoding="utf-8"):
        self.file.write_text(text, encoding=encoding)

    def load(self, overrides=None, environ=None, **options):
        return load_settings(overrides, environ=environ or {}, config_file=self.file, **options)


class PrecedenceTests(Folder):
    def test_defaults_without_file_or_environment(self):
        settings = load_settings(environ={})
        self.assertIsInstance(settings, Settings)
        self.assertEqual((None, 30.0, None, (), (), "http://127.0.0.1:8000/v1", None, "auto"),
                         tuple(getattr(settings, key) for key in KEYS))
        self.assertTrue(all(settings.origins[key] == ("default", None) for key in KEYS))
        self.assertFalse(settings.config_exists)

    def test_cli_beats_environment_beats_file_beats_default_for_every_key(self):
        folder = self.root / "books"
        folder.mkdir()
        other = self.root / "other"
        other.mkdir()
        creds = self.root / "c.toml"
        file_values = {"domain": '"https://file.example/"', "timeout": "60", "download_dir": f'"{_toml(folder)}"',
                       "languages": '["english"]', "formats": '["epub"]', "api_url": '"http://127.0.0.1:9000"',
                       "credentials_file": f'"{_toml(creds)}"', "progress": '"never"'}
        env = {"ZLIB_DOMAIN": "https://env.example", "ZLIB_TIMEOUT": "45", "ZLIB_DOWNLOAD_DIR": str(other),
               "ZLIB_LANGUAGES": "French, german", "ZLIB_FORMATS": "pdf,djvu", "ZLIB_API_URL": "http://localhost:9100/v1",
               "ZLIB_CREDENTIALS": str(other / "c.toml"), "ZLIB_PROGRESS": "Always"}
        cli = {"domain": "https://cli.example/", "timeout": 5, "download_dir": folder, "languages": ["Spanish"],
               "formats": ["djvu"], "api_url": "http://127.0.0.1:9200", "credentials_file": Path("relative.toml")}
        self.write("\n".join(f"{key} = {value}" for key, value in file_values.items()))
        from_file = self.load()
        self.assertEqual(("https://file.example/", 60.0, folder, ("english",), ("epub",), "http://127.0.0.1:9000/v1",
                          creds, "never"), tuple(getattr(from_file, key) for key in KEYS))
        self.assertTrue(all(from_file.origins[key] == ("config", None) for key in KEYS))
        from_env = self.load(environ=env)
        self.assertEqual(("https://env.example/", 45.0, other, ("french", "german"), ("pdf", "djvu"),
                          "http://127.0.0.1:9100/v1", other / "c.toml", "always"), tuple(getattr(from_env, key) for key in KEYS))
        self.assertEqual(("env", "ZLIB_LANGUAGES"), from_env.origins["languages"])
        from_cli = self.load(cli, env, options={"timeout": "--timeout"})
        self.assertEqual(("https://cli.example/", 5.0, folder, ("spanish",), ("djvu",), "http://127.0.0.1:9200/v1",
                          Path("relative.toml").absolute(), "always"), tuple(getattr(from_cli, key) for key in KEYS))
        self.assertEqual(("cli", "--timeout"), from_cli.origins["timeout"])

    def test_empty_environment_values_count_as_unset_and_lists_replace(self):
        self.write('formats = ["epub", "pdf"]\nlanguages = ["english"]\n')
        settings = self.load(environ={"ZLIB_FORMATS": "  ", "ZLIB_LANGUAGES": " , ,", "ZLIB_TIMEOUT": ""})
        self.assertEqual(("epub", "pdf"), settings.formats)
        self.assertEqual(("english",), settings.languages)
        self.assertEqual(30.0, settings.timeout)
        settings = self.load(environ={"ZLIB_FORMATS": "djvu"})
        self.assertEqual(("djvu",), settings.formats)  # Replaced, never merged.
        settings = self.load({"formats": None, "languages": []})
        self.assertEqual(("epub", "pdf"), settings.formats)

    def test_no_config_ignores_only_the_file(self):
        self.write('timeout = 60\nformats = ["epub"]\n')
        settings = self.load(environ={"ZLIB_FORMATS": "pdf"}, use_config=False)
        self.assertEqual((30.0, ("pdf",)), (settings.timeout, settings.formats))
        self.assertEqual(("default", None), settings.origins["timeout"])

    def test_zlib_config_names_the_file_and_must_exist(self):
        self.write("timeout = 12\n")
        settings = load_settings(environ={"ZLIB_CONFIG": str(self.file)})
        self.assertEqual((12.0, "env", self.file), (settings.timeout, settings.config_source, settings.config_path))
        for kwargs in ({"environ": {"ZLIB_CONFIG": str(self.root / "missing.toml")}},
                       {"environ": {}, "config_file": self.root / "missing.toml"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SettingsError) as caught:
                load_settings(**kwargs)
            self.assertEqual("config_invalid", caught.exception.code)
        self.assertFalse(load_settings(environ={}, config_file=self.root / "missing.toml", missing_ok=True).config_exists)


class InvalidValueTests(Folder):
    SECRET = "synthetic-secret-value"

    def test_each_source_names_the_key_and_source_but_never_the_value(self):
        cases = [("timeout", {"ZLIB_TIMEOUT": self.SECRET}, f'timeout = "{self.SECRET}"', {"timeout": math.nan}),
                 ("domain", {"ZLIB_DOMAIN": "http://" + self.SECRET}, f'domain = "http://{self.SECRET}"',
                  {"domain": "ftp://" + self.SECRET}),
                 ("formats", {"ZLIB_FORMATS": self.SECRET}, f'formats = ["{self.SECRET}"]', {"formats": [self.SECRET]}),
                 ("api_url", {"ZLIB_API_URL": "http://" + self.SECRET}, f'api_url = "https://{self.SECRET}"',
                  {"api_url": "http://example.org/" + self.SECRET}),
                 ("download_dir", {"ZLIB_DOWNLOAD_DIR": "rel/" + self.SECRET}, f'download_dir = "rel/{self.SECRET}"',
                  {"download_dir": 5}),
                 ("timeout", {"ZLIB_TIMEOUT": "301"}, "timeout = 0", {"timeout": 301}),
                 ("languages", {"ZLIB_LANGUAGES": "x" * 65}, "languages = 'english'", {"languages": ["\x07"]})]
        for key, env, line, cli in cases:
            for source in ("env", "config", "cli"):
                with self.subTest(key=key, source=source):
                    self.write(line if source == "config" else "")
                    with self.assertRaises(SettingsError) as caught:
                        self.load(cli if source == "cli" else None, env if source == "env" else None)
                    error = caught.exception
                    self.assertEqual((key, source), (error.key, error.source))
                    self.assertEqual("invalid_argument" if source == "cli" else "config_invalid", error.code)
                    self.assertNotIn(self.SECRET, str(error))
                    if source == "env":
                        self.assertIn("ZLIB_", str(error))
                    if source == "config":
                        self.assertIn(key, str(error))

    def test_strict_reading(self):
        cases = {"unknown key": 'colour = "blue"', "secret key": f'password = "{self.SECRET}"',
                 "secret email": f'email = "{self.SECRET}@example.com"',
                 "nested table": f'[domain]\nvalue = "{self.SECRET}"', "nested unknown table": "[proxy]\nurl = 'x'",
                 "wrong type": f"domain = 42\n# {self.SECRET}", "bool timeout": "timeout = true",
                 "relative path": f'credentials_file = "{self.SECRET}.toml"',
                 "invalid toml": f'domain = "{self.SECRET}', "list of numbers": "formats = [1, 2]"}
        for label, text in cases.items():
            with self.subTest(label=label):
                self.write(text)
                with self.assertRaises(SettingsError) as caught:
                    self.load()
                self.assertEqual("config_invalid", caught.exception.code)
                self.assertNotIn(self.SECRET, str(caught.exception))
                if "secret" in label:
                    self.assertIn("never stored in the config file", str(caught.exception))

    def test_bom_is_accepted_and_large_files_are_refused(self):
        self.write("timeout = 9\n", encoding="utf-8-sig")
        self.assertEqual(9.0, self.load().timeout)
        self.file.write_bytes(b"# " + b"x" * (64 * 1024) + b"\n")
        with self.assertRaises(SettingsError) as caught:
            self.load()
        self.assertIn("64 KiB", str(caught.exception))

    def test_unknown_override_key_is_refused(self):
        for key in ("password", "proxy"):
            with self.subTest(key=key), self.assertRaises(SettingsError) as caught:
                self.load({key: "x"})
            self.assertEqual("invalid_argument", caught.exception.code)


def _toml(path):
    return str(path).replace("\\", "\\\\")


class WriterTests(unittest.TestCase):
    def test_round_trip_through_tomllib(self):
        samples = [
            {"domain": "https://z-lib.by/", "timeout": 60, "formats": ["epub", "pdf"]},
            {"timeout": 2.5, "download_dir": r"C:\Users\me\Books \"quoted\"", "languages": ["english", "fran\u00e7ais"]},
            {"credentials_file": "/home/u/\u00e9t\u00e9/c\x01\x1f\x7f.toml", "api_url": "http://127.0.0.1:8000/v1"},
            {"download_dir": "emoji \U0001F4DA and tab\t and newline\n", "languages": ['a"b', "c\\d"]},
            {"timeout": 0.1, "languages": []},
            {},
        ]
        for values in samples:
            with self.subTest(values=values):
                text = dumps_config(values)
                self.assertTrue(text.isascii())
                self.assertTrue(text.startswith("# zlib-cli settings."))
                self.assertEqual(values, tomllib.loads(text))
        self.assertEqual(["domain", "timeout", "formats"],
                         [line.split(" = ")[0] for line in dumps_config(
                             {"formats": ["epub"], "timeout": 1, "domain": "https://a.example/"}).splitlines()[2:]])
        with self.assertRaises(ValueError):
            dumps_config({"timeout": math.inf})

    def test_comment_detection_and_text_parsing(self):
        self.assertFalse(has_comments(dumps_config({"domain": "https://a.example/#x"}).encode()))
        self.assertTrue(has_comments(b'timeout = 5 # short\n'))
        self.assertTrue(has_comments(b'# mine\ntimeout = 5\n'))
        self.assertFalse(has_comments(b"formats = ['#epub']\n"))
        self.assertEqual(["epub", "pdf"], parse_text("formats", ["epub, pdf"]))
        self.assertEqual(60, parse_text("timeout", "60"))
        self.assertEqual(2.5, parse_text("timeout", "2.5"))


class PythonApiTests(unittest.TestCase):
    def test_load_settings_never_reads_credentials_session_or_token(self):
        with patch("zlib_connector.credentials.load_credentials", side_effect=AssertionError), \
                patch("zlib_connector.sessions.SessionStore.read", side_effect=AssertionError), \
                patch("zlib_connector.api_token.read_api_token", side_effect=AssertionError):
            self.assertEqual(10.0, load_settings({"timeout": 10}, environ={}).timeout)


if __name__ == "__main__":
    unittest.main()
