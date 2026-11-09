"""Account download history through real loopback HTTP and the real API client."""

import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from zlib_connector import Client
from test_eapi import ORIGIN, QueueTransport
from test_http_api import request, running


def account_transport(*responses):
    return QueueTransport([{"response": {}}, {"success": 1, "user": {}}, *responses])


class HttpHistoryTests(unittest.TestCase):
    def test_history_reads_upstream_downloads_and_preserves_pagination_and_sparse_formats(self):
        payload = {"success": 1, "books": [
            {"id": 456, "hash": "cd34", "title": "Zulu sparse record"},
            {"id": 123, "hash": "ab12", "title": "Alpha legacy format", "extension": "mobi"},
        ], "pagination": {"current": "2", "limit": "3", "total_items": "8", "total_pages": "3",
                            "next": "3", "before": "1"}}
        transport = account_transport(payload)
        with patch("zlib_connector.credentials.load_credentials") as credentials, \
                patch("sqlite3.connect") as database, Client(ORIGIN, transport=transport) as client:
            client.login("synthetic-account", "synthetic-password")
            with running(client) as (server, _):
                status, headers, result = request(
                    server, "GET", "/v1/history?page=2&limit=3&order=downloaded_date")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(result["pagination"], {"page": 2, "limit": 3, "total_items": 8,
                                                "total_pages": 3, "next_page": 3, "previous_page": 1})
        self.assertEqual([book["book_id"] for book in result["books"]], ["456", "123"])
        self.assertEqual([book["extension"] for book in result["books"]], ["", "mobi"])
        self.assertEqual(result["books"][0]["authors"], "")
        self.assertIsNone(result["books"][0]["is_favorite"])
        upstream = urlsplit(transport.calls[-1][0])
        self.assertEqual(upstream.path, "/eapi/user/book/downloaded")
        self.assertEqual(parse_qs(upstream.query), {"page": ["2"], "limit": ["3"], "order": ["downloaded_date"]})
        self.assertEqual(transport.methods, ["POST", "GET", "GET"])
        self.assertTrue(all(not call[2] for call in transport.calls))
        self.assertEqual(transport.responses, [])
        credentials.assert_not_called()
        database.assert_not_called()

    def test_local_api_token_does_not_replace_the_required_zlibrary_account(self):
        transport = QueueTransport([])
        with patch("zlib_connector.credentials.load_credentials") as credentials, \
                patch("sqlite3.connect") as database, Client(ORIGIN, transport=transport) as client:
            with running(client) as (server, _):
                status, _, result = request(server, "GET", "/v1/history?page=2&limit=3&order=downloaded_date")
        self.assertEqual(status, 424)
        self.assertEqual(result["error"]["code"], "upstream_authentication_required")
        self.assertFalse(result["error"]["retryable"])
        self.assertEqual(transport.calls, [])
        credentials.assert_not_called()
        database.assert_not_called()

    def test_expired_upstream_session_returns_424_without_remote_error_details(self):
        transport = account_transport({"success": 0, "error": "Authorization failed synthetic-private-session"})
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic-account", "synthetic-password")
            with running(client) as (server, _):
                status, _, result = request(server, "GET", "/v1/history")
        self.assertEqual(status, 424)
        self.assertEqual(result["error"]["code"], "upstream_authentication_required")
        self.assertFalse(result["error"]["retryable"])
        self.assertNotIn("synthetic-private-session", json.dumps(result))
        self.assertNotIn("synthetic-password", json.dumps(result))
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(transport.responses, [])

    def test_upstream_quota_returns_429_without_automatically_repeating_the_read(self):
        transport = account_transport({"success": 0, "error": "quota reached synthetic-private-detail"})
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic-account", "synthetic-password")
            with running(client) as (server, _):
                status, _, result = request(server, "GET", "/v1/history")
        self.assertEqual(status, 429)
        self.assertEqual(result["error"]["code"], "upstream_rate_limited")
        self.assertTrue(result["error"]["retryable"])
        self.assertNotIn("synthetic-private-detail", json.dumps(result))
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(transport.responses, [])

    def test_absent_upstream_pagination_stays_unknown_instead_of_claiming_no_history(self):
        transport = account_transport({"success": 1, "books": []})
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic-account", "synthetic-password")
            with running(client) as (server, _):
                status, _, result = request(server, "GET", "/v1/history?page=2&limit=7")
        self.assertEqual(status, 200)
        self.assertEqual(result, {"books": [], "pagination": {
            "page": 2, "limit": 7, "total_items": None, "total_pages": None,
            "next_page": None, "previous_page": None}})
        self.assertEqual(parse_qs(urlsplit(transport.calls[-1][0]).query), {"page": ["2"], "limit": ["7"]})


if __name__ == "__main__":
    unittest.main()
