"""Artifact access is confined to task-owned files and pinned identities."""

import os
from pathlib import Path
import tempfile
import unittest

from zlib_connector.jobs.files import TaskFiles
from zlib_connector.jobs.models import JobFileError


class TaskFileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.root = self.folder / "downloads"
        self.root.mkdir()
        self.files = TaskFiles(self.root)
        self.task_id = "a" * 32
        self.owned = self.files.prepare(self.task_id)
        self.path = self.owned.path / "Example.pdf"
        self.path.write_bytes(b"%PDF-1.7\nSynthetic task artifact")

    def test_capture_and_stream_return_only_safe_file_metadata(self):
        receipt = self.files.capture(self.owned, self.path)
        self.assertEqual(receipt.name, "Example.pdf")
        self.assertEqual(receipt.size, self.path.stat().st_size)
        with self.files.open(self.task_id, receipt) as file:
            self.assertEqual((file.name, file.content_type, file.size), ("Example.pdf", "application/pdf", receipt.size))
            self.assertEqual(file.stream.read(5), b"%PDF-")
            self.assertTrue(file.stream.read().startswith(b"1.7"))
        with self.assertRaises(JobFileError):
            file.stream.read(1)

    def test_outside_results_and_existing_task_directories_are_rejected(self):
        outside = self.folder / "outside.pdf"
        outside.write_bytes(b"%PDF-1.7")
        for path in (outside, Path("Example.pdf"), self.owned.path):
            with self.subTest(path=path), self.assertRaises(JobFileError):
                self.files.capture(self.owned, path)
        with self.assertRaises(JobFileError):
            self.files.prepare(self.task_id)
        for identifier in ("../outside", "A" * 32, "b" * 31):
            with self.subTest(identifier=identifier), self.assertRaises(JobFileError):
                self.files.prepare(identifier)

    def test_unexpected_hard_links_are_rejected_at_capture_and_retrieval(self):
        receipt = self.files.capture(self.owned, self.path)
        duplicate = self.folder / "hardlink.pdf"
        os.link(self.path, duplicate)
        with self.assertRaises(JobFileError):
            self.files.capture(self.owned, self.path)
        with self.assertRaises(JobFileError):
            with self.files.open(self.task_id, receipt):
                self.fail("A multiply linked file was served")

    def test_same_size_replacement_and_missing_file_are_unavailable(self):
        receipt = self.files.capture(self.owned, self.path)
        replacement = self.owned.path / "replacement.pdf"
        replacement.write_bytes(self.path.read_bytes())
        os.replace(replacement, self.path)
        with self.assertRaises(JobFileError):
            with self.files.open(self.task_id, receipt):
                self.fail("A replacement was served")
        self.path.unlink()
        with self.assertRaises(JobFileError):
            with self.files.open(self.task_id, receipt):
                self.fail("A missing file was served")

    def test_mutation_after_open_is_detected_before_next_chunk(self):
        receipt = self.files.capture(self.owned, self.path)
        with self.files.open(self.task_id, receipt) as file:
            self.assertEqual(file.stream.read(5), b"%PDF-")
            self.path.write_bytes(b"x" * receipt.size)
            os.utime(self.path, ns=(self.path.stat().st_atime_ns, receipt.mtime_ns + 1_000_000_000))
            with self.assertRaises(JobFileError):
                file.stream.read(5)

    def test_task_directory_replacement_is_rejected_even_if_the_original_file_is_moved_back(self):
        receipt = self.files.capture(self.owned, self.path)
        previous = self.root / "previous"
        self.owned.path.rename(previous)
        self.owned.path.mkdir()
        (previous / self.path.name).rename(self.path)
        with self.assertRaises(JobFileError):
            with self.files.open(self.task_id, receipt):
                self.fail("A replacement directory was served")

    def test_root_replacement_is_rejected(self):
        receipt = self.files.capture(self.owned, self.path)
        self.root.rename(self.folder / "previous-root")
        self.root.mkdir()
        with self.assertRaises(JobFileError):
            with self.files.open(self.task_id, receipt):
                self.fail("A replacement root was accepted")

    def test_symbolic_file_and_root_links_are_rejected(self):
        receipt = self.files.capture(self.owned, self.path)
        original = self.owned.path / "original.pdf"
        self.path.rename(original)
        try:
            self.path.symlink_to(original)
        except OSError as error:
            self.skipTest("Creating a symbolic link is unavailable: " + type(error).__name__)
        with self.assertRaises(JobFileError):
            with self.files.open(self.task_id, receipt):
                self.fail("A symbolic file was served")
        link = self.folder / "linked-root"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(JobFileError):
            TaskFiles(link)


if __name__ == "__main__":
    unittest.main()
