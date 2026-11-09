"""Finite authenticated SSE over loopback sockets, with synthetic task state."""

from contextlib import contextmanager
from dataclasses import replace
import http.client
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

from zlib_connector.http_api import openapi_document
from zlib_connector.jobs.events import TaskEvent
from zlib_connector.jobs.models import JobBusyError, JobError, JobNotFoundError, TERMINAL_STATES
from test_http_api import TOKEN, request, running
from test_http_downloads import TASK_ID, FakeManager, TaskClient


CURSOR = "a" * 32 + ":1"
PATH = f"/v1/downloads/{TASK_ID}/events"


class FakeSubscription:
    def __init__(self, manager, after):
        self.manager, self.after = manager, after
        self.closed = False
        self.released = threading.Event()
        self.waiting = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        with self.manager.condition:
            self.closed = True
            self.released.set()
            self.manager.condition.notify_all()

    def next(self, timeout=5):
        deadline = time.monotonic() + timeout
        with self.manager.condition:
            while not self.closed:
                if self.manager.read_error:
                    raise self.manager.read_error
                event = self.manager.current
                if event.id != self.after or event.task.state in TERMINAL_STATES:
                    self.after = event.id
                    if event.task.state in TERMINAL_STATES:
                        self.closed = True
                    return event
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.waiting.set()
                self.manager.condition.wait(remaining)
            return None


