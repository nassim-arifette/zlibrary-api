import io
import unittest
from unittest.mock import patch

from zlib_connector.cli_progress import ProgressDisplay
from zlib_connector.transfer import TransferCancelled, TransferProgress


class ProgressDisplayTests(unittest.TestCase):
    def test_already_closed_stream_disables_progress_before_download(self):
        for enabled in (None, True, False):
            stream = io.StringIO()
            stream.close()
            with self.subTest(enabled=enabled), ProgressDisplay(enabled, stream=stream) as callback:
                self.assertIsNone(callback)

    def test_default_is_quiet_without_a_terminal(self):
        stream = io.StringIO()
        with ProgressDisplay(stream=stream) as callback:
            self.assertIsNone(callback)
        self.assertEqual(stream.getvalue(), "")

    def test_unknown_total_does_not_invent_percent_or_eta(self):
        stream = io.StringIO()
        with ProgressDisplay(True, stream=stream) as callback:
            callback(TransferProgress(1024, None, 2))
        self.assertIn("Received 1.0 KiB", stream.getvalue())
        self.assertIn("512 B/s", stream.getvalue())
        self.assertNotIn("%", stream.getvalue())
        self.assertNotIn("left", stream.getvalue())

    def test_pipe_progress_is_throttled_but_reports_final_bytes(self):
        stream = io.StringIO()
        with patch("zlib_connector.cli_progress.time.monotonic", side_effect=[10, 10.1, 10.2]), \
                ProgressDisplay(True, stream=stream) as callback:
            callback(TransferProgress(1, 10, 1))
            callback(TransferProgress(2, 10, 2))
            callback(TransferProgress(10, 10, 3))
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("100%", lines[-1])
        self.assertNotIn("completed", stream.getvalue().lower())
        self.assertNotIn("saved", stream.getvalue().lower())

    def test_short_unknown_length_transfer_flushes_only_the_last_throttled_sample_on_exit(self):
        stream = io.StringIO()
        with patch("zlib_connector.cli_progress.time.monotonic", side_effect=[10, 10.01, 10.02]), \
                ProgressDisplay(True, stream=stream) as callback:
            callback(TransferProgress(0, None, 0))
            callback(TransferProgress(1024, None, 0.01))
            callback(TransferProgress(65536, None, 0.02))
            self.assertEqual(stream.getvalue(), "Received 0 B\n")
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("Received 64.0 KiB", lines[-1])
        self.assertNotIn("Received 1.0 KiB", stream.getvalue())
        self.assertNotIn("%", stream.getvalue())
        self.assertNotIn("left", stream.getvalue())

    def test_last_sample_is_flushed_on_failure_without_claiming_completion_or_replacing_error(self):
        for failure in (TransferCancelled(), RuntimeError("Synthetic transfer error"), KeyboardInterrupt()):
            stream = io.StringIO()
            with self.subTest(error=type(failure).__name__), self.assertRaises(type(failure)) as caught:
                with patch("zlib_connector.cli_progress.time.monotonic", side_effect=[10, 10.01]), \
                        ProgressDisplay(True, stream=stream) as callback:
                    callback(TransferProgress(0, None, 0))
                    callback(TransferProgress(65536, None, 0.01))
                    raise failure
            self.assertIs(caught.exception, failure)
            self.assertIn("Received 64.0 KiB", stream.getvalue().splitlines()[-1])
            self.assertNotIn("completed", stream.getvalue().lower())
            self.assertNotIn("saved", stream.getvalue().lower())

    def test_closing_does_not_repeat_an_already_rendered_complete_sample(self):
        stream = io.StringIO()
        with patch("zlib_connector.cli_progress.time.monotonic", return_value=10), \
                ProgressDisplay(True, stream=stream) as callback:
            callback(TransferProgress(10, 10, 1))
            rendered = stream.getvalue()
        self.assertEqual(stream.getvalue(), rendered)
        self.assertEqual(len(rendered.splitlines()), 1)

    def test_continuing_transfer_keeps_the_normal_render_interval_before_final_flush(self):
        stream = io.StringIO()
        with patch("zlib_connector.cli_progress.time.monotonic", side_effect=[10, 10.1, 11, 11.1]), \
                ProgressDisplay(True, stream=stream) as callback:
            for received in (0, 100, 200, 300):
                callback(TransferProgress(received, None, received / 100))
            before_exit = stream.getvalue().splitlines()
            self.assertEqual(len(before_exit), 2)
            self.assertIn("Received 200 B", before_exit[-1])
        self.assertEqual(len(stream.getvalue().splitlines()), 3)
        self.assertIn("Received 300 B", stream.getvalue().splitlines()[-1])

    def test_closed_rendering_stream_does_not_abort_a_download(self):
        stream = io.StringIO()
        with ProgressDisplay(True, stream=stream) as callback:
            stream.close()
            callback(TransferProgress(1, None, 1))
            callback(TransferProgress(2, None, 2))

    def test_stream_closed_after_a_throttled_sample_does_not_fail_context_exit(self):
        for terminal in (False, True):
            stream = io.StringIO()
            stream.isatty = lambda: terminal
            with self.subTest(terminal=terminal), patch("zlib_connector.cli_progress.time.monotonic", side_effect=[10, 10.01]), \
                    ProgressDisplay(True, stream=stream) as callback:
                callback(TransferProgress(0, None, 0))
                callback(TransferProgress(65536, None, 0.01))
                stream.close()

    def test_unexpected_exit_rendering_failure_does_not_replace_transfer_outcome(self):
        stream = io.StringIO()
        with patch("zlib_connector.cli_progress.time.monotonic", side_effect=[10, 10.01]), \
                patch.object(stream, "write", side_effect=[None, RuntimeError("Synthetic renderer error")]), \
                ProgressDisplay(True, stream=stream) as callback:
            callback(TransferProgress(0, None, 0))
            callback(TransferProgress(65536, None, 0.01))

    def test_terminal_output_ends_its_line_even_on_an_interrupted_transfer(self):
        stream = io.StringIO()
        stream.isatty = lambda: True
        with self.assertRaises(KeyboardInterrupt):
            with ProgressDisplay(stream=stream) as callback:
                callback(TransferProgress(1, None, 1))
                raise KeyboardInterrupt
        self.assertTrue(stream.getvalue().startswith("\r"))
        self.assertTrue(stream.getvalue().endswith("\n"))


if __name__ == "__main__":
    unittest.main()
