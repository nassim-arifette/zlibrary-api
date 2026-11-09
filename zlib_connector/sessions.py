"""Private versioned session snapshots with atomic compare-and-swap publication."""
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time

from .errors import ConnectorError
from .local_state import state_directory
from .private_files import create_private_file, require_private_file
from .session_data import SavedSession, SessionCookie
from .session_lock import PrivateLockError, PrivateLockTimeout, private_lock


MAX_SESSION_BYTES = 32768
_UNSPECIFIED = object()


class SessionError(ConnectorError):
    """The private local session could not be read or updated safely."""


class SessionConflictError(SessionError):
    """Another login or logout changed the session after it was read."""


class SessionLockTimeoutError(SessionError):
    """The bounded wait for another session writer expired."""


@dataclass(frozen=True)
class SessionSnapshot:
    generation: str | None
    session: SavedSession | None = field(repr=False)

    def status(self) -> dict:
        if self.session is not None:
            return self.session.status()
        return {"state": "empty", "domain": None, "saved_at": None, "expires_at": None, "verified": False}


def _generation(value):
    if value is not None and (not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None):
        raise ValueError("The expected session generation is invalid.")
    return value


def _regular(info):
    return stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _reader(path: Path):
    """Windows readers share DELETE so an atomic publication can replace their name."""
    if os.name == "nt":
        import ctypes
        import msvcrt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                           ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        create.restype = ctypes.c_void_p
        close = kernel.CloseHandle
        close.argtypes, close.restype = [ctypes.c_void_p], ctypes.c_int
        handle = create(str(path), 0x80000000, 7, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            close(handle)
            raise
    else:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _private_bytes(path: Path) -> bytes | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not _regular(before) or before.st_size > MAX_SESSION_BYTES:
        raise ValueError
    require_private_file(path)
    with _reader(path) as source:
        opened = os.fstat(source.fileno())
        if not _regular(opened) or _signature(opened) != _signature(before) or opened.st_size > MAX_SESSION_BYTES:
            raise ValueError
        if os.name != "nt" and (opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) & 0o077):
            raise ValueError
        data = source.read(MAX_SESSION_BYTES + 1)
        if (len(data) > MAX_SESSION_BYTES or len(data) != opened.st_size
                or _signature(os.fstat(source.fileno())) != _signature(opened)):
            raise ValueError
    return data


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError


def _decode(data: bytes) -> SessionSnapshot:
    record = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if (not isinstance(record, dict) or set(record) != {"version", "generation", "session"}
            or type(record["version"]) is not int or record["version"] != 1
            or _generation(record["generation"]) is None):
        raise ValueError
    session = record["session"]
    if session is not None:
        if (not isinstance(session, dict) or set(session) != {"origin", "saved_at", "cookies"}
                or not isinstance(session["cookies"], list) or len(session["cookies"]) != 2):
            raise ValueError
        cookies = []
        for cookie in session["cookies"]:
            if not isinstance(cookie, dict) or set(cookie) != {"name", "value", "path", "expires"}:
                raise ValueError
            cookies.append(SessionCookie(**cookie))
        saved = SavedSession(session["origin"], tuple(cookies), session["saved_at"])
        if saved.origin != session["origin"]:
            raise ValueError
        session = saved
    return SessionSnapshot(record["generation"], session)


def _encode(snapshot: SessionSnapshot) -> bytes:
    session = snapshot.session
    record = None if session is None else {
        "origin": session.origin, "saved_at": session.saved_at,
        "cookies": [{"name": cookie.name, "value": cookie.value, "path": cookie.path, "expires": cookie.expires}
                    for cookie in session.cookies],
    }
    encoded = (json.dumps({"version": 1, "generation": snapshot.generation, "session": record},
                          ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(encoded) > MAX_SESSION_BYTES:
        raise ValueError
    return encoded


class SessionStore:
    """Read without locks or creation; publish under a stable private native lock.

    The default state directory is created only for writes. An explicit file's
    parent must already exist. Existing permissions are checked, never repaired.
    A tombstone's generation prevents a login started before logout from returning.
    Lock acquisition waits at most lock_timeout seconds (default five). On Windows,
    publication may wait at most another lock_timeout for an open reader to close;
    that timeout raises SessionError and leaves the previous snapshot in place.
    """

    def __init__(self, path=None, *, lock_timeout: float = 5.0):
        if type(lock_timeout) not in (int, float) or not math.isfinite(lock_timeout) or not 0 < lock_timeout <= 60:
            raise ValueError("Session lock timeout must be between 0 and 60 seconds.")
        self._default = path is None
        self.path = (state_directory() / "session.json" if path is None else Path(path).expanduser()).absolute()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.lock_timeout = float(lock_timeout)

    def read(self) -> SessionSnapshot:
        try:
            data = _private_bytes(self.path)
            return SessionSnapshot(None, None) if data is None else _decode(data)
        except Exception:
            raise SessionError("The local session could not be read safely.") from None

    @contextmanager
    def _write_lock(self):
        try:
            if self._default:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with private_lock(self.lock_path, self.lock_timeout) as check_lock:
                yield check_lock
        except PrivateLockTimeout:
            raise SessionLockTimeoutError("Another process is updating the local session; try again.") from None
        except SessionError:
            raise
        except (OSError, ValueError, PrivateLockError):
            raise SessionError("The local session could not be updated safely.") from None

    def _publish(self, snapshot: SessionSnapshot, check_lock) -> SessionSnapshot:
        encoded = _encode(snapshot)
        temporary = None
        try:
            candidate = self.path.parent / (".session-" + secrets.token_hex(16))
            with create_private_file(candidate) as target:
                temporary = candidate
                target.write(encoded)
                target.flush()
                os.fsync(target.fileno())
            deadline = time.monotonic() + self.lock_timeout
            while True:
                check_lock()
                try:
                    os.replace(temporary, self.path)
                    break
                except OSError as error:
                    remaining = deadline - time.monotonic()
                    if os.name != "nt" or getattr(error, "winerror", None) not in (5, 32) or remaining <= 0:
                        raise
                    # Windows may reject replacement while a short, lock-free read
                    # remains open. Keep the same complete temp and bound the wait.
                    time.sleep(min(0.02, remaining))
            temporary = None
            return snapshot
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def save(self, session: SavedSession, *, expected_generation: str | None = None) -> SessionSnapshot:
        if not isinstance(session, SavedSession):
            raise ValueError("A validated session is required.")
        expected_generation = _generation(expected_generation)
        with self._write_lock() as check_lock:
            current = self.read()
            if current.generation != expected_generation:
                raise SessionConflictError("The stored session changed; read its current state before saving again.")
            return self._publish(SessionSnapshot(secrets.token_hex(16), session), check_lock)

    def clear(self, *, expected_generation=_UNSPECIFIED) -> SessionSnapshot:
        if expected_generation is not _UNSPECIFIED:
            expected_generation = _generation(expected_generation)
        with self._write_lock() as check_lock:
            current = self.read()
            if expected_generation is not _UNSPECIFIED and current.generation != expected_generation:
                raise SessionConflictError("The stored session changed; read its current state before clearing it.")
            return self._publish(SessionSnapshot(secrets.token_hex(16), None), check_lock)
