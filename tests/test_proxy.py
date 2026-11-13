"""The explicit HTTPS proxy: settings, HTTP CONNECT, propagation and doctor. Offline: the proxy is a local stub."""
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from session_fixture import isolated_sessions
from zlib_connector import NetworkError, local_state
from zlib_connector.cli import main
from zlib_connector.client import Book
from zlib_connector.diagnostics import DomainDiagnostic, DomainReport, diagnose_domains
from zlib_connector.settings import SettingsError, load_settings
from zlib_connector.transport import Transport, validate_proxy


class ProxyStub:
    """Answers one CONNECT with ``status``, then keeps what the client sent next (the TLS ClientHello)."""

    def __init__(self, status=b"200 Connection established"):
        self.status, self.request, self.after = status, b"", b""
        self.server = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.server.getsockname()[1]}"
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        connection, _ = self.server.accept()
        with connection:
            connection.settimeout(5)
            while b"\r\n\r\n" not in self.request:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                self.request += chunk
            connection.sendall(b"HTTP/1.1 " + self.status + b"\r\nContent-Length: 0\r\n\r\n")
            try:
                self.after = connection.recv(4096)
            except OSError:
                pass

    def close(self):
        self.thread.join(5)
        self.server.close()


class ValidateProxyTests(unittest.TestCase):
    def test_accepts_http_host_and_port_only(self):
        self.assertEqual("http://proxy.example:3128", validate_proxy("http://Proxy.example:3128/"))
        self.assertEqual("http://127.0.0.1:8080", validate_proxy("http://127.0.0.1:8080"))
        self.assertEqual("http://[::1]:8080", validate_proxy("http://[::1]:8080"))

    def test_refuses_credentials_paths_other_schemes_and_missing_ports(self):
        for value in ("https://proxy.example:3128", "socks5://proxy.example:1080", "http://proxy.example",
                      "http://user:secret@proxy.example:3128", "http://user@proxy.example:3128",
                      "http://proxy.example:3128/path", "http://proxy.example:3128?x=1", "http://proxy.example:0",
                      "http://proxy.example:99999", "http://:3128", "proxy.example:3128", "http://proxy.example:31 28",
                      "http://proxy.example:3128\n"):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                validate_proxy(value)
            self.assertNotIn("secret", str(caught.exception))


