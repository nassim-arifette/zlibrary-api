"""MCP framing, lifecycle and protocol errors over real pipes; synthetic clients only, no network."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mcp_fixture import Harness, World, error_of, structured
from session_fixture import isolated_sessions
from zlib_connector.mcp_server.protocol import MAX_LINE, encode_message, parse_message


FIXTURE = Path(__file__).with_name("mcp_fixture.py")
ROOT = FIXTURE.parents[1]


def spawn(scenario, *argv, env=None):
    environment = {**os.environ, **(env or {})}
    environment.pop("PYTHONPATH", None)
    return subprocess.Popen([sys.executable, str(FIXTURE), scenario, *argv], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(ROOT), env=environment)


def line(message):
    return (json.dumps(message) + "\n").encode("utf-8")


INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


def envelope(version="2026-07-28", **params):
    meta = {"io.modelcontextprotocol/protocolVersion": version, "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "modern-test", "version": "1"}}
    return {"_meta": meta, **params}


@isolated_sessions
class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(["mcp"])
        self.addCleanup(self.harness.close)

    def test_p1_version_negotiation_and_ping(self):
        harness = self.harness
        self.assertEqual(harness.request("ping")["result"], {})
        response = harness.initialize("2025-06-18")["result"]
        self.assertEqual(response["protocolVersion"], "2025-06-18")
        self.assertEqual(response["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(response["serverInfo"]["name"], "zlib-connector")
        self.assertLessEqual(len(response["instructions"]), 512)
        self.assertEqual(harness.request("ping")["result"], {})
        for requested, expected in (("2025-11-25", "2025-11-25"), ("2025-03-26", "2025-11-25"), ("garbage", "2025-11-25")):
            with Harness(["mcp"]) as other:
                self.assertEqual(other.initialize(requested)["result"]["protocolVersion"], expected)

    def test_p2_discover_probe_is_answered_at_once_and_opens_a_modern_connection(self):
        harness = self.harness
        started = time.monotonic()
        response = harness.request("server/discover", envelope())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(response["result"]["supportedVersions"], ["2026-07-28"])
        refused = harness.request("initialize", INITIALIZE["params"])["error"]
        self.assertEqual(refused["code"], -32022)
        self.assertEqual(refused["data"]["supported"], ["2026-07-28"])

    def test_p2_an_enveloped_initialize_opens_a_legacy_connection(self):
        harness = self.harness
        params = {**INITIALIZE["params"], **envelope()}
        self.assertEqual(harness.request("initialize", params)["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(harness.request("tools/list", envelope())["error"]["code"], -32600)
        self.assertIn("tools", harness.request("tools/list")["result"])

    def test_p3_methods_before_initialize_and_second_initialize(self):
        harness = self.harness
        self.assertEqual(harness.request("tools/list")["error"]["code"], -32600)
        self.assertEqual(harness.request("tools/call", {"name": "server_status"})["error"]["code"], -32600)
        self.assertEqual(harness.request("resources/list")["error"]["code"], -32601)
        harness.initialize()
        self.assertEqual(harness.initialize()["error"]["code"], -32600)
        for method in ("logging/setLevel", "prompts/list", "completion/complete", "server/discover"):
            self.assertEqual(harness.request(method)["error"]["code"], -32601)
        self.assertEqual(harness.request("tools/list", {"cursor": "x"})["error"]["code"], -32602)
        self.assertEqual(harness.request("initialize", {"protocolVersion": 5})["error"]["code"], -32600)

    def test_bad_initialize_params(self):
        response = self.harness.request("initialize", {"capabilities": {}})
        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(self.harness.initialize()["result"]["protocolVersion"], "2025-11-25")

    def test_p4_parse_and_request_errors_and_silent_notifications(self):
        harness = self.harness
        for raw in (b"{not json", b'{"a":1,"a":2}', b'{"x":NaN}', b"\xff\xfe", b"[" * 100000):
            harness.send(raw)
            message = harness.receive()
            self.assertEqual(message, {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}})
        for raw, has_id in (([], False), ([{"jsonrpc": "2.0", "id": 1, "method": "ping"}], False),
                            ({"jsonrpc": "2.0", "id": None, "method": "ping"}, False),
                            ({"jsonrpc": "2.0", "id": True, "method": "ping"}, False),
                            ({"jsonrpc": "2.0", "id": 1.5, "method": "ping"}, False),
                            ({"id": 3, "method": "ping"}, True), ({"jsonrpc": "2.0", "id": 4, "method": 7}, True),
                            ({"jsonrpc": "2.0", "id": 5, "method": "ping", "params": [1]}, True), ({}, False)):
            harness.send(raw)
            message = harness.receive()
            self.assertEqual(message["error"]["code"], -32600)
            self.assertEqual("id" in message, has_id)
            self.assertNotIn("result", message)
        for notification in ({"jsonrpc": "2.0", "method": "notifications/initialized"},
                             {"jsonrpc": "2.0", "method": "notifications/unknown", "params": {}},
                             {"jsonrpc": "1.0", "method": 5},
                             {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": None}},
                             {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 999}},
                             {"jsonrpc": "2.0", "id": 77, "result": {}}):
            harness.send(notification)
        self.assertEqual(harness.request("ping", request_id="after")["id"], "after")

    def test_p5_crlf_unicode_and_clean_output(self):
        harness = self.harness
        harness.send(line(INITIALIZE)[:-1] + b"\r\n")
        self.assertEqual(harness.receive()["id"], 1)
        result = structured(harness.call("search_books", {"query": "echo:Émile 東京   ok"}))
        self.assertEqual(result["books"][0]["title"], "Émile 東京 ok")
        self.assertIn("Émile 東京".encode("utf-8"), bytes(harness.sink.raw))
        harness.close()
        raw = bytes(harness.sink.raw)
        self.assertNotIn(b"\r", raw)
        for chunk in raw.split(b"\n")[:-1]:
            json.loads(chunk.decode("utf-8"))

    def test_p7_oversized_line_is_refused_and_serving_continues(self):
        harness = self.harness
        harness.send(b"x" * (MAX_LINE + 1) + b"\n")
        message = harness.receive(timeout=10)
        self.assertEqual(message, {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request"}})
        self.assertEqual(harness.request("ping", request_id=2)["result"], {})

    def test_p9_duplicate_inflight_id_and_cancellation(self):
        harness = self.harness
        harness.initialize()
        gate = threading.Event()
        harness.world.gates["get_popular"] = gate
        try:
            harness.start_call("list_popular", request_id=7)
            self.assertTrue(harness.world.entered.setdefault("get_popular", threading.Event()).wait(3))
            harness.start_call("server_status", request_id=7)
            message = harness.receive()
            self.assertEqual(message["error"]["code"], -32600)
            self.assertNotIn("id", message)
        finally:
            gate.set()
        response = harness.wait_for(7)
        self.assertEqual(response["result"]["isError"], False)
        self.assertIn("books", response["result"]["structuredContent"])

        gate.clear()
        harness.world.entered["get_popular"].clear()
        harness.start_call("list_popular", request_id="cancel-me")
        self.assertTrue(harness.world.entered["get_popular"].wait(3))
        harness.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                      "params": {"requestId": "cancel-me", "reason": "User requested cancellation"}})
        time.sleep(0.05)
        gate.set()
        self.assertEqual(harness.request("ping", request_id="next")["id"], "next")
        harness.quiet(0.3)
        self.assertIn("suppressed", harness.stderr.getvalue())

    def test_tools_call_envelope_errors(self):
        harness = self.harness
        harness.initialize()
        for params in ({"name": 5}, {"name": "server_status", "arguments": []},
                       {"name": "server_status", "_meta": "x"}):
            self.assertEqual(harness.request("tools/call", params)["error"]["code"], -32602)
        for name in ("no_such_tool", "download_book", "delete_booklist"):
            response = harness.request("tools/call", {"name": name, "arguments": {}})
            self.assertEqual(response["error"], {"code": -32602, "message": "Unknown or disabled tool."})

    def test_concurrency_cap_returns_server_busy(self):
        harness = Harness(["mcp", "--anonymous"])
        self.addCleanup(harness.close)
        harness.initialize()
        gate = threading.Event()
        harness.world.gates["get_popular"] = gate
        try:
            for index in range(4):
                harness.start_call("list_popular", request_id=f"busy-{index}")
            self.assertTrue(harness.world.entered.setdefault("get_popular", threading.Event()).wait(3))
            time.sleep(0.1)
            error = error_of(harness.call("server_status", request_id="fifth"))
            self.assertEqual(error, {"code": "server_busy", "message": "Too many calls are running. Try again shortly.",
                                     "retryable": True, "outcome": "not_applied"})
        finally:
            gate.set()
        responses = harness.collect({f"busy-{index}" for index in range(4)})
        self.assertTrue(all(not response["result"]["isError"] for response in responses.values()))

    def test_p8_eof_stops_an_idle_server_quickly(self):
        harness = Harness(["mcp"])
        harness.initialize()
        started = time.monotonic()
        self.assertEqual(harness.close(), 0)
        self.assertLess(time.monotonic() - started, 0.3)


@isolated_sessions
class ModernTests(unittest.TestCase):
    """2026-07-28: no handshake, everything the server needs is in each request's _meta."""

    def setUp(self):
        self.harness = Harness(["mcp"])
        self.addCleanup(self.harness.close)

    def assert_complete(self, result):
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "zlib-connector")

    def test_t1_discover_list_and_call_without_initialize(self):
        harness = self.harness
        discover = harness.request("server/discover", envelope())["result"]
        self.assert_complete(discover)
        self.assertEqual(discover["capabilities"], {"tools": {}})
        self.assertEqual((discover["ttlMs"], discover["cacheScope"]), (300000, "private"))
        self.assertLessEqual(len(discover["instructions"]), 512)
        listed = harness.request("tools/list", envelope())["result"]
        self.assert_complete(listed)
        self.assertEqual((listed["ttlMs"], listed["cacheScope"]), (300000, "private"))
        with Harness(["mcp"]) as legacy:
            legacy.initialize()
            self.assertEqual(listed["tools"], legacy.request("tools/list")["result"]["tools"])
        called = harness.request("tools/call", envelope(name="server_status", arguments={}))["result"]
        self.assert_complete(called)
        self.assertFalse(called.get("isError", False))
        self.assertEqual(called["structuredContent"]["protocol_version"], "2026-07-28")
        self.assertIn("client modern-test, protocol 2026-07-28", harness.stderr.getvalue())

    def test_t2_envelope_errors(self):
        harness = self.harness
        unsupported = harness.request("server/discover", envelope("1900-01-01"))["error"]
        self.assertEqual(unsupported["code"], -32022)
        self.assertEqual(unsupported["data"], {"supported": ["2026-07-28"], "requested": "1900-01-01"})
        missing = envelope()
        del missing["_meta"]["io.modelcontextprotocol/clientCapabilities"]
        self.assertEqual(harness.request("tools/list", missing)["error"]["code"], -32602)
        self.assertEqual(harness.request("tools/list")["error"]["code"], -32602)
        self.assertEqual(harness.request("tools/list", envelope(cursor="x"))["error"]["code"], -32602)

    def test_t3_removed_and_unknown_methods(self):
        harness = self.harness
        for method in ("ping", "logging/setLevel", "resources/list", "prompts/list"):
            self.assertEqual(harness.request(method, envelope())["error"]["code"], -32601)
        self.assertEqual(harness.request("tools/call", envelope(name="nope", arguments={}))["error"]["code"], -32602)

    def test_t4_listen_is_acknowledged_held_and_ended_by_cancellation(self):
        harness = self.harness
        harness.send({"jsonrpc": "2.0", "id": "listen-1", "method": "subscriptions/listen",
                      "params": envelope(notifications={"toolsListChanged": True})})
        acknowledged = harness.receive()
        self.assertEqual(acknowledged, {"jsonrpc": "2.0", "method": "notifications/subscriptions/acknowledged",
                                        "params": {"_meta": {"io.modelcontextprotocol/subscriptionId": "listen-1"},
                                                   "notifications": {}}})
        harness.quiet()
        harness.send({"jsonrpc": "2.0", "id": "listen-1", "method": "subscriptions/listen",
                      "params": envelope(notifications={})})
        self.assertEqual(harness.receive(), {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request"}})
        self.assert_complete(harness.request("tools/list", envelope())["result"])
        harness.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "listen-1"}})
        harness.quiet()
        self.assertEqual(harness.request("subscriptions/listen", envelope())["error"]["code"], -32602)
        harness.send({"jsonrpc": "2.0", "id": "listen-1", "method": "subscriptions/listen",
                      "params": envelope(notifications={})})
        self.assertEqual(harness.receive()["method"], "notifications/subscriptions/acknowledged")


