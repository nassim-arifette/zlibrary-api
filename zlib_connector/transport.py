"""HTTPS transport with scoped cookies, bounded streaming and pinned public DNS."""
from contextlib import contextmanager
from dataclasses import dataclass
import http.client
from http.cookies import SimpleCookie
from http.cookiejar import CookieJar, DefaultCookiePolicy
import ipaddress
import io
import json
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request
from typing import BinaryIO, Callable

from .errors import ChallengeRequired, DownloadError, NetworkError, RateLimitError
from .transfer import CancellationToken, TransferProgress

HTML_LIMIT = 4 * 1024 * 1024
BOOK_LIMIT = 256 * 1024 * 1024
CHUNK_SIZE = 64 * 1024

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


@dataclass(frozen=True)
class _OpenedResponse:
    response: http.client.HTTPResponse
    connection: http.client.HTTPSConnection
    url: str
    started: float
    deadline: float
    sock: socket.socket | None


def _check_cancel(cancel):
    if cancel is not None:
        cancel.check()


def _remaining(deadline, cancel=None):
    _check_cancel(cancel)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise NetworkError("The request timed out.", reason="timeout")
    return remaining


def _network_call(operation, *, cancel=None, deadline=None):
    """Translate network failures only; sinks and callbacks run outside this scope."""
    def check_state():
        if deadline is None:
            _check_cancel(cancel)
        else:
            _remaining(deadline, cancel)

    check_state()
    try:
        return operation()
    except NetworkError:
        check_state()
        raise
    except ssl.SSLCertVerificationError:
        check_state()
        raise NetworkError("HTTPS certificate verification failed.", reason="certificate") from None
    except ssl.SSLError:
        check_state()
        raise NetworkError("The TLS connection failed.", reason="tls") from None
    except TimeoutError:
        check_state()
        raise NetworkError("The request timed out.", reason="timeout") from None
    except (OSError, http.client.HTTPException, ValueError):
        check_state()
        raise NetworkError("Network or TLS request failed. Check the domain and your connection.") from None


class _ReadDeadline:
    """Interrupt a socket even while http.client reads internal lines/trailers.

    A socket timeout limits individual reads, not repeated reads inside a header
    or chunk parser. One short-lived guard per response enforces the existing
    absolute deadline. Cancellation is checked at most 50 ms apart; no socket
    operation is retried, and the guard is stopped and joined before cleanup.
    """

    def __init__(self, sock, deadline, cancel):
        self._socket, self._deadline, self._cancel = sock, deadline, cancel
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._socket is not None:
            self._thread = threading.Thread(target=self._run, name="zlib-transfer-deadline", daemon=True)
            self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            remaining = self._deadline - time.monotonic()
            if remaining <= 0 or (self._cancel is not None and self._cancel.is_cancelled):
                try:
                    if isinstance(self._socket, socket.socket):
                        # Use the socket operation directly: SSLSocket.shutdown
                        # also mutates TLS parser state in the reading thread.
                        socket.socket.shutdown(self._socket, socket.SHUT_RDWR)
                    else:
                        self._socket.shutdown(socket.SHUT_RDWR)
                except (OSError, AttributeError):
                    pass  # The response may already have closed its socket.
                return
            self._stop.wait(min(remaining, 0.05) if self._cancel is not None else remaining)

    def close(self):
        self._stop.set()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join()


def _refresh_timeout(connection, deadline, cancel=None, *, sock=None):
    remaining = _remaining(deadline, cancel)
    active_socket = sock if sock is not None else connection.sock
    if active_socket is not None:
        _network_call(lambda: active_socket.settimeout(remaining), cancel=cancel, deadline=deadline)


def _header(response, name):
    values = response.headers.get_all(name, [])
    if len(values) > 1:
        raise DownloadError("The response contains ambiguous transfer headers.")
    return response.getheader(name)


def _body_length(response, limit, *, download):
    encoding = _header(response, "Content-Encoding")
    if encoding is not None and encoding.strip().lower() != "identity":
        raise DownloadError("Encoded responses are not supported.")
    transfer = _header(response, "Transfer-Encoding")
    if transfer is not None and transfer.strip().lower() != "chunked":
        raise DownloadError("The response uses an unsupported transfer encoding.")
    length = _header(response, "Content-Length")
    if transfer is not None and length is not None:
        raise DownloadError("The response contains ambiguous transfer headers.")
    if download and (response.status == 206 or _header(response, "Content-Range") is not None):
        raise DownloadError("The server returned a partial download without a range request.")
    if length is None:
        return None
    length = length.strip()
    if not re.fullmatch(r"[0-9]{1,12}", length):
        raise DownloadError("The response contains an invalid content length.")
    length = int(length)
    if length > limit:
        raise DownloadError("The response exceeds the configured size limit.")
    return length


