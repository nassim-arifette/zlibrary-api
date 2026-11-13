"""Persistent preferences: one resolver for the CLI, ``serve``, the TUI launcher and the MCP server.

Precedence for every key: an explicit override (a CLI option), then the ``ZLIB_*``
environment variable (non-empty), then the config file, then the built-in default.
Lists replace each other level by level. ``load_settings`` never prints, never
touches the network and never reads credentials, the session or the API token.
The config file never holds secrets. Writes (``ConfigStore``) are locked, compare
generations, go through a private temporary file and replace the file atomically.
"""
from dataclasses import dataclass, field
import hashlib
import math
import os
from pathlib import Path
import secrets
import stat
import time
import tomllib

from . import local_state
from .error_codes import Coded, ErrorCode
from .private_files import create_private_file
from .session_lock import PrivateLockError, PrivateLockTimeout, private_lock
from .terminal_text import terminal_text


KEYS = ("domain", "timeout", "download_dir", "languages", "formats", "api_url", "credentials_file", "progress")
LIST_KEYS = frozenset({"languages", "formats"})
VARIABLES = {"domain": "ZLIB_DOMAIN", "timeout": "ZLIB_TIMEOUT", "download_dir": "ZLIB_DOWNLOAD_DIR",
             "languages": "ZLIB_LANGUAGES", "formats": "ZLIB_FORMATS", "api_url": "ZLIB_API_URL",
             "credentials_file": "ZLIB_CREDENTIALS", "progress": "ZLIB_PROGRESS"}
CONFIG_VARIABLE = "ZLIB_CONFIG"
SECRET_KEYS = frozenset({"email", "password", "token", "api_token", "cookie", "cookies", "session", "secret"})
DEFAULT_TIMEOUT = 30.0
DEFAULT_API_URL = "http://127.0.0.1:8000/v1"
FILE_NAME = "config.toml"
MAX_CONFIG_BYTES = 64 * 1024
LOCK_TIMEOUT = 5.0
HEADER = '# zlib-cli settings. "zlib-cli config set" rewrites this file and drops comments.'
SECRET_MESSAGE = "Credentials and session data are never stored in the config file."
_DEFAULTS = {"domain": None, "timeout": DEFAULT_TIMEOUT, "download_dir": None, "languages": (), "formats": (),
             "api_url": DEFAULT_API_URL, "credentials_file": None, "progress": "auto"}
PROGRESS_CHOICES = ("auto", "always", "never")


class SettingsError(ValueError, Coded):
    """A settings failure. ``code``: config_invalid, invalid_argument (a CLI value), config_conflict,
    config_busy or local_io_error. ``key`` and ``source`` (cli, env, config) name the culprit; no value is shown."""

    def __init__(self, message, *, code="config_invalid", key=None, source=None, hint=None):
        super().__init__(message)
        self.code, self.key, self.source, self.hint = code, key, source, hint

    def classification(self) -> ErrorCode:
        return ErrorCode(self.code, retryable=self.code in ("config_conflict", "config_busy"))


@dataclass(frozen=True)
class Settings:
    domain: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    download_dir: Path | None = None
    languages: tuple = ()
    formats: tuple = ()
    api_url: str = DEFAULT_API_URL
    credentials_file: Path | None = None
    progress: str = "auto"  # auto (only in a terminal), always or never
    config_path: Path | None = None
    config_exists: bool = False
    config_source: str = "default"  # cli (--config), env (ZLIB_CONFIG) or default
    origins: dict = field(default_factory=dict)  # key -> (origin, detail): ("cli", "--timeout"), ("env", "ZLIB_TIMEOUT")

    def origin(self, key) -> str:
        return self.origins.get(key, ("default", None))[0]

    def source_label(self, key) -> str:
        """Who set a value, for messages: --timeout, ZLIB_TIMEOUT, the config file (timeout) or the default."""
        origin, detail = self.origins.get(key, ("default", None))
        if origin in ("cli", "env"):
            return detail
        if origin == "config":
            return f"the config file ({key})"
        return "the default"


