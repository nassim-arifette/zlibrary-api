"""The TUI settings page writes the same config file as zlib-cli config; synthetic folders only."""
import tempfile
import unittest
from pathlib import Path

from test_tui import TEXTUAL_AVAILABLE, TUITestCase, text
from test_tui_downloads import FakeDownloads

from zlib_connector.settings import UNSET, ConfigStore, SettingsError, load_settings
from zlib_connector.tui.settings_model import lock_reason, prepare, save

if TEXTUAL_AVAILABLE:
    from textual.widgets import Checkbox, Input
    from zlib_connector.tui.app import CollectionsApp
    from zlib_connector.tui.downloads import DownloadDialog
    from zlib_connector.tui.settings import SettingsScreen


class Folder(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.file = self.root / "config.toml"
        self.store = ConfigStore(self.file, source="cli")

    def settings(self, environ=None):
        return load_settings(environ=environ or {}, config_file=self.file, missing_ok=True)


class ModelTests(Folder):
    def test_only_changed_fields_are_written_and_empty_means_unset(self):
        books = self.root / "books"
        books.mkdir()
        settings = self.settings()
        changes = prepare(settings, {"download_dir": str(books), "domain": "", "timeout": "45", "proxy": ""}, False)
        self.assertEqual({"download_dir": str(books), "timeout": 45, "confirm_downloads": False}, changes)
        updated = save(self.store, settings, changes)
        self.assertEqual((books, 45.0, False), (updated.download_dir, updated.timeout, updated.confirm_downloads))
        reread = self.settings()
        self.assertEqual((books, 45.0, False), (reread.download_dir, reread.timeout, reread.confirm_downloads))
        cleared = prepare(reread, {"download_dir": "", "domain": "", "timeout": "45", "proxy": ""}, False)
        self.assertEqual({"download_dir": UNSET}, cleared)
        save(self.store, reread, cleared)
        self.assertIsNone(self.settings().download_dir)

    def test_invalid_values_name_the_field_and_write_nothing(self):
        settings = self.settings()
        for texts, key in (({"timeout": "soon"}, "timeout"), ({"timeout": "900"}, "timeout"),
                           ({"download_dir": str(self.root / "absent")}, "download_dir"),
                           ({"domain": "not a domain"}, "domain")):
            with self.subTest(texts=texts), self.assertRaises(SettingsError) as raised:
                prepare(settings, texts, True)
            self.assertEqual(key, raised.exception.key)
        self.assertFalse(self.file.exists())

    def test_environment_values_are_locked(self):
        settings = self.settings({"ZLIB_TIMEOUT": "12"})
        self.assertIn("ZLIB_TIMEOUT", lock_reason(settings, "timeout"))
        self.assertEqual({}, prepare(settings, {"timeout": "99"}, True))


@unittest.skipUnless(TEXTUAL_AVAILABLE, "Install the optional Textual dependency to test the TUI.")
class PageTests(Folder, TUITestCase):
    async def test_saving_turns_off_the_confirmation_and_moves_downloads(self):
        books = self.root / "books"
        books.mkdir()
        api = FakeDownloads()
        app = CollectionsApp(api, initial_view="favorites", download_dir=self.root, settings=self.settings(),
                             config_store=self.store, fallback_dir=self.root)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.ready(app, pilot)
            await pilot.press("comma")
            await self.until(pilot, lambda: isinstance(app.screen, SettingsScreen))
            self.assertIn("ctrl+s save", text(app, "#hints"))
            app.screen.query_one("#settings-download_dir", Input).value = str(books)
            app.screen.query_one("#settings-confirm_downloads", Checkbox).value = False
            await pilot.press("ctrl+s")
            await self.until(pilot, lambda: not isinstance(app.screen, SettingsScreen))
            self.assertIn("Settings saved", text(app, "#notice"))
            self.assertEqual((books, False), (self.settings().download_dir, self.settings().confirm_downloads))
            await pilot.press("d")
            await self.until(pilot, lambda: text(app, "#download-line").startswith("✓ Saved"))
            self.assertNotIsInstance(app.screen, DownloadDialog)
            self.assertTrue((books / "Synthetic book 1.epub").exists())

    async def test_an_invalid_value_stays_on_the_page_with_its_reason(self):
        app = CollectionsApp(FakeDownloads(), initial_view="favorites", download_dir=self.root, settings=self.settings(),
                             config_store=self.store)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.ready(app, pilot)
            await pilot.press("comma")
            await self.until(pilot, lambda: isinstance(app.screen, SettingsScreen))
            app.screen.query_one("#settings-timeout", Input).value = "soon"
            await pilot.press("ctrl+s")
            await self.until(pilot, lambda: "Invalid timeout" in str(app.screen.query_one("#settings-message").render()))
            self.assertIsInstance(app.screen, SettingsScreen)
            self.assertFalse(self.file.exists())
            await pilot.press("escape")
            await self.until(pilot, lambda: not isinstance(app.screen, SettingsScreen))

    async def test_without_a_config_file_the_page_is_read_only(self):
        app = CollectionsApp(FakeDownloads(), initial_view="favorites", download_dir=self.root, settings=self.settings())
        async with app.run_test(size=(100, 30)) as pilot:
            await self.ready(app, pilot)
            await pilot.press("comma")
            await self.until(pilot, lambda: isinstance(app.screen, SettingsScreen))
            self.assertTrue(app.screen.query_one("#settings-timeout", Input).disabled)
            self.assertIn("--no-config", str(app.screen.query_one("#settings-message").render()))
            self.assertEqual(0, len(app.screen.query("#settings-save")))


if __name__ == "__main__":
    unittest.main()
