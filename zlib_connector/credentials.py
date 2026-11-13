"""Explicit local credential configuration; never log or repr its contents.

Lookup (``credentials_path``): an explicit file, else ``credentials.toml`` in the
settings folder, else the legacy ``credentials.local.toml`` beside the package.
"""
from dataclasses import dataclass
from pathlib import Path
import tomllib

from . import local_state

# The legacy location, kept for compatibility: beside the package (a source checkout or site-packages).
DEFAULT_FILE = Path(__file__).resolve().parents[1] / "credentials.local.toml"
FILE_NAME = "credentials.toml"
_DEFAULT_DOMAIN = "https://z-lib.by/"


@dataclass(frozen=True, repr=False)
class Credentials:
    domain: str = _DEFAULT_DOMAIN
    email: str = ""
    password: str = ""
    has_domain: bool = True  # False when the file did not set domain (or no file was found).


def default_credentials_file() -> Path:
    """``credentials.toml`` in the settings folder, whether or not it exists."""
    return local_state.config_directory() / FILE_NAME


def legacy_credentials_file() -> Path:
    return DEFAULT_FILE


def credentials_path(explicit: Path | None = None) -> Path | None:
    """The file ``load_credentials`` reads: ``explicit``, else the settings folder's file, else the legacy one.

    Returns None when no file exists. Only checks existence; never reads a file.
    """
    if explicit is not None:
        return Path(explicit)
    for candidate in (default_credentials_file(), legacy_credentials_file()):
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def is_legacy(path: Path | None) -> bool:
    return path is not None and Path(path) == legacy_credentials_file()


def load_credentials(path: Path | None = None) -> Credentials:
    source = credentials_path(path)
    if source is None:
        return Credentials(has_domain=False)
    if not source.exists():
        if path is not None:
            raise ValueError("The specified credentials file does not exist.")
        return Credentials(has_domain=False)
    try:
        values = tomllib.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ValueError("Cannot read the local credentials file. Check its TOML syntax; its contents are not shown.") from None
    allowed = {"domain", "email", "password"}
    if set(values) - allowed or any(not isinstance(value, str) for value in values.values()):
        raise ValueError("Credentials must contain only string fields: domain, email, password.")
    return Credentials(domain=values.get("domain", _DEFAULT_DOMAIN).strip(), email=values.get("email", "").strip(),
                       password=values.get("password", ""), has_domain="domain" in values)
