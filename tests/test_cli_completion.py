"""Shell completion: candidates, glue scripts, and proof that __complete is offline and side-effect free."""
from contextlib import redirect_stderr, redirect_stdout
import builtins
import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from zlib_connector.cli import main
from zlib_connector.cli_completion import FILES, SHELLS, candidates, glue, run_complete, split_line
from zlib_connector.client import SUPPORTED_FORMATS
from zlib_connector.search import SEARCH_ORDERS
from zlib_connector.settings import KEYS


def complete(*arguments):
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(io.StringIO()):
        code = main(["__complete", *arguments])
    return code, output.getvalue().splitlines()


class CandidateTests(unittest.TestCase):
    def test_commands_actions_options_and_choices(self):
        cases = {
            ("se",): ["search", "serve"],
            ("queue", ""): ["add", "list", "resume"],
            ("search", "--or"): ["--order"],
            ("search", "--order", ""): list(SEARCH_ORDERS),
            ("search", "--format", "e"): ["epub"],
            ("search", "--format=e"): ["--format=epub"],
            ("search", "--format", "=", "e"): ["epub"],
            ("completion", ""): list(SHELLS),
            ("config", "set", ""): list(KEYS),
            ("config", "set", "formats", ""): list(SUPPORTED_FORMATS),
            ("config", "set", "progress", "a"): ["auto", "always"],
            ("mcp", "--print-config", "c"): ["claude-code", "claude-desktop", "codex"],
            ("--json", "dow"): ["download"],
            ("--domain", "https://z-lib.by/", "doc"): ["doctor"],
            ("info", ""): [],
            ("search", "dune", ""): [],
        }
        for words, expected in cases.items():
            with self.subTest(words=words):
                self.assertEqual(expected, candidates(words))

    def test_path_options_fall_back_to_file_completion(self):
        for words in (("download", "--output", ""), ("favorites", "add", "-a", "re"), ("queue", "--token-file", ""),
                      ("config", "set", "download_dir", "")):
            with self.subTest(words=words):
                self.assertEqual([FILES], candidates(words))

    def test_hidden_commands_are_not_offered(self):
        found = candidates([""])
        self.assertIn("completion", found)
        self.assertFalse({"check", "auth-test", "__complete"} & set(found))

    def test_domains_come_from_the_known_list(self):
        self.assertIn("https://z-lib.by/", candidates(["--domain", "https://z-lib.b"]))
        self.assertIn("https://z-lib.by/", candidates(["doctor", "https://z-lib.b"]))


class ProtocolTests(unittest.TestCase):
    def test_words_after_double_dash(self):
        self.assertEqual((0, ["search", "serve"]), complete("zsh", "--", "se"))

    def test_powershell_passes_the_current_word_as_an_option(self):
        self.assertEqual((0, list(SHELLS)), complete("powershell", "--current=", "--", "completion"))
        self.assertEqual((0, ["search", "serve"]), complete("powershell", "--current=se", "--"))

    def test_bash_gets_the_line_and_candidates_after_the_last_word_break(self):
        self.assertEqual((0, ["//z-lib.by/"]), complete("bash", "--line=zlib-cli --domain https://z-lib.b"))
        self.assertEqual((0, ["epub"]), complete("bash", "--line=zlib-cli search --format=e"))
        self.assertEqual((0, [FILES]), complete("bash", "--line=zlib-cli download --output "))

    def test_split_line_keeps_an_open_quote_and_a_trailing_space(self):
        self.assertEqual(["zlib-cli", "search", ""], split_line("zlib-cli search "))
        self.assertEqual(["zlib-cli", "search", "le pet"], split_line("zlib-cli search 'le pet"))

    def test_bad_requests_print_nothing(self):
        self.assertEqual((2, []), complete("tcsh", "--", "se"))
        with patch("zlib_connector.cli_completion.candidates", side_effect=RuntimeError("boom")):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(1, run_complete(["bash", "--", "se"]))
        self.assertEqual("", output.getvalue())


