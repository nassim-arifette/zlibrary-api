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
    from textual.command import CommandPalette
    from textual.widgets import DataTable, Input, Static
    from zlib_connector.tui.app import CollectionsApp, HelpScreen, Loaded
    from zlib_connector.tui.loader import LoadResult
    from zlib_connector.tui.views import detail_description, detail_fields, detail_header


def book(number, title=None, **fields):
    return Book(title or f"Synthetic book {number}", f"Author {number}", "epub", "", "2020", "English", "1 MB",
                str(number), "abc", **fields)


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
        return BookPage(() if self.empty else (book(99, "Previously downloaded"),), page, limit)

    def booklists(self, *, cancel=None):
        self.calls.append(("booklists", threading.get_ident()))
        cancel.check()
        if self.empty:
            return ()
        return (Booklist("1", "Weekend reading", book_count=None),
                Booklist("2", "Science", "Physics first.\nThen biology.\nThen more.", 2, False, "2026-01-02"))

    def booklist_books(self, identifier, *, page=1, cancel=None):
        self.calls.append(("members", identifier, page, threading.get_ident()))
        cancel.check()
        member = BooklistBook(**book(42).to_dict(), booklist_id=identifier, entry_id="8")
        return BookPage(() if self.empty else (member,), page, 20)

    def book(self, reference, *, cancel=None):
        self.calls.append(("book", reference, threading.get_ident()))
        cancel.check()
        number = int(reference.split(":")[0])
        return BookDetails(**book(number).to_dict(), description=("A synthetic description.\n" * 30), publisher="Test publisher")

    def search(self, query, *, page=1, limit=20, filters=None, cancel=None):
        self.calls.append(("search", query, page, limit, filters, threading.get_ident()))
        cancel.check()
        if self.error:
            raise NetworkError("secret synthetic server response")
        if self.empty:
            return BookPage((), page, limit, 0, 0)
        books = tuple(book(number, f"{query} result {number}") for number in range((page - 1) * limit + 1, page * limit + 1))
        return BookPage(books, page, limit, 3 * limit, 3, page + 1 if page < 3 else None, page - 1 if page > 1 else None)

    def popular(self, *, cancel=None):
        self.calls.append(("popular", threading.get_ident()))
        cancel.check()
        return () if self.empty else tuple(book(number, f"Popular book {number}") for number in range(200, 230))

    def recommended(self, *, cancel=None):
        self.calls.append(("recommended", threading.get_ident()))
        cancel.check()
        return () if self.empty else tuple(book(number, f"Recommended book {number}") for number in range(300, 305))

    def similar(self, reference, *, cancel=None):
        self.calls.append(("similar", reference, threading.get_ident()))
        cancel.check()
        number = int(reference.split(":")[0])
        return () if self.empty else tuple(book((number * 10 + offset) % 10**9, f"Similar to {number} #{offset}")
                                           for offset in range(1, 4))


def text(app, selector):
    """The plain text a page widget currently shows."""
    return str(app.query_one(selector).render())


class TUITestCase(unittest.IsolatedAsyncioTestCase):
    async def until(self, pilot, condition, message="The synthetic view did not update."):
        for _ in range(300):
            await pilot.pause(.01)
            if condition():
                return
        self.fail(message)

    async def ready(self, app, pilot):
        await self.until(pilot, lambda: not app.loading, "The synthetic view did not finish loading.")


