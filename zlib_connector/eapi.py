"""JSON API used by the mobile client; HTML login remains a separate handshake."""
import json
import re
from pathlib import Path
from urllib.parse import urljoin

from .client import Book, Client as HtmlClient, SUPPORTED_FORMATS, _clean_text, parse_html, same_origin
from .errors import AuthenticationError, DownloadError, FormatChangedError, RateLimitError


class Client(HtmlClient):
    def _json(self, path: str, *, form=None) -> dict:
        response = self._transport.request(urljoin(self.domain, path), form=form)
        if not same_origin(response.url, self.domain):
            raise FormatChangedError("The API moved to another domain. Select that domain explicitly.")
        try:
            value = json.loads(response.body)
        except (ValueError, UnicodeError):
            parse_html(response.body, self.domain)
            raise FormatChangedError("The API returned HTML instead of JSON. Check the selected domain.") from None
        if not isinstance(value, dict) or "success" not in value:
            raise FormatChangedError("The API response format changed.")
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

    def search(self, query: str, *, extension: str | None = None, page: int = 1, limit: int = 20) -> list[Book]:
        """Search the public API; authenticated sessions use the same endpoint."""
        query = query.strip()
        if not query or len(query) > 500:
            raise ValueError("Query must contain 1–500 characters.")
        if extension is not None and extension not in SUPPORTED_FORMATS:
            raise ValueError("Supported formats: pdf, epub, djvu.")
        if not 1 <= page <= 1000 or not 1 <= limit <= 50:
            raise ValueError("Page must be 1–1000 and result limit 1–50.")
        form = {"message": query, "page": str(page), "limit": str(limit)}
        if extension is not None:
            form["extensions[]"] = extension
        else:
            form["extensions[]"] = list(SUPPORTED_FORMATS)
        payload = self._json("eapi/book/search", form=form)
        rows = payload.get("books")
        if not isinstance(rows, list):
            raise FormatChangedError("The API search response has no books list.")
        books = []
        for row in rows:
            if not isinstance(row, dict):
                raise FormatChangedError("The API returned an invalid book record.")
            book_id = str(row.get("id", ""))
            book_hash = str(row.get("hash", ""))
            fmt = str(row.get("extension", "")).lower()
            if fmt not in SUPPORTED_FORMATS or (extension and fmt != extension):
                continue
            if not book_id.isascii() or not book_id.isdigit() or not re.fullmatch(r"[A-Za-z0-9]+", book_hash):
                raise FormatChangedError("The API returned an invalid book reference.")
            title = _clean_text(str(row.get("title", "")))
            if not title:
                raise FormatChangedError("The API returned a book without a title.")
            href = urljoin(self.domain, str(row.get("href") or f"book/{book_id}/{book_hash}"))
            if not same_origin(href, self.domain):
                href = urljoin(self.domain, f"book/{book_id}/{book_hash}")
            books.append(Book(title=title, authors=_clean_text(str(row.get("author") or "")), extension=fmt, url=href,
                              year=str(row.get("year") or ""), language=_clean_text(str(row.get("language") or "")),
                              size=str(row.get("filesizeString") or row.get("filesize") or ""),
                              book_id=book_id, book_hash=book_hash))
        return books[:limit]

    def download(self, book: Book, destination: str | Path = ".") -> Path:
        self._require_login()
        if (not book.book_id.isascii() or not book.book_id.isdigit()
                or not re.fullmatch(r"[A-Za-z0-9]+", book.book_hash)
                or book.extension not in SUPPORTED_FORMATS or not same_origin(book.url, self.domain)):
            raise ValueError("Download requires a supported book returned by this API domain.")
        folder = Path(destination).resolve(strict=True)
        if not folder.is_dir():
            raise ValueError("The download destination must be an existing directory.")
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