# Names and paths ---------------------------------------------------------------------------------------------


def shown_key(key) -> str:
    text = terminal_text(str(key))
    return text if len(text) <= 64 else text[:61] + "..."


def default_config_file() -> Path:
    return local_state.config_directory() / FILE_NAME


def config_file_path(config_file=None, *, environ=None) -> tuple[Path, str]:
    """The config file and its source: ``cli`` (``config_file``), ``env`` (ZLIB_CONFIG) or ``default``."""
    environ = os.environ if environ is None else environ
    if config_file is not None:
        return Path(config_file).expanduser().absolute(), "cli"
    value = (environ.get(CONFIG_VARIABLE) or "").strip()
    if value:
        return Path(value).expanduser().absolute(), "env"
    return default_config_file(), "default"


def _source_text(source, path) -> str:
    return {"cli": f"--config {path}", "env": f"{CONFIG_VARIABLE} ({path})"}.get(source, str(path))


# Validators: (value, source) -> normalized value; raise ValueError ------------------------------------------


def _domain(value):
    from .session_data import canonical_origin
    if not isinstance(value, str):
        raise TypeError
    return canonical_origin(value.strip())


def _timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError
    if not math.isfinite(value) or not 0 < value <= 300:
        raise ValueError
    return float(value)


def _path(value, *, relative_ok):
    if isinstance(value, Path):
        path = value
    elif isinstance(value, str) and value.strip() and value.isprintable():
        path = Path(value.strip())
    else:
        raise TypeError
    path = path.expanduser()
    if not path.is_absolute():
        if not relative_ok:
            raise ValueError
        path = path.absolute()
    return path


def _choices(value, *, formats):
    from .search import _choices as choices
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise TypeError
    return choices(value, formats=formats)


def _api_url(value):
    from .local_api import _address
    if not isinstance(value, str):
        raise TypeError
    host, port = _address(value.strip())
    return f"http://{host}:{port}/v1"


def validate(key, value, *, source="config"):
    """Normalize one value from ``source`` (cli, env, config); raise ValueError or TypeError on a bad value."""
    relative_ok = source == "cli"
    if key == "domain":
        return _domain(value)
    if key == "timeout":
        return _timeout(value)
    if key in ("download_dir", "credentials_file"):
        return _path(value, relative_ok=relative_ok)
    if key == "languages":
        return _choices(value, formats=False)
    if key == "formats":
        return _choices(value, formats=True)
    if key == "api_url":
        return _api_url(value)
    if key == "progress":
        if not isinstance(value, str):
            raise TypeError
        value = value.strip().lower()
        if value not in PROGRESS_CHOICES:
            raise ValueError("Choose auto, always or never.")
        return value
    raise KeyError(key)


