"""HTTP route contracts shared by input validation and OpenAPI generation."""

from dataclasses import dataclass, field
import math
import re
import unicodedata

from ..library import FAVORITE_ORDERS, HISTORY_ORDERS
from ..search import SEARCH_ORDERS
from ..jobs.models import TASK_STATES, TASK_ERROR_CODES


class RequestError(Exception):
    """A safe, local failure; messages never contain supplied values."""

    def __init__(self, status, code, message, *, retryable=False, outcome="not_applied", headers=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.retryable, self.outcome = retryable, outcome
        self.headers = headers or {}

    def to_dict(self):
        return {"error": {"code": self.code, "message": self.message,
                          "retryable": self.retryable, "outcome": self.outcome}}


_MISSING = object()


@dataclass(frozen=True)
class Field:
    kind: str = "string"
    required: bool = False
    default: object = _MISSING
    minimum: int | float | None = None
    maximum: int | float | None = None
    min_length: int = 0
    max_length: int = 2048
    enum: tuple = ()
    pattern: str | None = None
    repeated: bool = False
    multiline: bool = False
    max_utf16_units: int | None = None

    def schema(self):
        result = {"type": self.kind}
        if self.kind == "string":
            result.update(minLength=self.min_length, maxLength=self.max_length)
            if self.pattern:
                result["pattern"] = "^(?:" + self.pattern + ")$"
            result["description"] = ("Control characters are rejected except tab and line breaks." if self.multiline
                                     else "Control characters are rejected.")
            if self.min_length:
                result["description"] += " The value must contain non-whitespace text."
            if self.max_utf16_units is not None:
                result["x-maxUtf16CodeUnits"] = self.max_utf16_units
                result["description"] += f" At most {self.max_utf16_units} UTF-16 code units."
        elif self.kind in ("integer", "number"):
            if self.minimum is not None:
                result["minimum"] = self.minimum
            if self.maximum is not None:
                result["maximum"] = self.maximum
        if self.enum:
            result["enum"] = list(self.enum)
        if self.repeated:
            result = {"type": "array", "items": result, "minItems": 1, "maxItems": 50}
        if self.default is not _MISSING:
            result["default"] = self.default
        return result

    def parse(self, value, *, query=False):
        if self.repeated:
            if not isinstance(value, list) or not 1 <= len(value) <= 50:
                raise ValueError
            return tuple(self._scalar(item, query=query) for item in value)
        return self._scalar(value, query=query)

    def _scalar(self, value, *, query):
        if self.kind in ("integer", "number"):
            if query:
                pattern = r"[0-9]{1,12}" if self.kind == "integer" else r"[0-9]{1,12}(?:\.[0-9]{1,12})?"
                if not isinstance(value, str) or not re.fullmatch(pattern, value):
                    raise ValueError
                value = int(value) if self.kind == "integer" else float(value)
            if (self.kind == "integer" and type(value) is not int
                    or self.kind == "number" and type(value) not in (int, float)):
                raise ValueError
            if not math.isfinite(value) or self.minimum is not None and value < self.minimum:
                raise ValueError
            if self.maximum is not None and value > self.maximum:
                raise ValueError
        elif self.kind == "boolean":
            if type(value) is not bool:
                raise ValueError
        else:
            if not isinstance(value, str) or not self.min_length <= len(value) <= self.max_length:
                raise ValueError
            if self.min_length and not value.strip():
                raise ValueError
            allowed = "\r\n\t" if self.multiline else ""
            if any(unicodedata.category(char).startswith("C") and char not in allowed for char in value):
                raise ValueError
            if self.pattern and not re.fullmatch(self.pattern, value):
                raise ValueError
            if self.max_utf16_units is not None and len(value.encode("utf-16-le")) // 2 > self.max_utf16_units:
                raise ValueError
        if self.enum and value not in self.enum:
            raise ValueError
        return value


def validate_fields(values, fields, *, query=False):
    if not isinstance(values, dict) or values.keys() - fields.keys():
        raise RequestError(400, "invalid_argument", "The request contains unsupported fields.")
    result = {}
    for name, spec in fields.items():
        if name not in values:
            if spec.required:
                raise RequestError(400, "invalid_argument", f"The {name} field is required.")
            if spec.default is not _MISSING:
                result[name] = spec.default
            continue
        try:
            value = values[name]
            if query and not spec.repeated:
                if len(value) != 1:
                    raise ValueError
                value = value[0]
            result[name] = spec.parse(value, query=query)
        except (ValueError, TypeError, OverflowError):
            raise RequestError(400, "invalid_argument", f"The {name} field is invalid.") from None
    return result


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    operation: str
    summary: str
    response: str
    query: dict = field(default_factory=dict)
    body: dict | None = None
    status: int = 200
    mutation: bool = False
    partial: bool = False
    binary: bool = False
    events: bool = False

    @property
    def parameters(self):
        return tuple(re.findall(r"\{([a-z_]+)\}", self.path))

    def match(self, path):
        fragments = []
        start = 0
        for parameter in re.finditer(r"\{([a-z_]+)\}", self.path):
            fragments.append(re.escape(self.path[start:parameter.start()]))
            fragments.append(f"(?P<{parameter.group(1)}>[^/]+)")
            start = parameter.end()
        fragments.append(re.escape(self.path[start:]))
        return re.fullmatch("".join(fragments), path)


PAGE = Field("integer", default=1, minimum=1, maximum=1000)
LIMIT = Field("integer", default=20, minimum=1, maximum=50)
REFERENCE = Field(required=True, min_length=3, max_length=150, pattern=r"[0-9]{1,20}:[A-Za-z0-9]{1,128}")
EVENT_ID = Field(min_length=34, max_length=53, pattern=r"[0-9a-f]{32}:[0-9]{1,20}")
PATH_FIELDS = {
    "book_id": Field(required=True, min_length=1, max_length=20, pattern=r"[0-9]{1,20}"),
    "book_hash": Field(required=True, min_length=1, max_length=128, pattern=r"[A-Za-z0-9]{1,128}"),
    "booklist_id": Field(required=True, min_length=1, max_length=20, pattern=r"[1-9][0-9]{0,19}"),
    "entry_id": Field(required=True, min_length=1, max_length=20, pattern=r"[1-9][0-9]{0,19}"),
    "task_id": Field(required=True, min_length=32, max_length=32, pattern=r"[0-9a-f]{32}"),
}
SETTINGS = {
    "title": Field(min_length=1, max_length=120),
    "description": Field(max_length=1000, multiline=True),
    "is_public": Field("boolean"), "color": Field("integer", minimum=0, maximum=5),
}

ENDPOINTS = (
    Endpoint("GET", "/v1/health", "health", "Check the local service without contacting Z-Library", "Health"),
    Endpoint("GET", "/v1/capabilities", "capabilities", "List implemented HTTP capabilities", "Capabilities"),
    Endpoint("GET", "/v1/openapi.json", "openapi", "Read this API's OpenAPI document", "OpenAPI"),
    Endpoint("GET", "/v1/search", "search", "Search books with pagination and filters", "BookPage", query={
        "q": Field(required=True, min_length=1, max_length=500), "page": PAGE, "limit": LIMIT,
        "language": Field(min_length=1, max_length=64, repeated=True),
        "format": Field(enum=("pdf", "epub", "djvu"), repeated=True),
        "year_from": Field("integer", minimum=1, maximum=9999),
        "year_to": Field("integer", minimum=1, maximum=9999), "order": Field(enum=SEARCH_ORDERS)}),
    Endpoint("GET", "/v1/books/{book_id}/{book_hash}", "book", "Read book details", "BookDetails"),
    Endpoint("GET", "/v1/books/{book_id}/{book_hash}/formats", "formats", "List available formats", "Formats"),
    Endpoint("GET", "/v1/books/{book_id}/{book_hash}/similar", "similar", "List similar books", "Books"),
    Endpoint("GET", "/v1/profile", "profile", "Read the safe account profile", "AccountProfile"),
    Endpoint("GET", "/v1/quota", "quota", "Read the account download quota", "DownloadQuota"),
    Endpoint("GET", "/v1/domains", "domains", "List known domain candidates without probing them", "Domains"),
    Endpoint("POST", "/v1/domains/diagnostics", "diagnostics", "Probe domains without account credentials", "DomainReport", body={
        "domains": Field(min_length=1, max_length=2048, repeated=True),
        "timeout": Field("number", default=10, minimum=0.1, maximum=30),
        "workers": Field("integer", default=4, minimum=1, maximum=8)}),
    Endpoint("GET", "/v1/library", "library", "Preview favorites, account history and booklists (not native My Library)", "LibraryOverview", query={
        "limit": Field("integer", default=5, minimum=1, maximum=50)}),
    Endpoint("GET", "/v1/my-library/tags", "library_tags", "List native My Library tag names without inferring counts", "LibraryTags"),
    Endpoint("GET", "/v1/my-library/saved-ids", "saved_book_ids", "List native saved book IDs, not complete library entries", "SavedBookIds"),
    Endpoint("PUT", "/v1/my-library/books/{book_id}/{book_hash}/tags", "book_tags_add", "Add tags; Z-Library also adds this book to favorites", "BookTagUpdate", body={
        "tags": Field(required=True, min_length=1, max_length=30, repeated=True, max_utf16_units=30)}, mutation=True),
    Endpoint("DELETE", "/v1/my-library/books/{book_id}/{book_hash}/tags", "book_tags_remove", "Remove the supplied tags, including older long labels; keep the favorite", "BookTagUpdate", body={
        "tags": Field(required=True, min_length=1, max_length=65536, repeated=True)}, mutation=True),
    Endpoint("GET", "/v1/favorites", "favorites", "List account favorites", "BookPage", query={
        "page": PAGE, "limit": LIMIT, "order": Field(default="saved_date", enum=FAVORITE_ORDERS)}),
    Endpoint("PUT", "/v1/favorites/{book_id}/{book_hash}", "favorite_add", "Save a favorite", "FavoriteUpdate", mutation=True),
    Endpoint("DELETE", "/v1/favorites/{book_id}/{book_hash}", "favorite_remove", "Remove a favorite", "FavoriteUpdate", mutation=True),
    Endpoint("GET", "/v1/history", "history", "List account download history (server sorting may vary)", "BookPage", query={
        "page": PAGE, "limit": LIMIT, "order": Field(enum=HISTORY_ORDERS)}),
    Endpoint("GET", "/v1/booklists", "booklists", "List personal booklists", "Booklists"),
    Endpoint("POST", "/v1/booklists", "booklist_create", "Create a booklist (private by default)", "Booklist", body={
        "title": Field(required=True, min_length=1, max_length=120),
        "description": Field(default="", max_length=1000, multiline=True),
        "is_public": Field("boolean", default=False), "color": Field("integer", default=0, minimum=0, maximum=5)}, status=201, mutation=True),
    Endpoint("PATCH", "/v1/booklists/{booklist_id}", "booklist_update", "Update selected settings; concurrent edits may overwrite each other", "Booklist", body=SETTINGS, mutation=True, partial=True),
    Endpoint("DELETE", "/v1/booklists/{booklist_id}", "booklist_delete", "Delete a booklist", "BooklistDeletion", mutation=True),
    Endpoint("GET", "/v1/booklists/{booklist_id}/books", "booklist_books", "List booklist entries and their membership IDs", "BooklistPage", query={"page": PAGE}),
    Endpoint("POST", "/v1/booklists/{booklist_id}/books", "booklist_add", "Add a book by ID:HASH", "BooklistBookUpdate", body={"reference": REFERENCE}, mutation=True),
    Endpoint("DELETE", "/v1/booklists/{booklist_id}/entries/{entry_id}", "booklist_remove", "Remove an entry by membership ID, not catalog ID", "BooklistBookUpdate", mutation=True),
    Endpoint("GET", "/v1/discovery/popular", "popular", "List popular books", "Books"),
    Endpoint("GET", "/v1/discovery/recommended", "recommended", "List account recommendations", "Books"),
    Endpoint("POST", "/v1/downloads", "download_submit", "Persist a download task; reuse its key to replay the same submission", "DownloadTask", body={
        "reference": Field(required=True, min_length=3, max_length=2048),
        "idempotency_key": Field(required=True, min_length=8, max_length=128, pattern=r"[A-Za-z0-9._~-]{8,128}")}, status=202),
    Endpoint("GET", "/v1/downloads", "download_list", "List persisted tasks in insertion order", "TaskPage", query={
        "limit": Field("integer", default=50, minimum=1, maximum=1000),
        "offset": Field("integer", default=0, minimum=0, maximum=10000)}),
    Endpoint("POST", "/v1/downloads/resume", "download_resume", "Clear the quota pause and resume waiting tasks; never retry failed tasks", "DownloadResume"),
    Endpoint("GET", "/v1/downloads/{task_id}", "download_get", "Read task state and current transfer progress", "DownloadTask"),
    Endpoint("POST", "/v1/downloads/{task_id}/cancel", "download_cancel", "Persist a cancellation request; a published file keeps its success", "DownloadTask"),
    Endpoint("GET", "/v1/downloads/{task_id}/file", "download_file", "Download the owned file of a succeeded task; byte ranges are unsupported", "DownloadFile", binary=True),
    Endpoint("GET", "/v1/downloads/{task_id}/events", "download_events", "Observe complete task states in a finite event stream", "TaskEventStream", query={
        "duration": Field("number", default=30, minimum=1, maximum=60), "after": EVENT_ID}, events=True),
)


def resolve(method, path):
    matches = [(route, route.match(path)) for route in ENDPOINTS]
    matches = [(route, match) for route, match in matches if match]
    if not matches:
        raise RequestError(404, "not_found", "The API route does not exist.")
    # A literal route such as /downloads/resume takes precedence over {task_id},
    # even when the caller uses a method that the literal route does not support.
    specificity = min(len(route.parameters) for route, _ in matches)
    matches = [(route, match) for route, match in matches if len(route.parameters) == specificity]
    for route, match in matches:
        if route.method == method:
            fields = {name: PATH_FIELDS[name] for name in route.parameters}
            return route, validate_fields(match.groupdict(), fields)
    allowed = ", ".join(sorted({route.method for route, _ in matches}))
    raise RequestError(405, "method_not_allowed", "This method is not available for this route.", headers={"Allow": allowed})


def object_schema(properties, *, required=None):
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": list(properties) if required is None else required}


def _ref(name):
    return {"$ref": "#/components/schemas/" + name}


def schemas():
    text = {"type": "string"}
    count = {"type": ["integer", "null"], "minimum": 0}
    boolean = {"type": ["boolean", "null"]}
    book = {name: text for name in ("title", "authors", "extension", "url", "year", "language", "size", "book_id", "book_hash")}
    book["is_favorite"] = boolean
    books = {"type": "array", "items": _ref("Book")}
    page = {"books": books, "pagination": _ref("Pagination")}
    diagnostic = {"domain": text, "search_ok": {"type": "boolean"}, "checked_at": text,
                  "elapsed_ms": {"type": "integer", "minimum": 0}, "final_domain": {"type": ["string", "null"]},
                  "result_count": count, "error_code": {"type": ["string", "null"]}, "message": text}
    return {
        "Error": object_schema({"error": object_schema({"code": text, "message": text,
            "retryable": {"type": "boolean"}, "outcome": {"type": "string", "enum": ["not_applied", "unknown"]}})}),
        "Health": object_schema({"status": {"const": "ok"}, "api_version": {"const": "v1"}, "upstream_status": {"const": "not_checked"}}),
        "Capabilities": object_schema({"api_version": {"const": "v1"}, "authentication": {"const": "bearer"},
            "client_access": {"const": "serialized"}, "operations": {"type": "array", "items": text},
            "unavailable": {"type": "array", "items": text}, "downloads_enabled": {"type": "boolean"}}),
        "OpenAPI": {"type": "object", "description": "OpenAPI 3.1 document describing this server."},
        "Book": object_schema(book), "Books": books,
        "BookDetails": object_schema({**book, **{name: text for name in ("description", "cover_url", "identifier", "isbn10", "isbn13", "publisher", "edition", "pages")}, "categories": {"type": "array", "items": text}}),
        "Formats": {"type": "array", "items": object_schema({"book_id": text, "book_hash": text, "extension": text, "size": text, "download_supported": {"type": "boolean"}})},
        "Pagination": object_schema({"page": {"type": "integer", "minimum": 1}, "limit": {"type": "integer", "minimum": 1}, **{name: count for name in ("total_items", "total_pages", "next_page", "previous_page")}}),
        "BookPage": object_schema(page),
        "BooklistPage": object_schema({**page, "books": {"type": "array", "items": object_schema({**book, "booklist_id": text, "entry_id": {"type": ["string", "null"]}})}}),
        "DownloadQuota": object_schema({"limit": count, "used": count, "remaining": count}),
        "AccountProfile": object_schema({"quota": _ref("DownloadQuota")}),
        "Domains": object_schema({"domains": {"type": "array", "items": text}, "probed": {"const": False}}),
        "DomainReport": object_schema({"results": {"type": "array", "items": object_schema(diagnostic)}, "working_domains": {"type": "array", "items": text}, "recommended_domain": {"type": ["string", "null"]}}),
        "FavoriteUpdate": object_schema({"book_id": text, "is_favorite": {"type": "boolean"}}),
        "Booklist": object_schema({"booklist_id": text, "title": text, "description": text, "book_count": count, "is_public": boolean, "updated_at": text, "color": count}),
        "Booklists": {"type": "array", "items": _ref("Booklist")},
        "LibraryOverview": object_schema({"favorites": _ref("BookPage"), "history": _ref("BookPage"), "booklists": _ref("Booklists")}),
        "LibraryTags": object_schema({"tags": {"type": "array", "items": text}}),
        "SavedBookIds": object_schema({"book_ids": {"type": "array", "items": {"type": "string", "pattern": "^[0-9]{1,20}$"}}}),
        "BookTagUpdate": object_schema({"book_id": text, "action": {"type": "string", "enum": ["add", "remove"]},
            "tags": {"type": "array", "items": text}, "adds_to_favorites": {"type": "boolean", "description": "True for add, false for remove. This is an operation side effect, not a complete favorite-state snapshot."}}),
        "BooklistDeletion": object_schema({"booklist_id": text, "deleted": {"const": True}}),
        "BooklistBookUpdate": object_schema({"booklist_id": text, "action": {"type": "string", "enum": ["add", "remove"]}, "book_id": {"type": ["string", "null"]}, "entry_id": {"type": ["string", "null"]}}),
        "TransferProgress": object_schema({"bytes_received": {"type": "integer", "minimum": 0}, "total_bytes": count,
            "elapsed_seconds": {"type": "number", "minimum": 0},
            "bytes_per_second": {"type": ["number", "null"], "minimum": 0},
            "eta_seconds": {"type": ["number", "null"], "minimum": 0},
            "fraction": {"type": ["number", "null"], "minimum": 0, "maximum": 1}}),
        "DownloadTask": object_schema({"id": PATH_FIELDS["task_id"].schema(), "reference": {"type": "string", "pattern": "^[0-9]{1,20}:[A-Za-z0-9]{1,128}$"},
            "state": {"type": "string", "enum": list(TASK_STATES)}, "created_at": {"type": "number", "minimum": 0},
            "updated_at": {"type": "number", "minimum": 0}, "progress": {"anyOf": [_ref("TransferProgress"), {"type": "null"}]},
            "cancel_requested": {"type": "boolean"}, "error_code": {"type": ["string", "null"], "enum": [*TASK_ERROR_CODES, None]},
            "file_available": {"type": "boolean"}, "error_message": {"type": ["string", "null"]},
            "outcome": {"type": "string", "enum": ["not_started", "completed", "unknown"]}}),
        "TaskPage": object_schema({"tasks": {"type": "array", "items": _ref("DownloadTask"), "maxItems": 1000},
            "total": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "offset": {"type": "integer", "minimum": 0},
            "paused": {"type": "boolean", "description": "Pending downloads remain paused after a quota or rate limit until an explicit resume."}}),
        "DownloadResume": object_schema({"resumed": {"type": "integer", "minimum": 0}}),
        "DownloadFile": {"type": "string", "format": "binary", "description": "The task's complete pinned file; never a local path or an arbitrary file."},
        "TaskEvent": object_schema({"id": EVENT_ID.schema(), "task": _ref("DownloadTask"), "paused": {"type": "boolean"}}),
        "TaskEventEnd": object_schema({"reason": {"type": "string", "enum": ["terminal", "duration", "shutdown"]}}),
        "TaskEventStream": {"type": "string", "description": "UTF-8 SSE frames: event: task, id: cursor and data: compact TaskEvent JSON; comments are heartbeats. A final event: end carries TaskEventEnd JSON. Each frame is at most 64 KiB."},
    }


def openapi_document():
    paths = {}
    for route in ENDPOINTS:
        content = ({media: {"schema": _ref(route.response)} for media in
                    ("application/octet-stream", "application/pdf", "application/epub+zip", "image/vnd.djvu")}
                   if route.binary else {"application/json": {"schema": _ref(route.response)}})
        if route.events:
            content = {"text/event-stream": {"schema": _ref(route.response)}}
        operation = {"operationId": route.operation, "summary": route.summary,
                     "responses": {str(route.status): {"description": "Success", "content": content}}}
        if route.operation.startswith("download_"):
            operation["description"] = "Available only when the server is explicitly configured with a download directory and verified account; otherwise returns 503 downloads_unavailable. No local paths are accepted or returned."
        if route.operation == "download_submit":
            operation["description"] += " The reference accepts ID:HASH or a book URL on the configured domain. Reusing an idempotency key with the same normalized reference returns the existing task; a different reference returns 409. Acceptance does not guarantee a completed download, and failed or interrupted tasks are never automatically retried."
        if route.binary:
            operation["responses"][str(route.status)]["headers"] = {
                "Content-Disposition": {"schema": {"type": "string"}, "description": "Attachment with an encoded basename."},
                "Content-Length": {"schema": {"type": "integer", "minimum": 0}},
                "Accept-Ranges": {"schema": {"type": "string", "const": "none"}}}
            operation["description"] += " Range and If-Range are rejected. A changed file is refused before headers; a later read failure terminates the stream, so clients must verify Content-Length."
        parameters = [{"name": name, "in": "path", "required": True, "schema": PATH_FIELDS[name].schema()} for name in route.parameters]
        if route.events:
            parameters.append({"name": "Last-Event-ID", "in": "header", "required": False, "schema": EVENT_ID.schema(),
                               "description": "Alternative to after; supplying both is rejected."})
            operation["description"] += " Supply after or Last-Event-ID, never both. This is a coalesced full-state observation, not transition replay: stale or foreign cursors yield the latest snapshot, and terminal state is always sent once. Streams last 1–60 seconds, with comments at most five seconds apart, and end with terminal, duration or shutdown. At most four streams and half the connection slots (minimum one) are allowed concurrently; excess streams receive 503. No Content-Length, Transfer-Encoding or compression. Errors after headers terminate the stream without appending JSON. Browser clients must use fetch with a bearer header; native EventSource cannot set Authorization."
        for name, spec in route.query.items():
            parameter = {"name": name, "in": "query", "required": spec.required, "schema": spec.schema()}
            if spec.repeated:
                parameter.update(style="form", explode=True)
            parameters.append(parameter)
        if parameters:
            operation["parameters"] = parameters
        if route.body is not None:
            schema = object_schema({name: spec.schema() for name, spec in route.body.items()}, required=[name for name, spec in route.body.items() if spec.required])
            if route.partial:
                schema["minProperties"] = 1
            operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": schema}}}
        for status in (400, 401, 403, 404, 405, 408, 409, 410, 413, 414, 415, 417, 422, 424, 429, 431, 500, 502, 503, 504):
            operation["responses"][str(status)] = {"description": "Structured error; retry mutations only after checking their outcome.", "content": {"application/json": {"schema": _ref("Error")}}}
        paths.setdefault(route.path, {})[route.method.lower()] = operation
    return {"openapi": "3.1.0", "info": {"title": "Z-Library HTTP API", "version": "1.0.0",
        "description": "Local API with a caller-owned Client. Upstream calls are serialized; local task polling and cancellation remain independent. Downloads require an explicitly configured directory and verified account. No HTTP login. Bearer authentication is required, including files, health and this document. Browser origins are disabled unless explicitly allowed."},
        "servers": [{"url": "/"}], "security": [{"bearerAuth": []}], "paths": paths,
        "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}, "schemas": schemas()}}
