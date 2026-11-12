"""A private operational journal with durable idempotency and one native owner."""
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from threading import RLock
import time

from ..api_token import load_api_token
from ..private_files import create_private_file, require_private_file
from ..session_lock import PrivateLockError, PrivateLockTimeout, private_lock
from ..transfer import TransferProgress
from .models import (DownloadTask, FileReceipt, JobBusyError, JobCapacityError, JobConflictError,
                     JobError, JobNotFoundError, JobStateError, TERMINAL_STATES,
                     validate_idempotency_key, validate_reference, validate_task_id)


MAX_TASKS = 10_000
MAX_JOURNAL_BYTES = 32 * 1024 * 1024
_TRANSITIONS = {
    "queued": ("running", "cancelled"),
    "running": ("running", "waiting_quota", "succeeded", "failed", "cancelled", "interrupted"),
    "waiting_quota": ("queued", "cancelled"),
}


def _identity(info):
    return info.st_dev, info.st_ino


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _not_link(info):
    return not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400


def _directory(path: Path, *, create=False):
    try:
        if create:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or not _not_link(info):
            raise ValueError
        return _identity(info)
    except (OSError, ValueError):
        raise JobError("The configured task storage directory is unavailable or unsafe.") from None


def _root(directory) -> tuple[Path, tuple]:
    try:
        supplied = Path(directory).expanduser().absolute()
        identity = _directory(supplied)
        resolved = supplied.resolve(strict=True)
        if _directory(resolved) != identity:
            raise ValueError
        return resolved, identity
    except (OSError, ValueError, TypeError):
        raise JobError("Download tasks require an existing regular directory.") from None


def _check_directory(path: Path, identity):
    if _directory(path) != identity:
        raise JobError("A task storage directory changed. Stop and reopen the download manager.")


def load_scope_key(directory) -> str:
    """Reuse the private HMAC key; never replace a lost key when journals exist."""
    root, root_identity = _root(directory)
    metadata = root / ".zlib-jobs"
    metadata_identity = _directory(metadata, create=True)
    key_path = metadata / "scope-key"
    try:
        with private_lock(metadata / ".scope-key.lock", 1.0) as check_lock:
            _check_directory(root, root_identity)
            _check_directory(metadata, metadata_identity)
            try:
                key_path.lstat()
            except FileNotFoundError:
                for child in metadata.iterdir():
                    if re.fullmatch(r"[0-9a-f]{64}", child.name) is None:
                        continue
                    _directory(child)
                    try:
                        (child / "tasks.json").lstat()
                    except FileNotFoundError:
                        continue
                    raise JobStateError("The private queue key is missing while task journals exist.")
            key = load_api_token(key_path)
            check_lock()
            _check_directory(root, root_identity)
            _check_directory(metadata, metadata_identity)
            return key
    except PrivateLockTimeout:
        raise JobBusyError("Another process is initializing task storage. Try again shortly.") from None
    except JobError:
        raise
    except (OSError, ValueError, PrivateLockError):
        raise JobError("The private task scope key could not be loaded safely.") from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError


def _sync_directory(path: Path):
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _task_record(task: DownloadTask) -> dict:
    progress = task.progress
    return {"id": task.id, "reference": task.reference, "state": task.state,
            "created_at": task.created_at, "updated_at": task.updated_at,
            "progress": None if progress is None else {"bytes_received": progress.bytes_received,
                "total_bytes": progress.total_bytes, "elapsed_seconds": progress.elapsed_seconds},
            "cancel_requested": task.cancel_requested, "error_code": task.error_code,
            "receipt": asdict(task.receipt) if task.receipt is not None else None}


def _parse_task(record) -> DownloadTask:
    if not isinstance(record, dict) or set(record) != {
        "id", "reference", "state", "created_at", "updated_at", "progress", "cancel_requested", "error_code", "receipt"
    }:
        raise ValueError
    values = dict(record)
    progress = values["progress"]
    if progress is not None:
        if not isinstance(progress, dict) or set(progress) != {"bytes_received", "total_bytes", "elapsed_seconds"}:
            raise ValueError
        values["progress"] = TransferProgress(**progress)
    receipt = values["receipt"]
    if receipt is not None:
        if not isinstance(receipt, dict) or set(receipt) != {
            "name", "size", "device", "inode", "mtime_ns", "ctime_ns", "dir_device", "dir_inode"
        }:
            raise ValueError
        values["receipt"] = FileReceipt(**receipt)
    return DownloadTask(**values)


