"""What the TUI settings page shows and how it saves, without Textual: validation, then one write per key."""
from dataclasses import dataclass, replace
from pathlib import Path

from ..settings import _DEFAULTS, UNSET, VARIABLES, SettingsError, parse_text, toml_form, validate


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    help: str
    restart: bool = False  # used by the running server, so it applies at the next start


FIELDS = (
    Field("download_dir", "Download folder", "Where books are saved. Empty: your Downloads folder."),
    Field("confirm_downloads", "Ask first", "Ask for a confirmation before each download."),
    Field("domain", "Domain", "The Z-Library site, e.g. https://z-lib.by. Empty: the saved session's.", True),
    Field("timeout", "Timeout", "Seconds to wait for Z-Library, up to 300. Empty: 30.", True),
    Field("proxy", "Proxy", "http://HOST:PORT for the site's traffic. Empty: connect directly.", True),
)
TEXT_KEYS = tuple(field.key for field in FIELDS if field.key != "confirm_downloads")


def lock_reason(settings, key) -> str | None:
    """Why a field cannot be changed here: a command-line option or an environment variable sets it."""
    origin = settings.origin(key)
    if origin == "env":
        return f"Set by {VARIABLES[key]}; change or unset it there."
    if origin == "cli":
        return f"Set by {settings.source_label(key)} for this run."
    return None


def shown(settings, key) -> str:
    """The value as the page's text field shows it; empty when unset."""
    value = getattr(settings, key)
    if value is None:
        return ""
    if key == "timeout":
        return format(float(value), "g")
    return str(value)


def file_value(settings, key) -> str:
    """The text a locked-free field starts with: the file's value, not a default."""
    return shown(settings, key) if settings.origin(key) == "config" else ""


def prepare(settings, texts: dict, confirm: bool) -> dict:
    """Validate the page; return {key: TOML value or UNSET} for what changed. Raise SettingsError naming the field."""
    changes = {}
    for key in TEXT_KEYS:
        if key not in texts or lock_reason(settings, key) is not None:
            continue
        text = texts[key].strip()
        if text == file_value(settings, key):
            continue
        if not text:
            changes[key] = UNSET
            continue
        label = next(field.label for field in FIELDS if field.key == key)
        try:
            value = validate(key, parse_text(key, text), source="cli")
        except (ValueError, TypeError):
            raise SettingsError(f"Invalid {label.lower()}.", code="invalid_argument", key=key, source="cli") from None
        if key == "download_dir" and not value.is_dir():
            raise SettingsError("Choose an existing folder.", code="invalid_argument", key=key, source="cli")
        changes[key] = toml_form(key, value)
    if lock_reason(settings, "confirm_downloads") is None and confirm != settings.confirm_downloads:
        changes["confirm_downloads"] = confirm
    return changes


def save(store, settings, changes: dict):
    """Write each change under the file's lock; return the settings as they are now."""
    values, origins = {}, dict(settings.origins)
    for key, value in changes.items():
        store.update(key, value)
        if value is UNSET:
            values[key], origins[key] = _DEFAULTS[key], ("default", None)
        else:
            values[key] = Path(value) if key == "download_dir" else validate(key, value)
            origins[key] = ("config", None)
    return replace(settings, **values, origins=origins, config_exists=settings.config_exists or bool(changes))
