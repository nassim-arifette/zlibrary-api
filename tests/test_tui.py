"""Headless keyboard navigation against synthetic collection data."""
import importlib.util
import threading
import time
import unittest
from unittest.mock import patch

from zlib_connector.booklists import Booklist, BooklistBook
from zlib_connector.books import BookDetails
from zlib_connector.client import Book
from zlib_connector.errors import NetworkError
from zlib_connector.pagination import BookPage


TEXTUAL_AVAILABLE = importlib.util.find_spec("textual") is not None
if TEXTUAL_AVAILABLE:
    from rich.text import Text
    from textual.widgets import Button, DataTable, Footer, Static
    from zlib_connector.tui.app import CollectionsApp, HelpScreen, Loaded
    from zlib_connector.tui.loader import LoadResult
    from zlib_connector.tui.views import details_text


def book(number, title=None):
    return Book(title or f"Synthetic book {number}", f"Author {number}", "epub", "", "2020", "English", "1 MB", str(number), "abc")


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.error = False
        self.empty = False

    def favorites(self, *, page=1, limit=20, cancel=None):
        self.calls.append(("favorites", page, limit, threading.get_ident()))
        cancel.check()
        if self.error:
            raise NetworkError("secret synthetic server response")
        books = () if self.empty else tuple(book(number) for number in range((page - 1) * limit + 1, page * limit + 1))
        return BookPage(books, page, limit, 100, 5, page + 1 if page < 5 else None, page - 1 if page > 1 else None)

    def history(self, *, page=1, limit=20, cancel=None):
        self.calls.append(("history", page, limit, threading.get_ident()))
        cancel.check()
        return BookPage((book(99, "Previously downloaded"),), page, limit)

    def booklists(self, *, cancel=None):
        self.calls.append(("booklists", threading.get_ident()))
        cancel.check()
        return (Booklist("1", "Weekend reading", book_count=None), Booklist("2", "Science", book_count=2, is_public=False))

    def booklist_books(self, identifier, *, page=1, cancel=None):
        self.calls.append(("members", identifier, page, threading.get_ident()))
        cancel.check()
        member = BooklistBook(**book(42).to_dict(), booklist_id=identifier, entry_id="8")
        return BookPage((member,), page, 20)

    def book(self, reference, *, cancel=None):
        self.calls.append(("book", reference, threading.get_ident()))
        cancel.check()
        number = int(reference.split(":")[0])
        return BookDetails(**book(number).to_dict(), description=("A synthetic description.\n" * 30), publisher="Test publisher")


