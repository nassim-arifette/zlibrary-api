"""Native My Library tag catalogs and saved identifiers.

The website's tag catalog exposes labels as object keys; the associated values
have no verified public meaning. An empty catalog can be encoded as ``[]``.
This module intentionally does not derive a native library from other lists.
"""

from dataclasses import dataclass
import re
from typing import Literal
import unicodedata

from .errors import FormatChangedError


@dataclass(frozen=True)
class LibraryTags:
    names: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"tags": list(self.names)}


@dataclass(frozen=True)
class BookTagUpdate:
    """A confirmed tag mutation, not a snapshot of all tags or favorite state."""

    book_id: str
    action: Literal["add", "remove"]
    tags: tuple[str, ...]

    @property
    def adds_to_favorites(self) -> bool:
        return self.action == "add"

    def to_dict(self) -> dict:
        return {"book_id": self.book_id, "action": self.action, "tags": list(self.tags),
                "adds_to_favorites": self.adds_to_favorites}


def _safe_tag(value) -> bool:
    return (isinstance(value, str) and bool(value.strip())
            and not any(unicodedata.category(char).startswith("C") for char in value))


def parse_library_tags(payload: dict) -> LibraryTags:
    """Read labels without interpreting counts, colors, or other opaque values."""
    tags = payload.get("tags") if isinstance(payload, dict) else None
    if isinstance(tags, list) and not tags:
        return LibraryTags(())
    if not isinstance(tags, dict) or any(not _safe_tag(name) for name in tags):
        raise FormatChangedError("The API returned an invalid My Library tag catalog.")
    return LibraryTags(tuple(tags))


def parse_saved_book_ids(payload: dict) -> tuple[str, ...]:
    """Read the native saved IDs; they are not a complete native library page."""
    identifiers = payload.get("list") if isinstance(payload, dict) else None
    if not isinstance(identifiers, list):
        raise FormatChangedError("The API returned an invalid saved-book ID list.")
    result = []
    for identifier in identifiers:
        if type(identifier) not in (str, int) or not re.fullmatch(r"[0-9]{1,20}", str(identifier)):
            raise FormatChangedError("The API returned an invalid saved-book ID.")
        result.append(str(identifier))
    return tuple(result)


def validate_tags(tags: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Validate a mutation and remove exact duplicates, preserving label spelling.

    No trimming, case folding, Unicode normalization, or comma splitting is
    applied: the supplied strings identify the tags to add or remove. The
    website's 30-character limit is measured in JavaScript UTF-16 code units.
    Existing tags on the book and their combined limit are checked upstream.
    """
    if not isinstance(tags, (tuple, list)) or not tags:
        raise ValueError("Provide at least one My Library tag as a list or tuple.")
    result = []
    for tag in tags:
        if not _safe_tag(tag) or len(tag.encode("utf-16-le")) // 2 > 30:
            raise ValueError("Each My Library tag must contain 1–30 characters without control characters.")
        if tag not in result:
            result.append(tag)
    return tuple(result)
