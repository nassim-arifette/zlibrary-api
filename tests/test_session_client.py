"""Session transport/client contracts with synthetic cookies and API responses."""

from http.cookiejar import Cookie
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.parse import urlsplit
from urllib.request import Request

from zlib_connector import Client
from zlib_connector.errors import AuthenticationError, ChallengeRequired, FormatChangedError, NetworkError
from zlib_connector.session_data import SavedSession, SessionCookie
from zlib_connector.transport import Response, Transport


ORIGIN = "https://books.example/"
KEY = "synthetic-account-cookie"


def auth_cookie(name, value, *, domain="books.example", path="/", expires=None, domain_specified=False):
    return Cookie(version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=domain_specified, domain_initial_dot=domain.startswith("."),
        path=path, path_specified=True, secure=True, expires=expires, discard=expires is None,
        comment=None, comment_url=None, rest={"HttpOnly": None}, rfc2109=False)


def seed(transport, *, domain="books.example", path="/", expires=None):
    for name, value in (("remix_userid", "123"), ("remix_userkey", KEY)):
        transport.cookies.set_cookie(auth_cookie(name, value, domain=domain, path=path, expires=expires))


def saved(*, origin=ORIGIN, expires=None, path="/"):
    return SavedSession(origin, (SessionCookie("remix_userid", "123", path, expires),
                                 SessionCookie("remix_userkey", KEY, path, expires)), int(time.time()))


def cookie_header(transport, url):
    request = Request(url)
    transport.cookies.add_cookie_header(request)
    return request.get_header("Cookie", "")


class SessionTransport(Transport):
    """A real cookie jar/session implementation with queued, offline API replies."""

    def __init__(self, responses, *, on_request=None):
        super().__init__()
        self.responses = list(responses)
        self.calls = []
        self.on_request = on_request

    def request(self, url, *, form=None, **kwargs):
        self.calls.append((url, form, kwargs))
        if self.on_request is not None:
            self.on_request(self, url)
        if url.endswith("/rpc.php"):
            seed(self, domain=urlsplit(url).hostname)
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return Response(json.dumps(result).encode(), url)


def store_for(session=None, *, generation="initial-generation"):
    snapshot = SimpleNamespace(generation=generation, session=session,
        status=lambda: session.status() if session else
        {"state": "empty", "domain": None, "saved_at": None, "expires_at": None, "verified": False})
    return Mock(read=Mock(return_value=snapshot), save=Mock(), clear=Mock())


class SessionTransportTests(unittest.TestCase):
    def test_export_uses_only_applicable_authentication_cookies(self):
        transport = Transport()
        expires = int(time.time()) + 1000
        seed(transport, path="/eapi/", expires=expires)
        seed(transport, domain="other.example")
        transport.cookies.set_cookie(auth_cookie("challenge", "synthetic-browser-cookie"))
        original = list(transport.cookies)
        result = transport.export_session("https://BOOKS.example:443/")
        self.assertEqual(ORIGIN, result.origin)
        self.assertEqual({"remix_userid", "remix_userkey"}, {cookie.name for cookie in result.cookies})
        self.assertTrue(all(cookie.path == "/eapi/" and cookie.expires == expires for cookie in result.cookies))
        self.assertEqual(original, list(transport.cookies))
        self.assertNotIn(KEY, repr(result))

    def test_aggregate_authentication_flag_cannot_authorize_cross_origin_export(self):
        transport = Transport()
        transport.cookies.set_cookie(auth_cookie("remix_userid", "123"))
        transport.cookies.set_cookie(auth_cookie("remix_userkey", KEY, domain="other.example"))
        self.assertTrue(transport.authenticated)  # Legacy aggregate flag is deliberately insufficient.
        with self.assertRaises(AuthenticationError) as caught:
            transport.export_session(ORIGIN)
        self.assertNotIn(KEY, str(caught.exception))

    def test_expired_or_wrong_path_cookies_are_not_exported(self):
        for options in ({"expires": int(time.time()) - 1}, {"path": "/other/"}):
            transport = Transport()
            seed(transport, **options)
            with self.subTest(options=options), self.assertRaises(AuthenticationError):
                transport.export_session(ORIGIN)

    def test_duplicate_names_and_unsafe_values_are_rejected(self):
        transport = Transport()
        seed(transport)
        transport.cookies.set_cookie(auth_cookie("remix_userkey", "second-synthetic-key", path="/eapi/"))
        with self.assertRaises(ValueError):
            transport.export_session(ORIGIN)
        for value in ("injected\r\nheader", "contains;separator", "contains,separator", "contains space"):
            transport = Transport()
            seed(transport)
            transport.cookies.set_cookie(auth_cookie("remix_userkey", value))
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                transport.export_session(ORIGIN)
            self.assertNotIn(value, str(caught.exception))

    def test_restored_cookies_are_host_only_secure_and_keep_their_path(self):
        transport = Transport()
        transport.restore_session(saved(path="/eapi/"), origin="https://BOOKS.EXAMPLE:443/")
        self.assertIn(KEY, cookie_header(transport, ORIGIN + "eapi/user/profile"))
        for url in ("https://sub.books.example/eapi/user/profile", "https://other.example/eapi/user/profile",
                    "http://books.example/eapi/user/profile", ORIGIN + "papi/my-library/tags/"):
            self.assertEqual("", cookie_header(transport, url))
        for cookie in transport.cookies:
            self.assertEqual("books.example", cookie.domain)
            self.assertTrue(cookie.secure)
            self.assertFalse(cookie.domain_specified)
            self.assertFalse(cookie.domain_initial_dot)
            self.assertEqual("/eapi/", cookie.path)

    def test_failed_restore_clears_old_cookies(self):
        for session, origin, error in ((saved(), "https://other.example/", ValueError),
                                      (saved(expires=int(time.time()) - 1), ORIGIN, AuthenticationError),
                                      (saved(path="/other/"), ORIGIN, ValueError)):
            transport = Transport()
            seed(transport)
            with self.subTest(origin=origin, error=error), self.assertRaises(error):
                transport.restore_session(session, origin=origin)
            self.assertFalse(list(transport.cookies))