@unittest.skipUnless(TEXTUAL_AVAILABLE, "Install the optional Textual dependency to test the TUI.")
class CollectionsTests(unittest.IsolatedAsyncioTestCase):
    async def ready(self, app, pilot):
        for _ in range(120):
            await pilot.pause(.01)
            if not app.loading:
                return
        self.fail("The synthetic view did not finish loading.")

    async def test_initialization_is_offline_and_navigation_restores_page_row_and_scroll(self):
        api = FakeAPI()
        app = CollectionsApp(api)
        self.assertEqual([], api.calls)
        self.assertFalse(app._loader.alive)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            self.assertEqual("favorites", app.view.root)
            self.assertNotEqual(threading.get_ident(), api.calls[0][-1])
            await pilot.press("n")
            await self.ready(app, pilot)
            self.assertEqual(2, app.view.value.page)
            table = app.query_one("#collection", DataTable)
            table.move_cursor(row=15)
            await pilot.pause()
            old_scroll = table.scroll_y
            self.assertGreater(old_scroll, 0)
            await pilot.press("enter")
            await self.ready(app, pilot)
            self.assertEqual("details", app.view.kind)
            self.assertIn(("book", "36:abc"), [call[:2] for call in api.calls])
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(2, app.view.value.page)
            self.assertEqual(15, table.cursor_row)
            self.assertEqual(old_scroll, table.scroll_y)
            await pilot.press("h")
            await self.ready(app, pilot)
            self.assertEqual("history", app.view.root)
            self.assertIn("Z-Library account", str(app.query_one("#status", Static).render()))
            self.assertTrue(app.query_one("#next", Button).disabled)
            await pilot.press("f")
            await pilot.pause()
            self.assertEqual(2, app.view.value.page)
            self.assertEqual(15, table.cursor_row)
            self.assertEqual(2, sum(call[0] == "favorites" for call in api.calls))
        self.assertFalse(app._loader.alive)

    async def test_booklists_members_details_and_back_keep_only_bounded_state(self):
        api = FakeAPI()
        app = CollectionsApp(api, initial_view="booklists")
        async with app.run_test() as pilot:
            await self.ready(app, pilot)
            table = app.query_one("#collection", DataTable)
            self.assertEqual("Unknown", table.get_row_at(0)[1])
            await pilot.press("down", "enter")
            await self.ready(app, pilot)
            self.assertEqual(("members", "2"), (app.view.kind, app.view.identifier))
            await pilot.press("enter")
            await self.ready(app, pilot)
            self.assertEqual("details", app.view.kind)
            self.assertEqual(3, len(app._roots["booklists"]))
            await pilot.press("escape", "escape")
            await pilot.pause()
            self.assertEqual("booklists", app.view.kind)
            self.assertEqual(1, table.cursor_row)
            self.assertEqual(1, sum(call[0] == "booklists" for call in api.calls))

    async def test_error_empty_refresh_and_explicit_pagination(self):
        api = FakeAPI()
        api.error = True
        app = CollectionsApp(api)
        async with app.run_test() as pilot:
            await self.ready(app, pilot)
            self.assertNotIn("secret", app.view.error)
            self.assertTrue(app.query_one("#next", Button).disabled)
            self.assertTrue(app.query_one("#collection", DataTable).disabled)
            before = len(api.calls)
            await pilot.press("n", "enter")
            await pilot.pause()
            self.assertEqual(before, len(api.calls))
            api.error, api.empty = False, True
            await pilot.press("r")
            await self.ready(app, pilot)
            self.assertIn("No books", str(app.query_one("#status", Static).render()))
            api.empty = False
            await pilot.click("#refresh")
            await self.ready(app, pilot)
            self.assertIsNone(app.view.error)
            await pilot.press("h")
            await self.ready(app, pilot)
            self.assertIn("?", str(app.query_one("#page-label", Static).render()))
            before = len(api.calls)
            await pilot.press("n", "p")
            await pilot.pause()
            self.assertEqual(before, len(api.calls))

    async def test_metadata_is_literal_without_escape_codes_links_or_bidi(self):
        api = FakeAPI()
        api.favorites = lambda **kwargs: BookPage((book(1, "[bold][link=https://bad.invalid]Title[/link]\x1b[31m\u202e"),), 1, 20)
        app = CollectionsApp(api)
        async with app.run_test() as pilot:
            await self.ready(app, pilot)
            cell = app.query_one("#collection", DataTable).get_row_at(0)[0]
            self.assertIsInstance(cell, Text)
            self.assertIn("[bold][link=", cell.plain)
            self.assertEqual([], cell.spans)
            self.assertNotIn("\x1b", cell.plain)
            self.assertNotIn("\u202e", cell.plain)
            api.book = lambda reference, **kwargs: BookDetails(**book(1).to_dict(), description="[red]Literal[/red]\x1b]8;;https://bad.invalid\x07link\x1b]8;;\x07")
            await pilot.press("enter")
            await self.ready(app, pilot)
            content = app.query_one("#detail-text", Static).render()
            self.assertIn("[red]Literal[/red]link", content.plain)
            self.assertFalse(any(getattr(span.style, "link", None) for span in content.spans))

    async def test_delayed_obsolete_read_is_cancelled_and_late_message_cannot_replace_current_view(self):
        api = FakeAPI()
        started, release = threading.Event(), threading.Event()
        tokens = []

        def delayed(**kwargs):
            tokens.append(kwargs["cancel"])
            started.set()
            # Only the test releases this read. A wall-clock fallback could
            # complete it before a slow CI runner finishes the key presses.
            release.wait()
            return BookPage((book(1, "obsolete"),), 1, 20)

        api.favorites = delayed
        app = CollectionsApp(api)
        try:
            async with app.run_test() as pilot:
                await pilot.pause(.05)
                self.assertTrue(started.is_set())
                generation = app._generation
                await pilot.press("h", "b", "h")
                await pilot.pause()
                self.assertTrue(tokens[0].is_cancelled)
                self.assertEqual("history", app.view.root)
                self.assertTrue(app.loading)
                release.set()
                await self.ready(app, pilot)
                self.assertEqual("Previously downloaded", app.view.value.books[0].title)
                app.post_message(Loaded(LoadResult(generation, BookPage((book(1, "late secret"),), 1, 20))))
                await pilot.pause()
                self.assertEqual("Previously downloaded", app.view.value.books[0].title)
                self.assertEqual(["history"], [call[0] for call in api.calls])
        finally:
            release.set()

    async def test_quit_signals_cancellation_and_reaps_cooperative_worker(self):
        api = FakeAPI()
        tokens, stopped = [], threading.Event()

        def waiting(**kwargs):
            cancel = kwargs["cancel"]
            tokens.append(cancel)
            try:
                while not cancel.is_cancelled:
                    time.sleep(.005)
                cancel.check()
            finally:
                stopped.set()

        api.favorites = waiting
        app = CollectionsApp(api)
        async with app.run_test() as pilot:
            await pilot.pause(.05)
            await pilot.press("q")
        self.assertTrue(tokens[0].is_cancelled)
        self.assertTrue(stopped.is_set())
        self.assertFalse(app._loader.alive)

    async def test_help_palette_small_resize_and_scrollable_details(self):
        app = CollectionsApp(FakeAPI())
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            await pilot.resize_terminal(60, 18)
            await pilot.pause()
            footer = app.query_one(Footer)
            self.assertLessEqual(footer.region.bottom, 18)
            refresh = app.query_one("#refresh", Button)
            self.assertEqual(11, refresh.region.width)
            self.assertGreaterEqual(refresh.content_size.width, len("Refresh"))
            self.assertEqual(1, sum(key.key == "ctrl+p" for key in footer.query("FooterKey")))
            for selector in ("#favorites", "#booklists", "#history", "#back", "#refresh"):
                widget = app.query_one(selector)
                self.assertGreater(widget.region.width, 0)
                self.assertLessEqual(widget.region.right, 60)
                self.assertLessEqual(widget.region.bottom, 18)
            await pilot.press("question_mark")
            self.assertIsInstance(app.screen, HelpScreen)
            await pilot.press("escape", "ctrl+p")
            await pilot.pause()
            self.assertEqual("CommandPalette", type(app.screen).__name__)
            await pilot.press("escape", "enter")
            await self.ready(app, pilot)
            self.assertEqual("details", app.view.kind)
            self.assertIn("Scroll for details", str(app.query_one("#status", Static).render()))
            self.assertNotIn("Enter opens", str(app.query_one("#status", Static).render()))
            await pilot.press("pagedown")
            await pilot.pause()
            self.assertGreater(app.query_one("#detail-view").scroll_y, 0)
            await pilot.press("escape")
            self.assertEqual("books", app.view.kind)

    def test_no_color_and_themes_use_textual_support(self):
        with patch.dict("os.environ", {"NO_COLOR": "1"}):
            app = CollectionsApp(FakeAPI())
        self.assertTrue(app.no_color)
        app.theme = "textual-light"
        self.assertEqual("textual-light", app.theme)

    def test_details_preserve_the_adapters_full_validated_metadata_limits(self):
        title, authors, publisher = "T" * 4095 + "X", "A" * 4095 + "Y", "P" * 4095 + "Z"
        description = "D" * 65535 + "!"
        categories = tuple("C" * 253 + str(number).zfill(3) for number in range(100))
        details = BookDetails(title, authors, "epub", "", book_id="1", book_hash="abc",
                              publisher=publisher, description=description, categories=categories)
        rendered = details_text(details).plain
        for value in (title, authors, publisher, ", ".join(categories)):
            self.assertIn(value, rendered)
        self.assertTrue(rendered.endswith(description))

    def test_invalid_configuration_fails_before_any_read(self):
        api = FakeAPI()
        for options in ({"initial_view": "unknown"}, {"page_size": 0}, {"page_size": 51}):
            with self.assertRaises(ValueError):
                CollectionsApp(api, **options)
        self.assertEqual([], api.calls)
