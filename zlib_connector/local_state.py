"""Platform-specific local application data paths."""
import os
from pathlib import Path
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
