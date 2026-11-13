"""Platform-specific local application data paths."""
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys


def state_directory() -> Path:
    override = os.environ.get("ZLIB_STATE_DIR")
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "win32":
        path = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "zlibrary-api"
    else:
        path = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "zlibrary-api"
    if not path.is_absolute():
        raise ValueError("The local state directory must be an absolute path.")
    return path


def default_config_directory(environ=None, *, platform=None):
    """The settings folder: %APPDATA%\\zlibrary-api, else $XDG_CONFIG_HOME or ~/.config, plus zlibrary-api.

    Only absolute environment values count, so a minimal environment (APPDATA or HOME only) works.
    ``platform`` is for tests: for another platform family the result is a pure path of that family.
    """
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    windows = platform == "win32"
    native = windows == (sys.platform == "win32")
    flavor = Path if native else (PureWindowsPath if windows else PurePosixPath)
    if windows:
        base = _absolute(environ.get("APPDATA"), flavor)
        if base is None:
            home = _absolute(environ.get("USERPROFILE"), flavor) or Path.home()
            base = flavor(home) / "AppData" / "Roaming"
    else:
        base = _absolute(environ.get("XDG_CONFIG_HOME"), flavor)
        if base is None:
            home = _absolute(environ.get("HOME"), flavor) or Path.home()
            base = flavor(home) / ".config"
    return base / "zlibrary-api"


def config_directory() -> Path:
    """The settings folder for this process; the test suite replaces it to stay away from real settings."""
    return default_config_directory()


def _absolute(value, flavor):
    if not isinstance(value, str) or not value.strip():
        return None
    path = flavor(value.strip())
    return path if path.is_absolute() else None