@unittest.skipUnless(TEXTUAL_AVAILABLE, "Install the optional Textual dependency to test the TUI.")
class CollectionsTests(TUITestCase):
    async def test_home_is_the_default_start_and_reads_nothing(self):
        api = FakeAPI()
        app = CollectionsApp(api)
        self.assertEqual([], api.calls)
        self.assertFalse(app._loader.alive)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(.1)
            self.assertEqual(("home", "home"), (app.view.root, app.view.kind))
            self.assertTrue(app.query_one("#home").display)
            self.assertFalse(app.query_one("#header").display)
            self.assertEqual("/ commands · s search · ? help · q quit", text(app, "#hints"))
            entries = [entry.command_name for entry in app.query(".home-entry")]
            self.assertEqual(["/search", "/popular", "/recommended", "/favorites", "/booklists", "/history"], entries)
            await pilot.press("escape", "escape")
            await pilot.pause()
            self.assertEqual("home", app.view.kind)
            self.assertEqual([], api.calls)
        self.assertFalse(app._loader.alive)

    async def test_navigation_restores_page_row_and_scroll(self):
        api = FakeAPI()
        app = CollectionsApp(api, initial_view="favorites")
        self.assertEqual([], api.calls)
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
            self.assertIn("Z-Library account history", text(app, "#status"))
            before = len(api.calls)
            await pilot.press("n", "right")
            await pilot.pause()
            self.assertEqual(before, len(api.calls))
            await pilot.press("f")
            await pilot.pause()
            self.assertEqual(2, app.view.value.page)
            self.assertEqual(15, table.cursor_row)
            # Esc at a section's first view goes Home; the section keeps its page and row.
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual("home", app.view.kind)
            await pilot.press("1")
            await pilot.pause()
            self.assertEqual((2, 15), (app.view.value.page, table.cursor_row))
            self.assertEqual(2, sum(call[0] == "favorites" for call in api.calls))
        self.assertFalse(app._loader.alive)

    async def test_booklists_members_details_and_back_keep_only_bounded_state(self):
        api = FakeAPI()
        app = CollectionsApp(api, initial_view="booklists")
        async with app.run_test() as pilot:
            await self.ready(app, pilot)
            table = app.query_one("#collection", DataTable)
            # Unknown counts and visibility show a dim dash rather than a word.
            row = table.get_row_at(0)
            self.assertEqual(["1", "Weekend reading", "–", "–"], [cell.plain for cell in row])
            self.assertEqual(["dim", "dim"], [str(cell.style) for cell in row[2:]])
            self.assertEqual(["2", "Private"], [cell.plain for cell in table.get_row_at(1)[2:]])
            await pilot.press("down", "enter")
            await self.ready(app, pilot)
            self.assertEqual(("members", "2"), (app.view.kind, app.view.identifier))
            self.assertEqual("Booklists › Science", text(app, "#view-title"))
            await pilot.press("enter")
            await self.ready(app, pilot)
            self.assertEqual("details", app.view.kind)
            self.assertEqual(3, len(app._roots["booklists"]))
            self.assertEqual("Booklists › Science › Book details", text(app, "#status"))
            await pilot.press("escape", "escape")
            await pilot.pause()
            self.assertEqual("booklists", app.view.kind)
            self.assertEqual(1, table.cursor_row)
            self.assertEqual(1, sum(call[0] == "booklists" for call in api.calls))

    async def test_error_empty_retry_and_explicit_pagination(self):
        api = FakeAPI()
        api.error = True
        app = CollectionsApp(api, initial_view="favorites")
        async with app.run_test() as pilot:
            await self.ready(app, pilot)
            self.assertNotIn("secret", app.view.error)
            table, message = app.query_one("#collection", DataTable), app.query_one("#message", Static)
            self.assertFalse(table.display)
            self.assertFalse(app.query_one("#card").display)
            self.assertTrue(message.display)
            self.assertTrue(text(app, "#message").startswith("✗ Could not load favorites\nCould not reach the server."))
            self.assertNotIn("secret", text(app, "#message"))
            self.assertEqual("r retry · esc back · / commands", text(app, "#hints"))
            before = len(api.calls)
            await pilot.press("n", "enter", "m")
            await pilot.pause()
            self.assertEqual(before, len(api.calls))
            api.error, api.empty = False, True
            await pilot.press("r")
            await self.ready(app, pilot)
            self.assertEqual("No books in your favorites yet.", text(app, "#message"))
            api.empty = False
            await pilot.press("r")
            await self.ready(app, pilot)
            self.assertIsNone(app.view.error)
            self.assertTrue(table.display)
            self.assertFalse(message.display)
            await pilot.press("h")
            await self.ready(app, pilot)
            # Without a reported total, the page stays unknown and paging cannot guess.
            self.assertEqual("Page 1 · Book 1 of 1 · Z-Library account history", text(app, "#status"))
            before = len(api.calls)
            await pilot.press("n", "p", "left", "right")
            await pilot.pause()
            self.assertEqual(before, len(api.calls))

    async def test_metadata_is_literal_without_escape_codes_links_or_bidi(self):
        api = FakeAPI()
        hostile = "[bold][link=https://bad.invalid]Title[/link]\x1b[31m‮"
        api.favorites = lambda **kwargs: BookPage((book(1, hostile, is_favorite=True),), 1, 20)
        app = CollectionsApp(api, initial_view="favorites")
        async with app.run_test() as pilot:
            await self.ready(app, pilot)
            cell = app.query_one("#collection", DataTable).get_row_at(0)[1]
            self.assertIsInstance(cell, Text)
            self.assertIn("[bold][link=", cell.plain)
            self.assertEqual([], cell.spans)
            self.assertNotIn("\x1b", cell.plain)
            self.assertNotIn("‮", cell.plain)
            card = app.query_one("#card").render()
            self.assertTrue(card.plain.startswith("[bold][link=https://bad.invalid]Title[/link]\n"))
            self.assertIn("★ Saved", card.plain)
            self.assertFalse(any("link" in str(span.style) or "@click" in str(span.style) for span in card.spans))
            api.book = lambda reference, **kwargs: BookDetails(**book(1).to_dict(), description="[red]Literal[/red]\x1b]8;;https://bad.invalid\x07link\x1b]8;;\x07")
            await pilot.press("enter")
            await self.ready(app, pilot)
            content = app.query_one("#detail-description", Static).render()
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
        app = CollectionsApp(api, initial_view="favorites")
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

    async def test_going_home_cancels_the_read_and_leaving_home_reloads_it(self):
        api = FakeAPI()
        started, release, tokens = threading.Event(), threading.Event(), []

        def delayed(**kwargs):
            tokens.append(kwargs["cancel"])
            started.set()
            release.wait()
            return BookPage((book(1, "slow"),), 1, 20)

        api.favorites = delayed
        app = CollectionsApp(api, initial_view="favorites")
        try:
            async with app.run_test() as pilot:
                await self.until(pilot, started.is_set)
                generation = app._generation
                await pilot.press("escape")
                await pilot.pause()
                self.assertEqual("home", app.view.kind)
                self.assertTrue(tokens[0].is_cancelled)
                self.assertFalse(app.loading)
                app.post_message(Loaded(LoadResult(generation, BookPage((book(1, "late"),), 1, 20))))
                await pilot.pause()
                self.assertIsNone(app._roots["favorites"][0].value)
                release.set()
                api.favorites = FakeAPI().favorites
                await pilot.press("f")
                await self.ready(app, pilot)
                self.assertEqual("Synthetic book 1", app.view.value.books[0].title)
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
        for key in ("q", "ctrl+c"):
            with self.subTest(key=key):
                tokens.clear()
                stopped.clear()
                app = CollectionsApp(api, initial_view="favorites")
                async with app.run_test() as pilot:
                    await pilot.pause(.05)
                    await pilot.press(key)
                self.assertTrue(tokens[0].is_cancelled)
                self.assertTrue(stopped.is_set())
                self.assertFalse(app._loader.alive)

    async def test_ctrl_c_copies_selected_text_instead_of_quitting(self):
        app = CollectionsApp(FakeAPI(), initial_view="search")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press(*"dune")
            query = app.query_one("#search-query", Input)
            query.action_select_all()
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertTrue(app.is_running)
            self.assertEqual("dune", app.clipboard)
            # Without a selection, Ctrl+C quits, even from a text field.
            query.action_end()
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertTrue(app._stopping)
        self.assertFalse(app._loader.alive)

    async def test_help_commands_small_resize_and_scrollable_details(self):
        app = CollectionsApp(FakeAPI(), initial_view="favorites")
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            await pilot.resize_terminal(60, 18)
            await pilot.pause()
            hints = app.query_one("#hints")
            self.assertEqual((17, 1), (hints.region.y, hints.region.height))
            self.assertLessEqual(hints.region.right, 60)
            page_hints = text(app, "#hints")
            # While a dialog is open, the key hints are the dialog's (the help shows them in its last line);
            # closing it brings the page's back.
            for key in ("question_mark", "f1"):
                await pilot.press(key)
                self.assertIsInstance(app.screen, HelpScreen)
                await pilot.press("pagedown")
                await pilot.pause()
                self.assertGreater(app.focused.scroll_y, 0)
                self.assertEqual(("", "↑↓ scroll · esc close"), (text(app, "#hints"), text(app.screen, "#help-close")))
                await pilot.press(key)
                await pilot.pause()
                self.assertNotIsInstance(app.screen, HelpScreen)
                self.assertEqual(page_hints, text(app, "#hints"))
            for key in ("slash", "ctrl+p"):
                await pilot.press(key)
                await pilot.pause()
                self.assertIsInstance(app.screen, CommandPalette)
                self.assertEqual("↑↓ choose · enter run · esc close", text(app, "#hints"))
                await pilot.press("escape")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, CommandPalette)
                self.assertEqual(page_hints, text(app, "#hints"))
            await pilot.press("enter")
            await self.ready(app, pilot)
            self.assertEqual("details", app.view.kind)
            self.assertEqual(("Book details", "Favorites › Book details"), (text(app, "#view-title"), text(app, "#status")))
            # The long description scrolls, so ↑↓ is offered, then gives way to the narrow terminal first.
            await self.until(pilot, lambda: text(app, "#hints") == "m more like this · r refresh · esc back · / commands · …")
            await pilot.press("pagedown")
            await pilot.pause()
            self.assertGreater(app.query_one("#detail-view").scroll_y, 0)
            await pilot.press("escape")
            self.assertEqual("books", app.view.kind)

    def test_no_color_and_themes_use_textual_support(self):
        with patch.dict("os.environ", {"NO_COLOR": "1"}):
            app = CollectionsApp(FakeAPI())
        self.assertTrue(app.no_color)
        self.assertEqual("tokyo-night", app.theme)
        app.theme = "textual-light"
        self.assertEqual("textual-light", app.theme)

    def test_details_preserve_the_adapters_full_validated_metadata_limits(self):
        title, authors, publisher = "T" * 4095 + "X", "A" * 4095 + "Y", "P" * 4095 + "Z"
        description = "D" * 65535 + "!"
        categories = tuple("C" * 253 + str(number).zfill(3) for number in range(100))
        details = BookDetails(title, authors, "epub", "", book_id="1", book_hash="abc",
                              publisher=publisher, description=description, categories=categories)
        self.assertEqual((title, authors), detail_header(details)[:2])
        fields = dict(detail_fields(details))
        self.assertEqual(publisher, fields["Publisher"])
        self.assertEqual(", ".join(categories), fields["Categories"])
        self.assertEqual("1:abc", fields["Reference"])
        self.assertEqual(description, detail_description(details))
        self.assertNotIn("Saved", fields)

    def test_invalid_configuration_fails_before_any_read(self):
        api = FakeAPI()
        for options in ({"initial_view": "unknown"}, {"page_size": 0}, {"page_size": 51}):
            with self.assertRaises(ValueError):
                CollectionsApp(api, **options)
        self.assertEqual([], api.calls)


if __name__ == "__main__":
    unittest.main()
