from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from threading import Barrier, Event
import unittest
from unittest.mock import patch

from zlib_connector.private_files import create_private_file, require_private_file
from zlib_connector.session_data import SavedSession, SessionCookie, canonical_origin
from zlib_connector.session_lock import private_lock
from zlib_connector.sessions import MAX_SESSION_BYTES, SessionConflictError, SessionError, SessionLockTimeoutError, SessionStore


def synthetic_session(*, origin="https://example.org/", expires=None, key="synthetic-private-key"):
    return SavedSession(origin, (SessionCookie("remix_userid", "123456789"),
                                 SessionCookie("remix_userkey", key, "/eapi", expires)), 1700000000)


class SessionDataTests(unittest.TestCase):
    def test_canonical_origins_are_exact_https_origins(self):
        self.assertEqual(canonical_origin("https://EXAMPLE.org:443"), "https://example.org/")
        self.assertEqual(canonical_origin("https://bücher.example/"), "https://xn--bcher-kva.example/")
        self.assertEqual(canonical_origin("https://[2001:db8::1]:443/"), "https://[2001:db8::1]/")
        for origin in ("http://example.org", "https://user:private@example.org", "https://example.org/path",
                       "https://example.org/?", "https://example.org/#", "https://example.org:8443",
                       " https://example.org", "https://example.org\r\n", "https://example.org:",
                       "https://example.org./", "https://bad_host.example/", "https://example.org\\other/"):
            with self.subTest(origin=origin), self.assertRaises(ValueError) as caught:
                canonical_origin(origin)
            self.assertNotIn("user:private", str(caught.exception))

    def test_cookie_validation_is_bounded_and_does_not_echo_values(self):
        for value in ("", "private;data", "private,data", 'private"data', "private\\data", "private data",
                      "private\nheader", "clé", "x" * 4097):
            with self.subTest(length=len(value)), self.assertRaises(ValueError) as caught:
                SessionCookie("remix_userkey", value)
            self.assertNotIn("private", str(caught.exception))
        for value in ("0", "000", "userid", "1" * 21):
            with self.assertRaises(ValueError):
                SessionCookie("remix_userid", value)
        for path in ("relative", "/bad;path", "/bad\npath", "/" + "x" * 2048):
            with self.assertRaises(ValueError):
                SessionCookie("remix_userkey", "synthetic", path)
        for expiry in (True, -1, 1.2, float("inf")):
            with self.assertRaises(ValueError):
                SessionCookie("remix_userkey", "synthetic", expires=expiry)

    def test_saved_session_requires_one_authentication_pair_and_safe_timestamp(self):
        session = synthetic_session()
        for cookies in (session.cookies[:1], session.cookies + session.cookies[:1],
                        (session.cookies[0], session.cookies[0]), list(session.cookies)):
            with self.assertRaises(ValueError):
                SavedSession(session.origin, cookies, session.saved_at)
        for timestamp in (True, -1, 1.5, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                replace(session, saved_at=timestamp)

    def test_representations_and_status_hide_cookie_values_and_expiry_is_earliest(self):
        session = synthetic_session(expires=200)
        with patch("zlib_connector.session_data.time.time", return_value=199):
            self.assertFalse(session.expired)
        with patch("zlib_connector.session_data.time.time", return_value=200):
            self.assertTrue(session.expired)
            self.assertEqual(session.status(), {"state": "expired", "domain": "https://example.org/",
                                               "saved_at": 1700000000, "expires_at": 200, "verified": False})
        second = replace(session, cookies=(replace(session.cookies[0], expires=100), session.cookies[1]))
        self.assertEqual(second.expires_at, 100)
        for representation in (repr(session), repr(session.cookies), json.dumps(session.status())):
            self.assertNotIn("synthetic-private-key", representation)
            self.assertNotIn("123456789", representation)


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.path = self.folder / "session.json"
        self.store = SessionStore(self.path)
        self.default_folder = self.folder / "default" / "state"
        self.default_patch = patch("zlib_connector.sessions.state_directory", return_value=self.default_folder)
        self.default_patch.start()
        self.addCleanup(self.default_patch.stop)

    def write_raw(self, data):
        self.path.unlink(missing_ok=True)
        with create_private_file(self.path) as target:
            target.write(data)

    def test_missing_reads_create_neither_directory_nor_lock(self):
        default = SessionStore()
        snapshot = default.read()
        self.assertIsNone(snapshot.generation)
        self.assertIsNone(snapshot.session)
        self.assertEqual(snapshot.status(), {"state": "empty", "domain": None, "saved_at": None,
                                            "expires_at": None, "verified": False})
        self.assertFalse(self.default_folder.parent.exists())
        self.assertFalse(self.store.lock_path.exists())

    def test_private_versioned_roundtrip_and_safe_snapshot_projection(self):
        session = synthetic_session()
        stored = self.store.save(session)
        self.assertEqual(self.store.read(), stored)
        self.assertEqual(stored.session, session)
        self.assertRegex(stored.generation, r"^[0-9a-f]{32}$")
        require_private_file(self.path)
        require_private_file(self.store.lock_path)
        self.assertEqual(json.loads(self.path.read_bytes())["version"], 1)
        self.assertNotIn("synthetic-private-key", repr(stored))
        self.assertNotIn("123456789", json.dumps(stored.status()))
        self.assertEqual(stored.status()["state"], "stored")

    def test_default_parent_is_created_only_on_write_and_explicit_parent_is_not_created(self):
        default = SessionStore()
        default.save(synthetic_session())
        require_private_file(default.path)
        if os.name != "nt":
            self.assertEqual(default.path.parent.stat().st_mode & 0o777, 0o700)
        explicit = SessionStore(self.folder / "not-created" / "session.json")
        with self.assertRaises(SessionError):
            explicit.save(synthetic_session())
        self.assertFalse(explicit.path.parent.exists())

    def test_compare_and_swap_rejects_stale_login_and_stale_logout(self):
        first = self.store.save(synthetic_session())
        second = self.store.save(synthetic_session(key="new-synthetic-key"), expected_generation=first.generation)
        for expected in (None, first.generation):
            with self.subTest(expected=expected), self.assertRaises(SessionConflictError):
                self.store.save(synthetic_session(), expected_generation=expected)
        with self.assertRaises(SessionConflictError):
            self.store.clear(expected_generation=first.generation)
        self.assertEqual(self.store.read(), second)

    def test_logout_tombstone_even_when_absent_prevents_earlier_login(self):
        before = self.store.read()
        cleared = self.store.clear(expected_generation=before.generation)
        self.assertIsNone(cleared.session)
        self.assertIsNotNone(cleared.generation)
        self.assertEqual(cleared.status()["state"], "empty")
        self.assertEqual(self.store.read(), cleared)
        with self.assertRaises(SessionConflictError):
            self.store.save(synthetic_session(), expected_generation=before.generation)
        again = self.store.clear()
        self.assertNotEqual(again.generation, cleared.generation)
        self.assertIsNone(json.loads(self.path.read_bytes())["session"])

    def test_expired_session_is_retained_for_local_status_without_verification(self):
        snapshot = self.store.save(synthetic_session(expires=0))
        self.assertEqual(snapshot.status()["state"], "expired")
        self.assertFalse(snapshot.status()["verified"])
        self.assertEqual(self.store.read().session, snapshot.session)

    def test_malformed_duplicate_unknown_and_oversized_documents_fail_without_echo(self):
        valid = self.store.save(synthetic_session())
        original = json.loads(self.path.read_bytes())
        cases = [b"private-invalid-json", b"[]", b'{"version":1,"version":1}',
                 b'{"private":NaN}', b"\xff", b"x" * (MAX_SESSION_BYTES + 1)]
        for changes in ({"version": 2}, {"version": True}, {"generation": None},
                        {"generation": "private-invalid"}, {"extra": "private-value"}):
            cases.append(json.dumps({**original, **changes}).encode())
        bad_session = {**original["session"], "cookies": original["session"]["cookies"][:1]}
        cases.append(json.dumps({**original, "session": bad_session}).encode())
        for data in cases:
            with self.subTest(length=len(data)):
                self.write_raw(data)
                with self.assertRaises(SessionError) as caught:
                    self.store.read()
                self.assertNotIn("private-invalid", str(caught.exception))
                self.assertNotIn("private-value", str(caught.exception))
                self.assertNotIn(str(self.path), str(caught.exception))
        self.assertIsNotNone(valid.generation)

    def test_replacement_between_permission_check_and_open_is_rejected(self):
        self.store.save(synthetic_session())
        alternate = self.folder / "alternate"
        with create_private_file(alternate) as target:
            target.write(self.path.read_bytes())
        actual_check = require_private_file
        def replace_after_check(path):
            actual_check(path)
            os.replace(alternate, path)
        with patch("zlib_connector.sessions.require_private_file", side_effect=replace_after_check):
            with self.assertRaises(SessionError):
                self.store.read()

    def test_readers_do_not_take_the_write_lock_and_can_finish_during_atomic_replacement(self):
        from zlib_connector.sessions import _reader
        first = self.store.save(synthetic_session())
        with private_lock(self.store.lock_path, 1):
            self.assertEqual(self.store.read(), first)
        actual_replace = os.replace
        attempted = Event()
        def observe_replace(*args):
            try:
                return actual_replace(*args)
            finally:
                attempted.set()
        with patch("zlib_connector.sessions.os.replace", side_effect=observe_replace):
            with ThreadPoolExecutor(max_workers=1) as executor:
                with _reader(self.path) as reader:
                    pending = executor.submit(self.store.save, synthetic_session(key="second-synthetic-key"),
                                              expected_generation=first.generation)
                    self.assertTrue(attempted.wait(timeout=2))
                    self.assertEqual(json.loads(reader.read())["generation"], first.generation)
                second = pending.result(timeout=3)
        self.assertEqual(self.store.read(), second)

    def test_concurrent_writers_allow_only_one_generation_winner(self):
        start = Barrier(6)
        def write(_):
            start.wait(timeout=5)
            try:
                return SessionStore(self.path).save(synthetic_session())
            except SessionConflictError:
                return None
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(write, range(6)))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.store.read(), winners[0])
        self.assertEqual(self.store.lock_path.stat().st_size, 0)

    @unittest.skipUnless(os.name == "nt", "This contention case is specific to Windows replacement semantics.")
    def test_windows_busy_reader_wait_is_bounded_and_failed_publication_preserves_previous_session(self):
        from zlib_connector.sessions import _reader
        before = self.store.save(synthetic_session())
        previous_bytes = self.path.read_bytes()
        lock_identity = self.store.lock_path.stat().st_ino
        busy_store = SessionStore(self.path, lock_timeout=0.05)
        with _reader(self.path):
            start = time.monotonic()
            with self.assertRaises(SessionError):
                busy_store.save(synthetic_session(key="second-synthetic-key"), expected_generation=before.generation)
            self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual(self.path.read_bytes(), previous_bytes)
        self.assertEqual(self.store.read(), before)
        self.assertEqual(list(self.folder.glob(".session-*")), [])
        self.assertEqual(self.store.lock_path.stat().st_ino, lock_identity)
        self.store.clear(expected_generation=before.generation)

    def test_lock_timeout_is_bounded_and_release_allows_a_later_write(self):
        self.store.clear()
        with private_lock(self.store.lock_path, 1):
            with self.assertRaises(SessionLockTimeoutError):
                SessionStore(self.path, lock_timeout=0.05).clear()
        self.assertEqual(self.store.clear().status()["state"], "empty")

    def test_native_lock_excludes_another_process_and_is_released_after_error(self):
        self.store.clear()
        script = (
            "import sys\nfrom zlib_connector.sessions import SessionStore,SessionLockTimeoutError\n"
            "try:\n SessionStore(sys.argv[1],lock_timeout=0.08).clear()\n"
            "except SessionLockTimeoutError:\n sys.exit(7)\n"
        )
        with private_lock(self.store.lock_path, 1):
            child = subprocess.run([sys.executable, "-c", script, str(self.path)], capture_output=True, timeout=10)
            self.assertEqual(child.returncode, 7)
            self.assertEqual(child.stdout, b"")
            self.assertEqual(child.stderr, b"")
        child = subprocess.run([sys.executable, "-c", script, str(self.path)], capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 0)

    def test_failed_publication_keeps_previous_session_and_cleans_only_owned_temp(self):
        before = self.store.save(synthetic_session())
        owned = self.folder / ".session-unrelated"
        owned.write_bytes(b"unrelated synthetic data")
        for failure in (OSError("private-path-cookie"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                with patch("zlib_connector.sessions.os.replace", side_effect=failure):
                    with self.assertRaises(SessionError if isinstance(failure, OSError) else KeyboardInterrupt) as caught:
                        self.store.save(synthetic_session(key="new-synthetic-key"), expected_generation=before.generation)
                self.assertNotIn("private-path-cookie", str(caught.exception))
                self.assertEqual(self.store.read(), before)
                self.assertEqual(list(self.folder.glob(".session-*")), [owned])
        self.assertEqual(owned.read_bytes(), b"unrelated synthetic data")
        self.store.clear(expected_generation=before.generation)

    def test_nonregular_session_or_lock_is_rejected_without_repair(self):
        self.path.mkdir()
        with self.assertRaises(SessionError):
            self.store.read()
        self.path.rmdir()
        self.store.lock_path.mkdir()
        with self.assertRaises(SessionError):
            self.store.clear()
        self.assertTrue(self.store.lock_path.is_dir())

    def test_broad_permissions_on_existing_session_or_lock_are_rejected_without_repair(self):
        self.store.save(synthetic_session())
        def broaden(path):
            if os.name == "nt":
                from zlib_connector.private_files import _WindowsSecurity
                security = _WindowsSecurity()
                security._apply_dacl(path, f"D:P(A;;FA;;;{security.current_user()})(A;;FA;;;SY)(A;;GR;;;WD)")
            else:
                path.chmod(0o644)
        broaden(self.path)
        with self.assertRaises(SessionError):
            self.store.read()
        with self.assertRaises(SessionError):
            self.store.clear()
        with self.assertRaises(ValueError):
            require_private_file(self.path)
        self.path.unlink()
        broaden(self.store.lock_path)
        with self.assertRaises(SessionError):
            self.store.clear()
        with self.assertRaises(ValueError):
            require_private_file(self.store.lock_path)


if __name__ == "__main__":
    unittest.main()
