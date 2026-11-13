"""Synchronous API independent of any UI, web framework or Python CLI."""
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
import json
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin, urlsplit
import unicodedata

from .errors import AuthenticationError, ChallengeRequired, DownloadError, FormatChangedError
from .transport import Transport, validate_url
from .book_files import save_book, validate_book
from .transfer import CancellationToken, ProgressCallback

SUPPORTED_FORMATS = ("pdf", "epub", "djvu")

def _clean_text(value: str) -> str:
    # Remote metadata must not inject terminal escapes or bidirectional controls.
    return " ".join("".join(c for c in value if not unicodedata.category(c).startswith("C") or c in "\n\r\t").split())

@dataclass(frozen=True)
class Book:
    title: str
    authors: str
    extension: str
    url: str
    year: str = ""
    language: str = ""
    size: str = ""
    book_id: str = ""
    book_hash: str = ""
    is_favorite: bool | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

class _Cards(HTMLParser):
    def __init__(self, origin: str):
        super().__init__(convert_charrefs=True)
        self.origin = origin
        self.books: list[Book] = []
        self.card = None
        self.slot = None
        self.depth = 0
        self.slot_depth = 0
        self.recognized = False
        self.empty = False
        self.download_link = None

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        classes = (attrs.get("class") or "").split()
        if attrs.get("id") == "searchResultBox":
            self.recognized = True
        if "notFound" in classes:
            self.empty = True
        if tag == "a" and any(c in classes for c in ("addDownloadedBook", "dlButton")) and not self.download_link:
            self.download_link = attrs.get("href")
        if tag not in ("img", "br", "input", "meta", "link", "hr", "source", "wbr", "area", "base", "embed", "param", "track", "col"):
            self.depth += 1
        if tag == "z-bookcard":
            self.recognized = True
            self.card = {**attrs, "title": "", "authors": ""}
        if self.card is not None and attrs.get("slot") in ("title", "author"):
            self.slot = "title" if attrs["slot"] == "title" else "authors"
            self.slot_depth = self.depth

    def handle_data(self, data):
        if self.card is not None and self.slot:
            self.card[self.slot] += data

    def handle_endtag(self, tag):
        if self.depth == self.slot_depth:
            self.slot = None
        if tag == "z-bookcard" and self.card is not None:
            card = self.card
            extension = (card.get("extension") or "").lower().strip()
            url = urljoin(self.origin, card.get("href") or "")
            if extension in SUPPORTED_FORMATS and card["title"].strip() and same_origin(url, self.origin) and urlsplit(url).path.startswith("/book/"):
                self.books.append(Book(_clean_text(card["title"]), _clean_text(card["authors"]), extension, url, _clean_text(card.get("year") or ""), _clean_text(card.get("language") or ""), _clean_text(card.get("filesize") or "")))
            self.card = None
            self.slot = None
        self.depth = max(0, self.depth - 1)

def same_origin(first: str, second: str) -> bool:
    a, b = urlsplit(first), urlsplit(second)
    return (a.scheme, a.hostname, a.port or 443) == (b.scheme, b.hostname, b.port or 443)

def parse_html(data: bytes, origin: str) -> _Cards:
    page = data.decode("utf-8", errors="replace")
    lowered = page.lower()
    if any(marker in lowered for marker in ("cf-chl-", "<title>just a moment", "g-recaptcha", "h-captcha")):
        raise ChallengeRequired("The site requires browser verification; the connector cannot complete it.")
    parser = _Cards(origin)
    parser.feed(page)
    return parser