class ConnectTests(unittest.TestCase):
    def test_tls_runs_through_the_tunnel_to_the_site_and_only_the_proxy_is_resolved(self):
        stub = ProxyStub()
        self.addCleanup(stub.close)
        transport = Transport(5, proxy=stub.url)
        transport.cookies.set_cookie(_cookie("remix_userkey", "synthetic-key", "books.example"))
        with patch("zlib_connector.transport.public_addresses", side_effect=AssertionError("site DNS")), \
                self.assertRaises(NetworkError):
            transport.request("https://books.example/eapi/book/search", form={"message": "dune"})
        stub.close()
        head = stub.request.decode("latin-1")
        self.assertRegex(head, r"^CONNECT books\.example:443 HTTP/1\.[01]\r\n")
        # The proxy sees the site's name and port only: no cookie, path, form or authorization.
        for secret in ("synthetic-key", "Cookie", "eapi", "dune", "Authorization"):
            self.assertNotIn(secret, head)
        self.assertEqual(0x16, stub.after[0])  # A TLS handshake record, not plain HTTP
        self.assertIn(b"books.example", stub.after)  # SNI names the site, so its certificate is the one checked

    def test_a_refused_tunnel_is_a_proxy_error_without_the_proxys_text(self):
        stub = ProxyStub(b"407 Proxy Authentication Required <script>")
        self.addCleanup(stub.close)
        with self.assertRaises(NetworkError) as caught:
            Transport(5, proxy=stub.url).request("https://books.example/")
        self.assertEqual("proxy", caught.exception.reason)
        self.assertEqual("The proxy refused the HTTPS tunnel (HTTP 407).", str(caught.exception))

    def test_an_unreachable_proxy_is_a_proxy_error(self):
        server = socket.create_server(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.close()
        with self.assertRaises(NetworkError) as caught:
            Transport(5, proxy=f"http://127.0.0.1:{port}").request("https://books.example/")
        self.assertEqual("proxy", caught.exception.reason)

    def test_without_a_proxy_the_site_is_resolved_and_checked(self):
        with patch("zlib_connector.transport.public_addresses", side_effect=NetworkError("blocked")) as resolve, \
                self.assertRaises(NetworkError):
            Transport(5).request("https://books.example/")
        resolve.assert_called_once_with("books.example")

    def test_each_redirect_hop_is_checked_and_tunnelled_to_its_own_host(self):
        from test_transport import Connection, Reply

        class Tunnelled(Connection):
            def set_tunnel(self, host, port):
                self.tunnel = (host, port)

        cookie = ("Set-Cookie", "remix_userkey=synthetic; Secure; Path=/")
        hops = [Tunnelled(Reply(302, headers=[("Location", "https://cdn.example/file"), cookie])), Tunnelled(Reply())]
        made = []

        def connection(host, port, **_):
            made.append((host, port))
            return hops[len(made) - 1]

        with patch("zlib_connector.transport.proxy_addresses", return_value=["127.0.0.1"]), \
                patch("zlib_connector.transport.public_addresses", side_effect=AssertionError("site DNS")), \
                patch("http.client.HTTPSConnection", side_effect=connection):
            result = Transport(5, proxy="http://127.0.0.1:3128").request("https://books.example/eapi/file")
        self.assertEqual("https://cdn.example/file", result.url)
        self.assertEqual([("127.0.0.1", 3128)] * 2, made)
        self.assertEqual([("books.example", 443), ("cdn.example", 443)], [hop.tunnel for hop in hops])
        self.assertNotIn("Cookie", hops[1].calls[0][3])
        hops[:] = [Tunnelled(Reply(302, headers=[("Location", "http://books.example/file")]))]
        made.clear()
        with patch("zlib_connector.transport.proxy_addresses", return_value=["127.0.0.1"]), \
                patch("http.client.HTTPSConnection", side_effect=connection), self.assertRaises(NetworkError):
            Transport(5, proxy="http://127.0.0.1:3128").request("https://books.example/eapi/file")
        self.assertEqual(1, len(made))

    def test_only_https_site_urls_go_through_the_proxy(self):
        with self.assertRaises(NetworkError):
            Transport(5, proxy="http://127.0.0.1:3128").request("http://books.example/")


class SettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name) / "config.toml"

    def test_environment_and_config_set_the_proxy_and_system_variables_are_ignored(self):
        system = {"HTTP_PROXY": "http://system.example:1", "HTTPS_PROXY": "http://system.example:2"}
        self.assertIsNone(load_settings(environ=system, config_file=self.config, missing_ok=True).proxy)
        self.config.write_text('proxy = "http://127.0.0.1:3128"\n', encoding="ascii")
        self.assertEqual("http://127.0.0.1:3128", load_settings(environ=system, config_file=self.config).proxy)
        settings = load_settings(environ={"ZLIB_PROXY": "http://10.0.0.2:8080"}, config_file=self.config)
        self.assertEqual(("http://10.0.0.2:8080", ("env", "ZLIB_PROXY")), (settings.proxy, settings.origins["proxy"]))

    def test_a_proxy_with_credentials_is_refused_without_echoing_them(self):
        with self.assertRaises(SettingsError) as caught:
            load_settings(environ={"ZLIB_PROXY": "http://user:hunter2@proxy.example:3128"}, config_file=self.config)
        self.assertNotIn("hunter2", str(caught.exception))


