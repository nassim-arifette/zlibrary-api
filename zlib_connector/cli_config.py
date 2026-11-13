"""`zlib-cli config`: show and change the settings file. Local only: no network, no credentials, no token."""
import os
from pathlib import Path
import sys

from .cli_output import CommandError, print_json
from .terminal_text import terminal_text


ACTIONS = ("path", "list", "get", "set", "unset")


def add_config_parser(commands):
    command = commands.add_parser("config", help="show and change saved settings (config.toml)",
                                  description="Show and change the settings file. Keys: domain, timeout, download_dir,"
                                              " languages, formats, api_url, credentials_file. Secrets are never"
                                              " stored there.")
    actions = command.add_subparsers(dest="config_action", required=True)
    actions.add_parser("path", help="print the settings file path")
    listing = actions.add_parser("list", help="show every setting and its value")
    listing.add_argument("--show-origin", action="store_true", help="add where each value comes from")
    get = actions.add_parser("get", help="print one setting")
    get.add_argument("key")
    change = actions.add_parser("set", help="save a setting; lists take several values or a comma-separated one")
    change.add_argument("key")
    change.add_argument("values", nargs="+", metavar="VALUE")
    unset = actions.add_parser("unset", help="remove a setting from the file")
    unset.add_argument("key")
    return command


def _out(text):
    print(terminal_text(text, multiline=True), flush=True)


def _note(text):
    try:
        print(terminal_text(text), file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _json_value(key, value):
    from .settings import toml_form
    if value is None:
        return [] if key in ("languages", "formats") else None
    return toml_form(key, value)


def _text_value(key, value) -> str:
    if value is None or (isinstance(value, (list, tuple)) and not value):
        return "(not set)"
    if isinstance(value, (list, tuple)):
        return ", ".join(value)
    if key == "timeout":
        return format(float(value), "g")
    return str(value)


def _origin_entry(settings, key):
    origin, detail = settings.origins.get(key, ("default", None))
    entry = {"value": _json_value(key, getattr(settings, key)), "origin": origin}
    if origin == "cli":
        entry["option"] = detail
    elif origin == "env":
        entry["variable"] = detail
    return entry


def _origin_text(settings, key) -> str:
    origin, detail = settings.origins.get(key, ("default", None))
    return f"{origin} {detail}" if detail else origin


def _load(args):
    from .cli_settings import load_for
    return load_for(args, missing_ok=True)


def _credentials_location(settings):
    from .credentials import default_credentials_file, legacy_credentials_file
    candidates = [settings.credentials_file] if settings.credentials_file is not None else [
        default_credentials_file(), legacy_credentials_file()]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate, True
        except OSError:
            continue
    return candidates[0], False


def run_config(args, *, environ=None) -> int:
    from .settings import check_key
    if getattr(args, "no_config", False):
        raise CommandError("The config commands manage the settings file; omit --no-config.")
    action = args.config_action
    if action in ("get", "set", "unset"):
        check_key(args.key, source="cli")
    if action == "path":
        return _path(args)
    if action == "list":
        return _list(args)
    if action == "get":
        return _get(args)
    return _change(args, environ=os.environ if environ is None else environ)


def _path(args):
    from .settings import config_file_path, read_bytes
    path, source = config_file_path(getattr(args, "config", None))
    exists = read_bytes(path) is not None
    from .cli_settings import load_for
    try:
        settings = load_for(args, missing_ok=True)
    except Exception:
        settings = None  # config path works even when the file is invalid.
    if settings is not None:
        credentials, credentials_exists = _credentials_location(settings)
    else:
        from .credentials import default_credentials_file
        credentials = default_credentials_file()
        credentials_exists = credentials.is_file()
    if args.json:
        print_json({"path": str(path), "exists": exists, "source": source, "credentials_path": str(credentials),
                    "credentials_exists": credentials_exists})
        return 0
    _out(str(path))
    if not exists:
        _note("(not created yet; zlib-cli config set creates it)")
    return 0


def _list(args):
    from .settings import KEYS
    settings = _load(args)
    if args.json:
        print_json({"path": str(settings.config_path), "exists": settings.config_exists,
                    "settings": {key: _origin_entry(settings, key) for key in KEYS}})
        return 0
    rows = [(key, _text_value(key, getattr(settings, key)), _origin_text(settings, key)) for key in KEYS]
    width = max(len(row[0]) for row in rows) + 2
    value_width = max(len(row[1]) for row in rows) + 3
    for key, value, origin in rows:
        line = key.ljust(width) + (value.ljust(value_width) + origin if args.show_origin else value)
        _out(line.rstrip())
    return 0


def _get(args):
    settings = _load(args)
    key = args.key
    value = getattr(settings, key)
    if args.json:
        entry = _origin_entry(settings, key)
        print_json({"key": key, **entry})
        return 0
    if value is None:
        return 0
    if isinstance(value, (list, tuple)):
        for item in value:
            _out(item)
    else:
        _out(_text_value(key, value))
    return 0


def _new_value(key, values):
    from .settings import LIST_KEYS, SettingsError, parse_text, toml_form, validate
    if key not in LIST_KEYS and len(values) != 1:
        raise CommandError(f"{key} takes exactly one value.")
    try:
        raw = parse_text(key, values if key in LIST_KEYS else values[0])
    except (ValueError, TypeError):
        raise SettingsError(f"Invalid value for {key}.", code="invalid_argument", key=key, source="cli") from None
    if key in LIST_KEYS and not raw:
        raise CommandError(f"Give at least one value for {key}; zlib-cli config unset {key} clears it.")
    try:
        value = validate(key, raw, source="cli")
    except (ValueError, TypeError):
        raise SettingsError(f"Invalid value for {key}.", code="invalid_argument", key=key, source="cli") from None
    if key == "download_dir" and not value.is_dir():
        raise SettingsError("Choose an existing folder for download_dir.", code="invalid_argument", key=key,
                            source="cli")
    return toml_form(key, value)


def _change(args, *, environ):
    from .settings import UNSET, VARIABLES, ConfigStore, config_file_path
    key = args.key
    value = _new_value(key, args.values) if args.config_action == "set" else UNSET
    path, source = config_file_path(getattr(args, "config", None), environ=environ)
    change = ConfigStore(path, source=source).update(key, value)
    if args.config_action == "set":
        if args.json:
            print_json({"key": key, "value": change.value, "previous": change.previous, "path": str(path)})
        else:
            _out(f"Set {key} = {_text_value(key, change.value)} in {path}.")
    else:
        removed = change.changed
        if args.json:
            print_json({"key": key, "removed": removed, "previous": change.previous, "path": str(path)})
        elif removed:
            _out(f"Removed {key} from {path}.")
        else:
            _out(f"{key} was not set in {path}.")
    if change.dropped_comments:
        _note("Note: comments in the config file were not kept.")
    variable = VARIABLES[key]
    if (environ.get(variable) or "").strip():
        _note(f"Note: {variable} is set and overrides this value in this environment.")
    if args.config_action == "set" and key == "domain":
        _session_note(args, change.value)
    return 0


def _session_note(args, domain):
    """Warn when the saved session belongs to another domain. Reads the session file only; never fails."""
    if getattr(args, "no_session", False):
        return
    try:
        from .sessions import SessionStore
        saved = SessionStore(args.session_file).read().session
    except Exception:
        return
    if saved is not None and saved.origin != domain:
        _note(f"Note: the saved session belongs to {saved.origin}. Account commands will stop with an error until"
              " you run zlib-cli login.")
