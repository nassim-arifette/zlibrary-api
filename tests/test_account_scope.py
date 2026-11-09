"""Account namespaces use a private HMAC, with no network or cookie disclosure."""
import unittest
from unittest.mock import patch

from zlib_connector import Client, AuthenticationError
from test_session_client import auth_cookie, seed


class AccountScopeTests(unittest.TestCase):
    def client(self, domain="books.example", account="123"):
        client = Client("https://" + domain + "/")
        seed(client._transport, domain=domain)
        client._transport.cookies.set_cookie(auth_cookie("remix_userid", account, domain=domain))
        client._authenticated = True
        self.addCleanup(client.close)
        return client

    def test_account_domain_and_private_key_define_namespace(self):
        client = self.client()
        key = "a" * 43
        with patch.object(client._transport, "request", side_effect=AssertionError("Unexpected network")):
            fingerprint = client.account_fingerprint(key)
        self.assertRegex(fingerprint, r"^[a-f0-9]{64}$")
        self.assertEqual(fingerprint, self.client().account_fingerprint(key))
        self.assertNotEqual(fingerprint, self.client(account="124").account_fingerprint(key))
        self.assertNotEqual(fingerprint, self.client(domain="other.example").account_fingerprint(key))
        self.assertNotEqual(fingerprint, client.account_fingerprint("b" * 43))

    def test_cookie_key_rotation_preserves_scope(self):
        client = self.client()
        before = client.account_fingerprint("a" * 43)
        client._transport.cookies.set_cookie(auth_cookie("remix_userkey", "rotated-synthetic-cookie"))
        self.assertEqual(before, client.account_fingerprint("a" * 43))

    def test_requires_verified_account_and_valid_private_key(self):
        client = self.client()
        for key in (None, "short", "a" * 257, "\n" + "a" * 43):
            with self.subTest(key=key), self.assertRaises(ValueError):
                client.account_fingerprint(key)
        client._authenticated = False
        with self.assertRaises(AuthenticationError):
            client.account_fingerprint("a" * 43)