class RecordingClient:
    instances = []

    def __init__(self, domain="https://z-lib.by/", *, timeout=30, proxy=None):
        self.domain, self.proxy = domain, proxy
        RecordingClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def search(self, *_, **__):
        return [Book("First", "Author", "epub", "https://books.example/book/1/a1", book_id="1", book_hash="a1")]


@isolated_sessions
class CommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        location = patch.object(local_state, "config_directory", lambda: self.folder)
        location.start()
        self.addCleanup(location.stop)
        RecordingClient.instances = []

    def run_cli(self, *argv, environ=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        environment = {"ZLIB_NO_INPUT": "", "ZLIB_DEBUG": "", "ZLIB_PROXY": "", "HTTPS_PROXY": "http://system.example:9"}
        environment.update(environ or {})
        with patch("zlib_connector.cli.Client", RecordingClient), patch.dict(os.environ, environment), \
                patch("http.client.HTTPSConnection", side_effect=AssertionError("network")), \
                patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_site_clients_get_the_proxy_and_no_proxy_wins(self):
        self.run_cli("search", "dune", "--anonymous", "--json")
        self.assertIsNone(RecordingClient.instances[-1].proxy)  # HTTPS_PROXY is ignored
        self.run_cli("--proxy", "http://127.0.0.1:3128", "search", "dune", "--anonymous", "--json")
        self.assertEqual("http://127.0.0.1:3128", RecordingClient.instances[-1].proxy)
        self.run_cli("search", "dune", "--anonymous", "--json", environ={"ZLIB_PROXY": "http://10.0.0.2:8080"})
        self.assertEqual("http://10.0.0.2:8080", RecordingClient.instances[-1].proxy)
        self.run_cli("search", "dune", "--anonymous", "--no-proxy", "--json", environ={"ZLIB_PROXY": "http://10.0.0.2:8080"})
        self.assertIsNone(RecordingClient.instances[-1].proxy)

    def test_proxy_and_no_proxy_conflict(self):
        code, _, err = self.run_cli("--proxy", "http://127.0.0.1:3128", "search", "dune", "--no-proxy", "--json")
        self.assertEqual(2, code)
        self.assertEqual("invalid_argument", json.loads(err)["code"])

    def test_doctor_reports_the_proxy(self):
        report = DomainReport((DomainDiagnostic("https://z-lib.sk/", True, "2026-11-12T00:00:00Z", 5),),
                              "http://127.0.0.1:3128")
        with patch("zlib_connector.cli.diagnose_domains", return_value=report) as diagnose:
            code, out, _ = self.run_cli("--proxy", "http://127.0.0.1:3128", "doctor")
        self.assertEqual((0, "http://127.0.0.1:3128"), (code, diagnose.call_args.kwargs["proxy"]))
        self.assertIn("Through proxy: http://127.0.0.1:3128 (--proxy)", out)
        with patch("zlib_connector.cli.diagnose_domains", return_value=DomainReport(report.results)) as diagnose:
            code, out, _ = self.run_cli("doctor", "--json")
        self.assertIsNone(diagnose.call_args.kwargs["proxy"])
        self.assertIsNone(json.loads(out)["proxy"])

    def test_diagnostics_probe_through_the_proxy(self):
        seen = []

        def probe(domain, timeout, proxy=None):
            seen.append(proxy)
            return DomainDiagnostic(domain, True, "2026-11-12T00:00:00Z", 1)

        with patch("zlib_connector.diagnostics._probe", probe):
            report = diagnose_domains(["https://books.example"], proxy="http://Proxy.example:3128/")
        self.assertEqual((["http://proxy.example:3128"], "http://proxy.example:3128"), (seen, report.proxy))
        self.assertEqual("http://proxy.example:3128", report.to_dict()["proxy"])


def _cookie(name, value, domain):
    from http.cookiejar import Cookie
    return Cookie(0, name, value, None, False, domain, True, False, "/", True, True, None, False, None, None, {})


if __name__ == "__main__":
    unittest.main()
