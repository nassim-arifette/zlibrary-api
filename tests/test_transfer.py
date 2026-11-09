from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import json
from threading import Event
import unittest

from zlib_connector.errors import ConnectorError
from zlib_connector.transfer import CancellationToken, TransferCancelled, TransferProgress


class TransferProgressTests(unittest.TestCase):
    def test_known_size_reports_average_speed_remaining_time_and_fraction(self):
        progress = TransferProgress(250, 1000, 2)
        self.assertEqual(progress.bytes_per_second, 125.0)
        self.assertEqual(progress.eta_seconds, 6.0)
        self.assertEqual(progress.fraction, 0.25)
        self.assertEqual(progress.to_dict(), {
            "bytes_received": 250, "total_bytes": 1000, "elapsed_seconds": 2.0,
            "bytes_per_second": 125.0, "eta_seconds": 6.0, "fraction": 0.25})
        self.assertEqual(json.loads(json.dumps(progress.to_dict(), allow_nan=False)), progress.to_dict())

    def test_unknown_size_keeps_fraction_and_eta_unknown_but_can_report_speed(self):
        progress = TransferProgress(300, None, 2.5)
        self.assertEqual(progress.bytes_per_second, 120.0)
        self.assertIsNone(progress.eta_seconds)
        self.assertIsNone(progress.fraction)
        self.assertIsNone(progress.to_dict()["total_bytes"])

    def test_zero_samples_never_divide_by_zero_or_invent_speed(self):
        for progress in (TransferProgress(0, None, 0), TransferProgress(0, 0, 5),
                         TransferProgress(0, 100, 2), TransferProgress(10, 100, 0)):
            with self.subTest(progress=progress):
                self.assertIsNone(progress.bytes_per_second)
                self.assertIsNone(progress.eta_seconds)
                json.dumps(progress.to_dict(), allow_nan=False)
        self.assertIsNone(TransferProgress(0, 0, 0).fraction)
        self.assertEqual(TransferProgress(0, 100, 0).fraction, 0.0)
        self.assertEqual(TransferProgress(10, 100, 0).fraction, 0.1)

    def test_received_total_does_not_claim_validation_or_publication_and_snapshot_is_immutable(self):
        progress = TransferProgress(100, 100, 2)
        self.assertEqual(progress.fraction, 1.0)
        self.assertEqual(progress.eta_seconds, 0.0)
        self.assertNotIn("completed", progress.to_dict())
        self.assertNotIn("path", progress.to_dict())
        with self.assertRaises(FrozenInstanceError):
            progress.bytes_received = 200

    def test_invalid_counts_and_elapsed_times_fail_at_construction(self):
        for received in (True, False, None, "1", 1.0, -1):
            with self.subTest(received=received), self.assertRaises(ValueError):
                TransferProgress(received, None, 0)
        for total in (True, False, "1", 1.0, -1):
            with self.subTest(total=total), self.assertRaises(ValueError):
                TransferProgress(0, total, 0)
        with self.assertRaises(ValueError):
            TransferProgress(2, 1, 1)
        for elapsed in (True, False, None, "1", 1j, -1, float("nan"), float("inf"), -float("inf"), 10**400):
            with self.subTest(elapsed=elapsed), self.assertRaises(ValueError):
                TransferProgress(0, None, elapsed)

    def test_unrepresentable_estimates_remain_json_safe_without_rounding_byte_counts(self):
        huge = 10**400
        progress = TransferProgress(huge, huge * 2, 1)
        self.assertEqual(progress.bytes_received, huge)
        self.assertEqual(progress.fraction, 0.5)
        self.assertIsNone(progress.bytes_per_second)
        self.assertIsNone(progress.eta_seconds)
        json.dumps(progress.to_dict(), allow_nan=False)
        self.assertIsNone(TransferProgress(1, 2, 5e-324).bytes_per_second)
        self.assertIsNone(TransferProgress(1, 10**300, 1e300).eta_seconds)


class CancellationTests(unittest.TestCase):
    def test_cancel_is_idempotent_and_check_raises_the_fixed_public_error(self):
        token = CancellationToken()
        self.assertFalse(token.is_cancelled)
        self.assertIsNone(token.check())
        token.cancel()
        token.cancel()
        self.assertTrue(token.is_cancelled)
        for _ in range(2):
            with self.assertRaises(TransferCancelled) as caught:
                token.check()
            self.assertIsInstance(caught.exception, ConnectorError)
            self.assertEqual(str(caught.exception), "Transfer cancelled.")

    def test_cancel_from_another_thread_is_observed_at_the_next_check(self):
        token = CancellationToken()
        ready, proceed = Event(), Event()

        def worker():
            token.check()
            ready.set()
            if not proceed.wait(2):
                raise AssertionError("The test did not release the worker.")
            token.check()

        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(worker)
            try:
                self.assertTrue(ready.wait(2))
                token.cancel()
            finally:
                proceed.set()
            with self.assertRaises(TransferCancelled):
                result.result(timeout=2)


if __name__ == "__main__":
    unittest.main()
