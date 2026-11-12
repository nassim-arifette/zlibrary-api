import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zlib_connector import session_lock
from zlib_connector.session_lock import PrivateLockError, private_lock


@unittest.skipUnless(os.name == "nt", "The C runtime reports this sharing conflict only on Windows.")
class WindowsLockCreationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "session.json.lock"

    def test_a_brief_access_refusal_while_another_creator_holds_the_file_is_retried(self):
        real_open, calls = os.open, []

        def busy_once(path, flags, *args):
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError(13, "Permission denied")  # EACCES from the C runtime, no winerror
            return real_open(path, flags, *args)

        with patch.object(session_lock.os, "open", side_effect=busy_once):
            with private_lock(self.path, 2):
                pass
        self.assertEqual(2, len(calls))

    def test_a_refusal_that_persists_is_a_lock_error_not_a_busy_writer(self):
        with patch.object(session_lock.os, "open", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(PrivateLockError) as raised:
                with private_lock(self.path, 0.1):
                    pass
        self.assertNotIsInstance(raised.exception, session_lock.PrivateLockTimeout)


if __name__ == "__main__":
    unittest.main()
