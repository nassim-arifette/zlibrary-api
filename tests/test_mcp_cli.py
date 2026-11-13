"""`zlib-cli mcp` flags, refusals and --print-config; local only, no network and no files written."""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from mcp_fixture import ORIGIN, World, save_session
from session_fixture import isolated_sessions
from zlib_connector.cli import main
from zlib_connector.cli_parser import parser
from zlib_connector.mcp_cli import run_mcp


class NoRead(io.RawIOBase):
    def __init__(self):
        self.reads = 0

    def readable(self):
        return True

    def readline(self, *args):
        self.reads += 1
        return b""

    def readinto(self, buffer):
        self.reads += 1
        return 0


@isolated_sessions
class McpCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.books = self.root / "books"
        self.books.mkdir()
        self.session = self.root / "session.json"

    def run_cli(self, argv, *, world=None):
        world = world or World()
        stdin, stdout, stderr = NoRead(), io.BytesIO(), io.StringIO()
        code = run_mcp(parser().parse_args(argv), client_factory=world.factory, stdin=stdin, stdout=stdout,
                       stderr=stderr)
        return code, stdin, stdout.getvalue(), stderr.getvalue(), world

    def test_c1_refused_combinations_exit_before_reading_stdin(self):
        save_session(self.session)
        link = self.root / "link"
        try:
            os.symlink(self.books, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            link = None
        cases = [["--credentials", str(self.root / "c.toml"), "mcp"], ["--no-session", "mcp"],
                 ["mcp", "--anonymous", "--download-dir", str(self.books)],
                 ["mcp", "--anonymous", "--allow-account-changes"],
                 ["--session-file", str(self.session), "mcp", "--anonymous"],
                 ["mcp", "--no-download-confirmation"], ["--timeout", "0", "mcp"], ["--timeout", "301", "mcp"],
                 ["--timeout", "nan", "mcp"], ["mcp", "--download-dir", str(self.root / "missing")],
                 ["--domain", "http://insecure.example/", "mcp"],
                 ["--domain", "https://other.example/", "--session-file", str(self.session), "mcp"]]
        if link is not None:
            cases.append(["mcp", "--download-dir", str(link)])
        for argv in cases:
            with self.subTest(argv=argv):
                code, stdin, stdout, stderr, world = self.run_cli(argv)
                self.assertEqual(code, 1)
                self.assertEqual(stdin.reads, 0)
                self.assertEqual(stdout, b"")
                self.assertEqual(len(stderr.strip().splitlines()), 1)
                self.assertTrue(stderr.startswith("zlib-cli mcp: "))
                self.assertEqual(world.clients, [])

    def test_d9_a_folder_without_hard_links_stops_startup(self):
        with patch("os.link", side_effect=OSError("synthetic")):
            code, stdin, _, stderr, _ = self.run_cli(["mcp", "--download-dir", str(self.books)])
        self.assertEqual((code, stdin.reads), (1, 0))
        self.assertIn("hard links", stderr)
        self.assertEqual(os.listdir(self.books), [])

    def test_unreadable_or_missing_session_does_not_stop_startup(self):
        self.session.write_text("not json", encoding="utf-8")
        code, stdin, _, stderr, world = self.run_cli(["--session-file", str(self.session), "mcp"])
        self.assertEqual(code, 0)
        self.assertGreater(stdin.reads, 0)
        self.assertEqual(world.count("restore_session"), 0)

    def test_saved_domain_is_used_without_domain_flag(self):
        save_session(self.session, origin="https://saved.example/")
        _, _, _, _, world = self.run_cli(["--session-file", str(self.session), "mcp"])
        self.assertEqual(world.clients[0].domain, "https://saved.example/")
        _, _, _, _, world = self.run_cli(["mcp", "--anonymous"])
        self.assertEqual(world.clients, [])

    def print_config(self, client, *flags, globals_=()):
        out, err = io.StringIO(), io.StringIO()
        before = sorted(os.listdir(self.books))
        with patch.dict(os.environ, {"ZLIB_STATE_DIR": str(self.root / "state"), "XDG_STATE_HOME": "/xdg"}):
            code = run_mcp(parser().parse_args([*globals_, "mcp", *flags, "--print-config", client]),
                           client_factory=World().factory, stdin=NoRead(), stdout=out, stderr=err)
        self.assertEqual(code, 0)
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertEqual(sorted(os.listdir(self.books)), before)
        return out.getvalue()

    def test_c2_print_config_for_each_client(self):
        flags = ("--download-dir", str(self.books), "--allow-account-changes")
        code_entry = json.loads(self.print_config("claude-code", *flags))
        self.assertEqual(code_entry["type"], "stdio")
        self.assertTrue(Path(code_entry["command"]).is_absolute())
        self.assertEqual(code_entry["args"][:2], ["-m", "zlib_connector"])
        args = code_entry["args"]
        self.assertEqual(args[args.index("--timeout") + 1], "120")
        self.assertLess(args.index("--timeout"), args.index("mcp"))
        self.assertEqual(args[args.index("--download-dir") + 1], str(self.books.resolve()))
        self.assertIn("--allow-account-changes", args)
        self.assertEqual(set(code_entry["env"]), {"ZLIB_STATE_DIR", "XDG_STATE_HOME"})
        desktop = json.loads(self.print_config("claude-desktop", *flags, globals_=("--timeout", "200")))
        entry = desktop["mcpServers"]["zlib"]
        self.assertEqual(entry["args"][entry["args"].index("--timeout") + 1], "200")
        codex = tomllib.loads(self.print_config("codex", *flags))["mcp_servers"]["zlib"]
        self.assertEqual(codex["args"], args)
        self.assertEqual(codex["default_tools_approval_mode"], "writes")
        self.assertGreaterEqual(codex["tool_timeout_sec"], 135)
        self.assertEqual(codex["env"]["ZLIB_STATE_DIR"], str(self.root / "state"))
        for name in ("download_book", "resume_downloads", "delete_booklist", "add_favorite", "remove_from_booklist"):
            self.assertEqual(codex["tools"][name]["approval_mode"], "prompt")

    def test_c2_print_config_reflects_no_download_confirmation(self):
        text = self.print_config("codex", "--download-dir", str(self.books), "--no-download-confirmation")
        codex = tomllib.loads(text)["mcp_servers"]["zlib"]
        self.assertIn("--no-download-confirmation", codex["args"])
        self.assertNotIn("tools", codex)
        self.assertIn("--no-download-confirmation", text.split("args", 1)[1].split("\n", 1)[0])
        self.assertIn("# --no-download-confirmation", text)
        read_only = tomllib.loads(self.print_config("codex"))["mcp_servers"]["zlib"]
        self.assertNotIn("tools", read_only)
        self.assertEqual(read_only["args"][-1], "mcp")
        self.assertEqual(read_only["args"][read_only["args"].index("--timeout") + 1], "30")

    def test_c2_print_config_keeps_the_proxy_from_the_environment(self):
        with patch.dict(os.environ, {"ZLIB_PROXY": "http://127.0.0.1:3128"}):
            codex = tomllib.loads(self.print_config("codex"))["mcp_servers"]["zlib"]
        self.assertEqual(codex["env"]["ZLIB_PROXY"], "http://127.0.0.1:3128")
        self.assertNotIn("--proxy", codex["args"])

    def test_c3_help_lists_flags_and_says_actions_are_off(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            parser().parse_args(["mcp", "--help"])
        text = " ".join(out.getvalue().split())
        for flag in ("--anonymous", "--download-dir", "--no-download-confirmation", "--allow-account-changes",
                     "--print-config"):
            self.assertIn(flag, text)
        self.assertIn("Actions are off by default", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            parser().parse_args(["--help"])
        text = " ".join(out.getvalue().split()).replace("read- only", "read-only")
        self.assertIn("serve MCP tools over stdio for AI assistants; read-only unless enabled", text)

    def test_main_dispatches_mcp_refusals(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["mcp", "--no-download-confirmation"]), 1)
        self.assertIn("--no-download-confirmation requires --download-dir", err.getvalue())


if __name__ == "__main__":
    unittest.main()