class CodecTests(unittest.TestCase):
    def test_writer_never_emits_newlines_or_lone_surrogates(self):
        data = encode_message({"text": "a\nb\r \ud800"})
        self.assertNotIn(b"\n", data)
        self.assertNotIn(b"\r", data)
        data.decode("utf-8")

    def test_parser_rejects_constants_and_duplicates(self):
        for raw in (b"NaN", b"Infinity", b'{"a":1,"a":1}', b"\xc3"):
            with self.assertRaises(Exception):
                parse_message(raw)
        self.assertEqual(parse_message(b'{"a":[1,2]}'), {"a": [1, 2]})


class SubprocessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / "state"
        self.env = {"ZLIB_STATE_DIR": str(self.state)}

    def finish(self, process, data, timeout=10):
        try:
            out, err = process.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        return out, err

    def test_c4_p2_initialize_list_and_probe_without_network(self):
        process = spawn("basic", "mcp", env=self.env)
        data = b"".join(line(message) for message in (
            {"jsonrpc": "2.0", "id": 0, "method": "server/discover", "params": {}},
            INITIALIZE, {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"}))
        out, err = self.finish(process, data)
        self.assertEqual(process.returncode, 0, err)
        messages = [json.loads(chunk) for chunk in out.split(b"\n") if chunk]
        self.assertEqual([message.get("id") for message in messages], [0, 1, 2, 3])
        self.assertEqual(messages[0]["error"]["code"], -32601)
        self.assertEqual(len(messages[2]["result"]["tools"]), 13)
        self.assertNotIn(b"\r\n", out)
        self.assertIn(b"zlib-mcp: INFO", err)
        self.assertNotIn(b"Traceback", err)

    def test_p6_stray_output_never_reaches_the_protocol_stream(self):
        process = spawn("stray", "mcp", env=self.env)
        process.stdin.write(line(INITIALIZE))
        process.stdin.write(line({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": "search_books", "arguments": {"query": "anything"}}}))
        process.stdin.flush()
        out = b""
        while out.count(b"\n") < 2:
            chunk = process.stdout.readline()
            if not chunk:
                break
            out += chunk
        rest, err = self.finish(process, b"")
        out += rest
        messages = [json.loads(chunk) for chunk in out.split(b"\n") if chunk]
        self.assertEqual([message["id"] for message in messages], [1, 2])
        self.assertFalse(messages[1]["result"]["isError"])
        self.assertIn(b"stray print from a handler", err)
        self.assertIn(b"stray child output", err)

    @unittest.skipUnless(os.name == "posix", "POSIX signals")
    def test_p8_signals_exit_cleanly(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            process = spawn("basic", "mcp", env=self.env)
            process.stdin.write(line(INITIALIZE))
            process.stdin.flush()
            self.assertIn(b'"id":1', process.stdout.readline())
            process.send_signal(signum)
            _, err = self.finish(process, None)
            self.assertEqual(process.returncode, 0)
            self.assertNotIn(b"Traceback", err)


if __name__ == "__main__":
    unittest.main()
