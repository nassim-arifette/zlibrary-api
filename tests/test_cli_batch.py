"""Batch download CLI flows with a synthetic client, patched prompts and terminals."""
import io
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import Mock, patch

from zlib_connector import (BatchEntry, BatchItem, BatchReport, DownloadError, NetworkError, RateLimitError,
                            TransferCancelled)
from zlib_connector.batch import report_lock, write_report
from zlib_connector.cli import main
from zlib_connector.cli_batch import _tty, cooperative_interrupts, entry_line, guidance
from zlib_connector.credentials import Credentials
from zlib_connector.transfer import CancellationToken
from batch_fixture import ORIGIN, SECRET, BatchClient, book, staged
from session_fixture import isolated_sessions


CREDENTIALS = Credentials(ORIGIN, "synthetic@example.com", "synthetic-password")


def terminal(stream):
    stream.isatty = lambda: True
    return stream


@isolated_sessions
class BatchCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name).resolve()
        pause = patch("zlib_connector.cli_batch.PLANNING_PAUSE", 0)
        pause.start()
        self.addCleanup(pause.stop)

    def write(self, name, text):
        path = self.folder / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_cli(self, args, client=None, *, answers=None, tty=False, stdin=None, stdout=None,
                credentials=CREDENTIALS, output=True):
        client = client if client is not None else BatchClient()
        constructor = Mock(return_value=client)
        prompt = Mock(side_effect=AssertionError("Unexpected prompt") if answers is None else list(answers))
        loader = Mock(return_value=credentials)
        input_stream = stdin if stdin is not None else io.StringIO()
        output_stream, error_stream = stdout if stdout is not None else io.StringIO(), io.StringIO()
        if tty:
            terminal(input_stream)
            terminal(error_stream)
        options = ["--output", str(self.folder)] if output else []
        with patch("zlib_connector.cli.Client", constructor), patch("zlib_connector.cli.load_credentials", loader), \
                patch("zlib_connector.cli.prompt", prompt), \
                patch("zlib_connector.cli.getpass.getpass", side_effect=AssertionError("Password was prompted")), \
                patch("sys.stdin", input_stream), patch("sys.stdout", output_stream), patch("sys.stderr", error_stream):
            code = main([*args, *options])
        text = output_stream.getvalue() if isinstance(output_stream, io.StringIO) else ""
        errors = error_stream.getvalue()
        for secret in (SECRET, CREDENTIALS.email, CREDENTIALS.password):
            self.assertNotIn(secret, text + errors)
        return code, text, errors, client, constructor, prompt, loader

    def test_a_single_title_keeps_the_single_book_flow(self):
        client = BatchClient({"dune": [book(1)]})
        code, out, _, client, _, _, _ = self.run_cli(["download", "Dune", "--pick", "1", "--json"], client)
        self.assertEqual(code, 0)
        self.assertEqual(set(json.loads(out)), {"path"})
        self.assertEqual([call[0] for call in client.calls], ["search", "download"])

    def test_list_options_are_checked_before_credentials_or_requests(self):
        listed = self.write("list.txt", "Dune\n")
        empty = self.write("empty.txt", "# nothing\n\n")
        cases = (["download", "A", "B", "--page", "2"], ["download", "A", "B", "--pick", "6"],
                 ["download", "A", "B", "--limit", "51"], ["download", "A", "--yes", "--dry-run"],
                 ["download", "-a", str(listed), "Dune"], ["download"], ["download", "-a", "-", "--interactive"],
                 ["download", "A", "B", "--max-downloads", "0"], ["download", "A", "--max-downloads", "0"],
                 ["download", "-a", str(self.folder / "missing.txt")],
                 ["download", "A", "--batch-format", "csv"], ["download", "A", "B", "--year-from", "2010", "--year-to", "2000"],
                 ["download", "A", "B", "--pick", "1"], ["download", "A", "B", "--yes"],
                 ["download", "-a", str(empty), "--yes", "--pick", "1"],
                 ["download", "-a", str(listed), "--report", str(self.folder / "missing" / "r.json"), "--yes", "--pick", "1"])
        for args in cases:
            with self.subTest(args=args):
                code, out, errors, _, constructor, prompt, loader = self.run_cli(args)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                constructor.assert_not_called()
                loader.assert_not_called()
                prompt.assert_not_called()
        code, _, errors, *_ = self.run_cli(["download", "A", "B", "--yes"])
        self.assertIn("--pick", errors)
        code, _, errors, *_ = self.run_cli(["download", "A", "B", "--pick", "1"])
        self.assertIn("--yes", errors)
        code, _, errors, *_ = self.run_cli(["download", "-a", str(empty), "--yes", "--pick", "1"])
        self.assertIn("contains no items", errors)
        code, _, _, _, constructor, _, _ = self.run_cli(["download", "A", "B", "--output", str(self.folder / "missing")],
                                                        output=False)
        self.assertEqual(code, 2)
        for args in (["download", "--format", "mobi", "A"], ["download", "A", "--formt", "x"]):
            with self.subTest(args=args), patch("sys.stderr", io.StringIO()):
                self.assertEqual(main(args), 2)  # Usage errors are reported, not raised.

    def test_titles_may_follow_options_and_lines_report_each_item(self):
        client = BatchClient({"dune": [book(1, title="Dune")], "neuromancer": [book(2, title="Neuromancer")]})
        code, out, errors, client, *_ = self.run_cli(["download", "Dune", "--pick", "1", "Neuromancer", "--yes",
                                                      "--no-report"], client)
        self.assertEqual(code, 0)
        self.assertEqual(client.kinds("search"), ["Dune", "Neuromancer"])
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith('[1/2] downloaded  argument 1 "Dune" -> Dune | Author | 2001 | EPUB | 1 MB | Dune-'))
        self.assertTrue(lines[1].startswith('[2/2] downloaded  argument 2 "Neuromancer"'))
        self.assertEqual(lines[2], "2 downloaded.")
        self.assertTrue(out.isascii())

    def test_a_list_file_keeps_a_default_report_and_a_rerun_downloads_nothing_twice(self):
        listed = self.write("reading-list.txt", "Dune\nNeuromancer\nMissing\n123:\n")
        results = {"dune": [book(1)], "neuromancer": [book(2)]}
        code, out, _, client, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"],
                                                BatchClient(results))
        self.assertEqual(code, 4)
        self.assertIn("2 downloaded, 1 not found, 1 error.", out)
        self.assertIn("Not found: line 3.", out)
        self.assertIn("Errors: line 4.", out)
        report = self.folder / "reading-list.progress.json"
        self.assertTrue(report.is_file())
        self.assertTrue((self.folder / "reading-list.progress.json.lock").exists())
        code, out, _, again, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"], BatchClient(results))
        self.assertEqual(code, 4)
        self.assertIn("2 already downloaded", out)
        self.assertEqual(again.kinds("download"), [])
        self.assertEqual(again.kinds("search"), ["Missing"])
        other = self.folder / "other"
        other.mkdir()
        code, out, errors, _, constructor, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes",
                                                              "--output", str(other)], BatchClient(results),
                                                             output=False)
        self.assertEqual(code, 2)
        self.assertIn(f"The report recorded its books in {self.folder}, and none of them is in {other}.", errors)
        constructor.assert_not_called()
        code, out, _, fresh, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes", "--no-report"],
                                               BatchClient(results))
        self.assertEqual(fresh.kinds("download"), ["1:h1", "2:h2"])
        code, _, errors, *_ = self.run_cli(["download", "-a", str(report), "--yes"], BatchClient(results))
        self.assertEqual(code, 2)
        self.assertIn("1 item needs a search result choice", errors)
        code, out, _, reused, *_ = self.run_cli(["download", "-a", str(report), "--yes", "--pick", "1"],
                                                BatchClient(results))
        self.assertIn("2 already downloaded", out)
        self.assertEqual(reused.kinds("search"), ["Missing"])
        self.assertEqual(reused.kinds("download"), [])
        self.assertFalse((self.folder / "reading-list.progress.progress.json").exists())

    def test_exit_codes_for_waiting_complete_and_cancelled_runs(self):
        listed = self.write("list.txt", "Dune\nNeuromancer\n")
        results = {"dune": [book(1)], "neuromancer": [book(2)]}
        code, out, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"],
                                     BatchClient(results, remaining=0))
        self.assertEqual(code, 3)
        self.assertIn("2 waiting for quota.", out)
        self.assertIn("Stopped: no downloads remain", out)
        self.assertIn("Run the same command again to continue; progress is saved in", out)
        code, out, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"], BatchClient(results))
        self.assertEqual(code, 0)
        client = BatchClient({"x": [book(3)], "y": [book(4)]})
        client.download_failures["3:h3"] = staged(TransferCancelled(), "transfer")
        code, out, errors, *_ = self.run_cli(["download", "X", "Y", "--pick", "1", "--yes", "--json", "--no-report"], client)
        self.assertEqual(code, 130)
        document = json.loads(out)
        self.assertEqual(document["stopped"]["error_code"], "Cancelled")
        self.assertEqual([item["status"] for item in document["items"]], ["cancelled", "not_started"])
        self.assertIn("Cancelled.", errors)

    def test_dry_run_json_is_a_list_that_downloads_without_searching_again(self):
        listed = self.write("list.txt", "Dune\nNeuromancer\n")
        results = {"dune": [book(1), book(2)], "neuromancer": [book(3)]}
        code, out, _, client, *_ = self.run_cli(["download", "-a", str(listed), "--dry-run", "--json"], BatchClient(results))
        self.assertEqual(code, 4)
        plan = json.loads(out)
        self.assertEqual(plan["kind"], "zlib-connector-batch-plan")
        self.assertEqual([item["status"] for item in plan["items"]], ["needs_choice", "needs_choice"])
        self.assertEqual(client.kinds("download"), [])
        self.assertFalse((self.folder / "list.progress.json").exists())
        plan["items"][0]["pick"] = 2
        plan["items"][1]["reference"] = plan["items"][1]["candidates"][0]["reference"]
        edited = self.write("plan.json", json.dumps(plan))
        code, out, _, client, *_ = self.run_cli(["download", "-a", str(edited), "--report",
                                                 str(self.folder / "list.progress.json"), "--yes"], BatchClient(results))
        self.assertEqual(code, 0)
        self.assertEqual(client.kinds("search"), [])
        self.assertEqual(client.kinds("download"), ["2:h2", "3:h3"])
        code, out, _, client, *_ = self.run_cli(["download", "-a", str(listed), "--dry-run", "--pick", "1"],
                                                BatchClient(results))
        self.assertEqual(code, 0)
        self.assertIn("2 already downloaded.", out)
        self.assertEqual(client.calls, [("quota",)])

    def test_interactive_choices_reprompt_and_confirm(self):
        client = BatchClient({"dune": [book(1), book(2), book(3)]})
        code, out, errors, client, _, prompt, _ = self.run_cli(["download", "Dune", "--no-report"], client,
                                                               answers=["", "abc", "9", "2", "y"], tty=True)
        self.assertEqual(code, 0)
        self.assertEqual(client.kinds("download"), ["2:h2"])
        labels = [call.args[0] for call in prompt.call_args_list]
        self.assertEqual(labels[:4], ["Choose 1-3, s to skip, q to stop: "] * 4)
        self.assertTrue(labels[4].startswith(f"Download 1 book into {self.folder}? Quota: 10 of 10 remaining."))
        self.assertIn('[1/1] argument 1 "Dune"', errors)
        self.assertIn("Type a number from 1 to 3, s or q.", errors)
        self.assertIn("Quota: 10 of 10 downloads remaining", errors)

    def test_interactive_q_stops_choosing_and_a_decline_downloads_nothing(self):
        listed = self.write("list.txt", "A\nB\nC\n7:h7\n")
        results = {"a": [book(1)], "b": [book(2)], "c": [book(3)]}
        code, out, errors, client, _, prompt, _ = self.run_cli(["download", "-a", str(listed)], BatchClient(results),
                                                               answers=["1", "q", "y"], tty=True)
        self.assertEqual(code, 3)
        self.assertEqual(client.kinds("search"), ["A", "B"])
        self.assertEqual(client.kinds("download"), ["1:h1", "7:h7"])
        self.assertIn("2 downloaded, 2 not started.", out)
        report = self.folder / "list.progress.json"
        before = report.read_bytes()
        code, out, errors, client, _, prompt, _ = self.run_cli(["download", "-a", str(listed)], BatchClient(results),
                                                               answers=["s", "1", "n"], tty=True)
        self.assertEqual(code, 130)
        self.assertEqual(client.kinds("search"), ["B", "C"])
        self.assertEqual(client.kinds("download"), [])
        self.assertIn("Cancelled.", errors)
        self.assertEqual(report.read_bytes(), before)
        code, *_ = self.run_cli(["download", "-a", str(listed)], BatchClient(results), answers=[EOFError(), EOFError()],
                                tty=True)
        self.assertEqual(code, 3)

    def test_ctrl_c_while_choosing_keeps_earlier_choices_in_the_report(self):
        listed = self.write("list.txt", "A\nB\n")
        results = {"a": [book(1), book(5)], "b": [book(2)]}
        code, out, errors, *_ = self.run_cli(["download", "-a", str(listed), "--json"], BatchClient(results),
                                             answers=["2", KeyboardInterrupt()], tty=True)
        self.assertEqual(code, 130)
        self.assertEqual([item["status"] for item in json.loads(out)["items"]], ["planned", "not_started"])
        code, out, errors, client, *_ = self.run_cli(["download", "-a", str(listed)], BatchClient(results),
                                                     answers=["1", "y"], tty=True)
        self.assertEqual(code, 0)
        self.assertEqual(client.kinds("search"), ["B"])
        self.assertEqual(client.kinds("download"), ["5:h5", "2:h2"])

    def test_planning_failures_keep_choices_and_stop_before_downloads(self):
        listed = self.write("list.txt", "A\nB\nC\n")
        client = BatchClient({"a": [book(1)], "b": [book(2)], "c": [book(3)]})
        client.search_failures = [None, RateLimitError("synthetic")]
        code, out, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"], client)
        self.assertEqual(code, 3)
        self.assertEqual(client.kinds("download"), [])
        self.assertIn("Stopped: the service reported a quota or request limit.", out)
        code, out, _, again, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"],
                                               BatchClient({"a": [book(9)], "b": [book(2)], "c": [book(3)]}))
        self.assertEqual(code, 0)
        self.assertEqual(again.kinds("search"), ["B", "C"])
        self.assertEqual(again.kinds("download"), ["1:h1", "2:h2", "3:h3"])

    def test_standard_input_lists_need_a_usable_login_and_explicit_csv(self):
        stdin = io.TextIOWrapper(io.BytesIO("Dune\n".encode()), encoding="utf-8")
        code, out, errors, _, constructor, _, _ = self.run_cli(["download", "-a", "-", "--pick", "1", "--yes"],
                                                               stdin=stdin, credentials=Credentials(ORIGIN))
        self.assertEqual(code, 2)
        self.assertIn("saved session", errors)
        self.assertEqual(stdin.buffer.tell(), 0)
        constructor.assert_not_called()
        stdin = terminal(io.TextIOWrapper(io.BytesIO("Dune\r\nLes Misérables\r\n".encode("utf-8-sig")), encoding="utf-8"))
        client = BatchClient({"dune": [book(1)], "les misérables": [book(2)]})
        code, out, _, client, *_ = self.run_cli(["download", "-a", "-", "--pick", "1", "--yes"], client, stdin=stdin)
        self.assertEqual(code, 0)
        self.assertEqual(client.kinds("search"), ["Dune", "Les Misérables"])
        csv_input = io.TextIOWrapper(io.BytesIO(b"title,author\nDune,Herbert\n"), encoding="utf-8")
        code, _, errors, *_ = self.run_cli(["download", "-a", "-", "--pick", "1", "--yes"], stdin=csv_input)
        self.assertEqual(code, 2)
        self.assertIn("--batch-format csv", errors)
        csv_input = io.TextIOWrapper(io.BytesIO(b"title,author\nDune,Herbert\n"), encoding="utf-8")
        code, _, _, client, *_ = self.run_cli(["download", "-a", "-", "--batch-format", "csv", "--pick", "1", "--yes"],
                                              BatchClient({"dune herbert": [book(1)]}), stdin=csv_input)
        self.assertEqual(code, 0)

    def test_titles_without_a_report_refuse_runs_the_quota_cannot_finish(self):
        results = {"a": [book(1)], "b": [book(2)]}
        code, out, errors, client, *_ = self.run_cli(["download", "A", "B", "--pick", "1", "--yes"],
                                                     BatchClient(results, remaining=1))
        self.assertEqual(code, 2)
        self.assertIn("--report FILE", errors)
        self.assertEqual(client.kinds("download"), [])
        code, out, errors, client, *_ = self.run_cli(["download", "A", "B", "--pick", "1", "--yes", "--no-report"],
                                                     BatchClient(results, remaining=1))
        self.assertEqual(code, 3)
        self.assertEqual(client.kinds("download"), ["1:h1"])
        self.assertIn("No report was saved", out)

    def test_an_interactive_run_without_a_report_warns_when_the_quota_is_unknown(self):
        client = BatchClient({"a": [book(1)], "b": [book(2)]}, remaining=None)
        code, out, errors, client, _, prompt, _ = self.run_cli(["download", "A", "B", "--pick", "1", "--yes"], client,
                                                               answers=[], tty=True)
        self.assertEqual(code, 0)
        self.assertIn("Without --report, a later run will download these books again.", errors)
        prompt.assert_not_called()
        self.assertEqual(client.kinds("download"), ["1:h1", "2:h2"])

    def test_reports_are_exclusive_and_never_replace_the_list(self):
        listed = self.write("list.txt", "Dune\n")
        with report_lock(self.folder / "list.progress.json"):
            code, _, errors, _, constructor, _, _ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("Another batch is using this report", errors)
        constructor.assert_not_called()
        blocked = self.folder / "list.progress.json.lock"
        blocked.unlink()
        blocked.mkdir()
        code, _, errors, _, constructor, _, _ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn(f"Cannot lock {blocked}. Choose a --report file in a local folder, or add --no-report.", errors)
        constructor.assert_not_called()
        code, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes", "--no-report"],
                                BatchClient({"dune": [book(1)]}))
        self.assertEqual(code, 0)
        plain = self.write("list.json", '["Dune"]')
        code, _, errors, *_ = self.run_cli(["download", "-a", str(plain), "--report", str(plain), "--pick", "1", "--yes"])
        self.assertEqual(code, 2)
        self.assertEqual(plain.read_text(encoding="utf-8"), '["Dune"]')
        broken = self.write("broken.json", "{}")
        code, _, errors, _, constructor, _, _ = self.run_cli(["download", "-a", str(listed), "--report", str(broken),
                                                              "--pick", "1", "--yes"])
        self.assertEqual(code, 2)
        self.assertEqual(broken.read_text(encoding="utf-8"), "{}")
        constructor.assert_not_called()

    def test_output_survives_a_legacy_code_page_and_json_stays_ascii(self):
        title = "Война и мир"
        client = BatchClient({"war": [book(1, title=title)], "peace": [book(2, title="Paix")]})
        raw = io.BytesIO()
        stdout = io.TextIOWrapper(raw, encoding="cp1252")
        code, *_ = self.run_cli(["download", "War", "Peace", "--pick", "1", "--yes", "--no-report"], client, stdout=stdout)
        stdout.flush()
        self.assertEqual(code, 0)
        self.assertIn(b"\\u0412", raw.getvalue())
        code, out, *_ = self.run_cli(["download", "War", "--pick", "1", "--yes", "--json", "--no-report"],
                                     BatchClient({"war": [book(3, title=title)]}))
        self.assertEqual(code, 0)
        self.assertTrue(out.isascii())
        self.assertEqual(json.loads(out)["items"][0]["book"]["title"], title)

    def test_uncertain_downloads_are_reported_and_need_an_explicit_retry(self):
        listed = self.write("list.txt", "A\nB\n")
        client = BatchClient({"a": [book(1)], "b": [book(2)]})
        client.download_failures["1:h1"] = staged(NetworkError(f"synthetic {SECRET}"), "transfer")
        code, out, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"], client)
        self.assertEqual(code, 4)
        self.assertIn("May have used quota: line 1.", out)
        code, out, _, again, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"],
                                               BatchClient({"a": [book(1)], "b": [book(2)]}))
        self.assertEqual(again.kinds("download"), ["2:h2"])
        self.assertEqual(code, 4)
        code, out, _, retry, *_ = self.run_cli(["download", "-a", str(listed), "--yes", "--retry-uncertain"],
                                               BatchClient())
        self.assertEqual(code, 0)
        self.assertEqual(retry.kinds("download"), ["1:h1"])

    def test_a_retry_run_does_not_advise_repeating_its_own_uncertain_requests(self):
        listed = self.write("list.txt", "Old\nA\nB\n")
        results = {"old": [book(9)], "a": [book(1)], "b": [book(2)]}
        client = BatchClient(results)
        client.download_failures["9:h9"] = staged(NetworkError("synthetic"), "transfer")
        self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"], client)
        client = BatchClient(results)
        client.download_failures["1:h1"] = staged(NetworkError("synthetic"), "transfer")
        code, out, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes", "--retry-uncertain"],
                                     client)
        self.assertEqual(code, 4)
        self.assertEqual(client.kinds("download"), ["9:h9", "1:h1"])
        self.assertIn("Run the command again without --retry-uncertain to continue", out)
        self.assertNotIn("Run the same command again", out)
        self.assertIn("--retry-uncertain would request it again", out)
        self.assertNotIn("not retried automatically", out)
        code, out, _, again, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"],
                                               BatchClient(results))
        self.assertEqual(again.kinds("download"), ["2:h2"])
        code, out, errors, again, _, prompt, _ = self.run_cli(["download", "-a", str(listed), "--retry-uncertain"],
                                                              BatchClient(results), answers=["n"], tty=True)
        self.assertEqual(code, 130)
        self.assertIn("Trying again downloads that may have used quota: line 2.", errors)
        self.assertEqual(again.kinds("download"), [])
        self.write("list.txt", "Old\nA\nB\nNew\n")
        code, out, *_ = self.run_cli(["download", "-a", str(listed), "--retry-uncertain", "--json"],
                                     BatchClient({**results, "new": [book(4)]}), answers=[KeyboardInterrupt()], tty=True)
        self.assertEqual(code, 130)
        self.assertEqual([(item["status"], item["quota_may_be_used"]) for item in json.loads(out)["items"]][1],
                         ("interrupted", True))  # Ctrl+C while choosing: the retry did not happen, the doubt stays.
        code, out, _, again, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"],
                                               BatchClient({**results, "new": [book(4)]}))
        self.assertEqual(again.kinds("download"), ["4:h4"])

    def test_the_credentials_file_is_never_read_as_a_list(self):
        content = 'domain = "https://books.example/"\nemail = "reader@example.com"\npassword = "other-secret"\n'
        default = self.write("credentials.local.toml", content)
        other = self.write("mine.toml", content)
        backup = self.write("credentials.local.toml.bak", content)
        for args, reason in ((["download", "-a", str(default), "--pick", "1", "--yes"], "the credentials file, not"),
                             (["--credentials", str(other), "download", "-a", str(other), "--pick", "1", "--yes"],
                              "the credentials file, not"),
                             (["download", "-a", str(backup), "--pick", "1", "--yes"], "a credentials setting")):
            with self.subTest(args=args[:2]), patch("zlib_connector.cli_batch.DEFAULT_FILE", default):
                code, out, errors, _, constructor, prompt, loader = self.run_cli(args)
                self.assertEqual(code, 2)
                self.assertIn(reason, errors)
                self.assertNotIn("other-secret", out + errors)
                self.assertNotIn("reader@example.com", out + errors)
                constructor.assert_not_called()
                loader.assert_not_called()
                prompt.assert_not_called()
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()),
                         ["credentials.local.toml", "credentials.local.toml.bak", "mine.toml"])

    def test_a_dry_run_says_that_nothing_was_saved(self):
        listed = self.write("list.txt", "Dune\nNeuromancer\n")
        results = {"dune": [book(1)], "neuromancer": [book(2)]}
        report = self.folder / "list.progress.json"
        mine = self.folder / "mine.json"
        for args, target in ((["-a", str(listed)], f"--report {report}"), (["Dune", "Neuromancer", "--report", str(mine)],
                                                                            f"--report {mine}"),
                             (["Dune", "Neuromancer"], "--report FILE or --no-report")):
            with self.subTest(args=args):
                code, out, *_ = self.run_cli(["download", *args, "--dry-run", "--pick", "1"], BatchClient(results))
                self.assertEqual(code, 0)
                self.assertNotIn("progress is saved", out)
                self.assertNotIn("finished books", out)
                self.assertIn("Dry run: nothing was downloaded and no report was written.", out)
                self.assertIn(f"then download -a plan.json {target}.", out)
        self.assertFalse(report.exists() or mine.exists())
        code, out, errors, *_ = self.run_cli(["download", "-a", str(listed), "--dry-run"], BatchClient(results),
                                             answers=["1", "1"], tty=True)
        self.assertIn("Dry run: choices are not saved; add --json > plan.json to keep them.", errors)
        self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"], BatchClient(results))
        before = report.read_bytes()
        report.with_name(report.name + ".lock").unlink()
        code, out, *_ = self.run_cli(["download", "-a", str(listed), "--dry-run", "--pick", "1"], BatchClient(results))
        self.assertEqual(report.read_bytes(), before)
        self.assertFalse(report.with_name(report.name + ".lock").exists())
        self.assertIn(f"Dry run: nothing was downloaded and {report} was not changed.", out)

    def test_report_write_failures_list_only_unrecorded_books(self):
        listed = self.write("list.txt", "A\nB\nC\n")
        results = {"a": [book(1)], "b": [book(2)], "c": [book(3)]}
        report = self.folder / "list.progress.json"
        real = write_report
        # Writes: before the batch, before and after each book, then at the end.
        for failing, unrecorded, pending in (({8}, [], False), ({5, 6}, ["Book 2-000000000002.epub"], True)):
            calls = []

            def flaky(batch, path, failing=failing):
                calls.append(path)
                if len(calls) in failing:
                    raise OSError("synthetic")
                real(batch, path)

            report.unlink(missing_ok=True)
            with self.subTest(failing=failing), patch("zlib_connector.batch.write_report", side_effect=flaky):
                code, out, errors, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"],
                                                     BatchClient(results))
                self.assertEqual(code, 4)
                self.assertIn(f"The report could not be updated: {report}.", errors)
                noted = [line.split(": ", 1)[1] for line in errors.splitlines()
                         if line.startswith("Downloaded but not recorded")]
                self.assertEqual([Path(line).name for line in noted], unrecorded)
                self.assertNotIn("No report was saved", out)
                self.assertEqual("keeps the progress of its last update" in out, pending)
        listed = self.write("list.txt", "A\n")
        with patch("zlib_connector.cli_batch.write_report", side_effect=OSError("synthetic")):
            code, out, errors, client, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"],
                                                         BatchClient(results))
        self.assertEqual(code, 4)
        self.assertIn("The report could not be updated", errors)
        self.assertEqual(client.kinds("download"), [])

    def test_an_interrupted_download_in_the_report_needs_attention(self):
        listed = self.write("list.txt", "A\nB\n")
        results = {"a": [book(1)], "b": [book(2)]}
        self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"], BatchClient(results, remaining=1))
        report = self.folder / "list.progress.json"
        record = json.loads(report.read_text())
        record["items"][1]["status"] = "downloading"  # A hard kill during the second book.
        report.write_text(json.dumps(record))
        code, out, _, client, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"],
                                                BatchClient(results))
        self.assertEqual(code, 4)
        self.assertTrue(out.splitlines()[1].startswith('[2/2] interrupted  line 2 "B"'))
        self.assertEqual(client.kinds("download"), [])

    def test_standard_input_is_not_read_when_yes_is_missing(self):
        stdin = terminal(io.TextIOWrapper(io.BytesIO(b"Dune\n"), encoding="utf-8"))
        code, out, errors, _, constructor, _, loader = self.run_cli(["download", "-a", "-", "--pick", "1"],
                                                                    stdin=stdin, tty=True)
        self.assertEqual(code, 2)
        self.assertIn("--yes", errors)
        self.assertEqual(stdin.buffer.tell(), 0)
        loader.assert_not_called()
        constructor.assert_not_called()
        listed = self.write("list.txt", "Dune\n")
        code, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1"])
        self.assertEqual(code, 2)
        self.assertFalse((self.folder / "list.progress.json.lock").exists())

    def test_lookups_are_paced_when_a_terminal_run_uses_pick(self):
        listed = self.write("list.txt", "A\nB\nC\n")
        client = BatchClient({"a": [book(1)], "b": [book(2)], "c": [book(3)]})
        with patch("zlib_connector.cli_batch.PLANNING_PAUSE", 0.5), patch("zlib_connector.batch.time.sleep") as sleep:
            code, out, errors, client, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"],
                                                         client, tty=True)
        self.assertEqual(code, 0)
        pauses = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(len(pauses), 2)
        self.assertTrue(all(0 < pause <= 0.5 for pause in pauses))
        self.assertIn("Checked 3 of 3", errors)

    def test_printed_paths_are_terminal_safe(self):
        entry = BatchEntry(1, BatchItem("line 1", query="Dune"), "downloaded", book=book(1),
                           path=self.folder / "Dune\x1b[31m-1.epub")
        line = entry_line(entry, 1, self.folder)
        self.assertNotIn("\x1b", line)
        self.assertTrue(line.endswith("| Dune-1.epub"))
        outside = entry_line(entry, 1, self.folder / "other")
        self.assertNotIn("\x1b", outside)
        pending = BatchReport([BatchEntry(1, BatchItem("line 1", query="Dune"))])
        for report in (pending, BatchReport([BatchEntry(1, BatchItem("line 1", query="Dune"))], saved=False)):
            lines = guidance(report, report_path=self.folder / "list\x1b]0;title\x07.progress.json")
            self.assertTrue(lines)
            self.assertTrue(all("\x1b" not in text and "\x07" not in text for text in lines))

    def test_interactive_override_max_downloads_and_leftover_notice(self):
        (self.folder / ".zlib-old.part").write_bytes(b"partial")
        client = BatchClient({"a": [book(1)], "b": [book(2)]})
        code, out, errors, client, _, prompt, _ = self.run_cli(
            ["download", "A", "B", "--interactive", "--max-downloads", "1", "--no-report"], client,
            answers=["1", "1", "y"])
        self.assertEqual(code, 3)
        self.assertEqual(prompt.call_count, 3)
        self.assertEqual(client.kinds("download"), ["1:h1"])
        self.assertIn("Stopped at the --max-downloads limit.", out)
        self.assertIn("Quota: 9 of 10 downloads remaining.", out)
        self.assertIn("1 temporary .zlib-*.part file", errors)
        self.assertTrue((self.folder / ".zlib-old.part").exists())

    def test_one_ctrl_c_finishes_the_current_book_and_a_second_one_cancels_it(self):
        listed = self.write("list.txt", "A\nB\n")
        original = signal.getsignal(signal.SIGINT)
        for presses, first in ((1, ("downloaded", False)), (2, ("cancelled", True))):
            client = BatchClient({"a": [book(1)], "b": [book(2)]})

            def press(reference, presses=presses):
                for _ in range(presses):
                    signal.raise_signal(signal.SIGINT)  # Delivered to the handler while the book transfers.

            client.during_download = press
            (self.folder / "list.progress.json").unlink(missing_ok=True)
            with self.subTest(presses=presses), patch("zlib_connector.cli_batch.os.write") as write:
                code, out, errors, client, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes",
                                                              "--json"], client)
                self.assertEqual(code, 130)
                items = json.loads(out)["items"]
                self.assertEqual([(item["status"], item["quota_may_be_used"]) for item in items],
                                 [first, ("not_started", False)])
                self.assertEqual(client.kinds("download"), ["1:h1"])
                messages = b"".join(call.args[1] for call in write.call_args_list)
                self.assertIn(b"Stopping after the current book", messages)
                self.assertEqual(b"Cancelling the current download" in messages, presses == 2)
            self.assertIs(signal.getsignal(signal.SIGINT), original)
        stop, transfer = CancellationToken(), CancellationToken()
        with patch("zlib_connector.cli_batch.os.write"), cooperative_interrupts(stop, transfer):
            handler = signal.getsignal(signal.SIGINT)
            handler(signal.SIGINT, None)
            self.assertEqual((stop.is_cancelled, transfer.is_cancelled), (True, False))
            handler(signal.SIGINT, None)
            self.assertTrue(transfer.is_cancelled)
            with self.assertRaises(KeyboardInterrupt):
                handler(signal.SIGINT, None)
        self.assertIs(signal.getsignal(signal.SIGINT), original)

    def test_a_stopped_run_does_not_show_the_quota_read_before_its_last_download(self):
        listed = self.write("list.txt", "A\nB\nC\n")
        report = self.folder / "list.progress.json"
        for extra in ([], ["--json"]):
            client = BatchClient({"a": [book(1)], "b": [book(2)], "c": [book(3)]}, remaining=3)
            client.during_download = lambda reference: reference == "2:h2" and signal.raise_signal(signal.SIGINT)
            report.unlink(missing_ok=True)
            with self.subTest(extra=extra), patch("zlib_connector.cli_batch.os.write"):
                code, out, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes", *extra], client)
                self.assertEqual((code, client.kinds("download")), (130, ["1:h1", "2:h2"]))
                # Read before planning and before each book; no request follows the stop.
                self.assertEqual(sum(call[0] == "quota" for call in client.calls), 3)
                self.assertIsNone(json.loads(report.read_text())["quota"])
                if extra:
                    self.assertIsNone(json.loads(out)["quota"])
                else:
                    self.assertIn("2 downloaded, 1 not started.", out)
                    self.assertNotIn("Quota:", out)

    def test_a_run_without_a_report_names_the_books_left_to_download(self):
        results = {"a": [book(1)], "b": [book(2)], "c": [book(3)]}
        client = BatchClient(results)
        client.during_download = lambda reference: signal.raise_signal(signal.SIGINT)  # Ctrl+C once, during book 1.
        with patch("zlib_connector.cli_batch.os.write"):
            code, out, *_ = self.run_cli(["download", "A", "B", "C"], client, answers=["1", "1", "1", "y"], tty=True)
        self.assertEqual((code, client.kinds("download")), (130, ["1:h1"]))
        self.assertIn("1 downloaded, 2 not started.", out)
        waiting = ("No report was saved: to finish, run again with only argument 2, argument 3; the same command would"
                   " download the finished books a second time.")
        self.assertIn(waiting, out)
        code, out, _, client, *_ = self.run_cli(["download", "A", "B", "C", "--pick", "1", "--yes", "--no-report"],
                                                BatchClient(results, remaining=1))
        self.assertEqual((code, client.kinds("download")), (3, ["1:h1"]))
        self.assertIn(waiting, out)
        self.assertNotIn("unless you add --report FILE", out)
        code, out, *_ = self.run_cli(["download", "A", "B", "--pick", "1", "--yes"], BatchClient(results, remaining=0))
        self.assertEqual(code, 3)
        self.assertIn("No report was saved; run the same command again to continue.", out)

    def test_a_plan_downloads_into_the_list_report_so_the_list_knows_it(self):
        listed = self.write("list.txt", "A\nB\n")
        report = self.folder / "list.progress.json"
        results = {"a": [book(1)], "b": [book(2)], "c": [book(3)]}
        code, out, errors, *_ = self.run_cli(["download", "-a", str(listed), "--dry-run", "--json", "--pick", "1"],
                                             BatchClient(results))
        self.assertEqual(code, 0)
        self.assertIn(f"Download this plan with --report {report}", errors)
        plan = self.write("plan.json", out)
        code, _, errors, _, constructor, _, loader = self.run_cli(["download", "-a", str(plan), "--yes"],
                                                                  BatchClient(results))
        self.assertEqual(code, 2)
        self.assertIn("Downloading a plan needs --report FILE or --no-report", errors)
        constructor.assert_not_called()
        loader.assert_not_called()
        self.assertFalse((self.folder / "plan.progress.json").exists())
        client = BatchClient(results)
        client.download_failures["2:h2"] = staged(NetworkError("synthetic"), "transfer")
        code, out, *_ = self.run_cli(["download", "-a", str(plan), "--report", str(report), "--yes"], client)
        self.assertEqual(code, 4)
        self.assertEqual(client.kinds("download"), ["1:h1", "2:h2"])
        self.write("list.txt", "A\nB\nC\n")
        code, out, _, again, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"],
                                               BatchClient(results))
        self.assertEqual(code, 4)
        self.assertEqual(again.kinds("search"), ["C"])
        self.assertEqual(again.kinds("download"), ["3:h3"])
        self.assertIn("May have used quota: line 2.", out)
        code, out, _, other, *_ = self.run_cli(["download", "-a", str(plan), "--no-report", "--yes"], BatchClient(results))
        self.assertEqual(other.kinds("download"), ["1:h1", "2:h2"])

    def test_a_plan_or_report_on_standard_input_needs_a_report_choice_too(self):
        listed = self.write("list.txt", "A\nB\n")
        report = self.folder / "list.progress.json"
        results = {"a": [book(1)], "b": [book(2)], "c": [book(3)]}
        _, plan, *_ = self.run_cli(["download", "-a", str(listed), "--dry-run", "--json", "--pick", "1"],
                                   BatchClient(results))

        def piped(text):
            return io.TextIOWrapper(io.BytesIO(text.encode()), encoding="utf-8")

        code, _, errors, _, constructor, *_ = self.run_cli(["download", "-a", "-", "--yes"], stdin=piped(plan))
        self.assertEqual(code, 2)
        self.assertIn("Downloading a plan needs --report FILE or --no-report", errors)
        constructor.assert_not_called()
        code, _, _, client, *_ = self.run_cli(["download", "-a", "-", "--yes", "--report", str(report)],
                                              BatchClient(results), stdin=piped(plan))
        self.assertEqual((code, client.kinds("download")), (0, ["1:h1", "2:h2"]))
        self.write("list.txt", "A\nB\nC\n")
        code, _, _, again, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"], BatchClient(results))
        self.assertEqual(again.kinds("download"), ["3:h3"])
        code, _, errors, _, constructor, *_ = self.run_cli(["download", "-a", "-", "--yes"],
                                                           stdin=piped(report.read_text(encoding="utf-8")))
        self.assertEqual(code, 2)
        self.assertIn("Downloading a report from standard input needs --report FILE or --no-report", errors)
        constructor.assert_not_called()
        code, _, _, client, *_ = self.run_cli(["download", "-a", "-", "--yes", "--no-report"], BatchClient(results),
                                              stdin=piped(plan))
        self.assertEqual((code, client.kinds("download")), (0, ["1:h1", "2:h2"]))

    def test_a_plan_saved_with_a_text_name_is_refused_before_any_request(self):
        listed = self.write("list.txt", "A\nB\n")
        _, out, *_ = self.run_cli(["download", "-a", str(listed), "--dry-run", "--json"],
                                  BatchClient({"a": [book(1), book(2)], "b": [book(3)]}))
        plan = self.write("plan.txt", out)  # Instead of plan.json, for example to edit it in Notepad.
        code, _, errors, _, constructor, prompt, loader = self.run_cli(["download", "-a", str(plan), "--pick", "1",
                                                                        "--yes"])
        self.assertEqual(code, 2)
        self.assertIn("This list holds JSON, such as a plan or a report; name it .json or use --batch-format json.",
                      errors)
        constructor.assert_not_called()
        loader.assert_not_called()
        self.assertFalse((self.folder / "plan.progress.json").exists())

    def test_a_run_from_another_folder_keeps_the_report_folder_and_never_downloads_again(self):
        listed = self.write("list.txt", "A\nB\n")
        report = self.folder / "list.progress.json"
        results = {"a": [book(1)], "b": [book(2)]}
        self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"], BatchClient(results))
        other = self.folder / "other"
        other.mkdir()
        before = report.read_bytes()
        where = f"The report recorded its books in {self.folder}, and none of them is in {other}"
        for extra in ([], ["--redownload-missing"], ["--redownload-missing", "--json"]):
            with self.subTest(extra=extra):
                code, _, errors, _, constructor, *_ = self.run_cli(
                    ["download", "-a", str(listed), "--pick", "1", "--yes", *extra, "--output", str(other)],
                    BatchClient(results, remaining=1), output=False)
                message = json.loads(errors)["message"] if "--json" in extra else errors
                self.assertEqual(code, 2)
                self.assertIn(f"{where}. Pass --output with that folder to continue this list there", message)
                constructor.assert_not_called()
                self.assertEqual(report.read_bytes(), before)
        code, out, errors, *_ = self.run_cli(["download", "-a", str(listed), "--dry-run", "--pick", "1",
                                              "--output", str(other)], BatchClient(results), output=False)
        self.assertEqual(code, 0)
        self.assertIn(f"{where}; pass --output with that folder to find them.", errors)
        self.assertIn(f"Missing files: line 1, line 2. The report recorded them in {self.folder}; pass --output", out)
        self.assertNotIn("--redownload-missing", out)
        for _ in range(2):  # A terminal run explains it every time, and the report keeps its folder.
            code, out, errors, client, *_ = self.run_cli(["download", "-a", str(listed), "--output", str(other)],
                                                         BatchClient(results), answers=[], tty=True, output=False)
            self.assertEqual(code, 0)
            self.assertIn(where, errors)
            self.assertNotIn("--redownload-missing", out)
            self.assertEqual(client.kinds("download"), [])
            self.assertEqual(json.loads(report.read_text())["folder"], str(self.folder))
        code, out, errors, client, _, prompt, _ = self.run_cli(
            ["download", "-a", str(listed), "--redownload-missing", "--output", str(other)], BatchClient(results),
            answers=["n"], tty=True, output=False)
        self.assertEqual(code, 130)
        self.assertEqual(errors.count(where), 2)  # Once before planning, once before the confirmation.
        self.assertTrue(prompt.call_args.args[0].startswith(f"Download 2 books into {other}?"))
        self.assertEqual(client.kinds("download"), [])
        for path in self.folder.glob("*.epub"):  # Once the books are there, that folder is the report's.
            path.rename(other / path.name)
        code, out, errors, client, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes",
                                                      "--output", str(other)], BatchClient(results), output=False)
        self.assertEqual(code, 0)
        self.assertIn("2 already downloaded.", out)
        self.assertNotIn("recorded its books", errors)
        self.assertEqual(json.loads(report.read_text())["folder"], str(other))

    def test_a_confirmed_run_in_another_folder_keeps_the_report_folder_until_a_book_lands_there(self):
        listed = self.write("list.txt", "A\nB\n")
        report = self.folder / "list.progress.json"
        results = {"a": [book(1)], "b": [book(2)], "c": [book(3)]}
        self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"], BatchClient(results))
        names = [item["path"] for item in json.loads(report.read_text())["items"]]
        self.write("list.txt", "A\nB\nC\n")
        other = self.folder / "other"
        other.mkdir()
        where = f"The report recorded its books in {self.folder}, and none of them is in {other}"
        command = ["download", "-a", str(listed), "--pick", "1", "--output", str(other)]

        def quota_read_fails(client):
            client.quota_failures = [None, NetworkError("synthetic")]

        def link_rate_limited(client):
            client.download_failures["3:h3"] = staged(RateLimitError("synthetic"), "link")

        def ctrl_c_before_the_link(client):
            read = client.get_quota

            def quota():
                if sum(call[0] == "quota" for call in client.calls) == 1:  # The read before the first book.
                    signal.raise_signal(signal.SIGINT)
                return read()

            client.get_quota = quota

        for stop, expected in ((quota_read_fails, 3), (link_rate_limited, 3), (ctrl_c_before_the_link, 130)):
            client = BatchClient(results)
            stop(client)
            with self.subTest(stop=stop.__name__), patch("zlib_connector.cli_batch.os.write"):
                code, out, errors, *_ = self.run_cli(command, client, answers=["y"], tty=True, output=False)
                self.assertEqual(code, expected)
                self.assertEqual(list(other.iterdir()), [])
                self.assertEqual(json.loads(report.read_text())["folder"], str(self.folder))
                for extra in ([], ["--redownload-missing"]):  # Nothing landed there, so scripts are still refused.
                    code, _, errors, _, constructor, *_ = self.run_cli([*command, "--yes", *extra],
                                                                       BatchClient(results), output=False)
                    self.assertEqual(code, 2)
                    self.assertIn(where, errors)
                    constructor.assert_not_called()
        code, _, _, client, *_ = self.run_cli(command, BatchClient(results), answers=["y"], tty=True, output=False)
        self.assertEqual((code, client.kinds("download")), (0, ["3:h3"]))
        document = json.loads(report.read_text())
        self.assertEqual(document["folder"], str(other))  # A book is there now, so that folder is the report's.
        self.assertEqual([item["path"] for item in document["items"][:2]], names)
        self.assertTrue((other / document["items"][2]["path"]).is_file())

    def test_titles_without_a_report_refuse_a_max_downloads_below_the_plan(self):
        results = {"a": [book(1)], "b": [book(2)], "c": [book(3)]}
        code, _, errors, client, *_ = self.run_cli(["download", "A", "B", "C", "--pick", "1", "--yes",
                                                    "--max-downloads", "1"], BatchClient(results))
        self.assertEqual(code, 2)
        self.assertIn("--max-downloads 1 is lower than the 3 books planned", errors)
        self.assertIn("--no-report to download only 1", errors)
        self.assertEqual(client.kinds("download"), [])
        stdin = io.TextIOWrapper(io.BytesIO(b"A\nB\nC\n"), encoding="utf-8")
        code, _, errors, client, *_ = self.run_cli(["download", "-a", "-", "--pick", "1", "--yes", "--max-downloads",
                                                    "2"], BatchClient(results), stdin=stdin)
        self.assertEqual(code, 2)
        self.assertEqual(client.kinds("download"), [])
        code, out, _, client, *_ = self.run_cli(["download", "A", "B", "C", "--pick", "1", "--yes", "--max-downloads",
                                                 "1", "--no-report"], BatchClient(results))
        self.assertEqual((code, client.kinds("download")), (3, ["1:h1"]))
        code, _, errors, client, _, prompt, _ = self.run_cli(["download", "A", "B", "C", "--pick", "1",
                                                              "--max-downloads", "1"], BatchClient(results),
                                                             answers=["n"], tty=True)
        self.assertEqual(code, 130)
        self.assertIn("Without --report, a later run will download these books again.", errors)
        self.assertTrue(prompt.call_args.args[0].startswith(f"Download 1 of 3 books into {self.folder}?"))
        self.assertEqual(client.kinds("download"), [])
        code, _, _, client, *_ = self.run_cli(["download", "A", "B", "--pick", "1", "--yes", "--max-downloads", "2"],
                                              BatchClient(results))
        self.assertEqual((code, client.kinds("download")), (0, ["1:h1", "2:h2"]))

    def test_dry_runs_of_a_plan_never_advise_writing_over_it(self):
        listed = self.write("list.txt", "A\nB\n")
        results = {"a": [book(1), book(4)], "b": [book(2)]}
        _, out, *_ = self.run_cli(["download", "-a", str(listed), "--dry-run", "--json", "--pick", "1"],
                                  BatchClient(results))
        plan = self.write("plan.json", out)
        code, out, errors, *_ = self.run_cli(["download", "-a", str(plan), "--dry-run"], BatchClient(results))
        self.assertEqual(code, 0)
        self.assertNotIn("> plan.json", out + errors)
        self.assertIn(f"The plan already holds these choices: download it with -a {plan} --report with the list's"
                      " .progress.json file, or --no-report.", out)
        document = json.loads(plan.read_text(encoding="utf-8"))
        document["items"][1]["reference"] = None  # Search this one again, and choose in the terminal.
        plan.write_text(json.dumps(document), encoding="utf-8")
        code, out, errors, *_ = self.run_cli(["download", "-a", str(plan), "--dry-run"], BatchClient(results),
                                             answers=["1"], tty=True)
        self.assertEqual(code, 0)
        self.assertNotIn("> plan.json", out + errors)
        self.assertIn("Dry run: choices are not saved; add --json > reviewed-plan.json to keep them.", errors)
        self.assertIn("save them with --dry-run --json > reviewed-plan.json, then download -a reviewed-plan.json",
                      out)
        plan.write_bytes(b"")  # What "-a plan.json --dry-run --json > plan.json" leaves behind.
        code, _, errors, *_ = self.run_cli(["download", "-a", str(plan), "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("The JSON list is empty. Redirecting a command's output into the file it reads", errors)

    def test_a_file_over_the_size_limit_is_reported_without_retry_advice(self):
        listed = self.write("list.txt", "A\nB\n")
        client = BatchClient({"a": [book(1)], "b": [book(2)]})
        client.download_failures["1:h1"] = staged(DownloadError("synthetic", reason="size_limit"), "transfer")
        code, out, *_ = self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"], client)
        self.assertEqual(code, 4)
        self.assertEqual(client.kinds("download"), ["1:h1", "2:h2"])
        self.assertIn("Larger than 256 MiB: line 1.", out)
        self.assertNotIn("--retry-uncertain", out)
        code, out, _, again, *_ = self.run_cli(["download", "-a", str(listed), "--yes", "--retry-uncertain"],
                                               BatchClient({"a": [book(1)], "b": [book(2)]}))
        self.assertEqual((code, again.kinds("download")), (4, []))
        self.assertIn("Larger than 256 MiB: line 1. These editions are never requested again. To get another edition,"
                      " replace the item with that edition's ID:HASH", out)
        self.assertNotIn("May have used quota", out)
        self.write("list.txt", "5:h5\nB\n")  # As advised: another edition's ID:HASH in place of the line.
        code, out, _, again, *_ = self.run_cli(["download", "-a", str(listed), "--yes"], BatchClient())
        self.assertEqual((code, again.kinds("download")), (0, ["5:h5"]))
        self.assertNotIn("256 MiB", out)

    def test_streams_without_a_console_are_not_terminals(self):
        for mode in ("r", "w"):
            with open(os.devnull, mode) as stream:  # On Windows, isatty() is true for NUL.
                self.assertFalse(_tty(stream))
        with patch("zlib_connector.cli_terminal._console", return_value=False):
            code, _, errors, _, constructor, prompt, _ = self.run_cli(["download", "A", "B", "--yes"], tty=True)
        self.assertEqual(code, 2)
        self.assertIn("2 items need a search result choice. Use --pick N", errors)
        constructor.assert_not_called()
        prompt.assert_not_called()

    def test_a_recorded_download_without_a_file_name_is_kept_when_its_plan_is_downloaded(self):
        listed = self.write("list.txt", "A\n")
        self.run_cli(["download", "-a", str(listed), "--pick", "1", "--yes"], BatchClient({"a": [book(1)]}))
        document = json.loads((self.folder / "list.progress.json").read_text())
        document["items"][0]["path"] = None
        for name, kind in (("plan.json", "zlib-connector-batch-plan"), ("copy.json", "zlib-connector-batch-report")):
            path = self.write(name, json.dumps({**document, "kind": kind}))
            with self.subTest(kind=kind):
                code, out, _, client, *_ = self.run_cli(["download", "-a", str(path), "--no-report", "--yes"])
                self.assertEqual((code, client.kinds("download")), (0, []))
                self.assertIn("1 already downloaded.", out)


if __name__ == "__main__":
    unittest.main()