class Client:
    """Session-only client. Not thread-safe; use one client per independent session."""
    def __init__(self, domain: str = "https://z-lib.by/", *, timeout: float = 30, transport: Transport | None = None):
        validate_url(domain)
        parsed = urlsplit(domain)
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Use the site origin only, for example https://z-lib.by/.")
        self.domain = domain.rstrip("/") + "/"
        self._transport = transport if transport is not None else Transport(timeout)
        self._authenticated = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        """Discard local session cookies; does not claim to revoke the server session."""
        self._authenticated = False
        self._transport.close()

    def login(self, email: str, password: str) -> None:
        self.close()
        if not email.strip() or not password:
            raise AuthenticationError("Email and password are required.")
        try:
            response = self._transport.request(urljoin(self.domain, "rpc.php"), form={"email": email.strip(), "password": password, "action": "login", "isModal": "true", "site_mode": "books", "isSingleLogin": "1", "redirectUrl": "", "gg_json_mode": "1"})
            try:
                payload = json.loads(response.body)
            except (ValueError, UnicodeError):
                parse_html(response.body, self.domain)
                raise FormatChangedError("The login endpoint did not return JSON. Check the domain or browser verification.") from None
            result = payload.get("response") if isinstance(payload, dict) else None
            if not isinstance(result, dict):
                raise FormatChangedError("The login response format changed.")
            if result.get("validationError") or not self._transport.authenticated:
                raise AuthenticationError("Login refused. Check the credentials and configured site domain.")
            self._authenticated = True
        except BaseException:
            self.close()
            raise

    def _require_login(self):
        if not self._authenticated:
            raise AuthenticationError("Call login() before searching or downloading.")

    def search(self, query: str, *, extension: str | None = None, page: int = 1, limit: int = 20) -> list[Book]:
        self._require_login()
        query = query.strip()
        if not query or len(query) > 500:
            raise ValueError("Query must contain 1 to 500 characters.")
        if extension is not None and extension not in SUPPORTED_FORMATS:
            raise ValueError("Supported formats: pdf, epub, djvu.")
        if not 1 <= page <= 1000 or not 1 <= limit <= 50:
            raise ValueError("Page must be 1 to 1000 and result limit 1 to 50.")
        parameters = [("page", str(page))] + [("extensions[]", ext) for ext in ((extension,) if extension else SUPPORTED_FORMATS)]
        url = urljoin(self.domain, "s/" + quote(query, safe="")) + "?" + urlencode(parameters)
        response = self._transport.request(url)
        if not same_origin(response.url, self.domain):
            raise FormatChangedError("Search moved to another domain. Configure it explicitly and reconnect.")
        parsed = parse_html(response.body, self.domain)
        if not parsed.recognized and not parsed.empty:
            raise FormatChangedError("No recognizable book results. The session may have expired or the site format changed.")
        return [book for book in parsed.books if extension is None or book.extension == extension][:limit]

    @staticmethod
    def _download_token(progress, cancel) -> CancellationToken:
        if progress is not None and not callable(progress):
            raise ValueError("Download progress must be a callable or None.")
        if cancel is not None and not isinstance(cancel, CancellationToken):
            raise ValueError("Download cancellation requires a CancellationToken.")
        token = cancel if cancel is not None else CancellationToken()
        token.check()
        return token

    def download(self, book: Book, destination: str | Path = ".", *,
                 progress: ProgressCallback | None = None, cancel: CancellationToken | None = None) -> Path:
        token = self._download_token(progress, cancel)
        self._require_login()
        if not isinstance(book, Book):
            raise ValueError("Download requires a supported Book from this site.")
        validate_url(book.url)
        if not same_origin(book.url, self.domain) or not urlsplit(book.url).path.startswith("/book/") or book.extension not in SUPPORTED_FORMATS:
            raise ValueError("Download requires a supported Book from this site.")
        folder = Path(destination).resolve(strict=True)
        if not folder.is_dir():
            raise ValueError("The download destination must be an existing directory.")

        def receive(output):
            token.check()
            response = self._transport.request(book.url)
            token.check()
            if not same_origin(response.url, self.domain):
                raise AuthenticationError("The book page moved to another domain. Reconnect using that domain.")
            parsed = parse_html(response.body, self.domain)
            if not parsed.download_link:
                raise DownloadError("No download link available. Check account quota, session or site changes.", reason="no_link")
            self._transport.download_to(urljoin(response.url, parsed.download_link), output,
                                        progress=progress, cancel=token)

        return save_book(book.title, book.extension, folder, receive, cancel=token)
