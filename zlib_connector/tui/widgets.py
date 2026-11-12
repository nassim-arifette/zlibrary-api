"""Small shared widgets: the book table, key hints, compact form controls and help."""
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Checkbox, DataTable, Select, Static


SEPARATOR = " · "


def hint_line(items, width: int) -> Content:
    """Key hints that fit in width cells; optional ones go first, then the line ends in …

    Each item is (key, description, priority); priority 0 is kept longest.
    """
    def size(chosen):
        return sum(len(key) + 1 + len(text) for key, text, _ in chosen) + len(SEPARATOR) * max(0, len(chosen) - 1)

    chosen, dropped = list(items), False
    for level in (2, 1):
        while size(chosen) + (len(SEPARATOR) + 1 if dropped else 0) > width and any(p == level for *_, p in chosen):
            index = max(i for i, (*_, p) in enumerate(chosen) if p == level)
            chosen.pop(index)
            dropped = True
    parts = []
    for key, text, _ in chosen:
        if parts:
            parts.append(SEPARATOR)
        parts += [(key, "$foreground"), " " + text]
    if dropped:
        parts.append(SEPARATOR + "…")
    line = Content.assemble(*parts)
    return line if line.cell_length <= width else line.truncate(max(0, width - 1)) + Content("…")


class HintBar(Widget):
    """One line of contextual key hints, truncated by dropping optional items."""

    DEFAULT_CSS = "HintBar { height: 1; padding: 0 1; color: $foreground-muted; }"

    def __init__(self, *, id: str | None = None):
        super().__init__(id=id)
        self._items: tuple = ()

    @property
    def items(self) -> tuple:
        return self._items

    def show(self, items) -> None:
        items = tuple(items)
        if items != self._items:
            self._items = items
            self.refresh()

    def render(self) -> Content:
        return hint_line(self._items, self.content_size.width)


class SectionRule(Widget):
    """A `Title ───── note` section header that fills its width."""

    DEFAULT_CSS = "SectionRule { height: 1; color: $foreground-muted; }"

    def __init__(self, title: str, *, id: str | None = None):
        super().__init__(id=id)
        self._title, self._note = title, ""

    @property
    def note(self) -> str:
        return self._note

    def set_note(self, note: str) -> None:
        if note != self._note:
            self._note = note
            self.refresh()

    def render(self) -> Content:
        fill = max(1, self.content_size.width - len(self._title) - len(self._note) - 2)
        return Content.assemble((self._title, "bold $foreground"), " ", ("─" * fill, "$foreground 25%"), " ", self._note)


class BookTable(DataTable):
    """A row-cursor table: j/k select, Home/End jump, ←/→ change the page."""

    BINDINGS = [Binding("j", "cursor_down", show=False), Binding("k", "cursor_up", show=False),
                Binding("home", "scroll_top", show=False), Binding("end", "scroll_bottom", show=False),
                Binding("left", "app.previous_page", show=False), Binding("right", "app.next_page", show=False)]
    DEFAULT_CSS = """
    BookTable { height: 1fr; background: $background; scrollbar-size: 0 0; }
    BookTable:focus { background-tint: $foreground 0%; }
    BookTable > .datatable--header, BookTable:focus > .datatable--header {
        background: $background; color: $foreground-muted; text-style: none; background-tint: $foreground 0%;
    }
    """

    def __init__(self, *, id: str | None = None):
        super().__init__(id=id, cursor_type="row", zebra_stripes=False)


class Card(Static):
    """The selected item's summary in a rounded accent border."""

    DEFAULT_CSS = """
    Card { height: 5; border: round $accent; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    """


class ActionButton(Static, can_focus=True):
    """A borderless button filled with the accent color while focused."""

    BINDINGS = [Binding("enter,space", "press", show=False)]
    DEFAULT_CSS = """
    ActionButton { width: auto; height: 1; padding: 0 2; background: $surface; color: $text; }
    ActionButton:focus { background: $accent; color: $text; text-style: bold; }
    """

    class Pressed(Message):
        def __init__(self, button: "ActionButton"):
            self.button = button
            super().__init__()

    def __init__(self, label: str, *, id: str | None = None):
        super().__init__(label, id=id, markup=False)

    def action_press(self) -> None:
        self.post_message(self.Pressed(self))

    def on_click(self) -> None:
        self.focus()
        self.action_press()


class FormatBox(Checkbox, inherit_bindings=False):
    """A compact `[x] EPUB` checkbox; Space toggles, ←/→ move along the row, Enter is left to the form."""

    BINDINGS = [Binding("space", "toggle_button", show=False),
                Binding("left", "step(-1)", show=False), Binding("right", "step(1)", show=False)]
    # The focus rule matches Checkbox's compact one; on equal specificity a subclass wins.
    DEFAULT_CSS = """
    FormatBox { background: transparent; margin-right: 2; }
    FormatBox.-textual-compact:focus > .toggle--label { background: $accent; color: $text; text-style: bold; }
    """

    def __init__(self, label: str, value: bool = False, *, id: str | None = None):
        super().__init__(label, value, id=id, compact=True)

    def action_step(self, delta: int) -> None:
        boxes = [child for child in self.parent.children if isinstance(child, FormatBox)]
        index = boxes.index(self) + delta
        if 0 <= index < len(boxes):
            boxes[index].focus()

    @property
    def _button(self) -> Content:
        return Content.assemble(("[", "$foreground-muted"), ("x" if self.value else " ", "bold $text-accent"), ("]", "$foreground-muted"))