def parse_text(key, text):
    """A value typed as text (environment or ``config set``) in its TOML type, before validation."""
    if key in LIST_KEYS:
        values = text if isinstance(text, (list, tuple)) else [text]
        return [item.strip() for value in values for item in value.split(",") if item.strip()]
    if key == "timeout":
        text = text.strip()
        try:
            return int(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                raise TypeError from None
    return text


def check_key(key, *, source="cli"):
    """Refuse a key that is not one of the known keys, with the specific message for secret-looking keys."""
    if key in KEYS:
        return key
    code = "invalid_argument" if source == "cli" else "config_invalid"
    if isinstance(key, str) and key.strip().lower() in SECRET_KEYS:
        raise SettingsError(SECRET_MESSAGE, code=code, key=shown_key(key), source=source)
    raise SettingsError(f"Unknown setting {shown_key(key)}. Settings: {', '.join(KEYS)}.", code=code,
                        key=shown_key(key), source=source)


def _invalid(key, source, *, detail=None, path=None):
    if source == "cli":
        option = detail or key
        return SettingsError(f"Invalid value for {option}.", code="invalid_argument", key=key, source="cli")
    if source == "env":
        return SettingsError(f"The environment variable {VARIABLES[key]} has an invalid value for {key}.", key=key,
                             source="env", hint=f"Fix or unset {VARIABLES[key]}.")
    where = f" ({path})" if path is not None else ""
    return SettingsError(f"The config file has an invalid value for {key}{where}.", key=key, source="config",
                         hint=f"Run: zlib-cli config unset {key}")


# Reading the file -----------------------------------------------------------------------------------------


def read_bytes(path: Path) -> bytes | None:
    """The file's bytes (links are followed), None when missing; at most MAX_CONFIG_BYTES."""
    try:
        with open(path, "rb") as source:
            data = source.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise SettingsError(f"The config file cannot be read: {path}", code="local_io_error", source="config") from None
    if len(data) > MAX_CONFIG_BYTES:
        raise SettingsError(f"The config file is larger than 64 KiB: {path}", source="config")
    return data


def decode(data: bytes, path) -> dict:
    """TOML 1.0, UTF-8 (BOM accepted). The content is never shown in errors."""
    try:
        return tomllib.loads(data.decode("utf-8-sig"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise SettingsError(f"The config file is not valid TOML: {path}. Its contents are not shown.",
                            source="config", hint="Fix or remove the config file.") from None


def check_document(document: dict, path, *, repair=None) -> tuple[dict, dict]:
    """Strict reading. Return (raw TOML values, normalized values).

    ``repair``: a key whose bad value is dropped instead of refused, so ``config set`` and
    ``config unset`` can fix it.
    """
    raw, values = {}, {}
    for key, value in document.items():
        if key not in KEYS:
            if isinstance(value, dict):
                raise SettingsError(f"The config file has a table ({shown_key(key)}), but it holds one flat table:"
                                    f" {path}", key=shown_key(key), source="config")
            if isinstance(key, str) and key.strip().lower() in SECRET_KEYS:
                raise SettingsError(SECRET_MESSAGE + f" Remove {shown_key(key)} from {path}.", key=shown_key(key),
                                    source="config")
            raise SettingsError(f"The config file has an unknown key {shown_key(key)}: {path}", key=shown_key(key),
                                source="config")
        try:
            if isinstance(value, dict):
                raise TypeError
            values[key] = validate(key, value, source="config")
            raw[key] = value
        except (ValueError, TypeError):
            if key == repair:
                continue
            raise _invalid(key, "config", path=path) from None
    return raw, values


@dataclass(frozen=True)
class ConfigFile:
    path: Path
    source: str
    exists: bool
    raw: dict
    values: dict


def read_config(config_file=None, *, environ=None, missing_ok=False) -> ConfigFile:
    """Read and check the config file. A file named by --config or ZLIB_CONFIG must exist unless ``missing_ok``."""
    path, source = config_file_path(config_file, environ=environ)
    data = read_bytes(path)
    if data is None:
        if source != "default" and not missing_ok:
            raise SettingsError(f"The config file {_source_text(source, path)} does not exist.", source="config",
                                hint="Run: zlib-cli config path")
        return ConfigFile(path, source, False, {}, {})
    raw, values = check_document(decode(data, path), path)
    return ConfigFile(path, source, True, raw, values)


# Resolution ---------------------------------------------------------------------------------------------


def _environment_value(key, environ):
    text = environ.get(VARIABLES[key])
    if text is None or not text.strip():
        return None
    value = parse_text(key, text)
    if key in LIST_KEYS and not value:
        return None
    return value


def load_settings(overrides=None, *, environ=None, config_file=None, use_config=True, missing_ok=False,
                  options=None) -> Settings:
    """Resolve every key: ``overrides`` (None means not given), environment, config file, default.

    ``config_file`` stands in for --config, ``use_config=False`` for --no-config (the file only is
    ignored). ``options`` names the CLI option of each override, for messages and origins.
    Raises SettingsError: ``invalid_argument`` for a bad override, ``config_invalid`` otherwise.
    """
    environ = os.environ if environ is None else environ
    overrides = dict(overrides or {})
    options = dict(options or {})
    for key in overrides:
        check_key(key, source="cli")
    if use_config:
        config = read_config(config_file, environ=environ, missing_ok=missing_ok)
    else:
        path, source = config_file_path(config_file, environ=environ)
        config = ConfigFile(path, source, False, {}, {})
    resolved, origins = {}, {}
    for key in KEYS:
        given = overrides.get(key)
        if given is not None and not (key in LIST_KEYS and not given):
            try:
                resolved[key] = validate(key, given, source="cli")
            except (ValueError, TypeError):
                raise _invalid(key, "cli", detail=options.get(key)) from None
            origins[key] = ("cli", options.get(key, "--" + key.replace("_", "-")))
            continue
        try:
            text = _environment_value(key, environ)
            if text is not None:
                resolved[key] = validate(key, text, source="env")
                origins[key] = ("env", VARIABLES[key])
                continue
        except (ValueError, TypeError):
            raise _invalid(key, "env") from None
        if key in config.values:
            resolved[key] = config.values[key]
            origins[key] = ("config", None)
            continue
        resolved[key] = _DEFAULTS[key]
        origins[key] = ("default", None)
    return Settings(**resolved, config_path=config.path, config_exists=config.exists, config_source=config.source,
                    origins=origins)


# Writing ------------------------------------------------------------------------------------------------


def _string(value: str) -> str:
    out = ['"']
    for character in value:
        code = ord(character)
        if character == "\\":
            out.append("\\\\")
        elif character == '"':
            out.append('\\"')
        elif code < 0x20 or code == 0x7F or code > 0x7E:
            out.append(f"\\u{code:04X}" if code <= 0xFFFF else f"\\U{code:08X}")
        else:
            out.append(character)
    out.append('"')
    return "".join(out)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Config values must be finite.")
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError("Unsupported config value.")


def dumps_config(values: dict) -> str:
    """The file ``config set`` writes: a header, a blank line, then the set keys in KEYS order. ASCII only."""
    lines = [HEADER, ""]
    for key in KEYS:
        if key in values and values[key] is not None:
            lines.append(f"{key} = {_toml_value(values[key])}")
    for key in values:  # Keys outside KEYS are never written by the CLI; keep the writer total for tests.
        if key not in KEYS and values[key] is not None:
            lines.append(f"{_string(key)} = {_toml_value(values[key])}")
    return "\n".join(lines) + "\n"


def toml_form(key, value):
    """A normalized value as written in the file."""
    if value is None:
        return None
    if key in LIST_KEYS:
        return list(value)
    if key == "timeout":
        return int(value) if float(value).is_integer() else float(value)
    if isinstance(value, Path):
        return str(value)
    return value


def has_comments(data: bytes) -> bool:
    """True when the file has a comment other than the header (a # outside a string)."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        return False
    for line in text.splitlines():
        if line.strip() == HEADER:
            continue
        quote = None
        index = 0
        while index < len(line):
            character = line[index]
            if quote is None:
                if character == "#":
                    return True
                if character in "\"'":
                    quote = character
            elif character == "\\" and quote == '"':
                index += 1
            elif character == quote:
                quote = None
            index += 1
    return False


def _generation(data: bytes | None) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def _refuse_link(path: Path) -> bool:
    """True when the path exists. Refuse a link or anything but a regular file."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise SettingsError(f"The config file cannot be read: {path}", code="local_io_error", source="config") from None
    reparse = getattr(info, "st_file_attributes", 0) & 0x400
    if stat.S_ISLNK(info.st_mode) or reparse:
        raise SettingsError("config set refuses a linked config file; edit its target directly.", source="config")
    if not stat.S_ISREG(info.st_mode):
        raise SettingsError(f"The config file is not a regular file: {path}", source="config")
    return True


UNSET = object()


@dataclass(frozen=True)
class ConfigChange:
    key: str
    value: object        # The normalized TOML form written, or None after unset.
    previous: object     # The previous TOML form, or None.
    path: Path
    changed: bool
    dropped_comments: bool


class ConfigStore:
    """Update one key of the config file under a lock; never write secrets, never replace a link."""

    def __init__(self, path: Path, *, source="default", lock_timeout=LOCK_TIMEOUT):
        self.path = Path(path).absolute()
        self.source = source
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.lock_timeout = lock_timeout

    def _read(self) -> bytes | None:
        return read_bytes(self.path)

    def update(self, key, value=UNSET) -> ConfigChange:
        """Set ``key`` to an already validated TOML-form value, or remove it with UNSET."""
        check_key(key, source="cli")
        self._prepare_folder()
        _refuse_link(self.path)
        try:
            with private_lock(self.lock_path, self.lock_timeout) as check_lock:
                return self._update(key, value, check_lock)
        except PrivateLockTimeout:
            raise SettingsError("Another process is updating the config file; try again.", code="config_busy",
                                source="config", hint="Try again in a moment.") from None
        except PrivateLockError:
            raise SettingsError(f"The config file lock could not be used: {self.lock_path}", code="local_io_error",
                                source="config") from None

    def _prepare_folder(self):
        folder = self.path.parent
        if self.source == "default":
            try:
                folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError:
                raise SettingsError(f"The config folder cannot be created: {folder}", code="local_io_error",
                                    source="config") from None
        elif not folder.is_dir():
            raise SettingsError(f"The config file's folder does not exist: {folder}", source="config")

    def _update(self, key, value, check_lock) -> ConfigChange:
        exists = _refuse_link(self.path)
        data = self._read() if exists else None
        generation = _generation(data)
        raw = {}
        if data is not None:
            try:
                raw, _ = check_document(decode(data, self.path), self.path, repair=key)
            except SettingsError:
                raise SettingsError(f"Fix or remove the config file first: {self.path}", source="config") from None
        previous = raw.get(key)
        updated = dict(raw)
        if value is UNSET:
            updated.pop(key, None)
        else:
            updated[key] = value
        if data is not None and updated == raw and key in raw:
            # Same value already written; nothing to do (a repaired key is never equal).
            return ConfigChange(key, raw.get(key), previous, self.path, False, False)
        if value is UNSET and key not in raw and (data is None or _key_present(data, key) is False):
            return ConfigChange(key, None, None, self.path, False, False)
        encoded = dumps_config(updated).encode("ascii")
        self._publish(encoded, generation, check_lock)
        dropped = data is not None and has_comments(data)
        return ConfigChange(key, None if value is UNSET else value, previous, self.path, True, dropped)

    def _publish(self, encoded: bytes, generation: str, check_lock) -> None:
        temporary = None
        try:
            candidate = self.path.parent / (".config-" + secrets.token_hex(16))
            try:
                with create_private_file(candidate) as target:
                    temporary = candidate
                    target.write(encoded)
                    target.flush()
                    os.fsync(target.fileno())
            except (OSError, ValueError):
                raise SettingsError(f"The config file cannot be written: {self.path}", code="local_io_error",
                                    source="config") from None
            check_lock()
            current = self._read() if _refuse_link(self.path) else None
            if _generation(current) != generation:
                raise SettingsError("The config file changed while it was being updated; run the command again.",
                                    code="config_conflict", source="config", hint="Run: zlib-cli config list")
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    os.replace(temporary, self.path)
                    break
                except OSError as error:
                    remaining = deadline - time.monotonic()
                    if os.name != "nt" or getattr(error, "winerror", None) not in (5, 32) or remaining <= 0:
                        raise SettingsError(f"The config file cannot be written: {self.path}",
                                            code="local_io_error", source="config") from None
                    time.sleep(min(0.02, remaining))  # A short read may hold the file open on Windows.
            temporary = None
        except PrivateLockError:
            raise SettingsError(f"The config file lock changed: {self.lock_path}", code="local_io_error",
                                source="config") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def _key_present(data: bytes, key) -> bool:
    """Whether a (possibly invalid) value for ``key`` is in the file, so unset can remove a bad value."""
    try:
        return key in tomllib.loads(data.decode("utf-8-sig"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        return False
