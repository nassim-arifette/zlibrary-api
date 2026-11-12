"""Synthetic MCP clients: a fake Z-Library world, an in-process pipe harness and a subprocess entry.

Nothing here contacts the network: the fake Client never opens a socket, and
the subprocess entry also disables socket connections before serving.

Subprocess use: python tests/mcp_fixture.py SCENARIO [zlib-cli arguments...]
"""

from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zlib_connector.account import DownloadQuota
from zlib_connector.booklists import Booklist, BooklistBook, BooklistBookUpdate, BooklistDeletion
from zlib_connector.books import BookDetails, BookFormat, BookReference
from zlib_connector.client import Book
from zlib_connector.errors import AuthenticationError
from zlib_connector.library import FavoriteUpdate
from zlib_connector.my_library import BookTagUpdate, LibraryTags
from zlib_connector.pagination import BookPage
from zlib_connector.session_data import SavedSession, SessionCookie
from zlib_connector.sessions import SessionStore
from zlib_connector.transfer import TransferProgress


ORIGIN = "https://books.example/"
ACCOUNT_ID = "918273645"
COOKIE_SECRET = "SyntheticCookieSecretValue7731"
SIGNED_LINK = "https://cdn.example/dl/SIGNED-LINK-TOKEN-5521"
EPUB = b"PK\x03\x04synthetic epub bytes " * 64


def save_session(path, *, origin=ORIGIN, expires=None):
    store = SessionStore(path)
    snapshot = store.read()
    session = SavedSession(origin, (SessionCookie("remix_userid", ACCOUNT_ID, expires=expires),
                                    SessionCookie("remix_userkey", COOKIE_SECRET, expires=expires)), int(time.time()))
    return store.save(session, expected_generation=snapshot.generation)


def make_book(book_id="12", book_hash="abc", *, title="Example", extension="epub", favorite=None, **fields):
    return Book(title, fields.pop("authors", "Author"), extension, f"{ORIGIN}book/{book_id}/{book_hash}",
                year=fields.pop("year", "2001"), language=fields.pop("language", "english"),
                size=fields.pop("size", "1 MB"), book_id=book_id, book_hash=book_hash, is_favorite=favorite)


