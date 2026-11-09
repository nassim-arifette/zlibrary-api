"""Coalesced task subscriptions use only synthetic clients and temporary journals."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from zlib_connector.jobs import (DownloadManager, DownloadTask, JobBusyError, JobError,
                                 JobNotFoundError, TaskEvent, validate_event_id)
from zlib_connector.transfer import TransferProgress

from test_jobs import TaskClient


class ObservedClient(TaskClient):
    def __init__(self):
        super().__init__()
        self.progress_ready = threading.Event()
        self.emit = None

    def download(self, reference, destination, *, progress, cancel):
        self.emit = progress

        def observed(value):
            progress(value)
            self.progress_ready.set()

        return super().download(reference, destination, progress=observed, cancel=cancel)


class TaskEventTests(unittest.TestCase):
    def test_event_is_immutable_and_projects_only_public_task_fields(self):
        task = DownloadTask("a" * 32, "123:abcd", "queued", 1, 1)
        event = TaskEvent("b" * 32 + ":0", task, False)
        self.assertEqual(event.to_dict(), {"id": event.id, "task": task.to_dict(), "paused": False})
        self.assertNotIn("receipt", event.to_dict()["task"])
        with self.assertRaises(FrozenInstanceError):
            event.paused = True
        for changes in ({"id": "secret-invalid"}, {"task": {}}, {"paused": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(event, **changes)

    def test_cursor_validation_is_bounded_and_never_echoes_input(self):
        for suffix in ("0", "123", "0" * 20, "9" * 20):
            cursor = "a" * 32 + ":" + suffix
            self.assertEqual(validate_event_id(cursor), cursor)
        for cursor in (None, 1, True, "secret", "a" * 32, "A" * 32 + ":1",
                       "a" * 32 + ":-1", "a" * 32 + ":1\n", "a" * 32 + ":" + "1" * 21):
            with self.subTest(cursor=cursor), self.assertRaisesRegex(ValueError, "^The task event cursor is invalid.$"):
                validate_event_id(cursor)


class TaskSubscriptionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.client = ObservedClient()

    def manager(self):
        manager = DownloadManager(self.client, self.folder)
        self.addCleanup(manager.close)
        return manager

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("The synthetic task did not reach its expected state")

    def running(self):
        self.client.release = threading.Event()
        manager = self.manager()
        task = manager.submit("123:abcd", idempotency_key="synthetic-first")
        self.assertTrue(self.client.progress_ready.wait(3))
        return manager, task

    def test_idle_reads_and_matching_reconnect_keep_the_same_cursor(self):
        manager, task = self.running()
        with manager.watch(task.id) as first, manager.watch(task.id) as second:
            event = first.next(0)
            self.assertEqual(event, second.next(0))
            self.assertEqual(event.task, manager.get(task.id))
            self.assertIsNone(first.next(0))
            self.assertIsNone(first.next(0.01))
            with manager.watch(task.id, after=event.id) as matching:
                self.assertIsNone(matching.next(0))
            for after in ("f" * 32 + ":0", event.id.split(":")[0] + ":0"):
                with manager.watch(task.id, after=after) as reconnect:
                    self.assertEqual(reconnect.next(0), event)
            manager.submit("123:abcd", idempotency_key="synthetic-first")
            manager.submit("456:dcba", idempotency_key="synthetic-second")
            self.assertIsNone(first.next(0))

    def test_progress_coalesces_without_network_or_journal_writes(self):
        manager, task = self.running()
        with manager.watch(task.id) as subscription:
            initial = subscription.next(0)
            with (patch.object(manager._store, "update", side_effect=AssertionError("unexpected write")),
                  patch.object(manager._store, "create", side_effect=AssertionError("unexpected write")),
                  patch.object(manager._store, "set_paused", side_effect=AssertionError("unexpected write")),
                  patch.object(self.client, "get_quota", side_effect=AssertionError("unexpected request")),
                  patch.object(self.client, "account_fingerprint", side_effect=AssertionError("unexpected account access"))):
                for received in range(6, 100):
                    self.client.emit(TransferProgress(received, 100, 1))
                latest = subscription.next(0)
                self.assertNotEqual(initial.id, latest.id)
                self.assertEqual(latest.task.progress, TransferProgress(99, 100, 1))
                self.assertIsNone(manager._store.get(task.id).progress)
                self.assertIsNone(subscription.next(0))
                self.client.emit(TransferProgress(99, 100, 1))
                self.assertIsNone(subscription.next(0))
                with manager.watch(task.id) as another:
                    self.assertEqual(another.next(0), latest)

    def test_pause_and_resume_are_observable_on_an_unchanged_queued_task(self):
        self.client.remaining = 0
        quota_entered, quota_release = threading.Event(), threading.Event()
        original_quota = self.client.get_quota

        def quota():
            quota_entered.set()
            if not quota_release.wait(3):
                raise AssertionError("The synthetic quota gate was not released")
            return original_quota()

        self.client.get_quota = quota
        manager = self.manager()
        self.addCleanup(quota_release.set)
        first = manager.submit("123:abcd", idempotency_key="synthetic-first")
        self.assertTrue(quota_entered.wait(3))
        queued = manager.submit("456:dcba", idempotency_key="synthetic-second")
        with manager.watch(queued.id) as subscription:
            initial = subscription.next(0)
            self.assertFalse(initial.paused)
            quota_release.set()
            self.wait(lambda: manager.get(first.id).state == "waiting_quota")
            paused = subscription.next(0)
            self.assertTrue(paused.paused)
            self.assertEqual(paused.task, initial.task)
            self.assertNotEqual(paused.id, initial.id)
            self.client.remaining = 5
            self.client.release = threading.Event()
            manager.resume()
            resumed = subscription.next(0)
            self.assertFalse(resumed.paused)
            self.assertEqual(resumed.task, initial.task)
            self.assertNotEqual(resumed.id, paused.id)

    def test_terminal_state_is_delivered_once_even_with_a_matching_cursor(self):
        manager, _ = self.running()
        queued = manager.submit("456:dcba", idempotency_key="synthetic-second")
        manager.cancel(queued.id)
        with manager.watch(queued.id) as subscription:
            terminal = subscription.next(0)
            self.assertEqual(terminal.task.state, "cancelled")
            self.assertTrue(subscription.closed)
            self.assertIsNone(subscription.next(0))
        with manager.watch(queued.id, after=terminal.id) as reconnect:
            self.assertEqual(reconnect.next(0), terminal)
            self.assertTrue(reconnect.closed)

    def test_success_keeps_persisted_progress_in_the_terminal_observation(self):
        manager, task = self.running()
        with manager.watch(task.id) as subscription:
            subscription.next(0)
            self.client.release.set()
            self.wait(lambda: manager.get(task.id).state == "succeeded")
            terminal = subscription.next(0)
            self.assertEqual(terminal.task, manager.get(task.id))
            self.assertEqual(terminal.task.progress, manager._store.get(task.id).progress)
            self.assertEqual(terminal.task.state, "succeeded")
            self.assertTrue(subscription.closed)

    def test_failed_durable_cancel_does_not_signal_or_emit_an_unrecorded_state(self):
        manager, task = self.running()
        with manager.watch(task.id) as subscription:
            subscription.next(0)
            with (patch.object(manager._store, "update", side_effect=JobError("synthetic storage failure")),
                  patch.object(self.client.last_token, "cancel") as cancel):
                with self.assertRaises(JobError):
                    manager.cancel(task.id)
                cancel.assert_not_called()
                self.assertIsNone(subscription.next(0))
                self.assertFalse(manager.get(task.id).cancel_requested)

    def test_cancellation_observation_follows_the_durable_request(self):
        manager, task = self.running()
        with manager.watch(task.id) as subscription:
            subscription.next(0)
            original_cancel = self.client.last_token.cancel

            def cancel():
                self.assertTrue(manager._store.get(task.id).cancel_requested)
                original_cancel()

            with patch.object(self.client.last_token, "cancel", side_effect=cancel):
                manager.cancel(task.id)
            event = subscription.next(0)
            self.assertTrue(event.task.cancel_requested)
            self.assertIn(event.task.state, ("running", "cancelled"))

    def test_capacity_is_released_by_close_and_terminal_consumption(self):
        manager, task = self.running()
        subscriptions = [manager.watch(task.id) for _ in range(32)]
        for subscription in subscriptions:
            self.addCleanup(subscription.close)
        with self.assertRaises(JobBusyError):
            manager.watch(task.id)
        subscriptions[0].close()
        subscriptions[0].close()
        queued = manager.submit("456:dcba", idempotency_key="synthetic-second")
        manager.cancel(queued.id)
        terminal = manager.watch(queued.id)
        self.assertEqual(terminal.next(0).task.state, "cancelled")
        self.assertTrue(terminal.closed)
        with manager.watch(task.id):
            with self.assertRaises(JobBusyError):
                manager.watch(task.id)

    def test_explicit_close_wakes_a_waiter_and_is_idempotent(self):
        manager, task = self.running()
        subscription = manager.watch(task.id)
        subscription.next(0)
        with ThreadPoolExecutor(max_workers=1) as executor:
            waiting = executor.submit(subscription.next, 60)
            subscription.close()
            self.assertIsNone(waiting.result(timeout=1))
        self.assertTrue(subscription.closed)
        subscription.close()

    def test_shutdown_wakes_subscribers_before_a_blocked_worker_finishes(self):
        self.client.publication_gate = threading.Event()
        manager = self.manager()
        task = manager.submit("123:abcd", idempotency_key="synthetic-first")
        self.assertTrue(self.client.published.wait(3))
        subscription = manager.watch(task.id)
        subscription.next(0)
        with ThreadPoolExecutor(max_workers=2) as executor:
            waiting = executor.submit(subscription.next, 60)
            closing = executor.submit(manager.close)
            try:
                self.assertIsNone(waiting.result(timeout=1))
                self.assertTrue(subscription.closed)
                self.assertFalse(closing.done())
                with self.assertRaises(JobBusyError):
                    manager.watch(task.id)
            finally:
                self.client.publication_gate.set()
            closing.result(timeout=3)

    def test_restart_uses_a_new_epoch_and_resynchronizes_the_current_state(self):
        self.client.remaining = 0
        manager = self.manager()
        task = manager.submit("123:abcd", idempotency_key="synthetic-first")
        self.wait(lambda: manager.get(task.id).state == "waiting_quota")
        with manager.watch(task.id) as subscription:
            previous = subscription.next(0)
        manager.close()
        restarted = self.manager()
        with restarted.watch(task.id, after=previous.id) as subscription:
            current = subscription.next(0)
            self.assertNotEqual(current.id.split(":")[0], previous.id.split(":")[0])
            self.assertEqual(current.task, previous.task)
            self.assertTrue(current.paused)

    def test_invalid_inputs_and_storage_errors_do_not_leak_subscriptions(self):
        manager, task = self.running()
        with self.assertRaises(ValueError):
            manager.watch("../private")
        with self.assertRaises(ValueError):
            manager.watch(task.id, after="secret-invalid")
        with self.assertRaises(JobNotFoundError):
            manager.watch("e" * 32)
        with manager.watch(task.id) as subscription:
            for timeout in (None, True, -1, 61, float("inf"), float("nan"), "5"):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    subscription.next(timeout)
            subscription.next(0)
            with patch.object(manager._store, "get", side_effect=JobError("safe storage error")):
                with self.assertRaises(JobError):
                    subscription.next(0)
            self.assertTrue(subscription.closed)
        with manager.watch(task.id) as valid:
            self.assertIsNotNone(valid.next(0))


if __name__ == "__main__":
    unittest.main()