class SortSelect(Select, inherit_bindings=False):
    """A one-line choice; Enter or Space opens the list, ↑/↓ move between form fields."""

    BINDINGS = [Binding("enter,space", "show_overlay", show=False)]
    DEFAULT_CSS = """
    SortSelect { width: 24; }
    /* The list opens upward when it does not fit below, and never covers the key hints. */
    SortSelect > SelectOverlay { constrain: none inflect; margin-bottom: 1; }
    SortSelect > SelectCurrent { background: transparent; }
    SortSelect:focus > SelectCurrent { background-tint: $foreground 0%; }
    SortSelect:nocolor:focus > SelectCurrent { text-style: reverse; }
    SortSelect:nocolor SelectCurrent #label { text-style: dim; }
    SortSelect:nocolor SelectCurrent.-has-value #label { text-style: none; }
    """


HELP_SECTIONS = (
    ("Global", (("/ or ctrl+p", "Commands"), ("s or 4", "Search books"), ("f or 1", "Favorites"),
                ("b or 2", "Booklists"), ("h or 3", "Download history"), ("5", "Popular books"),
                ("6", "Recommended for you"), ("? or f1", "This help"),
                ("esc", "Back, or Home from a first page"), ("q or ctrl+c", "Quit"))),
    ("Book lists", (("↑ ↓ or j k", "Select a book"), ("pgup pgdn", "Move by a screen"),
                    ("home end", "First or last book"), ("← → or p n", "Previous or next page"),
                    ("enter", "Book details; open a booklist"), ("m", "More like this"),
                    ("r", "Refresh, or retry after an error"))),
    ("Book details", (("↑ ↓ pgup pgdn", "Scroll"), ("m", "More like this"), ("r", "Refresh"))),
    ("Search form", (("tab shift+tab", "Next or previous field (also ↑ ↓)"), ("enter", "Search; on Sort by, open the list"),
                     ("space", "Toggle a format"), ("ctrl+s", "Search from any field"),
                     ("esc", "Back without searching"))),
)
HELP_TIPS = ("On the search form, letter and digit keys never switch pages or quit; press Esc to leave the form.",
             "Z-Library applies the search filters; with no format checked, PDF, EPUB and DjVu are included.",
             "Download history and recommendations come from your account; recommendations need a "
             "signed-in server.",
             "Changing pages stops waiting for obsolete reads. A request the server already accepted may still finish.")


def help_keys() -> Content:
    lines = []
    for title, keys in HELP_SECTIONS:
        lines.append(Content.styled(title, "bold"))
        lines += [Content.assemble(("  " + key.ljust(15), "$foreground"), (text, "$foreground-muted")) for key, text in keys]
        lines.append(Content(""))
    lines.append(Content.styled("Tips", "bold"))
    return Content("\n").join(lines)


class HelpScreen(ModalScreen):
    """Keys and tips; Esc, ? or F1 close it, arrows and page keys scroll it.

    Its last line names those keys; the app's key hints stay empty meanwhile.
    """

    AUTO_FOCUS = "#help-scroll"
    BINDINGS = [Binding("escape,f1,question_mark", "dismiss", "Close help", priority=True)]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; background: $background 60%; }
    #help-box { width: 72; max-width: 100%; height: auto; background: $surface; padding: 1 2; }
    #help-scroll { height: auto; scrollbar-size-vertical: 1; overflow-x: hidden; scrollbar-background: $surface;
                   scrollbar-background-hover: $surface; scrollbar-background-active: $surface; }
    #help-scroll:focus { background-tint: $foreground 0%; }
    #help-title { height: 1; margin-bottom: 1; color: $text-accent; text-style: bold; }
    .help-tip { height: auto; padding-left: 2; color: $foreground-muted; }
    #help-close { height: 1; margin-top: 1; color: $foreground-muted; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._scrolls = False

    @property
    def hints(self) -> tuple:
        """The keys that apply: ↑↓ only while the help is taller than the terminal allows."""
        return (("↑↓", "scroll", 0),) * self._scrolls + (("esc", "close", 0),)

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static("Keys and tips", id="help-title", markup=False)
            with VerticalScroll(id="help-scroll", can_focus=True):
                yield Static(help_keys(), id="help-text", markup=False)
                for tip in HELP_TIPS:
                    yield Static(tip, classes="help-tip", markup=False)
            yield Static(hint_line(self.hints, 80), id="help-close", markup=False)

    def on_mount(self) -> None:
        self.on_resize()

    def on_resize(self) -> None:
        # Title, close hint, their margins and the box padding take 6 rows.
        self.query_one("#help-scroll").styles.max_height = max(3, self.app.size.height - 6)
        self.call_after_refresh(self._fit)

    def _fit(self) -> None:
        if not self.is_attached:
            return
        scrolls = self.query_one("#help-scroll").max_scroll_y > 0
        if scrolls != self._scrolls:
            self._scrolls = scrolls
            self.query_one("#help-close", Static).update(hint_line(self.hints, 80))
