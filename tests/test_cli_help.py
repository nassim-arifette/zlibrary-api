import argparse
import contextlib
import io
import json
import shlex
import tokenize
from pathlib import Path
import unittest

from zlib_connector.cli import main
from zlib_connector.cli_examples import COMMANDS, GROUPS, HIDDEN
from zlib_connector.cli_output import UsageError
from zlib_connector.cli_parser import parser
from zlib_connector.cli_table import print_books, render
from zlib_connector.client import Book


def _parsers(command, path=()):
    yield path, command
    for action in command._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield from _parsers(child, path + (name,))


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class HelpTests(unittest.TestCase):
    def test_every_command_has_a_group_or_is_hidden_and_examples_parse(self):
        root = parser()
        commands = next(action for action in root._actions if isinstance(action, argparse._SubParsersAction))
        grouped = {name for _, names in GROUPS for name in names}
        for name in commands.choices:
            with self.subTest(name=name):
                self.assertIn(name, COMMANDS)
                self.assertTrue(name in grouped or name in HIDDEN)
                self.assertTrue(2 <= len(COMMANDS[name][2]) <= 4 or name in HIDDEN)
                for example in COMMANDS[name][2]:
                    self.assertEqual([], parser().parse_known_args(shlex.split(example))[1], example)

    def test_every_option_has_help_and_all_help_is_ascii(self):
        for path, command in _parsers(parser()):
            with self.subTest(path=path):
                for action in command._actions:
                    if action.option_strings:
                        self.assertTrue(action.help, action.option_strings)
                text = command.format_help()
                self.assertTrue(text.isascii(), [char for char in text if not char.isascii()])

    def test_text_the_cli_writes_itself_is_ascii(self):
        package = Path(__file__).resolve().parents[1] / "zlib_connector"
        for path in sorted(package.rglob("*.py")):
            if "tui" in path.relative_to(package).parts or path.name == "cli_tui.py":
                continue
            with path.open("rb") as source:
                for token in tokenize.tokenize(source.readline):
                    if token.type == tokenize.STRING:
                        self.assertTrue(token.string.isascii(), f"{path.name}:{token.start[0]}")

    def test_root_help_groups_commands_and_hides_deprecated_ones(self):
        text = parser().format_help()
        self.assertIn("Find:\n  search", text)
        self.assertIn("exit codes:", text)
        self.assertIn("Scripts should use --json", text)
        for name in HIDDEN:
            self.assertNotIn(f"  {name} ", text)

    def test_command_help_shows_examples(self):
        text = parser()._subparsers._group_actions[0].choices["search"].format_help()
        self.assertIn("Examples:\n  zlib-cli search", text)

    def test_mistyped_command_suggests_the_closest_one(self):
        with self.assertRaises(UsageError) as raised:
            parser().parse_args(["serch", "dune"])
        self.assertEqual("unknown command. Did you mean: search?", str(raised.exception))

    def test_no_command_prints_grouped_help_to_stderr_and_exits_2(self):
        code, out, err = _run([])
        self.assertEqual((2, ""), (code, out))
        self.assertIn("Find:", err)
        code, out, err = _run(["--json"])
        self.assertEqual(2, code)
        self.assertEqual("no command given", json.loads(err)["message"])


class TableTests(unittest.TestCase):
    def test_references_are_never_truncated_and_titles_are(self):
        book = Book("A very long synthetic title that cannot fit in a narrow terminal", "Some Author", "epub", "",
                    year="2001", language="english", size="1 MB", book_id="1234567", book_hash="abcdef")
        lines = render(("TITLE", "REFERENCE"), [(book.title, "1234567:abcdef")], flexible=(0,), width=30)
        self.assertTrue(lines[1].endswith("1234567:abcdef"))
        self.assertIn("...", lines[1])
        out = io.StringIO()
        print_books([book], file=out)
        header, row = out.getvalue().splitlines()
        self.assertEqual("#", header.split()[0])
        self.assertTrue(row.rstrip().endswith("1234567:abcdef"))

    def test_empty_list_and_control_characters(self):
        out = io.StringIO()
        print_books([], file=out)
        self.assertEqual("No books found.\n", out.getvalue())
        self.assertEqual(["A", "x y"], render(("A",), [("x\x1b[31m\ny",)]))


if __name__ == "__main__":
    unittest.main()
