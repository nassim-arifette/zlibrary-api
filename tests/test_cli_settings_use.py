"""How commands use the settings: filters, folders, domains, serve, jobs, tui, MCP. Offline and synthetic."""
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from session_fixture import isolated_sessions
from zlib_connector import credentials as credentials_module, local_state
from zlib_connector.cli import main
from zlib_connector.cli_parser import parser
from zlib_connector.client import Book
from zlib_connector.credentials import Credentials
from zlib_connector.diagnostics import DomainDiagnostic, DomainReport
from zlib_connector.session_data import SavedSession, SessionCookie
from zlib_connector.sessions import SessionStore


ORIGIN = "https://books.example/"
OTHER = "https://other.example/"
BOOKS = [Book("First", "Author", "epub", ORIGIN + "book/1/a1", book_id="1", book_hash="a1")]


class FakeClient:
    instances = []

    def __init__(self, domain="https://z-lib.by/", *, timeout=30):
        self.domain, self.timeout, self.calls = domain, timeout, []
        FakeClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def login(self, email, password, **_):
        self.calls.append(("login",))

    def restore_session(self, store):
        self.calls.append(("restore",))

    def search(self, query, *, filters=None, page=1, limit=20):
        self.calls.append(("search", filters))
        return BOOKS

    def get_book(self, reference):
        self.calls.append(("get_book", reference))
        return BOOKS[0]

    def download(self, book, folder, **_):
        self.calls.append(("download", Path(folder)))
        return Path(folder) / "book.epub"

    def get_history(self, **_):
        self.calls.append(("history",))
        return Mock(to_dict=lambda: {"books": [], "pagination": {}})


def save_session(origin=ORIGIN):
    session = SavedSession(origin, (SessionCookie("remix_userid", "7"), SessionCookie("remix_userkey", "k")),
                           int(time.time()))
    store = SessionStore()
    return store.save(session, expected_generation=store.read().generation)


