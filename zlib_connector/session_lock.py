"""A stable private OS lock for short session writes; lock files are never removed."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import time

from .private_files import create_private_file, require_private_file


class PrivateLockError(Exception):
    pass


class PrivateLockTimeout(PrivateLockError):
    pass


def _identity(info):
    return info.st_dev, info.st_ino


def _regular(info):
    return stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400


def _open_lock(path: Path):
    try:
        with create_private_file(path):
            pass
    except FileExistsError:
        pass
    before = path.lstat()
    if not _regular(before) or before.st_size != 0:
        raise PrivateLockError
    require_private_file(path)
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not _regular(opened) or _identity(opened) != _identity(before) or opened.st_size != 0:
            raise PrivateLockError
        source = os.fdopen(descriptor, "r+b")
    except BaseException:
        os.close(descriptor)
        raise
    return source, _identity(opened)


def _pause(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PrivateLockTimeout
    time.sleep(min(0.02, remaining))


def _lock(descriptor):
    if os.name == "nt":
        import msvcrt
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)  # Locking beyond EOF is supported.
    else:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(descriptor):
    if os.name == "nt":
        import msvcrt
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def private_lock(path: Path, timeout: float):
    """Yield an identity check while holding a lock; never recover by unlinking it."""
    deadline = time.monotonic() + timeout
    source = None
    acquired = False
    try:
        while source is None:
            try:
                source, identity = _open_lock(path)
            except OSError as error:
                # Another creator's initial private handle briefly denies write sharing.
                if os.name == "nt" and getattr(error, "winerror", None) == 32:
                    _pause(deadline)
                    continue
                raise PrivateLockError from None
            except ValueError:
                raise PrivateLockError from None
        while not acquired:
            try:
                _lock(source.fileno())
                acquired = True
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise PrivateLockError from None
                _pause(deadline)

        def check_identity():
            try:
                current = path.lstat()
                if not _regular(current) or _identity(current) != identity or current.st_size != 0:
                    raise PrivateLockError
                require_private_file(path)
            except (OSError, ValueError):
                raise PrivateLockError from None

        check_identity()
        yield check_identity
    finally:
        if source is not None:
            if acquired:
                try:
                    _unlock(source.fileno())
                except OSError:
                    pass  # Closing the descriptor also releases the OS lock.
            try:
                source.close()
            except OSError:
                pass
