"""Private bearer-token storage for the local HTTP API."""
import os
from pathlib import Path
import re
import secrets
import stat

from .private_files import create_private_file, require_private_file


def _read_token(path: Path) -> str:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("The API token must be stored in a regular file, not a link.")
        identity = info.st_dev, info.st_ino
        require_private_file(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("The API token must be stored in a regular file.")
            if (info.st_dev, info.st_ino) != identity:
                raise ValueError("The API token file changed while opening it.")
            if info.st_size > 1024:
                raise ValueError("The API token file exceeds its size limit.")
            if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("The API token file must be accessible only to its owner.")
            value = source.read(1024).decode("ascii").rstrip("\r\n")
    except UnicodeError:
        raise ValueError("The API token file is not a valid ASCII token.") from None
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value):
        raise ValueError("The API token file must contain 32 to 256 URL-safe characters.")
    return value


def read_api_token(path: Path) -> str:
    """Read an existing private token; a CLI client never creates server state."""
    return _read_token(Path(path).expanduser().absolute())


def load_api_token(path: Path) -> str:
    """Reuse a private token or publish a new one atomically, without printing it."""
    path = Path(path).expanduser().absolute()
    if path.exists() or path.is_symlink():
        return _read_token(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        candidate = path.parent / (".api-token-" + secrets.token_hex(16))
        with create_private_file(candidate) as target:
            temporary = candidate
            target.write((secrets.token_urlsafe(32) + "\n").encode("ascii"))
            target.flush()
            os.fsync(target.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass  # A concurrent process published its complete token first.
        return _read_token(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
