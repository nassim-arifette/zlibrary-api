"""Batch parsing, planning, downloading and reports with synthetic clients only."""
import codecs
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from zlib_connector import (AuthenticationError, BatchEntry, BatchInputError, BatchItem, BatchReport, BatchReportError,
                            ChallengeRequired, Client, DownloadError, FormatChangedError, NetworkError, RateLimitError,
                            SearchFilters, StopPlanning, TransferCancelled, load_report, parse_batch, pick, plan_batch,
                            run_batch)
from zlib_connector.batch import (MISSING_FILE, MOVED_FILE, REFUSED_FILE, check_folder, report_lock, searches_needed,
                                  write_report)
from zlib_connector.batch_input import PLAN_KIND, REPORT_KIND, book_file_name
from zlib_connector.search import SearchPage
from zlib_connector.transfer import CancellationToken
from zlib_connector.transport import _write_chunk
from batch_fixture import SECRET, BatchClient, book, staged
from test_eapi import ORIGIN, QueueTransport


def items_of(text, **options):
    return parse_batch(text, **options)


class BatchParsingTests(unittest.TestCase):
    def test_text_lines_keep_positions_and_classify_references_fail_closed(self):
        items = items_of("# reading list\n\nDune  Herbert\r\n123:ab12\r2001: A Space Odyssey\n123:ab12/\n"
                         "https://other-mirror.example/book/45/cd67/title.html?dsource=recommend#top\n"
                         "https://books.example/book/45\n\x1b[31mRed\n" + "x" * 501 + "\n")
        self.assertEqual([item.position for item in items], [f"line {n}" for n in (3, 4, 5, 6, 7, 8, 9, 10)])
        self.assertEqual(items[0].query, "Dune Herbert")
        self.assertEqual(items[1].reference, "123:ab12")
        self.assertEqual(items[2].query, "2001: A Space Odyssey")
        self.assertIsNone(items[3].query)
        self.assertIn("Line 6 is not a valid ID:HASH reference", items[3].error)
        self.assertEqual(items[4].reference, "45:cd67")
        self.assertIn("Line 8", items[5].error)
        self.assertIn("control characters", items[6].error)
        self.assertIn("1 to 500 characters", items[7].error)
        self.assertNotIn("x" * 40, items[7].error)

    def test_invisible_format_marks_are_dropped_but_joiners_are_kept(self):
        item, = items_of("\ufeff\u202eAbc\u00ad\u200cdef\u200f\n")
        self.assertEqual(item.query, "Abc\u200cdef")

    def test_encodings_byte_order_marks_and_invalid_bytes(self):
        self.assertEqual(items_of(codecs.BOM_UTF8 + "Les Misérables\n".encode())[0].query, "Les Misérables")
        utf16 = "Les Misérables\r\nDune\r\n".encode("utf-16")
        self.assertEqual([item.query for item in items_of(utf16)], ["Les Misérables", "Dune"])
        with self.assertRaises(BatchInputError) as caught:
            items_of(b"Dune\nLes Mis\xe9rables\n")
        self.assertIn("Line 2 is not valid UTF-8", str(caught.exception))
        self.assertIn("CSV UTF-8", str(caught.exception))
        with self.assertRaises(BatchInputError):
            items_of(b"Dune\x00\n")

    def test_size_and_item_limits_reject_the_whole_list(self):
        with self.assertRaises(BatchInputError):
            items_of("\n".join(f"Book {number}" for number in range(1001)))
        with self.assertRaises(BatchInputError):
            items_of(b"x" * (16 * 1024 * 1024 + 1))
        self.assertEqual(len(items_of("\n".join(f"Book {number}" for number in range(1000)))), 1000)

    def test_a_csv_header_in_text_input_is_refused_unless_text_is_explicit(self):
        with self.assertRaises(BatchInputError) as caught:
            items_of("Title,Author,ISBN\nDune,Frank Herbert,\n")
        self.assertIn("--batch-format csv", str(caught.exception))
        self.assertEqual(items_of("Title,Author\n", kind="txt")[0].query, "Title,Author")

    def test_goodreads_export_ignores_unknown_columns_and_unwraps_isbns(self):
        notices = []
        csv_text = ('Book Id,Title,Author,Author l-f,ISBN,ISBN13,My Rating,Exclusive Shelf,My Review\n'
                    '1,"Leviathan Wakes (The Expanse, #1)",James S.A. Corey,"Corey, James",="0316129089",'
                    '="9780316129084",5,to-read,"long review, with\nseveral lines"\n'
                    '2,,,,="",="9780441013593",0,read,\n'
                    '3,,,,,="9.78044E+12",0,read,\n'
                    ',,,,,,,,\n')
        items = items_of(csv_text, kind="csv", warn=notices.append)
        self.assertEqual(items[0].query, "Leviathan Wakes James S.A. Corey")
        self.assertEqual(items[0].position, "line 2")
        self.assertEqual(items[1].query, "9780441013593")
        self.assertEqual(items[1].position, "line 4")
        self.assertIn("ISBN changed into a number", items[2].error)
        self.assertEqual(len(items), 3)
        self.assertEqual(len(notices), 1)
        self.assertIn("Book Id", notices[0])
        self.assertIn("My Rating", notices[0])
        self.assertNotIn("long review", notices[0])

    def test_csv_delimiters_filters_references_and_storygraph_formats(self):
        notices = []
        text = ("Title;Authors;Format;Extension;Language;Reference;URL\n"
                "Dune;Frank Herbert;paperback;pdf;French|English;;\n"
                "Exact;;;;;123:ab12;\n"
                "Linked;;;;;;https://books.example/book/9/ff/title\n"
                "Catalog;;epub;;;;https://catalog.example/item/9\n"
                "Bad;;;mobi;;;\n")
        items = items_of(text, kind="csv", warn=notices.append)
        self.assertEqual(items[0].query, "Dune Frank Herbert")
        self.assertEqual(items[0].filters, SearchFilters(formats=("pdf",), languages=("french", "english")))
        self.assertEqual(items[1].reference, "123:ab12")
        self.assertEqual(items[2].reference, "9:ff")
        self.assertEqual(items[3].query, "Catalog")
        self.assertEqual(items[3].filters.formats, ("epub",))
        self.assertIn("unsupported format", items[4].error)
        self.assertTrue(any("paperback" in notice for notice in notices))
        tabbed = items_of("query\tlanguage\nHistory\tfrench\n", kind="csv")
        self.assertEqual((tabbed[0].query, tabbed[0].filters.languages), ("History", ("french",)))

    def test_tsv_and_one_column_lists_keep_delimiters_inside_values(self):
        titles = ["Godel, Escher, Bach", "Guns, Germs, and Steel"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "list.tsv"
            path.write_text("title\r\nGodel, Escher, Bach\r\nGuns, Germs, and Steel\r\n", encoding="utf-8")
            self.assertEqual([item.query for item in parse_batch(path)], titles)
            self.assertEqual([item.query for item in parse_batch(path, kind="csv")], titles)
            path.write_text("title,author\nDune,Herbert\n", encoding="utf-8")
            with self.assertRaises(BatchInputError):  # A comma list named .tsv fails closed.
                parse_batch(path)
        for text in ("title\nGodel, Escher, Bach\nGuns, Germs, and Steel\n", "Title\nGodel, Escher, Bach\r\n"
                     '"Guns, Germs, and Steel"\n', "query\nGodel, Escher, Bach\nGuns, Germs, and Steel"):
            with self.subTest(text=text[:12]):
                self.assertEqual([item.query for item in items_of(text, kind="csv")], titles)
        self.assertEqual(items_of("title\nDune; Messiah\n", kind="csv")[0].query, "Dune; Messiah")

    def test_rows_wider_than_the_header_are_item_errors_never_searches(self):
        items = items_of("title,author\nGodel, Escher, Bach,Douglas Hofstadter\nDune,,Frank Herbert\n"
                         "Neuromancer,Gibson,\n", kind="csv")
        self.assertIn("Line 2 has more cells than the header", items[0].error)
        self.assertIn("Line 3 has more cells than the header", items[1].error)
        self.assertNotIn("Escher", items[0].error)
        self.assertEqual(items[2].query, "Neuromancer Gibson")
        client = BatchClient({"neuromancer gibson": [book(1)]})
        plan_batch(client, items, choose=pick(1))
        self.assertEqual(client.kinds("search"), ["Neuromancer Gibson"])

    def test_an_unclosed_quote_rejects_the_list_and_swallowed_rows_are_errors(self):
        rows = "".join(f"Title {number},Author {number}\n" for number in range(4, 9))
        for text in ('title,author\nA,B\n"C,D\n' + rows, 'title,author\nA,B\nC,"D\n' + rows,
                     'title,author,notes\nA,B,x\nC,D,"to read\n' + rows.replace("\n", ",y\n"),
                     'title,author\r\nA,B\r\n"C,D\r\n' + rows.replace("\n", "\r\n"),
                     'title,author\nA,B\n"C,D\n' + rows.rstrip("\n"),
                     '#TYPE System.Management.Automation.PSCustomObject\n"Title"\n"Dune\nNeuromancer\n'):
            with self.subTest(text=text[:24]), self.assertRaises(BatchInputError) as caught:
                items_of(text, kind="csv")
            self.assertIn("Line 3 opens a quote that is never closed", str(caught.exception))
            self.assertNotIn("Title 4", str(caught.exception))
        closed = items_of('title,review\nA,"line one\nline two, still"\nB,ok\n', kind="csv")
        self.assertEqual([item.query for item in closed], ["A", "B"])
        self.assertEqual(items_of('title,author\nA,B\nC,"D\n', kind="csv")[1].query, "C D")
        merged = items_of('title,author\nA,"B\nC,D\n"E, x",F\nG,H\n', kind="csv")
        self.assertIn("Line 2 has more cells than the header", merged[0].error)
        self.assertEqual(merged[1].query, "G H")
        spanning = items_of('title,author,language,notes\nA,"B\nC,D\n"E",french,x\nG,H,,\n', kind="csv")
        self.assertIn("Line 2 has a value that spans several rows", spanning[0].error)
        self.assertEqual(spanning[1].query, "G H")
        self.assertIn("spans several rows", items_of('title\n"Dune\nNeuromancer\n"Snow Crash"\n', kind="csv")[0].error)

    def test_text_lists_refuse_a_column_name_and_skip_hidden_comments(self):
        for text in ("title\nDune\n", '"Title"\nDune\n', "ISBN\n9780441013593\n", "URL\n1:h1\n", "Author\nHerbert\n",
                     "\u200bTitle\nDune\n", "# exported\nQuery\nDune\n"):
            with self.subTest(text=text), self.assertRaises(BatchInputError) as caught:
                items_of(text)
            self.assertIn("column name", str(caught.exception))
        self.assertEqual(items_of("title\nDune\n", kind="txt")[0].query, "title")
        self.assertEqual([item.query for item in items_of("title\nDune\n", kind="csv")], ["Dune"])
        self.assertEqual(items_of("Language\nDune\n")[0].query, "Language")
        hidden = items_of("\ufeff# Science fiction\n\u200b# todo\n\u200e# note\n\ufeff\n\u200b  \n\u200d\n"
                          "# bell \x07\nDune\n")
        self.assertEqual([(item.position, item.query) for item in hidden], [("line 8", "Dune")])

    def test_a_credentials_file_read_as_a_list_is_refused_without_echoing_it(self):
        for text in ('domain = "https://books.example/"\nemail = "reader@example.com"\npassword = "synthetic-secret"\n',
                     "Dune\nPASSWORD=synthetic-secret\n", "\"email\" = 'reader@example.com'\n"):
            for kind in (None, "txt"):
                with self.subTest(text=text[:12], kind=kind), self.assertRaises(BatchInputError) as caught:
                    items_of(text, kind=kind)
                self.assertIn("looks like a credentials setting", str(caught.exception))
                self.assertNotIn("synthetic-secret", str(caught.exception))
                self.assertNotIn("reader@example.com", str(caught.exception))
        self.assertEqual(items_of("E=mc2: A Biography\n")[0].query, "E=mc2: A Biography")

    def test_json_in_a_text_list_is_refused_unless_text_is_explicit(self):
        plan = json.dumps({"kind": PLAN_KIND, "version": 1, "items": [{"source": {"query": "Dune"}}]}, indent=2)
        broken = plan.replace('"version": 1,', '"version": 1,,')  # A plan edited by hand.
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for number, text in enumerate((plan, '["Dune", "Neuromancer"]\n', '[\n  "Dune",\n  "Neuromancer"\n]\n',
                                           broken, "# saved with --json\n" + plan)):
                path = folder / f"plan{number}.txt"
                path.write_text(text, encoding="utf-8")
                with self.subTest(number=number), self.assertRaises(BatchInputError) as caught:
                    parse_batch(path)
                self.assertIn("name it .json or use --batch-format json", str(caught.exception))
                self.assertNotIn("Dune", str(caught.exception))
                self.assertTrue(all(item.query for item in parse_batch(path, kind="txt")))  # Explicit text is searched.
            titles = folder / "titles.txt"
            titles.write_text("[Graphic novel] Maus\n{Braces} in a title\n", encoding="utf-8")
            self.assertEqual([item.query for item in parse_batch(titles)], ["[Graphic novel] Maus", "{Braces} in a title"])
            titles.write_text("{Braces}\n", encoding="utf-8")
            self.assertEqual([item.query for item in parse_batch(titles)], ["{Braces}"])

    def test_recorded_file_names_are_checked_without_the_filesystem(self):
        for name in ("Dune-0123456789ab.epub", "Les Misérables-0123456789ab.PDF", "x.djvu", "Book\u200c 1.epub"):
            self.assertTrue(book_file_name(name), name)
        for name in ("", ".", "..", ".epub", "x.mobi", "dir/x.epub", "dir\\x.epub", "/x.epub", "../x.epub",
                     "C:x.epub", "C:\\x.epub", "\\\\server\\share\\x.epub", "\\\\?\\C:\\x.epub", "x.epub:stream",
                     "CON.epub", "nul.pdf", "COM1.epub", "lpt9 .djvu", "x\x1b[2J.epub", "x\x85.epub", " x.epub",
                     "x.epub ", "x" * 300 + ".epub", "a?.epub", None, 5):
            self.assertFalse(book_file_name(name), repr(name))

    def test_csv_without_usable_columns_names_the_headers_found(self):
        with self.assertRaises(BatchInputError) as caught:
            items_of("Name,Rating\nDune,5\n", kind="csv")
        self.assertIn("Name, Rating", str(caught.exception))
        self.assertNotIn("Dune", str(caught.exception))
        powershell = items_of('#TYPE System.Management.Automation.PSCustomObject\n"Title"\n"Dune"\n', kind="csv")
        self.assertEqual((powershell[0].query, powershell[0].position), ("Dune", "line 3"))
        self.assertIn("Line 2 has no query", items_of("Title,Author\n,Frank Herbert\n", kind="csv")[0].error)

    def test_json_shapes_include_cli_book_output_and_reject_unsafe_documents(self):
        books = [book(1).to_dict(), {**book(2).to_dict(), "book_id": "", "book_hash": ""}]
        items = items_of(json.dumps({"books": books, "pagination": {"page": 1}}))
        self.assertEqual(items[0].reference, "1:h1")
        self.assertEqual(items[1].reference, "2:h2")
        mixed = items_of(json.dumps(["Dune", {"Title": "Neuromancer", "authors": "Gibson", "format": "epub"},
                                     {"isbn13": 9780441013593}, {"reference": "123"}, 5, {"year": 1990}]))
        self.assertEqual(mixed[0].query, "Dune")
        self.assertEqual((mixed[1].query, mixed[1].filters.formats), ("Neuromancer Gibson", ("epub",)))
        self.assertEqual(mixed[2].query, "9780441013593")
        self.assertIn("Item 4 is not a valid ID:HASH", mixed[3].error)
        self.assertIn("must be text or an object", mixed[4].error)
        self.assertIn("has no query", mixed[5].error)
        self.assertEqual(items_of('{"items": ["Dune"]}')[0].query, "Dune")
        for text in ('{"items": [], "items": []}', '[NaN]', '[1e999]', "[" * 100000 + "]" * 100000,
                     '{"kind": "other", "items": []}', '{"value": 1}', '[1,'):
            with self.subTest(text=text[:20]), self.assertRaises(BatchInputError):
                items_of(text)
        # The depth limit is explicit, so it does not depend on the interpreter's stack (Python 3.14 on Linux).
        with self.assertRaisesRegex(BatchInputError, "nested too deeply"):
            items_of("[" * 33 + "]" * 33)
        with self.assertRaisesRegex(BatchInputError, "nested too deeply"):
            items_of(json.dumps({"items": [{"title": "Dune", "notes": [[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[1]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]}]}))
        self.assertIn("must be text or an object", items_of("[" * 32 + "]" * 32)[0].error)

    def test_paths_text_bytes_and_sequences_are_distinct_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "list.csv"
            path.write_text("title\nDune\n", encoding="utf-8")
            self.assertEqual(parse_batch(path)[0].query, "Dune")
            self.assertEqual(parse_batch(str(path))[0].query, str(path))
            with self.assertRaises(BatchInputError):
                parse_batch(Path(directory) / "missing.txt")
            with self.assertRaises(BatchInputError):
                parse_batch(Path(directory))
        self.assertEqual([item.query for item in parse_batch(("Dune", "  "))][:1], ["Dune"])
        self.assertIn("is empty", parse_batch(["Dune", " "])[1].error)
        with self.assertRaises(ValueError):
            parse_batch(["Dune"], kind="txt")
        with self.assertRaises(ValueError):
            parse_batch(42)


class BatchPlanningTests(unittest.TestCase):
    def test_identical_requests_search_once_and_identical_editions_are_duplicates(self):
        client = BatchClient({"dune": [book(1), book(2)], "dune herbert": [book(1)], "dune messiah": [book(3)]})
        items = items_of("Dune\n  DUNE \ndune herbert\nDune Messiah\n")
        plan = plan_batch(client, items, choose=pick(1))
        self.assertEqual([entry.status for entry in plan.entries], ["planned", "duplicate", "duplicate", "planned"])
        self.assertEqual([entry.duplicate_of for entry in plan.entries], [None, 1, 1, None])
        self.assertEqual(client.kinds("search"), ["Dune", "dune herbert", "Dune Messiah"])
        formats = plan_batch(BatchClient({"dune": [book(1)]}), items_of("Dune\nDune\n"), choose=pick(1),
                             filters=SearchFilters(formats=("epub",)))
        self.assertEqual(formats.entries[1].status, "duplicate")

    def test_pick_bounds_not_found_and_per_item_filters(self):
        client = BatchClient({"dune": [book(1), book(2)], "rare": [], "history": [book(5, "pdf")]})
        items = items_of("query,extension,language\nDune,,\nRare,,\nHistory,pdf,\n", kind="csv")
        plan = plan_batch(client, items, choose=pick(2), filters=SearchFilters(formats=("epub",), languages=("french",)))
        self.assertEqual([entry.status for entry in plan.entries], ["planned", "not_found", "not_found"])
        self.assertEqual(plan.entries[0].reference, "2:h2")
        self.assertIn("Only 1 result matched; result 2 was requested", plan.entries[2].message)
        searched = [call[2] for call in client.calls if call[0] == "search"]
        self.assertEqual(searched[2], SearchFilters(formats=("pdf",), languages=("french",)))
        self.assertTrue(all(call[3] == 5 for call in client.calls if call[0] == "search"))
        with self.assertRaises(ValueError):
            pick(0)

    def test_references_use_get_book_and_unsupported_formats_are_item_errors(self):
        client = BatchClient()
        client.extensions["2:h2"] = "mobi"
        plan = plan_batch(client, items_of("1:h1\n2:h2\n"), filters=SearchFilters(formats=("pdf",)))
        self.assertEqual([entry.status for entry in plan.entries], ["planned", "error"])
        self.assertTrue(plan.entries[0].verified)
        self.assertIn("apply only to searches", plan.entries[0].message)
        self.assertEqual(plan.entries[1].error_code, "UnsupportedFormat")
        self.assertEqual(client.kinds("book"), ["1:h1", "2:h2"])
        self.assertEqual(client.kinds("search"), [])

    def test_interactive_choices_skip_stop_and_invalid_returns(self):
        client = BatchClient({"a": [book(1), book(2)], "b": [book(3)], "c": [book(4)]})
        answers = iter([lambda books: books[1], lambda books: None])

        def choose(item, books):
            self.assertIsInstance(item, BatchItem)
            try:
                return next(answers)(books)
            except StopIteration:
                raise StopPlanning from None

        plan = plan_batch(client, items_of("A\nB\nC\n5:h5\n"), choose=choose)
        self.assertEqual([entry.status for entry in plan.entries], ["planned", "skipped", "not_started", "planned"])
        self.assertEqual(plan.entries[0].reference, "2:h2")
        self.assertEqual(plan.stopped[0], "Stopped")
        self.assertEqual(client.kinds("book"), ["5:h5"])
        with self.assertRaises(ValueError):
            plan_batch(BatchClient({"a": [book(1)]}), items_of("A\n"), choose=lambda item, books: book(1))

    def test_without_choose_items_need_a_choice_and_keep_candidates(self):
        plan = plan_batch(BatchClient({"a": [book(1), book(2)]}), items_of("A\n"))
        entry, = plan.entries
        self.assertEqual(entry.status, "needs_choice")
        self.assertEqual([candidate.book_id for candidate in entry.candidates], ["1", "2"])
        record = plan.to_dict()["items"][0]
        self.assertEqual([candidate["reference"] for candidate in record["candidates"]], ["1:h1", "2:h2"])

    def test_account_wide_failures_stop_resolution_but_keep_offline_results(self):
        for error, code in ((RateLimitError("synthetic"), "RateLimitError"), (NetworkError("synthetic"), "NetworkError"),
                            (AuthenticationError("synthetic"), "AuthenticationError"),
                            (ChallengeRequired("synthetic"), "ChallengeRequired")):
            client = BatchClient({"a": [book(1)], "b": [book(2)]})
            client.search_failures = [None, error]
            with self.subTest(code=code):
                plan = plan_batch(client, items_of("A\nB\nC\n9:h9\n123:\n"), choose=pick(1))
                self.assertEqual([entry.status for entry in plan.entries],
                                 ["planned", "not_started", "not_started", "not_started", "error"])
                self.assertEqual(plan.stopped[0], code)
                self.assertEqual(client.kinds("search"), ["A", "B"])
                self.assertEqual(client.kinds("book"), [])

    def test_one_format_error_is_an_item_error_but_two_in_a_row_stop(self):
        client = BatchClient({"c": [book(3)]})
        client.search_failures = [FormatChangedError("synthetic"), None, FormatChangedError("synthetic"),
                                  DownloadError("synthetic")]
        client.results["b"] = [book(2)]
        plan = plan_batch(client, items_of("A\nB\nC\nD\nE\n"), choose=pick(1))
        self.assertEqual([entry.status for entry in plan.entries], ["error", "planned", "error", "error", "not_started"])
        self.assertEqual(plan.stopped[0], "DownloadError")

    def test_interrupting_a_choice_returns_the_partial_plan(self):
        for error in (KeyboardInterrupt(), TransferCancelled()):
            client = BatchClient({"a": [book(1)], "b": [book(2)]})
            calls = []

            def choose(item, books):
                calls.append(item.query)
                if len(calls) == 2:
                    raise error
                return books[0]

            with self.subTest(error=type(error).__name__):
                plan = plan_batch(client, items_of("A\nB\n3:h3\n"), choose=choose)
                self.assertTrue(plan.cancelled)
                self.assertEqual([entry.status for entry in plan.entries], ["planned", "not_started", "not_started"])
                self.assertEqual(client.kinds("book"), [])

    def test_searches_needed_finds_choices_without_any_request(self):
        items = items_of("A\n1:h1\nB\n")
        self.assertEqual([item.query for item in searches_needed(items)], ["A", "B"])

    def test_filters_are_part_of_an_item_identity(self):
        class FilteredClient(BatchClient):
            def search_page(self, query, *, filters=None, page=1, limit=20):
                found = super().search_page(query, filters=filters, page=page, limit=limit)
                formats = filters.formats if filters is not None else ()
                return SearchPage(tuple(entry for entry in found.books if not formats or entry.extension in formats),
                                  page, limit)

        results = {"dune": [book(1, "epub"), book(2, "pdf")]}
        client = FilteredClient(results)
        plan = plan_batch(client, items_of("title,format\nDune,epub\nDune,pdf\n", kind="csv"), choose=pick(1))
        self.assertEqual([(entry.status, entry.reference) for entry in plan.entries],
                         [("planned", "1:h1"), ("planned", "2:h2")])
        self.assertEqual([call[2].formats for call in client.calls if call[0] == "search"], [("epub",), ("pdf",)])
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory).resolve()
            report = folder / "progress.json"
            client = FilteredClient(results)
            run_batch(client, plan_batch(client, items_of("Dune\n"), choose=pick(1),
                                         filters=SearchFilters(formats=("epub",))), folder, report=report)
            again = FilteredClient(results)
            plan = plan_batch(again, items_of("Dune\n"), choose=pick(1), filters=SearchFilters(formats=("pdf",)),
                              previous=load_report(report), folder=folder)
            self.assertEqual([(entry.status, entry.reference) for entry in plan.entries], [("planned", "2:h2")])
            self.assertEqual(again.kinds("search"), ["Dune"])
            self.assertEqual([entry.reference for entry in plan.previous], ["1:h1"])

    def test_lookups_are_spaced_from_the_end_of_the_previous_one(self):
        # A ends at 0.0; B starts at 0.1 and ends at 0.6; C starts at 10.6, after a long pause.
        times = [0.0, 0.1, 0.6, 10.6, 10.7]
        clock = Mock(side_effect=lambda: times.pop(0) if len(times) > 1 else times[0])
        with patch("zlib_connector.batch.time.monotonic", clock), \
                patch("zlib_connector.batch.time.sleep") as sleep:
            plan_batch(BatchClient({"a": [book(1)], "b": [book(2)], "c": [book(3)]}), items_of("A\nB\nC\n"),
                       choose=pick(1), pause=0.5)
        # 0.1 s after the first lookup ended: wait the rest; 10 s spent choosing: no wait.
        self.assertEqual([round(call.args[0], 3) for call in sleep.call_args_list], [0.4])

    def test_an_uncertain_record_without_an_edition_is_searched_only_on_request(self):
        previous = BatchReport([BatchEntry(1, BatchItem("line 1", query="A"), "error", quota_may_be_used=True)])
        client = BatchClient({"a": [book(1)]})
        kept = plan_batch(client, items_of("A\n"), choose=pick(1), previous=previous)
        self.assertEqual((kept.entries[0].status, kept.entries[0].quota_may_be_used), ("error", True))
        self.assertEqual(client.calls, [])
        released = plan_batch(client, items_of("A\n"), choose=pick(1), previous=previous, retry_uncertain=True)
        self.assertEqual([entry.status for entry in released.entries], ["planned"])
        self.assertEqual(client.kinds("search"), ["A"])


class BatchRunTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name).resolve()
        self.report = self.folder / "progress.json"

    def planned(self, client, text, **options):
        return plan_batch(client, items_of(text), choose=pick(1), **options)

    def statuses(self, batch):
        return [entry.status for entry in batch.entries]

    def recorded(self, text="A\nB\nC\n"):
        """Record editions without downloading, then return a client and a plan that resumes them."""
        self.report.unlink(missing_ok=True)
        client = BatchClient({"a": [book(1)], "b": [book(2)], "c": [book(3)]}, remaining=0)
        run_batch(client, self.planned(client, text), self.folder, report=self.report)
        again = BatchClient(remaining=5)
        return again, self.planned(again, text, previous=load_report(self.report), folder=self.folder)

    def test_quota_zero_partial_and_unknown(self):
        client = BatchClient({"a": [book(1)], "b": [book(2)], "c": [book(3)]}, remaining=0)
        report = run_batch(client, self.planned(client, "A\nB\n"), self.folder)
        self.assertEqual(self.statuses(report), ["waiting_quota", "waiting_quota"])
        self.assertEqual(report.stopped[0], "QuotaExhausted")
        self.assertEqual(client.kinds("download"), [])
        client = BatchClient({"a": [book(1)], "b": [book(2)], "c": [book(3)]}, remaining=1)
        report = run_batch(client, self.planned(client, "A\nB\nC\n"), self.folder)
        self.assertEqual(self.statuses(report), ["downloaded", "waiting_quota", "waiting_quota"])
        self.assertEqual(report.quota.remaining, 0)
        client = BatchClient({"a": [book(1)], "b": [book(2)]}, remaining=None)
        report = run_batch(client, self.planned(client, "A\nB\n"), self.folder)
        self.assertEqual(self.statuses(report), ["downloaded", "downloaded"])
        self.assertEqual(report.quota.to_dict(), {"limit": None, "used": None, "remaining": None})

    def test_failed_quota_reads_never_request_a_download(self):
        for error, code, rest in ((NetworkError("synthetic"), "QuotaCheckFailed", "not_started"),
                                  (FormatChangedError("synthetic"), "QuotaCheckFailed", "not_started"),
                                  (RateLimitError("synthetic"), "RateLimitError", "waiting_quota"),
                                  (AuthenticationError("synthetic"), "AuthenticationError", "not_started")):
            client = BatchClient({"a": [book(1)], "b": [book(2)]})
            client.quota_failures = [error]
            with self.subTest(code=code):
                report = run_batch(client, self.planned(client, "A\nB\n"), self.folder)
                self.assertEqual(self.statuses(report), [rest, rest])
                self.assertEqual(report.stopped[0], code)
                self.assertEqual(client.kinds("download"), [])

    def test_uncertain_failures_stop_once_and_are_never_repeated(self):
        client = BatchClient({"a": [book(1)], "b": [book(2)]})
        client.download_failures["1:h1"] = staged(NetworkError(f"synthetic {SECRET}", reason="timeout"), "transfer")
        report = run_batch(client, self.planned(client, "A\nB\n"), self.folder, report=self.report)
        self.assertEqual(self.statuses(report), ["error", "not_started"])
        self.assertTrue(report.entries[0].quota_may_be_used)
        self.assertIn("--timeout", report.entries[0].message)
        self.assertEqual(client.kinds("download"), ["1:h1"])
        again = BatchClient({"a": [book(1)], "b": [book(2)]})
        rerun = run_batch(again, self.planned(again, "A\nB\n", previous=load_report(self.report)), self.folder,
                          report=self.report)
        self.assertEqual(self.statuses(rerun), ["error", "downloaded"])
        self.assertEqual(again.kinds("download"), ["2:h2"])
        self.assertEqual(again.kinds("search"), [])
        retry = BatchClient()
        retried = run_batch(retry, plan_batch(retry, items_of("A\nB\n"), previous=load_report(self.report),
                                              retry_uncertain=True, folder=self.folder), self.folder,
                            report=self.report)
        self.assertEqual(self.statuses(retried), ["downloaded", "already_downloaded"])
        self.assertEqual(retry.kinds("download"), ["1:h1"])
        self.assertNotIn(SECRET, self.report.read_text())

    def test_account_wide_download_failures_stop_and_classify_quota_use(self):
        cases = ((AuthenticationError("synthetic"), None, "not_started", "not_started"),
                 (AuthenticationError("synthetic"), "link", "not_started", "not_started"),
                 (ChallengeRequired("synthetic"), "link", "error", "not_started"),
                 (FormatChangedError("synthetic"), "link", "error", "not_started"),
                 (RateLimitError("synthetic"), "link", "waiting_quota", "waiting_quota"),
                 (RateLimitError("synthetic"), "transfer", "error", "waiting_quota"),
                 (NetworkError("synthetic"), "link", "error", "not_started"))
        for error, stage, first, rest in cases:
            client = BatchClient({"a": [book(1)], "b": [book(2)]})
            client.download_failures["1:h1"] = staged(error, stage) if stage else error
            with self.subTest(error=type(error).__name__, stage=stage):
                report = run_batch(client, self.planned(client, "A\nB\n"), self.folder)
                self.assertEqual(self.statuses(report), [first, rest])
                self.assertEqual(report.entries[0].quota_may_be_used, first == "error")
                self.assertEqual(report.stopped[0], type(error).__name__)
                self.assertEqual(client.kinds("download"), ["1:h1"])

    def test_per_book_failures_continue_but_two_in_a_row_stop(self):
        client = BatchClient({name: [book(number)] for number, name in enumerate("abcdef", 1)})
        client.download_failures["1:h1"] = staged(DownloadError("synthetic", reason="invalid_book"), "transfer")
        # A file over the size limit is that edition's problem only, so it never counts toward a stop.
        client.download_failures["3:h3"] = staged(DownloadError("synthetic", reason="size_limit"), "transfer")
        client.download_failures["4:h4"] = staged(DownloadError("synthetic", reason="format_mismatch"), "link")
        client.download_failures["5:h5"] = staged(DownloadError("synthetic", reason="invalid_book"), "transfer")
        report = run_batch(client, self.planned(client, "A\nB\nC\nD\nE\nF\n"), self.folder)
        self.assertEqual(self.statuses(report), ["error", "downloaded", "error", "error", "error", "not_started"])
        self.assertEqual([entry.error_code for entry in report.entries],
                         ["DownloadError", None, "TooLarge", "FormatMismatch", "DownloadError", None])
        self.assertEqual(report.stopped[0], "DownloadError")
        self.assertTrue(all(entry.quota_may_be_used for entry in report.entries if entry.status == "error"))
        self.assertIn("another format", report.entries[3].message)
        self.assertIn("--retry-uncertain", report.entries[3].message)

    def test_files_over_the_size_limit_are_final_and_never_requested_again(self):
        client = BatchClient({"a": [book(1)], "b": [book(2)]})
        client.download_failures["1:h1"] = staged(DownloadError("synthetic", reason="size_limit"), "transfer")
        report = run_batch(client, self.planned(client, "A\nB\n"), self.folder, report=self.report)
        self.assertEqual(self.statuses(report), ["error", "downloaded"])
        first = report.entries[0]
        self.assertEqual((first.error_code, first.quota_may_be_used), ("TooLarge", True))
        self.assertIn("256 MiB", first.message)
        self.assertNotIn("--retry-uncertain", first.message)
        self.assertIsNone(report.stopped)
        for label, items, previous in (("list", items_of("A\nB\nC\n"), load_report(self.report)),
                                       ("report as a list", parse_batch(self.report), None)):
            for retry in (False, True):
                again = BatchClient({"c": [book(1)]})
                with self.subTest(source=label, retry_uncertain=retry):
                    plan = plan_batch(again, items, choose=pick(1), previous=previous, retry_uncertain=retry,
                                      folder=self.folder)
                    entry = plan.entries[0]
                    self.assertEqual((entry.status, entry.error_code, entry.quota_may_be_used),
                                     ("error", "TooLarge", True))
                    self.assertNotIn("--retry-uncertain", entry.message)
                    run_batch(again, plan, self.folder)
                    self.assertEqual(again.kinds("download"), [])
        # Another request that reaches the same edition is not sent for it either.
        again = BatchClient({"c": [book(1)]})
        plan = plan_batch(again, items_of("C\n"), choose=pick(1), previous=load_report(self.report), folder=self.folder)
        self.assertEqual([(entry.status, entry.error_code, entry.quota_may_be_used) for entry in plan.entries],
                         [("error", "TooLarge", False)])
        run_batch(again, plan, self.folder)
        self.assertEqual(again.kinds("download"), [])

    def test_refused_links_check_the_quota_before_continuing(self):
        client = BatchClient({"a": [book(1)], "b": [book(2)]}, remaining=5)
        client.download_failures["1:h1"] = staged(DownloadError("synthetic", reason="unavailable"), "link")
        report = run_batch(client, self.planned(client, "A\nB\n"), self.folder)
        self.assertEqual(self.statuses(report), ["error", "downloaded"])
        self.assertEqual(report.entries[0].error_code, "Unavailable")
        self.assertFalse(report.entries[0].quota_may_be_used)
        client = BatchClient({"a": [book(1)], "b": [book(2)]}, remaining=5)
        client.download_failures["1:h1"] = staged(DownloadError("synthetic", reason="no_link"), "link")
        client.quota_failures = [None, NetworkError("synthetic")]
        report = run_batch(client, self.planned(client, "A\nB\n"), self.folder)
        self.assertEqual(self.statuses(report), ["waiting_quota", "waiting_quota"])
        self.assertEqual(report.stopped[0], "Unavailable")

    def test_local_storage_failures_stop_the_batch(self):
        for error, used in ((staged(DownloadError("synthetic", reason="save_failed"), "transfer"), True),
                            (staged(DownloadError("synthetic", reason="local_write"), "transfer"), True),
                            (DownloadError("synthetic", reason="save_failed"), False), (OSError("synthetic"), False)):
            client = BatchClient({"a": [book(1)], "b": [book(2)]})
            client.download_failures["1:h1"] = error
            with self.subTest(error=repr(error)):
                report = run_batch(client, self.planned(client, "A\nB\n"), self.folder)
                self.assertEqual(self.statuses(report), ["error" if used else "not_started", "not_started"])
                self.assertEqual(report.stopped[0], "LocalStorageError")
                self.assertEqual(client.kinds("download"), ["1:h1"])

    def test_local_write_failures_are_tagged_by_the_transport(self):
        class Broken:
            def write(self, data):
                raise OSError("synthetic")

        class Stuck:
            def write(self, data):
                return 0

        for target in (Broken(), Stuck()):
            with self.subTest(target=type(target).__name__), self.assertRaises(DownloadError) as caught:
                _write_chunk(target, b"synthetic")
            self.assertEqual(caught.exception.reason, "local_write")

    def test_a_protocol_error_after_the_link_request_stops_the_batch(self):
        client = BatchClient({"a": [book(1)], "b": [book(2)]})
        client.download_failures["1:h1"] = staged(DownloadError("synthetic", reason="protocol"), "transfer")
        report = run_batch(client, self.planned(client, "A\nB\n"), self.folder)
        self.assertEqual(self.statuses(report), ["error", "not_started"])
        self.assertEqual((report.entries[0].error_code, report.entries[0].quota_may_be_used), ("ProtocolError", True))
        self.assertEqual(report.stopped[0], "ProtocolError")
        self.assertEqual(client.kinds("download"), ["1:h1"])

    def test_cancellation_during_a_download_records_the_item_and_stops(self):
        client = BatchClient({"a": [book(1)], "b": [book(2)]})
        client.download_failures["1:h1"] = staged(TransferCancelled(), "transfer")
        report = run_batch(client, self.planned(client, "A\nB\n"), self.folder, report=self.report)
        self.assertTrue(report.cancelled)
        self.assertEqual(self.statuses(report), ["cancelled", "not_started"])
        self.assertTrue(report.entries[0].quota_may_be_used)
        self.assertEqual(json.loads(self.report.read_text())["stopped"]["error_code"], "Cancelled")
        self.assertEqual(sum(call[0] == "quota" for call in client.calls), 1)  # No refresh after a cancellation.
        again = BatchClient({"a": [book(1)], "b": [book(2)]})
        rerun = run_batch(again, self.planned(again, "A\nB\n", previous=load_report(self.report), folder=self.folder),
                          self.folder, report=self.report)
        self.assertEqual([(entry.status, entry.quota_may_be_used) for entry in rerun.entries],
                         [("cancelled", True), ("downloaded", False)])
        self.assertEqual(again.kinds("download"), ["2:h2"])

    def test_a_stop_request_lets_the_current_book_finish(self):
        stop = CancellationToken()
        client = BatchClient({"a": [book(1)], "b": [book(2)]})
        client.during_download = lambda reference: stop.cancel()
        report = run_batch(client, self.planned(client, "A\nB\n"), self.folder, report=self.report, stop=stop)
        self.assertTrue(report.cancelled)
        self.assertEqual(report.stopped[0], "Cancelled")
        self.assertEqual([(entry.status, entry.quota_may_be_used) for entry in report.entries],
                         [("downloaded", False), ("not_started", False)])
        self.assertTrue(report.entries[0].path.is_file())
        self.assertEqual(client.kinds("download"), ["1:h1"])
        self.assertEqual(sum(call[0] == "quota" for call in client.calls), 1)  # Nothing is read for the next book.
        stop = CancellationToken()
        again, plan = self.recorded("A\nB\n")
        real = again.get_book
        again.get_book = lambda reference: (stop.cancel(), real(reference))[1]
        report = run_batch(again, plan, self.folder, report=self.report, stop=stop)
        self.assertEqual(self.statuses(report), ["not_started", "not_started"])
        self.assertEqual(again.kinds("download"), [])

    def test_a_stop_without_a_final_quota_read_drops_only_a_quota_read_before_a_download(self):
        results = {"a": [book(1)], "b": [book(2)]}
        stop = CancellationToken()
        client = BatchClient(results, remaining=5)
        client.during_download = lambda reference: stop.cancel()
        report = run_batch(client, self.planned(client, "A\nB\n"), self.folder, report=self.report, stop=stop)
        self.assertEqual((report.cancelled, client.kinds("download")), (True, ["1:h1"]))
        self.assertIsNone(report.quota)  # Read before book 1, which used one since.
        self.assertIsNone(json.loads(self.report.read_text())["quota"])
        stop = CancellationToken()
        client = BatchClient(results, remaining=5)
        read = client.get_quota

        def stop_while_reading():
            if client.kinds("download"):
                stop.cancel()  # During the read before book 2: that figure stays current.
            return read()

        client.get_quota = stop_while_reading
        report = run_batch(client, self.planned(client, "A\nB\n"), self.folder, stop=stop)
        self.assertEqual((report.cancelled, client.kinds("download"), report.quota.remaining), (True, ["1:h1"], 4))
        client = BatchClient({"a": [book(1)]}, remaining=5)
        client.download_failures["1:h1"] = staged(AuthenticationError("synthetic"), "link")  # A refused link uses none.
        report = run_batch(client, self.planned(client, "A\n"), self.folder)
        self.assertEqual((report.stopped[0], report.quota.remaining), ("AuthenticationError", 5))
        client = BatchClient(results, remaining=5)
        client.quota_failures = [None, AuthenticationError("synthetic")]
        report = run_batch(client, self.planned(client, "A\nB\n"), self.folder)
        self.assertEqual((report.stopped[0], report.quota, client.kinds("download")),
                         ("AuthenticationError", None, ["1:h1"]))

    def test_write_ahead_report_marks_the_active_download(self):
        client = BatchClient({"a": [book(1)]})
        seen = []
        client.during_download = lambda reference: seen.append(json.loads(self.report.read_text())["items"][0]["status"])
        report = run_batch(client, self.planned(client, "A\n"), self.folder, report=self.report)
        self.assertEqual(seen, ["downloading"])
        record = json.loads(self.report.read_text())
        self.assertEqual(record["kind"], "zlib-connector-batch-report")
        self.assertEqual(record["items"][0]["path"], report.entries[0].path.name)
        self.assertEqual(record["summary"], {"downloaded": 1})
        self.assertEqual(list(self.folder.glob(".progress.json.*")), [])

    def test_a_crash_during_a_download_is_interrupted_on_the_next_read(self):
        client = BatchClient({"a": [book(1)]})

        def crash(reference):
            raise SystemExit("synthetic exit before the link request")

        client.during_download = crash
        with self.assertRaises(SystemExit):
            run_batch(client, self.planned(client, "A\n"), self.folder, report=self.report)
        # A handled exit is recorded; only a hard kill leaves the write-ahead state behind.
        self.assertEqual(load_report(self.report).entries[0].status, "not_started")
        record = json.loads(self.report.read_text())
        record["items"][0]["status"] = "downloading"
        self.report.write_text(json.dumps(record))
        entry = load_report(self.report).entries[0]
        self.assertEqual((entry.status, entry.quota_may_be_used), ("interrupted", True))
        again = BatchClient({"a": [book(1)]})
        plan = plan_batch(again, items_of("A\n"), choose=pick(1), previous=load_report(self.report))
        self.assertEqual(plan.entries[0].status, "interrupted")
        self.assertEqual(again.calls, [])

    def test_resume_skips_downloaded_books_and_handles_missing_files(self):
        client = BatchClient({"a": [book(1)], "b": [book(2)]})
        first = run_batch(client, self.planned(client, "A\nB\n"), self.folder, report=self.report)
        first.entries[1].path.unlink()
        again = BatchClient({"a": [book(1)], "b": [book(2)]})
        plan = plan_batch(again, items_of("A\nB\n"), choose=pick(1), previous=load_report(self.report),
                          folder=self.folder)
        self.assertEqual(self.statuses(plan), ["already_downloaded", "already_downloaded"])
        self.assertEqual([entry.message for entry in plan.entries], [None, MISSING_FILE])
        self.assertEqual(again.calls, [])
        redo = BatchClient()
        plan = plan_batch(redo, items_of("A\nB\n"), previous=load_report(self.report), redownload_missing=True,
                          folder=self.folder)
        report = run_batch(redo, plan, self.folder, report=self.report)
        self.assertEqual(self.statuses(report), ["already_downloaded", "downloaded"])
        self.assertEqual(redo.kinds("book"), ["2:h2"])
        self.assertEqual(redo.kinds("download"), ["2:h2"])
        elsewhere = plan_batch(BatchClient(), items_of("A\nB\n"), previous=load_report(self.report),
                               folder=self.folder / "missing")
        self.assertEqual([entry.message for entry in elsewhere.entries], [MOVED_FILE, MOVED_FILE])

    def test_recorded_paths_are_looked_up_only_by_name_in_the_output_folder(self):
        client = BatchClient({"a": [book(1)], "b": [book(2)], "c": [book(3)], "d": [book(4)], "e": [book(5)]})
        first = run_batch(client, self.planned(client, "A\nB\nC\nD\nE\n"), self.folder, report=self.report)
        record = json.loads(self.report.read_text())
        names = [entry.path.name for entry in first.entries]
        self.assertEqual(record["folder"], str(self.folder))
        self.assertEqual([item["path"] for item in record["items"]], names)
        self.assertEqual(record["items"][0]["book"]["reference"], "1:h1")
        outside = self.folder.parent / "elsewhere" / names[2]
        record["items"][0]["path"] = "\\\\attacker.example\\share\\" + names[0]
        record["items"][1]["path"] = "../" + names[1]
        record["items"][2]["path"] = str(outside)
        record["items"][3]["path"] = str(self.folder / names[3])  # An absolute path inside the folder is that file.
        self.report.write_text(json.dumps(record))
        checked = []
        real = os.path.isfile

        def isfile(path):
            checked.append(Path(path))
            return Path(path).parent == self.folder and real(path)  # Never touch a path outside the folder.

        for label, items, previous in (("list", parse_batch(self.report), None),
                                       ("report", items_of("A\nB\nC\nD\nE\n"), load_report(self.report))):
            checked.clear()
            again = BatchClient()
            with self.subTest(source=label), patch("zlib_connector.batch.os.path.isfile", side_effect=isfile):
                plan = plan_batch(again, items, choose=pick(1), previous=previous, folder=self.folder)
                self.assertEqual(checked, [self.folder / names[3], self.folder / names[4]])
                self.assertEqual(self.statuses(plan), ["already_downloaded"] * 5)
                self.assertEqual([entry.message for entry in plan.entries], [REFUSED_FILE] * 3 + [None, None])
                self.assertEqual(again.calls, [])
        report = run_batch(BatchClient(), plan, self.folder, report=self.report)
        self.assertEqual([item["path"] for item in json.loads(self.report.read_text())["items"]],
                         [None, None, None, names[3], names[4]])
        self.assertEqual(report.entries[3].path, self.folder / names[3])
        redo = BatchClient()
        plan = plan_batch(redo, items_of("A\nB\nC\nD\nE\n"), previous=load_report(self.report),
                          redownload_missing=True, folder=self.folder)
        self.assertEqual(self.statuses(plan), ["planned"] * 3 + ["already_downloaded"] * 2)

    def test_a_plan_made_for_another_folder_never_runs_or_erases_recorded_names(self):
        client = BatchClient({"a": [book(1)]})
        name = run_batch(client, self.planned(client, "A\n"), self.folder, report=self.report).entries[0].path.name
        before = self.report.read_bytes()
        other = self.folder / "other"
        other.mkdir()
        for folder in (None, other):  # None looks in the current folder, which is not self.folder here.
            again = BatchClient()
            with self.subTest(folder=folder):
                plan = plan_batch(again, items_of("A\n"), choose=pick(1), previous=load_report(self.report),
                                  folder=folder)
                self.assertEqual(plan.entries[0].path, Path(name))  # The recorded name, not where it was sought.
                self.assertEqual(BatchReport.from_plan(plan, self.folder).to_dict()["items"][0]["path"], name)
                with self.assertRaises(ValueError) as caught:
                    run_batch(again, plan, self.folder, report=self.report)
                self.assertIn("plan_batch(folder=...)", str(caught.exception))
                self.assertEqual(again.calls, [])
                self.assertEqual(self.report.read_bytes(), before)
        plan = plan_batch(BatchClient(), items_of("A\n"), previous=load_report(self.report), folder=self.folder)
        self.assertEqual([(entry.status, entry.message) for entry in plan.entries], [("already_downloaded", None)])
        fresh = BatchClient({"b": [book(2)]})  # A plan that looked up no recorded file runs in any folder.
        run_batch(fresh, self.planned(fresh, "B\n"), other)
        self.assertEqual(fresh.kinds("download"), ["2:h2"])

    def test_resume_reuses_choices_keeps_skips_and_earlier_records(self):
        client = BatchClient({"a": [book(1)], "b": [book(2)], "c": [book(3)]}, remaining=1)
        answers = iter([0, None, 0])
        plan = plan_batch(client, items_of("A\nB\nC\n"),
                          choose=lambda item, books: None if (n := next(answers)) is None else books[n])
        run_batch(client, plan, self.folder, report=self.report)
        later = BatchClient(remaining=5)
        plan = plan_batch(later, items_of("C\nB\n"), choose=pick(1), previous=load_report(self.report))
        self.assertEqual(self.statuses(plan), ["planned", "skipped"])
        self.assertEqual(later.kinds("search"), [])
        self.assertEqual([entry.item.query for entry in plan.previous], ["A"])
        report = run_batch(later, plan, self.folder, report=self.report)
        self.assertEqual(later.kinds("book"), ["3:h3"])
        record = json.loads(self.report.read_text())
        self.assertEqual([entry["source"]["query"] for entry in record["previous"]], ["A"])
        newest = BatchClient({"x": [book(1)]})
        plan = plan_batch(newest, items_of("X\n"), choose=pick(1), previous=load_report(self.report))
        self.assertEqual(plan.entries[0].status, "already_downloaded")
        self.assertEqual(report.entries[0].status, "downloaded")

    def test_an_edition_recorded_for_one_line_is_a_duplicate_for_another(self):
        client = BatchClient({"a": [book(1)]})
        run_batch(client, self.planned(client, "A\n"), self.folder, report=self.report)
        again = BatchClient({"b": [book(1)]})
        plan = plan_batch(again, items_of("A\nB\n"), choose=pick(1), previous=load_report(self.report))
        self.assertEqual(self.statuses(plan), ["already_downloaded", "duplicate"])
        self.assertEqual(plan.entries[1].duplicate_of, 1)

    def test_an_edition_check_that_cannot_be_made_stops_before_any_link(self):
        for error, rest in ((RateLimitError("synthetic"), "waiting_quota"), (NetworkError("synthetic"), "not_started"),
                            (AuthenticationError("synthetic"), "not_started"),
                            (ChallengeRequired("synthetic"), "not_started")):
            client, plan = self.recorded()
            client.book_failures["1:h1"] = error
            with self.subTest(error=type(error).__name__):
                report = run_batch(client, plan, self.folder, report=self.report)
                self.assertEqual(self.statuses(report), [rest] * 3)
                self.assertEqual(report.stopped[0], type(error).__name__)
                self.assertEqual(client.kinds("download"), [])

    def test_an_edition_check_that_rejects_an_edition_is_an_item_error(self):
        for error, code in ((FormatChangedError("synthetic"), "FormatChangedError"),
                            (DownloadError("synthetic", reason="refused"), "DownloadError"),
                            (ValueError("synthetic"), "InvalidBook")):
            client, plan = self.recorded()
            client.book_failures["1:h1"] = error
            with self.subTest(code=code):
                report = run_batch(client, plan, self.folder, report=self.report)
                self.assertEqual(self.statuses(report), ["error", "downloaded", "downloaded"])
                self.assertEqual((report.entries[0].error_code, report.entries[0].quota_may_be_used), (code, False))
                self.assertEqual(client.kinds("download"), ["2:h2", "3:h3"])
        client, plan = self.recorded()
        client.book_failures.update({"1:h1": FormatChangedError("synthetic"), "2:h2": ValueError("synthetic")})
        report = run_batch(client, plan, self.folder, report=self.report)
        self.assertEqual(self.statuses(report), ["error", "error", "not_started"])
        self.assertEqual(report.stopped[0], "InvalidBook")
        self.assertEqual(client.kinds("download"), [])
        client, plan = self.recorded()
        client.extensions.update({"1:h1": "mobi", "2:h2": "mobi"})
        report = run_batch(client, plan, self.folder, report=self.report)
        self.assertEqual(self.statuses(report), ["error", "error", "downloaded"])
        self.assertEqual(report.entries[0].error_code, "UnsupportedFormat")
        self.assertIsNone(report.stopped)
        self.assertEqual(client.kinds("download"), ["3:h3"])

    def test_an_uncertain_edition_reached_by_another_request_is_kept(self):
        client = BatchClient({"a": [book(1)]})
        client.download_failures["1:h1"] = staged(NetworkError("synthetic", reason="timeout"), "transfer")
        run_batch(client, self.planned(client, "A\n"), self.folder, report=self.report)
        for text, options in (("A Herbert\n", {}), ("A\n", {"filters": SearchFilters(formats=("epub",))})):
            again = BatchClient({"a": [book(1)], "a herbert": [book(1)]})
            with self.subTest(text=text, options=options):
                plan = self.planned(again, text, previous=load_report(self.report), **options)
                self.assertEqual([(entry.status, entry.quota_may_be_used) for entry in plan.entries],
                                 [("interrupted", True)])
                retry = self.planned(again, text, previous=load_report(self.report), retry_uncertain=True, **options)
                self.assertEqual([(entry.status, entry.retried) for entry in retry.entries], [("planned", True)])

    def test_a_retry_run_flags_new_doubts_and_keeps_unattempted_retries_uncertain(self):
        client = BatchClient({"a": [book(1)], "b": [book(2)]})
        client.download_failures["1:h1"] = staged(NetworkError("synthetic"), "transfer")
        run_batch(client, self.planned(client, "A\nB\n"), self.folder, report=self.report)
        retry = BatchClient()
        retry.download_failures["2:h2"] = staged(NetworkError("synthetic"), "transfer")
        plan = self.planned(retry, "A\nB\n", previous=load_report(self.report), retry_uncertain=True, folder=self.folder)
        self.assertEqual([entry.retried for entry in plan.entries], [True, False])
        report = run_batch(retry, plan, self.folder, report=self.report)
        self.assertEqual(self.statuses(report), ["downloaded", "error"])
        self.assertIn("--retry-uncertain would request it again", report.entries[1].message)
        self.assertNotIn("not retried automatically", report.entries[1].message)
        self.report.unlink()
        client = BatchClient({"a": [book(1)]})
        client.download_failures["1:h1"] = staged(NetworkError("synthetic"), "transfer")
        run_batch(client, self.planned(client, "A\n"), self.folder, report=self.report)
        empty = BatchClient(remaining=0)
        plan = self.planned(empty, "A\n", previous=load_report(self.report), retry_uncertain=True, folder=self.folder)
        report = run_batch(empty, plan, self.folder, report=self.report)
        self.assertEqual([(entry.status, entry.quota_may_be_used) for entry in report.entries], [("interrupted", True)])
        self.assertEqual(empty.kinds("download"), [])
        later = BatchClient()
        self.assertEqual(self.statuses(self.planned(later, "A\n", previous=load_report(self.report))), ["interrupted"])
        self.assertEqual(later.calls, [])
        stopped = BatchClient({"b": [book(2)]})
        stopped.search_failures = [NetworkError("synthetic")]
        plan = self.planned(stopped, "B\nA\n", previous=load_report(self.report), retry_uncertain=True)
        self.assertEqual(plan.stopped[0], "NetworkError")
        self.assertEqual([(entry.status, entry.quota_may_be_used) for entry in plan.entries],
                         [("not_started", False), ("interrupted", True)])

    def test_control_characters_in_titles_never_reach_recorded_file_names(self):
        client = BatchClient({"a": [book(1, title="Bad\x85Title\x9b")]})
        report = run_batch(client, self.planned(client, "A\n"), self.folder, report=self.report)
        self.assertEqual(report.entries[0].path.name, "Bad_Title_-000000000001.epub")
        self.assertEqual(report.entries[0].book.title, "Bad\x85Title\x9b")
        self.assertEqual(json.loads(self.report.read_text())["items"][0]["path"], report.entries[0].path.name)
        self.assertEqual(report.recorded, frozenset({1}))
        again = plan_batch(BatchClient(), items_of("A\n"), previous=load_report(self.report), folder=self.folder)
        self.assertEqual([(entry.status, entry.message) for entry in again.entries], [("already_downloaded", None)])

    def test_ctrl_c_while_recording_the_next_attempt_is_not_uncertain(self):
        client = BatchClient({"a": [book(1)]})
        writes = []
        real = write_report

        def interrupted(batch, path):
            writes.append(batch.entries[0].status)
            if writes == ["planned", "downloading"]:
                raise KeyboardInterrupt
            real(batch, path)

        with patch("zlib_connector.batch.write_report", side_effect=interrupted):
            report = run_batch(client, self.planned(client, "A\n"), self.folder, report=self.report)
        self.assertTrue(report.cancelled)
        self.assertEqual((report.entries[0].status, report.entries[0].quota_may_be_used), ("not_started", False))
        self.assertEqual(client.kinds("download"), [])
        self.assertEqual(load_report(self.report).entries[0].status, "not_started")

    def test_a_failed_write_ahead_record_stops_before_the_download(self):
        client = BatchClient({"a": [book(1)]})
        writes = []

        def flaky(batch, path):
            writes.append(batch.entries[0].status)
            if len(writes) == 2:
                raise OSError("synthetic")
            write_report(batch, path)

        with patch("zlib_connector.batch.write_report", side_effect=flaky):
            report = run_batch(client, self.planned(client, "A\n"), self.folder, report=self.report)
        self.assertEqual(writes[:2], ["planned", "downloading"])
        self.assertEqual(client.kinds("download"), [])
        self.assertEqual(report.stopped[0], "ReportError")
        self.assertEqual([(entry.status, entry.quota_may_be_used) for entry in report.entries], [("not_started", False)])

    def test_max_downloads_report_failures_and_existing_reports(self):
        client = BatchClient({"a": [book(1)], "b": [book(2)]})
        report = run_batch(client, self.planned(client, "A\nB\n"), self.folder, max_downloads=1)
        self.assertEqual(self.statuses(report), ["downloaded", "not_started"])
        self.assertEqual(report.stopped[0], "MaxDownloads")
        self.report.write_text("{}")
        with self.assertRaises(BatchInputError):
            run_batch(client, self.planned(client, "A\n"), self.folder, report=self.report)
        self.report.unlink()
        with patch("zlib_connector.batch.os.replace", side_effect=PermissionError("synthetic")):
            with self.assertRaises(BatchReportError):
                run_batch(client, self.planned(client, "A\n"), self.folder, report=self.report)
        self.assertFalse(self.report.exists())
        client = BatchClient({"a": [book(1)], "b": [book(2)]})
        writes = []
        real = write_report

        def flaky(batch, path):
            writes.append(batch.entries[0].status)
            if len(writes) == 3:
                raise OSError("synthetic")
            real(batch, path)

        with patch("zlib_connector.batch.write_report", side_effect=flaky):
            report = run_batch(client, self.planned(client, "A\nB\n"), self.folder, report=self.report)
        self.assertEqual(self.statuses(report), ["downloaded", "not_started"])
        self.assertEqual(report.stopped[0], "ReportError")
        self.assertTrue(report.saved)
        self.assertEqual(client.kinds("download"), ["1:h1"])

    def test_invalid_reports_are_refused_and_left_unchanged(self):
        for content in ("not json", '{"kind": "zlib-connector-batch-plan", "version": 1, "items": []}',
                        '{"kind": "zlib-connector-batch-report", "version": 2, "items": []}',
                        '{"kind": "zlib-connector-batch-report", "version": 1, "items": [{"source": null}]}',
                        '{"kind": "zlib-connector-batch-report", "version": 1, "items": [], "extra": 1}'):
            self.report.write_text(content)
            with self.subTest(content=content[:40]), self.assertRaises(BatchInputError):
                load_report(self.report)
            self.assertEqual(self.report.read_text(), content)
        self.assertIsNone(load_report(self.folder / "missing.json"))

    def test_reports_contain_no_secrets_or_links(self):
        client = BatchClient({"a": [book(1)], "b": [book(2)]})
        client.download_failures["1:h1"] = staged(DownloadError(f"{SECRET} https://cdn.example/{SECRET}",
                                                                reason="invalid_book"), "transfer")
        report = run_batch(client, self.planned(client, "A\nB\n"), self.folder, report=self.report)
        encoded = self.report.read_text() + json.dumps(report.to_dict())
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn("cdn.example", encoded)
        self.assertTrue(self.report.read_bytes().isascii())

    def test_run_validates_its_inputs(self):
        client = BatchClient({"a": [book(1)]})
        plan = self.planned(client, "A\n")
        for options in ({"max_downloads": 0}, {"cancel": object()}, {"on_item": 5}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                run_batch(client, plan, self.folder, **options)
        with self.assertRaises(ValueError):
            run_batch(client, plan, self.folder / "missing")
        plan.cancelled = True
        with self.assertRaises(ValueError):
            run_batch(client, plan, self.folder)


class PlanRoundTripTests(unittest.TestCase):
    def test_a_plan_is_a_list_and_its_metadata_is_never_searched(self):
        client = BatchClient({"dune": [book(1, title="Dune"), book(2)], "lost": []})
        plan = plan_batch(client, items_of("Dune\nLost\nNeeds\n"), choose=None)
        plan.entries[0].status = "planned"
        plan.entries[0].book = book(1, title="A different title")
        document = plan.to_dict()
        document["items"][2]["status"] = "needs_choice"
        document["items"][2]["candidates"] = [{"reference": "7:h7", "title": "Seven", "authors": "", "extension": "pdf",
                                               "year": "", "language": "", "size": ""}]
        document["items"][2]["pick"] = 1
        items = parse_batch(json.dumps(document))
        self.assertEqual(items[0].query, "Dune")
        self.assertEqual(items[0].choice.book_id, "1")
        self.assertEqual((items[1].query, items[1].choice), ("Lost", None))
        self.assertEqual(items[2].choice.book_id, "7")
        again = BatchClient({"lost": [book(3)]})
        replanned = plan_batch(again, items, choose=pick(1))
        self.assertEqual([entry.status for entry in replanned.entries], ["planned", "planned", "planned"])
        self.assertEqual(again.kinds("search"), ["Lost"])
        self.assertEqual(again.kinds("book"), [])

    def test_plan_documents_are_strict(self):
        plan = plan_batch(BatchClient({"a": [book(1)]}), items_of("A\n"), choose=pick(1))
        for change in (lambda doc: doc["items"][0].update(refrence="1:h1"),
                       lambda doc: doc["items"][0].update(reference="1:../x"),
                       lambda doc: doc["items"][0].update(status="done"),
                       lambda doc: doc["items"][0].update(pick=2),
                       lambda doc: doc["items"][0]["source"].update(filters={"formats": ["mobi"]}),
                       lambda doc: doc["items"][0].update(path="Dune\u001b]0;title\u0007.epub"),
                       lambda doc: doc["items"][0].update(path="Dune\u009b2J.epub"),
                       lambda doc: doc["items"][0]["book"].pop("reference")):
            document = plan.to_dict()
            change(document)
            with self.subTest(document=document["items"][0]), self.assertRaises(BatchInputError):
                parse_batch(json.dumps(document))

    def test_recorded_outcomes_survive_a_plan_read_without_its_report(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory).resolve()
            client = BatchClient({"a": [book(1)], "b": [book(2)], "c": [book(3)]})
            client.download_failures["2:h2"] = staged(NetworkError("synthetic"), "transfer")
            plan = plan_batch(client, items_of("A\nB\nC\n"), choose=pick(1))
            report = run_batch(client, plan, folder)
            path = folder / "copy.json"
            path.write_text(json.dumps(report.to_dict()))
            again = BatchClient()
            replanned = plan_batch(again, parse_batch(path), choose=pick(1), folder=folder)
            self.assertEqual([(entry.status, entry.message) for entry in replanned.entries][::2],
                             [("already_downloaded", None), ("planned", None)])
            self.assertEqual(replanned.entries[1].status, "interrupted")
            self.assertEqual(again.calls, [])
            client = BatchClient({"a": [book(1)], "b": [book(2)]})
            client.download_failures["1:h1"] = staged(TransferCancelled(), "transfer")
            cancelled = run_batch(client, plan_batch(client, items_of("A\nB\n"), choose=pick(1)), folder)
            self.assertEqual(cancelled.to_dict()["items"][0]["status"], "cancelled")
            path.write_text(json.dumps(cancelled.to_dict()))
            again = BatchClient({"a": [book(1)], "b": [book(2)]})
            replanned = plan_batch(again, parse_batch(path), choose=pick(1), folder=folder)
            self.assertEqual([entry.status for entry in replanned.entries], ["interrupted", "planned"])
            self.assertEqual(again.calls, [])

    def test_downloaded_or_attempted_items_keep_their_edition(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory).resolve()
            report = folder / "progress.json"
            client = BatchClient({"a": [book(1)], "b": [book(2)], "c": [book(3)]})
            client.download_failures["2:h2"] = staged(NetworkError("synthetic"), "transfer")
            run_batch(client, plan_batch(client, items_of("A\nB\nC\n"), choose=pick(1)), folder, report=report)
            original = json.loads(report.read_text())
            self.assertEqual([item["status"] for item in original["items"]], ["downloaded", "error", "not_started"])
            candidate = {"reference": "5:h5", "title": "", "authors": "", "extension": "epub", "year": "",
                         "language": "", "size": ""}
            changes = [(index, {"reference": value}) for index in (0, 1) for value in ("5:h5", None)]
            changes += [(index, {"candidates": [candidate], "pick": 1}) for index in (0, 1)]
            for index, change in changes:
                document = json.loads(json.dumps(original))
                document["items"][index].update(change)
                report.write_text(json.dumps(document))
                with self.subTest(index=index, change=change):
                    for read in (load_report, parse_batch):
                        with self.assertRaises(BatchInputError) as caught:
                            read(report)
                        self.assertIn("downloaded or attempted already", str(caught.exception))
                        self.assertIn(f"Item {index + 1} of the report", str(caught.exception))
                        # The advice names a form that works: a copy of the item would only be a duplicate.
                        self.assertIn('{"reference": "ID:HASH"}', str(caught.exception))
            document = json.loads(json.dumps(original))
            document["items"][2]["reference"] = "5:h5"  # Not attempted yet: the choice may change.
            report.write_text(json.dumps(document))
            entry = load_report(report).entries[2]
            self.assertEqual((entry.reference, entry.book.title), ("5:h5", ""))
            again = BatchClient()
            plan = plan_batch(again, items_of("A\nB\nC\n"), choose=pick(1), previous=load_report(report), folder=folder)
            self.assertEqual([entry.status for entry in plan.entries], ["already_downloaded", "error", "planned"])
            self.assertEqual(plan.entries[2].reference, "5:h5")
            result = run_batch(again, plan, folder, report=report)
            self.assertEqual(again.kinds("download"), ["5:h5"])
            self.assertEqual(result.entries[2].book.title, "Book 5")

    def test_another_edition_of_a_downloaded_item_is_a_new_reference_item(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory).resolve()
            client = BatchClient({"dune": [book(1)]})
            document = run_batch(client, plan_batch(client, items_of("Dune\n"), choose=pick(1)), folder).to_dict()
            # The copy that the error message warns about, then the item it advises instead.
            copy = {**document["items"][0], "reference": "5:h5", "status": "planned", "book": None, "path": None}
            document["items"] += [copy, {"source": {"reference": "6:h6"}, "status": "planned"}]
            again = BatchClient()
            plan = plan_batch(again, parse_batch(json.dumps(document)), folder=folder)
            self.assertEqual([entry.status for entry in plan.entries], ["already_downloaded", "duplicate", "planned"])
            run_batch(again, plan, folder)
            self.assertEqual(again.kinds("download"), ["6:h6"])

    def test_a_recorded_download_without_a_usable_name_is_not_downloaded_again(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory).resolve()
            client = BatchClient({"a": [book(1)], "b": [book(2)]})
            document = run_batch(client, plan_batch(client, items_of("A\nB\n"), choose=pick(1)), folder).to_dict()
            document["items"][0]["path"] = None  # As written for a name the tool refused to record.
            for kind in (PLAN_KIND, REPORT_KIND):
                items = parse_batch(json.dumps({**document, "kind": kind}))
                again = BatchClient()
                with self.subTest(kind=kind):
                    plan = plan_batch(again, items, choose=pick(1), folder=folder)
                    self.assertEqual([(entry.status, entry.message) for entry in plan.entries],
                                     [("already_downloaded", MISSING_FILE), ("already_downloaded", None)])
                    self.assertEqual(again.calls, [])
                    redo = plan_batch(again, items, choose=pick(1), folder=folder, redownload_missing=True)
                    self.assertEqual([entry.status for entry in redo.entries], ["planned", "already_downloaded"])


class DownloadStageTests(unittest.TestCase):
    def client(self, *responses):
        transport = QueueTransport([{"response": {}}, {"success": 1, "user": {}}, *responses])
        client = Client(ORIGIN, transport=transport)
        client.login("synthetic", "synthetic")
        return client

    def test_link_refusals_and_post_link_failures_are_marked(self):
        target = book(123)
        target = type(target)(**{**target.to_dict(), "book_id": "123", "book_hash": "ab12", "extension": "pdf",
                                 "url": ORIGIN + "book/123/ab12"})
        file_reply = {"success": 1, "file": {"allowDownload": True, "extension": "pdf",
                                             "downloadLink": "https://cdn.example/file?token=" + SECRET}}
        cases = (({"success": 1, "file": {"allowDownload": False}}, "unavailable", "link"),
                 ({"success": 1, "file": {"allowDownload": True, "extension": "epub", "downloadLink": "https://cdn.example/f"}},
                  "format_mismatch", "link"),
                 ({"success": 0, "error": "synthetic refusal"}, "refused", "link"))
        with tempfile.TemporaryDirectory() as directory:
            for reply, reason, stage in cases:
                with self.subTest(reason=reason), self.client(reply) as client, self.assertRaises(DownloadError) as caught:
                    client.download(target, directory)
                self.assertEqual((caught.exception.reason, caught.exception.download_stage), (reason, stage))
            with self.client(file_reply, NetworkError("synthetic")) as client, self.assertRaises(NetworkError) as caught:
                client.download(target, directory)
            self.assertEqual(caught.exception.download_stage, "transfer")
            with self.client(file_reply, b"<html>quota</html>") as client, self.assertRaises(DownloadError) as caught:
                client.download(target, directory)
            self.assertEqual((caught.exception.reason, caught.exception.download_stage), ("invalid_book", "transfer"))
            with self.client() as client, patch("zlib_connector.book_files.tempfile.NamedTemporaryFile",
                                                 side_effect=OSError("synthetic")), self.assertRaises(DownloadError) as caught:
                client.download(target, directory)
            self.assertEqual(caught.exception.reason, "save_failed")
            self.assertFalse(hasattr(caught.exception, "download_stage"))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_a_real_client_batch_searches_downloads_and_records(self):
        record = {"id": 123, "hash": "ab12", "title": "Example", "author": "Author", "extension": "pdf"}
        client = self.client({"success": 1, "user": {"downloads_limit": 10, "downloads_today": 3}},
                             {"success": 1, "books": [record]},
                             {"success": 1, "user": {"downloads_limit": 10, "downloads_today": 3}},
                             {"success": 1, "file": {"allowDownload": True, "extension": "pdf",
                                                     "downloadLink": "https://cdn.example/file?token=" + SECRET}},
                             b"%PDF-1.7\nsynthetic",
                             {"success": 1, "user": {"downloads_limit": 10, "downloads_today": 4}})
        with tempfile.TemporaryDirectory() as directory, client:
            folder = Path(directory).resolve()
            quota = client.get_quota()
            plan = plan_batch(client, parse_batch("Example\n"), choose=pick(1))
            plan.quota = quota
            report = run_batch(client, plan, folder, report=folder / "progress.json")
            self.assertEqual(report.entries[0].status, "downloaded")
            self.assertEqual(report.entries[0].path.read_bytes(), b"%PDF-1.7\nsynthetic")
            self.assertEqual(report.quota.remaining, 6)
            self.assertNotIn(SECRET, (folder / "progress.json").read_text())


    def test_cancelling_a_real_transfer_removes_partial_files_and_records_the_item(self):
        record = {"id": 123, "hash": "ab12", "title": "Example", "author": "Author", "extension": "pdf"}
        content = b"%PDF-1.7\n" + b"x" * (3 * 65536)
        client = self.client({"success": 1, "books": [record]}, {"success": 1, "books": [{**record, "id": 456}]},
                             {"success": 1, "user": {}},
                             {"success": 1, "file": {"allowDownload": True, "extension": "pdf",
                                                     "downloadLink": "https://cdn.example/file?token=" + SECRET}},
                             content)
        token = CancellationToken()

        @contextmanager
        def cancelling(entry):
            def progress(update):
                if update.bytes_received:
                    token.cancel()
            yield progress

        with tempfile.TemporaryDirectory() as directory, client:
            folder = Path(directory).resolve()
            plan = plan_batch(client, parse_batch("Example\nOther\n"), choose=pick(1))
            report = run_batch(client, plan, folder, report=folder / "progress.json", progress_factory=cancelling,
                               cancel=token)
            self.assertTrue(report.cancelled)
            self.assertEqual([entry.status for entry in report.entries], ["cancelled", "not_started"])
            self.assertTrue(report.entries[0].quota_may_be_used)
            self.assertEqual(sorted(path.name for path in folder.iterdir()), ["progress.json"])
            self.assertEqual(json.loads((folder / "progress.json").read_text())["stopped"]["error_code"], "Cancelled")


class FolderAndLockTests(unittest.TestCase):
    def test_folders_must_accept_long_names_and_hard_links(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.assertEqual(check_folder(folder), folder.resolve())
            self.assertEqual(list(folder.iterdir()), [])
            with patch("zlib_connector.batch.os.link", side_effect=OSError("synthetic")), \
                    self.assertRaises(ValueError) as caught:
                check_folder(folder)
            self.assertIn("hard links", str(caught.exception))
            self.assertEqual(list(folder.iterdir()), [])
            with patch("zlib_connector.batch.os.open", side_effect=OSError("synthetic")), self.assertRaises(ValueError):
                check_folder(folder)
            with self.assertRaises(ValueError):
                check_folder(folder / "missing")

    def test_report_replacement_waits_briefly_for_a_windows_sharing_violation(self):
        attempts = []
        real = os.replace

        def busy(source, target, *, times):
            attempts.append(target)
            if len(attempts) <= times:
                raise PermissionError("synthetic sharing violation")
            return real(source, target)

        clock = Mock()
        clock.monotonic.side_effect = (step * 1.5 for step in range(10**6))
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory).resolve()
            report = run_batch(BatchClient(), plan_batch(BatchClient(), parse_batch("1:h1\n")), folder)
            with patch("zlib_connector.batch._sharing_violation", return_value=True), \
                    patch("zlib_connector.batch.os.replace", side_effect=lambda s, t: busy(s, t, times=2)):
                write_report(report, folder / "progress.json")
            self.assertEqual(len(attempts), 3)
            attempts.clear()
            with patch("zlib_connector.batch._sharing_violation", return_value=True), \
                    patch("zlib_connector.batch.time", clock), \
                    patch("zlib_connector.batch.os.replace", side_effect=lambda s, t: busy(s, t, times=10**6)):
                with self.assertRaises(PermissionError):
                    write_report(report, folder / "progress.json")
            self.assertEqual(len(attempts), 2)
            self.assertEqual(sorted(path.name for path in folder.iterdir() if path.suffix != ".epub"), ["progress.json"])
        with patch("zlib_connector.batch.os.replace", side_effect=PermissionError("synthetic")), \
                tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PermissionError):
                write_report(report, Path(directory) / "progress.json")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_one_run_at_a_time_per_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            with report_lock(path):
                with self.assertRaises(BatchReportError):
                    with report_lock(path):
                        pass
            with report_lock(path):
                pass

    def test_report_locks_need_no_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            lock = Path(directory) / "progress.json.lock"
            lock.write_bytes(b"")
            if os.name != "nt":
                lock.chmod(0o644)  # As a copied, synced or shared folder leaves it.
            refuse = ValueError("synthetic: not private")
            with patch("zlib_connector.session_lock.require_private_file", side_effect=refuse), \
                    patch("zlib_connector.private_files.require_private_file", side_effect=refuse):
                with report_lock(path):
                    with self.assertRaises(BatchReportError) as caught:
                        with report_lock(path):
                            pass
                    self.assertIn("Another batch is using this report", str(caught.exception))
            self.assertEqual(lock.read_bytes(), b"")

    def test_lock_paths_that_are_not_empty_files_are_refused_with_advice(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            lock = Path(directory) / "progress.json.lock"
            lock.mkdir()
            cases = [lambda: None, lambda: (lock.rmdir(), lock.write_bytes(b"synthetic"))]
            if os.name != "nt":
                target = Path(directory) / "target"
                target.write_bytes(b"")
                cases.append(lambda: (lock.unlink(), lock.symlink_to(target)))
            for prepare in cases:
                prepare()
                with self.subTest(lock=lock.is_symlink() or lock.is_dir() or lock.stat().st_size), \
                        self.assertRaises(BatchReportError) as caught:
                    with report_lock(path):
                        pass
                self.assertIn(f"Cannot lock {lock}.", str(caught.exception))
                self.assertIn("--report", str(caught.exception))
                self.assertIn("--no-report", str(caught.exception))
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
