"""The optional TUI launcher must not enter account login or affect core CLI."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import builtins
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from zlib_connector.cli import main
from zlib_connector.cli_tui import VIEWS


@contextmanager
def terminal(interactive=True):
    # The CLI's terminal test also checks the Windows console mode, so patch the test itself.
    with patch("zlib_connector.cli_terminal.is_terminal", return_value=interactive):
        yield


class TUILaunchTests(unittest.TestCase):
    def invoke(self, argv):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors), \
                patch("zlib_connector.cli.AccountSession", side_effect=AssertionError("Account files accessed")):
            code = main(argv)
        return code, output.getvalue(), errors.getvalue()

    def test_help_needs_no_optional_import_token_or_terminal(self):
        original = builtins.__import__

        def importing(name, *args, **kwargs):
            if name == "textual" or name.startswith("textual.") or name == "tui.app":
                raise AssertionError("Optional UI imported by help")
            return original(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=importing), self.assertRaises(SystemExit) as exited:
            self.invoke(["tui", "--help"])
        self.assertEqual(0, exited.exception.code)

    def test_help_describes_every_starting_view_and_the_search_page_size(self):
        pages = []
        for argv in (["--help"], ["tui", "--help"]):
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(argv)
            pages.append(" ".join(output.getvalue().split()))
        self.assertIn("browse collections, search and discover books in an interactive terminal", pages[0])
        self.assertIn("{" + ",".join(VIEWS) + "}", pages[1])
        self.assertIn("home reads nothing and search opens the search form (default: home)", pages[1])
        self.assertIn("favorites, history and search page size", pages[1])

    @unittest.skipUnless(importlib.util.find_spec("textual"), "Install the optional TUI extra")
    def test_starting_views_match_the_tui_pages(self):
        from zlib_connector.tui.views import ROOTS, START_VIEWS
        self.assertEqual(START_VIEWS, VIEWS)
        self.assertEqual(("home", *ROOTS), VIEWS)

    def test_every_starting_view_reaches_the_app_and_unknown_views_are_rejected(self):
        app_factory = Mock()
        ui_module, catalog_module = types.ModuleType("zlib_connector.tui.app"), types.ModuleType("zlib_connector.catalog_api")
        ui_module.CollectionsApp = app_factory
        catalog_module.CatalogAPI = Mock()
        modules = {"zlib_connector.tui": types.ModuleType("zlib_connector.tui"),
                   "zlib_connector.tui.app": ui_module, "zlib_connector.catalog_api": catalog_module}
        self.assertEqual("home", VIEWS[0])
        for view in VIEWS:
            with self.subTest(view=view), patch.dict(sys.modules, modules), redirect_stdout(io.StringIO()), \
                    redirect_stderr(io.StringIO()), terminal(), \
                    patch("zlib_connector.cli.AccountSession", side_effect=AssertionError("Account access")):
                self.assertEqual(0, main(["tui", "--token-file", "chosen-token", "--view", view]))
                self.assertEqual(view, app_factory.call_args.kwargs["initial_view"])
        with patch.dict(sys.modules, modules), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                terminal(), patch("zlib_connector.cli.AccountSession", side_effect=AssertionError("Account access")):
            self.assertEqual(0, main(["tui", "--token-file", "chosen-token"]))
        self.assertEqual("home", app_factory.call_args.kwargs["initial_view"])
        for unknown in ("recently", "similar", "details"):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(2, main(["tui", "--view", unknown]))
        self.assertEqual(len(VIEWS) + 1, app_factory.call_count)

    def test_noninteractive_launch_fails_before_reading_token(self):
        code, _, error = self.invoke(["tui", "--token-file", "nonexistent-token"])
        self.assertEqual(2, code)  # input_required
        self.assertIn("interactive terminal", error)

    def test_invalid_page_size_fails_before_optional_import(self):
        for value in ("0", "51"):
            with self.subTest(value=value):
                code, _, error = self.invoke(["tui", "--page-size", value])
                self.assertEqual(2, code)  # invalid_argument
                self.assertIn("page size", error)

    def test_missing_textual_has_install_hint_without_account_access(self):
        original = builtins.__import__

        def importing(name, *args, **kwargs):
            if name == "tui.app":
                raise ModuleNotFoundError("No module named textual", name="textual")
            return original(name, *args, **kwargs)

        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors), terminal(), \
                patch("builtins.__import__", side_effect=importing), \
                patch("zlib_connector.cli.AccountSession", side_effect=AssertionError("Account access")):
            code = main(["tui"])
        self.assertEqual(1, code)
        self.assertIn('python -m pip install ".[tui]"', errors.getvalue())

    def test_launch_uses_local_api_and_restores_no_color_environment(self):
        api = Mock()
        api_factory = Mock(return_value=api)
        app = Mock()
        seen = []

        def app_factory(client, **kwargs):
            seen.append((client, kwargs, os.environ.get("NO_COLOR")))
            return app

        ui_module, catalog_module = types.ModuleType("zlib_connector.tui.app"), types.ModuleType("zlib_connector.catalog_api")
        ui_module.CollectionsApp = app_factory
        catalog_module.CatalogAPI = api_factory
        modules = {"zlib_connector.tui": types.ModuleType("zlib_connector.tui"),
                   "zlib_connector.tui.app": ui_module, "zlib_connector.catalog_api": catalog_module}
        for saved in (None, "chosen"):
            with self.subTest(saved=saved), patch.dict(sys.modules, modules), patch.dict(os.environ, {}, clear=False):
                if saved is None:
                    os.environ.pop("NO_COLOR", None)
                else:
                    os.environ["NO_COLOR"] = saved
                with terminal(), patch("zlib_connector.cli.AccountSession", side_effect=AssertionError("Account access")):
                    code = main(["--timeout", "3", "tui", "--api-url", "http://127.0.0.1:8910/v1",
                                 "--token-file", "chosen-token", "--view", "history", "--page-size", "7", "--no-color"])
                self.assertEqual(0, code)
                self.assertEqual(saved, os.environ.get("NO_COLOR"))
                self.assertEqual((api, {"initial_view": "history", "page_size": 7}, "1"), seen[-1])
                api_factory.assert_called_with("http://127.0.0.1:8910/v1", Path("chosen-token"), timeout=3)
        self.assertEqual(2, app.run.call_count)

    def test_missing_token_explains_server_setup_without_reflecting_path(self):
        ui_module = types.ModuleType("zlib_connector.tui.app")
        ui_module.CollectionsApp = Mock(side_effect=AssertionError("UI must not start"))
        modules = {"zlib_connector.tui": types.ModuleType("zlib_connector.tui"), "zlib_connector.tui.app": ui_module}
        with tempfile.TemporaryDirectory() as folder, patch.dict(sys.modules, modules):
            missing = Path(folder) / "private-token-path"
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors), terminal(), \
                    patch("zlib_connector.cli.AccountSession", side_effect=AssertionError("Account access")):
                code = main(["tui", "--token-file", str(missing)])
            self.assertEqual(1, code)
            self.assertIn("Start zlib-cli serve", errors.getvalue())
            self.assertIn("--token-file", errors.getvalue())
            self.assertNotIn(str(missing), errors.getvalue())
            self.assertFalse(missing.exists())


class PrivateServerTests(unittest.TestCase):
    """Without a running server, the TUI starts a private one for its session and stops it on exit."""

    def launch(self, argv, *, listening):
        events = []
        ui_module, catalog_module = types.ModuleType("zlib_connector.tui.app"), types.ModuleType("zlib_connector.catalog_api")
        app = Mock()
        app.run.side_effect = lambda: events.append("app ran")
        ui_module.CollectionsApp = Mock(return_value=app)
        catalog_module.CatalogAPI = Mock()
        modules = {"zlib_connector.tui": types.ModuleType("zlib_connector.tui"),
                   "zlib_connector.tui.app": ui_module, "zlib_connector.catalog_api": catalog_module}
        server = Mock(server_address=("127.0.0.1", 54321))
        server.__enter__ = Mock(return_value=server)
        server.__exit__ = Mock(side_effect=lambda *_: events.append("server closed"))
        server.shutdown.side_effect = lambda: events.append("server stopped")
        server.serve_forever.side_effect = lambda: events.append("serving")
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        account = Mock(domain="https://example.invalid")
        account.authenticate.side_effect = lambda *_, **__: events.append("signed in")
        with tempfile.TemporaryDirectory() as folder, patch.dict(sys.modules, modules), \
                patch.dict(os.environ, {"ZLIB_NO_CONFIG": "1"}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), terminal(), \
                patch("zlib_connector.cli_tui.state_directory", return_value=Path(folder)), \
                patch("zlib_connector.cli_tui._listening", return_value=listening), \
                patch("zlib_connector.http_api.create_server", return_value=server) as create, \
                patch("zlib_connector.eapi.Client", return_value=client), \
                patch("zlib_connector.api_token.load_api_token", return_value="t" * 43), \
                patch("zlib_connector.cli_session.AccountSession", return_value=account):
            code = main(["--no-config", *argv])
            token_file = Path(folder) / "api-token"
        return code, events, create, catalog_module.CatalogAPI, token_file

    def test_no_running_server_starts_a_private_one_for_the_session(self):
        code, events, create, api_factory, token_file = self.launch(["tui"], listening=False)
        self.assertEqual(0, code)
        self.assertEqual(0, create.call_args.kwargs["port"])
        self.assertEqual("127.0.0.1", create.call_args.kwargs["host"])
        api_factory.assert_called_once()
        self.assertEqual(("http://127.0.0.1:54321/v1", token_file), api_factory.call_args.args)
        self.assertEqual(["signed in", "app ran", "server stopped", "server closed"],
                         [event for event in events if event != "serving"])

    def test_a_running_server_is_reused(self):
        code, events, create, api_factory, _ = self.launch(["tui"], listening=True)
        self.assertEqual(0, code)
        create.assert_not_called()
        self.assertEqual(["app ran"], events)

    def test_an_explicit_server_or_the_opt_out_never_starts_one(self):
        for argv in (["tui", "--no-auto-server"], ["tui", "--api-url", "http://127.0.0.1:8911/v1"],
                     ["tui", "--token-file", "chosen-token"]):
            with self.subTest(argv=argv):
                code, events, create, _, _ = self.launch(argv, listening=False)
                self.assertEqual(0, code)
                create.assert_not_called()
