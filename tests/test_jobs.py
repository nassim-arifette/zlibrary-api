"""Durable queue behavior using a synthetic account and temporary files only."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from zlib_connector.account import DownloadQuota
from zlib_connector.errors import NetworkError, RateLimitError
from zlib_connector.jobs import DownloadManager, JobBusyError, JobConflictError, JobError, JobStateError
from zlib_connector.jobs.store import TaskStore, load_scope_key
from zlib_connector.transfer import TransferProgress


CONTENT = b"%PDF-1.7\nSynthetic task download"


class TaskClient:
    domain = "https://books.example/"

    def __init__(self):
        self.scope = "b" * 64
        self.remaining = 5
        self.downloads = []
        self.quota_calls = 0
        self.quota_failure = None
        self.started = threading.Event()
        self.release = None
        self.published = threading.Event()
        self.publication_gate = None
        self.failure = None
        self.refresh_failure = False
        self.change_account = False
        self.last_token = None

    def account_fingerprint(self, key):
        return self.scope

    def get_quota(self):
        self.quota_calls += 1
        if self.quota_failure:
            raise self.quota_failure
        if self.refresh_failure and self.downloads:
            raise self.refresh_failure if isinstance(self.refresh_failure, Exception) else NetworkError("synthetic-private-refresh")
        return DownloadQuota(None, None, self.remaining)

    def download(self, reference, destination, *, progress, cancel):
        self.last_token = cancel
        self.downloads.append(reference)
        self.started.set()
        progress(TransferProgress(5, len(CONTENT), 0.5))
        if self.release is not None:
            while not self.release.wait(0.01):
                cancel.check()
        cancel.check()
        if self.failure:
            raise self.failure
        path = Path(destination) / "Example.pdf"
        path.write_bytes(CONTENT)
        self.published.set()
        if self.publication_gate is not None and not self.publication_gate.wait(5):
            raise AssertionError("Publication test did not release its worker")
        progress(TransferProgress(len(CONTENT), len(CONTENT), 1))
        if self.change_account:
            self.scope = "c" * 64
        return path


class DownloadManagerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.client = TaskClient()

    def manager(self, **kwargs):
        manager = DownloadManager(self.client, self.folder, **kwargs)
        self.addCleanup(manager.close)
        return manager

    def wait_task(self, manager, task_id, states):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = manager.get(task_id)
            if task.state in states:
                return task
            time.sleep(0.01)
        self.fail("Task did not reach " + str(states) + "; state=" + manager.get(task_id).state)

    def test_completed_task_returns_a_safe_snapshot_and_a_pinned_file(self):
        manager = self.manager()
        task = manager.submit("123:ab12", idempotency_key="download-1")
        done = self.wait_task(manager, task.id, {"succeeded"})
        self.assertEqual(done.reference, "123:ab12")
        self.assertEqual(done.progress.bytes_received, len(CONTENT))
        self.assertEqual(manager.list().tasks, (done,))
        with manager.open_file(task.id) as file:
            self.assertEqual(file.stream.read(), CONTENT)
            self.assertEqual(file.content_type, "application/pdf")
        encoded = json.dumps(done.to_dict())
        self.assertNotIn(str(self.folder), encoded)
        self.assertNotIn("receipt", encoded)
        self.assertNotIn("http", encoded)
        self.assertEqual(done.to_dict()["outcome"], "completed")

    def test_concurrent_duplicate_submissions_create_only_one_transfer_and_conflicts_fail(self):
        self.client.release = threading.Event()
        manager = self.manager()
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: manager.submit("123:ab12", idempotency_key="same-request"), range(12)))
        self.assertEqual(len({task.id for task in results}), 1)
        replay = manager.submit("https://books.example/book/123/ab12/title", idempotency_key="same-request")
        self.assertEqual(replay.id, results[0].id)
        with self.assertRaises(JobConflictError):
            manager.submit("456:cd34", idempotency_key="same-request")
        self.client.release.set()
        self.wait_task(manager, replay.id, {"succeeded"})
        self.assertEqual(self.client.downloads, ["123:ab12"])

    def test_state_polling_and_cancellation_remain_responsive_during_blocked_network(self):
        self.client.release = threading.Event()
        manager = self.manager()
        first = manager.submit("123:ab12", idempotency_key="first-task")
        self.assertTrue(self.client.started.wait(3))
        second = manager.submit("456:cd34", idempotency_key="second-task")
        with ThreadPoolExecutor(max_workers=1) as executor:
            self.assertEqual(executor.submit(manager.get, first.id).result(timeout=1).state, "running")
            self.assertEqual(executor.submit(manager.list).result(timeout=1).total, 2)
            queued_cancel = executor.submit(manager.cancel, second.id).result(timeout=1)
            active_cancel = executor.submit(manager.cancel, first.id).result(timeout=1)
        self.assertEqual(queued_cancel.state, "cancelled")
        self.assertTrue(active_cancel.cancel_requested)
        self.assertIsNotNone(active_cancel.progress)
        self.wait_task(manager, first.id, {"cancelled"})
        self.assertEqual(self.client.downloads, ["123:ab12"])
        self.assertEqual(manager.cancel(second.id), queued_cancel)
        self.assertFalse(list((self.folder / first.id).glob("*.pdf")))

    def test_zero_quota_pauses_dispatch_until_explicit_resume(self):
        self.client.remaining = 0
        manager = self.manager()
        first = manager.submit("123:ab12", idempotency_key="first-task")
        self.wait_task(manager, first.id, {"waiting_quota"})
        second = manager.submit("456:cd34", idempotency_key="second-task")
        self.assertEqual(manager.get(second.id).state, "queued")
        self.assertEqual(self.client.downloads, [])
        self.assertFalse((self.folder / first.id).exists())
        self.assertTrue(manager.list().to_dict()["paused"])
        self.client.remaining = 2
        self.assertEqual(manager.resume(), {"resumed": 1})
        self.assertFalse(manager.list().paused)
        self.wait_task(manager, first.id, {"succeeded"})
        self.wait_task(manager, second.id, {"succeeded"})
        self.assertEqual(self.client.downloads, ["123:ab12", "456:cd34"])

    def test_unknown_quota_does_not_block_and_refresh_failure_does_not_undo_success(self):
        self.client.remaining = None
        self.client.refresh_failure = True
        manager = self.manager()
        task = manager.submit("123:ab12", idempotency_key="unknown-quota")
        self.wait_task(manager, task.id, {"succeeded"})
        manager.close()
        self.assertEqual(self.client.quota_calls, 2)
        self.assertEqual(self.client.downloads, ["123:ab12"])

    def test_rate_limits_durably_pause_even_an_empty_queue_without_retrying_the_failed_task(self):
        for stage in ("quota", "download"):
            with self.subTest(stage=stage):
                root = self.folder / stage
                root.mkdir()
                client = TaskClient()
                if stage == "quota":
                    client.quota_failure = RateLimitError("synthetic-private-limit")
                else:
                    client.failure = RateLimitError("synthetic-private-limit")
                manager = DownloadManager(client, root)
                self.addCleanup(manager.close)
                first = manager.submit("123:ab12", idempotency_key="limited-task")
                failed = self.wait_task(manager, first.id, {"failed"})
                self.assertEqual(failed.error_code, "RateLimitError")
                self.assertTrue(manager._store.paused)
                self.assertNotIn("synthetic-private-limit", json.dumps(failed.to_dict()))
                manager.close()
                client.failure = client.quota_failure = None
                reopened = DownloadManager(client, root)
                self.addCleanup(reopened.close)
                self.assertTrue(reopened._store.paused)
                self.assertTrue(reopened.list().paused)
                following = reopened.submit("456:cd34", idempotency_key="after-restart")
                self.assertEqual(following.state, "queued")
                self.assertEqual(reopened.resume(), {"resumed": 0})
                self.wait_task(reopened, following.id, {"succeeded"})
                self.assertEqual(reopened.get(first.id).state, "failed")
                self.assertEqual(client.downloads, (["123:ab12"] if stage == "download" else []) + ["456:cd34"])

    def test_rate_limit_during_refresh_preserves_success_and_pauses_following_tasks(self):
        self.client.refresh_failure = RateLimitError("synthetic-private-limit")
        manager = self.manager()
        first = manager.submit("123:ab12", idempotency_key="first-task")
        self.wait_task(manager, first.id, {"succeeded"})
        deadline = time.monotonic() + 3
        while not manager._store.paused and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(manager._store.paused)
        self.assertTrue(manager.list().to_dict()["paused"])
        second = manager.submit("456:cd34", idempotency_key="second-task")
        self.assertEqual(manager.get(first.id).state, "succeeded")
        self.assertEqual(manager.get(second.id).state, "queued")
        self.assertEqual(self.client.downloads, ["123:ab12"])
        self.client.refresh_failure = False
        self.assertEqual(manager.resume(), {"resumed": 0})
        self.wait_task(manager, second.id, {"succeeded"})

    def test_late_cancel_after_publication_preserves_success_and_the_file(self):
        self.client.publication_gate = threading.Event()
        manager = self.manager()
        task = manager.submit("123:ab12", idempotency_key="late-cancel")
        self.assertTrue(self.client.published.wait(3))
        requested = manager.cancel(task.id)
        self.assertTrue(requested.cancel_requested)
        self.client.publication_gate.set()
        done = self.wait_task(manager, task.id, {"succeeded"})
        self.assertTrue(done.cancel_requested)
        with manager.open_file(task.id) as file:
            self.assertEqual(file.stream.read(), CONTENT)

    def test_orderly_close_after_publication_records_success_before_releasing_the_lease(self):
        self.client.publication_gate = threading.Event()
        manager = self.manager()
        task = manager.submit("123:ab12", idempotency_key="close-after-publish")
        self.assertTrue(self.client.published.wait(3))
        with ThreadPoolExecutor(max_workers=1) as executor:
            closing = executor.submit(manager.close)
            try:
                deadline = time.monotonic() + 2
                while not self.client.last_token.is_cancelled and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(self.client.last_token.is_cancelled)
            finally:
                self.client.publication_gate.set()
            closing.result(timeout=3)
        with TaskStore(self.folder, self.client.scope) as store:
            done = store.get(task.id)
            self.assertEqual(done.state, "succeeded")
            self.assertEqual((self.folder / task.id / done.receipt.name).read_bytes(), CONTENT)

    def test_progress_is_live_without_rewriting_the_durable_record_per_chunk(self):
        self.client.release = threading.Event()
        manager = self.manager()
        task = manager.submit("123:ab12", idempotency_key="live-progress")
        self.assertTrue(self.client.started.wait(3))
        deadline = time.monotonic() + 2
        while manager.get(task.id).progress is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(manager.get(task.id).progress.bytes_received, 5)
        self.assertIsNone(manager._store.get(task.id).progress)
        self.client.release.set()
        self.assertEqual(self.wait_task(manager, task.id, {"succeeded"}).progress.bytes_received, len(CONTENT))

    def test_preexisting_task_directory_fails_before_download_and_keeps_unrelated_files(self):
        client_lock = threading.Lock()
        manager = self.manager(client_lock=client_lock)
        with client_lock:
            task = manager.submit("123:ab12", idempotency_key="existing-directory")
            directory = self.folder / task.id
            directory.mkdir()
            unrelated = directory / "keep.txt"
            unrelated.write_text("keep")
        failed = self.wait_task(manager, task.id, {"failed"})
        self.assertEqual(failed.error_code, "JobFileError")
        self.assertEqual(self.client.downloads, [])
        self.assertEqual(unrelated.read_text(), "keep")

    def test_close_interrupts_active_work_keeps_queue_and_restart_does_not_retry_it(self):
        self.client.release = threading.Event()
        manager = self.manager()
        first = manager.submit("123:ab12", idempotency_key="first-task")
        self.assertTrue(self.client.started.wait(3))
        second = manager.submit("456:cd34", idempotency_key="second-task")
        manager.close()
        with self.assertRaises(JobBusyError):
            manager.get(first.id)
        self.client.release.set()
        reopened = self.manager()
        self.assertEqual(reopened.get(first.id).state, "interrupted")
        self.wait_task(reopened, second.id, {"succeeded"})
        self.assertEqual(self.client.downloads, ["123:ab12", "456:cd34"])
        self.assertEqual(reopened.submit("123:ab12", idempotency_key="first-task").state, "interrupted")

    def test_running_journal_after_crash_is_interrupted_without_download_retry(self):
        key = load_scope_key(self.folder)
        with TaskStore(self.folder, self.client.account_fingerprint(key)) as store:
            task = store.create("123:ab12", "crashed-task")
            store.update(replace(task, state="running"))
        manager = self.manager()
        self.assertEqual(manager.get(task.id).state, "interrupted")
        self.assertEqual(manager.resume(), {"resumed": 0})
        self.assertEqual(self.client.downloads, [])
        self.assertEqual(self.client.quota_calls, 0)

    def test_single_journal_owner_is_enforced(self):
        manager = self.manager()
        with self.assertRaises(JobBusyError):
            DownloadManager(self.client, self.folder)
        manager.close()
        self.manager().close()

    def test_state_write_failure_stops_dispatch_before_any_upstream_request(self):
        manager = self.manager()
        attempted = threading.Event()

        def failed_update(task):
            attempted.set()
            raise JobError("synthetic-private-storage")

        with patch.object(manager._store, "update", side_effect=failed_update):
            task = manager.submit("123:ab12", idempotency_key="first-task")
            self.assertTrue(attempted.wait(3))
            with self.assertRaises(JobBusyError):
                manager.submit("456:cd34", idempotency_key="second-task")
            self.assertEqual(manager.get(task.id).state, "queued")
        self.assertEqual(self.client.quota_calls, 0)
        self.assertEqual(self.client.downloads, [])

    def test_failure_to_record_success_keeps_the_file_and_stops_all_further_dispatch(self):
        self.client.release = threading.Event()
        manager = self.manager()
        original_update = manager._store.update
        failed = threading.Event()

        def update(task):
            if task.state == "succeeded":
                failed.set()
                raise JobError("synthetic-private-storage")
            return original_update(task)

        with patch.object(manager._store, "update", side_effect=update):
            first = manager.submit("123:ab12", idempotency_key="first-task")
            self.assertTrue(self.client.started.wait(3))
            second = manager.submit("456:cd34", idempotency_key="second-task")
            self.client.release.set()
            self.assertTrue(failed.wait(3))
            self.assertEqual(manager.get(first.id).state, "running")
            self.assertEqual(manager.get(second.id).state, "queued")
            self.assertEqual((self.folder / first.id / "Example.pdf").read_bytes(), CONTENT)
            with self.assertRaises(JobBusyError):
                manager.resume()
            manager.close()
        self.assertEqual(self.client.downloads, ["123:ab12"])
        with TaskStore(self.folder, self.client.scope) as store:
            self.assertEqual(store.get(first.id).state, "interrupted")
            self.assertEqual(store.get(second.id).state, "queued")

    def test_cancellation_is_not_signalled_until_its_request_has_been_recorded(self):
        self.client.release = threading.Event()
        manager = self.manager()
        task = manager.submit("123:ab12", idempotency_key="cancel-recording")
        self.assertTrue(self.client.started.wait(3))
        with patch.object(manager._store, "update", side_effect=JobError("synthetic-private-storage")):
            with self.assertRaises(JobError):
                manager.cancel(task.id)
        self.assertFalse(self.client.last_token.is_cancelled)
        self.assertFalse(manager.get(task.id).cancel_requested)
        self.client.release.set()
        self.wait_task(manager, task.id, {"succeeded"})

    def test_account_change_in_a_download_response_stops_queue_and_does_not_expose_artifact(self):
        self.client.change_account = True
        manager = self.manager()
        task = manager.submit("123:ab12", idempotency_key="first-task")
        failed = self.wait_task(manager, task.id, {"failed"})
        self.assertEqual(failed.error_code, "AccountChanged")
        self.assertFalse(failed.to_dict()["file_available"])
        self.assertTrue((self.folder / task.id / "Example.pdf").exists())
        with self.assertRaises(JobStateError):
            with manager.open_file(task.id):
                self.fail("A changed account exposed a file")
        with self.assertRaises(JobBusyError):
            manager.submit("456:cd34", idempotency_key="second-task")

    def test_failed_transfer_has_safe_error_and_is_not_retried_by_resume_or_key_replay(self):
        self.client.failure = NetworkError("synthetic-private-upstream")
        manager = self.manager()
        task = manager.submit("123:ab12", idempotency_key="failed-task")
        failed = self.wait_task(manager, task.id, {"failed"})
        self.assertEqual(failed.error_code, "NetworkError")
        self.assertNotIn("synthetic-private-upstream", json.dumps(failed.to_dict()))
        self.assertEqual(manager.resume(), {"resumed": 0})
        self.assertEqual(manager.submit("123:ab12", idempotency_key="failed-task"), failed)
        self.assertEqual(self.client.downloads, ["123:ab12"])


class TaskLookupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.client = TaskClient()

    def test_find_is_a_side_effect_free_lookup_and_file_path_checks_identity(self):
        from zlib_connector.jobs import JobFileError
        self.client.release = threading.Event()
        manager = DownloadManager(self.client, self.folder)
        self.addCleanup(manager.close)
        self.assertIsNone(manager.find("lookup-key"))
        self.assertEqual(manager.list().total, 0)
        task = manager.submit("123:ab12", idempotency_key="lookup-key")
        self.assertEqual(manager.find("lookup-key").id, task.id)
        with self.assertRaises(ValueError):
            manager.find("bad key")
        with self.assertRaises(JobStateError):
            manager.file_path(task.id)
        self.client.release.set()
        deadline = time.monotonic() + 5
        while manager.get(task.id).state != "succeeded" and time.monotonic() < deadline:
            time.sleep(0.01)
        path = manager.file_path(task.id)
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.read_bytes(), CONTENT)
        self.assertEqual(path.parent, self.folder.resolve() / task.id)
        path.unlink()
        with self.assertRaises(JobFileError):
            manager.file_path(task.id)


if __name__ == "__main__":
    unittest.main()
