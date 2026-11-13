"""Collections, search, discovery and confirmed downloads through a local API server."""
import asyncio
import time
from dataclasses import replace

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import CommandPalette
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.theme import ThemeProvider
from textual.widgets import DataTable, Input, Static

from ..booklists import Booklist
from ..books import BookDetails
from ..pagination import BookPage, validate_page
from .commands import CommandDialog, CommandsProvider
from .downloads import DownloadDialog, DownloadRunner, DownloadTarget, DownloadUpdate
from .home import HomePage
from .loader import BoundedLoader, LoadResult
from .search import SearchForm
from .views import (BOOK_KINDS, EMPTY, MAX_DEPTH, NO_DESCRIPTION, PAGE_KINDS, ROOTS, START_VIEWS, SearchRequest,
                    ViewState, book_meta, book_reference, booklist_meta, description_lines, detail_description,
                    detail_fields, detail_header, filters_text, fit_summary, format_label, literal, loading_text,
                    position_text, root_view, safe_text, split_columns, status_extras, valid_value)
from .widgets import BookTable, Card, HelpScreen, HintBar


__all__ = ["CollectionsApp", "HelpScreen", "Loaded"]

DEFAULT_THEME = "tokyo-night"
THEME_PALETTE = "--theme-palette"
SPINNER = "⣾⣽⣻⢿⡿⣟⣯⣷"
MIN_SIZE = (40, 12)
DETAIL_LABELS = ("Publisher", "Edition", "Pages", "Year", "Language", "ISBN-10", "ISBN-13", "Identifier",
                 "Categories", "Saved", "Reference")
# Single-key shortcuts; the search page and the size prompt disable them.
SHORTCUTS = {"open_root", "search", "similar", "previous_page", "next_page", "refresh", "quit_shortcut", "download",
             "cancel_download"}
QUIT_AGAIN_SECONDS = 5


class Loaded(Message):
    def __init__(self, result: LoadResult):
        self.result = result
        super().__init__()


class DownloadProgress(Message):
    def __init__(self, update: DownloadUpdate):
        self.update = update
        super().__init__()


