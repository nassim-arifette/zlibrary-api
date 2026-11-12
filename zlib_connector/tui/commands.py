"""The `/` command dialog: Textual's command palette fed only by these commands."""
from functools import partial

from textual import events
from textual.command import CommandInput, CommandPalette, DiscoveryHit, Hit, Hits, Provider
from textual.widgets import Input
from textual.worker import Worker


# Name, help text and app action. /similar and /refresh appear only where they apply.
COMMANDS = (("/search", "Search books", "search"), ("/favorites", "Saved books", "open_root('favorites')"),
            ("/booklists", "Your booklists", "open_root('booklists')"),
            ("/history", "Download history", "open_root('history')"),
            ("/popular", "Popular books", "open_root('popular')"),
            ("/recommended", "Recommended for you", "open_root('recommended')"),
            ("/similar", "More like this", "similar"), ("/refresh", "Reload this page", "refresh"),
            ("/home", "Home", "home"), ("/help", "Keys and tips", "help"),
            ("/theme", "Change theme", "change_theme"), ("/quit", "Quit", "quit"))


class CommandsProvider(Provider):
    """List the app's commands in a fixed order; typing filters them fuzzily."""

    def _commands(self):
        app = self.app
        for name, help_text, action in COMMANDS:
            if app.command_available(name):
                yield name, help_text, partial(app.run_command, action)

    async def discover(self) -> Hits:
        for name, help_text, callback in self._commands():
            yield DiscoveryHit(name, callback, help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_text, callback in self._commands():
            if (score := matcher.match(name)) > 0:
                yield Hit(score, matcher.highlight(name), callback, text=name, help=help_text)


class CommandDialog(CommandPalette):
    """Textual's command palette, kept in step with keys typed faster than it opens.

    Keys typed straight after `/` (a fast roll, a keyboard macro) can reach the
    dialog before its input has focus; they are typed into it, in order. Enter
    waits until the list matches the typed text, so it never runs an entry of an
    older list. This relies on palette internals of the pinned Textual version.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._enter_waiting = False

    @property
    def _matching(self) -> bool:
        """True while the palette is still gathering the entries for the typed text."""
        return any(worker.node is self and worker.group == self._GATHER_COMMANDS_GROUP and not worker.is_finished
                   for worker in self.workers)

    def on_key(self, event: events.Key) -> None:
        # The focused input takes printable keys itself; these reached the dialog before it had focus.
        if event.is_printable:
            event.stop()
            self.query_one(CommandInput).insert_text_at_cursor(event.character)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._matching:
            event.stop()
            event.prevent_default()
            self._enter_waiting = True

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if self._enter_waiting and self.is_current and not self._matching:
            self._enter_waiting = False
            self._select_or_command()
