from email.message import Message
import io
import unittest
from unittest.mock import patch

from zlib_connector import ChallengeRequired, DownloadError, NetworkError, RateLimitError
from zlib_connector.transport import Transport

class Reply:
    def __init__(self, status=200, data=b'ok', headers=()):
        self.status = status
        self.headers = Message()
        for name, value in headers:
            self.headers.add_header(name, value)
        self.stream = io.BytesIO(data)

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def info(self):
        return self.headers

    def read(self, count):
        return self.stream.read(count)

class Connection:
    def __init__(self, reply):
        self.reply = reply
        self.sock = None
        self.calls = []
        self.closed = False

    def request(self, *args):
        self.calls.append(args)

    def getresponse(self):
        return self.reply

    def close(self):
        self.closed = True

class TransportTests(unittest.TestCase):
    def invoke(self, replies, *, form=None, transport=None, download=False):
        connections = [Connection(reply) for reply in replies]
        with patch('zlib_connector.transport.public_addresses', return_value=['8.8.8.8']), patch('http.client.HTTPSConnection', side_effect=connections):
            result = (transport or Transport()).request('https://books.example/', form=form, download=download)
        self.assertTrue(all(c.closed for c in connections))
        return result, connections

    def test_login_cookies_are_kept_only_in_memory(self):
        transport = Transport()
        reply = Reply(headers=[('Set-Cookie', 'remix_userkey=synthetic; Secure; Path=/'), ('Set-Cookie', 'remix_userid=123; Secure; Path=/')])
        self.invoke([reply], form={'password':'synthetic-password'}, transport=transport)
        self.assertTrue(transport.authenticated)
        _, connections = self.invoke([Reply()], transport=transport)
        headers = connections[0].calls[0][3]
        self.assertIn('remix_userkey=synthetic', headers['Cookie'])
        transport.close()
        self.assertFalse(transport.authenticated)

    def test_cross_origin_redirect_does_not_forward_cookies(self):
        transport = Transport()
        reply = Reply(302, headers=[('Location','https://cdn.example/book'), ('Set-Cookie','remix_userkey=synthetic; Secure; Path=/')])
        result, connections = self.invoke([reply,Reply()], transport=transport)
        self.assertEqual(result.url, 'https://cdn.example/book')
        self.assertNotIn('Cookie', connections[1].calls[0][3])

    def test_api_array_form_encoding(self):
        _, connections = self.invoke([Reply()], form={'extensions[]':['pdf','epub']})
        self.assertEqual(connections[0].calls[0][2],b'extensions%5B%5D=pdf&extensions%5B%5D=epub')

    def test_api_auth_headers_are_scoped_to_cookie_origin(self):
        transport = Transport()
        reply = Reply(headers=[('Set-Cookie','remix_userkey=synthetic; Secure; Path=/'),('Set-Cookie','remix_userid=123; Secure; Path=/')])
        self.invoke([reply],transport=transport)
        connections=[Connection(Reply(302,headers=[('Location','https://cdn.example/eapi/file')])),Connection(Reply())]
        with patch('zlib_connector.transport.public_addresses',return_value=['8.8.8.8']), patch('http.client.HTTPSConnection',side_effect=connections):
            transport.request('https://books.example/eapi/book/123/abc/file')
        self.assertEqual(connections[0].calls[0][3]['Remix-userkey'],'synthetic')
        self.assertNotIn('Remix-userkey',connections[1].calls[0][3])
        self.assertNotIn('Remix-userid',connections[1].calls[0][3])

    def test_login_redirect_never_replays_credentials(self):
        with self.assertRaises(NetworkError):
            self.invoke([Reply(307, headers=[('Location','https://other.example/')])], form={'password':'synthetic'})

    def test_same_origin_307_preserves_post_and_session_cookie(self):
        reply = Reply(307, headers=[('Location','https://books.example/rpc.php'),('Set-Cookie','session=synthetic; Secure; Path=/')])
        _, connections = self.invoke([reply,Reply()], form={'password':'synthetic-password'})
        self.assertEqual(connections[1].calls[0][0], 'POST')
        self.assertIn('session=synthetic',connections[1].calls[0][3]['Cookie'])

    def test_login_self_redirect_loop_is_actionable(self):
        with self.assertRaises(ChallengeRequired):
            self.invoke([Reply(307,headers=[('Location','https://books.example/')])],form={'password':'synthetic'})

    def test_cleartext_redirect_is_blocked(self):
        with self.assertRaises(NetworkError):
            self.invoke([Reply(302, headers=[('Location','http://books.example/download')])])

    def test_body_limit_and_incomplete_transfer_are_errors(self):
        with self.assertRaises(DownloadError):
            self.invoke([Reply(data=b'12345')], transport=Transport(max_download_bytes=4), download=True)
        with self.assertRaises(DownloadError):
            self.invoke([Reply(headers=[('Content-Length','5')])], transport=Transport(max_download_bytes=4), download=True)
        with self.assertRaises(NetworkError):
            self.invoke([Reply(data=b'ab', headers=[('Content-Length','5')])])

    def test_site_status_codes_have_typed_errors(self):
        for status, error in ((403,ChallengeRequired),(503,ChallengeRequired),(429,RateLimitError),(513,ChallengeRequired),(517,ChallengeRequired)):
            with self.subTest(status=status), self.assertRaises(error):
                self.invoke([Reply(status)])

if __name__ == '__main__':
    unittest.main()