class SideEffectTests(unittest.TestCase):
    def test_complete_never_connects_reads_account_files_or_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            for name in ("config.toml", "credentials.json", "session.json", "api-token"):
                (home / name).write_text("secret\n", encoding="utf-8")
            environment = {"HOME": str(home), "USERPROFILE": str(home), "XDG_CONFIG_HOME": str(home),
                           "XDG_STATE_HOME": str(home), "APPDATA": str(home), "LOCALAPPDATA": str(home),
                           "ZLIB_STATE_DIR": str(home), "ZLIB_CONFIG": str(home / "config.toml"),
                           "ZLIB_CREDENTIALS": str(home / "credentials.json")}
            touched = []
            real_open, real_os_open = builtins.open, os.open

            def guarded_open(file, mode="r", *args, **kwargs):
                if isinstance(file, (str, os.PathLike)):
                    if any(flag in mode for flag in "wax+") or Path(file).resolve().is_relative_to(home):
                        touched.append((os.fspath(file), mode))
                        raise PermissionError(file)
                return real_open(file, mode, *args, **kwargs)

            def guarded_os_open(path, flags, *args, **kwargs):
                if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT) or Path(path).resolve().is_relative_to(home):
                    touched.append((os.fspath(path), flags))
                    raise PermissionError(path)
                return real_os_open(path, flags, *args, **kwargs)

            def refuse(*args, **kwargs):
                touched.append("network")
                raise OSError("network")

            with patch.dict(os.environ, environment), patch("builtins.open", guarded_open), \
                    patch("io.open", guarded_open), patch("os.open", guarded_os_open), \
                    patch.object(socket.socket, "connect", refuse), patch("socket.create_connection", refuse), \
                    patch("zlib_connector.cli.ensure_settings", side_effect=AssertionError("settings loaded")):
                for arguments in (("bash", "--line=zlib-cli "), ("bash", "--line=zlib-cli config set formats "),
                                  ("fish", "--", "download", "--output", ""), ("zsh", "--", "--domain", "")):
                    code, found = complete(*arguments)
                    self.assertEqual(0, code)
                    self.assertTrue(found)
            self.assertEqual([], touched)


class GlueTests(unittest.TestCase):
    def test_completion_prints_the_glue_without_loading_settings(self):
        for shell in SHELLS:
            output = io.StringIO()
            with self.subTest(shell=shell), redirect_stdout(output), \
                    patch("zlib_connector.cli.ensure_settings", side_effect=AssertionError("settings loaded")):
                self.assertEqual(0, main(["completion", shell]))
            self.assertEqual(glue(shell), output.getvalue())
            self.assertIn("zlib-cli __complete " + shell, output.getvalue())

    def test_the_hidden_command_stays_out_of_the_help(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            main(["--help"])
        self.assertIn("completion", output.getvalue())
        self.assertNotIn("__complete", output.getvalue())


@unittest.skipIf(sys.platform == "win32", "the shells run through a POSIX wrapper script")
class RealShellTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        wrapper = Path(folder.name) / "zlib-cli"
        root = Path(__file__).resolve().parent.parent
        wrapper.write_text(f'#!/bin/sh\nPYTHONPATH="{root}" exec "{sys.executable}" -m zlib_connector "$@"\n')
        wrapper.chmod(0o755)
        self.environment = {**os.environ, "PATH": f"{folder.name}{os.pathsep}{os.environ.get('PATH', '')}"}

    def run_shell(self, *command):
        return subprocess.run(command, capture_output=True, text=True, env=self.environment, timeout=60).stdout

    @unittest.skipIf(shutil.which("bash") is None, "bash is not installed")
    def test_bash(self):
        script = '''eval "$(zlib-cli completion bash)"
COMP_LINE="zlib-cli --domain https://z-lib.b"; COMP_POINT=${#COMP_LINE}
COMP_WORDS=(zlib-cli --domain https : //z-lib.b); COMP_CWORD=4
_zlib_cli_complete; printf '%s\\n' "${COMPREPLY[@]}"
COMP_LINE="zlib-cli se"; COMP_POINT=${#COMP_LINE}; COMP_WORDS=(zlib-cli se); COMP_CWORD=1
_zlib_cli_complete; printf '%s\\n' "${COMPREPLY[@]}"'''
        self.assertEqual(["//z-lib.by/", "search", "serve"], self.run_shell("bash", "-c", script).split())

    @unittest.skipIf(shutil.which("fish") is None, "fish is not installed")
    def test_fish(self):
        script = 'zlib-cli completion fish | source; complete -C "zlib-cli search --format e"'
        self.assertEqual(["epub"], self.run_shell("fish", "-c", script).split())


if __name__ == "__main__":
    unittest.main()
