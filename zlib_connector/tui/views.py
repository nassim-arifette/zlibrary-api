"""Bounded view state and literal text presentation of catalog models.

Every server value passes through safe_text, which removes terminal escapes
and Unicode controls without interpreting markup; widgets show the result as
literal text.
"""
from dataclasses import dataclass

from rich.cells import cell_len
from rich.text import Text

from ..booklists import Booklist
from ..books import BookDetails, BookReference
from ..catalog_schema import MAX_ROWS
from ..client import Book
from ..pagination import BookPage
from ..search import SearchFilters
from ..terminal_text import terminal_text


ROOTS = {"favorites": "Favorites", "booklists": "Booklists", "history": "Download history",
         "search": "Search", "popular": "Popular", "recommended": "Recommended"}
START_VIEWS = ("home", *ROOTS)
ROOT_KINDS = {"favorites": "books", "booklists": "booklists", "history": "books",
              "search": "form", "popular": "popular", "recommended": "recommended"}
PAGE_KINDS = ("books", "members", "results")
LIST_KINDS = ("popular", "recommended", "similar")
BOOK_KINDS = PAGE_KINDS + LIST_KINDS
MAX_DEPTH = 32
FORMATS = (("pdf", "PDF"), ("epub", "EPUB"), ("djvu", "DjVu"))
ORDERS = (("bestmatch", "Best match"), ("popular", "Popular"), ("date", "Recently added"),
          ("titleA", "Title A–Z"), ("title", "Title Z–A"), ("year", "Year"),
          ("filesize", "Largest first"), ("filesizeA", "Smallest first"))
# What each view loads, in the loading, empty and error sentences.
NOUNS = {"favorites": "favorites", "history": "download history", "booklists": "booklists",
         "members": "this booklist", "results": "search results", "popular": "popular books",
         "recommended": "recommendations", "similar": "similar books", "details": "book details"}
EMPTY = {"favorites": "No books in your favorites yet.", "history": "No download history yet.",
         "booklists": "No booklists yet.", "members": "No books in this booklist yet.",
         "results": "No books found for this search.", "popular": "No popular books were returned.",
         "recommended": "No recommendations yet.", "similar": "No similar books were returned."}
NO_DESCRIPTION = "No description available."


def safe_text(value: str, *, limit: int = 512, multiline: bool = False) -> str:
    return terminal_text(value[:limit], multiline=multiline)


def literal(value: str, *, limit: int = 512, fallback: str = "Unknown") -> Text:
    """A table cell; the fallback for a missing value is dim, so it does not read as data."""
    text = safe_text(value, limit=limit)
    return Text(text or fallback, style="" if text else "dim", no_wrap=True, overflow="ellipsis")


def count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def book_reference(book: Book) -> str | None:
    """Return ID:HASH, or None when an adapter supplied an incomplete reference."""
    try:
        return str(BookReference(book.book_id, book.book_hash))
    except ValueError:
        return None


def format_label(extension: str) -> str:
    """A file format as the search form names it (PDF, EPUB, DjVu); other formats in capitals."""
    text = safe_text(extension, limit=32)
    return dict(FORMATS).get(text.lower(), text.upper())


def book_meta(book: Book) -> str:
    """FORMAT · size · year · language, plus ★ Saved; unknown fields are skipped."""
    parts = [format_label(book.extension)] + [safe_text(value, limit=64) for value in (book.size, book.year, book.language)]
    if book.is_favorite is True:
        parts.append("★ Saved")
    return " · ".join(part for part in parts if part)


def booklist_meta(item: Booklist) -> str:
    parts = ["" if item.is_public is None else "Public" if item.is_public else "Private",
             "" if item.book_count is None else count(item.book_count, "book"),
             "updated " + updated if (updated := safe_text(item.updated_at, limit=64)) else ""]
    return " · ".join(part for part in parts if part)


def description_lines(text: str, limit: int = 2) -> list[str]:
    """The first non-empty lines of a description, as literal text."""
    lines = (line.strip() for line in safe_text(text, limit=4096, multiline=True).split("\n"))
    return [line for line in lines if line][:limit]


@dataclass(frozen=True)
class SearchRequest:
    """A search the form validated with search_parameters before submitting it."""

    query: str
    filters: SearchFilters = SearchFilters()


def filters_text(filters: SearchFilters) -> str:
    """Summarize active filters and the sort order in one short literal line; empty when none apply."""
    parts = []
    if filters.languages:
        extra = len(filters.languages) - 3
        parts.append(", ".join(filters.languages[:3]) + (f" +{extra} more" if extra > 0 else ""))
    if filters.formats:
        parts.append(", ".join(label for value, label in FORMATS if value in filters.formats))
    start, end = filters.year_from, filters.year_to
    if start is not None or end is not None:
        parts.append(f"from {start}" if end is None else f"until {end}" if start is None
                     else str(start) if start == end else f"{start}–{end}")
    if filters.order is not None:
        parts.append("Sort: " + dict(ORDERS)[filters.order])
    text = safe_text(" · ".join(parts), limit=4096)
    return text if len(text) <= 160 else text[:159] + "…"


def fit_summary(summary: str, cells: int) -> str:
    """The leading ` · ` parts of a summary that fit in cells, then … for the dropped ones.

    Empty when not even the first part fits, so a line never ends in a cut word.
    """
    parts = summary.split(" · ")
    for end in range(len(parts), 0, -1):
        line = " · ".join(parts[:end] + ["…"] * (end < len(parts)))
        if cell_len(line) <= cells:
            return line
    return ""