def _write_chunk(target, chunk, cancel=None):
    remaining = memoryview(chunk)
    while remaining:
        _check_cancel(cancel)
        try:
            written = target.write(remaining)
        except OSError:
            raise DownloadError("Could not write downloaded data to the local destination.") from None
        if type(written) is not int or not 0 < written <= len(remaining):
            raise DownloadError("The local destination did not accept the downloaded data.")
        remaining = remaining[written:]
        _check_cancel(cancel)

class Transport:
    def __init__(self, timeout: float = 30, max_download_bytes: int = BOOK_LIMIT):
        if (type(timeout) not in (int, float) or not 0 < timeout <= 300
                or type(max_download_bytes) is not int or not 0 < max_download_bytes <= BOOK_LIMIT):
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

    def request(self, url: str, *, form: dict[str, str | list[str]] | None = None,
                json_body: dict | None = None, method: str | None = None,
                download: bool = False, follow_redirects: bool = True) -> Response:
        if form is not None and json_body is not None:
            raise ValueError("Choose a form or a JSON body, not both.")
        payload = urlencode(form, doseq=True).encode() if form is not None else None
        content_type = "application/x-www-form-urlencoded"
        if json_body is not None:
            payload = json.dumps(json_body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            # PAPI's browser client sends JSON via Request(string), with this MIME type.
            content_type = "text/plain;charset=UTF-8"
        method = method if method is not None else "POST" if payload is not None else "GET"
        if method not in ("GET", "POST", "PUT", "DELETE") or (method == "GET" and payload is not None):
            raise ValueError("Use GET without a body, or POST, PUT or DELETE.")
        with self._open_response(url, payload=payload, content_type=content_type, method=method,
                                 follow_redirects=follow_redirects) as opened:
            target = io.BytesIO()
            self._copy_response(opened, target, limit=self.max_download_bytes if download else HTML_LIMIT,
                                download=download)
            return Response(target.getvalue(), opened.url, opened.response.getheader("Content-Type", ""))

    def download_to(self, url: str, target: BinaryIO, *,
                    progress: Callable[[TransferProgress], None] | None = None,
                    cancel: CancellationToken | None = None) -> int:
        """Stream a complete response to a caller-owned binary destination.

        The destination can contain partial data on failure; its publication and
        cleanup belong to the caller. Callbacks run after headers and after each
        fully written chunk. No response body is accumulated or retried here.
        Active response reads are interrupted on cancellation or deadline expiry;
        DNS and connection establishment still use their platform/socket bounds.
        """
        _check_cancel(cancel)
        if not callable(getattr(target, "write", None)):
            raise TypeError("The download destination must be a writable binary stream.")
        if progress is not None and not callable(progress):
            raise TypeError("The progress callback must be callable.")
        with self._open_response(url, cancel=cancel) as opened:
            return self._copy_response(opened, target, limit=self.max_download_bytes,
                                       download=True, progress=progress, cancel=cancel)

    @contextmanager
    def _open_response(self, url, *, payload=None, content_type=None, method="GET",
                       follow_redirects=True, cancel=None):
        started = time.monotonic()
        deadline = started + self.timeout
        for _ in range(6):
            _remaining(deadline, cancel)
            validate_url(url)
            parsed = urlsplit(url)
            addresses = _network_call(lambda: public_addresses(parsed.hostname), cancel=cancel, deadline=deadline)
            remaining = _remaining(deadline, cancel)
            connection = _network_call(lambda: http.client.HTTPSConnection(
                parsed.hostname, timeout=remaining, context=ssl.create_default_context()), cancel=cancel, deadline=deadline)
            # TLS still checks the original hostname; the socket uses validated DNS answers.
            def connect(_address, timeout=remaining, source_address=None):
                last_error = OSError("No reachable public address")
                for address in addresses:
                    remaining_connect = _remaining(deadline, cancel)
                    try:
                        sock = socket.create_connection((address, 443), min(timeout, remaining_connect), source_address)
                    except OSError as error:
                        _check_cancel(cancel)
                        last_error = error
                    else:
                        try:
                            sock.settimeout(_remaining(deadline, cancel))
                        except BaseException:
                            sock.close()
                            raise
                        return sock
                raise last_error
            connection._create_connection = connect
            response = None
            guard = None
            try:
                request = Request(url, data=payload, method=method, headers={
                    "User-Agent": "ZlibConnector/0.1", "Accept": "*/*", "Accept-Encoding": "identity", "Connection": "close"})
                if payload is not None:
                    request.add_header("Content-Type", content_type)
                self.cookies.add_cookie_header(request)
                if parsed.path.startswith("/eapi/"):
                    scoped_cookies = SimpleCookie()
                    scoped_cookies.load(request.get_header("Cookie", ""))
                    for name in ("remix_userid", "remix_userkey"):
                        if name in scoped_cookies:
                            request.add_header(name.replace("_", "-"), scoped_cookies[name].value)
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                _refresh_timeout(connection, deadline, cancel)
                _network_call(lambda: connection.request(method, path, payload, dict(request.header_items())), cancel=cancel, deadline=deadline)
                _refresh_timeout(connection, deadline, cancel)
                # getresponse() can detach a Connection: close socket while its
                # response file still owns it. Retain it for per-read deadlines.
                active_socket = connection.sock
                guard = _ReadDeadline(active_socket, deadline, cancel)
                guard.start()
                response = _network_call(connection.getresponse, cancel=cancel, deadline=deadline)
                _network_call(lambda: self.cookies.extract_cookies(response, request), cancel=cancel, deadline=deadline)
                _check_cancel(cancel)
                if response.status in (301, 302, 303, 307, 308):
                    if not follow_redirects:
                        raise NetworkError("The account update returned a redirect; it was not followed.", reason="redirect")
                    location = response.getheader("Location")
                    if not location:
                        raise NetworkError("The site returned an invalid redirect.")
                    target = urljoin(url, location)
                    validate_url(target)
                    next_url = urlsplit(target)
                    same_origin = (parsed.scheme, parsed.hostname, parsed.port or 443) == (next_url.scheme, next_url.hostname, next_url.port or 443)
                    if method != "GET" and (not same_origin or response.status not in (307, 308)):
                        raise NetworkError("The request redirects to a different origin or flow. Set the current site domain explicitly; request data was not forwarded.", reason="redirect")
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
                yield _OpenedResponse(response, connection, url, started, deadline, active_socket)
                return
            finally:
                if guard is not None:
                    guard.close()
                close_response = getattr(response, "close", None)
                if close_response is not None:
                    try:
                        close_response()
                    except OSError:
                        pass
                try:
                    connection.close()
                except OSError:
                    pass
        raise ChallengeRequired("The site keeps redirecting. Browser verification may be required.")

    @staticmethod
    def _copy_response(opened, target, *, limit, download, progress=None, cancel=None):
        response, connection = opened.response, opened.connection
        total = _body_length(response, limit, download=download)
        received = 0

        def report():
            _remaining(opened.deadline, cancel)
            if progress is not None:
                progress(TransferProgress(bytes_received=received, total_bytes=total,
                                          elapsed_seconds=max(0.0, time.monotonic() - opened.started)))
            _remaining(opened.deadline, cancel)

        report()
        while True:
            # Respect HTTP framing: bytes after a completed Content-Length body
            # are not part of this response. http.client also marks completed
            # chunked/EOF-framed responses closed, even if the last read had data.
            if (total is not None and received == total) or getattr(response, "isclosed", lambda: False)():
                break
            _refresh_timeout(connection, opened.deadline, cancel, sock=opened.sock)
            # read1 yields between available blocks; the socket guard also
            # bounds the internal readline/_safe_read calls of chunked parsing.
            read = getattr(response, "read1", response.read)
            chunk = _network_call(lambda: read(min(CHUNK_SIZE, limit + 1 - received)),
                                  cancel=cancel, deadline=opened.deadline)
            _remaining(opened.deadline, cancel)
            if not chunk:
                break
            count = received + len(chunk)
            if count > limit:
                raise DownloadError("The response exceeds the configured size limit.")
            if total is not None and count > total:
                raise NetworkError("The response body does not match its declared length.")
            _write_chunk(target, chunk, cancel)
            received = count
            report()
        if total is not None and received != total:
            raise NetworkError("The download ended before its declared length was received.")
        return received
