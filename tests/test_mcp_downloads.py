"""MCP download tools on a real task journal with a synthetic transfer; no network, no real book."""

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from mcp_fixture import (ACCOUNT_ID, COOKIE_SECRET, SIGNED_LINK, World, error_of, save_session, structured)
from mcp_schema import validate
from session_fixture import isolated_sessions
from test_mcp_protocol import INITIALIZE, line, spawn
from test_mcp_tools import McpTestCase
from zlib_connector.errors import NetworkError


REF = "12:abc"


@isolated_sessions
class DownloadToolTests(McpTestCase):
    def serve_downloads(self, *flags, world=None):
        return self.serve("--download-dir", str(self.books), *flags, world=world)

    def wait_state(self, harness, task_id, states, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = structured(harness.call("get_download", {"task_id": task_id}))["task"]
            if task["state"] in states:
                return task
            time.sleep(0.02)
        self.fail(f"Task did not reach {states}")

    def schemas(self, harness):
        return {tool["name"]: tool["outputSchema"] for tool in self.tools(harness)}

    def test_d1_d8_repeat_is_a_lookup_and_the_file_path_is_inside_the_folder(self):
        world = World()
        harness = self.serve_downloads(world=world)
        schemas = self.schemas(harness)
        first = structured(harness.call("download_book", {"reference": REF}))
        validate(first, schemas["download_book"])
        self.assertIs(first["existing"], False)
        self.assertEqual(first["book"]["reference"], REF)
        self.assertEqual(first["quota"], {"limit": 10, "used": 3, "remaining": 7})
        task_id = first["task"]["task_id"]
        done = self.wait_state(harness, task_id, {"succeeded"})
        path = Path(done["file"]["path"])
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.parent.parent, self.books.resolve())
        self.assertTrue(path.is_file())
        self.assertEqual(done["outcome"], "completed")
        self.assertRegex(done["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        second = structured(harness.call("download_book", {"reference": REF}))
        self.assertIs(second["existing"], True)
        self.assertEqual(second["task"]["task_id"], task_id)
        self.assertIsNone(second["book"])
        self.assertEqual(world.link_requests, 1)
        listing = structured(harness.call("list_downloads"))
        validate(listing, schemas["list_downloads"])
        self.assertEqual((listing["total"], listing["order"]), (1, "newest"))
        harness.close()

        restarted = self.serve_downloads(world=world)
        again = structured(restarted.call("download_book", {"reference": REF}))
        self.assertIs(again["existing"], True)
        self.assertEqual(world.link_requests, 1)
        path.unlink()
        missing = structured(restarted.call("get_download", {"task_id": task_id}))["task"]
        self.assertIsNone(missing["file"])
        self.assertIs(missing["file_missing"], True)
        for name in os.listdir(self.books):
            self.assertTrue(name == ".zlib-jobs" or name == task_id, name)

    def test_d2_retry_rules(self):
        world = World()
        world.download_failures = [NetworkError("private")]
        harness = self.serve_downloads(world=world)
        failed_id = structured(harness.call("download_book", {"reference": REF}))["task"]["task_id"]
        failed = self.wait_state(harness, failed_id, {"failed"})
        self.assertEqual((failed["outcome"], failed["error_code"]), ("unknown", "NetworkError"))
        self.assertIn("quota outcome is unknown", failed["message"])
        time.sleep(0.2)
        self.assertEqual(world.link_requests, 1)
        good_id = structured(harness.call("download_book", {"reference": "13:def"}))["task"]["task_id"]
        self.wait_state(harness, good_id, {"succeeded"})
        refused = ((REF, good_id), ("13:def", good_id), (REF, "f" * 32), ("13:def", failed_id))
        for reference, retry in refused:
            with self.subTest(reference=reference, retry=retry):
                error = error_of(harness.call("download_book", {"reference": reference, "retry_task_id": retry}))
                self.assertEqual(error["code"], "retry_not_allowed")
        retried = structured(harness.call("download_book", {"reference": REF, "retry_task_id": failed_id}))
        self.assertIs(retried["existing"], False)
        self.assertNotEqual(retried["task"]["task_id"], failed_id)
        self.wait_state(harness, retried["task"]["task_id"], {"succeeded"})
        repeat = structured(harness.call("download_book", {"reference": REF, "retry_task_id": failed_id}))
        self.assertIs(repeat["existing"], True)
        self.assertEqual(world.link_requests, 3)
        newest = structured(harness.call("list_downloads", {"limit": 2}))
        self.assertEqual([task["task_id"] for task in newest["tasks"]], [retried["task"]["task_id"], good_id])
        oldest = structured(harness.call("list_downloads", {"order": "oldest", "offset": 1, "limit": 1}))
        self.assertEqual([task["task_id"] for task in oldest["tasks"]], [good_id])
        self.assertEqual(structured(harness.call("list_downloads", {"offset": 9}))["tasks"], [])

    def test_d3_d4_prechecks_create_no_task(self):
        world = World()
        world.remaining = 0
        world.extensions["14:ghi"] = "mobi"
        harness = self.serve_downloads(world=world)
        error = error_of(harness.call("download_book", {"reference": REF}))
        self.assertEqual((error["code"], error["outcome"]), ("quota_exhausted", "not_applied"))
        error = error_of(harness.call("download_book", {"reference": "14:ghi"}))
        self.assertEqual(error["code"], "format_not_supported")
        self.assertEqual(structured(harness.call("list_downloads"))["total"], 0)
        self.assertEqual(world.link_requests, 0)

    def test_d3_quota_check_is_skipped_while_a_transfer_holds_the_account(self):
        world = World()
        world.download_gate = threading.Event()
        with patch("zlib_connector.mcp_server.backend.QUOTA_WAIT", 0.2):
            harness = self.serve_downloads(world=world)
        try:
            first = structured(harness.call("download_book", {"reference": REF}))["task"]["task_id"]
            self.assertTrue(world.download_started.wait(3))
            second = structured(harness.call("download_book", {"reference": "13:def"}))
            self.assertIsNone(second["quota"])
            self.assertEqual(second["task"]["state"], "queued")
            found = structured(harness.call("search_books", {"query": "x"}))
            self.assertIsNone(found["books"][0]["is_favorite"])
        finally:
            world.download_gate.set()
        self.wait_state(harness, first, {"succeeded"})
        self.wait_state(harness, second["task"]["task_id"], {"succeeded"})

    def test_d5_cancelling_a_wait_leaves_the_task_and_cancel_download_stops_it(self):
        world = World()
        world.download_gate = threading.Event()
        harness = self.serve_downloads(world=world)
        task_id = structured(harness.call("download_book", {"reference": REF}))["task"]["task_id"]
        self.assertTrue(world.download_started.wait(3))
        harness.start_call("get_download", {"task_id": task_id, "wait_seconds": 30}, request_id="wait")
        time.sleep(0.2)
        harness.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "wait"}})
        self.assertEqual(harness.request("ping", request_id="after")["id"], "after")
        harness.quiet(0.3)
        running = structured(harness.call("get_download", {"task_id": task_id}))["task"]
        self.assertEqual(running["state"], "running")
        cancelled = structured(harness.call("cancel_download", {"task_id": task_id}))["task"]
        self.assertIs(cancelled["cancel_requested"], True)
        final = self.wait_state(harness, task_id, {"cancelled"})
        self.assertEqual(final["outcome"], "unknown")
        self.assertIsNone(final["file"])
        self.assertEqual(list((self.books / task_id).iterdir()), [])
        time.sleep(0.2)
        self.assertEqual(world.link_requests, 1)
        self.assertEqual(error_of(harness.call("cancel_download", {"task_id": "e" * 32}))["code"], "download_not_found")
        resumed = structured(harness.call("resume_downloads"))
        self.assertEqual(resumed, {"resumed": 0, "queue_paused": False})

    def test_d6_progress_notifications(self):
        world = World()
        world.download_steps, world.download_delay = 10, 0.15
        harness = self.serve_downloads(world=world)
        task_id = structured(harness.call("download_book", {"reference": REF}))["task"]["task_id"]
        self.assertTrue(world.download_started.wait(3))
        harness.start_call("get_download", {"task_id": task_id, "wait_seconds": 10}, request_id="watch",
                           meta={"progressToken": "p1"})
        stamps, values = [], []
        while True:
            stamp, message = harness.receive_timed(timeout=10)
            if message.get("id") == "watch":
                break
            self.assertEqual(message["method"], "notifications/progress")
            self.assertEqual(message["params"]["progressToken"], "p1")
            self.assertRegex(message["params"]["message"], r"^[0-9.]+ (KiB|MiB)( of [0-9.]+ (KiB|MiB))?$")
            stamps.append(stamp)
            values.append(message["params"]["progress"])
        self.assertEqual(message["result"]["structuredContent"]["task"]["state"], "succeeded")
        self.assertGreaterEqual(len(values), 1)
        self.assertEqual(values, sorted(set(values)))
        for earlier, later in zip(stamps, stamps[1:]):
            self.assertGreater(later - earlier, 0.4)
        harness.quiet(0.3)

        world.download_started.clear()
        other = structured(harness.call("download_book", {"reference": "13:def"}))["task"]["task_id"]
        self.assertTrue(world.download_started.wait(3))
        response = harness.call("get_download", {"task_id": other, "wait_seconds": 10}, request_id="no-token")
        self.assertEqual(response["structuredContent"]["task"]["state"], "succeeded")
        self.assertEqual(harness.notifications, [])

    def test_d7_a_second_server_on_the_same_folder_gets_downloads_busy(self):
        world = World()
        first = self.serve_downloads(world=world)
        structured(first.call("list_downloads"))
        second = self.serve_downloads(world=World())
        error = error_of(second.call("list_downloads"))
        self.assertEqual((error["code"], error["retryable"]), ("downloads_busy", True))
        first.close()
        self.assertEqual(structured(second.call("list_downloads"))["total"], 0)

    def test_download_tools_need_a_session(self):
        harness = self.serve("--download-dir", str(self.books), signed_in=False)
        error = error_of(harness.call("download_book", {"reference": REF}))
        self.assertEqual(error["code"], "upstream_authentication_required")
        self.assertEqual(os.listdir(self.books), [])

    def test_server_status_reports_the_folder(self):
        harness = self.serve_downloads("--no-download-confirmation")
        status = structured(harness.call("server_status"))
        self.assertEqual(status["download_folder"], str(self.books.resolve()))
        self.assertEqual(status["actions"], {"downloads": True, "account_changes": False})


