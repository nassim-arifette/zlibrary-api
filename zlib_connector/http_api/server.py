"""Bounded loopback HTTP transport for one caller-owned, serialized Client.

This server deliberately does not accept account credentials over HTTP, perform
downloads, or expose local files. It is not an Internet-facing web server.
"""

from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import re
import socket
import threading
from time import monotonic
from urllib.parse import parse_qsl, urlsplit

from ..diagnostics import KNOWN_DOMAINS, diagnose_domains, _origin
from ..errors import (AuthenticationError, BooklistNotFoundError, ChallengeRequired, ConnectorError, DownloadError,
                      FormatChangedError, MutationUncertainError, NetworkError, RateLimitError)
from ..search import SearchFilters
from ..my_library import validate_tags
from .contract import ENDPOINTS, RequestError, openapi_document, resolve, validate_fields


MAX_TARGET = 8192
MAX_QUERY = 4096
MAX_HEADERS = 16384
MAX_BODY = 65536
_HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def _finish_response(connection):
    """Half-close output, then discard bounded late input before a full close.

    Closing with an arriving, unread request body can reset TCP and erase the
    error response on Windows. Discard bytes only: never parse another request.
    """
    try:
        connection.shutdown(socket.SHUT_WR)
        remaining = MAX_TARGET + MAX_HEADERS + MAX_BODY
        deadline = monotonic() + 0.1
        while remaining > 0:
            wait = deadline - monotonic()
            if wait <= 0:
                break
            connection.settimeout(wait)
            chunk = connection.recv(min(4096, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
    except OSError:
        pass


def _public_result(value):
    if isinstance(value, (list, tuple)):
        return [_public_result(item) for item in value]
    return value.to_dict()


def _upstream_error(error, *, mutation):
    if isinstance(error, BooklistNotFoundError):
        return RequestError(404, "booklist_not_found", "The booklist was not found in the account's editable lists.")
    if isinstance(error, MutationUncertainError):
        return RequestError(409, "mutation_uncertain", "The server may have applied this change. Check the account before trying again.", outcome="unknown")
    if isinstance(error, AuthenticationError):
        return RequestError(424, "upstream_authentication_required", "The Z-Library session is missing or expired.")
    if isinstance(error, ChallengeRequired):
        return RequestError(502, "upstream_challenge", "Z-Library requires browser verification.")
    if isinstance(error, RateLimitError):
        return RequestError(429, "upstream_rate_limited", "Z-Library rejected the request because of a limit.", retryable=not mutation)
    if isinstance(error, NetworkError):
        if mutation:
            return _upstream_error(MutationUncertainError(), mutation=True)
        timeout = error.reason == "timeout"
        return RequestError(504 if timeout else 502, "upstream_timeout" if timeout else "upstream_network_error",
                            "The Z-Library request could not be completed.", retryable=True)
    if isinstance(error, FormatChangedError):
        if mutation:
            return _upstream_error(MutationUncertainError(), mutation=True)
        return RequestError(502, "upstream_protocol_changed", "Z-Library returned an unsupported response.")
    if isinstance(error, DownloadError):
        return RequestError(422, "upstream_refused", "Z-Library refused the operation.")
    if mutation:
        return _upstream_error(MutationUncertainError(), mutation=True)
    return RequestError(502 if isinstance(error, ConnectorError) else 500,
                        "upstream_error" if isinstance(error, ConnectorError) else "internal_error",
                        "The request could not be completed.")


def _allowed_origin(value):
    if not isinstance(value, str) or not value.isascii() or len(value) > 2048:
        raise ValueError("Allowed origins must be explicit HTTP or HTTPS origins.")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("Allowed origins must be explicit HTTP or HTTPS origins.") from None
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.path or parts.query or parts.fragment
            or "*" in value or "\\" in value or not value.isprintable()):
        raise ValueError("Allowed origins must be explicit HTTP or HTTPS origins without paths.")
    hostname = parts.hostname
    if ":" in hostname:
        hostname = "[" + hostname + "]"
    default_port = 80 if parts.scheme == "http" else 443
    return parts.scheme + "://" + hostname.lower() + (f":{port}" if port is not None and port != default_port else "")


class HttpApiServer(ThreadingHTTPServer):
    """A loopback server. Close it before closing its injected Client.

    All Client operations (including multi-step mutations) share one lock. An
    overloaded connection receives 503. The server owns neither Client lifetime
    nor account/token files. Use its context manager and call shutdown from a
    thread other than the one running serve_forever.
    """

    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True
    request_queue_size = 8

    def __init__(self, client, *, token, host="127.0.0.1", port=8000, allowed_origins=(),
                 diagnostics=diagnose_domains, max_connections=8, request_timeout=5):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", token):
            raise ValueError("The API bearer token must contain 32 to 256 URL-safe ASCII characters.")
        host = "127.0.0.1" if host == "localhost" else host
        try:
            address = ipaddress.IPv4Address(host)
        except (ValueError, TypeError):
            raise ValueError("Bind the API to localhost or an IPv4 loopback address.") from None
        if not address.is_loopback:
            raise ValueError("The HTTP API currently supports loopback addresses only.")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("The API port must be between 0 and 65535.")
        if type(max_connections) is not int or not 1 <= max_connections <= 32:
            raise ValueError("The API connection limit must be between 1 and 32.")
        if type(request_timeout) not in (int, float) or not 0 < request_timeout <= 30:
            raise ValueError("The request timeout must be between 0 and 30 seconds.")
        if isinstance(allowed_origins, str):
            raise ValueError("Pass allowed origins as a sequence of origins.")
        origins = tuple(allowed_origins)
        if len(origins) > 20:
            raise ValueError("At most 20 browser origins can be allowed.")
        self.allowed_origins = frozenset(_allowed_origin(origin) for origin in origins)
        self._client, self._token, self._diagnostics = client, token, diagnostics
        self.request_timeout = request_timeout
        self._client_lock = threading.Lock()
        self._connections = threading.BoundedSemaphore(max_connections)
        super().__init__((str(address), port), _Handler)
        actual_port = self.server_address[1]
        suffix = "" if actual_port == 80 else f":{actual_port}"
        self.allowed_hosts = frozenset((str(address) + suffix, "localhost" + suffix))

    def process_request(self, request, client_address):
        if not self._connections.acquire(blocking=False):
            try:
                request.settimeout(0.1)
                data = json.dumps(RequestError(503, "server_busy", "The local API is busy.", retryable=True).to_dict()).encode()
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nCache-Control: no-store\r\nConnection: close\r\nContent-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data)
            except OSError:
                pass
            finally:
                _finish_response(request)
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()

    def handle_error(self, request, client_address):
        # BaseServer prints tracebacks. Neither incoming bytes nor upstream errors
        # belong in logs, and request handlers already return safe JSON failures.
        pass

    @contextmanager
    def _access_client(self):
        if not self._client_lock.acquire(timeout=self.request_timeout):
            raise RequestError(503, "client_busy", "Another request is using the account session.", retryable=True)
        try:
            yield self._client
        finally:
            self._client_lock.release()

    def execute(self, route, path, query, body):
        operation = route.operation
        if operation == "health":
            return {"status": "ok", "api_version": "v1", "upstream_status": "not_checked"}
        if operation == "capabilities":
            return {"api_version": "v1", "authentication": "bearer", "client_access": "serialized",
                    "operations": [endpoint.operation for endpoint in ENDPOINTS],
                    "unavailable": ["http_login", "http_downloads", "native_my_library_pages", "local_download_history", "uploads"]}
        if operation == "openapi":
            return openapi_document()
        if operation == "domains":
            return {"domains": list(KNOWN_DOMAINS), "probed": False}
        reference = f"{path['book_id']}:{path['book_hash']}" if "book_id" in path else None
        # Validate cross-field constraints before acquiring the account lock or
        # initiating any upstream operation.
        filters = None
        if operation == "search":
            try:
                filters = SearchFilters(languages=query.get("language", ()), formats=query.get("format", ()),
                    year_from=query.get("year_from"), year_to=query.get("year_to"), order=query.get("order"))
            except ValueError:
                raise RequestError(400, "invalid_argument", "The search filter combination is invalid.") from None
        if operation == "diagnostics" and "domains" in body:
            try:
                body["domains"] = tuple(_origin(domain) for domain in body["domains"])
            except (ValueError, ConnectorError):
                raise RequestError(400, "invalid_argument", "Domains must be public HTTPS site origins.") from None
        if operation in ("book_tags_add", "book_tags_remove"):
            try:
                body["tags"] = validate_tags(body["tags"], for_removal=operation == "book_tags_remove")
            except ValueError:
                raise RequestError(400, "invalid_argument", "The My Library tags are invalid.") from None
        with self._access_client() as client:
            try:
                if operation == "diagnostics":
                    result = self._diagnostics(**body)
                elif operation == "search":
                    result = client.search_page(query["q"], page=query["page"], limit=query["limit"], filters=filters)
                elif operation in ("book", "formats", "similar"):
                    result = getattr(client, {"book": "get_book", "formats": "get_formats", "similar": "get_similar"}[operation])(reference)
                elif operation in ("profile", "quota", "library", "favorites", "history", "booklists", "popular", "recommended"):
                    method = "get_recommendations" if operation == "recommended" else "get_" + operation
                    result = getattr(client, method)(**query)
                elif operation == "library_tags":
                    result = client.get_library_tags()
                elif operation == "saved_book_ids":
                    return {"book_ids": list(client.get_saved_book_ids())}
                elif operation in ("book_tags_add", "book_tags_remove"):
                    method = client.add_book_tags if operation == "book_tags_add" else client.remove_book_tags
                    result = method(reference, body["tags"])
                elif operation in ("favorite_add", "favorite_remove"):
                    result = client.set_favorite(reference, saved=operation == "favorite_add")
                elif operation == "booklist_create":
                    result = client.create_booklist(**body)
                elif operation == "booklist_update":
                    result = client.update_booklist(path["booklist_id"], **body)
                elif operation == "booklist_delete":
                    result = client.delete_booklist(path["booklist_id"])
                elif operation == "booklist_books":
                    result = client.get_booklist_books(path["booklist_id"], **query)
                elif operation == "booklist_add":
                    result = client.add_booklist_book(path["booklist_id"], body["reference"])
                elif operation == "booklist_remove":
                    result = client.remove_booklist_book(path["booklist_id"], path["entry_id"])
                else:
                    raise RuntimeError("Unimplemented HTTP operation.")
                return _public_result(result)
            except RequestError:
                raise
            except Exception as error:
                raise _upstream_error(error, mutation=route.mutation) from None


