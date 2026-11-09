"""Read-only collection navigation through an existing local API server."""
import asyncio

from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Static

from ..booklists import Booklist
from ..books import BookDetails, BookReference
from ..pagination import BookPage, validate_page
from .loader import BoundedLoader, LoadResult
from .views import ROOTS, ViewState, details_text, literal


class Loaded(Message):
    def __init__(self, result: LoadResult):
        self.result = result
        super().__init__()


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,f1,question_mark", "dismiss", "Close help", priority=True)]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #help-box { width: 64; max-width: 95%; height: 85%; border: round $accent; background: $surface; padding: 1 2; }
    #help-text { height: auto; }
    #close-help { width: 100%; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            with VerticalScroll():
                yield Static("Collections\n\nF / 1  Favorites\nB / 2  Booklists\nH / 3  Download history\n\n"
                             "Up / Down  Select a row\nEnter  Open a list or book\nEsc  Back to the previous view\n"
                             "P / N  Previous / next page\nR  Refresh this view\nCtrl+P  Commands and themes\nQ  Quit\n\n"
                             "Download history belongs to your Z-Library account.\n\n"
                             "Changing views stops waiting for obsolete local requests. A request already accepted by the server may still finish.",
                             id="help-text", markup=False)
            yield Button("Close help", id="close-help")

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss()