class SessionClientTests(unittest.TestCase):
    def test_login_reads_revision_before_network_and_saves_only_after_confirmation(self):
        events = []
        store = store_for()
        snapshot = store.read.return_value
        store.read.side_effect = lambda: events.append("read") or snapshot
        store.save.side_effect = lambda *args, **kwargs: events.append("save")
        transport = SessionTransport([{"response": {}}, {"success": 1, "user": {}}],
                                     on_request=lambda transport, url: events.append(url))
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic-email", "synthetic-password", session_store=store)
            self.assertTrue(client._authenticated)
            self.assertEqual(["read", ORIGIN + "rpc.php", ORIGIN + "eapi/user/profile", "save"], events)
            persisted = store.save.call_args.args[0]
            self.assertIsInstance(persisted, SavedSession)
            self.assertEqual(ORIGIN, persisted.origin)
            self.assertEqual("initial-generation", store.save.call_args.kwargs["expected_generation"])
        store.read.assert_called_once()
        store.save.assert_called_once()
        store.clear.assert_not_called()

    def test_profile_is_confirmed_before_authenticated_state_or_persistence(self):
        store = store_for()
        transport = SessionTransport([{"response": {}}, {"success": 1, "user": {}}])
        with Client(ORIGIN, transport=transport) as client:
            def inspect(transport, url):
                if url.endswith("/eapi/user/profile"):
                    self.assertFalse(client._authenticated)
                    store.save.assert_not_called()

            transport.on_request = inspect
            client.login("synthetic", "synthetic", session_store=store)

    def test_storage_read_failure_prevents_login_and_clears_memory(self):
        transport = SessionTransport([])
        seed(transport)
        store = store_for()
        store.read.side_effect = ValueError("synthetic storage failure")
        with Client(ORIGIN, transport=transport) as client:
            with self.assertRaises(ValueError):
                client.login("synthetic", "synthetic", session_store=store)
            self.assertEqual([], transport.calls)
            self.assertFalse(list(transport.cookies))
            self.assertFalse(client._authenticated)
        store.save.assert_not_called()

    def test_failed_login_confirmation_or_save_clears_memory_without_retry(self):
        for failure in (NetworkError("synthetic"), ChallengeRequired("synthetic"), {"success": 1, "user": []}):
            store = store_for()
            transport = SessionTransport([{"response": {}}, failure])
            error = type(failure) if isinstance(failure, BaseException) else FormatChangedError
            with self.subTest(error=error), Client(ORIGIN, transport=transport) as client:
                with self.assertRaises(error):
                    client.login("synthetic", "synthetic", session_store=store)
                self.assertFalse(client._authenticated)
                self.assertFalse(list(transport.cookies))
                self.assertEqual(2, len(transport.calls))
            store.save.assert_not_called()
            store.clear.assert_not_called()
        store = store_for()
        store.save.side_effect = ValueError("synthetic save failure")
        transport = SessionTransport([{"response": {}}, {"success": 1, "user": {}}])
        with Client(ORIGIN, transport=transport) as client:
            with self.assertRaises(ValueError):
                client.login("synthetic", "synthetic", session_store=store)
            self.assertFalse(client._authenticated)
            self.assertFalse(list(transport.cookies))
        store.save.assert_called_once()

    def test_restore_confirms_profile_and_returns_only_safe_status(self):
        store = store_for(saved())
        transport = SessionTransport([{"success": 1, "user": {"email": "synthetic-private-email", "id": "synthetic-private-id"}}])
        with Client(ORIGIN, transport=transport) as client:
            def inspect(transport, url):
                self.assertFalse(client._authenticated)
                self.assertIn(KEY, cookie_header(transport, url))

            transport.on_request = inspect
            result = client.restore_session(store)
            self.assertTrue(client._authenticated)
        self.assertEqual({**store.read.return_value.status(), "authenticated": True, "verified": True}, result)
        self.assertEqual([(ORIGIN + "eapi/user/profile", None, {})], transport.calls)
        for secret in (KEY, "synthetic-private-email", "synthetic-private-id"):
            self.assertNotIn(secret, json.dumps(result))
        store.save.assert_not_called()
        store.clear.assert_not_called()

    def test_restore_absent_expired_or_different_domain_does_not_contact_server(self):
        for session, error in ((None, AuthenticationError), (saved(expires=int(time.time()) - 1), AuthenticationError),
                               (saved(origin="https://other.example/"), ValueError)):
            store, transport = store_for(session), SessionTransport([])
            seed(transport)
            with self.subTest(error=error), Client(ORIGIN, transport=transport) as client:
                with self.assertRaises(error):
                    client.restore_session(store)
                self.assertFalse(client._authenticated)
                self.assertFalse(list(transport.cookies))
            self.assertEqual([], transport.calls)
            store.save.assert_not_called()
            store.clear.assert_not_called()

    def test_restore_errors_preserve_stored_state_and_clear_memory(self):
        for response, error in ((NetworkError("synthetic", reason="timeout"), NetworkError),
                               (ChallengeRequired("synthetic"), ChallengeRequired),
                               ({"success": 0, "error": "login required"}, AuthenticationError),
                               ({"success": 1, "user": []}, FormatChangedError)):
            session = saved()
            store, transport = store_for(session), SessionTransport([response])
            with self.subTest(error=error), Client(ORIGIN, transport=transport) as client:
                with self.assertRaises(error):
                    client.restore_session(store)
                self.assertFalse(client._authenticated)
                self.assertFalse(list(transport.cookies))
            self.assertIs(session, store.read.return_value.session)
            self.assertEqual(1, len(transport.calls))
            store.save.assert_not_called()
            store.clear.assert_not_called()

    def test_profile_cookie_rotation_is_not_automatically_saved(self):
        session = saved()
        store = store_for(session)

        def rotate(transport, url):
            transport.cookies.set_cookie(auth_cookie("remix_userkey", "rotated-synthetic-key"))

        transport = SessionTransport([{"success": 1, "user": {}}], on_request=rotate)
        with Client(ORIGIN, transport=transport) as client:
            client.restore_session(store)
            self.assertIn("rotated-synthetic-key", cookie_header(transport, ORIGIN + "eapi/user/profile"))
        store.save.assert_not_called()
        self.assertEqual(KEY, next(cookie.value for cookie in session.cookies if cookie.name == "remix_userkey"))

    def test_logout_during_login_cannot_resurrect_saved_session(self):
        from zlib_connector.sessions import SessionConflictError, SessionStore

        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(Path(folder) / "session.json")
            tombstones = []

            def logout(transport, url):
                if url.endswith("/rpc.php"):
                    tombstones.append(store.clear())

            transport = SessionTransport([{"response": {}}, {"success": 1, "user": {}}], on_request=logout)
            with Client(ORIGIN, transport=transport) as client:
                with self.assertRaises(SessionConflictError):
                    client.login("synthetic", "synthetic", session_store=store)
                self.assertFalse(client._authenticated)
                self.assertFalse(list(transport.cookies))
            current = store.read()
            self.assertIsNone(current.session)
            self.assertEqual(tombstones[0].generation, current.generation)
            self.assertEqual(2, len(transport.calls))


if __name__ == "__main__":
    unittest.main()
