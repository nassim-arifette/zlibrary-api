"""Bounded view state and literal text presentation of catalog models."""
from dataclasses import dataclass

from rich.text import Text

from ..booklists import Booklist
from ..books import BookDetails
from ..pagination import BookPage
from ..terminal_text import terminal_text


ROOTS = {"favorites": "Favorites", "booklists": "Booklists", "history": "Download history"}


def safe_text(value: str, *, limit: int = 512, multiline: bool = False) -> str:
    return terminal_text(value[:limit], multiline=multiline)


def literal(value: str, *, limit: int = 512) -> Text:
    return Text(safe_text(value, limit=limit) or "Unknown", no_wrap=True, overflow="ellipsis")


@dataclass
class ViewState:
    root: str
    kind: str
    page: int = 1
    identifier: str = ""
    label: str = ""
    value: BookPage | tuple[Booklist, ...] | BookDetails | None = None
    error: str | None = None
    cursor: int = 0
    scroll_x: float = 0
    scroll_y: float = 0

    @property
    def title(self) -> str:
        if self.kind == "members":
            return "Booklists / " + safe_text(self.label)
        if self.kind == "details":
            return ROOTS[self.root] + " / Book details"
        return ROOTS[self.root]


def details_text(book: BookDetails) -> Text:
    result = Text()
    result.append((safe_text(book.title, limit=4096) or "Untitled book") + "\n\n", style="bold")
    fields = (("Author", book.authors), ("Format", book.extension), ("Size", book.size),
              ("Year", book.year), ("Language", book.language), ("Publisher", book.publisher),
              ("Edition", book.edition), ("Pages", book.pages), ("ISBN-10", book.isbn10),
              ("ISBN-13", book.isbn13), ("Identifier", book.identifier),
              ("Categories", ", ".join(book.categories)),
              ("Favorite", "Unknown" if book.is_favorite is None else "Yes" if book.is_favorite else "No"),
              ("Book reference", book.book_id + ":" + book.book_hash))
    for label, value in fields:
        result.append(label + ": ", style="bold")
        # The detail view preserves the adapter's validated metadata bounds;
        # only compact table cells use the shorter presentation limit.
        limit = 100 * 256 + 99 * 2 if label == "Categories" else 4096
        result.append((safe_text(value, limit=limit) or "Unknown") + "\n")
    result.append("\nDescription\n", style="bold")
    result.append(safe_text(book.description, limit=65536, multiline=True) or "No description available.")
    return result