class CollectionsApp(App):
    """Collections, search and discovery; the class keeps its original name.

    The adapter is invoked only by one background loader, never by UI handlers.
    Each section (root) keeps its own stack of views; Home holds none and reads
    nothing.
    """

    TITLE = "Z-Library"
    # Focus follows the page (_render); a hidden search field must never take keys.
    AUTO_FOCUS = None
    COMMANDS = {CommandsProvider}
    CSS = """
    #header { height: auto; padding: 0 1; }
    #title-row { height: 1; }
    #view-title { width: 1fr; height: 1; color: $text-accent; text-style: bold; text-wrap: nowrap; text-overflow: ellipsis; }
    #notice { width: auto; max-width: 80%; height: 1; padding-left: 2; color: $text-error; text-wrap: nowrap;
              text-overflow: ellipsis; }
    #status { height: 1; color: $foreground-muted; text-wrap: nowrap; text-overflow: ellipsis; }
    #list-page { height: 1fr; padding: 0 1; }
    #card-gap { height: 1; }
    .message { height: auto; margin-top: 1; }
    /* Under an empty status line, that line already separates the message from the title. */
    #message.-flush { margin-top: 0; }
    #detail-view { height: 1fr; padding: 0 1; overflow-x: hidden; scrollbar-size-vertical: 1;
                   scrollbar-background: $background; scrollbar-background-hover: $background;
                   scrollbar-background-active: $background; }
    #detail-view:focus { background-tint: $foreground 0%; }
    #detail-body { max-width: 100; height: auto; padding-right: 1; }
    #detail-header { height: auto; margin-top: 1; }
    #detail-fields { height: auto; margin-top: 1; }
    .detail-row { height: auto; }
    .detail-label { width: 12; color: $foreground-muted; }
    .detail-value { width: 1fr; height: auto; }
    #detail-description-title { height: 1; margin-top: 1; text-style: bold; }
    #detail-description { height: auto; }
    #footer { dock: bottom; height: auto; }
    #download-line { height: 1; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #too-small { width: 1fr; height: 1fr; content-align: center middle; text-align: center; color: $foreground-muted; }
    Screen.-roomy #list-page, Screen.-roomy #detail-view { padding: 0 1 1 1; }
    CommandPalette > Vertical { width: 64; max-width: 100%; margin-top: 2; }
    CommandPalette #--input { border: none; height: auto; padding: 1 1; }
    CommandPalette SearchIcon { display: none; }
    CommandPalette CommandInput, CommandPalette CommandInput:focus { border: none; height: 1; padding: 0 1; }
    CommandPalette CommandList, CommandPalette CommandList:focus {
        border: none; padding-bottom: 1; scrollbar-size-vertical: 1; scrollbar-background: $surface;
        scrollbar-background-hover: $surface; scrollbar-background-active: $surface;
        &:dark { scrollbar-background: $panel-darken-1; scrollbar-background-hover: $panel-darken-1;
                 scrollbar-background-active: $panel-darken-1; }
    }
    """
    # Ctrl+C quits like Ctrl+Q; an input or the screen copies instead when text is selected. `/` is a priority
    # binding, so its dialog opens before the keys typed after it are dispatched and none of them is lost.
    BINDINGS = [Binding("q", "quit_shortcut", "Quit", show=False),
                Binding("ctrl+c", "quit", "Quit", show=False),
                Binding("escape", "back", "Back", show=False),
                Binding("f1,question_mark", "help", "Help", show=False),
                Binding("slash", "commands", "Commands", show=False, priority=True),
                Binding("ctrl+p", "command_palette", "Commands", show=False, priority=True),
                Binding("f,1", "open_root('favorites')", "Favorites", show=False),
                Binding("b,2", "open_root('booklists')", "Booklists", show=False),
                Binding("h,3", "open_root('history')", "Download history", show=False),
                Binding("s,4", "search", "Search", show=False),
                Binding("5", "open_root('popular')", "Popular", show=False),
                Binding("6", "open_root('recommended')", "Recommended", show=False),
                Binding("m", "similar", "More like this", show=False),
                Binding("d", "download", "Download", show=False),
                Binding("x", "cancel_download", "Cancel download", show=False),
                Binding("left,p", "previous_page", "Previous page", show=False),
                Binding("right,n", "next_page", "Next page", show=False),
                Binding("r", "refresh", "Refresh", show=False)]

    def __init__(self, api, *, initial_view="home", page_size=20, download_dir=None):
        if initial_view not in START_VIEWS:
            raise ValueError("Choose home, favorites, booklists, history, search, popular or recommended.")
        validate_page(1, page_size)
        super().__init__()
        self._running_command = False
        self.theme = DEFAULT_THEME
        self.api = api
        self.page_size = page_size
        self._root = initial_view
        self._home = ViewState("home", "home")
        self._roots = {name: [root_view(name)] for name in ROOTS}
        self._form_open = False  # The search form covers retained results.
        self._focus_form = False
        self._generation = 0
        self._loading = False
        self._stopping = False
        self._spin = 0
        self._loader = BoundedLoader(lambda result: self.post_message(Loaded(result)))
        # Downloads need a folder; without one, d says that they are off.
        self.download_dir = download_dir
        self._downloads = (None if download_dir is None else
                           DownloadRunner(api, download_dir, lambda update: self.post_message(DownloadProgress(update))))
        self._quit_armed = -QUIT_AGAIN_SECONDS - 1.0

    @property
    def view(self) -> ViewState:
        if self._root == "home":
            return self._home
        stack = self._roots[self._root]
        return stack[0] if self._root == "search" and self._form_open else stack[-1]

    @property
    def loading(self) -> bool:
        return self._loading

    @property
    def search_request(self) -> SearchRequest | None:
        stack = self._roots["search"]
        return stack[1].search if len(stack) > 1 else None

    @property
    def _live(self) -> bool:
        """Running and not shutting down: widgets may be updated."""
        return self.is_running and not self._stopping

    @property
    def too_small(self) -> bool:
        width, height = self.size
        return width < MIN_SIZE[0] or height < MIN_SIZE[1]

    def compose(self) -> ComposeResult:
        with Vertical(id="header"):
            with Horizontal(id="title-row"):
                yield Static("", id="view-title", markup=False)
                yield Static("", id="notice", markup=False)
            yield Static("", id="status", markup=False)
        yield HomePage(id="home")
        yield SearchForm(page_size=self.page_size, id="search-page")
        with Vertical(id="list-page"):
            yield Card("", id="card", markup=False)
            yield Static("", id="card-gap", markup=False)
            yield BookTable(id="collection")
            yield Static("", id="message", classes="message", markup=False)
        with VerticalScroll(id="detail-view", can_focus=True):
            yield Static("", id="detail-message", classes="message", markup=False)
            with Vertical(id="detail-body"):
                yield Static("", id="detail-header", markup=False)
                with Vertical(id="detail-fields"):
                    for label in DETAIL_LABELS:
                        with Horizontal(classes="detail-row", id="field-" + label.lower()):
                            yield Static(label, classes="detail-label", markup=False)
                            yield Static("", classes="detail-value", markup=False)
                yield Static("Description", id="detail-description-title", markup=False)
                yield Static("", id="detail-description", markup=False)
        yield Static(f"Enlarge the terminal to at least {MIN_SIZE[0]}×{MIN_SIZE[1]} · q quit", id="too-small",
                     markup=False)
        with Vertical(id="footer"):
            yield Static("", id="download-line", markup=False)
            yield HintBar(id="hints")

    def on_mount(self):
        self.query_one("#download-line").display = False
        self._spinner = self.set_interval(.08, self._tick, pause=True)
        self._focus_form = self.view.kind == "form"
        # Opening or closing a dialog (commands, themes, help) switches the key hints to its keys and back.
        self.screen_change_signal.subscribe(self, lambda screen: self._show_hints())
        self._load()

    async def on_unmount(self):
        self._stopping = True
        await asyncio.to_thread(self._loader.close)
        if self._downloads is not None:
            await asyncio.to_thread(self._downloads.close)

    async def action_quit(self):
        # Quitting stops a running download: the first press says so, a second one quits.
        if (self._downloads is not None and self._downloads.active
                and time.monotonic() - self._quit_armed > QUIT_AGAIN_SECONDS):
            self._quit_armed = time.monotonic()
            self._notice("A download is running. Press q again to stop it and quit.")
            return
        self._stopping = True
        self._loader.cancel()
        self.exit()

    async def action_quit_shortcut(self):
        await self.action_quit()

    def check_action(self, action: str, parameters) -> bool | None:
        if not self.is_mounted:
            return True
        if self.too_small:
            return action in ("quit", "quit_shortcut")
        if action == "commands":
            # `/` types a slash in a field that takes one, and does nothing over a dialog.
            focused = self.focused
            return self.screen is self.default_screen and (focused is None or not focused.check_consume_key("slash", "/"))
        # Typed keys never trigger shortcuts on the search page, but a command chosen in the dialog still runs.
        return action not in SHORTCUTS or self.view.kind != "form" or self._running_command

    async def run_command(self, action: str) -> None:
        """Run an action chosen in the command dialog, even where its single-key binding is off."""
        self._running_command = True
        try:
            await self.run_action(action)
        finally:
            self._running_command = False

    async def on_event(self, event: events.Event) -> None:
        # A transient notice lasts until the next key press.
        if isinstance(event, events.Key) and self._live and str(self.query_one("#notice", Static).render()):
            self._notice("")
        await super().on_event(event)

    def _notice(self, text: str) -> None:
        self.query_one("#notice", Static).update(text)

    def action_command_palette(self) -> None:
        if not CommandPalette.is_open(self):
            self.push_screen(CommandDialog(placeholder="Type a command", id="--command-palette"))

    def action_commands(self) -> None:
        self.action_command_palette()

    def action_change_theme(self) -> None:
        """Textual's theme picker, named so the key hints can say what Enter does there."""
        if not CommandPalette.is_open(self):
            self.push_screen(CommandDialog(providers=[ThemeProvider], placeholder="Search for themes…", id=THEME_PALETTE))

    def command_available(self, name: str) -> bool:
        if name == "/similar":
            return self._similar_target() is not None
        if name == "/download":
            return self._downloads is not None and self._download_target() is not None
        if name == "/refresh":
            return self.view.kind not in ("home", "form")
        return True

    # Positions and loading

    def _save_position(self):
        state = self.view
        if self._loading or state.value is None or state.error is not None or state.kind in ("home", "form"):
            return
        widget = self.query_one("#detail-view" if state.kind == "details" else "#collection")
        if not widget.display:
            return
        state.scroll_x, state.scroll_y = widget.scroll_x, widget.scroll_y
        if isinstance(widget, DataTable):
            state.cursor = widget.cursor_row

    def _restore_position(self, generation):
        state = self.view
        if generation != self._generation or not self._live or state.kind in ("home", "form"):
            return
        widget = self.query_one("#detail-view" if state.kind == "details" else "#collection")
        if isinstance(widget, DataTable):
            widget.move_cursor(row=state.cursor, scroll=False)
        widget.scroll_to(x=state.scroll_x, y=state.scroll_y, animate=False, immediate=True, force=True)
        self._show_selection()

    def _read(self, state):
        """Return the adapter read for a view, or None when there is nothing to load."""
        page, identifier, search, limit = state.page, state.identifier, state.search, self.page_size
        if state.kind in ("home", "form"):
            return None
        if state.kind == "booklists":
            return lambda cancel: self.api.booklists(cancel=cancel)
        if state.kind == "members":
            return lambda cancel: self.api.booklist_books(identifier, page=page, cancel=cancel)
        if state.kind == "details":
            return lambda cancel: self.api.book(identifier, cancel=cancel)
        if state.kind == "similar":
            return lambda cancel: self.api.similar(identifier, cancel=cancel)
        if state.kind == "popular":
            return lambda cancel: self.api.popular(cancel=cancel)
        if state.kind == "recommended":
            return lambda cancel: self.api.recommended(cancel=cancel)
        if state.kind == "results":
            return lambda cancel: self.api.search(search.query, page=page, limit=limit, filters=search.filters, cancel=cancel)
        method = self.api.favorites if state.root == "favorites" else self.api.history
        return lambda cancel: method(page=page, limit=limit, cancel=cancel)

    def _load(self):
        if self._stopping:
            return
        self._generation += 1
        read = self._read(self.view)
        self._loading = read is not None
        self.view.error = None
        if read is None:
            self._loader.cancel()
        self._render()
        if read is not None:
            self._loader.submit(self._generation, read)

    def on_loaded(self, message: Loaded):
        result = message.result
        if result.generation != self._generation or not self._live:
            return
        self._loading = False
        self.view.error = result.error
        if result.error is None:
            if valid_value(self.view.kind, result.value):
                self.view.value = result.value
            else:
                self.view.error = "The server returned an unsupported view. Retry, or go back."
        self._render()

    def _tick(self):
        if not self._loading or not self._live:
            self._spinner.pause()
            return
        self._spin = (self._spin + 1) % len(SPINNER)
        self._show_status()

    # Rendering

    def _page_name(self, state) -> str:
        if self.too_small:
            return "small"
        return {"home": "home", "form": "search-page", "details": "detail-view"}.get(state.kind, "list-page")

    def _render(self):
        if not self._live:
            return
        state, (width, height) = self.view, self.size
        page = self._page_name(state)
        for name in ("home", "search-page", "list-page", "detail-view"):
            self.query_one("#" + name).display = name == page
        self.query_one("#too-small").display = page == "small"
        self.query_one("#header").display = page in ("search-page", "list-page", "detail-view")
        self.query_one("#hints").display = page != "small"
        self.default_screen.set_class(height >= 24, "-roomy")
        self.query_one("#view-title", Static).update(state.title, layout=False)
        focus = None
        if page == "small":
            prompt = f"Enlarge the terminal to at least {MIN_SIZE[0]}×{MIN_SIZE[1]}"
            self.query_one("#too-small", Static).update(prompt + (" · q quit" if width > len(prompt) + 8 else "\nq quit"))
        elif page == "home":
            self.query_one(HomePage).fit(width, height)
        elif page == "search-page":
            focus = self._render_form()
        elif page == "list-page":
            focus = self._render_list(state, width, height)
        elif page == "detail-view":
            focus = self._render_details(state)
        if self._loading:
            self._spinner.resume()
        self._show_status()
        self._show_hints()
        if focus is not None:
            focus.focus()
        elif page != "search-page":
            self.default_screen.set_focus(None)
        self.call_after_refresh(self._restore_position, self._generation)

    def _render_form(self):
        form = self.query_one(SearchForm)
        form.fit(self.size.height)
        if self._focus_form:
            self._focus_form = False
            form.load(self.search_request)
            return form.query_one("#search-query", Input)
        focused = self.default_screen.focused
        return None if focused is not None and form in focused.ancestors else form.query_one("#search-query", Input)

    def _render_list(self, state, width, height):
        table, card, message = self.query_one(BookTable), self.query_one(Card), self.query_one("#message", Static)
        rows, shown = state.rows, state.error is None and bool(state.rows)
        if state.error is not None:
            message.update(self._failure(state))
        elif state.value is not None and not rows:
            message.update(self._empty(state))
        message.display = not shown and (state.error is not None or state.value is not None)
        table.display = shown
        card.display = shown and height >= 16
        self.query_one("#card-gap").display = shown and height >= 24
        card.set_class(height < 20, "-compact")
        card.styles.height = 3 if height < 20 else self._card_lines(state) + 2
        if shown:
            self._fill_table(state, width)
            table.move_cursor(row=state.cursor, scroll=False)
            self._show_selection()
        return table if shown and not self._loading else None

    @staticmethod
    def _empty(state) -> Content:
        page = state.value if isinstance(state.value, BookPage) else None
        if page is not None and page.page > 1:
            return Content.styled("No books on this page.", "$foreground-muted")
        key = state.root if state.kind == "books" else state.kind
        if state.kind != "results" or state.search is None:
            return Content.styled(EMPTY[key], "$foreground-muted")
        # A sort order never removes books, so only the filters are offered for loosening.
        filters = filters_text(replace(state.search.filters, order=None))
        advice = ("Try another title, author or ISBN, or loosen the filters: " + filters if filters
                  else "Try another title, author or ISBN.")
        return Content.assemble((EMPTY[key], "$foreground"), "\n", (advice, "$foreground-muted"))

    def _fill_table(self, state, width):
        table = self.query_one(BookTable)
        table.clear(columns=True)
        usable = width - 2
        rows = state.rows
        if state.kind == "booklists":
            number = len(str(len(rows)))
            table.add_column(Text("#", justify="right"), width=number)
            table.add_column(Text("Booklist"), width=max(8, usable - number - 7 - 7 - 8))
            table.add_column(Text("Books", justify="right"), width=7)
            table.add_column(Text("Access"), width=7)
            for index, item in enumerate(rows):
                # Unknown counts and visibility stay quiet, as in the card, which leaves them out.
                table.add_row(Text(str(index + 1), justify="right"), literal(item.title, fallback="Untitled booklist"),
                              Text("–", style="dim", justify="right") if item.book_count is None
                              else Text(str(item.book_count), justify="right"),
                              Text("–", style="dim") if item.is_public is None
                              else Text("Public" if item.is_public else "Private"))
            return
        page = state.value if isinstance(state.value, BookPage) else None
        offset = (page.page - 1) * page.limit if page is not None else 0
        number = len(str(offset + len(rows)))
        names = [(literal(item.title), literal(item.authors)) for item in rows]
        lengths = [cell.cell_len for cell, _ in names]
        needs = (max(lengths), max(cell.cell_len for _, cell in names))
        # Each column adds two cells of padding. Format and Year (14 cells) give way when they would shorten a
        # title that fits without them; a title too long for either layout does not hide them.
        cells = usable - (number + 2) - 4
        wide = False
        if width >= 100:
            narrow, full = split_columns(cells - 14, *needs)[0], split_columns(cells, *needs)[0]
            wide = not any(narrow < length <= full for length in lengths)
        title, author = split_columns(cells - (14 if wide else 0), *needs)
        table.add_column(Text("#", justify="right"), width=number)
        table.add_column(Text("Title"), width=title)
        table.add_column(Text("Author"), width=author)
        if wide:
            table.add_column(Text("Format"), width=6)
            table.add_column(Text("Year"), width=4)
        for index, (item, (title_cell, author_cell)) in enumerate(zip(rows, names)):
            cells = [Text(str(offset + index + 1), justify="right"), title_cell, author_cell]
            if wide:
                cells += [literal(format_label(item.extension), fallback=""), literal(item.year, fallback="")]
            table.add_row(*cells)

    def _render_details(self, state):
        book = state.value if isinstance(state.value, BookDetails) else None
        body, message = self.query_one("#detail-body"), self.query_one("#detail-message", Static)
        body.display = book is not None and state.error is None
        message.display = state.error is not None
        if state.error is not None:
            message.update(self._failure(state))
        if not body.display:
            return None
        title, authors, meta = detail_header(book)
        lines = [Content.styled(title, "bold $text-accent")]
        lines += [Content.styled(text, style) for text, style in ((authors, "$foreground"), (meta, "$foreground-muted")) if text]
        self.query_one("#detail-header", Static).update(Content("\n").join(lines))
        values = dict(detail_fields(book))
        for label in DETAIL_LABELS:
            row = self.query_one("#field-" + label.lower())
            row.display = label in values
            row.query_one(".detail-value", Static).update(values.get(label, ""))
        self.query_one("#detail-description", Static).update(
            detail_description(book) or Content.styled(NO_DESCRIPTION, "$foreground-muted"))
        # Whether the page scrolls, and so whether the hints offer ↑↓, is known once it is laid out.
        self.call_after_refresh(self._show_hints)
        return None if self._loading else self.query_one("#detail-view")

    @staticmethod
    def _failure(state) -> Content:
        return Content.assemble(("✗ Could not load " + state.noun, "bold $text-error"), "\n", (state.error, "$foreground-muted"))

    def _show_selection(self):
        """Update the card and position from the highlighted row's list data."""
        state = self.view
        if state.kind in ("home", "form", "details") or not self._live:
            return
        table, card = self.query_one(BookTable), self.query_one(Card)
        rows, index = state.rows, table.cursor_row
        if card.display and 0 <= index < len(rows):
            card.update(self._card(rows[index], compact=card.has_class("-compact")), layout=False)
        self._show_status()
        self._show_hints()

    @staticmethod
    def _card_lines(state) -> int:
        """Inner card lines: three for books; for booklists, as many as the fullest list needs.

        The height stays the same while the selection moves, so the table never jumps.
        """
        rows = state.rows
        if state.kind != "booklists" or not rows:
            return 3
        return (1 + max(len(description_lines(item.description)) for item in rows)
                + any(booklist_meta(item) for item in rows))

    @staticmethod
    def _card(item, *, compact) -> Content:
        """Title, authors and meta of a book, or title, description and meta of a booklist."""
        if isinstance(item, Booklist):
            title, second, meta = safe_text(item.title) or "Untitled booklist", "", booklist_meta(item)
            extra = [] if compact else description_lines(item.description)
        else:
            title, second, meta = safe_text(item.title) or "Untitled book", safe_text(item.authors), book_meta(item)
            extra = []
        if compact:
            tail = second or meta
            return Content.assemble((title, "bold"), (" — " + tail, "$foreground-muted") if tail else "")
        lines = [Content.styled(title, "bold")]
        lines += [Content.styled(text, "$foreground") for text in (second,) if text]
        lines += [Content.styled(text, "$foreground-muted") for text in (*extra, meta) if text]
        return Content("\n").join(lines)

    def _show_status(self):
        state, status = self.view, self.query_one("#status", Static)
        if state.kind == "form":
            status.update("Set filters, then search", layout=False)
            return
        width = self.size.width - 2
        if self._loading:
            text = loading_text(state)
            if state.kind == "results" and state.search is not None:
                if filters := fit_summary(filters_text(state.search.filters), width - 2 - len(text) - 1):
                    text += " " + filters
            status.update(Content.assemble((SPINNER[self._spin], "$text-accent"), " ", text), layout=False)
            return
        if state.kind == "details":
            status.update(self._breadcrumb(), layout=False)
            return
        table = self.query_one(BookTable)
        lead = position_text(state, table.cursor_row if table.display else 0)
        if state.error is not None:
            lead = f"Page {state.page}" if state.kind in PAGE_KINDS else ""
        parts = [lead] if lead else []
        parts += status_extras(state)
        text = " · ".join(parts)
        # The filters give way first, whole parts from the end; all of them below 12 free cells.
        if state.kind == "results" and state.search is not None and (room := width - len(text) - 3) >= 12:
            if filters := fit_summary(filters_text(state.search.filters), room if text else width):
                text = text + " · " + filters if text else filters
        status.update(text, layout=False)
        # Set only once loaded, so the message does not move while a refresh shows the spinner.
        self.query_one("#message").set_class(not text, "-flush")

    def _breadcrumb(self) -> str:
        """Where the open book came from; the oldest steps give way first."""
        crumbs = [view.crumb for view in self._roots[self._root] if view.kind != "form"]
        for start in range(len(crumbs)):
            line = " › ".join(["…"] * bool(start) + crumbs[start:])
            if len(line) <= self.size.width - 2 or start >= len(crumbs) - 2:
                return line
        return ""

    def _show_hints(self):
        """Contextual key hints; priority 2 items go first when space runs out, then priority 1."""
        if self._live:
            self.query_one(HintBar).show(item for item in self._hints() if item is not None)

    def _hints(self) -> list:
        """The keys of the open dialog, or else of the page and its state."""
        state, dialog, results = self.view, self.screen, self.view.kind == "results"
        back, commands = ("esc", "back", 0), ("/", "commands", 0)
        # The help lists its keys under its text; this line would repeat them, half hidden by the box.
        if self.too_small or isinstance(dialog, HelpScreen):
            return []
        if isinstance(dialog, DownloadDialog):
            return [("enter", "select", 0), ("←→", "choose", 1), ("esc", "cancel", 0)]
        if isinstance(dialog, CommandPalette):
            return [("↑↓", "choose", 0), ("enter", "apply" if dialog.id == THEME_PALETTE else "run", 0), ("esc", "close", 0)]
        if state.kind == "home":
            return [commands, ("s", "search", 0), ("?", "help", 0), ("q", "quit", 0)]
        if state.kind == "form":
            form = self.query_one(SearchForm)
            if form.choosing:
                return [("↑↓", "choose", 0), ("enter", "select", 0), ("esc", "close", 0)]
            first = {"order": ("enter", "choose", 0), "formats": ("space", "toggle", 0)}.get(form.field, ("enter", "search", 0))
            return [first, ("tab", "next field", 1), ("ctrl+s", "search", 2 if first[1] == "search" else 0), back]
        if self._loading and state.value is None:
            return [back, commands]
        # On search results, s (the form, filled in with this search) is the way to change what failed or found nothing.
        edit = ("s", "edit search", 0) if results else None
        if state.error is not None:
            return [edit, ("r", "retry", 0), back, commands]
        if state.kind == "details":
            # As in the help, ↑↓ appears only while the page is taller than the terminal.
            scroll = ("↑↓", "scroll", 2) if self.query_one("#detail-view").max_scroll_y > 0 else None
            return [scroll, self._download_hint(), ("m", "more like this", 0), ("r", "refresh", 1), back, commands]
        if state.kind == "booklists":
            if not state.rows:
                return [("r", "refresh", 0), back, commands]
            return [("↑↓", "select", 2), ("enter", "open", 0), ("r", "refresh", 1), back, commands]
        page = state.value if isinstance(state.value, BookPage) else None
        paging = ("←→", "page", 1) if page is not None and (page.next_page or page.previous_page) else None
        if state.rows:
            return [("↑↓", "select", 2), paging, ("enter", "details", 0), self._download_hint(), ("m", "more like this", 1),
                    edit, back, commands]
        return [paging, edit, ("r", "refresh", 1), back, commands]

    def _download_hint(self):
        if self._downloads is None:
            return None
        return ("x", "cancel download", 1) if self._downloads.active else ("d", "download", 1)

    def on_resize(self):
        # This handler runs before Textual records the new size; render after it.
        self.call_later(self._resized)

    def _resized(self):
        if self._live:
            # The size prompt replaces everything, including an open dialog.
            while self.too_small and len(self.screen_stack) > 1:
                self.pop_screen()
            self._save_position()
            self._render()

    # Navigation

    def _navigate(self, change):
        """Leave the current view, cancelling its read, then show (and load) the next one."""
        before = self.view
        self._save_position()
        self._generation += 1
        self._loader.cancel()
        self._loading = False
        change()
        after = self.view
        if after.kind == "form" and before is not after:
            self._focus_form = True
        if after.kind not in ("home", "form") and after.value is None:
            self._load()
        else:
            self._render()

    def _switch(self, root):
        def change():
            if self._root == "search" and root != "search":
                self._form_open = False
            self._root = root
        self._navigate(change)

    def action_open_root(self, name):
        if name in ROOTS and name != self._root:
            self._switch(name)

    def action_home(self):
        if self._root != "home":
            self._switch("home")

    def action_search(self):
        """Open the Search section: its retained results, or the form."""
        if self._root != "search":
            self._switch("search")
        elif self.view.kind != "form":
            self._navigate(lambda: setattr(self, "_form_open", True))

    def on_search_form_submitted(self, message: SearchForm.Submitted):
        """Replace the Search section's results with page 1 of a submitted search."""
        if self._stopping or self.view.kind != "form":
            return

        def change():
            self._roots["search"] = [self._roots["search"][0], ViewState("search", "results", search=message.request)]
            self._form_open = False
        self._navigate(change)

    def on_search_form_field_focused(self, message: SearchForm.FieldFocused):
        self._show_hints()

    def _open(self, child: ViewState):
        stack = self._roots[self._root]
        if len(stack) >= MAX_DEPTH:
            self._notice("✗ Go back before opening another view.")
            return
        self._navigate(lambda: stack.append(child))

    def _selected_book(self):
        """The reference and the book that m and d would use, if any."""
        state = self.view
        if self._loading or state.error is not None or state.value is None:
            return None
        if state.kind == "details":
            return (state.identifier, state.value) if isinstance(state.value, BookDetails) else None
        rows, index = state.books, self.query_one(BookTable).cursor_row
        if state.kind not in BOOK_KINDS or not 0 <= index < len(rows):
            return None
        reference = book_reference(rows[index])
        return None if reference is None else (reference, rows[index])

    def _similar_target(self) -> tuple[str, str] | None:
        """The reference and title of the book that m would use, if any."""
        selected = self._selected_book()
        return None if selected is None else (selected[0], selected[1].title)

    def _download_target(self) -> DownloadTarget | None:
        selected = self._selected_book()
        if selected is None:
            return None
        reference, book = selected
        return DownloadTarget(reference, book.title, getattr(book, "extension", "") or "",
                              getattr(book, "size", "") or "")

    def action_download(self):
        target = self._download_target()
        if target is None:
            return
        if self._downloads is None:
            self._notice("✗ Downloads are not available in this session.")
            return
        if self._downloads.active:
            self._notice("✗ A download is already running. Press x to cancel it.")
            return

        def confirmed(answer):
            if answer and not self._stopping and not self._downloads.active:
                self._downloads.start(target)
        self.push_screen(DownloadDialog(target, self.download_dir), confirmed)

    def action_cancel_download(self):
        if self._downloads is not None and self._downloads.active:
            self._downloads.cancel()
            self._notice("Cancelling the download…")

    def on_download_progress(self, message: DownloadProgress):
        if not self._live:
            return
        line = self.query_one("#download-line", Static)
        line.update(Content.styled(message.update.text, message.update.style))
        line.display = True
        self._show_hints()

    def action_similar(self):
        target = self._similar_target()
        if target is not None:
            self._open(ViewState(self._root, "similar", identifier=target[0], label=target[1]))

    def action_refresh(self):
        if self.view.kind in ("home", "form"):
            return
        self._save_position()
        self._load()

    def action_back(self):
        if len(self.screen_stack) > 1:
            self.pop_screen()
            return
        if self._root == "home":
            return
        stack = self._roots[self._root]
        if self.view.kind == "form" or len(stack) == 1:
            self._switch("home")
        elif self._root == "search" and len(stack) == 2:
            self._navigate(lambda: setattr(self, "_form_open", True))
        else:
            self._navigate(stack.pop)

    def _page(self, direction):
        state = self.view
        if self._loading or state.error is not None or not isinstance(state.value, BookPage):
            return
        page = state.value.next_page if direction == "next" else state.value.previous_page
        if page is None:
            return
        stack = self._roots[self._root]
        self._navigate(lambda: stack.__setitem__(-1, replace(state, page=page, value=None, error=None, cursor=0,
                                                                 scroll_x=0, scroll_y=0)))

    def action_previous_page(self):
        self._page("previous")

    def action_next_page(self):
        self._page("next")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        self._show_selection()

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        state, index = self.view, event.cursor_row
        if self._loading or state.error is not None:
            return
        if state.kind == "booklists" and 0 <= index < len(state.rows):
            item = state.rows[index]
            self._open(ViewState(self._root, "members", identifier=item.booklist_id, label=item.title))
        elif 0 <= index < len(state.books):
            reference = book_reference(state.books[index])
            if reference is not None:
                self._open(ViewState(self._root, "details", identifier=reference))

    def action_help(self):
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())
