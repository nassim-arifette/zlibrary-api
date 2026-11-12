"""Bounded background work uses real tokens and never queues obsolete requests."""
import threading
import time
import unittest

from zlib_connector.errors import NetworkError
from zlib_connector.transfer import TransferCancelled
from zlib_connector.tui.loader import BoundedLoader


class LoaderTests(unittest.TestCase):
    def test_one_active_one_replaceable_pending_and_late_results_are_dropped(self):
        started, release, delivered = threading.Event(), threading.Event(), threading.Event()
        calls, results, tokens = [], [], []

        def receive(result):
            results.append(result)
            delivered.set()

        loader = BoundedLoader(receive)
        self.addCleanup(loader.close)

        def first(cancel):
            calls.append(1)
            tokens.append(cancel)
            started.set()
            release.wait(2)
            return "obsolete"

        loader.submit(1, first)
        self.assertTrue(started.wait(1))
        for number in range(2, 50):
            loader.submit(number, lambda cancel, number=number: calls.append(number) or "latest")
        self.assertTrue(tokens[0].is_cancelled)
        self.assertEqual([1], calls)
        release.set()
        self.assertTrue(delivered.wait(1))
        loader.close()
        self.assertFalse(loader.alive)
        self.assertEqual([1, 49], calls)
        self.assertEqual([(49, "latest")], [(result.generation, result.value) for result in results])

    def test_close_cancels_active_request_and_does_not_execute_pending(self):
        started, stopped = threading.Event(), threading.Event()
        calls, results = [], []
        loader = BoundedLoader(results.append)
        self.addCleanup(loader.close)

        def read(cancel):
            started.set()
            try:
                while not cancel.is_cancelled:
                    time.sleep(.005)
                cancel.check()
            finally:
                stopped.set()

        loader.submit(1, read)
        self.assertTrue(started.wait(1))
        loader.submit(2, lambda cancel: calls.append("pending"))
        loader.close()
        self.assertTrue(stopped.is_set())
        self.assertFalse(loader.alive)
        self.assertEqual([], calls)
        self.assertEqual([], results)
        with self.assertRaises(RuntimeError):
            loader.submit(3, read)

    def test_close_is_bounded_even_for_an_uncooperative_adapter(self):
        started, release = threading.Event(), threading.Event()
        results = []
        loader = BoundedLoader(results.append)

        def read(cancel):
            started.set()
            release.wait(2)
            return "late"

        loader.submit(1, read)
        self.assertTrue(started.wait(1))
        before = time.monotonic()
        try:
            loader.close(timeout=.02)
            self.assertLess(time.monotonic() - before, .3)
            self.assertTrue(loader.alive)
        finally:
            release.set()
            loader.close()
        self.assertEqual([], results)
        self.assertFalse(loader.alive)

    def test_failures_are_safe_and_explicit_refresh_can_succeed(self):
        done = threading.Event()
        results = []

        def receive(result):
            results.append(result)
            done.set()

        loader = BoundedLoader(receive)
        self.addCleanup(loader.close)

        def fail(cancel):
            raise NetworkError("secret response body")

        loader.submit(1, fail)
        self.assertTrue(done.wait(1))
        self.assertNotIn("secret", results[0].error)
        self.assertIn("zlib-cli serve", results[0].error)
        done.clear()

        def invalid(cancel):
            from zlib_connector.catalog_schema import invalid_catalog
            invalid_catalog()

        loader.submit(2, invalid)
        self.assertTrue(done.wait(1))
        self.assertIn("cannot read", results[1].error)
        self.assertNotIn("zlib-cli serve", results[1].error)
        results.pop()
        done.clear()
        loader.submit(3, lambda cancel: "fresh")
        self.assertTrue(done.wait(1))
        self.assertEqual("fresh", results[1].value)
        self.assertIsNone(results[1].error)

    def test_cancelled_request_does_not_become_an_error_message(self):
        stopped = threading.Event()
        results = []
        loader = BoundedLoader(results.append)
        self.addCleanup(loader.close)

        def read(cancel):
            stopped.set()
            raise TransferCancelled()

        loader.submit(1, read)
        self.assertTrue(stopped.wait(1))
        loader.close()
        self.assertEqual([], results)

    def test_account_and_rate_errors_explain_explicit_recovery_without_retry(self):
        for status, expected in ((400, "rejected this request. Change it"), (401, "token file, then retry"),
                                 (403, "token file, then retry"), (404, "Go back or retry"),
                                 (424, "Run zlib-cli login if needed, then restart zlib-cli serve without --anonymous"),
                                 (429, "Wait before retrying"),
                                 (502, "Z-Library did not complete"), (503, "Wait, then retry"),
                                 (504, "Z-Library did not complete"), (500, "The request failed. Retry, or go back.")):
            done = threading.Event()
            results, calls = [], []

            class APIError(Exception):
                pass

            def fail(cancel):
                calls.append(status)
                error = APIError("secret response body")
                error.status = status
                raise error

            def receive(result):
                results.append(result)
                done.set()

            loader = BoundedLoader(receive)
            try:
                loader.submit(1, fail)
                self.assertTrue(done.wait(1))
            finally:
                loader.close()
            with self.subTest(status=status):
                self.assertIn(expected, results[0].error)
                self.assertNotIn("secret", results[0].error)
                self.assertEqual([status], calls)
