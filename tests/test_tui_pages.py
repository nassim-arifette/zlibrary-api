"""Page structure of the TUI: Home, commands, the shared book list, details and small terminals."""
import importlib.util
import threading
import unittest

from zlib_connector.booklists import Booklist
from zlib_connector.books import BookDetails
from zlib_connector.client import Book
from zlib_connector.pagination import BookPage
from zlib_connector.search import SearchFilters

from test_tui import FakeAPI, TUITestCase, book, text


TEXTUAL_AVAILABLE = importlib.util.find_spec("textual") is not None
if TEXTUAL_AVAILABLE:
    from textual import events
    from textual.command import CommandInput, CommandList, CommandPalette
    from textual.geometry import Region
    from textual.widgets import DataTable, Input
    from zlib_connector.tui.app import CollectionsApp, HelpScreen
    from zlib_connector.tui.search import SearchForm
    from zlib_connector.tui.views import (SearchRequest, ViewState, fit_summary, position_text, split_columns,
                                          status_extras)
    from zlib_connector.tui.widgets import hint_line


ALL_COMMANDS = ["/search", "/favorites", "/booklists", "/history", "/popular", "/recommended", "/similar",
                "/refresh", "/home", "/help", "/settings", "/theme", "/quit"]


def listed(app) -> list[str]:
    """The command names the open command dialog shows."""
    if not isinstance(app.screen, CommandPalette):
        return []
    options = app.screen.query_one(CommandList)
    return [options.get_option_at_index(index).hit.text for index in range(options.option_count)]


