"""Real loopback HTTP requests; no upstream network or account credentials."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import http.client
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

from zlib_connector import (AccountProfile, Book, BookDetails, BookFormat, BookPage, Booklist,
    BooklistBook, BooklistBookUpdate, BooklistDeletion, BooklistNotFoundError, Client,
    DownloadQuota, FavoriteUpdate, LibraryOverview)
from zlib_connector.diagnostics import DomainDiagnostic, DomainReport
from zlib_connector.errors import (AuthenticationError, ChallengeRequired, DownloadError,
    FormatChangedError, MutationUncertainError, NetworkError, RateLimitError)
from zlib_connector.http_api import create_server, openapi_document
from zlib_connector.my_library import BookTagUpdate, LibraryTags
from test_eapi import ORIGIN
from test_library import transport_for


TOKEN = "test-token-" + "a" * 32
BOOK = Book("Example", "Author", "epub", "https://example.org/book/12/abc", book_id="12", book_hash="abc")
PAGE = BookPage((BOOK,), 2, 3, 8, 3, 3, 1)
LIST = Booklist("5", "Reading", book_count=1, is_public=False, color=2)


class FakeClient:
    def __init__(self):
        self.calls = []
        self.error = None
        self.closed = False

    def _call(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.error:
            raise self.error

    def search_page(self, query, **kwargs):
        self._call("search", query, **kwargs)
        return PAGE

    def get_book(self, reference):
        self._call("book", reference)
        return BookDetails(**BOOK.to_dict(), description="Description", categories=("Fiction",))

    def get_formats(self, reference):
        self._call("formats", reference)
        return [BookFormat("12", "abc", "epub", "1 MB")]

    def get_similar(self, reference):
        self._call("similar", reference)
        return [BOOK]

    def get_profile(self):
        self._call("profile")
        return AccountProfile(DownloadQuota(10, 2, 8))

    def get_quota(self):
        self._call("quota")
        return DownloadQuota(10, 2, 8)

    def get_library(self, **kwargs):
        self._call("library", **kwargs)
        return LibraryOverview(PAGE, PAGE, (LIST,))

    def get_favorites(self, **kwargs):
        self._call("favorites", **kwargs)
        return PAGE

    def set_favorite(self, reference, *, saved):
        self._call("set_favorite", reference, saved=saved)
        return FavoriteUpdate("12", saved)

    def get_history(self, **kwargs):
        self._call("history", **kwargs)
        return PAGE

    def get_booklists(self):
        self._call("booklists")
        return [LIST]

    def create_booklist(self, **kwargs):
        self._call("create", **kwargs)
        return Booklist("5", **kwargs)

    def update_booklist(self, identifier, **kwargs):
        self._call("update", identifier, **kwargs)
        return Booklist("5", "Renamed")

    def delete_booklist(self, identifier):
        self._call("delete", identifier)
        return BooklistDeletion(identifier)

    def get_booklist_books(self, identifier, **kwargs):
        self._call("entries", identifier, **kwargs)
        return BookPage((BooklistBook(**BOOK.to_dict(), booklist_id=identifier, entry_id="99"),), 1, 20)

    def add_booklist_book(self, identifier, reference):
        self._call("add", identifier, reference)
        return BooklistBookUpdate(identifier, "add", book_id="12")

    def remove_booklist_book(self, identifier, entry_id):
        self._call("remove", identifier, entry_id)
        return BooklistBookUpdate(identifier, "remove", entry_id=entry_id)

    def get_popular(self):
        self._call("popular")
        return [BOOK]

    def get_recommendations(self):
        self._call("recommendations")
        return [BOOK]

    def get_library_tags(self):
        self._call("library_tags")
        return LibraryTags(("Reading", "Old label " * 5))

    def get_saved_book_ids(self):
        self._call("saved_book_ids")
        return ("12", "98765432109876543210")

    def add_book_tags(self, reference, tags):
        self._call("add_tags", reference, tags)
        return BookTagUpdate("12", "add", tags)

    def remove_book_tags(self, reference, tags):
        self._call("remove_tags", reference, tags)
        return BookTagUpdate("12", "remove", tags)

    def close(self):
        self.closed = True


@contextmanager
def running(client=None, **kwargs):
    client = client or FakeClient()
    with create_server(client, token=TOKEN, port=0, **kwargs) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield server, client
        finally:
            server.shutdown()
            thread.join(3)


def request(server, method, path, *, body=None, headers=None, authenticate=True, raw=False):
    values = {"Authorization": "Bearer " + TOKEN} if authenticate else {}
    values.update(headers or {})
    if body is not None and not raw:
        body = json.dumps(body)
        values.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        connection.request(method, path, body=body, headers=values)
        response = connection.getresponse()
        data = response.read()
        return response.status, dict(response.getheaders()), json.loads(data) if data else None
    finally:
        connection.close()


def raw_request(server, data):
    with socket.create_connection(server.server_address, timeout=3) as connection:
        connection.sendall(data)
        connection.shutdown(socket.SHUT_WR)
        output = bytearray()
        while chunk := connection.recv(65536):
            output.extend(chunk)
    header, body = bytes(output).split(b"\r\n\r\n", 1)
    return int(header.split(b" ")[1]), json.loads(body)


class HttpApiTests(unittest.TestCase):
    def test_health_and_spec_require_token_without_upstream_access(self):
        with running() as (server, client):
            for path in ("/v1/health", "/v1/capabilities", "/v1/openapi.json"):
                status, headers, payload = request(server, "GET", path, authenticate=False)
                self.assertEqual(401, status)
                self.assertEqual("Bearer", headers["WWW-Authenticate"])
                self.assertFalse(payload["error"]["retryable"])
                self.assertNotIn(TOKEN, json.dumps(payload))
            status, headers, payload = request(server, "GET", "/v1/health")
            self.assertEqual((200, "not_checked"), (status, payload["upstream_status"]))
            self.assertEqual("no-store", headers["Cache-Control"])
            self.assertEqual([], client.calls)
            self.assertEqual("v1", request(server, "GET", "/v1/capabilities")[2]["api_version"])
        self.assertFalse(client.closed)

    def test_host_origin_and_preflight_policy(self):
        with running(allowed_origins=("http://localhost:9000",)) as (server, client):
            for headers in ({"Host": "evil.example"}, {"Origin": "http://evil.example"}, {"Origin": "null"}):
                status, response_headers, _ = request(server, "GET", "/v1/health", headers=headers)
                self.assertEqual(403, status)
                self.assertNotIn("Access-Control-Allow-Origin", response_headers)
            origin = {"Origin": "http://localhost:9000"}
            status, headers, _ = request(server, "GET", "/v1/health", headers=origin)
            self.assertEqual(200, status)
            self.assertEqual(origin["Origin"], headers["Access-Control-Allow-Origin"])
            self.assertEqual("Origin", headers["Vary"])
            preflight = {**origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "Authorization, Content-Type"}
            status, headers, payload = request(server, "OPTIONS", "/v1/booklists", headers=preflight, authenticate=False)
            self.assertEqual(204, status)
            self.assertIsNone(payload)
            self.assertEqual("POST", headers["Access-Control-Allow-Methods"])
            self.assertNotIn(TOKEN, json.dumps(headers))
            self.assertEqual(403, request(server, "OPTIONS", "/v1/booklists", authenticate=False)[0])
            self.assertEqual(405, request(server, "OPTIONS", "/v1/booklists", headers={**preflight, "Access-Control-Request-Method": "PUT"}, authenticate=False)[0])
            self.assertEqual(403, request(server, "OPTIONS", "/v1/booklists", headers={**preflight, "Access-Control-Request-Headers": "X-Secret"}, authenticate=False)[0])
            self.assertEqual([], client.calls)
        with running() as (server, _):
            self.assertEqual(403, request(server, "GET", "/v1/health", headers=origin)[0])

    def test_preflight_validates_route_queries_without_account_access(self):
        origin = "http://localhost:9000"
        headers = {"Origin": origin, "Access-Control-Request-Method": "GET",
                   "Access-Control-Request-Headers": "Authorization"}
        with running(allowed_origins=(origin,)) as (server, client):
            status, response_headers, payload = request(server, "OPTIONS", "/v1/search?q=test", headers=headers, authenticate=False)
            self.assertEqual(204, status)
            self.assertIsNone(payload)
            self.assertEqual(origin, response_headers["Access-Control-Allow-Origin"])
            self.assertEqual("GET", response_headers["Access-Control-Allow-Methods"])
            self.assertEqual(400, request(server, "OPTIONS", "/v1/search?q=test&unknown=value", headers=headers, authenticate=False)[0])
            self.assertEqual(403, request(server, "OPTIONS", "/v1/search?q=test",
                                         headers={**headers, "Access-Control-Request-Headers": "Authorization, Last-Event-ID"}, authenticate=False)[0])
            self.assertEqual([], client.calls)

    def test_pagination_and_search_filters(self):
        with running() as (server, client):
            status, _, result = request(server, "GET", "/v1/search?q=hello&page=2&limit=3&language=english&language=french&format=epub&year_from=1900&year_to=2000&order=popular")
            self.assertEqual(200, status)
            self.assertEqual(PAGE.to_dict(), result)
            name, args, kwargs = client.calls[-1]
            self.assertEqual(("search", ("hello",), 2, 3), (name, args, kwargs["page"], kwargs["limit"]))
            self.assertEqual(("english", "french"), kwargs["filters"].languages)
            self.assertEqual(("epub",), kwargs["filters"].formats)
            self.assertEqual(1900, kwargs["filters"].year_from)
            for path in ("/v1/favorites?page=2&limit=3&order=title", "/v1/history?page=2&limit=3&order=downloaded_dateA"):
                self.assertEqual(PAGE.to_dict(), request(server, "GET", path)[2])
            self.assertEqual("downloaded_dateA", client.calls[-1][2]["order"])

    def test_all_read_capabilities(self):
        with running() as (server, client):
            paths = ["/v1/books/12/abc", "/v1/books/12/abc/formats", "/v1/books/12/abc/similar",
                     "/v1/profile", "/v1/quota", "/v1/library", "/v1/booklists",
                     "/v1/booklists/5/books?page=2", "/v1/discovery/popular", "/v1/discovery/recommended"]
            for path in paths:
                with self.subTest(path=path):
                    self.assertEqual(200, request(server, "GET", path)[0])
            self.assertEqual(("book", ("12:abc",), {}), client.calls[0])
            self.assertEqual(5, client.calls[5][2]["limit"])
            entry = request(server, "GET", "/v1/booklists/5/books")[2]["books"][0]
            self.assertEqual(("12", "99"), (entry["book_id"], entry["entry_id"]))

    def test_booklist_and_favorite_writes(self):
        with running() as (server, client):
            status, _, result = request(server, "POST", "/v1/booklists", body={"title": "New"})
            self.assertEqual((201, False, 0), (status, result["is_public"], result["color"]))
            self.assertEqual({"title": "New", "description": "", "is_public": False, "color": 0}, client.calls[-1][2])
            self.assertEqual(200, request(server, "PATCH", "/v1/booklists/5", body={"title": "Renamed"})[0])
            self.assertEqual(("update", ("5",), {"title": "Renamed"}), client.calls[-1])
            self.assertEqual(200, request(server, "POST", "/v1/booklists/5/books", body={"reference": "12:abc"})[0])
            self.assertEqual(("add", ("5", "12:abc"), {}), client.calls[-1])
            self.assertEqual("99", request(server, "DELETE", "/v1/booklists/5/entries/99")[2]["entry_id"])
            self.assertEqual(("remove", ("5", "99"), {}), client.calls[-1])
            self.assertTrue(request(server, "PUT", "/v1/favorites/12/abc")[2]["is_favorite"])
            self.assertFalse(request(server, "DELETE", "/v1/favorites/12/abc")[2]["is_favorite"])
            self.assertEqual({"booklist_id": "5", "deleted": True}, request(server, "DELETE", "/v1/booklists/5")[2])

    def test_native_tags_and_saved_ids_are_separate_from_full_library(self):
        with running() as (server, client):
            self.assertEqual({"tags": ["Reading", "Old label " * 5]}, request(server, "GET", "/v1/my-library/tags")[2])
            self.assertEqual({"book_ids": ["12", "98765432109876543210"]}, request(server, "GET", "/v1/my-library/saved-ids")[2])
            for path in ("/v1/my-library/tags", "/v1/my-library/saved-ids"):
                self.assertEqual(401, request(server, "GET", path, authenticate=False)[0])
            self.assertEqual(404, request(server, "GET", "/v1/my-library")[0])
            self.assertIn("native_my_library_pages", request(server, "GET", "/v1/capabilities")[2]["unavailable"])
            self.assertEqual([("library_tags", (), {}), ("saved_book_ids", (), {})], client.calls)

    def test_tag_mutations_preserve_labels_and_announce_favorite_side_effect(self):
        path = "/v1/my-library/books/12/abc/tags"
        with running() as (server, client):
            tags = [" Read, later ", "Été", "été", "Été", "😀" * 15]
            status, _, result = request(server, "PUT", path, body={"tags": tags})
            expected = [" Read, later ", "Été", "été", "😀" * 15]
            self.assertEqual((200, "add", True, expected), (status, result["action"], result["adds_to_favorites"], result["tags"]))
            self.assertEqual(("add_tags", ("12:abc", tuple(expected)), {}), client.calls[-1])
            old_label = "Old label " * 200
            status, _, result = request(server, "DELETE", path, body={"tags": [old_label, "Été"]})
            self.assertEqual((200, "remove", False, [old_label, "Été"]), (status, result["action"], result["adds_to_favorites"], result["tags"]))
            self.assertEqual(("remove_tags", ("12:abc", (old_label, "Été")), {}), client.calls[-1])

    def test_tag_validation_blocks_invalid_changes_before_client_access(self):
        path = "/v1/my-library/books/12/abc/tags"
        with running() as (server, client):
            for tags in (["x" * 31], ["😀" * 16], ["😀" * 15 + "a"]):
                self.assertEqual(400, request(server, "PUT", path, body={"tags": tags})[0])
            for method in ("PUT", "DELETE"):
                for body in ({}, {"tags": []}, {"tags": "Reading"}, {"tags": [""]}, {"tags": ["   "]}, {"tags": [True]},
                             {"tags": ["x\n"]}, {"tags": ["x\u202e"]}, {"tags": ["x\ud800"]}, {"tags": ["x"] * 51}, {"tags": ["x"], "extra": 1}):
                    with self.subTest(method=method, body=body):
                        self.assertEqual(400, request(server, method, path, body=body)[0])
                self.assertEqual(401, request(server, method, path, body={"tags": ["Reading"]}, authenticate=False)[0])
            self.assertEqual([], client.calls)

    def test_tag_mutation_errors_and_openapi_contract(self):
        path = "/v1/my-library/books/12/abc/tags"
        with running() as (server, client):
            client.error = MutationUncertainError("private upstream response")
            for method in ("PUT", "DELETE"):
                status, _, result = request(server, method, path, body={"tags": ["Reading"]})
                self.assertEqual((409, "mutation_uncertain", False, "unknown"),
                    (status, result["error"]["code"], result["error"]["retryable"], result["error"]["outcome"]))
                self.assertNotIn("private upstream response", json.dumps(result))
        contract = openapi_document()["paths"]["/v1/my-library/books/{book_id}/{book_hash}/tags"]
        addition = contract["put"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["tags"]
        removal = contract["delete"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["tags"]
        self.assertEqual((1, 50, 30), (addition["minItems"], addition["maxItems"], addition["items"]["x-maxUtf16CodeUnits"]))
        self.assertNotIn("x-maxUtf16CodeUnits", removal["items"])
        self.assertGreater(removal["items"]["maxLength"], 30)

    def test_invalid_inputs_fail_before_account_access(self):
        with running() as (server, client):
            for path in ("/v1/search", "/v1/search?q=a&q=b", "/v1/search?q=a&limit=51", "/v1/search?q=a&page=0", "/v1/search?q=a&extra=1",
                         "/v1/search?q=a&year_from=2000&year_to=1900", "/v1/search?q=a&format=exe", "/v1/search?q=%FF", "/v1/search?q=%ZZ",
                         "/v1/search?q=a&language=", "/v1/books/../abc", "/v1/books/12/abc%2Fdef", "/v1/booklists/0/books"):
                with self.subTest(path=path):
                    self.assertEqual(400, request(server, "GET", path)[0])
            for method, path, body in (("POST", "/v1/booklists", {"title": ""}), ("POST", "/v1/booklists", {"title": "x", "color": True}),
                    ("POST", "/v1/booklists", {"title": "x", "is_public": "false"}), ("PATCH", "/v1/booklists/5", {}),
                    ("PATCH", "/v1/booklists/5", {"title": None}), ("POST", "/v1/booklists", {"title": "x", "password": "not-real"}),
                    ("POST", "/v1/booklists", {"title": "x\n"}), ("POST", "/v1/booklists/5/books", {"reference": "https://evil.example/book/1/abc"})):
                with self.subTest(body=body):
                    self.assertEqual(400, request(server, method, path, body=body)[0])
            for raw_body in ('{"title":"a","title":"b"}', '{"title":"a","color":NaN}', '[]', '{broken', '{"title":'):
                self.assertEqual(400, request(server, "POST", "/v1/booklists", body=raw_body, headers={"Content-Type": "application/json"}, raw=True)[0])
            self.assertEqual(415, request(server, "POST", "/v1/booklists", body="title=x", raw=True)[0])
            self.assertEqual([], client.calls)

    def test_rejected_request_delivers_json_when_body_arrives_after_headers(self):
        with running() as (server, client):
            for _ in range(5):
                connection = http.client.HTTPConnection(*server.server_address, timeout=3)
                try:
                    connection.putrequest("POST", "/v1/booklists")
                    connection.putheader("Authorization", "Bearer " + TOKEN)
                    connection.putheader("Content-Length", "7")
                    connection.endheaders()
                    # Exercise separate packets: the header refusal must still
                    # arrive when the body follows it, not reset the connection.
                    time.sleep(0.005)
                    connection.send(b"title=x")
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                    self.assertEqual((415, "unsupported_media_type"), (response.status, payload["error"]["code"]))
                finally:
                    connection.close()
            self.assertEqual([], client.calls)

    def test_close_drain_is_bounded_and_never_executes_pipelined_input(self):
        with running(max_connections=1) as (server, client):
            closed = threading.Event()
            original_shutdown = server.shutdown_request

            def observe_close(connection):
                original_shutdown(connection)
                closed.set()

            host = f"127.0.0.1:{server.server_address[1]}"
            first = (f"POST /v1/booklists HTTP/1.1\r\nHost: {host}\r\n"
                     f"Authorization: Bearer {TOKEN}\r\nContent-Length: 7\r\n\r\n").encode()
            second_body = b'{"title":"Must not be created"}'
            second = (f"POST /v1/booklists HTTP/1.1\r\nHost: {host}\r\n"
                      f"Authorization: Bearer {TOKEN}\r\nContent-Type: application/json\r\n"
                      f"Content-Length: {len(second_body)}\r\n\r\n").encode() + second_body
            with patch.object(server, "shutdown_request", observe_close), socket.create_connection(server.server_address, timeout=2) as connection:
                connection.sendall(first)
                time.sleep(0.005)
                connection.sendall(b"title=x" + second)
                response = b""
                while chunk := connection.recv(4096):
                    response += chunk
                self.assertEqual(1, response.count(b"HTTP/1.1 "))
                self.assertTrue(response.startswith(b"HTTP/1.1 415 "))
                self.assertEqual("unsupported_media_type", json.loads(response.split(b"\r\n\r\n", 1)[1])["error"]["code"])
                # The peer keeps its write side open: cleanup must not wait for
                # the five-second request timeout or process its next request.
                self.assertTrue(closed.wait(1), "Response cleanup exceeded its bounded drain.")
            self.assertEqual([], client.calls)

    def test_methods_and_unsupported_routes(self):
        with running() as (server, client):
            status, headers, _ = request(server, "PUT", "/v1/booklists", body={})
            self.assertEqual(405, status)
            self.assertEqual("GET, POST", headers["Allow"])
            for path in ("/v1/login", "/v1/my-library", "/v1/files/C:/secret"):
                self.assertEqual(404, request(server, "GET", path)[0])
            self.assertEqual(400, request(server, "GET", "/v1/health", body={})[0])
            self.assertEqual([], client.calls)

    def test_route_literals_are_exact(self):
        with running() as (server, client):
            self.assertEqual(200, request(server, "GET", "/v1/openapi.json")[0])
            for path in ("/v1/openapiXjson", "/v1/openapi/json", "/v1/openapi-json", "/v1/openapi.json/", "/v1/openapi.json.extra"):
                with self.subTest(path=path):
                    status, _, result = request(server, "GET", path)
                    self.assertEqual((404, "not_found"), (status, result["error"]["code"]))
            self.assertEqual([], client.calls)

    def test_missing_booklist_with_real_client_only_reads_and_returns_404(self):
        self.assertTrue(issubclass(BooklistNotFoundError, ValueError))
        for rows in ([], [{"id": 8, "title": "Other list"}]):
            transport = transport_for({"success": 1, "list": rows}, {"success": 1, "list": rows})
            with self.subTest(rows=rows), Client(ORIGIN, transport=transport) as client:
                client.login("synthetic", "synthetic")
                with self.assertRaises(BooklistNotFoundError):
                    client.update_booklist("5", title="Renamed")
                with running(client) as (server, _):
                    status, _, result = request(server, "PATCH", "/v1/booklists/5", body={"title": "Renamed"})
                self.assertEqual((404, "booklist_not_found", False, "not_applied"),
                    (status, result["error"]["code"], result["error"]["retryable"], result["error"]["outcome"]))
            self.assertEqual([ORIGIN + "papi/booklist/current-user/0"] * 2, [call[0] for call in transport.calls[2:]])
            self.assertEqual(["GET", "GET"], transport.methods[2:])
            self.assertEqual([None, None], transport.json_bodies[2:])

    def test_limits_and_wire_format(self):
        with running() as (server, client):
            host = f"127.0.0.1:{server.server_address[1]}".encode()
            base = b"Host: " + host + b"\r\nAuthorization: Bearer " + TOKEN.encode() + b"\r\n"
            cases = [
                (b"GET /v1/health HTTP/1.1\r\n" + base + b"Host: evil.example\r\n\r\n", 400),
                (b"GET /v1/health HTTP/1.1\r\n" + base + b" folded: bad\r\n\r\n", 400),
                (b"GET /v1/health HTTP/1.1\r\n" + base + b"Transfer-Encoding: chunked\r\n\r\n", 400),
                (b"GET /v1/health HTTP/1.1\r\n" + base + b"Content-Length: -1\r\n\r\n", 400),
                (b"POST /v1/booklists HTTP/1.1\r\n" + base + b"Content-Length: 65537\r\n\r\n", 413),
                (b"GET /v1/health HTTP/1.1\r\n" + base + b"X-Long: " + b"a" * 17000 + b"\r\n\r\n", 431),
                (b"GET /v1/health HTTP/1.1\r\n" + base + b"Expect: 100-continue\r\n\r\n", 417),
                (b"GET http://evil.example/v1/health HTTP/1.1\r\n" + base + b"\r\n", 400),
            ]
            for wire, expected in cases:
                with self.subTest(expected=expected, size=len(wire)):
                    status, result = raw_request(server, wire)
                    self.assertEqual(expected, status)
                    self.assertEqual({"code", "message", "retryable", "outcome"}, set(result["error"]))
            self.assertEqual(414, request(server, "GET", "/v1/search?q=" + "x" * 4100)[0])
            self.assertEqual([], client.calls)

    def test_errors_are_stable_and_redacted(self):
        secret = "never-echo-this-private-response"
        with running() as (server, client):
            for error, status, code, retryable in (
                    (AuthenticationError(secret), 424, "upstream_authentication_required", False),
                    (ChallengeRequired(secret), 502, "upstream_challenge", False),
                    (RateLimitError(secret), 429, "upstream_rate_limited", True),
                    (FormatChangedError(secret), 502, "upstream_protocol_changed", False),
                    (NetworkError(secret, reason="timeout"), 504, "upstream_timeout", True),
                    (NetworkError(secret), 502, "upstream_network_error", True),
                    (DownloadError(secret), 422, "upstream_refused", False),
                    (RuntimeError(secret), 500, "internal_error", False)):
                client.error = error
                actual, headers, body = request(server, "GET", "/v1/books/12/abc")
                self.assertEqual((status, code, retryable), (actual, body["error"]["code"], body["error"]["retryable"]))
                self.assertNotIn(secret, json.dumps(body) + json.dumps(headers))
            for error in (MutationUncertainError(secret), NetworkError(secret), RuntimeError(secret), ValueError(secret)):
                client.error = error
                status, _, body = request(server, "POST", "/v1/booklists", body={"title": "x"})
                self.assertEqual((409, "unknown", False), (status, body["error"]["outcome"], body["error"]["retryable"]))
            client.error = RateLimitError(secret)
            self.assertFalse(request(server, "PUT", "/v1/favorites/12/abc")[2]["error"]["retryable"])

    def test_diagnostics_are_explicit_and_anonymous(self):
        calls = []

        def diagnostics(**kwargs):
            calls.append(kwargs)
            return DomainReport((DomainDiagnostic("https://example.org/", True, "2026-01-01", 2),))

        with running(diagnostics=diagnostics) as (server, client):
            self.assertFalse(request(server, "GET", "/v1/domains")[2]["probed"])
            self.assertEqual([], calls)
            status, _, result = request(server, "POST", "/v1/domains/diagnostics", body={"domains": ["https://example.org"], "timeout": 2, "workers": 1})
            self.assertEqual((200, "https://example.org/"), (status, result["recommended_domain"]))
            self.assertEqual({"domains": ("https://example.org/",), "timeout": 2, "workers": 1}, calls[0])
            for domains in (["http://example.org"], ["https://example.org/path"], ["https://user:password@example.org"]):
                self.assertEqual(400, request(server, "POST", "/v1/domains/diagnostics", body={"domains": domains})[0])
            self.assertEqual([], client.calls)

    def test_session_access_is_serialized(self):
        class SlowClient(FakeClient):
            concurrent = 0
            maximum = 0

            def get_quota(self):
                self.concurrent += 1
                self.maximum = max(self.maximum, self.concurrent)
                time.sleep(0.03)
                self.concurrent -= 1
                return super().get_quota()

        client = SlowClient()
        with running(client) as (server, _), ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: request(server, "GET", "/v1/quota")[0], range(4)))
            self.assertEqual([200] * 4, results)
            self.assertEqual(1, client.maximum)

    def test_constructor_boundaries(self):
        for kwargs in ({"host": "0.0.0.0"}, {"host": "::1"}, {"host": "example.org"}, {"port": True},
                       {"allowed_origins": ("*",)}, {"allowed_origins": ("http://example.org/path",)}, {"max_connections": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                create_server(FakeClient(), token=TOKEN, **kwargs)
        for token in ("short", " " * 32, "é" * 32):
            with self.assertRaises(ValueError):
                create_server(FakeClient(), token=token)

    def test_slow_request_deadline(self):
        with running(request_timeout=0.1) as (server, client):
            with socket.create_connection(server.server_address, timeout=2) as slow:
                slow.sendall(b"GET /v1/health HTTP/1.1\r\nHost:")
                data = b""
                while chunk := slow.recv(4096):
                    data += chunk
                self.assertTrue(data.startswith(b"HTTP/1.1 408 "))
            self.assertEqual([], client.calls)

    def test_connection_limit_rejects_excess_without_reset(self):
        entered, release = threading.Event(), threading.Event()

        class WaitingClient(FakeClient):
            def get_quota(self):
                entered.set()
                release.wait(2)
                return super().get_quota()

        with running(WaitingClient(), max_connections=1) as (server, client), ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(request, server, "GET", "/v1/quota")
            try:
                self.assertTrue(entered.wait(1))
                status, _, payload = request(server, "GET", "/v1/health")
                self.assertEqual((503, True), (status, payload["error"]["retryable"]))
            finally:
                release.set()
            self.assertEqual(200, first.result()[0])
            self.assertEqual(1, len(client.calls))

    def test_header_count_and_body_timeout(self):
        with running(request_timeout=0.1) as (server, client):
            host = f"127.0.0.1:{server.server_address[1]}".encode()
            headers = b"Host: " + host + b"\r\nAuthorization: Bearer " + TOKEN.encode() + b"\r\n"
            status, _ = raw_request(server, b"GET /v1/health HTTP/1.1\r\n" + headers
                                    + b"".join(f"X-{index}: value\r\n".encode() for index in range(65)) + b"\r\n")
            self.assertEqual(431, status)
            with socket.create_connection(server.server_address, timeout=2) as slow:
                slow.sendall(b"POST /v1/booklists HTTP/1.1\r\n" + headers
                             + b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{\"title\":")
                data = b""
                while chunk := slow.recv(4096):
                    data += chunk
                self.assertTrue(data.startswith(b"HTTP/1.1 408 "))
            self.assertEqual([], client.calls)

    def test_unserializable_mutation_reply_is_uncertain(self):
        class InvalidReply:
            def to_dict(self):
                return {"unexpected": object()}

        client = FakeClient()
        client.create_booklist = lambda **kwargs: InvalidReply()
        with running(client) as (server, _):
            status, _, body = request(server, "POST", "/v1/booklists", body={"title": "x"})
            self.assertEqual((409, "unknown", False), (status, body["error"]["outcome"], body["error"]["retryable"]))

    def test_openapi_documents_effective_routes_and_validation(self):
        document = openapi_document()
        self.assertEqual("3.1.0", document["openapi"])
        self.assertEqual([{"bearerAuth": []}], document["security"])
        with running() as (server, _):
            self.assertEqual(document, request(server, "GET", "/v1/openapi.json")[2])
            operations = request(server, "GET", "/v1/capabilities")[2]["operations"]
        self.assertEqual(set(operations), {value["operationId"] for route in document["paths"].values()
            for value in route.values() if not value["operationId"].startswith("download_")})
        create = document["paths"]["/v1/booklists"]["post"]
        self.assertIn("201", create["responses"])
        self.assertFalse(create["requestBody"]["content"]["application/json"]["schema"]["additionalProperties"])
        self.assertEqual(1, document["paths"]["/v1/booklists/{booklist_id}"]["patch"]["requestBody"]["content"]["application/json"]["schema"]["minProperties"])
        self.assertIn("/v1/downloads", document["paths"])
        self.assertNotIn("/v1/my-library", document["paths"])


if __name__ == "__main__":
    unittest.main()