class CollectionsApp(App):
    """The adapter is invoked only by one background loader, never by UI handlers."""

    TITLE = "Z-Library collections"
    CSS = """
    Screen { layout: vertical; }
    #roots { height: 3; }
    #roots Button { width: 1fr; min-width: 0; padding: 0; }
    #view-title { height: 1; padding: 0 1; text-style: bold; }
    #status { height: auto; min-height: 1; max-height: 3; padding: 0 1; color: $text-muted; }
    #body { height: 1fr; min-height: 1; }
    #collection { height: 1fr; }
    #detail-view { height: 1fr; padding: 0 1; }
    #detail-text { height: auto; }
    #navigation { height: 3; }
    #navigation Button { min-width: 8; width: 8; padding: 0; }
    #navigation #refresh { min-width: 11; width: 11; }
    #page-label { width: 1fr; height: 3; content-align: center middle; }
    Footer { height: 1; }
    """
    BINDINGS = [Binding("q", "quit", "Quit"),
                Binding("escape", "back", "Back"),
                Binding("f1,question_mark", "help", "Help", key_display="?"),
                Binding("f,1", "collection('favorites')", "Favorites", show=False),
                Binding("b,2", "collection('booklists')", "Booklists", show=False),
                Binding("h,3", "collection('history')", "History", show=False),
                Binding("p", "previous_page", "Previous page", show=False),
                Binding("n", "next_page", "Next page", show=False),
                Binding("r", "refresh", "Refresh", show=False)]

    def __init__(self, api, *, initial_view="favorites", page_size=20):
        if initial_view not in ROOTS:
            raise ValueError("Choose favorites, booklists or history.")
        validate_page(1, page_size)
        super().__init__()
        self.api = api
        self.page_size = page_size
        self._root = initial_view
        self._roots = {name: [ViewState(name, "booklists" if name == "booklists" else "books")]
                       for name in ROOTS}
        self._generation = 0
        self._loading = False
        self._stopping = False
        self._loader = BoundedLoader(lambda result: self.post_message(Loaded(result)))

    @property
    def view(self) -> ViewState:
        return self._roots[self._root][-1]

    @property
    def loading(self) -> bool:
        return self._loading

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="roots"):
            yield Button("F Favorites", id="favorites")
            yield Button("B Booklists", id="booklists")
            yield Button("H History", id="history")
        yield Static("", id="view-title", markup=False)
        yield Static("", id="status", markup=False)
        with Vertical(id="body"):
            yield DataTable(id="collection", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="detail-view", can_focus=True):
                yield Static("", id="detail-text", markup=False)
        with Horizontal(id="navigation"):
            yield Button("Back", id="back")
            yield Button("Prev", id="previous")
            yield Static("", id="page-label", markup=False)
            yield Button("Next", id="next")
            yield Button("Refresh", id="refresh")
        yield Footer()

    def on_mount(self):
        self._load()

    async def on_unmount(self):
        self._stopping = True
        await asyncio.to_thread(self._loader.close)

    async def action_quit(self):
        self._stopping = True
        self._loader.cancel()
        self.exit()

    def _save_position(self):
        if self._loading or self.view.value is None:
            return
        widget = self.query_one("#detail-view" if self.view.kind == "details" else "#collection")
        self.view.scroll_x, self.view.scroll_y = widget.scroll_x, widget.scroll_y
        if isinstance(widget, DataTable):
            self.view.cursor = widget.cursor_row

    def _restore_position(self, generation):
        if generation != self._generation or self._stopping:
            return
        widget = self.query_one("#detail-view" if self.view.kind == "details" else "#collection")
        if isinstance(widget, DataTable):
            widget.move_cursor(row=self.view.cursor, scroll=False)
        widget.scroll_to(x=self.view.scroll_x, y=self.view.scroll_y, animate=False, immediate=True, force=True)

    def _read(self, state):
        if state.kind == "booklists":
            return lambda cancel: self.api.booklists(cancel=cancel)
        if state.kind == "members":
            return lambda cancel: self.api.booklist_books(state.identifier, page=state.page, cancel=cancel)
        if state.kind == "details":
            return lambda cancel: self.api.book(state.identifier, cancel=cancel)
        method = self.api.favorites if state.root == "favorites" else self.api.history
        return lambda cancel: method(page=state.page, limit=self.page_size, cancel=cancel)

    def _load(self):
        self._generation += 1
        self._loading = True
        self.view.error = None
        self._render()
        self._loader.submit(self._generation, self._read(self.view))

    def on_loaded(self, message: Loaded):
        result = message.result
        if result.generation != self._generation or self._stopping:
            return
        self._loading = False
        self.view.error = result.error
        if result.error is None:
            value = result.value
            valid = (isinstance(value, BookDetails) if self.view.kind == "details" else
                     isinstance(value, tuple) and len(value) <= 1000 and all(isinstance(item, Booklist) for item in value)
                     if self.view.kind == "booklists" else isinstance(value, BookPage) and len(value.books) <= 1000)
            if not valid:
                self.view.error = "The server returned an unsupported view. Refresh to try again."
            else:
                self.view.value = value
        self._render()

    def _render(self):
        state = self.view
        for name in ROOTS:
            self.query_one("#" + name, Button).variant = "primary" if name == self._root else "default"
        self.query_one("#view-title", Static).update(state.title)
        table = self.query_one("#collection", DataTable)
        detail = self.query_one("#detail-view", VerticalScroll)
        detail.display = state.kind == "details"
        table.display = state.kind != "details"
        table.disabled = self._loading or state.error is not None
        status = "Loading…" if self._loading else state.error
        page_text = ""
        previous = following = None
        if state.kind == "details":
            self.query_one("#detail-text", Static).update(details_text(state.value) if isinstance(state.value, BookDetails) else "")
            if not self._loading:
                detail.focus()
        else:
            table.clear(columns=True)
            width = max(30, self.size.width - 6)
            if state.kind == "booklists":
                table.add_column("Booklist", width=max(12, width - 20))
                table.add_column("Books", width=7)
                table.add_column("Access", width=9)
                rows = state.value or ()
                for item in rows:
                    table.add_row(literal(item.title), "Unknown" if item.book_count is None else str(item.book_count),
                                  "Unknown" if item.is_public is None else "Public" if item.is_public else "Private")
                page_text = f"{len(rows)} lists" if state.value is not None else ""
            else:
                table.add_column("Title", width=max(12, int(width * .6) - 3))
                table.add_column("Author", width=max(8, int(width * .4) - 3))
                table.add_column("Format", width=6)
                rows = state.value.books if isinstance(state.value, BookPage) else ()
                for item in rows:
                    table.add_row(literal(item.title), literal(item.authors), literal(item.extension))
                if isinstance(state.value, BookPage):
                    page = state.value
                    previous, following = page.previous_page, page.next_page
                    page_text = f"Page {page.page}" + (f" / {page.total_pages}" if page.total_pages is not None else " / ?")
                    if status is None and page.total_items is not None:
                        status = f"{page.total_items} books in this collection."
            if not rows and not self._loading and state.error is None:
                status = "No booklists here yet." if state.kind == "booklists" else "No books on this page."
            if self._root == "history" and not self._loading and state.error is None:
                status = "Z-Library account download history." + (" " + status if status else "")
            if not self._loading:
                table.focus()
        hint = "Scroll for details. Esc returns to the collection." if state.kind == "details" else "Enter opens the selected item."
        self.query_one("#status", Static).update(status or hint)
        self.query_one("#page-label", Static).update(page_text)
        self.query_one("#back", Button).disabled = len(self._roots[self._root]) == 1
        blocked = self._loading or state.error is not None
        self.query_one("#previous", Button).disabled = blocked or previous is None
        self.query_one("#next", Button).disabled = blocked or following is None
        self.call_after_refresh(self._restore_position, self._generation)

    def on_resize(self):
        if self.is_mounted:
            self._save_position()
            self._render()

    def action_collection(self, name):
        if name not in ROOTS or name == self._root:
            return
        self._save_position()
        self._generation += 1
        self._loader.cancel()
        self._root = name
        self._loading = False
        if self.view.value is None:
            self._load()
        else:
            self._render()

    def action_refresh(self):
        self._save_position()
        self._load()

    def action_back(self):
        if len(self.screen_stack) > 1:
            self.pop_screen()
            return
        if len(self._roots[self._root]) > 1:
            self._generation += 1
            self._loader.cancel()
            self._roots[self._root].pop()
            self._loading = False
            self._render()

    def _page(self, direction):
        state = self.view
        if self._loading or state.error is not None or not isinstance(state.value, BookPage):
            return
        page = state.value.next_page if direction == "next" else state.value.previous_page
        if page is None:
            return
        self._roots[self._root][-1] = ViewState(state.root, state.kind, page, state.identifier, state.label)
        self._load()

    def action_previous_page(self):
        self._page("previous")

    def action_next_page(self):
        self._page("next")

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        if self._loading or self.view.error is not None:
            return
        self._save_position()
        state = self.view
        index = event.cursor_row
        if state.kind == "booklists" and isinstance(state.value, tuple) and 0 <= index < len(state.value):
            item = state.value[index]
            child = ViewState(self._root, "members", identifier=item.booklist_id, label=item.title)
        elif isinstance(state.value, BookPage) and 0 <= index < len(state.value.books):
            item = state.value.books[index]
            child = ViewState(self._root, "details", identifier=str(BookReference(item.book_id, item.book_hash)))
        else:
            return
        self._roots[self._root].append(child)
        self._load()

    def action_help(self):
        self.push_screen(HelpScreen())

    def on_button_pressed(self, event: Button.Pressed):
        name = event.button.id
        if name in ROOTS:
            self.action_collection(name)
        elif name == "back":
            self.action_back()
        elif name == "previous":
            self.action_previous_page()
        elif name == "next":
            self.action_next_page()
        elif name == "refresh":
            self.action_refresh()

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        for name, title in ROOTS.items():
            yield SystemCommand(title, "Open this collection", lambda name=name: self.action_collection(name))
        yield SystemCommand("Refresh collection", "Reload the current view", self.action_refresh)
        yield SystemCommand("Back", "Return to the previous collection view", self.action_back)
        yield SystemCommand("Collection help", "Show navigation keys", self.action_help)
