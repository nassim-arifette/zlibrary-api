import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from zlib_connector import Client, Book, AuthenticationError, DownloadError, FormatChangedError, RateLimitError
from zlib_connector.cli import main
from zlib_connector.transport import Response

ORIGIN = 'https://books.example/'
BOOK = {'id': 123, 'hash': 'ab12', 'title': 'Example', 'author': 'Author', 'extension': 'pdf',
        'href': ORIGIN+'book/opaque/example.html', 'year': 1900, 'language': 'English', 'filesizeString': '2 KB'}

class QueueTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.authenticated = False

    def close(self):
        self.authenticated = False

    def request(self, url, *, form=None, download=False, follow_redirects=True):
        self.calls.append((url, form, download))
        value = self.responses.pop(0)
        if url.endswith('/rpc.php'):
            self.authenticated = True
        if isinstance(value, Response):
            return value
        return Response(value if isinstance(value, bytes) else json.dumps(value).encode(), url)

class EapiTests(unittest.TestCase):
    def test_public_search_parses_real_shape_and_preserves_api_reference(self):
        transport = QueueTransport([{'success':1,'books':[BOOK],'pagination':{'current':1}}])
        with Client(ORIGIN,transport=transport) as client:
            books = client.search('Example',extension='pdf',limit=3)
        self.assertEqual(books[0].book_id,'123')
        self.assertEqual(books[0].book_hash,'ab12')
        self.assertEqual(books[0].url,BOOK['href'])
        self.assertEqual(transport.calls[0][0],ORIGIN+'eapi/book/search')
        self.assertEqual(transport.calls[0][1]['message'],'Example')
        self.assertEqual(transport.calls[0][1]['extensions[]'],'pdf')

    def test_login_confirms_profile_before_download_and_publishes_file(self):
        transport = QueueTransport([{'response':{}},{'success':1,'user':{'id':123}},
                                    {'success':1,'books':[BOOK]},
                                    {'success':1,'file':{'allowDownload':True,'downloadLink':'https://cdn.example/file','extension':'pdf'}},
                                    b'%PDF-1.7\nsynthetic'])
        with tempfile.TemporaryDirectory() as directory, Client(ORIGIN,transport=transport) as client:
            client.login('synthetic@example.com','synthetic-password')
            book = client.search('Example')[0]
            result = client.download(book,directory)
            self.assertTrue(result.read_bytes().startswith(b'%PDF-'))
            self.assertEqual(transport.calls[-2][0],ORIGIN+'eapi/book/123/ab12/file')
            self.assertTrue(transport.calls[-1][2])
        self.assertFalse(transport.authenticated)

    def test_unconfirmed_profile_clears_session(self):
        transport = QueueTransport([{'response':{}},{'success':1}])
        client = Client(ORIGIN,transport=transport)
        with self.assertRaises(FormatChangedError):
            client.login('synthetic','synthetic')
        self.assertFalse(transport.authenticated)

    def test_empty_list_is_success_but_missing_list_is_format_error(self):
        transport = QueueTransport([{'success':1,'books':[]},{'success':1}])
        with Client(ORIGIN,transport=transport) as client:
            self.assertEqual(client.search('missing'),[])
            with self.assertRaises(FormatChangedError): client.search('missing')

    def test_api_errors_never_echo_remote_secrets(self):
        for error, expected in [('Authorization failed synthetic-secret',AuthenticationError),('quota synthetic-secret',RateLimitError)]:
            transport = QueueTransport([{'success':0,'error':error}])
            with Client(ORIGIN,transport=transport) as client, self.assertRaises(expected) as caught:
                client.search('example')
            self.assertNotIn('synthetic-secret',str(caught.exception))

    def test_invalid_reference_cannot_become_download_path(self):
        transport = QueueTransport([{'success':1,'books':[{**BOOK,'hash':'../../account'}]}])
        with Client(ORIGIN,transport=transport) as client, self.assertRaises(FormatChangedError):
            client.search('example')

    def test_denied_download_does_not_request_file(self):
        transport = QueueTransport([{'response':{}},{'success':1,'user':{}},
                                    {'success':1,'file':{'allowDownload':False,'downloadLink':'https://cdn.example/file'}}])
        with tempfile.TemporaryDirectory() as directory, Client(ORIGIN,transport=transport) as client:
            client.login('synthetic','synthetic')
            with self.assertRaises(DownloadError):
                client.download(Book('Example','','pdf',ORIGIN+'book/123/ab12',book_id='123',book_hash='ab12'),directory)
            self.assertEqual(list(Path(directory).iterdir()),[])
        self.assertEqual(len(transport.calls),3)

    def test_anonymous_cli_never_loads_local_credentials(self):
        with patch('zlib_connector.cli.Client') as constructor, patch('zlib_connector.cli.load_credentials') as load, patch('sys.stdout',io.StringIO()):
            constructor.return_value.__enter__.return_value.search.return_value=[]
            self.assertEqual(main(['--domain',ORIGIN,'search','Example','--anonymous','--json']),0)
            load.assert_not_called()
            constructor.return_value.__enter__.return_value.login.assert_not_called()

    def test_check_exercises_search_instead_of_just_the_homepage(self):
        with patch('zlib_connector.cli.Client') as constructor, patch('zlib_connector.cli.load_credentials') as load, patch('sys.stdout',io.StringIO()):
            constructor.return_value.__enter__.return_value.search.return_value=[]
            constructor.return_value.__enter__.return_value.domain=ORIGIN
            self.assertEqual(main(['--domain',ORIGIN,'check']),0)
            constructor.return_value.__enter__.return_value.search.assert_called_once()
            load.assert_not_called()

if __name__ == '__main__':
    unittest.main()