class DownloadSubprocessTests(McpTestCase):
    def setUp(self):
        super().setUp()
        save_session(self.session)

    def run_server(self, scenario, messages, *, wait_for=(), close=True, env=None):
        process = spawn(scenario, "--session-file", str(self.session), "--timeout", "20", "mcp",
                        "--download-dir", str(self.books), env=env)
        out = []
        for message in messages:
            process.stdin.write(line(message))
            process.stdin.flush()
            if "id" in message:
                out.append(json.loads(process.stdout.readline()))
        return process, out

    def test_p8_shutdown_interrupts_a_running_transfer_and_never_retries_it(self):
        process, out = self.run_server("transfer", [INITIALIZE, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "download_book", "arguments": {"reference": REF}}}])
        task_id = out[1]["result"]["structuredContent"]["task"]["task_id"]
        deadline = time.monotonic() + 5
        state = None
        request = 3
        while time.monotonic() < deadline and state != "running":
            process.stdin.write(line({"jsonrpc": "2.0", "id": request, "method": "tools/call",
                                      "params": {"name": "get_download", "arguments": {"task_id": task_id}}}))
            process.stdin.flush()
            state = json.loads(process.stdout.readline())["result"]["structuredContent"]["task"]["state"]
            request += 1
        self.assertEqual(state, "running")
        started = time.monotonic()
        process.stdin.close()
        process.wait(5)
        elapsed = time.monotonic() - started
        err = process.stderr.read()
        process.stdout.close()
        process.stderr.close()
        self.assertEqual(process.returncode, 0, err)
        self.assertLess(elapsed, 1.5)
        self.assertNotIn(b"Traceback", err)

        restarted, out = self.run_server("basic", [INITIALIZE, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "get_download", "arguments": {"task_id": task_id}}}])
        restarted.communicate(timeout=10)
        task = out[1]["result"]["structuredContent"]["task"]
        self.assertEqual((task["state"], task["outcome"]), ("interrupted", "unknown"))

    def test_t11_secret_scan_of_a_full_scenario(self):
        calls = [("server_status", {}), ("search_books", {"query": "x"}), ("get_quota", {}),
                 ("list_favorites", {}), ("download_book", {"reference": REF}), ("list_downloads", {})]
        messages = [INITIALIZE] + [{"jsonrpc": "2.0", "id": index + 2, "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments}}
                                   for index, (name, arguments) in enumerate(calls)]
        process, out = self.run_server("secrets", messages)
        task_id = out[5]["result"]["structuredContent"]["task"]["task_id"]
        for attempt in range(50):
            process.stdin.write(line({"jsonrpc": "2.0", "id": 100 + attempt, "method": "tools/call",
                                      "params": {"name": "get_download", "arguments": {"task_id": task_id,
                                                                                       "wait_seconds": 1}}}))
            process.stdin.flush()
            reply = process.stdout.readline()
            if b'"state":"failed"' in reply:
                break
        else:
            self.fail("The synthetic transfer did not fail")
        rest, err = process.communicate(timeout=10)
        stdout = b"".join(json.dumps(message).encode() for message in out) + reply + rest
        everything = stdout + err
        secrets = [COOKIE_SECRET, ACCOUNT_ID, SIGNED_LINK, "SIGNED-LINK-TOKEN", str(self.session)]
        for secret in secrets:
            for encoded in (secret.encode(), json.dumps(secret)[1:-1].encode()):
                self.assertNotIn(encoded, everything, secret)
        self.assertNotIn(b"Traceback", err)


if __name__ == "__main__":
    unittest.main()
