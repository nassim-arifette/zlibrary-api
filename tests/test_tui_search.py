"""Headless search, discovery and similar-book navigation against synthetic data."""
import importlib.util
import threading
import unittest
from unittest.mock import patch

from zlib_connector.books import BookDetails
from zlib_connector.client import Book
from zlib_connector.pagination import BookPage
from zlib_connector.search import SEARCH_ORDERS, SearchFilters

from test_tui import FakeAPI, TUITestCase, book, text


TEXTUAL_AVAILABLE = importlib.util.find_spec("textual") is not None
if TEXTUAL_AVAILABLE:
    from textual.color import Color
    from textual.command import CommandPalette
    from textual.widgets import Checkbox, DataTable, Input, Select, Static
    from zlib_connector.tui.app import CollectionsApp, Loaded
    from zlib_connector.tui.loader import LoadResult
    from zlib_connector.tui.search import SearchForm
    from zlib_connector.tui.views import (MAX_DEPTH, ORDERS, ROOTS, START_VIEWS, SearchRequest, ViewState, book_meta,
                                          filters_text, format_label, valid_value)


FILTERS = dict(languages=("french", "english"), formats=("epub",), year_from=2000, year_to=2010, order="year")


@unittest.skipUnless(TEXTUAL_AVAILABLE, "Install the optional Textual dependency to test the TUI.")
class SearchAndDiscoveryTests(TUITestCase):
    async def submit(self, app, pilot, *keys):
        """Press keys on the search page, then wait for the search it starts."""
        generation = app._generation
        await pilot.press(*keys)
        await self.until(pilot, lambda: app._generation != generation and not app.loading)

    @staticmethod
    def searches(api):
        return [call[1:5] for call in api.calls if call[0] == "search"]

    @staticmethod
    def fields(form):
        return {name: form.query_one("#search-" + name, Input).value for name in ("query", "languages", "year-from", "year-to")}

    async def test_form_submits_one_exact_filtered_search_and_pages_keep_it(self):
        api = FakeAPI()
        app = CollectionsApp(api, page_size=7)
        expected = SearchFilters(**FILTERS)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            form = app.query_one(SearchForm)
            self.assertEqual(("search", "form"), (app.view.root, app.view.kind))
            self.assertEqual(("Search books", "Set filters, then search"), (text(app, "#view-title"), text(app, "#status")))
            self.assertIs(form.query_one("#search-query", Input), app.focused)
            await pilot.press(*" history ")
            form.query_one("#search-languages", Input).value = "French, english,"
            # Query, Year from, Year to, Languages, PDF, then EPUB.
            await pilot.press("tab", "tab", "tab", "tab", "tab", "space")
            self.assertTrue(form.query_one("#search-format-epub", Checkbox).value)
            form.query_one("#search-year-from", Input).value = "2000"
            form.query_one("#search-year-to", Input).value = "2010"
            form.query_one("#search-order", Select).focus()
            await pilot.pause()
            await pilot.press("enter", *["down"] * 6, "enter")
            self.assertEqual("year", form.query_one("#search-order", Select).value)
            self.assertEqual("Filters ─", text(app, "#filters-rule")[:9])
            self.assertTrue(text(app, "#filters-rule").endswith(" 5 active"))
            self.assertEqual([], self.searches(api))
            await self.submit(app, pilot, "ctrl+s")
            self.assertEqual([("history", 1, 7, expected)], self.searches(api))
            self.assertNotEqual(threading.get_ident(), api.calls[-1][-1])
            self.assertEqual(SearchRequest("history", expected), app.view.search)
            self.assertEqual("Search: history", text(app, "#view-title"))
            # The reported total comes before the filters, and the sort order is named as such. At 80 columns the
            # last part gives way whole, marked by …
            self.assertEqual("Page 1 of 3 · Book 1 of 7 · 21 books · french, english · EPUB · 2000–2010 · …",
                             text(app, "#status"))
            await pilot.resize_terminal(100, 24)
            await pilot.pause()
            self.assertEqual("Page 1 of 3 · Book 1 of 7 · 21 books · french, english · EPUB · 2000–2010 · Sort: Year",
                             text(app, "#status"))
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            await pilot.press("n")
            await self.ready(app, pilot)
            self.assertTrue(text(app, "#status").startswith("Page 2 of 3 · Book 1 of 7"))
            await pilot.press("left")
            await self.ready(app, pilot)
            self.assertEqual([("history", page, 7, expected) for page in (1, 2, 1)], self.searches(api))
            self.assertEqual(SearchRequest("history", expected), app.view.search)
            table = app.query_one("#collection", DataTable)
            await pilot.press("down", "down", "down", "enter")
            await self.ready(app, pilot)
            self.assertEqual(("details", "4:abc"), (app.view.kind, app.view.identifier))
            self.assertEqual("Search: history › Book details", text(app, "#status"))
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual((1, 3), (app.view.value.page, table.cursor_row))
            # s within the Search section opens the form, filled in with the current search.
            await pilot.press("s")
            await pilot.pause()
            self.assertEqual("form", app.view.kind)
            self.assertEqual({"query": "history", "languages": "french, english", "year-from": "2000", "year-to": "2010"},
                             self.fields(form))
            self.assertEqual([False, True, False],
                             [form.query_one("#search-format-" + value, Checkbox).value for value in ("pdf", "epub", "djvu")])
            self.assertEqual("year", form.query_one("#search-order", Select).value)
            # Esc on the form goes Home without searching; s then shows the retained results.
            form.query_one("#search-query", Input).value = "discarded"
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual("home", app.view.kind)
            await pilot.press("s")
            await pilot.pause()
            self.assertEqual(("results", 3), (app.view.kind, table.cursor_row))
            self.assertEqual(3, len(self.searches(api)))
            # Esc on results reopens the form with the search that produced them.
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(("form", "history"), (app.view.kind, form.query_one("#search-query", Input).value))
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual("home", app.view.kind)
            self.assertEqual(3, len(self.searches(api)))

    async def test_search_key_shows_results_and_a_new_search_replaces_the_stack(self):
        api = FakeAPI()
        app = CollectionsApp(api, initial_view="favorites")
        async with app.run_test() as pilot:
            await self.ready(app, pilot)
            await pilot.press("s")
            await pilot.pause()
            self.assertEqual("form", app.view.kind)
            await self.submit(app, pilot, *"tolkien", "enter")
            await pilot.press("enter")
            await self.ready(app, pilot)
            await pilot.press("m")
            await self.ready(app, pilot)
            self.assertEqual(["form", "results", "details", "similar"], [state.kind for state in app._roots["search"]])
            await pilot.press("f", "4")
            await pilot.pause()
            self.assertEqual(("search", "similar"), (app.view.root, app.view.kind))
            await pilot.press("s")
            await pilot.pause()
            form = app.query_one(SearchForm)
            self.assertEqual("form", app.view.kind)
            self.assertEqual("tolkien", form.query_one("#search-query", Input).value)
            form.query_one("#search-query", Input).value = "le guin"
            await self.submit(app, pilot, "enter")
            self.assertEqual(["form", "results"], [state.kind for state in app._roots["search"]])
            self.assertEqual(("results", 1, SearchRequest("le guin")),
                             (app.view.kind, app.view.value.page, app.view.search))
            self.assertEqual([("tolkien", 1), ("le guin", 1)], [call[:2] for call in self.searches(api)])
            self.assertEqual(1, sum(call[0] == "favorites" for call in api.calls))

    async def test_invalid_values_show_inline_literal_errors_under_their_field_and_never_read(self):
        api = FakeAPI()
        app = CollectionsApp(api, initial_view="search")
        languages = ", ".join(f"{number:02}" + "x" * 62 for number in range(50))
        # Field-specific wording for an empty query and an invalid language; other messages come from validation.
        cases = (({"query": " "}, "query", "Type a title, author or ISBN."),
                 ({"year-from": "2010", "year-to": "2000"}, "years", "The starting year must not exceed the ending year."),
                 ({"year-from": "20x0"}, "years", "Years must be integers between 1 and 9999."),
                 ({"year-to": "0"}, "years", "Years must be integers between 1 and 9999."),
                 ({"query": "a\x1b[31mb"}, "query", "Search text cannot contain control or formatting characters."),
                 ({"query": "a‮b"}, "query", "Search text cannot contain control or formatting characters."),
                 ({"languages": "english, fr‮nch"}, "languages", "Each language must be 1–64 printable characters."),
                 ({"languages": "english, " + "x" * 65}, "languages", "Each language must be 1–64 printable characters."),
                 ({"languages": ", ".join(f"l{number}" for number in range(51))}, "languages",
                  "Use at most 50 values per filter."),
                 ({"query": "é" * 500, "languages": languages}, "query", "Shorten the search text or remove some languages."))
        below = {"query": "#search-query", "years": "#row-year-to", "languages": "#row-languages"}
        focus = {"query": "search-query", "years": "search-year-from", "languages": "search-languages"}
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            form = app.query_one(SearchForm)
            self.assertEqual({}, form.errors())
            form.query_one("#search-format-pdf", Checkbox).value = True
            for values, field, message in cases:
                with self.subTest(message=message):
                    for name, value in {"query": "history", "languages": "", "year-from": "", "year-to": "", **values}.items():
                        form.query_one("#search-" + name, Input).value = value
                    form.query_one("#search-query", Input).focus()
                    await pilot.pause()
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual("form", app.view.kind)
                    self.assertEqual({field: message}, form.errors())
                    error = form.query_one(f"#search-{field}-error", Static)
                    self.assertEqual([], list(error.render().spans))
                    self.assertEqual(form.query_one(below[field]).region.bottom, error.region.y)
                    self.assertEqual(focus[field], app.focused.id)
            # An error clears as soon as its field changes.
            form.query_one("#search-query", Input).focus()
            await pilot.press("a")
            await pilot.pause()
            self.assertEqual({}, form.errors())
            self.assertEqual([], api.calls)

    async def test_escape_on_the_form_goes_home_and_discards_typed_values(self):
        api = FakeAPI()
        app = CollectionsApp(api, initial_view="search")
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one(SearchForm)
            await pilot.press(*"dune")
            form.query_one("#search-format-pdf", Checkbox).value = True
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual("home", app.view.kind)
            self.assertIsNone(app.search_request)
            await pilot.press("s")
            await pilot.pause()
            self.assertEqual("form", app.view.kind)
            self.assertEqual({"query": "", "languages": "", "year-from": "", "year-to": ""}, self.fields(form))
            self.assertFalse(form.query_one("#search-format-pdf", Checkbox).value)
            self.assertTrue(text(app, "#filters-rule").endswith(" none set"))
            # Shortcuts do nothing here; Enter on Search validates the empty query.
            form.query_one("#search-submit").focus()
            await pilot.press("r", "n", "p", "m", "enter")
            await pilot.pause()
            self.assertEqual({"query": "Type a title, author or ISBN."}, form.errors())
            self.assertEqual("search-query", app.focused.id)
            self.assertEqual([], api.calls)

    async def test_ctrl_s_submits_from_any_field_and_a_repeated_submit_reads_once(self):
        api = FakeAPI()
        app = CollectionsApp(api, initial_view="search")
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one(SearchForm)
            # The form focuses its query once mounted; wait for that before moving focus.
            await self.until(pilot, lambda: form.query_one("#search-query", Input).has_focus)
            form.query_one("#search-query", Input).value = "history"
            form.query_one("#search-format-djvu", Checkbox).focus()
            hint = "space toggle · tab next field · ctrl+s search · esc back"
            await self.until(pilot, lambda: text(app, "#hints") == hint)
            await self.submit(app, pilot, "ctrl+s")
            self.assertEqual([("history", 1)], [call[:2] for call in self.searches(api)])
            await pilot.press("s")
            await pilot.pause()
            form.action_submit()
            form.action_submit()
            await self.ready(app, pilot)
            await pilot.pause()
            self.assertEqual([("history", 1)] * 2, [call[:2] for call in self.searches(api)])
            self.assertEqual(["form", "results"], [state.kind for state in app._roots["search"]])

    async def test_escape_closes_an_open_sort_menu_before_the_form(self):
        app = CollectionsApp(FakeAPI(), initial_view="search")
        async with app.run_test(size=(60, 18)) as pilot:
            await pilot.pause()
            order = app.query_one("#search-order", Select)
            await self.until(pilot, lambda: app.query_one("#search-query", Input).has_focus)
            order.focus()
            hint = "enter choose · tab next field · ctrl+s search · esc back"
            await self.until(pilot, lambda: text(app, "#hints") == hint)
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(order.expanded)
            self.assertEqual("↑↓ choose · enter select · esc close", text(app, "#hints"))
            # The open list may cover the help line, which stays empty until the list closes.
            self.assertEqual("", text(app, "#search-help"))
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(order.expanded)
            self.assertEqual("form", app.view.kind)
            self.assertEqual("Order chosen by Z-Library. Default keeps the server's order.", text(app, "#search-help"))
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual("home", app.view.kind)
            self.assertIsNone(app.search_request)

    async def test_typing_in_form_fields_never_triggers_app_bindings(self):
        api = FakeAPI()
        app = CollectionsApp(api, initial_view="search")
        typed = "q5/smfbh?1r"
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one(SearchForm)
            await pilot.press(*typed)
            self.assertEqual(typed, form.query_one("#search-query", Input).value)
            await pilot.press("tab", "tab", "tab", *typed)
            self.assertEqual(typed, form.query_one("#search-languages", Input).value)
            # Shortcuts stay off on the search page, even when a checkbox or the button has focus.
            await pilot.press("tab", *"q5fbhsmrn", "tab", "tab", "tab", "tab", *"q6")
            self.assertEqual("search-submit", app.focused.id)
            # Ctrl+P still opens the command dialog from a field; Esc returns to it with its text.
            form.query_one("#search-query", Input).focus()
            await pilot.press("ctrl+p")
            await pilot.pause()
            self.assertIsInstance(app.screen, CommandPalette)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(("search-query", typed), (app.focused.id, form.query_one("#search-query", Input).value))
            form.query_one("#search-year-from", Input).focus()
            await pilot.pause()
            await pilot.press(*"1x9845")
            self.assertEqual("1984", form.query_one("#search-year-from", Input).value)
            self.assertTrue(app.is_running)
            self.assertEqual(("search", "form"), (app.view.root, app.view.kind))
            self.assertEqual([], api.calls)

    async def test_popular_and_recommended_open_by_digit_and_command(self):
        api = FakeAPI()
        app = CollectionsApp(api, initial_view="favorites")
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            await pilot.press("o", "c")
            await pilot.pause()
            self.assertEqual("favorites", app.view.root)
            await pilot.press("5")
            await self.ready(app, pilot)
            self.assertEqual(("popular", "popular", "Popular"), (app.view.root, app.view.kind, text(app, "#view-title")))
            self.assertEqual("Book 1 of 30", text(app, "#status"))
            self.assertNotIn("page", text(app, "#hints"))
            before = len(api.calls)
            await pilot.press("n", "p", "right")
            await pilot.pause()
            self.assertEqual(before, len(api.calls))
            table = app.query_one("#collection", DataTable)
            table.move_cursor(row=4)
            await pilot.press("enter")
            await self.ready(app, pilot)
            self.assertEqual(("details", "204:abc", "Popular › Book details"),
                             (app.view.kind, app.view.identifier, text(app, "#status")))
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(("popular", 4), (app.view.kind, table.cursor_row))
            await pilot.press("6")
            await self.ready(app, pilot)
            self.assertEqual(("recommended", "Recommended", "Book 1 of 5"),
                             (app.view.root, text(app, "#view-title"), text(app, "#status")))
            await pilot.press("5", "6")
            await pilot.pause()
            self.assertEqual(["popular", "recommended"],
                             [call[0] for call in api.calls if call[0] in ("popular", "recommended")])
            self.assertTrue(all(call[-1] != threading.get_ident() for call in api.calls))
            api.empty = True
            await pilot.press("r")
            await self.ready(app, pilot)
            self.assertEqual(("No recommendations yet.", ""), (text(app, "#message"), text(app, "#status")))
            self.assertEqual("r refresh · esc back · / commands", text(app, "#hints"))
            self.assertIsNone(app._similar_target())
            await pilot.press("5", "r")
            await self.ready(app, pilot)
            self.assertEqual("No popular books were returned.", text(app, "#message"))

    async def test_recommendations_explain_a_missing_account_session_safely(self):
        api = FakeAPI()

        class SessionUnavailable(Exception):
            status = 424

        def unavailable(*, cancel=None):
            api.calls.append(("recommended", threading.get_ident()))
            raise SessionUnavailable("secret upstream body")

        api.recommended = unavailable
        app = CollectionsApp(api, initial_view="recommended")
        async with app.run_test() as pilot:
            await self.ready(app, pilot)
            message = text(app, "#message")
            self.assertTrue(message.startswith("✗ Could not load recommendations\n"))
            # The usual cause is a server started with --anonymous, so the reason names it.
            self.assertIn("no signed-in Z-Library session", message)
            self.assertIn("zlib-cli login", message)
            self.assertIn("restart zlib-cli serve without --anonymous", message)
            self.assertNotIn("secret", message)
            self.assertFalse(app.query_one("#collection", DataTable).display)
            self.assertFalse(app.command_available("/similar"))
            await pilot.press("m", "enter")
            await pilot.pause()
            self.assertEqual(1, len(api.calls))

    async def test_similar_books_open_from_a_list_row_and_from_details(self):
        api = FakeAPI()
        app = CollectionsApp(api, initial_view="favorites")
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            table = app.query_one("#collection", DataTable)
            await pilot.press("down", "j", "m")
            await self.ready(app, pilot)
            self.assertEqual(("similar", "3:abc", "Synthetic book 3"), (app.view.kind, app.view.identifier, app.view.label))
            self.assertEqual("Similar to: Synthetic book 3", text(app, "#view-title"))
            self.assertEqual("Book 1 of 3", text(app, "#status"))
            self.assertNotEqual(threading.get_ident(), api.calls[-1][-1])
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(("books", 2), (app.view.kind, table.cursor_row))
            await pilot.press("enter")
            await self.ready(app, pilot)
            self.assertTrue(app.command_available("/similar"))
            await pilot.press("m")
            await self.ready(app, pilot)
            self.assertEqual(("similar", "3:abc", 3), (app.view.kind, app.view.identifier, len(app._roots["favorites"])))
            await pilot.press("down", "enter")
            await self.ready(app, pilot)
            self.assertEqual(("details", "32:abc"), (app.view.kind, app.view.identifier))
            self.assertEqual("Favorites › Book details › Similar to: Synthetic book 3 › Book details", text(app, "#status"))
            self.assertEqual([("similar", "3:abc")] * 2, [call[:2] for call in api.calls if call[0] == "similar"])
            api.empty = True
            await pilot.press("m")
            await self.ready(app, pilot)
            self.assertEqual("Similar to: Synthetic book 32", text(app, "#view-title"))
            self.assertEqual(("No similar books were returned.", ""), (text(app, "#message"), text(app, "#status")))
            self.assertFalse(app.command_available("/similar"))
            await pilot.press("escape", "escape", "escape", "escape")
            await pilot.pause()
            self.assertEqual((1, "books", 1, 2), (len(app._roots["favorites"]), app.view.kind,
                                                  app.view.value.page, table.cursor_row))

    async def test_more_like_this_needs_a_loaded_book_row_or_details(self):
        api = FakeAPI()
        started, release = threading.Event(), threading.Event()

        def slow(*, cancel=None):
            started.set()
            release.wait()
            return (book(1),)

        api.popular = slow
        app = CollectionsApp(api, initial_view="booklists")
        try:
            async with app.run_test() as pilot:
                await self.ready(app, pilot)
                self.assertFalse(app.command_available("/similar"))
                await pilot.press("m")
                api.error = True
                await pilot.press("f")
                await self.ready(app, pilot)
                self.assertFalse(app.command_available("/similar"))
                await pilot.press("m")
                api.error, api.empty = False, True
                await pilot.press("r")
                await self.ready(app, pilot)
                self.assertFalse(app.command_available("/similar"))
                await pilot.press("m")
                await pilot.press("5")
                await self.until(pilot, started.is_set)
                self.assertTrue(app.loading)
                self.assertFalse(app.command_available("/similar"))
                await pilot.press("m")
                release.set()
                await self.ready(app, pilot)
                self.assertTrue(app.command_available("/similar"))
                self.assertFalse(any(call[0] == "similar" for call in api.calls))
                self.assertEqual(1, len(app._roots["popular"]))
        finally:
            release.set()

    async def test_details_and_similar_cycles_stop_at_the_depth_cap(self):
        api = FakeAPI()
        app = CollectionsApp(api, initial_view="popular")
        async with app.run_test() as pilot:
            await self.ready(app, pilot)
            stack = app._roots["popular"]
            # Start near the cap with loaded details/similar views, then cycle with keys.
            for number in range(2, MAX_DEPTH - 1):
                reference = f"{number}:abc"
                stack.append(ViewState("popular", "details", identifier=reference, value=BookDetails(**book(number).to_dict()))
                             if number % 2 == 0 else
                             ViewState("popular", "similar", identifier=reference, label="Synthetic", value=(book(number),)))
            app._render()
            while len(stack) < MAX_DEPTH:
                await pilot.press("m" if app.view.kind == "details" else "enter")
                await self.ready(app, pilot)
            self.assertEqual("details", app.view.kind)
            self.assertEqual(["similar", "book"], [call[0] for call in api.calls[1:]])
            await pilot.press("escape", "m")
            await self.ready(app, pilot)
            self.assertEqual((MAX_DEPTH, "similar"), (len(stack), app.view.kind))
            calls = len(api.calls)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual((MAX_DEPTH, "similar", calls), (len(stack), app.view.kind, len(api.calls)))
            self.assertEqual("✗ Go back before opening another view.", text(app, "#notice"))
            await pilot.press("m")
            await pilot.pause()
            self.assertEqual("✗ Go back before opening another view.", text(app, "#notice"))
            # The notice lasts until the next key press.
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual("", text(app, "#notice"))
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(MAX_DEPTH - 1, len(stack))

    async def test_a_slow_obsolete_search_cannot_replace_the_newer_one(self):
        api = FakeAPI()
        started, release, tokens = threading.Event(), threading.Event(), []
        search = api.search

        def delayed(query, **kwargs):
            if query != "first":
                return search(query, **kwargs)
            tokens.append(kwargs["cancel"])
            started.set()
            # Only the test releases this read, after the newer search started.
            release.wait()
            return BookPage((book(1, "obsolete first"),), 1, 20)

        api.search = delayed
        app = CollectionsApp(api, initial_view="search")
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press(*"first", "enter")
                await self.until(pilot, started.is_set)
                generation = app._generation
                await pilot.press("s")
                await pilot.pause()
                form = app.query_one(SearchForm)
                self.assertEqual("form", app.view.kind)
                self.assertTrue(tokens[0].is_cancelled)
                self.assertEqual("first", form.query_one("#search-query", Input).value)
                form.query_one("#search-query", Input).value = "second"
                await pilot.press("enter")
                await pilot.pause()
                self.assertTrue(app.loading)
                release.set()
                await self.ready(app, pilot)
                self.assertEqual("second result 1", app.view.value.books[0].title)
                app.post_message(Loaded(LoadResult(generation, BookPage((book(1, "late secret"),), 1, 20))))
                await pilot.pause()
                self.assertEqual("second result 1", app.view.value.books[0].title)
                self.assertEqual("Search: second", text(app, "#view-title"))
                self.assertEqual(["second"], [call[0] for call in self.searches(api)])
        finally:
            release.set()

    async def test_queries_and_titles_with_markup_or_escape_codes_stay_literal(self):
        api = FakeAPI()
        hostile = "[bold][link=https://bad.invalid]Title[/link]\x1b[31m‮"
        api.search = lambda query, **kwargs: BookPage((book(1, hostile),), 1, 20)
        api.similar = lambda reference, **kwargs: (book(2, hostile),)
        app = CollectionsApp(api, initial_view="search")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press(*"[red]")
            app.query_one("#search-query", Input).value += "x[/red]"
            await self.submit(app, pilot, "enter")
            title = app.query_one("#view-title", Static).render()
            self.assertEqual(("Search: [red]x[/red]", []), (str(title), list(title.spans)))
            cell = app.query_one("#collection", DataTable).get_row_at(0)[1]
            self.assertEqual(("[bold][link=https://bad.invalid]Title[/link]", []), (cell.plain, cell.spans))
            await pilot.press("m")
            await self.ready(app, pilot)
            title = app.query_one("#view-title", Static).render()
            self.assertEqual("Similar to: [bold][link=https://bad.invalid]Title[/link]", str(title))
            self.assertEqual([], list(title.spans))
            self.assertNotIn("\x1b", app.query_one("#collection", DataTable).get_row_at(0)[1].plain)

    @staticmethod
    def shade(app, widget, line=0, text=None):
        """Brightness of the background shown behind some text, once the app's color filters apply."""
        segments = list(widget.render_line(line))
        for line_filter in app.get_line_filters():
            segments = line_filter.apply(segments, app.screen.styles.background)
        style = next(segment.style for segment in segments
                     if segment.text.strip() and (text is None or text in segment.text))
        return Color.from_rich_color(style.color if style.reverse else style.bgcolor).brightness

    async def test_no_color_marks_selection_and_focus_by_shade_and_markers(self):
        for theme in ("tokyo-night", "textual-light"):
            with patch.dict("os.environ", {"NO_COLOR": "1"}):
                app = CollectionsApp(FakeAPI(), initial_view="popular")
            app.theme = theme
            self.assertTrue(app.no_color)
            async with app.run_test() as pilot:
                await self.ready(app, pilot)
                table = app.query_one("#collection", DataTable)
                self.assertTrue(table.has_focus)
                # Textual shows NO_COLOR in shades of gray; the selected row stands out by its shade.
                with self.subTest(theme=theme, cue="selected row"):
                    self.assertGreater(abs(self.shade(app, table, 1) - self.shade(app, table, 2)), .3)
                await pilot.press("s")
                await pilot.pause()
                form = app.query_one(SearchForm)
                query = form.query_one("#search-query", Input)
                self.assertEqual("heavy", query.styles.border.top[0])
                box, idle = form.query_one("#search-format-epub", Checkbox), form.query_one("#search-format-pdf")
                box.focus()
                await pilot.pause()
                row = form.query_one("#row-formats")
                # Every theme keeps the row marker and a bold label; dark themes also shade the focused control.
                self.assertEqual("visible", row.query_one(".marker").styles.visibility)
                self.assertEqual("hidden", form.query_one("#row-languages .marker").styles.visibility)
                self.assertTrue(box.get_component_styles("toggle--label").text_style.bold)
                if theme == "tokyo-night":
                    with self.subTest(theme=theme, cue="focused format"):
                        self.assertGreater(abs(self.shade(app, box, text="EPUB") - self.shade(app, idle, text="PDF")), .3)
                button = form.query_one("#search-submit")
                before = self.shade(app, button)
                button.focus()
                await pilot.pause()
                if theme == "tokyo-night":
                    with self.subTest(theme=theme, cue="focused button"):
                        self.assertGreater(abs(self.shade(app, button) - before), .3)
                self.assertTrue(button.styles.text_style.bold)
                self.assertEqual("visible", form.query_one("#row-submit .marker").styles.visibility)

    async def test_help_lists_the_keys_of_every_page(self):
        app = CollectionsApp(FakeAPI())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            content = text(app.screen, "#help-text")
            for heading in ("Global", "Book lists", "Book details", "Search form", "Tips"):
                self.assertIn(heading, content.splitlines())
            for key, meaning in (("/ or ctrl+p", "Commands"), ("s or 4", "Search books"), ("5", "Popular books"),
                                 ("6", "Recommended for you"), ("m", "More like this"), ("← → or p n", "Previous or next page"),
                                 ("ctrl+s", "Search from any field"), ("space", "Toggle a format"),
                                 ("q or ctrl+c", "Quit")):
                self.assertIn(f"  {key.ljust(15)}{meaning}", content)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual("home", app.view.kind)

    def test_unsupported_adapter_values_are_rejected_before_display(self):
        page = BookPage((book(1),), 1, 20)
        self.assertTrue(valid_value("results", page))
        self.assertTrue(valid_value("popular", (book(1),)))
        self.assertTrue(valid_value("similar", ()))
        for kind, value in (("results", (book(1),)), ("popular", [book(1)]), ("popular", page),
                            ("recommended", tuple(book(1) for _ in range(1001))), ("similar", ("not a book",)),
                            ("popular", (Book("Incomplete", "", "", ""),)),
                            ("results", BookPage((book(1),) * 1001, 1, 20)), ("details", book(1))):
            with self.subTest(kind=kind):
                self.assertFalse(valid_value(kind, value))

    async def test_mismatched_discovery_result_shows_the_unsupported_view_error(self):
        api = FakeAPI()
        api.popular = lambda **kwargs: [book(1)]
        app = CollectionsApp(api, initial_view="popular")
        async with app.run_test() as pilot:
            await self.ready(app, pilot)
            self.assertEqual("✗ Could not load popular books\nThe server returned an unsupported view. Retry, or go back.",
                             text(app, "#message"))
            self.assertIsNone(app.view.value)

    def test_view_model_titles_filters_and_orders_are_bounded_and_complete(self):
        self.assertEqual(SEARCH_ORDERS, tuple(value for value, _ in ORDERS))
        self.assertEqual(("favorites", "booklists", "history", "search", "popular", "recommended"), tuple(ROOTS))
        self.assertEqual(("home", *ROOTS), START_VIEWS)
        self.assertEqual("", filters_text(SearchFilters()))
        self.assertEqual("french, english · EPUB · 2000–2010 · Sort: Year", filters_text(SearchFilters(**FILTERS)))
        self.assertEqual("PDF, DjVu · from 1990", filters_text(SearchFilters(formats=("djvu", "pdf"), year_from=1990)))
        self.assertEqual("until 1900 · Sort: Smallest first", filters_text(SearchFilters(year_to=1900, order="filesizeA")))
        self.assertEqual("Sort: Popular", filters_text(SearchFilters(order="popular")))
        self.assertEqual("2001", filters_text(SearchFilters(year_from=2001, year_to=2001)))
        many = filters_text(SearchFilters(languages=tuple("l" * 60 + str(number).zfill(4) for number in range(50))))
        self.assertLessEqual(len(many), 160)
        self.assertTrue(many.endswith("…"))
        self.assertEqual("Search books", ViewState("search", "form").title)
        long = SearchRequest("[b]" + "x" * 600)
        self.assertEqual("Search: [b]" + "x" * 509, ViewState("search", "results", search=long).title)
        similar = ViewState("history", "similar", identifier="1:abc", label="T\x1b[31m‮")
        self.assertEqual("Similar to: T", similar.title)
        self.assertEqual("Booklists › Untitled booklist", ViewState("booklists", "members", label="\x1b[0m").title)
        state = ViewState("search", "results", 1, "", "", SearchRequest("x"))
        self.assertEqual(SearchRequest("x"), state.search)
        # Formats read as on the search form; others in capitals, without escape codes.
        self.assertEqual(["PDF", "EPUB", "DjVu", "DjVu", "MOBI", "FB2", ""],
                         [format_label(value) for value in ("pdf", "EPUB", "djvu", "DJVU", "mobi", "\x1b[31mfb2", "")])
        self.assertEqual("DjVu · 1 MB · 2020 · English", book_meta(Book("T", "A", "djvu", "", "2020", "English", "1 MB",
                                                                         "1", "abc")))


if __name__ == "__main__":
    unittest.main()
