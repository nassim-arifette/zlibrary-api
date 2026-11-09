import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from zlib_connector.credentials import Credentials, load_credentials
from zlib_connector.cli import main

class CredentialTests(unittest.TestCase):
    def test_loads_utf8_bom_and_preserves_password_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'credentials.toml'
            path.write_text('domain="https://books.example/"\nemail="synthetic@example.com"\npassword=" spaced synthetic "',encoding='utf-8-sig')
            credentials = load_credentials(path)
            self.assertEqual(credentials.password, ' spaced synthetic ')
            self.assertNotIn('spaced', repr(credentials))
            self.assertNotIn('synthetic', repr(credentials))

    def test_bad_syntax_and_types_do_not_echo_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'credentials.toml'
            for value in ('password="synthetic-secret', 'password=123', 'unknown="synthetic-secret"'):
                path.write_text(value,encoding='utf-8')
                with self.assertRaises(ValueError) as error:
                    load_credentials(path)
                self.assertNotIn('synthetic-secret',str(error.exception))

    def test_explicit_missing_file_fails_without_prompt(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            load_credentials(Path(directory)/'missing.toml')

    def test_auth_test_uses_file_without_prompting_or_printing_secrets(self):
        stdout = io.StringIO()
        with patch('zlib_connector.cli.load_credentials', return_value=Credentials('https://books.example/','synthetic@example.com','synthetic-password')), patch('zlib_connector.cli.Client') as constructor, patch('zlib_connector.cli.prompt') as prompt, patch('getpass.getpass') as password_prompt, patch('sys.stdout',stdout):
            self.assertEqual(main(['auth-test']),0)
            client=constructor.return_value.__enter__.return_value
            client.login.assert_called_once_with('synthetic@example.com','synthetic-password')
            constructor.assert_called_once_with('https://books.example/',timeout=30)
            prompt.assert_not_called()
            password_prompt.assert_not_called()
        self.assertEqual(stdout.getvalue().strip(),'{"authenticated": true}')

    def test_domain_override_is_explicit(self):
        with patch('zlib_connector.cli.load_credentials',return_value=Credentials('https://old.example/','synthetic','synthetic')), patch('zlib_connector.cli.Client') as constructor, patch('sys.stdout',io.StringIO()):
            main(['--domain','https://new.example/','auth-test'])
            constructor.assert_called_once_with('https://new.example/',timeout=30)
