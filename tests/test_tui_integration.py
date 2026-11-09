"""Collections navigation crosses the real local HTTP interface, offline."""
import asyncio
from pathlib import Path
import tempfile
import threading
import unittest

from zlib_connector import Book, BookDetails, BookPage, Booklist, BooklistBook
from zlib_connector.api_token import load_api_token
from zlib_connector.http_api import create_server

try:
    import textual
except ModuleNotFoundError:
    textual = None

if textual is not None:
    from textual.widgets import DataTable
    from zlib_connector.catalog_api import CatalogAPI
    from zlib_connector.tui.app import CollectionsApp


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
        book = self.books[int(reference.split(":")[0]) - 1]
        return BookDetails(**book.to_dict(), description="A synthetic description. " * 30,
                           publisher="Example press", pages="123", categories=("Fiction",))


@unittest.skipIf(textual is None, "Install the optional TUI extra")
class CollectionsIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def wait_loaded(self, pilot, app):
        for _ in range(200):
            await pilot.pause(.01)
            if not app.loading:
                return
        self.fail("Collection request did not finish")

    async def test_collections_details_and_back_cross_authenticated_http(self):
        with tempfile.TemporaryDirectory() as folder:
            token_file = Path(folder) / "token"
            token = load_api_token(token_file)
            upstream = CollectionFixture()
            with create_server(upstream, token=token, port=0) as server:
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
                thread.start()
                try:
                    api = CatalogAPI(f"http://127.0.0.1:{server.server_port}/v1", token_file, timeout=3)
                    app = CollectionsApp(api, page_size=2)
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
                finally:
                    await asyncio.to_thread(server.shutdown)
                    thread.join(2)