def _decode(data: bytes, scope: str):
    try:
        record = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        if (not isinstance(record, dict) or set(record) != {"version", "scope", "tasks", "paused"}
                or type(record["version"]) is not int or record["version"] != 1 or record["scope"] != scope
                or type(record["paused"]) is not bool or not isinstance(record["tasks"], list)
                or len(record["tasks"]) > MAX_TASKS):
            raise ValueError
        entries, identifiers, keys = [], set(), set()
        for row in record["tasks"]:
            if not isinstance(row, dict) or set(row) != {"key_hash", "task"}:
                raise ValueError
            key = row["key_hash"]
            if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None or key in keys:
                raise ValueError
            task = _parse_task(row["task"])
            if task.id in identifiers:
                raise ValueError
            entries.append((key, task))
            identifiers.add(task.id)
            keys.add(key)
        return tuple(entries), record["paused"]
    except Exception:
        raise JobStateError("The task journal has an invalid or unsupported format.") from None


class TaskStore:
    """Own a scoped journal until close; snapshots are immutable and writes are atomic.

    Task count is capped at 10,000 and JSON at 32 MiB. A reduced max_tasks keeps
    existing records/idempotency keys readable but blocks new admissions at capacity.
    A storage failure stops further use until this owner closes and the store reopens.
    """

    def __init__(self, directory, scope: str, *, max_tasks: int = 1000):
        if not isinstance(scope, str) or re.fullmatch(r"[0-9a-f]{64}", scope) is None:
            raise ValueError("A task account scope must be a SHA-256 fingerprint.")
        if type(max_tasks) is not int or not 1 <= max_tasks <= MAX_TASKS:
            raise ValueError("Maximum task count must be between 1 and 10000.")
        self._mutex = RLock()
        self._lease = None
        self._closed = False
        self._failed = False
        self._paused = False
        self._journal_signature = None
        self._entries = ()
        self._tasks = {}
        self._keys = {}
        self.scope, self.max_tasks = scope, max_tasks
        try:
            self.directory, self._root_identity = _root(directory)
            load_scope_key(self.directory)
            self._metadata = self.directory / ".zlib-jobs"
            self._metadata_identity = _directory(self._metadata)
            self._scope_directory = self._metadata / scope
            self._scope_identity = _directory(self._scope_directory, create=True)
            self.path = self._scope_directory / "tasks.json"
            self.lock_path = self._scope_directory / ".owner.lock"
            lease = private_lock(self.lock_path, 0.1)
            try:
                self._check_lease = lease.__enter__()
            except PrivateLockTimeout:
                raise JobBusyError("Another download manager owns this task journal.") from None
            except (PrivateLockError, OSError, ValueError):
                raise JobError("The task ownership lease could not be acquired safely.") from None
            self._lease = lease
            entries, self._paused = self._read()
            now = time.time()
            recovered = tuple((key, replace(task, state="interrupted", error_code="Interrupted",
                                            updated_at=max(now, task.updated_at))) if task.state == "running" else (key, task)
                              for key, task in entries)
            if self._journal_signature is None or recovered != entries:
                self._publish(recovered)
            else:
                self._install(entries)
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        with self._mutex:
            self._closed = True
            if self._lease is not None:
                lease, self._lease = self._lease, None
                lease.__exit__(None, None, None)

    def _check(self):
        if self._closed or self._failed:
            raise JobError("Task storage is closed or unavailable; reopen the download manager.")
        try:
            _check_directory(self.directory, self._root_identity)
            _check_directory(self._metadata, self._metadata_identity)
            _check_directory(self._scope_directory, self._scope_identity)
            self._check_lease()
            try:
                info = self.path.lstat()
            except FileNotFoundError:
                if self._journal_signature is not None:
                    raise ValueError
            else:
                if (not stat.S_ISREG(info.st_mode) or not _not_link(info)
                        or _signature(info) != self._journal_signature):
                    raise ValueError
                require_private_file(self.path)
        except (OSError, ValueError, PrivateLockError, JobError):
            self._failed = True
            raise JobError("Task storage changed or became unsafe. Further downloads must stop.") from None

    def _read(self):
        try:
            try:
                before = self.path.lstat()
            except FileNotFoundError:
                return (), False
            if not stat.S_ISREG(before.st_mode) or not _not_link(before) or before.st_size > MAX_JOURNAL_BYTES:
                raise ValueError
            require_private_file(self.path)
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            try:
                source = os.fdopen(descriptor, "rb")
            except BaseException:
                os.close(descriptor)
                raise
            with source:
                opened = os.fstat(source.fileno())
                if (not stat.S_ISREG(opened.st_mode) or _signature(opened) != _signature(before)
                        or (os.name != "nt" and (opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) & 0o077))):
                    raise ValueError
                data = source.read(MAX_JOURNAL_BYTES + 1)
                if len(data) != opened.st_size or len(data) > MAX_JOURNAL_BYTES or _signature(os.fstat(source.fileno())) != _signature(opened):
                    raise ValueError
            self._journal_signature = _signature(opened)
        except (OSError, ValueError):
            raise JobError("The task journal could not be read safely.") from None
        return _decode(data, self.scope)

    def _install(self, entries):
        self._entries = tuple(entries)
        self._tasks = {task.id: task for _, task in entries}
        self._keys = {key: task.id for key, task in entries}

    def _publish(self, entries, *, paused=None):
        paused = self._paused if paused is None else paused
        record = {"version": 1, "scope": self.scope, "paused": paused,
                  "tasks": [{"key_hash": key, "task": _task_record(task)} for key, task in entries]}
        encoded = (json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        if len(encoded) > MAX_JOURNAL_BYTES:
            raise JobCapacityError("The task journal has reached its storage limit.")
        temporary = None
        try:
            self._check()
            candidate = self._scope_directory / (".tasks-" + secrets.token_hex(16))
            with create_private_file(candidate) as target:
                temporary = candidate
                target.write(encoded)
                target.flush()
                os.fsync(target.fileno())
            self._check()
            os.replace(temporary, self.path)
            temporary = None
            _sync_directory(self._scope_directory)
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or not _not_link(info):
                raise ValueError
            self._journal_signature = _signature(info)
            self._install(entries)
            self._paused = paused
        except (OSError, ValueError, PrivateLockError):
            self._failed = True
            raise JobError("The task update could not be saved safely. Further downloads must stop.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def create(self, reference: str, idempotency_key: str) -> DownloadTask:
        reference = validate_reference(reference)
        key = hashlib.sha256(validate_idempotency_key(idempotency_key).encode("ascii")).hexdigest()
        with self._mutex:
            self._check()
            existing_id = self._keys.get(key)
            if existing_id is not None:
                existing = self._tasks[existing_id]
                if existing.reference != reference:
                    raise JobConflictError("This idempotency key already belongs to a different download request.")
                return existing
            if len(self._tasks) >= self.max_tasks:
                raise JobCapacityError("The task registry is full. Existing idempotency keys remain usable.")
            identifier = secrets.token_hex(16)
            if identifier in self._tasks:
                raise JobError("A unique task identifier could not be allocated.")
            now = time.time()
            task = DownloadTask(identifier, reference, "queued", now, now)
            self._publish((*self._entries, (key, task)))
            return task

    def find(self, idempotency_key: str) -> DownloadTask | None:
        """Return the task recorded for this idempotency key, without creating one."""
        key = hashlib.sha256(validate_idempotency_key(idempotency_key).encode("ascii")).hexdigest()
        with self._mutex:
            self._check()
            identifier = self._keys.get(key)
            return None if identifier is None else self._tasks[identifier]

    def get(self, identifier: str) -> DownloadTask:
        identifier = validate_task_id(identifier)
        with self._mutex:
            self._check()
            if identifier not in self._tasks:
                raise JobNotFoundError("The download task was not found.")
            return self._tasks[identifier]

    def all(self) -> tuple[DownloadTask, ...]:
        with self._mutex:
            self._check()
            return tuple(task for _, task in self._entries)

    @property
    def paused(self) -> bool:
        with self._mutex:
            self._check()
            return self._paused

    def set_paused(self, paused: bool) -> bool:
        if type(paused) is not bool:
            raise ValueError("The task queue pause state must be a boolean.")
        with self._mutex:
            self._check()
            if paused != self._paused:
                self._publish(self._entries, paused=paused)
            return self._paused

    def update(self, task: DownloadTask) -> DownloadTask:
        if not isinstance(task, DownloadTask):
            raise ValueError("A validated download task is required.")
        with self._mutex:
            self._check()
            current = self._tasks.get(task.id)
            if current is None:
                raise JobNotFoundError("The download task was not found.")
            if task.reference != current.reference or task.created_at != current.created_at:
                raise JobStateError("Task identifiers, references and creation times cannot change.")
            if task == current:
                return current
            if current.state in TERMINAL_STATES or task.state not in _TRANSITIONS.get(current.state, ()):
                raise JobStateError("This task state transition is not allowed.")
            task = replace(task, cancel_requested=current.cancel_requested or task.cancel_requested,
                           updated_at=max(task.updated_at, current.updated_at))
            self._publish(tuple((key, task if existing.id == task.id else existing) for key, existing in self._entries))
            return task
