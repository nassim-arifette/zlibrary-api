from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from zlib_connector.jobs.models import (DownloadTask, FileReceipt, JobBusyError, JobCapacityError,
                                       JobConflictError, JobError, JobNotFoundError, JobStateError, TaskPage)
from zlib_connector.jobs.store import TaskStore, load_scope_key
from zlib_connector.private_files import create_private_file, protect_private_file, require_private_file
from zlib_connector.transfer import TransferProgress


SCOPE = "a" * 64
KEY = "synthetic-idempotency-key"
REFERENCE = "123:abcd"


def receipt():
    return FileReceipt("private-book.pdf", 20, 1, 2, 3, 4, 5, 6)


class TaskModelTests(unittest.TestCase):
    def test_empty_task_page_still_exposes_queue_pause_without_coercing_values(self):
        self.assertFalse(TaskPage((), 0, 50, 0).to_dict()["paused"])
        self.assertEqual(TaskPage((), 0, 50, 0, paused=True).to_dict(),
                         {"tasks": [], "total": 0, "limit": 50, "offset": 0, "paused": True})
        for paused in (0, 1, "true", None):
            with self.subTest(paused=paused), self.assertRaises(ValueError):
                TaskPage((), 0, 50, 0, paused=paused)

    def test_public_projection_contains_no_receipt_or_path_and_only_fixed_error_message(self):
        task = DownloadTask("b" * 32, REFERENCE, "succeeded", 10, 11,
                            progress=TransferProgress(20, 20, 2), receipt=receipt())
        result = task.to_dict()
        self.assertEqual(result["outcome"], "completed")
        self.assertTrue(result["file_available"])
        serialized = json.dumps(TaskPage((task,), 1, 50, 0).to_dict())
        self.assertNotIn("private-book", serialized)
        self.assertNotIn("receipt", serialized)
        self.assertNotIn("dir_device", serialized)
        self.assertNotIn("private-book", repr(task))
        interrupted = replace(task, state="interrupted", receipt=None, error_code="Interrupted")
        self.assertEqual(interrupted.to_dict()["outcome"], "unknown")
        self.assertIn("will not be retried", interrupted.to_dict()["error_message"])

    def test_invalid_identifiers_receipts_progress_and_error_codes_are_rejected(self):
        base = DownloadTask("b" * 32, REFERENCE, "queued", 10, 10)
        for changes in ({"id": "../private"}, {"reference": "https://example.org/book/1/a"},
                        {"state": "private"}, {"updated_at": 9}, {"created_at": float("nan")},
                        {"error_code": "raw-private-exception"}, {"cancel_requested": 1},
                        {"state": "succeeded"}, {"receipt": receipt()},
                        {"progress": TransferProgress(2**64, None, 1)}):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                replace(base, **changes)
        for name in ("../book.pdf", "folder/book.pdf", "folder\\book.pdf", "book.pdf:stream", "..", "bad\nname"):
            with self.assertRaises(ValueError):
                replace(receipt(), name=name)
        for limit, offset in ((True, 0), (1001, 0), (10, -1)):
            with self.assertRaises(ValueError):
                TaskPage((), 0, limit, offset)


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def store(self, *, scope=SCOPE, max_tasks=1000):
        store = TaskStore(self.root, scope, max_tasks=max_tasks)
        self.addCleanup(store.close)
        return store

    def rewrite(self, path, data):
        path.unlink(missing_ok=True)
        with create_private_file(path) as target:
            target.write(data)

    def test_create_is_private_durable_and_idempotency_keys_are_only_hashed(self):
        store = self.store()
        first = store.create(REFERENCE, KEY)
        self.assertEqual(store.create(REFERENCE, KEY), first)
        self.assertEqual(store.all(), (first,))
        record = json.loads(store.path.read_bytes())
        self.assertEqual(record["tasks"][0]["key_hash"], hashlib.sha256(KEY.encode()).hexdigest())
        self.assertNotIn(KEY.encode(), store.path.read_bytes())
        require_private_file(store.path)
        require_private_file(store.lock_path)
        store.close()
        reopened = self.store()
        self.assertEqual(reopened.create(REFERENCE, KEY), first)
        with self.assertRaises(JobConflictError):
            reopened.create("456:abcd", KEY)

    def test_capacity_keeps_existing_keys_and_a_lower_reopen_limit_does_not_evict(self):
        store = self.store(max_tasks=2)
        first = store.create(REFERENCE, KEY)
        second = store.create("456:abcd", KEY + "-second")
        with self.assertRaises(JobCapacityError):
            store.create("789:abcd", KEY + "-third")
        self.assertEqual(store.create(REFERENCE, KEY), first)
        store.close()
        reopened = self.store(max_tasks=1)
        self.assertEqual(reopened.all(), (first, second))
        self.assertEqual(reopened.create("456:abcd", KEY + "-second"), second)
        with self.assertRaises(JobCapacityError):
            reopened.create("789:abcd", KEY + "-third")

    def test_transitions_are_atomic_immutable_and_cancellation_is_monotone(self):
        store = self.store()
        queued = store.create(REFERENCE, KEY)
        with self.assertRaises(JobStateError):
            store.update(replace(queued, state="succeeded", receipt=receipt()))
        running = store.update(replace(queued, state="running", updated_at=queued.updated_at + 1))
        cancelled_request = store.update(replace(running, cancel_requested=True, updated_at=running.updated_at + 1))
        succeeded = store.update(replace(running, state="succeeded", receipt=receipt()))
        self.assertTrue(succeeded.cancel_requested)
        self.assertGreaterEqual(succeeded.updated_at, cancelled_request.updated_at)
        self.assertEqual(store.update(succeeded), succeeded)
        for changes in ({"state": "running", "receipt": None}, {"reference": "456:abcd"},
                        {"created_at": succeeded.created_at - 1}):
            with self.subTest(changes=list(changes)), self.assertRaises(JobStateError):
                store.update(replace(succeeded, **changes))
        store.close()
        self.assertEqual(self.store().get(queued.id), succeeded)

    def test_waiting_tasks_require_explicit_state_change_and_queued_cancellation_blocks_start(self):
        store = self.store()
        first = store.create(REFERENCE, KEY)
        running = store.update(replace(first, state="running"))
        waiting = store.update(replace(running, state="waiting_quota", error_code="RateLimitError"))
        store.close()
        store = self.store()
        self.assertEqual(store.get(first.id), waiting)
        queued = store.update(replace(waiting, state="queued", error_code=None))
        cancelled = store.update(replace(queued, state="cancelled", cancel_requested=True, error_code="Cancelled"))
        with self.assertRaises(JobStateError):
            store.update(replace(queued, state="running"))
        self.assertEqual(store.get(first.id), cancelled)

    def test_pause_is_durable_without_pending_tasks_and_only_clears_explicitly(self):
        store = self.store()
        self.assertFalse(store.paused)
        self.assertTrue(store.set_paused(True))
        before = store.path.read_bytes()
        self.assertTrue(store.set_paused(True))
        self.assertEqual(store.path.read_bytes(), before)
        store.close()
        reopened = self.store()
        self.assertTrue(reopened.paused)
        task = reopened.create(REFERENCE, KEY)
        self.assertTrue(reopened.paused)
        self.assertEqual(task.state, "queued")
        self.assertFalse(reopened.set_paused(False))
        with self.assertRaises(ValueError):
            reopened.set_paused(1)
        reopened.close()
        self.assertFalse(self.store().paused)

    def test_restart_recovers_running_as_interrupted_without_replaying_or_deleting_files(self):
        store = self.store()
        task = store.create(REFERENCE, KEY)
        store.update(replace(task, state="running", progress=TransferProgress(10, None, 2)))
        unrelated = self.root / "keep.pdf"
        unrelated.write_bytes(b"synthetic")
        store.close()
        reopened = self.store()
        interrupted = reopened.get(task.id)
        self.assertEqual(interrupted.state, "interrupted")
        self.assertEqual(interrupted.error_code, "Interrupted")
        self.assertEqual(interrupted.progress.bytes_received, 10)
        self.assertEqual(reopened.create(REFERENCE, KEY), interrupted)
        self.assertEqual(json.loads(reopened.path.read_bytes())["tasks"][0]["task"]["state"], "interrupted")
        self.assertEqual(unrelated.read_bytes(), b"synthetic")

    def test_abrupt_process_exit_releases_lease_and_running_is_not_requeued(self):
        script = (
            "import os,sys\nfrom dataclasses import replace\nfrom zlib_connector.jobs.store import TaskStore\n"
            "store=TaskStore(sys.argv[1],'a'*64)\n"
            "task=store.create('123:abcd','synthetic-idempotency-key')\n"
            "store.update(replace(task,state='running'))\nos._exit(0)\n"
        )
        child = subprocess.run([sys.executable, "-c", script, str(self.root)], capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 0)
        self.assertEqual(child.stdout, b"")
        self.assertEqual(child.stderr, b"")
        store = self.store()
        self.assertEqual(len(store.all()), 1)
        self.assertEqual(store.all()[0].state, "interrupted")
        self.assertEqual(store.create(REFERENCE, KEY).state, "interrupted")

    def test_competing_owners_fail_and_independent_scopes_remain_isolated(self):
        first = self.store()
        task = first.create(REFERENCE, KEY)
        with self.assertRaises(JobBusyError):
            TaskStore(self.root, SCOPE)
        second = self.store(scope="b" * 64)
        other = second.create(REFERENCE, KEY)
        self.assertNotEqual(task.id, other.id)
        with self.assertRaises(JobNotFoundError):
            second.get(task.id)
        identity = first.lock_path.stat().st_ino
        first.close()
        first.close()
        reopened = self.store()
        self.assertEqual(reopened.lock_path.stat().st_ino, identity)
        self.assertEqual(reopened.get(task.id), task)

    def test_native_lease_excludes_another_process(self):
        self.store()
        script = (
            "import sys\nfrom zlib_connector.jobs.store import TaskStore\n"
            "from zlib_connector.jobs.models import JobBusyError\n"
            "try:\n TaskStore(sys.argv[1],'a'*64)\nexcept JobBusyError:\n sys.exit(7)\n"
        )
        child = subprocess.run([sys.executable, "-c", script, str(self.root)], capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 7)
        self.assertEqual(child.stdout, b"")
        self.assertEqual(child.stderr, b"")

    def test_concurrent_duplicate_submissions_create_exactly_one_persistent_record(self):
        store = self.store()
        start = Barrier(8)
        def submit(_):
            start.wait(timeout=5)
            return store.create(REFERENCE, KEY)
        with ThreadPoolExecutor(max_workers=8) as executor:
            tasks = list(executor.map(submit, range(8)))
        self.assertEqual(len({task.id for task in tasks}), 1)
        self.assertEqual(len(json.loads(store.path.read_bytes())["tasks"]), 1)

    def test_malformed_strict_schema_rejects_without_repair_and_releases_lease(self):
        store = self.store()
        task = store.create(REFERENCE, KEY)
        path = store.path
        original_bytes = path.read_bytes()
        original = json.loads(original_bytes)
        store.close()
        cases = [b"private-invalid-json", b'{"version":1,"version":1}', b'{"private":NaN}', b"[]"]
        for changes in ({"version": 2}, {"scope": "b" * 64}, {"extra": "private-value"}, {"paused": 1},
                        {"tasks": original["tasks"] * 2}):
            cases.append(json.dumps({**original, **changes}).encode())
        changed = json.loads(original_bytes)
        changed["tasks"][0]["task"]["error_code"] = "private-raw-exception"
        cases.append(json.dumps(changed).encode())
        for data in cases:
            with self.subTest(length=len(data)):
                self.rewrite(path, data)
                with self.assertRaises(JobStateError) as caught:
                    TaskStore(self.root, SCOPE)
                self.assertNotIn("private-raw", str(caught.exception))
                self.assertNotIn(str(path), str(caught.exception))
                self.assertEqual(path.read_bytes(), data)
        self.rewrite(path, original_bytes)
        self.assertEqual(self.store().get(task.id), task)

    def test_journal_byte_limit_is_checked_before_json_parsing(self):
        store = self.store()
        store.create(REFERENCE, KEY)
        store.close()
        with patch("zlib_connector.jobs.store.MAX_JOURNAL_BYTES", 32):
            with patch("zlib_connector.jobs.store.json.loads", side_effect=AssertionError("Oversized JSON was parsed")):
                with self.assertRaises(JobError):
                    TaskStore(self.root, SCOPE)

    def test_storage_failure_preserves_old_journal_cleans_own_temp_and_stops_owner(self):
        store = self.store()
        first = store.create(REFERENCE, KEY)
        original = store.path.read_bytes()
        unrelated = store.path.parent / ".tasks-unrelated"
        unrelated.write_bytes(b"keep")
        with patch("zlib_connector.jobs.store.os.replace", side_effect=OSError("private-file-path")):
            with self.assertRaises(JobError) as caught:
                store.create("456:abcd", KEY + "-second")
        self.assertNotIn("private-file-path", str(caught.exception))
        self.assertEqual(store.path.read_bytes(), original)
        self.assertEqual(list(store.path.parent.glob(".tasks-*")), [unrelated])
        with self.assertRaises(JobError) as caught:
            store.create("789:abcd", KEY + "-third")
        self.assertIs(type(caught.exception), JobError)
        store.close()
        reopened = self.store()
        self.assertEqual(reopened.all(), (first,))
        self.assertEqual(unrelated.read_bytes(), b"keep")

    def test_removed_scope_key_never_creates_a_replacement_namespace(self):
        store = self.store()
        store.create(REFERENCE, KEY)
        store.close()
        key_path = self.root / ".zlib-jobs" / "scope-key"
        key_path.unlink()
        with self.assertRaises(JobStateError):
            load_scope_key(self.root)
        with self.assertRaises(JobStateError):
            TaskStore(self.root, SCOPE)
        self.assertFalse(key_path.exists())

    def test_scope_key_is_private_reused_and_not_stored_in_journal(self):
        key = load_scope_key(self.root)
        self.assertEqual(load_scope_key(self.root), key)
        store = self.store()
        store.create(REFERENCE, KEY)
        self.assertNotIn(key.encode(), store.path.read_bytes())
        require_private_file(self.root / ".zlib-jobs" / "scope-key")

    def test_unsafe_permissions_are_rejected_without_repair(self):
        store = self.store()
        store.create(REFERENCE, KEY)
        path = store.path
        store.close()
        if os.name == "nt":
            from zlib_connector.private_files import _WindowsSecurity
            security = _WindowsSecurity()
            security._apply_dacl(path, f"D:P(A;;FA;;;{security.current_user()})(A;;FA;;;SY)(A;;GR;;;WD)")
        else:
            path.chmod(0o644)
        with self.assertRaises(JobError):
            TaskStore(self.root, SCOPE)
        with self.assertRaises(ValueError):
            require_private_file(path)
        protect_private_file(path)
        self.assertEqual(len(self.store().all()), 1)

    def test_nonregular_metadata_and_missing_root_are_not_created_or_repaired(self):
        missing = self.root / "missing"
        with self.assertRaises(JobError):
            TaskStore(missing, SCOPE)
        self.assertFalse(missing.exists())
        metadata = self.root / ".zlib-jobs"
        metadata.write_bytes(b"keep")
        with self.assertRaises(JobError):
            TaskStore(self.root, SCOPE)
        self.assertEqual(metadata.read_bytes(), b"keep")

    def test_replaced_journal_stops_the_existing_owner(self):
        store = self.store()
        store.create(REFERENCE, KEY)
        data = store.path.read_bytes()
        self.rewrite(store.path, data)
        with self.assertRaises(JobError):
            store.all()
        with self.assertRaises(JobError) as caught:
            store.create(REFERENCE, KEY)
        self.assertIs(type(caught.exception), JobError)


if __name__ == "__main__":
    unittest.main()
