"""Account collections, kept distinct from local files and download queues."""

from dataclasses import asdict, dataclass

from .pagination import BookPage
from .booklists import Booklist


FAVORITE_ORDERS = ("saved_date", "saved_dateA", "titleA", "title", "year", "filesize", "filesizeA")
HISTORY_ORDERS = ("downloaded_date", "downloaded_dateA", "titleA", "title", "year", "filesize", "filesizeA")


@dataclass(frozen=True)
class LibraryOverview:
    favorites: BookPage
    history: BookPage
    booklists: tuple[Booklist, ...]

    def to_dict(self) -> dict:
        return {"favorites": self.favorites.to_dict(), "history": self.history.to_dict(),
                "booklists": [item.to_dict() for item in self.booklists]}


@dataclass(frozen=True)
class FavoriteUpdate:
    book_id: str
    is_favorite: bool

    def to_dict(self) -> dict:
        return asdict(self)
