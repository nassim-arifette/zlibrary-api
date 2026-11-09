import io
import json
import unittest
from unittest.mock import patch

from zlib_connector import AuthenticationError, Client, FormatChangedError
from zlib_connector.cli import main
from zlib_connector.credentials import Credentials
from test_eapi import ORIGIN, QueueTransport


def signed_in_transport(*users):
    return QueueTransport([{"response": {}}, {"success": 1, "user": {}},
                           *({"success": 1, "user": user} for user in users)])


class AccountTests(unittest.TestCase):
    def test_profile_projects_only_quota_and_ignores_remote_secrets(self):
        user = {"downloads_limit": "10", "downloads_today": 3, "remix_userkey": "synthetic-secret",
                "email": "synthetic@example.com", "userkey": "synthetic-key", "nested": {"password": "synthetic"}}
        transport = signed_in_transport(user)
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            profile = client.get_profile()
        self.assertEqual(profile.to_dict(), {"quota": {"limit": 10, "used": 3, "remaining": 7}})
        self.assertNotIn("synthetic", repr(profile))
        self.assertNotIn("synthetic", json.dumps(profile.to_dict()))

    def test_unknown_fields_stay_unknown_and_exhaustion_is_zero(self):
        users = [{}, {"downloads_limit": 10}, {"downloads_limit": 0, "downloads_today": 0},
                 {"downloads_limit": 5, "downloads_today": 7}]
        transport = signed_in_transport(*users)
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            quotas = [client.get_quota() for _ in users]
        self.assertEqual([quota.remaining for quota in quotas], [None, None, 0, 0])
        self.assertIsNone(quotas[0].limit)
        self.assertIsNone(quotas[1].used)

    def test_quota_refreshes_instead_of_returning_stale_values(self):
        transport = signed_in_transport({"downloads_limit": 10, "downloads_today": 3},
                                        {"downloads_limit": 10, "downloads_today": 4})
        with Client(ORIGIN, transport=transport) as client:
            client.login("synthetic", "synthetic")
            self.assertEqual(client.get_quota().remaining, 7)
            self.assertEqual(client.get_quota().remaining, 6)

    def test_invalid_quota_is_not_silently_coerced(self):
        for invalid in (True, -1, 3.5, "ten synthetic-secret", {}, []):
            transport = signed_in_transport({"downloads_limit": invalid, "downloads_today": 0})
            with self.subTest(invalid=invalid), Client(ORIGIN, transport=transport) as client:
                client.login("synthetic", "synthetic")
                with self.assertRaises(FormatChangedError) as caught:
                    client.get_quota()
                self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_profile_requires_session_and_valid_user_object(self):
        transport = signed_in_transport(None)
        with Client(ORIGIN, transport=transport) as client:
            with self.assertRaises(AuthenticationError):
                client.get_profile()
            self.assertEqual(transport.calls, [])
            client.login("synthetic", "synthetic")
            with self.assertRaises(FormatChangedError):
                client.get_profile()

    def test_cli_profile_and_quota_json_have_no_account_secrets(self):
        for command, expected in (("quota", {"limit": 5, "used": 2, "remaining": 3}),
                                  ("profile", {"quota": {"limit": 5, "used": 2, "remaining": 3}})):
            transport = signed_in_transport({"downloads_limit": 5, "downloads_today": 2, "userkey": "synthetic-secret"})
            output = io.StringIO()
            with patch("zlib_connector.cli.Client", return_value=Client(ORIGIN, transport=transport)), \
                    patch("zlib_connector.cli.load_credentials", return_value=Credentials(ORIGIN, "synthetic", "synthetic")), \
                    patch("sys.stdout", output):
                self.assertEqual(main([command, "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue()), expected)


if __name__ == "__main__":
    unittest.main()
