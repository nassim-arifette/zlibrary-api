import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from zlib_connector.private_files import create_private_file, protect_private_file, require_private_file


class _TemporaryFileTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.path = self.folder / "synthetic-token"
        self.path.touch()


class PrivateFileTests(_TemporaryFileTestCase):
    def test_exclusive_creation_is_private_while_open_and_does_not_overwrite(self):
        path = self.folder / "new-private-file"
        with create_private_file(path) as target:
            require_private_file(path)
            self.assertFalse(os.get_inheritable(target.fileno()))
            target.write(b"synthetic-test-data")
            target.flush()
            self.assertEqual(path.read_bytes(), b"synthetic-test-data")
        with self.assertRaises(FileExistsError) as caught:
            create_private_file(path)
        self.assertNotIn(str(path), str(caught.exception))
        self.assertEqual(path.read_bytes(), b"synthetic-test-data")
        require_private_file(path)

    def test_creation_failure_closes_the_file_descriptor(self):
        real_fdopen = os.fdopen
        descriptors = []
        def fail_wrapping(descriptor, mode):
            descriptors.append(descriptor)
            raise OSError("synthetic-sensitive-detail")
        with patch("zlib_connector.private_files.os.fdopen", side_effect=fail_wrapping):
            with self.assertRaises(ValueError) as caught:
                create_private_file(self.folder / "unwrapped")
        self.assertNotIn("synthetic-sensitive-detail", str(caught.exception))
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
        self.assertIs(os.fdopen, real_fdopen)

    def test_protection_before_writing_keeps_file_readable_and_data_unchanged(self):
        with self.path.open("w", encoding="ascii") as target:
            protect_private_file(self.path)
            require_private_file(self.path)
            self.assertEqual(self.path.stat().st_size, 0)
            target.write("synthetic-test-data")
        require_private_file(self.path)
        self.assertEqual(self.path.read_text(encoding="ascii"), "synthetic-test-data")

    def test_missing_files_directories_and_symbolic_links_are_rejected(self):
        for path in (self.folder / "missing-private-name", self.folder):
            for operation in (require_private_file, protect_private_file):
                with self.subTest(operation=operation.__name__), self.assertRaises(ValueError) as caught:
                    operation(path)
                self.assertNotIn(str(path), str(caught.exception))
        link = self.folder / "link"
        try:
            link.symlink_to(self.path)
        except (OSError, NotImplementedError):
            return
        for operation in (require_private_file, protect_private_file):
            with self.assertRaises(ValueError):
                operation(link)

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits require POSIX.")
    def test_posix_mode_bits_are_restricted_and_broad_modes_are_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            require_private_file(self.path)
        protect_private_file(self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        require_private_file(self.path)


@unittest.skipUnless(os.name == "nt", "Native ACL tests require Windows.")
class WindowsPrivateFileTests(_TemporaryFileTestCase):
    def setUp(self):
        super().setUp()
        from zlib_connector.private_files import _WindowsSecurity
        self.security = _WindowsSecurity()
        self.user = self.security.current_user()

    def set_acl(self, entries):
        self.security._apply_dacl(self.path, f"D:P(A;;FA;;;{self.user})(A;;FA;;;SY){entries}")

    def test_protection_removes_everyone_access_and_prevents_parent_inheritance(self):
        self.security._apply_dacl(self.folder, f"D:P(A;OICI;FA;;;{self.user})(A;OICI;FA;;;SY)(A;OICI;GR;;;WD)")
        shared_file = self.folder / "shared-file"
        shared_file.touch()
        with self.assertRaises(ValueError):
            require_private_file(shared_file)
        protect_private_file(shared_file)
        self.security.require(shared_file, self.user, protected=True)
        shared_file.write_text("synthetic", encoding="ascii")
        self.security._apply_dacl(self.folder, f"D:P(A;OICI;FA;;;{self.user})(A;OICI;FA;;;SY)(A;OICI;GA;;;WD)")
        require_private_file(shared_file)
        self.assertEqual(shared_file.read_text(encoding="ascii"), "synthetic")

    def test_open_named_temporary_file_can_be_protected_before_write_and_published_by_hardlink(self):
        self.security._apply_dacl(self.folder, f"D:P(A;OICI;FA;;;{self.user})(A;OICI;FA;;;SY)(A;OICI;GR;;;WD)")
        with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", dir=self.folder, delete=False) as target:
            temporary = Path(target.name)
            protect_private_file(temporary)
            require_private_file(temporary)
            self.assertEqual(temporary.stat().st_size, 0)
            target.write("synthetic-token-value")
            target.flush()
            os.fsync(target.fileno())
        published = self.folder / "published-token"
        os.link(temporary, published)
        temporary.unlink()
        self.security.require(published, self.user, protected=True)
        require_private_file(published)
        self.assertEqual(published.read_text(encoding="ascii"), "synthetic-token-value")

    def test_create_file_supplies_private_dacl_at_creation_in_a_shared_folder(self):
        self.security._apply_dacl(self.folder, f"D:P(A;OICI;FA;;;{self.user})(A;OICI;FA;;;SY)(A;OICI;GR;;;WD)")
        path = self.folder / "created-private"
        with patch("zlib_connector.private_files._WindowsSecurity._apply_dacl",
                   side_effect=AssertionError("Creation must not repair an inherited DACL")):
            with create_private_file(path) as target:
                self.security.require(path, self.user, protected=True)
                self.assertEqual(path.stat().st_size, 0)
                target.write(b"synthetic-data")
                target.flush()
                os.fsync(target.fileno())
        published = self.folder / "published"
        os.link(path, published)
        path.unlink()
        require_private_file(published)
        self.assertEqual(published.read_bytes(), b"synthetic-data")

    def test_failed_handle_transfer_closes_native_handle_once(self):
        import msvcrt
        from zlib_connector.private_files import _WindowsSecurity
        security = _WindowsSecurity()
        real_close = security._close
        with patch.object(security, "_close", wraps=real_close) as close:
            with patch("zlib_connector.private_files._WindowsSecurity", return_value=security):
                with patch.object(msvcrt, "open_osfhandle", side_effect=OSError("synthetic-detail")):
                    with self.assertRaises(ValueError):
                        create_private_file(self.folder / "untransferred")
            # One process token handle, then the untransferred file handle.
            self.assertEqual(close.call_count, 2)

    def test_everyone_authenticated_users_and_builtin_users_are_not_private(self):
        for alias in ("WD", "AU", "BU"):
            with self.subTest(alias=alias):
                self.set_acl(f"(A;;GR;;;{alias})")
                with self.assertRaises(ValueError) as caught:
                    require_private_file(self.path)
                self.assertNotIn(self.user, str(caught.exception))
                self.assertNotIn(str(self.path), str(caught.exception))
                protect_private_file(self.path)
                require_private_file(self.path)

    def test_administrators_are_allowed_but_a_deny_does_not_cancel_a_broad_allow(self):
        self.set_acl("(A;;FA;;;BA)")
        require_private_file(self.path)
        self.security._apply_dacl(self.path,
            f"D:P(D;;0x00000001;;;WD)(A;;FA;;;{self.user})(A;;FA;;;SY)(A;;GR;;;WD)")
        with self.assertRaises(ValueError):
            require_private_file(self.path)
        protect_private_file(self.path)

    def test_null_and_empty_dacls_are_rejected(self):
        from zlib_connector.private_files import _DACL_INFORMATION, _FILE_OBJECT, _PROTECTED_DACL_INFORMATION
        result = self.security._set_security(str(self.path), _FILE_OBJECT,
            _DACL_INFORMATION | _PROTECTED_DACL_INFORMATION, None, None, None, None)
        self.assertEqual(result, 0)
        with self.assertRaises(ValueError):
            require_private_file(self.path)
        protect_private_file(self.path)
        self.security._apply_dacl(self.path, "D:P")
        try:
            with self.assertRaises(ValueError):
                require_private_file(self.path)
        finally:
            protect_private_file(self.path)

    def test_native_failure_is_neutral_and_cannot_claim_success(self):
        with patch("zlib_connector.private_files._WindowsSecurity", side_effect=OSError("private-path S-1-private")):
            for operation in (protect_private_file, require_private_file):
                with self.subTest(operation=operation.__name__), self.assertRaises(ValueError) as caught:
                    operation(self.path)
                self.assertNotIn("private-path", str(caught.exception))
                self.assertNotIn("S-1-private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
