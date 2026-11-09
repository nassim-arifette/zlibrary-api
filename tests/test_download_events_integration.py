"""Live CLI observations cross the real HTTP and manager interfaces offline."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from zlib_connector import Client, TransferProgress
from zlib_connector.api_token import load_api_token
from zlib_connector.cli import main
from zlib_connector.local_api import LocalAPI
from test_download_tasks_integration import OfflineTransport, PDF, running


class ControlledTransport(OfflineTransport):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def download_to(self, url, output, *, progress, cancel):
        self.transfers += 1
        midpoint = len(PDF) // 2
        output.write(PDF[:midpoint])
        progress(TransferProgress(midpoint, len(PDF), .1))
        self.started.set()
        deadline = time.monotonic() + 5
        while not self.release.wait(.01):
            cancel.check()
            if time.monotonic() >= deadline:
                raise AssertionError("The CLI did not observe the running transfer")
        cancel.check()
        output.write(PDF[midpoint:])
        progress(TransferProgress(len(PDF), len(PDF), .2))
        return len(PDF)


class ObservationOutput(io.StringIO):
    def __init__(self, transport):
        super().__init__()
        self.transport = transport

    def write(self, value):
        result = super().write(value)
        if value.startswith("{") and json.loads(value)["task"]["state"] == "running":
            self.transport.release.set()
        return result


class DownloadEventIntegrationTests(unittest.TestCase):
    def test_cli_watch_receives_running_and_terminal_states_without_account_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            downloads = root / "downloads"
            downloads.mkdir()
            token_file = root / "api-token"
            token = load_api_token(token_file)
            transport = ControlledTransport()
            try:
                with Client("https://books.example/", transport=transport) as client:
                    client._authenticated = True
                    with running(client, token, downloads) as url:
                        api = LocalAPI(url, token_file)
                        task = api.request("POST", "/downloads", body={"reference": "123:abc", "idempotency_key": "watch-flow-001"})
                        self.assertTrue(transport.started.wait(2))
                        output, errors = ObservationOutput(transport), io.StringIO()
                        with redirect_stdout(output), redirect_stderr(errors), \
                                patch("zlib_connector.cli.AccountSession", side_effect=AssertionError("Account access")):
                            code = main(["jobs", "--api-url", url, "--token-file", str(token_file),
                                         "watch", task["id"], "--duration", "5", "--json"])
                        self.assertEqual(0, code, errors.getvalue())
                        events = [json.loads(line) for line in output.getvalue().splitlines()]
                        self.assertEqual("running", events[0]["task"]["state"])
                        self.assertEqual("succeeded", events[-1]["task"]["state"])
                        self.assertTrue(all(event["task"]["id"] == task["id"] for event in events))
                        self.assertNotEqual(events[0]["id"], events[-1]["id"])
                        self.assertEqual((1, 1), (transport.links, transport.transfers))
                        self.assertNotIn(token, output.getvalue() + errors.getvalue())
                        self.assertNotIn(str(root), output.getvalue())
            finally:
                transport.release.set()
