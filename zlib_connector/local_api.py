"""Bounded, authenticated client for an already running loopback HTTP API."""
from contextlib import contextmanager
from functools import partial
import http.client
import ipaddress
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlencode, urlsplit

from .api_token import read_api_token
from .book_files import save_book
from .error_codes import Coded, ErrorCode
from .errors import ConnectorError, NetworkError
from .jobs.events import validate_event_id
from .jobs.models import TERMINAL_STATES, validate_task_id as task_id
from .network_deadline import ReadDeadline
from .local_http import CancellableResponse
from .sse import SSEDecoder, event_json, invalid_stream, validate_public_event
from .transport import BOOK_LIMIT, CHUNK_SIZE
from .transfer import CancellationToken


JSON_LIMIT = 2 * 1024 * 1024
# The local server answers 414 to a longer encoded query string.
MAX_QUERY_LENGTH = 4096
_FILE_TYPES = {"application/pdf": "pdf", "application/epub+zip": "epub", "image/vnd.djvu": "djvu"}
_STATUS_MESSAGES = {
    400: "The local API rejected the request. Check its arguments.",
    401: "The local API rejected the bearer token. Use the server's token file.",
    403: "The local API refused this client.",
    404: "The download task was not found.",
    409: "The task conflicts with this request or its file is not ready.",
    410: "The task's completed file is missing or has changed.",
    503: "Download tasks are disabled, busy or at capacity. Check the server.",
}


ERROR_BODY_LIMIT = 64 * 1024
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class LocalAPIError(ConnectorError, Coded):
    """A local API failure with a safe message, never a reflected response body.

    ``code``, ``retryable`` and ``outcome`` come from the server's error object when
    they are valid; the server's message is never kept.
    """

    def __init__(self, status, code=None, retryable=None, outcome=None):
        self.status = status
        self.code, self.retryable, self.outcome = code, retryable, outcome
        super().__init__(_STATUS_MESSAGES.get(status, "The local API could not complete the request."))

    def classification(self) -> ErrorCode:
        return ErrorCode(self.code or "local_api_error", retryable=self.retryable is True,
                         outcome=self.outcome or "not_applied")


