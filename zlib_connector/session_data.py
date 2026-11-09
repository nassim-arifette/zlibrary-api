"""Validated authentication data; safe representations never reveal cookie values."""
from dataclasses import dataclass, field
import ipaddress
import re
import time
from urllib.parse import urlsplit


AUTH_COOKIE_NAMES = ("remix_userid", "remix_userkey")
_COOKIE_VALUE = re.compile(r"[!\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,4096}\Z")
_MAX_TIMESTAMP = 2**63 - 1


def canonical_origin(value: str) -> str:
    """Normalize an HTTPS origin without resolving it or accepting URL credentials."""
    try:
        if (not isinstance(value, str) or not 1 <= len(value) <= 2048
                or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)
                or "?" in value or "#" in value or "\\" in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.path not in ("", "/") or parsed.netloc.endswith(":")):
            raise ValueError
        hostname = parsed.hostname
        if "%" in hostname or hostname.endswith("."):
            raise ValueError
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            hostname = hostname.encode("idna").decode("ascii").lower()
            if len(hostname) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                          for label in hostname.split(".")):
                raise ValueError
        else:
            hostname = f"[{address.compressed}]" if address.version == 6 else address.compressed
        return f"https://{hostname}/"
    except (ValueError, UnicodeError, TypeError):
        raise ValueError("A session requires a valid HTTPS origin without credentials or a URL path.") from None


def _timestamp(value) -> bool:
    return type(value) is int and 0 <= value <= _MAX_TIMESTAMP


@dataclass(frozen=True)
class SessionCookie:
    name: str
    value: str = field(repr=False)
    path: str = "/"
    expires: int | None = None

    def __post_init__(self):
        if (self.name not in AUTH_COOKIE_NAMES or not isinstance(self.value, str)
                or _COOKIE_VALUE.fullmatch(self.value) is None
                or not isinstance(self.path, str) or not self.path.startswith("/") or len(self.path) > 2048
                or any(not 32 <= ord(character) <= 126 or character == ";" for character in self.path)
                or (self.expires is not None and not _timestamp(self.expires))):
            raise ValueError("The authentication cookie is not valid for session storage.")
        if self.name == "remix_userid" and (not re.fullmatch(r"[0-9]{1,20}", self.value) or int(self.value) == 0):
            raise ValueError("The authentication cookie is not valid for session storage.")


@dataclass(frozen=True)
class SavedSession:
    origin: str
    cookies: tuple[SessionCookie, ...] = field(repr=False)
    saved_at: int

    def __post_init__(self):
        origin = canonical_origin(self.origin)
        if (not isinstance(self.cookies, tuple) or len(self.cookies) != 2
                or any(not isinstance(cookie, SessionCookie) for cookie in self.cookies)
                or {cookie.name for cookie in self.cookies} != set(AUTH_COOKIE_NAMES)
                or not _timestamp(self.saved_at)):
            raise ValueError("The saved session must contain one validated authentication cookie pair and a timestamp.")
        object.__setattr__(self, "origin", origin)

    @property
    def expires_at(self) -> int | None:
        known = [cookie.expires for cookie in self.cookies if cookie.expires is not None]
        return min(known) if known else None

    @property
    def expired(self) -> bool:
        expires = self.expires_at
        return expires is not None and expires <= time.time()

    def status(self) -> dict:
        return {"state": "expired" if self.expired else "stored", "domain": self.origin,
                "saved_at": self.saved_at, "expires_at": self.expires_at, "verified": False}
