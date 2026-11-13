"""Keep every CLI test away from sessions, settings and credentials stored by a real user."""

import atexit
from functools import wraps
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

from zlib_connector import credentials, local_state


def _isolate_settings():
    """For the whole test process: no real config folder, no legacy credentials file, no ZLIB_* settings.

    Test discovery imports every module before running any test, so one import isolates all of them.
    """
    root = Path(tempfile.mkdtemp(prefix="zlib-settings-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    patch.object(local_state, "config_directory", lambda: root / "config").start()
    patch.object(credentials, "DEFAULT_FILE", root / "legacy" / "credentials.local.toml").start()
    for name in ("ZLIB_CONFIG", "ZLIB_DOMAIN", "ZLIB_TIMEOUT", "ZLIB_DOWNLOAD_DIR", "ZLIB_LANGUAGES",
                 "ZLIB_FORMATS", "ZLIB_API_URL", "ZLIB_CREDENTIALS", "XDG_CONFIG_HOME"):
        os.environ.pop(name, None)
    return root


SETTINGS_ROOT = _isolate_settings()


def isolated_sessions(test_class):
    """Wrap existing setUp without requiring a cooperative superclass call."""
    original_setup = test_class.setUp

    @wraps(original_setup)
    def set_up(test):
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        state = Path(directory.name) / "state"
        environment = patch.dict(os.environ, {"ZLIB_STATE_DIR": str(state)})
        environment.start()
        test.addCleanup(environment.stop)
        test._isolated_session_state = state
        original_setup(test)

    test_class.setUp = set_up
    return test_class
