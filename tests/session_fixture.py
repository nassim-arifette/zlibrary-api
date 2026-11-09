"""Keep every CLI test away from sessions stored by a real user."""

from functools import wraps
import os
from pathlib import Path
import tempfile
from unittest.mock import patch


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
