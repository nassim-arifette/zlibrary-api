"""One offline flow across CLI, HTTP, durable tasks and the real Python client."""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from zlib_connector import Client, TransferProgress
from zlib_connector.api_token import load_api_token
from zlib_connector.cli import main
from zlib_connector.http_api import create_server
from zlib_connector.local_api import LocalAPI
from zlib_connector.transport import Response, Transport
from test_session_client import seed


PDF = b"%PDF-1.7\nSynthetic offline book\n"


class OfflineTransport(Transport):
    def __init__(self):
        super().__init__()
        seed(self)
        self.links = self.transfers = 0

    def request(self, url, **_):
        if url.endswith("/eapi/user/profile"):
            payload = {"user": {"downloads_limit": 10, "downloads_today": self.transfers}}
        elif url.endswith("/eapi/book/123/abc/file"):
            self.links += 1
            payload = {"file": {"allowDownload": True, "extension": "pdf", "downloadLink": "/synthetic-file"}}
        elif url.endswith("/eapi/book/123/abc"):
            payload = {"book": {"id": 123, "hash": "abc", "title": "Synthetic book", "extension": "pdf"}}
        else:
            raise AssertionError("Unexpected offline endpoint")
        return Response(json.dumps({"success": 1, **payload}).encode(), url)

    def download_to(self, url, output, *, progress, cancel):
        self.transfers += 1
        cancel.check()
        output.write(PDF)
        progress(TransferProgress(len(PDF), len(PDF), .1))
        return len(PDF)


@contextmanager
def running(client, token, folder):
    with create_server(client, token=token, port=0, download_dir=folder) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}/v1"
        finally:
            server.shutdown()
            thread.join(2)


class DownloadTasksIntegrationTests(unittest.TestCase):
    def test_cli_submission_download_file_and_restart_replay_use_one_upstream_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            downloads, copies = root / "downloads", root / "copies"
            downloads.mkdir()
            copies.mkdir()
            token_file = root / "api-token"
            token = load_api_token(token_file)
            transport = OfflineTransport()
            with Client("https://books.example/", transport=transport) as client:
                client._authenticated = True
                with running(client, token, downloads) as url:
                    output = io.StringIO()
                    with redirect_stdout(output), redirect_stderr(io.StringIO()):
                        code = main(["jobs", "--api-url", url, "--token-file", str(token_file),
                                     "add", "123:abc", "--key", "offline-flow-001", "--json"])
                    self.assertEqual(0, code)
                    identifier = json.loads(output.getvalue())["id"]
                    api = LocalAPI(url, token_file)
                    deadline = time.monotonic() + 5
                    while True:
                        task = api.request("GET", "/downloads/" + identifier)
                        if task["state"] not in ("queued", "running") or time.monotonic() >= deadline:
                            break
                        time.sleep(.01)
                    self.assertEqual("succeeded", task["state"], task)
                    self.assertEqual(len(PDF), task["progress"]["bytes_received"])
                    copied = api.file(identifier, copies)
                    self.assertEqual(PDF, copied.read_bytes())
                with running(client, token, downloads) as url:
                    api = LocalAPI(url, token_file)
                    replay = api.request("POST", "/downloads", body={"reference": "123:abc", "idempotency_key": "offline-flow-001"})
                    self.assertEqual((identifier, "succeeded"), (replay["id"], replay["state"]))
                    self.assertEqual(1, api.request("GET", "/downloads")["total"])
                    self.assertEqual(PDF, api.file(identifier, copies).read_bytes())
                self.assertEqual((1, 1), (transport.links, transport.transfers))
                self.assertEqual(2, len(list(copies.glob("*.pdf"))))
                self.assertNotIn(str(root), json.dumps(task))
