"""Task-owned download directories and identity-checked artifact reads."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
from typing import BinaryIO

from .models import FileReceipt, JobFileError, validate_task_id


_TYPES = {".pdf": "application/pdf", ".epub": "application/epub+zip", ".djvu": "image/vnd.djvu"}
_FILE_ERROR = "The task file is missing, changed, or outside its owned directory."


def _identity(info):
    return info.st_dev, info.st_ino


def _path_signature(info):
    # Windows lstat and fstat can give ctime different meanings. Compare it
    # only between descriptor snapshots, including the persisted receipt.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _signature(info):
    return (*_path_signature(info), info.st_ctime_ns)


def _directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise JobFileError(_FILE_ERROR)
    return info


def _regular(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise JobFileError(_FILE_ERROR)


def _task_id(value):
    try:
        return validate_task_id(value)
    except ValueError:
        raise JobFileError(_FILE_ERROR) from None


@dataclass(frozen=True)
class OwnedDirectory:
    path: Path = field(repr=False)
    device: int
    inode: int


@dataclass(frozen=True)
class DownloadFile:
    stream: BinaryIO = field(repr=False)
    name: str
    content_type: str
    size: int


class _CheckedReader:
    """Check a pinned descriptor before and after each bounded HTTP read."""

    def __init__(self, stream, signature):
        self._stream, self._signature = stream, signature

    def read(self, size=-1):
        try:
            before = os.fstat(self._stream.fileno())
            _regular(before)
            if _signature(before) != self._signature:
                raise JobFileError(_FILE_ERROR)
            data = self._stream.read(size)
            after = os.fstat(self._stream.fileno())
            _regular(after)
            if _signature(after) != self._signature:
                raise JobFileError(_FILE_ERROR)
            return data
        except (OSError, ValueError):
            raise JobFileError(_FILE_ERROR) from None


class TaskFiles:
    def __init__(self, directory):
        try:
            supplied = Path(directory).expanduser().absolute()
            # A lexical '..' or a reparse point cannot hide a different root.
            if ".." in supplied.parts:
                raise JobFileError(_FILE_ERROR)
            for parent in (supplied, *supplied.parents):
                _directory(parent)
            self.directory = supplied.resolve(strict=True)
            self._root_identity = _identity(_directory(self.directory))
        except (OSError, ValueError):
            raise JobFileError("The download root must be an existing directory without links.") from None

    def _check_root(self):
        for parent in (self.directory, *self.directory.parents):
            _directory(parent)
        if _identity(_directory(self.directory)) != self._root_identity:
            raise JobFileError(_FILE_ERROR)

    def _check_owned(self, owned):
        self._check_root()
        if owned.path.parent != self.directory or _identity(_directory(owned.path)) != (owned.device, owned.inode):
            raise JobFileError(_FILE_ERROR)

    def prepare(self, task_id) -> OwnedDirectory:
        try:
            task_id = _task_id(task_id)
            self._check_root()
            path = self.directory / task_id
            path.mkdir(mode=0o700)
            info = _directory(path)
            owned = OwnedDirectory(path, *_identity(info))
            self._check_owned(owned)
            return owned
        except (OSError, ValueError):
            raise JobFileError("A fresh task directory could not be created.") from None

    @contextmanager
    def _open(self, owned, name, *, expected=None):
        stream = None
        try:
            self._check_owned(owned)
            if (not isinstance(name, str) or not name or name in (".", "..")
                    or Path(name).name != name or any(character in name for character in ("/", "\\", ":", "\x00"))
                    or Path(name).suffix.lower() not in _TYPES):
                raise JobFileError(_FILE_ERROR)
            path = owned.path / name
            before = path.lstat()
            _regular(before)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags)
            try:
                stream = os.fdopen(descriptor, "rb")
            except BaseException:
                os.close(descriptor)
                raise
            opened = os.fstat(stream.fileno())
            _regular(opened)
            if (_path_signature(before) != _path_signature(opened)
                    or (expected is not None and _signature(opened) != expected)):
                raise JobFileError(_FILE_ERROR)
            self._check_owned(owned)
            final = path.lstat()
            _regular(final)
            if _path_signature(final) != _path_signature(opened):
                raise JobFileError(_FILE_ERROR)
            yield stream, opened
        except (OSError, ValueError):
            raise JobFileError(_FILE_ERROR) from None
        finally:
            if stream is not None:
                stream.close()

    def capture(self, owned: OwnedDirectory, result: Path) -> FileReceipt:
        try:
            result = Path(result)
            if not result.is_absolute() or result.parent != owned.path:
                raise JobFileError(_FILE_ERROR)
            with self._open(owned, result.name) as (_, info):
                return FileReceipt(result.name, info.st_size, info.st_dev, info.st_ino,
                                   info.st_mtime_ns, info.st_ctime_ns, owned.device, owned.inode)
        except (OSError, ValueError, TypeError):
            raise JobFileError(_FILE_ERROR) from None

    @contextmanager
    def open(self, task_id, receipt: FileReceipt):
        task_id = _task_id(task_id)
        owned = OwnedDirectory(self.directory / task_id, receipt.dir_device, receipt.dir_inode)
        expected = (receipt.device, receipt.inode, receipt.size, receipt.mtime_ns, receipt.ctime_ns)
        with self._open(owned, receipt.name, expected=expected) as (stream, _):
            yield DownloadFile(_CheckedReader(stream, expected), receipt.name,
                               _TYPES[Path(receipt.name).suffix.lower()], receipt.size)