def valid_value(kind: str, value) -> bool:
    """Accept only the model each view kind displays, within the row bound."""
    if kind == "details":
        return isinstance(value, BookDetails)
    if kind in PAGE_KINDS:
        if not isinstance(value, BookPage) or not isinstance(value.books, (tuple, list)):
            return False
        value = value.books
    elif not isinstance(value, tuple):
        return False
    if len(value) > MAX_ROWS:
        return False
    if kind == "booklists":
        return all(isinstance(item, Booklist) for item in value)
    return all(isinstance(item, Book) and book_reference(item) is not None for item in value)


@dataclass
class ViewState:
    root: str
    kind: str
    page: int = 1
    identifier: str = ""
    label: str = ""
    search: SearchRequest | None = None
    value: BookPage | tuple[Booklist, ...] | tuple[Book, ...] | BookDetails | None = None
    error: str | None = None
    cursor: int = 0
    scroll_x: float = 0
    scroll_y: float = 0

    @property
    def title(self) -> str:
        if self.kind == "home":
            return "Z-Library"
        if self.kind == "form":
            return "Search books"
        if self.kind == "members":
            return "Booklists › " + (safe_text(self.label) or "Untitled booklist")
        if self.kind == "details":
            return "Book details"
        if self.kind == "similar":
            return "Similar to: " + (safe_text(self.label) or "this book")
        if self.kind == "results" and self.search is not None:
            return "Search: " + safe_text(self.search.query)
        return ROOTS[self.root]

    @property
    def crumb(self) -> str:
        """This view's part of the details breadcrumb."""
        if self.kind == "members":
            return safe_text(self.label) or "Untitled booklist"
        return self.title

    @property
    def noun(self) -> str:
        """What the view loads, as used in loading and error sentences."""
        return NOUNS.get(self.root if self.kind == "books" else self.kind, "this page")

    @property
    def books(self) -> tuple[Book, ...]:
        """Book rows shown by this view; empty for booklists and details."""
        if self.kind in PAGE_KINDS and isinstance(self.value, BookPage):
            return tuple(self.value.books)
        return self.value if self.kind in LIST_KINDS and isinstance(self.value, tuple) else ()

    @property
    def rows(self) -> tuple:
        """Table rows: books, or booklists in the booklists view."""
        if self.kind == "booklists":
            return self.value if isinstance(self.value, tuple) else ()
        return self.books


def root_view(name: str) -> ViewState:
    return ViewState(name, ROOT_KINDS[name])


def split_columns(cells: int, title_needs: int, author_needs: int) -> tuple[int, int]:
    """Share cells between Title and Author.

    When both fit, Title takes what it needs and Author the rest, so authors stay next to their titles;
    otherwise Title keeps at least 60%.
    """
    title_needs, author_needs = max(title_needs, 5), max(author_needs, 6)
    if title_needs + author_needs <= cells:
        return title_needs, cells - title_needs
    author = max(min(author_needs, cells * 2 // 5), cells - title_needs)
    return cells - author, author


def position_text(state: ViewState, cursor: int) -> str:
    """Page n of N · Book i of k, using only what the server reported."""
    parts, page, rows = [], state.value if isinstance(state.value, BookPage) else None, state.rows
    if page is not None:
        parts.append(f"Page {page.page} of {page.total_pages}" if page.total_pages else f"Page {page.page}")
    if rows:
        noun = "List" if state.kind == "booklists" else "Book"
        parts.append(f"{noun} {min(max(cursor, 0), len(rows) - 1) + 1} of {len(rows)}")
    return " · ".join(parts)


def status_extras(state: ViewState) -> list[str]:
    page = state.value if isinstance(state.value, BookPage) else None
    extras = []
    if state.kind in PAGE_KINDS and page is not None and page.total_items is not None:
        extras.append(count(page.total_items, "book"))
    if state.kind == "books" and state.root == "history":
        extras.append("Z-Library account history")
    return extras


def loading_text(state: ViewState) -> str:
    if state.kind == "results":
        return "Searching…"
    return f"Loading {state.noun}…"


def detail_header(book: BookDetails) -> tuple[str, str, str]:
    """Title, authors and meta line of the details header, within the adapter's bounds."""
    return (safe_text(book.title, limit=4096) or "Untitled book", safe_text(book.authors, limit=4096),
            book_meta(book))


def detail_fields(book: BookDetails) -> list[tuple[str, str]]:
    """Labelled detail values, skipping empty ones and keeping validated metadata whole."""
    saved = "" if book.is_favorite is None else "Yes" if book.is_favorite else "No"
    fields = (("Publisher", book.publisher, 4096), ("Edition", book.edition, 512), ("Pages", book.pages, 512),
              ("Year", book.year, 64), ("Language", book.language, 256), ("ISBN-10", book.isbn10, 256),
              ("ISBN-13", book.isbn13, 256), ("Identifier", book.identifier, 2048),
              ("Categories", ", ".join(book.categories), 100 * 256 + 99 * 2), ("Saved", saved, 8),
              ("Reference", book.book_id + ":" + book.book_hash, 256))
    return [(label, text) for label, value, limit in fields if (text := safe_text(value, limit=limit))]


def detail_description(book: BookDetails) -> str:
    """The literal description; empty when the server sent none (the page then shows NO_DESCRIPTION)."""
    return safe_text(book.description, limit=65536, multiline=True)
