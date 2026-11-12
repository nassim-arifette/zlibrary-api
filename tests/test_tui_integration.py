"""TUI navigation crosses the real local HTTP interface and catalog client, offline."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
import threading
import unittest

from zlib_connector import Book, BookDetails, BookPage, Booklist, BooklistBook
from zlib_connector.api_token import load_api_token
from zlib_connector.http_api import create_server
from zlib_connector.search import SearchFilters

try:
    import textual
except ModuleNotFoundError:
    textual = None

if textual is not None:
    from textual.widgets import Checkbox, DataTable, Input, Select
    from zlib_connector.catalog_api import CatalogAPI
    from zlib_connector.tui.app import CollectionsApp
    from zlib_connector.tui.search import SearchForm


class CollectionFixture:
    def __init__(self):
        self.calls = []
        self.books = tuple(Book(title, "Example author", "epub", f"https://books.example/book/{index}/abc",
                                year="2024", language="English", book_id=str(index), book_hash="abc")
                           for index, title in enumerate(("First book", "[bold red]Literal title[/bold red]", "Third book"), 1))

    def get_favorites(self, *, page, limit, **kwargs):
        self.calls.append(("favorites", page))
        return BookPage(self.books[:2] if page == 1 else self.books[2:], page, limit, 3, 2,
                        2 if page == 1 else None, 1 if page == 2 else None)

    def get_history(self, *, page, limit, **kwargs):
        self.calls.append(("history", page))
        return BookPage((self.books[2],), page, limit, 1, 1)

    def get_booklists(self):
        self.calls.append(("booklists", None))
        return [Booklist("5", "Reading", book_count=1, is_public=False)]

    def get_booklist_books(self, identifier, *, page):
        self.calls.append(("members", identifier))
        return BookPage((BooklistBook(**self.books[0].to_dict(), booklist_id=identifier, entry_id="99"),), page, 20)

    def get_book(self, reference):
        self.calls.append(("book", reference))
        book = self.books[(int(reference.split(":")[0]) - 1) % 3]
        return BookDetails(**{**book.to_dict(), "book_id": reference.split(":")[0]}, description="A synthetic description. " * 30,
                           publisher="Example press", pages="123", categories=("Fiction",))


class DiscoveryFixture(CollectionFixture):
    """Search pages and discovery lists in the shapes the upstream client returns."""

    def search_page(self, query, *, page, limit, filters):
        self.calls.append(("search", query, page, limit, filters))
        books = tuple(Book(f"{query} {page}.{index}", "Search author", "epub", "", year="1981", language="French",
                           book_id=str(page * 10 + index), book_hash="abc") for index in range(1, limit + 1))
        return BookPage(books, page, limit, 2 * limit, 2, 2 if page == 1 else None, 1 if page == 2 else None)

    def get_similar(self, reference):
        self.calls.append(("similar", reference))
        return [Book("[i]Similar[/i] one", "Similar author", "pdf", "", book_id="70", book_hash="abc")]

    def get_popular(self):
        self.calls.append(("popular", None))
        return [Book("Popular one", "Popular author", "epub", "", book_id="80", book_hash="abc"),
                Book("Popular two", "", "", "", book_id="81", book_hash="abc")]

    def get_recommendations(self):
        self.calls.append(("recommendations", None))
        return [Book("Recommended one", "Recommended author", "djvu", "", book_id="90", book_hash="abc")]


@unittest.skipIf(textual is None, "Install the optional TUI extra")
class CollectionsIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def wait_loaded(self, pilot, app):
        for _ in range(200):
            await pilot.pause(.01)
            if not app.loading:
                return
        self.fail("Collection request did not finish")

    @asynccontextmanager
    async def serving(self, upstream):
        """Yield a CatalogAPI bound to a real authenticated loopback server, and its token."""
        with tempfile.TemporaryDirectory() as folder:
            token_file = Path(folder) / "token"
            token = load_api_token(token_file)
            with create_server(upstream, token=token, port=0) as server:
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
                thread.start()
                try:
                    yield CatalogAPI(f"http://127.0.0.1:{server.server_port}/v1", token_file, timeout=3), token
                finally:
                    await asyncio.to_thread(server.shutdown)
                    thread.join(2)

    async def test_collections_details_and_back_cross_authenticated_http(self):
        upstream = CollectionFixture()
        async with self.serving(upstream) as (api, token):
            app = CollectionsApp(api, initial_view="favorites", page_size=2)
            async with app.run_test(size=(80, 24)) as pilot:
                await self.wait_loaded(pilot, app)
                table = app.query_one("#collection", DataTable)
                self.assertEqual(2, table.row_count)
                self.assertIn("[bold red]Literal title[/bold red]", str(table.get_row_at(1)))
                table.focus()
                await pilot.press("down", "enter")
                await self.wait_loaded(pilot, app)
                self.assertIn(("book", "2:abc"), upstream.calls)
                await pilot.press("escape")
                self.assertEqual(1, table.cursor_row)
                await pilot.press("n")
                await self.wait_loaded(pilot, app)
                self.assertEqual(1, table.row_count)
                self.assertIn(("favorites", 2), upstream.calls)
                await pilot.press("h")
                await self.wait_loaded(pilot, app)
                self.assertIn(("history", 1), upstream.calls)
                await pilot.press("b")
                await self.wait_loaded(pilot, app)
                self.assertIn(("booklists", None), upstream.calls)
                table.focus()
                await pilot.press("enter")
                await self.wait_loaded(pilot, app)
                self.assertIn(("members", "5"), upstream.calls)
                await pilot.press("enter")
                await self.wait_loaded(pilot, app)
                self.assertIn(("book", "1:abc"), upstream.calls)
                await pilot.press("escape", "escape", "f")
                await self.wait_loaded(pilot, app)
                self.assertEqual(2, app.view.page)
                self.assertTrue(all(name in {"favorites", "history", "booklists", "members", "book"}
                                    for name, _ in upstream.calls))
                self.assertNotIn(token, app.export_screenshot())

    async def test_search_paging_details_similar_and_discovery_cross_authenticated_http(self):
        upstream = DiscoveryFixture()
        expected = SearchFilters(languages=("french", "english"), formats=("epub",), year_from=1960, year_to=1990,
                                 order="year")
        async with self.serving(upstream) as (api, token):
            app = CollectionsApp(api, page_size=2)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                self.assertEqual("home", app.view.kind)
                await pilot.press("s")
                await pilot.pause()
                form = app.query_one(SearchForm)
                await pilot.press(*"dune")
                form.query_one("#search-languages", Input).value = "french, English"
                form.query_one("#search-year-from", Input).value = "1960"
                form.query_one("#search-year-to", Input).value = "1990"
                form.query_one("#search-format-epub", Checkbox).value = True
                form.query_one("#search-order", Select).value = "year"
                await pilot.pause()
                self.assertEqual([], upstream.calls)
                await pilot.press("ctrl+s")
                await self.wait_loaded(pilot, app)
                self.assertEqual([("search", "dune", 1, 2, expected)], upstream.calls)
                # At 80 columns the sort order, last in the summary, gives way.
                self.assertEqual("Page 1 of 2 · Book 1 of 2 · 4 books · french, english · EPUB · 1960–1990 · …",
                                 str(app.query_one("#status").render()))
                await pilot.press("n")
                await self.wait_loaded(pilot, app)
                self.assertEqual(("search", "dune", 2, 2, expected), upstream.calls[-1])
                table = app.query_one("#collection", DataTable)
                self.assertEqual(["3", "dune 2.1"], [cell.plain for cell in table.get_row_at(0)[:2]])
                await pilot.press("down", "enter")
                await self.wait_loaded(pilot, app)
                self.assertEqual(("book", "22:abc"), upstream.calls[-1])
                self.assertEqual("Search: dune › Book details", str(app.query_one("#status").render()))
                await pilot.press("m")
                await self.wait_loaded(pilot, app)
                self.assertEqual(("similar", "22:abc"), upstream.calls[-1])
                self.assertEqual("[i]Similar[/i] one", table.get_row_at(0)[1].plain)
                await pilot.press("escape", "escape")
                await pilot.pause()
                self.assertEqual(("results", 2, 1), (app.view.kind, app.view.value.page, table.cursor_row))
                await pilot.press("5")
                await self.wait_loaded(pilot, app)
                self.assertEqual(("popular", None), upstream.calls[-1])
                self.assertEqual(["Popular one", "Popular two"], [table.get_row_at(index)[1].plain for index in range(2)])
                # The placeholder for a missing author is dim, so it does not read as an author's name.
                self.assertEqual(("Unknown", "dim"), (table.get_row_at(1)[2].plain, str(table.get_row_at(1)[2].style)))
                self.assertEqual("", str(table.get_row_at(0)[2].style))
                await pilot.press("6")
                await self.wait_loaded(pilot, app)
                self.assertEqual(("recommendations", None), upstream.calls[-1])
                self.assertEqual("Recommended one", str(app.query_one("#card").render()).splitlines()[0])
                await pilot.press("s")
                await pilot.pause()
                self.assertEqual(("results", 2), (app.view.kind, app.view.value.page))
                self.assertEqual(6, len(upstream.calls))
                self.assertNotIn(token, app.export_screenshot())