@isolated_sessions
class SettingsUseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.folder = self.root / "settings"
        self.folder.mkdir()
        self.config = self.folder / "config.toml"
        location = patch.object(local_state, "config_directory", lambda: self.folder)
        location.start()
        self.addCleanup(location.stop)
        self.books = self.root / "books"
        self.books.mkdir()
        FakeClient.instances = []

    def configure(self, **values):
        from zlib_connector.settings import dumps_config
        self.config.write_text(dumps_config(values), encoding="ascii")

    def run_cli(self, *argv, environ=None, credentials=None, loader=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        loader = loader or Mock(return_value=credentials or Credentials(ORIGIN, "synthetic@example.com",
                                                                        "synthetic-password"))
        environment = {"ZLIB_NO_INPUT": "", "ZLIB_DEBUG": ""}
        environment.update(environ or {})
        with patch("zlib_connector.cli.Client", FakeClient), patch("zlib_connector.cli.load_credentials", loader), \
                patch("http.client.HTTPSConnection", side_effect=AssertionError("network")), \
                patch("zlib_connector.cli_terminal.is_terminal", return_value=False), \
                patch.dict(os.environ, environment), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main(list(argv))
        self.loader = loader
        return code, stdout.getvalue(), stderr.getvalue()

    # Filters ----------------------------------------------------------------------------------------------

    def test_configured_filters_apply_to_searches_with_one_notice(self):
        self.configure(formats=["epub"], languages=["english"])
        code, out, err = self.run_cli("search", "dune", "--json")
        self.assertEqual(0, code)
        filters = FakeClient.instances[-1].calls[-1][1]
        self.assertEqual((("epub",), ("english",)), (filters.formats, filters.languages))
        self.assertEqual(1, err.count("Using formats from the config file: epub. Use --format to replace them, or"
                                      " --no-config to ignore the file."))
        self.assertEqual(1, err.count("Using languages from the config file: english."))
        self.assertEqual(1, len(json.loads(out)))
        code, _, err = self.run_cli("search", "dune", "--format", "pdf", "--language", "french")
        filters = FakeClient.instances[-1].calls[-1][1]
        self.assertEqual((("pdf",), ("french",)), (filters.formats, filters.languages))
        self.assertEqual("", err)
        code, _, err = self.run_cli("search", "dune", "--no-config", environ={"ZLIB_LANGUAGES": "german,french"})
        filters = FakeClient.instances[-1].calls[-1][1]
        self.assertEqual(((), ("german", "french")), (filters.formats, filters.languages))
        self.assertIn("Using languages from ZLIB_LANGUAGES: german, french.", err)

    def test_direct_reference_ignores_configured_filters_and_succeeds(self):
        self.configure(formats=["pdf"], download_dir=str(self.books))
        code, out, err = self.run_cli("download", "1:a1", "--json")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual({"path": str(self.books / "book.epub")}, json.loads(out))
        self.assertNotIn("search", [call[0] for call in FakeClient.instances[-1].calls])
        code, _, err = self.run_cli("download", "1:a1", "--format", "pdf", "--json")
        self.assertEqual(2, code)
        self.assertIn("direct book reference", json.loads(err)["message"])

    # Folders ----------------------------------------------------------------------------------------------

    def test_download_dir_is_the_default_output_and_must_exist(self):
        self.configure(download_dir=str(self.books))
        code, _, _ = self.run_cli("download", "dune", "--pick", "1")
        self.assertEqual(0, code)
        self.assertEqual(("download", self.books), FakeClient.instances[-1].calls[-1])
        other = self.root / "other"
        other.mkdir()
        self.run_cli("download", "dune", "--pick", "1", "--output", str(other))
        self.assertEqual(("download", other), FakeClient.instances[-1].calls[-1])
        missing = self.root / "missing"
        code, _, err = self.run_cli("download", "dune", "--pick", "1", "--json",
                                    environ={"ZLIB_DOWNLOAD_DIR": str(missing)})
        error = json.loads(err)
        self.assertEqual((2, "config_invalid", "download_dir", "env"), (code, error["code"], error["key"],
                                                                         error["source"]))
        self.assertIn("ZLIB_DOWNLOAD_DIR", error["message"])
        self.assertNotIn(str(missing), err)

    def test_download_dir_is_the_default_output_for_lists(self):
        self.configure(download_dir=str(self.books))
        seen = {}

        def batch(args, **_):
            seen["output"] = args.output
            return 0

        with patch("zlib_connector.cli.run_batch_command", side_effect=batch):
            self.assertEqual(0, self.run_cli("download", "a", "b", "--pick", "1", "--yes", "--no-report")[0])
        self.assertEqual(self.books, seen["output"])

    def test_batch_list_that_is_the_resolved_credentials_file_is_refused(self):
        creds = self.folder / "credentials.toml"
        creds.write_text('email = "synthetic@example.com"\n', encoding="utf-8")
        code, _, err = self.run_cli("download", "-a", str(creds), "--dry-run", "--json")
        self.assertEqual(2, code)
        self.assertIn("credentials file", json.loads(err)["message"])
        named = self.root / "named.toml"
        named.write_text('email = "synthetic@example.com"\n', encoding="utf-8")
        code, _, err = self.run_cli("download", "-a", str(named), "--dry-run", "--json",
                                    environ={"ZLIB_CREDENTIALS": str(named)})
        self.assertIn("credentials file", json.loads(err)["message"])

    def test_jobs_file_output_and_api_url_come_from_settings(self):
        self.configure(download_dir=str(self.books), api_url="http://127.0.0.1:9123/v1", timeout=12)
        seen = []

        class API:
            def __init__(self, url, token, *, timeout):
                seen.append((url, timeout))

            def file(self, task, folder):
                seen.append(folder)
                return folder / "book.epub"

        with patch("zlib_connector.cli_jobs.LocalAPI", API):
            code, out, _ = self.run_cli("jobs", "file", "0" * 32)
        self.assertEqual(0, code)
        self.assertEqual([("http://127.0.0.1:9123/v1", 12.0), self.books], seen)

    def test_tui_uses_the_configured_api_url(self):
        self.configure(api_url="http://localhost:9124")
        seen = {}

        def refuse(args):
            seen["url"], seen["timeout"] = args.api_url, args.timeout
            return None

        with patch("zlib_connector.cli.run_tui", side_effect=refuse):
            self.assertEqual(0, self.run_cli("tui")[0])
        self.assertEqual({"url": "http://127.0.0.1:9124/v1", "timeout": 30.0}, seen)

    # Domains ----------------------------------------------------------------------------------------------

    def test_doctor_ignores_configured_domain_and_prints_the_hint(self):
        self.configure(domain=OTHER)
        report = DomainReport((DomainDiagnostic("https://z-lib.sk/", True, "2026-11-12T00:00:00Z", 5),))
        with patch("zlib_connector.cli.diagnose_domains", return_value=report) as diagnose:
            code, out, _ = self.run_cli("doctor")
        self.assertEqual(0, code)
        self.assertIsNone(diagnose.call_args.args[0])
        self.assertIn(f"Configured domain: {OTHER} (config)", out)
        self.assertIn("To use it: zlib-cli config set domain https://z-lib.sk/", out)
        with patch("zlib_connector.cli.diagnose_domains", return_value=report) as diagnose:
            code, out, _ = self.run_cli("doctor", "--json", environ={"ZLIB_DOMAIN": OTHER})
        self.assertIsNone(diagnose.call_args.args[0])
        self.assertNotIn("Configured", out)
        with patch("zlib_connector.cli.diagnose_domains", return_value=report) as diagnose:
            self.run_cli("--domain", ORIGIN, "doctor")
        self.assertEqual([ORIGIN], diagnose.call_args.args[0])

    def test_domain_chain_and_mismatch_messages_name_the_source(self):
        save_session(ORIGIN)
        code, _, _ = self.run_cli("history", "--json")
        self.assertEqual(ORIGIN, FakeClient.instances[-1].domain)
        cases = [({"argv": ("--domain", OTHER)}, "--domain"), ({"environ": {"ZLIB_DOMAIN": OTHER}}, "ZLIB_DOMAIN"),
                 ({"config": OTHER}, "the config file (domain)")]
        for case, source in cases:
            with self.subTest(source=source):
                if "config" in case:
                    self.configure(domain=case["config"])
                code, _, err = self.run_cli(*case.get("argv", ()), "history", "--json", environ=case.get("environ"))
                error = json.loads(err)
                self.assertEqual((2, "session_domain_mismatch"), (code, error["code"]))
                self.assertIn(f"another domain than the one set by {source}.", error["message"])
                self.config.unlink(missing_ok=True)
        named = self.root / "named.toml"
        named.write_text("", encoding="utf-8")
        loader = Mock(return_value=Credentials(OTHER, "e", "p"))
        code, _, err = self.run_cli("--credentials", str(named), "history", "--json", loader=loader)
        self.assertIn("set by the credentials file (domain)", json.loads(err)["message"])
        # A named file without domain no longer forces https://z-lib.by/ over the session's domain.
        loader = Mock(return_value=Credentials("https://z-lib.by/", "e", "p", has_domain=False))
        code, _, _ = self.run_cli("--credentials", str(named), "history", "--json", loader=loader)
        self.assertEqual((0, ORIGIN), (code, FakeClient.instances[-1].domain))

    def test_configured_domain_is_used_without_a_session_and_by_anonymous_commands(self):
        self.configure(domain=OTHER)
        self.run_cli("search", "dune", "--anonymous", "--json")
        self.assertEqual(OTHER, FakeClient.instances[-1].domain)
        self.run_cli("history", "--json")
        self.assertEqual(OTHER, FakeClient.instances[-1].domain)
        self.run_cli("--no-config", "search", "dune", "--anonymous", "--json")
        self.assertEqual("https://z-lib.by/", FakeClient.instances[-1].domain)

    def test_status_and_logout_note_a_configured_domain_instead_of_failing(self):
        save_session(ORIGIN)
        self.configure(domain=OTHER)
        code, out, err = self.run_cli("status", "--json")
        self.assertEqual((0, "stored"), (code, json.loads(out)["state"]))
        self.assertIn(f"Note: the saved session belongs to {ORIGIN}, not to the domain set by the config file", err)
        code, _, err = self.run_cli("--domain", OTHER, "status", "--json")
        self.assertEqual((2, "session_domain_mismatch"), (code, json.loads(err)["code"]))
        code, out, err = self.run_cli("logout", "--json", environ={"ZLIB_DOMAIN": OTHER})
        self.assertEqual(0, code)
        self.assertIn("set by ZLIB_DOMAIN", err)

    # Credentials ------------------------------------------------------------------------------------------

    def test_legacy_credentials_notice_once_and_configured_file_must_exist(self):
        legacy = self.root / "legacy.toml"
        legacy.write_text('email = "synthetic@example.com"\n', encoding="utf-8")
        with patch.object(credentials_module, "DEFAULT_FILE", legacy):
            code, _, err = self.run_cli("history", "--json")
        self.assertEqual(0, code)
        self.assertEqual(1, err.count("Note: using credentials.local.toml beside the installed package. Move it to "
                                      f"{self.folder / 'credentials.toml'}"))
        self.loader.assert_called_once_with(None)
        (self.folder / "credentials.toml").write_text("", encoding="utf-8")
        with patch.object(credentials_module, "DEFAULT_FILE", legacy):
            code, _, err = self.run_cli("history", "--json")
        self.assertEqual("", err)
        code, _, err = self.run_cli("history", "--json", environ={"ZLIB_CREDENTIALS": str(self.root / "none.toml")})
        error = json.loads(err)
        self.assertEqual((2, "config_invalid", "credentials_file", "env"), (code, error["code"], error["key"],
                                                                             error["source"]))
        named = self.root / "named.toml"
        named.write_text("", encoding="utf-8")
        self.configure(credentials_file=str(named))
        self.run_cli("history", "--json")
        self.loader.assert_called_once_with(named)

    # serve ------------------------------------------------------------------------------------------------

    def serve(self, *argv, environ=None):
        seen = {}

        class Server:
            server_address = ("127.0.0.1", 1)

            def __init__(self, client, *, token, host, port, allowed_origins):
                seen["listen"] = (host, port)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def enable_downloads(self, folder, *, max_tasks):
                seen["downloads"] = folder

            def serve_forever(self):
                pass

        save_session(ORIGIN)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("zlib_connector.http_cli.create_server", Server), patch("zlib_connector.cli.Client", FakeClient), \
                patch("zlib_connector.http_cli.load_api_token", return_value="t" * 43), \
                patch("zlib_connector.cli.load_credentials", side_effect=AssertionError), \
                patch.dict(os.environ, environ or {}), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main(["serve", *argv])
        return code, seen, stderr.getvalue()

    def test_serve_downloads_needs_a_configured_folder_that_never_enables_tasks_alone(self):
        code, seen, err = self.serve("--downloads", "--json")
        self.assertEqual((2, "invalid_argument"), (code, json.loads(err)["code"]))
        self.assertIn("No download folder is configured", json.loads(err)["message"])
        self.configure(download_dir=str(self.books))
        code, seen, _ = self.serve()
        self.assertEqual(0, code)
        self.assertNotIn("downloads", seen)
        code, seen, _ = self.serve("--downloads")
        self.assertEqual(self.books, seen["downloads"])
        code, seen, err = self.serve("--downloads", "--download-dir", str(self.books), "--json")
        self.assertEqual(2, code)
        self.assertIn("not allowed with", json.loads(err)["message"])

    def test_serve_listens_on_the_configured_api_url(self):
        code, seen, _ = self.serve()
        self.assertEqual(("127.0.0.1", 8000), seen["listen"])
        self.configure(api_url="http://127.0.0.2:9300/v1")
        code, seen, _ = self.serve()
        self.assertEqual(("127.0.0.2", 9300), seen["listen"])
        code, seen, _ = self.serve("--port", "9400")
        self.assertEqual(("127.0.0.1", 9400), seen["listen"])
        code, seen, _ = self.serve(environ={"ZLIB_API_URL": "http://localhost:9500"})
        self.assertEqual(("127.0.0.1", 9500), seen["listen"])

    # MCP --------------------------------------------------------------------------------------------------

    def test_mcp_uses_the_configured_domain_and_timeout_and_refuses_a_bad_file_in_one_line(self):
        from mcp_fixture import World
        from zlib_connector.mcp_cli import run_mcp

        class NoRead(io.RawIOBase):
            def readable(self):
                return True

            def readinto(self, buffer):
                return 0

        def start(*argv):
            world, stderr = World(), io.StringIO()
            code = run_mcp(parser().parse_args(list(argv)), client_factory=world.factory, stdin=NoRead(),
                           stdout=io.BytesIO(), stderr=stderr)
            return code, world, stderr.getvalue()

        self.configure(domain=OTHER, timeout=45)
        code, world, _ = start("mcp")
        self.assertEqual((0, OTHER, 45.0), (code, world.clients[0].domain, world.clients[0].timeout))
        save_session(ORIGIN)
        code, world, err = start("mcp")
        self.assertEqual(1, code)
        self.assertIn("set by the config file (domain)", err)
        self.config.write_text("timeout = 'x'\n", encoding="utf-8")
        code, world, err = start("mcp")
        self.assertEqual((1, 1), (code, len(err.strip().splitlines())))
        self.assertTrue(err.startswith("zlib-cli mcp: "))


if __name__ == "__main__":
    unittest.main()
