"""Transport-agnostic MCP (legacy revisions) over two byte streams: one JSON-RPC message per line.

The dispatcher answers lifecycle methods inline, runs tool calls on daemon
threads and never sends anything for a request after its cancellation. stdout
carries protocol messages only; diagnostics go to the injected logger.
"""

import json
import logging
import os
import sys
import threading
import time

from ..terminal_text import terminal_text


SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18")
LATEST_VERSION = SUPPORTED_VERSIONS[0]
MAX_LINE = 4 * 1024 * 1024
MAX_CONCURRENT = 4

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_MESSAGES = {
    PARSE_ERROR: "Parse error",
    INVALID_REQUEST: "Invalid request",
    METHOD_NOT_FOUND: "Method not found",
    INVALID_PARAMS: "Invalid params",
    INTERNAL_ERROR: "Internal error",
}
_KNOWN = frozenset(("initialize", "ping", "tools/list", "tools/call"))


class _Invalid(Exception):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Invalid
        result[key] = value
    return result


def _constant(_):
    raise _Invalid


def parse_message(line: bytes):
    """Decode one line strictly: UTF-8, no NaN/Infinity, no duplicate keys."""
    try:
        return json.loads(line.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
    except (ValueError, _Invalid, UnicodeDecodeError, RecursionError, TypeError):
        raise _Invalid from None


def _valid_id(value):
    return isinstance(value, str) or (type(value) is int)


def _id_key(value):
    return (type(value).__name__, value)


def _clean(value):
    """Replace lone surrogates so the UTF-8 encoding can never fail."""
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8") if any(
            0xD800 <= ord(char) <= 0xDFFF for char in value) else value
    if isinstance(value, dict):
        return {_clean(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def encode_message(message) -> bytes:
    try:
        data = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        data = json.dumps(_clean(message), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if b"\n" in data or b"\r" in data:
        raise ValueError("A protocol message must fit on one line.")
    return data


class ShutdownRequested(Exception):
    """Raised by a signal handler on the main thread to stop serving."""


class RequestContext:
    """What a tool handler may know about its request: cancellation, progress and the negotiated revision."""

    def __init__(self, server, key, progress_token, protocol_version):
        self._server = server
        self._key = key
        self.progress_token = progress_token
        self.protocol_version = protocol_version
        self._cancelled = threading.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self):
        self._cancelled.set()

    def wait(self, seconds) -> bool:
        """Sleep up to seconds; return True early when the request is cancelled."""
        return self._cancelled.wait(seconds)

    def progress(self, progress, total=None, message=None) -> bool:
        if self.progress_token is None:
            return False
        params = {"progressToken": self.progress_token, "progress": progress}
        if total is not None:
            params["total"] = total
        if message is not None:
            params["message"] = message
        return self._server._notify_progress(self._key, {"jsonrpc": "2.0", "method": "notifications/progress",
                                                        "params": params})


class _Entry:
    __slots__ = ("request_id", "context", "state", "name", "started")

    def __init__(self, request_id, context, name):
        self.request_id, self.context, self.name = request_id, context, name
        self.state = "running"
        self.started = time.monotonic()


class McpServer:
    """Serve one connection. ``tools`` provides list_tools(), has_tool(), call() and busy_result()."""

    def __init__(self, tools, *, server_info, instructions, logger=None, max_line=MAX_LINE,
                 max_concurrent=MAX_CONCURRENT):
        self._tools = tools
        self._server_info = dict(server_info)
        self._instructions = instructions
        self._log = logger or logging.getLogger("zlib_connector.mcp")
        self._max_line = max_line
        self._max_concurrent = max_concurrent
        self._write_lock = threading.Lock()
        self._registry = threading.Lock()
        self._inflight = {}
        self._writer = None
        self._closing = threading.Event()
        self.protocol_version = None
        self.client_name = None

    # Output -----------------------------------------------------------------

    def _send(self, message) -> bool:
        try:
            data = encode_message(message)
        except (TypeError, ValueError):
            self._log.error("A protocol message could not be serialized.")
            if "id" in message and "result" in message:
                return self._send({"jsonrpc": "2.0", "id": message["id"],
                                   "error": {"code": INTERNAL_ERROR, "message": _MESSAGES[INTERNAL_ERROR]}})
            return False
        with self._write_lock:
            if self._closing.is_set() and self._writer is None:
                return False
            try:
                self._writer.write(data + b"\n")
                self._writer.flush()
                return True
            except (OSError, ValueError, AttributeError):
                self._closing.set()
                return False

    def _error(self, code, request_id=None, *, method=None):
        message = {"jsonrpc": "2.0", "error": {"code": code, "message": _MESSAGES[code]}}
        if request_id is not None:
            message = {"jsonrpc": "2.0", "id": request_id, "error": message["error"]}
        if code != METHOD_NOT_FOUND or method not in ("server/discover",):
            name = terminal_text(method)[:64] if isinstance(method, str) else None
            self._log.warning("protocol error %d%s", code, f" ({name})" if name else "")
        self._send(message)

    def _result(self, request_id, result):
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _notify_progress(self, key, message) -> bool:
        with self._registry:
            entry = self._inflight.get(key)
            if entry is None or entry.state != "running" or self._closing.is_set():
                return False
            return self._send(message)

    # Input ------------------------------------------------------------------

    def _lines(self, reader):
        while not self._closing.is_set():
            line = reader.readline(self._max_line + 1)
            if not line:
                return
            if len(line) > self._max_line and not line.endswith(b"\n"):
                # Discard the rest of the oversized line, then keep serving.
                while not line.endswith(b"\n"):
                    line = reader.readline(self._max_line + 1)
                    if not line:
                        break
                yield None
                continue
            if line.endswith(b"\n"):
                line = line[:-1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if line.strip(b" \t"):
                yield line

    def serve(self, reader, writer) -> None:
        """Serve until EOF, a broken pipe or KeyboardInterrupt, then cancel every request in flight."""
        self._writer = writer
        try:
            for line in self._lines(reader):
                if line is None:
                    self._error(INVALID_REQUEST)
                    continue
                try:
                    self._dispatch(line)
                except ShutdownRequested:
                    raise
                except Exception:
                    self._log.exception("dispatcher fault")
        except (KeyboardInterrupt, ShutdownRequested):
            pass
        except (OSError, ValueError):
            pass
        finally:
            self.shutdown()

    def shutdown(self):
        self._closing.set()
        with self._registry:
            for entry in self._inflight.values():
                if entry.state == "running":
                    entry.state = "cancelled"
                    entry.context.cancel()
        with self._write_lock:
            self._writer = None

    def _dispatch(self, line):
        try:
            message = parse_message(line)
        except _Invalid:
            self._error(PARSE_ERROR)
            return
        if not isinstance(message, dict):
            self._error(INVALID_REQUEST)
            return
        if "id" not in message:
            if "method" in message:
                self._notification(message)
            elif "error" not in message and "result" not in message:
                self._error(INVALID_REQUEST)
            return
        request_id = message["id"]
        if not _valid_id(request_id):
            self._error(INVALID_REQUEST)
            return
        method = message.get("method")
        if "method" not in message and ("result" in message or "error" in message):
            return  # A response: this server never sends requests, so there is nothing to match.
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            self._error(INVALID_REQUEST, request_id)
            return
        params = message.get("params", {})
        if params is None or not isinstance(params, dict):
            self._error(INVALID_REQUEST, request_id, method=method)
            return
        if method not in _KNOWN:
            self._error(METHOD_NOT_FOUND, request_id, method=method)
            return
        if method == "ping":
            self._result(request_id, {})
            return
        if method == "initialize":
            self._initialize(request_id, params)
            return
        if self.protocol_version is None:
            self._error(INVALID_REQUEST, request_id, method=method)
            return
        if method == "tools/list":
            if params.get("cursor") is not None:
                self._error(INVALID_PARAMS, request_id, method=method)
                return
            self._result(request_id, {"tools": self._tools.list_tools()})
            return
        self._call(request_id, params)

    def _notification(self, message):
        method = message.get("method")
        params = message.get("params")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return
        if method == "notifications/cancelled" and isinstance(params, dict):
            request_id = params.get("requestId")
            if not _valid_id(request_id):
                return
            with self._registry:
                entry = self._inflight.get(_id_key(request_id))
                if entry is not None and entry.state == "running":
                    entry.state = "cancelled"
                    entry.context.cancel()
        # notifications/initialized and every other notification need no action.

    def _initialize(self, request_id, params):
        if self.protocol_version is not None:
            self._error(INVALID_REQUEST, request_id, method="initialize")
            return
        requested = params.get("protocolVersion")
        capabilities = params.get("capabilities", {})
        client = params.get("clientInfo", {})
        if (not isinstance(requested, str) or not isinstance(capabilities, dict)
                or not isinstance(client, dict)):
            self._error(INVALID_PARAMS, request_id, method="initialize")
            return
        version = requested if requested in SUPPORTED_VERSIONS else LATEST_VERSION
        self.protocol_version = version
        name = client.get("name")
        self.client_name = terminal_text(name)[:64] if isinstance(name, str) else None
        result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": self._server_info}
        if self._instructions:
            result["instructions"] = self._instructions
        self._result(request_id, result)
        self._log.info("client %s, protocol %s", self.client_name or "(unnamed)", version)

    def _call(self, request_id, params):
        name = params.get("name")
        arguments = params.get("arguments", {})
        meta = params.get("_meta", {})
        if (not isinstance(name, str) or not isinstance(arguments, dict) or not isinstance(meta, dict)):
            self._error(INVALID_PARAMS, request_id, method="tools/call")
            return
        if not self._tools.has_tool(name):
            self._send({"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": INVALID_PARAMS, "message": "Unknown or disabled tool."}})
            self._log.warning("protocol error %d (unknown or disabled tool)", INVALID_PARAMS)
            return
        token = meta.get("progressToken")
        if not _valid_id(token):
            token = None
        key = _id_key(request_id)
        with self._registry:
            if key in self._inflight:
                duplicate = True
            else:
                duplicate = False
                running = sum(1 for entry in self._inflight.values() if entry.state == "running")
                busy = running >= self._max_concurrent
                if not busy:
                    context = RequestContext(self, key, token, self.protocol_version)
                    entry = _Entry(request_id, context, name)
                    self._inflight[key] = entry
        if duplicate:
            self._error(INVALID_REQUEST, method="tools/call")
            return
        if busy:
            self._result(request_id, self._tools.busy_result())
            self._log.info("tool %s: server_busy", name)
            return
        worker = threading.Thread(target=self._run, args=(entry, key, name, arguments),
                                  name="zlib-mcp-call", daemon=True)
        worker.start()

    def _run(self, entry, key, name, arguments):
        outcome = None
        try:
            result = self._tools.call(name, arguments, entry.context)
            outcome = ("result", result)
        except BaseException:
            self._log.exception("tool %s: dispatcher fault", name)
            outcome = ("error", None)
        with self._registry:
            state = entry.state
            if state == "running":
                entry.state = "responding"
            self._inflight.pop(key, None)
        elapsed = int((time.monotonic() - entry.started) * 1000)
        if state != "running":
            self._log.warning("tool %s: response suppressed after cancellation (%d ms)", name, elapsed)
            return
        if outcome[0] == "result":
            result = outcome[1]
            code = "ok"
            if result.get("isError"):
                code = getattr(self._tools, "error_code", lambda value: "error")(result)
            self._result(entry.request_id, result)
            self._log.info("tool %s: %s (%d ms)", name, code, elapsed)
        else:
            self._send({"jsonrpc": "2.0", "id": entry.request_id,
                        "error": {"code": INTERNAL_ERROR, "message": _MESSAGES[INTERNAL_ERROR]}})


def divert_stdio():
    """Duplicate the protocol descriptors, then point fd 1 at stderr and fd 0 at the null device.

    Returns (reader, writer) binary streams. Stray print() calls or child
    processes can then never write into the protocol stream.
    """
    try:
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        pass
    out_fd = os.dup(1)
    in_fd = os.dup(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(out_fd, os.O_BINARY)
        msvcrt.setmode(in_fd, os.O_BINARY)
    writer = os.fdopen(out_fd, "wb", buffering=0)
    reader = os.fdopen(in_fd, "rb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    null = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(null, 0)
    finally:
        os.close(null)
    sys.stdin = open(os.devnull, "r", encoding="utf-8")
    return reader, writer
