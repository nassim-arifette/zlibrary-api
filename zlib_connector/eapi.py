"""JSON API used by the mobile client; HTML login remains a separate handshake."""
import json
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode, urljoin

from .client import Book, Client as HtmlClient, SUPPORTED_FORMATS, parse_html, same_origin
from .errors import AuthenticationError, ChallengeRequired, DownloadError, FormatChangedError, MutationUncertainError, NetworkError, RateLimitError
from .search import SearchFilters, SearchPage, parse_page, validate_search
from .books import BookDetails, BookFormat, BookReference, parse_books, parse_details, parse_format
from .account import AccountProfile, DownloadQuota, parse_profile
from .library import FAVORITE_ORDERS, HISTORY_ORDERS, FavoriteUpdate, LibraryOverview
from .booklists import (
    Booklist, BooklistBookUpdate, BooklistDeletion, BooklistSettings,
    booklist_entry_id, booklist_id, parse_booklist, parse_booklist_books, validate_booklist_changes,
)
from .pagination import BookPage, validate_page
from .my_library import BookTagUpdate, LibraryTags, parse_library_tags, parse_saved_book_ids, validate_tags


class Client(HtmlClient):
    def _json(self, path: str, *, form=None, json_body=None, method=None, mutation=False) -> dict:
        options = {"follow_redirects": False} if mutation else {}
        if json_body is not None:
            options["json_body"] = json_body
        if method is not None:
            options["method"] = method
        try:
            response = self._transport.request(urljoin(self.domain, path), form=form, **options)
            if not same_origin(response.url, self.domain):
                raise FormatChangedError("The API moved to another domain. Select that domain explicitly.")
            try:
                value = json.loads(response.body)
            except (ValueError, UnicodeError):
                parse_html(response.body, self.domain)
                raise FormatChangedError("The API returned HTML instead of JSON. Check the selected domain.") from None
            if not isinstance(value, dict) or "success" not in value:
                raise FormatChangedError("The API response format changed.")
        except (NetworkError, DownloadError, FormatChangedError, ChallengeRequired):
            if mutation:
                raise MutationUncertainError(
                    "The account update could not be confirmed. Check its current state before trying again."
                ) from None
            raise
        # Only a readable API response can confirm success or explicitly refuse a mutation.
        if value["success"] != 1:
            # Classify known errors, but never echo remote bodies or account fields.
            error = str(value.get("error", "")).lower()
            if "limit" in error or "quota" in error:
                raise RateLimitError("The account download quota or request limit was reached.")
            if "auth" in error or "login" in error or "password" in error:
                raise AuthenticationError("The API refused the session. Reconnect to the selected domain.")
            raise DownloadError("The API refused this operation.")
        return value

    def login(self, email: str, password: str) -> None:
        super().login(email, password)
        try:
            profile = self._json("eapi/user/profile")
            if not isinstance(profile.get("user"), dict):
                raise FormatChangedError("Login returned cookies but the API did not confirm an account.")
        except BaseException:
            self.close()
            raise

    def search(self, query: str, *, extension: str | None = None, page: int = 1,
               limit: int = 20, filters: SearchFilters | None = None) -> list[Book]:
        """Return books only, preserving the original list-based interface."""
        return list(self.search_page(query, extension=extension, page=page, limit=limit, filters=filters).books)

    def search_page(self, query: str, *, extension: str | None = None, page: int = 1,
                    limit: int = 20, filters: SearchFilters | None = None) -> SearchPage:
        """Search anonymously or signed in, preserving upstream pagination metadata."""
        query = validate_search(query, page, limit)
        filters = filters or SearchFilters()
        form = {"message": query, "page": str(page), "limit": str(limit), **filters.to_form(extension)}
        formats = (extension,) if extension else filters.formats or SUPPORTED_FORMATS
        payload = self._json("eapi/book/search", form=form)
        books = parse_books(payload.get("books"), self.domain, formats=formats)
        return parse_page(payload, books[:limit], page, limit)

    def iter_search(self, query: str, *, filters: SearchFilters | None = None,
                    extension: str | None = None, page: int = 1, limit: int = 20):
        """Yield books lazily; stop when the server supplies no next page or at page 1000."""
        while True:
            result = self.search_page(query, filters=filters, extension=extension, page=page, limit=limit)
            yield from result.books
            if result.next_page is None or result.next_page > 1000:
                return
            if result.next_page <= page:
                raise FormatChangedError("The API did not advance to the next search page.")
            page = result.next_page

    def get_book(self, reference: str) -> BookDetails:
        reference = BookReference.parse(reference, self.domain)
        payload = self._json(f"eapi/book/{reference.book_id}/{reference.book_hash}")
        book = parse_details(payload.get("book"), self.domain)
        if (book.book_id, book.book_hash) != (reference.book_id, reference.book_hash):
            raise FormatChangedError("The API returned a different book from the requested reference.")
        return book

    def get_profile(self) -> AccountProfile:
        """Fetch a fresh public projection; no raw account fields are exposed."""
        self._require_login()
        return parse_profile(self._json("eapi/user/profile"))

    def get_quota(self) -> DownloadQuota:
        return self.get_profile().quota

    def get_formats(self, reference: str) -> list[BookFormat]:
        reference = BookReference.parse(reference, self.domain)
        payload = self._json(f"eapi/book/{reference.book_id}/{reference.book_hash}/formats")
        rows = payload.get("books")
        if not isinstance(rows, list):
            raise FormatChangedError("The API response has no formats list.")
        return [parse_format(row) for row in rows]

    def _collection_page(self, path: str, *, page: int, limit: int) -> BookPage:
        payload = self._json(path)
        # Keep sparse and unsupported-format records visible; details can resolve them later.
        return parse_page(payload, parse_books(payload.get("books"), self.domain), page, limit)

    def _account_books(self, collection: str, *, page: int, limit: int, order: str | None) -> BookPage:
        self._require_login()
        validate_page(page, limit)
        orders = FAVORITE_ORDERS if collection == "saved" else HISTORY_ORDERS
        if order is not None and order not in orders:
            raise ValueError("Unsupported order for this account collection.")
        params = {"page": page, "limit": limit}
        if order is not None:
            params["order"] = order
        return self._collection_page(f"eapi/user/book/{collection}?{urlencode(params)}", page=page, limit=limit)

    def get_favorites(self, *, page: int = 1, limit: int = 20, order: str | None = "saved_date") -> BookPage:
        result = self._account_books("saved", page=page, limit=limit, order=order)
        return replace(result, books=tuple(replace(book, is_favorite=True) for book in result.books))

    def set_favorite(self, reference: str, *, saved: bool) -> FavoriteUpdate:
        """Explicit account update; never follow redirects or retry an uncertain response."""
        self._require_login()
        reference = BookReference.parse(reference, self.domain)
        if type(saved) is not bool:
            raise ValueError("Favorite state must be a boolean.")
        action = "save" if saved else "unsave"
        self._json(f"eapi/user/book/{reference.book_id}/{action}", mutation=True)
        return FavoriteUpdate(reference.book_id, saved)

    def get_history(self, *, page: int = 1, limit: int = 20, order: str | None = None) -> BookPage:
        """Read account download history; the service may ignore requested ordering."""
        return self._account_books("downloaded", page=page, limit=limit, order=order)

    def get_library_tags(self) -> LibraryTags:
        """Read the native My Library tag catalog, without guessing tag counts."""
        self._require_login()
        return parse_library_tags(self._json("papi/my-library/tags/"))

    def get_saved_book_ids(self) -> tuple[str, ...]:
        """Read saved identifiers; these are not a full native My Library page."""
        self._require_login()
        return parse_saved_book_ids(self._json("papi/my-library/saved-book-ids"))

    def _update_book_tags(self, reference: str, tags, *, action: str) -> BookTagUpdate:
        self._require_login()
        reference = BookReference.parse(reference, self.domain)
        tags = validate_tags(tags)
        self._json(f"papi/my-library/tags/{reference.book_id}",
                   method="PUT" if action == "add" else "DELETE", json_body={"tags": list(tags)}, mutation=True)
        return BookTagUpdate(reference.book_id, action, tags)

    def add_book_tags(self, reference: str, tags: tuple[str, ...] | list[str]) -> BookTagUpdate:
        """Add native tags. The site also adds the book to favorites."""
        return self._update_book_tags(reference, tags, action="add")

    def remove_book_tags(self, reference: str, tags: tuple[str, ...] | list[str]) -> BookTagUpdate:
        """Remove only the supplied tags; this does not remove the favorite."""
        return self._update_book_tags(reference, tags, action="remove")

    def get_booklists(self) -> list[Booklist]:
        return [parse_booklist(row) for row in self._booklist_records()]

    def _booklist_records(self) -> list[dict]:
        self._require_login()
        rows = self._json("papi/booklist/current-user/0").get("list")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise FormatChangedError("The API response has no valid booklists list.")
        return rows

    def _write_booklist(self, path: str, settings: BooklistSettings, identifier: str | None = None) -> Booklist:
        payload = self._json(path, json_body=settings.to_payload(), mutation=True)
        try:
            row = payload.get("readlist")
            result = parse_booklist(row)
            # Display defaults must never substitute for missing mutation acknowledgments.
            BooklistSettings.from_record(row, {})
            if ((identifier is not None and result.booklist_id != identifier) or not settings.matches(result)):
                raise FormatChangedError("The updated booklist could not be confirmed.")
            return result
        except FormatChangedError:
            raise MutationUncertainError(
                "The booklist update could not be confirmed. Read your lists and visibility before trying again."
            ) from None

    def create_booklist(self, title: str, *, description: str = "", is_public: bool = False, color: int = 0) -> Booklist:
        """Create a private list by default. Publishing requires is_public=True."""
        self._require_login()
        settings = BooklistSettings(title, description, is_public, color)
        return self._write_booklist("papi/booklist/create", settings)

    def update_booklist(self, identifier: str | int, *, title: str | None = None,
                        description: str | None = None, is_public: bool | None = None,
                        color: int | None = None) -> Booklist:
        """Read current settings, then replace only requested fields in the site's full form."""
        self._require_login()
        identifier = booklist_id(identifier)
        changes = validate_booklist_changes(title=title, description=description, is_public=is_public, color=color)
        for row in self._booklist_records():
            if parse_booklist(row).booklist_id == identifier:
                settings = BooklistSettings.from_record(row, changes)
                return self._write_booklist(f"papi/booklist/{identifier}/edit", settings, identifier)
        raise ValueError("This booklist was not found in the account's editable lists.")

    def delete_booklist(self, identifier: str | int) -> BooklistDeletion:
        """Delete a remote list permanently; downloaded files are unaffected."""
        self._require_login()
        identifier = booklist_id(identifier)
        self._json(f"papi/booklist/{identifier}/delete", mutation=True)
        return BooklistDeletion(identifier)

    def get_booklist_books(self, identifier: str | int, *, page: int = 1) -> BookPage:
        self._require_login()
        identifier = booklist_id(identifier)
        validate_page(page, 20)
        # This is a read operation despite the upstream endpoint requiring POST.
        payload = self._json(f"papi/booklist/{identifier}/get-books/{page}", form={})
        books = parse_booklist_books(payload.get("books"), self.domain, identifier)
        return parse_page(payload, books, page, 20)

    def add_booklist_book(self, identifier: str | int, reference: str) -> BooklistBookUpdate:
        """Add a book to an existing list; a failed confirmation is never retried."""
        self._require_login()
        identifier = booklist_id(identifier)
        reference = BookReference.parse(reference, self.domain)
        self._json(f"papi/booklist/{identifier}/add-book/{reference.book_id}", mutation=True)
        return BooklistBookUpdate(identifier, "add", book_id=reference.book_id)

    def remove_booklist_book(self, identifier: str | int, entry_id: str | int) -> BooklistBookUpdate:
        """Remove the list entry returned by get_booklist_books(), not a catalog book ID."""
        self._require_login()
        identifier = booklist_id(identifier)
        entry_id = booklist_entry_id(entry_id)
        self._json(f"papi/booklist/{identifier}/remove-book/{entry_id}", mutation=True)
        return BooklistBookUpdate(identifier, "remove", entry_id=entry_id)

    def get_library(self, *, limit: int = 5) -> LibraryOverview:
        """Show separate account collections; no local-file inventory is implied."""
        self._require_login()
        validate_page(1, limit)
        return LibraryOverview(self.get_favorites(limit=limit), self.get_history(limit=limit),
                               tuple(self.get_booklists()))

    def get_similar(self, reference: str) -> list[Book]:
        reference = BookReference.parse(reference, self.domain)
        payload = self._json(f"eapi/book/{reference.book_id}/{reference.book_hash}/similar")
        return parse_books(payload.get("books"), self.domain)

    def get_popular(self) -> list[Book]:
        return parse_books(self._json("eapi/book/most-popular").get("books"), self.domain)

    def get_recommendations(self) -> list[Book]:
        self._require_login()
        return parse_books(self._json("eapi/user/book/recommended").get("books"), self.domain)

    def download(self, book: Book | str, destination: str | Path = ".") -> Path:
        self._require_login()
        folder = Path(destination).resolve(strict=True)
        if not folder.is_dir():
            raise ValueError("The download destination must be an existing directory.")
        if isinstance(book, str):
            book = self.get_book(book)
        elif not book.extension:
            if not same_origin(book.url, self.domain):
                raise ValueError("Download requires a book from the selected API domain.")
            book = self.get_book(str(BookReference(book.book_id, book.book_hash)))
        if (not book.book_id.isascii() or not book.book_id.isdigit()
                or not re.fullmatch(r"[A-Za-z0-9]+", book.book_hash)
                or book.extension not in SUPPORTED_FORMATS or not same_origin(book.url, self.domain)):
            raise ValueError("Download requires a supported book returned by this API domain.")
        payload = self._json(f"eapi/book/{book.book_id}/{book.book_hash}/file")
        file = payload.get("file")
        if not isinstance(file, dict):
            raise FormatChangedError("The API response has no download information.")
        if file.get("allowDownload") in (False, 0, "0"):
            raise DownloadError("Download is unavailable for this account or its current quota.")
        link = file.get("downloadLink")
        if not isinstance(link, str) or not link:
            raise DownloadError("The API did not issue a download link. Check the account quota.")
        actual_format = str(file.get("extension") or book.extension).lower()
        if actual_format != book.extension:
            raise DownloadError("The download format differs from the selected book.")
        response = self._transport.request(urljoin(self.domain, link), download=True)
        return self._save_download(book, folder, response.body)