class _Handler(BaseHTTPRequestHandler):
    """Parse a single bounded HTTP/1 request, then always close its connection."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _receive(self, maximum):
        remaining = self._deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError
        self.connection.settimeout(remaining)
        chunk = self.rfile.read1(maximum)
        if not chunk:
            raise RequestError(400, "invalid_request", "The HTTP request is incomplete.")
        return chunk

    def _line(self, maximum, status):
        while b"\n" not in self._buffer:
            if len(self._buffer) > maximum:
                raise RequestError(status, "request_too_large", "The HTTP request exceeds the permitted size.")
            self._buffer += self._receive(min(4096, maximum + 1 - len(self._buffer)))
        line, self._buffer = self._buffer.split(b"\n", 1)
        if len(line) + 1 > maximum:
            raise RequestError(status, "request_too_large", "The HTTP request exceeds the permitted size.")
        if not line.endswith(b"\r"):
            raise RequestError(400, "invalid_request", "HTTP lines must end with CRLF.")
        return line[:-1]

    def _request(self):
        line = self._line(MAX_TARGET, 414)
        try:
            command, target, version = line.decode("ascii").split(" ")
        except (UnicodeDecodeError, ValueError):
            raise RequestError(400, "invalid_request", "The HTTP request line is invalid.") from None
        if not re.fullmatch(r"[A-Z]{1,20}", command) or version not in ("HTTP/1.0", "HTTP/1.1"):
            raise RequestError(400, "invalid_request", "The HTTP method or version is invalid.")
        if not target.startswith("/") or target.startswith("//") or "#" in target or "\\" in target:
            raise RequestError(400, "invalid_request", "Use an origin-relative API request target.")
        path, _, raw_query = target.partition("?")
        if len(path) > 1024 or len(raw_query) > MAX_QUERY:
            raise RequestError(414, "request_too_large", "The API path or query exceeds the permitted size.")
        if "%" in path or any(ord(char) < 33 or ord(char) > 126 for char in target):
            raise RequestError(400, "invalid_request", "The API request target is invalid.")
        if re.search(r"%(?![0-9A-Fa-f]{2})", raw_query):
            raise RequestError(400, "invalid_request", "The query encoding is invalid.")
        headers, size = {}, 0
        for _ in range(65):
            header = self._line(MAX_HEADERS - size, 431)
            size += len(header) + 2
            if size > MAX_HEADERS:
                raise RequestError(431, "request_too_large", "The HTTP headers exceed the permitted size.")
            if not header:
                break
            name, colon, value = header.partition(b":")
            if not colon or not _HEADER_NAME.fullmatch(name) or any(byte < 32 and byte != 9 or byte == 127 for byte in value):
                raise RequestError(400, "invalid_request", "An HTTP header is invalid.")
            name = name.decode("ascii").lower()
            if name in headers:
                raise RequestError(400, "invalid_request", "Duplicate HTTP headers are not supported.")
            headers[name] = value.decode("latin-1").strip(" \t")
        else:
            raise RequestError(431, "request_too_large", "The request contains too many headers.")
        if headers.get("host", "").lower() not in self.server.allowed_hosts:
            raise RequestError(403, "host_not_allowed", "The Host header does not identify this local API.")
        origin = headers.get("origin")
        if origin is not None:
            if origin not in self.server.allowed_origins:
                raise RequestError(403, "origin_not_allowed", "This browser origin is not allowed.")
            self._origin = origin
        if "transfer-encoding" in headers or "content-encoding" in headers:
            raise RequestError(400, "invalid_request", "Transfer and content encodings are not supported.")
        if "expect" in headers:
            raise RequestError(417, "expectation_failed", "HTTP expectations are not supported.")
        length_text = headers.get("content-length", "0")
        if not re.fullmatch(r"[0-9]{1,10}", length_text):
            raise RequestError(400, "invalid_request", "The Content-Length header is invalid.")
        length = int(length_text)
        if length > MAX_BODY:
            raise RequestError(413, "request_too_large", "The JSON request body exceeds the permitted size.")
        if command == "OPTIONS":
            if not origin or length or raw_query:
                raise RequestError(403, "preflight_rejected", "An explicit allowed browser origin is required for preflight.")
            requested = headers.get("access-control-request-method", "")
            resolve(requested, path)
            requested_headers = [value.strip().lower() for value in headers.get("access-control-request-headers", "").split(",") if value.strip()]
            if set(requested_headers) - {"authorization", "content-type"}:
                raise RequestError(403, "preflight_rejected", "The requested browser headers are not allowed.")
            return None, None, None, {"Access-Control-Allow-Methods": requested, "Access-Control-Allow-Headers": "Authorization, Content-Type"}
        authorization = headers.get("authorization", "")
        parts = authorization.split(" ")
        if (len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].isascii()
                or not hmac.compare_digest(parts[1], self.server._token)):
            raise RequestError(401, "unauthorized", "A valid API bearer token is required.", headers={"WWW-Authenticate": "Bearer"})
        route, path_values = resolve(command, path)
        try:
            pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=True, encoding="utf-8", errors="strict", max_num_fields=128)
        except (ValueError, UnicodeDecodeError):
            raise RequestError(400, "invalid_argument", "The query parameters are invalid.") from None
        query = {}
        for name, value in pairs:
            query.setdefault(name, []).append(value)
        query = validate_fields(query, route.query, query=True)
        body = None
        if route.body is None:
            if length:
                raise RequestError(400, "invalid_request", "This route does not accept a request body.")
        else:
            content_type = headers.get("content-type", "").lower().replace(" ", "")
            if content_type not in ("application/json", "application/json;charset=utf-8"):
                raise RequestError(415, "unsupported_media_type", "Use application/json with UTF-8 encoding.")
            if not length:
                raise RequestError(400, "invalid_request", "A JSON object body is required.")
            raw = self._buffer[:length]
            self._buffer = self._buffer[length:]
            while len(raw) < length:
                raw += self._receive(min(4096, length - len(raw)))
            try:
                body = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            except (ValueError, UnicodeDecodeError, RecursionError):
                raise RequestError(400, "invalid_request", "The request body is not a valid JSON object.") from None
            body = validate_fields(body, route.body)
            if route.partial and not body:
                raise RequestError(400, "invalid_argument", "Choose at least one setting to update.")
        return route, path_values, query, body

    def handle_one_request(self):
        self.close_connection = True
        self._origin = None
        self._buffer = b""
        self._deadline = monotonic() + self.server.request_timeout
        route = None
        try:
            route, path, query, body = self._request()
            if route is None:
                self._send(204, None, body)
            else:
                self._send(route.status, self.server.execute(route, path, query, body))
        except RequestError as error:
            self._send(error.status, error.to_dict(), error.headers)
        except (TimeoutError, socket.timeout):
            self._send(408, RequestError(408, "request_timeout", "The HTTP request took too long.").to_dict())
        except (ConnectionError, OSError):
            pass
        except Exception as error:
            failure = _upstream_error(error, mutation=route is not None and route.mutation)
            self._send(failure.status, failure.to_dict())
        finally:
            _finish_response(self.connection)

    def _send(self, status, payload, headers=None):
        data = b"" if payload is None else json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.connection.settimeout(self.server.request_timeout)
        reason = self.responses.get(status, ("Error",))[0]
        response_headers = {"Content-Type": "application/json; charset=utf-8", "Content-Length": str(len(data)),
                            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Connection": "close", "Vary": "Origin"}
        if self._origin:
            response_headers["Access-Control-Allow-Origin"] = self._origin
        response_headers.update(headers or {})
        output = f"HTTP/1.1 {status} {reason}\r\n" + "".join(f"{name}: {value}\r\n" for name, value in response_headers.items()) + "\r\n"
        try:
            self.wfile.write(output.encode("ascii") + data)
            self.wfile.flush()
        except OSError:
            pass


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON field.")
        result[name] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON number.")


def create_server(client, *, token, host="127.0.0.1", port=8000, allowed_origins=(),
                  diagnostics=diagnose_domains, max_connections=8, request_timeout=5):
    """Create a loopback API without taking ownership of the Client or secrets."""
    return HttpApiServer(client, token=token, host=host, port=port, allowed_origins=allowed_origins,
                         diagnostics=diagnostics, max_connections=max_connections, request_timeout=request_timeout)