class EventManager(FakeManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.condition = threading.Condition()
        self.subscriptions, self.watch_calls = [], []
        self.read_error = None
        self.current = TaskEvent(CURSOR, replace(self.completed(), state="running", receipt=None), False)

    def watch(self, task_id, *, after=None):
        self._call("watch")
        if task_id != TASK_ID:
            raise JobNotFoundError("private task ID")
        subscription = FakeSubscription(self, after)
        self.watch_calls.append((task_id, after))
        self.subscriptions.append(subscription)
        return subscription

    def terminal(self):
        with self.condition:
            self.current = TaskEvent(CURSOR, self.completed(), False)
            self.condition.notify_all()

    def close(self):
        super().close()
        for subscription in self.subscriptions:
            subscription.close()


@contextmanager
def event_server(**kwargs):
    managers = []

    def factory(*args, **options):
        manager = EventManager(*args, **options)
        managers.append(manager)
        return manager

    with patch("zlib_connector.http_api.downloads._new_manager", side_effect=factory):
        with running(TaskClient(), download_dir="synthetic-configured-root", **kwargs) as (server, client):
            yield server, client, managers[0]


def open_events(server, path=PATH, *, headers=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        connection.request("GET", path, headers={"Authorization": "Bearer " + TOKEN, **(headers or {})})
        return connection, connection.getresponse()
    except BaseException:
        connection.close()
        raise


def read_events(server, path=PATH, *, headers=None):
    connection, response = open_events(server, path, headers=headers)
    try:
        return response.status, dict(response.getheaders()), response.read()
    finally:
        response.close()
        connection.close()


def frames(body):
    result = []
    for frame in body.decode("utf-8").split("\n\n"):
        if not frame or frame.startswith(":"):
            continue
        fields = dict(line.split(": ", 1) for line in frame.splitlines())
        fields["data"] = json.loads(fields["data"])
        result.append(fields)
    return result


class HttpEventTests(unittest.TestCase):
    def test_terminal_snapshot_has_explicit_end_and_no_http_body_framing(self):
        with event_server() as (server, client, manager):
            manager.terminal()
            status, headers, body = read_events(server)
            self.assertEqual(200, status)
            self.assertEqual("text/event-stream; charset=utf-8", headers["Content-Type"])
            self.assertEqual("no-store", headers["Cache-Control"])
            self.assertEqual("close", headers["Connection"])
            for name in ("Content-Length", "Transfer-Encoding", "Content-Encoding"):
                self.assertNotIn(name, headers)
            self.assertEqual([{"event": "task", "id": CURSOR, "data": manager.current.to_dict()},
                              {"event": "end", "data": {"reason": "terminal"}}], frames(body))
            self.assertTrue(manager.subscriptions[0].released.wait(1))
            self.assertEqual([], client.calls)
            self.assertNotIn(b"synthetic-configured-root", body)

    def test_query_and_header_cursors_are_alternatives_and_terminal_is_not_suppressed(self):
        with event_server() as (server, _, manager):
            manager.terminal()
            for path, headers in ((PATH + "?after=" + CURSOR + "&duration=1.25", {}),
                                  (PATH, {"Last-Event-ID": CURSOR})):
                self.assertEqual("terminal", frames(read_events(server, path, headers=headers)[2])[-1]["data"]["reason"])
                self.assertEqual((TASK_ID, CURSOR), manager.watch_calls[-1])
            for suffix in ("?duration=0.9", "?duration=61", "?duration=nan", "?duration=1e2", "?duration=1&duration=2",
                           "?after=private-invalid-cursor", "?token=private", "?after=" + "a" * 32 + ":" + "1" * 21):
                status, _, payload = request(server, "GET", PATH + suffix)
                self.assertEqual(400, status)
                self.assertNotIn("private", json.dumps(payload))
            self.assertEqual(400, request(server, "GET", PATH + "?after=" + CURSOR, headers={"Last-Event-ID": CURSOR})[0])
            self.assertEqual(400, request(server, "GET", PATH, headers={"Last-Event-ID": "invalid"})[0])
            self.assertEqual(2, len(manager.watch_calls))

    def test_auth_origin_host_and_subscription_failures_remain_json_before_200(self):
        with event_server() as (server, _, manager):
            for options, status in (({"authenticate": False}, 401), ({"headers": {"Host": "evil.example"}}, 403),
                                    ({"headers": {"Origin": "http://evil.example"}}, 403)):
                self.assertEqual(status, request(server, "GET", PATH, **options)[0])
            self.assertEqual([], manager.watch_calls)
            self.assertEqual(404, request(server, "GET", PATH.replace(TASK_ID, "2" * 32))[0])
            manager.error = JobBusyError("private manager state")
            self.assertEqual(503, request(server, "GET", PATH)[0])
            manager.error = None
            manager.terminal()
            self.assertEqual(200, read_events(server)[0])
        with running(TaskClient()) as (server, client):
            status, _, payload = request(server, "GET", PATH)
            self.assertEqual((503, "downloads_unavailable"), (status, payload["error"]["code"]))
            self.assertEqual([], client.calls)

    def test_browser_preflight_allows_cursor_header_and_valid_event_query_only(self):
        origin = "http://localhost:9000"
        with event_server(allowed_origins=(origin,)) as (server, _, manager):
            headers = {"Origin": origin, "Access-Control-Request-Method": "GET",
                       "Access-Control-Request-Headers": "Authorization, Last-Event-ID"}
            status, response_headers, _ = request(server, "OPTIONS", PATH + "?duration=1.5", headers=headers, authenticate=False)
            self.assertEqual(204, status)
            self.assertIn("Last-Event-ID", response_headers["Access-Control-Allow-Headers"])
            self.assertEqual(origin, response_headers["Access-Control-Allow-Origin"])
            self.assertEqual("Origin", response_headers["Vary"])
            self.assertEqual(400, request(server, "OPTIONS", PATH + "?token=bad", headers=headers, authenticate=False)[0])
            self.assertEqual(403, request(server, "OPTIONS", "/v1/health", headers=headers, authenticate=False)[0])
            self.assertEqual([], manager.watch_calls)
            manager.terminal()
            self.assertEqual(origin, read_events(server, headers={"Origin": origin, "Last-Event-ID": CURSOR})[1]["Access-Control-Allow-Origin"])

    def test_idle_duration_has_heartbeats_and_releases_subscription_without_client_lock(self):
        with event_server() as (server, client, manager), patch("zlib_connector.http_api.events.HEARTBEAT_INTERVAL", 0.05):
            server._client_lock.acquire()
            try:
                status, _, body = read_events(server, PATH + "?duration=1&after=" + CURSOR)
            finally:
                server._client_lock.release()
            self.assertEqual(200, status)
            self.assertIn(b": heartbeat\n\n", body)
            self.assertEqual([{"event": "end", "data": {"reason": "duration"}}], frames(body))
            self.assertTrue(manager.subscriptions[0].released.wait(1))
            self.assertEqual([], client.calls)

    def test_stream_cap_reserves_ordinary_connections_and_disconnect_releases_slots(self):
        with event_server(max_connections=4) as (server, _, manager), patch("zlib_connector.http_api.events.HEARTBEAT_INTERVAL", 0.05):
            opened = []
            try:
                for _ in range(2):
                    opened.append(open_events(server, PATH + "?duration=3&after=" + CURSOR))
                status, _, payload = request(server, "GET", PATH)
                self.assertEqual((503, "event_streams_busy"), (status, payload["error"]["code"]))
                self.assertEqual(200, request(server, "GET", "/v1/quota")[0])
                for connection, response in opened:
                    response.close()
                    connection.close()
                for subscription in manager.subscriptions:
                    self.assertTrue(subscription.released.wait(2))
                manager.terminal()
                self.assertEqual(200, read_events(server)[0])
            finally:
                for connection, response in opened:
                    response.close()
                    connection.close()

    def test_slow_reader_cannot_keep_a_stream_slot_past_its_send_timeout(self):
        class BusySubscription(FakeSubscription):
            sequence = 0

            def next(self, timeout=5):
                self.sequence += 1
                return TaskEvent("a" * 32 + ":" + str(self.sequence), self.manager.current.task, False)

        with event_server(request_timeout=0.2) as (server, _, manager):
            subscription = BusySubscription(manager, None)
            original_process_request = server.process_request

            def small_send_buffer(connection, address):
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
                return original_process_request(connection, address)

            with patch.object(manager, "watch", return_value=subscription), patch.object(server, "process_request", side_effect=small_send_buffer):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as peer:
                    peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
                    peer.settimeout(3)
                    peer.connect(server.server_address)
                    peer.sendall((f"GET {PATH}?duration=1 HTTP/1.1\r\nHost: 127.0.0.1:{server.server_address[1]}\r\n"
                                  f"Authorization: Bearer {TOKEN}\r\n\r\n").encode("ascii"))
                    # Do not read: both small socket buffers force backpressure.
                    self.assertTrue(subscription.released.wait(3))
                    self.assertGreater(subscription.sequence, 1)
            self.assertEqual(200, request(server, "GET", "/v1/health")[0])

    def test_manager_shutdown_wakes_waiting_stream_and_emits_shutdown_end(self):
        with event_server() as (server, _, manager):
            connection, response = open_events(server, PATH + "?after=" + CURSOR)
            try:
                self.assertTrue(manager.subscriptions[0].waiting.wait(1))
                manager.close()
                self.assertEqual([{"event": "end", "data": {"reason": "shutdown"}}], frames(response.read()))
                self.assertTrue(manager.subscriptions[0].released.is_set())
            finally:
                response.close()
                connection.close()

    def test_failure_after_headers_terminates_without_json_or_fabricated_end(self):
        with event_server(max_connections=2) as (server, _, manager):
            manager.read_error = JobError("private storage failure")
            status, _, body = read_events(server)
            self.assertEqual((200, b""), (status, body))
            self.assertTrue(manager.subscriptions[0].released.wait(1))
            manager.read_error = None
            manager.terminal()
            self.assertEqual(200, read_events(server)[0])

    def test_oversized_frame_is_not_sent_and_subscription_is_released(self):
        with event_server() as (server, _, manager):
            with patch.object(TaskEvent, "to_dict", return_value={"data": "x" * 65536}):
                status, _, body = read_events(server)
            self.assertEqual((200, b""), (status, body))
            self.assertTrue(manager.subscriptions[0].released.wait(1))

    def test_openapi_and_capabilities_describe_the_actual_stream_contract(self):
        document = openapi_document()
        operation = document["paths"]["/v1/downloads/{task_id}/events"]["get"]
        self.assertEqual({"text/event-stream"}, set(operation["responses"]["200"]["content"]))
        parameters = {(item["in"], item["name"]): item for item in operation["parameters"]}
        self.assertEqual(60, parameters[("query", "duration")]["schema"]["maximum"])
        self.assertIn(("query", "after"), parameters)
        self.assertIn(("header", "Last-Event-ID"), parameters)
        self.assertIn("never both", operation["description"])
        self.assertEqual(["terminal", "duration", "shutdown"], document["components"]["schemas"]["TaskEventEnd"]["properties"]["reason"]["enum"])
        with event_server() as (server, _, _):
            self.assertIn("download_events", request(server, "GET", "/v1/capabilities")[2]["operations"])
        with running() as (server, _):
            self.assertNotIn("download_events", request(server, "GET", "/v1/capabilities")[2]["operations"])


if __name__ == "__main__":
    unittest.main()