@unittest.skipUnless(TEXTUAL_AVAILABLE, "Install the optional Textual dependency to test the TUI.")
class PageTests(TUITestCase):
    async def commands(self, app, pilot, count=None):
        """Open the command dialog and return the command names it lists."""
        await pilot.press("slash")
        await self.until(pilot, lambda: listed(app) and (count is None or len(listed(app)) == count))
        await pilot.pause(.3)
        return listed(app)

    async def run_command(self, app, pilot, typed, name):
        """Open the dialog, filter it until name is the only match, then press Enter."""
        await self.commands(app, pilot)
        await pilot.press(*typed)
        await self.until(pilot, lambda: listed(app) == [name])
        # A slash typed in the dialog goes into its input.
        self.assertEqual(typed, app.screen.query_one(CommandInput).value)
        await pilot.press("enter")

    async def test_command_dialog_lists_only_our_commands_and_runs_a_filtered_one(self):
        api = FakeAPI()
        app = CollectionsApp(api)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            names = await self.commands(app, pilot)
            # Home has no selected book and no data to reload.
            self.assertEqual([name for name in ALL_COMMANDS if name not in ("/similar", "/refresh")], names)
            await pilot.press(*"pop")
            await self.until(pilot, lambda: listed(app)[:1] == ["/popular"])
            await pilot.press("enter")
            await self.until(pilot, lambda: app.view.kind == "popular" and not app.loading)
            self.assertNotIsInstance(app.screen, CommandPalette)
            self.assertEqual(["popular"], [call[0] for call in api.calls])
            table = app.query_one("#collection", DataTable)
            table.move_cursor(row=5)
            await pilot.pause()
            names = await self.commands(app, pilot, count=len(ALL_COMMANDS))
            self.assertEqual(ALL_COMMANDS, names)
            for system in ("Keys", "Screenshot", "Maximize", "Theme", "Quit"):
                self.assertNotIn(system, names)
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, CommandPalette)
            self.assertEqual(("popular", 5), (app.view.kind, table.cursor_row))
            self.assertTrue(table.has_focus)
            await self.run_command(app, pilot, "/simi", "/similar")
            await self.until(pilot, lambda: app.view.kind == "similar" and not app.loading)
            self.assertEqual(("similar", "205:abc"), api.calls[-1][:2])
            await self.run_command(app, pilot, "/home", "/home")
            await self.until(pilot, lambda: app.view.kind == "home")
            await self.run_command(app, pilot, "/help", "/help")
            await self.until(pilot, lambda: isinstance(app.screen, HelpScreen))

    async def test_section_commands_run_from_the_search_form(self):
        api = FakeAPI()
        app = CollectionsApp(api, initial_view="search")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+p")
            await self.until(pilot, lambda: isinstance(app.screen, CommandPalette))
            await pilot.press(*"/favo")
            await self.until(pilot, lambda: listed(app)[:1] == ["/favorites"])
            await pilot.press("enter")
            await self.until(pilot, lambda: app.view.kind != "form" and not app.loading)
            self.assertEqual("favorites", app.view.root)
            await pilot.press("s")
            await self.until(pilot, lambda: app.view.kind == "form")
            await pilot.press("f")
            await pilot.pause()
            self.assertEqual("form", app.view.kind)  # typed keys still never trigger shortcuts on the form

    async def test_keys_typed_straight_after_slash_reach_the_command_dialog(self):
        api = FakeAPI()
        app = CollectionsApp(api)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            for start, typed, kind in (("home", "pop", "popular"), ("popular", "hist", "books")):
                with self.subTest(start=start):
                    # A fast roll or a keyboard macro: every key is queued before the dialog opens or lists anything.
                    for key, character in (("slash", "/"), *((letter, letter) for letter in typed), ("enter", "\r")):
                        app.post_message(events.Key(key, character))
                    await self.until(pilot, lambda: app.view.kind == kind and not app.loading)
                    self.assertNotIsInstance(app.screen, CommandPalette)
            self.assertEqual(["popular", "history"], [call[0] for call in api.calls])
            # Esc right after the text closes the dialog without running anything.
            for key, character in (("slash", "/"), ("p", "p"), ("escape", "\x1b")):
                app.post_message(events.Key(key, character))
            await self.until(pilot, lambda: len(app.screen_stack) == 1)
            await pilot.pause(.2)
            self.assertEqual(("history", 1, 2), (app.view.root, len(app.screen_stack), len(api.calls)))

    async def test_dialogs_show_their_own_keys_and_give_the_page_hints_back(self):
        app = CollectionsApp(FakeAPI())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            home = text(app, "#hints")
            await self.commands(app, pilot)
            self.assertEqual("↑↓ choose · enter run · esc close", text(app, "#hints"))
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(home, text(app, "#hints"))
            # A command that opens another dialog hands the hints over to it.
            await self.run_command(app, pilot, "/theme", "/theme")
            await self.until(pilot, lambda: isinstance(app.screen, CommandPalette) and app.screen.id == "--theme-palette")
            await pilot.pause()
            self.assertEqual("↑↓ choose · enter apply · esc close", text(app, "#hints"))
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(home, text(app, "#hints"))
            # Help names its keys in its own last line, ↑↓ only while it needs to scroll; the bottom line stays empty
            # instead of repeating them beside the box. `/` does nothing there.
            await pilot.press("question_mark")
            await self.until(pilot, lambda: text(app.screen, "#help-close") == "↑↓ scroll · esc close")
            self.assertEqual("", text(app, "#hints"))
            await pilot.press("slash")
            await pilot.pause()
            self.assertEqual((2, HelpScreen), (len(app.screen_stack), type(app.screen)))
            await pilot.resize_terminal(100, 60)
            await self.until(pilot, lambda: text(app.screen, "#help-close") == "esc close")
            self.assertEqual(0, app.screen.query_one("#help-scroll").max_scroll_y)
            self.assertEqual("", text(app, "#hints"))
            await pilot.resize_terminal(80, 24)
            await self.until(pilot, lambda: text(app.screen, "#help-close") == "↑↓ scroll · esc close")
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(home, text(app, "#hints"))
            # A command that changes the page shows that page's keys.
            await self.run_command(app, pilot, "/pop", "/popular")
            await self.until(pilot, lambda: app.view.kind == "popular" and not app.loading)
            await pilot.pause()
            self.assertEqual("↑↓ select · enter details · m more like this · esc back · / commands", text(app, "#hints"))

    async def test_results_keep_edit_search_in_the_hints_at_common_sizes(self):
        expected = {100: "↑↓ select · ←→ page · enter details · m more like this · s edit search · esc back · / commands",
                    80: "←→ page · enter details · s edit search · esc back · / commands · …",
                    60: "enter details · s edit search · esc back · / commands · …"}
        api = FakeAPI()
        app = CollectionsApp(api, initial_view="search")
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.post_message(SearchForm.Submitted(SearchRequest("dune")))
            await self.until(pilot, lambda: app.view.kind == "results" and not app.loading)
            for width, height in ((100, 30), (80, 24), (60, 18)):
                with self.subTest(width=width):
                    await pilot.resize_terminal(width, height)
                    await pilot.pause()
                    self.assertEqual(expected[width], text(app, "#hints"))
            # A rejected search offers the form, filled in with it, before a retry of the same request.
            api.error = True
            await pilot.press("r")
            await self.ready(app, pilot)
            self.assertEqual("s edit search · r retry · esc back · / commands", text(app, "#hints"))
            await pilot.press("s")
            await pilot.pause()
            self.assertEqual(("form", "dune"), (app.view.kind, app.query_one("#search-query", Input).value))

    async def test_home_entries_open_their_section_when_clicked(self):
        api = FakeAPI()
        app = CollectionsApp(api)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            entry = next(entry for entry in app.query(".home-entry") if entry.command_name == "/favorites")
            await pilot.click(entry)
            await self.until(pilot, lambda: app.view.root == "favorites" and not app.loading)
            await pilot.press("escape")
            await pilot.pause()
            entry = next(entry for entry in app.query(".home-entry") if entry.command_name == "/search")
            await pilot.click(entry)
            await pilot.pause()
            self.assertEqual("form", app.view.kind)
            self.assertEqual(["favorites"], [call[0] for call in api.calls])

    async def test_a_row_is_selected_by_a_click_and_opened_by_a_second_click(self):
        api = FakeAPI()
        app = CollectionsApp(api, initial_view="favorites")
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            table = app.query_one("#collection", DataTable)
            # The header takes the table's first line; the third book is on its fourth.
            await pilot.click(table, offset=(10, 3))
            await pilot.pause()
            self.assertEqual(("books", 2), (app.view.kind, table.cursor_row))
            self.assertTrue(str(app.query_one("#card").render()).startswith("Synthetic book 3\n"))
            await pilot.click(table, offset=(10, 3))
            await self.ready(app, pilot)
            self.assertEqual(("details", "3:abc"), (app.view.kind, app.view.identifier))
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(("books", 2), (app.view.kind, table.cursor_row))

    async def test_escape_returns_home_from_every_section_and_keeps_it_there(self):
        api = FakeAPI()
        app = CollectionsApp(api)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            for keys, depth in ((("f", "enter"), 2), (("b", "enter", "enter"), 3), (("h", "enter"), 2),
                                (("5", "enter", "m"), 3), (("6", "m"), 2), (("s",), 1)):
                with self.subTest(keys=keys):
                    for key in keys:
                        await pilot.press(key)
                        await self.ready(app, pilot)
                    self.assertEqual(depth, 1 if app.view.kind == "form" else len(app._roots[app.view.root]))
                    for _ in range(depth):
                        self.assertNotEqual("home", app.view.kind)
                        await pilot.press("escape")
                        await pilot.pause()
                    self.assertEqual("home", app.view.kind)
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertEqual("home", app.view.kind)

    async def test_card_follows_the_highlighted_row_without_any_request(self):
        api, reads = FakeAPI(), []

        def favorites(**kwargs):
            reads.append(kwargs["page"])
            return BookPage((book(1, "First", is_favorite=True), Book("Sparse", "", "", "", book_id="2", book_hash="abc"),
                             book(3, "Third", is_favorite=False)), 1, 20)

        api.favorites = favorites
        app = CollectionsApp(api, initial_view="favorites")
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            card = app.query_one("#card")
            self.assertEqual("First\nAuthor 1\nEPUB · 1 MB · 2020 · English · ★ Saved", str(card.render()))
            self.assertEqual("Page 1 · Book 1 of 3", text(app, "#status"))
            await pilot.press("down")
            await pilot.pause()
            # Missing metadata is skipped instead of printed as Unknown; the table's placeholder is dim.
            self.assertEqual("Sparse", str(card.render()))
            author = app.query_one("#collection", DataTable).get_row_at(1)[2]
            self.assertEqual(("Unknown", "dim"), (author.plain, str(author.style)))
            self.assertEqual("Page 1 · Book 2 of 3", text(app, "#status"))
            await pilot.press("j")
            await pilot.pause()
            self.assertEqual("Third\nAuthor 3\nEPUB · 1 MB · 2020 · English", str(card.render()))
            await pilot.press("k", "end")
            await pilot.pause()
            self.assertEqual(2, app.query_one("#collection", DataTable).cursor_row)
            await pilot.press("home")
            await pilot.pause()
            self.assertEqual(0, app.query_one("#collection", DataTable).cursor_row)
            self.assertEqual([1], reads)
            self.assertEqual([], api.calls)

    async def test_card_and_spacing_follow_the_terminal_height(self):
        app = CollectionsApp(FakeAPI(), initial_view="favorites")
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            card, gap = app.query_one("#card"), app.query_one("#card-gap")
            self.assertEqual((True, 5, True), (card.display, card.region.height, gap.display))
            self.assertEqual(3, str(card.render()).count("\n") + 1)
            for height, shown, card_height, lines in ((23, True, 5, 3), (20, True, 5, 3), (19, True, 3, 1),
                                                      (16, True, 3, 1), (15, False, 0, 0)):
                with self.subTest(height=height):
                    await pilot.resize_terminal(80, height)
                    await pilot.pause()
                    self.assertFalse(gap.display)
                    self.assertEqual(shown, card.display)
                    if shown:
                        self.assertEqual(card_height, card.region.height)
                        self.assertEqual(lines, str(card.render()).count("\n") + 1)
            await pilot.resize_terminal(80, 19)
            await pilot.pause()
            self.assertEqual("Synthetic book 1 — Author 1", str(card.render()))

    async def test_optional_columns_appear_from_100_columns_and_give_way_before_titles(self):
        titles = {"short": "Synthetic book 1", "long": "A synthetic title that needs exactly seventy cells to be shown in full",
                  "outlier": "An outlier title " * 9 + "that never fits"}
        # A title that no layout can show in full does not hide Format and Year.
        for kind, cases in (("short", ((100, True), (99, False), (60, False), (40, False))),
                            ("long", ((100, False), (110, True), (99, False), (40, False))),
                            ("outlier", ((100, True), (140, True), (99, False)))):
            api = FakeAPI()
            api.popular = lambda title=titles[kind], **kwargs: (book(1, title), book(2))
            app = CollectionsApp(api, initial_view="popular")
            async with app.run_test(size=(100, 30)) as pilot:
                await self.ready(app, pilot)
                table = app.query_one("#collection", DataTable)
                for width, wide in cases:
                    with self.subTest(kind=kind, width=width):
                        await pilot.resize_terminal(width, 30)
                        await pilot.pause()
                        labels = ["#", "Title", "Author"] + ["Format", "Year"] * wide
                        self.assertEqual(labels, [column.label.plain for column in table.ordered_columns])
                        self.assertLessEqual(table.virtual_size.width, table.size.width)
                        self.assertLessEqual(table.region.right, width - 1)
                        title = table.get_row_at(0)[1]
                        self.assertEqual(titles[kind], title.plain)
                        if kind == "long" and width >= 100:
                            # Without the optional columns, the whole title fits.
                            self.assertGreaterEqual(table.ordered_columns[1].width, len(titles[kind]))
                        if kind == "short":
                            # A title that fits gets what it needs and Author the rest, so authors stay close.
                            self.assertEqual(len(titles[kind]), table.ordered_columns[1].width)
                        if wide:
                            self.assertEqual(["EPUB", "2020"], [cell.plain for cell in table.get_row_at(1)[3:]])
                self.assertEqual(["1", "2"], [table.get_row_at(index)[0].plain for index in range(2)])
        self.assertEqual(70, len(titles["long"]))
        # Title keeps at least 60% of the shared width when both columns need more; when both fit, Author takes the
        # spare cells.
        self.assertEqual((30, 20), split_columns(50, 200, 200))
        self.assertEqual((10, 40), split_columns(50, 10, 1))
        self.assertEqual((20, 30), split_columns(50, 20, 30))
        self.assertEqual((29, 21), split_columns(50, 29, 30))

    async def test_paged_numbers_are_absolute(self):
        app = CollectionsApp(FakeAPI(), initial_view="favorites", page_size=7)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            await pilot.press("n")
            await self.ready(app, pilot)
            table = app.query_one("#collection", DataTable)
            self.assertEqual(["8", "14"], [table.get_row_at(index)[0].plain for index in (0, 6)])
            self.assertEqual("Page 2 of 5 · Book 1 of 7 · 100 books", text(app, "#status"))

    def test_status_parts_use_only_what_the_server_reported(self):
        page = BookPage(tuple(book(number) for number in range(1, 4)), 2, 3, None, None, 3, 1)
        self.assertEqual("Page 2 · Book 3 of 3", position_text(ViewState("favorites", "books", value=page), 9))
        self.assertEqual([], status_extras(ViewState("favorites", "books", value=page)))
        known = BookPage(page.books, 2, 3, 40, 14)
        self.assertEqual("Page 2 of 14 · Book 1 of 3", position_text(ViewState("favorites", "books", value=known), 0))
        self.assertEqual(["40 books"], status_extras(ViewState("favorites", "books", value=known)))
        self.assertEqual(["1 book", "Z-Library account history"],
                         status_extras(ViewState("history", "books", value=BookPage(page.books[:1], 1, 3, 1, 1))))
        # A search shows the total the server reported, and nothing when it reported none.
        self.assertEqual(["40 books"], status_extras(ViewState("search", "results", value=known, search=SearchRequest("x"))))
        self.assertEqual([], status_extras(ViewState("search", "results", value=page, search=SearchRequest("x"))))
        self.assertEqual("Book 2 of 3", position_text(ViewState("popular", "popular", value=page.books), 1))
        self.assertEqual("List 1 of 1", position_text(ViewState("booklists", "booklists", value=(Booklist("1", "x"),)), 0))
        self.assertEqual("Page 1", position_text(ViewState("favorites", "books", value=BookPage((), 1, 3, 0, 0)), 0))
        self.assertEqual("", position_text(ViewState("popular", "popular", value=()), 0))

    async def test_loading_status_names_the_source_and_search_filters_drop_when_narrow(self):
        api = FakeAPI()
        started, release = threading.Event(), threading.Event()
        search = api.search

        def slow(query, **kwargs):
            started.set()
            release.wait()
            return search(query, **kwargs)

        api.search = slow
        app = CollectionsApp(api, initial_view="search")
        filters = SearchFilters(languages=("french",), formats=("epub",))
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                app.post_message(SearchForm.Submitted(SearchRequest("dune", filters)))
                await self.until(pilot, started.is_set)
                status = text(app, "#status")
                self.assertRegex(status, r"^[⣾⣽⣻⢿⡿⣟⣯⣷] Searching… french · EPUB$")
                self.assertEqual("esc back · / commands", text(app, "#hints"))
                release.set()
                await self.ready(app, pilot)
                self.assertEqual("Page 1 of 3 · Book 1 of 20 · 60 books · french · EPUB", text(app, "#status"))
                await pilot.resize_terminal(40, 24)
                await pilot.pause()
                # Fewer than 12 cells remain after the position and the total, so the filters give way.
                self.assertEqual("Page 1 of 3 · Book 1 of 20 · 60 books", text(app, "#status"))
                # A sort order is named as one, so it does not read as a section or a filter value.
                await pilot.resize_terminal(80, 24)
                for chosen, loading, loaded in (
                        (SearchFilters(order="popular"), "Sort: Popular", "Sort: Popular"),
                        # Too long for the line: whole parts give way from the end, marked by …
                        (SearchFilters(languages=("french", "english"), formats=("epub",), year_from=2000,
                                       year_to=2010, order="popular"),
                         "french, english · EPUB · 2000–2010 · Sort: Popular", "french, english · EPUB · 2000–2010 · …")):
                    with self.subTest(filters=chosen):
                        await pilot.press("s")
                        await pilot.pause()
                        started.clear()
                        release.clear()
                        app.post_message(SearchForm.Submitted(SearchRequest("dune", chosen)))
                        await self.until(pilot, started.is_set)
                        self.assertRegex(text(app, "#status"), r"^[⣾⣽⣻⢿⡿⣟⣯⣷] Searching… " + loading + "$")
                        release.set()
                        await self.ready(app, pilot)
                        self.assertEqual("Page 1 of 3 · Book 1 of 20 · 60 books · " + loaded, text(app, "#status"))
                await pilot.resize_terminal(60, 24)
                await pilot.press("s")
                await pilot.pause()
                started.clear()
                release.clear()
                app.post_message(SearchForm.Submitted(SearchRequest("dune", chosen)))
                await self.until(pilot, started.is_set)
                self.assertRegex(text(app, "#status"), r"^[⣾⣽⣻⢿⡿⣟⣯⣷] Searching… french, english · EPUB · 2000–2010 · …$")
                release.set()
                await self.ready(app, pilot)
        finally:
            release.set()

    async def test_empty_and_error_panels_name_their_source(self):
        cases = (("favorites", "No books in your favorites yet."), ("history", "No download history yet."),
                 ("booklists", "No booklists yet."), ("popular", "No popular books were returned."),
                 ("recommended", "No recommendations yet."))
        for view, message in cases:
            with self.subTest(view=view):
                api = FakeAPI()
                api.empty = True
                app = CollectionsApp(api, initial_view=view)
                async with app.run_test(size=(80, 24)) as pilot:
                    await self.ready(app, pilot)
                    self.assertEqual(message, text(app, "#message"))
                    self.assertFalse(app.query_one("#collection").display)
                    self.assertFalse(app.query_one("#card").display)
                    # Nothing to select or open: only the keys that still apply. Synthetic favorites report a next page.
                    self.assertEqual(("←→ page · " if view == "favorites" else "") + "r refresh · esc back · / commands",
                                     text(app, "#hints"))
                    # One blank line separates the message from the header, whether or not the status line has text.
                    status = text(app, "#status")
                    self.assertEqual(view in ("favorites", "history"), bool(status))
                    blank = app.query_one("#status").region.y + (2 if status else 1)
                    self.assertEqual(blank, app.query_one("#message").region.y)
        api = FakeAPI()
        api.empty = True
        app = CollectionsApp(api, initial_view="search")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            # A sort order never removes books, so the advice leaves it out.
            for request, advice in ((SearchRequest("dune"), "Try another title, author or ISBN."),
                                    (SearchRequest("dune", SearchFilters(order="popular")),
                                     "Try another title, author or ISBN."),
                                    (SearchRequest("dune", SearchFilters(formats=("pdf",), year_from=1990, order="year")),
                                     "Try another title, author or ISBN, or loosen the filters: PDF · from 1990")):
                app.post_message(SearchForm.Submitted(request))
                await self.until(pilot, lambda: app.view.kind == "results" and not app.loading)
                self.assertEqual("No books found for this search.\n" + advice, text(app, "#message"))
                self.assertEqual("s edit search · r refresh · esc back · / commands", text(app, "#hints"))
                await pilot.press("s")
                await pilot.pause()
            api.empty, api.error = False, True
            app.post_message(SearchForm.Submitted(SearchRequest("dune")))
            await self.until(pilot, lambda: app.view.kind == "results" and not app.loading)
            self.assertTrue(text(app, "#message").startswith("✗ Could not load search results\n"))
            self.assertEqual("Page 1", text(app, "#status"))
            self.assertEqual("s edit search · r retry · esc back · / commands", text(app, "#hints"))
            api.error = False
            await pilot.press("r")
            await self.ready(app, pilot)
            self.assertTrue(app.query_one("#collection").display)

    async def test_booklist_card_shows_title_description_and_known_details(self):
        api = FakeAPI()
        app = CollectionsApp(api, initial_view="booklists")
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            card = app.query_one("#card")
            self.assertEqual((6, "Weekend reading"), (card.region.height, str(card.render())))
            self.assertEqual("List 1 of 2", text(app, "#status"))
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual("Science\nPhysics first.\nThen biology.\nPrivate · 2 books · updated 2026-01-02",
                             str(card.render()))
            self.assertEqual("↑↓ select · enter open · r refresh · esc back · / commands", text(app, "#hints"))
            table = app.query_one("#collection", DataTable)
            self.assertEqual(["#", "Booklist", "Books", "Access"], [column.label.plain for column in table.ordered_columns])
            await pilot.resize_terminal(80, 18)
            await pilot.pause()
            self.assertEqual("Science — Private · 2 books · updated 2026-01-02", str(card.render()))
            # Without descriptions, the card keeps only the rows the lists need, whichever is selected.
            api.booklists = lambda **kwargs: (Booklist("1", "Weekend reading"), Booklist("2", "Science", "", 2, False))
            await pilot.resize_terminal(80, 24)
            await pilot.press("r")
            await self.ready(app, pilot)
            for key, shown in (("home", "Weekend reading"), ("down", "Science\nPrivate · 2 books")):
                await pilot.press(key)
                await pilot.pause()
                self.assertEqual((4, shown), (card.region.height, str(card.render())))

    async def test_details_show_a_header_known_fields_and_the_description(self):
        api = FakeAPI()
        app = CollectionsApp(api, initial_view="favorites")
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            await pilot.press("enter")
            await self.ready(app, pilot)
            # The synthetic description is taller than the page, so ↑↓ is offered.
            scrolling = "↑↓ scroll · m more like this · r refresh · esc back · / commands"
            await self.until(pilot, lambda: text(app, "#hints") == scrolling)
            self.assertGreater(app.query_one("#detail-view").max_scroll_y, 0)
            api.book = lambda reference, **kwargs: BookDetails(
                "Dune", "Frank Herbert", "epub", "", "1965", "English", "2 MB", "7", "abc", True,
                description="Arrakis.\n\nSpice.", publisher="Chilton", isbn13="9780441013593", categories=("Fiction", "SF"))
            await pilot.press("r")
            await self.ready(app, pilot)
            # A page that fits does not offer to scroll.
            await self.until(pilot, lambda: text(app, "#hints") == scrolling.removeprefix("↑↓ scroll · "))
            self.assertEqual(0, app.query_one("#detail-view").max_scroll_y)
            self.assertEqual("Dune\nFrank Herbert\nEPUB · 2 MB · 1965 · English · ★ Saved", text(app, "#detail-header"))
            shown = {row.id.removeprefix("field-"): str(row.query_one(".detail-value").render())
                     for row in app.query(".detail-row") if row.display}
            self.assertEqual({"publisher": "Chilton", "year": "1965", "language": "English", "isbn-13": "9780441013593",
                              "categories": "Fiction, SF", "saved": "Yes", "reference": "7:abc"}, shown)
            self.assertEqual(12, app.query_one(".detail-label").region.width)
            self.assertEqual("Arrakis.\n\nSpice.", text(app, "#detail-description"))
            self.assertEqual([], list(app.query_one("#detail-description").render().spans))
            self.assertTrue(app.query_one("#detail-view").has_focus)
            api.book = lambda reference, **kwargs: BookDetails("Sparse", "", "", "", book_id="7", book_hash="abc")
            await pilot.press("r")
            await self.ready(app, pilot)
            self.assertEqual("Sparse", text(app, "#detail-header"))
            self.assertEqual(["field-reference"], [row.id for row in app.query(".detail-row") if row.display])
            # The placeholder is muted, so it does not read as the book's description.
            self.assertEqual("No description available.", text(app, "#detail-description"))
            self.assertEqual(["$foreground-muted"],
                             [span.style for span in app.query_one("#detail-description").render().spans])

    async def test_minimum_size_prompt_replaces_every_page_and_ignores_keys_but_quit(self):
        api = FakeAPI()
        app = CollectionsApp(api)
        async with app.run_test(size=(39, 12)) as pilot:
            await pilot.pause()
            prompt = app.query_one("#too-small")
            self.assertTrue(prompt.display)
            self.assertEqual("Enlarge the terminal to at least 40×12\nq quit", text(app, "#too-small"))
            for name in ("#home", "#header", "#hints", "#list-page", "#search-page", "#detail-view"):
                self.assertFalse(app.query_one(name).display, name)
            await pilot.press("f", "s", "slash", "question_mark", "escape")
            await pilot.pause()
            self.assertEqual(("home", 1), (app.view.kind, len(app.screen_stack)))
            self.assertEqual([], api.calls)
            await pilot.resize_terminal(40, 11)
            await pilot.pause()
            self.assertTrue(prompt.display)
            await pilot.resize_terminal(60, 18)
            await pilot.pause()
            self.assertFalse(prompt.display)
            self.assertTrue(app.query_one("#home").display)
            await pilot.resize_terminal(100, 10)
            await pilot.pause()
            self.assertEqual("Enlarge the terminal to at least 40×12 · q quit", text(app, "#too-small"))
            # A dialog that was open when the terminal shrank closes, so the prompt is all that remains.
            for key in ("question_mark", "slash"):
                await pilot.resize_terminal(60, 18)
                await pilot.pause()
                await pilot.press(key)
                await pilot.pause(.2)
                self.assertEqual(2, len(app.screen_stack))
                await pilot.resize_terminal(39, 18)
                await pilot.pause()
                self.assertEqual(1, len(app.screen_stack))
                self.assertTrue(prompt.display)
            await pilot.press("q")
            await pilot.pause()
            self.assertTrue(app._stopping)
        self.assertFalse(app._loader.alive)

    async def test_every_page_fits_60_by_18_and_80_by_24(self):
        steps = {"home": (), "favorites": ("f",), "booklists": ("b",), "members": ("b", "enter"),
                 "details": ("f", "enter"), "form": ("s",), "results": ("s", "results"), "popular": ("5",),
                 "help": ("question_mark",), "commands": ("slash",)}
        for width, height in ((60, 18), (80, 24)):
            for page, keys in steps.items():
                with self.subTest(size=(width, height), page=page):
                    app = CollectionsApp(FakeAPI())
                    async with app.run_test(size=(width, height)) as pilot:
                        await pilot.pause()
                        for key in keys:
                            if key == "results":
                                app.post_message(SearchForm.Submitted(SearchRequest("dune", SearchFilters(formats=("epub",)))))
                                await self.until(pilot, lambda: app.view.kind == "results")
                            else:
                                await pilot.press(key)
                            await self.ready(app, pilot)
                            await pilot.pause(.2)
                        screen = Region(0, 0, width, height)
                        hints = app.query_one("#hints")
                        if page not in ("help", "commands"):
                            self.assertEqual((height - 1, 1), (hints.region.y, hints.region.height))
                            self.assertLessEqual(hints.region.right, width)
                        if page in ("favorites", "booklists", "members", "results", "popular"):
                            table = app.query_one("#collection", DataTable)
                            self.assertEqual((0, 1), (app.query_one("#view-title").region.y, app.query_one("#status").region.y))
                            self.assertTrue(screen.contains_region(table.region))
                            self.assertLessEqual(table.region.bottom, hints.region.y)
                            self.assertLessEqual(table.virtual_size.width, table.size.width)
                            self.assertGreaterEqual(table.region.height, 6)
                        elif page == "details":
                            self.assertTrue(screen.contains_region(app.query_one("#detail-header").region))
                            self.assertLessEqual(app.query_one("#detail-view").region.bottom, hints.region.y)
                        elif page == "form":
                            form = app.query_one(SearchForm)
                            # The whole form fits without scrolling, the Search button included.
                            self.assertEqual(0, form.query_one("#search-fields").max_scroll_y)
                            for name in ("#search-query", "#search-submit", "#search-order", "#search-help"):
                                self.assertTrue(screen.contains_region(form.query_one(name).region), name)
                            self.assertLessEqual(form.query_one("#search-help").region.bottom, hints.region.y)
                        elif page == "home":
                            for entry in app.query(".home-entry"):
                                self.assertTrue(screen.contains_region(entry.region))
                            self.assertEqual(2 if width >= 76 else 1, len({entry.region.x for entry in app.query(".home-entry")}))
                        elif page == "help":
                            self.assertIsInstance(app.screen, HelpScreen)
                            for name in ("#help-title", "#help-scroll", "#help-close"):
                                self.assertTrue(screen.contains_region(app.screen.query_one(name).region), name)
                        elif page == "commands":
                            await self.until(pilot, lambda: app.screen.query_one(CommandList).option_count > 0)
                            self.assertTrue(screen.contains_region(app.screen.query_one(CommandList).region))

    async def test_scrolling_form_keeps_the_focused_field_visible(self):
        app = CollectionsApp(FakeAPI(), initial_view="search")
        async with app.run_test(size=(60, 14)) as pilot:
            await pilot.pause()
            form = app.query_one(SearchForm)
            fields = form.query_one("#search-fields")
            self.assertGreater(fields.max_scroll_y, 0)
            order = ("search-query", "search-year-from", "search-year-to", "search-languages", "search-format-pdf",
                     "search-format-epub", "search-format-djvu", "search-order", "search-submit")
            for name in order:
                await pilot.pause()
                self.assertEqual(name, app.focused.id)
                self.assertTrue(fields.region.contains_region(app.focused.region), name)
                await pilot.press("tab")
            # Up and down move between rows; Left and Right move between formats.
            form.query_one("#search-query", Input).focus()
            await pilot.pause()
            await pilot.press("down", "down", "down", "down", "right", "right", "left")
            self.assertEqual("search-format-epub", app.focused.id)
            await pilot.press("down", "down")
            self.assertEqual("search-submit", app.focused.id)
            await pilot.press("up", "up", "up")
            self.assertEqual("search-languages", app.focused.id)

    def test_status_filters_give_way_by_whole_parts(self):
        summary = "french, english · EPUB · 2000–2010 · Sort: Year"
        self.assertEqual(summary, fit_summary(summary, 47))
        self.assertEqual("french, english · EPUB · 2000–2010 · …", fit_summary(summary, 46))
        self.assertEqual("french, english · …", fit_summary(summary, 19))
        # When not even the first part fits, nothing is shown rather than a cut word.
        self.assertEqual("", fit_summary(summary, 18))
        self.assertEqual("Sort: Year", fit_summary("Sort: Year", 10))
        self.assertEqual("", fit_summary("Sort: Year", 9))
        self.assertEqual("", fit_summary("", 9))

    def test_hint_line_drops_optional_items_before_truncating(self):
        items = (("↑↓", "select", 2), ("enter", "details", 0), ("m", "more like this", 1), ("esc", "back", 0))
        self.assertEqual("↑↓ select · enter details · m more like this · esc back", hint_line(items, 80).plain)
        self.assertEqual("enter details · m more like this · esc back · …", hint_line(items, 50).plain)
        self.assertEqual("enter details · esc back · …", hint_line(items, 30).plain)
        narrow = hint_line(items, 20)
        self.assertEqual((20, "…"), (narrow.cell_length, narrow.plain[-1]))


if __name__ == "__main__":
    unittest.main()
