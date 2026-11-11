"""Bounded book validation and atomic publication, independent of the API client."""
from contextlib import contextmanager
import errno
import io
import os
from pathlib import Path
import re
import struct
import tempfile
from typing import BinaryIO, Callable, TYPE_CHECKING
import uuid
import zipfile
import zlib

from .errors import DownloadError

if TYPE_CHECKING:
    from .transfer import CancellationToken


# EPUB validation accepts single-disk ZIP containers with ordinary directory metadata.
# ZIP64 end records are unsupported; these caps apply before ZipFile allocates its directory.
MAX_EPUB_DIRECTORY_BYTES = 8 * 1024 * 1024
MAX_EPUB_ENTRIES = 10_000
MAX_EPUB_MIMETYPE_BYTES = 100
MAX_EPUB_MIMETYPE_COMPRESSED_BYTES = 1024
_EOCD_WINDOW = 22 + 65535
_INVALID_BOOK = "The site did not return a valid book. Check the session, quota or browser verification."
_SAVE_ERROR = "Could not save the book. Check free space, directory permissions and hard-link support."


def _check_epub_directory(stream: BinaryIO) -> None:
    """Bound both declared size and actual entry count before constructing ZipFile."""
    stream.seek(0, os.SEEK_END)
    length = stream.tell()
    tail_size = min(length, _EOCD_WINDOW)
    stream.seek(length - tail_size)
    tail = stream.read(tail_size)
    end = tail.rfind(b"PK\x05\x06")
    if end < 0 or len(tail) - end < 22:
        raise zipfile.BadZipFile
    _, disk, directory_disk, disk_entries, entries, size, offset, comment_size = struct.unpack_from("<4s4H2IH", tail, end)
    end_offset = length - tail_size + end
    if (disk != 0 or directory_disk != 0 or disk_entries != entries
            or not 1 <= entries <= MAX_EPUB_ENTRIES or size > MAX_EPUB_DIRECTORY_BYTES
            or size < entries * 46 or offset + size != end_offset
            or end + 22 + comment_size != len(tail)
            or size == 0xFFFFFFFF or offset == 0xFFFFFFFF):
        raise zipfile.BadZipFile
    # ZipFile probes for a ZIP64 locator here even if it overlaps an entry comment.
    # Reject it explicitly so it cannot replace the directory sizes checked above.
    if end_offset >= 20:
        stream.seek(end_offset - 20)
        if stream.read(4) == b"PK\x06\x07":
            raise zipfile.BadZipFile
    stream.seek(offset)
    count = 0
    while stream.tell() < end_offset:
        if count >= MAX_EPUB_ENTRIES:
            raise zipfile.BadZipFile
        header = stream.read(46)
        if len(header) != 46:
            raise zipfile.BadZipFile
        fields = struct.unpack("<4s6H3I5H2I", header)
        name_size, extra_size, entry_comment = fields[10:13]
        if (fields[0] != b"PK\x01\x02" or fields[13] != 0
                or fields[8] == 0xFFFFFFFF or fields[9] == 0xFFFFFFFF
                or fields[16] == 0xFFFFFFFF or fields[16] + 30 > offset or not name_size):
            raise zipfile.BadZipFile
        following = stream.tell() + name_size + extra_size + entry_comment
        if following > end_offset:
            raise zipfile.BadZipFile
        stream.seek(following)
        count += 1
    if count != entries:
        raise zipfile.BadZipFile


