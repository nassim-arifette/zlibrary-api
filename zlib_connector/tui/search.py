"""The search page, validated synchronously before the app starts any local read."""
import re

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Checkbox, Input, Select, Static

from ..catalog_api import search_parameters
from ..search import SearchFilters
from .views import FORMATS, ORDERS, SearchRequest
from .widgets import ActionButton, FormatBox, SectionRule, SortSelect


FIELD_HELP = {"query": "Title, author or ISBN.", "years": "Publication years, inclusive. Leave empty for any.",
              "languages": "Comma-separated, e.g. english, french. Leave empty for any.",
              "formats": "Space toggles. None checked means PDF, EPUB and DjVu.",
              "order": "Order chosen by Z-Library. Default keeps the server's order.",
              "submit": "Search with this text and these filters."}
# The rows ↑/↓ move between, top to bottom.
ROWS = ("query", "year-from", "year-to", "languages", "formats", "order", "submit")
EMPTY_QUERY = "Type a title, author or ISBN."
# Filter messages worded for the Languages field; any other message is shown as SearchFilters gives it.
LANGUAGE_ERRORS = {"Filter values must contain 1–64 printable characters.":
                   "Each language must be 1–64 printable characters."}


def _year(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    if re.fullmatch(r"[0-9]{1,4}", text) is None:
        raise ValueError("Years must be integers between 1 and 9999.")
    return int(text)


def _field(widget) -> str:
    """The help key of a form control, or of the control containing it (the open sort list)."""
    for node in (widget, *widget.ancestors):
        if (name := getattr(node, "id", None) or "").startswith("search-"):
            name = name.removeprefix("search-")
            return "years" if name.startswith("year") else "formats" if name.startswith("format") else name
    return ""


class FieldScroll(VerticalScroll, inherit_bindings=False):
    """Scrolls to the focused field; the arrow keys belong to the form."""


class FieldInput(Input):
    """A form field whose ↑/↓ move between rows (Input would scroll itself)."""

    BINDINGS = [Binding("up", "row(-1)", show=False), Binding("down", "row(1)", show=False)]

    def action_row(self, delta: int) -> None:
        form = next((node for node in self.ancestors if isinstance(node, SearchForm)), None)
        if form is not None:
            form.action_row(delta)


class SearchForm(Vertical):
    """Query, filters and the Search action on one page.

    Inline errors appear under the field they concern and clear when it
    changes. A valid form posts Submitted; nothing here reads from the API.
    """

    BINDINGS = [Binding("ctrl+s", "submit", "Search", show=False), Binding("enter", "submit", show=False),
                Binding("up", "row(-1)", show=False), Binding("down", "row(1)", show=False)]
    DEFAULT_CSS = """
    SearchForm { height: 1fr; padding: 0 1; }
    /* A scrollbar appears only when the fields overflow, e.g. with errors in a short terminal. */
    #search-fields { height: 1fr; max-width: 100; scrollbar-size: 0 1; scrollbar-background: $background;
                     scrollbar-background-hover: $background; scrollbar-background-active: $background; }
    .form-heading { height: 1; margin-top: 1; color: $text; text-style: bold; }
    SearchForm.-compact .form-heading, SearchForm.-compact #search-help { margin-top: 0; }
    #search-query { height: 3; border: round $foreground 30%; padding: 0 1; background: transparent; }
    #search-query:focus { border: round $accent; background-tint: $foreground 0%; }
    #search-query:nocolor:focus { border: heavy $accent; }
    #filters-rule { margin-top: 1; }
    .form-row { height: 1; }
    .form-row:focus-within { background: $boost; }
    .marker { width: 2; color: $text-accent; text-style: bold; visibility: hidden; }
    .form-row:focus-within > .marker { visibility: visible; }
    .field-label { width: 12; color: $foreground-muted; }
    .form-row Input { height: 1; padding: 0; border: none; background: transparent; }
    .form-row Input:focus { background-tint: $foreground 0%; }
    #search-year-from, #search-year-to { width: 10; }
    #search-languages { width: 1fr; }
    #row-submit { margin-top: 1; }
    .field-error { height: auto; padding-left: 14; color: $text-error; display: none; }
    #search-query-error { padding-left: 1; }
    .field-error.-shown { display: block; }
    #search-help { height: 1; margin-top: 1; color: $foreground-muted; text-wrap: nowrap; text-overflow: ellipsis; }
    Input:nocolor > .input--placeholder { text-style: dim; }
    """

    class Submitted(Message):
        def __init__(self, request: SearchRequest):
            self.request = request
            super().__init__()

    class FieldFocused(Message):
        """The focused field changed; the app updates its key hints."""

    def __init__(self, *, page_size: int = 20, id: str | None = None):
        super().__init__(id=id)
        self._page_size = page_size

    def compose(self) -> ComposeResult:
        with FieldScroll(id="search-fields", can_focus=False):
            yield Static("Query", classes="form-heading", markup=False)
            yield FieldInput(placeholder="Title, author or ISBN", max_length=500, id="search-query")
            yield Static("", id="search-query-error", classes="field-error", markup=False)
            yield SectionRule("Filters", id="filters-rule")
            for name, label in (("from", "Year from"), ("to", "Year to")):
                with self._row("year-" + name, label):
                    yield FieldInput(placeholder="Any", type="integer", restrict=r"[0-9]*", max_length=4,
                                     valid_empty=True, id="search-year-" + name, compact=True)
            yield Static("", id="search-years-error", classes="field-error", markup=False)
            with self._row("languages", "Languages"):
                yield FieldInput(placeholder="Any — e.g. english, french", max_length=4096, id="search-languages",
                                 compact=True)
            yield Static("", id="search-languages-error", classes="field-error", markup=False)
            with self._row("formats", "Formats"):
                for value, label in FORMATS:
                    yield FormatBox(label, id="search-format-" + value)
            with self._row("order", "Sort by"):
                yield SortSelect([(label, value) for value, label in ORDERS], prompt="Default",
                                 id="search-order", compact=True)
            with Horizontal(id="row-submit", classes="form-row"):
                yield Static(">", classes="marker", markup=False)
                yield ActionButton("Search", id="search-submit")
        yield Static("", id="search-help", markup=False)

    @staticmethod
    def _row(name: str, label: str) -> Horizontal:
        row = Horizontal(id="row-" + name, classes="form-row")
        row.compose_add_child(Static(">", classes="marker", markup=False))
        row.compose_add_child(Static(label, classes="field-label", markup=False))
        return row

    def on_mount(self):
        self.load(None)

    def fit(self, height: int) -> None:
        """Drop the spacing above Query and the help line so the form fits short terminals."""
        self.set_class(height < 20, "-compact")

    def _text(self, name: str) -> str:
        return self.query_one("#search-" + name, Input).value

    def load(self, request: SearchRequest | None) -> None:
        """Show a search's values, discarding anything typed since."""
        request = request or SearchRequest("")
        filters = request.filters
        for name, value in (("query", request.query), ("languages", ", ".join(filters.languages)),
                            ("year-from", "" if filters.year_from is None else str(filters.year_from)),
                            ("year-to", "" if filters.year_to is None else str(filters.year_to))):
            self.query_one("#search-" + name, Input).value = value
        for value, _ in FORMATS:
            self.query_one("#search-format-" + value, Checkbox).value = value in filters.formats
        order = self.query_one("#search-order", Select)
        if filters.order is None:
            order.clear()
        else:
            order.value = filters.order
        self.show_errors({})
        self._count()

    @property
    def active_filters(self) -> int:
        return (sum(bool(self._text(name).strip()) for name in ("year-from", "year-to", "languages"))
                + any(box.value for box in self.query(FormatBox))
                + (not self.query_one("#search-order", Select).is_blank()))

    def _count(self) -> None:
        active = self.active_filters
        self.query_one("#filters-rule", SectionRule).set_note(f"{active} active" if active else "none set")

    def show_errors(self, errors: dict[str, str]) -> None:
        for name in ("query", "years", "languages"):
            self._error(name, errors.get(name, ""))

    def _error(self, name: str, message: str) -> None:
        error = self.query_one(f"#search-{name}-error", Static)
        error.update(message)
        error.set_class(bool(message), "-shown")

    def errors(self) -> dict[str, str]:
        return {name: str(self.query_one(f"#search-{name}-error", Static).render())
                for name in ("query", "years", "languages")
                if self.query_one(f"#search-{name}-error").has_class("-shown")}

    def read(self) -> tuple[SearchRequest | None, dict[str, str]]:
        """Validate every field; return the request, or the errors by field."""
        errors, query = {}, self._text("query")
        try:
            search_parameters(query, page=1, limit=self._page_size)
        except ValueError as error:
            errors["query"] = EMPTY_QUERY if not query.strip() else str(error)
        years = (None, None)
        try:
            years = (_year(self._text("year-from")), _year(self._text("year-to")))
            SearchFilters(year_from=years[0], year_to=years[1])
        except ValueError as error:
            errors["years"] = str(error)
        languages = tuple(part.strip() for part in self._text("languages").split(",") if part.strip())
        try:
            SearchFilters(languages=languages)
        except ValueError as error:
            errors["languages"] = LANGUAGE_ERRORS.get(str(error), str(error))
        if errors:
            return None, errors
        order = self.query_one("#search-order", Select).value
        filters = SearchFilters(languages=languages, year_from=years[0], year_to=years[1],
                                formats=tuple(value for value, _ in FORMATS
                                              if self.query_one("#search-format-" + value, Checkbox).value),
                                order=None if order is Select.NULL else order)
        try:
            # Checked at the longest page number, so paging never makes the request too long later.
            pairs = search_parameters(query, page=1000, limit=self._page_size, filters=filters)
        except ValueError as error:
            return None, {"query": str(error)}
        return SearchRequest(pairs[0][1], filters), {}

    def action_submit(self) -> None:
        request, errors = self.read()
        self.show_errors(errors)
        if request is None:
            first = next(name for name in ("query", "years", "languages") if name in errors)
            self.query_one({"query": "#search-query", "years": "#search-year-from",
                            "languages": "#search-languages"}[first]).focus()
            return
        self.post_message(self.Submitted(request))

    def action_row(self, delta: int) -> None:
        focused = self.screen.focused
        current = _field(focused) if focused is not None else "query"
        current = {"years": (focused.id or "").removeprefix("search-")}.get(current, current)
        index = ROWS.index(current) + delta if current in ROWS else 0
        if 0 <= index < len(ROWS):
            target = ROWS[index]
            self.query_one("#search-format-pdf" if target == "formats" else "#search-" + target).focus()

    @property
    def field(self) -> str:
        """The help key of the focused control."""
        focused = self.screen.focused
        return _field(focused) if focused is not None and self in focused.ancestors else "query"

    @property
    def choosing(self) -> bool:
        """True while the sort list is open."""
        return self.query_one("#search-order", Select).expanded

    def on_descendant_focus(self) -> None:
        # The open sort list may cover the help line; leave no fragments beside it.
        self.query_one("#search-help", Static).update("" if self.choosing else FIELD_HELP.get(self.field, ""))
        self.post_message(self.FieldFocused())

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        field = _field(event.input)
        if field in ("query", "years", "languages"):
            self._error(field, "")
        self._count()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        event.stop()
        self._count()

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        self._count()

    def on_action_button_pressed(self, event: ActionButton.Pressed) -> None:
        event.stop()
        self.action_submit()
