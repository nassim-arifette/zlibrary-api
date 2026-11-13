"""The settings page: the config file's values for the TUI, saved with the same rules as zlib-cli config."""
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, Static

from ..settings import SettingsError
from .settings_model import FIELDS, file_value, lock_reason, prepare, save
from .views import safe_text
from .widgets import ActionButton


class SettingsScreen(ModalScreen):
    """Edit download folder, confirmation, domain, timeout and proxy. Esc leaves without saving.

    Dismisses with the new Settings after a save, or None. ``store`` None (--no-config) shows the
    values read-only.
    """

    AUTO_FOCUS = None
    BINDINGS = [Binding("escape", "dismiss(None)", "Close", priority=True),
                Binding("ctrl+s", "save", "Save", show=False, priority=True),
                Binding("down", "app.focus_next", show=False), Binding("up", "app.focus_previous", show=False)]
    DEFAULT_CSS = """
    SettingsScreen { align: center middle; background: $background 60%; }
    #settings-box { width: 76; max-width: 100%; height: auto; background: $surface; padding: 1 2; }
    #settings-title { height: 1; margin-bottom: 1; color: $text-accent; text-style: bold; }
    .settings-row { height: 1; }
    .settings-row:focus-within { background: $boost; }
    .settings-label { width: 17; color: $foreground-muted; }
    .settings-row Input { height: 1; width: 1fr; padding: 0; border: none; background: transparent; }
    .settings-row Input:focus { background-tint: $foreground 0%; }
    .settings-row Input:disabled { color: $foreground-muted; }
    .settings-row Input > .input--placeholder { color: $foreground-muted 60%; text-style: italic; }
    .settings-row Checkbox { background: transparent; }
    #settings-help { height: auto; margin-top: 1; color: $foreground-muted; }
    #settings-message { height: auto; margin-top: 1; color: $foreground-muted; }
    #settings-message.-error { color: $text-error; }
    #settings-message.-done { color: $text-success; }
    #settings-buttons { height: 1; margin-top: 1; }
    #settings-buttons ActionButton { margin-right: 2; }
    """

    def __init__(self, settings, store, *, where: str = ""):
        super().__init__()
        self.settings, self.store, self.where = settings, store, where

    def compose(self) -> ComposeResult:
        settings = self.settings
        with Vertical(id="settings-box"):
            yield Static("Settings", id="settings-title", markup=False)
            for field in FIELDS:
                locked = self.store is None or lock_reason(settings, field.key) is not None
                with Horizontal(classes="settings-row", id="row-" + field.key):
                    yield Static(field.label, classes="settings-label", markup=False)
                    if field.key == "confirm_downloads":
                        yield Checkbox("Before each download", settings.confirm_downloads, compact=True,
                                       id="settings-confirm_downloads", disabled=locked)
                    else:
                        value = file_value(settings, field.key) if not locked else safe_text(_current(settings, field.key))
                        yield Input(value, placeholder=_placeholder(settings, field.key), compact=True,
                                    id="settings-" + field.key, disabled=locked)
            yield Static("", id="settings-help", markup=False)
            yield Static(self._intro(), id="settings-message", markup=False)
            with Horizontal(id="settings-buttons"):
                if self.store is not None:
                    yield ActionButton("Save", id="settings-save")
                yield ActionButton("Close" if self.store is None else "Cancel", id="settings-cancel")

    def _intro(self) -> str:
        if self.store is None:
            return "Read-only: zlib-cli was started with --no-config."
        return f"File: {safe_text(self.where)}" if self.where else ""

    def on_mount(self) -> None:
        first = next((widget for widget in self.query("Input, Checkbox") if not widget.disabled), None)
        (first or self.query_one("#settings-cancel")).focus()

    def on_descendant_focus(self, event) -> None:
        widget = event.widget
        key = (widget.id or "").removeprefix("settings-")
        field = next((field for field in FIELDS if field.key == key), None)
        help_text = ""
        if field is not None:
            help_text = lock_reason(self.settings, key) or field.help
            if field.restart and lock_reason(self.settings, key) is None:
                help_text += " Applies the next time you start the TUI."
        self.query_one("#settings-help", Static).update(help_text)

    def on_input_submitted(self, message: Input.Submitted) -> None:
        self.action_save()

    def on_action_button_pressed(self, message: ActionButton.Pressed) -> None:
        if message.button.id == "settings-save":
            self.action_save()
        else:
            self.dismiss(None)

    def _message(self, text: str, kind: str) -> None:
        widget = self.query_one("#settings-message", Static)
        widget.update(text)
        widget.set_class(kind == "error", "-error")
        widget.set_class(kind == "done", "-done")

    def action_save(self) -> None:
        if self.store is None:
            return
        texts = {field.key: self.query_one("#settings-" + field.key, Input).value
                 for field in FIELDS if field.key != "confirm_downloads"}
        confirm = self.query_one("#settings-confirm_downloads", Checkbox).value
        try:
            changes = prepare(self.settings, texts, confirm)
            if not changes:
                self.dismiss(None)
                return
            updated = save(self.store, self.settings, changes)
        except SettingsError as error:
            self._message("✗ " + safe_text(str(error)), "error")
            if error.key:
                try:
                    self.query_one("#settings-" + error.key).focus()
                except Exception:
                    pass
            return
        self.dismiss((updated, sorted(changes)))


def _current(settings, key) -> str:
    value = getattr(settings, key)
    if value is None:
        return "(not set)"
    if key == "timeout":
        return format(float(value), "g")
    return str(value)


def _placeholder(settings, key) -> str:
    return {"download_dir": "Downloads folder", "domain": "From the saved session", "timeout": "30",
            "proxy": "None"}.get(key, "")
