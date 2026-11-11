import errno
import io
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from zlib_connector.book_files import _file_stem, save_book, validate_book, validate_book_stream
from zlib_connector.errors import DownloadError, NetworkError


def epub(marker=b"application/epub+zip", *, compression=zipfile.ZIP_STORED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        archive.writestr("mimetype", marker)
    return output.getvalue()


class TrackedStream(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.largest_read = 0
        self.bytes_read = 0

    def read(self, size=-1):
        actual = len(self.getbuffer()) - self.tell() if size < 0 else size
        self.largest_read = max(self.largest_read, actual)
        result = super().read(size)
        self.bytes_read += len(result)
        return result


class BookValidationTests(unittest.TestCase):
    def test_compatible_valid_pdf_djvu_and_epub_signatures(self):
        for content, extension in ((b"%PDF-", "pdf"), (b"AT&TFORM\0\0\0\0DJVU", "djvu"),
                                   (b"AT&TFORM\0\0\0\0DJVM", "djvu"), (epub(), "epub"),
                                   (epub(b" application/epub+zip\n", compression=zipfile.ZIP_DEFLATED), "epub")):
            with self.subTest(extension=extension):
                validate_book(content, extension)
                stream = io.BytesIO(content)
                validate_book_stream(stream, extension)
                self.assertFalse(stream.closed)

    def test_rejects_html_unknown_formats_truncation_bad_crc_and_wrong_mimetype(self):
        corrupted = bytearray(epub())
        corrupted[corrupted.index(b"application/epub+zip")] = ord("X")
        for content, extension in ((b"<html>login</html>", "pdf"), (b"%PDF", "pdf"),
                                   (b"AT&TFORM\0\0", "djvu"), (b"PKnot-an-epub", "epub"),
                                   (epub()[:-1], "epub"), (epub(b"text/html"), "epub"),
                                   (corrupted, "epub"), (b"%PDF-", "unknown")):
            with self.subTest(extension=extension, length=len(content)), self.assertRaises(DownloadError):
                validate_book(content, extension)

    def test_reads_only_headers_for_large_pdf_and_djvu(self):
        for prefix, extension, bound in ((b"%PDF-", "pdf", 5), (b"AT&TFORM\0\0\0\0DJVU", "djvu", 16)):
            stream = TrackedStream(prefix + b"x" * (2 * 1024 * 1024))
            validate_book_stream(stream, extension)
            self.assertEqual(stream.bytes_read, bound)

    def test_large_epub_payload_is_not_read_or_decompressed(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr("large-resource", b"x" * (2 * 1024 * 1024))
        stream = TrackedStream(output.getvalue())
        validate_book_stream(stream, "epub")
        self.assertLessEqual(stream.largest_read, 65557)
        self.assertLess(stream.bytes_read, 140_000)

    def test_declared_huge_directory_or_entry_count_rejected_before_zipfile(self):
        original = epub()
        for offset, format, value in ((12, "<I", 0x7FFFFFFF), (10, "<H", 10001),
                                     (16, "<I", 0xFFFFFFFF), (4, "<H", 1)):
            data = bytearray(original)
            end = data.rfind(b"PK\x05\x06")
            struct.pack_into(format, data, end + offset, value)
            with self.subTest(offset=offset):
                with patch("zlib_connector.book_files.zipfile.ZipFile", side_effect=AssertionError("Unbounded metadata reached ZipFile")):
                    with self.assertRaises(DownloadError):
                        validate_book(data, "epub")

    def test_actual_directory_entry_count_cannot_bypass_false_eocd_count(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr("other", "synthetic")
        data = bytearray(output.getvalue())
        end = data.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", data, end + 8, 1, 1)
        with patch("zlib_connector.book_files.zipfile.ZipFile", side_effect=AssertionError("False count reached ZipFile")):
            with self.assertRaises(DownloadError):
                validate_book(data, "epub")

    def test_local_zip64_header_is_compatible_with_bounded_ordinary_directory(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            with archive.open("mimetype", "w", force_zip64=True) as marker:
                marker.write(b"application/epub+zip")
        validate_book(output.getvalue(), "epub")

    def test_zip64_locator_hidden_in_entry_comment_cannot_override_checked_sizes(self):
        output = io.BytesIO()
        marker = zipfile.ZipInfo("mimetype")
        marker.comment = (struct.pack("<4sQ2H2I4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, 1, 1, 0x7FFFFFFF, 0)
                          + struct.pack("<4sIQI", b"PK\x06\x07", 0, 0, 1))
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr(marker, "application/epub+zip")
        with patch("zlib_connector.book_files.zipfile.ZipFile", side_effect=AssertionError("ZIP64 override reached ZipFile")):
            with self.assertRaises(DownloadError):
                validate_book(output.getvalue(), "epub")

    def test_mimetype_bombs_and_lzma_are_rejected_before_inflation(self):
        cases = (epub(b"x" * 1_000_000, compression=zipfile.ZIP_DEFLATED), epub(compression=zipfile.ZIP_LZMA))
        for data in cases:
            with self.subTest(length=len(data)), patch.object(zipfile.ZipFile, "read", side_effect=AssertionError("Unsafe mimetype was inflated")):
                with self.assertRaises(DownloadError):
                    validate_book(data, "epub")


class BookPublicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name).resolve()

    def test_temp_exists_before_writer_and_published_file_is_complete(self):
        calls = []
        def writer(stream):
            calls.append(stream.name)
            self.assertTrue(Path(stream.name).is_file())
            self.assertEqual(list(self.folder.iterdir()), [Path(stream.name)])
            stream.write(b"%PDF-synthetic")
        result = save_book('Example: / title', "pdf", self.folder, writer)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.read_bytes(), b"%PDF-synthetic")
        self.assertRegex(result.name, r"^Example_ _ title-[0-9a-f]{12}\.pdf$")
        self.assertEqual(list(self.folder.iterdir()), [result])

    def test_disk_failure_happens_before_writer_and_has_neutral_message(self):
        writer = Mock()
        with patch("zlib_connector.book_files.tempfile.NamedTemporaryFile", side_effect=OSError("private-path")):
            with self.assertRaises(DownloadError) as caught:
                save_book("Example", "pdf", self.folder, writer)
        writer.assert_not_called()
        self.assertNotIn("private-path", str(caught.exception))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_callback_errors_and_interruptions_are_not_masked_and_part_is_removed(self):
        for error in (NetworkError("synthetic"), DownloadError("synthetic"), OSError("callback"), KeyboardInterrupt()):
            def writer(stream):
                stream.write(b"partial")
                raise error
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)) as caught:
                save_book("Example", "pdf", self.folder, writer)
            self.assertIs(caught.exception, error)
            self.assertEqual(list(self.folder.iterdir()), [])

    def test_invalid_book_and_fsync_failure_leave_no_part_or_published_file(self):
        with self.assertRaises(DownloadError):
            save_book("Example", "pdf", self.folder, lambda stream: stream.write(b"<html>"))
        with patch("zlib_connector.book_files.os.fsync", side_effect=OSError("private-path")):
            with self.assertRaises(DownloadError) as caught:
                save_book("Example", "pdf", self.folder, lambda stream: stream.write(b"%PDF-synthetic"))
        self.assertNotIn("private-path", str(caught.exception))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_publication_collision_never_overwrites_and_never_retries_writer(self):
        existing = self.folder / "Example-aaaaaaaaaaaa.pdf"
        existing.write_bytes(b"existing")
        writer = Mock(side_effect=lambda stream: stream.write(b"%PDF-new"))
        with patch("zlib_connector.book_files.uuid.uuid4", return_value=Mock(hex="a" * 32)):
            with self.assertRaises(DownloadError):
                save_book("Example", "pdf", self.folder, writer)
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(existing.read_bytes(), b"existing")
        self.assertEqual(list(self.folder.iterdir()), [existing])

    def test_long_titles_fit_a_name_limit_in_bytes_by_whole_characters(self):
        suffix = "-0123456789ab.djvu"
        han, emoji, accent = "\u6f22", "\U0001f600", "\u00e9"
        for title, kept in ((han * 90, 79), (emoji * 90, 59), (accent * 90, 90), ("A" * 90, 90)):
            with self.subTest(title=title[:1]):
                stem = _file_stem(title, suffix, 255)
                self.assertEqual(stem, title[:kept])
                self.assertLessEqual(len((stem + suffix).encode("utf-8")), 255)
        hindi = "\u0939\u093f\u0928\u094d\u0926\u0940 \u092a\u0941\u0938\u094d\u0924\u0915 " * 10
        stem = _file_stem(hindi, suffix, 255)
        self.assertTrue(hindi.startswith(stem) and len(stem) < 90)
        self.assertLessEqual(len((stem + suffix).encode("utf-8")), 255)
        self.assertEqual(_file_stem(han * 90, suffix, None), han * 90)  # Windows limits characters, not bytes.
        self.assertEqual(_file_stem(han * 3 + " ." + han * 3, suffix, len(suffix) + 11), han * 3)
        self.assertEqual(_file_stem(han, suffix, len(suffix) + 2), "book")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux file systems limit names to 255 bytes")
    def test_a_long_cjk_title_is_saved_under_a_name_the_folder_accepts(self):
        result = save_book("\u6f22" * 90, "pdf", self.folder, lambda stream: stream.write(b"%PDF-synthetic"))
        self.assertEqual(result.read_bytes(), b"%PDF-synthetic")
        self.assertLessEqual(len(os.fsencode(result.name)), 255)

    def test_a_name_the_folder_still_refuses_is_saved_under_a_short_one(self):
        real, names = os.link, []

        def link(source, target):
            names.append(Path(target).name)
            if len(names) == 1:
                raise OSError(errno.ENAMETOOLONG, "synthetic")
            return real(source, target)

        with patch("zlib_connector.book_files.os.link", side_effect=link):
            result = save_book("Example", "pdf", self.folder, lambda stream: stream.write(b"%PDF-synthetic"))
        self.assertEqual(len(names), 2)
        self.assertRegex(result.name, r"^book-[0-9a-f]{12}\.pdf$")
        self.assertEqual(names[0][len("Example"):], names[1][len("book"):])
        self.assertEqual(list(self.folder.iterdir()), [result])

    def test_cancellation_before_writer_and_before_publication_never_saves(self):
        from zlib_connector.transfer import CancellationToken, TransferCancelled
        before = CancellationToken()
        before.cancel()
        writer = Mock()
        with self.assertRaises(TransferCancelled):
            save_book("Example", "pdf", self.folder, writer, cancel=before)
        writer.assert_not_called()
        before_publish = CancellationToken()
        def fsync_and_cancel(_):
            before_publish.cancel()
        with patch("zlib_connector.book_files.os.fsync", side_effect=fsync_and_cancel):
            with self.assertRaises(TransferCancelled):
                save_book("Example", "pdf", self.folder, lambda stream: stream.write(b"%PDF-new"), cancel=before_publish)
        self.assertEqual(list(self.folder.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
