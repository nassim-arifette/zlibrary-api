"""Local HTTP task and binary routes using synthetic managers and streams only."""

from contextlib import contextmanager
from dataclasses import replace
import http.client
import io
import json
import time
import unittest
from unittest.mock import patch

from zlib_connector.errors import AuthenticationError
from zlib_connector.http_api import create_server, openapi_document
from zlib_connector.jobs.files import DownloadFile
from zlib_connector.jobs.models import (DownloadTask, FileReceipt, TaskPage, JobError, JobConflictError,
    JobNotFoundError, JobCapacityError, JobStateError, JobFileError, JobBusyError, TASK_STATES, TASK_ERROR_CODES)
from test_http_api import TOKEN, FakeClient, request, running


TASK_ID = "1" * 32
FILE_BYTES = b"%PDF-1.7\n" + b"synthetic" * 20000


class TaskClient(FakeClient):
    domain = "https://books.example/"
    verified = True


class TrackedStream(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.read_sizes = []

    def read(self, count=-1):
        if not 0 < count <= 65536:
            raise AssertionError("File reads must be bounded.")
        self.read_sizes.append(count)
        return super().read(count)


class FakeManager:
    def __init__(self, client, directory, *, client_lock, max_tasks):
        if not client.verified:
            raise AuthenticationError("Synthetic account is not verified.")
        self.client, self.client_lock, self.directory, self.capacity = client, client_lock, directory, max_tasks
        self.calls, self.records, self.keys = [], {}, {}
        self.close_count = 0
        self.error = None
        self.file_name, self.content_type = "Été.pdf", "application/pdf"
        self.file_size = len(FILE_BYTES)
        self.stream_factory = lambda: TrackedStream(FILE_BYTES)
        self.streams = []

    def _call(self, operation):
        self.calls.append(operation)
        if self.error:
            raise self.error

    def submit(self, reference, *, idempotency_key):
        self._call("submit")
        if idempotency_key in self.keys:
            task = self.records[self.keys[idempotency_key]]
            if task.reference != reference:
                raise JobConflictError("private idempotency data")
            return task
        task = DownloadTask(f"{len(self.records) + 1:032x}", reference, "queued", 1, 1)
        self.records[task.id] = task
        self.keys[idempotency_key] = task.id
        return task

    def list(self, *, limit=50, offset=0):
        self._call("list")
        records = tuple(self.records.values())
        return TaskPage(records[offset:offset + limit], len(records), limit, offset)

    def get(self, task_id):
        self._call("get")
        if task_id not in self.records:
            raise JobNotFoundError("private record ID")
        return self.records[task_id]

    def cancel(self, task_id):
        self._call("cancel")
        task = self.get(task_id)
        if task.state != "succeeded":
            task = replace(task, state="cancelled", cancel_requested=True, error_code="Cancelled")
            self.records[task_id] = task
        return task

    def resume(self):
        self._call("resume")
        return {"resumed": 0}

    def completed(self):
        receipt = FileReceipt("book.pdf", self.file_size, 1, 2, 3, 4, 1, 5)
        task = DownloadTask(TASK_ID, "12:abc", "succeeded", 1, 2, receipt=receipt)
        self.records[task.id] = task
        return task

    @contextmanager
    def open_file(self, task_id):
        self._call("file")
        task = self.get(task_id)
        if task.state != "succeeded":
            raise JobStateError("private state details")
        stream = self.stream_factory()
        self.streams.append(stream)
        try:
            yield DownloadFile(stream, self.file_name, self.content_type, self.file_size)
        finally:
            stream.close()

    def close(self):
        self.close_count += 1


@contextmanager
def download_server(*, enabled=True, client=None, **kwargs):
    managers = []

    def factory(*args, **options):
        manager = FakeManager(*args, **options)
        managers.append(manager)
        return manager

    with patch("zlib_connector.http_api.downloads._new_manager", side_effect=factory) as construct:
        with running(client or TaskClient(), download_dir="configured-root" if enabled else None, **kwargs) as (server, client):
            yield server, client, managers, construct


def binary_request(server, path, *, headers=None, authenticate=True):
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    values = {"Authorization": "Bearer " + TOKEN} if authenticate else {}
    values.update(headers or {})
    try:
        connection.request("GET", path, headers=values)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class HttpDownloadTests(unittest.TestCase):
    def test_disabled_routes_have_no_manager_or_client_side_effects(self):
        with download_server(enabled=False) as (server, client, managers, construct):
            routes = [("GET", "/v1/downloads", None), ("GET", f"/v1/downloads/{TASK_ID}", None),
                      ("POST", "/v1/downloads", {"reference": "12:abc", "idempotency_key": "request-123"}),
                      ("POST", f"/v1/downloads/{TASK_ID}/cancel", None), ("POST", "/v1/downloads/resume", None),
                      ("GET", f"/v1/downloads/{TASK_ID}/file", None)]
            for method, path, body in routes:
                status, _, payload = request(server, method, path, body=body)
                self.assertEqual((503, "downloads_unavailable"), (status, payload["error"]["code"]))
            capabilities = request(server, "GET", "/v1/capabilities")[2]
            self.assertFalse(capabilities["downloads_enabled"])
            self.assertIn("http_downloads", capabilities["unavailable"])
            self.assertFalse(any(value.startswith("download_") for value in capabilities["operations"]))
            construct.assert_not_called()
            self.assertEqual([], client.calls)

    def test_enable_is_explicit_and_manager_closes_once_without_closing_client(self):
        with download_server(enabled=False) as (server, client, managers, construct):
            server.enable_downloads("configured-root", max_tasks=17)
            manager = managers[0]
            self.assertIs(server._client_lock, manager.client_lock)
            self.assertEqual(17, manager.capacity)
            with self.assertRaises(ValueError):
                server.enable_downloads("different-root")
            construct.assert_called_once()
            capabilities = request(server, "GET", "/v1/capabilities")[2]
            self.assertTrue(capabilities["downloads_enabled"])
            self.assertNotIn("http_downloads", capabilities["unavailable"])
            self.assertIn("download_submit", capabilities["operations"])
        self.assertEqual(1, manager.close_count)
        self.assertFalse(client.closed)
        server.server_close()
        self.assertEqual(1, manager.close_count)

    def test_unverified_enable_and_invalid_capacity_fail_before_worker_creation(self):
        client = TaskClient()
        client.verified = False
        with download_server(enabled=False, client=client) as (server, _, managers, construct):
            with self.assertRaises(ValueError):
                server.enable_downloads("configured-root", max_tasks=0)
            construct.assert_not_called()
            with self.assertRaises(AuthenticationError):
                server.enable_downloads("configured-root")
            self.assertEqual([], managers)
            self.assertFalse(request(server, "GET", "/v1/capabilities")[2]["downloads_enabled"])

    def test_submit_replay_conflict_pagination_cancel_and_resume(self):
        with download_server() as (server, _, managers, _):
            body = {"reference": "https://books.example/book/12/abc/title", "idempotency_key": "request-123"}
            status, _, task = request(server, "POST", "/v1/downloads", body=body)
            self.assertEqual((202, "12:abc", "queued"), (status, task["reference"], task["state"]))
            self.assertNotIn("request-123", json.dumps(task))
            self.assertNotIn("receipt", task)
            self.assertEqual(task, request(server, "POST", "/v1/downloads", body=body)[2])
            self.assertEqual(409, request(server, "POST", "/v1/downloads", body={**body, "reference": "99:def"})[0])
            page = request(server, "GET", "/v1/downloads?limit=1&offset=0")[2]
            self.assertEqual({"tasks": [task], "total": 1, "limit": 1, "offset": 0, "paused": False}, page)
            self.assertEqual(task, request(server, "GET", f"/v1/downloads/{task['id']}")[2])
            cancelled = request(server, "POST", f"/v1/downloads/{task['id']}/cancel")[2]
            self.assertEqual(("cancelled", True), (cancelled["state"], cancelled["cancel_requested"]))
            self.assertEqual({"resumed": 0}, request(server, "POST", "/v1/downloads/resume")[2])
            self.assertEqual(1, len(managers[0].records))

    def test_inputs_are_closed_and_static_routes_take_precedence(self):
        with download_server() as (server, _, managers, _):
            for body in ({"reference": "12:abc"}, {"reference": "12:abc", "idempotency_key": "short"},
                         {"reference": "12:abc", "idempotency_key": "valid-key", "directory": "C:/private"},
                         {"reference": "https://other.example/book/12/abc", "idempotency_key": "valid-key"},
                         {"reference": "C:/private/book.pdf", "idempotency_key": "valid-key"}):
                self.assertEqual(400, request(server, "POST", "/v1/downloads", body=body)[0])
            for path in ("/v1/downloads?limit=1001", "/v1/downloads?offset=-1", "/v1/downloads?path=C:/private", "/v1/downloads/not-an-id"):
                self.assertEqual(400, request(server, "GET", path)[0])
            self.assertEqual(400, request(server, "POST", "/v1/downloads/resume", body={})[0])
            self.assertEqual(405, request(server, "GET", "/v1/downloads/resume")[0])
            self.assertEqual(405, request(server, "DELETE", f"/v1/downloads/{TASK_ID}")[0])
            self.assertEqual([], managers[0].calls)

    def test_task_errors_are_typed_and_do_not_leak_private_details(self):
        with download_server() as (server, _, managers, _):
            manager = managers[0]
            for kind, expected, code in ((JobNotFoundError, 404, "download_not_found"),
                    (JobConflictError, 409, "download_conflict"), (JobCapacityError, 503, "download_capacity"),
                    (JobBusyError, 503, "downloads_busy"), (JobError, 503, "download_storage_error")):
                manager.error = kind("private account, token or path")
                status, _, body = request(server, "GET", "/v1/downloads")
                self.assertEqual((expected, code), (status, body["error"]["code"]))
                self.assertNotIn("private account", json.dumps(body))
            manager.error = JobError("private storage error")
            body = request(server, "POST", "/v1/downloads", body={"reference": "12:abc", "idempotency_key": "valid-key"})[2]
            self.assertEqual("unknown", body["error"]["outcome"])
            self.assertFalse(body["error"]["retryable"])

    def test_local_routes_remain_responsive_while_client_lock_is_held(self):
        with download_server(request_timeout=0.1) as (server, client, managers, _):
            managers[0].completed()
            server._client_lock.acquire()
            try:
                self.assertEqual(200, request(server, "GET", "/v1/downloads")[0])
                self.assertEqual(200, request(server, "GET", f"/v1/downloads/{TASK_ID}")[0])
                self.assertEqual(200, request(server, "POST", f"/v1/downloads/{TASK_ID}/cancel")[0])
                self.assertEqual(200, request(server, "POST", "/v1/downloads/resume")[0])
                self.assertEqual(FILE_BYTES, binary_request(server, f"/v1/downloads/{TASK_ID}/file")[2])
                self.assertEqual(503, request(server, "GET", "/v1/quota")[0])
            finally:
                server._client_lock.release()
            self.assertEqual([], client.calls)

    def test_binary_files_keep_auth_host_origin_and_range_policy(self):
        with download_server(allowed_origins=("http://localhost:9000",)) as (server, _, managers, _):
            manager = managers[0]
            manager.completed()
            path = f"/v1/downloads/{TASK_ID}/file"
            for options, expected in (({"authenticate": False}, 401), ({"headers": {"Host": "evil.example"}}, 403),
                                      ({"headers": {"Origin": "http://evil.example"}}, 403), ({"headers": {"Range": "bytes=0-2"}}, 400)):
                status, _, body = binary_request(server, path, **options)
                self.assertEqual(expected, status)
                self.assertIn("error", json.loads(body))
            self.assertEqual([], manager.calls)
            status, headers, data = binary_request(server, path, headers={"Origin": "http://localhost:9000"})
            self.assertEqual((200, FILE_BYTES), (status, data))
            self.assertEqual(str(len(FILE_BYTES)), headers["Content-Length"])
            self.assertEqual("application/pdf", headers["Content-Type"])
            self.assertEqual("none", headers["Accept-Ranges"])
            self.assertEqual("no-store", headers["Cache-Control"])
            self.assertEqual("Origin", headers["Vary"])
            self.assertEqual("http://localhost:9000", headers["Access-Control-Allow-Origin"])
            self.assertIn("filename*=UTF-8''%C3%89t%C3%A9.pdf", headers["Content-Disposition"])
            self.assertNotIn("configured-root", json.dumps(headers))
            self.assertTrue(manager.streams[0].closed)
            self.assertLessEqual(max(manager.streams[0].read_sizes), 65536)

    def test_file_readiness_removal_and_unsafe_metadata_have_json_errors(self):
        with download_server() as (server, _, managers, _):
            manager = managers[0]
            path = f"/v1/downloads/{TASK_ID}/file"
            self.assertEqual(404, binary_request(server, path)[0])
            manager.records[TASK_ID] = DownloadTask(TASK_ID, "12:abc", "queued", 1, 1)
            self.assertEqual(409, binary_request(server, path)[0])
            manager.error = JobFileError("private local filename")
            status, _, body = binary_request(server, path)
            self.assertEqual((410, "download_file_unavailable"), (status, json.loads(body)["error"]["code"]))
            self.assertNotIn(b"private local filename", body)
            manager.error = None
            manager.completed()
            manager.file_name = "../private.pdf"
            self.assertEqual(410, binary_request(server, path)[0])
            self.assertTrue(manager.streams[-1].closed)

    def test_midstream_change_truncates_file_without_appending_json(self):
        class ChangedStream(TrackedStream):
            def read(self, count=-1):
                if self.tell():
                    raise JobFileError("private changed file")
                return super().read(count)

        with download_server() as (server, _, managers, _):
            manager = managers[0]
            manager.completed()
            manager.stream_factory = lambda: ChangedStream(FILE_BYTES)
            with self.assertRaises(http.client.IncompleteRead) as caught:
                binary_request(server, f"/v1/downloads/{TASK_ID}/file")
            self.assertEqual(FILE_BYTES[:65536], caught.exception.partial)
            self.assertNotIn(b'"error"', caught.exception.partial)
            self.assertTrue(manager.streams[0].closed)

    def test_file_response_has_an_absolute_deadline(self):
        class SlowStream(TrackedStream):
            def read(self, count=-1):
                time.sleep(0.1)  # Well past the deadline even with Windows' 15.6 ms monotonic clock ticks.
                return super().read(count)

        with download_server() as (server, _, managers, _):
            manager = managers[0]
            manager.completed()
            manager.stream_factory = lambda: SlowStream(FILE_BYTES)
            with patch("zlib_connector.http_api.downloads.FILE_RESPONSE_TIMEOUT", 0.02), self.assertRaises(http.client.IncompleteRead) as caught:
                binary_request(server, f"/v1/downloads/{TASK_ID}/file")
            self.assertEqual(b"", caught.exception.partial)
            self.assertTrue(manager.streams[0].closed)

    def test_openapi_describes_tasks_binary_files_and_conditional_availability(self):
        document = openapi_document()
        schemas = document["components"]["schemas"]
        self.assertEqual(list(TASK_STATES), schemas["DownloadTask"]["properties"]["state"]["enum"])
        self.assertEqual([*TASK_ERROR_CODES, None], schemas["DownloadTask"]["properties"]["error_code"]["enum"])
        self.assertNotIn("receipt", schemas["DownloadTask"]["properties"])
        self.assertEqual("boolean", schemas["TaskPage"]["properties"]["paused"]["type"])
        self.assertIn("paused", schemas["TaskPage"]["required"])
        create = document["paths"]["/v1/downloads"]["post"]
        self.assertIn("202", create["responses"])
        self.assertIn("503 downloads_unavailable", create["description"])
        binary = document["paths"]["/v1/downloads/{task_id}/file"]["get"]
        self.assertIn("application/pdf", binary["responses"]["200"]["content"])
        self.assertNotIn("application/json", binary["responses"]["200"]["content"])
        self.assertIn("410", binary["responses"])
        self.assertEqual("none", binary["responses"]["200"]["headers"]["Accept-Ranges"]["schema"]["const"])
        with download_server() as (server, _, _, _):
            operations = request(server, "GET", "/v1/capabilities")[2]["operations"]
        self.assertEqual(set(operations), {operation["operationId"] for path in document["paths"].values() for operation in path.values()})


if __name__ == "__main__":
    unittest.main()
