"""TUI downloads: an explicit confirmation, one task per confirmation, and a clear outcome. Synthetic only."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from test_tui import TEXTUAL_AVAILABLE, FakeAPI, TUITestCase, text

from zlib_connector.local_api import LocalAPIError

if TEXTUAL_AVAILABLE:
    from zlib_connector.tui.app import CollectionsApp
    from zlib_connector.tui.downloads import DownloadDialog, DownloadRunner, DownloadTarget, progress_text


class FakeDownloads(FakeAPI):
    """Task states advance one step per poll; the saved file is a synthetic placeholder."""

    def __init__(self, states=("running", "succeeded"), *, error=None):
        super().__init__()
        self.states, self.error_on_submit = list(states), error
        self.submitted, self.cancelled, self.saved = [], [], []
        self.release = threading.Event()
        self.release.set()

    def submit_download(self, reference, *, key):
        if self.error_on_submit is not None:
            raise self.error_on_submit
        self.submitted.append((reference, key))
        return {"id": "a" * 32, "state": "queued", "progress": None}

    def download_task(self, identifier):
        self.release.wait(5)
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        progress = {"bytes_received": 512, "total_bytes": 1024} if state == "running" else None
        return {"id": identifier, "state": state, "progress": progress,
                "error_message": "The synthetic transfer failed." if state == "failed" else None}

    def cancel_download(self, identifier):
        self.cancelled.append(identifier)
        self.states = ["cancelled"]
        self.release.set()

    def save_download(self, identifier, folder, *, title):
        path = Path(folder) / (title + ".epub")
        path.write_bytes(b"synthetic")
        self.saved.append((identifier, title))
        return path


@unittest.skipUnless(TEXTUAL_AVAILABLE, "Install the optional Textual dependency to test the TUI.")
class RunnerTests(unittest.TestCase):
    def run_once(self, api, folder):
        updates = []
        runner = DownloadRunner(api, folder, updates.append)
        runner.start(DownloadTarget("7:abc", "Synthetic book 7", "epub", "1 MB"))
        runner._thread.join(10)
        self.assertFalse(runner.active)
        return updates

    def test_a_confirmed_download_is_followed_then_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            api = FakeDownloads()
            updates = self.run_once(api, folder)
            self.assertTrue((Path(folder) / "Synthetic book 7.epub").exists())
        self.assertEqual("7:abc", api.submitted[0][0])
        self.assertTrue(updates[-1].done)
        self.assertTrue(updates[-1].text.startswith("✓ Saved Synthetic book 7.epub"))
        self.assertTrue(any("50%" in update.text for update in updates))

    def test_every_start_uses_a_new_request_key(self):
        with tempfile.TemporaryDirectory() as folder:
            api = FakeDownloads()
            self.run_once(api, folder)
            api.states = ["succeeded"]
            self.run_once(api, folder)
        self.assertEqual(2, len({key for _, key in api.submitted}))

    def test_failures_are_reported_and_never_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            api = FakeDownloads(states=("failed",))
            updates = self.run_once(api, folder)
            self.assertEqual("✗ The synthetic transfer failed.", updates[-1].text)
            self.assertEqual(1, len(api.submitted))
            self.assertEqual([], api.saved)
            off = FakeDownloads(error=LocalAPIError(503, code="downloads_unavailable"))
            self.assertIn("Downloads are off on this server", self.run_once(off, folder)[-1].text)

    def test_an_exhausted_allowance_cancels_the_waiting_task(self):
        with tempfile.TemporaryDirectory() as folder:
            api = FakeDownloads(states=("waiting_quota",))
            updates = self.run_once(api, folder)
        self.assertEqual(["a" * 32], api.cancelled)
        self.assertIn("allowance is used up", updates[-1].text)

    def test_cancel_asks_the_server_and_reports_it(self):
        with tempfile.TemporaryDirectory() as folder:
            api = FakeDownloads(states=("running",))
            api.release.clear()
            updates = []
            runner = DownloadRunner(api, folder, updates.append)
            runner.start(DownloadTarget("7:abc", "Synthetic book 7"))
            deadline = time.monotonic() + 5
            while runner._task_id is None and time.monotonic() < deadline:
                time.sleep(.01)
            runner.cancel()
            runner._thread.join(10)
        self.assertEqual(["a" * 32], api.cancelled)
        self.assertEqual("Download cancelled · Synthetic book 7", updates[-1].text)

    def test_progress_text(self):
        self.assertEqual("↓ Waiting to start · T", progress_text({"state": "queued"}, "T"))
        self.assertEqual("↓ 50% · 1.0 KB of 2.0 KB · T",
                         progress_text({"state": "running", "progress": {"bytes_received": 1024, "total_bytes": 2048}}, "T"))
        self.assertEqual("↓ Starting · T", progress_text({"state": "running", "progress": None}, "T"))


@unittest.skipUnless(TEXTUAL_AVAILABLE, "Install the optional Textual dependency to test the TUI.")
class DownloadKeyTests(TUITestCase):
    async def test_d_asks_first_and_only_a_confirmation_downloads(self):
        with tempfile.TemporaryDirectory() as folder:
            api = FakeDownloads()
            app = CollectionsApp(api, initial_view="favorites", download_dir=Path(folder))
            async with app.run_test(size=(100, 30)) as pilot:
                await self.ready(app, pilot)
                self.assertIn("d download", text(app, "#hints"))
                await pilot.press("d")
                await self.until(pilot, lambda: isinstance(app.screen, DownloadDialog))
                self.assertIn("Synthetic book 1", str(app.screen.query_one("#download-text").render()))
                await pilot.press("escape")
                await self.until(pilot, lambda: not isinstance(app.screen, DownloadDialog))
                self.assertEqual([], api.submitted)
                await pilot.press("d")
                await self.until(pilot, lambda: isinstance(app.screen, DownloadDialog))
                await pilot.press("enter")
                await self.until(pilot, lambda: text(app, "#download-line").startswith("✓ Saved"))
                self.assertEqual([("1:abc", api.submitted[0][1])], api.submitted)
                self.assertTrue((Path(folder) / "Synthetic book 1.epub").exists())

    async def test_without_a_folder_d_is_not_offered(self):
        api = FakeDownloads()
        app = CollectionsApp(api, initial_view="favorites")
        async with app.run_test(size=(100, 30)) as pilot:
            await self.ready(app, pilot)
            self.assertNotIn("download", text(app, "#hints"))
            await pilot.press("d")
            await pilot.pause(.1)
            self.assertNotIsInstance(app.screen, DownloadDialog)
            self.assertEqual([], api.submitted)

    async def test_quitting_during_a_download_asks_for_a_second_press(self):
        with tempfile.TemporaryDirectory() as folder:
            api = FakeDownloads(states=("running",))
            api.release.clear()
            app = CollectionsApp(api, initial_view="favorites", download_dir=Path(folder))
            async with app.run_test(size=(100, 30)) as pilot:
                await self.ready(app, pilot)
                await pilot.press("d")
                await self.until(pilot, lambda: isinstance(app.screen, DownloadDialog))
                await pilot.press("enter")
                await self.until(pilot, lambda: app._downloads.active and api.submitted)
                await pilot.press("q")
                await self.until(pilot, lambda: "Press q again" in text(app, "#notice"))
                self.assertTrue(app.is_running)
                await pilot.press("q")
                await self.until(pilot, lambda: not app.is_running)
            self.assertEqual(["a" * 32], api.cancelled)


if __name__ == "__main__":
    unittest.main()
