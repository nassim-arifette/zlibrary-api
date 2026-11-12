"""Global options work before or after the command; conflicts are refused at any position."""
import shlex
import unittest
from pathlib import Path

from zlib_connector.cli_output import UsageError
from zlib_connector.cli_parser import GLOBAL_DEFAULTS, parser


# Section 11.1 of docs/design/cli-complete.md: lines that must keep parsing (fake values).
EXAMPLES = r'''
search "dune" --format epub --language english --limit 10
search "9780441013593" --anonymous --json --with-pagination
info 1234567:a1b2c3 --json
formats 1234567:a1b2c3
download "dune frank herbert" --format epub --pick 1 --output .
download 1234567:a1b2c3 --output . --json
download -a reading-list.txt --dry-run
download -a reading-list.txt --pick 1 --yes
profile --json
quota
--timeout 5 doctor --json
doctor https://z-lib.by/ https://z-lib.sk/ --workers 2
domains --json
library --limit 3
favorites --order title --limit 50
favorites add 1234567:a1b2c3 --yes
favorites remove 1234567:a1b2c3
history --page 2 --json
booklists
booklists show 98765 --page 2
booklists add 98765 1234567:a1b2c3
booklists remove 98765 4242
booklists create "To read" --description "Next month" --private
booklists edit 98765 --title "Read soon" --public
booklists delete 98765
tags add 1234567:a1b2c3 "science fiction"
tags saved-ids --json
similar 1234567:a1b2c3 --limit 5
popular --anonymous --json
recommended --limit 10
--domain https://z-lib.by/ login
logout --json
status --verify --json
jobs add 1234567:a1b2c3 --key reading-2026-11-11-01
jobs list --limit 20
jobs watch 0123456789abcdef0123456789abcdef --duration 300 --json
serve --download-dir .
tui --view booklists
'''.strip().splitlines()


def parse(line):
    args, extras = parser().parse_known_args(shlex.split(line))
    return vars(args), extras


class ExampleTests(unittest.TestCase):
    def test_the_38_example_lines_still_parse(self):
        self.assertEqual(38, len(EXAMPLES))
        for line in EXAMPLES:
            with self.subTest(line=line):
                _, extras = parse(line)
                self.assertEqual([], extras)


class PositionTests(unittest.TestCase):
    def test_every_global_option_works_before_and_after_the_command_and_after_an_action(self):
        options = ["--domain", "https://books.example/", "--credentials", "c.toml", "--timeout", "7",
                   "--session-file", "s.json", "--json", "--interactive"]
        expected = {"domain": "https://books.example/", "credentials": Path("c.toml"), "timeout": 7.0,
                    "session_file": Path("s.json"), "json": True, "interactive": True, "no_input": False,
                    "no_session": False}
        for argv in ([*options, "favorites", "add", "1:a"], ["favorites", *options, "add", "1:a"],
                     ["favorites", "add", "1:a", *options], [*options, "domains"], ["domains", *options],
                     ["jobs", "list", *options], ["tui", *options], ["serve", *options], ["mcp", *options],
                     ["booklists", "show", "42", *options], ["tags", "saved-ids", *options]):
            with self.subTest(argv=argv):
                values, extras = parse(shlex.join(argv))
                self.assertEqual([], extras)
                self.assertEqual(expected, {name: values[name] for name in expected})
        for argv in (["--no-input", "--no-session", "domains"], ["domains", "--no-input", "--no-session"],
                     ["favorites", "add", "1:a", "--no-input", "--no-session"]):
            with self.subTest(argv=argv):
                values, _ = parse(shlex.join(argv))
                self.assertEqual((True, True), (values["no_input"], values["no_session"]))

    def test_the_same_namespace_in_either_order(self):
        self.assertEqual(parse("--json favorites add X --yes"), parse("favorites add X --yes --json"))
        self.assertEqual(parse("--timeout 5 doctor"), parse("doctor --timeout 5"))

    def test_defaults_are_filled_once(self):
        values, _ = parse("domains")
        self.assertEqual(GLOBAL_DEFAULTS, {name: values[name] for name in GLOBAL_DEFAULTS})

    def test_the_last_occurrence_wins(self):
        values, _ = parse("--timeout 5 domains --timeout 9")
        self.assertEqual(9.0, values["timeout"])

    def test_download_interactive_still_selects_the_list_flow(self):
        from zlib_connector.cli_batch import is_batch
        for line in ("download dune --interactive", "--interactive download dune"):
            with self.subTest(line=line):
                args = parser().parse_args(shlex.split(line))
                self.assertTrue(is_batch(args))
        self.assertFalse(is_batch(parser().parse_args(["download", "dune"])))

    def test_conflicts_are_refused_at_any_level(self):
        for line in ("--no-input domains --interactive", "domains --no-input --interactive",
                     "--interactive favorites add X --no-input", "--session-file s.json status --no-session",
                     "status --session-file s.json --no-session"):
            with self.subTest(line=line), self.assertRaises(UsageError) as caught:
                parse(line)
            self.assertIn("not allowed with argument", str(caught.exception))

    def test_usage_errors_raise_instead_of_exiting(self):
        for line in ("domains --bogus-option-x", "", "tui --view nowhere", "--timeout x domains"):
            with self.subTest(line=line), self.assertRaises(UsageError):
                parser().parse_args(shlex.split(line))


if __name__ == "__main__":
    unittest.main()
