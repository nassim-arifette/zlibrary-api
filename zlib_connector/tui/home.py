"""The start page: a title and the commands that open each section, without any read."""
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.widgets import Static


# Two columns, discovery then library; one column below TWO_COLUMNS cells.
HOME_COLUMNS = ((("/search", "Search books", "search"), ("/popular", "Popular books", "open_root('popular')"),
                 ("/recommended", "Recommended", "open_root('recommended')")),
                (("/favorites", "Saved books", "open_root('favorites')"),
                 ("/booklists", "Your booklists", "open_root('booklists')"),
                 ("/history", "Download history", "open_root('history')")))
TWO_COLUMNS = 76
NAME_WIDTH = 15


class HomeEntry(Static):
    """A command and its description; clicking it runs the command."""

    def __init__(self, name: str, description: str, action: str):
        super().__init__(Content.assemble((name.ljust(NAME_WIDTH), "bold $text-accent"), (description, "$foreground-muted")),
                         markup=False, classes="home-entry")
        self.command_name, self.action = name, action

    async def on_click(self) -> None:
        await self.app.run_action(self.action)


class HomePage(Vertical):
    DEFAULT_CSS = """
    HomePage { height: 1fr; align: center middle; }
    #home-block { width: auto; height: auto; }
    #home-title { width: 100%; height: 1; text-align: center; text-style: bold; color: $text-accent; }
    #home-commands { width: auto; height: auto; margin-top: 2; }
    #home-commands.-compact { margin-top: 1; }
    #home-commands.-narrow { layout: vertical; }
    .home-column { width: auto; height: auto; }
    #home-right { margin-left: 4; }
    #home-commands.-narrow #home-right { margin-left: 0; }
    .home-entry { width: auto; height: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="home-block"):
            yield Static("Z-Library", id="home-title", markup=False)
            with Horizontal(id="home-commands"):
                for column, entries in zip(("home-left", "home-right"), HOME_COLUMNS):
                    with Vertical(id=column, classes="home-column"):
                        for entry in entries:
                            yield HomeEntry(*entry)

    def fit(self, width: int, height: int) -> None:
        commands = self.query_one("#home-commands")
        commands.set_class(width < TWO_COLUMNS, "-narrow")
        commands.set_class(height < 16, "-compact")
