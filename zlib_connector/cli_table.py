"""Plain tables for CLI text output: one header row, two-space gaps, one line per row.

Widths follow the terminal only when stdout is a terminal. Only flexible columns (title, authors) are
shortened, with "..."; references are never truncated so they can be copied.
"""
import shutil
import sys

from .terminal_text import terminal_text

GAP = "  "
MIN_FLEX = 8


def _cell(value) -> str:
    if value is None or value == "":
        return "-"
    return " ".join(terminal_text(str(value)).split())


def _shorten(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[:max(width - 3, 0)] + "..." if width > 3 else text[:width]


def _is_terminal(stream) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, OSError, ValueError):
        return False


def render(columns, rows, *, flexible=(), right=(), width=None, file=None) -> list[str]:
    """The lines of a table. ``columns`` are header names; ``flexible`` and ``right`` hold column indexes."""
    stream = sys.stdout if file is None else file
    body = [[_cell(value) for value in row] for row in rows]
    widths = [max([len(name), *(len(row[index]) for row in body)]) for index, name in enumerate(columns)]
    if width is None and _is_terminal(stream):
        width = shutil.get_terminal_size().columns
    if width is not None and flexible:
        excess = sum(widths) + len(GAP) * (len(columns) - 1) - width
        for index in sorted(flexible, key=lambda item: -widths[item]):
            if excess <= 0:
                break
            floor = max(MIN_FLEX, len(columns[index]))
            cut = min(excess, widths[index] - floor)
            if cut > 0:
                widths[index] -= cut
                excess -= cut

    def line(cells):
        parts = []
        for index, text in enumerate(cells):
            text = _shorten(text, widths[index]) if index in flexible else text
            parts.append(text.rjust(widths[index]) if index in right else text.ljust(widths[index]))
        return GAP.join(parts).rstrip()

    return [line(list(columns)), *(line(row) for row in body)]


def print_table(columns, rows, *, flexible=(), right=(), file=None, empty=None) -> None:
    stream = sys.stdout if file is None else file
    rows = list(rows)
    if not rows and empty:
        print(empty, file=stream)
        return
    for text in render(columns, rows, flexible=flexible, right=right, file=stream):
        print(text, file=stream)


BOOK_COLUMNS = ("#", "TITLE", "AUTHORS", "YEAR", "LANG", "EXT", "SIZE", "REFERENCE")


def book_rows(books):
    for index, book in enumerate(books, 1):
        title = book.title + (" [saved]" if getattr(book, "is_favorite", False) else "")
        reference = f"{book.book_id}:{book.book_hash}" if book.book_id and book.book_hash else ""
        entry = getattr(book, "entry_id", None)
        row = [index, title, book.authors, book.year, book.language, (book.extension or "").upper(), book.size,
               reference]
        yield row + ([entry] if entry is not None else [])


def print_books(books, *, file=None) -> None:
    rows = list(book_rows(books))
    columns = BOOK_COLUMNS + (("ENTRY",) if any(len(row) > len(BOOK_COLUMNS) for row in rows) else ())
    rows = [row + [""] * (len(columns) - len(row)) for row in rows]
    print_table(columns, rows, flexible=(1, 2), right=(0,), file=file, empty="No books found.")
