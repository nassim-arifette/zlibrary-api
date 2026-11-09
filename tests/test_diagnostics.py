import io
import json
import ssl
import unittest
from unittest.mock import MagicMock, patch

from zlib_connector import ChallengeRequired, NetworkError, diagnose_domains
from zlib_connector.cli import main
from zlib_connector.transport import Response, Transport
from session_fixture import isolated_sessions


@isolated_sessions
class DomainDiagnosticsTests(unittest.TestCase):
    def test_real_search_is_probed_with_isolated_clients_and_no_credentials(self):
        requests = []

        def request(transport, url, *, form=None, download=False):
            requests.append((transport, url, form, download))
            if "blocked.example" in url:
                raise ChallengeRequired("synthetic-secret")
            return Response(b'{"success":1,"books":[]}', url)

        with patch.object(Transport, "request", request):
            report = diagnose_domains(["https://working.example", "https://blocked.example"], timeout=2, workers=2)
        self.assertEqual([item.search_ok for item in report.results], [True, False])
        self.assertEqual(report.recommended_domain, "https://working.example/")
        self.assertEqual(report.results[1].error_code, "challenge")
        self.assertEqual(report.results[0].result_count, 0)
        self.assertIsNone(report.results[1].final_domain)
        self.assertNotIn("synthetic-secret", json.dumps(report.to_dict()))
        self.assertIsNot(requests[0][0], requests[1][0])
        for transport, url, form, download in requests:
            self.assertEqual(len(transport.cookies), 0)
            self.assertTrue(url.endswith("/eapi/book/search"))
            self.assertEqual(form["limit"], "1")
            self.assertNotIn("email", form)
            self.assertNotIn("password", form)
            self.assertFalse(download)

    def test_failure_categories_are_stable_and_raw_bodies_are_never_returned(self):
        responses = [
            (NetworkError("synthetic-secret", reason="dns"), "dns"),
            (NetworkError("synthetic-secret", reason="unknown-private-text"), "network"),
            ({"success": 0, "error": "Authorization failed synthetic-secret"}, "authentication_required"),
            ({"success": 0, "error": "quota synthetic-secret"}, "rate_limited"),
            ({"success": 1}, "protocol_changed"),
            ({"success": 0, "error": "synthetic-secret"}, "api_refused"),
        ]
        for payload, expected in responses:
            def request(transport, url, *, form=None, download=False):
                if isinstance(payload, Exception):
                    raise payload
                return Response(json.dumps(payload).encode(), url)
            with self.subTest(expected=expected), patch.object(Transport, "request", request):
                report = diagnose_domains(["https://example.com/"])
            self.assertEqual(report.results[0].error_code, expected)
            self.assertIsNone(report.recommended_domain)
            self.assertNotIn("synthetic-secret", json.dumps(report.to_dict()))

    def test_domains_are_normalized_deduplicated_and_validated_before_probing(self):
        with patch.object(Transport, "request", return_value=Response(b'{"success":1,"books":[]}', "https://example.com/eapi/book/search")) as request:
            report = diagnose_domains(["https://EXAMPLE.com:443/", "https://example.com"])
        self.assertEqual(len(report.results), 1)
        self.assertEqual(request.call_count, 1)
        for domains in ([], "https://example.com", ["https://example.com/path"], ["http://example.com"],
                        ["https://user:password@example.com"], ["https://example.com"] * 51, 1):
            with self.subTest(domains=domains), patch.object(Transport, "request") as request, \
                    self.assertRaises((ValueError, NetworkError)):
                diagnose_domains(domains)
            request.assert_not_called()

    def test_invalid_timeouts_and_worker_counts_fail_without_network(self):
        for options in ({"timeout": 0}, {"timeout": float("nan")}, {"timeout": True},
                        {"workers": 0}, {"workers": 9}, {"workers": True}):
            with self.subTest(options=options), patch.object(Transport, "request") as request, self.assertRaises(ValueError):
                diagnose_domains(["https://example.com"], **options)
            request.assert_not_called()

    def test_cli_diagnostics_and_domain_listing_never_load_account_file(self):
        with patch("zlib_connector.cli.load_credentials") as credentials, \
                patch.object(Transport, "request", return_value=Response(b'{"success":1,"books":[]}', "https://example.com/eapi/book/search")) as request:
            output = io.StringIO()
            with patch("sys.stdout", output):
                self.assertEqual(main(["--domain", "https://example.com", "doctor", "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue())["working_domains"], ["https://example.com/"])
            request.reset_mock()
            with patch("sys.stdout", io.StringIO()):
                self.assertEqual(main(["domains", "--json"]), 0)
            request.assert_not_called()
            credentials.assert_not_called()

    def test_cli_returns_failure_when_no_candidate_can_search(self):
        with patch.object(Transport, "request", side_effect=NetworkError("synthetic", reason="dns")), \
                patch("sys.stdout", io.StringIO()) as output:
            status = main(["doctor", "https://example.com", "--json"])
        self.assertEqual(status, 1)
        self.assertIsNone(json.loads(output.getvalue())["recommended_domain"])


class NetworkReasonTests(unittest.TestCase):
    def test_tcp_timeouts_keep_their_reason_and_share_one_deadline(self):
        now = [0.0]

        def resolve(host):
            now[0] += 1  # DNS uses part of the same five-second budget.
            return ["8.8.8.8", "1.1.1.1"]

        def fail_connection(*args):
            now[0] += 2
            raise TimeoutError("synthetic-private")

        with patch("zlib_connector.transport.public_addresses", side_effect=resolve), \
                patch("zlib_connector.transport.time.monotonic", side_effect=lambda: now[0]), \
                patch("socket.create_connection", side_effect=fail_connection) as connect, \
                self.assertRaises(NetworkError) as caught:
            Transport(timeout=5).request("https://example.com/")
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertNotIn("synthetic-private", str(caught.exception))
        self.assertEqual([call.args[1] for call in connect.call_args_list], [4, 2])

    def test_expired_deadline_stops_before_trying_another_address(self):
        now = [0.0]

        def fail_connection(*args):
            now[0] += 6  # The first attempt exhausts the whole request deadline.
            raise TimeoutError

        with patch("zlib_connector.transport.public_addresses", return_value=["8.8.8.8", "1.1.1.1"]), \
                patch("zlib_connector.transport.time.monotonic", side_effect=lambda: now[0]), \
                patch("socket.create_connection", side_effect=fail_connection) as connect, \
                self.assertRaises(NetworkError) as caught:
            Transport(timeout=5).request("https://example.com/")
        self.assertEqual(caught.exception.reason, "timeout")
        connect.assert_called_once()
        self.assertEqual(connect.call_args.args[:2], (("8.8.8.8", 443), 5))

    def test_dns_error_reason_is_available_through_transport(self):
        with patch("socket.getaddrinfo", side_effect=OSError("synthetic-private-details")):
            with self.assertRaises(NetworkError) as caught:
                Transport().request("https://example.com/")
        self.assertEqual(caught.exception.reason, "dns")
        self.assertNotIn("synthetic-private-details", str(caught.exception))

    def test_tls_certificate_and_timeout_errors_are_distinct(self):
        for error, expected in ((ssl.SSLCertVerificationError("synthetic"), "certificate"),
                                (ssl.SSLError("synthetic"), "tls"), (TimeoutError("synthetic"), "timeout")):
            connection = MagicMock()
            connection.request.side_effect = error
            with self.subTest(expected=expected), patch("zlib_connector.transport.public_addresses", return_value=["8.8.8.8"]), \
                    patch("http.client.HTTPSConnection", return_value=connection), self.assertRaises(NetworkError) as caught:
                Transport().request("https://example.com/")
            self.assertEqual(caught.exception.reason, expected)
            self.assertNotIn("synthetic", str(caught.exception))
            connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