def validate_book_stream(stream: BinaryIO, extension: str) -> None:
    """Validate signatures/EPUB mimetype without loading an entire book into memory.

    EPUB directory metadata is capped at 8 MiB and 10,000 entries. ZIP64 end records
    and multi-disk archives are unsupported; ZIP64 local headers remain compatible
    when ordinary central metadata gives bounded sizes. Only mimetype is inflated.
    The caller retains ownership of the stream; its position may change.
    """
    valid = False
    try:
        stream.seek(0)
        if extension == "pdf":
            valid = stream.read(5) == b"%PDF-"
        elif extension == "djvu":
            header = stream.read(16)
            valid = header.startswith(b"AT&TFORM") and header[12:16] in (b"DJVU", b"DJVM")
        elif extension == "epub":
            _check_epub_directory(stream)
            with zipfile.ZipFile(stream, "r") as archive:
                entry = archive.getinfo("mimetype")
                if (entry.file_size <= MAX_EPUB_MIMETYPE_BYTES
                        and entry.compress_size <= MAX_EPUB_MIMETYPE_COMPRESSED_BYTES
                        and entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    valid = archive.read(entry).strip() == b"application/epub+zip"
    except (zipfile.BadZipFile, KeyError, RuntimeError, OSError, NotImplementedError,
            EOFError, ValueError, struct.error, zlib.error):
        pass
    if not valid:
        raise DownloadError(_INVALID_BOOK, reason="invalid_book")


def validate_book(data: bytes, extension: str) -> None:
    """Compatibility adapter for callers that already hold complete book bytes."""
    validate_book_stream(io.BytesIO(data), extension)


@contextmanager
def _file_errors():
    try:
        yield
    except OSError:
        raise DownloadError(_SAVE_ERROR, reason="save_failed") from None


def _name_limit(folder: Path) -> int | None:
    """The folder's file name limit in bytes on POSIX; None on Windows, where 90 characters always fit."""
    if os.name == "nt":
        return None
    try:
        limit = os.pathconf(folder, "PC_NAME_MAX")
    except (AttributeError, OSError, ValueError):
        return 255
    return limit if isinstance(limit, int) and limit > 0 else 255


def _file_stem(title: str, suffix: str, limit: int | None) -> str:
    """At most 90 characters of the title; on POSIX also shortened, by whole code points, until
    the name with ``suffix`` fits ``limit`` bytes (long CJK or emoji titles exceed 255 bytes)."""
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title)[:90].strip(' .') or "book"
    if limit is not None:
        room = limit - len(suffix.encode("utf-8"))
        while stem and len(stem.encode("utf-8", "surrogatepass")) > room:
            stem = stem[:-1]
        stem = stem.strip(' .') or "book"
    return stem


def save_book(title: str, extension: str, folder: Path, writer: Callable[[BinaryIO], None], *,
              cancel: "CancellationToken | None" = None) -> Path:
    """Write once into an owned temporary file, validate, fsync and publish atomically.

    The temporary file is opened before writer is invoked, so an unwritable folder
    fails before the callback obtains a quota-consuming link. Callback exceptions
    propagate unchanged. Cancellation is checked before the callback and publication;
    a file already published is a completed download and is never removed here.
    """
    if extension not in ("pdf", "epub", "djvu"):
        raise DownloadError(_INVALID_BOOK, reason="invalid_book")
    if not isinstance(title, str):
        raise ValueError("The book title must be text.")
    if cancel is not None:
        cancel.check()
    temporary = None
    output = None
    try:
        with _file_errors():
            folder = Path(folder).resolve(strict=True)
            if not folder.is_dir():
                raise DownloadError(_SAVE_ERROR, reason="save_failed")
            output = tempfile.NamedTemporaryFile(mode="w+b", dir=folder, prefix=".zlib-", suffix=".part", delete=False)
            temporary = Path(output.name)
        if cancel is not None:
            cancel.check()
        writer(output)
        if cancel is not None:
            cancel.check()
        with _file_errors():
            output.flush()
            validate_book_stream(output, extension)
            os.fsync(output.fileno())
            output.close()
        suffix = f"-{uuid.uuid4().hex[:12]}.{extension}"
        target = folder / (_file_stem(title, suffix, _name_limit(folder)) + suffix)
        if cancel is not None:
            cancel.check()
        with _file_errors():
            try:
                os.link(temporary, target)
            except OSError as error:
                if error.errno != errno.ENAMETOOLONG:
                    raise
                # The link was already paid for: keep the book under a short name rather than lose it.
                target = folder / ("book" + suffix)
                os.link(temporary, target)
        return target
    finally:
        if output is not None:
            try:
                output.close()
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
