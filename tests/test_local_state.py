import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from zlib_connector.local_state import state_directory


class LocalStateTests(unittest.TestCase):
    def test_explicit_state_directory_overrides_platform_defaults(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ZLIB_STATE_DIR": directory}):
            self.assertEqual(state_directory(), Path(directory))
        with patch.dict(os.environ, {"ZLIB_STATE_DIR": "relative/path"}), self.assertRaises(ValueError):
            state_directory()
