"""HTTPS transport with memory-only cookies, bounded reads and pinned public DNS."""
from dataclasses import dataclass
import http.client
from http.cookies import SimpleCookie
from http.cookiejar import CookieJar, DefaultCookiePolicy
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request

from .errors import ChallengeRequired, DownloadError, NetworkError, RateLimitError

HTML_LIMIT = 4 * 1024 * 1024
BOOK_LIMIT = 256 * 1024 * 1024

def validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme == "https" and bool(parsed.hostname) and parsed.port in (None, 443) and parsed.username is None and parsed.password is None
    except ValueError:
        valid = False
    if not valid:
        raise NetworkError("Use an HTTPS URL without embedded credentials or a custom port.")

def public_addresses(host: str) -> list[str]:
    try:
        addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    except OSError:
        raise NetworkError("DNS resolution failed. Check the configured domain.", reason="dns") from None
    if not addresses:
        raise NetworkError("The domain has no reachable addresses.", reason="dns")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise NetworkError("Private, reserved and local network destinations are not allowed.", reason="blocked_address")
    return addresses

@dataclass(frozen=True)
class Response:
    body: bytes
    url: str
    content_type: str = ""

class Transport:
    def __init__(self, timeout: float = 30, max_download_bytes: int = BOOK_LIMIT):
        if not 0 < timeout <= 300 or not 0 < max_download_bytes <= BOOK_LIMIT:
            raise ValueError("Timeout must be 0–300 seconds and download limit at most 256 MiB.")
        self.timeout = timeout
        self.max_download_bytes = max_download_bytes
        self.cookies = CookieJar(policy=DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrict))

    def close(self) -> None:
        self.cookies.clear()

    @property
    def authenticated(self) -> bool:
        values = {cookie.name: cookie.value for cookie in self.cookies}
        return bool(values.get("remix_userkey")) and values.get("remix_userid", "0") not in ("", "0")

    def request(self, url: str, *, form: dict[str, str | list[str]] | None = None, download: bool = False) -> Response:
        deadline = time.monotonic() + self.timeout
        for _ in range(6):
            validate_url(url)
            parsed = urlsplit(url)
            addresses = public_addresses(parsed.hostname)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NetworkError("The request timed out.", reason="timeout")
            connection = http.client.HTTPSConnection(parsed.hostname, timeout=remaining, context=ssl.create_default_context())
            # TLS still checks the original hostname; the socket uses validated DNS answers.
            def connect(_address, timeout=remaining, source_address=None):
                for address in addresses:
                    try:
                        return socket.create_connection((address, 443), timeout, source_address)
                    except OSError:
                        continue
                raise OSError("No reachable public address")
            connection._create_connection = connect
            payload = urlencode(form, doseq=True).encode() if form is not None else None
            request = Request(url, data=payload, headers={"User-Agent": "ZlibConnector/0.1", "Accept": "*/*", "Accept-Encoding": "identity"})
            if payload is not None:
                request.add_header("Content-Type", "application/x-www-form-urlencoded")
            self.cookies.add_cookie_header(request)
            if parsed.path.startswith("/eapi/"):
                scoped_cookies = SimpleCookie()
                scoped_cookies.load(request.get_header("Cookie", ""))
                for name in ("remix_userid", "remix_userkey"):
                    if name in scoped_cookies:
                        request.add_header(name.replace("_", "-"), scoped_cookies[name].value)
            try:
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                connection.request("POST" if form is not None else "GET", path, payload, dict(request.header_items()))
                response = connection.getresponse()
                self.cookies.extract_cookies(response, request)
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise NetworkError("The site returned an invalid redirect.")
                    target = urljoin(url, location)
                    validate_url(target)
                    next_url = urlsplit(target)
                    same_origin = (parsed.scheme, parsed.hostname, parsed.port or 443) == (next_url.scheme, next_url.hostname, next_url.port or 443)
                    if form is not None and (not same_origin or response.status not in (307, 308)):
                        raise NetworkError("The request redirects to a different origin or flow. Set the current site domain explicitly; form data was not forwarded.", reason="redirect")
                    followup = Request(target)
                    self.cookies.add_cookie_header(followup)
                    if target == url and followup.get_header("Cookie") == request.get_header("Cookie"):
                        raise ChallengeRequired("The site redirects to the same URL without establishing a session. Browser verification may be required.")
                    url = target
                    continue
                if response.status == 429:
                    raise RateLimitError("The site rate limit was reached. Try again later.")
                if response.status in (513, 517):
                    raise ChallengeRequired("DiamWall blocks API access on this domain. Check another Z-Library domain explicitly with --domain URL check before logging in.")
                if response.status in (403, 503):
                    raise ChallengeRequired("The site refused access or requires browser verification.")
                if response.status < 200 or response.status >= 300:
                    raise NetworkError(f"The site returned HTTP {response.status}.", reason="http")
                limit = self.max_download_bytes if download else HTML_LIMIT
                length = response.getheader("Content-Length")
                if length and (not length.isdigit() or int(length) > limit):
                    raise DownloadError("The response exceeds the configured size limit.")
                chunks = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise NetworkError("The request timed out.", reason="timeout")
                    if connection.sock:
                        connection.sock.settimeout(remaining)
                    chunk = response.read(min(64 * 1024, limit + 1 - len(chunks)))
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if len(chunks) > limit:
                        raise DownloadError("The response exceeds the configured size limit.")
                if length and len(chunks) != int(length):
                    raise NetworkError("The download was interrupted. No file was saved.")
                return Response(bytes(chunks), url, response.getheader("Content-Type", ""))
            except ssl.SSLCertVerificationError:
                raise NetworkError("HTTPS certificate verification failed.", reason="certificate") from None
            except ssl.SSLError:
                raise NetworkError("The TLS connection failed.", reason="tls") from None
            except TimeoutError:
                raise NetworkError("The request timed out.", reason="timeout") from None
            except (OSError, http.client.HTTPException, ValueError):
                raise NetworkError("Network or TLS request failed. Check the domain and your connection.") from None
            finally:
                connection.close()
        raise ChallengeRequired("The site keeps redirecting. Browser verification may be required.")