def error_details(data) -> dict:
    """The valid code, retryable and outcome of an HTTP API error body, or nothing."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > ERROR_BODY_LIMIT:
        return {}
    try:
        value = json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_unique_json_object,
                           parse_constant=_invalid_json_constant)
    except (ValueError, UnicodeError, RecursionError):
        return {}
    error = value.get("error") if isinstance(value, dict) else None
    if not isinstance(error, dict):
        return {}
    details = {}
    code = error.get("code")
    if isinstance(code, str) and _ERROR_CODE.fullmatch(code):
        details["code"] = code
    if type(error.get("retryable")) is bool:
        details["retryable"] = error["retryable"]
    if error.get("outcome") in ("not_applied", "unknown"):
        details["outcome"] = error["outcome"]
    return details


def _address(value):
    try:
        if (not isinstance(value, str) or not value.isascii() or len(value) > 2048
                or any(character.isspace() or ord(character) < 32 for character in value)
                or any(character in value for character in "\\?#")):
            raise ValueError
        parts = urlsplit(value)
        host = "127.0.0.1" if parts.hostname == "localhost" else parts.hostname
        address = ipaddress.IPv4Address(host)
        port = parts.port or 80
        if (parts.scheme != "http" or not address.is_loopback or parts.username is not None
                or parts.password is not None or parts.path not in ("", "/", "/v1", "/v1/")
                or parts.netloc.endswith(":") or parts.port == 0):
            raise ValueError
        return str(address), port
    except (ValueError, TypeError):
        raise ValueError("Use an HTTP API URL on localhost or an IPv4 loopback address, optionally ending in /v1.") from None


class LocalAPI:
    """No redirects, proxy environment, credential prompts or automatic retries."""

    def __init__(self, url, token_file, *, timeout=30):
        self._host, self._port = _address(url)
        if type(timeout) not in (int, float) or not 0 < timeout <= 300:
            raise ValueError("The local API timeout must be between 0 and 300 seconds.")
        self._timeout = timeout
        self._token = read_api_token(token_file)

    @staticmethod
    def _validate_operation(method, path):
        if method not in ("GET", "POST") or re.fullmatch(r"/downloads(?:/[a-z0-9]+(?:/[a-z]+)?)?", path) is None:
            raise ValueError("Unsupported local task operation.")

    @staticmethod
    def _status_error(status, **details):
        return LocalAPIError(status, **details)

    @contextmanager
    def _response(self, method, path, *, body=None, query=None, stream_duration=None, stop_at=None,
                  cancel: CancellationToken | None = None):
        self._validate_operation(method, path)
        if cancel is not None:
            cancel.check()
        target = "/v1" + path
        if query:
            encoded = urlencode(query)
            if len(encoded) > MAX_QUERY_LENGTH:
                raise ValueError("The local API request exceeds its size limit.")
            target += "?" + encoded
        data = json.dumps(body, ensure_ascii=True).encode("ascii") if body is not None else None
        if data is not None and len(data) > 65536:
            raise ValueError("The local API request exceeds its size limit.")
        headers = {"Authorization": "Bearer " + self._token, "Connection": "close", "Accept-Encoding": "identity"}
        if stream_duration is not None:
            headers["Accept"] = "text/event-stream"
        if data is not None:
            headers["Content-Type"] = "application/json"
        deadline = time.monotonic() + self._timeout + (stream_duration or 0)
        if stop_at is not None:
            deadline = min(deadline, stop_at)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._network_error(deadline, cancel)
        connection = http.client.HTTPConnection(self._host, self._port, timeout=min(self._timeout, remaining))
        if cancel is not None:
            connection.response_class = partial(CancellableResponse, deadline=deadline, cancel=cancel)
        guard = None
        response = None
        try:
            try:
                connection.connect()
                if cancel is not None:
                    cancel.check()
                # getresponse() can detach a close-delimited socket from the
                # connection; retain it while the response owns the stream.
                connected_socket = connection.sock
                guard = ReadDeadline(connected_socket, deadline, cancel)
                guard.start()
                connection.request(method, target, body=data, headers=headers)
                response = connection.getresponse()
            except (OSError, http.client.HTTPException):
                self._network_error(deadline, cancel)
            if cancel is not None:
                cancel.check()
            if not 200 <= response.status < 300:
                raise self._status_error(response.status, **self._error_body(response))
            if stream_duration is not None:
                self._event_framing(response)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._network_error(deadline)
                if connected_socket is not None:
                    # Valid SSE may be silent until its heartbeat or duration
                    # end. Headers use the ordinary timeout; stream reads use
                    # their absolute budget, still enforced by the guard.
                    try:
                        connected_socket.settimeout(remaining)
                    except OSError:
                        self._network_error(deadline)
                yield response, None, deadline
            else:
                lengths = response.headers.get_all("Content-Length", [])
                encodings = response.headers.get_all("Content-Encoding", [])
                if (len(lengths) != 1 or re.fullmatch(r"[0-9]{1,12}", lengths[0]) is None
                        or response.headers.get("Transfer-Encoding") is not None
                        or (encodings and (len(encodings) != 1 or encodings[0].lower() != "identity"))):
                    raise NetworkError("The local API returned unsupported response framing.")
                yield response, int(lengths[0]), deadline
        finally:
            if guard is not None:
                guard.close()
            if response is not None:
                response.close()
            connection.close()

    @staticmethod
    def _error_body(response) -> dict:
        """Read at most 64 KiB of an error body within the request deadline; never fail on it."""
        try:
            data = response.read(ERROR_BODY_LIMIT + 1)
        except (AttributeError, OSError, http.client.HTTPException, ValueError):
            return {}
        return error_details(data)

    @staticmethod
    def _event_framing(response):
        headers = response.headers
        content_types = headers.get_all("Content-Type", [])
        encodings = headers.get_all("Content-Encoding", [])
        connections = headers.get_all("Connection", [])
        if (response.status != 200 or headers.get("Content-Length") is not None
                or headers.get("Transfer-Encoding") is not None
                or len(content_types) != 1
                or re.fullmatch(r'text/event-stream\s*;\s*charset=(?:utf-8|"utf-8")', content_types[0], re.I) is None
                or (encodings and (len(encodings) != 1 or encodings[0].lower() != "identity"))
                or len(connections) != 1 or connections[0].lower() != "close"):
            raise NetworkError("The local API returned unsupported event stream framing.")

    def events(self, identifier, *, after=None, duration=30):
        """Observe one bounded stream. Closing the iterator releases its socket.

        A normal end means an explicit duration end or a terminal observation.
        Unexpected EOF and an unfinished server shutdown raise NetworkError.
        """
        yield from self._events(identifier, after=after, duration=duration)

    def _events(self, identifier, *, after=None, duration=30, stop_at=None):
        identifier = task_id(identifier)
        if after is not None:
            validate_event_id(after)
        if type(duration) not in (int, float) or not 1 <= duration <= 60 or not math.isfinite(duration):
            raise ValueError("Choose an event stream duration between 1 and 60 seconds.")
        # Query numbers have a bounded decimal grammar. Monotonic-clock
        # subtraction can produce a longer float representation; keep the
        # original value for deadlines and bound only its wire encoding.
        query = {"duration": format(duration, ".9f").rstrip("0").rstrip(".")}
        if after is not None:
            query["after"] = after
        decoder = SSEDecoder()
        with self._response("GET", f"/downloads/{identifier}/events", query=query,
                            stream_duration=duration, stop_at=stop_at) as (response, _, deadline):
            while True:
                if time.monotonic() >= deadline:
                    self._network_error(deadline)
                try:
                    chunk = response.read1(16 * 1024)
                except (OSError, http.client.HTTPException):
                    self._network_error(deadline)
                if not chunk:
                    if time.monotonic() >= deadline:
                        self._network_error(deadline)
                    raise NetworkError("The local API event stream ended unexpectedly.")
                for name, cursor, data in decoder.feed(chunk):
                    value = event_json(data)
                    if name == "end":
                        if cursor is not None or set(value) != {"reason"}:
                            invalid_stream()
                        if value["reason"] == "duration":
                            return
                        if value["reason"] == "shutdown":
                            raise NetworkError("The local API stopped while the observed task was unfinished.")
                        # A terminal observation completes the stream immediately;
                        # a terminal end without that observation is inconsistent.
                        invalid_stream()
                    if name != "task":
                        invalid_stream()
                    value = validate_public_event(value, identifier=identifier, cursor=cursor)
                    terminal = value["task"]["state"] in TERMINAL_STATES
                    yield value
                    if terminal:
                        return

    @staticmethod
    def _network_error(deadline, cancel=None):
        if cancel is not None:
            cancel.check()
        if time.monotonic() >= deadline:
            raise NetworkError("The local API request timed out.", reason="timeout") from None
        raise NetworkError("Could not contact the local API. Start zlib-cli serve and check its address.") from None

    @staticmethod
    def _copy(response, size, deadline, writer, *, cancel=None):
        remaining = size
        while remaining:
            if cancel is not None:
                cancel.check()
            if time.monotonic() >= deadline:
                raise NetworkError("The local API request timed out.", reason="timeout")
            try:
                chunk = response.read1(min(CHUNK_SIZE, remaining))
            except (OSError, http.client.HTTPException):
                LocalAPI._network_error(deadline, cancel)
            if cancel is not None:
                cancel.check()
            if time.monotonic() >= deadline:
                LocalAPI._network_error(deadline, cancel)
            if not chunk:
                if time.monotonic() >= deadline:
                    LocalAPI._network_error(deadline)
                raise NetworkError("The local API response ended before the file was complete.")
            writer(chunk)
            remaining -= len(chunk)

    def _request_json(self, method, path, *, body=None, query=None, cancel=None):
        with self._response(method, path, body=body, query=query, cancel=cancel) as (response, size, deadline):
            if (size > JSON_LIMIT or len(response.headers.get_all("Content-Type", [])) != 1
                    or response.headers.get_content_type() != "application/json"):
                raise NetworkError("The local API returned an unsupported JSON response.")
            chunks = []
            self._copy(response, size, deadline, chunks.append, cancel=cancel)
            try:
                value = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=_unique_json_object,
                                   parse_constant=_invalid_json_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise NetworkError("The local API returned invalid JSON.") from None
            if cancel is not None:
                cancel.check()
            return value

    def request(self, method, path, *, body=None, query=None):
        value = self._request_json(method, path, body=body, query=query)
        if not isinstance(value, dict):
            raise NetworkError("The local API returned an unexpected response.")
        return value

    def file(self, identifier, folder, *, title=None):
        """Save a completed task's file in folder, named after title when given."""
        identifier = task_id(identifier)
        folder = Path(folder)
        if not folder.is_dir():
            raise ValueError("Choose an existing destination directory.")
        with self._response("GET", f"/downloads/{identifier}/file") as (response, size, deadline):
            extension = _FILE_TYPES.get(response.headers.get_content_type())
            if extension is None or not 0 < size <= BOOK_LIMIT:
                raise NetworkError("The local API returned an unsupported book file.")
            return save_book(title or "download-" + identifier, extension, folder,
                             lambda stream: self._copy(response, size, deadline, stream.write))


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field.")
        result[key] = value
    return result


def _invalid_json_constant(_):
    raise ValueError("Invalid JSON number.")
