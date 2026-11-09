"""Book references and public metadata, independent of the presentation layer."""

from dataclasses import asdict, dataclass
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit

from .client import Book, SUPPORTED_FORMATS, _clean_text, same_origin
from .errors import FormatChangedError, NetworkError
from .transport import validate_url


@dataclass(frozen=True)
class BookReference:
    book_id: str
    book_hash: str

    def __post_init__(self):
        if (not isinstance(self.book_id, str) or not re.fullmatch(r"[0-9]{1,20}", self.book_id)
                or not isinstance(self.book_hash, str) or not re.fullmatch(r"[A-Za-z0-9]{1,128}", self.book_hash)):
            raise ValueError("Use a complete book reference: ID:HASH.")

    @classmethod
    def parse(cls, value: str, domain: str | None = None):
        if not isinstance(value, str) or len(value) > 2048:
            raise ValueError("Use ID:HASH or a book HTTPS URL on the selected domain.")
        value = value.strip()
        if re.fullmatch(r"[0-9]+:[A-Za-z0-9]+", value):
            return cls(*value.split(":"))
        try:
            validate_url(value)
            parsed = urlsplit(value)
            match = re.fullmatch(r"/book/([0-9]+)/([A-Za-z0-9]+)(?:/[^?#]*)?", parsed.path)
            if not match or parsed.query or parsed.fragment or (domain and not same_origin(value, domain)):
                raise ValueError
            return cls(*match.groups())
        except (ValueError, NetworkError):
            raise ValueError("Use ID:HASH or a book HTTPS URL on the selected domain.") from None

    def __str__(self):
        return f"{self.book_id}:{self.book_hash}"


@dataclass(frozen=True)
class BookDetails(Book):
    description: str = ""
    cover_url: str = ""
    identifier: str = ""
    isbn10: str = ""
    isbn13: str = ""
    publisher: str = ""
    edition: str = ""
    pages: str = ""
    categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class BookFormat:
    book_id: str
    book_hash: str
    extension: str
    size: str = ""

    def to_dict(self) -> dict:
        return {**asdict(self), "download_supported": self.extension in SUPPORTED_FORMATS}


def _text(value) -> str:
    return _clean_text(str(value)) if type(value) in (str, int, float) else ""


def _reference(row: dict) -> BookReference:
    try:
        return BookReference(str(row.get("id", "")), str(row.get("hash", "")))
    except ValueError:
        raise FormatChangedError("The API returned an invalid book reference.") from None


def parse_book(row: dict, domain: str) -> Book:
    if not isinstance(row, dict):
        raise FormatChangedError("The API returned an invalid book record.")
    reference = _reference(row)
    title = _text(row.get("title"))
    if not title:
        raise FormatChangedError("The API returned a book without a title.")
    saved = row.get("_isUserSavedBook")
    if type(saved) in (bool, int, str) and saved in (True, 1, "1", "true"):
        saved = True
    elif type(saved) in (bool, int, str) and saved in (False, 0, "0", "false"):
        saved = False
    elif saved is not None:
        raise FormatChangedError("The API returned an invalid saved-book flag.")
    canonical = urljoin(domain, f"book/{reference.book_id}/{reference.book_hash}")
    href = row.get("href")
    try:
        href = urljoin(domain, href) if isinstance(href, str) else canonical
        validate_url(href)
        if not same_origin(href, domain) or urlsplit(href).query or urlsplit(href).fragment:
            href = canonical
    except (ValueError, NetworkError):
        href = canonical
    return Book(title=title, authors=_text(row.get("author")), extension=_text(row.get("extension")).lower(),
                url=href, year=_text(row.get("year")), language=_text(row.get("language")),
                size=_text(row.get("filesizeString") or row.get("filesize")),
                book_id=reference.book_id, book_hash=reference.book_hash, is_favorite=saved)


class _Description(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _isbn(identifier: str) -> tuple[str, str]:
    isbn10 = isbn13 = ""
    # Split independent identifiers before removing ISBN formatting separators.
    for candidate in re.findall(r"(?<![0-9])[0-9][0-9Xx -]{8,30}[0-9Xx](?![0-9])", identifier):
        candidate = candidate.replace("-", "").replace(" ", "").upper()
        if re.fullmatch(r"[0-9]{9}[0-9X]", candidate):
            if sum((10 - index) * (10 if digit == "X" else int(digit)) for index, digit in enumerate(candidate)) % 11 == 0:
                isbn10 = candidate
        elif re.fullmatch(r"97[89][0-9]{10}", candidate):
            if sum(int(digit) * (1 if index % 2 == 0 else 3) for index, digit in enumerate(candidate)) % 10 == 0:
                isbn13 = candidate
    return isbn10, isbn13


def parse_details(row: dict, domain: str) -> BookDetails:
    book = parse_book(row, domain)
    description = _Description()
    description.feed(_text(row.get("description")))
    identifier = _text(row.get("identifier"))
    isbn10, isbn13 = _isbn(identifier)
    cover = _text(row.get("cover"))
    if cover:
        try:
            cover = urljoin(domain, cover)
            validate_url(cover)
        except (ValueError, NetworkError):
            cover = ""
    categories = row.get("categories", ())
    if isinstance(categories, str):
        categories = [categories]
    if not isinstance(categories, (list, tuple)):
        categories = []
    categories = tuple(text for item in categories
                       if (text := _text(item.get("name") if isinstance(item, dict) else item)))
    return BookDetails(**book.to_dict(), description=_clean_text(" ".join(description.parts)),
                       cover_url=cover, identifier=identifier, isbn10=isbn10, isbn13=isbn13,
                       publisher=_text(row.get("publisher")), edition=_text(row.get("edition")),
                       pages=_text(row.get("pages")), categories=categories)


def parse_format(row: dict) -> BookFormat:
    if not isinstance(row, dict):
        raise FormatChangedError("The API returned an invalid format record.")
    reference = _reference(row)
    extension = _text(row.get("extension")).lower()
    if not re.fullmatch(r"[a-z0-9]{1,20}", extension):
        raise FormatChangedError("The API returned an invalid book format.")
    return BookFormat(reference.book_id, reference.book_hash, extension,
                      _text(row.get("filesizeString") or row.get("filesize")))