class World:
    """Shared, thread-safe state of the synthetic service, seen by every fake Client."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []
        self.clients = []
        self.errors = {}
        self.gates = {}
        self.entered = {}
        self.restores = 0
        self.restore_error = None
        self.books = [make_book("12", "abc", title="Example"), make_book("13", "def", title="Second", extension="pdf")]
        self.extensions = {}
        self.details = {}
        self.formats = None
        self.booklists = [Booklist("5", "Reading", "A list", 2, False, "2026-01-01", 2)]
        self.tags = ("Reading", "Science")
        self.remaining = 7
        self.link_requests = 0
        self.download_failures = []
        self.download_gate = None
        self.download_started = threading.Event()
        self.download_steps = 1
        self.download_delay = 0.0
        self.printer = None

    def factory(self, domain, *, timeout=30):
        client = FakeClient(self, domain, timeout=timeout, role="account" if not self.clients else "public")
        with self.lock:
            self.clients.append(client)
        return client

    def count(self, name, role=None):
        with self.lock:
            return sum(1 for call in self.calls if call[1] == name and (role is None or call[0] == role))


class FakeClient:
    def __init__(self, world, domain, *, timeout, role):
        self.world, self.domain, self.timeout, self.role = world, domain, timeout, role
        self.authenticated = False
        self.closed = False

    def _call(self, name, *args):
        world = self.world
        with world.lock:
            world.calls.append((self.role, name, args))
            entered = world.entered.setdefault(name, threading.Event())
        entered.set()
        gate = world.gates.get(name)
        if gate is not None and not gate.wait(10):
            raise AssertionError("A synthetic gate was never released.")
        error = world.errors.get(name)
        if error is not None:
            raise error
        if world.printer is not None:
            world.printer(name)

    def _login(self):
        if not self.authenticated:
            raise AuthenticationError("Synthetic account requires a session.")

    def close(self):
        self.closed = True
        self.authenticated = False

    def restore_session(self, store):
        with self.world.lock:
            self.world.restores += 1
        snapshot = store.read()
        if snapshot.session is None:
            raise AuthenticationError("No synthetic session.")
        if self.world.restore_error is not None:
            raise self.world.restore_error
        self.authenticated = True
        return {"authenticated": True}

    def account_fingerprint(self, key):
        self._login()
        return "b" * 64

    def _books(self, books=None):
        books = self.world.books if books is None else books
        return tuple(Book(**{**item.to_dict(), "is_favorite": (True if self.authenticated else None)
                             if item.is_favorite is None else item.is_favorite}) for item in books)

    def search_page(self, query, *, page=1, limit=20, filters=None, extension=None):
        self._call("search_page", query, page, limit, filters)
        books = self._books()
        if query.startswith("echo:"):
            books = (make_book("44", "e1", title=query[5:]),)
        return BookPage(books[:limit], page, limit, len(books), 1, None, None)

    def get_book(self, reference):
        self._call("get_book", reference)
        parsed = BookReference.parse(reference)
        reference = str(parsed)
        if reference in self.world.details:
            return self.world.details[reference]
        base = next((item for item in self.world.books if f"{item.book_id}:{item.book_hash}" == reference),
                    make_book(parsed.book_id, parsed.book_hash))
        extension = self.world.extensions.get(reference, base.extension)
        return BookDetails(**{**base.to_dict(), "extension": extension}, description="A description.",
                           publisher="Publisher", categories=("Fiction",), isbn13="9780000000002")

    def get_formats(self, reference):
        self._call("get_formats", reference)
        if self.world.formats is not None:
            return self.world.formats
        return [BookFormat("12", "abc", "epub", "1 MB"), BookFormat("14", "ghi", "mobi", "2 MB")]

    def get_similar(self, reference):
        self._call("get_similar", reference)
        return list(self._books())

    def get_popular(self):
        self._call("get_popular")
        return list(self._books())

    def get_recommendations(self):
        self._login()
        self._call("get_recommendations")
        return list(self._books())

    def get_quota(self):
        self._login()
        self._call("get_quota")
        return DownloadQuota(10, 10 - self.world.remaining, self.world.remaining)

    def get_favorites(self, *, page=1, limit=20, order="saved_date"):
        self._login()
        self._call("get_favorites", page, limit, order)
        books = tuple(Book(**{**item.to_dict(), "is_favorite": True}) for item in self.world.books)
        return BookPage(books[:limit], page, limit, len(books), 1, None, None)

    def get_history(self, *, page=1, limit=20, order=None):
        self._login()
        self._call("get_history", page, limit, order)
        return BookPage(self._books()[:limit], page, limit, None, None, 2, None)

    def get_booklists(self):
        self._login()
        self._call("get_booklists")
        return list(self.world.booklists)

    def get_booklist_books(self, identifier, *, page=1):
        self._login()
        self._call("get_booklist_books", identifier, page)
        books = tuple(BooklistBook(**item.to_dict(), booklist_id=identifier, entry_id=str(100 + index))
                      for index, item in enumerate(self.world.books))
        return BookPage(books, page, 20, len(books), 1, None, None)

    def get_library_tags(self):
        self._login()
        self._call("get_library_tags")
        return LibraryTags(tuple(self.world.tags))

    def set_favorite(self, reference, *, saved):
        self._login()
        self._call("set_favorite", reference, saved)
        return FavoriteUpdate(reference.split(":")[0], saved)

    def add_book_tags(self, reference, tags):
        self._login()
        self._call("add_book_tags", reference, tuple(tags))
        return BookTagUpdate(reference.split(":")[0], "add", tuple(tags))

    def remove_book_tags(self, reference, tags):
        self._login()
        self._call("remove_book_tags", reference, tuple(tags))
        return BookTagUpdate(reference.split(":")[0], "remove", tuple(tags))

    def create_booklist(self, title, *, description="", is_public=False, color=0):
        self._login()
        self._call("create_booklist", title, description, is_public, color)
        return Booklist("6", title, description, 0, is_public, "", color)

    def update_booklist(self, identifier, **changes):
        self._login()
        self._call("update_booklist", identifier, tuple(sorted(changes.items())))
        current = next((item for item in self.world.booklists if item.booklist_id == identifier), None)
        if current is None:
            from zlib_connector.errors import BooklistNotFoundError
            raise BooklistNotFoundError("Synthetic booklist not found.")
        return Booklist(identifier, changes.get("title", current.title), changes.get("description", current.description),
                        current.book_count, changes.get("is_public", current.is_public), current.updated_at,
                        changes.get("color", current.color))

    def delete_booklist(self, identifier):
        self._login()
        self._call("delete_booklist", identifier)
        return BooklistDeletion(identifier)

    def add_booklist_book(self, identifier, reference):
        self._login()
        self._call("add_booklist_book", identifier, reference)
        return BooklistBookUpdate(identifier, "add", book_id=reference.split(":")[0])

    def remove_booklist_book(self, identifier, entry_id):
        self._login()
        self._call("remove_booklist_book", identifier, entry_id)
        return BooklistBookUpdate(identifier, "remove", entry_id=entry_id)

    def download(self, reference, destination, *, progress=None, cancel=None):
        self._login()
        world = self.world
        self._call("download", reference)
        with world.lock:
            world.link_requests += 1
            failure = world.download_failures.pop(0) if world.download_failures else None
        folder = Path(destination)
        partial = folder / ".zlib-synthetic.part"
        partial.write_bytes(b"")
        world.download_started.set()
        try:
            started = time.monotonic()
            total = len(EPUB)
            for step in range(1, world.download_steps + 1):
                if cancel is not None:
                    cancel.check()
                if world.download_delay:
                    if cancel is not None and cancel._cancelled.wait(world.download_delay):
                        cancel.check()
                    elif cancel is None:
                        time.sleep(world.download_delay)
                if progress is not None:
                    progress(TransferProgress(total * step // world.download_steps, total, time.monotonic() - started))
            gate = world.download_gate
            if gate is not None:
                while not gate.wait(0.02):
                    if cancel is not None:
                        cancel.check()
            if cancel is not None:
                cancel.check()
            if failure is not None:
                raise failure
            result = folder / f"Book-{reference.replace(':', '-')}.epub"
            result.write_bytes(EPUB)
            return result
        finally:
            partial.unlink(missing_ok=True)


class Sink:
    """A binary writer that splits protocol output into lines and keeps the raw bytes."""

    def __init__(self):
        self.raw = bytearray()
        self.lines = queue.Queue()
        self._pending = b""
        self._lock = threading.Lock()

    def write(self, data):
        with self._lock:
            self.raw += data
            self._pending += data
            while b"\n" in self._pending:
                line, self._pending = self._pending.split(b"\n", 1)
                self.lines.put((time.monotonic(), line))
        return len(data)

    def flush(self):
        pass


class Harness:
    """Run run_mcp in a thread over a real pipe, as a synthetic MCP client."""

    def __init__(self, argv, world=None):
        from zlib_connector.cli_parser import parser
        from zlib_connector.mcp_cli import run_mcp
        self.world = world or World()
        self.args = parser().parse_args(argv)
        read_fd, write_fd = os.pipe()
        self._input = os.fdopen(write_fd, "wb", buffering=0)
        self._reader = os.fdopen(read_fd, "rb")
        self.sink = Sink()
        self.stderr = io.StringIO()
        self.code = None
        self.notifications = []
        self._next_id = 100
        self._closed = False

        def run():
            try:
                self.code = run_mcp(self.args, client_factory=self.world.factory, stdin=self._reader,
                                    stdout=self.sink, stderr=self.stderr)
            finally:
                self._reader.close()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def send(self, message):
        data = message if isinstance(message, bytes) else json.dumps(message).encode("utf-8")
        self._input.write(data if data.endswith(b"\n") else data + b"\n")

    def receive(self, timeout=5):
        try:
            _, line = self.sink.lines.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError("No protocol message arrived in time.") from None
        return json.loads(line)

    def receive_timed(self, timeout=5):
        stamp, line = self.sink.lines.get(timeout=timeout)
        return stamp, json.loads(line)

    def quiet(self, seconds=0.2):
        """Assert nothing else is sent for a short while."""
        try:
            _, line = self.sink.lines.get(timeout=seconds)
        except queue.Empty:
            return
        raise AssertionError("Unexpected protocol message: " + line.decode("utf-8", "replace"))

    def request(self, method, params=None, *, request_id=None, timeout=5):
        if request_id is None:
            self._next_id += 1
            request_id = self._next_id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return self.wait_for(request_id, timeout=timeout)

    def wait_for(self, request_id, timeout=5):
        deadline = time.monotonic() + timeout
        while True:
            message = self.receive(timeout=max(0.01, deadline - time.monotonic()))
            if "id" not in message and message.get("method") == "notifications/progress":
                self.notifications.append(message)
                continue
            if message.get("id") == request_id:
                return message
            raise AssertionError("Unexpected protocol message: " + json.dumps(message))

    def collect(self, request_ids, timeout=10):
        """Wait for responses to several requests, in any order."""
        pending, found = set(request_ids), {}
        deadline = time.monotonic() + timeout
        while pending:
            message = self.receive(timeout=max(0.01, deadline - time.monotonic()))
            if message.get("id") in pending:
                pending.discard(message["id"])
                found[message["id"]] = message
            elif message.get("method") == "notifications/progress":
                self.notifications.append(message)
            else:
                raise AssertionError("Unexpected protocol message: " + json.dumps(message))
        return found

    def initialize(self, version="2025-11-25"):
        response = self.request("initialize", {"protocolVersion": version, "capabilities": {},
                                               "clientInfo": {"name": "synthetic", "version": "1"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response

    def call(self, name, arguments=None, *, meta=None, request_id=None, timeout=5):
        params = {"name": name, "arguments": arguments or {}}
        if meta is not None:
            params["_meta"] = meta
        response = self.request("tools/call", params, request_id=request_id, timeout=timeout)
        return response["result"]

    def start_call(self, name, arguments=None, *, request_id, meta=None):
        params = {"name": name, "arguments": arguments or {}}
        if meta is not None:
            params["_meta"] = meta
        self.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params})

    def close(self, timeout=5):
        if not self._closed:
            self._closed = True
            self._input.close()
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise AssertionError("The MCP server did not stop after EOF.")
        return self.code


def structured(result):
    if result.get("isError"):
        raise AssertionError("Tool failed: " + result["content"][0]["text"])
    return result["structuredContent"]


def error_of(result):
    if not result.get("isError"):
        raise AssertionError("Tool succeeded unexpectedly.")
    if "structuredContent" in result:
        raise AssertionError("Error results must not carry structuredContent.")
    return json.loads(result["content"][0]["text"])["error"]


@contextmanager
def released(event):
    try:
        yield event
    finally:
        event.set()


# Subprocess entry --------------------------------------------------------------

def _block_network():
    import socket

    def refuse(*args, **kwargs):
        raise OSError("Network access is disabled in MCP tests.")

    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse
    socket.create_connection = refuse
    socket.getaddrinfo = refuse


def _scenario(name):
    world = World()
    if name == "stray":
        def printer(method):
            if method == "search_page":
                import subprocess
                print("stray print from a handler")
                subprocess.run([sys.executable, "-c", "print('stray child output')"], check=False)
        world.printer = printer
    elif name == "transfer":
        world.download_gate = threading.Event()
    elif name == "secrets":
        from zlib_connector.errors import NetworkError
        world.download_failures = [NetworkError("failed at " + SIGNED_LINK + " cookie " + COOKIE_SECRET)]
    return world


def main(argv):
    _block_network()
    from zlib_connector.cli_parser import parser
    from zlib_connector.mcp_cli import run_mcp
    world = _scenario(argv[0])
    args = parser().parse_args(argv[1:])
    return run_mcp(args, client_factory=world.factory)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
